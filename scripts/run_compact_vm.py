"""Headless, offline maintenance-copy runner. Control permits ACPI shutdown only."""
import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_config import QEMU
import launch_runtime as runtime
from portable_app import ENGINE_SHA256
from prepare_compact_master import plain_path
from profile_store import _pinned_directory, _plain
from reference_vm import read_qmp_stream
from runtime_logs import CappedWriter, start_capped_drain
from runtime_config import load_settings
from file_snapshot import qemu_path
import windows_hardware
import windows_guest_settings as windows_settings


def arguments(project, operation, phase):
    settings = load_settings(project)
    escape = lambda path: qemu_path(path).replace(',', ',,')
    share = project / QEMU / 'share'
    return ['-name', 'WindowsClaude-Maintenance-' + phase, '-machine', 'q35', '-accel', 'whpx',
            '-cpu', windows_hardware.CPU_MODEL, '-smp', str(settings['vcpus']), '-m', str(settings['work_memory_mib']), '-L', qemu_path(share),
            '-nic', 'none', '-display', 'none', '-vga', 'std', '-serial', 'none',
            '-parallel', 'none', '-monitor', 'none', '-qmp', 'stdio', '-no-reboot',
            *windows_settings.vga_arguments(windows_settings.DEFAULTS),
            *windows_hardware.arguments(serial='WC-MAINTENANCE-COPY'),
            '-drive', 'if=pflash,unit=0,format=raw,readonly=on,file=' + escape(share / 'edk2-x86_64-code.fd'),
            '-drive', 'if=pflash,unit=1,format=raw,file=' + escape(operation / (phase + '-vars.fd')),
            '-drive', 'if=ide,index=0,format=qcow2,discard=unmap,file=' + escape(operation / (phase + '.qcow2')),
            '-boot', 'order=c,strict=on']


def shutdown_confirmed(events, exit_code):
    return type(exit_code) is int and exit_code == 0 and any(
        event.get('event') == 'SHUTDOWN' and event.get('data', {}).get('guest') is True
        and event['data'].get('reason') == 'guest-shutdown' for event in events)


def powerdown_request(value, nonce):
    if type(value) is not dict or set(value) != {'nonce', 'action'} or value != {'nonce': nonce, 'action': 'powerdown'}:
        raise ValueError('Maintenance control must match this run and request ACPI shutdown only')
    return True


class QmpRefused(RuntimeError):
    pass


class QmpDisconnected(EOFError):
    pass


def save_advisory(state, save):
    try:
        save()
    except (OSError, ValueError, RuntimeError) as error:
        state['journal_error'] = type(error).__name__ + ': ' + str(error)[:500]


def supervise(process, directory, state, inbox, save):
    sequence = 0

    def command(name, parameters=None):
        nonlocal sequence
        sequence += 1
        value = {'execute': name, 'id': sequence}
        if parameters:
            value['arguments'] = parameters
        try:
            process.stdin.write(json.dumps(value) + '\n')
            process.stdin.flush()
        except (BrokenPipeError, ValueError) as error:
            raise QmpDisconnected('Maintenance QMP input closed') from error
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                message = inbox.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty as error:
                raise TimeoutError('Maintenance QMP response timed out: ' + name) from error
            if message.get('eof'):
                raise QmpDisconnected('Maintenance QMP stream ended')
            if message.get('id') == sequence:
                if 'error' in message:
                    raise QmpRefused('Maintenance QMP command refused: ' + name + ': ' + str(message['error']))
                return message.get('return')
        raise TimeoutError('Maintenance QMP response timed out: ' + name)

    try:
        try:
            greeting = inbox.get(timeout=15)
            if greeting.get('eof'):
                raise QmpDisconnected('Maintenance QMP stream ended before greeting')
            if 'QMP' not in greeting:
                state['greeting_error'] = 'Missing maintenance QMP greeting'
        except queue.Empty:
            state['greeting_error'] = 'Maintenance QMP greeting timed out'
        boot_started = time.monotonic()
        ready, requested, next_capture = False, False, boot_started + 1
        while process.poll() is None:
            if not ready:
                try:
                    command('qmp_capabilities')
                    ready = True
                except (QmpRefused, OSError) as error:
                    state['control_error'] = type(error).__name__ + ': ' + str(error)[:500]
                    try:
                        # Capabilities may already be applied while their reply was lost.
                        state['status'] = command('query-status')
                        ready = True
                    except (QmpRefused, OSError) as probe_error:
                        state['query_error'] = type(probe_error).__name__ + ': ' + str(probe_error)[:500]
                        save_advisory(state, save)
                        time.sleep(2)
                        continue
            request = directory / 'powerdown.json'
            try:
                _plain(request)
                if request.exists() and not requested:
                    powerdown_request(runtime.read_json(plain_path(request)), state['nonce'])
                    command('system_powerdown')
                    requested = True
                    state['powerdown_requested'] = True
            except (QmpRefused, OSError, ValueError) as error:
                state['request_error'] = type(error).__name__ + ': ' + str(error)[:500]
            if process.poll() is not None:
                break
            try:
                state['status'] = command('query-status')
            except (QmpRefused, OSError) as error:
                state['query_error'] = type(error).__name__ + ': ' + str(error)[:500]
            if time.monotonic() >= next_capture:
                try:
                    snapshot = directory / 'surface.ppm'
                    _plain(snapshot)
                    if snapshot.exists() and snapshot.stat().st_nlink != 1:
                        raise ValueError('Maintenance screenshot became a hard link')
                    command('screendump', {'filename': qemu_path(snapshot)})
                    state['surface'] = str(snapshot)
                except (QmpRefused, OSError, ValueError) as error:
                    state['capture_error'] = type(error).__name__ + ': ' + str(error)[:500]
                try:
                    state['blocks'] = command('query-block')
                except (QmpRefused, OSError) as error:
                    state['block_query_error'] = type(error).__name__ + ': ' + str(error)[:500]
                # Early boot failures may reset before the usual screenshot interval.
                next_capture = time.monotonic() + (1 if time.monotonic() - boot_started < 30 else 30)
            save_advisory(state, save)
            time.sleep(.5 if time.monotonic() - boot_started < 30 else 2)
    except QmpDisconnected as error:
        state['control_error'] = type(error).__name__ + ': ' + str(error)[:500]
        save_advisory(state, save)
        # Only a lost transport leaves no channel for ACPI; await the guest's own exit.


