"""Public guest preferences and read-only QEMU vvfat handoff media."""
import json
import os
from pathlib import Path
import re

DEFAULTS = {'schema': 1, 'width': 1920, 'height': 1080, 'fit': False}
MODES = ((800, 600), (1024, 768), (1280, 720), (1280, 800), (1366, 768), (1440, 900),
         (1600, 900), (1680, 1050), (1920, 1080), (1920, 1200), (2560, 1440))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate guest settings field')
        result[key] = value
    return result


def _json(value):
    if not isinstance(value, (str, bytes)) or len(value) > 8192:
        raise ValueError('Guest settings JSON is too large or invalid')
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='strict')
    return json.loads(value, object_pairs_hook=_unique)


def _checked(value):
    if type(value) is not dict or set(value) - set(DEFAULTS):
        raise ValueError('Unknown Windows guest setting')
    result = DEFAULTS | value
    if (type(result['fit']) is not bool or any(type(result[key]) is not int for key in ('schema', 'width', 'height'))
            or result['schema'] != 1):
        raise ValueError('Invalid Windows resolution')
    return result


def validate_settings(value):
    """Saved preference: one listed standard size. Windows saves its desktop mode and the firmware can only
    restore a standard one: an old "as the PC screen" size broke the next start from another monitor
    (garbled picture, 22.10), so that choice now means the default size."""
    result = _checked(value)
    if result['fit']:
        result = dict(DEFAULTS)
    if (result['width'], result['height']) not in MODES:
        raise ValueError('Invalid Windows resolution')
    return result


def validate_request(value):
    """The size actually sent to Windows: a listed mode or one fitted to the screen."""
    result = _checked(value)
    if not (640 <= result['width'] <= 7680 and 480 <= result['height'] <= 4320) or result['width'] % 8:
        raise ValueError('Invalid Windows resolution')
    return result


def host_display(window=None):
    """The window's monitor, or the monitor under Start's cursor, entirely in physical pixels."""
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT), ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.MonitorFromPoint.restype = wintypes.HMONITOR
    user.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user.MonitorFromWindow.restype = wintypes.HMONITOR
    user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MonitorInfo)]
    previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2: physical pixels
    try:
        if window is None:
            point = wintypes.POINT()
            if not user.GetCursorPos(ctypes.byref(point)):
                raise OSError('Cursor position is unavailable')
            monitor = user.MonitorFromPoint(point, 2)
        else:
            monitor = user.MonitorFromWindow(window, 2)  # MONITOR_DEFAULTTONEAREST
        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        if not monitor or not user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise OSError('Screen size is unavailable')
        dpi_x, dpi_y = wintypes.UINT(96), wintypes.UINT(96)
        shcore = ctypes.WinDLL('shcore')
        shcore.GetDpiForMonitor.argtypes = [wintypes.HMONITOR, ctypes.c_int,
                                          ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
        if shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) != 0 or not dpi_x.value:
            dpi_x.value = 96
        size = lambda rect: (rect.right - rect.left, rect.bottom - rect.top)
        metric = (lambda index: user.GetSystemMetricsForDpi(index, dpi_x.value)) if hasattr(user, 'GetSystemMetricsForDpi') else user.GetSystemMetrics
        work = info.rcWork
        return {'screen': size(info.rcMonitor), 'work': size(work), 'frame': metric(32) + metric(92), 'caption': metric(4),
                'monitor': (work.left, work.top, work.right, work.bottom)}
    finally:
        if previous:
            user.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))


def vga_memory_mib(settings):
    needed = settings['width'] * settings['height'] * 4
    return next(size for size in (16, 32, 64, 128) if needed <= size * 1024 * 1024)


def load_settings(root):
    from file_snapshot import locked_read
    from profile_store import _pinned_directory, _plain

    root = Path(root)
    folder = root / 'config'
    with _pinned_directory(root, create=False):
        if not folder.exists():
            return dict(DEFAULTS)
        with _pinned_directory(folder, create=False):
            path = folder / 'windows.json'
            _plain(path)
            if not path.exists():
                return dict(DEFAULTS)
            with locked_read(path) as source:
                _plain(path)
                opened, named = os.fstat(source.fileno()), path.lstat()
                if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                    raise ValueError('Guest settings file identity changed')
                return validate_settings(_json(source.read(8193)))


