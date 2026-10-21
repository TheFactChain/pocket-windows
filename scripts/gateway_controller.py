"""Own one ephemeral gateway; a matching live handshake gates the Windows NIC."""
import argparse
import base64
from contextlib import ExitStack
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import launch_runtime as runtime
from file_snapshot import locked_read, QEMU_IMG_SHA256, remove_owned_file
from gateway_profile import validate_session
from profile_store import ProfileStore, _exclusive_file, _pinned_directory, _plain
from runtime_config import load_settings
from runtime_logs import CappedWriter, start_capped_drain
from session_carrier import create_carrier

START_TIMEOUT = 180
HEALTH_TIMEOUT = 45
SHUTDOWN_RETRY_INTERVAL = 5


class SessionStatus:
    def __init__(self, session):
        self.identity = {key: session[key] for key in ('nonce', 'revision', 'gateway_version')}
        self.firewall_ready = self.network_attached = self.ready = self.failed = self.ever_ready = False
        self.last_health = None

    def accept(self, value, now):
        if not isinstance(value, dict) or any(value.get(k) != v for k, v in self.identity.items()):
            return None
        event = value.get('event')
        if event not in ('FIREWALL_READY', 'READY', 'HEALTHY', 'RECONNECTING', 'ERROR', 'STOPPED'):
            return None
        if set(value) - {'event', 'nonce', 'revision', 'gateway_version', 'code'}:
            return None
        if event in ('ERROR', 'STOPPED'):
            self.failed, self.ready = True, False
        elif self.failed:
            return None
        elif event == 'RECONNECTING':
            if not self.ever_ready or not self.network_attached:
                return None
            self.ready, self.last_health = False, None
        elif event == 'FIREWALL_READY':
            if self.firewall_ready:
                return None
            self.firewall_ready = True
        elif event == 'READY':
            if not self.firewall_ready or not self.network_attached or self.ready:
                return None
            self.ready, self.ever_ready, self.last_health = True, True, now
        elif event == 'HEALTHY':
            if not self.ready:
                return None
            self.last_health = now
        return event

    def healthy(self, now):
        return self.ready and not self.failed and self.last_health is not None and now - self.last_health <= HEALTH_TIMEOUT


def make_session(store, version, profile_id=None):
    selected = store.selected_summary() if profile_id is None else store.get_view(profile_id)
    if not selected:
        raise ValueError('Select a connection before starting the gateway')
    profile = store.secret_config(selected['profile_id'], selected['revision_id'])
    # Resolve once on the host; only the pinned IPv4 can leave the gateway WAN.
    try:
        endpoint = str(ipaddress.IPv4Address(profile['host']))
    except ipaddress.AddressValueError:
        replies = queue.Queue(maxsize=1)
        def resolve():
            try:
                replies.put(socket.getaddrinfo(profile['host'], profile['port'], socket.AF_INET, socket.SOCK_STREAM))
            except OSError:
                replies.put(None)
        threading.Thread(target=resolve, daemon=True).start()
        try:
            addresses = replies.get(timeout=10)
        except queue.Empty:
            raise ValueError('Connection hostname resolution timed out') from None
        if not addresses:
            raise ValueError('Connection hostname has no IPv4 address')
        endpoint = addresses[0][4][0]
    return selected, validate_session({
        'schema': 1, 'nonce': uuid.uuid4().hex, 'revision': selected['revision_id'],
        'gateway_version': version, 'endpoint_ipv4': endpoint, 'profile': profile})


