"""Native tests inside the new Debian gateway. Only public results go to serial.

Reads no private config contents. Uses fresh DNS/HTTPS requests; never uses host VPN
as evidence. Host separately inspects the gateway's WAN pcap for bypass traffic.
"""
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import time

ENDPOINT = None
PORT = None
EXPECTED_EGRESS = None
results = []


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def note(name, passed, **data):
    result = {'test': name, 'passed': bool(passed), 'monotonic': round(time.monotonic(), 3), **data}
    results.append(result)
    print('WC_RESULT ' + json.dumps(result), flush=True)
    Path('/root/windowsclaude-runtime-results.json').write_text(json.dumps(results, indent=2))
    if not passed:
        raise AssertionError(name)


def dns(name='api.ipify.org', resolver='192.168.233.1', timeout=4, require_address=True):
    ident = int.from_bytes(os.urandom(2), 'big')
    qname = b''.join(bytes([len(s)]) + s.encode() for s in name.split('.')) + b'\0'
    query = struct.pack('!6H', ident, 0x100, 1, 0, 0, 0) + qname + struct.pack('!HH', 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(query, (resolver, 53))
        data, _ = s.recvfrom(4096)
    header = struct.unpack_from('!6H', data)
    assert header[0] == ident and header[1] & 0x8000
    rcode = header[1] & 15
    if rcode == 2:
        raise OSError('DNS upstream failed')
    if not require_address:
        return {'rcode': rcode}
    assert rcode == 0

    def skip(offset):
        while data[offset]:
            if data[offset] & 0xc0 == 0xc0:
                return offset + 2
            offset += 1 + data[offset]
        return offset + 1

    offset = 12
    for _ in range(header[2]):
        offset = skip(offset) + 4
    addresses = []
    for _ in range(header[3]):
        offset = skip(offset)
        kind, cls, ttl, length = struct.unpack_from('!HHIH', data, offset)
        offset += 10
        if kind == 1 and cls == 1 and length == 4:
            addresses.append(socket.inet_ntoa(data[offset:offset + 4]))
        offset += length
    assert addresses
    return addresses[0]


def https(address, timeout=5):
    with socket.create_connection((address, 443), timeout=timeout) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname='api.ipify.org') as secure:
            secure.sendall(b'GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n')
            response = http.client.HTTPResponse(secure)
            response.begin()
            assert response.status == 200
            return str(ipaddress.ip_address(response.read(128).decode().strip()))


def healthy():
    last = None
    for _ in range(12):
        try:
            address = dns()
            return address, https(address)
        except Exception as error:
            last = type(error).__name__
            time.sleep(2)
    raise RuntimeError('VPN health check failed: ' + str(last))


def blocked(address):
    failures = {}
    fresh_name = 'wc-' + os.urandom(8).hex() + '.example.com'
    for label, probe in [('dns_uncached', lambda: dns(name=fresh_name, timeout=2, require_address=False)),
                         ('https_new_connection', lambda: https(address, timeout=3))]:
        try:
            probe()
        except Exception as error:
            failures[label] = type(error).__name__
        else:
            failures[label] = 'UNEXPECTED_SUCCESS'
    return all(v != 'UNEXPECTED_SUCCESS' for v in failures.values()), failures


def policies():
    rules = json.loads(run('/usr/sbin/nft', '-j', 'list', 'table', 'inet', 'windowsclaude').stdout)
    chains = [x['chain'] for x in rules['nftables'] if 'chain' in x and x['chain'].get('hook')]
    return len(chains) == 3 and all(c['policy'] == 'drop' for c in chains)


def main():
    global ENDPOINT, PORT, EXPECTED_EGRESS
    assert os.geteuid() == 0 and Path('/sys/class/net/wan0').exists()
    profile = json.loads(Path('/etc/windowsclaude/profile-public.json').read_text())
    ENDPOINT = str(ipaddress.IPv4Address(profile['endpoint_ipv4']))
    PORT = profile['endpoint_port']
    assert type(PORT) is int and 1 <= PORT <= 65535
    EXPECTED_EGRESS = str(ipaddress.IPv4Address(profile.get('expected_egress_ipv4', ENDPOINT)))
    note('native_config_and_firewall', run('/usr/local/bin/sing-box', 'check', '-c', '/etc/sing-box/config.json').returncode == 0 and policies())
    run('systemctl', 'start', 'sing-box.service')
    address, egress = healthy()
    note('real_dns_tls_and_egress', egress == EXPECTED_EGRESS, egress=egress)

    run('systemctl', 'stop', 'sing-box.service')
    ok, errors = blocked(address)
    note('core_stopped_offline', ok and policies() and Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() == '0', probes=errors)
    run('systemctl', 'start', 'sing-box.service')
    address, egress = healthy()
    note('core_stop_recovery', egress == EXPECTED_EGRESS, egress=egress)

    before = run('systemctl', 'show', '-p', 'MainPID', '--value', 'sing-box.service').stdout.strip()
    run('systemctl', 'kill', '--kill-whom=main', '--signal=SIGKILL', 'sing-box.service')
    time.sleep(.2)
    run('systemctl', 'is-active', 'sing-box.service', check=False)
    address, egress = healthy()
    after = run('systemctl', 'show', '-p', 'MainPID', '--value', 'sing-box.service').stdout.strip()
    note('core_crash_restart_recovery', before != after and after != '0' and egress == EXPECTED_EGRESS, main_pid_changed=before != after, egress=egress)

    fault = 'table inet wc_fault { chain output { type filter hook output priority -50; policy accept; oifname "wan0" ip daddr ' + ENDPOINT + ' udp dport ' + str(PORT) + ' counter drop; } }'
    subprocess.run(['/usr/sbin/nft', '-f', '-'], input=fault, text=True, check=True, capture_output=True)
    try:
        ok, errors = blocked(address)
        note('vps_endpoint_blocked_offline', ok, probes=errors)
    finally:
        run('/usr/sbin/nft', 'delete', 'table', 'inet', 'wc_fault', check=False)
    address, egress = healthy()
    note('endpoint_recovery', egress == EXPECTED_EGRESS, egress=egress)

    run('systemctl', 'stop', 'nftables.service')
    ok, errors = blocked(address)
    note('firewall_service_stopped_still_closed', ok and policies() and Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() == '0', probes=errors)
    run('systemctl', 'start', 'nftables.service')
    run('systemctl', 'start', 'networking.service')
    run('systemctl', 'start', 'windowsclaude-dhcp.service')
    run('systemctl', 'start', 'sing-box.service')
    address, egress = healthy()
    note('firewall_service_recovery', egress == EXPECTED_EGRESS and policies(), egress=egress)

    try:
        socket.create_connection(('2001:4860:4860::8888', 443), timeout=2).close()
    except OSError as error:
        note('ipv6_blocked', True, error_type=type(error).__name__)
    else:
        note('ipv6_blocked', False)
    print('WC_GATEWAY_NATIVE_TESTS_DONE', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        run('systemctl', 'stop', 'sing-box.service', check=False)
        run('/usr/sbin/sysctl', '-w', 'net.ipv4.ip_forward=0', check=False)
        print('WC_RUNTIME_FAILED ' + type(error).__name__, flush=True)
        raise SystemExit(1)
