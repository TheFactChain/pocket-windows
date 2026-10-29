import importlib.util
import ast
import contextlib
import io
import json
import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/gateway_controller.py'
spec = importlib.util.spec_from_file_location('gateway_controller', SCRIPT)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


class GatewayControllerTests(unittest.TestCase):
    def setUp(self):
        self.session = {'nonce': 'a' * 32, 'revision': 'b' * 32, 'gateway_version': '2026.10.20.1'}
        self.status = controller.SessionStatus(self.session)

    def event(self, name, **changes):
        return {'event': name, **self.session, **changes}

    @contextlib.contextmanager
    def startup_fixture(self):
        import gateway_package
        import portable_app
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as held:
            root = Path(temporary)
            engine = root / 'runtime/qemu/qemu-system-x86_64.exe'
            engine.parent.mkdir(parents=True)
            engine.write_bytes(b'fixture engine')
            engine.with_name('qemu-img.exe').write_bytes(b'fixture image tool')
            held.enter_context(mock.patch.object(portable_app, 'ENGINE_SHA256', hashlib.sha256(b'fixture engine').hexdigest()))
            held.enter_context(mock.patch.object(controller, 'QEMU_IMG_SHA256', hashlib.sha256(b'fixture image tool').hexdigest()))
            held.enter_context(mock.patch.object(controller, 'load_settings', return_value={}))
            held.enter_context(mock.patch.object(controller.runtime, 'engine_path', return_value=engine))
            held.enter_context(mock.patch.object(controller.runtime, 'engine_processes', return_value={}))
            held.enter_context(mock.patch.object(controller.runtime, 'process_identity', return_value={'pid': 123}))
            held.enter_context(mock.patch.object(gateway_package, 'open_current', return_value=contextlib.nullcontext(
                {'version': '1', 'disk': root / 'gateway/base.qcow2', 'sha256': 'a' * 64})))
            session = self.session | {'endpoint_ipv4': '203.0.113.7'}
            held.enter_context(mock.patch.object(controller, 'make_session', return_value=({'profile_id': 'c' * 32}, session)))
            held.enter_context(mock.patch.object(controller, 'select_uplink', return_value={'interface_index': 4}))
            def create_disk(argv, **kwargs):
                Path(argv[-1]).write_bytes(b'owned temporary disk')
                return controller.subprocess.CompletedProcess(argv, 0)
            held.enter_context(mock.patch.object(controller.subprocess, 'run', side_effect=create_disk))
            spawn = held.enter_context(mock.patch.object(controller.subprocess, 'Popen'))
            yield root, root / 'logs' / ('guest-gateway-boot-' + self.session['nonce']), spawn

    def test_failed_carrier_creation_removes_the_owned_disk_and_records_exit(self):
        with self.startup_fixture() as (root, directory, spawn), \
                mock.patch.object(controller, 'create_carrier', side_effect=RuntimeError('fixture ACL failure')):
            with self.assertRaisesRegex(RuntimeError, 'fixture ACL failure'):
                controller.run(root)
            self.assertFalse((directory / 'gateway.qcow2').exists())
            state = json.loads((directory / 'state.json').read_text())
            self.assertTrue(state['overlay_removed'])
            self.assertEqual(state['status'], 'exited')
            spawn.assert_not_called()

    def test_early_stop_after_disk_preparation_never_spawns_gateway(self):
        with self.startup_fixture() as (root, directory, spawn), \
                mock.patch.object(controller, 'create_carrier') as carrier:
            def prepare(argv, **kwargs):
                Path(argv[-1]).write_bytes(b'owned image')
                (directory / 'control-runtime-cancel.json').write_text('{"action":"powerdown"}')
            with mock.patch.object(controller.subprocess, 'run', side_effect=prepare):
                self.assertEqual(controller.run(root), 1)
            spawn.assert_not_called()
            carrier.assert_not_called()
            state = json.loads((directory / 'state.json').read_text())
            self.assertTrue(state['stop_requested'])
            self.assertTrue(state['overlay_removed'])
            self.assertFalse((directory / 'gateway.qcow2').exists())
            self.assertEqual(state['status'], 'exited')

    def test_failed_first_state_write_still_removes_the_owned_disk(self):
        with self.startup_fixture() as (root, directory, spawn), \
                mock.patch.object(controller.runtime, 'atomic_json', side_effect=OSError('fixture disk full')), \
                mock.patch.object(controller, 'create_carrier') as create:
            with self.assertRaises(OSError):
                controller.run(root)
            self.assertFalse((directory / 'gateway.qcow2').exists())
            create.assert_not_called()
            spawn.assert_not_called()

    def test_image_creation_failure_after_partial_write_removes_only_its_owned_file(self):
        with self.startup_fixture() as (root, directory, spawn), \
                mock.patch.object(controller, 'create_carrier') as create:
            def partial_disk(argv, **kwargs):
                Path(argv[-1]).write_bytes(b'partial qcow header')
                raise controller.subprocess.CalledProcessError(1, argv)
            with mock.patch.object(controller.subprocess, 'run', side_effect=partial_disk), \
                    self.assertRaises(controller.subprocess.CalledProcessError):
                controller.run(root)
            self.assertFalse((directory / 'gateway.qcow2').exists())
            state = json.loads((directory / 'state.json').read_text())
            self.assertTrue(state['overlay_removed'])
            self.assertEqual(state['status'], 'exited')
            create.assert_not_called()
            spawn.assert_not_called()

    def test_stale_ready_never_opens_network_or_enables_windows(self):
        for field in self.session:
            self.assertIsNone(self.status.accept(self.event('FIREWALL_READY', **{field: 'stale'}), 1))
        self.assertFalse(self.status.firewall_ready)
        self.assertIsNone(self.status.accept(self.event('READY'), 1))
        self.assertFalse(self.status.healthy(1))
        qmp = mock.Mock()
        with self.assertRaises(RuntimeError):
            controller.attach_network(qmp, self.status, 31861, 4)
        qmp.command.assert_not_called()

    def test_firewall_ack_precedes_nics_then_fresh_ready_and_health(self):
        self.assertEqual(self.status.accept(self.event('FIREWALL_READY'), 1), 'FIREWALL_READY')
        qmp = mock.Mock()
        controller.attach_network(qmp, self.status, 31861, 4)
        calls = qmp.command.call_args_list
        self.assertEqual([call.args[0] for call in calls], ['netdev_add', 'netdev_add', 'device_add', 'device_add'])
        self.assertEqual(calls[0].args[1]['addr']['host'], '127.0.0.1')
        self.assertEqual(calls[1].args[1]['x-wcp-outbound-if'], 4)
        self.assertNotIn('hostfwd', json.dumps([c.args for c in calls]))
        self.assertEqual(self.status.accept(self.event('READY'), 2), 'READY')
        self.assertTrue(self.status.healthy(30))
        self.assertFalse(self.status.healthy(48))
        self.assertEqual(self.status.accept(self.event('HEALTHY'), 40), 'HEALTHY')
        self.assertTrue(self.status.healthy(48))
        self.status.accept(self.event('ERROR', code='SESSION_FAILED'), 50)
        self.assertFalse(self.status.healthy(50))
        self.assertIsNone(self.status.accept(self.event('READY'), 51))

    def test_invalid_direct_interface_never_attaches_network(self):
        self.status.accept(self.event('FIREWALL_READY'), 0)
        for index in (True, 0, -1, 2**24, '4'):
            qmp = mock.Mock()
            with self.subTest(index=index), self.assertRaises(ValueError):
                controller.attach_network(qmp, self.status, 31861, index)
            qmp.command.assert_not_called()

    def test_only_explicit_host_local_proxy_uses_host_network(self):
        with mock.patch.object(controller, 'select_physical_ipv4', return_value={'interface_index': 4}) as physical:
            for endpoint in ('10.0.2.2', '127.0.0.1'):
                self.assertIsNone(controller.select_uplink(endpoint)['interface_index'])
            physical.assert_not_called()
            self.assertEqual(controller.select_uplink('203.0.113.7')['interface_index'], 4)
            physical.assert_called_once_with('203.0.113.7')
            physical.reset_mock()
            with self.assertRaises(ValueError):
                controller.select_uplink('127.0.0.2')
            physical.assert_not_called()
        self.status.accept(self.event('FIREWALL_READY'), 0)
        qmp = mock.Mock()
        controller.attach_network(qmp, self.status, 31861, None)
        self.assertNotIn('x-wcp-outbound-if', qmp.command.call_args_list[1].args[1])

    def test_failed_or_partial_hotplug_does_not_ack_network(self):
        self.status.accept(self.event('FIREWALL_READY'), 0)
        qmp = mock.Mock()
        qmp.command.side_effect = [None, RuntimeError('occupied port')]
        with self.assertRaises(RuntimeError):
            controller.attach_network(qmp, self.status, 31861, 4)
        self.assertFalse(self.status.network_attached)
        self.assertIsNone(self.status.accept(self.event('READY'), 1))

    def test_missing_partial_nic_does_not_prevent_other_link_closure_or_acpi(self):
        for missing in ('wcniclan', 'wcnicwan'):
            def command(name, parameters=None):
                if name == 'set_link' and parameters['name'] == missing:
                    raise RuntimeError('NIC not created')
            qmp = mock.Mock()
            qmp.command.side_effect = command
            self.assertTrue(controller.stop_failed_gateway(qmp))
            self.assertEqual(qmp.command.call_args_list, [
                mock.call('set_link', {'name': 'wcniclan', 'up': False}),
                mock.call('set_link', {'name': 'wcnicwan', 'up': False}),
                mock.call('system_powerdown')])
        qmp.command.side_effect = controller.queue.Empty
        self.assertFalse(controller.stop_failed_gateway(qmp))
        self.assertEqual(qmp.command.call_args.args, ('system_powerdown',))

    def test_failed_boot_retries_early_acpi_without_forcing_the_guest(self):
        process = mock.Mock()
        process.poll.side_effect = [None, None, 0]
        qmp = mock.Mock()
        with mock.patch.object(controller.time, 'sleep'), \
             mock.patch.object(controller, 'stop_failed_gateway', return_value=True) as stop:
            self.assertEqual(controller.wait_failed_gateway_exit(process, qmp), 1)
        stop.assert_called_once_with(qmp)
        process.wait.assert_called_once_with()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def requested_stop(self, exit_at, *, qmp_error=False):
        self.assertTrue(callable(getattr(controller, 'wait_requested_gateway_exit', None)),
                        'Requested shutdown needs a bounded watchdog before waiting for guest exit')
        clock = [0]
        process = mock.Mock()
        process.poll.side_effect = lambda: 0 if clock[0] >= exit_at else None
        report = {'status': 'running', 'network_verified': True, 'pid': 123,
                  'process': {'pid': 123, 'created': 'fixture'}}
        snapshots, commands = [], []

        def command(name, parameters=None):
            commands.append((clock[0], name, parameters))
            if qmp_error:
                raise RuntimeError('QMP unavailable')

        def sleep(seconds):
            clock[0] += seconds
            self.assertLess(clock[0], exit_at + 10, 'Watchdog did not observe guest exit')

        with mock.patch.object(controller.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(controller.time, 'sleep', side_effect=sleep):
            controller.wait_requested_gateway_exit(process, mock.Mock(command=command, events=[]), report,
                lambda: snapshots.append((clock[0], dict(report))))
        process.wait.assert_called_once_with()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        self.assertEqual(report['pid'], 123)
        self.assertEqual(report['process'], {'pid': 123, 'created': 'fixture'})
        self.assertTrue(all(not state['network_verified'] for _, state in snapshots))
        return report, snapshots, commands

    def test_requested_shutdown_retries_then_preserves_unconfirmed_guest(self):
        report, snapshots, commands = self.requested_stop(135)
        self.assertEqual(snapshots[0][1]['status'], 'stopping')
        self.assertEqual(report['status'], 'shutdown-unconfirmed')
        self.assertEqual(report['error'], 'SHUTDOWN_UNCONFIRMED')
        self.assertEqual(next(at for at, state in snapshots
                              if state['status'] == 'shutdown-unconfirmed'), 120)
        powerdowns = [at for at, name, _ in commands if name == 'system_powerdown']
        self.assertEqual(powerdowns, list(range(0, 120, 5)))
        self.assertEqual(report['shutdown_retry_count'], 23)
        for nic in ('wcniclan', 'wcnicwan'):
            self.assertIn((0, 'set_link', {'name': nic, 'up': False}), commands)

    def test_requested_shutdown_exits_normally_without_false_timeout(self):
        report, _, commands = self.requested_stop(5)
        self.assertEqual(report['status'], 'stopping')
        self.assertNotIn('error', report)
        self.assertEqual([name for _, name, _ in commands].count('system_powerdown'), 1)
        self.assertEqual(report['shutdown_retry_count'], 0)

    def test_requested_shutdown_lost_qmp_still_closes_both_links_and_retains_guest(self):
        report, snapshots, commands = self.requested_stop(135, qmp_error=True)
        self.assertIn('control-unavailable-guest-running', [state['status'] for _, state in snapshots])
        self.assertEqual(report['status'], 'shutdown-unconfirmed')
        self.assertIn((0, 'set_link', {'name': 'wcniclan', 'up': False}), commands)
        self.assertIn((0, 'set_link', {'name': 'wcnicwan', 'up': False}), commands)
        self.assertTrue(all(at < 120 for at, _, _ in commands))

    def test_requested_shutdown_qmp_closing_with_guest_exit_is_success(self):
        process = mock.Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        qmp = mock.Mock(events=[{'event': 'SHUTDOWN',
                                'data': {'guest': True, 'reason': 'guest-shutdown'}}])
        qmp.command.side_effect = RuntimeError('QMP closed during guest exit')
        report = {'status': 'running', 'network_verified': True}
        controller.wait_requested_gateway_exit(process, qmp, report, lambda: None)
        self.assertNotIn('error', report)
        self.assertEqual(report['status'], 'stopping')

    def test_outage_revokes_readiness_then_matching_recovery_is_accepted(self):
        self.assertIsNone(self.status.accept(self.event('RECONNECTING'), 0))
        self.status.accept(self.event('FIREWALL_READY'), 1)
        controller.attach_network(mock.Mock(), self.status, 31861, 4)
        self.status.accept(self.event('READY'), 2)
        self.assertEqual(self.status.accept(self.event('RECONNECTING', code='CONNECTION_LOST'), 4),
                         'RECONNECTING')
        self.assertTrue(self.status.ever_ready)
        self.assertFalse(self.status.failed)
        self.assertFalse(self.status.healthy(4))
        self.assertIsNone(self.status.accept(self.event('READY', nonce='old'), 5))
        self.assertEqual(self.status.accept(self.event('READY'), 6), 'READY')
        self.assertTrue(self.status.healthy(6))

    def test_command_is_headless_and_initially_has_no_nic_or_credentials(self):
        root = SCRIPT.parents[1]
        directory = root / 'logs/guest-gateway-boot-fixture'
        media = {'path': 'C:/private/session.tar', 'serial': 'WCSESSION', 'readonly': True}
        args = controller.arguments(root, directory, media, {'vcpus': 2, 'gateway_memory_mib': 768})
        self.assertEqual(args[args.index('-nic') + 1], 'none')
        self.assertEqual(args[args.index('-display') + 1], 'none')
        self.assertNotIn('-netdev', args)
        self.assertNotIn('filter-dump', ' '.join(args))
        self.assertIn('ringbuf,id=wcserial,size=65536', args)
        self.assertNotIn('file:' + directory.relative_to(root).as_posix() + '/runtime-serial.log', args)
        self.assertNotIn('password', ' '.join(args))
        block = json.loads(args[args.index('-blockdev') + 1])
        self.assertTrue(block['read-only'])
        self.assertNotIn('workvm', ' '.join(args))

    def test_selected_revision_is_pinned_before_decryption_and_resolution(self):
        store = mock.Mock()
        view = {'profile_id': 'c' * 32, 'revision_id': 'b' * 32}
        store.selected_summary.return_value = view
        store.secret_config.return_value = {'name': 'Proxy', 'protocol': 'http', 'host': 'proxy.example',
                                             'port': 8080, 'credentials': {'username': 'u', 'password': 'sensitive'}}
        store.session_profile.return_value = contextlib.nullcontext((view, store.secret_config.return_value))
        with mock.patch.object(controller.socket, 'getaddrinfo',
                               return_value=[(2, 1, 6, '', ('203.0.113.5', 8080))]) as resolve:
            selected, session = controller.make_session(store, '2026.10.20.1')
        self.assertEqual(selected, view)
        self.assertEqual(session['endpoint_ipv4'], '203.0.113.5')
        store.session_profile.assert_called_once_with(None)
        self.assertEqual(resolve.call_count, 1)
        self.assertNotIn('sensitive', json.dumps(controller.SessionStatus(session).identity))

    def test_draft_session_uses_supplied_profile_and_ephemeral_ids_without_store(self):
        store = mock.Mock()
        draft = {'profile_id': 'd' * 32, 'revision_id': 'e' * 32,
                 'profile': {'protocol': 'http', 'host': '203.0.113.77', 'port': 8443,
                             'credentials': {'username': 'draft-user', 'password': 'draft-secret'}}}
        selected, session = controller.make_session(store, '2026.10.20.1', draft=draft)
        self.assertEqual(selected, {'profile_id': 'd' * 32, 'revision_id': 'e' * 32})
        self.assertEqual(session['revision'], 'e' * 32)
        self.assertEqual(session['endpoint_ipv4'], '203.0.113.77')
        self.assertEqual(session['profile']['credentials']['password'], 'draft-secret')
        self.assertEqual(session['profile']['port'], 8443)
        self.assertEqual(store.mock_calls, [])

    def test_dns_skips_disallowed_addresses_before_pinning_the_first_valid_address(self):
        store = mock.Mock()
        store.selected_summary.return_value = {'profile_id': 'c' * 32, 'revision_id': 'b' * 32}
        store.secret_config.return_value = {'protocol': 'http', 'host': 'proxy.example', 'port': 8080}
        store.session_profile.side_effect = lambda selected: contextlib.nullcontext((store.selected_summary.return_value, store.secret_config.return_value))
        addresses = ['169.254.1.2', '0.0.0.0', '224.0.0.1', '127.0.0.2', '192.168.1.5', '203.0.113.5']
        with mock.patch.object(controller.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', (address, 8080)) for address in addresses]):
            _, session = controller.make_session(store, 'test')
        self.assertEqual(session['endpoint_ipv4'], '192.168.1.5')
        with mock.patch.object(controller.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', (address, 8080)) for address in addresses[:4]]):
            with self.assertRaises(ValueError):
                controller.make_session(store, 'test')

    def test_failed_probe_rounds_keep_liveness_separate_from_verified_health(self):
        self.assertIsNone(self.status.accept(self.event('DEGRADED', code='PROBE_FAILED_1'), 1))
        self.status.accept(self.event('FIREWALL_READY'), 1)
        self.status.network_attached = True
        self.status.accept(self.event('READY'), 2)
        self.assertEqual(self.status.accept(self.event('DEGRADED', code='PROBE_FAILED_1'), 27), 'DEGRADED')
        self.assertFalse(self.status.healthy(27))
        self.assertTrue(self.status.responsive(27))
        self.status.accept(self.event('DEGRADED', code='PROBE_FAILED_2'), 52)
        self.assertTrue(self.status.responsive(52))
        self.assertFalse(self.status.responsive(98))
        self.assertEqual(self.status.accept(self.event('HEALTHY'), 53), 'HEALTHY')
        self.assertTrue(self.status.healthy(53))
        self.status.accept(self.event('RECONNECTING'), 54)
        self.assertFalse(self.status.healthy(54))
        self.assertFalse(self.status.responsive(54))

    def test_degraded_report_keeps_lan_up_without_claiming_fresh_verification(self):
        source = SCRIPT.read_text(encoding='utf-8')
        branch = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.If)
                      and ast.unparse(node.test) == "event == 'DEGRADED'"
                      and 'report.update' in ast.get_source_segment(source, node))
        report = {'status': 'running', 'network_verified': True, 'health_utc': 'last-success'}
        qmp, save = mock.Mock(), mock.Mock()
        scope = {'report': report, 'value': {'code': 'PROBE_FAILED_2'}, 'qmp': qmp, 'save': save}
        exec(compile(ast.Module(body=branch.body, type_ignores=[]), str(SCRIPT), 'exec'), scope)
        self.assertFalse(report['network_verified'])
        self.assertTrue(report['network_degraded'])
        self.assertEqual(report['probe_failures'], 2)
        self.assertEqual(report['health_utc'], 'last-success')
        qmp.command.assert_not_called()
        save.assert_called_once()

    def test_session_owner_retains_the_selected_revision_until_its_stack_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = controller.ProfileStore(Path(temporary)/'profiles')
            config = {'protocol': 'http', 'host': '203.0.113.5', 'port': 8080}
            first = store.save(config)
            with self.assertRaisesRegex(RuntimeError, 'synthetic'), contextlib.ExitStack() as held:
                selected, session = controller.make_session(store, 'test', first['profile_id'], held=held)
                store.save(config | {'port': 9090}, profile_id=first['profile_id'])
                self.assertEqual(store.secret_config(first['profile_id'], selected['revision_id'])['port'], 8080)
                self.assertEqual(session['revision'], first['revision_id'])
                raise RuntimeError('synthetic startup failure')
            store.save(config, profile_id=first['profile_id'])
            with self.assertRaises(ValueError):
                store.secret_config(first['profile_id'], first['revision_id'])

    def test_draft_cli_reads_bounded_private_stdin_before_run(self):
        draft = {'profile_id': 'd' * 32, 'revision_id': 'e' * 32,
                 'profile': {'protocol': 'http', 'host': '203.0.113.77', 'port': 8080}}
        project = SCRIPT.parents[1]
        with mock.patch.object(sys, 'argv', ['controller', '--project', str(project), '--test-draft']), \
                mock.patch.object(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(draft).encode()))), \
                mock.patch.object(controller, 'run', return_value=0) as run:
            self.assertEqual(controller.main(), 0)
        self.assertEqual(run.call_args.args, (project, None, True))
        submitted = run.call_args.kwargs['draft']
        self.assertEqual(submitted['profile_id'], draft['profile_id'])
        self.assertEqual(submitted['revision_id'], draft['revision_id'])
        self.assertEqual(submitted['profile']['host'], '203.0.113.77')

    def test_draft_cli_refuses_invalid_or_oversized_stdin_without_echoing_it(self):
        project = SCRIPT.parents[1]
        profile = {'protocol': 'hysteria2', 'host': '203.0.113.77', 'port': 443}
        draft = {'profile_id': 'd' * 32, 'revision_id': 'e' * 32, 'profile': profile}
        malformed = [b'x' * 65537, b'[]', b'{"profile":{},"profile":{}}',
                     json.dumps(draft).encode(), json.dumps(draft | {'profile_id': '../private-secret'}).encode()]
        for payload in malformed:
            with self.subTest(payload_length=len(payload)), \
                    mock.patch.object(sys, 'argv', ['controller', '--project', str(project), '--test-draft']), \
                    mock.patch.object(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(payload))), \
                    mock.patch.object(controller, 'run') as run, contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(controller.main(), 1)
                run.assert_not_called()
                self.assertEqual(json.loads(output.getvalue()), {'error': 'GATEWAY_START_FAILED'})

    def test_saved_profile_cli_still_passes_existing_identifier(self):
        project = SCRIPT.parents[1]
        with mock.patch.object(sys, 'argv', ['controller', '--project', str(project), '--test-profile', 'a' * 32]), \
                mock.patch.object(controller, 'run', return_value=0) as run:
            self.assertEqual(controller.main(), 0)
        run.assert_called_once_with(project, 'a' * 32, True)

    def test_draft_and_saved_profile_cli_modes_cannot_be_combined(self):
        with mock.patch.object(sys, 'argv', ['controller', '--project', str(SCRIPT.parents[1]),
                                            '--test-profile', 'a' * 32, '--test-draft']), \
                mock.patch.object(controller, 'run') as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                controller.main()
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()

    def test_draft_cannot_be_used_for_a_normal_windows_session(self):
        with mock.patch.object(controller, 'load_settings', side_effect=AssertionError('must refuse before starting')):
            with self.assertRaisesRegex(ValueError, 'test-only'):
                controller.run(SCRIPT.parents[1], draft={})

    def test_graceful_stop_before_ready_does_not_pass_connection_test(self):
        report = {'guest_shutdown': True, 'exit_code': 0}
        self.assertEqual(controller.result_code(report, test_only=True), 1)
        self.assertEqual(controller.result_code(report, test_only=False), 0)
        self.assertEqual(controller.result_code(report | {'connection_test_passed': True}, test_only=True), 0)

    def test_qmp_close_after_requested_guest_shutdown_is_not_controller_failure(self):
        process = mock.Mock()
        process.wait.return_value = 0
        qmp = mock.Mock(events=[{'event': 'SHUTDOWN',
                                'data': {'guest': True, 'reason': 'guest-shutdown'}}])
        self.assertTrue(controller.requested_shutdown_completed(process, qmp, True))
        self.assertFalse(controller.requested_shutdown_completed(process, qmp, False))
        for events in ([], [{'event': 'SHUTDOWN', 'data': {'guest': True, 'reason': 'guest-reset'}}]):
            self.assertFalse(controller.requested_shutdown_completed(process, mock.Mock(events=events), True))
        process.wait.return_value = 1
        self.assertFalse(controller.requested_shutdown_completed(process, qmp, True))
        process.wait.side_effect = controller.subprocess.TimeoutExpired('fixture', 10)
        self.assertFalse(controller.requested_shutdown_completed(process, qmp, True))

    def test_dns_timeout_does_not_start_a_gateway_or_echo_credentials(self):
        store = mock.Mock()
        store.selected_summary.return_value = {'profile_id': 'c' * 32, 'revision_id': 'b' * 32}
        store.secret_config.return_value = {'name': 'Proxy', 'protocol': 'http', 'host': 'proxy.example',
            'port': 8080, 'credentials': {'username': 'u', 'password': 'not-for-errors'}}
        store.session_profile.return_value = contextlib.nullcontext((store.selected_summary.return_value, store.secret_config.return_value))
        with mock.patch.object(controller.queue, 'Queue') as replies, \
                mock.patch.object(controller.threading, 'Thread'):
            replies.return_value.get.side_effect = controller.queue.Empty
            with self.assertRaisesRegex(ValueError, 'resolution timed out') as error:
                controller.make_session(store, '2026.10.20.3')
        self.assertNotIn('not-for-errors', str(error.exception))


if __name__ == '__main__':
    unittest.main()
