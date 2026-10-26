"""Manual inventory of an owned overlay; offline by default, never generalizes or kills a VM."""
import argparse
import base64
from collections import deque
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import launch_runtime as runtime
import windows_hardware
import windows_guest_settings as windows_settings
from file_snapshot import locked_read, QEMU_IMG_SHA256
from profile_store import _plain, _pinned_directory
import reference_prep
from runtime_config import load_settings
from runtime_network_plan import arguments as network_arguments
from runtime_logs import CappedWriter, start_capped_drain

ENGINE_SHA256 = '63dc4e2f5fc131afe5ad2416c2d9627f26ae764eec3a03a86acb41eae71b2b2a'
SOURCE = 'images/workvm-empty.qcow2'
VARS = 'state/workvm-vars.fd'
CODE = 'runtime/qemu-11.1.0/share/edk2-x86_64-code.fd'
TOOL = 'runtime/qemu-11.1.0/qemu-img.exe'
PUBLIC_FILES = ('Collect-ReferenceInventory.ps1', 'Run-Inventory.cmd', 'README.md', 'handoff.json')


def identity(info):
    return {'device': info.st_dev, 'file_id': info.st_ino, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns,
            'created_ns': getattr(info, 'st_birthtime_ns', info.st_ctime_ns)}


def file_info(path):
    _plain(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Expected one plain regular file')
    return identity(info)


def guard_source(expected, opened, named):
    if expected != identity(opened) or expected != identity(named):
        raise ValueError('Reference input changed; preparation must be repeated')


def clean_header(header):
    if len(header) < 104 or header[:4] != b'QFI\xfb':
        raise ValueError('Invalid standalone qcow2 header')
    version = struct.unpack_from('>I', header, 4)[0]
    if (version not in (2, 3) or struct.unpack_from('>Q', header, 8)[0]
            or struct.unpack_from('>I', header, 16)[0]
            or struct.unpack_from('>I', header, 32)[0]
            or (version == 3 and struct.unpack_from('>Q', header, 72)[0])):
        raise ValueError('Reference source must be clean, unencrypted standalone qcow2')


def operation_path(root, operation):
    path = Path(operation)
    if not path.is_absolute():
        path = root / path
    # Do not resolve links before checking their lexical location.
    if path.parent != root / 'state' or not re.fullmatch(r'reference-[0-9a-f]{32}', path.name):
        raise ValueError('Invalid owned reference operation path')
    return path


def arguments(operation, reference_id, settings, gateway=False, template_nonce=None):
    relative = operation.relative_to(operation.parent.parent).as_posix()
    if not re.fullmatch(r'state/reference-[0-9a-f]{32}', relative) or not re.fullmatch('[0-9a-f]{32}', reference_id):
        raise ValueError('Invalid reference identity')
    if template_nonce is not None and (gateway or not re.fullmatch('[0-9a-f]{32}', template_nonce)):
        raise ValueError('Template preparation must remain offline with its own nonce')
    serial = 'WCMASTER-' + template_nonce if template_nonce else 'WCREFERENCE-' + reference_id
    handoff = 'template-handoff' if template_nonce else 'handoff'
    result = ['-name', 'WindowsClaude-Reference', '-machine', 'q35', '-accel', 'whpx',
            '-cpu', windows_hardware.CPU_MODEL, '-smp', str(settings['vcpus']), '-m', str(settings['work_memory_mib']),
            '-L', 'runtime/qemu-11.1.0/share', '-nic', 'none', '-display', 'sdl,gl=off',
            '-vga', 'std', '-serial', 'none',
            '-chardev', 'ringbuf,id=wcrefreturn,size=16777216', '-serial', 'chardev:wcrefreturn',
            '-parallel', 'none', '-monitor', 'none', '-qmp', 'stdio', '-S', '-no-reboot',
            '-drive', 'if=pflash,unit=0,format=raw,readonly=on,file=' + CODE,
            '-drive', 'if=pflash,unit=1,format=raw,file=' + relative + '/vars.fd',
            '-drive', 'if=ide,index=0,format=qcow2,file=' + relative + '/overlay.qcow2',
            '-device', 'qemu-xhci,id=wcrefusb',
            '-drive', 'if=none,id=wcrefhand,format=raw,readonly=on,file=fat:' + relative + '/' + handoff,
            '-device', 'usb-storage,drive=wcrefhand,bus=wcrefusb.0,removable=on',
            '-boot', 'order=c,strict=on']
    result += windows_hardware.arguments(serial)
    result += windows_settings.vga_arguments(windows_settings.DEFAULTS)
    if gateway:
        result += network_arguments('work', relative, lan_port=settings['lan_port'])
    return result


def require_gateway(root, gateway=None, reference=None):
    if gateway is None:
        guests = runtime.discover_guests(root)
        if set(guests) != {'gateway'}:
            raise RuntimeError('Exactly one production gateway and no other guests are required')
        gateway = guests['gateway']
    expected = {gateway['process']['pid']: gateway['process']}
    if reference is not None:
        expected[reference['pid']] = reference
    processes = runtime.engine_processes(root)
    if set(processes) != set(expected) or any(
            not runtime.same_identity(identity, processes[pid]) for pid, identity in expected.items()):
        raise RuntimeError('Unexpected or changed project QEMU process')
    if not runtime.gateway_ready(root, gateway):
        raise RuntimeError('The original production gateway is not freshly verified')
    return gateway


def inventory_timed_out(elapsed, received, gateway=False):
    return not gateway and not received and elapsed > 1800


def continue_guest(root, gateway, reference, command):
    filters = command('query-rx-filter')
    if gateway is None:
        if filters != []:
            raise RuntimeError('Unexpected reference network device')
    else:
        require_gateway(root, gateway, reference)
    command('cont')


def pin_inputs(stack, root, expected=None):
    paths = [SOURCE, VARS, CODE, TOOL, runtime.engine_path(root).relative_to(root).as_posix()]
    result, streams = {}, {}
    for relative in paths:
        path = root / relative
        stack.enter_context(_pinned_directory(path.parent, create=False))
        before = file_info(path)
        stream = stack.enter_context(locked_read(path))
        guard_source((expected or {}).get(relative, before), os.fstat(stream.fileno()), path.stat())
        result[relative] = before
        streams[relative] = stream
    for relative, digest in ((TOOL, QEMU_IMG_SHA256), (paths[-1], ENGINE_SHA256)):
        if hashlib.file_digest(streams[relative], 'sha256').hexdigest() != digest:
            raise ValueError('Reference engine verification failed')
    streams[SOURCE].seek(0)
    clean_header(streams[SOURCE].read(104))
    return result, streams


def prepare(root):
    with ExitStack() as stack:
        stack.enter_context(_pinned_directory(root / 'state', create=False))
        if not stack.enter_context(runtime.ProjectLock(root)) or runtime.engine_processes(root):
            raise RuntimeError('All project VMs must be off before reference preparation')
        sources, streams = pin_inputs(stack, root)
        operation_id = uuid.uuid4().hex
        operation = root / 'state' / ('reference-' + operation_id)
        operation.mkdir(exist_ok=False)
        stack.enter_context(_pinned_directory(operation, create=False))
        handoff = reference_prep.prepare_handoff(operation / 'handoff')
        streams[VARS].seek(0)
        with (operation / 'vars.fd').open('xb') as output:
            output.write(streams[VARS].read())
        result = subprocess.run([str(root / TOOL), 'create', '-f', 'qcow2', '-F', 'qcow2',
                                 '-b', '../../' + SOURCE, str(operation / 'overlay.qcow2')],
                                cwd=root, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode != 0:
            raise RuntimeError('Reference overlay creation failed; partial files retained')
        files = {name: hashlib.sha256((operation / 'handoff' / name).read_bytes()).hexdigest()
                 for name in PUBLIC_FILES}
        manifest = {'schema': 1, 'kind': 'reference-inventory', 'operation_id': operation_id,
                    'reference_id': handoff['reference_id'], 'sources': sources, 'public_files': files,
                    'owned_files': {name: file_info(operation / name) for name in ('vars.fd', 'overlay.qcow2')},
                    'prepared_utc': runtime.utc_now()}
        with (operation / 'work-lifecycle.lock').open('xb'):
            pass
        runtime.atomic_json(operation / 'operation.json', manifest)
        return operation


def read_operation(root, operation):
    operation = operation_path(root, operation)
    manifest = runtime.read_json(operation / 'operation.json')
    if (manifest.get('schema') != 1 or manifest.get('kind') != 'reference-inventory'
            or manifest.get('operation_id') != operation.name.removeprefix('reference-')
            or not re.fullmatch('[0-9a-f]{32}', str(manifest.get('reference_id')))
            or set(manifest.get('sources', {})) != {SOURCE, VARS, CODE, TOOL, runtime.engine_path(root).relative_to(root).as_posix()}
            or set(manifest.get('public_files', {})) != set(PUBLIC_FILES)
            or set(manifest.get('owned_files', {})) != {'vars.fd', 'overlay.qcow2'}):
        raise ValueError('Invalid reference operation manifest')
    for name, expected in manifest['owned_files'].items():
        actual = file_info(operation / name)
        if any(actual[key] != expected[key] for key in ('device', 'file_id')):
            raise ValueError('Reference writable file ownership changed')
    return operation, manifest


def reset_proven(events, generation, exit_code, process, native_identity):
    return (type(exit_code) is int and exit_code == 0 and not runtime.same_identity(process, native_identity)
            and any(e.get('event') == 'SHUTDOWN' and e.get('generation') == generation
                    and e.get('data', {}).get('guest') is True
                    and e.get('data', {}).get('reason') == 'guest-reset' for e in events))


def terminal_exit_kind(error, stage, events, generation, exit_code, process, native_identity, earlier_failure):
    if (earlier_failure or stage not in {'ringbuf-read', 'powerdown'}
            or not isinstance(error, (EOFError, OSError, queue.Empty))):
        return None
    if reset_proven(events, generation, exit_code, process, native_identity):
        return 'reset'
    if (type(exit_code) is int and exit_code == 0 and not runtime.same_identity(process, native_identity)
            and any(event.get('event') == 'SHUTDOWN' and event.get('generation') == generation
                    and event.get('data', {}).get('guest') is True
                    and event.get('data', {}).get('reason') == 'guest-shutdown' for event in events)):
        return 'shutdown'
    return None


def read_qmp_stream(stream, inbox, events, generation):
    try:
        for line in stream:
            try:
                message = json.loads(line)
                if 'event' in message:
                    name = message['event']
                    if name in {'SHUTDOWN', 'RESET', 'STOP', 'RESUME', 'POWERDOWN'}:
                        data = message.get('data', {})
                        safe = {'guest': data['guest']} if type(data.get('guest')) is bool else {}
                        if data.get('reason') in {'guest-reset', 'guest-shutdown', 'host-qmp-quit', 'host-ui'}:
                            safe['reason'] = data['reason']
                        events.append({'event': name, 'data': safe, 'generation': generation})
                else:
                    inbox.put(message)
            except ValueError:
                pass
    finally:
        inbox.put({'eof': True})


def valid_control(marker, state):
    if (type(marker) is not dict or set(marker) != {'action', 'operation_id', 'session_nonce', 'controller'}
            or marker['action'] != 'powerdown' or marker['operation_id'] != state['operation_id']
            or marker['session_nonce'] != state['session_nonce']
            or not runtime.same_identity(marker['controller'], state['controller'])):
        raise ValueError('Reference control ownership mismatch')
    return True


def stop(root, operation):
    operation = operation_path(root, operation)
    with _pinned_directory(operation, create=False), runtime.work_session_lock(operation):
        operation, manifest = read_operation(root, operation)
        state = runtime.read_json(operation / 'state.json')
        if (state.get('operation_id') != manifest['operation_id']
                or not runtime.same_identity(state.get('controller'), runtime.process_identity(state['controller']['pid']))):
            raise ValueError('Reference controller is not live')
        marker = {key: state[key] for key in ('operation_id', 'session_nonce', 'controller')}
        marker['action'] = 'powerdown'
        runtime.atomic_json(operation / 'control.json', marker)


class ReportFrame:
    """Discard bounded UART startup noise, then accept exactly one inventory frame."""
    def __init__(self, reference_id, template=False):
        self.reference_id = reference_id
        self.marker = b'WCMASTER' if template else b'WCREFERENCE'
        self.max_bytes = 65536 if template else reference_prep.MAX_REPORT_BYTES
        self.header = bytearray()
        self.payload = bytearray()
        self.length = None
        self.complete = False
        self.failed = False
        self.preamble_bytes = 0

    def feed(self, chunk):
        if self.failed:
            return False
        try:
            if self.complete and chunk:
                raise ValueError('Extra serial data')
            marker = self.marker
            index = 0
            while self.length is None and index < len(chunk):
                self.header.append(chunk[index])
                index += 1
                if len(self.header) <= len(marker) and not marker.startswith(self.header):
                    # Keep only a possible next marker prefix; raw startup bytes are never retained.
                    keep = int(self.header[-1] == marker[0])
                    self.preamble_bytes += len(self.header) - keep
                    self.header[:] = marker[:keep]
                    # One page is already generous for UART initialization; larger noise fails closed.
                    if self.preamble_bytes > 4096:
                        raise ValueError('Excessive serial preamble')
                    continue
                if len(self.header) > 96:
                    raise ValueError('Oversized serial header')
                if self.header[-1] != 10:
                    continue
                match = re.fullmatch(marker + rb' ([0-9a-f]{32}) ([1-9][0-9]{0,7})\n', self.header)
                if not match or match[1].decode('ascii') != self.reference_id:
                    raise ValueError('Invalid reference serial header')
                self.length = int(match[2])
                if self.length > self.max_bytes:
                    raise ValueError('Oversized inventory')
            if self.length is None:
                return False
            chunk = chunk[index:] if index else chunk
            if len(self.payload) + len(chunk) > self.length:
                raise ValueError('Extra serial data')
            self.payload.extend(chunk)
            self.complete = len(self.payload) == self.length
            return self.complete
        except ValueError:
            self.failed = True
            self.payload.clear()
            raise ValueError('Invalid reference serial frame') from None


def receive_report(path, reference_id, payload):
    # The temporary is bounded public inventory, never a serial log.
    if not 0 < len(payload) <= reference_prep.MAX_REPORT_BYTES:
        raise ValueError('Invalid reference inventory size')
    temporary = path.with_name('report-pending.json')
    with temporary.open('xb') as stream:
        stream.write(payload)
    reference_prep.read_report(temporary, expected_reference_id=reference_id)
    os.replace(temporary, path)
    return True


def receive_template_report(path, nonce, payload):
    import template_prep
    if not 0 < len(payload) <= template_prep.MAX_RECEIPT_BYTES:
        raise ValueError('Invalid template receipt size')
    if path.exists():
        raise FileExistsError('Template receipt already exists')
    temporary = path.with_suffix('.pending.json')
    with temporary.open('xb') as stream:
        stream.write(payload)
    data = template_prep.read_receipt(temporary, expected_nonce=nonce)
    os.rename(temporary, path)
    return data


def pin_template(stack, operation, operation_id):
    import template_prep
    frozen_path = operation / 'template-preparation.json'
    file_info(frozen_path)
    stack.enter_context(locked_read(frozen_path))
    frozen = reference_prep._json(frozen_path, 65536)
    names = set(template_prep.SOURCE_FILES) | {'Claude.msix', 'Firefox.msi', 'handoff.json'}
    if (type(frozen) is not dict or set(frozen) != {'schema', 'operation_id', 'nonce', 'files'}
            or type(frozen['schema']) is not int or frozen['schema'] != 1 or frozen['operation_id'] != operation_id
            or not re.fullmatch('[0-9a-f]{32}', str(frozen['nonce'])) or type(frozen['files']) is not dict
            or set(frozen['files']) != names):
        raise ValueError('Invalid frozen template preparation')
    directory = operation / 'template-handoff'
    stack.enter_context(_pinned_directory(directory, create=False))
    if set(p.name for p in directory.iterdir()) != names:
        raise ValueError('Unexpected template handoff content')
    for name, expected in frozen['files'].items():
        path = directory / name
        stream = stack.enter_context(locked_read(path))
        if (type(expected) is not dict or set(expected) != {'identity', 'sha256'}
                or file_info(path) != expected['identity'] or identity(os.fstat(stream.fileno())) != expected['identity']
                or hashlib.file_digest(stream, 'sha256').hexdigest() != expected['sha256']):
            raise ValueError('Frozen template payload changed')
    handoff = reference_prep._json(directory / 'handoff.json', 65536)
    if handoff.get('nonce') != frozen['nonce'] or handoff.get('expected_smbios_serial') != 'WCMASTER-' + frozen['nonce']:
        raise ValueError('Template handoff nonce changed')
    return frozen['nonce']


def template_completed(operation, state):
    import template_prep
    try:
        if state.get('template_received') is not True or state.get('template_phase') != 'apps_verified':
            return False
        generation = state['generation']
        if type(generation) is not int or not 1 <= generation <= 4:
            return False
        path = operation / ('template-receipt-' + str(generation) + '.json')
        file_info(path)
        receipt = template_prep.read_receipt(path, expected_nonce=state['template_nonce'])
        return receipt['phase'] == 'apps_verified'
    except (ValueError, OSError, KeyError, TypeError):
        return False


def record_failure(state, error, stage, qmp_command=None, qmp_error_class=None):
    stages = {'startup', 'gateway-check', 'greeting', 'control-read', 'powerdown', 'ringbuf-read', 'base64-decode',
              'frame-parse', 'report-validation', 'state-save', 'inventory-timeout', 'idle'}
    commands = {'qmp_capabilities', 'query-rx-filter', 'cont', 'system_powerdown', 'ringbuf-read'}
    error_types = {'ValueError', 'RuntimeError', 'OSError', 'PermissionError', 'FileNotFoundError',
                   'TimeoutError', 'Empty', 'EOFError', 'BrokenPipeError', 'Error', 'TypeError', 'AttributeError', 'KeyboardInterrupt', 'SystemExit'}
    classes = {'GenericError', 'CommandNotFound', 'DeviceNotActive', 'DeviceNotFound'}
    name = type(error).__name__
    state.setdefault('failure_diagnostic', {
        'stage': stage if stage in stages else 'unknown',
        'exception_type': name if name in error_types else 'Other',
        'qmp_command': qmp_command if qmp_command in commands else None,
        'qmp_error_class': qmp_error_class if qmp_error_class in classes else None})


def run(root, operation, gateway=False, template=False):
    with ExitStack() as stack:
        if gateway and template:
            raise ValueError('Template preparation must remain offline')
        operation = operation_path(root, operation)
        stack.enter_context(_pinned_directory(operation, create=False))
        if not stack.enter_context(runtime.ProjectLock(root)):
            raise RuntimeError('Another project operation is running')
        gateway_record = require_gateway(root) if gateway else None
        if not gateway and runtime.engine_processes(root):
            raise RuntimeError('All project VMs must be off before offline reference startup')
        operation, manifest = read_operation(root, operation)
        pin_inputs(stack, root, manifest['sources'])
        template_nonce = pin_template(stack, operation, manifest['operation_id']) if template else None
        for name, expected in manifest['owned_files'].items():
            if file_info(operation / name) != expected:
                raise ValueError('Prepared reference overlay or firmware changed')
        stack.enter_context(_pinned_directory(operation / 'handoff', create=False))
        if set(p.name for p in (operation / 'handoff').iterdir()) != set(PUBLIC_FILES):
            raise ValueError('Unexpected public handoff content')
        for name, digest in manifest['public_files'].items():
            path = operation / 'handoff' / name
            file_info(path)
            stream = stack.enter_context(locked_read(path))
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise ValueError('Reference handoff changed')
        # One run owns one session; only its proven cold restarts reuse mutable files.
        for name in ('state.json', 'control.json', 'reference-report.json', 'report-pending.json', 'template-receipt-1.json'):
            if (operation / name).exists():
                raise ValueError('Reference operation was already run; prepare a fresh operation')
        state = {'schema': 1, 'operation_id': manifest['operation_id'], 'reference_id': manifest['reference_id'],
                 'controller': runtime.process_identity(os.getpid()), 'session_nonce': uuid.uuid4().hex,
                 'generation': 0, 'status': 'starting', 'inventory_received': False,
                 'ready_for_generalize': False, 'stop_requested': False,
                 'network': 'gateway' if gateway else 'none', 'gateway': gateway_record,
                 'template_nonce': template_nonce, 'template_received': False}
        argv = arguments(operation, manifest['reference_id'], load_settings(root), gateway=gateway, template_nonce=template_nonce)
        env = runtime.qemu_environment(root)
        controls = queue.Queue()
        def stdin_controls():
            for line in sys.stdin:
                try:
                    if json.loads(line) == {'action': 'powerdown'}:
                        controls.put(True)
                except ValueError:
                    pass
        threading.Thread(target=stdin_controls, daemon=True).start()
        def save():
            runtime.atomic_json(operation / ('qmp-events-' + str(state['generation']) + '.json'), list(events))
            runtime.atomic_json(operation / 'state.json', state)
        failed = False
        frame = ReportFrame(template_nonce or manifest['reference_id'], template=template)
        for generation in range(1, 5):  # Initial boot plus at most three proven cold restarts.
            if state['stop_requested']:
                break
            if template:
                frame = ReportFrame(template_nonce, template=True)
            state.update(generation=generation, status='starting')
            events, inbox = deque(maxlen=64), queue.Queue(maxsize=256)
            writer = CappedWriter(operation / ('stderr-' + str(generation) + '.txt'))
            with runtime.work_session_lock(operation):
                marker = operation / 'control.json'
                if marker.exists():
                    valid_control(runtime.read_json(marker), state)
                    state['stop_requested'] = True
                    writer.close()
                    break
                if gateway_record is not None:
                    require_gateway(root, gateway_record)
                process = subprocess.Popen([str(runtime.engine_path(root)), *argv], cwd=root, env=env,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
            reader = threading.Thread(target=read_qmp_stream, args=(process.stdout, inbox, events, generation), daemon=True)
            drain = None
            sequence, capable = 0, False
            diagnostic = {'stage': 'startup', 'command': None, 'class': None}
            def command(name, parameters=None):
                nonlocal sequence
                sequence += 1
                diagnostic.update(command=name, **{'class': None})
                request = {'execute': name, 'id': sequence}
                if parameters:
                    request['arguments'] = parameters
                process.stdin.write(json.dumps(request) + '\n')
                process.stdin.flush()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    msg = inbox.get(timeout=max(.01, deadline - time.monotonic()))
                    if msg.get('eof') is True:
                        raise EOFError('QMP stream ended')
                    if msg.get('id') == sequence:
                        if 'error' in msg:
                            diagnostic['class'] = msg['error'].get('class')
                            raise RuntimeError('QMP command failed')
                        return msg.get('return')
                raise TimeoutError('QMP command timed out')
            begin = time.monotonic()
            try:
                reader.start()
                drain = start_capped_drain(process.stderr.buffer, writer)
                state['process'] = runtime.process_identity(process.pid)
                if not state['process']:
                    raise RuntimeError('Reference process identity unavailable')
                save()
                diagnostic['stage'] = 'greeting'
                greeting = inbox.get(timeout=15)
                if 'QMP' not in greeting:
                    raise RuntimeError('Missing reference QMP greeting')
                command('qmp_capabilities')
                capable = True
                diagnostic['stage'] = 'gateway-check' if gateway_record is not None else 'startup'
                continue_guest(root, gateway_record, state['process'], command)
                state['status'] = 'running'
                save()
            except BaseException as error:
                record_failure(state, error, diagnostic['stage'], diagnostic['command'], diagnostic['class'])
                failed = True
                state.update(status='failure-guest-retained', error='Reference startup/control failed')
            # Errors never release pins, close QMP, or hard-stop the guest.
            while process.poll() is None:
                try:
                    diagnostic.update(stage='control-read', command=None, **{'class': None})
                    marker = operation / 'control.json'
                    if marker.exists() and not state['stop_requested']:
                        try:
                            valid_control(runtime.read_json(marker), state)
                            state['stop_requested'] = True
                        except (ValueError, OSError):
                            state['invalid_control'] = True
                            failed = True
                            save()
                    if not controls.empty():
                        controls.get_nowait()
                        state['stop_requested'] = True
                    if state['stop_requested'] and capable and not state.get('powerdown_sent'):
                        diagnostic['stage'] = 'powerdown'
                        command('system_powerdown')
                        state['powerdown_sent'] = True
                        diagnostic['stage'] = 'state-save'
                        save()
                    if capable:
                        diagnostic['stage'] = 'ringbuf-read'
                        encoded = command('ringbuf-read', {'device': 'wcrefreturn', 'size': 65536, 'format': 'base64'})
                        diagnostic['stage'] = 'base64-decode'
                        chunk = base64.b64decode(encoded, validate=True)
                        diagnostic['stage'] = 'frame-parse'
                        complete = not frame.failed and frame.feed(chunk)
                        if state.get('serial_preamble_bytes') != frame.preamble_bytes:
                            state['serial_preamble_bytes'] = frame.preamble_bytes
                            save()
                        if template and complete and not state['template_received'] and not failed:
                            diagnostic['stage'] = 'report-validation'
                            receipt = receive_template_report(operation / ('template-receipt-' + str(generation) + '.json'), template_nonce, frame.payload)
                            state.update(template_received=True, template_phase=receipt['phase'])
                            if receipt['phase'] != 'audit_requested':
                                state['stop_requested'] = True
                            if receipt['phase'] == 'failed':
                                failed = True
                                state['template_error_code'] = receipt['code']
                            diagnostic['stage'] = 'state-save'
                            save()
                        elif not template and complete and not state['inventory_received'] and not failed:
                            diagnostic['stage'] = 'report-validation'
                            receive_report(operation / 'reference-report.json', manifest['reference_id'], frame.payload)
                            state.update(inventory_received=True, status='inventory_received')
                            diagnostic['stage'] = 'state-save'
                            save()
                    if not failed and inventory_timed_out(time.monotonic() - begin, state['inventory_received'], gateway=gateway or template):
                        diagnostic['stage'] = 'inventory-timeout'
                        raise TimeoutError('Manual inventory timeout')
                    diagnostic['stage'] = 'idle'
                    time.sleep(.2)
                except BaseException as error:
                    if (not failed and diagnostic['stage'] in {'ringbuf-read', 'powerdown'}
                            and isinstance(error, (EOFError, OSError, queue.Empty))):
                        try:
                            process.wait(timeout=2)
                            reader.join(timeout=.2)
                            terminal = terminal_exit_kind(error, diagnostic['stage'], list(events), generation,
                                process.returncode, state['process'], runtime.process_identity(process.pid), failed)
                            if terminal and not reader.is_alive():
                                state['terminal_transport_exit'] = terminal
                                break
                        except BaseException:
                            # A failed grace check keeps normal error handling and the live guest's pins.
                            pass
                    state['serial_preamble_bytes'] = frame.preamble_bytes
                    record_failure(state, error, diagnostic['stage'], diagnostic['command'], diagnostic['class'])
                    failed = True
                    state.update(status='failure-guest-retained', error='Reference inventory/control failed', inventory_received=False)
                    try:
                        save()
                        time.sleep(.2)
                    except BaseException:
                        pass
            process.wait()
            if reader.ident is not None:
                reader.join(timeout=2)
            if drain is not None:
                drain.close()
            writer.close()
            native = runtime.process_identity(process.pid)
            reset = not failed and not reader.is_alive() and reset_proven(list(events), generation, process.returncode, state.get('process'), native)
            if reset:
                state.setdefault('reset_proofs', []).append({'generation': generation, 'process': state['process'],
                    'exit_code': 0, 'reason': 'guest-reset', 'gone': True})
            if state['inventory_received']:
                try:
                    reference_prep.read_report(operation / 'reference-report.json', expected_reference_id=manifest['reference_id'])
                except (ValueError, OSError):
                    failed = True
                    state['inventory_received'] = False
            shutdown = any(e['event'] == 'SHUTDOWN' and e['data'].get('guest') is True
                           and e['data'].get('reason') == 'guest-shutdown' for e in events)
            state.update(exit_code=process.returncode, status='restarting' if reset else 'exited')
            if not reset and (not shutdown or process.returncode != 0):
                failed = True
            save()
            if not reset or state['stop_requested']:
                break
            if generation == 4:
                failed = True
                state['error'] = 'Reference cold restart limit reached'
                break
            state.pop('powerdown_sent', None)
            if template:
                state['template_received'] = False
        if template:
            state['template_completed'] = template_completed(operation, state)
            if not state['template_completed']:
                failed = True
        state['status'] = 'failed' if failed else 'stopped'
        save()
        return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('prepare')
    launch = commands.add_parser('run')
    launch.add_argument('operation', type=Path)
    mode = launch.add_mutually_exclusive_group()
    mode.add_argument('--gateway', action='store_true', help='Use only the verified existing production gateway')
    mode.add_argument('--template', action='store_true', help='Run the frozen offline Audit app preparation')
    commands.add_parser('stop').add_argument('operation', type=Path)
    args = parser.parse_args(argv)
    root = args.project.absolute()
    try:
        if os.name != 'nt' or '..' in root.parts or str(root).startswith('\\\\'):
            raise ValueError('Local Windows project required')
        if args.command == 'prepare':
            print(json.dumps({'operation': str(prepare(root)), 'manual_action': 'Run handoff drive Run-Inventory.cmd inside guest and accept UAC'}))
        elif args.command == 'stop':
            stop(root, args.operation)
        else:
            result = run(root, args.operation, gateway=args.gateway, template=args.template)
            print(json.dumps({key: result[key] for key in ('status', 'inventory_received', 'ready_for_generalize')}))
            return int(result['status'] == 'failed')
    except (ValueError, RuntimeError, OSError):
        parser.exit(1, 'Reference operation refused or failed; owned files retained.\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
