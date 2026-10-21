"""Public guest preferences and read-only QEMU vvfat handoff media."""
import json
import os
from pathlib import Path
import re

DEFAULTS = {'schema': 1, 'width': 1280, 'height': 720}
MODES = ((800, 600), (1024, 768), (1280, 720), (1280, 800),
         (1366, 768), (1600, 900), (1920, 1080))


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


def validate_settings(value):
    if type(value) is not dict or set(value) - set(DEFAULTS):
        raise ValueError('Unknown Windows guest setting')
    result = DEFAULTS | value
    if (any(type(item) is not int for item in result.values()) or result['schema'] != 1
            or (result['width'], result['height']) not in MODES):
        raise ValueError('Invalid Windows resolution')
    return result


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
    payload = validate_settings(value) | {'session_nonce': _nonce(nonce)}
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
    settings, nonce = validate_settings(settings), _nonce(nonce)
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