def arguments(root, directory, media, settings):
    relative = directory.relative_to(root).as_posix()
    if directory.parent != root / 'logs' or not directory.name.startswith('guest-gateway-boot-'):
        raise ValueError('Gateway output must be an owned session directory')
    if media.get('serial') != 'WCSESSION' or media.get('readonly') is not True:
        raise ValueError('A readonly private session carrier is required')
    return ['-name', 'WindowsClaude-gateway-boot', '-machine', 'q35', '-accel', 'whpx',
            '-cpu', 'Westmere', '-smp', str(settings['vcpus']), '-m', str(settings['gateway_memory_mib']),
            '-L', 'runtime/qemu-11.1.0/share', '-display', 'none', '-vga', 'none', '-nic', 'none',
            '-monitor', 'none', '-parallel', 'none', '-qmp', 'stdio',
            '-chardev', 'ringbuf,id=wcserial,size=65536', '-serial', 'chardev:wcserial',
            '-drive', 'if=none,id=wcroot,format=qcow2,file=' + relative + '/gateway.qcow2',
            '-device', 'virtio-blk-pci,drive=wcroot,serial=WCROOT,bootindex=1',
            '-blockdev', json.dumps({'driver': 'raw', 'node-name': 'wcsession', 'read-only': True,
                'file': {'driver': 'file', 'filename': str(media['path'])}}, ensure_ascii=True),
            '-device', 'virtio-blk-pci,drive=wcsession,serial=WCSESSION',
            '-device', 'pcie-root-port,id=wcportwan,chassis=1,slot=1',
            '-device', 'pcie-root-port,id=wcportlan,chassis=2,slot=2',
            '-boot', 'order=c,strict=on', '-no-reboot']


def attach_network(qmp, status, port):
    if not status.firewall_ready or status.network_attached or status.failed:
        raise RuntimeError('Current firewall acknowledgement is required before attaching NICs')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid local LAN port')
    qmp.command('netdev_add', {'type': 'stream', 'id': 'wclan', 'server': True,
        'addr': {'type': 'inet', 'host': '127.0.0.1', 'port': str(port),
                 'numeric': True, 'ipv4': True, 'ipv6': False}})
    qmp.command('netdev_add', {'type': 'user', 'id': 'wcwan', 'ipv6': False,
                             'net': '10.0.2.0/24', 'host': '10.0.2.2'})
    for name, network, mac in (('wcnicwan', 'wcwan', '52:54:00:77:00:01'),
                               ('wcniclan', 'wclan', '52:54:00:77:00:02')):
        qmp.command('device_add', {'driver': 'virtio-net-pci', 'id': name, 'netdev': network, 'mac': mac,
                                  'bus': 'wcportwan' if network == 'wcwan' else 'wcportlan'})
    status.network_attached = True


class Qmp:
    def __init__(self, process, directory):
        self.process, self.directory = process, directory
        self.messages, self.events, self.sequence = queue.Queue(), [], 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        if 'QMP' not in self.messages.get(timeout=20):
            raise RuntimeError('Gateway QMP greeting is missing')
        self.command('qmp_capabilities')

    def _read(self):
        for line in self.process.stdout:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if 'event' in value:
                if value['event'] in ('SHUTDOWN', 'RESET', 'SUSPEND', 'SUSPEND_DISK', 'RESUME'):
                    event = {'event': value['event'], 'observed_utc': runtime.utc_now(),
                             'data': {k: v for k, v in value.get('data', {}).items() if k in ('guest', 'reason')}}
                    self.events.append(event)
                    self.events[:] = self.events[-64:]
                    runtime.atomic_json(self.directory / 'qmp-events.json', self.events)
                continue
            self.messages.put(value)
        self.messages.put({'eof': True})

    def command(self, name, parameters=None):
        self.sequence += 1
        request = {'execute': name, 'id': self.sequence}
        if parameters is not None:
            request['arguments'] = parameters
        self.process.stdin.write(json.dumps(request) + '\n')
        self.process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            value = self.messages.get(timeout=max(.01, deadline - time.monotonic()))
            if value.get('eof'):
                raise RuntimeError('Gateway exited before QMP response')
            if value.get('id') == self.sequence:
                if 'error' in value:
                    raise RuntimeError('Gateway QMP command failed: ' + name)
                return value.get('return')
        raise TimeoutError('Gateway QMP response timed out')


