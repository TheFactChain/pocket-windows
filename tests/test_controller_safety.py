"""Exercise controller actions without starting QEMU or opening guest disks."""
import ast
from collections import deque
import io
import json
import queue
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / 'scripts/control_guest_install.py'


class ControllerSafety(unittest.TestCase):
    def test_every_windows_boot_passes_trim_to_the_qcow2_disk(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        branch = next(node for node in tree.body if isinstance(node, ast.If)
                      and ast.unparse(node.test) == 'work')
        with mock.patch.object(Path, 'exists', return_value=True):
            root = Path('fixture-maintenance-root').resolve()
            for guest in ('work-install', 'work-resume', 'work-boot'):
                scope = {'work': True, 'root': root, 'arguments': [],
                         'args': SimpleNamespace(guest=guest), 'install': guest == 'work-install',
                         'windows_hardware': SimpleNamespace(arguments=lambda: []),
                         'sha': lambda path: ('cad59a1a6303b8a89ac18f9353e1cff44212f5836c83473a345dfe7e199d7743'
                             if path.suffix == '.iso' else 'a79d8fb7bf7a32f05b766b0d7e418ad1579be51805a77567d1f47fc2db19ec8f')}
                exec(compile(ast.Module(body=[branch], type_ignores=[]), str(SOURCE), 'exec'), scope)
                disks = [value for value in scope['arguments'] if 'file=images/workvm-empty.qcow2' in value]
                self.assertEqual(disks, ['if=ide,index=0,format=qcow2,discard=unmap,file=images/workvm-empty.qcow2'], guest)

    def test_no_reboot_is_only_added_for_work_reboot_session(self):
        tree=ast.parse(SOURCE.read_text(encoding='utf-8'))
        boundary=next(node for node in tree.body if isinstance(node,ast.If)
                      and ast.unparse(node.test)=='work_reboot_session'
                      and "arguments.append('-no-reboot')" in ast.unparse(node))
        for normal in (True,False):
            scope={'work_reboot_session':normal,'arguments':[]}
            exec(compile(ast.Module(body=[boundary],type_ignores=[]),str(SOURCE),'exec'),scope)
            self.assertEqual(scope['arguments'],['-no-reboot'] if normal else [])

    def test_qmp_event_ring_keeps_latest_sixty_four_and_does_not_queue_events(self):
        from datetime import datetime, timezone
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'read_qmp')
        messages = [{'event':'RESET','data':{'reason':str(index)}} for index in range(90)]
        messages += [{'event':'SHUTDOWN','data':{'guest':True,'reason':'guest-shutdown'}}, {'return':{},'id':1}]
        api = mock.Mock()
        inbox = queue.Queue()
        scope = {'process':SimpleNamespace(stdout=io.StringIO('\n'.join(map(json.dumps,messages)))),
                 'datetime':datetime,'timezone':timezone,'json':json,'qmp_events':deque(maxlen=64),
                 'runtime':api,'output':Path('fixture-output'),'inbox':inbox,'normal_runtime':False,'work_reboot_session':False}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), 'exec'), scope)
        scope['read_qmp']()
        events = api.atomic_json.call_args.args[1]
        self.assertEqual(len(events), 64)
        self.assertEqual(events[0]['data']['reason'], '27')
        self.assertEqual(events[-1]['event'], 'SHUTDOWN')
        self.assertEqual(inbox.get_nowait(), {'return':{},'id':1})
        self.assertEqual(inbox.get_nowait(), {'eof':True})
        self.assertTrue(inbox.empty())

    def test_work_boot_is_allowed_without_administrator_but_build_modes_are_not(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        boundary = next(node for node in tree.body if isinstance(node, ast.If)
                        and ast.unparse(node.test) == "args.guest != 'work-boot'")
        api = mock.Mock()
        api.windll.shell32.IsUserAnAdmin.return_value = 0
        for guest in ('work-boot', 'work-install', 'gateway-boot'):
            scope = {'args': SimpleNamespace(guest=guest), 'ctypes': api}
            code = compile(ast.Module(body=[boundary], type_ignores=[]), str(SOURCE), 'exec')
            if guest == 'work-boot':
                exec(code, scope)
                api.windll.shell32.IsUserAnAdmin.assert_not_called()
            else:
                with self.assertRaises(AssertionError):
                    exec(code, scope)

    def test_pre_spawn_boundary_rechecks_exact_gateway_before_windows_open(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        boundary = next(node for node in tree.body if isinstance(node, ast.If)
                        and ast.unparse(node.test) == "args.guest == 'work-boot' and args.network == 'isolated-lan'")
        api = mock.Mock()
        api.gateway_ready.return_value = False
        gateway = {'nonce': 'a'*32}
        scope = {'args': SimpleNamespace(guest='work-boot', network='isolated-lan'),
                 'runtime': api, 'root': 'fixture-root', 'gateway': gateway}
        with self.assertRaisesRegex(AssertionError, 'stopped or changed'):
            exec(compile(ast.Module(body=[boundary], type_ignores=[]), str(SOURCE), 'exec'), scope)
        api.gateway_ready.assert_called_once_with('fixture-root', gateway)

    def setUp(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'handle_control']
        self.assertEqual(len(functions), 1, 'Control failures need an isolated recoverable boundary')
        self.commands = []
        self.saved = []
        self.scope = {
            'report': {'network': 'isolated-lan', 'status': 'running'},
            'command': self.command, 'save': lambda: self.saved.append(True),
            'capture': lambda label: None,'normal_runtime':False,'work_reboot_session':False,'process':mock.Mock(poll=mock.Mock(return_value=None)),
            'queue': queue,
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

    def test_qmp_timeout_marks_control_unavailable_without_ending_windows(self):
        for error in (queue.Empty(), TimeoutError(), BrokenPipeError()):
            with self.subTest(error=type(error).__name__):
                self.scope['report'] = {'status': 'running'}
                self.scope['command'] = mock.Mock(side_effect=error)
                self.scope['handle_control']({'action': 'powerdown'})
                self.assertEqual(self.scope['report']['status'], 'control-unavailable-guest-running')
                self.scope['process'].kill.assert_not_called()
                self.scope['process'].terminate.assert_not_called()

    def test_display_poll_detects_lost_and_recovered_qmp_without_a_guest_receipt(self):
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='read_display_receipt')
        self.scope.update(display_buffer='',display_settings={'schema':1},session_nonce='a'*32,
                          windows_settings=mock.Mock())
        exec(compile(ast.Module(body=[function],type_ignores=[]),str(SOURCE),'exec'),self.scope)
        self.scope['command']=mock.Mock(side_effect=queue.Empty())
        self.scope['read_display_receipt']()
        self.assertEqual(self.scope['report']['status'],'control-unavailable-guest-running')
        for state in ('running', 'paused'):
            with self.subTest(recovered_state=state):
                self.scope['report']['status']='control-unavailable-guest-running'
                self.scope['command']=mock.Mock(side_effect=['',{'status':state}])
                self.scope['read_display_receipt']()
                self.assertEqual(self.scope['report']['status'],state)
                self.assertEqual(self.scope['report']['windows_display']['status'],'unconfirmed')

    def test_powerdown_uses_acpi(self):
        from datetime import datetime, timezone
        self.scope.update(datetime=datetime, timezone=timezone)
        self.scope['handle_control']({'action': 'powerdown'})
        self.assertEqual(self.commands, ['system_powerdown'])
        self.assertIn('acpi_powerdown_requested_utc', self.scope['report'])

if __name__ == '__main__':
    unittest.main()
