import base64
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest import mock
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import clipboard_bridge as bridge

NONCE = 'c' * 32


class FakeClipboard:
    def __init__(self):
        self.written, self.callback = [], None

    def start(self, callback):
        self.callback = callback

    def stop(self):
        pass

    def set_text(self, text, guard):
        if not guard():
            return False
        self.written.append(text)
        return True


class Clock:
    def __init__(self):
        self.now = 0.


class TimedPeer:
    def __init__(self, clock, arrivals=(), *, stopping=None, stop_at=None):
        self.clock, self.arrivals = clock, list(arrivals)
        self.stopping, self.stop_at = stopping, stop_at
        self.timeout, self.timeouts = 0, []

    def settimeout(self, timeout):
        self.timeout = timeout
        self.timeouts.append(timeout)

    def recv(self, limit):
        end = self.clock.now + self.timeout
        if self.stop_at is not None and self.stop_at <= end:
            self.clock.now = self.stop_at
            self.stop_at = None
            self.stopping.set()
            raise socket.timeout()
        if self.arrivals and self.arrivals[0][0] <= end:
            when, value = self.arrivals.pop(0)
            self.clock.now = max(self.clock.now, when)
            if len(value) > limit:
                self.arrivals.insert(0, (when, value[limit:]))
            return value[:limit]
        self.clock.now = end
        raise socket.timeout()


class ColdHandshakeTests(unittest.TestCase):
    def setUp(self):
        self.clipboard = FakeClipboard()
        self.bridge = bridge.Bridge(0, NONCE, self.clipboard)
        self.addCleanup(self.bridge.stop)
        self.clock = Clock()

    def authenticate(self, peer):
        with mock.patch.object(bridge.time, 'monotonic', side_effect=lambda: self.clock.now):
            return self.bridge.authenticate(peer)

    def test_qemu_may_open_the_channel_before_windows_is_ready(self):
        peer = TimedPeer(self.clock, [(3., bridge.packet(NONCE, 'hello', o='guest', ready=True))])
        self.assertEqual(self.authenticate(peer), b'')
        self.assertEqual(self.clock.now, 3.)
        self.assertEqual(self.clipboard.written, [])

    def test_fragment_deadline_starts_with_the_first_byte_not_the_socket(self):
        hello = bridge.packet(NONCE, 'hello', o='guest', ready=True)
        peer = TimedPeer(self.clock, [(1.9, hello[:3]), (3.5, hello[3:])])
        self.assertEqual(self.authenticate(peer), b'')
        self.assertEqual(self.clock.now, 3.5)

    def test_fragment_deadline_is_absolute_even_when_bytes_keep_arriving(self):
        peer = TimedPeer(self.clock, [(3., b'{'), (3.7, b'"v":'), (4.4, b'2'), (5.1, b',')])
        with self.assertRaises(socket.timeout):
            self.authenticate(peer)
        self.assertAlmostEqual(self.clock.now, 5.)

    def test_empty_prefix_does_not_extend_the_fragment_deadline(self):
        peer = TimedPeer(self.clock, [(3., b'\n'), (4., b'\n'), (4.9, b'\n'), (5.1, bridge.packet(NONCE, 'hello', o='guest', ready=True))])
        with self.assertRaises(socket.timeout):
            self.authenticate(peer)
        self.assertAlmostEqual(self.clock.now, 5.)

    def test_one_empty_prefix_before_hello_is_accepted(self):
        peer = TimedPeer(self.clock, [(3., b'\n'), (3.1, bridge.packet(NONCE, 'hello', o='guest', ready=True))])
        self.assertEqual(self.authenticate(peer), b'')

    def test_large_pipelined_copy_does_not_count_against_the_hello_limit(self):
        hello = bridge.packet(NONCE, 'hello', o='guest', ready=True)
        text = bridge.packet(NONCE, 'text', s=1, o='guest', z=0, d=base64.b64encode(b'x' * 10000).decode())
        peer = TimedPeer(self.clock, [(0., hello + text)])
        self.assertEqual(self.authenticate(peer), (hello + text)[len(hello):4097])
        self.assertEqual(self.clipboard.written, [])

    def test_idle_socket_has_a_finite_cold_boot_budget(self):
        with self.assertRaises(socket.timeout):
            self.authenticate(TimedPeer(self.clock))
        self.assertAlmostEqual(self.clock.now, 300.)

    def test_stop_cancels_an_idle_handshake(self):
        peer = TimedPeer(self.clock, stopping=self.bridge.stopping, stop_at=.7)
        self.assertIsNone(self.authenticate(peer))
        self.assertLessEqual(self.clock.now, .9)

    def test_stop_cancels_a_partial_handshake(self):
        peer = TimedPeer(self.clock, [(.1, b'{')], stopping=self.bridge.stopping, stop_at=.4)
        self.assertIsNone(self.authenticate(peer))
        self.assertLessEqual(self.clock.now, .6)

    def test_oversized_or_unauthenticated_hello_never_releases_data(self):
        values = [b'x' * 4097, b'{}\n', b'broken\n', bridge.packet('d' * 32, 'hello', o='guest', ready=True),
                  bridge.packet(NONCE, 'hello', o='host', ready=True), bridge.packet(NONCE, 'hello', o='guest', ready=1)]
        for value in values:
            with self.subTest(value=value[:30]):
                self.clock.now = 0.
                self.bridge.local_change('private pending')
                self.assertIsNone(self.authenticate(TimedPeer(self.clock, [(0., value)])))
                self.assertEqual(self.clipboard.written, [])


