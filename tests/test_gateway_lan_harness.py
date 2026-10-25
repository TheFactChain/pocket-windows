import io
from pathlib import Path
import socket
import struct
import ssl
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance_gateway_lan as harness


class GatewayLanHarnessTests(unittest.TestCase):
    def test_large_post_echo_requires_exact_nonce_payload_and_length(self):
        nonce = 'a' * 32
        payload = (nonce * 2048).encode('ascii')
        for length, body, accepted in ((65536, payload, True), (65535, payload, False),
                                       (65536, payload[:-1], False), (65536, b'b' * 65536, False)):
            handler = object.__new__(harness.TlsHandler)
            handler.server = mock.Mock(nonce=nonce, valid_requests=0, large_requests=0)
            handler.rfile = io.BytesIO(('POST /probe/' + nonce + ' HTTP/1.0\r\nContent-Length: '
                                       + str(length) + '\r\n\r\n').encode('ascii') + body)
            handler.wfile = io.BytesIO()
            with self.subTest(length=length, body_length=len(body)):
                handler.handle()
                self.assertEqual(handler.server.large_requests, int(accepted))
                self.assertEqual(handler.server.valid_requests, int(accepted))
                self.assertEqual(handler.wfile.getvalue().endswith(b'WCPROBE ' + nonce.encode() + b'\n' + payload), accepted)

    def test_temporary_certificate_authenticates_only_fixture_hostname(self):
        with tempfile.TemporaryDirectory(prefix='lan-tls-fixture-', dir=harness.ROOT / 'state') as name:
            directory = Path(name).resolve()
            self.assertTrue(directory.is_relative_to(harness.ROOT / 'state'))
            harness.make_certificate(directory)
            with harness.TlsFixture(('127.0.0.1', 0), harness.TlsHandler) as server:
                server.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                server.context.load_cert_chain(directory / 'probe-cert.pem', directory / 'probe-key.pem')
                server.nonce, server.valid_requests = 'a' * 32, 0
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    client.load_verify_locations(cafile=directory / 'probe-cert.pem')
                    client.verify_flags |= ssl.VERIFY_X509_STRICT
                    with socket.create_connection(('127.0.0.1', server.server_address[1]), timeout=5) as raw:
                        with client.wrap_socket(raw, server_hostname=harness.HOSTNAME) as secure:
                            secure.sendall(('GET /probe/' + server.nonce + ' HTTP/1.0\r\n\r\n').encode())
                            chunks = []
                            while chunk := secure.recv(4096):
                                chunks.append(chunk)
                            self.assertTrue(b''.join(chunks).endswith(('WCPROBE ' + server.nonce + '\n').encode()))
                    self.assertEqual(server.valid_requests, 1)
                    with socket.create_connection(('127.0.0.1', server.server_address[1]), timeout=5) as raw, \
                         self.assertRaises(ssl.SSLCertVerificationError):
                        client.wrap_socket(raw, server_hostname='wrong.wcp.test')
                finally:
                    server.shutdown()
                    worker.join(timeout=5)

    def test_missing_qmp_stops_only_identical_disposable_peer(self):
        process = mock.Mock(pid=123, poll=mock.Mock(return_value=None))
        identity = {'pid': 123, 'created': 456, 'path': 'fixture-qemu'}
        observed = {}
        with mock.patch.object(harness.runtime, 'process_identity', return_value=identity), \
             mock.patch.object(harness.runtime, 'same_identity', return_value=True):
            harness.stop_peer(process, None, identity, False, observed)
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=20)
        self.assertTrue(observed['probe_forced_stop'])
        process.reset_mock()
        process.wait.side_effect = harness.subprocess.TimeoutExpired('fixture', 2)
        with mock.patch.object(harness.runtime, 'process_identity', return_value=None), \
             mock.patch.object(harness.runtime, 'same_identity', return_value=False), \
             self.assertRaises(RuntimeError):
            harness.stop_peer(process, None, identity, False, {})
        process.terminate.assert_not_called()

    def test_peer_exit_during_native_identity_check_never_terminates_foreign_pid(self):
        process = mock.Mock(pid=123, poll=mock.Mock(return_value=None), wait=mock.Mock(return_value=1))
        with mock.patch.object(harness.runtime, 'process_identity', return_value=None):
            harness.stop_peer(process, None, None, False, {})
        process.wait.assert_called_once_with(timeout=2)
        process.terminate.assert_not_called()

    def test_capture_is_installed_before_any_guest_nic_is_added(self):
        qmp, status = mock.Mock(), mock.Mock(firewall_ready=True, network_attached=False, failed=False)
        path = harness.ROOT / 'logs/gateway-lan-fixture/wan.pcap'
        harness.attach_capture(harness.gateway_controller.attach_network, qmp, status, 33861, path)
        calls = qmp.command.call_args_list
        capture = next(index for index, call in enumerate(calls)
                       if call.args[0] == 'object-add' and call.args[1]['netdev'] == 'wcwan')
        first_device = next(index for index, call in enumerate(calls) if call.args[0] == 'device_add')
        self.assertLess(capture, first_device)
        self.assertEqual(calls[capture].args[1]['file'], 'logs/gateway-lan-fixture/wan.pcap')

    def test_proxy_maps_only_exact_fixture_and_public_ipv4_https(self):
        handler = object.__new__(harness.ProbeProxyHandler)
        handler.server = mock.Mock(fixture_connects=0, fixture4_connects=0, fixture_port=12345)
        self.assertEqual(handler.destination(b'[2001:db8:77::2]:443'), ('127.0.0.1', 12345))
        self.assertEqual(handler.server.fixture_connects, 1)
        self.assertEqual(handler.destination(b'198.51.100.77:443'), ('127.0.0.1', 12345))
        self.assertEqual(handler.server.fixture4_connects, 1)
        self.assertEqual(handler.destination(b'1.1.1.1:443'), ('1.1.1.1', 443))
        for value in (b'127.0.0.1:443', b'10.0.2.2:443', b'example.com:443',
                      b'[::1]:443', b'[2001:db8:77::2]:444', b'1.1.1.1:80'):
            with self.subTest(value=value), self.assertRaises((ValueError, UnicodeError)):
                handler.destination(value)

    def capture(self, packets):
        header = b'\xd4\xc3\xb2\xa1' + struct.pack('<HHIIII', 2, 4, 0, 0, 65536, 1)
        return header + b''.join(struct.pack('<IIII', 0, 0, len(p), len(p)) + p for p in packets)

    def packet(self, destination='10.0.2.2', dport=12345, protocol=6):
        ip = bytearray(20)
        ip[0], ip[9] = 0x45, protocol
        ip[12:16], ip[16:20] = socket.inet_aton('10.0.2.15'), socket.inet_aton(destination)
        return bytes(12) + b'\x08\x00' + ip + struct.pack('!HH', 49152, dport) + bytes(16)

    def test_capture_requires_positive_proxy_evidence_and_rejects_bypass(self):
        good = self.packet()
        path = mock.Mock()
        with mock.patch.object(path, 'open', return_value=io.BytesIO(self.capture([good]))):
            self.assertEqual(harness.inspect_wan(path, 12345)['proxy_ipv4_packets'], 1)
        bad_packets = ([], [self.packet('1.1.1.1', 443)], [self.packet(dport=53, protocol=17)],
                       [bytes(12) + b'\x86\xdd' + bytes(60)])
        for packets in bad_packets:
            with self.subTest(packets=packets), \
                 mock.patch.object(path, 'open', return_value=io.BytesIO(self.capture(packets))), \
                 self.assertRaises(ValueError):
                harness.inspect_wan(path, 12345)
        with mock.patch.object(path, 'open', return_value=io.BytesIO(self.capture([good])[:-1])), \
             self.assertRaises(ValueError):
            harness.inspect_wan(path, 12345)

    def test_probe_has_only_owned_linux_overlay_public_carrier_and_isolated_lan(self):
        directory = harness.ROOT / 'logs' / 'gateway-lan-acceptance-fixture'
        carrier = directory / 'probe.tar'
        args = harness.probe_arguments(directory, carrier, 33861)
        self.assertEqual(args[args.index('-display') + 1], 'none')
        self.assertIn('virtio-net-pci,netdev=wclan,mac=52:54:00:77:00:03', args)
        self.assertEqual(args.count('-netdev'), 1)
        self.assertFalse(any('user,' in value or 'workvm' in value or 'password' in value for value in args))
        self.assertIn('"read-only": true', args[args.index('-blockdev') + 1])
        block = harness.json.loads(args[args.index('-blockdev') + 1])
        self.assertEqual(block['file']['filename'], 'logs/gateway-lan-acceptance-fixture/probe.tar')
        self.assertIn('-no-reboot', args)


if __name__ == '__main__':
    unittest.main()