def requested_shutdown_completed(process, qmp, requested):
    if not requested or qmp is None or not any(
            event.get('event') == 'SHUTDOWN' and event.get('data', {}).get('guest') is True
            and event['data'].get('reason') == 'guest-shutdown' for event in qmp.events):
        return False
    try:
        return process.wait(timeout=10) == 0
    except subprocess.TimeoutExpired:
        return False


def stop_failed_gateway(qmp):
    # A partial hotplug can leave either NIC absent; still close the other and send ACPI.
    for name in ('wcniclan', 'wcnicwan'):
        try:
            qmp.command('set_link', {'name': name, 'up': False})
        except (OSError, RuntimeError, TimeoutError, queue.Empty):
            pass
    try:
        qmp.command('system_powerdown')
        return True
    except (OSError, RuntimeError, TimeoutError, queue.Empty):
        return False


def wait_failed_gateway_exit(process, qmp):
    retries = 0
    while process.poll() is None:
        # Early boot may not yet have started acpid when the first request arrives.
        time.sleep(5)
        if process.poll() is None and qmp is not None:
            stop_failed_gateway(qmp)
            retries += 1
    process.wait()
    return retries


def wait_requested_gateway_exit(process, qmp, report, save):
    deadline = time.monotonic() + runtime.SHUTDOWN_TIMEOUT
    report.update(status='stopping', network_verified=False, shutdown_retry_count=0,
                  acpi_powerdown_requested_utc=runtime.utc_now())
    save()
    if not stop_failed_gateway(qmp) and not requested_shutdown_completed(process, qmp, True):
        report.update(status='control-unavailable-guest-running', error='CONTROLLER_FAILED')
        save()
    while process.poll() is None:
        if time.monotonic() >= deadline:
            if report['status'] != 'shutdown-unconfirmed':
                report.update(status='shutdown-unconfirmed', error='SHUTDOWN_UNCONFIRMED')
                save()
        time.sleep(SHUTDOWN_RETRY_INTERVAL)
        if process.poll() is None and time.monotonic() < deadline:
            report['shutdown_retry_count'] += 1
            try:
                qmp.command('system_powerdown')
            except (OSError, RuntimeError, TimeoutError, queue.Empty):
                if not requested_shutdown_completed(process, qmp, True):
                    report.update(status='control-unavailable-guest-running', error='CONTROLLER_FAILED')
            save()
    # Keep the caller's protected media and process handles until confirmed exit.
    process.wait()


