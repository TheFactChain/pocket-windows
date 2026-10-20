"""Supervise the two owned guests; never terminate a guest or send QMP quit."""
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


NETWORKS = {'gateway': 'gateway', 'work': 'isolated-lan'}
DISKS = {'gateway': 'images/gateway-empty.qcow2', 'work': 'images/workvm-empty.qcow2'}
START_TIMEOUT = 75
SHUTDOWN_TIMEOUT = 120
FILETIME_EPOCH = 11644473600


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def same_path(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def atomic_json(path, value):
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
    # The existing controller rewrites state.json in place.
    for attempt in range(3):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(.05)


def engine_path(root):
    return root / 'runtime/qemu-11.1.0/qemu-system-x86_64.exe'


def controller_command(root, role, work_test_media=False):
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


def process_identity(pid):
    """Creation FILETIME prevents a recycled PID from becoming an owned guest."""
    if type(pid) is not int or pid <= 0:
        raise RuntimeError('Invalid process identity')
    kernel = kernel_api()
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        if ctypes.get_last_error() == 87:  # Process no longer exists.
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
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
    finally:
        kernel.CloseHandle(handle)


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
    return (actual is not None and expected.get('pid') == actual.get('pid')
            and expected.get('created_filetime') == actual.get('created_filetime')
            and same_path(expected.get('image', ''), actual.get('image', '')))


def safe_state_path(root, path):
    path = Path(path).resolve()
    if (path.name != 'state.json' or not path.parent.name.startswith('guest-')
            or path.parent.parent != (root / 'logs').resolve()):
        raise RuntimeError('Refusing a guest state path outside this project log directory')
    return path


def safe_receipt_path(root, path):
    path = Path(path).resolve()
    if (path.name != 'receipt.json' or not path.parent.name.startswith('runtime-launch-')
            or path.parent.parent != (root / 'logs').resolve()):
        raise RuntimeError('Refusing a runtime receipt path outside this project log directory')
    return path


def state_matches(root, path, state, identity):
    try:
        safe_state_path(root, path)
        role = state['guest'].split('-', 1)[0]
        started = datetime.fromisoformat(state['started_utc'])
        created = identity['created_filetime'] / 10_000_000 - FILETIME_EPOCH
        return (role in DISKS and type(state['pid']) is int and state['pid'] == identity['pid']
                and same_path(identity['image'], engine_path(root)) and started.tzinfo is not None
                and -.25 <= started.timestamp() - created <= 5
                and any('file=' + DISKS[role] in argument.split(',')
                        for argument in state['arguments']))
    except (KeyError, TypeError, ValueError, RuntimeError, OverflowError):
        return False


def discover_guests(root):
    processes = engine_processes(root)
    found, tracked = {}, set()
    for path in (root / 'logs').glob('guest-*/state.json'):
        try:
            state = read_json(path)
            identity = processes.get(state.get('pid'))
            if not identity or not state_matches(root, path, state, identity):
                continue
            tracked.add(identity['pid'])
            role = state['guest'].split('-', 1)[0]
            if role in found:
                raise RuntimeError('Multiple live ' + role + ' guests reference the project disk')
            if (state['guest'] != role + '-boot' or state.get('network') != NETWORKS[role]
                    or state.get('status') != 'running'):
                raise RuntimeError('The live ' + role + ' guest is not a normal boot with the accepted network; close it normally first')
            found[role] = {'state': str(path.resolve()), 'process': identity}
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    if set(processes) - tracked:
        raise RuntimeError('Untracked live project QEMU process; no disk will be opened. Inspect its controller log first')
    return found


def record_live(root, record):
    path = safe_state_path(root, record['state'])
    expected = record['process']
    actual = process_identity(expected['pid'])
    if not same_identity(expected, actual):
        return False
    state = read_json(path)
    if not state_matches(root, path, state, expected):
        raise RuntimeError('Guest state no longer matches the recorded process identity')
    role = state['guest'].split('-', 1)[0]
    if state['guest'] != role + '-boot' or state.get('network') != NETWORKS[role]:
        raise RuntimeError('Recorded guest is not a normal runtime guest')
    return True


def request_powerdown(root, record):
    path = safe_state_path(root, record['state'])
    if not record_live(root, record):
        return False
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


def finish_windows(root, report):
    work = report['guests']['work']
    state = wait_exit_state(root, work)
    if state.get('exit_code') != 0 or state.get('error'):
        raise RuntimeError('Windows did not exit normally; gateway retained for inspection')
    confirmed = False
    try:
        with safe_state_path(root, work['state']).with_name('qmp-events.jsonl').open(encoding='utf-8') as events:
            for line in events:
                event = json.loads(line)
                if event.get('event') == 'SHUTDOWN' and event.get('data', {}).get('reason') == 'guest-shutdown':
                    confirmed = True
    except (OSError, ValueError, AttributeError):
        pass
    if not confirmed:
        raise RuntimeError('Normal Windows shutdown was not confirmed by QMP; gateway retained')
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
        self.stream = None

    def __enter__(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.stream.write(b'\0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            self.stream.close()
            self.stream = None
            return False

    def __exit__(self, *unused):
        if self.stream:
            import msvcrt
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()


def launch_guest(root, role, report, save, cancelled):
    directory = Path(report['receipt']).parent
    stdout_path, stderr_path = directory / (role + '-controller.stdout.log'), directory / (role + '-controller.stderr.log')
    with stdout_path.open('w', encoding='utf-8') as stdout, stderr_path.open('w', encoding='utf-8') as stderr:
        controller = subprocess.Popen(controller_command(root, role, report.get('work_test_media', False)), cwd=root, stdin=subprocess.DEVNULL,
                                      stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
    report.setdefault('controllers', {})[role] = {'pid': controller.pid, 'stdout': str(stdout_path), 'stderr': str(stderr_path)}
    save()
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        for line in stdout_path.read_text(encoding='utf-8', errors='replace').splitlines():
            try:
                event = json.loads(line)
                if 'state' not in event:
                    continue
                path = safe_state_path(root, event['state'])
                state = read_json(path)
                identity = process_identity(state['pid'])
                if (identity and state_matches(root, path, state, identity)
                        and state.get('guest') == role + '-boot' and state.get('network') == NETWORKS[role]
                        and state.get('status') == 'running'):
                    return {'state': str(path), 'process': identity}
                if state.get('status') == 'exited':
                    raise RuntimeError(role + ' controller reported an early exit; see ' + str(stderr_path))
            except (ValueError, OSError, KeyError):
                continue
        if controller.poll() is not None:
            raise RuntimeError(role + ' controller exited before startup; see ' + str(stderr_path))
        time.sleep(.2)
    raise RuntimeError(role + ' startup timed out; any running guest is retained. See ' + str(directory))


def start_pair(root, report, save, cancelled):
    guests = discover_guests(root)
    if 'work' in guests and 'gateway' not in guests:
        raise RuntimeError('Windows is already running without its normal gateway; close Windows normally first')
    report['guests'].update(guests)
    save()
    for role in ('gateway', 'work'):
        if cancelled():
            raise RuntimeError('Stop requested during startup; any running guests retained for graceful shutdown')
        if role not in report['guests']:
            # Recheck immediately before each new disk open, including adoption races.
            current = discover_guests(root)
            if (role == 'work' and ('gateway' not in current
                    or not same_identity(report['guests']['gateway']['process'], current['gateway']['process']))):
                raise RuntimeError('Gateway stopped or changed during startup; Windows was not started')
            if role in current:
                report['guests'][role] = current[role]
            else:
                report['guests'][role] = launch_guest(root, role, report, save, cancelled)
            save()


def active_receipt(root):
    pointer = read_json(root / 'state/runtime-active.json')
    path = safe_receipt_path(root, pointer['receipt'])
    report = read_json(path)
    if report.get('schema') != 1 or not same_path(report.get('project', ''), root):
        raise RuntimeError('Active runtime receipt belongs to another project')
    return path, report


def join_existing(root):
    deadline = time.monotonic() + START_TIMEOUT * 2 + 10
    while time.monotonic() < deadline:
        try:
            path, report = active_receipt(root)
            if report['phase'] == 'ready':
                if all(record_live(root, report['guests'][role]) for role in ('gateway', 'work')):
                    return path
                raise RuntimeError('The existing runtime is shutting down; inspect its receipt before retrying')
            if report['phase'] != 'starting':
                raise RuntimeError(report.get('message', 'Existing runtime is not ready') + '; see ' + str(path))
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.2)
    raise RuntimeError('Another launcher holds the project lock but did not become ready; guests were not changed')


def require_runtime(root):
    if os.name != 'nt' or not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError('Administrator approval is required; run Start-Runtime.ps1 or Stop-Runtime.ps1')
    if not same_path(sys.executable, root / 'runtime/python-3.12.14/python.exe'):
        raise RuntimeError('Use this project\'s bundled Python runtime')


def run_start(root, receipt, work_test_media=False):
    receipt.parent.mkdir(parents=True, exist_ok=True)
    report = {'schema': 1, 'project': str(root), 'receipt': str(receipt),
              'phase': 'starting', 'started_utc': utc_now(), 'guests': {},
              'work_test_media': bool(work_test_media)}
    def save():
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
            check_acceptance(root)
            for path in [engine_path(root), Path(controller_command(root, 'work')[0]),
                         root / 'scripts/control_guest_install.py', *(root / value for value in DISKS.values())]:
                if not path.is_file():
                    raise RuntimeError('Required runtime file is missing: ' + str(path))
            start_pair(root, report, save, lambda: (receipt.parent / 'stop-request.json').exists())
            if (receipt.parent / 'stop-request.json').exists():
                raise RuntimeError('Stop requested during startup; any running guests retained for graceful shutdown')
            report.update(phase='ready', message='Gateway and Windows are running')
            save()
            while record_live(root, report['guests']['work']):
                if not record_live(root, report['guests']['gateway']):
                    raise RuntimeError('Gateway exited while Windows remains active; Windows retained')
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


def owned_records(root):
    records, seen = {'work': [], 'gateway': []}, set()
    for path in (root / 'logs').glob('runtime-launch-*/receipt.json'):
        try:
            safe_receipt_path(root, path)
            report = read_json(path)
            if report.get('schema') != 1 or not same_path(report.get('project', ''), root):
                continue
            for role, record in report.get('guests', {}).items():
                if role not in records:
                    continue
                state_path = safe_state_path(root, record['state'])
                state = read_json(state_path)
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
    stop_records(root, owned_records(root))
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
