"""Pure unit checks: no guest, native directory operations, network or build execution."""
import importlib.util
import io
import json
from pathlib import Path
import ssl
import stat
import struct
import sys
import tarfile
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load('acceptance_probe', 'gateway/acceptance/probe.py')
CONFIG = {'schema': 1, 'nonce': 'a' * 32, 'target': '2001:db8:77::2', 'hostname': 'probe.wcp.test'}
CA = b'-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n'


def carrier(members=None):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name, content, kind in members or [('probe.json', json.dumps(CONFIG).encode(), tarfile.REGTYPE),
                                              ('ca.pem', CA, tarfile.REGTYPE)]:
            entry = tarfile.TarInfo(name)
            entry.size, entry.type = len(content), kind
            archive.addfile(entry, io.BytesIO(content))
    return data.getvalue()


class CarrierTests(unittest.TestCase):
    def read(self, data):
        with patch.object(probe.ssl, 'SSLContext') as factory:
            result = probe.read_carrier(io.BytesIO(data))
            factory.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
            factory.return_value.load_verify_locations.assert_called_once_with(cadata=CA.decode())
            return result

    def test_config_and_exclusive_ca(self):
        self.assertEqual(self.read(carrier())[0], CONFIG)

    def test_reject_member_types_names_duplicates_and_sizes(self):
        for member in [('extra', b'x', tarfile.REGTYPE), ('../ca.pem', CA, tarfile.REGTYPE),
                       ('ca.pem', CA, tarfile.REGTYPE), ('link', b'', tarfile.SYMTYPE),
                       ('ca.pem', b'x' * 16385, tarfile.REGTYPE)]:
            with self.subTest(member=member[0]), self.assertRaises(ValueError):
                self.read(carrier([('probe.json', json.dumps(CONFIG).encode(), tarfile.REGTYPE),
                                   ('ca.pem', CA, tarfile.REGTYPE), member]))

    def test_reject_json_values_and_duplicate_keys(self):
        values = [dict(CONFIG, schema=True), dict(CONFIG, nonce='a' * 31),
                  dict(CONFIG, target='::1'), dict(CONFIG, hostname='example.com'),
                  dict(CONFIG, extra=1), []]
        payloads = [json.dumps(value).encode() for value in values]
        payloads.append(json.dumps(CONFIG).replace('"schema": 1', '"schema": 1, "schema": 1').encode())
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.read(carrier([('probe.json', payload, tarfile.REGTYPE), ('ca.pem', CA, tarfile.REGTYPE)]))

    def test_reject_private_key_trailing_data_and_oversize(self):
        bad_ca = CA + b'PRIVATE KEY'
        cases = [carrier() + b'x' * 512, carrier()[:-512] + b'x' * 512, b'x' * 65537,
                 carrier([('probe.json', json.dumps(CONFIG).encode(), tarfile.REGTYPE),
                          ('ca.pem', bad_ca, tarfile.REGTYPE)])]
        for data in cases:
            with self.assertRaises(ValueError):
                self.read(data)

    def test_invalid_certificate_is_not_accepted(self):
        with self.assertRaises(ssl.SSLError):
            probe.read_carrier(io.BytesIO(carrier()))


