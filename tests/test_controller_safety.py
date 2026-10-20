"""Exercise controller actions without starting QEMU or opening guest disks."""
import ast
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'scripts/control_guest_install.py'


class ControllerSafety(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'handle_control']
        self.assertEqual(len(functions), 1, 'Control failures need an isolated recoverable boundary')
        self.commands = []
        self.saved = []
        self.scope = {
            'report': {'network': 'isolated-lan', 'status': 'running'},
            'command': self.command, 'save': lambda: self.saved.append(True),
            'capture': lambda label: None,
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), 'exec'), self.scope)

    def command(self, name, parameters=None):
        self.commands.append(name)
        if name == 'query-status':
            raise RuntimeError('synthetic rejected QMP response')

    def test_rejected_qmp_command_keeps_guest_running(self):
        self.scope['handle_control']({'action': 'status'})
        self.assertEqual(self.scope['report']['status'], 'running')
        self.assertIn('control_errors', self.scope['report'])
        self.assertEqual(self.commands, ['query-status'])

    def test_abrupt_quit_is_rejected_without_sending_it(self):
        self.scope['handle_control']({'action': 'quit'})
        self.assertEqual(self.commands, [])
        self.assertIn('control_errors', self.scope['report'])

    def test_powerdown_uses_acpi(self):
        from datetime import datetime, timezone
        self.scope.update(datetime=datetime, timezone=timezone)
        self.scope['handle_control']({'action': 'powerdown'})
        self.assertEqual(self.commands, ['system_powerdown'])
        self.assertIn('acpi_powerdown_requested_utc', self.scope['report'])

    def test_fatal_cleanup_has_no_hard_stop(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, ('terminate', 'kill'))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'command':
                self.assertFalse(node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'quit')


if __name__ == '__main__':
    unittest.main()
