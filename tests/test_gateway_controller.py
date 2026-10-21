import importlib.util
import json
from pathlib import Path
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

    def test_stale_ready_never_opens_network_or_enables_windows(self):
        for field in self.session:
            self.assertIsNone(self.status.accept(self.event('FIREWALL_READY', **{field: 'stale'}), 1))
        self.assertFalse(self.status.firewall_ready)
        self.assertIsNone(self.status.accept(self.event('READY'), 1))
        self.assertFalse(self.status.healthy(1))
        qmp = mock.Mock()
        with self.assertRaises(RuntimeError):
            controller.attach_network(qmp, self.status, 31861)
        qmp.command.assert_not_called()

    def test_firewall_ack_precedes_nics_then_fresh_ready_and_health(self):
        self.assertEqual(self.status.accept(self.event('FIREWALL_READY'), 1), 'FIREWALL_READY')
        qmp = mock.Mock()
        controller.attach_network(qmp, self.status, 31861)
        calls = qmp.command.call_args_list
        self.assertEqual([call.args[0] for call in calls], ['netdev_add', 'netdev_add', 'device_add', 'device_add'])
        self.assertEqual(calls[0].args[1]['addr']['host'], '127.0.0.1')
        self.assertNotIn('hostfwd', json.dumps([c.args for c in calls]))
        self.assertEqual(self.status.accept(self.event('READY'), 2), 'READY')
        self.assertTrue(self.status.healthy(30))
        self.assertFalse(self.status.healthy(48))
        self.assertEqual(self.status.accept(self.event('HEALTHY'), 40), 'HEALTHY')
        self.assertTrue(self.status.healthy(48))
        self.status.accept(self.event('ERROR', code='SESSION_FAILED'), 50)
        self.assertFalse(self.status.healthy(50))
        self.assertIsNone(self.status.accept(self.event('READY'), 51))

    def test_failed_or_partial_hotplug_does_not_ack_network(self):
        self.status.accept(self.event('FIREWALL_READY'), 0)
        qmp = mock.Mock()
        qmp.command.side_effect = [None, RuntimeError('occupied port')]
        with self.assertRaises(RuntimeError):
            controller.attach_network(qmp, self.status, 31861)
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
        controller.attach_network(mock.Mock(), self.status, 31861)
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
        with mock.patch.object(controller.socket, 'getaddrinfo',
                               return_value=[(2, 1, 6, '', ('203.0.113.5', 8080))]) as resolve:
            selected, session = controller.make_session(store, '2026.10.20.1')
        self.assertEqual(selected, view)
        self.assertEqual(session['endpoint_ipv4'], '203.0.113.5')
        store.secret_config.assert_called_once_with(view['profile_id'], view['revision_id'])
        self.assertEqual(resolve.call_count, 1)
        self.assertNotIn('sensitive', json.dumps(controller.SessionStatus(session).identity))

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
        with mock.patch.object(controller.queue, 'Queue') as replies, \
                mock.patch.object(controller.threading, 'Thread'):
            replies.return_value.get.side_effect = controller.queue.Empty
            with self.assertRaisesRegex(ValueError, 'resolution timed out') as error:
                controller.make_session(store, '2026.10.20.3')
        self.assertNotIn('not-for-errors', str(error.exception))


if __name__ == '__main__':
    unittest.main()
