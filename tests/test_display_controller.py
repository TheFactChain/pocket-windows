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
import launch_runtime as runtime


SCREEN = {'screen': (1920, 1200), 'work': (1920, 1152), 'frame': 8, 'caption': 23}


class DisplayControllerTests(unittest.TestCase):
    def tree(self):
        return ast.parse((ROOT / 'scripts/control_guest_install.py').read_text(encoding='utf-8'))

    def test_every_qemu_launch_overrides_inherited_dpi_without_changing_the_parent(self):
        inherited = {'SystemRoot': r'C:\Windows', 'PATH': 'untrusted-path',
                     'SDL_WINDOWS_DPI_AWARENESS': 'unaware', 'SDL_WINDOWS_DPI_SCALING': '1'}
        for script in ('control_guest_install.py', 'reference_vm.py'):
            with self.subTest(script=script), mock.patch.dict(os.environ, inherited, clear=True):
                parent = dict(os.environ)
                tree = ast.parse((ROOT / 'scripts' / script).read_text(encoding='utf-8'))
                assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                               and any((isinstance(target, ast.Name) and target.id == 'env')
                                       or (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                                           and target.value.id == 'env') for target in node.targets)]
                scope = dict(os=os, root=ROOT, engine=runtime.engine_path(ROOT), runtime=runtime)
                exec(compile(ast.Module(body=assignments, type_ignores=[]), script, 'exec'), scope)
                child = scope['env']
                self.assertEqual(child.get('SDL_WINDOWS_DPI_AWARENESS'), 'permonitorv2')
                self.assertEqual(child.get('SDL_WINDOWS_DPI_SCALING'), '0')
                self.assertEqual(child['PATH'], str(runtime.engine_path(ROOT).parent) + r';C:\Windows\System32;C:\Windows')
                self.assertEqual(dict(os.environ), parent)

    def test_normal_boot_attaches_readonly_current_session_settings_and_safe_close(self):
        block = next(node for node in self.tree().body if isinstance(node, ast.If)
                     and ast.unparse(node.test) == 'normal_runtime')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'logs/session'
            output.mkdir(parents=True)
            arguments = ['-machine', 'q35', '-display', 'sdl,gl=off', '-serial', 'none']
            scope = dict(root=root, output=output, arguments=arguments, normal_runtime=True,
                         session_nonce='a' * 32, windows_settings=settings,
                         load_viewer_settings=config.load_viewer_settings)
            with mock.patch.object(settings, 'host_display', return_value=SCREEN):
                exec(compile(ast.Module(body=[block], type_ignores=[]), 'normal-boot', 'exec'), scope)
            medium = json.loads((output / 'display-settings/SETTINGS.JSN').read_bytes())
            self.assertEqual(medium['session_nonce'], 'a' * 32)
            device = json.loads(arguments[arguments.index('-blockdev') + 1])
            self.assertTrue(device['read-only'])
            self.assertFalse(device['rw'])
            self.assertEqual(device['label'], 'WCPSETTINGS')
            self.assertEqual(arguments[arguments.index('-serial') + 1], 'chardev:wcdisplay')
            self.assertEqual(arguments[arguments.index('-display') + 1], 'sdl,gl=off,window-close=off')
            # One standard size whatever monitor Start was clicked on; the monitor only sizes the window.
            self.assertIn('VGA.xres=1920', arguments)
            self.assertIn('VGA.yres=1080', arguments)
            self.assertIn('usb-tablet,id=wcabspointer,bus=wcsettingsusb.0', arguments)
            # No PS/2 controller: the pointer never switches back to relative mode (QEMU SDL deadlock).
            self.assertEqual(arguments[arguments.index('-machine') + 1], 'q35,i8042=off')
            self.assertIn('usb-kbd,id=wckeyboard,bus=wcsettingsusb.0', arguments)

    def test_fullscreen_preference_changes_only_the_viewer(self):
        block = next(node for node in self.tree().body if isinstance(node, ast.If)
                     and ast.unparse(node.test) == 'normal_runtime')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'logs/session'
            output.mkdir(parents=True)
            arguments = ['-machine', 'q35', '-display', 'sdl,gl=off', '-serial', 'none']
            scope = dict(root=root, output=output, arguments=arguments, normal_runtime=True,
                         session_nonce='a' * 32, windows_settings=settings,
                         load_viewer_settings=lambda root: {'schema': 1, 'start_fullscreen': True, 'remember_window': True})
            with mock.patch.object(settings, 'host_display', return_value=SCREEN):
                exec(compile(ast.Module(body=[block], type_ignores=[]), 'normal-boot', 'exec'), scope)
            self.assertEqual(arguments[arguments.index('-display') + 1],
                             'sdl,gl=off,window-close=off,full-screen=on')
            # Full screen keeps Windows' own size too; SDL scales it to the monitor.
            self.assertIn('VGA.xres=1920', arguments)
            self.assertIn('VGA.yres=1080', arguments)

    def test_normal_boot_uses_saved_guest_mode_for_vga(self):
        block = next(node for node in self.tree().body if isinstance(node, ast.If)
                     and ast.unparse(node.test) == 'normal_runtime')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'config').mkdir()
            (root / 'config/windows.json').write_text('{"width":2560,"height":1440}', encoding='utf-8')
            output = root / 'logs/session'
            output.mkdir(parents=True)
            arguments = ['-machine', 'q35', '-display', 'sdl,gl=off', '-serial', 'none']
            scope = dict(root=root, output=output, arguments=arguments, normal_runtime=True,
                         session_nonce='a' * 32, windows_settings=settings,
                         load_viewer_settings=config.load_viewer_settings)
            with mock.patch.object(settings, 'host_display', return_value=SCREEN):
                exec(compile(ast.Module(body=[block], type_ignores=[]), 'normal-boot', 'exec'), scope)
            self.assertEqual([arguments[i + 1] for i, name in enumerate(arguments) if name == '-global'],
                             ['VGA.xres=2560', 'VGA.yres=1440', 'VGA.vgamem_mb=16'])

    def test_controller_never_retitles_the_qemu_window(self):
        # The size observer stays read-only; the caption and icon are set only by brand_work_window, with timeouts.
        function = next(node for node in self.tree().body if isinstance(node, ast.FunctionDef) and node.name == 'observe_work_window')
        text = (ROOT / 'scripts/control_guest_install.py').read_text(encoding='utf-8')
        self.assertNotIn('0x000C', ast.get_source_segment(text, function))
        self.assertNotIn('window_title', [node.name for node in self.tree().body if isinstance(node, ast.FunctionDef)])

    @unittest.skipUnless(os.name == 'nt', 'Windows geometry observation')
    def test_observer_records_actual_four_by_three_client_without_imposing_requested_mode(self):
        display = dict(work=(1920, 1040), frame=8, caption=23, monitor=(0, 0, 1920, 1040))
        function, user, state, scope = self.window_function('observe_work_window', display)
        state.client = (1024, 768)
        with mock.patch.object(ctypes, 'WinDLL', return_value=user), \
                mock.patch.object(settings, 'host_display', return_value=display):
            self.assertTrue(function())
            self.assertTrue(function())
        self.assertEqual(scope['report']['window_geometry']['client'], [1024, 768])
        self.assertEqual(scope['report']['window_geometry']['owner'], 'qemu-sdl')
        self.assertNotIn('target', scope['report']['window_geometry'])
        user.SetWindowPos.assert_not_called()
        scope['save'].assert_called_once()
        self.assertEqual(state.dpi, 111)

    def window_function(self, name, display):
        function = next(node for node in self.tree().body if isinstance(node, ast.FunctionDef) and node.name == name)
        state = SimpleNamespace(client=(1920, 1080), position=(0, 0), dpi=111, contexts=[])
        user = mock.Mock()
        user.EnumWindows.side_effect = lambda callback, unused: callback(11, 0)
        def identify(window, pid):
            pid._obj.value = 456
        def context(value):
            previous, state.dpi = state.dpi, value.value
            return previous
        def client_rect(window, reference):
            state.contexts.append(state.dpi)
            rect = reference._obj
            rect.left, rect.top, rect.right, rect.bottom = 0, 0, *state.client
            return True
        def outer_rect(window, reference):
            state.contexts.append(state.dpi)
            rect = reference._obj
            rect.left, rect.top = state.position
            rect.right, rect.bottom = rect.left + state.client[0] + 16, rect.top + state.client[1] + 39
            return True
        user.GetWindowThreadProcessId.side_effect = identify
        user.SetThreadDpiAwarenessContext.side_effect = context
        user.IsWindowVisible.return_value = user.SendMessageTimeoutW.return_value = True
        from ctypes import wintypes
        timeout_signature = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                               wintypes.WPARAM, ctypes.c_void_p, wintypes.UINT,
                                               wintypes.UINT, ctypes.POINTER(ctypes.c_size_t))
        user.SendMessageTimeoutW = timeout_signature(lambda window, message, wp, lp, flags, timeout, result: 1)
        user.IsIconic.return_value = user.IsZoomed.return_value = False
        user.GetWindowLongPtrW.return_value = 0x00C00000
        user.GetClientRect.side_effect, user.GetWindowRect.side_effect = client_rect, outer_rect
        user.SetWindowPos.return_value = True
        scope = dict(ctypes=ctypes, process=SimpleNamespace(pid=456), report={'window_parked': 'released'},
                     display_settings=settings.DEFAULTS, host_display=display, windows_settings=settings, save=mock.Mock())
        exec(compile(ast.Module(body=[function], type_ignores=[]), name, 'exec'), scope)
        return scope[name], user, state, scope

    @unittest.skipUnless(os.name == 'nt', 'Windows geometry observation')
    def test_oversized_or_unmeasurable_window_is_not_ready_for_placement(self):
        display = dict(work=(1920, 1032), frame=8, caption=23, monitor=(0, 0, 1920, 1032))
        function, user, state, scope = self.window_function('observe_work_window', display)
        with mock.patch.object(ctypes, 'WinDLL', return_value=user), \
                mock.patch.object(settings, 'host_display', return_value=display):
            self.assertFalse(function())
            self.assertFalse(scope['report']['window_geometry']['fits_work_area'])
            user.GetClientRect.side_effect = None
            user.GetClientRect.return_value = False
            self.assertFalse(function())
        user.SetWindowPos.assert_not_called()

    def test_controller_does_not_compete_with_sdl_for_window_size(self):
        function = next(node for node in self.tree().body if isinstance(node, ast.FunctionDef)
                        and node.name == 'observe_work_window')
        calls = [node.func.attr for node in ast.walk(function) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)]
        self.assertNotIn('SetWindowPos', calls)
        self.assertNotIn('window_size', calls)

    @unittest.skipUnless(os.name == 'nt', 'Windows window placement regression')
    def test_placement_waits_until_the_owned_window_reaches_its_target(self):
        function, user, state, scope = self.window_function('place_work_window', SCREEN)
        state.client = (960, 540)
        with mock.patch.object(ctypes, 'WinDLL', return_value=user):
            user.SetWindowPos.return_value = False
            self.assertFalse(function((0, 0, 1920, 1040)))
            user.SetWindowPos.return_value = True
            self.assertFalse(function((0, 0, 1920, 1040)), 'A queued move is not proof of actual placement')
            calls = user.SetWindowPos.call_count
            state.position = (472, 230)
            self.assertTrue(function((0, 0, 1920, 1040)))
            self.assertEqual(user.SetWindowPos.call_count, calls, 'An already placed window must not move again')

    def test_response_must_confirm_current_nonce_and_actual_resolution(self):
        function = next(node for node in self.tree().body if isinstance(node, ast.FunctionDef)
                        and node.name == 'read_display_receipt')
        receipt = dict(schema=1, session_nonce='a' * 32, requested=dict(width=1920, height=1080),
                       current=dict(width=1920, height=1080), success=True, code='OK', display_code='OK')
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

    def test_firmware_text_before_the_receipt_does_not_make_the_display_unconfirmed(self):
        function = next(node for node in self.tree().body if isinstance(node, ast.FunctionDef)
                        and node.name == 'read_display_receipt')
        receipt = dict(schema=1, session_nonce='a' * 32, requested=dict(width=1920, height=1080),
                       current=dict(width=1920, height=1080), success=True, code='OK', display_code='OK')
        boot = '\x1b[2J\x1b[01;01HBdsDxe: loading Boot0005 "Windows Boot Manager"\r\nBdsDxe: starting Boot0005\r\n'
        for data, status in [(boot, None), (boot + json.dumps(receipt) + '\n', 'applied')]:
            report = {}
            scope = dict(command=mock.Mock(return_value=data), report=report, display_buffer='',
                         display_settings=settings.DEFAULTS, session_nonce='a' * 32, generation=1,
                         windows_settings=settings, save=mock.Mock(), queue=queue)
            exec(compile(ast.Module(body=[function], type_ignores=[]), 'display-response', 'exec'), scope)
            scope['read_display_receipt']()
            self.assertEqual(report.get('windows_display', {}).get('status'), status)

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
