import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import portable_app as app


class GuiStartupResultTests(unittest.TestCase):
    def simulate(self, root, phase='ready', change=None, running=True):
        identity = {'pid': 501, 'created_filetime': 134000000000000000,
                    'image': str(root / 'runtime/python-3.12.14/python.exe')}
        child = mock.Mock(pid=501, returncode=0)
        child.poll.return_value = None if running else 0
        commands = []
        def spawn(command, **unused):
            commands.append(command)
            if '--receipt' in command and phase is not None:
                receipt = Path(command[command.index('--receipt') + 1])
                value = {'schema': 1, 'project': str(root), 'phase': phase,
                         'supervisor': identity, 'message': 'fixture-private-output'}
                value.update(change or {})
                receipt.write_text(json.dumps(value), encoding='utf-8')
            return child
        before = {'ok': True, 'network_verified': False, 'network_acceptance_scope': 'historical',
                  'network_warning': 'fixture historical acceptance', 'connection': {}, 'runtime': {}}
        with mock.patch.object(app, 'preflight', return_value=before), \
                mock.patch.object(app.subprocess, 'Popen', side_effect=spawn), \
                mock.patch.object(app.runtime, 'process_identity', return_value=identity), \
                mock.patch.object(app, 'time', create=True) as clock:
            clock.monotonic.side_effect = [0, 300]
            result = app.start(root)
        self.assertNotIn('fixture-private-output', json.dumps(result))
        return result, child, commands

    def test_ready_supervisor_returns_without_waiting_for_its_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, child, commands = self.simulate(root)
            self.assertTrue(result['ok'])
            self.assertEqual(result['code'], 'ready')
            self.assertEqual(commands[0][0], str(root / 'runtime/python-3.12.14/python.exe'))
            self.assertIn(str(root / 'scripts/launch_runtime.py'), commands[0])
            self.assertNotIn('-File', commands[0])
            child.wait.assert_not_called()
            child.kill.assert_not_called()
            child.terminate.assert_not_called()

    def test_runtime_error_returns_owned_receipt_without_raw_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, _, _ = self.simulate(root, phase='error')
            self.assertFalse(result['ok'])
            self.assertEqual(result['code'], 'runtime_error')
            self.assertTrue((root / result['receipt']).is_file())

    def test_known_gateway_error_names_its_cause(self):
        for error, code in (('CONNECTION_FAILED', 'CONNECTION_FAILED'), ('NOT_A_KNOWN_CODE', 'runtime_error')):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temporary:
                result, _, _ = self.simulate(Path(temporary), phase='error', change={'gateway_error': error})
                self.assertFalse(result['ok'])
                self.assertEqual(result['code'], code)
                if code != 'runtime_error':
                    self.assertTrue(result['message'].startswith(app.CONNECTION_ERRORS[error] + '\n'))

    def test_foreign_or_invalid_receipt_cannot_confirm_success(self):
        for change in ({'project': 'C:/foreign'}, {'schema': True},
                       {'supervisor': {'pid': 999}}, {'phase': 'unknown'}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                result, _, _ = self.simulate(Path(temporary), change=change)
                self.assertFalse(result['ok'])
                self.assertEqual(result['code'], 'result_channel_invalid')
                self.assertTrue(result['outcome_unknown'])

    def test_timeout_retains_supervisor_and_historical_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, child, _ = self.simulate(Path(temporary), phase=None)
            self.assertEqual(result['code'], 'startup_timeout')
            self.assertTrue(result['outcome_unknown'])
            self.assertFalse(result['network_verified'])
            self.assertEqual(result['network_warning'], 'fixture historical acceptance')
            child.kill.assert_not_called()
            child.terminate.assert_not_called()

    def test_exited_supervisor_without_receipt_is_not_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, _, _ = self.simulate(Path(temporary), phase=None, running=False)
            self.assertEqual(result['code'], 'supervisor_exited')
            self.assertFalse(result['ok'])

    def test_ready_result_does_not_certify_historical_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, _, _ = self.simulate(Path(temporary))
            self.assertFalse(result['network_verified'])
            self.assertEqual(result['network_acceptance_scope'], 'historical')


if __name__ == '__main__':
    unittest.main()
