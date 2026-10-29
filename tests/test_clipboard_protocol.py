"""Protocol/real-stage fixtures with fake native publication; no clipboard, VM or network."""
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'scripts'))
import clipboard_files as files

NONCE, ID = 'a' * 32, 'c' * 32
BOOT, NEXT_BOOT = '20261027000000.000001Z', '20261028000000.000001Z'


def packet(kind, sequence, origin='host', **fields):
    return json.dumps(dict(v=2, n=NONCE, t=kind, s=sequence, o=origin, **fields), ensure_ascii=True)


def text(value, sequence, origin='host'):
    packed = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = value.encode('utf-8')
    return packet('text', sequence, origin, z=1, d=base64.b64encode(packed.compress(raw) + packed.flush()).decode())


def body_packet(data=b'owned fixture', transfer_id=ID, path='copied.bin', zone=None):
    def body(event, **fields):
        return dict(id=transfer_id, event=event, **fields)
    result = [body('begin', count=1, bytes=len(data) + len(zone or b'')),
              body('entry', path=path, kind='file', size=len(data), zone=None if zone is None else base64.b64encode(zone).decode())]
    for offset in range(0, len(data), files.CHUNK):
        result.append(body('data', offset=offset, data=base64.b64encode(data[offset:offset + files.CHUNK]).decode()))
    return result + [body('file_end', sha256=hashlib.sha256(data).hexdigest()), body('end')]


class Harness:
    def __init__(self, exe, parent, origin='guest'):
        self.process = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding='utf-8', bufsize=1)
        self.events, self.records = queue.Queue(), []
        self.reader = threading.Thread(target=self.read, daemon=True); self.reader.start()
        self.counter, self.closed = 0, False
        self.process.stdin.write(json.dumps(dict(nonce=NONCE, parent=str(parent), origin=origin, boot=BOOT)) + '\n')
        self.process.stdin.flush()
        ready = self.events.get(timeout=5)
        if ready.get('kind') != 'ready': raise AssertionError('Fixture did not initialize: ' + repr(ready))

    def read(self):
        for line in self.process.stdout:
            event = json.loads(line)
            self.records.append(event)
            if event.get('kind') != 'status': self.events.put(event)

    def request(self, op, **fields):
        self.counter += 1
        self.process.stdin.write(json.dumps(dict(id=self.counter, op=op, **fields)) + '\n'); self.process.stdin.flush()
        response = self.events.get(timeout=8)
        if response.get('id') != self.counter or 'error' in response:
            raise AssertionError('Fixture command failed: ' + op + ': ' + repr(response))
        return response

    def receive(self, line): return self.request('receive', line=line)
    def next(self, wait=0): return self.request('next', wait=wait)['line']
    def stats(self): return self.request('stats')

    def stop(self):
        if self.closed: return
        try:
            result = self.request('stop')
            if not result['stopped']: raise AssertionError('Protocol I/O worker did not stop')
            self.process.stdin.close()
            if self.process.wait(timeout=8) != 0: raise AssertionError('Protocol fixture failed')
            self.reader.join(timeout=2)
            error = self.process.stderr.read()
            if error: raise AssertionError(error)
        finally:
            if self.process.poll() is None: self.process.kill(); self.process.wait()
            self.process.stdout.close(); self.process.stderr.close(); self.closed = True


