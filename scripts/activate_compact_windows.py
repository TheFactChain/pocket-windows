"""Explicit, forward-only activation of an accepted compact Windows candidate."""
from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import compact_windows as compact
from runtime_config import QEMU
from file_snapshot import locked_read
from profile_store import _pinned_directory, _plain
import launch_runtime as runtime


def _identity(path):
    return compact._identity(compact._plain(path))


def _expected(value):
    fields = {'device', 'file_id', 'size', 'mtime_ns', 'created_ns'}
    if not isinstance(value, dict) or set(value) != fields or any(type(n) is not int or n < 0 for n in value.values()):
        raise ValueError('Invalid prepared disk identity')
    return value


def _match(path, expected):
    if _identity(path) != expected:
        raise ValueError('Prepared disk identity changed')


def _at(path):
    try:
        return _identity(path)
    except FileNotFoundError:
        return None


@contextmanager
def _native_file(source):
    import msvcrt
    compact._plain(source)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(source), 0x80010100, 1, None, 3, 0x08200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, 'rb') as stream:
        yield kernel, handle, stream


def _restore_creation(kernel, handle, stream, expected, allowed_creation):
    actual = compact._identity(os.fstat(stream.fileno()))
    if actual == expected:
        return
    if actual != expected | {'created_ns': allowed_creation}:
        raise ValueError('Disk identity changed during native rename')
    # NTFS filename tunneling can inherit the former canonical name's creation time.
    ticks = expected['created_ns'] // 100 + 116444736000000000
    created = wintypes.FILETIME(ticks & 0xffffffff, ticks >> 32)
    kernel.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), wintypes.LPVOID, wintypes.LPVOID]
    kernel.SetFileTime.restype = wintypes.BOOL
    if not kernel.SetFileTime(handle, ctypes.byref(created), None, None):
        raise ctypes.WinError(ctypes.get_last_error())
    if compact._identity(os.fstat(stream.fileno())) != expected:
        raise ValueError('Could not preserve prepared disk creation time')


