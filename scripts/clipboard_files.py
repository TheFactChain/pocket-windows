"""Streaming file-copy frames. No clipboard, transport, or remote source paths.

Each frame has id/event. begin: count/bytes (including zone bytes); entry:
path/kind/size/zone (base64 or null); data: offset/data; file_end: sha256;
end: no payload. Entries are preorder, one file at a time. detach returns the
stage path and retains this Receiver as an identity-safe cleanup lease: keep
it alive with the clipboard, then call release. close leaves detached data.
"""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import stat
import unicodedata
import uuid

CHUNK = 65536
MAX_ENTRIES = 100000
MAX_BYTES = 9007199254740991
MARGIN = 16 * 1024 * 1024
ENTRY_RESERVE = 4096
MAX_METADATA = 256 * 1024 * 1024
MAX_DEPTH = 256
MAX_PINS = 16384
_ID = re.compile(r"[0-9a-f]{32}\Z")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])\Z", re.I)


def _path(value):
    if (not isinstance(value, str) or not value or _utf16_bytes(value) > 48000
            or value.count('/') + 1 > MAX_DEPTH):
        raise ValueError("invalid path")
    for part in value.split('/'):
        if (not part or part in ('.', '..') or part[-1] in '. '
                or any(unicodedata.category(c) == 'Cc' or c in '\\:*?"<>|' for c in part)
                or _RESERVED.fullmatch(part.split('.')[0].rstrip(' '))):
            raise ValueError("unsafe path")
    return value


def _utf16_bytes(value):
    return len(value.encode('utf-16-le', 'surrogatepass'))


def _selection(path):
    if _utf16_bytes(str(path)) > 48000 or len(path.parts) - 1 > MAX_DEPTH:
        raise ValueError('selection path limit')
    return path


def _ancestors(path):
    current = Path(path.anchor)
    yield current
    for part in path.parts[1:]:
        current /= part
        yield current


def _charge(used, relative, absolute, zone_size=0):
    if used < 0 or used > MAX_METADATA:
        raise ValueError('metadata budget exceeded')
    total = used + zone_size + 2 * _utf16_bytes(relative) + 2 * _utf16_bytes(str(absolute)) + 512
    if total > MAX_METADATA:
        raise ValueError('metadata budget exceeded')
    return total


def _number(frame, key):
    n = frame[key]
    if type(n) is not int or n < 0 or n > MAX_BYTES:
        raise ValueError("invalid number")
    return n


def _check_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("invalid transfer id")


_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_kernel.CreateFileW.restype = wintypes.HANDLE
_kernel.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel.CompareStringOrdinal.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int, wintypes.BOOL]
_kernel.CompareStringOrdinal.restype = ctypes.c_int
_kernel.LCMapStringEx.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, ctypes.c_int,
                                wintypes.LPWSTR, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t]
_kernel.LCMapStringEx.restype = ctypes.c_int
_kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_kernel.SetFileInformationByHandle.restype = wintypes.BOOL


class _FileInfo(ctypes.Structure):
    _fields_ = [('attributes', wintypes.DWORD), ('created', wintypes.FILETIME), ('accessed', wintypes.FILETIME),
                ('written', wintypes.FILETIME), ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                ('size_low', wintypes.DWORD), ('links', wintypes.DWORD), ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]


_kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
_kernel.GetFileInformationByHandle.restype = wintypes.BOOL


def _identity(handle):
    info = _FileInfo()
    if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    if info.attributes & 0x400:
        raise ValueError('reparse entry')
    return info.volume, info.index_high, info.index_low


