#!/usr/bin/python3
"""Single immutable gateway session. No configuration persists outside /run."""
import glob
import io
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import tarfile
import time
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gateway_profile import closed_firewall, firewall, sing_box_config, validate_session

RUN = Path('/run/wc-session')
VERSION = Path('/etc/windowsclaude/gateway-version')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate field')
        result[key] = value
    return result


def read_session(stream):
    # Read the fixed archive member directly, never mount or extract untrusted media.
    with tarfile.open(fileobj=io.BytesIO(stream.read(131072)), mode='r|') as archive:
        members = iter(archive)
        member = next(members, None)
        if member is None or member.name != 'session.json' or not member.isfile() or not 0 < member.size <= 65536:
            raise ValueError('Invalid session carrier')
        value = json.loads(archive.extractfile(member).read(), object_pairs_hook=unique_object)
        if next(members, None) is not None:
            raise ValueError('Extra carrier member')
    return validate_session(value)


def status_record(event, session=None, code=None):
    record = {'event': event}
    if session:
        record.update({key: session[key] for key in ('nonce', 'revision', 'gateway_version')})
    if code:
        record['code'] = code
    return record


def emit(event, session=None, code=None):
    with open('/dev/ttyS0', 'w') as serial:
        serial.write(json.dumps(status_record(event, session, code), separators=(',', ':')) + '\n')
        serial.flush()