class NetworkTests(unittest.TestCase):
    def test_network_setup_adds_only_static_ipv4_fixture_and_preserves_ipv6_ra(self):
        nic = MagicMock()
        nic.name = 'ens3'
        (nic / 'address').read_text.return_value = probe.MAC
        with patch.object(probe, 'Path') as path, patch.object(probe, 'command') as command:
            path.return_value.iterdir.return_value = [nic]
            self.assertEqual(probe.setup_network(), 'ens3')
        calls = [call.args for call in command.call_args_list]
        self.assertIn('net.ipv6.conf.ens3.accept_ra=1', calls[0])
        self.assertIn('net.ipv6.conf.ens3.autoconf=1', calls[0])
        self.assertEqual(calls[-2], ('/usr/sbin/ip', '-4', 'addr', 'add', '192.168.233.3/24', 'dev', 'ens3'))
        self.assertEqual(calls[-1], ('/usr/sbin/ip', '-4', 'route', 'add', 'default', 'via', '192.168.233.1', 'dev', 'ens3'))
        self.assertFalse(any('-6' in call and 'add' in call for call in calls))

    def test_ra_rejects_default_routes_even_when_device_field_is_omitted(self):
        addresses = [{'addr_info': [{'family': 'inet6', 'local': 'fd77:233::1234',
                                    'prefixlen': 64, 'dynamic': True}]}]
        route = {'dst': 'default', 'protocol': 'ra', 'gateway': 'fe80::1'}
        with patch.object(probe, 'command', side_effect=[json.dumps(addresses), json.dumps([route])]) as command:
            self.assertFalse(probe.ra_ready('ens3'))
        command.assert_called_with('/usr/sbin/ip', '-j', '-6', 'route', 'show', 'default', 'dev', 'ens3')
        with patch.object(probe, 'command', side_effect=[json.dumps(addresses), json.dumps([dict(route, dev='ens4')])]):
            self.assertFalse(probe.ra_ready('ens3'))

    def test_wait_requires_dynamic_ula_without_external_ipv6_route(self):
        addr = [{'addr_info': [{'family': 'inet6', 'local': 'fd77:233::1234',
                               'prefixlen': 64, 'dynamic': True}]}]
        route = []
        with patch.object(probe, 'command', side_effect=[json.dumps(addr), json.dumps(route)]):
            self.assertTrue(probe.ra_ready('ens3'))
        for change in ({'dynamic': False}, {'tentative': True}, {'local': '2001:db8::2'}):
            candidate = [{'addr_info': [dict(addr[0]['addr_info'][0], **change)]}]
            with patch.object(probe, 'command', side_effect=[json.dumps(candidate), json.dumps(route)]):
                self.assertFalse(probe.ra_ready('ens3'))
        for protocol in ('static', 'ra'):
            route = [{'dst': 'default', 'protocol': protocol, 'dev': 'ens3', 'gateway': 'fe80::1'}]
            with patch.object(probe, 'command', side_effect=[json.dumps(addr), json.dumps(route)]):
                self.assertFalse(probe.ra_ready('ens3'))

    def test_ra_wait_never_sets_static_address(self):
        with patch.object(probe, 'ra_ready', side_effect=[False, True]) as ready:
            pause = Mock()
            probe.wait_ra('ens3', 10, clock=lambda: 1, pause=pause)
            self.assertEqual(ready.call_count, 2)
            pause.assert_called_once_with(3)
        with patch.object(probe, 'ra_ready', return_value=False), self.assertRaises(TimeoutError):
            probe.wait_ra('ens3', 1, clock=lambda: 2, pause=Mock())

    def test_dns_validates_successful_empty_aaaa_and_rejects_ipv6_answers(self):
        header = struct.pack('!6H', 123, 0x8180, 1, 0, 0, 0)
        question = b'\x07example\x03com\0' + struct.pack('!HH', 28, 1)
        answer = b'\xc0\x0c' + struct.pack('!HHIH', 28, 1, 60, 16) + bytes.fromhex('20010db8000000000000000000000002')
        data = header + question
        self.assertTrue(probe.valid_dns(data, 123))
        self.assertFalse(probe.valid_dns(struct.pack('!6H', 123, 0x8180, 1, 1, 0, 0) + question + answer, 123))
        self.assertFalse(probe.valid_dns(data, 124))
        self.assertFalse(probe.valid_dns(data[:-1], 123))
        self.assertFalse(probe.valid_dns(data[:2] + b'\x83\x80' + data[4:], 123))
        self.assertFalse(probe.valid_dns(header + question.replace(b'example', b'invalid'), 123))

    def test_dns_a_and_aaaa_use_distinct_gateway_sockets_and_record_types(self):
        for family, resolver, qtype, address in (
                (probe.socket.AF_INET, '192.168.233.1', 1, bytes((198, 51, 100, 77))),
                (probe.socket.AF_INET6, 'fd77:233::1', 28, bytes.fromhex('20010db8000000000000000000000002'))):
            header = struct.pack('!6H', 123, 0x8180, 1, int(qtype == 1), 0, 0)
            question = b'\x07example\x03com\0' + struct.pack('!HH', qtype, 1)
            answer = b'\xc0\x0c' + struct.pack('!HHIH', qtype, 1, 60, len(address)) + address
            data = header + question + (answer if qtype == 1 else b'')
            client = MagicMock()
            client.__enter__.return_value = client
            client.recv.return_value = data
            with self.subTest(family=family), patch.object(probe.socket, 'socket', return_value=client) as factory, \
                 patch.object(probe.os, 'urandom', return_value=b'\x00{'), patch.object(probe, 'remaining', return_value=2):
                self.assertTrue(probe.dns_probe(10, family))
            factory.assert_called_once_with(family, probe.socket.SOCK_DGRAM)
            client.connect.assert_called_once_with((resolver, 53))
            self.assertEqual(client.send.call_args.args[0][-4:], struct.pack('!HH', qtype, 1))
            self.assertFalse(probe.valid_dns(data, 123, 1 if qtype == 28 else 28))
            self.assertFalse(probe.valid_dns(data[:-1], 123, qtype))
            wrong_size = struct.pack('!6H', 123, 0x8180, 1, 1, 0, 0) + question + b'\xc0\x0c' + struct.pack('!HHIH', qtype, 1, 60, len(address) - 1) + address[:-1]
            self.assertFalse(probe.valid_dns(wrong_size, 123, qtype))

    def test_tls_uses_literal_ips_supplied_ca_and_exact_large_echo_response(self):
        payload = b'a' * 65536
        body = b'WCPROBE ' + b'a' * 32 + b'\n' + payload
        responses = [(b'HTTP/1.0 200 OK\r\n\r\n' + body, True),
                     (b'HTTP/1.1 200 OK\r\nContent-Length: 65577\r\n\r\n' + body, True),
                     (b'HTTP/1.0 403 Forbidden\r\n\r\n' + body, False),
                     (b'HTTP/1.0 200 OK\r\n\r\nWCPROBE ' + b'b' * 32 + b'\n', False),
                     (b'HTTP/1.0 200 OK\r\n\r\n' + body + b'extra', False),
                     (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n' + body, False),
                     (b'x' * 73770, False),
                     (b'HTTP/1.0 200 OK\r\n\r\n' + body[:-1], False)]
        for family, target in ((probe.socket.AF_INET6, CONFIG['target']), (probe.socket.AF_INET, '198.51.100.77')):
            for response, expected in responses:
                raw, secure, context = MagicMock(), MagicMock(), Mock()
                raw.__enter__.return_value = raw
                secure.__enter__.return_value = secure
                context.wrap_socket.return_value = secure
                secure.recv.side_effect = [response, b'']
                with self.subTest(target=target, response=response[:40]), \
                     patch.object(probe.socket, 'socket', return_value=raw) as factory, patch.object(probe, 'remaining', return_value=2):
                    self.assertEqual(probe.tls_probe(CONFIG, context, 10, family), expected)
                factory.assert_called_once_with(family, probe.socket.SOCK_STREAM)
                raw.connect.assert_called_once_with((target, 443))
                context.wrap_socket.assert_called_once_with(raw, server_hostname='probe.wcp.test')
                request = secure.sendall.call_args.args[0]
                self.assertIn(b'POST /probe/' + b'a' * 32, request)
                self.assertIn(b'Content-Length: 65536\r\n', request)
                self.assertTrue(request.endswith(payload))

    def test_state_requires_ipv4_and_local_dns_with_external_ipv6_blocked(self):
        # DNS6 means a valid empty AAAA response; TLS6 must fail in every online state.
        samples = [(True, True, True, True), (False, False, True, True),
                   (True, False, True, True), (False, False, True, True),
                   (True, True, False, False), (True, False, False, True),
                   (False, False, False, False), (True, True, False, False),
                   (True, False, True, True)]
        with patch.object(probe, 'dns_probe', side_effect=[pair[index] for pair in samples for index in (0, 2)]) as dns, \
             patch.object(probe, 'tls_probe', side_effect=[pair[index] for pair in samples for index in (1, 3)]) as tls, \
             patch.object(probe, 'emit') as emit:
            transitions = []
            emit.side_effect = lambda config, phase: transitions.append((phase, dns.call_count))
            probe.monitor(CONFIG, None, 100, clock=lambda: 1, pause=Mock())
        self.assertEqual(dns.call_count, 2 * len(samples))
        self.assertEqual(tls.call_count, 2 * len(samples))
        self.assertEqual([call.args[-1] for call in dns.call_args_list], [probe.socket.AF_INET6, probe.socket.AF_INET] * len(samples))
        self.assertEqual([call.args[-1] for call in tls.call_args_list], [probe.socket.AF_INET6, probe.socket.AF_INET] * len(samples))
        self.assertEqual([call.args[1] for call in emit.call_args_list], ['connected', 'lost', 'recovered', 'finished'])
        self.assertEqual(transitions, [('connected', 6), ('lost', 14), ('recovered', 18), ('finished', 18)])

    def test_timeout_has_no_finished_event(self):
        with patch.object(probe, 'emit') as emit, self.assertRaises(TimeoutError):
            probe.monitor(CONFIG, None, 1, clock=lambda: 2, pause=Mock())
        emit.assert_not_called()

    def test_timeout_has_only_fixed_last_check_code(self):
        for dns, tls, expected in ((False, True, 'dns-unavailable'),
                                   (True, False, 'tls-unavailable'),
                                   (False, False, 'dns-tls-unavailable')):
            with self.subTest(expected=expected), patch.object(probe, 'dns_probe', return_value=dns), \
                 patch.object(probe, 'tls_probe', side_effect=lambda *args: tls if args[-1] == probe.socket.AF_INET else False), patch.object(probe, 'emit'), \
                 self.assertRaises(TimeoutError) as error:
                ticks = iter((1, 1, 1, 1, 1, 1, 2, 10))
                probe.monitor(CONFIG, None, 5, clock=lambda: next(ticks), pause=Mock())
            self.assertEqual(error.exception.args, (expected,))


class ShutdownTests(unittest.TestCase):
    def test_fixture_failure_powers_off_without_leaking_exception(self):
        with patch.object(probe, 'Path') as path, patch.object(probe, 'emit') as emit, \
             patch.object(probe, 'command') as command:
            path.return_value.stat.side_effect = OSError('PRIVATE fixture error text')
            self.assertEqual(probe.main(), 1)
        emit.assert_called_once_with(None, 'failed', 'carrier-invalid')
        command.assert_called_once_with('/usr/bin/systemctl', '--no-block', 'poweroff')

    def test_ra_timeout_powers_off_with_nonce_and_fixed_error(self):
        def readonly(fd, operation, output):
            output[:] = struct.pack('=I', 1)

        stream = Mock()
        opened = Mock()
        opened.__enter__ = Mock(return_value=stream)
        opened.__exit__ = Mock(return_value=False)
        with patch.object(probe, 'Path') as path, patch.object(probe, 'RUN'), \
             patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(ioctl=readonly)}), \
             patch.object(probe, 'read_carrier', return_value=(CONFIG, CA.decode(), Mock())), \
             patch.object(probe, 'setup_network', return_value='ens3'), \
             patch.object(probe, 'wait_ra', side_effect=TimeoutError('PRIVATE network error text')), \
             patch.object(probe, 'monitor') as monitor, patch.object(probe, 'emit') as emit, \
             patch.object(probe, 'command') as command:
            path.return_value.stat.return_value.st_mode = stat.S_IFBLK
            path.return_value.open.return_value = opened
            self.assertEqual(probe.main(), 1)
        monitor.assert_not_called()
        emit.assert_called_once_with(CONFIG, 'failed', 'ra-not-ready')
        command.assert_called_once_with('/usr/bin/systemctl', '--no-block', 'poweroff')

    def test_monitor_routes_only_ipv6_fixture_cleans_up_and_does_not_echo_exception(self):
        def readonly(fd, operation, output):
            output[:] = struct.pack('=I', 1)

        for message, expected in ((None, None), ('dns-unavailable', 'dns-unavailable'),
                                  ('tls-unavailable', 'tls-unavailable'),
                                  ('dns-tls-unavailable', 'dns-tls-unavailable'),
                                  ('PRIVATE diagnostic details', 'connection-probes-failed')):
            opened, stream = Mock(), Mock()
            opened.__enter__ = Mock(return_value=stream)
            opened.__exit__ = Mock(return_value=False)
            with self.subTest(message=message), patch.object(probe, 'Path') as path, patch.object(probe, 'RUN'), \
                 patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(ioctl=readonly)}), \
                 patch.object(probe, 'read_carrier', return_value=(CONFIG, CA.decode(), Mock())), \
                 patch.object(probe, 'setup_network', return_value='ens3'), patch.object(probe, 'wait_ra'), \
                 patch.object(probe, 'monitor', side_effect=TimeoutError(message) if message else None), \
                 patch.object(probe, 'emit') as emit, patch.object(probe, 'command') as command:
                path.return_value.stat.return_value.st_mode = stat.S_IFBLK
                path.return_value.open.return_value = opened
                self.assertEqual(probe.main(), int(message is not None))
            if message:
                emit.assert_called_once_with(CONFIG, 'failed', expected)
            else:
                emit.assert_not_called()
            self.assertEqual([call.args for call in command.call_args_list], [
                ('/usr/sbin/ip', '-6', 'route', 'add', '2001:db8:77::2/128',
                 'via', 'fd77:233::1', 'dev', 'ens3'),
                ('/usr/sbin/ip', '-6', 'route', 'del', '2001:db8:77::2/128',
                 'via', 'fd77:233::1', 'dev', 'ens3'),
                ('/usr/bin/systemctl', '--no-block', 'poweroff')])


