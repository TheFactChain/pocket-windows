"""Supervise owned guests; Windows is never terminated or sent QMP quit."""
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import re
from contextlib import ExitStack, contextmanager
from collections import deque

sys.path.insert(0, str(Path(__file__).resolve().parent))


NETWORKS = {'gateway': 'gateway', 'work': 'isolated-lan'}
DISKS = {'gateway': 'images/gateway-empty.qcow2', 'work': 'images/workvm-empty.qcow2'}
START_TIMEOUT = 210
SHUTDOWN_TIMEOUT = 120
FILETIME_EPOCH = 11644473600
_CONTROLLER_DRAINS = []
_CONTROLLER_WRITERS = []


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def same_path(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def atomic_json(path, value):
    from profile_store import _plain, _pinned_directory
    with _pinned_directory(path.parent, create=False):
        _plain(path)
        _atomic_json_pinned(path, value)


def _atomic_json_pinned(path, value):
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(.05)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    from profile_store import _plain, _pinned_directory
    from file_snapshot import locked_read
    for attempt in range(3):
        try:
            with _pinned_directory(path.parent, create=False), locked_read(path) as stream:
                _plain(path)
                opened, named = os.fstat(stream.fileno()), path.lstat()
                if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                    raise RuntimeError('Project state file identity changed')
                return json.load(stream)
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(.05)


def engine_path(root):
    return root / 'runtime/qemu-11.1.0/qemu-system-x86_64.exe'


def controller_command(root, role, work_test_media=False):
    if role == 'gateway':
        return [str(root / 'runtime/python-3.12.14/python.exe'), '-I', '-S', '-B',
                str(root / 'scripts/gateway_controller.py'), '--project', str(root)]
    command = [str(root / 'runtime/python-3.12.14/python.exe'), '-I', '-S', '-B',
            str(root / 'scripts/control_guest_install.py'), '--project', str(root),
            '--guest', role + '-boot', '--network', NETWORKS[role]]
    if work_test_media and role == 'work':
        command.append('--work-test-media')
    return command


def kernel_api():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    return kernel


def _handle_identity(kernel, handle, pid):
    code = wintypes.DWORD()
    if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
        raise ctypes.WinError(ctypes.get_last_error())
    if code.value != 259:  # STILL_ACTIVE
        return None
    name = ctypes.create_unicode_buffer(32768)
    length = wintypes.DWORD(len(name))
    if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    created, exited, cpu_kernel, cpu_user = (wintypes.FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                  ctypes.byref(cpu_kernel), ctypes.byref(cpu_user)):
        raise ctypes.WinError(ctypes.get_last_error())
    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
    return {'pid': pid, 'image': name.value, 'created_filetime': ticks}


def process_identity(pid):
    """Creation FILETIME prevents a recycled PID from becoming an owned guest."""
    if type(pid) is not int or pid <= 0:
        raise RuntimeError('Invalid process identity')
    kernel = kernel_api()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _handle_identity(kernel, handle, pid)
    finally:
        kernel.CloseHandle(handle)


def _handle_arguments(handle):
    """Query the pinned process itself; editable state cannot authorise termination."""
    class UnicodeString(ctypes.Structure):
        _fields_ = [('length', wintypes.USHORT), ('maximum', wintypes.USHORT), ('buffer', ctypes.c_void_p)]
    query = ctypes.WinDLL('ntdll').NtQueryInformationProcess
    query.argtypes = [wintypes.HANDLE, wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG,
                      ctypes.POINTER(wintypes.ULONG)]
    query.restype = wintypes.LONG
    needed = wintypes.ULONG()
    query(handle, 60, None, 0, ctypes.byref(needed))  # ProcessCommandLineInformation, Windows 8.1+
    if not ctypes.sizeof(UnicodeString) < needed.value <= 131072:
        raise RuntimeError('Cannot verify gateway command line; reset refused')
    buffer = ctypes.create_string_buffer(needed.value)
    if query(handle, 60, buffer, len(buffer), ctypes.byref(needed)) < 0:
        raise RuntimeError('Cannot read gateway command line; reset refused')
    value = UnicodeString.from_buffer(buffer)
    start, pointer = ctypes.addressof(buffer), value.buffer
    if (not pointer or not value.length or value.length % 2
            or pointer < start + ctypes.sizeof(UnicodeString)
            or pointer + value.length > start + len(buffer)):
        raise RuntimeError('Invalid gateway command line; reset refused')
    command = ctypes.wstring_at(pointer, value.length // 2)
    parse = ctypes.WinDLL('shell32', use_last_error=True).CommandLineToArgvW
    parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    arguments = parse(command, ctypes.byref(count))
    if not arguments:
        raise ctypes.WinError(ctypes.get_last_error())
    kernel = kernel_api()
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    kernel.LocalFree.restype = wintypes.LPVOID
    try:
        return list(arguments[:count.value])
    finally:
        kernel.LocalFree(arguments)


def engine_processes(root):
    """Fail closed for an untracked process using this project's QEMU binary."""
    class ProcessEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    kernel = kernel_api()
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ('Process32FirstW', 'Process32NextW'):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        success = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while success:
            if entry.szExeFile.casefold() == engine_path(root).name.casefold():
                identity = process_identity(entry.th32ProcessID)
                if identity and same_path(identity['image'], engine_path(root)):
                    result[identity['pid']] = identity
            success = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(snapshot)
    return result


def same_identity(expected, actual):
    return (isinstance(expected, dict) and isinstance(actual, dict)
            and type(expected.get('pid')) is int and type(expected.get('created_filetime')) is int
            and expected.get('pid') == actual.get('pid')
            and expected.get('created_filetime') == actual.get('created_filetime')
            and same_path(expected.get('image', ''), actual.get('image', '')))


def safe_state_path(root, path):
    path = plain_log_path(root, path)
    if (path.name != 'state.json' or not path.parent.name.startswith('guest-')
            or path.parent.parent != (root / 'logs').resolve()):
        raise RuntimeError('Refusing a guest state path outside this project log directory')
    return path


def safe_receipt_path(root, path):
    path = plain_log_path(root, path)
    if (path.name != 'receipt.json' or not path.parent.name.startswith('runtime-launch-')
            or path.parent.parent != (root / 'logs').resolve()):
        raise RuntimeError('Refusing a runtime receipt path outside this project log directory')
    return path


def plain_log_path(root, path):
    from profile_store import _plain, StoreError
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise RuntimeError('Unsafe project state path')
    try:
        for item in [*reversed(path.parents), path]:
            _plain(item)
    except StoreError as error:
        raise RuntimeError('Unsafe project state path') from error
    return path


def state_matches(root, path, state, identity):
    try:
        safe_state_path(root, path)
        role = state['guest'].split('-', 1)[0]
        started = datetime.fromisoformat(state['started_utc'])
        created = identity['created_filetime'] / 10_000_000 - FILETIME_EPOCH
        disk = DISKS.get(role)
        if role == 'work' and 'session_nonce' in state:
            if not valid_work_session(root, state):
                return False
        if role == 'gateway':
            nonce = state.get('nonce', '')
            disk = 'logs/guest-gateway-boot-' + nonce + '/gateway.qcow2'
            if (state.get('schema') != 2 or not re.fullmatch('[0-9a-f]{32}', nonce)
                    or Path(path).parent.name != 'guest-gateway-boot-' + nonce
                    or state.get('disk') != disk or type(state.get('test_only')) is not bool
                    or not same_identity(identity, state.get('process'))
                    or not all(re.fullmatch('[0-9a-f]{32}', state.get(key, ''))
                               for key in ('profile_id', 'profile_revision_id'))
                    or not isinstance(state.get('gateway_version'), str)
                    or not valid_controller(root, state.get('controller'))):
                return False
        return (role in DISKS and type(state['pid']) is int and state['pid'] == identity['pid']
                and same_path(identity['image'], engine_path(root)) and started.tzinfo is not None
                and -.25 <= started.timestamp() - created <= 5
                and any('file=' + disk in argument.split(',')
                        for argument in state['arguments']))
    except (KeyError, TypeError, ValueError, RuntimeError, OverflowError):
        return False


def valid_controller(root, identity):
    return (isinstance(identity, dict) and type(identity.get('pid')) is int
            and identity['pid'] > 0 and type(identity.get('created_filetime')) is int
            and identity['created_filetime'] > 0
            and same_path(identity.get('image', ''), root / 'runtime/python-3.12.14/python.exe'))


def valid_vm_identity(root, identity):
    return (isinstance(identity, dict) and type(identity.get('pid')) is int and identity['pid'] > 0
            and type(identity.get('created_filetime')) is int and identity['created_filetime'] > 0
            and same_path(identity.get('image', ''), engine_path(root)))


def reset_proof_valid(root, proof, identity, generation):
    return (isinstance(proof, dict) and proof.get('generation') == generation
            and type(proof.get('generation')) is int and proof.get('exit_code') == 0
            and type(proof.get('exit_code')) is int and proof.get('reason') == 'guest-reset'
            and proof.get('gone') is True and valid_vm_identity(root, identity)
            and same_identity(identity, proof.get('process')))


def valid_work_session(root, state):
    nonce, generation = state.get('session_nonce'), state.get('generation')
    history = state.get('previous_generations')
    return (type(state.get('schema')) is int and state['schema'] == 1
            and state.get('guest') == 'work-boot' and state.get('network') == 'isolated-lan'
            and isinstance(nonce, str) and re.fullmatch('[a-f0-9]{32}', nonce) is not None
            and type(generation) is int and 1 <= generation <= 64
            and valid_controller(root, state.get('controller'))
            and valid_vm_identity(root,state.get('process')) and type(state.get('pid')) is int
            and state['pid'] == state['process']['pid']
            and isinstance(history, list) and len(history) == generation-1
            and all(isinstance(proof,dict) and reset_proof_valid(root, proof, proof.get('process'), index+1)
                    for index, proof in enumerate(history)))


@contextmanager
def work_session_lock(directory):
    from profile_store import _pinned_directory, _exclusive_file, StoreBusy
    with _pinned_directory(directory, create=False):
        for attempt in range(50):
            try:
                with _exclusive_file(directory/'work-lifecycle.lock'):
                    yield
                return
            except StoreBusy:
                if attempt == 49:
                    raise RuntimeError('Windows lifecycle operation has not settled') from None
                time.sleep(.1)


def refresh_work_record(root, record, state, allow_terminal_reset=False):
    if (not valid_work_session(root, state) or record.get('session_nonce') != state['session_nonce']
            or not same_identity(record.get('controller'), state['controller'])
            or not state_matches(root, Path(record['state']), state, state['process'])):
        raise RuntimeError('Windows controller session identity changed')
    old, new = record.get('generation'), state['generation']
    if type(old) is not int or new < old:
        raise RuntimeError('Windows generation moved backwards')
    if new == old:
        if not same_identity(record['process'], state['process']):
            raise RuntimeError('Windows PID changed without a verified reboot')
        return
    if not same_identity(state['controller'], process_identity(state['controller']['pid'])):
        # Explicit Stop may reconcile a completed reset after its controller exited.
        # This never adopts a live replacement QEMU without a live controller.
        if (not allow_terminal_reset or state.get('status') != 'exited'
                or type(state.get('exit_code')) is not int or state['exit_code'] != 0
                or same_identity(state['process'],process_identity(state['process']['pid']))
                or not reset_proof_valid(root,state.get('reset_proof'),state['process'],new)):
            raise RuntimeError('Windows controller disappeared during reboot; gateway retained')
    for generation in range(old, new):
        proof=state['previous_generations'][generation-1]
        expected=record['process'] if generation == old else proof['process']
        if (not reset_proof_valid(root, proof, expected, generation)
                or same_identity(expected, process_identity(expected['pid']))):
            raise RuntimeError('Previous Windows generation exit is not proven')
    record.update(process=state['process'], generation=new)


def state_record(path, state, identity):
    record = {'state': str(path), 'process': identity}
    for key in ('controller', 'nonce', 'session_nonce', 'generation'):
        if key in state:
            record[key] = state[key]
    return record


def controller_live(root, record):
    try:
        if not record_live(root, record):
            return False
        state = read_json(safe_state_path(root, record['state']))
        expected = state.get('controller')
        return (state.get('status') not in ('exited', 'control-unavailable-guest-running')
                and valid_controller(root, expected)
                and same_identity(expected, process_identity(expected['pid']))
                and ('controller' not in record or same_identity(record['controller'], expected)))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return False


def gateway_ready(root, record):
    try:
        if not controller_live(root, record):
            return False
        state = read_json(safe_state_path(root, record['state']))
        health = datetime.fromisoformat(state['health_utc'])
        age = (datetime.now(timezone.utc) - health).total_seconds()
        return (state.get('schema') == 2 and state.get('guest') == 'gateway-boot'
                and state.get('test_only') is False
                and state.get('status') == 'running' and state.get('network_verified') is True
                and record.get('nonce') == state['nonce'] and -2 <= age < 45)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return False


def discover_guests(root):
    processes = engine_processes(root)
    found, tracked = {}, set()
    for path in (root / 'logs').glob('guest-*/state.json'):
        try:
            state = read_json(path)
            identity = processes.get(state.get('pid'))
            if ('session_nonce' in state and state.get('guest') == 'work-boot'
                    and not identity and state.get('status') in ('restarting','starting','running','paused')
                    and valid_work_session(root, state)):
                candidate=state_record(path,state,state['process'])
                if record_live(root,candidate):
                    if 'work' in found:
                        raise RuntimeError('Multiple live work controller sessions')
                    found['work']=candidate
                continue
            if not identity or not state_matches(root, path, state, identity):
                continue
            tracked.add(identity['pid'])
            role = state['guest'].split('-', 1)[0]
            if role in found:
                raise RuntimeError('Multiple live ' + role + ' guests reference the project disk')
            if state['guest'] != role + '-boot' or state.get('network') != NETWORKS[role]:
                raise RuntimeError('The live ' + role + ' guest is not a normal boot with the accepted network; close it normally first')
            found[role] = state_record(path, state, identity)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    if set(processes) - tracked:
        raise RuntimeError('Untracked live project QEMU process; no disk will be opened. Inspect its controller log first')
    return found


def recovery_snapshot(root):
    """Read-only proof: no owned Windows/controller and no unknown project QEMU."""
    processes = engine_processes(root)
    work, gateways, tracked = [], [], set()
    for path in (root / 'logs').glob('guest-*/state.json'):
        state = read_json(safe_state_path(root, path))
        if not isinstance(state, dict):
            raise RuntimeError('Invalid session state; recovery refused')
        identity = state.get('process')
        if state.get('guest') == 'work-boot' and 'session_nonce' in state:
            if state.get('status') in ('exited', 'interrupted') and not valid_work_session(root, state):
                controller = state.get('controller')
                image = Path(controller['image']) if isinstance(controller, dict) and isinstance(controller.get('image'), str) else Path()
                old_root = image.parents[2] if image.is_absolute() and len(image.parents) > 2 else root
                # Moved terminal history grants no ownership; both recorded processes must be gone.
                if not same_path(old_root, root) and valid_work_session(old_root, state):
                    if (same_identity(identity, process_identity(identity['pid']))
                            or same_identity(controller, process_identity(controller['pid']))):
                        raise RuntimeError('Historical Windows or its controller is still active; recovery refused')
                    continue
            if not valid_work_session(root, state) or not state_matches(root, path, state, identity):
                raise RuntimeError('Windows session identity is invalid; recovery refused')
            if (same_identity(identity, process_identity(identity['pid']))
                    or same_identity(state['controller'], process_identity(state['controller']['pid']))):
                raise RuntimeError('Windows or its controller is still active; recovery refused')
            if state.get('status') not in ('exited', 'interrupted'):
                work.append(state_record(path, state, identity))
        elif state.get('guest') == 'gateway-boot' and isinstance(identity, dict):
            actual = processes.get(identity.get('pid'))
            if not actual:
                continue
            if not same_identity(identity, actual) or not state_matches(root, path, state, actual):
                raise RuntimeError('Gateway identity changed; recovery refused')
            if actual['pid'] in tracked:
                raise RuntimeError('Duplicate gateway ownership; recovery refused')
            gateways.append(state_record(path, state, actual))
            tracked.add(actual['pid'])
    if set(processes) != tracked:
        raise RuntimeError('Untracked project QEMU; recovery refused')
    return {'available': bool(work or gateways), 'work': work, 'gateways': gateways}


def reset_gateway(root, record):
    """Explicit recovery may reset only a proved disposable gateway, never Windows."""
    from profile_store import _plain
    path = safe_state_path(root, record['state'])
    state = read_json(path)
    if (state.get('guest') != 'gateway-boot' or not state_matches(root, path, state, record['process'])
            or state.get('nonce') != record.get('nonce')):
        raise RuntimeError('Only an owned disposable gateway may be reset')
    disk = path.parent / 'gateway.qcow2'
    _plain(disk)
    info = disk.stat()
    if state.get('disk_identity') != {'device': info.st_dev, 'file_id': info.st_ino}:
        raise RuntimeError('Gateway overlay identity changed; reset refused')
    snapshot = recovery_snapshot(root)
    if not any(same_identity(record['process'], item['process']) for item in snapshot['gateways']):
        raise RuntimeError('Gateway is no longer the recovery candidate')
    expected = record['process']
    kernel = kernel_api()
    handle = kernel.OpenProcess(0x101001, False, expected['pid'])
    if not handle:
        if ctypes.get_last_error() == 87:
            return False
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not same_identity(expected, _handle_identity(kernel, handle, expected['pid'])):
            raise RuntimeError('Gateway process changed; reset refused')
        from gateway_controller import arguments as gateway_arguments
        from runtime_config import validate_settings
        actual = _handle_arguments(handle)
        try:
            settings = validate_settings({'vcpus': int(actual[actual.index('-smp') + 1]),
                'gateway_memory_mib': int(actual[actual.index('-m') + 1])})
            carrier = state['carrier']
            if not isinstance(carrier, str) or not Path(carrier).is_absolute():
                raise ValueError('Invalid carrier path')
            allowed = gateway_arguments(root, path.parent,
                {'path': carrier, 'serial': 'WCSESSION', 'readonly': True}, settings)
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise RuntimeError('Cannot prove disposable gateway arguments; reset refused') from error
        if (not actual or not same_path(actual[0], engine_path(root))
                or actual[1:] != allowed or state.get('arguments') != allowed):
            raise RuntimeError('Process is not the disposable gateway; reset refused')
        # Pin the target handle and recheck Windows immediately before the explicit reset.
        recovery_snapshot(root)
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        if not kernel.TerminateProcess(handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        if kernel.WaitForSingleObject(handle, 10000) != 0:
            raise RuntimeError('Gateway reset has not completed; state retained')
        return True
    finally:
        kernel.CloseHandle(handle)


def recover_session(root):
    with ProjectLock(root) as acquired:
        if not acquired:
            raise RuntimeError('Runtime operation is still active; recovery refused')
        snapshot = recovery_snapshot(root)
        graceful, remaining = [], []
        for record in snapshot['gateways']:
            state = read_json(safe_state_path(root, record['state']))
            if controller_live(root, record) and state.get('status') != 'shutdown-unconfirmed':
                request_powerdown(root, record)
                graceful.append(record)
            else:
                remaining.append(record)
        remaining.extend(wait_records(root, graceful))
        reset = False
        for record in remaining:
            reset = reset_gateway(root, record) or reset
        deadline = time.monotonic() + 10
        for record in snapshot['gateways']:
            controller = record.get('controller')
            while same_identity(controller, process_identity(controller['pid'])):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Gateway controller is finishing recovery; retry diagnostics')
                time.sleep(.2)
        current = recovery_snapshot(root)
        if current['gateways']:
            raise RuntimeError('Gateway remains active; recovery not completed')
        operation = uuid.uuid4().hex
        for record in current['work']:
            path = safe_state_path(root, record['state'])
            with work_session_lock(path.parent):
                state = read_json(path)
                from file_snapshot import locked_read
                with locked_read(path) as source:
                    original = source.read()
                with path.with_name('state-before-recovery-' + operation + '.json').open('xb') as backup:
                    backup.write(original)
                    backup.flush()
                    os.fsync(backup.fileno())
                state.update(status='interrupted', recovered_utc=utc_now(), recovery_id=operation)
                atomic_json(path, state)
        result = {'schema': 1, 'ok': True, 'code': 'session_recovered', 'gateway_reset': reset,
                  'windows_terminated': False, 'work_sessions': len(current['work']),
                  'message': 'Прерванный сеанс восстановлен. Диск Windows и подключения сохранены. Можно запускать Windows заново.'}
        atomic_json(root / 'state' / ('recovery-' + operation + '.json'), result)
        return result


def record_live(root, record):
    path = safe_state_path(root, record['state'])
    if 'session_nonce' in record:
        state=read_json(path)
        refresh_work_record(root,record,state)
        identity=record['process']
        live=same_identity(identity,process_identity(identity['pid']))
        if not live and state.get('status') == 'restarting':
            if not reset_proof_valid(root,state.get('reset_proof'),identity,record['generation']):
                raise RuntimeError('Windows reboot gap lacks verified guest reset')
            if not same_identity(record['controller'],process_identity(record['controller']['pid'])):
                raise RuntimeError('Windows controller disappeared in reboot gap; gateway retained')
            return True
        if not live and state.get('status') in ('starting','running','paused'):
            if not same_identity(record['controller'],process_identity(record['controller']['pid'])):
                raise RuntimeError('Windows controller disappeared while settling its exit; gateway retained')
            return True
        return live
    expected = record['process']
    actual = process_identity(expected['pid'])
    if not same_identity(expected, actual):
        return False
    state = read_json(path)
    if not state_matches(root, path, state, expected):
        raise RuntimeError('Guest state no longer matches the recorded process identity')
    if ('nonce' in record and record['nonce'] != state.get('nonce')) or ('controller' in record and not same_identity(record['controller'], state.get('controller'))):
        raise RuntimeError('Guest session identity changed')
    role = state['guest'].split('-', 1)[0]
    if state['guest'] != role + '-boot' or state.get('network') != NETWORKS[role]:
        raise RuntimeError('Recorded guest is not a normal runtime guest')
    return True


def request_powerdown(root, record):
    path = safe_state_path(root, record['state'])
    if not record_live(root, record):
        return False
    if not controller_live(root, record):
        raise RuntimeError('Guest controller is unavailable; running guest retained')
    if 'session_nonce' in record:
        with work_session_lock(path.parent):
            atomic_json(path.with_name('stop-session.json'), {'session_nonce':record['session_nonce'], 'controller':record['controller']})
    atomic_json(path.parent / ('control-runtime-' + uuid.uuid4().hex + '.json'), {'action': 'powerdown'})
    return True


def wait_records(root, records):
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT
    while True:
        live = [record for record in records if record_live(root, record)]
        if not live or time.monotonic() >= deadline:
            return live
        time.sleep(.5)


def wait_exit_state(root, record):
    path = safe_state_path(root, record['state'])
    deadline = time.monotonic() + 5
    while True:
        state = read_json(path)
        if not state_matches(root, path, state, record['process']):
            raise RuntimeError('Exit state does not match the owned Windows process')
        if state.get('status') == 'exited':
            return state
        if time.monotonic() >= deadline:
            raise RuntimeError('Windows exit was not confirmed by its controller; gateway retained')
        time.sleep(.2)


def confirm_windows_shutdown(root, work, allow_reset_stop=False):
    if same_identity(work['process'], process_identity(work['process']['pid'])):
        raise RuntimeError('Windows process is still running; gateway retained')
    state = wait_exit_state(root, work)
    if ((state.get('stop_requested') is True or allow_reset_stop) and valid_work_session(root,state)
            and state.get('status') == 'exited'
            and type(state.get('exit_code')) is int and state['exit_code'] == 0
            and reset_proof_valid(root,state.get('reset_proof'),work['process'],work.get('generation'))):
        return
    if state.get('exit_code') != 0 or state.get('error'):
        raise RuntimeError('Windows did not exit normally; gateway retained for inspection')
    confirmed = False
    try:
        path = safe_state_path(root, work['state'])
        ring = path.with_name('qmp-events.json')
        if ring.exists():
            events = read_json(ring)
            if not isinstance(events, list) or len(events) > 64:
                raise ValueError('Invalid QMP event ring')
        else:
            with path.with_name('qmp-events.jsonl').open(encoding='utf-8') as stream:
                events = deque((json.loads(line) for line in stream), maxlen=64)
        for event in events:
            if (event.get('event') == 'SHUTDOWN' and event.get('data', {}).get('reason') == 'guest-shutdown'
                    and event.get('data', {}).get('guest') is True
                    and ('session_nonce' not in work or event.get('generation') == work['generation'])):
                confirmed = True
    except (OSError, ValueError, AttributeError):
        pass
    if not confirmed:
        raise RuntimeError('Normal Windows shutdown was not confirmed by QMP; gateway retained')


def finish_windows(root, report):
    confirm_windows_shutdown(root, report['guests']['work'])
    gateway = report['guests']['gateway']
    request_powerdown(root, gateway)
    if wait_records(root, [gateway]):
        raise RuntimeError('Gateway ignored graceful shutdown; running guest retained. Shut it down inside its window')


def stop_records(root, records):
    for role in ('work', 'gateway'):
        guests = records.get(role, [])
        for record in guests:
            request_powerdown(root, record)
        if wait_records(root, guests):
            raise RuntimeError(role + ' ignored graceful shutdown; running guests retained. Shut it down inside its window')
        if role == 'work':
            for record in guests:
                confirm_windows_shutdown(root, record, allow_reset_stop=True)


def check_acceptance(root):
    try:
        accepted = read_json(root / 'state/gateway-network-accepted.json')
        valid = (accepted.get('native_tests_passed') is True
                 and type(accepted.get('wan_bypass_packets')) is int
                 and accepted['wan_bypass_packets'] == 0)
    except (OSError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise RuntimeError('Gateway network acceptance is missing or failed; finish the native network tests first')


class ProjectLock:
    def __init__(self, root):
        self.path = root / 'state/runtime.lock'
        self.stack = None

    def __enter__(self):
        from profile_store import _pinned_directory, _exclusive_file, StoreBusy, StoreError
        self.stack = ExitStack()
        try:
            self.stack.enter_context(_pinned_directory(self.path.parent))
            self.stack.enter_context(_exclusive_file(self.path))
            return True
        except StoreBusy:
            self.stack.close()
            return False
        except (StoreError, OSError) as error:
            self.stack.close()
            raise RuntimeError('Cannot safely acquire runtime operation lock') from error

    def __exit__(self, *unused):
        if self.stack:
            self.stack.close()


def launch_guest(root, role, report, save, cancelled, startup_deadline=None):
    from runtime_logs import start_capped_drain, CappedWriter
    deadline = time.monotonic() + START_TIMEOUT
    if startup_deadline is not None:
        deadline = min(deadline, startup_deadline)
    if time.monotonic() >= deadline:
        raise RuntimeError('Runtime startup budget expired; no additional guest was started')
    directory = Path(report['receipt']).parent
    stdout_path, stderr_path = directory / (role + '-controller.stdout.log'), directory / (role + '-controller.stderr.log')
    # Validate/open both owned log sinks before launching a child that needs draining.
    with ExitStack() as pending:
        stdout_writer = pending.enter_context(CappedWriter(stdout_path))
        stderr_writer = pending.enter_context(CappedWriter(stderr_path))
        controller = subprocess.Popen(controller_command(root, role, report.get('work_test_media', False)), cwd=root, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        pending.pop_all()
    _CONTROLLER_WRITERS.extend((stdout_writer,stderr_writer))
    for source, destination in ((controller.stdout, stdout_writer), (controller.stderr, stderr_writer)):
        _CONTROLLER_DRAINS.append(start_capped_drain(source, destination))
    controller_identity = process_identity(controller.pid)
    report.setdefault('controllers', {})[role] = {'pid': controller.pid, 'identity': controller_identity,
                                                'stdout': str(stdout_path), 'stderr': str(stderr_path)}
    save()
    while time.monotonic() < deadline:
        for line in stdout_path.read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                event = json.loads(line)
                if 'state' not in event:
                    continue
                path = safe_state_path(root, event['state'])
                state = read_json(path)
                identity = process_identity(state['pid'])
                record = state_record(path, state, identity)
                if (identity and state_matches(root, path, state, identity)
                        and state.get('guest') == role + '-boot' and state.get('network') == NETWORKS[role]
                        and state.get('status') == 'running'
                        and same_identity(controller_identity or {}, state.get('controller'))
                        and (role != 'gateway' or gateway_ready(root, record))):
                    return record
                if state.get('status') == 'exited':
                    raise RuntimeError(role + ' controller reported an early exit; see ' + str(stderr_path))
            except (ValueError, OSError, KeyError):
                continue
        if controller.poll() is not None:
            raise RuntimeError(role + ' controller exited before startup; see ' + str(stderr_path))
        time.sleep(.2)
    raise RuntimeError(role + ' startup timed out; any running guest is retained. See ' + str(directory))


def start_pair(root, report, save, cancelled):
    deadline = time.monotonic() + START_TIMEOUT
    guests = discover_guests(root)
    if 'work' in guests and 'gateway' not in guests:
        raise RuntimeError('Windows is already running without its normal gateway; close Windows normally first')
    if 'gateway' in guests and not gateway_ready(root, guests['gateway']):
        raise RuntimeError('Existing gateway is not a freshly verified normal session; stop it normally before starting')
    report['guests'].update(guests)
    save()
    for role in ('gateway', 'work'):
        if time.monotonic() >= deadline:
            raise RuntimeError('Runtime startup budget expired; existing guests retained')
        if cancelled():
            raise RuntimeError('Stop requested during startup; any running guests retained for graceful shutdown')
        if role == 'work' and not gateway_ready(root, report['guests']['gateway']):
            raise RuntimeError('Gateway connection is not freshly verified; Windows was not started')
        if role not in report['guests']:
            # Recheck immediately before each new disk open, including adoption races.
            current = discover_guests(root)
            if (role == 'work' and ('gateway' not in current
                    or not same_identity(report['guests']['gateway']['process'], current['gateway']['process']))):
                raise RuntimeError('Gateway stopped or changed during startup; Windows was not started')
            if role in current:
                report['guests'][role] = current[role]
            else:
                report['guests'][role] = launch_guest(root, role, report, save, cancelled, startup_deadline=deadline)
            save()
    if (not controller_live(root, report['guests']['work'])
            or read_json(safe_state_path(root, report['guests']['work']['state'])).get('status') != 'running'):
        raise RuntimeError('Windows controller is unavailable; existing guests retained')


def active_receipt(root):
    pointer = read_json(root / 'state/runtime-active.json')
    path = safe_receipt_path(root, pointer['receipt'])
    report = read_json(path)
    if report.get('schema') != 1 or not same_path(report.get('project', ''), root):
        raise RuntimeError('Active runtime receipt belongs to another project')
    return path, report


def join_existing(root):
    deadline = time.monotonic() + START_TIMEOUT + 10
    while time.monotonic() < deadline:
        try:
            path, report = active_receipt(root)
            if report['phase'] in ('ready','restarting'):
                if (all(record_live(root, report['guests'][role]) for role in ('gateway', 'work'))
                        and controller_live(root, report['guests']['work'])
                        and gateway_ready(root, report['guests']['gateway'])):
                    return path
                raise RuntimeError('The existing runtime is shutting down; inspect its receipt before retrying')
            if report['phase'] != 'starting':
                raise RuntimeError(report.get('message', 'Existing runtime is not ready') + '; see ' + str(path))
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.2)
    raise RuntimeError('Another launcher holds the project lock but did not become ready; guests were not changed')


def require_runtime(root):
    if os.name != 'nt':
        raise RuntimeError('Windows is required')
    if not same_path(sys.executable, root / 'runtime/python-3.12.14/python.exe'):
        raise RuntimeError('Use this project\'s bundled Python runtime')


def run_start(root, receipt, work_test_media=False):
    receipt.parent.mkdir(parents=True, exist_ok=True)
    report = {'schema': 1, 'project': str(root), 'receipt': str(receipt),
              'phase': 'starting', 'started_utc': utc_now(), 'guests': {},
              'work_test_media': bool(work_test_media), 'normal_runtime': not work_test_media}
    def save():
        writers = [report['supervisor']] if report.get('supervisor') else []
        writers.extend(value['identity'] for value in report.get('controllers', {}).values() if value.get('identity'))
        for record in report['guests'].values():
            writers.extend(record[key] for key in ('process', 'controller') if record.get(key))
        report['writers'] = writers
        report['writers_complete'] = (bool(report.get('supervisor'))
            and all(value.get('identity') for value in report.get('controllers', {}).values()))
        report['observed_utc'] = utc_now()
        atomic_json(receipt, report)
    save()
    try:
        require_runtime(root)
        report['supervisor'] = process_identity(os.getpid())
        with ProjectLock(root) as acquired:
            if not acquired:
                existing = join_existing(root)
                report.update(phase='ready', existing_receipt=str(existing), message='Workspace is already running')
                save()
                return 0
            atomic_json(root / 'state/runtime-active.json', {'receipt': str(receipt)})
            if report['normal_runtime']:
                report['log_budget'] = prepare_log_budget(root)
                save()
            for path in [engine_path(root), Path(controller_command(root, 'work')[0]),
                         root / 'scripts/control_guest_install.py', root / 'scripts/gateway_controller.py',
                         root / DISKS['work']]:
                if not path.is_file():
                    raise RuntimeError('Required runtime file is missing: ' + str(path))
            start_pair(root, report, save, lambda: (receipt.parent / 'stop-request.json').exists())
            if (receipt.parent / 'stop-request.json').exists():
                raise RuntimeError('Stop requested during startup; any running guests retained for graceful shutdown')
            report.update(phase='ready', message='Gateway and Windows are running')
            save()
            last_work_state=None
            while record_live(root, report['guests']['work']):
                if not record_live(root, report['guests']['gateway']):
                    raise RuntimeError('Gateway exited while Windows remains active; Windows retained')
                work_state=read_json(safe_state_path(root,report['guests']['work']['state']))
                observed=(work_state.get('status'),work_state.get('generation'),report['guests']['work']['process']['created_filetime'])
                if observed != last_work_state:
                    report.update(phase='restarting' if work_state.get('status') == 'restarting' else 'ready',
                                  message='Windows is restarting' if work_state.get('status') == 'restarting' else 'Gateway and Windows are running')
                    save()
                    last_work_state=observed
                time.sleep(.5)
            report.update(phase='stopping', message='Windows exited; checking guest shutdown before stopping gateway')
            save()
            finish_windows(root, report)
            report.update(phase='stopped', message='Windows and gateway have stopped')
            save()
            return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        report.update(phase='error', message=str(error), retained_guests=report['guests'])
        save()
        return 1
    finally:
        # A retained controller must keep its PIPE reader after an error receipt.
        # The background supervisor waits for EOF; neither child nor pipe is killed.
        for drain in _CONTROLLER_DRAINS:
            drain.close()
        _CONTROLLER_DRAINS.clear()
        for writer in _CONTROLLER_WRITERS:
            writer.close()
        _CONTROLLER_WRITERS.clear()


def normal_log_descriptors(root):
    """Only new explicit normal-runtime metadata grants cleanup authority."""
    descriptors = []
    for path in (root / 'logs').glob('*/state.json'):
        try:
            path = safe_state_path(root, path)
            state = read_json(path)
            if state.get('normal_runtime') is not True:
                continue
            identity = state.get('process')
            valid = (state_matches(root, path, state, identity)
                     and valid_controller(root, state.get('controller')))
            writers = [identity, state['controller']] if valid else []
            descriptors.append({'directory': path.parent.name, 'normal': True,
                                'exited': valid and state.get('status') in ('exited', 'interrupted'), 'processes': writers})
        except (OSError, ValueError, RuntimeError, TypeError, KeyError):
            continue
    for path in (root / 'logs').glob('runtime-launch-*/receipt.json'):
        try:
            path = safe_receipt_path(root, path)
            report = read_json(path)
            if (report.get('normal_runtime') is not True or report.get('schema') != 1
                    or not same_path(report.get('project', ''), root)):
                continue
            writers = report.get('writers', [])
            valid = (isinstance(writers, list) and all(valid_controller(root, writer)
                     or (isinstance(writer, dict) and type(writer.get('pid')) is int and writer['pid'] > 0
                         and type(writer.get('created_filetime')) is int and writer['created_filetime'] > 0
                         and same_path(writer.get('image', ''), engine_path(root))) for writer in writers))
            descriptors.append({'directory': path.parent.name, 'normal': True,
                'exited': bool(valid and report.get('writers_complete') is True and report.get('phase') in ('stopped', 'error')),
                'processes': writers if valid else []})
        except (OSError, ValueError, RuntimeError, TypeError, KeyError):
            continue
    return descriptors


def prepare_log_budget(root):
    from runtime_logs import prune_logs, MAX_LOG_BYTES, NORMAL_PAIR_RESERVE
    from profile_store import _pinned_directory
    def gone(identity):
        return not same_identity(identity, process_identity(identity['pid']))
    report = prune_logs(root, normal_log_descriptors(root), process_gone=gone,
                        pin_directory=lambda path: _pinned_directory(path, create=False),
                        budget_bytes=MAX_LOG_BYTES-NORMAL_PAIR_RESERVE)
    if not report['budget_met']:
        raise RuntimeError('Normal runtime logs reached the 50 MiB limit; protected or live logs leave insufficient space for a new session')
    report['reserved_bytes'] = NORMAL_PAIR_RESERVE
    return report


def owned_records(root):
    records, seen = {'work': [], 'gateway': []}, set()
    processes = engine_processes(root)
    for path in (root / 'logs').glob('runtime-launch-*/receipt.json'):
        try:
            safe_receipt_path(root, path)
            report = read_json(path)
            if report.get('schema') != 1 or not same_path(report.get('project', ''), root):
                continue
            guests = report.get('guests', {})
            # An old completed pair must not become a prerequisite for today's
            # shutdown. A dead Windows record paired with a still-live gateway
            # remains relevant: its normal QMP shutdown still needs proof.
            if not any(same_identity(record.get('process'), processes.get(record.get('process', {}).get('pid')))
                       for record in guests.values() if isinstance(record, dict)):
                continue
            for role, record in guests.items():
                if role not in records:
                    continue
                if role == 'gateway' and not same_identity(record.get('process'), processes.get(record.get('process', {}).get('pid'))):
                    continue
                state_path = safe_state_path(root, record['state'])
                state = read_json(state_path)
                if 'session_nonce' in record:
                    refresh_work_record(root,record,state,allow_terminal_reset=True)
                identity = record['process']
                if state.get('guest') != role + '-boot' or not state_matches(root, state_path, state, identity):
                    raise RuntimeError('A runtime receipt has inconsistent guest ownership: ' + str(path))
                key = (identity['pid'], identity['created_filetime'])
                if key not in seen:
                    records[role].append(record)
                    seen.add(key)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return records


def run_stop(root):
    require_runtime(root)
    try:
        path, report = active_receipt(root)
    except (OSError, ValueError, KeyError):
        path, report = None, {}
    if path:
        atomic_json(path.parent / 'stop-request.json', {'requested_utc': utc_now()})
        deadline = time.monotonic() + START_TIMEOUT + 5
        while report.get('phase') == 'starting' and time.monotonic() < deadline:
            supervisor = report.get('supervisor')
            if supervisor and not same_identity(supervisor, process_identity(supervisor['pid'])):
                break
            time.sleep(.2)
            report = read_json(path)
        if report.get('phase') == 'starting':
            raise RuntimeError('Startup has not settled; running guests retained. Retry Stop after checking ' + str(path))
    records = owned_records(root)
    for role, record in discover_guests(root).items():
        if not any(same_identity(record['process'], existing['process']) for existing in records.get(role, [])):
            records.setdefault(role, []).append(record)
    processes = engine_processes(root)
    known = {record['process']['pid']: record['process'] for group in records.values() for record in group}
    if any(not same_identity(known.get(pid, {}), identity) for pid, identity in processes.items()):
        raise RuntimeError('Untracked live project QEMU process; guests retained')
    stop_records(root, records)
    print(json.dumps({'phase': 'stopped', 'message': 'All recorded runtime guests have stopped; no process was forced to exit'}))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--plan', action='store_true', help='Print fixed commands; no files or processes are changed')
    mode.add_argument('--stop', action='store_true', help='Request graceful Windows shutdown, then gateway shutdown')
    parser.add_argument('--receipt', help='Startup receipt under logs/runtime-launch-*/receipt.json')
    parser.add_argument('--work-test-media', action='store_true', help='Attach the verified public diagnostic USB for acceptance only')
    args = parser.parse_args(argv)
    try:
        if not Path(args.project).is_absolute():
            raise RuntimeError('An absolute project directory is required')
        root = Path(args.project).resolve(strict=True)
        if args.plan:
            print(json.dumps({'project': str(root), 'start_order': ['gateway', 'work'],
                              'commands': {role: controller_command(root, role, args.work_test_media) for role in NETWORKS},
                              'shutdown': 'ACPI powerdown only; retain guests on timeout'}, ensure_ascii=True))
            return 0
        if args.stop:
            return run_stop(root)
        default = root / 'logs' / ('runtime-launch-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex[:8]) / 'receipt.json'
        receipt = safe_receipt_path(root, args.receipt or default)
        return run_start(root, receipt, args.work_test_media)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(json.dumps({'phase': 'error', 'message': str(error)}, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
