import importlib.util
import ctypes
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
SPEC = importlib.util.spec_from_file_location('windows_guest_settings', ROOT / 'scripts/windows_guest_settings.py')
guest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guest)


class WindowsGuestSettingsTests(unittest.TestCase):
    NONCE = 'a1' * 16

    def temporary(self):
        return tempfile.TemporaryDirectory(prefix='guest-settings-test-', dir=ROOT / 'state')

    def receipt(self):
        return {'schema': 1, 'session_nonce': self.NONCE,
                'requested': {'width': 1920, 'height': 1080},
                'current': {'width': 1920, 'height': 1080}, 'success': True, 'code': 'OK'}

    def test_missing_file_returns_independent_defaults(self):
        with self.temporary() as temporary:
            first = guest.load_settings(Path(temporary))
            first['width'] = 800
            self.assertEqual(guest.load_settings(Path(temporary)), {'schema': 1, 'width': 1920, 'height': 1080, 'fit': False})

    def test_load_merges_partial_settings_from_selected_root(self):
        with self.temporary() as temporary:
            root = Path(temporary)
            (root / 'config').mkdir()
            (root / 'config/windows.json').write_text('{"width":1600,"height":900}', encoding='utf-8')
            self.assertEqual(guest.load_settings(root), {'schema': 1, 'width': 1600, 'height': 900, 'fit': False})

    def test_settings_reject_unknown_modes_secrets_types_and_disk_options(self):
        invalid = ({'password': 'private'}, {'width': True}, {'schema': True}, {'height': 719},
                   {'width': 1280}, {'width': '1280'}, {'disk_size_gib': 80}, [])
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                guest.validate_settings(value)
        for width, height in guest.MODES:
            self.assertEqual(guest.validate_settings({'width': width, 'height': height})['width'], width)

    def test_old_screen_fit_choice_becomes_the_standard_default(self):
        # A saved window-sized mode garbled the next start from another monitor (live, 22.10).
        self.assertEqual(guest.validate_settings({'fit': True, 'width': 1366, 'height': 768}),
                         {'schema': 1, 'width': 1920, 'height': 1080, 'fit': False})

    def test_vga_memory_covers_the_configured_surface(self):
        self.assertEqual(guest.vga_memory_mib({'width': 1920, 'height': 1080}), 16)
        self.assertEqual(guest.vga_memory_mib({'width': 3840, 'height': 2160}), 32)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows monitor coordinates')
    def test_monitor_work_area_and_metrics_are_physical_at_125_percent(self):
        user, shcore = mock.Mock(), mock.Mock()
        user.SetThreadDpiAwarenessContext.return_value = 111
        user.MonitorFromPoint.return_value = 123
        def monitor_info(handle, reference):
            info = reference._obj
            for rect, bottom in ((info.rcMonitor, 1440), (info.rcWork, 1380)):
                rect.left, rect.top, rect.right, rect.bottom = 0, 0, 2560, bottom
            return True
        def dpi(handle, kind, horizontal, vertical):
            horizontal._obj.value = vertical._obj.value = 120
            return 0
        user.GetMonitorInfoW.side_effect = monitor_info
        shcore.GetDpiForMonitor.side_effect = dpi
        user.GetSystemMetricsForDpi.side_effect = lambda index, dpi: {32: 10, 92: 5, 4: 29}[index] if dpi == 120 else 0
        with mock.patch.object(ctypes, 'WinDLL', side_effect=lambda name, **kwargs: user if name == 'user32' else shcore):
            display = guest.host_display()
        self.assertEqual(display['screen'], (2560, 1440))
        self.assertEqual(display['work'], (2560, 1380))
        self.assertEqual(display['monitor'], (0, 0, 2560, 1380))
        self.assertEqual((display['frame'], display['caption']), (15, 29))
        self.assertEqual(user.SetThreadDpiAwarenessContext.call_args.args[0].value, 111)

    def test_request_allows_fitted_sizes_but_saved_settings_only_listed_ones(self):
        self.assertEqual(guest.validate_request({'width': 1752, 'height': 1096})['width'], 1752)
        with self.assertRaises(ValueError):
            guest.validate_settings({'width': 1752, 'height': 1096})
        for bad in ({'width': 1750, 'height': 1096}, {'width': 600, 'height': 400}, {'width': 8000, 'height': 4000}, {'fit': 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                guest.validate_request(bad)

    def test_load_rejects_duplicate_and_oversize_json(self):
        with self.temporary() as temporary:
            folder = Path(temporary) / 'config'
            folder.mkdir()
            for content in ('{"width":1280,"width":800}', ' ' * 8193):
                (folder / 'windows.json').write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError):
                    guest.load_settings(Path(temporary))

    def test_settings_directory_contains_exact_current_session_payload(self):
        with self.temporary() as temporary:
            folder = Path(temporary)
            identities = guest.write_settings(folder, {}, self.NONCE)
            self.assertEqual(set(identities), {'SETTINGS.JSN'})
            self.assertEqual(json.loads((folder / 'SETTINGS.JSN').read_bytes()), {'schema': 1, 'width': 1920, 'height': 1080, 'session_nonce': self.NONCE})
            self.assertEqual(identities['SETTINGS.JSN']['file_id'], (folder / 'SETTINGS.JSN').stat().st_ino)
            with self.assertRaises(ValueError):
                guest.write_settings(folder, {}, self.NONCE)

    def test_setup_directory_preserves_executable_larger_than_64k(self):
        executable = b'MZ' + bytes(range(256)) * 513
        with self.temporary() as temporary:
            folder = Path(temporary)
            guest.write_setup(folder, executable)
            self.assertEqual(set(path.name for path in folder.iterdir()), {'SETUP.JSN', 'GUESTSET.EXE', 'RUNSETUP.CMD'})
            self.assertEqual((folder / 'GUESTSET.EXE').read_bytes(), executable)
            self.assertEqual(json.loads((folder / 'SETUP.JSN').read_bytes()), {'schema': 1, 'app': 'WindowsClaude'})
            self.assertIn(b'"%~dp0GUESTSET.EXE" --install', (folder / 'RUNSETUP.CMD').read_bytes())

    def test_media_arguments_are_readonly_and_preserve_paths_without_interpolation(self):
        directory = 'state/path with spaces,and=equals'
        result = guest.media_arguments(directory)
        block = json.loads(result[result.index('-blockdev') + 1])
        self.assertEqual(block['dir'], directory)
        self.assertEqual(block['label'], 'WCPSETTINGS')
        self.assertTrue(block['read-only'])
        self.assertFalse(block['rw'])
        self.assertIn('usb-storage,drive=wcsettings,bus=wcsettingsusb.0,removable=on', result)
        setup = guest.media_arguments(directory, 'WCPSETUP')
        self.assertEqual(json.loads(setup[1])['node-name'], 'wcsetup')
        self.assertIn('usb-storage,drive=wcsetup,bus=wcsetupusb.0,removable=on', setup)
        for label in ('../X', 'not-a-volume', None):
            with self.assertRaises(ValueError):
                guest.media_arguments(directory, label)
        for nonce in ('A1' * 16, 'x' * 32, self.NONCE + '0', True):
            with self.temporary() as temporary, self.assertRaises(ValueError):
                guest.write_settings(Path(temporary), {}, nonce)

    def test_native_qemu_vvfat_exposes_requested_volume_label(self):
        with self.temporary() as temporary:
            root = Path(temporary)
            folder = root / 'handoff'
            folder.mkdir()
            guest.write_settings(folder, {}, self.NONCE)
            block = json.loads(guest.media_arguments(folder.relative_to(ROOT).as_posix())[1])
            block.pop('node-name')
            boot = root / 'boot.bin'
            # Read only one FAT16 boot sector, rather than converting the virtual disk.
            result = subprocess.run([str(ROOT / 'runtime/qemu-11.1.0/qemu-img.exe'), 'dd',
                                     'if=json:' + json.dumps(block, separators=(',', ':')),
                                     'of=' + str(boot), 'bs=512', 'skip=63', 'count=64'],
                                    cwd=ROOT, capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
            content = boot.read_bytes()
            self.assertEqual(len(content), 512, (result.stdout + result.stderr).decode(errors='replace'))
            self.assertEqual(content[510:512], b'\x55\xaa')
            self.assertEqual(struct.unpack_from('<H', content, 11)[0], 512)
            self.assertEqual(content[43:54], b'WCPSETTINGS')
            self.assertEqual(content[54:62], b'FAT16   ')

    def test_receipt_accepts_matching_success_and_matching_failure(self):
        receipt = self.receipt()
        self.assertEqual(guest.validate_receipt(json.dumps(receipt), {}, self.NONCE), receipt)
        receipt.update(success=False, code='DISPLAY_MODE_UNSUPPORTED')
        receipt['current']['width'] = 1024
        self.assertFalse(guest.validate_receipt(json.dumps(receipt).encode(), {}, self.NONCE)['success'])

    def test_receipt_rejects_stale_or_mismatched_requested_settings(self):
        for section, key, value in [(None, 'session_nonce', 'b2' * 16), ('requested', 'width', 800)]:
            receipt = self.receipt()
            (receipt if section is None else receipt[section])[key] = value
            with self.subTest(section=section, key=key), self.assertRaises(ValueError):
                guest.validate_receipt(json.dumps(receipt), {}, self.NONCE)

    def test_receipt_rejects_false_success_types_bounds_and_extra_data(self):
        for key, value in [('schema', True), ('success', 1), ('code', 'OK\nsecret'),
                           ('extra', 'data'), ('display_code', 'DISPLAY_MODE_UNSUPPORTED')]:
            receipt = self.receipt()
            receipt[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                guest.validate_receipt(json.dumps(receipt), {}, self.NONCE)
        for key, value in [('width', 1024), ('height', True), ('width', -1), ('height', 5000)]:
            receipt = self.receipt()
            receipt['current'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                guest.validate_receipt(json.dumps(receipt), {}, self.NONCE)
        receipt = self.receipt()
        receipt.update(success=False)
        with self.assertRaises(ValueError):
            guest.validate_receipt(json.dumps(receipt), {}, self.NONCE)

    def test_receipt_rejects_oversize_duplicates_and_malformed_utf8(self):
        for content in (' ' * 8193, '{"schema":1,"schema":1}', b'\xff', '{}', '[]'):
            with self.subTest(content=repr(content[:80])), self.assertRaises(ValueError):
                guest.validate_receipt(content, {}, self.NONCE)


if __name__ == '__main__':
    unittest.main()
