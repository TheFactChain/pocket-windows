import ast
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import queue
from types import SimpleNamespace
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / 'scripts/control_guest_install.py'


class ControlMessageTests(unittest.TestCase):
    def test_invalid_messages_do_not_prevent_the_next_powerdown(self):
        messages = [[], None, 'private-input', 42, True, {'action': 'unsupported'},
                    {'action': 'powerdown'}]
        commands, saved = [], []
        report = {'status': 'running'}
        process = mock.Mock()
        process.poll.return_value = None
        scope = {
            'controls': queue.Queue(), 'queue': queue, 'json': json,
            'sys': SimpleNamespace(stdin=io.StringIO('\n'.join(map(json.dumps, messages)))),
            'report': report, 'save': lambda: saved.append(True),
            'command': lambda name: commands.append(name),
            'work_reboot_session': False, 'process': process,
            'datetime': datetime, 'timezone': timezone,
        }
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ('read_controls', 'handle_control')]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), 'exec'), scope)
        scope['read_controls']()
        while not scope['controls'].empty():
            scope['handle_control'](scope['controls'].get_nowait())

        self.assertEqual(commands, ['system_powerdown'])
        self.assertEqual(report['status'], 'running')
        self.assertIn('acpi_powerdown_requested_utc', report)
        self.assertEqual(len(report['control_errors']), 6)
        self.assertEqual(report['control_errors'][-1]['action'], 'unsupported')
        self.assertTrue(all(error['error'].startswith('ValueError:')
                            for error in report['control_errors']))
        self.assertNotIn('private-input', json.dumps(report))
        self.assertEqual(len(saved), 7)
        process.wait.assert_not_called()
        process.kill.assert_not_called()
        process.terminate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
