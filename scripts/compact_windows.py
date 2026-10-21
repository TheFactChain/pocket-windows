"""Build-only Windows disk compaction. This module never activates or deletes disks.

Activation belongs to a separate explicit acceptance operation: verify this journal,
candidate file identity and a fresh successful boot report for that exact candidate,
then journal source->old and candidate->canonical renames. Keep the old disk until
acceptance completes; never automatically roll back after activation/user writes.
"""
import argparse
from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import uuid

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from file_snapshot import locked_read, QEMU_IMG_SHA256
import launch_runtime as runtime

RESERVE_BYTES = 2 * 1024 ** 3


class CompactionError(RuntimeError):
    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence


def _plain(path, directory=False):
    info = path.lstat()
    if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise CompactionError('Compaction paths cannot contain reparse points')
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise CompactionError('Compaction directory is unavailable')
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise CompactionError('Compaction files must be regular files without hard links')
    return info


@contextmanager
def _pin_directory(path):
    _plain(path, directory=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80, 3, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise CompactionError('Cannot pin compaction directory')
    try:
        _plain(path, directory=True)
        yield
    finally:
        kernel.CloseHandle(handle)


def _identity(info):
    return {'device': info.st_dev, 'file_id': info.st_ino, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns,
            'created_ns': getattr(info, 'st_birthtime_ns', info.st_ctime_ns)}


def _execute(command, *, cwd):
    return subprocess.run(command, cwd=cwd, capture_output=True,
                          creationflags=subprocess.CREATE_NO_WINDOW)


def _output(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return value if isinstance(value, str) else ''


def _validate_info(info, virtual_size=None):
    if (not isinstance(info, dict) or info.get('format') != 'qcow2'
            or type(info.get('virtual-size')) is not int or info['virtual-size'] <= 0
            or info.get('backing-filename') or info.get('full-backing-filename')
            or info.get('data-file') or info.get('dirty-flag') or info.get('encrypted')):
        raise CompactionError('The disk is not a clean standalone qcow2 image')
    specific = info.get('format-specific', {}).get('data', {})
    if specific.get('corrupt') or specific.get('data-file'):
        raise CompactionError('The disk has unsafe qcow2 metadata')
    if virtual_size is not None and (info['virtual-size'] != virtual_size or info.get('snapshots')):
        raise CompactionError('Candidate virtual size or snapshot state does not match the compaction contract')
    return info['virtual-size']


def _validate_check(checked):
    if type(checked.get('check-errors')) is not int or checked['check-errors'] != 0:
        raise CompactionError('qcow2 check did not confirm an error-free image')
    for name in ('corruptions', 'leaks'):
        if type(checked.get(name, 0)) is not int or checked.get(name, 0) != 0:
            raise CompactionError('qcow2 check reported image errors; automatic repair is forbidden')


def prepare_compact_windows(root, executor=None):
    """Prepare a fresh candidate, retaining source and partial outputs on every failure.

    executor(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess is
    injectable for fixture checks. Production calls the project-owned qemu-img.
    The result is awaiting_boot, never ready; callers must not switch runtime disks.
    """
    root = Path(root)
    if (os.name != 'nt' or not root.is_absolute() or '..' in root.parts
            or str(root).startswith('\\\\')):
        raise CompactionError('An absolute local Windows project directory is required')
    root = root.absolute()
    executor = _execute if executor is None else executor
    source = root / 'images/workvm-empty.qcow2'
    tool = root / 'runtime/qemu-11.1.0/qemu-img.exe'
    journal = None
    journal_path = None

    def save(phase):
        journal['phase'] = phase
        journal['updated_utc'] = runtime.utc_now()
        journal['transitions'].append({'phase': phase, 'utc': journal['updated_utc']})
        runtime.atomic_json(journal_path, journal)

    def command(arguments, json_result=False):
        values = [str(tool), *map(str, arguments)]
        record = {'argv': values, 'started_utc': runtime.utc_now(), 'status': 'running'}
        journal['commands'].append(record)
        runtime.atomic_json(journal_path, journal)
        try:
            result = executor(values, cwd=root)
        except BaseException as error:
            record.update(status='interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'failed',
                          error_type=type(error).__name__)
            runtime.atomic_json(journal_path, journal)
            raise
        stdout, stderr = _output(result.stdout), _output(result.stderr)
        record.update(status='finished', returncode=result.returncode,
                      stdout=stdout[:16384], stderr=stderr[:16384], finished_utc=runtime.utc_now())
        runtime.atomic_json(journal_path, journal)
        if type(result.returncode) is not int or result.returncode != 0:
            raise CompactionError('qemu-img did not confirm the compaction step')
        if json_result:
            if len(stdout.encode('utf-8')) > 65536:
                raise CompactionError('qemu-img returned an oversized report')
            value = json.loads(stdout)
            if not isinstance(value, dict):
                raise CompactionError('qemu-img returned an invalid report')
            return value

    stack = ExitStack()
    try:
        for parent in [*reversed(root.parents), root]:
            stack.enter_context(_pin_directory(parent))
        for directory in (root / 'images', root / 'runtime', tool.parent):
            stack.enter_context(_pin_directory(directory))
        _plain(source)
        _plain(tool)
        state = root / 'state'
        if not state.exists():
            state.mkdir()
        stack.enter_context(_pin_directory(state))
        lock_path = state / 'runtime.lock'
        if lock_path.exists():
            _plain(lock_path)
        if not stack.enter_context(runtime.ProjectLock(root)):
            raise CompactionError('Another project operation is running')
        _plain(lock_path)
        if runtime.engine_processes(root):
            raise CompactionError('Shut down project virtual machines before preparing a compact disk')
        source_stream = stack.enter_context(locked_read(source))
        tool_stream = stack.enter_context(locked_read(tool))
        tool_digest = hashlib.file_digest(tool_stream, 'sha256').hexdigest()
        if tool_digest != QEMU_IMG_SHA256:
            raise CompactionError('The verified qemu-img component is missing or changed')
        original_identity = _identity(os.fstat(source_stream.fileno()))
        if original_identity != _identity(_plain(source)):
            raise CompactionError('Source disk identity changed before preparation')
        operation_id = uuid.uuid4().hex
        operation = root / ('images/compact-' + operation_id)
        operation.mkdir(exist_ok=False)
        stack.enter_context(_pin_directory(operation))
        candidate = operation / 'candidate.qcow2'
        journal_path = operation / 'journal.json'
        journal = {'schema': 1, 'operation_id': operation_id, 'kind': 'compact-windows-prepare',
                   'source': source.relative_to(root).as_posix(),
                   'candidate': candidate.relative_to(root).as_posix(),
                   'source_identity': original_identity, 'source_write_denied': True,
                   'tool_sha256': tool_digest,
                   'started_utc': runtime.utc_now(), 'commands': [], 'transitions': [],
                   'awaiting_boot': False, 'ready': False, 'activated': False}
        save('created')
        source_info = command(['info', '--output=json', source], json_result=True)
        virtual_size = _validate_info(source_info)
        journal['source_virtual_size'] = virtual_size
        journal['source_snapshot_ids'] = [item.get('id') for item in source_info.get('snapshots', [])]
        checked = command(['check', '--output=json', '-f', 'qcow2', source], json_result=True)
        _validate_check(checked)
        save('source_checked')
        measure = command(['measure', '--output=json', '-f', 'qcow2', '-O', 'qcow2', source], json_result=True)
        required, fully_allocated = measure.get('required'), measure.get('fully-allocated')
        if (type(required) is not int or type(fully_allocated) is not int
                or not 0 < required <= fully_allocated):
            raise CompactionError('Invalid qemu-img space estimate')
        # Reserve the worst reported allocation, not only the expected sparse output.
        budget = fully_allocated + RESERVE_BYTES
        journal['space'] = {'required_bytes': required, 'fully_allocated_bytes': fully_allocated,
                            'reserve_bytes': RESERVE_BYTES, 'budget_bytes': budget,
                            'available_bytes': shutil.disk_usage(root).free}
        save('budget_checked')
        if shutil.disk_usage(root).free < budget:
            raise CompactionError('Insufficient free space for a safe compact disk candidate')
        if runtime.engine_processes(root) or candidate.exists() or candidate.is_symlink():
            raise CompactionError('A guest or unexpected candidate appeared during preparation')
        save('converting')
        command(['convert', '-f', 'qcow2', '-O', 'qcow2', source, candidate])
        _plain(candidate)
        save('converted')
        candidate_stream = stack.enter_context(locked_read(candidate))
        candidate_identity = _identity(os.fstat(candidate_stream.fileno()))
        candidate_info = command(['info', '--output=json', candidate], json_result=True)
        _validate_info(candidate_info, virtual_size)
        checked = command(['check', '--output=json', '-f', 'qcow2', candidate], json_result=True)
        _validate_check(checked)
        save('candidate_checked')
        command(['compare', '-f', 'qcow2', '-F', 'qcow2', source, candidate])
        if (_identity(_plain(source)) != original_identity
                or _identity(os.fstat(source_stream.fileno())) != original_identity
                or _identity(_plain(candidate)) != candidate_identity):
            raise CompactionError('A disk identity changed during preparation')
        journal['candidate_identity'] = candidate_identity | {'virtual_size': virtual_size}
        save('content_compared')
        journal['awaiting_boot'] = True
        save('awaiting_boot')
        return {'schema': 1, 'phase': 'awaiting_boot', 'awaiting_boot': True, 'ready': False,
                'candidate': journal['candidate'], 'evidence': journal_path.relative_to(root).as_posix(),
                'source_identity': original_identity, 'candidate_identity': journal['candidate_identity']}
    except BaseException as error:
        if journal is not None:
            journal['awaiting_boot'] = False
            journal['error_type'] = type(error).__name__
            save('interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'failed')
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(error, CompactionError):
            error.evidence = str(journal_path) if journal_path else None
            raise
        raise CompactionError('Compact disk preparation failed; the source disk was not replaced',
                              str(journal_path) if journal_path else None) from error

    finally:
        stack.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    arguments = parser.parse_args()
    try:
        print(json.dumps(prepare_compact_windows(arguments.root)))
    except CompactionError as error:
        print(json.dumps({'schema': 1, 'phase': 'failed', 'ready': False,
                          'awaiting_boot': False, 'error': str(error), 'evidence': error.evidence}))
        raise SystemExit(1)
