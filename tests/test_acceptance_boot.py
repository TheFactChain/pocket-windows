import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('acceptance_boot_fixture', ROOT / 'tests/acceptance_boot.py')
boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)


class ColdRestartTests(unittest.TestCase):
    def proof(self, reason='guest-reset', code=0, gone=True):
        return {'outcome': 'reset' if reason == 'guest-reset' else 'shutdown', 'exit_code': code,
                'process_gone': gone, 'events': [{'event': 'SHUTDOWN', 'data': {'guest': True, 'reason': reason}}]}

    def test_exact_command_and_new_boot_wait_for_each_verified_reset(self):
        command = ['qemu', '-no-reboot', '-drive', 'same-overlay', '-drive', 'same-vars']
        report = {'generations': []}
        calls = []
        def run(cmd, index):
            calls.append((list(cmd), index))
            return self.proof('guest-reset' if index < 3 else 'guest-shutdown')
        boot.run_cold_generations(command, report, lambda: None, 2, run)
        self.assertEqual(calls, [(command, 1), (command, 2), (command, 3)])
        self.assertEqual(len(report['generations']), 3)

    def test_no_restart_on_crash_nonzero_live_or_missing_reset_evidence(self):
        invalid = [self.proof(code=1), self.proof(gone=False), self.proof(reason='host-error'),
                   self.proof() | {'events': []}]
        for proof in invalid:
            with self.subTest(proof=proof):
                run = mock.Mock(return_value=proof)
                with self.assertRaises(RuntimeError):
                    boot.run_cold_generations(['qemu', '-no-reboot'], {'generations': []}, lambda: None, 3, run)
                self.assertEqual(run.call_count, 1)

    def test_reset_limit_and_zero_restart_never_become_success(self):
        for limit in (0, 1, 3):
            run = mock.Mock(side_effect=lambda cmd, index: self.proof())
            with self.assertRaises(RuntimeError):
                boot.run_cold_generations(['qemu', '-no-reboot'], {'generations': []}, lambda: None, limit, run)
            self.assertEqual(run.call_count, limit + 1)

    def test_canned_qmp_reset_requires_native_exit_and_records_current_events(self):
        process = mock.Mock()
        process.stdin = io.StringIO()
        process.poll.return_value = 0
        process.wait.return_value = 0
        generation = {'process': {'pid': 123, 'image': 'qemu', 'created_filetime': 1}, 'events': []}
        messages = iter([{'QMP': {}}, {'id': 1, 'return': {}},
                         {'event': 'SHUTDOWN', 'data': {'guest': True, 'reason': 'guest-reset'}}, {'eof': True}])
        with mock.patch.object(boot.runtime, 'process_identity', return_value=None):
            result = boot.run_generation(process, generation, {'events': []}, lambda: None, 150,
                                         receive_message=lambda timeout: next(messages))
        self.assertEqual(result['outcome'], 'reset')
        self.assertTrue(result['process_gone'])
        self.assertEqual(process.wait.call_args, mock.call(timeout=10))

    def test_canned_clean_shutdown_waits_full_new_generation_boot_interval(self):
        for started in (0, 200):
            with self.subTest(started=started):
                clock = [started]
                process = mock.Mock()
                process.stdin = io.StringIO()
                process.poll.return_value = 0
                process.wait.return_value = 0
                generation = {'process': {'pid': 123, 'image': 'qemu', 'created_filetime': started + 1}, 'events': []}
                messages = iter([{'QMP': {}}, {'id': 1, 'return': {}}, {'id': 2, 'return': {'running': True}},
                                 'boot-wait', {'id': 3, 'return': []}, {'id': 4, 'return': {}},
                                 {'event': 'SHUTDOWN', 'data': {'guest': True, 'reason': 'guest-shutdown'}}, {'eof': True}])
                def receive(timeout):
                    value = next(messages)
                    if value == 'boot-wait':
                        clock[0] += 90
                        process.poll.return_value = None
                        raise boot.queue.Empty()
                    if value.get('eof'):
                        process.poll.return_value = 0
                    return value
                with mock.patch.object(boot.time, 'monotonic', side_effect=lambda: clock[0]), \
                     mock.patch.object(boot.runtime, 'process_identity', return_value=None):
                    result = boot.run_generation(process, generation, {'events': []}, lambda: None, 90,
                                                 receive_message=receive)
                self.assertEqual(result['outcome'], 'shutdown')
                self.assertEqual(result['boot_deadline_monotonic'], started + 90)
                commands = [json.loads(line)['execute'] for line in process.stdin.getvalue().splitlines()]
                self.assertEqual(commands, ['qmp_capabilities', 'query-status', 'query-blockstats', 'system_powerdown'])

    def test_native_identity_still_live_prevents_reset_acceptance(self):
        process = mock.Mock()
        process.stdin = io.StringIO()
        process.poll.return_value = 0
        process.wait.return_value = 0
        identity = {'pid': 123, 'image': 'qemu', 'created_filetime': 1}
        generation = {'process': identity, 'events': []}
        messages = iter([{'QMP': {}}, {'id': 1, 'return': {}},
                         {'event': 'SHUTDOWN', 'data': {'guest': True, 'reason': 'guest-reset'}}, {'eof': True}])
        with mock.patch.object(boot.runtime, 'process_identity', return_value=identity):
            with self.assertRaises(RuntimeError):
                boot.run_generation(process, generation, {'events': []}, lambda: None, 150,
                                    receive_message=lambda timeout: next(messages))
        self.assertIsNot(generation.get('outcome'), 'reset')


if __name__ == '__main__':
    unittest.main()