class BuilderTests(unittest.TestCase):
    def setUp(self):
        # Loading the real builder would initialize platform-specific modules. Its interface is enough here.
        self.fake = types.ModuleType('build_gateway')
        self.fake.ROOT = ROOT
        self.fake.GATEWAY_VERSION = 'production-v1'
        self.fake.write_delivery = Mock(return_value={'sha256': 'verified'})
        self.fake._write_bytes = Mock()
        self.fake.install_gateway = Mock(return_value={'phase': 'installed', 'operation': str(ROOT / 'state/gateway-build/install-unit'), 'disk_bytes': 42})
        self.fake._hash = Mock(return_value='b' * 64)
        self.fake.runtime = types.SimpleNamespace(atomic_json=Mock())
        with patch.dict(sys.modules, {'build_gateway': self.fake}):
            self.helper = load('build_gateway_probe_unit', 'scripts/build_gateway_probe.py')

    def test_override_restored_even_after_failure(self):
        writer = self.fake.write_delivery
        with self.assertRaises(RuntimeError):
            with self.helper.acceptance_delivery():
                self.assertEqual(self.fake.GATEWAY_VERSION, 'acceptance-probe-v1')
                self.assertIsNot(self.fake.write_delivery, writer)
                raise RuntimeError('test')
        self.assertIs(self.fake.write_delivery, writer)
        self.assertEqual(self.fake.GATEWAY_VERSION, 'production-v1')

    def test_reuses_install_pipeline_and_restores(self):
        writer = self.fake.write_delivery
        self.assertEqual(self.helper.install_probe()['phase'], 'installed')
        self.fake.install_gateway.assert_called_once_with()
        self.assertIs(self.fake.write_delivery, writer)
        report = self.fake.runtime.atomic_json.call_args.args[1]
        self.assertEqual(report['version'], 'acceptance-probe-v1')
        self.assertEqual(report['disk_sha256'], 'b' * 64)
        self.assertEqual(report['virtual_bytes'], 4 * 1024**3)

    def test_delivery_substitutes_only_copy_and_preserves_hash_writer(self):
        writer = self.fake.write_delivery
        files = {'install.sh': Path('source-install.sh'), 'session.py': Path('source-session.py'),
                 'wc-session.service': Path('source-service'), 'sing-box': Path('verified-binary')}
        source = b"printf '%s' 'production-v1'\nprintf 'WC_BUILD_COMPLETE production-v1\\n' > /dev/ttyS0\n"
        with self.helper.acceptance_delivery(), patch.object(Path, 'read_bytes', side_effect=[source, b'# probe installer']):
            result = self.fake.write_delivery(files, Path('operation/delivery.tar'))
        self.assertEqual(result, {'sha256': 'verified'})
        payload = self.fake._write_bytes.call_args.args[1]
        self.assertIn(b'# probe installer\nprintf', payload)
        self.assertNotIn(b'production-v1', payload)
        delivered = writer.call_args.args[0]
        self.assertEqual(delivered['install.sh'], Path('operation/acceptance-install.sh'))
        self.assertEqual(delivered['session.py'], ROOT / 'gateway/acceptance/probe.py')
        self.assertEqual(files['install.sh'], Path('source-install.sh'))


if __name__ == '__main__':
    unittest.main()