def run(root, profile_id=None, test_only=False, *, store=None, stop_after_ready=True):
    from gateway_package import open_current
    from portable_app import ENGINE_SHA256
    root = Path(root).absolute()
    settings = load_settings(root)
    carrier = process = None
    report = {}
    with ExitStack() as held:
        held.enter_context(_pinned_directory(root / 'state'))
        held.enter_context(_exclusive_file(root / 'state/gateway-controller.lock'))
        if runtime.engine_processes(root):
            raise RuntimeError('Stop the existing portable VMs before a new gateway session')
        package = held.enter_context(open_current(root))
        engine = runtime.engine_path(root)
        held.enter_context(_pinned_directory(engine.parent, create=False))
        for path in (engine, engine.with_name('qemu-img.exe')):
            _plain(path)
            stream = held.enter_context(locked_read(path))
            expected = ENGINE_SHA256 if path == engine else QEMU_IMG_SHA256
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                raise ValueError('A verified QEMU component has changed')
        selected, session = make_session(store or ProfileStore(root / 'profiles'), package['version'], profile_id)
        status = SessionStatus(session)
        held.enter_context(_pinned_directory(root / 'logs'))
        directory = root / 'logs' / ('guest-gateway-boot-' + session['nonce'])
        directory.mkdir()
        held.enter_context(_pinned_directory(directory, create=False))
        disk = directory / 'gateway.qcow2'
        backing = os.path.relpath(package['disk'], directory).replace('\\', '/')
        subprocess.run([str(engine.with_name('qemu-img.exe')), 'create', '-f', 'qcow2', '-F', 'qcow2',
                        '-b', backing, str(disk)], cwd=root, capture_output=True, check=True, timeout=60)
        disk_identity = {'device': disk.stat().st_dev, 'file_id': disk.stat().st_ino}
        report = {'schema': 2, 'guest': 'gateway-boot', 'network': 'gateway', 'status': 'starting',
                  'started_utc': runtime.utc_now(), 'nonce': session['nonce'],
                  'profile_id': selected['profile_id'], 'profile_revision_id': session['revision'],
                  'gateway_version': package['version'], 'base_sha256': package['sha256'],
                  'disk': disk.relative_to(root).as_posix(), 'network_verified': False,
                  'controller': runtime.process_identity(os.getpid()), 'test_only': test_only,
                  'normal_runtime': not test_only, 'disk_identity': disk_identity}

        def save():
            report['observed_utc'] = runtime.utc_now()
            runtime.atomic_json(directory / 'state.json', report)
            print(json.dumps({'state': str(directory / 'state.json'), 'status': report['status']}), flush=True)

        def close_network(qmp):
            status.failed, status.ready = True, False
            report['network_verified'] = False
            if status.network_attached:
                for name in ('wcniclan', 'wcnicwan'):
                    qmp.command('set_link', {'name': name, 'up': False})

        carrier = create_carrier(session)
        report['carrier'] = str(carrier.path)
        # The only remaining sensitive copy is the protected carrier attached readonly.
        del session
        try:
            with carrier.hold() as media, CappedWriter(directory / 'runtime-serial.log') as serial_log, \
                    CappedWriter(directory / 'stderr.txt') as stderr_log:
                argv = arguments(root, directory, media, settings)
                environment = os.environ.copy()
                environment['PATH'] = str(engine.parent) + ';' + os.environ['SystemRoot'] + '\\System32'
                process = subprocess.Popen([str(engine), *argv], cwd=root, env=environment,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                    creationflags=subprocess.CREATE_NO_WINDOW)
                report.update(pid=process.pid, process=runtime.process_identity(process.pid), arguments=argv,
                              started_utc=runtime.utc_now())
                save()
                qmp = stderr = None
                stopped_requested = False
                try:
                    stderr = start_capped_drain(process.stderr.buffer, stderr_log)
                    qmp = Qmp(process, directory)
                    deadline, pending = time.monotonic() + START_TIMEOUT, b''
                    lan_blocked = False
                    while process.poll() is None:
                        encoded = qmp.command('ringbuf-read', {'device': 'wcserial', 'size': 65536, 'format': 'base64'})
                        chunk = base64.b64decode(encoded, validate=True)
                        if chunk:
                            serial_log.write(chunk)
                            pending += chunk
                            if len(pending) > 131072:
                                raise RuntimeError('Gateway serial response is invalid')
                            lines = pending.split(b'\n')
                            pending = lines.pop()
                            for line in lines:
                                try:
                                    value = json.loads(line) if len(line) <= 4096 else None
                                except (ValueError, UnicodeError):
                                    continue
                                event = status.accept(value, time.monotonic())
                                if event == 'FIREWALL_READY':
                                    attach_network(qmp, status, settings['lan_port'])
                                elif event in ('READY', 'HEALTHY'):
                                    if lan_blocked:
                                        qmp.command('set_link', {'name': 'wcniclan', 'up': True})
                                        lan_blocked = False
                                    report.update(status='running', network_verified=True,
                                                  health_utc=runtime.utc_now())
                                    report.pop('connection_error', None)
                                    save()
                                    if test_only:
                                        report['connection_test_passed'] = True
                                    if test_only and stop_after_ready and not stopped_requested:
                                        qmp.command('system_powerdown')
                                        stopped_requested = True
                                        save()
                                elif event == 'RECONNECTING':
                                    if not lan_blocked:
                                        qmp.command('set_link', {'name': 'wcniclan', 'up': False})
                                        lan_blocked = True
                                    report.update(status='reconnecting', network_verified=False,
                                                  connection_error='CONNECTION_LOST')
                                    save()
                                elif event == 'STOPPED':
                                    report.update(status='stopping', network_verified=False)
                                    if not stopped_requested:
                                        report['error'] = 'CONNECTION_LOST'
                                        qmp.command('system_powerdown')
                                        stopped_requested = True
                                    save()
                                elif event == 'ERROR':
                                    close_network(qmp)
                                    from portable_app import CONNECTION_ERRORS
                                    code = value.get('code')
                                    report.update(status='connection-failed',
                                                  error=code if code in CONNECTION_ERRORS else 'SESSION_FAILED')
                                    if not stopped_requested:
                                        qmp.command('system_powerdown')
                                        stopped_requested = True
                                    save()
                        if status.ready and not stopped_requested and not lan_blocked and not status.healthy(time.monotonic()):
                            if not lan_blocked:
                                qmp.command('set_link', {'name': 'wcniclan', 'up': False})
                                lan_blocked = True
                            report.update(status='reconnecting', network_verified=False,
                                          connection_error='HEALTH_TIMEOUT')
                            save()
                        if not status.ever_ready and not status.failed and time.monotonic() > deadline:
                            close_network(qmp)
                            report.update(status='connection-failed', error='STARTUP_TIMEOUT')
                            qmp.command('system_powerdown')
                            stopped_requested = True
                            save()
                        for control in directory.glob('control-runtime-*.json'):
                            _plain(control)
                            with locked_read(control) as stream:
                                raw = stream.read(4097)
                            value = json.loads(raw) if len(raw) <= 4096 else {}
                            if value == {'action': 'powerdown'}:
                                stopped_requested = True
                                if not test_only:
                                    control.unlink()
                                    wait_requested_gateway_exit(process, qmp, report, save)
                                    break
                                qmp.command('system_powerdown')
                                report['acpi_powerdown_requested_utc'] = runtime.utc_now()
                                save()
                            control.unlink()
                        time.sleep(.2)
                except Exception:
                    # QMP can close between poll() and ringbuf-read during normal shutdown.
                    if not requested_shutdown_completed(process, qmp, stopped_requested):
                        report.update(status='control-unavailable-guest-running', network_verified=False)
                        report.setdefault('error', 'CONTROLLER_FAILED')
                        if qmp is not None and process.poll() is None:
                            stopped_requested = stop_failed_gateway(qmp)
                        save()
                        # Retain process handles and protected media until this guest really exits.
                        report['shutdown_retry_count'] = wait_failed_gateway_exit(process, qmp)
                finally:
                    process.wait()
                    if stderr is not None:
                        stderr.join()
                    if qmp is not None:
                        qmp.reader.join(timeout=5)
                        report['guest_shutdown'] = any(e['event'] == 'SHUTDOWN'
                            and e['data'].get('guest') is True and e['data'].get('reason') == 'guest-shutdown'
                            for e in qmp.events)
                    report.update(status='exited', exit_code=process.returncode, network_verified=False)
                    save()
        finally:
            if process is None or process.poll() is not None:
                carrier.cleanup(guest_stopped=True)
                report['carrier_removed'] = True
                remove_owned_file(disk, disk_identity)
                report['overlay_removed'] = True
                save()
        return result_code(report, test_only)


def result_code(report, test_only):
    return 0 if (report.get('guest_shutdown') is True and not report.get('error')
                 and report.get('exit_code') == 0
                 and (not test_only or report.get('connection_test_passed') is True)) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--test-profile')
    args = parser.parse_args()
    if not args.project.is_absolute():
        parser.error('An absolute project path is required')
    try:
        return run(args.project, args.test_profile, bool(args.test_profile))
    except Exception:
        # Parser/DNS/tool messages must never echo profile credentials.
        print(json.dumps({'error': 'GATEWAY_START_FAILED'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
