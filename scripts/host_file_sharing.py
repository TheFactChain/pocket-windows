"""Local file permissions, isolated SMB transport and per-Windows-session ownership."""
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import hashlib
import json
import ntpath
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import threading
import uuid

from file_snapshot import locked_read
from profile_store import _pinned_directory, _plain

DEFAULTS = {'schema': 1, 'mode': 'off', 'folders': []}
# Guest receipt: its file NIC is static with no gateway/DNS, so the host may connect the link.
LINK_REQUEST = 'NIC_STATIC'
STARTUP_FOLDERS = ('B97D20BB-F46A-4C97-BA10-5E3608430854', '82A5EA35-D9CD-47C5-9629-E15D2F714E6E')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Повторяющееся поле доступа к файлам.')
        value[key] = item
    return value


def _json(data, limit=65536):
    if not isinstance(data, (str, bytes)) or len(data) > limit:
        raise ValueError('Данные доступа к файлам слишком велики.')
    return json.loads(data, object_pairs_hook=_unique)


def validate_settings(value):
    if type(value) is not dict or set(value) - set(DEFAULTS):
        raise ValueError('Неизвестные настройки доступа к файлам ПК.')
    result = DEFAULTS | value
    if (type(result['schema']) is not int or result['schema'] != 1
            or result['mode'] not in ('off', 'all', 'selected')
            or type(result['folders']) is not list or len(result['folders']) > 64):
        raise ValueError('Некорректные настройки доступа к файлам ПК.')
    folders, seen = [], set()
    for path in result['folders']:
        if (type(path) is not str or len(path) > 32767
                or re.match(r'^[A-Za-z]:[\\/]', path) is None
                or any(ord(character) < 32 or character in '*?"<>|' for character in path)
                or ':' in path[2:]):
            raise ValueError('Выбери абсолютный локальный путь к папке ПК.')
        path = ntpath.normpath(path)
        if path.casefold() in seen:
            raise ValueError('Папка добавлена несколько раз.')
        seen.add(path.casefold())
        folders.append(path)
    if result['mode'] == 'selected' and not folders:
        raise ValueError('Добавь хотя бы одну папку ПК.')
    return dict(result, folders=folders)


def load_settings(root):
    root = Path(root)
    folder = root / 'config'
    with _pinned_directory(root, create=False):
        if not folder.exists():
            return validate_settings({})
        with _pinned_directory(folder, create=False):
            path = folder / 'files.json'
            _plain(path)
            if not path.exists():
                return validate_settings({})
            with locked_read(path) as source:
                return validate_settings(_json(source.read(65537)))


def local_drives():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetLogicalDrives.restype = wintypes.DWORD
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    mask = kernel.GetLogicalDrives()
    if not mask:
        raise OSError('Не удалось получить список дисков ПК.')
    return [chr(65 + index) + ':\\' for index in range(26)
            if mask & (1 << index) and kernel.GetDriveTypeW(chr(65 + index) + ':\\') in (2, 3)]


def _known_folder(identifier):
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    ole = ctypes.WinDLL('ole32')
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    guid = ctypes.create_string_buffer(uuid.UUID(identifier).bytes_le, 16)
    pointer = ctypes.c_void_p()
    try:
        if shell.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(pointer)) != 0:
            return None
        return Path(ctypes.wstring_at(pointer))
    finally:
        ole.CoTaskMemFree(pointer)


def protected_paths(root):
    """Real folders the guest must never reach, even with every drive shared for writing:
    this program (the host runs its scripts), its private session state and autostart."""
    from session_carrier import _local_app_data
    candidates = [Path(root), _local_app_data() / 'WindowsClaudePortable',
                  *(_known_folder(identifier) for identifier in STARTUP_FOLDERS)]
    result = []
    for path in candidates:
        try:
            path = str(path.resolve(strict=True)) if path is not None else ''
        except OSError:
            continue
        if re.match(r'^[A-Za-z]:\\', path) and path.casefold() not in (item.casefold() for item in result):
            result.append(path)
    return result


