import base64
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
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

    def set_text(self, text):
        self.written.append(text)


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
        for bad in (b'', b'not json', b'[]', b'{}', json.dumps({'v': 2, 'n': NONCE, 't': 'text', 'z': 0, 'd': 'aGk='}).encode(),
                    json.dumps({'v': 1, 'n': NONCE, 't': 'image', 'z': 0, 'd': 'aGk='}).encode(),
                    json.dumps({'v': 1, 'n': NONCE, 't': 'text', 'z': 0, 'd': '!!!'}).encode(),
                    json.dumps({'v': 1, 'n': NONCE, 't': 'text', 'z': 0, 'd': base64.b64encode(b'\xff').decode()}).encode()):
            self.assertIsNone(bridge.decode(bad, NONCE), bad)
        self.assertEqual(bridge.decode(json.dumps({'v': 1, 'n': NONCE, 't': 'text', 'z': 0, 'd': 'aGk='}).encode(), NONCE), 'hi')

    def test_decompression_bomb_is_refused(self):
        packer = zlib.compressobj(9, zlib.DEFLATED, -15)
        packed = packer.compress(b'\0' * (8 * 1024 * 1024)) + packer.flush()
        line = json.dumps({'v': 1, 'n': NONCE, 't': 'text', 'z': 1, 'd': base64.b64encode(packed).decode()}).encode()
        self.assertLess(len(line), 100000)
        self.assertIsNone(bridge.decode(line, NONCE))

    def test_qemu_arguments_use_a_private_irq_and_loopback(self):
        args = bridge.arguments(4242)
        self.assertIn('socket,id=wcclip,host=127.0.0.1,port=4242,server=on,wait=off', args)
        self.assertIn('isa-serial,index=2,iobase=0x3e8,irq=5,chardev=wcclip', args)
        port = bridge.reserve_port()
        self.assertTrue(1024 <= port <= 65535)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(1)
        self.clipboard = FakeClipboard()
        self.bridge = bridge.Bridge(self.server.getsockname()[1], NONCE, self.clipboard)
        self.bridge.start()
        self.guest, _ = self.server.accept()
        self.guest.settimeout(3)

    def tearDown(self):
        self.bridge.stop()
        self.guest.close()
        self.server.close()

    def test_copy_on_this_pc_reaches_the_guest_once(self):
        self.clipboard.callback('копия')
        self.clipboard.callback('копия')
        data = b''
        while b'\n' not in data:
            data += self.guest.recv(65536)
        self.assertEqual(bridge.decode(data.split(b'\n')[0], NONCE), 'копия')
        self.guest.settimeout(0.4)
        with self.assertRaises(socket.timeout):
            self.guest.recv(65536)

    def test_copy_in_the_guest_is_written_and_not_sent_back(self):
        self.guest.sendall(bridge.encode(NONCE, 'из гостя'))
        self.assertTrue(wait_for(lambda: self.clipboard.written == ['из гостя']))
        self.clipboard.callback('из гостя')  # the change caused by our own write
        self.guest.settimeout(0.5)
        with self.assertRaises(socket.timeout):
            self.guest.recv(65536)

    def test_foreign_and_broken_lines_leave_the_clipboard_alone(self):
        self.guest.sendall(bridge.encode('e' * 32, 'foreign') + b'garbage\n' + bridge.encode(NONCE, 'ok'))
        self.assertTrue(wait_for(lambda: self.clipboard.written == ['ok']))

    def test_the_same_text_may_be_copied_again_after_the_other_side_changed_it(self):
        self.clipboard.callback('A')
        self.guest.recv(65536)
        self.guest.sendall(bridge.encode(NONCE, 'B'))
        self.assertTrue(wait_for(lambda: self.clipboard.written == ['B']))
        self.clipboard.callback('B')  # echo of the write
        self.clipboard.callback('A')  # the user copies A again
        data = self.guest.recv(65536)
        self.assertEqual(bridge.decode(data.split(b'\n')[0], NONCE), 'A')


if __name__ == '__main__':
    unittest.main()