def _rename_verified(source, destination, expected, allowed_creation=None):
    """Check identity and rename through the same exclusive native file handle."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Activation destination already exists')
    with _native_file(source) as (kernel, handle, stream):
        if compact._identity(os.fstat(stream.fileno())) != expected:
            raise ValueError('Native activation handle identity changed')
        _match(source, expected)
        class RenameInfo(ctypes.Structure):
            _fields_ = [('ReplaceIfExists', ctypes.c_ubyte), ('RootDirectory', wintypes.HANDLE),
                        ('FileNameLength', wintypes.DWORD), ('FileName', wintypes.WCHAR * 1)]
        encoded = str(destination).encode('utf-16-le')
        size = RenameInfo.FileName.offset + len(encoded) + 2
        buffer = ctypes.create_string_buffer(size)
        info = ctypes.cast(buffer, ctypes.POINTER(RenameInfo)).contents
        info.ReplaceIfExists, info.RootDirectory, info.FileNameLength = 0, None, len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.FileName.offset, encoded, len(encoded))
        if not kernel.SetFileInformationByHandle(handle, 3, buffer, size):
            raise ctypes.WinError(ctypes.get_last_error())
        _restore_creation(kernel, handle, stream, expected, allowed_creation)


def _load_held(stack, path):
    compact._plain(path)
    reader = stack.enter_context(locked_read(path))
    compact._plain(path)
    data = reader.read(1024**2 + 1)
    if len(data) > 1024**2:
        raise ValueError('Activation evidence is oversized')
    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('Duplicate evidence JSON key')
            result[key] = item
        return result
    value = json.loads(data, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError('Invalid activation evidence')
    return value, hashlib.sha256(data).hexdigest()


def _time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('Evidence timestamp requires timezone')
    return parsed


def _validate_evidence(root, operation, prepared, boot):
    source, candidate = root / 'images/workvm-empty.qcow2', operation / 'candidate.qcow2'
    if (type(prepared.get('schema')) is not int or prepared.get('schema') != 1 or prepared.get('kind') != 'compact-windows-prepare'
            or prepared.get('operation_id') != operation.name.removeprefix('compact-')
            or prepared.get('phase') != 'awaiting_boot' or prepared.get('awaiting_boot') is not True
            or prepared.get('activated') is not False or prepared.get('source_write_denied') is not True
            or prepared.get('tool_sha256') != compact.QEMU_IMG_SHA256
            or prepared.get('source') != source.relative_to(root).as_posix()
            or prepared.get('candidate') != candidate.relative_to(root).as_posix()):
        raise ValueError('Compact preparation is not eligible for activation')
    original = _expected(prepared.get('source_identity'))
    identity = prepared.get('candidate_identity')
    if not isinstance(identity, dict):
        raise ValueError('Missing prepared candidate identity')
    virtual = prepared.get('source_virtual_size')
    if type(virtual) is not int or virtual <= 0 or identity.get('virtual_size') != virtual:
        raise ValueError('Candidate virtual size mismatch')
    compact_id = _expected({key: value for key, value in identity.items() if key != 'virtual_size'})
    tool = root / QEMU / 'qemu-img.exe'
    commands = prepared.get('commands')
    if not isinstance(commands, list):
        raise ValueError('Missing check and compare evidence')
    for args, checked in ((['check', '--output=json', '-f', 'qcow2', str(source)], True),
                          (['check', '--output=json', '-f', 'qcow2', str(candidate)], True),
                          (['compare', '-f', 'qcow2', '-F', 'qcow2', str(source), str(candidate)], False)):
        records = [record for record in commands if isinstance(record, dict) and record.get('argv') == [str(tool), *args]]
        if len(records) != 1 or records[0].get('status') != 'finished' or type(records[0].get('returncode')) is not int or records[0]['returncode'] != 0:
            raise ValueError('Checks and content comparison were not successful')
        if checked:
            compact._validate_check(json.loads(records[0].get('stdout', '')))
    reported_source = boot.get('source')
    if ('backing_evidence' in boot or not isinstance(reported_source, str) or '..' in Path(reported_source).parts
            or Path(reported_source).is_absolute()
            or root / reported_source != candidate
            or boot.get('passed') is not True or boot.get('phase') != 'completed'
            or boot.get('network') != 'none' or boot.get('source_write_denied') is not True
            or boot.get('source_metadata_unchanged') is not True or boot.get('process_still_running') is not False
            or type(boot.get('exit_code')) is not int or boot['exit_code'] != 0
            or not any(event.get('event') == 'SHUTDOWN' and event.get('data', {}).get('guest') is True
                       and event['data'].get('reason') == 'guest-shutdown'
                       for event in boot.get('events', []) if isinstance(event, dict))):
        raise ValueError('An exact candidate boot and guest shutdown are required')
    started, finished = _time(boot['started_utc']), _time(boot['finished_utc'])
    if not _time(prepared['updated_utc']) <= started <= finished <= datetime.now(timezone.utc):
        raise ValueError('Candidate boot evidence is stale or inconsistent')
    return original, compact_id


def _validate_direct_overlay(stack, root, operation, boot_path, boot):
    overlay = boot_path.parent / 'work-test.qcow2'
    candidate = operation / 'candidate.qcow2'
    command = boot.get('command')
    drive = 'if=ide,index=0,format=qcow2,file=' + overlay.relative_to(root).as_posix()
    if (not isinstance(command, list) or not all(isinstance(part, str) for part in command)
            or not any(command[index:index + 2] == ['-drive', drive] for index in range(len(command) - 1))):
        raise ValueError('Boot evidence must attach its fresh fixed acceptance overlay')
    compact._plain(overlay)
    reader = stack.enter_context(locked_read(overlay))
    info = compact._plain(overlay)
    header = reader.read(20)
    if len(header) != 20:
        raise ValueError('Acceptance overlay header is incomplete')
    magic, version, offset, length = struct.unpack('>IIQI', header)
    if (magic != 0x514649FB or version not in (2, 3) or not 0 < length <= 32768
            or not 20 <= offset <= info.st_size - length):
        raise ValueError('Acceptance overlay is not a standalone direct test of this candidate')
    reader.seek(offset)
    encoded = reader.read(length)
    try:
        backing = encoded.decode('utf-8')
    except UnicodeDecodeError:
        backing = encoded.decode('mbcs')
    if '\0' in backing or not Path(backing).is_absolute() or Path(backing) != candidate:
        raise ValueError('Acceptance overlay must immediately back onto the exact compact candidate')
    created = datetime.fromtimestamp(getattr(info, 'st_birthtime_ns', info.st_ctime_ns) / 10**9, timezone.utc)
    # FILETIME and wall-clock timestamps can differ below one millisecond on Windows.
    tolerance = timedelta(milliseconds=1)
    if not _time(boot['started_utc']) - tolerance <= created <= _time(boot['finished_utc']) + tolerance:
        raise ValueError('Acceptance overlay predates this boot attempt')


def activate_compact(root, prepare_journal, boot_evidence):
    return _activate(root, prepare_journal, boot_evidence, resume=False)


def resume_compact(root, prepare_journal, boot_evidence):
    return _activate(root, prepare_journal, boot_evidence, resume=True)


def _activate(root, prepare_journal, boot_evidence, resume):
    root = Path(root)
    if os.name != 'nt' or not root.is_absolute() or '..' in root.parts or str(root).startswith('\\\\'):
        raise ValueError('An absolute local Windows project root is required')
    prepare = Path(prepare_journal)
    boot_path = Path(boot_evidence)
    prepare = prepare if prepare.is_absolute() else root / prepare
    boot_path = boot_path if boot_path.is_absolute() else root / boot_path
    operation = prepare.parent
    if (prepare.name != 'journal.json' or operation.parent != root / 'images'
            or not re.fullmatch('compact-[a-f0-9]{32}', operation.name)
            or not boot_path.is_relative_to(root / 'logs') or boot_path.name != 'boot-result.json'
            or '..' in prepare.parts or '..' in boot_path.parts):
        raise ValueError('Activation evidence paths must belong to this compact operation')
    source, candidate, backup = root / 'images/workvm-empty.qcow2', operation / 'candidate.qcow2', operation / 'original.qcow2'
    wal_path = operation / 'activation.json'
    with ExitStack() as stack:
        for directory in (root, root / 'images', operation, root / 'state', boot_path.parent):
            stack.enter_context(_pinned_directory(directory, create=False))
        _plain(root / 'state/runtime.lock')
        if not stack.enter_context(runtime.ProjectLock(root)) or runtime.engine_processes(root):
            raise RuntimeError('Stop all project QEMU processes before disk activation')
        prepared, prepare_hash = _load_held(stack, prepare)
        boot, boot_hash = _load_held(stack, boot_path)
        original_id, compact_id = _validate_evidence(root, operation, prepared, boot)
        _validate_direct_overlay(stack, root, operation, boot_path, boot)
        _plain(wal_path)
        identity = {'schema': 1, 'kind': 'compact-windows-activate', 'operation_id': prepared['operation_id'],
                    'prepare_sha256': prepare_hash, 'boot_sha256': boot_hash,
                    'boot_evidence': boot_path.relative_to(root).as_posix(),
                    'source_identity': original_id, 'candidate_identity': compact_id,
                    'source': source.relative_to(root).as_posix(), 'candidate': candidate.relative_to(root).as_posix(),
                    'backup': backup.relative_to(root).as_posix()}
        if resume:
            with ExitStack() as wal_reader:
                wal, _ = _load_held(wal_reader, wal_path)
            if (not isinstance(wal, dict) or set(wal) != set(identity) | {'phase'}
                    or any(wal.get(key) != value for key, value in identity.items())
                    or wal.get('phase') not in ('prepared', 'original_moved', 'candidate_moved', 'activated')):
                raise ValueError('Resume requires the exact recorded activation and evidence')
        else:
            if wal_path.exists():
                raise ValueError('Activation already exists; use explicit resume')
            wal = {**identity, 'phase': 'prepared'}
        if resume and _at(source) == compact_id | {'created_ns': original_id['created_ns']}:
            with _native_file(source) as (kernel, handle, stream):
                _restore_creation(kernel, handle, stream, compact_id, original_id['created_ns'])
        layout = (_at(source), _at(candidate), _at(backup))
        before = (original_id, compact_id, None)
        middle = (None, compact_id, original_id)
        after = (compact_id, None, original_id)
        if layout not in (before, middle, after) or (not resume and layout != before):
            raise ValueError('Disk layout differs from the exact activation identities; no rollback performed')
        if resume and ((wal['phase'] == 'prepared' and layout == after)
                       or (wal['phase'] == 'original_moved' and layout == before)
                       or (wal['phase'] in ('candidate_moved', 'activated') and layout != after)):
            raise ValueError('Activation journal and disk layout disagree; no rollback performed')
        if not resume:
            runtime.atomic_json(wal_path, wal)
        def save(phase):
            wal['phase'] = phase
            _plain(wal_path)
            runtime.atomic_json(wal_path, wal)
        if layout == before:
            if runtime.engine_processes(root):
                raise RuntimeError('A QEMU process appeared before activation')
            _rename_verified(source, backup, original_id)
            save('original_moved')
            layout = middle
        stack.enter_context(locked_read(backup))
        _match(backup, original_id)
        if layout == middle:
            if runtime.engine_processes(root):
                raise RuntimeError('A QEMU process appeared before candidate promotion')
            _rename_verified(candidate, source, compact_id, original_id['created_ns'])
            save('candidate_moved')
        _match(source, compact_id)
        _match(backup, original_id)
        save('activated')
        return {'schema': 1, 'activated': True, 'phase': 'activated',
                'source': source, 'retained_original': backup, 'journal': wal_path}

