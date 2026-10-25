#!/usr/bin/python3
"""Disposable IPv4-egress/local-IPv6 client; config and public CA live only in /run."""
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import struct
import subprocess
import tarfile
import time

MAC = '52:54:00:77:00:03'
RUN = Path('/run/wc-probe')
PREFIX = ipaddress.IPv6Network('fd77:233::/64')
IPV4_TARGET = '198.51.100.77'


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('config')
        value[key] = item
    return value


def read_carrier(stream):
    data = stream.read(65537)
    if len(data) > 65536 or len(data) % 512:
        raise ValueError('carrier')
    files, offset = {}, 0
    while offset + 512 <= len(data) and data[offset:offset + 512] != bytes(512):
        member = tarfile.TarInfo.frombuf(data[offset:offset + 512], 'utf-8', 'strict')
        limit = {'probe.json': 1024, 'ca.pem': 16384}.get(member.name)
        if (not limit or member.name in files or member.type != tarfile.REGTYPE
                or not 0 < member.size <= limit or member.linkname):
            raise ValueError('carrier')
        offset += 512
        end = offset + member.size
        padded = offset + ((member.size + 511) // 512) * 512
        if padded > len(data) or any(data[end:padded]):
            raise ValueError('carrier')
        files[member.name] = data[offset:end]
        offset = padded
    if (set(files) != {'probe.json', 'ca.pem'} or len(data) - offset < 1024
            or any(data[offset:])):
        raise ValueError('carrier')
    config = json.loads(files['probe.json'], object_pairs_hook=unique_object)
    if (not isinstance(config, dict) or set(config) != {'schema', 'nonce', 'target', 'hostname'}
            or type(config['schema']) is not int or config['schema'] != 1
            or not isinstance(config['nonce'], str) or not re.fullmatch('[0-9a-f]{32}', config['nonce'])
            or config['target'] != '2001:db8:77::2' or config['hostname'] != 'probe.wcp.test'):
        raise ValueError('config')
    ca = files['ca.pem'].decode('ascii')
    if ('PRIVATE KEY' in ca or ca.count('-----BEGIN CERTIFICATE-----') != 1
            or not ca.strip().startswith('-----BEGIN CERTIFICATE-----')
            or not ca.strip().endswith('-----END CERTIFICATE-----')):
        raise ValueError('ca')
    # No system roots: only the supplied public acceptance CA can authenticate the fixture.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=ca)
    return config, ca, context


def emit(config, phase, code=None):
    value = {'phase': phase}
    if config:
        value['nonce'] = config['nonce']
    if code:
        value['code'] = code
    with open('/dev/ttyS0', 'w') as output:
        output.write(json.dumps(value, separators=(',', ':')) + '\n')
        output.flush()


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=3).stdout


def setup_network():
    nics = [path for path in Path('/sys/class/net').iterdir() if path.name != 'lo']
    if len(nics) != 1 or (nics[0] / 'address').read_text().strip().lower() != MAC:
        raise ValueError('nic')
    nic = nics[0].name
    if not re.fullmatch('[a-zA-Z0-9_-]{1,15}', nic):
        raise ValueError('nic')
    command('/usr/sbin/sysctl', '-w', 'net.ipv6.conf.all.disable_ipv6=0',
            'net.ipv6.conf.default.disable_ipv6=0', 'net.ipv6.conf.all.forwarding=0',
            'net.ipv6.conf.default.forwarding=0', 'net.ipv6.conf.all.accept_ra=1',
            'net.ipv6.conf.default.accept_ra=1', f'net.ipv6.conf.{nic}.disable_ipv6=0',
            f'net.ipv6.conf.{nic}.forwarding=0', f'net.ipv6.conf.{nic}.accept_ra=1',
            f'net.ipv6.conf.{nic}.autoconf=1', f'net.ipv6.conf.{nic}.accept_ra_defrtr=1',
            f'net.ipv6.conf.{nic}.accept_ra_pinfo=1')
    command('/usr/sbin/ip', 'link', 'set', 'dev', nic, 'up')
    # This fixed acceptance fixture checks IPv4 routing, not DHCP lease acquisition.
    command('/usr/sbin/ip', '-4', 'addr', 'add', '192.168.233.3/24', 'dev', nic)
    command('/usr/sbin/ip', '-4', 'route', 'add', 'default', 'via', '192.168.233.1', 'dev', nic)
    return nic