class RepeatedHelloTests(unittest.TestCase):
    def setUp(self):
        self.clipboard = FakeClipboard()
        self.bridge = bridge.Bridge(0, NONCE, self.clipboard)
        self.addCleanup(self.bridge.stop)

    def hello(self):
        return self.bridge.receive(bridge.packet(NONCE, 'hello', o='guest', ready=True))

    def test_repeated_hello_replies_before_reoffering_the_latest_text(self):
        self.bridge.local_change('old')
        self.bridge.next_outgoing()
        self.bridge.local_change('latest')
        self.bridge.next_outgoing()
        self.bridge.control, self.bridge.cancel = b'stale control', b'stale cancel'
        epoch = self.bridge.epoch
        reply = self.hello()
        self.assertEqual(reply, b'\n' + bridge.packet(NONCE, 'hello', o='host', ready=True))
        self.assertEqual(self.bridge.epoch, epoch + 1)
        self.assertEqual(bridge.decode(self.bridge.next_outgoing(), NONCE), 'latest')

    def test_repeated_hello_discards_only_the_unpublished_receiver(self):
        receiver = mock.Mock()
        self.bridge.incoming = {'receiver': receiver, 'ticket': None}
        self.hello()
        self.assertIsNone(self.bridge.incoming)
        receiver.close.assert_called_once_with()

    def test_repeated_hello_aborts_a_complete_but_unpublished_ticket(self):
        ticket = mock.Mock(parked=False)
        self.bridge.incoming = {'receiver': mock.Mock(), 'ticket': ticket}
        self.hello()
        self.assertIsNone(self.bridge.incoming)
        ticket.abort.assert_called_once_with()

    def test_fresh_offer_after_hello_is_not_blocked_by_an_abandoned_file_version(self):
        self.bridge.incoming = {'receiver': mock.Mock(), 'ticket': None}
        self.bridge.latest_seen = ((10, 'guest'), 'files', 'd' * 32)
        self.bridge.clock = 10
        self.hello()
        reply = self.bridge.receive(bridge.packet(NONCE, 'skip', s=2, o='guest'))
        self.assertTrue(json.loads(reply)['ok'])
        self.assertEqual(self.bridge.version, (2, 'guest'))
        self.assertEqual(self.bridge.clock, 10)

    def test_committed_skip_is_replayed_without_changing_clipboard_or_file_paths(self):
        paths = [r'C:\owned-qa\retained-paste-source.txt']
        self.bridge.local_change(paths)
        reply = self.bridge.receive(bridge.packet(NONCE, 'skip', s=10, o='guest'))
        self.assertFalse(json.loads(reply)['applied'])
        self.assertIsNone(self.bridge.pending)
        self.hello()
        replay = self.bridge.next_outgoing()
        self.assertIsNotNone(replay, 'A restarted helper needs the committed sequence watermark')
        self.assertEqual(json.loads(replay), dict(v=2, n=NONCE, t='skip', s=10, o='guest'))
        self.assertEqual((self.bridge.clock, self.bridge.version), (10, (10, 'guest')))
        self.assertEqual(self.bridge.snapshot_paths, paths)
        self.assertEqual(self.clipboard.written, [])
        self.assertIsNone(self.bridge.last_transfer_utc)
        self.bridge.receive(bridge.packet(NONCE, 'ack', s=10, o='guest', ok=True))
        self.assertIsNone(self.bridge.pending)
        reply = self.bridge.receive(bridge.encode(NONCE, 'fresh after the watermark', 11, 'guest'))
        self.assertTrue(json.loads(reply)['applied'])
        self.assertEqual(self.clipboard.written, ['fresh after the watermark'])
        self.assertEqual(self.bridge.version, (11, 'guest'))

    def test_published_file_paths_survive_repeated_hello_and_stop(self):
        native = mock.Mock()
        native.set_files.side_effect = lambda roots, guard: guard()
        self.bridge.clipboard = native
        ticket = mock.Mock(roots=[r'C:\owned-qa\paste-source.txt'])
        self.bridge.incoming = {'receiver': mock.Mock(), 'ticket': ticket, 'cancelled': False,
                                'version': (10, 'guest'), 'id': 'd' * 32}
        self.assertTrue(json.loads(self.bridge.apply_incoming())['applied'])
        self.hello()
        self.assertEqual(self.bridge.pending['paths'], ticket.roots)
        self.assertFalse(self.bridge.pending['stored'])
        self.bridge.stop()
        ticket.park.assert_called_once_with()
        ticket.abort.assert_not_called()

    def test_repeated_hello_invalidates_queued_file_frames(self):
        self.bridge.local_change([r'C:\owned-qa\copy.txt'])
        copy = self.bridge.current
        copy['stored'] = True
        self.bridge.pending = None
        self.bridge.source_frames.put_nowait((copy, self.bridge.epoch, b'stale file frame'))
        self.hello()
        self.assertIs(self.bridge.pending, copy)
        self.assertFalse(copy['stored'])
        self.assertIsNone(self.bridge.next_outgoing())

    def test_malformed_or_foreign_hello_cannot_reset_the_stream(self):
        self.bridge.local_change('retained')
        self.bridge.pending = None
        epoch = self.bridge.epoch
        invalid = [bridge.packet('d' * 32, 'hello', o='guest', ready=True),
                   bridge.packet(NONCE, 'hello', o='host', ready=True),
                   bridge.packet(NONCE, 'hello', o='guest'), bridge.packet(NONCE, 'hello', o='guest', ready=1),
                   bridge.packet(NONCE, 'hello', o='guest', ready=False), bridge.packet(NONCE, 'hello', o='guest', ready=True, extra=1)]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(self.bridge.receive(value))
                self.assertIsNone(self.bridge.pending)
                self.assertEqual(self.bridge.epoch, epoch)

    def test_ping_is_strict_and_does_not_confirm_or_reset_a_copy(self):
        self.bridge.local_change('retained')
        pending, epoch, version = self.bridge.pending, self.bridge.epoch, self.bridge.version
        self.assertEqual(self.bridge.receive(bridge.packet(NONCE, 'ping', o='guest')),
                         bridge.packet(NONCE, 'pong', o='host'))
        self.assertIs(self.bridge.pending, pending)
        self.assertEqual(self.bridge.epoch, epoch)
        self.assertEqual(self.bridge.version, version)
        self.assertIsNone(self.bridge.last_transfer_utc)
        invalid = [bridge.packet('d' * 32, 'ping', o='guest'), bridge.packet(NONCE, 'ping', o='host'),
                   bridge.packet(NONCE, 'ping', o='guest', s=1), bridge.packet(NONCE, 'pong', o='guest')]
        for value in invalid:
            self.assertIsNone(self.bridge.receive(value))
        self.assertEqual(self.clipboard.written, [])

    def test_ping_does_not_disturb_an_incoming_file_or_its_controls(self):
        receiver = mock.Mock()
        incoming = {'receiver': receiver, 'ticket': None}
        self.bridge.incoming = incoming
        self.bridge.control, self.bridge.cancel = b'control', b'cancel'
        self.bridge.last_transfer_utc = 'earlier transfer'
        self.bridge.receive(bridge.packet(NONCE, 'ping', o='guest'))
        self.assertIs(self.bridge.incoming, incoming)
        self.assertEqual((self.bridge.control, self.bridge.cancel), (b'control', b'cancel'))
        self.assertEqual(self.bridge.last_transfer_utc, 'earlier transfer')
        receiver.close.assert_not_called()