def _nonce(value):
    if type(value) is not str or re.fullmatch('[a-f0-9]{32}', value) is None:
        raise ValueError('A current lowercase session nonce is required')
    return value


def _write_files(directory, files):
    from profile_store import _pinned_directory, _plain

    directory = Path(directory)
    identities = {}
    with _pinned_directory(directory, create=False):
        if any(directory.iterdir()):
            raise ValueError('Guest handoff directory must be empty')
        for name, data in files.items():
            path = directory / name
            _plain(path)
            with path.open('xb') as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
                info = os.fstat(target.fileno())
            identities[name] = {'device': info.st_dev, 'file_id': info.st_ino}
    return identities


def write_settings(directory, value, nonce):
    payload = {key: item for key, item in validate_request(value).items() if key != 'fit'} | {'session_nonce': _nonce(nonce)}
    return _write_files(directory, {'SETTINGS.JSN': json.dumps(payload, separators=(',', ':')).encode('ascii')})


def write_setup(directory, executable):
    if type(executable) is not bytes or not executable.startswith(b'MZ') or len(executable) > 2 * 1024 ** 2:
        raise ValueError('A bounded compiled guest executable is required')
    return _write_files(directory, {'SETUP.JSN': b'{"schema":1,"app":"WindowsClaude"}',
                                    'GUESTSET.EXE': executable,
                                    'RUNSETUP.CMD': b'@echo off\r\n"%~dp0GUESTSET.EXE" --install\r\nset "WCP_SETUP_EXIT=%errorlevel%"\r\necho Guest settings installation exit code: %WCP_SETUP_EXIT%\r\npause\r\nexit /b %WCP_SETUP_EXIT%\r\n'})


def media_arguments(directory, label='WCPSETTINGS'):
    """Attach to its own USB controller; JSON preserves commas/spaces in paths."""
    if label not in ('WCPSETTINGS', 'WCPSETUP') or not isinstance(directory, (str, Path)) or not str(directory) or '\x00' in str(directory):
        raise ValueError('Invalid guest handoff directory or label')
    node = 'wcsettings' if label == 'WCPSETTINGS' else 'wcsetup'
    block = {'driver': 'vvfat', 'dir': str(directory), 'label': label, 'fat-type': 16,
             'rw': False, 'read-only': True, 'node-name': node}
    return ['-blockdev', json.dumps(block, separators=(',', ':')),
            '-device', 'qemu-xhci,id=' + node + 'usb',
            '-device', 'usb-storage,drive=' + node + ',bus=' + node + 'usb.0,removable=on']


def validate_receipt(data, settings, nonce):
    settings, nonce = validate_request(settings), _nonce(nonce)
    receipt = _json(data)
    keys = {'schema', 'session_nonce', 'requested', 'current', 'success', 'code'}
    if type(receipt) is not dict or not keys <= set(receipt) or set(receipt) - keys - {'display_code'}:
        raise ValueError('Invalid guest settings receipt')
    expected = {key: settings[key] for key in ('width', 'height')}
    requested, current = receipt['requested'], receipt['current']
    if (type(receipt['schema']) is not int or receipt['schema'] != 1
            or receipt['session_nonce'] != nonce or type(receipt['success']) is not bool
            or type(receipt['code']) is not str or re.fullmatch('[A-Z_]{1,64}', receipt['code']) is None
            or type(requested) is not dict or set(requested) != set(expected)
            or any(type(value) is not int for value in requested.values()) or requested != expected
            or type(current) is not dict or set(current) != {'width', 'height'}
            or any(type(value) is not int for value in current.values())):
        raise ValueError('Guest settings receipt does not match the current request')
    if 'display_code' in receipt and (type(receipt['display_code']) is not str or re.fullmatch('[A-Z_]{1,64}', receipt['display_code']) is None):
        raise ValueError('Invalid guest display outcome')
    if (not 0 <= current['width'] <= 7680 or not 0 <= current['height'] <= 4320
            or receipt['success'] != (receipt['code'] == 'OK')):
        raise ValueError('Invalid guest settings outcome')
    if receipt['success'] and (current != expected or receipt.get('display_code', 'OK') != 'OK'):
        raise ValueError('Guest settings success is not confirmed by its current values')
    return receipt
