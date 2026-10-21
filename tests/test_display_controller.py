import ast
import ctypes
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import windows_guest_settings as settings
import runtime_config as config


class DisplayControllerTests(unittest.TestCase):
    def tree(self):
        return ast.parse((ROOT / 'scripts/control_guest_install.py').read_text(encoding='utf-8'))

    def test_normal_boot_attaches_readonly_current_session_settings_and_safe_close(self):
        block = next(node for node in self.tree().body if isinstance(node, ast.If)
                     and ast.unparse(node.test) == 'normal_runtime')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'logs/session'
            output.mkdir(parents=True)
            arguments = ['-display', 'sdl,gl=off', '-serial', 'none']
            scope = dict(root=root, output=output, arguments=arguments, normal_runtime=True,
                         session_nonce='a' * 32, windows_settings=settings,
                         load_viewer_settings=config.load_viewer_settings)
            exec(compile(ast.Module(body=[block], type_ignores=[]), 'normal-boot', 'exec'), scope)
            medium = json.loads((output / 'display-settings/SETTINGS.JSN').read_bytes())
            self.assertEqual(medium['session_nonce'], 'a' * 32)
            device = json.loads(arguments[arguments.index('-blockdev') + 1])
            self.assertTrue(device['read-only'])
            self.assertFalse(device['rw'])
            self.assertEqual(device['label'], 'WCPSETTINGS')
            self.assertEqual(arguments[arguments.index('-serial') + 1], 'chardev:wcdisplay')
            self.assertEqual(arguments[arguments.index('-display') + 1], 'sdl,gl=off,window-close=off')
            self.assertIn('VGA.xres=1280', arguments)
            self.assertIn('usb-tablet,id=wcabspointer,bus=wcsettingsusb.0', arguments)

    def test_fullscreen_preference_changes_only_the_viewer(self):
        block = next(node for node in self.tree().body if isinstance(node, ast.If)
                     and ast.unparse(node.test) == 'normal_runtime')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'logs/session'
            output.mkdir(parents=True)
            arguments = ['-display', 'sdl,gl=off', '-serial', 'none']
            scope = dict(root=root, output=output, arguments=arguments, normal_runtime=True,
                         session_nonce='a' * 32, windows_settings=settings,
                         load_viewer_settings=lambda root: {'schema': 1, 'start_fullscreen': True, 'remember_window': True})
            exec(compile(ast.Module(body=[block], type_ignores=[]), 'normal-boot', 'exec'), scope)
            self.assertEqual(arguments[arguments.index('-display') + 1],
                             'sdl,gl=off,window-close=off,full-screen=on')

    def test_response_must_confirm_current_nonce_and_actual_resolution(self):
        function = next(node for node in self.tree().body if isinstance(node, ast.FunctionDef)
                        and node.name == 'read_display_receipt')
        receipt = dict(schema=1, session_nonce='a' * 32, requested=dict(width=1280, height=720),
                       current=dict(width=1280, height=720), success=True, code='OK', display_code='OK')
        for nonce, status in [('a' * 32, 'applied'), ('b' * 32, 'unconfirmed')]:
            data = dict(receipt, session_nonce=nonce)
            report = {}
            scope = dict(command=mock.Mock(return_value=json.dumps(data) + '\n'), report=report,
                         display_buffer='', display_settings=settings.DEFAULTS, session_nonce='a' * 32,
                         generation=2, windows_settings=settings, save=mock.Mock(), queue=queue)
            exec(compile(ast.Module(body=[function], type_ignores=[]), 'display-response', 'exec'), scope)
            scope['read_display_receipt']()
            self.assertEqual(report['windows_display']['status'], status)
            if status == 'applied':
                self.assertEqual(report['windows_display']['generation'], 2)
            self.assertEqual(scope['command'].call_args.args[0], 'ringbuf-read')

    @unittest.skipUnless(os.name == 'nt', 'Windows menu ownership regression')
    def test_close_menu_is_disabled_only_for_owned_qemu_process(self):
        function = next(node for node in self.tree().body if isinstance(node, ast.FunctionDef)
                        and node.name == 'protect_work_window')
        user = mock.Mock()
        def enumerate_windows(callback, unused):
            callback(11, 0)
            callback(22, 0)
        def identify(window, pid):
            pid._obj.value = 123 if window == 11 else 456
        user.EnumWindows.side_effect = enumerate_windows
        user.GetWindowThreadProcessId.side_effect = identify
        user.GetSystemMenu.return_value = 77
        scope = dict(ctypes=ctypes, process=SimpleNamespace(pid=456))
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'owned-window', 'exec'), scope)
        with mock.patch.object(ctypes, 'WinDLL', return_value=user):
            self.assertTrue(scope['protect_work_window']())
        user.GetSystemMenu.assert_called_once_with(22, False)
        user.EnableMenuItem.assert_called_once_with(77, 0xF060, 1)
        user.DrawMenuBar.assert_called_once_with(22)


if __name__ == '__main__':
    unittest.main()
