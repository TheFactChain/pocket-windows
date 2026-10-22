import ctypes
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import portable_app as app


class DesktopWindowTests(unittest.TestCase):
    def test_only_the_current_owned_guest_window_is_restored(self):
        show = getattr(app, 'show_desktop', None)
        self.assertTrue(callable(show), 'The running desktop needs a separate open action')
        identity = {'pid': 456, 'created_filetime': 10, 'image': 'fixture-qemu.exe'}
        user = mock.Mock()
        user.IsWindowVisible.return_value = True
        user.SetForegroundWindow.return_value = True
        user.EnumWindows.side_effect = lambda callback, unused: (callback(11, 0), callback(22, 0))
        def identify(window, pid):
            pid._obj.value = 123 if window == 11 else 456
        user.GetWindowThreadProcessId.side_effect = identify
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(app.runtime, 'discover_guests', return_value={'work': {'process': identity}}), \
                mock.patch.object(app.runtime, 'process_identity', return_value=identity), \
                mock.patch.object(ctypes, 'WinDLL', return_value=user):
            result = show(Path(temporary))
        self.assertTrue(result['ok'])
        user.ShowWindowAsync.assert_called_once_with(22, 9)  # async: a busy QEMU UI thread cannot stall the panel
        user.SetForegroundWindow.assert_called_once_with(22)

    def test_exited_or_reused_process_never_activates_a_window(self):
        show = getattr(app, 'show_desktop', None)
        self.assertTrue(callable(show), 'The running desktop needs a separate open action')
        identity = {'pid': 456, 'created_filetime': 10, 'image': 'fixture-qemu.exe'}
        for guests in ({}, {'work': {'process': identity}}):
            with self.subTest(guests=guests), tempfile.TemporaryDirectory() as temporary, \
                    mock.patch.object(app.runtime, 'discover_guests', return_value=guests), \
                    mock.patch.object(app.runtime, 'process_identity', return_value=identity | {'created_filetime': 20}), \
                    mock.patch.object(ctypes, 'WinDLL') as native:
                self.assertFalse(show(Path(temporary))['ok'])
                native.assert_not_called()


if __name__ == '__main__':
    unittest.main()
