import ast
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import window_branding


class WindowBrandingTests(unittest.TestCase):
    def test_every_icon_frame_is_a_png_of_its_own_size(self):
        self.assertEqual(sorted(window_branding.FRAMES), [16, 32, 64, 256])
        for size, encoded in window_branding.FRAMES.items():
            data = base64.b64decode(encoded)
            self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II', data[16:24]), (size, size))

    def test_window_icon_differs_from_the_panel_icon(self):
        panel = (ROOT / 'ui/AppIcon.ico').read_bytes()
        for encoded in window_branding.FRAMES.values():
            self.assertNotIn(base64.b64decode(encoded), panel)

    def test_branding_and_pointer_release_are_wired_into_the_controller(self):
        text = (ROOT / 'scripts/control_guest_install.py').read_text(encoding='utf-8')
        tree = ast.parse(text)
        names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertLessEqual({'brand_work_window', 'release_pointer_in_fullscreen'}, names)
        self.assertIn("WINDOW_TITLE = 'Pocket Windows'", text)
        self.assertIn("env['SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS']='0'", text)
        branding = ast.get_source_segment(text, next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'brand_work_window'))
        self.assertIn('SendMessageTimeoutW', branding)
        self.assertNotIn('SetWindowTextW', branding)  # a plain call would wait for QEMU's UI thread

    @unittest.skipUnless(os.name == 'nt', 'Native Windows/SDL first-show check')
    def test_real_qemu_is_branded_before_its_first_visible_window(self):
        tree = ast.parse((ROOT / 'scripts/control_guest_install.py').read_text(encoding='utf-8'))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ('work_startup_info', 'prepare_taskbar_icon', 'set_taskbar_identity',
                                       'brand_work_window', 'show_work_window', 'clear_work_taskbar')]
        temporary = tempfile.TemporaryDirectory(prefix='Pocket taskbar Тест ')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        controller = {'ctypes': ctypes, 'subprocess': subprocess, 'normal_runtime': True,
                      'report': {}, 'branding_icons': {}, 'WINDOW_TITLE': 'Pocket Windows',
                      'os': os, 'uuid': uuid, 'hashlib': hashlib, 'Path': Path, 'root': root}
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'window-branding-controller', 'exec'), controller)
        controller['taskbar_icon'] = controller['prepare_taskbar_icon'](root)
        user = ctypes.WinDLL('user32', use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPVOID,
                                           wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        args = ['-S', '-machine', 'q35,i8042=off', '-accel', 'whpx', '-cpu', 'Westmere', '-m', '256',
                '-L', 'runtime/qemu-11.1.0/share', '-nic', 'none', '-vga', 'std',
                '-device', 'qemu-xhci,id=usb', '-device', 'usb-tablet,bus=usb.0', '-device', 'usb-kbd,bus=usb.0',
                '-display', 'sdl,gl=off,window-close=off', '-qmp', 'stdio', '-monitor', 'none']
        process = subprocess.Popen([str(ROOT / 'runtime/qemu-11.1.0/qemu-system-x86_64.exe'), *args], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            startupinfo=controller['work_startup_info'](), creationflags=subprocess.CREATE_NO_WINDOW)
        controller['process'] = process
        def finish():
            try:
                if process.poll() is None:
                    process.stdin.write(b'{"execute":"qmp_capabilities"}\n{"execute":"quit"}\n')
                    process.stdin.flush()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()  # This test has no disk or guest OS.
                        process.wait(timeout=5)
            finally:
                process.stdin.close()
        self.addCleanup(finish)
        process.stdin.write(b'{"execute":"qmp_capabilities"}\n')
        process.stdin.flush()
        windows = []
        def visit(window, unused):
            pid, name = wintypes.DWORD(), ctypes.create_unicode_buffer(64)
            user.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if pid.value == process.pid:
                user.GetClassNameW(window, name, len(name))
                if name.value == 'SDL_app' and window not in windows:
                    windows.append(window)
            return True
        deadline = time.monotonic() + 10
        while not windows and time.monotonic() < deadline and process.poll() is None:
            user.EnumWindows(callback_type(visit), 0)
            time.sleep(.01)
        self.assertTrue(windows, 'QEMU did not create its SDL window')
        window = windows[0]
        for _ in range(20):
            self.assertFalse(user.IsWindowVisible(window), 'Stock QEMU icon could reach the taskbar')
            time.sleep(.01)
        while not controller['show_work_window'](7) and time.monotonic() < deadline:
            self.assertFalse(user.IsWindowVisible(window))
            time.sleep(.01)
        self.assertTrue(controller['report'].get('window_icon_set'))
        checked = set()
        deadline = time.monotonic() + 5
        while checked != {0, 1} and time.monotonic() < deadline:
            for kind, role in ((0, 'small'), (1, 'big')):
                result = ctypes.c_size_t()
                sent = user.SendMessageTimeoutW(window, 0x007f, kind, None, 2, 200, ctypes.byref(result))
                if sent:
                    self.assertEqual(result.value, controller['branding_icons'][role])
                    checked.add(kind)
                else:
                    # SDL can be busy while processing the first minimize event.
                    self.assertEqual(ctypes.get_last_error(), 1460)
            time.sleep(.01)
        self.assertEqual(checked, {0, 1}, 'QEMU must respond with both product icons')
        self.assertTrue(user.IsWindowVisible(window), 'Branded window must be available in the taskbar')
        self.assert_taskbar_properties(window, controller)
        controller['clear_work_taskbar']()
        self.assertEqual(controller['report'].get('window_taskbar_cleanup'), 'cleared')
        self.assert_taskbar_properties(window, controller, cleared=True)

    def assert_taskbar_properties(self, window, controller, cleared=False):
        shell, ole = ctypes.WinDLL('shell32'), ctypes.WinDLL('ole32')
        shell.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        ole.PropVariantClear.argtypes = [ctypes.c_void_p]
        self.assertGreaterEqual(ole.CoInitializeEx(None, 2), 0)
        store = ctypes.c_void_p()
        interface = ctypes.create_string_buffer(uuid.UUID('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99').bytes_le)
        try:
            self.assertEqual(shell.SHGetPropertyStoreForWindow(window, interface, ctypes.byref(store)), 0)
            vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            get_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(vtable[5])
            release = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])
            try:
                expected = {2: f'"{controller["root"] / "Launcher.exe"}"',
                            3: str(controller['taskbar_icon']) + ',0', 4: 'Pocket Windows',
                            5: controller['report']['window_taskbar_id']}
                self.assertTrue(expected[5].startswith('PocketWindows.Desktop.'))
                for number, text in expected.items():
                    key = ctypes.create_string_buffer(uuid.UUID('9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3').bytes_le + struct.pack('<I', number))
                    value = ctypes.create_string_buffer(24)
                    self.assertEqual(get_value(store, key, value), 0)
                    try:
                        self.assertEqual(struct.unpack_from('<H', value)[0], 0 if cleared else 31)
                        if not cleared:
                            self.assertEqual(ctypes.wstring_at(ctypes.c_void_p.from_buffer(value, 8).value), text)
                    finally:
                        ole.PropVariantClear(value)
            finally:
                release(store)
        finally:
            ole.CoUninitialize()

    def test_taskbar_cleanup_waits_for_actual_shutdown(self):
        tree = ast.parse((ROOT / 'scripts/control_guest_install.py').read_text(encoding='utf-8'))
        reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'read_qmp')
        cleanup = mock.Mock()
        scope = {'process': SimpleNamespace(stdout=[json.dumps({'event': name}) for name in ('STOP', 'RESUME', 'SHUTDOWN')]),
                 'json': json, 'datetime': datetime, 'timezone': timezone, 'work_reboot_session': True,
                 'normal_runtime': True, 'report': {'generation': 1}, 'qmp_events': deque(),
                 'runtime': mock.Mock(), 'output': Path('unused'), 'inbox': queue.Queue(), 'clear_work_taskbar': cleanup}
        exec(compile(ast.Module(body=[reader], type_ignores=[]), 'taskbar-shutdown', 'exec'), scope)
        scope['read_qmp']()
        cleanup.assert_called_once_with()

    def test_late_window_is_still_shown_after_a_minute(self):
        tree = ast.parse((ROOT / 'scripts/control_guest_install.py').read_text(encoding='utf-8'))
        blocks = [next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                       and f"report.get('window_parked') is {value}" in ast.unparse(node.test)
                       and 'show_work_window(' in ast.unparse(node))
                  for value in ('False', 'True')]
        show = mock.Mock(side_effect=[False, True, True])
        scope = {'normal_runtime': True, 'report': {'window_parked': False}, 'begin': 0, 'display_poll': 5,
                 'time': mock.Mock(monotonic=mock.Mock(return_value=90)), 'show_work_window': show,
                 'pointer_settled': mock.Mock(return_value=True), 'save': mock.Mock()}
        code = compile(ast.Module(body=blocks, type_ignores=[]), 'late-window', 'exec')
        exec(code, scope)
        self.assertIs(scope['report']['window_parked'], False)
        exec(code, scope)
        self.assertEqual(show.call_args_list, [mock.call(7), mock.call(7), mock.call(9)])
        self.assertEqual(scope['report']['window_parked'], 'released')


if __name__ == '__main__':
    unittest.main()