def wait_for(condition, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


class ProtocolTests(unittest.TestCase):
    def test_round_trip_keeps_any_text(self):
        for text in ('hello', 'Привет, мир', 'emoji \U0001F600\r\n\ttabs', 'x' * 100000):
            self.assertEqual(bridge.decode(bridge.encode(NONCE, text).rstrip(b'\n'), NONCE), text)

    def test_empty_and_oversized_copies_are_not_sent(self):
        self.assertIsNone(bridge.encode(NONCE, ''))
        self.assertIsNone(bridge.encode(NONCE, 'y' * (bridge.MAX_CHARS + 1)))

    def test_only_valid_lines_of_this_session_are_accepted(self):
        line = bridge.encode(NONCE, 'secret').rstrip(b'\n')
        self.assertIsNone(bridge.decode(line, 'd' * 32))
        for bad in (b'', b'not json', b'[]', b'{}', json.dumps({'v': 3, 'n': NONCE, 't': 'text', 'z': 0, 'd': 'aGk='}).encode(),
                    json.dumps({'v': 1, 'n': NONCE, 't': 'image', 'z': 0, 'd': 'aGk='}).encode(),
                    json.dumps({'v': 1, 'n': NONCE, 't': 'text', 'z': 0, 'd': '!!!'}).encode(),
                    json.dumps({'v': 1, 'n': NONCE, 't': 'text', 'z': 0, 'd': base64.b64encode(b'\xff').decode()}).encode()):
            self.assertIsNone(bridge.decode(bad, NONCE), bad)
        self.assertEqual(bridge.decode(bridge.packet(NONCE, 'text', s=1, o='guest', z=0, d='aGk='), NONCE), 'hi')

    def test_decompression_bomb_is_refused(self):
        packer = zlib.compressobj(9, zlib.DEFLATED, -15)
        packed = packer.compress(b'\0' * (8 * 1024 * 1024)) + packer.flush()
        line = bridge.packet(NONCE, 'text', s=1, o='guest', z=1, d=base64.b64encode(packed).decode())
        self.assertLess(len(line), 100000)
        self.assertIsNone(bridge.decode(line, NONCE))

    def test_qemu_forwards_only_the_explicit_guest_clipboard_endpoint(self):
        self.assertEqual(bridge.network_forward(4242), 'guestfwd=tcp:192.168.234.4:27844-tcp:127.0.0.1:4242,,reconnect-ms=1000')
        for invalid in (True, 0, 65536, '4242'):
            with self.assertRaises(ValueError):
                bridge.network_forward(invalid)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.clipboard = FakeClipboard()
        self.bridge = bridge.Bridge(0, NONCE, self.clipboard)
        self.bridge.start()
        try:
            self.guest = socket.create_connection(('127.0.0.1', self.bridge.port), timeout=3)
            self.guest.sendall(bridge.packet(NONCE, 'hello', o='guest', ready=True))
        except Exception:
            self.bridge.stop()
            raise
        self.guest.settimeout(3)
        self.buffer = b''
        self.assertEqual(self.receive()['t'], 'hello')

    def receive(self):
        for _ in range(3):
            while b'\n' not in self.buffer:
                self.buffer += self.guest.recv(65536)
            line, self.buffer = self.buffer.split(b'\n', 1)
            if line:
                return json.loads(line)
        self.fail('Unexpected repeated empty frames')

    def tearDown(self):
        self.bridge.stop()
        self.guest.close()

    def test_copy_on_this_pc_reaches_the_guest_once(self):
        self.clipboard.callback('копия')
        message = self.receive()
        self.assertEqual(bridge.decode(json.dumps(message).encode(), NONCE), 'копия')
        self.guest.sendall(bridge.packet(NONCE, 'ack', s=message['s'], o=message['o'], ok=True))
        self.guest.settimeout(0.4)
        with self.assertRaises(socket.timeout):
            self.guest.recv(65536)

    def test_copy_in_the_guest_is_written_and_not_sent_back(self):
        self.guest.sendall(bridge.encode(NONCE, 'из гостя'))
        self.assertTrue(wait_for(lambda: self.clipboard.written == ['из гостя']))
        self.assertEqual(self.receive()['t'], 'ack')
        self.guest.settimeout(0.5)
        with self.assertRaises(socket.timeout):
            self.guest.recv(65536)

    def test_foreign_and_broken_lines_leave_the_clipboard_alone(self):
        deep = b'[' * 10000 + b'0' + b']' * 10000 + b'\n'
        self.guest.sendall(bridge.encode('e' * 32, 'foreign') + b'garbage\n' + deep + bridge.encode(NONCE, 'ok'))
        self.assertTrue(wait_for(lambda: self.clipboard.written == ['ok']))
        self.assertTrue(self.bridge.thread.is_alive())

    def test_the_same_text_may_be_copied_again_after_the_other_side_changed_it(self):
        self.clipboard.callback('A')
        self.receive()
        self.guest.sendall(bridge.encode(NONCE, 'B', 2))
        self.assertTrue(wait_for(lambda: self.clipboard.written == ['B']))
        self.assertEqual(self.receive()['t'], 'ack')
        self.clipboard.callback('A')  # the user copies A again
        self.assertEqual(bridge.decode(json.dumps(self.receive()).encode(), NONCE), 'A')

    def test_repeated_hello_on_the_same_socket_precedes_the_latest_offer(self):
        self.clipboard.callback('latest')
        message = self.receive()
        self.guest.sendall(bridge.packet(NONCE, 'ack', s=message['s'], o=message['o'], ok=True))
        self.assertTrue(wait_for(lambda: self.bridge.pending is None))
        self.guest.sendall(b'\n' + bridge.packet(NONCE, 'hello', o='guest', ready=True))
        self.assertEqual(self.receive()['t'], 'hello')
        self.assertEqual(bridge.decode(json.dumps(self.receive()).encode(), NONCE), 'latest')

    def test_ping_is_answered_on_the_existing_socket(self):
        self.guest.sendall(bridge.packet(NONCE, 'ping', o='guest'))
        self.assertEqual(self.receive(), dict(v=2, n=NONCE, t='pong', o='host'))
        self.assertIsNone(self.bridge.last_transfer_utc)


class ListenerHandshakeTests(unittest.TestCase):
    def setUp(self):
        self.clipboard = FakeClipboard()
        self.bridge = bridge.Bridge(0, NONCE, self.clipboard)
        self.bridge.start()
        self.addCleanup(self.bridge.stop)

    def client(self):
        connection = socket.create_connection(('127.0.0.1', self.bridge.port), timeout=3)
        self.addCleanup(connection.close)
        return connection

    def test_listener_retains_its_port_before_qemu_can_use_the_forward(self):
        with socket.socket() as other, self.assertRaises(OSError):
            other.bind(('127.0.0.1', self.bridge.port))

    def test_foreign_handshake_cannot_receive_the_pending_clipboard(self):
        self.clipboard.callback('private pending text')
        client = self.client()
        client.sendall(bridge.packet('d' * 32, 'hello', o='guest', ready=True))
        self.assertEqual(client.recv(4096), b'')
        self.assertEqual(self.clipboard.written, [])

    def test_a_guest_with_a_failed_native_listener_is_visible_as_failed(self):
        client = self.client()
        client.sendall(bridge.packet(NONCE, 'hello', o='guest', ready=False))
        self.assertEqual(client.recv(4096), b'')
        self.assertEqual(self.bridge.public()['code'], 'GUEST_CLIPBOARD_UNAVAILABLE')

    def test_pipelined_hello_and_first_copy_are_processed_without_another_read(self):
        client = self.client()
        client.sendall(bridge.packet(NONCE, 'hello', o='guest', ready=True) + bridge.encode(NONCE, 'first pipelined copy'))
        self.assertTrue(wait_for(lambda: self.clipboard.written == ['first pipelined copy']))
        self.assertEqual(self.bridge.public()['status'], 'ready')

    def test_initial_host_hello_starts_a_new_line_boundary(self):
        client = self.client()
        client.sendall(bridge.packet(NONCE, 'hello', o='guest', ready=True))
        self.assertEqual(client.recv(1), b'\n')


if __name__ == '__main__':
    unittest.main()
