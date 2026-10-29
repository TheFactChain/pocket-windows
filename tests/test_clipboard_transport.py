"""Real .NET/Python loopback peers; no guest VM, clipboard, or NIC configuration."""
from pathlib import Path
import base64
import hashlib
import json
import os
import queue
import select
import socket
import subprocess
import tempfile
import threading
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1]
NONCE = 'a' * 32
MAX_LINE = 16 * 1024 * 1024
STATUSES = {'WAITING', 'CONNECTED', 'RECONNECTING', 'NETWORK_UNAVAILABLE', 'PROTOCOL_FAILED'}


def hello(**changes):
    value = dict(v=2, n=NONCE, t='hello', o='host', ready=True)
    value.update(changes)
    return json.dumps(value, separators=(',', ':')).encode() + b'\n'


class Harness:
    def __init__(self, exe, port, mode='idle', frames=(), ready=True, **options):
        self.process = subprocess.Popen([str(exe), 'client', str(port), NONCE], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding='utf-8', bufsize=1)
        self.events, self.backlog, self.records = queue.Queue(), [], []
        self.output = []
        self.reader = threading.Thread(target=self.read_output, daemon=True)
        self.reader.start()
        self.process.stdin.write(json.dumps(dict(mode=mode, frames=list(frames), ready=ready, **options), ensure_ascii=False) + '\n')
        self.process.stdin.flush()
        self.wait('started')
        self.stopped = None

    def read_output(self):
        for line in self.process.stdout:
            self.output.append(line)
            try: event = json.loads(line)
            except ValueError: event = dict(kind='bad_output')
            self.records.append(event)
            self.events.put(event)

    def command(self, **command):
        self.process.stdin.write(json.dumps(command) + '\n')
        self.process.stdin.flush()

    def wait(self, kind, predicate=lambda value: True, timeout=4):
        def matches(value):
            return value.get('kind') == kind and predicate(value)
        for index, event in enumerate(self.backlog):
            if matches(event): return self.backlog.pop(index)
        deadline = time.monotonic() + timeout
        while True:
            try: event = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty: raise AssertionError('Missing harness event ' + kind + ': ' + repr(self.records))
            if matches(event): return event
            self.backlog.append(event)
            if time.monotonic() >= deadline: raise AssertionError('Missing harness event ' + kind)

    def stats(self):
        self.command(op='stats')
        return self.wait('stats')

    def stop(self):
        if self.stopped is not None: return self.stopped
        if self.process.poll() is None:
            self.command(op='stop')
            self.process.stdin.close()
        try: result = self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait()
            raise AssertionError('Transport failed to stop its owned connection/reader')
        self.reader.join(timeout=2)
        error = self.process.stderr.read()
        if result != 0: raise AssertionError('Harness failed: ' + repr(self.records) + error)
        self.stopped = self.wait('stopped', timeout=1)
        for event in self.records:
            if event.get('kind') == 'status' and event['value'] not in STATUSES:
                raise AssertionError('Unsafe status value')
            if event.get('kind') == 'peer' and not event['value'] and event['readers'] != 0:
                raise AssertionError('Disconnect callback ran before the old reader stopped')
        self.process.stdout.close(); self.process.stderr.close()
        return self.stopped


class Peer:
    def __init__(self, listener):
        self.socket, _ = listener.accept()
        self.socket.settimeout(7)
        self.file = self.socket.makefile('rb')
        self.auto_pong = True
        greeting = self.line()
        value = json.loads(greeting)
        if value != dict(v=2, n=NONCE, t='hello', o='guest', ready=True):
            raise AssertionError('Invalid guest ready HELLO')

    def line(self):
        while True:
            line = self.file.readline(MAX_LINE + 2)
            if not line.endswith(b'\n'): raise AssertionError('Expected a complete peer line: ' + repr(line[:100]))
            if line == b'\n': continue
            try: value = json.loads(line)
            except ValueError: value = None
            if self.auto_pong and value == dict(v=2, n=NONCE, t='ping', o='guest'):
                self.socket.sendall(json.dumps(dict(v=2, n=NONCE, t='pong', o='host')).encode() + b'\n')
                continue
            return line[:-1]

    def authenticate(self, harness):
        self.socket.sendall(hello())
        connected = harness.wait('peer', lambda event: event['value'])
        if connected['send'] != 5000 or connected['receive'] != 0:
            raise AssertionError('Write timeout and idle receive settings must remain bounded/infinite')

    def close(self):
        self.file.close(); self.socket.close()


