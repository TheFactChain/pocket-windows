"""Isolated dual-stack LAN/TUN acceptance; no Windows or user profiles are attached."""
import argparse
import base64
from contextlib import ExitStack
import io
import ipaddress
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tarfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from acceptance_gateway import Proxy, ConnectHandler
from file_snapshot import locked_read, remove_owned_file
from gateway_package import _check_disk
from profile_store import ProfileStore, _pinned_directory, _plain
import gateway_controller
import launch_runtime as runtime

TARGET = '2001:db8:77::2'
TARGET_IPV4 = '198.51.100.77'
HOSTNAME = 'probe.wcp.test'


class ProbeProxyHandler(ConnectHandler):
    def destination(self, authority):
        if authority == ('[' + TARGET + ']:443').encode('ascii'):
            self.server.fixture_connects += 1
            return '127.0.0.1', self.server.fixture_port
        if authority == (TARGET_IPV4 + ':443').encode('ascii'):
            self.server.fixture4_connects += 1
            return '127.0.0.1', self.server.fixture_port
        return super().destination(authority)


class TlsFixture(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def get_request(self):
        raw, peer = super().get_request()
        raw.settimeout(8)
        try:
            return self.context.wrap_socket(raw, server_side=True), peer
        except BaseException:
            raw.close()
            raise

    def handle_error(self, request, client_address):
        pass


class TlsHandler(socketserver.StreamRequestHandler):
    def handle(self):
        header = self.rfile.readline(4097)
        if header != ('GET /probe/' + self.server.nonce + ' HTTP/1.0\r\n').encode('ascii'):
            return
        total = len(header)
        while True:
            line = self.rfile.readline(4097)
            total += len(line)
            if not line or total > 8192:
                return
            if line == b'\r\n':
                break
        body = ('WCPROBE ' + self.server.nonce + '\n').encode('ascii')
        self.server.valid_requests += 1
        self.wfile.write(b'HTTP/1.0 200 OK\r\nConnection: close\r\nContent-Length: '
                         + str(len(body)).encode('ascii') + b'\r\n\r\n' + body)


def make_certificate(directory):
    shell = shutil.which('pwsh')
    if not shell:
        raise RuntimeError('PowerShell 7 is needed for the temporary test certificate')
    # This creates PEM files in this operation only, never a host trust-store entry.
    script = r"""$ErrorActionPreference='Stop'
$rsa=[System.Security.Cryptography.RSA]::Create(2048)
try {
 $request=[System.Security.Cryptography.X509Certificates.CertificateRequest]::new('CN=probe.wcp.test',$rsa,[System.Security.Cryptography.HashAlgorithmName]::SHA256,[System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
 $san=[System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
 $san.AddDnsName('probe.wcp.test')
 $request.CertificateExtensions.Add($san.Build())
 $certificate=$request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddMinutes(-5),[DateTimeOffset]::UtcNow.AddHours(2))
 try {
  [IO.File]::WriteAllText([IO.Path]::GetFullPath('probe-cert.pem'),$certificate.ExportCertificatePem())
  [IO.File]::WriteAllText([IO.Path]::GetFullPath('probe-key.pem'),$rsa.ExportPkcs8PrivateKeyPem())
 } finally { $certificate.Dispose() }
} finally { $rsa.Dispose() }
"""
    subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', script], cwd=directory,
                   check=True, capture_output=True, timeout=30)


