"""File-transfer regressions on owned fixtures only; no clipboard/VM/network."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'scripts'))
import clipboard_files as files

ID = 'a' * 32


def frame(event, **fields):
    return dict(id=ID, event=event, **fields)


def packet(data=b'abc', path='file', zone=None):
    raw_zone = None if zone is None else base64.b64encode(zone).decode('ascii')
    result = [frame('begin', count=1, bytes=len(data) + len(zone or b'')),
              frame('entry', path=path, kind='file', size=len(data), zone=raw_zone)]
    if data:
        result.append(frame('data', offset=0, data=base64.b64encode(data).decode('ascii')))
    return result + [frame('file_end', sha256=hashlib.sha256(data).hexdigest()), frame('end')]


class FileTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='clipboard-build-')
        cls.exe = Path(cls.build.name) / 'ClipboardFilesRegression.exe'
        compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        result = subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:x64',
                                 '/win32manifest:' + str(SOURCE / 'ui/app.manifest'), '/r:System.Web.Extensions.dll',
                                 '/out:' + str(cls.exe), str(SOURCE / 'guest/GuestEnvironment.cs'),
                                 str(SOURCE / 'guest/ClipboardFiles.cs'),
                                 str(SOURCE / 'tests/ClipboardFilesRegression.cs')],
                                capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode:
            cls.build.cleanup()
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clipboard-fixture-')
        self.root = Path(self.temp.name)
        self.destination = self.root / 'destination'
        self.destination.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def harness(self, operation, data, success=True):
        result = subprocess.run([str(self.exe), operation, ID, str(self.destination)],
                                input=data, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0 if success else 2, result.stdout + result.stderr)
        return result.stdout

    def receive_cs(self, frames, success=True):
        return self.harness('receive', ''.join(json.dumps(f) + '\n' for f in frames), success)

    def reject_both(self, frames):
        foreign = self.destination / 'foreign.txt'
        foreign.write_text('keep', encoding='utf-8')
        with self.assertRaises((ValueError, KeyError, TypeError, OSError)):
            with files.Receiver(self.destination, ID) as receiver:
                for f in frames:
                    receiver.accept(f)
                if receiver.roots is None:
                    raise ValueError('truncated')
        self.receive_cs(frames, success=False)
        self.assertEqual(list(self.destination.iterdir()), [foreign])
        self.assertEqual(foreign.read_text(encoding='utf-8'), 'keep')

    def tree(self):
        tree = self.root / 'Дерево 😀'
        (tree / 'пустой').mkdir(parents=True)
        (tree / 'нулевой.txt').write_bytes(b'')
        (tree / 'binary.bin').write_bytes(os.urandom(files.CHUNK * 3 + 127))
        (tree / 'пустой' / 'emoji 🐱.txt').write_text('Содержимое\n🌍', encoding='utf-8')
        zone = b'[ZoneTransfer]\r\nZoneId=3\r\n'
        Path(str(tree / 'binary.bin') + ':Zone.Identifier').write_bytes(zone)
        second = self.root / 'other' / tree.name
        second.mkdir(parents=True)
        (second / 'duplicate.txt').write_bytes(b'duplicate basename')
        return tree, second

    def assert_tree(self, source, target):
        expected = {str(p.relative_to(source)): p for p in source.rglob('*')}
        actual = {str(p.relative_to(target)): p for p in target.rglob('*')}
        self.assertEqual(set(expected), set(actual))
        for relative, p in expected.items():
            self.assertEqual(p.is_dir(), actual[relative].is_dir())
            if p.is_file():
                self.assertEqual(p.read_bytes(), actual[relative].read_bytes())
                self.assertEqual(files._zone(p), files._zone(actual[relative]))

    def long_tree(self):
        tree = self.root / 'long-tree'
        deep = tree / ('a' * 80) / ('b' * 80) / ('c' * 80)
        extended = Path('\\\\?\\' + str(deep))
        extended.mkdir(parents=True)
        (extended / 'empty').mkdir()
        (extended / 'tiny.bin').write_bytes(bytes(range(256)))
        Path(str(extended / 'tiny.bin') + ':Zone.Identifier').write_bytes(b'[ZoneTransfer]\r\nZoneId=3\r\n')
        self.assertLess(len(str(tree)), 240)
        self.assertGreater(len(str(deep / 'tiny.bin')), 260)
        return tree, deep / 'tiny.bin'

    def test_real_normal_long_file_selection(self):
        _, source = self.long_tree()
        self.assertFalse(str(source).startswith('\\\\?\\'))
        frames = [json.loads(line) for line in self.harness('send', json.dumps([str(source)])).splitlines()]
        with files.Receiver(self.destination, ID) as receiver:
            for frame in frames:
                receiver.accept(frame)
            actual = Path(receiver.roots[0])
            self.assertEqual(actual.read_bytes(), source.read_bytes())
            self.assertEqual(files._zone(actual), files._zone(source))

    def test_csharp_native_drive_unc_conversion_without_network(self):
        self.assertIn('Native absolute drive/UNC conversion: OK', self.harness('native-paths', ''))

    def test_real_normal_long_tree_both_directions_and_abort(self):
        tree, _ = self.long_tree()
        frames = list(files.Sender([tree], ID).frames())
        received = json.loads(self.receive_cs(frames))
        self.assert_tree(tree, Path(received['roots'][0]))
        shutil.rmtree(received['stage'])  # Own completed fixture only.
        cs_frames = [json.loads(line) for line in self.harness('send', json.dumps([str(tree)])).splitlines()]
        with files.Receiver(self.destination, ID) as receiver:
            for frame in cs_frames:
                receiver.accept(frame)
            self.assert_tree(tree, Path(receiver.roots[0]))
        # Truncated transfer must still identity-clean every created deep entry.
        self.receive_cs(frames[:-2], success=False)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_python_to_csharp_and_csharp_to_python(self):
        selected = self.tree()
        frames = list(files.Sender(selected, ID).frames())
        self.assertTrue(all(len(json.dumps(f).encode()) < 1048576 for f in frames))
        received = json.loads(self.receive_cs(frames))
        for source, target in zip(selected, received['roots']):
            self.assert_tree(source, Path(target))
        self.assertTrue(Path(received['roots'][1]).name.endswith(' (2)'))
        shutil.rmtree(received['stage'])
        cs_frames = [json.loads(line) for line in self.harness('send', json.dumps([str(p) for p in selected]) + '\n').splitlines()]
        with files.Receiver(self.destination, ID) as receiver:
            for f in cs_frames[:-1]:
                self.assertIsNone(receiver.accept(f))
                self.assertIsNone(receiver.roots)
            roots = receiver.accept(cs_frames[-1])
            for source, target in zip(selected, roots):
                self.assert_tree(source, Path(target))
            stage = receiver.detach()
        self.assertTrue(Path(stage).exists())
        receiver.release()
        self.assertFalse(Path(stage).exists())

    def test_unsafe_paths_rejected_in_both(self):
        for path in ('../escape', '/absolute', 'C:/drive', '//server/share', 'dir/../file',
                     'ads:stream', 'NUL.txt', 'com1', 'LPT¹.txt', 'CONIN$', 'CON .txt',
                     'trail.', 'trail ', 'line\nfile', 'control\x7ffile', 'control\x85file', 'back\\slash'):
            with self.subTest(path=path):
                self.reject_both(packet(path=path))

    def test_wrong_id_number_types_unknown_fields_and_size(self):
        cases = []
        for change in ({'id': 'b' * 32}, {'count': True}, {'count': 100001}, {'bytes': files.MAX_BYTES + 1},
                       {'bytes': -1}, {'count': 1.0}, {'extra': 1}, {'extra': 'x' * 1048576}):
            frames = packet(); frames[0].update(change); cases.append(frames)
        for frames in cases:
            with self.subTest(begin=str(frames[0])[:150]): self.reject_both(frames)

    def test_order_offsets_hash_truncation_and_trailing(self):
        cases = []
        f = packet(); f[2]['offset'] = 1; cases.append(f)
        f = packet(); f[2]['data'] = base64.b64encode(b'too long').decode(); cases.append(f)
        f = packet(); f[2]['data'] = '!'; cases.append(f)
        f = packet(); f[2]['data'] = base64.b64encode(b'a').decode(); cases.append(f)
        f = packet(); f[3]['sha256'] = '0' * 64; cases.append(f)
        f = packet(); f[1]['path'] = 'missing/file'; cases.append(f)
        f = packet(); f.insert(2, f[1].copy()); cases.append(f)
        f = packet(); f.insert(1, f[0].copy()); cases.append(f)
        cases += [packet()[1:], packet()[:-1], packet() + [frame('end')], packet()[:2] + [frame('end')]]
        for index, frames in enumerate(cases):
            with self.subTest(case=index): self.reject_both(frames)

    def test_case_collision_and_directory_payload(self):
        frames = [frame('begin', count=2, bytes=0), frame('entry', path='A', kind='dir', size=0, zone=None),
                  frame('entry', path='a', kind='dir', size=0, zone=None), frame('end')]
        self.reject_both(frames)
        frames = [frame('begin', count=1, bytes=1), frame('entry', path='dir', kind='dir', size=1, zone=None), frame('end')]
        self.reject_both(frames)

    def test_zone_bounds_and_empty_zone(self):
        self.reject_both(packet(zone=b'x' * (files.CHUNK + 1)))
        for zone in (b'', b'x' * files.CHUNK):
            received = json.loads(self.receive_cs(packet(zone=zone)))
            self.assertEqual(files._zone(Path(received['roots'][0])), zone)
            shutil.rmtree(received['stage'])
            with files.Receiver(self.destination, ID) as receiver:
                for f in packet(zone=zone): receiver.accept(f)
                self.assertEqual(files._zone(Path(receiver.roots[0])), zone)

    def test_cancel_cleanup_and_no_premature_publish(self):
        foreign = self.destination / 'foreign'; foreign.write_bytes(b'keep')
        receiver = files.Receiver(self.destination, ID)
        stage = receiver.stage
        with self.assertRaises(OSError): stage.rename(self.destination / 'moved-stage')
        for f in packet()[:3]: self.assertIsNone(receiver.accept(f))
        with self.assertRaises(ValueError): receiver.detach()
        receiver.abort(); receiver.close()
        self.assertFalse(stage.exists())
        self.assertEqual(foreign.read_bytes(), b'keep')

    def test_completed_lifetime_owned_until_close_or_detach(self):
        with files.Receiver(self.destination, ID) as receiver:
            for f in packet(): receiver.accept(f)
            stage = receiver.stage
            self.assertTrue(Path(receiver.roots[0]).exists())
        self.assertFalse(stage.exists())

    def test_detached_lease_keeps_pins_until_explicit_release(self):
        receiver = files.Receiver(self.destination, ID)
        for f in packet(): receiver.accept(f)
        stage = Path(receiver.detach())
        receiver.close()
        self.assertEqual((stage / 'file').read_bytes(), b'abc')
        with self.assertRaises(OSError): stage.rename(self.destination / 'moved')
        receiver.release()
        self.assertFalse(stage.exists())

    def test_cleanup_refuses_substitution_at_stage_pin_release(self):
        original_delete = files._delete_owned
        for detached in (False, True):
            with self.subTest(detached=detached):
                receiver = files.Receiver(self.destination, ID)
                for f in packet() if detached else packet()[:-1]: receiver.accept(f)
                stage = receiver.stage
                moved = stage.with_name(stage.name + '-moved')
                foreign = stage.with_name(stage.name + '-foreign')
                foreign.mkdir()
                (foreign / 'foreign-sentinel').write_bytes(b'keep')
                if detached:
                    receiver.detach(); receiver.close()
                attempts = []
                def replace_then_delete(path, identity, directory):
                    if path == stage:
                        try:
                            stage.rename(moved)
                        except PermissionError:
                            attempts.append('blocked')
                        else:
                            foreign.rename(stage)
                            attempts.append('replaced')
                    return original_delete(path, identity, directory)
                with mock.patch.object(files, '_delete_owned', side_effect=replace_then_delete):
                    if attempts:
                        self.fail('cleanup hook ran prematurely')
                    try: receiver.release()
                    except OSError:
                        self.assertEqual(attempts, ['replaced'])
                    else:
                        self.assertEqual(attempts, ['blocked'])
                sentinel = (stage if attempts == ['replaced'] else foreign) / 'foreign-sentinel'
                self.assertEqual(sentinel.read_bytes(), b'keep')
                self.assertFalse((moved / 'file').exists())

    def test_cleanup_file_id_mismatch_refuses_delete(self):
        receiver = files.Receiver(self.destination, ID)
        for f in packet(): receiver.accept(f)
        stage = receiver.stage
        original_delete = files._delete_owned
        def replace_file_then_delete(path, identity, directory):
            if not directory:
                path.unlink()
                path.write_bytes(b'foreign replacement')
            return original_delete(path, identity, directory)
        with mock.patch.object(files, '_delete_owned', side_effect=replace_file_then_delete):
            with self.assertRaises(OSError): receiver.release()
        self.assertEqual((stage / 'file').read_bytes(), b'foreign replacement')

    def test_cleanup_leaves_unknown_foreign_entry(self):
        receiver = files.Receiver(self.destination, ID)
        for f in packet(): receiver.accept(f)
        stage = receiver.stage
        (stage / 'foreign').write_bytes(b'keep')
        with self.assertRaises(OSError): receiver.close()
        self.assertEqual((stage / 'foreign').read_bytes(), b'keep')
        self.assertFalse((stage / 'file').exists())

    def test_windows_ordinal_unicode_names_crosslanguage(self):
        tree = self.root / 'unicode'; tree.mkdir()
        for name in ('ß', 'SS', 'ı', 'I', 'ſ', 'S'):
            (tree / name).write_text(name, encoding='utf-8')
        received = json.loads(self.receive_cs(list(files.Sender([tree], ID).frames())))
        self.assert_tree(tree, Path(received['roots'][0]))
        shutil.rmtree(received['stage'])
        frames = [json.loads(line) for line in self.harness('send', json.dumps([str(tree)]) + '\n').splitlines()]
        with files.Receiver(self.destination, ID) as receiver:
            for f in frames: receiver.accept(f)
            self.assert_tree(tree, Path(receiver.roots[0]))

    def test_space_rechecked_before_directory_emptyfile_and_ads(self):
        low = shutil._ntuple_diskusage(100000000, 100000000 - files.MARGIN, files.MARGIN)
        for kind in ('dir', 'file'):
            with self.subTest(kind=kind):
                receiver = files.Receiver(self.destination, ID)
                receiver.accept(frame('begin', count=1, bytes=0))
                stage = receiver.stage
                with mock.patch.object(files.shutil, 'disk_usage', return_value=low):
                    with self.assertRaises(ValueError):
                        receiver.accept(frame('entry', path='empty', kind=kind, size=0, zone=None))
                self.assertFalse(stage.exists())
        receiver = files.Receiver(self.destination, ID)
        receiver.accept(frame('begin', count=1, bytes=1))
        receiver.accept(frame('entry', path='empty', kind='file', size=0, zone='eA=='))
        stage = receiver.stage
        with mock.patch.object(files.shutil, 'disk_usage', return_value=low):
            with self.assertRaises(ValueError): receiver.accept(frame('file_end', sha256=hashlib.sha256(b'').hexdigest()))
        self.assertFalse(stage.exists())
        receiver = files.Receiver(self.destination, ID)
        with mock.patch.object(files.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(1000000000, 0, files.MARGIN + 100000)):
            with self.assertRaises(ValueError): receiver.accept(frame('begin', count=100000, bytes=0))

    def test_source_mutation_after_inventory(self):
        p = self.root / 'source'; p.write_bytes(b'original')
        iterator = files.Sender([p], ID).frames()
        self.assertEqual(next(iterator)['event'], 'begin')
        p.write_bytes(b'changed content')
        with self.assertRaises(ValueError): list(iterator)

    def test_selection_and_reparse_flag_limits(self):
        with mock.patch.object(files, 'MAX_ENTRIES', 2):
            with self.assertRaises(ValueError): files.Sender(('unused' for _ in range(3)), ID)
        p = self.root / 'source'; p.write_bytes(b'x')
        signature = os.lstat(p)
        marked = mock.Mock(st_file_attributes=signature.st_file_attributes | 0x400)
        with mock.patch.object(files.os, 'lstat', return_value=marked):
            with self.assertRaises(ValueError): files._signature(p)
            with self.assertRaises(ValueError): files._pin(self.destination)

    def test_source_replacement_while_read_is_denied(self):
        p = self.root / 'source'; p.write_bytes(b'x' * (files.CHUNK * 2))
        iterator = files.Sender([p], ID).frames()
        while next(iterator)['event'] != 'data': pass
        with self.assertRaises(OSError): p.write_bytes(b'changed')
        with self.assertRaises(OSError): p.rename(self.root / 'replaced')
        iterator.close()

    def test_oversized_manifest_free_space_and_receiver_file_pin(self):
        with files.Receiver(self.destination, ID) as receiver:
            with mock.patch.object(files.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100, 99, 1)):
                with self.assertRaises(ValueError): receiver.accept(frame('begin', count=1, bytes=3))
        receiver = files.Receiver(self.destination, ID)
        for f in packet()[:-1]: receiver.accept(f)
        staged = receiver.stage / 'file'
        with self.assertRaises(OSError): staged.write_bytes(b'tampered')
        receiver.accept(frame('end')); receiver.close()

    def test_reparse_selection_and_parent_rejected(self):
        p = self.root / 'real'; p.mkdir()
        (p / 'file').write_bytes(b'x')
        link = self.root / 'link'
        quoted_link = str(link).replace("'", "''")
        quoted_target = str(p).replace("'", "''")
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                 "New-Item -ItemType Junction -Path '" + quoted_link + "' -Target '" + quoted_target + "' | Out-Null"],
                                capture_output=True, text=True, timeout=30)
        if result.returncode and ('Access is denied' in result.stderr or 'Отказано в доступе' in result.stderr):
            self.skipTest('Environment denies fixture junction creation')
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaises(ValueError): list(files.Sender([link], ID).frames())
        self.harness('send', json.dumps([str(link)]) + '\n', success=False)
        with self.assertRaises(ValueError): list(files.Sender([link / 'file'], ID).frames())
        self.harness('send', json.dumps([str(link / 'file')]) + '\n', success=False)
        with self.assertRaises(ValueError): files.Receiver(link, ID)
        result = subprocess.run([str(self.exe), 'receive', ID, str(link)], input='',
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 2)
        self.assertEqual((p / 'file').read_bytes(), b'x')

    def test_csharp_mutation_and_pins(self):
        self.assertIn('cancellation: OK', self.harness('selfcheck', ''))

    def test_csharp_resource_budgets(self):
        self.assertIn('Resource budgets: OK', self.harness('resources', ''))

    def test_utf16_metadata_formula_and_budget_boundary(self):
        relative, absolute, zone_size = 'emoji-😀', str(self.root / 'данные-🐱'), 19
        charge = zone_size + 2 * len(relative.encode('utf-16-le')) + 2 * len(absolute.encode('utf-16-le')) + 512
        self.assertEqual(files._utf16_bytes('😀'), 4)
        self.assertEqual(files._charge(0, relative, absolute, zone_size), charge)
        self.assertEqual(files._charge(files.MAX_METADATA - charge, relative, absolute, zone_size), files.MAX_METADATA)
        with self.assertRaises(ValueError): files._charge(files.MAX_METADATA - charge + 1, relative, absolute, zone_size)
        with self.assertRaises(ValueError): files._charge(2**63 - 1, relative, absolute, zone_size)

    def test_selection_and_zone_manifest_budget_without_pressure(self):
        selected = self.root / 'selected-😀'; selected.write_bytes(b'')
        selection_cost = files._charge(0, selected.name, selected)
        with mock.patch.object(files, 'MAX_METADATA', selection_cost - 1), mock.patch.object(files, '_pin') as pin:
            with self.assertRaises(ValueError): files.Sender([selected], ID)
            pin.assert_not_called()
        Path(str(selected) + ':Zone.Identifier').write_bytes(b'z' * files.CHUNK)
        sender = files.Sender([selected], ID)
        ancestor_cost = sum(files._charge(0, '', p) for p in files._ancestors(selected.parent))
        entry_cost = files._charge(0, selected.name, selected, files.CHUNK)
        sender._metadata_used = files.MAX_METADATA - ancestor_cost - entry_cost + 1
        with self.assertRaises(ValueError): next(sender.frames())

    def test_relative_and_absolute_depth_rejected_before_io(self):
        accepted = '/'.join(['a'] * files.MAX_DEPTH)
        rejected = accepted + '/a'
        self.assertEqual(files._path(accepted), accepted)
        with self.assertRaises(ValueError): files._path(rejected)
        absolute = Path(self.root.anchor).joinpath(*(['a'] * (files.MAX_DEPTH + 1)))
        with mock.patch.object(files, '_pin') as pin:
            with self.assertRaises(ValueError): files.Sender([absolute], ID)
            pin.assert_not_called()
        receiver = files.Receiver(self.destination, ID)
        receiver.accept(frame('begin', count=1, bytes=0))
        with mock.patch.object(Path, 'mkdir') as mkdir:
            with self.assertRaises(ValueError): receiver.accept(frame('entry', path=rejected, kind='dir', size=0, zone=None))
            mkdir.assert_not_called()

    def test_shared_sender_ancestor_pins_deduplicated(self):
        selected = []
        for index in range(128):
            p = self.root / ('selected-' + str(index)); p.write_bytes(b'x'); selected.append(p)
        expected = len(list(files._ancestors(self.root)))
        with mock.patch.object(files, '_pin', wraps=files._pin) as pin, mock.patch.object(files, 'MAX_PINS', expected):
            frames = files.Sender(selected, ID).frames()
            try:
                self.assertEqual(next(frames)['count'], 128)
                self.assertEqual(pin.call_count, expected)
            finally:
                frames.close()
        with mock.patch.object(files, '_pin', wraps=files._pin) as pin, mock.patch.object(files, 'MAX_PINS', expected - 1):
            with self.assertRaises(ValueError): next(files.Sender(selected, ID).frames())
            self.assertEqual(pin.call_count, expected - 1)

    def test_unbuffered_short_writes_abort_before_publication(self):
        receiver = files.Receiver(self.destination, ID)
        for f in packet()[:2]: receiver.accept(f)
        stage = receiver.stage
        receiver._file = mock.Mock(wraps=receiver._file)
        receiver._file.write.return_value = 1
        with self.assertRaises(OSError): receiver.accept(packet()[2])
        self.assertFalse(stage.exists())
        receiver = files.Receiver(self.destination, ID)
        for f in packet(data=b'', zone=b'z')[:2]: receiver.accept(f)
        stage = receiver.stage
        original_write_open = files._write_open
        def partial_zone(path):
            stream = mock.Mock(wraps=original_write_open(path))
            stream.write.return_value = 0
            return stream
        with mock.patch.object(files, '_write_open', side_effect=partial_zone):
            with self.assertRaises(OSError): receiver.accept(frame('file_end', sha256=hashlib.sha256(b'').hexdigest()))
        self.assertFalse(stage.exists())

    def test_receiver_resource_limits_before_creation_preserve_foreign(self):
        foreign = self.destination / 'foreign'; foreign.write_bytes(b'keep')
        for mode in ('metadata', 'pins'):
            with self.subTest(mode=mode):
                receiver = files.Receiver(self.destination, ID)
                receiver.accept(frame('begin', count=1, bytes=0))
                stage = receiver.stage
                if mode == 'metadata':
                    receiver._metadata_used = files.MAX_METADATA - 1
                    budget = mock.patch.object(files, 'MAX_PINS', files.MAX_PINS)
                else:
                    budget = mock.patch.object(files, 'MAX_PINS', len(receiver._handles))
                with budget, mock.patch.object(Path, 'mkdir') as mkdir:
                    with self.assertRaises(ValueError): receiver.accept(frame('entry', path='refused', kind='dir', size=0, zone=None))
                    mkdir.assert_not_called()
                self.assertFalse(stage.exists())
                self.assertEqual(foreign.read_bytes(), b'keep')

    def test_hardlink_selected_regular_file(self):
        original = self.root / 'original'; original.write_bytes(b'selected hardlink content')
        selected = self.root / 'selected-hardlink'
        try: os.link(original, selected)
        except PermissionError: self.skipTest('Environment denies fixture hardlink creation')
        frames = list(files.Sender([selected], ID).frames())
        received = json.loads(self.receive_cs(frames))
        self.assertEqual(Path(received['roots'][0]).read_bytes(), original.read_bytes())
        shutil.rmtree(received['stage'])
        frames = [json.loads(line) for line in self.harness('send', json.dumps([str(selected)]) + '\n').splitlines()]
        with files.Receiver(self.destination, ID) as receiver:
            for f in frames: receiver.accept(f)
            self.assertEqual(Path(receiver.roots[0]).read_bytes(), original.read_bytes())


if __name__ == '__main__':
    unittest.main()