class _Ordinal:
    """Windows ordinal equality; the invariant uppercase hash may have extra collisions."""
    __slots__ = ('value', 'hashed')

    def __init__(self, value):
        self.value = value
        count = _kernel.LCMapStringEx('', 0x200, value, -1, None, 0, None, None, 0)
        if not count:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(count)
        if not _kernel.LCMapStringEx('', 0x200, value, -1, buffer, count, None, None, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        self.hashed = hash(buffer.value)

    def __hash__(self):
        return self.hashed

    def __eq__(self, other):
        if not isinstance(other, _Ordinal):
            return NotImplemented
        result = _kernel.CompareStringOrdinal(self.value, -1, other.value, -1, True)
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 2


def _space(path, size):
    if shutil.disk_usage(path).free < size + MARGIN:
        raise ValueError('insufficient disk space')


def _delete_owned(path, identity, directory):
    h = _kernel.CreateFileW(str(path), 0x80010000, 1, None, 3, 0x00200000 | (0x02000000 if directory else 0), None)
    if h == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if _identity(h) != identity:
            raise OSError('staging cleanup refused a replaced entry')
        delete = ctypes.c_ubyte(1)
        if not _kernel.SetFileInformationByHandle(h, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _kernel.CloseHandle(h)


def _pin(path):
    h = _kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x02200000, None)
    if h == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        s = os.lstat(path)
        if s.st_file_attributes & 0x400 or not stat.S_ISDIR(s.st_mode):
            raise ValueError("reparse/non-directory parent")
    except BaseException:
        _kernel.CloseHandle(h)
        raise
    return h


def _pins(path):
    _selection(path)
    result = []
    try:
        for p in _ancestors(path):
            if len(result) >= MAX_PINS:
                raise ValueError('pin handle budget exceeded')
            result.append(_pin(p))
        return result
    except BaseException:
        for h in reversed(result):
            _kernel.CloseHandle(h)
        raise


def _signature(path):
    s = os.lstat(path)
    if s.st_file_attributes & 0x400 or not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):
        raise ValueError("only regular files and directories")
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, stat.S_ISDIR(s.st_mode)