def _inside(path, folder):
    path, folder = ntpath.normcase(ntpath.normpath(path)), ntpath.normcase(ntpath.normpath(folder))
    return path == folder or path.startswith(folder.rstrip('\\') + '\\')


def resolve_shares(settings, protected=()):
    settings = validate_settings(settings)
    if settings['mode'] == 'off':
        return [], []
    paths = local_drives() if settings['mode'] == 'all' else settings['folders']
    shares, unavailable = [], []
    for path in paths:
        drive = len(path) == 3
        label = 'Диск ' + path[:2] + ' на ПК' if drive else (ntpath.basename(path) + ' на ПК')[:128]
        try:
            with _pinned_directory(Path(path), create=False):
                if not Path(path).is_dir():
                    raise ValueError('Not a local directory')
            real = str(Path(path).resolve(strict=True))
        except (OSError, ValueError, RuntimeError):
            unavailable.append({'label': label, 'status': 'unavailable'})
            continue
        if any(_inside(real, folder) or _inside(path, folder) for folder in protected):
            # The server refuses these too; reporting them keeps the reason visible.
            unavailable.append({'label': label, 'status': 'protected'})
            continue
        name = 'PC_' + path[0].upper() if drive else 'PC_' + hashlib.sha256(path.casefold().encode('utf-8')).hexdigest()[:12]
        shares.append({'name': name, 'path': path, 'label': label, 'preferred_letter': path[0].upper() if drive else ''})
    return shares, unavailable


def network_arguments(port):
    if type(port) is not int or not 49152 <= port <= 65535:
        raise ValueError('Invalid loopback SMB session port')
    return ['-netdev', 'user,id=wcfiles,restrict=on,ipv6=off,net=192.168.234.0/24,'
            'host=192.168.234.2,dhcpstart=192.168.234.15,'
            'guestfwd=tcp:192.168.234.4:445-tcp:127.0.0.1:' + str(port),
            '-device', 'e1000,id=wcfilesnic,netdev=wcfiles,mac=52:54:00:77:00:04']


def validate_receipt(data, config):
    value = _json(data, 32768)
    if (type(value) is not dict or set(value) != {'schema', 'session_nonce', 'success', 'code', 'mappings'}
            or type(value['schema']) is not int or value['schema'] != 1
            or value['session_nonce'] != config['session_nonce'] or type(value['success']) is not bool
            or type(value['code']) is not str or re.fullmatch('[A-Z_]{1,64}', value['code']) is None
            or value['success'] != (value['code'] == 'OK') or type(value['mappings']) is not list
            or len(value['mappings']) > 64):
        raise ValueError('Invalid current file-sharing receipt')
    expected = {item['name'] for item in config['shares']}
    names, letters = set(), set()
    for item in value['mappings']:
        if (type(item) is not dict or set(item) != {'name', 'letter'} or type(item['name']) is not str
                or item['name'] not in expected or item['name'] in names or type(item['letter']) is not str
                or re.fullmatch('[A-BD-Z]?', item['letter']) is None
                or item['letter'] and item['letter'] in letters):
            raise ValueError('File-sharing receipt contains an unexpected mapping')
        names.add(item['name'])
        if item['letter']:
            letters.add(item['letter'])
    if value['success'] and names != expected:
        raise ValueError('Not all requested file shares were connected')
    return value