@unittest.skipUnless(os.name == 'nt', '.NET Framework transport regressions require Windows')
class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='clipboard-transport-build-')
        cls.exe = Path(cls.build.name) / 'ClipboardTransportRegression.exe'
        compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        result = subprocess.run([str(compiler), '/nologo', '/r:System.Web.Extensions.dll', '/out:' + str(cls.exe),
                                 str(SOURCE / 'guest/ClipboardTransport.cs'), str(SOURCE / 'tests/ClipboardTransportRegression.cs')],
                                capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode:
            cls.build.cleanup()
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(('127.0.0.1', 0)); self.listener.listen(16); self.listener.settimeout(4)
        self.port = self.listener.getsockname()[1]
        self.harnesses, self.peers = [], []

    def tearDown(self):
        try:
            for harness in self.harnesses: harness.stop()
        finally:
            for peer in self.peers: peer.close()
            self.listener.close()

    def harness(self, **options):
        harness = Harness(self.exe, self.port, **options)
        self.harnesses.append(harness)
        return harness

    def peer(self):
        peer = Peer(self.listener); self.peers.append(peer); return peer

    def test_pure_source_interface_policy_and_network_byte_order(self):
        result = subprocess.run([str(self.exe), 'metadata'], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(kind='metadata', ok=True))

    def test_network_readiness_before_connect_and_before_hello(self):
        harness = self.harness(ready=False)
        harness.wait('status', lambda event: event['value'] == 'NETWORK_UNAVAILABLE')
        self.assertEqual(harness.stats()['factories'], 0)
        self.assertFalse(select.select([self.listener], [], [], 0.1)[0])
        harness.command(op='ready', value=True)
        peer = self.peer(); peer.authenticate(harness)
        harness.stop(); peer.close(); self.peers.remove(peer)
        blocked = self.harness(fail_before_hello=True)
        connection, _ = self.listener.accept(); connection.settimeout(2)
        try: self.assertEqual(connection.recv(1), b'')
        finally: connection.close()
        stats = blocked.stats()
        self.assertEqual(stats['outgoingCalls'], 0)
        self.assertEqual(stats['peers'], 0)

    def test_no_provider_or_delivery_before_authenticated_host_hello(self):
        outgoing = '{"v":2,"d":"protected"}'
        harness = self.harness(frames=[outgoing])
        peer = self.peer()
        stats = harness.stats()
        self.assertEqual((stats['outgoingCalls'], stats['received']), (0, 0))
        self.assertFalse(select.select([peer.socket], [], [], 0.15)[0])
        peer.authenticate(harness)
        self.assertEqual(peer.line().decode(), outgoing)

    def test_old_character_stream_frames_are_discarded_before_fresh_host_hello(self):
        harness = self.harness()
        peer = self.peer()
        old = json.dumps(dict(v=2, n=NONCE, t='text', s=50, o='host', z=0, d='x' * 32768)).encode()
        peer.socket.sendall(b'\n' + old + b'\n\n' + hello())
        harness.wait('peer', lambda event: event['value'])
        self.assertEqual(harness.stats()['received'], 0)
        value = b'{"d":"only after fresh authentication"}'
        peer.socket.sendall(value + b'\n')
        self.assertEqual(harness.wait('received')['sha256'], hashlib.sha256(value).hexdigest())

    def test_host_heartbeat_and_repeated_hello_do_not_reach_clipboard_provider(self):
        harness = self.harness()
        peer = self.peer(); peer.authenticate(harness)
        pong = json.dumps(dict(v=2, n=NONCE, t='pong', o='host')).encode() + b'\n'
        value = b'{"d":"one actual clipboard frame"}'
        peer.socket.sendall(b'\n' + pong + hello() + value + b'\n')
        harness.wait('received', lambda event: event['sha256'] == hashlib.sha256(value).hexdigest())
        self.assertEqual(harness.stats()['received'], 1)

    def test_silent_character_backend_forces_reconnect_without_tcp_eof(self):
        harness = self.harness()
        peer = self.peer(); peer.authenticate(harness); peer.auto_pong = False
        self.assertTrue(select.select([peer.socket], [], [], 3)[0], 'Guest must test the character backend, not just TCP')
        self.assertEqual(json.loads(peer.line()), dict(v=2, n=NONCE, t='ping', o='guest'))
        harness.wait('peer', lambda event: not event['value'], timeout=18)
        harness.wait('status', lambda event: event['value'] == 'RECONNECTING')
        self.assertEqual(harness.stop()['peakReaders'], 1)

    def test_foreign_premature_deep_malformed_and_oversized_hello_rejected(self):
        values = [hello(n='b' * 32), b'{"t":"text","d":"premature"}\n', hello(ready=False),
                  b'{"v":2,"nested":' + b'[' * 30 + b'0' + b']' * 30 + b'}\n',
                  b'{"d":"\xff"}\n', hello()[:-1] + b' ' * 4096 + b'\n']
        for value in values:
            with self.subTest(value=value[:90]):
                harness = self.harness(frames=['{"d":"never released"}'])
                peer = self.peer()
                peer.socket.sendall(value)
                self.assertEqual(peer.socket.recv(1), b'')
                harness.wait('status', lambda event: event['value'] == 'PROTOCOL_FAILED')
                stopped = harness.stop()
                self.assertEqual((stopped['outgoingCalls'], stopped['received'], stopped['peers']), (0, 0, 0))

    def test_hello_has_absolute_five_second_deadline(self):
        harness = self.harness()
        peer = self.peer()
        before = time.monotonic()
        while True:
            peer.socket.sendall(b' ')
            if select.select([peer.socket], [], [], 0.9)[0]: break
            self.assertLess(time.monotonic() - before, 6.5)
        self.assertEqual(peer.socket.recv(1), b'')
        self.assertLess(time.monotonic() - before, 6.5)
        harness.wait('status', lambda event: event['value'] == 'PROTOCOL_FAILED')
        self.assertEqual(harness.stop()['outgoingCalls'], 0)

    def test_exact_byte_boundary_and_hello_coalesced_with_first_frame(self):
        harness = self.harness(mode='echo')
        peer = self.peer()
        value = b'{"d":"' + b'x' * (MAX_LINE - 8) + b'"}'
        self.assertEqual(len(value), MAX_LINE)
        peer.socket.sendall(hello() + value + b'\n')
        harness.wait('peer', lambda event: event['value'])
        received = harness.wait('received')
        self.assertEqual(received['sha256'], hashlib.sha256(value).hexdigest())
        self.assertEqual(peer.line(), value)

    def test_two_way_large_frames_partial_utf8_and_idle_connection(self):
        harness = self.harness(mode='echo')
        peer = self.peer(); peer.authenticate(harness)
        values = [json.dumps(dict(d='Привет 😀 é 🐱'), ensure_ascii=False).encode(),
                  json.dumps(dict(d='x' * (256 * 1024))).encode(),
                  json.dumps(dict(id='a' * 32, event='data', offset=0, data=base64.b64encode(os.urandom(65536)).decode())).encode()]
        for index, value in enumerate(values):
            if index == 0:
                for byte in value + b'\n': peer.socket.sendall(bytes([byte]))
            else: peer.socket.sendall(value + b'\n')
            received = harness.wait('received')
            self.assertEqual(received['sha256'], hashlib.sha256(value).hexdigest())
            self.assertEqual(received['bytes'], len(value))
            self.assertEqual(peer.line(), value)
        time.sleep(1.3)
        self.assertEqual(harness.stats()['factories'], 1)
        peer.socket.sendall(b'{"d":"still connected"}\n')
        self.assertEqual(peer.line(), b'{"d":"still connected"}')
        self.assertEqual(harness.stop()['peakReaders'], 1)

    def test_incoming_utf8_byte_limit_invalid_utf8_and_partial_eof(self):
        values = [('{"d":"' + 'é' * (MAX_LINE // 2 + 1) + '"}\n').encode(), b'{"d":"\xf0\x9f"}\n', b'{"d":"unfinished"']
        for value in values:
            with self.subTest(size=len(value)):
                harness = self.harness()
                peer = self.peer(); peer.authenticate(harness)
                try: peer.socket.sendall(value)
                except (BrokenPipeError, ConnectionResetError): pass
                if not value.endswith(b'\n'): peer.socket.shutdown(socket.SHUT_WR)
                harness.wait('peer', lambda event: not event['value'])
                harness.wait('status', lambda event: event['value'] == 'PROTOCOL_FAILED')
                self.assertEqual(harness.stop()['received'], 0)

    def test_outgoing_byte_limit_and_embedded_newline_rejected_before_write(self):
        for value in ('{"d":"' + 'é' * (MAX_LINE // 2 + 1) + '"}', '{"d":"bad\nline"}'):
            with self.subTest(chars=len(value)):
                harness = self.harness(frames=[value])
                peer = self.peer(); peer.authenticate(harness)
                self.assertEqual(peer.socket.recv(1), b'')
                harness.wait('status', lambda event: event['value'] == 'PROTOCOL_FAILED')
                stopped = harness.stop()
                self.assertEqual(stopped['received'], 0)
                self.assertGreater(stopped['outgoingCalls'], 0)

    def test_reconnect_resends_provider_state_without_overlapping_readers(self):
        value = '{"d":"same pending value"}'
        harness = self.harness(mode='resend', frames=[value])
        for index in range(12):
            peer = self.peer(); peer.authenticate(harness)
            self.assertEqual(peer.line().decode(), value)
            peer.close(); self.peers.remove(peer)
            disconnected = harness.wait('peer', lambda event: not event['value'])
            self.assertEqual(disconnected['readers'], 0)
            stats = harness.stats()
            self.assertLessEqual(stats['peakReaders'], 1)
            self.assertLess(stats['threadGrowth'], 8)
        stopped = harness.stop()
        self.assertEqual(stopped['peers'], 12)
        self.assertEqual(stopped['losses'], 12)
        self.assertEqual(stopped['readers'], 0)

    def test_callback_exception_does_not_leak_content_and_stop_releases_idle_reader(self):
        harness = self.harness(mode='throw_receive')
        peer = self.peer(); peer.authenticate(harness)
        peer.socket.sendall(b'{"d":"callback fixture"}\n')
        harness.wait('peer', lambda event: not event['value'])
        harness.stop()
        self.assertNotIn('raw-fixture-MUST_NOT_BE_LOGGED', ''.join(harness.output))
        idle = self.harness()
        peer2 = self.peer(); peer2.authenticate(idle)
        before = time.monotonic(); stopped = idle.stop()
        self.assertLess(time.monotonic() - before, 2)
        self.assertEqual((stopped['readers'], stopped['peers'], stopped['losses']), (0, 1, 1))
        self.assertEqual(peer2.socket.recv(1), b'')


if __name__ == '__main__':
    unittest.main()
