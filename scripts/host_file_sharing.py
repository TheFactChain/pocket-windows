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
from profile_store import _pinned_directory, _plain, _exclusive_file, _winapi

DEFAULTS = {'schema': 1, 'mode': 'off', 'folders': []}
SERVER_REQUEST_LIMIT = 65536
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
    try:
        return json.loads(data, object_pairs_hook=_unique)
    except RecursionError as error:
        raise ValueError('Слишком сложные данные доступа к файлам.') from error


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


def _share_name(path):
    return ('PC_' + path[0].upper() if len(path) == 3 else
            'PC_' + hashlib.sha256(path.casefold().encode('utf-8')).hexdigest()[:12])


def serialize_server_request(config, shares, protected):
    request = {key: config[key] for key in ('schema', 'session_nonce', 'username', 'password')}
    request.update(shares=[{key: item[key] for key in ('name', 'path')} for item in shares], protected=protected)
    payload = json.dumps(request, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    if len(payload) > SERVER_REQUEST_LIMIT:
        raise ValueError('Список доступных папок слишком велик.')
    return payload


def validate_saved_settings(root, value):
    settings = validate_settings(value)
    if settings['mode'] != 'off':
        # Include every configured path, even one currently unavailable, and every
        # possible local drive. Session credentials have these fixed encoded lengths.
        paths = ([chr(65 + index) + ':\\' for index in range(26)]
                 if settings['mode'] == 'all' else settings['folders'])
        shares = [{'name': _share_name(path), 'path': path} for path in paths]
        config = {'schema': 1, 'session_nonce': 'a' * 32,
                  'username': 'WCP_' + 'a' * 24, 'password': 'a' * 43}
        serialize_server_request(config, shares, protected_paths(root))
    return settings


def local_drives():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetLogicalDrives.restype = wintypes.DWORD
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    mask = kernel.GetLogicalDrives()
    if not mask:
        raise OSError('Не удалось получить список дисков ПК.')
    return [chr(65 + index) + ':\\' for index in range(26)
            if mask & (1 << index) and kernel.GetDriveTypeW(chr(65 + index) + ':\\') in (2, 3, 6)]


def _known_folder(identifier):
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    ole = ctypes.WinDLL('ole32')
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    guid = ctypes.create_string_buffer(uuid.UUID(identifier).bytes_le, 16)
    pointer = ctypes.c_void_p()
    try:
        # KF_FLAG_DONT_VERIFY also returns the intended path before Startup exists.
        if shell.SHGetKnownFolderPath(guid, 0x4000, None, ctypes.byref(pointer)) != 0:
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
            if path is None:
                raise ValueError('Unknown protected folder')
            # Keep the namespace even when the protected directory has not been created.
            resolved = str(path.resolve(strict=False))
            for candidate in (str(path.absolute()), resolved):
                if not re.match(r'^[A-Za-z]:\\.+', candidate):
                    raise ValueError('Not a local protected folder')
                if candidate.casefold() not in (item.casefold() for item in result):
                    result.append(candidate)
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError('Не удалось определить защищённые папки ПК. Доступ к папкам не включён.') from error
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
        name = _share_name(path)
        shares.append({'name': name, 'path': path, 'label': label, 'preferred_letter': path[0].upper() if drive else ''})
    return shares, unavailable


def network_arguments(port=None, clipboard_port=None):
    if port is not None and (type(port) is not int or not 49152 <= port <= 65535):
        raise ValueError('Invalid loopback SMB session port')
    forwards = []
    if port is not None:
        forwards.append('guestfwd=tcp:192.168.234.4:445-tcp:127.0.0.1:' + str(port))
    if clipboard_port is not None:
        from clipboard_bridge import network_forward
        forwards.append(network_forward(clipboard_port))
    if not forwards:
        raise ValueError('An explicit local forwarding endpoint is required')
    return ['-netdev', 'user,id=wcfiles,restrict=on,ipv6=off,net=192.168.234.0/24,'
            'host=192.168.234.2,dhcpstart=192.168.234.15,' + ','.join(forwards),
            '-device', 'e1000,id=wcfilesnic,netdev=wcfiles,mac=52:54:00:77:00:04']


def validate_receipt(data, config):
    value = _json(data, 32768)
    base = {'schema', 'session_nonce', 'success', 'code', 'mappings'}
    if (type(value) is not dict or type(value.get('schema')) is not int or value['schema'] not in (1, 2)
            or set(value) != (base | {'serial', 'clipboard'} if value['schema'] == 2 else base)
            or value['session_nonce'] != config['session_nonce'] or type(value['success']) is not bool
            or type(value['code']) is not str or re.fullmatch('[A-Z_]{1,64}', value['code']) is None
            or value['success'] != (value['code'] == 'OK') or type(value['mappings']) is not list
            or len(value['mappings']) > 64):
        raise ValueError('Invalid current file-sharing receipt')
    if value['schema'] == 2:
        clipboard = value['clipboard']
        if (type(value['serial']) is not int or not 1 <= value['serial'] <= 9007199254740991
                or type(clipboard) is not dict or set(clipboard) != {'status', 'code'}
                or clipboard['status'] not in ('off', 'connecting', 'ready', 'failed', 'unconfirmed')
                or type(clipboard['code']) is not str or re.fullmatch('[A-Z_]{1,64}', clipboard['code']) is None
                or (clipboard['status'] == 'off') != (not config.get('clipboard', {}).get('enabled', False))):
            raise ValueError('Invalid current guest subsystem receipt')
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


RECOVERY_EVENT_LIMIT = 256 * 1024
RECOVERY_CODES = {'SUPERSEDE_BACKUP_RETAINED', 'SUPERSEDE_ROLLBACK_BACKUP_RETAINED',
                  'SUPERSEDE_NEW_ENTRY_RETAINED', 'SUPERSEDE_CLEANUP_UNVERIFIED'}


def validate_recovery_event(value, nonce, shares):
    fields = {'t', 'schema', 'session_nonce', 'seq', 'share', 'original', 'backup', 'code'}
    if (type(value) is not dict or set(value) != fields or value['t'] != 'files-recovery'
            or type(value['schema']) is not int or value['schema'] != 1
            or value['session_nonce'] != nonce or type(value['seq']) is not int
            or not 1 <= value['seq'] <= 9007199254740991
            or type(value['share']) is not str or type(value['code']) is not str
            or value['share'] not in shares or value['code'] not in RECOVERY_CODES
            or type(value['original']) is not str or type(value['backup']) is not str):
        raise ValueError('Invalid file recovery event')
    original = value['original']
    if (not original or len(original.encode('utf-16-le')) > 65534
            or any(ord(char) < 32 or char in '/:*?"<>|' for char in original)
            or any(part in ('', '.', '..') or part.endswith((' ', '.')) for part in original.split('\\'))
            or re.fullmatch(r'(?:\.~wcp-[a-f0-9]{32}\.(?:backup|tmp))?', value['backup']) is None):
        raise ValueError('Invalid relative file recovery path')
    if value['code'] in {'SUPERSEDE_BACKUP_RETAINED', 'SUPERSEDE_ROLLBACK_BACKUP_RETAINED'} and not value['backup'].endswith('.backup'):
        raise ValueError('Recovery backup name is required')
    return value


def append_recovery(root, nonce, value):
    # Keep the tail check, optional delimiter and new record under the same
    # exclusive/no-reparse handle. Preserve any previous partial data verbatim.
    payload = json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('ascii') + b'\n'
    if len(payload) > RECOVERY_EVENT_LIMIT + 1:
        raise ValueError('Recovery record is too large')
    root = Path(root)
    with _pinned_directory(root, create=False):
        folder = root / 'logs'
        with _pinned_directory(folder, create=True):
            path = folder / ('host-files-recovery-' + nonce + '.jsonl')
            with _exclusive_file(path) as handle:
                kernel = _winapi()
                kernel.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
                kernel.SetFilePointerEx.restype = wintypes.BOOL
                kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
                kernel.ReadFile.restype = wintypes.BOOL
                position = ctypes.c_longlong()
                if not kernel.SetFilePointerEx(handle, 0, ctypes.byref(position), 2):
                    raise ctypes.WinError(ctypes.get_last_error())
                if position.value:
                    if not kernel.SetFilePointerEx(handle, -1, None, 2):
                        raise ctypes.WinError(ctypes.get_last_error())
                    tail, count = ctypes.create_string_buffer(1), wintypes.DWORD()
                    if not kernel.ReadFile(handle, tail, 1, ctypes.byref(count), None) or count.value != 1:
                        raise OSError('Cannot inspect recovery log tail')
                    if tail.raw != b'\n':
                        count = wintypes.DWORD()
                        if not kernel.WriteFile(handle, b'\n', 1, ctypes.byref(count), None) or count.value != 1:
                            raise OSError('Cannot separate partial recovery record')
                        if not kernel.FlushFileBuffers(handle):
                            raise ctypes.WinError(ctypes.get_last_error())
                offset = 0
                while offset < len(payload):
                    count = wintypes.DWORD()
                    chunk = payload[offset:]
                    if not kernel.WriteFile(handle, chunk, len(chunk), ctypes.byref(count), None) or not count.value:
                        raise OSError('Cannot append recovery record')
                    offset += count.value
                if not kernel.FlushFileBuffers(handle):
                    raise ctypes.WinError(ctypes.get_last_error())


class Session:
    """The controller keeps this object, stdin and private media across QEMU reboots."""
    def __init__(self, root, nonce, *, base_directory=None, clipboard_port=None, clipboard_nonce=None):
        self.stack, self.process, self.medium = ExitStack(), None, None
        self.arguments, self.config, self.network = [], None, False
        self.public = {'status': 'off', 'share_count': 0, 'unavailable': [],
                       'recovery_required': False, 'recovery_log_failed': False,
                       'recovery_channel_failed': False}
        self._root, self._nonce = Path(root), nonce
        self._reader, self._closing, self._output_failed = None, False, False
        self._last_recovery_sequence = 0
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
            if clipboard_port is not None:
                from clipboard_bridge import GUEST_PORT, network_forward
                network_forward(clipboard_port)
                if (type(clipboard_nonce) is not str or re.fullmatch('[a-f0-9]{32}', clipboard_nonce) is None
                        or clipboard_nonce.lower() == nonce.lower()):
                    raise ValueError('Clipboard requires a separate private handshake nonce')
                self.config['clipboard'] = {'enabled': True, 'port': GUEST_PORT, 'nonce': clipboard_nonce}
            component = Path(root) / 'runtime/host-files'
            # Hold executables and DLLs against replacement until this session ends.
            helper = component / 'HostFilesGuest.exe'
            manifest = _json((component / 'build-manifest.json').read_bytes())
            records = {record['name']: record for record in manifest['files']}
            required = (('HostFiles.exe', 'HostFilesGuest.exe', 'SMBLibrary.dll', 'SMBLibrary.Win32.dll', 'Utilities.dll')
                        if shares else ('HostFilesGuest.exe',))
            for name in required:
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
                request_bytes = serialize_server_request(self.config, shares, protected)
                executable = component / 'HostFiles.exe'
                self.process = subprocess.Popen([str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                self.process.stdin.write(request_bytes + b'\n')
                self.process.stdin.flush()
                ready_queue = queue.Queue(maxsize=1)
                self._reader = threading.Thread(target=self._read_output,
                                                args=(self.process, ready_queue, {item['name'] for item in shares}), daemon=True)
                self._reader.start()
                ready = _json(ready_queue.get(timeout=10), 4096)
                if (type(ready) is not dict or set(ready) != {'ok', 'schema', 'session_nonce', 'port', 'share_count'}
                        or ready['ok'] is not True or type(ready['schema']) is not int or ready['schema'] != 1
                        or ready['session_nonce'] != nonce or type(ready['share_count']) is not int
                        or ready['share_count'] != len(shares)):
                    raise ValueError('File-sharing helper did not confirm this session')
                self.arguments += network_arguments(ready['port'], clipboard_port)
            elif clipboard_port is not None:
                self.arguments += network_arguments(clipboard_port=clipboard_port)
            self.network = bool(shares or clipboard_port is not None)
            # The empty config also removes mappings from an earlier enabled session.
            if self.medium is not None:
                self.arguments += self.stack.enter_context(self.medium.hold())
                self.arguments += ['-chardev', 'ringbuf,id=wcfilesreceipt,size=32768',
                                   '-device', 'isa-serial,index=1,chardev=wcfilesreceipt']
        except BaseException:
            self.close(guest_stopped=True)
            raise

    def _persist_recovery(self, value):
        self.public['recovery_required'] = True
        self.public['recovery_log'] = 'logs/host-files-recovery-' + self._nonce + '.jsonl'
        try:
            append_recovery(self._root, self._nonce, value)
        except (OSError, ValueError):
            # Hardware/permission failure is visible; never silently claim the event
            # is durable, and never ask the store to discard its retained backup.
            self.public['recovery_log_failed'] = True

    def _channel_failed(self):
        if not self._output_failed:
            self._output_failed = True
            self.public['recovery_channel_failed'] = True
            self._persist_recovery({'t': 'files-recovery-channel', 'schema': 1,
                                    'session_nonce': self._nonce, 'code': 'RECOVERY_CHANNEL_FAILED'})

    def _read_output(self, process, ready_queue, shares):
        ready_seen = False
        try:
            while True:
                line = process.stdout.readline(RECOVERY_EVENT_LIMIT + 3)
                if not line:
                    if not self._closing:
                        self._channel_failed()
                    break
                data = line.rstrip(b'\r\n')
                if len(data) > RECOVERY_EVENT_LIMIT or not line.endswith(b'\n'):
                    self._channel_failed()
                    while line and not line.endswith(b'\n'):
                        line = process.stdout.readline(8193)
                    continue
                try:
                    value = _json(data, RECOVERY_EVENT_LIMIT)
                    if type(value) is dict and value.get('t') == 'files-recovery':
                        value = validate_recovery_event(value, self._nonce, shares)
                        if value['seq'] <= self._last_recovery_sequence:
                            raise ValueError('Recovery sequence is not increasing')
                        self._last_recovery_sequence = value['seq']
                        self.public['recovery_latest'] = value
                        self._persist_recovery(value)
                    elif not ready_seen:
                        if len(data) > 4096:
                            raise ValueError('Initial helper receipt is too large')
                        ready_seen = True
                        ready_queue.put_nowait(data)
                    else:
                        raise ValueError('Unknown file helper output')
                except (OSError, ValueError, RecursionError):
                    self._channel_failed()
        except (OSError, ValueError):
            if not self._closing:
                self._channel_failed()
        finally:
            if not ready_seen:
                try:
                    ready_queue.put_nowait(b'')
                except queue.Full:
                    pass

    def healthy(self):
        return not self._output_failed and (self.process is None or self.process.poll() is None)

    def close(self, *, guest_stopped):
        if guest_stopped is not True:
            raise RuntimeError('File access must remain available until Windows exits')
        self._closing = True
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
            if self._reader is not None:
                self._reader.join(timeout=5)
                if self._reader.is_alive():
                    self._channel_failed()
            self.process.stdout.close()
            self.process = None
        self.stack.close()
        if self.medium is not None:
            self.medium.cleanup(guest_stopped=True)
            self.medium = None