def inspect_wan(path, proxy_port):
    packets = ipv4 = 0
    with path.open('rb') as stream:
        header = stream.read(24)
        if len(header) != 24 or header[:4] not in (b'\xd4\xc3\xb2\xa1', b'\xa1\xb2\xc3\xd4'):
            raise ValueError('Invalid acceptance capture')
        endian = '<' if header[:4] == b'\xd4\xc3\xb2\xa1' else '>'
        if struct.unpack_from(endian + 'I', header, 20)[0] != 1:
            raise ValueError('Ethernet capture is required')
        while record := stream.read(16):
            if len(record) != 16 or stream.tell() > 17 * 1024**2:
                raise ValueError('Truncated or oversized acceptance capture')
            size = struct.unpack_from(endian + 'I', record, 8)[0]
            if not 14 <= size <= 65536:
                raise ValueError('Unexpected packet size')
            packet = stream.read(size)
            if len(packet) != size:
                raise ValueError('Truncated acceptance packet')
            packets += 1
            kind = packet[12:14]
            if kind == b'\x08\x06':
                continue
            if kind != b'\x08\x00' or len(packet) < 54:
                raise ValueError('Non-IPv4 traffic escaped to WAN')
            ip = packet[14:]
            ihl = (ip[0] & 15) * 4
            if ip[0] >> 4 != 4 or not 20 <= ihl <= 60 or len(ip) < ihl + 20 or ip[9] != 6:
                raise ValueError('Non-proxy protocol escaped to WAN')
            source, destination = socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
            sport, dport = struct.unpack_from('!HH', ip, ihl)
            if not ((destination == '10.0.2.2' and dport == proxy_port)
                    or (source == '10.0.2.2' and sport == proxy_port)):
                raise ValueError('Traffic bypassed the pinned proxy')
            ipv4 += 1
    if not ipv4:
        raise ValueError('No positive WAN capture evidence')
    return {'packets': packets, 'proxy_ipv4_packets': ipv4, 'bypass_packets': 0}


def probe_arguments(directory, carrier, port):
    relative = directory.relative_to(ROOT).as_posix()
    return ['-name', 'WindowsClaude-acceptance-probe', '-machine', 'q35', '-accel', 'whpx',
            '-cpu', 'Westmere', '-smp', '1', '-m', '512', '-L', 'runtime/qemu-11.1.0/share',
            '-display', 'none', '-vga', 'none', '-monitor', 'none', '-parallel', 'none',
            '-qmp', 'stdio', '-chardev', 'ringbuf,id=wcserial,size=65536', '-serial', 'chardev:wcserial',
            '-drive', 'if=none,id=wcroot,format=qcow2,file=' + relative + '/probe.qcow2',
            '-device', 'virtio-blk-pci,drive=wcroot,serial=WCROOT,bootindex=1',
            '-blockdev', json.dumps({'driver': 'raw', 'node-name': 'wcprobe', 'read-only': True,
                'file': {'driver': 'file', 'filename': carrier.relative_to(ROOT).as_posix()}}, ensure_ascii=True),
            '-device', 'virtio-blk-pci,drive=wcprobe,serial=WCPROBE',
            '-netdev', 'stream,id=wclan,server=off,addr.type=inet,addr.host=127.0.0.1,addr.port=' + str(port),
            '-device', 'virtio-net-pci,netdev=wclan,mac=52:54:00:77:00:03', '-boot', 'order=c,strict=on', '-no-reboot']


