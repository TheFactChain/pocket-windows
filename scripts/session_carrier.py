"""Private Windows raw-tar session media; cleanup always needs proven guest shutdown."""
from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile

from file_snapshot import locked_read
from gateway_profile import validate_session
from profile_store import StoreError, _pinned_directory, _plain

MAX_SESSION = 65536
MAX_MEDIA = 81920
SERIAL = 'WCSESSION'


class CarrierError(RuntimeError):
    pass


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [('length', wintypes.DWORD), ('descriptor', wintypes.LPVOID), ('inherit', wintypes.BOOL)]


class _FileInfo(ctypes.Structure):
    _fields_ = [('attributes', wintypes.DWORD), ('creation', wintypes.FILETIME),
                ('access', wintypes.FILETIME), ('write', wintypes.FILETIME),
                ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]


def _api():
    if os.name != 'nt':
        raise CarrierError('Private session media requires Windows')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    signatures = {
        'CreateFileW': ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        'GetCurrentProcess': ([], wintypes.HANDLE),
        'LocalFree': ([wintypes.LPVOID], wintypes.LPVOID),
        'CreateDirectoryW': ([wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes)], wintypes.BOOL),
        'GetFileInformationByHandle': ([wintypes.HANDLE, ctypes.POINTER(_FileInfo)], wintypes.BOOL),
        'ReadFile': ([wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID], wintypes.BOOL),
        'WriteFile': ([wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID], wintypes.BOOL),
        'FlushFileBuffers': ([wintypes.HANDLE], wintypes.BOOL),
        'SetFileInformationByHandle': ([wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD], wintypes.BOOL),
        'GetVolumePathNameW': ([wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD], wintypes.BOOL),
        'GetVolumeInformationW': ([wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        getattr(kernel, name).argtypes, getattr(kernel, name).restype = arguments, result
    signatures = {
        'OpenProcessToken': ([wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL),
        'GetTokenInformation': ([wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        'ConvertSidToStringSidW': ([wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)], wintypes.BOOL),
        'ConvertStringSecurityDescriptorToSecurityDescriptorW': ([wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID], wintypes.BOOL),
        'GetSecurityInfo': ([wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                             ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID,
                             ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID,
                             ctypes.POINTER(wintypes.LPVOID)], wintypes.DWORD),
        'GetSecurityDescriptorControl': ([wintypes.LPVOID, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        'GetAce': ([wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        getattr(security, name).argtypes, getattr(security, name).restype = arguments, result
    return kernel, security


def _sid_text(pointer):
    kernel, security = _api()
    text = wintypes.LPWSTR()
    if not security.ConvertSidToStringSidW(pointer, ctypes.byref(text)):
        raise CarrierError('Cannot validate private media identity')
    try:
        return text.value
    finally:
        kernel.LocalFree(ctypes.cast(text, wintypes.LPVOID))


def _current_sid():
    kernel, security = _api()
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise CarrierError('Cannot determine the current Windows user')
    try:
        needed = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not 0 < needed.value <= 65536:
            raise CarrierError('Cannot determine the current Windows user')
        buffer = ctypes.create_string_buffer(needed.value)
        if not security.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(needed)):
            raise CarrierError('Cannot determine the current Windows user')
        return _sid_text(ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0])
    finally:
        kernel.CloseHandle(token)


@contextmanager
def _descriptor(directory):
    kernel, security = _api()
    sid = _current_sid()
    flags = 'OICI' if directory else ''
    sddl = f'O:{sid}D:P(A;{flags};FA;;;{sid})(A;{flags};FA;;;SY)'
    descriptor = wintypes.LPVOID()
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise CarrierError('Cannot create protected media permissions')
    try:
        yield _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
    finally:
        kernel.LocalFree(descriptor)


def _check_acl(handle, directory):
    kernel, security = _api()
    descriptor, owner, dacl = (wintypes.LPVOID() for _ in range(3))
    if security.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None,
                                ctypes.byref(dacl), None, ctypes.byref(descriptor)):
        raise CarrierError('Cannot validate private media permissions')
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise CarrierError('Cannot validate private media permissions')
        sid = _current_sid()
        if not dacl.value or not control.value & 0x1000 or _sid_text(owner) != sid:
            raise CarrierError('Session media permissions are not private to this Windows user')
        count = wintypes.WORD.from_address(dacl.value + 4).value
        allowed = []
        if count != 2:
            raise CarrierError('Session media permissions contain unexpected access rules')
        for index in range(count):
            ace = wintypes.LPVOID()
            if not security.GetAce(dacl, index, ctypes.byref(ace)):
                raise CarrierError('Invalid session media permissions')
            kind = ctypes.c_ubyte.from_address(ace.value).value
            flags = ctypes.c_ubyte.from_address(ace.value + 1).value
            size = wintypes.WORD.from_address(ace.value + 2).value
            mask = wintypes.DWORD.from_address(ace.value + 4).value
            if kind != 0 or flags != (3 if directory else 0) or size < 12 or mask != 0x1F01FF:
                raise CarrierError('Session media permissions contain unexpected access rules')
            allowed.append(_sid_text(ace.value + 8))
        if set(allowed) != {sid, 'S-1-5-18'}:
            raise CarrierError('Session media permissions grant access to another user')
        return {'owner_sid': sid, 'allowed_sids': allowed, 'protected': True}
    finally:
        kernel.LocalFree(descriptor)


@contextmanager
def _open_private(path, directory=False, delete=False):
    kernel, _ = _api()
    access = 0x80020000 | (0x10000 if delete else 0)
    flags = 0x00200000 | (0x02000000 if directory else 0)
    handle = kernel.CreateFileW(str(path), access, 0 if delete and not directory else 3,
                                None, 3, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        raise CarrierError('Private session media is unavailable or still in use')
    try:
        information = _FileInfo()
        if (not kernel.GetFileInformationByHandle(handle, ctypes.byref(information))
                or information.attributes & 0x400 or bool(information.attributes & 0x10) != directory
                or (not directory and information.links != 1)):
            raise CarrierError('Session media cannot contain reparse points or hard links')
        _check_acl(handle, directory)
        yield handle
    finally:
        kernel.CloseHandle(handle)


def inspect_private_acl(path):
    """Non-secret evidence for the CurrentUser+SYSTEM protected DACL boundary."""
    path = Path(path)
    try:
        _plain(path)
        with _open_private(path, directory=path.is_dir()) as handle:
            return _check_acl(handle, path.is_dir())
    except (OSError, StoreError):
        raise CarrierError('Private session media permissions could not be verified') from None


def _persistent_acls(path):
    kernel, _ = _api()
    volume = ctypes.create_unicode_buffer(32768)
    flags = wintypes.DWORD()
    if (not kernel.GetVolumePathNameW(str(path), volume, len(volume))
            or not kernel.GetVolumeInformationW(volume.value, None, 0, None, None, ctypes.byref(flags), None, 0)
            or not flags.value & 8):
        raise CarrierError('Session media requires a local filesystem with persistent Windows ACLs')


def _local_app_data():
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR]
    shell.SHGetFolderPathW.restype = ctypes.c_long
    buffer = ctypes.create_unicode_buffer(260)
    if shell.SHGetFolderPathW(None, 0x1C, None, 0, buffer) != 0:
        raise CarrierError('The private Windows application-data directory is unavailable')
    return Path(buffer.value)


def _base_path(base_directory):
    base = Path(base_directory) if base_directory is not None else _local_app_data() / 'WindowsClaudePortable/sessions'
    project = Path(__file__).resolve().parents[1]
    if (not base.is_absolute() or '..' in base.parts or str(base).startswith('\\\\')
            or base.is_relative_to(project)):
        raise CarrierError('Session media must be outside the portable application in a private local directory')
    return base


@contextmanager
def _private_base(base, create=False):
    missing, parent = [], base
    while not parent.exists() and not parent.is_symlink():
        missing.append(parent)
        if parent == parent.parent:
            raise CarrierError('Private media directory is unavailable')
        parent = parent.parent
    if missing and not create:
        raise CarrierError('Private media directory does not exist')
    with ExitStack() as stack:
        stack.enter_context(_pinned_directory(parent, create=False))
        _persistent_acls(parent)
        for directory in reversed(missing):
            kernel, _ = _api()
            with _descriptor(True) as attributes:
                if not kernel.CreateDirectoryW(str(directory), ctypes.byref(attributes)):
                    raise CarrierError('Private media directory already exists or cannot be created')
            stack.enter_context(_pinned_directory(directory, create=False))
            stack.enter_context(_open_private(directory, directory=True))
        if not missing:
            stack.enter_context(_open_private(base, directory=True))
        yield


def _identity(path):
    value = path.lstat()
    return {'device': value.st_dev, 'file_id': value.st_ino, 'size': value.st_size, 'mtime_ns': value.st_mtime_ns}


def _read(handle, limit):
    kernel, _ = _api()
    buffer, count = ctypes.create_string_buffer(limit + 1), wintypes.DWORD()
    if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None) or count.value > limit:
        raise CarrierError('Session media content is invalid or oversized')
    return buffer.raw[:count.value]


@contextmanager
def _write_new(path, value):
    kernel, _ = _api()
    with _descriptor(False) as attributes:
        handle = kernel.CreateFileW(str(path), 0xC0030000, 0, ctypes.byref(attributes), 1, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise CarrierError('Session media already exists or cannot be created')
    try:
        _check_acl(handle, False)
        written = wintypes.DWORD()
        if (not kernel.WriteFile(handle, value, len(value), ctypes.byref(written), None)
                or written.value != len(value) or not kernel.FlushFileBuffers(handle)):
            raise CarrierError('Private session media could not be written')
        yield handle
    except BaseException:
        # CREATE_NEW plus an exclusive DELETE-capable handle identifies exactly our partial file.
        _delete_handle(handle)
        raise
    finally:
        kernel.CloseHandle(handle)


def _nonce(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise CarrierError('A session nonce must contain exactly 32 lowercase hex characters')
    return value


def _check_content(raw, marker):
    if len(raw) != marker['media_identity']['size'] or hashlib.sha256(raw).hexdigest() != marker['sha256']:
        raise CarrierError('Session media content no longer matches its ownership marker')
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
            members = archive.getmembers()
            if len(members) != 1 or members[0].name != 'session.json' or not members[0].isfile() or not 0 < members[0].size <= MAX_SESSION:
                raise ValueError()
            value = validate_session(json.load(archive.extractfile(members[0])))
        if any(value[key] != marker[key] for key in ('nonce', 'revision', 'gateway_version')):
            raise ValueError()
    except (ValueError, TypeError, tarfile.TarError, UnicodeError):
        raise CarrierError('Session media has invalid or mismatched content') from None


def _read_marker(handle, nonce):
    try:
        marker = json.loads(_read(handle, 8192))
        fields = {'schema','kind','nonce','revision','gateway_version','media_identity','directory_id','sha256'}
        if (not isinstance(marker, dict) or set(marker) != fields or type(marker['schema']) is not int
                or marker['schema'] != 1 or marker['kind'] != 'gateway-session-carrier'
                or marker['nonce'] != nonce or not re.fullmatch('[a-f0-9]{64}', marker['sha256'])
                or not isinstance(marker['media_identity'], dict)
                or set(marker['media_identity']) != {'device','file_id','size','mtime_ns'}
                or any(type(item) is not int or item < 0 for item in marker['media_identity'].values())
                or not 0 < marker['media_identity']['size'] <= MAX_MEDIA
                or type(marker['directory_id']) is not int):
            raise ValueError()
        return marker
    except (ValueError, TypeError, KeyError):
        raise CarrierError('Session media ownership marker is invalid') from None


def _delete_handle(handle):
    kernel, _ = _api()
    delete = wintypes.BOOL(True)
    if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
        raise CarrierError('Private media cannot be removed; it may still be in use')


class Carrier:
    def __init__(self, base, nonce, identity):
        self.base, self.nonce, self.identity = base, nonce, identity
        self.path = base / nonce / 'session.tar'
        self._holds = 0

    def _validate(self, marker, raw):
        if (marker['media_identity'] != self.identity or _identity(self.path) != self.identity
                or self.path.parent.lstat().st_ino != marker['directory_id']):
            raise CarrierError('Session carrier file or directory identity changed')
        _check_content(raw, marker)

    @contextmanager
    def hold(self):
        """Caller must keep this context open while QEMU has the readonly carrier attached."""
        try:
            with _private_base(self.base), _open_private(self.path.parent, directory=True), \
                    _open_private(self.path.parent / 'carrier.json') as marker_handle, \
                    _open_private(self.path), locked_read(self.path) as stream:
                marker = _read_marker(marker_handle, self.nonce)
                raw = stream.read(MAX_MEDIA + 1)
                self._validate(marker, raw)
                self._holds += 1
                try:
                    yield {'path': str(self.path), 'serial': SERIAL, 'format': 'raw',
                           'readonly': True, 'nonce': self.nonce, 'bytes': len(raw)}
                finally:
                    self._holds -= 1
        except (OSError, StoreError):
            raise CarrierError('Private session media is unavailable or still in use') from None

    def cleanup(self, *, guest_stopped):
        if guest_stopped is not True or self._holds:
            raise CarrierError('Carrier cleanup requires explicit confirmation that its guest is stopped')
        try:
            with _private_base(self.base), _open_private(self.path.parent, directory=True, delete=True) as directory:
                if {item.name for item in self.path.parent.iterdir()} != {'session.tar', 'carrier.json'}:
                    raise CarrierError('Unexpected files prevent session carrier cleanup')
                with _open_private(self.path.parent / 'carrier.json', delete=True) as marker_handle, \
                        _open_private(self.path, delete=True) as media_handle:
                    marker = _read_marker(marker_handle, self.nonce)
                    self._validate(marker, _read(media_handle, MAX_MEDIA))
                    _delete_handle(media_handle)
                    _delete_handle(marker_handle)
                _delete_handle(directory)
        except (OSError, StoreError):
            raise CarrierError('Private session media cannot be safely removed') from None


def create_carrier(session, *, base_directory=None):
    try:
        value = validate_session(session)
        nonce = _nonce(value['nonce'])
        payload = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if not 0 < len(payload) <= MAX_SESSION:
            raise ValueError()
    except (ValueError, TypeError, UnicodeError):
        raise CarrierError('Invalid gateway session data') from None
    base = _base_path(base_directory)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        entry = tarfile.TarInfo('session.json')
        entry.size, entry.mode = len(payload), 0o400
        archive.addfile(entry, io.BytesIO(payload))
    raw = output.getvalue()
    try:
        with _private_base(base, create=True):
            directory = base / nonce
            kernel, _ = _api()
            with _descriptor(True) as attributes:
                if not kernel.CreateDirectoryW(str(directory), ctypes.byref(attributes)):
                    raise CarrierError('A carrier already exists for this session; it was not overwritten')
            with _open_private(directory, directory=True, delete=True) as directory_handle:
                try:
                    path = directory / 'session.tar'
                    with _write_new(path, raw):
                        identity = _identity(path)
                        marker = {'schema': 1, 'kind': 'gateway-session-carrier', 'nonce': nonce,
                                  'revision': value['revision'], 'gateway_version': value['gateway_version'],
                                  'media_identity': identity, 'directory_id': directory.lstat().st_ino,
                                  'sha256': hashlib.sha256(raw).hexdigest()}
                        with _write_new(directory / 'carrier.json', json.dumps(marker).encode('utf-8')):
                            carrier = Carrier(base, nonce, identity)
                    return carrier
                except BaseException:
                    # Owned files have rolled back before this point; never remove an unexpected member.
                    if not any(directory.iterdir()):
                        _delete_handle(directory_handle)
                    raise
    except (OSError, StoreError):
        raise CarrierError('Private session media could not be created') from None


def open_carrier(path, nonce, *, base_directory=None):
    """Reopen one known orphan; never scan or delete unrelated application-data files."""
    nonce = _nonce(nonce)
    base = _base_path(base_directory)
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or path != base / nonce / 'session.tar':
        raise CarrierError('Carrier path does not match the selected session')
    try:
        with _private_base(base), _open_private(path.parent, directory=True), _open_private(path.parent / 'carrier.json') as handle:
            marker = _read_marker(handle, nonce)
            carrier = Carrier(base, nonce, marker['media_identity'])
            with carrier.hold():
                pass
            return carrier
    except (OSError, StoreError):
        raise CarrierError('The orphaned session carrier could not be verified') from None
