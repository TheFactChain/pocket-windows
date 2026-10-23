import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/portable_app.py'
app = None
if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location('portable_app', SCRIPT)
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)


class PortableAppTests(unittest.TestCase):
    def test_diagnose_busy_initial_check_returns_valid_json(self):
        import package_integrity
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with mock.patch.object(sys, 'argv', ['app', '--root', temporary, '--action', 'diagnose']), \
                    mock.patch.object(package_integrity, 'verify_initial', return_value={'ok': False, 'code': 'busy', 'message': 'busy'}), \
                    mock.patch.object(app, 'save_diagnostic_report', return_value='logs/fixture.json'), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(app.main(), 1)
            reply = json.loads(output.getvalue())
            self.assertEqual(reply['code'], 'busy')
            self.assertEqual(reply['package_check']['code'], 'busy')

    def test_start_rejects_damaged_initial_payload_before_preflight_or_spawn(self):
        import package_integrity
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(package_integrity, 'verify_initial', side_effect=ValueError('damaged initial payload')), \
                mock.patch.object(app, 'preflight', return_value={'ok': False}) as preflight, \
                mock.patch.object(app.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'damaged initial payload'):
                app.start(Path(temporary))
            preflight.assert_not_called()
            spawn.assert_not_called()

    def test_quiet_status_skips_component_inspection_and_hashing(self):
        status = getattr(app, 'runtime_status', None)
        self.assertTrue(callable(status), 'A separate lightweight runtime status is required')
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as held:
            root = Path(temporary)
            engine = root / app.RUNTIME_FILES[1]
            engine.parent.mkdir(parents=True)
            engine.write_bytes(b'fixture engine')
            held.enter_context(mock.patch.object(app.runtime, 'discover_guests', return_value={}))
            held.enter_context(mock.patch.object(app.runtime, 'active_receipt', side_effect=ValueError('No active session')))
            held.enter_context(mock.patch.object(app, 'ProfileStore')).return_value.selected_summary.return_value = None
            inspect = held.enter_context(mock.patch.object(app, 'inspect_current'))
            digest = held.enter_context(mock.patch.object(app.hashlib, 'file_digest'))
            report = status(root, host=self.facts())
        inspect.assert_not_called()
        digest.assert_not_called()
        self.assertEqual(report['report_kind'], 'runtime_status')
        self.assertEqual(report['runtime']['work'], 'stopped')
        self.assertNotIn('engine_identity', report['checks'])
        self.assertNotIn('gateway_package', report['checks'])
        self.assertFalse(report['network_requests_made'])
        self.assertFalse(report['secret_values_exposed'])

    def test_full_preflight_still_verifies_engine_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = root / app.RUNTIME_FILES[1]
            engine.parent.mkdir(parents=True)
            engine.write_bytes(b'fixture engine')
            with mock.patch.object(app, 'ENGINE_SHA256', hashlib.sha256(b'fixture engine').hexdigest()), \
                    mock.patch.object(app.hashlib, 'file_digest', wraps=hashlib.file_digest) as digest:
                report = app.preflight(root, host=self.facts())
            digest.assert_called_once()
            self.assertTrue(report['checks']['engine_identity']['passed'])

    @contextlib.contextmanager
    def connection_boundary(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as held:
            root = Path(temporary).resolve()
            nonce = 'a' * 32
            path = root / 'logs' / ('guest-gateway-boot-' + nonce) / 'state.json'
            path.parent.mkdir(parents=True)
            view = {'profile_id': 'b' * 32, 'revision_id': 'c' * 32, 'needs_credentials': False}
            controller = {'pid': 4321, 'created_filetime': 134000000000000000,
                          'image': str(root / 'runtime/python-3.12.14/python.exe')}
            state = {'schema': 2, 'guest': 'gateway-boot', 'test_only': True, 'nonce': nonce,
                     'profile_id': view['profile_id'], 'profile_revision_id': view['revision_id'],
                     'controller': controller, 'status': 'exited', 'connection_test_passed': True,
                     'guest_shutdown': True, 'carrier_removed': True, 'overlay_removed': True}
            path.write_text(json.dumps(state))
            process = mock.Mock(pid=4321, returncode=0)
            held.enter_context(mock.patch.object(app, 'ProfileStore')).return_value.get_view.return_value = view
            held.enter_context(mock.patch.object(app, 'host_facts', return_value=self.facts()))
            held.enter_context(mock.patch.object(app, 'load_settings', return_value=dict(app.DEFAULTS)))
            held.enter_context(mock.patch.object(app.runtime, 'ProjectLock')).return_value.__enter__.return_value = True
            held.enter_context(mock.patch.object(app.runtime, 'engine_processes', return_value={}))
            held.enter_context(mock.patch.object(app.runtime, 'process_identity', return_value=controller))
            inspect = held.enter_context(mock.patch.object(app, 'inspect_current', return_value={'version': 'fixture'}))
            popen = held.enter_context(mock.patch.object(app.subprocess, 'Popen', return_value=process))
            yield root, view, state, path, process, popen, inspect

    def test_connection_success_binds_controller_nonce_revision_and_graceful_cleanup(self):
        with self.connection_boundary() as (root, view, state, path, process, popen, inspect):
            reply = app.test_connection(root, {'profile_id': view['profile_id']})
            self.assertTrue(reply['verified'])
            self.assertEqual(reply['revision_id'], view['revision_id'])
            self.assertIsNone(reply['applied_revision_id'])
            process.wait.assert_called_once_with(timeout=300)
            process.communicate.assert_not_called()
            self.assertEqual(popen.call_args.kwargs['stdout'], app.subprocess.DEVNULL)
            self.assertEqual(popen.call_args.kwargs['stderr'], app.subprocess.DEVNULL)
            inspect.assert_called_once_with(root)

    def test_connection_rejects_wrong_identity_revision_nonce_or_incomplete_cleanup(self):
        changes = [{'profile_revision_id': 'd' * 32}, {'nonce': 'e' * 32},
                   {'controller': {'pid': 4321, 'created_filetime': 1, 'image': 'foreign.exe'}},
                   {'guest_shutdown': False}, {'carrier_removed': False}, {'overlay_removed': False},
                   {'status': 'running'}, {'test_only': False}, {'connection_test_passed': False}]
        for change in changes:
            with self.subTest(change=change), self.connection_boundary() as (root, view, state, path, process, popen, inspect):
                path.write_text(json.dumps(state | change))
                self.assertFalse(app.test_connection(root, {'profile_id': view['profile_id']})['verified'])

    def test_connection_rejects_ambiguous_or_oversized_states_without_raw_error_echo(self):
        with self.connection_boundary() as (root, view, state, path, process, popen, inspect):
            other = root / 'logs' / ('guest-gateway-boot-' + 'd' * 32) / 'state.json'
            other.parent.mkdir(); other.write_text(json.dumps(state | {'nonce': 'd' * 32}))
            self.assertFalse(app.test_connection(root, {'profile_id': view['profile_id']})['verified'])
            other.unlink()
            path.write_text(json.dumps(state | {'connection_test_passed': False, 'error': 'NEVER_ECHO_PRIVATE'}))
            self.assertNotIn('NEVER_ECHO_PRIVATE', json.dumps(app.test_connection(root, {'profile_id': view['profile_id']})))
            path.write_bytes(b'x' * 65537)
            self.assertFalse(app.test_connection(root, {'profile_id': view['profile_id']})['verified'])

    def test_connection_timeout_keeps_child_and_uses_no_owner_pipes(self):
        with self.connection_boundary() as (root, view, state, path, process, popen, inspect):
            process.wait.side_effect = app.subprocess.TimeoutExpired('fake controller', 300)
            reply = app.test_connection(root, {'profile_id': view['profile_id']})
            self.assertEqual(reply['code'], 'test_pending')
            self.assertNotIn('outcome_unknown', reply)
            process.kill.assert_not_called(); process.terminate.assert_not_called()
            process.communicate.assert_not_called()
            self.assertEqual(popen.call_args.kwargs['stdout'], app.subprocess.DEVNULL)

    def test_connection_result_survives_more_than_a_thousand_historical_sessions(self):
        with self.connection_boundary() as (root, view, state, path, process, popen, inspect):
            for number in range(1001):
                nonce = format(number, '032x')
                old = root / 'logs' / ('guest-gateway-boot-' + nonce) / 'state.json'
                old.parent.mkdir()
                old.write_text(json.dumps(state | {'nonce': nonce,
                    'controller': state['controller'] | {'created_filetime': 1}}))
            self.assertTrue(app.test_connection(root, {'profile_id': view['profile_id']})['verified'])

    def test_connection_host_busy_and_unavailable_credentials_never_spawn(self):
        for condition in ('host', 'busy', 'credentials'):
            with self.subTest(condition=condition), self.connection_boundary() as (root, view, state, path, process, popen, inspect):
                with contextlib.ExitStack() as held:
                    if condition == 'host': held.enter_context(mock.patch.object(app, 'host_facts', return_value=self.facts() | {'whpx': False}))
                    if condition == 'busy': held.enter_context(mock.patch.object(app.runtime, 'engine_processes', return_value={1: {}}))
                    if condition == 'credentials': view['needs_credentials'] = True
                    self.assertFalse(app.test_connection(root, {'profile_id': view['profile_id']})['ok'])
                popen.assert_not_called()

    def test_test_profile_main_redacts_exception_and_missing_recovery_is_russian(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = io.StringIO()
            with mock.patch.object(sys, 'argv', ['app', '--root', str(root), '--action', 'test-profile']), \
                    mock.patch.object(app, 'read_request', return_value={}), \
                    mock.patch.object(app, 'test_connection', side_effect=ValueError('NEVER_ECHO_PRIVATE')), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(app.main(), 1)
            self.assertNotIn('NEVER_ECHO_PRIVATE', output.getvalue())
            missing = app.repair_gateway(root)
            self.assertEqual(missing['code'], 'package_missing')
            self.assertIn('Не найден', missing['message'])

    def test_preflight_preserves_reconnect_state_and_keeps_stop_without_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            states = {'work': {'status': 'running'}, 'gateway': {'status': 'reconnecting'}}
            guests = {role: {'state': role} for role in states}
            with mock.patch.object(app.runtime, 'discover_guests', return_value=guests), \
                    mock.patch.object(app.runtime, 'read_json', side_effect=lambda path: states[str(path)]), \
                    mock.patch.object(app.runtime, 'controller_live', return_value=True), \
                    mock.patch.object(app.runtime, 'gateway_ready', return_value=False):
                report = app.preflight(root, host=self.facts(512))
            self.assertEqual(report['runtime']['gateway'], 'reconnecting')
            self.assertTrue(report['already_running'])
            self.assertTrue(report['checks']['memory']['passed'])
            self.assertTrue(report['capabilities']['stop'])
            self.assertFalse(report['network_verified'])
            self.assertIn('Связь восстанавливается', report['network_warning'])
            self.assertFalse(report['secret_values_exposed'])

    def test_gateway_without_windows_is_not_ready_for_another_start(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(app.runtime, 'discover_guests', return_value={'gateway': {'state': 'gateway'}}), \
                mock.patch.object(app.runtime, 'read_json', return_value={'status': 'running'}), \
                mock.patch.object(app.runtime, 'controller_live', return_value=True), \
                mock.patch.object(app.runtime, 'gateway_ready', return_value=True):
            report = app.preflight(Path(temporary), host=self.facts())
        self.assertFalse(report['checks']['runtime_state']['passed'])
        self.assertTrue(report['capabilities']['stop'])
        self.assertFalse(report['already_running'])

    def test_interrupted_session_offers_explicit_recovery_without_touching_it(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(app.runtime,'discover_guests',side_effect=RuntimeError('stale fixture')), \
                mock.patch.object(app.runtime,'recovery_snapshot',return_value={'available':True}), \
                mock.patch.object(app.runtime,'recover_session',side_effect=AssertionError('diagnostics must not mutate')):
            report=app.preflight(Path(temporary),host=self.facts())
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks']['runtime_state']['action'],'recover_session')
        self.assertTrue(report['capabilities']['recover_session'])

    def test_live_supervisor_during_start_or_stop_does_not_offer_session_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            supervisor={'pid':501,'created_filetime':134000000000000000,'image':str(root/'runtime/python-3.12.14/python.exe')}
            with mock.patch.object(app.runtime,'discover_guests',return_value={'gateway':{'state':'gateway'}}), \
                    mock.patch.object(app.runtime,'read_json',return_value={'status':'stopping'}), \
                    mock.patch.object(app.runtime,'controller_live',return_value=True), \
                    mock.patch.object(app.runtime,'gateway_ready',return_value=False), \
                    mock.patch.object(app.runtime,'recovery_snapshot',return_value={'available':True}), \
                    mock.patch.object(app.runtime,'active_receipt',return_value=(None,{'phase':'stopping','supervisor':supervisor})), \
                    mock.patch.object(app.runtime,'process_identity',return_value=supervisor):
                report=app.preflight(root,host=self.facts())
            self.assertFalse(report['capabilities']['recover_session'])
            self.assertNotEqual(report['checks']['runtime_state']['action'],'recover_session')

    def test_owned_failed_or_test_guest_is_stoppable_but_blocks_new_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            for state in ({'status': 'connection-failed'}, {'status': 'running', 'test_only': True}):
                with self.subTest(state=state), mock.patch.object(app.runtime, 'discover_guests', return_value={'gateway': {'state': 'gateway'}}), \
                        mock.patch.object(app.runtime, 'read_json', return_value=state), \
                        mock.patch.object(app.runtime, 'controller_live', return_value=False), \
                        mock.patch.object(app.runtime, 'gateway_ready', return_value=False):
                    report = app.preflight(Path(temporary), host=self.facts())
                self.assertTrue(report['capabilities']['stop'])
                self.assertFalse(report['checks']['runtime_state']['passed'])

    def setUp(self):
        self.assertIsNotNone(app, 'EXE backend is missing')

    def facts(self, available=8192):
        return {'windows': True, 'x64': True, 'whpx': True, 'ram_total_mib': 16384,
                'ram_available_mib': available, 'admin': False}

    def test_clean_empty_copy_is_blocked_without_writing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = list(root.rglob('*'))
            report = app.preflight(root, host=self.facts())
            self.assertFalse(report['ok'])
            self.assertFalse(report['checks']['runtime_files']['passed'])
            self.assertFalse(report['checks']['guest_files']['passed'])
            self.assertEqual(list(root.rglob('*')), before)

    def test_legacy_images_without_gateway_package_do_not_enable_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'images').mkdir()
            for name in ('workvm-empty.qcow2', 'gateway-empty.qcow2'):
                (root / 'images' / name).write_bytes(b'fixture')
            report = app.preflight(root, host=self.facts())
            self.assertFalse(report['checks']['gateway_package']['passed'])
            self.assertFalse(report['ok'])

    def test_low_available_memory_is_an_explicit_blocker(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = app.preflight(Path(temporary), host=self.facts(512))
            self.assertFalse(report['checks']['memory']['passed'])
            self.assertIn('6144', report['checks']['memory']['detail'])

    def test_diagnostics_never_expose_saved_profile_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / 'private/profile/sing-box.json'
            profile.parent.mkdir(parents=True)
            original = json.dumps({'password': 'fixture-for-leak-check'}).encode()
            profile.write_bytes(original)
            report = app.preflight(root, host=self.facts())
            self.assertNotIn('fixture-for-leak-check', json.dumps(report))
            self.assertEqual(profile.read_bytes(), original)

    def test_selected_profile_checks_secret_availability_without_exposing_or_claiming_application(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = app.ProfileStore(root / 'profiles')
            view = store.save({'name': 'Test proxy', 'protocol': 'socks5', 'host': 'proxy.example.com',
                               'port': 1080, 'credentials': {'username': 'user', 'password': 'never-in-diagnostics'}})
            store.select(view['profile_id'])
            with mock.patch('profile_store._unprotect', side_effect=app.SecretsUnavailable('private failure')):
                report = app.preflight(root, host=self.facts())
            self.assertFalse(report['checks']['connection']['passed'])
            self.assertTrue(report['connection']['selected_profile']['needs_credentials'])
            blocker = next(item for item in report['blockers'] if item['code'] == 'connection')
            self.assertEqual(blocker['action'], 'connection')
            self.assertIn('заново', blocker['detail'])
            self.assertEqual(report['connection']['selected_profile']['name'], 'Test proxy')
            self.assertEqual(report['connection']['selected_profile']['revision_id'], view['revision_id'])
            self.assertEqual(report['connection']['selected_profile']['state'], 'pending')
            self.assertFalse(report['connection']['verified'])
            self.assertIsNone(report['connection']['applied_revision_id'])
            self.assertEqual(report['runtime']['work'], 'stopped')
            self.assertNotIn('never-in-diagnostics', json.dumps(report))

    def test_settings_roundtrip_at_a_root_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='portable app ') as temporary:
            root = Path(temporary)
            saved = app.save_settings(root, {'schema': 1, 'lan_port': 32861,
                                             'work_memory_mib': 3072, 'gateway_memory_mib': 1024,
                                             'vcpus': 2})
            self.assertEqual(saved['lan_port'], 32861)
            self.assertEqual(app.load_settings(root), saved)

    def test_display_preferences_save_without_touching_the_windows_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / 'images/workvm-empty.qcow2'
            image.parent.mkdir()
            header = b'QFI\xfb' + bytes(20) + (80 * 1024 ** 3).to_bytes(8, 'big')
            image.write_bytes(header)
            value = dict(app.DEFAULTS, windows={'schema': 1, 'width': 1920, 'height': 1080, 'fit': False})
            app.save_settings(root, value)
            view = app.windows_preferences(root)
            self.assertEqual(view['windows_settings'], value['windows'])
            self.assertEqual(view['windows_disk']['virtual_bytes'], 80 * 1024 ** 3)
            self.assertFalse(view['windows_disk']['resize_available'])
            self.assertEqual(image.read_bytes(), header)
            before = (root / 'config/runtime.json').read_bytes()
            with self.assertRaises(ValueError):
                app.save_settings(root, dict(value, windows={'schema': 1, 'width': 1, 'height': 1}))
            self.assertEqual((root / 'config/runtime.json').read_bytes(), before)

    def test_invalid_settings_do_not_replace_existing_public_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app.save_settings(root, {'schema': 1, 'lan_port': 32861})
            path = root / 'config/runtime.json'
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                app.save_settings(root, {'schema': 1, 'lan_port': 80})
            self.assertEqual(path.read_bytes(), before)

    def test_blocked_start_never_invokes_a_guest_or_powershell(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(app.subprocess, 'Popen', side_effect=AssertionError('spawn')):
                result = app.start(Path(temporary))
            self.assertFalse(result['ok'])

    def test_settings_are_refused_while_startup_lock_is_held_without_any_qemu(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with app.runtime.ProjectLock(root) as acquired:
                self.assertTrue(acquired)
                with self.assertRaisesRegex(ValueError, 'запуск|startup'):
                    app.save_settings(root, {'schema': 1, 'lan_port': 32861})
            self.assertFalse((root / 'config/runtime.json').exists())

    def test_running_owned_pair_does_not_require_memory_for_another_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            identities = {}
            for pid, role in enumerate(('gateway', 'work'), 101):
                identity = {'pid': pid, 'created_filetime': 134000000000000000 + pid,
                            'image': str(root / 'runtime/qemu-11.1.0/qemu-system-x86_64.exe')}
                identities[pid] = identity
                nonce = 'a' * 32
                folder = root / 'logs' / ('guest-' + role + '-boot-' + (nonce if role == 'gateway' else 'fixture'))
                folder.mkdir(parents=True)
                started = identity['created_filetime'] / 10_000_000 - 11644473600
                disk = 'logs/' + folder.name + '/gateway.qcow2' if role == 'gateway' else 'images/workvm-empty.qcow2'
                state = {
                    'guest': role + '-boot', 'pid': pid, 'status': 'running',
                    'network': 'gateway' if role == 'gateway' else 'isolated-lan',
                    'started_utc': datetime.fromtimestamp(started, timezone.utc).isoformat(),
                    'controller': {'pid': pid + 100, 'created_filetime': identity['created_filetime'],
                                   'image': str(root / 'runtime/python-3.12.14/python.exe')},
                    'arguments': ['-drive', 'if=ide,file=' + disk]}
                if role == 'gateway':
                    state.update(schema=2, nonce=nonce, disk=disk, test_only=False, process=identity,
                                 profile_id='b' * 32, profile_revision_id='c' * 32, gateway_version='fixture')
                (folder / 'state.json').write_text(json.dumps(state))
            with mock.patch.object(app.runtime, 'engine_processes', return_value=identities):
                report = app.preflight(root, host=self.facts(512))
            self.assertTrue(report['checks']['memory']['passed'])
            self.assertTrue(report['already_running'])

    def test_untracked_process_does_not_bypass_memory_or_ownership_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(app.runtime, 'engine_processes', return_value={101: {'pid': 101}}):
                report = app.preflight(Path(temporary), host=self.facts(512))
            self.assertFalse(report['checks']['memory']['passed'])
            self.assertFalse(report['checks']['runtime_state']['passed'])
            self.assertFalse(report['ok'])

    def test_get_settings_opens_recovery_without_modifying_invalid_config(self):
        for invalid in ('{broken', '{"lan_port":80}'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = root / 'config/runtime.json'
                path.parent.mkdir()
                path.write_text(invalid, encoding='utf-8')
                output = io.StringIO()
                with mock.patch.object(sys, 'argv', ['app', '--root', str(root), '--action', 'get-settings']), \
                        contextlib.redirect_stdout(output):
                    status = app.main()
                reply = json.loads(output.getvalue())
                self.assertEqual(status, 0)
                self.assertTrue(reply['recovery_required'])
                self.assertEqual(reply['settings']['lan_port'], 31861)
                self.assertEqual(path.read_text(encoding='utf-8'), invalid)

    def test_saving_recovered_settings_preserves_the_invalid_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'config/runtime.json'
            path.parent.mkdir()
            invalid = b'{broken public settings\r\n'
            path.write_bytes(invalid)
            saved = app.save_settings(root, {'schema': 1, 'lan_port': 33861})
            self.assertEqual(app.load_settings(root), saved)
            backups = list(path.parent.glob('runtime.invalid-*.json'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), invalid)

    def test_historical_marker_never_certifies_the_current_copy_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'state').mkdir()
            (root / 'state/gateway-network-accepted.json').write_text(
                '{"native_tests_passed":true,"wan_bypass_packets":0}')
            report = app.preflight(root, host=self.facts())
            self.assertNotIn('network_acceptance', report['checks'])
            self.assertFalse(report['network_verified'])
            self.assertEqual(report['network_acceptance_scope'], 'unverified')
            self.assertTrue(report['network_warning'])

    def test_low_memory_report_explains_the_blocker_and_next_action(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (*app.RUNTIME_FILES, *app.GUEST_FILES):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fixture')
            (root / 'state/gateway-network-accepted.json').write_text(
                '{"native_tests_passed":true,"wan_bypass_packets":0}')
            with mock.patch.object(app, 'ENGINE_SHA256', hashlib.sha256(b'fixture').hexdigest()), \
                    mock.patch.object(app.shutil, 'disk_usage', return_value=mock.Mock(free=10 * 1024 ** 3)):
                report = app.preflight(root, host=self.facts(4096))
            self.assertFalse(report['ok'])
            self.assertIn('памят', report['message'].lower())
            self.assertEqual(report.get('blockers', [{}])[0].get('code'), 'memory')
            self.assertEqual(report['blockers'][0]['action'], 'settings')
            self.assertEqual(report['checks']['memory'].get('title'), 'Оперативная память')

    def test_missing_runtime_report_points_to_component_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = app.preflight(Path(temporary), host=self.facts())
            blockers = {item['code']: item for item in report.get('blockers', [])}
            self.assertIn('runtime_files', blockers)
            self.assertEqual(blockers['runtime_files']['action'], 'prepare')

    def test_missing_application_script_does_not_offer_runtime_only_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (*app.RUNTIME_FILES, *app.GUEST_FILES):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fixture')
            (root / 'scripts/control_guest_install.py').unlink()
            (root / 'state/gateway-network-accepted.json').write_text(
                '{"native_tests_passed":true,"wan_bypass_packets":0}')
            with mock.patch.object(app, 'ENGINE_SHA256', hashlib.sha256(b'fixture').hexdigest()):
                report = app.preflight(root, host=self.facts())
            self.assertTrue(report['checks']['runtime_files']['passed'])
            blockers = {item['code']: item for item in report.get('blockers', [])}
            self.assertIn('application_files', blockers)
            self.assertNotEqual(blockers['application_files']['action'], 'prepare')

    def test_diagnostics_cli_keeps_one_public_report_without_private_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / 'private/profile.json'
            private.parent.mkdir()
            private.write_text('{"password":"fixture-do-not-log"}')
            for _ in range(2):
                with mock.patch.object(sys, 'argv', ['app', '--root', str(root), '--action', 'diagnose']), \
                        mock.patch.object(app, 'host_facts', return_value=self.facts()), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    app.main()
                reply = json.loads(output.getvalue())
                self.assertEqual(reply.get('diagnostic_report'), 'logs/diagnostics-latest.json')
            reports = list((root / 'logs').glob('diagnostics-*.json'))
            self.assertEqual(len(reports), 1)
            saved = json.loads(reports[0].read_text(encoding='utf-8'))
            self.assertFalse(saved['ok'])
            self.assertTrue(saved['blockers'])
            self.assertNotIn('fixture-do-not-log', reports[0].read_text())
            self.assertNotIn('root', saved)


if __name__ == '__main__':
    unittest.main()