def ra_ready(nic):
    addresses = json.loads(command('/usr/sbin/ip', '-j', '-6', 'addr', 'show', 'dev', nic))
    address_ready = any(
        item.get('family') == 'inet6' and ipaddress.IPv6Address(item['local']) in PREFIX
        and 'tentative' not in item.get('flags', []) and not item.get('tentative')
        and not item.get('dadfailed') and 'dadfailed' not in item.get('flags', [])
        and item.get('prefixlen') == 64 and item.get('dynamic') is True
        for link in addresses for item in link.get('addr_info', []))
    routes = json.loads(command('/usr/sbin/ip', '-j', '-6', 'route', 'show', 'default', 'dev', nic))
    return address_ready and not routes


def wait_ra(nic, deadline, clock=time.monotonic, pause=time.sleep):
    while clock() < deadline:
        if ra_ready(nic):
            return
        pause(min(3, max(0, deadline - clock())))
    raise TimeoutError('ra')


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError('timeout')
    return min(2, value)


def dns_name(data, offset):
    seen, labels, end = set(), [], None
    while True:
        if offset in seen or offset >= len(data) or len(seen) > 128:
            raise ValueError('dns')
        seen.add(offset)
        length = data[offset]
        offset += 1
        if length & 0xc0 == 0xc0:
            if offset >= len(data):
                raise ValueError('dns')
            pointer = ((length & 63) << 8) | data[offset]
            end = offset + 1 if end is None else end
            offset = pointer
        elif length == 0:
            return b'.'.join(labels).lower(), offset if end is None else end
        elif length > 63 or offset + length > len(data):
            raise ValueError('dns')
        else:
            labels.append(data[offset:offset + length])
            offset += length


def valid_dns(data, query_id, question_type=28):
    if not 12 <= len(data) <= 4096:
        return False
    ident, flags, questions, answers, authority, additional = struct.unpack('!6H', data[:12])
    if ident != query_id or flags & 0xfa0f != 0x8000 or questions != 1 or not 0 <= answers <= 64:
        return False
    name, offset = dns_name(data, 12)
    if name != b'example.com' or data[offset:offset + 4] != struct.pack('!HH', question_type, 1):
        return False
    offset += 4
    if question_type == 28:
        return answers == 0
    found, names = False, {b'example.com'}
    for _ in range(answers):
        name, offset = dns_name(data, offset)
        if offset + 10 > len(data):
            return False
        kind, cls, ttl, size = struct.unpack('!HHIH', data[offset:offset + 10])
        offset += 10
        if offset + size > len(data):
            return False
        if kind == 5 and cls == 1 and name in names:
            alias, end = dns_name(data, offset)
            if end != offset + size:
                return False
            names.add(alias)
        if kind == question_type and cls == 1 and size == (4 if question_type == 1 else 16) and name in names:
            found = True
        offset += size
    return found


def dns_probe(deadline, family=socket.AF_INET6):
    question_type = 1 if family == socket.AF_INET else 28
    resolver = '192.168.233.1' if family == socket.AF_INET else 'fd77:233::1'
    query_id = int.from_bytes(os.urandom(2), 'big')
    query = struct.pack('!6H', query_id, 0x100, 1, 0, 0, 0) + b'\x07example\x03com\0' + struct.pack('!HH', question_type, 1)
    with socket.socket(family, socket.SOCK_DGRAM) as client:
        client.settimeout(remaining(deadline))
        client.connect((resolver, 53))
        client.send(query)
        return valid_dns(client.recv(4097), query_id, question_type)


def tls_probe(config, context, deadline, family=socket.AF_INET6):
    target = IPV4_TARGET if family == socket.AF_INET else config['target']
    payload = (config['nonce'] * 2048).encode('ascii')
    expected_body = ('WCPROBE ' + config['nonce'] + '\n').encode('ascii') + payload
    limit = 8192 + len(expected_body)
    with socket.socket(family, socket.SOCK_STREAM) as raw:
        raw.settimeout(remaining(deadline))
        raw.connect((target, 443))
        with context.wrap_socket(raw, server_hostname=config['hostname']) as secure:
            secure.settimeout(remaining(deadline))
            secure.sendall(('POST /probe/' + config['nonce'] + ' HTTP/1.0\r\nHost: '
                            + config['hostname'] + '\r\nContent-Length: 65536\r\n'
                            + 'Connection: close\r\n\r\n').encode('ascii') + payload)
            response = bytearray()
            while len(response) <= limit:
                secure.settimeout(remaining(deadline))
                part = secure.recv(16384)
                if not part:
                    break
                response.extend(part)
            if len(response) > limit:
                return False
    headers, separator, body = bytes(response).partition(b'\r\n\r\n')
    if not separator or len(headers) > 8192:
        return False
    lines = headers.split(b'\r\n')
    if lines[0].split()[:2] not in ([b'HTTP/1.0', b'200'], [b'HTTP/1.1', b'200']):
        return False
    if any(line.lower().startswith(b'transfer-encoding:') for line in lines[1:]):
        return False
    return body == expected_body