def stop_peer(process, qmp, identity, finished, observed):
    if process.poll() is not None:
        return
    if finished:
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is None and qmp is not None:
        try:
            qmp.command('system_powerdown')
            process.wait(timeout=20)
        except (OSError, RuntimeError, TimeoutError, queue.Empty, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        # Only the fixed isolated Linux argv above is eligible for this user-approved fallback.
        if not runtime.same_identity(identity, runtime.process_identity(process.pid)):
            try:
                process.wait(timeout=2)
                return
            except subprocess.TimeoutExpired:
                raise RuntimeError('Disposable Linux process identity changed') from None
        observed['probe_forced_stop'] = True
        process.terminate()
        process.wait(timeout=20)


def attach_capture(attach, qmp, status, port, path):
    class CapturingCommands:
        def command(self, name, parameters=None):
            result = qmp.command(name, parameters)
            if name == 'netdev_add' and parameters.get('id') in ('wcwan', 'wclan'):
                network = parameters['id']
                capture = path if network == 'wcwan' else path.with_name('lan.pcap')
                qmp.command('object-add', {'qom-type': 'filter-dump', 'id': 'wcacceptancecapture' + network,
                    'netdev': network, 'file': capture.relative_to(ROOT).as_posix()})
            return result
    attach(CapturingCommands(), status, port)


def run_peer(directory, disk, nonce, port, proxy, observed, finished):
    carrier, overlay = directory / 'probe.tar', directory / 'probe.qcow2'
    with tarfile.open(carrier, 'x') as archive:
        for name, data in (('probe.json', json.dumps({'schema': 1, 'nonce': nonce, 'target': TARGET,
                                                   'hostname': HOSTNAME}).encode('ascii')),
                           ('ca.pem', (directory / 'probe-cert.pem').read_bytes())):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o400
            archive.addfile(member, io.BytesIO(data))
    identities = {carrier: {'device': carrier.stat().st_dev, 'file_id': carrier.stat().st_ino}}
    subprocess.run([str(runtime.engine_path(ROOT).with_name('qemu-img.exe')), 'create', '-f', 'qcow2',
                    '-F', 'qcow2', '-b', os.path.relpath(disk, directory).replace('\\', '/'),
                    str(overlay)], cwd=ROOT, check=True, capture_output=True, timeout=30)
    identities[overlay] = {'device': overlay.stat().st_dev, 'file_id': overlay.stat().st_ino}
    process = qmp = None
    phases, pending = [], b''
    with carrier.open('rb'), (directory / 'probe-stderr.txt').open('w') as errors:
        try:
            process = subprocess.Popen([str(runtime.engine_path(ROOT)), *probe_arguments(directory, carrier, port)],
                cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
            observed['probe_process'] = runtime.process_identity(process.pid)
            runtime.atomic_json(directory / 'probe-process.json', {'process': observed['probe_process'],
                'arguments': probe_arguments(directory, carrier, port), 'windows_attached': False,
                'user_secrets_attached': False})
            qmp = gateway_controller.Qmp(process, directory)
            deadline = time.monotonic() + 210
            while process.poll() is None and time.monotonic() < deadline and not finished.is_set():
                if any(path.exists() and path.stat().st_size > 16 * 1024**2
                       for path in (directory / 'wan.pcap', directory / 'lan.pcap')):
                    raise ValueError('Acceptance capture limit exceeded')
                pending += base64.b64decode(qmp.command('ringbuf-read', {
                    'device': 'wcserial', 'size': 65536, 'format': 'base64'}), validate=True)
                if len(pending) > 131072:
                    raise ValueError('Invalid probe serial stream')
                lines = pending.split(b'\n')
                pending = lines.pop()
                for line in lines:
                    try:
                        value = json.loads(line) if len(line) <= 4096 else None
                    except ValueError:
                        continue
                    if not isinstance(value, dict) or value.get('nonce') != nonce:
                        continue
                    phase = value.get('phase')
                    if phase not in ('connected', 'lost', 'recovered', 'finished', 'failed'):
                        raise ValueError('Unknown probe phase')
                    phases.append(phase)
                    runtime.atomic_json(directory / 'probe-phases.json', phases)
                    if phase == 'failed':
                        allowed = ('carrier-invalid', 'network-setup-failed', 'ra-not-ready',
                                   'connection-probes-failed', 'dns-unavailable', 'tls-unavailable',
                                   'dns-tls-unavailable')
                        observed['probe_failure_code'] = value.get('code') if value.get('code') in allowed else 'unknown'
                    if phase == 'connected' and phases == ['connected']:
                        proxy.reject = True
                    if phase == 'lost' and phases == ['connected', 'lost']:
                        proxy.reject = False
                time.sleep(.2)
        except (OSError, ValueError, RuntimeError, TimeoutError, queue.Empty) as error:
            observed['peer_error'] = type(error).__name__
        finally:
            if process is not None:
                stop_peer(process, qmp, observed['probe_process'], 'finished' in phases, observed)
            gone = process is not None and process.poll() is not None and not runtime.same_identity(
                observed['probe_process'], runtime.process_identity(process.pid))
            observed.update(probe_phases=phases, probe_exit_code=process.poll() if process else None,
                            probe_gone=gone, probe_guest_shutdown=bool(qmp and any(
                                row.get('event') == 'SHUTDOWN' and row.get('data') ==
                                {'guest': True, 'reason': 'guest-shutdown'} for row in qmp.events)))
    if observed['probe_gone']:
        for path, identity in identities.items():
            remove_owned_file(path, identity)
        observed['probe_media_removed'] = True
    proxy.reject = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe-result', required=True)
    args = parser.parse_args()
    metadata = Path(args.probe_result).absolute()
    if metadata.name != 'probe-result.json' or metadata.parent.parent != ROOT / 'state/gateway-build':
        raise ValueError('An owned acceptance probe build is required')
    operation = ROOT / 'logs' / ('gateway-lan-acceptance-' + uuid.uuid4().hex)
    observed = {'windows_attached': False, 'user_profiles_read': False, 'outage_observed': False}
    with runtime.ProjectLock(ROOT) as acquired, ExitStack() as held:
        if not acquired or runtime.engine_processes(ROOT):
            raise RuntimeError('Stop all portable VMs before this isolated test')
        held.enter_context(_pinned_directory(metadata.parent, create=False))
        _plain(metadata)
        source = runtime.read_json(metadata)
        build = runtime.read_json(metadata.with_name('build-result.json'))
        if (source.get('version') != 'acceptance-probe-v1' or build.get('accepted') is not True
                or build.get('phase') != 'installed' or build.get('windows_attached') is not False
                or build.get('user_secrets_attached') is not False):
            raise ValueError('The isolated probe image was not accepted')
        disk = metadata.with_name('gateway.qcow2')
        _plain(disk)
        stream = held.enter_context(locked_read(disk))
        _check_disk(stream, {'virtual_bytes': source['virtual_bytes'], 'bytes': source['disk_bytes'],
                             'sha256': source['disk_sha256']})
        held.enter_context(_pinned_directory(ROOT / 'logs'))
        operation.mkdir()
        held.enter_context(_pinned_directory(operation, create=False))
        make_certificate(operation)
        nonce = uuid.uuid4().hex
        try:
            with TlsFixture(('127.0.0.1', 0), TlsHandler) as tls, Proxy(('127.0.0.1', 0), ProbeProxyHandler) as proxy:
                tls.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                tls.context.load_cert_chain(operation / 'probe-cert.pem', operation / 'probe-key.pem')
                tls.nonce, tls.valid_requests = nonce, 0
                proxy.reject, proxy.attempts, proxy.fixture_connects, proxy.fixture4_connects = False, 0, 0, 0
                proxy.fixture_port = tls.server_address[1]
                servers = [threading.Thread(target=server.serve_forever, daemon=True) for server in (tls, proxy)]
                for server in servers:
                    server.start()
                store = ProfileStore(operation / 'fixture-profiles')
                profile = store.save({'name': 'Isolated dual-stack acceptance', 'protocol': 'http',
                    'host': '10.0.2.2', 'port': proxy.server_address[1]})
                store.select(profile['profile_id'])
                before = set((ROOT / 'logs').glob('guest-gateway-boot-*'))
                finished = threading.Event()
                attach = gateway_controller.attach_network
                def attach_with_capture(qmp, status, port):
                    get = qmp.messages.get
                    def observe_reply(*args, **kwargs):
                        reply = get(*args, **kwargs)
                        if 'error' in reply:
                            # Only this no-secret fixture's network hotplug commands are observed.
                            runtime.atomic_json(operation / 'capture-qmp-error.json', reply['error'])
                        return reply
                    qmp.messages.get = observe_reply
                    try:
                        attach_capture(attach, qmp, status, port, operation / 'wan.pcap')
                        observed['capture_before_nics'] = True
                    finally:
                        qmp.messages.get = get
                def observe():
                    state_path = None
                    try:
                        deadline = time.monotonic() + 200
                        while not finished.wait(.2) and time.monotonic() < deadline:
                            created = set((ROOT / 'logs').glob('guest-gateway-boot-*')) - before
                            if len(created) != 1:
                                continue
                            state_path = created.pop() / 'state.json'
                            if not state_path.exists():
                                continue
                            state = runtime.read_json(state_path)
                            if state.get('profile_id') != profile['profile_id'] or state.get('test_only') is not True:
                                raise ValueError('Unrelated gateway state')
                            if state.get('status') == 'running':
                                port = gateway_controller.load_settings(ROOT)['lan_port']
                                run_peer(operation, disk, nonce, port, proxy, observed, finished)
                                break
                            if state.get('status') == 'exited':
                                return
                    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                        observed['observer_error'] = type(error).__name__
                    finally:
                        if state_path and state_path.exists() and not finished.is_set():
                            runtime.atomic_json(state_path.parent / 'control-runtime-lan-probe.json', {'action': 'powerdown'})
                watcher = threading.Thread(target=observe, daemon=True)
                gateway_controller.attach_network = attach_with_capture
                watcher.start()
                try:
                    code = gateway_controller.run(ROOT, test_only=True, store=store, stop_after_ready=False)
                finally:
                    gateway_controller.attach_network = attach
                    finished.set()
                    watcher.join(timeout=45)
                    for server in (tls, proxy):
                        server.shutdown()
                    for server in servers:
                        server.join(timeout=5)
                created = set((ROOT / 'logs').glob('guest-gateway-boot-*')) - before
                if len(created) != 1:
                    raise RuntimeError('Gateway evidence is not unique')
                state_path = created.pop() / 'state.json'
                state = runtime.read_json(state_path)
                serial = state_path.with_name('runtime-serial.log').read_text()
                observed['outage_observed'] = '"event":"RECONNECTING"' in serial
                try:
                    observed['wan_capture'] = inspect_wan(operation / 'wan.pcap', proxy.server_address[1])
                except (OSError, ValueError) as error:
                    observed['capture_error'] = str(error)
                observed.update(schema=1, state=str(state_path), controller_exit_code=code,
                    proxy_attempts=proxy.attempts, ipv6_connects=proxy.fixture_connects,
                    ipv4_connects=proxy.fixture4_connects, dhcp_tested=False,
                    verified_tls_requests=tls.valid_requests, gateway_guest_shutdown=state.get('guest_shutdown'),
                    gateway_carrier_removed=state.get('carrier_removed'), gateway_overlay_removed=state.get('overlay_removed'),
                    live_qemu=list(runtime.engine_processes(ROOT)), external_ipv6_tested=False,
                    scope='IPv4/IPv6 LAN/TUN and CONNECT authorities; controlled TLS targets map to IPv4 loopback')
                observed['passed'] = (code == 0 and observed.get('probe_phases') == ['connected', 'lost', 'recovered', 'finished']
                    and observed.get('probe_exit_code') == 0 and observed.get('probe_gone') is True
                    and observed.get('probe_guest_shutdown') is True and observed.get('probe_media_removed') is True
                    and not observed.get('probe_forced_stop') and observed['outage_observed']
                    and tls.valid_requests >= 4 and proxy.fixture_connects >= 2 and proxy.fixture4_connects >= 2
                    and 'wan_capture' in observed
                    and observed.get('capture_before_nics') is True
                    and all(observed.get(key) is True for key in ('gateway_guest_shutdown', 'gateway_carrier_removed', 'gateway_overlay_removed'))
                    and not observed['live_qemu'])
        finally:
            # Private key belongs only to this local TLS fixture and is never retained in reports.
            _plain(operation / 'probe-key.pem')
            (operation / 'probe-key.pem').unlink(missing_ok=True)
        runtime.atomic_json(operation / 'result.json', observed)
        print(json.dumps(observed), flush=True)
        return 0 if observed['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
