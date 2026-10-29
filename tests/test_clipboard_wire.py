"""Current Python/C# codecs over owned TCP sockets and fake clipboard publication."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'scripts'))
import clipboard_bridge
import clipboard_files
from clipboard_stage import Cache
import test_clipboard_protocol as rpc


class FakeClipboard:
    def __init__(self):
        self.gate = threading.Lock()
        self.text, self.paths, self.callback = None, None, None

    def start(self, callback):
        self.callback = callback

    def stop(self):
        pass

    def set_text(self, text, guard):
        if not guard():
            return False
        with self.gate:
            self.text, self.paths = text, None
        return True

    def set_files(self, paths, guard):
        if not guard():
            return False
        with self.gate:
            self.text, self.paths = None, list(paths)
        return True

    def snapshot(self):
        with self.gate:
            return self.text, self.paths


@unittest.skipUnless(os.name == 'nt', 'Native protocol/transport interoperability requires Windows/.NET Framework')
class WireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rpc.ProtocolTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        rpc.ProtocolTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='clipboard-wire-fixture-')
        self.root = Path(self.temporary.name)
        self.native = FakeClipboard()
        host = self.root / 'host-cache'; host.mkdir()
        guest = self.root / 'guest-cache'; guest.mkdir()
        self.bridge = clipboard_bridge.Bridge(0, rpc.NONCE, self.native, cache=Cache(host, boot_id=rpc.BOOT))
        self.harness = None
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.bridge.stop)
        self.bridge.start()
        self.harness = rpc.Harness(self.exe, guest)
        self.addCleanup(self.harness.stop)
        self.harness.request('transport-start', port=self.bridge.port)
        self.until(lambda: self.harness.request('transport-status')['connected'], 'native transport did not authenticate')

    def until(self, condition, message, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(.02)
        statuses = [row['value'] for row in self.harness.records if row.get('kind') == 'status'][-8:]
        self.fail(message + '; native statuses=' + repr(statuses) + '; host=' + repr(self.bridge.public()))

    def apply_guest(self, seconds=10):
        def apply():
            value = self.harness.request('apply', ok=True)
            return value if value['applied'] else None
        return self.until(apply, 'native protocol did not prepare a fake publication', seconds)

    @staticmethod
    def digest(path):
        with path.open('rb') as source:
            return hashlib.file_digest(source, 'sha256').hexdigest()

    def tree(self, name):
        root = self.root / name
        (root / 'Пустая папка').mkdir(parents=True)
        (root / 'empty.txt').write_bytes(b'')
        (root / 'Привет-😀.txt').write_text('Own clipboard wire fixture\nПривет 😀\n', encoding='utf-8')
        data = root / '16МБ.bin'
        with data.open('xb') as target:
            block = bytes(range(256)) * 4096
            for _ in range(16):
                target.write(block)
        Path(str(data) + ':Zone.Identifier').write_bytes(b'[ZoneTransfer]\r\nZoneId=3\r\n')
        return root

    def assert_tree(self, source, target):
        expected = {str(path.relative_to(source)): path for path in source.rglob('*')}
        actual = {str(path.relative_to(target)): path for path in target.rglob('*')}
        self.assertEqual(set(expected), set(actual))
        for name, path in expected.items():
            self.assertEqual(path.is_dir(), actual[name].is_dir(), name)
            if path.is_file():
                self.assertEqual(self.digest(path), self.digest(actual[name]), name)
                self.assertEqual(clipboard_files._zone(path), clipboard_files._zone(actual[name]), name)

    def test_unicode_and_large_text_in_both_directions_uses_real_wire(self):
        for value in ('Привет 😀\r\n\ttest', 'Большой текст 🌍 ' * 20000):
            with self.subTest(characters=len(value)):
                self.native.callback(value)
                applied = self.apply_guest()
                self.assertEqual(applied['text'], value)
                self.until(lambda: self.bridge.pending is None, 'native applied ACK did not clear the host offer')
                returning = value + ' из гостя'
                self.harness.request('local_text', text=returning)
                self.until(lambda: self.native.snapshot()[0] == returning, 'guest text did not reach fake host clipboard')
                self.until(lambda: self.harness.stats()['outgoing'] is None, 'host applied ACK did not clear native text')
        self.assertIsNotNone(self.bridge.last_transfer_utc)

    def test_new_helper_learns_committed_private_watermark_before_its_next_copy(self):
        for _ in range(10):
            self.harness.request('local_text', text=None)
        self.until(lambda: self.bridge.version == (10, 'guest'), 'private sequence watermark did not reach host')
        self.assertEqual(self.native.snapshot(), (None, None))
        self.assertIsNone(self.bridge.last_transfer_utc)
        self.harness.stop()
        self.until(lambda: self.bridge.public()['status'] == 'waiting', 'old helper socket did not close')

        parent = self.root / 'new-helper-cache'; parent.mkdir()
        self.harness = rpc.Harness(self.exe, parent)
        self.addCleanup(self.harness.stop)
        self.assertEqual(self.harness.stats()['sequence'], 0)
        acknowledged = threading.Event()
        receive = self.bridge.receive
        def observe_ack(line):
            reply = receive(line)
            message = clipboard_bridge.decode_packet(line, rpc.NONCE)
            if (message is not None and message.get('t') == 'ack' and message.get('s') == 10
                    and message.get('o') == 'guest' and message.get('ok') is True and message.get('applied', False) is False):
                acknowledged.set()
            return reply
        with mock.patch.object(self.bridge, 'receive', side_effect=observe_ack):
            self.harness.request('transport-start', port=self.bridge.port)
            self.until(acknowledged.is_set, 'new helper did not acknowledge the replayed SKIP10 watermark')
        self.assertEqual(self.native.snapshot(), (None, None))
        self.assertIsNone(self.bridge.last_transfer_utc)
        self.harness.request('local_text', text='Новая копия после перезапуска 😀')
        self.until(lambda: self.native.snapshot()[0] == 'Новая копия после перезапуска 😀', 'new helper copy was rejected after replay')
        self.assertEqual(self.bridge.version, (11, 'guest'))

    def test_initial_local_copy_before_handshake_survives_own_old_watermark(self):
        self.harness.stop()
        self.until(lambda: self.bridge.public()['status'] == 'waiting', 'old helper socket did not close')
        self.bridge.receive(clipboard_bridge.packet(rpc.NONCE, 'skip', s=10, o='guest'))
        parent = self.root / 'initial-copy-helper-cache'; parent.mkdir()
        self.harness = rpc.Harness(self.exe, parent)
        self.addCleanup(self.harness.stop)
        initial = 'Скопировано до подключения 😀'
        self.harness.request('local_text', text=initial)
        self.assertEqual(self.harness.stats()['sequence'], 1)
        self.harness.request('transport-start', port=self.bridge.port)
        self.until(lambda: self.native.snapshot()[0] == initial,
                   'initial local copy was lost when the helper received its old watermark', seconds=3)
        self.assertGreater(self.bridge.version[0], 10)

    def test_initial_pending_files_before_handshake_are_rebased_without_another_copy(self):
        self.harness.stop()
        self.until(lambda: self.bridge.public()['status'] == 'waiting', 'old helper socket did not close')
        self.bridge.receive(clipboard_bridge.packet(rpc.NONCE, 'skip', s=10, o='guest'))
        parent = self.root / 'initial-files-helper-cache'; parent.mkdir()
        self.harness = rpc.Harness(self.exe, parent)
        self.addCleanup(self.harness.stop)
        source = self.root / 'initial-files-😀'
        (source / 'empty-dir').mkdir(parents=True)
        (source / 'own.txt').write_bytes(b'Owned initial file before HELLO\n')
        self.harness.request('local_files', paths=[str(source)])
        initial = self.harness.stats()
        self.assertEqual(initial['fileSequence'], 1)
        self.assertIsNotNone(initial['fileId'])
        self.harness.request('transport-start', port=self.bridge.port)
        paths = self.until(lambda: self.native.snapshot()[1], 'initial pending files were lost after replay')
        copied = Path(paths[0])
        self.assert_tree(source, copied)
        self.assertNotEqual(copied.parent.name, 'clipboard-' + initial['fileId'])
        self.assertGreater(self.bridge.version[0], 10)
        self.until(lambda: self.harness.stats()['fileId'] is None, 'rebased native file copy was not acknowledged')

    def test_fresh_host_origin_text_keeps_priority_over_guest_initial_snapshot(self):
        self.harness.stop()
        self.until(lambda: self.bridge.public()['status'] == 'waiting', 'old helper socket did not close')
        fresh = 'Новая копия на ПК должна победить 😀'
        for _ in range(10):
            self.native.callback(fresh)
        parent = self.root / 'host-priority-helper-cache'; parent.mkdir()
        self.harness = rpc.Harness(self.exe, parent)
        self.addCleanup(self.harness.stop)
        self.harness.request('local_text', text='Старый снимок гостевого буфера')
        self.assertEqual(self.harness.stats()['sequence'], 1)
        self.harness.request('transport-start', port=self.bridge.port)
        applied = self.apply_guest()
        self.assertEqual(applied['text'], fresh)
        self.until(lambda: self.bridge.pending is None, 'fresh host text did not receive its applied ACK')
        self.assertEqual(self.bridge.version, (10, 'host'))
        self.assertEqual(self.native.snapshot(), (None, None))

    def test_16mib_trees_zone_reconnect_latest_and_published_lifetime(self):
        host_source = self.tree('От ПК 😀')
        self.native.callback([str(host_source)])
        self.until(lambda: self.harness.stats()['marker'], 'native file receiver did not prepare 16MiB', seconds=45)
        applied = self.apply_guest()
        guest_copy, guest_marker = Path(applied['paths'][0]), Path(applied['marker'])
        self.assert_tree(host_source, guest_copy)
        self.until(lambda: self.bridge.pending is None, 'native file ACK did not clear the host offer')

        self.assertTrue(self.harness.request('transport-stop')['stopped'])
        self.until(lambda: self.bridge.public()['status'] == 'waiting', 'host did not observe the owned socket close')
        self.native.callback('последнее состояние после разрыва 😀')
        self.harness.request('transport-start', port=self.bridge.port)
        self.until(lambda: self.harness.request('transport-status')['connected'], 'native transport did not reconnect')
        self.assertEqual(self.apply_guest()['text'], 'последнее состояние после разрыва 😀')
        self.assertTrue(guest_copy.exists())
        self.assertTrue(guest_marker.exists())

        guest_source = self.tree('От гостя 🐱')
        self.harness.request('local_files', paths=[str(guest_source)])
        host_paths = self.until(lambda: self.native.snapshot()[1], 'host file receiver did not publish 16MiB', seconds=45)
        host_copy = Path(host_paths[0])
        self.assert_tree(guest_source, host_copy)
        self.until(lambda: self.harness.stats()['fileId'] is None, 'host file ACK did not clear native source')
        self.harness.stop()
        self.bridge.stop()
        self.assertTrue(guest_marker.exists())
        self.assert_tree(host_source, guest_copy)
        self.assert_tree(guest_source, host_copy)


if __name__ == '__main__':
    unittest.main()