def command(*args, content=None):
    subprocess.run(args, input=content, text=True, check=True, timeout=15,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply_rules(content):
    command('/usr/sbin/nft', '-f', '-', content=content)


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError('Connection probe deadline exceeded')
    return min(8, seconds)


def proxy_https(address, hostname, path, accept='text/html', *, deadline=None):
    deadline = time.monotonic() + 25 if deadline is None else deadline
    # Explicit loopback CONNECT prevents ambient proxy environment or direct fallback.
    with socket.create_connection(('127.0.0.1', 18080), timeout=remaining(deadline)) as raw:
        raw.settimeout(remaining(deadline))
        raw.sendall(f'CONNECT {address}:443 HTTP/1.1\r\nHost: {address}:443\r\n\r\n'.encode('ascii'))
        header = bytearray()
        while not header.endswith(b'\r\n\r\n'):
            raw.settimeout(remaining(deadline))
            chunk = raw.recv(1)
            if not chunk or len(header) >= 8192:
                raise ValueError('Invalid CONNECT reply')
            header.extend(chunk)
        if header.split(b'\r\n', 1)[0].split()[1:2] != [b'200']:
            raise ValueError('CONNECT failed')
        context = ssl.create_default_context()
        raw.settimeout(remaining(deadline))
        with context.wrap_socket(raw, server_hostname=hostname) as secure:
            secure.settimeout(remaining(deadline))
            secure.sendall(f'GET {path} HTTP/1.0\r\nHost: {hostname}\r\nAccept: {accept}\r\nConnection: close\r\n\r\n'.encode('ascii'))
            response = bytearray()
            while len(response) <= 131072:
                secure.settimeout(remaining(deadline))
                chunk = secure.recv(8192)
                if not chunk:
                    break
                response.extend(chunk)
            if len(response) > 131072:
                raise ValueError('Probe response too large')
    headers, body = bytes(response).split(b'\r\n\r\n', 1)
    if headers.split(b'\r\n')[0].split()[1:2] != [b'200']:
        raise ValueError('HTTPS probe failed')
    # HTTP/1.0 is requested; reject unexpected transfer encoding instead of parsing it loosely.
    if b'transfer-encoding:' in headers.lower():
        raise ValueError('Unsupported probe transfer encoding')
    remaining(deadline)
    return body


def health(deadline=None):
    deadline = min(deadline, time.monotonic() + 25) if deadline is not None else time.monotonic() + 25
    remaining(deadline)
    answer = json.loads(proxy_https('1.1.1.1', 'cloudflare-dns.com',
                                   '/dns-query?name=example.com&type=A', 'application/dns-json', deadline=deadline))
    if answer.get('Status') != 0:
        raise ValueError('DNS probe failed')
    addresses = [str(ipaddress.IPv4Address(row['data'])) for row in answer.get('Answer', []) if row.get('type') == 1]
    if not addresses:
        raise ValueError('DNS returned no IPv4')
    remaining(deadline)
    proxy_https(addresses[0], 'example.com', '/', deadline=deadline)
    remaining(deadline)


def wait_nics(stopping):
    expected = {'wan0': '52:54:00:77:00:01', 'lan0': '52:54:00:77:00:02'}
    deadline = time.monotonic() + 30
    while not stopping():
        try:
            if all((Path('/sys/class/net') / name / 'address').read_text().strip().lower() == mac
                   for name, mac in expected.items()):
                return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
    raise ValueError('Expected network interfaces unavailable')


def setup_forwarding():
    # Keep AF_INET6 available for TUN policy rules; only LAN/TUN carry IPv6.
    command('/usr/sbin/sysctl', '-w', 'net.ipv4.ip_forward=1',
            'net.ipv6.conf.all.disable_ipv6=0', 'net.ipv6.conf.default.disable_ipv6=0')
    command('/usr/sbin/sysctl', '-w', 'net.ipv6.conf.wan0.disable_ipv6=1',
            'net.ipv6.conf.all.accept_ra=0', 'net.ipv6.conf.default.accept_ra=0',
            'net.ipv6.conf.wan0.accept_ra=0', 'net.ipv6.conf.lan0.accept_ra=0',
            'net.ipv6.conf.all.accept_redirects=0', 'net.ipv6.conf.default.accept_redirects=0',
            'net.ipv6.conf.wan0.accept_redirects=0', 'net.ipv6.conf.lan0.accept_redirects=0',
            'net.ipv6.conf.all.forwarding=1')


def wait_lan_ipv6(stopping):
    deadline = time.monotonic() + 15
    while not stopping() and time.monotonic() < deadline:
        result = subprocess.run(['/usr/sbin/ip', '-j', '-6', 'address', 'show', 'dev', 'lan0'],
                                check=True, timeout=3, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True)
        addresses = [address for interface in json.loads(result.stdout)
                     if interface.get('ifname') == 'lan0' for address in interface.get('addr_info', [])
                     if address.get('family') == 'inet6']
        if any(address.get('dadfailed') or 'dadfailed' in address.get('flags', []) for address in addresses):
            raise ValueError('LAN IPv6 duplicate address')
        usable = [address for address in addresses if not address.get('tentative')
                  and 'tentative' not in address.get('flags', [])]
        ula = any(address.get('local') == 'fd77:233::1' and address.get('prefixlen') == 64 for address in usable)
        link_local = any(ipaddress.IPv6Address(address['local']).is_link_local for address in usable)
        if ula and link_local:
            return
        time.sleep(1)
    raise ValueError('LAN IPv6 address not ready')


def wait_delay(seconds, stopping):
    for _ in range(seconds):
        if stopping():
            return False
        time.sleep(1)
    return not stopping()


def start_services(config, children):
    # Reuse only the already checked immutable configuration and owned children.
    with open(os.devnull, 'wb') as null:
        children.append(subprocess.Popen(['/usr/local/bin/sing-box', 'run', '-c', str(config)], stdout=null, stderr=null))
        children.append(subprocess.Popen(['/usr/sbin/dnsmasq', '--keep-in-foreground', '--conf-file=/dev/null',
            '--port=0', '--interface=lan0', '--bind-interfaces', '--except-interface=lo',
            '--enable-ra', '--dhcp-range=fd77:233::,ra-stateless,64,1h',
            '--dhcp-option=option6:dns-server,[fd77:233::1]', '--ra-param=lan0,mtu:1280,30,90',
            '--dhcp-range=192.168.233.2,192.168.233.2,255.255.255.0,1h',
            '--dhcp-host=52:54:00:77:00:03,192.168.233.2', '--dhcp-option=3,192.168.233.1',
            '--dhcp-option=6,192.168.233.1', '--dhcp-leasefile=' + str(RUN / 'leases'),
            '--pid-file=' + str(RUN / 'dnsmasq.pid')], stdout=null, stderr=null))


def stop_children(children):
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
    children.clear()


def main():
    os.umask(0o077)
    children = []
    session = None
    stopping = False
    failure_code = 'FIREWALL_SETUP_FAILED'

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        apply_rules(closed_firewall())
        failure_code = 'SESSION_MEDIA_INVALID'
        RUN.mkdir(mode=0o700, exist_ok=True)
        devices = []
        for _ in range(30):
            devices = list({os.path.realpath(path) for path in glob.glob('/dev/disk/by-id/*WCSESSION*')
                            if '-part' not in path})
            if devices or stopping:
                break
            time.sleep(1)
        if len(devices) != 1 or stopping:
            raise ValueError('Carrier unavailable')
        with open(devices[0], 'rb', buffering=0) as carrier:
            session = read_session(carrier)
        failure_code = 'GATEWAY_VERSION_MISMATCH'
        if session['gateway_version'] != VERSION.read_text().strip():
            raise ValueError('Gateway version mismatch')
        config = RUN / 'sing-box.json'
        config.write_text(json.dumps(sing_box_config(session)), encoding='utf-8')
        failure_code = 'PROXY_CONFIG_INVALID'
        command('/usr/local/bin/sing-box', 'check', '-c', str(config))
        emit('FIREWALL_READY', session)
        failure_code = 'NETWORK_DEVICES_UNAVAILABLE'
        wait_nics(lambda: stopping)
        failure_code = 'NFT_RULES_FAILED'
        apply_rules(firewall(session, connected=False))
        # Keep the IPv6 address family for TUN strict-route rules, but disable
        # WAN IPv6 before raising interfaces; nft remains fail-closed.
        failure_code = 'FORWARDING_SETUP_FAILED'
        setup_forwarding()
        failure_code = 'INTERFACE_SETUP_FAILED'
        command('/usr/sbin/ip', 'link', 'set', 'lo', 'up')
        for name, address in (('wan0', '10.0.2.15/24'), ('lan0', '192.168.233.1/24')):
            command('/usr/sbin/ip', 'address', 'replace', address, 'dev', name)
            command('/usr/sbin/ip', 'link', 'set', name, 'up')
        failure_code = 'IPV6_ADDRESS_FAILED'
        command('/usr/sbin/ip', '-6', 'address', 'replace', 'fd77:233::1/64', 'dev', 'lan0')
        wait_lan_ipv6(lambda: stopping)
        failure_code = 'ENDPOINT_ROUTE_FAILED'
        command('/usr/sbin/ip', 'route', 'replace', session['endpoint_ipv4'] + '/32', 'via', '10.0.2.2', 'dev', 'wan0')
        # sing-box's interface monitor requires a main-table default route.
        # This does not grant WAN access: nft permits only the pinned proxy.
        command('/usr/sbin/ip', 'route', 'replace', 'default', 'via', '10.0.2.2', 'dev', 'wan0')
        failure_code = 'PROXY_SERVICE_FAILED'
        start_services(config, children)
        deadline = time.monotonic() + 75
        ever_ready, connected, retry = False, False, 2
        while not stopping:
            failure_code = 'CONNECTION_LOST' if ever_ready else 'CONNECTION_FAILED'
            if any(child.poll() is not None for child in children):
                if not ever_ready:
                    failure_code = 'PROXY_SERVICE_FAILED'
                    raise ValueError('Service stopped before first readiness')
                failure_code = 'NFT_RULES_FAILED'
                apply_rules(firewall(session, connected=False))
                connected = False
                emit('RECONNECTING', session, 'CONNECTION_LOST')
                failure_code = 'PROXY_SERVICE_FAILED'
                stop_children(children)
                if not wait_delay(retry, lambda: stopping):
                    break
                start_services(config, children)
                retry = min(15, retry * 2)
                continue
            if not ever_ready and time.monotonic() >= deadline:
                raise ValueError('Initial connection failed')
            probe_deadline = time.monotonic() + 25
            if not ever_ready:
                probe_deadline = min(deadline, probe_deadline)
            try:
                health(probe_deadline)
                remaining(probe_deadline)
            except ssl.SSLCertVerificationError:
                raise
            except (OSError, ValueError):
                if stopping:
                    break
                if not ever_ready:
                    if time.monotonic() >= deadline:
                        raise ValueError('Initial connection failed') from None
                else:
                    failure_code = 'NFT_RULES_FAILED'
                    apply_rules(firewall(session, connected=False))
                    connected = False
                    emit('RECONNECTING', session, 'CONNECTION_LOST')
                if not wait_delay(retry, lambda: stopping):
                    break
                if ever_ready:
                    retry = min(15, retry * 2)
                continue
            if stopping:
                break
            remaining(probe_deadline)
            if not connected:
                failure_code = 'NFT_RULES_FAILED'
                apply_rules(firewall(session))
                remaining(probe_deadline)
                connected, ever_ready, retry = True, True, 2
                emit('READY', session)
            else:
                emit('HEALTHY', session)
            if not wait_delay(10, lambda: stopping or any(child.poll() is not None for child in children)) and stopping:
                break
    except Exception:
        # Error text from tools/configuration can contain credentials, never serialize it.
        if not stopping:
            try:
                apply_rules(closed_firewall())
            finally:
                emit('ERROR', session, failure_code)
            return 1
    finally:
        apply_rules(closed_firewall())
        stop_children(children)
        for name in ('sing-box.json', 'leases', 'dnsmasq.pid'):
            (RUN / name).unlink(missing_ok=True)
    emit('STOPPED', session)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