def run(project, operation, phase):
    project, operation = plain_path(project, directory=True), plain_path(operation, directory=True)
    manifest = runtime.read_json(plain_path(operation / 'operation.json'))
    if (operation.name != 'automatic-maintenance-' + manifest['nonce']
            or not operation.parent.name.startswith('compact-master-')):
        raise ValueError('Only an explicit compact-master maintenance operation is allowed')
    for name in (phase + '.qcow2', phase + '-vars.fd'):
        plain_path(operation / name)
    engine = plain_path(runtime.engine_path(project))
    with engine.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != ENGINE_SHA256:
            raise ValueError('Maintenance engine differs from the approved runtime')
    with _pinned_directory(operation, create=False), runtime.ProjectLock(operation) as acquired:
        if not acquired:
            raise RuntimeError('Another maintenance controller already owns this operation')
        nonce = uuid.uuid4().hex
        directory = operation / (phase + '-' + nonce)
        directory.mkdir()
        state = {'schema': 1, 'nonce': nonce, 'phase': phase, 'run': str(directory),
                 'started_utc': runtime.utc_now(), 'guest_shutdown_confirmed': False, 'exit_code': None,
                 'controller': runtime.process_identity(__import__('os').getpid())}
        inbox, events = queue.Queue(), deque(maxlen=256)
        def save():
            state['events'] = list(events)
            state['updated_utc'] = runtime.utc_now()
            runtime.atomic_json(directory / 'state.json', state)
            runtime.atomic_json(operation / (phase + '-live.json'), state)

        # Both journals must be writable before a guest can be started.
        save()
        command_line = [str(engine), *arguments(project, operation, phase)]
        environment = runtime.qemu_environment(project)
        with CappedWriter(directory / 'stderr.txt') as writer:
            process = reader = drain = None
            try:
                process = subprocess.Popen(command_line, cwd=project, env=environment,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    state['process'] = runtime.process_identity(process.pid)
                except (OSError, ValueError, RuntimeError) as error:
                    state['process_identity_error'] = type(error).__name__ + ': ' + str(error)[:500]
                reader = threading.Thread(target=read_qmp_stream, args=(process.stdout, inbox, events, 1), daemon=True)
                reader.start()
                drain = start_capped_drain(process.stderr.buffer, writer)
                save_advisory(state, save)
                try:
                    print(json.dumps({'running': True, 'phase': phase, 'pid': process.pid,
                                      'state': str(operation / (phase + '-live.json'))}), flush=True)
                except OSError:
                    pass
                supervise(process, directory, state, inbox, save)
            except Exception as error:
                state['control_error'] = type(error).__name__ + ': ' + str(error)[:500]
            finally:
                if process is not None:
                    # This protected lifetime never cuts power, including setup failures.
                    state['exit_code'] = process.wait()
                if reader is not None and reader.ident is not None:
                    reader.join(timeout=5)
                if drain is not None:
                    drain.join(timeout=5)
                state['guest_shutdown_confirmed'] = shutdown_confirmed(events, state['exit_code'])
                state['finished_utc'] = runtime.utc_now()
                save_advisory(state, save)
            try:
                print(json.dumps(state), flush=True)
            except OSError:
                pass
            return 0 if state['guest_shutdown_confirmed'] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--operation', type=Path, required=True)
    parser.add_argument('--phase', choices=('baseline', 'maintenance', 'verification'), required=True)
    args = parser.parse_args()
    return run(args.project, args.operation, args.phase)


if __name__ == '__main__':
    raise SystemExit(main())