@unittest.skipUnless(os.name == 'nt', 'Real stage regression requires Windows/.NET Framework')
class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='clipboard-protocol-build-')
        snapshot = Path(cls.build.name)
        names = ('guest/GuestClipboard.cs', 'guest/ClipboardFiles.cs', 'guest/ClipboardStage.cs',
                 'guest/ClipboardTransport.cs', 'guest/ClipboardProtocol.cs', 'guest/GuestEnvironment.cs',
                 'guest/files/GuestFiles.cs', 'tests/ClipboardProtocolRegression.cs')
        sources = []
        for index, name in enumerate(names):
            data = (SOURCE / name).read_bytes()
            target = snapshot / (str(index) + '-' + Path(name).name)
            target.write_bytes(data); sources.append(str(target))
        cls.exe = snapshot / 'ClipboardProtocolRegression.exe'
        compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        result = subprocess.run([str(compiler), '/nologo', '/platform:x64', '/main:ClipboardProtocolRegression',
                                 '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', '/r:System.Web.Extensions.dll',
                                 '/r:Microsoft.CSharp.dll', '/out:' + str(cls.exe), *sources],
                                capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode:
            cls.build.cleanup(); raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls): cls.build.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clipboard-protocol-fixture-')
        self.root = Path(self.temp.name); self.harnesses = []

    def tearDown(self):
        try:
            for harness in self.harnesses: harness.stop()
        finally: self.temp.cleanup()

    def harness(self, origin='guest'):
        parent = self.root / ('cache-' + str(len(self.harnesses))); parent.mkdir()
        harness = Harness(self.exe, parent, origin); harness.parent = parent
        self.harnesses.append(harness); return harness

    def feed(self, harness, bodies=None, sequence=10, origin='host'):
        for body in body_packet() if bodies is None else bodies:
            harness.receive(packet('files', sequence, origin, b=body))

    def control(self, harness, kind, wait=1500):
        deadline = time.monotonic() + wait / 1000
        while time.monotonic() < deadline:
            line = harness.next(100)
            if line is not None:
                value = json.loads(line)
                if value['t'] == kind: return value
        self.fail('Missing outgoing control ' + kind)

    def idle_cleanup(self, harness):
        deadline = time.monotonic() + 5
        while harness.stats()['cleanup']:
            if time.monotonic() >= deadline: self.fail('Cleanup did not finish')
            time.sleep(0.01)

    def tree(self, name='Дерево-😀'):
        tree = self.root / name
        (tree / 'empty-dir').mkdir(parents=True)
        (tree / 'empty.txt').write_bytes(b'')
        (tree / 'data-🐱.bin').write_bytes(os.urandom(files.CHUNK * 4 + 17))
        Path(str(tree / 'data-🐱.bin') + ':Zone.Identifier').write_bytes(b'[ZoneTransfer]\r\nZoneId=3\r\n')
        return tree

    def assert_tree(self, source, target):
        expected = {str(p.relative_to(source)): p for p in source.rglob('*')}
        actual = {str(p.relative_to(target)): p for p in target.rglob('*')}
        self.assertEqual(set(expected), set(actual))
        for key, path in expected.items():
            self.assertEqual(path.is_dir(), actual[key].is_dir())
            if path.is_file():
                self.assertEqual(path.read_bytes(), actual[key].read_bytes())
                self.assertEqual(files._zone(path), files._zone(actual[key]))

    def transfer(self, source, target):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            line = source.next(100)
            if line is None: continue
            value = json.loads(line)
            target.receive(line)
            if value['t'] == 'files' and value['b']['event'] == 'end': return value
        self.fail('File transfer did not reach end')

    def test_text_exact_ack_retry_duplicate_and_obsolete(self):
        source = self.harness()
        source.request('local_text', text='same pending text')
        line = source.next(1000); version = json.loads(line)
        self.assertIsNone(source.next())
        source.receive(packet('ack', version['s'], 'host', ok=True, applied=True))
        time.sleep(1.05); self.assertEqual(source.next(), line)
        source.receive(packet('ack', version['s'], version['o'], ok=True, applied=True))
        self.assertIsNone(source.next())
        target = self.harness()
        target.receive(text('remote text', 20))
        self.assertFalse(target.request('apply', ok=False)['applied'])
        result = target.request('apply', ok=True)
        self.assertEqual((result['attempts'], result['publications'], result['text']), (2, 1, 'remote text'))
        ack = self.control(target, 'ack'); self.assertTrue(ack['ok'] and ack['applied'])
        target.receive(text('remote text', 20))
        self.assertTrue(self.control(target, 'ack')['applied'])
        target.receive(text('obsolete text', 19))
        self.assertFalse(self.control(target, 'ack')['applied'])
        self.assertFalse(target.request('apply', ok=True)['applied'])
        self.assertEqual(target.stats()['publications'], 1)

    def test_python_file_envelopes_stored_busy_retry_and_park(self):
        source = self.tree(); target = self.harness()
        frames = list(files.Sender([source], ID).frames())
        self.feed(target, frames[:-1])
        self.assertFalse(target.request('apply', ok=True)['applied'])
        self.feed(target, frames[-1:])
        stored = self.control(target, 'stored'); self.assertEqual(stored['i'], ID)
        marker = target.stats()['marker']; self.assertTrue(Path(marker).exists())
        busy = target.request('apply', ok=False)
        self.assertTrue(busy['prepared']); self.assertEqual(busy['marker'], marker)
        target.request('reconnected', value=False); target.request('reconnected', value=True)
        self.feed(target, frames[:1])
        self.assertEqual(self.control(target, 'stored')['i'], ID)
        applied = target.request('apply', ok=True)
        self.assertEqual((applied['attempts'], applied['publications']), (2, 1))
        self.assert_tree(source, Path(applied['paths'][0]))
        ack = self.control(target, 'ack'); self.assertTrue(ack['ok'] and ack['applied']); self.assertEqual(ack['i'], ID)
        self.feed(target, frames[:1])
        self.assertTrue(self.control(target, 'ack')['applied'])
        stage = Path(applied['paths'][0]).parent
        target.stop(); self.assertTrue(stage.exists()); self.assertTrue(Path(marker).exists())

    def test_equal_version_committed_before_apply_does_not_repeat_native_publication(self):
        target = self.harness()
        value = text('already published by retained legacy path', 20)
        target.receive(value)
        target.request('legacy_apply_text', line=value)
        result = target.request('apply', ok=True)
        self.assertFalse(result['applied'])
        self.assertEqual((result['attempts'], result['publications']), (1, 1))
        self.assertTrue(self.control(target, 'ack')['ok'])

    def test_csharp_sender_python_receiver_stored_pause_and_wrong_late_ack(self):
        source = self.tree(); sender = self.harness()
        sender.request('local_files', paths=[str(source)])
        received = self.root / 'python-destination'; received.mkdir(); receiver = None
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                line = sender.next(100)
                if line is None: continue
                frame = json.loads(line)
                if frame['t'] != 'files': continue
                if receiver is None: receiver = files.Receiver(received, frame['b']['id'])
                roots = receiver.accept(frame['b'])
                if roots is not None: break
            else: self.fail('C# sender never completed')
            self.assert_tree(source, Path(roots[0]))
            sender.receive(packet('stored', frame['s'], frame['o'], i=frame['b']['id']))
            self.assertTrue(sender.stats()['stored'])
            self.assertIsNone(sender.next(1100))
            sender.receive(packet('ack', frame['s'], frame['o'], i='f' * 32, ok=True, applied=True))
            self.assertEqual(sender.stats()['fileId'], frame['b']['id'])
            sender.receive(packet('ack', frame['s'] + 1, frame['o'], i=frame['b']['id'], ok=True, applied=True))
            self.assertIsNotNone(sender.stats()['fileId'])
            sender.receive(packet('ack', frame['s'], frame['o'], i=frame['b']['id'], ok=True, applied=True))
            self.assertIsNone(sender.stats()['fileId'])
        finally:
            if receiver is not None: receiver.close()

    def test_both_protocol_directions_stored_prevents_whole_file_resend(self):
        host, guest = self.harness('host'), self.harness('guest')
        first, second = self.tree('host-tree'), self.tree('guest-tree')
        host.request('local_files', paths=[str(first)])
        self.transfer(host, guest)
        host.receive(json.dumps(self.control(guest, 'stored')))
        self.assertFalse(guest.request('apply', ok=False)['applied'])
        self.assertIsNone(host.next(1100))
        applied = guest.request('apply', ok=True); self.assert_tree(first, Path(applied['paths'][0]))
        host.receive(json.dumps(self.control(guest, 'ack')))
        guest.request('local_files', paths=[str(second)])
        self.transfer(guest, host)
        guest.receive(json.dumps(self.control(host, 'stored')))
        applied = host.request('apply', ok=True); self.assert_tree(second, Path(applied['paths'][0]))
        guest.receive(json.dumps(self.control(host, 'ack')))
        self.assertIsNone(host.stats()['fileId']); self.assertIsNone(guest.stats()['fileId'])

    def test_private_skip_announces_clock_and_aborts_unpublished_only(self):
        target = self.harness(); foreign = target.parent / 'foreign'; foreign.write_bytes(b'keep')
        self.feed(target, body_packet(b'x' * (files.CHUNK + 1))[:3], sequence=5)
        target.request('local_text', text=None)
        skip = self.control(target, 'skip')
        self.assertEqual(set(skip), {'v', 'n', 't', 's', 'o'})
        self.assertGreater(skip['s'], 5)
        self.idle_cleanup(target)
        self.assertFalse(target.request('apply', ok=True)['applied'])
        self.assertEqual(foreign.read_bytes(), b'keep')
        self.feed(target, sequence=20)
        self.control(target, 'stored')
        marker = target.stats()['marker']; stage = Path(marker[:-11])
        target.receive(packet('skip', 21))
        ack = self.control(target, 'ack'); self.assertFalse(ack['applied'])
        self.idle_cleanup(target)
        self.assertFalse(stage.exists()); self.assertEqual(target.stats()['publications'], 0)
        target.receive(text('fresh after private copies', 22))
        self.assertTrue(target.request('apply', ok=True)['applied'])

    def test_newer_progress_older_text_wrong_id_and_corrupt_data_never_reach_native(self):
        target = self.harness(); frames = body_packet()
        self.feed(target, frames[:2], sequence=20)
        target.receive(text('older text', 19))
        self.assertFalse(target.request('apply', ok=True)['applied'])
        wrong = dict(frames[2], id='b' * 32)
        self.feed(target, [wrong], sequence=20)
        self.feed(target, frames[2:], sequence=20)
        self.control(target, 'stored')
        self.assertTrue(target.request('apply', ok=True)['applied'])
        self.assertEqual(target.stats()['publications'], 1)
        bad = body_packet(transfer_id='d' * 32); bad[-2]['sha256'] = '0' * 64
        self.feed(target, bad, sequence=30)
        self.idle_cleanup(target)
        self.assertFalse(target.request('apply', ok=True)['applied'])
        self.assertEqual(target.stats()['publications'], 1)

    def test_partial_disconnect_cancel_and_published_current_boot_retention(self):
        target = self.harness(); foreign = target.parent / 'foreign'; foreign.write_text('keep')
        frames = body_packet()
        self.feed(target, frames[:3])
        target.request('reconnected', value=False); self.idle_cleanup(target)
        self.assertEqual(list(target.parent.iterdir()), [foreign])
        target.request('reconnected', value=True); self.feed(target, frames)
        self.control(target, 'stored')
        target.receive(packet('cancel', 10, i='f' * 32))
        self.assertEqual(target.stats()['candidate'], 1)
        published = target.request('apply', ok=True)
        stage = Path(published['paths'][0]).parent
        target.receive(packet('cancel', 10, i=ID))
        self.assertTrue(stage.exists())
        fresh = body_packet(transfer_id='d' * 32)
        self.feed(target, fresh, sequence=11); self.control(target, 'stored')
        other_marker = target.stats()['marker']; target.receive(packet('cancel', 11, i='d' * 32))
        self.idle_cleanup(target)
        self.assertFalse(Path(other_marker).exists()); self.assertTrue(stage.exists())
        retained = target.request('cleanup', boot=BOOT, paths=[])
        self.assertGreaterEqual(retained['retained'], 1); self.assertTrue(stage.exists())
        target.request('cleanup', boot=NEXT_BOOT, paths=[published['paths'][0]])
        self.assertTrue(stage.exists())
        target.request('cleanup', boot=NEXT_BOOT, paths=[])
        self.assertFalse(stage.exists()); self.assertEqual(foreign.read_text(), 'keep')

    def test_publication_racing_newer_local_or_dispose_always_parks_actual_success(self):
        for change in ({'local_during': 'newer local'}, {'dispose_during': True}):
            with self.subTest(change=change):
                target = self.harness(); self.feed(target)
                self.control(target, 'stored')
                applied = target.request('apply', ok=True, **change)
                self.assertEqual(applied['publications'], 1)
                root = Path(applied['paths'][0]); self.assertTrue(root.exists())
                if 'local_during' in change:
                    self.assertGreater(target.stats()['sequence'], 10)
                    self.assertTrue(self.control(target, 'ack')['applied'])
                target.stop(); self.assertTrue(root.exists())
        target = self.harness(); self.feed(target); self.control(target, 'stored')
        marker = target.stats()['marker']
        self.assertFalse(target.request('apply', ok=False, dispose_during=True)['applied'])
        target.stop(); self.assertFalse(Path(marker).exists())

    def test_native_guard_rechecks_cancel_or_newer_remote_after_snapshot(self):
        newer_begin = packet('files', 11, b=body_packet(transfer_id='e' * 32)[0])
        for incoming in (packet('cancel', 10, i=ID), text('newer remote', 11), newer_begin):
            with self.subTest(incoming=json.loads(incoming)['t']):
                target = self.harness(); self.feed(target); self.control(target, 'stored')
                marker = Path(target.stats()['marker'])
                result = target.request('apply', ok=True, preserve_lines=[incoming])
                self.assertTrue(result['initiallyCurrent'])
                self.assertFalse(result['cloneCurrent'])
                self.assertFalse(result['guardAllowed'])
                self.assertFalse(result['currentOutside'])
                self.assertFalse(result['applied'])
                self.assertEqual(result['nativeEmpties'], 0)
                self.assertEqual(result['publications'], 0)
                self.idle_cleanup(target)
                self.assertFalse(marker.exists())
                if json.loads(incoming)['t'] == 'text':
                    self.assertTrue(target.request('apply', ok=True)['applied'])
                target.stop()

    def test_native_guard_allows_only_exact_active_frame_and_rejects_local_or_dispose(self):
        for change in ({}, {'local_during': 'newer local'}, {'dispose_during': True}):
            with self.subTest(change=change):
                target = self.harness(); self.feed(target); self.control(target, 'stored')
                marker = Path(target.stats()['marker'])
                result = target.request('apply', ok=True, preserve_lines=[], **change)
                self.assertTrue(result['initiallyCurrent'])
                self.assertFalse(result['cloneCurrent'])
                self.assertFalse(result['currentOutside'])
                self.assertEqual(result['guardAllowed'], not change)
                self.assertEqual(result['nativeEmpties'], 0 if change else 1)
                self.assertEqual(result['publications'], 0 if change else 1)
                if change:
                    self.idle_cleanup(target)
                    self.assertFalse(marker.exists())
                else:
                    target.stop()
                    self.assertTrue(marker.exists())

    def test_actual_set_before_late_remote_cancel_or_supersession_always_parks(self):
        newer_begin = packet('files', 11, b=body_packet(transfer_id='e' * 32)[0])
        for incoming in (packet('cancel', 10, i=ID), text('later remote', 11), newer_begin):
            with self.subTest(incoming=json.loads(incoming)['t']):
                target = self.harness(); self.feed(target); self.control(target, 'stored')
                marker = Path(target.stats()['marker'])
                result = target.request('apply', ok=True, preserve_lines=[], after_set_lines=[incoming])
                self.assertTrue(result['guardAllowed'])
                self.assertTrue(result['applied'])
                self.assertEqual(result['nativeEmpties'], 1)
                self.assertEqual(result['publications'], 1)
                roots = [Path(path) for path in result['paths']]
                target.stop()
                self.assertTrue(marker.exists())
                self.assertTrue(all(path.exists() for path in roots))

    def test_source_mutation_cancels_pending_without_killing_channel(self):
        source = self.root / 'source-mutation'; source.mkdir()
        (source / 'A-stream').write_bytes(os.urandom(files.CHUNK * 8))
        later = source / 'Z-later'; later.write_bytes(b'initial')
        sender = self.harness(); sender.request('local_files', paths=[str(source)])
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            line = sender.next(100)
            if line and json.loads(line).get('b', {}).get('event') == 'data': break
        else: self.fail('No source data emitted')
        later.write_bytes(b'changed after inventory with a different length')
        cancel = self.control(sender, 'cancel', wait=6000)
        self.assertEqual(cancel['t'], 'cancel'); self.assertIsNone(sender.stats()['fileId'])
        sender.request('local_text', text='after source error')
        self.assertEqual(json.loads(sender.next(1000))['t'], 'text')
        status = [row['value'] for row in sender.records if row.get('kind') == 'status']
        self.assertIn('FILES_FAILED', status)
        self.assertTrue(all(str(source) not in item for item in status))

    def test_cleanup_is_single_background_callback_and_does_not_block_native_ui(self):
        target = self.harness(); self.feed(target); self.control(target, 'stored')
        marker = target.stats()['marker']
        target.request('hold_cleanup')
        target.receive(text('newer text', 11))
        before = time.monotonic(); applied = target.request('apply', ok=True)
        self.assertLess(time.monotonic() - before, 0.5)
        self.assertTrue(applied['applied']); self.assertTrue(Path(marker).exists())
        newer = body_packet(transfer_id='e' * 32)
        self.feed(target, newer[:1], sequence=12)
        self.assertEqual(target.stats()['candidate'], 0)
        target.request('release_cleanup'); self.idle_cleanup(target)
        self.assertFalse(Path(marker).exists())
        self.feed(target, newer, sequence=12); self.control(target, 'stored')
        self.assertEqual(target.stats()['candidate'], 1)
        self.assertEqual(target.stats()['cleanupPeak'], 1)


if __name__ == '__main__': unittest.main()