def checked(function, *args):
    try:
        return bool(function(*args))
    except (OSError, ValueError, TimeoutError):
        return False


def monitor(config, context, deadline, clock=time.monotonic, pause=time.sleep):
    phase = 'waiting'
    last_check_code = 'dns-tls-unavailable'
    while clock() < deadline:
        # IPv6 DNS stays local and empty AAAA is healthy; external IPv6 TLS must fail.
        dns6 = checked(dns_probe, min(deadline, clock() + 4), socket.AF_INET6)
        tls6 = checked(tls_probe, config, context, min(deadline, clock() + 6), socket.AF_INET6)
        dns4 = checked(dns_probe, min(deadline, clock() + 4), socket.AF_INET)
        tls4 = checked(tls_probe, config, context, min(deadline, clock() + 6), socket.AF_INET)
        dns, tls = dns6 and dns4, not tls6 and tls4
        last_check_code = ('connection-probes-failed' if dns and tls else
                           'tls-unavailable' if dns else
                           'dns-unavailable' if tls else 'dns-tls-unavailable')
        if clock() >= deadline:
            break
        if dns and tls and phase == 'waiting':
            emit(config, 'connected')
            phase = 'connected'
        elif not any((dns6, tls6, dns4, tls4)) and phase == 'connected':
            emit(config, 'lost')
            phase = 'lost'
        elif dns and tls and phase == 'lost':
            emit(config, 'recovered')
            emit(config, 'finished')
            return
        pause(min(3, max(0, deadline - clock())))
    raise TimeoutError(last_check_code)


def main():
    config = None
    result = 0
    failure_code = 'carrier-invalid'
    try:
        deadline = time.monotonic() + 180
        carrier = Path('/dev/disk/by-id/virtio-WCPROBE')
        if not stat.S_ISBLK(carrier.stat().st_mode):
            raise ValueError('carrier')
        with carrier.open('rb') as stream:
            import fcntl
            readonly = bytearray(4)
            fcntl.ioctl(stream.fileno(), 0x125e, readonly)  # BLKROGET
            if struct.unpack('=I', readonly)[0] != 1:
                raise ValueError('carrier')
            config, ca, context = read_carrier(stream)
        RUN.mkdir(mode=0o700)
        (RUN / 'probe.json').write_text(json.dumps(config), encoding='ascii')
        (RUN / 'ca.pem').write_text(ca, encoding='ascii')
        failure_code = 'network-setup-failed'
        nic = setup_network()
        failure_code = 'ra-not-ready'
        wait_ra(nic, min(deadline, time.monotonic() + 60))
        failure_code = 'connection-probes-failed'
        # Exercise the gateway's IPv6 rejection despite having no IPv6 default route.
        fixture_route = ('2001:db8:77::2/128', 'via', 'fd77:233::1', 'dev', nic)
        command('/usr/sbin/ip', '-6', 'route', 'add', *fixture_route)
        try:
            monitor(config, context, deadline)
        finally:
            command('/usr/sbin/ip', '-6', 'route', 'del', *fixture_route)
    except Exception as error:
        if (failure_code == 'connection-probes-failed' and isinstance(error, TimeoutError)
                and len(error.args) == 1 and type(error.args[0]) is str
                and error.args[0] in ('dns-unavailable', 'tls-unavailable', 'dns-tls-unavailable')):
            failure_code = error.args[0]
        emit(config, 'failed', failure_code)
        result = 1
    finally:
        try:
            command('/usr/bin/systemctl', '--no-block', 'poweroff')
        except Exception:
            emit(config, 'failed', 'poweroff-failed')
            result = 1
    return result


if __name__ == '__main__':
    raise SystemExit(main())