def _read_open(path):
    # No share-write/delete: prevent replacement or mutation while streaming.
    import msvcrt
    h = _kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if h == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(h, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        _kernel.CloseHandle(h)
        raise
    return os.fdopen(fd, 'rb')


def _write_open(path):
    import msvcrt
    h = _kernel.CreateFileW(str(path), 0x40000000, 1, None, 1, 0x00200000, None)
    if h == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(h, os.O_WRONLY | os.O_BINARY)
    except BaseException:
        _kernel.CloseHandle(h)
        raise
    return os.fdopen(fd, 'wb', buffering=0)


def _zone(path):
    try:
        with _read_open(str(path) + ':Zone.Identifier') as f:
            raw = f.read(CHUNK + 1)
        if len(raw) > CHUNK:
            raise ValueError("Zone.Identifier too large")
        return raw
    except OSError as e:
        if e.winerror in (2, 3):
            return None
        raise


class Sender:
    def __init__(self, paths, transfer_id):
        _check_id(transfer_id)
        self.id = transfer_id
        self.paths = []
        self._metadata_used = 0
        for path in itertools.islice(paths, MAX_ENTRIES + 1):
            if len(self.paths) == MAX_ENTRIES:
                raise ValueError('selection limit')
            p = _selection(Path(os.path.abspath(path)))
            self._metadata_used = _charge(self._metadata_used, p.name, p)
            self.paths.append(p)

    def frames(self):
        handles, entries, used = [], [], set()
        pinned = set()
        metadata = self._metadata_used
        total = 0
        try:
            def pin_directory(p):
                nonlocal metadata
                key = _Ordinal(str(p))
                if key in pinned:
                    return
                if len(handles) >= MAX_PINS:
                    raise ValueError('pin handle budget exceeded')
                metadata = _charge(metadata, '', p)
                handles.append(_pin(p))
                pinned.add(key)

            def visit(p, relative):
                nonlocal total, metadata
                _path(relative)
                signature = _signature(p)
                directory = signature[-1]
                if _Ordinal(relative) in used:
                    raise ValueError("case collision")
                zone = None if directory else _zone(p)
                metadata = _charge(metadata, relative, p, len(zone or b''))
                total += (0 if directory else signature[2]) + len(zone or b'')
                if len(entries) >= MAX_ENTRIES or total > MAX_BYTES:
                    raise ValueError("transfer limit")
                used.add(_Ordinal(relative))
                entries.append((p, relative, signature, zone))
                if directory:
                    pin_directory(p)
                    with os.scandir(p) as children:
                        for child in children:
                            child_relative = _path(relative + '/' + child.name)
                            visit(Path(child.path), child_relative)

            for p in self.paths:
                for parent in _ancestors(p.parent):
                    pin_directory(parent)
                name, index = p.name, 2
                stem, dot, extension = name.rpartition('.')
                if _signature(p)[-1] or not stem or not dot:
                    stem, dot, extension = name, '', ''
                _path(name)
                while _Ordinal(name) in used:
                    name = stem + ' (' + str(index) + ')' + dot + extension
                    index += 1
                visit(p, name)
            if not entries:
                raise ValueError("empty selection")
            def frame(event, **kw):
                return dict(id=self.id, event=event, **kw)
            yield frame('begin', count=len(entries), bytes=total)
            for p, relative, signature, zone in entries:
                if _signature(p) != signature:
                    raise ValueError("source changed")
                directory = signature[-1]
                yield frame('entry', path=relative, kind='dir' if directory else 'file',
                            size=0 if directory else signature[2],
                            zone=None if zone is None else base64.b64encode(zone).decode('ascii'))
                if directory:
                    continue
                digest, offset = hashlib.sha256(), 0
                with _read_open(p) as f:
                    s = os.fstat(f.fileno())
                    if (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, False) != signature:
                        raise ValueError("source replaced")
                    while raw := f.read(CHUNK):
                        digest.update(raw)
                        yield frame('data', offset=offset, data=base64.b64encode(raw).decode('ascii'))
                        offset += len(raw)
                    if offset != signature[2] or _signature(p) != signature or _zone(p) != zone:
                        raise ValueError("source changed")
                yield frame('file_end', sha256=digest.hexdigest())
            for p, _, signature, zone in entries:
                if _signature(p) != signature or (not signature[-1] and _zone(p) != zone):
                    raise ValueError("source changed")
            yield frame('end')
        finally:
            for h in reversed(handles):
                _kernel.CloseHandle(h)
            handles.clear()
            entries.clear()
            used.clear()
            pinned.clear()


class Receiver:
    def __init__(self, private_stage_parent, transfer_id):
        _check_id(transfer_id)
        self.id = transfer_id
        parent = _selection(Path(os.path.abspath(private_stage_parent)))
        self._metadata_used = 0
        for p in _ancestors(parent):
            self._metadata_used = _charge(self._metadata_used, '', p)
        self._handles = _pins(parent)
        self.stage = None
        self.roots = None
        self._file = None
        self._outputs = []
        self._owned = []
        self._detached = False
        self._closed = False
        self._begun = False
        self._entries = {}
        self._root_names = []
        self._count = self._bytes = 0
        try:
            _space(parent, ENTRY_RESERVE)
            candidate = parent / ('clipboard-' + uuid.uuid4().hex)
            self._metadata_used = _charge(self._metadata_used, '', candidate)
            if len(self._handles) >= MAX_PINS:
                raise ValueError('pin handle budget exceeded')
            candidate.mkdir()
            self.stage = candidate
            handle = _pin(self.stage)
            self._handles.append(handle)
            self._owned.append((self.stage, _identity(handle), True, handle))
        except BaseException:
            self.close()
            raise

    def accept(self, frame):
        try:
            return self._accept(frame)
        except BaseException:
            self.close()
            raise

    def _accept(self, f):
        if self._closed or self.roots is not None or not isinstance(f, dict):
            raise ValueError("closed/completed transfer")
        if len(json.dumps(f, ensure_ascii=True).encode('utf-8')) >= 1024 * 1024 or f.get('id') != self.id:
            raise ValueError("invalid frame")
        event = f.get('event')
        fields = {'begin': {'count', 'bytes'}, 'entry': {'path', 'kind', 'size', 'zone'},
                  'data': {'offset', 'data'}, 'file_end': {'sha256'}, 'end': set()}
        if event not in fields or set(f) != fields[event] | {'id', 'event'}:
            raise ValueError("unexpected fields/event")
        if event == 'begin':
            if self._begun:
                raise ValueError("duplicate begin")
            self._expected_count, self._expected_bytes = _number(f, 'count'), _number(f, 'bytes')
            if not 1 <= self._expected_count <= MAX_ENTRIES:
                raise ValueError("entry limit")
            _space(self.stage, self._expected_bytes + self._expected_count * ENTRY_RESERVE * 2)
            self._begun = True
        elif not self._begun:
            raise ValueError("missing begin")
        elif event == 'entry':
            if self._file:
                raise ValueError("unfinished file")
            relative = _path(f['path'])
            key = _Ordinal(relative)
            parent = relative.rpartition('/')[0]
            if (key in self._entries or (parent and self._entries.get(_Ordinal(parent)) != 'dir')
                    or f['kind'] not in ('file', 'dir') or self._count >= self._expected_count):
                raise ValueError("invalid entry order/collision")
            size = _number(f, 'size')
            zone = None
            if f['zone'] is not None:
                if not isinstance(f['zone'], str) or len(f['zone']) > 87384:
                    raise ValueError("invalid zone")
                zone = base64.b64decode(f['zone'], validate=True)
                if len(zone) > CHUNK:
                    raise ValueError("invalid zone")
            if f['kind'] == 'dir' and (size or zone is not None):
                raise ValueError("directory payload")
            if self._bytes + size + len(zone or b'') > self._expected_bytes:
                raise ValueError("excess bytes")
            target = self.stage.joinpath(*relative.split('/'))
            self._metadata_used = _charge(self._metadata_used, relative, target, len(zone or b''))
            if f['kind'] == 'dir' and len(self._handles) >= MAX_PINS:
                raise ValueError('pin handle budget exceeded')
            _space(self.stage, size + len(zone or b'') + ENTRY_RESERVE)
            if f['kind'] == 'dir':
                target.mkdir()
                handle = _pin(target)
                self._handles.append(handle)
                self._owned.append((target, _identity(handle), True, handle))
            else:
                self._file = _write_open(target)
                import msvcrt
                self._owned.append((target, _identity(msvcrt.get_osfhandle(self._file.fileno())), False, None))
                self._target, self._size, self._offset, self._zone = target, size, 0, zone
                self._hash = hashlib.sha256()
            self._entries[key] = f['kind']
            self._count += 1
            if not parent:
                self._root_names.append(relative)
        elif event == 'data':
            if self._file is None or _number(f, 'offset') != self._offset or not isinstance(f['data'], str) or len(f['data']) > 87384:
                raise ValueError("invalid data offset")
            raw = base64.b64decode(f['data'], validate=True)
            if not 1 <= len(raw) <= CHUNK or self._offset + len(raw) > self._size:
                raise ValueError("invalid data size")
            _space(self.stage, len(raw))
            if self._file.write(raw) != len(raw):
                raise OSError('short staging write')
            self._hash.update(raw)
            self._offset += len(raw)
        elif event == 'file_end':
            if self._file is None or self._offset != self._size or f['sha256'] != self._hash.hexdigest():
                raise ValueError("incomplete/corrupt file")
            self._file.flush()
            self._outputs.append(self._file)
            self._file = None
            if self._zone is not None:
                _space(self.stage, len(self._zone) + ENTRY_RESERVE)
                z = _write_open(str(self._target) + ':Zone.Identifier')
                self._outputs.append(z)
                if z.write(self._zone) != len(self._zone):
                    raise OSError('short Zone.Identifier write')
                z.flush()
            self._bytes += self._size + len(self._zone or b'')
        elif event == 'end':
            if self._file or self._count != self._expected_count or self._bytes != self._expected_bytes:
                raise ValueError("incomplete transfer")
            for output in self._outputs:
                output.close()
            self._outputs.clear()
            self.roots = [str(self.stage / name) for name in self._root_names]
            return self.roots
        return None

    def export_lease(self, boot_id):
        """Completed metadata only; caller must persist it before park/publish."""
        if self._closed or self.roots is None or self._file is not None:
            raise ValueError('not completed')
        if boot_id is not None and (type(boot_id) is not str or re.fullmatch(r'[0-9]{14}\.[0-9]{6}Z', boot_id) is None):
            raise ValueError('invalid boot id')
        entries, used = [], 256
        roots = list(self._root_names)
        for name in roots:
            used += len(json.dumps(name, ensure_ascii=True).encode('ascii')) + 1
            if used > 64 * 1024 * 1024:
                raise ValueError('lease metadata limit')
        for path, identity, directory, _ in self._owned:
            if not (identity[1] or identity[2]):
                raise ValueError('unusable stage identity')
            relative = '' if path == self.stage else path.relative_to(self.stage).as_posix()
            record = {'path': relative, 'kind': 'dir' if directory else 'file',
                      'volume': f'{identity[0]:016x}', 'file_id': f'{(identity[1] << 32) | identity[2]:016x}'}
            used += len(json.dumps(record, ensure_ascii=True, separators=(',', ':')).encode('ascii')) + 1
            if used > 64 * 1024 * 1024:
                raise ValueError('lease metadata limit')
            entries.append(record)
        return {'schema': 1, 'stage': self.stage.name, 'boot_id': boot_id, 'roots': roots, 'entries': entries}

    def park(self):
        """Close completed-stage handles without deleting; durable lease is caller-owned."""
        if self._closed or self.roots is None or self._file is not None:
            raise ValueError('not completed')
        for output in self._outputs:
            output.close()
        for handle in reversed(self._handles):
            _kernel.CloseHandle(handle)
        self._outputs.clear()
        self._handles.clear()
        self._owned.clear()
        self._entries.clear()
        self._root_names.clear()
        self._zone = self._hash = self._target = None
        self.roots = None
        self.stage = None
        self._closed = self._detached = True

    @staticmethod
    def resume_cleanup(private_stage_parent, lease):
        """Delete only recorded identities; no traversal/enumeration of unknown children."""
        if (type(lease) is not dict or set(lease) != {'schema', 'stage', 'boot_id', 'roots', 'entries'}
                or type(lease['schema']) is not int or lease['schema'] != 1
                or type(lease['stage']) is not str or re.fullmatch(r'clipboard-[0-9a-f]{32}', lease['stage']) is None
                or lease['boot_id'] is not None and (type(lease['boot_id']) is not str or re.fullmatch(r'[0-9]{14}\.[0-9]{6}Z', lease['boot_id']) is None)
                or type(lease['roots']) is not list or not 1 <= len(lease['roots']) <= MAX_ENTRIES
                or type(lease['entries']) is not list or not 2 <= len(lease['entries']) <= MAX_ENTRIES + 1):
            raise ValueError('invalid stage lease')
        used, known, records, roots = 256, {}, [], []
        for root in lease['roots']:
            _path(root)
            if '/' in root:
                raise ValueError('invalid root')
            used += len(json.dumps(root, ensure_ascii=True).encode('ascii')) + 1
        for index, item in enumerate(lease['entries']):
            if (type(item) is not dict or set(item) != {'path', 'kind', 'volume', 'file_id'}
                    or item['kind'] not in ('dir', 'file') or type(item['path']) is not str
                    or any(type(item[key]) is not str or re.fullmatch(r'[0-9a-f]{16}', item[key]) is None for key in ('volume', 'file_id'))):
                raise ValueError('invalid stage identity')
            volume, file_id = int(item['volume'], 16), int(item['file_id'], 16)
            if volume > 0xffffffff or not file_id:
                raise ValueError('unusable stage identity')
            relative = item['path']
            if index == 0:
                if relative or item['kind'] != 'dir':
                    raise ValueError('missing stage identity')
            else:
                _path(relative)
                key, parent = _Ordinal(relative), relative.rpartition('/')[0]
                if key in known or parent and known.get(_Ordinal(parent)) != 'dir':
                    raise ValueError('invalid stage entry order')
                known[key] = item['kind']
                if not parent:
                    roots.append(relative)
            used += len(json.dumps(item, ensure_ascii=True, separators=(',', ':')).encode('ascii')) + 1
            if used > 64 * 1024 * 1024:
                raise ValueError('lease metadata limit')
            records.append((relative, (volume, file_id >> 32, file_id & 0xffffffff), item['kind'] == 'dir'))
        if roots != lease['roots']:
            raise ValueError('stage roots differ')
        parent = _selection(Path(os.path.abspath(private_stage_parent)))
        if re.match(r'^[A-Za-z]:\\', str(parent)) is None:
            raise ValueError('a local private parent is required')
        stage, parent_pins, directory_pins = parent / lease['stage'], _pins(parent), {}
        try:
            # Check stage and every known existing identity before deleting anything.
            for relative, identity, directory in records:
                target = stage.joinpath(*relative.split('/')) if relative else stage
                try:
                    if directory:
                        if len(parent_pins) + len(directory_pins) >= MAX_PINS:
                            raise ValueError('pin handle budget exceeded')
                        handle = _pin(target)
                        directory_pins[relative] = handle
                        actual = _identity(handle)
                    else:
                        import msvcrt
                        with _read_open(target) as stream:
                            actual = _identity(msvcrt.get_osfhandle(stream.fileno()))
                    if actual != identity:
                        raise ValueError('staging cleanup refused a replaced entry')
                except OSError as error:
                    if getattr(error, 'winerror', None) in (2, 3) or isinstance(error, FileNotFoundError):
                        if not relative:
                            return
                        continue
                    raise
            for relative, identity, directory in reversed(records):
                handle = directory_pins.pop(relative, None)
                if handle is not None:
                    _kernel.CloseHandle(handle)
                try:
                    _delete_owned(stage.joinpath(*relative.split('/')) if relative else stage, identity, directory)
                except OSError as error:
                    if getattr(error, 'winerror', None) not in (2, 3) and not isinstance(error, FileNotFoundError):
                        raise
        finally:
            for handle in reversed(list(directory_pins.values())):
                _kernel.CloseHandle(handle)
            for handle in reversed(parent_pins):
                _kernel.CloseHandle(handle)

    def detach(self):
        if self._closed or self.roots is None:
            raise ValueError("not completed")
        self._detached = True
        return str(self.stage)

    def close(self):
        if self._closed or self._detached:
            return
        self.release()

    def release(self):
        """Release clipboard lifetime; delete only original entries, never unknown children."""
        if self._closed:
            return
        if self._file:
            self._file.close()
            self._file = None
        for output in self._outputs:
            output.close()
        self._outputs.clear()
        errors = []
        if self.stage is not None and not self._owned:
            errors.append(OSError('staging identity was unavailable; cleanup refused'))
        try:
            for path, identity, directory, handle in reversed(self._owned):
                if handle is not None:
                    _kernel.CloseHandle(handle)
                    self._handles.remove(handle)
                try:
                    _delete_owned(path, identity, directory)
                except (OSError, ValueError) as error:
                    errors.append(error)
        finally:
            for h in reversed(self._handles):
                _kernel.CloseHandle(h)
            self._handles.clear()
            self._owned.clear()
            self._entries.clear()
            self._root_names.clear()
            self._closed = True
        if errors:
            raise OSError('identity-safe staging cleanup could not remove all entries') from errors[0]
        self.stage = None

    abort = close

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