class Session:
    """The controller keeps this object, stdin and private media across QEMU reboots."""
    def __init__(self, root, nonce, *, base_directory=None):
        self.stack, self.process, self.medium = ExitStack(), None, None
        self.arguments, self.config, self.network = [], None, False
        self.public = {'status': 'off', 'share_count': 0, 'unavailable': []}
        try:
            from file_share_carrier import Medium
            if re.fullmatch('[a-f0-9]{32}', nonce) is None:
                raise ValueError('Invalid Windows file session nonce')
            settings = load_settings(root)
            protected = protected_paths(root) if settings['mode'] != 'off' else []
            shares, unavailable = resolve_shares(settings, protected)
            self.public.update(status='pending' if shares else 'off' if settings['mode'] == 'off' else 'unavailable',
                               share_count=len(shares), unavailable=unavailable)
            self.config = {'schema': 1, 'session_nonce': nonce, 'username': 'WCP_' + secrets.token_hex(12),
                           'password': secrets.token_urlsafe(32),
                           'shares': [{key: item[key] for key in ('name', 'label', 'preferred_letter')} for item in shares]}
            component = Path(root) / 'runtime/host-files'
            # Hold executables and DLLs against replacement until this session ends.
            helper = component / 'HostFilesGuest.exe'
            if shares or helper.exists():
                manifest = _json((component / 'build-manifest.json').read_bytes())
                records = {record['name']: record for record in manifest['files']}
                for name in ('HostFiles.exe', 'HostFilesGuest.exe', 'SMBLibrary.dll', 'SMBLibrary.Win32.dll', 'Utilities.dll'):
                    path = component / name
                    _plain(path)
                    stream = self.stack.enter_context(locked_read(path))
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != records[name]['sha256']:
                        raise ValueError('File-sharing component integrity check failed')
                _plain(helper)
                with locked_read(helper) as source:
                    helper_bytes = source.read(2 * 1024 ** 2 + 1)
                # Created before the server starts, so its private base folder exists to be protected.
                self.medium = Medium(self.config, helper_bytes, base_directory=base_directory)
            if shares:
                protected = protected_paths(root)
                request = dict(self.config, shares=[{key: item[key] for key in ('name', 'path')} for item in shares],
                               protected=protected)
                request_bytes = json.dumps(request, ensure_ascii=True, separators=(',', ':')).encode('ascii')
                if len(request_bytes) > 65536:
                    raise ValueError('Список доступных папок слишком велик.')
                executable = component / 'HostFiles.exe'
                self.process = subprocess.Popen([str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                self.process.stdin.write(request_bytes + b'\n')
                self.process.stdin.flush()
                ready_queue = queue.Queue(maxsize=1)
                def read_ready():
                    try:
                        ready_queue.put(self.process.stdout.readline(4097))
                    except OSError:
                        ready_queue.put(b'')
                threading.Thread(target=read_ready, daemon=True).start()
                ready = _json(ready_queue.get(timeout=10), 4096)
                if (type(ready) is not dict or set(ready) != {'ok', 'schema', 'session_nonce', 'port', 'share_count'}
                        or ready['ok'] is not True or type(ready['schema']) is not int or ready['schema'] != 1
                        or ready['session_nonce'] != nonce or type(ready['share_count']) is not int
                        or ready['share_count'] != len(shares)):
                    raise ValueError('File-sharing helper did not confirm this session')
                self.arguments += network_arguments(ready['port'])
                self.network = True
            # The empty config also removes mappings from an earlier enabled session.
            if self.medium is not None:
                self.arguments += self.stack.enter_context(self.medium.hold())
                self.arguments += ['-chardev', 'ringbuf,id=wcfilesreceipt,size=32768',
                                   '-device', 'isa-serial,index=1,chardev=wcfilesreceipt']
        except BaseException:
            self.close(guest_stopped=True)
            raise

    def healthy(self):
        return self.process is None or self.process.poll() is None

    def close(self, *, guest_stopped):
        if guest_stopped is not True:
            raise RuntimeError('File access must remain available until Windows exits')
        if self.process is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # This is our pipe-owned file helper, never a Windows or gateway process.
                self.process.terminate()
                self.process.wait(timeout=5)
            self.process.stdout.close()
            self.process = None
        self.stack.close()
        if self.medium is not None:
            self.medium.cleanup(guest_stopped=True)
            self.medium = None
