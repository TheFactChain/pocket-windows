"""File clipboard integration with owned files and a fake native clipboard."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import clipboard_bridge as bridge
from clipboard_files import Sender
from clipboard_stage import Cache

NONCE = 'c' * 32


class Native:
    def __init__(self):
        self.busy = False
        self.files = []
        self.after_set = lambda: None

    def set_files(self, paths, guard):
        if self.busy or not guard():
            return False
        self.files.append(list(paths))
        self.after_set()
        return True

    def set_text(self, text, guard):
        return not self.busy and guard()

    def stop(self):
        pass


class FileBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='clipboard-bridge-files-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'Копия.txt'
        self.source.write_bytes(b'payload\0' * 20000)
        self.parent = self.root / 'private'
        self.parent.mkdir()
        self.native = Native()
        self.bridge = bridge.Bridge(0, NONCE, self.native,
                                   cache=Cache(self.parent, boot_id='20261027000000.000001Z'))
        self.addCleanup(self.bridge.stop)

    def transfer(self, sequence=10, identifier='d' * 32):
        replies = []
        for body in Sender([self.source], identifier).frames():
            reply = self.bridge.receive(bridge.packet(NONCE, 'files', s=sequence, o='guest', b=body))
            if reply:
                replies.append(json.loads(reply))
        return replies

    def test_received_files_wait_for_native_success_and_survive_stop(self):
        replies = self.transfer()
        self.assertEqual(replies[-1]['t'], 'stored')
        self.assertFalse(any(item['t'] == 'ack' and item.get('applied') for item in replies))
        marker = self.bridge.incoming['ticket'].marker
        self.assertTrue(marker.exists())
        self.native.busy = True
        self.assertIsNone(self.bridge.apply_incoming())
        self.assertEqual(self.native.files, [])
        self.native.busy = False
        reply = json.loads(self.bridge.apply_incoming())
        self.assertTrue(reply['applied'])
        target = Path(self.native.files[-1][0])
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        self.bridge.stop()
        self.assertTrue(marker.exists())
        self.assertTrue(target.exists())

    def test_native_success_racing_a_new_copy_retains_paste_source(self):
        self.transfer()
        self.native.after_set = lambda: self.bridge.local_change('newer local')
        self.assertTrue(json.loads(self.bridge.apply_incoming())['applied'])
        self.assertEqual(bridge.decode(self.bridge.pending, NONCE), 'newer local')
        self.assertTrue(Path(self.native.files[-1][0]).exists())

    def test_newer_private_copy_aborts_only_unpublished_files(self):
        self.transfer()
        stage = self.bridge.incoming['ticket'].stage
        self.bridge.local_change(None)
        self.bridge.apply_incoming()
        self.assertFalse(stage.exists())
        self.assertEqual(self.native.files, [])

    def test_newer_remote_text_that_is_busy_prevents_older_files_from_applying(self):
        self.native.busy = True
        self.assertFalse(json.loads(self.bridge.receive(bridge.encode(NONCE, 'newest', 20)))['ok'])
        replies = self.transfer(sequence=19)
        self.assertTrue(all(item['t'] == 'ack' and not item['ok'] for item in replies))
        self.native.busy = False
        self.assertIsNone(self.bridge.apply_incoming())
        self.assertEqual(self.native.files, [])

    def test_sender_streams_bounded_frames_then_waits_for_native_ack(self):
        self.bridge.io_thread = threading.Thread(target=self.bridge.file_io)
        self.bridge.io_thread.start()
        self.bridge.local_change([str(self.source)])
        copy = self.bridge.pending
        frames = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            line = self.bridge.next_outgoing()
            if line:
                message = json.loads(line)
                frames.append(message)
                if message.get('b', {}).get('event') == 'end':
                    break
            time.sleep(.005)
        self.assertEqual(frames[0]['b']['event'], 'begin')
        self.assertEqual(frames[-1]['b']['event'], 'end')
        self.bridge.receive(bridge.packet(NONCE, 'stored', s=copy['version'][0], o='host', i=copy['id']))
        self.assertIs(self.bridge.pending, copy)
        self.assertTrue(copy['stored'])
        self.bridge.receive(bridge.packet(NONCE, 'ack', s=copy['version'][0], o='host', i='e' * 32, ok=True, applied=True))
        self.assertIs(self.bridge.pending, copy)
        self.bridge.receive(bridge.packet(NONCE, 'ack', s=copy['version'][0], o='host', i=copy['id'], ok=True, applied=True))
        self.assertIsNone(self.bridge.pending)
        self.assertIsNotNone(self.bridge.last_transfer_utc)


if __name__ == '__main__':
    unittest.main()
