import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/portable_app.py'
spec = importlib.util.spec_from_file_location('gui_start_app', SCRIPT)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class GuiStartupResultTests(unittest.TestCase):
    def simulate(self, root, result=None, stderr=''):
        def spawn(command, **unused):
            self.assertIn('-GuiResultPath', command, 'A typed startup result channel is missing')
            result_path = Path(command[command.index('-GuiResultPath') + 1])
            if result is not None:
                result_path.write_text(json.dumps(result), encoding='utf-8')
            child = mock.Mock()
            child.returncode = 1
            child.communicate.return_value = ('fixture-private-stdout', stderr)
            return child
        with mock.patch.object(app, 'preflight', return_value={'ok': True}), \
                mock.patch.object(app.subprocess, 'Popen', side_effect=spawn):
            return app.start(root)

    def test_uac_cancel_is_specific_without_forwarding_raw_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = self.simulate(Path(temporary), {'schema': 1, 'code': 'uac_cancelled'})
            self.assertEqual(report['code'], 'uac_cancelled')
            self.assertIn('отмен', report['message'])
            self.assertNotIn('fixture-private-stdout', json.dumps(report))

    def test_policy_refusal_before_script_execution_has_a_specific_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = self.simulate(Path(temporary), stderr='PSSecurityException: fixture-private-stderr')
            self.assertEqual(report['code'], 'execution_policy')
            self.assertNotIn('fixture-private-stderr', json.dumps(report))

    def test_runtime_failure_returns_validated_receipt_without_reading_its_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = 'logs/runtime-launch-fixture/receipt.json'
            receipt = root / name
            receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({'message': 'fixture-private-log-content'}))
            report = self.simulate(root, {'schema': 1, 'code': 'runtime_error', 'receipt': name})
            self.assertEqual(report['code'], 'runtime_error')
            self.assertEqual(report['receipt'], name)
            self.assertIn(name, report['message'])
            self.assertNotIn('fixture-private-log-content', json.dumps(report))

    def test_receipt_outside_owned_log_tree_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = self.simulate(Path(temporary), {'schema': 1, 'code': 'runtime_error',
                                                   'receipt': '../private/profile.json'})
            self.assertEqual(report['code'], 'result_channel_invalid')


if __name__ == '__main__':
    unittest.main()
