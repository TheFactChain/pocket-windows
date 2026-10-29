"""Own private-stage fixtures only: no clipboard, boot query, VM or network."""
import base64
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import clipboard_files as core
import clipboard_stage as stage

BOOT_A, BOOT_B = '20260101000000.000000Z', '20260102000000.000000Z'


def frames():
    ident, data = 'a' * 32, b'clipboard fixture'
    return [dict(id=ident, event='begin', count=2, bytes=len(data)),
            dict(id=ident, event='entry', path='folder', kind='dir', size=0, zone=None),
            dict(id=ident, event='entry', path='folder/test.exe', kind='file', size=len(data), zone=None),
            dict(id=ident, event='data', offset=0, data=base64.b64encode(data).decode()),
            dict(id=ident, event='file_end', sha256=hashlib.sha256(data).hexdigest()), dict(id=ident, event='end')]


class ClipboardStageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='stage-build-')
        cls.exe = Path(cls.build.name) / 'ClipboardStageRegression.exe'
        compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        result = subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:x64',
                                 '/win32manifest:' + str(ROOT / 'ui/app.manifest'), '/r:System.Web.Extensions.dll',
                                 '/r:Microsoft.CSharp.dll', '/out:' + str(cls.exe),
                                 str(ROOT / 'guest/GuestEnvironment.cs'),
                                 str(ROOT / 'guest/ClipboardFiles.cs'), str(ROOT / 'guest/ClipboardStage.cs'),
                                 str(ROOT / 'tests/ClipboardStageRegression.cs')], capture_output=True)
        if result.returncode:
            cls.build.cleanup()
            raise AssertionError((result.stdout + result.stderr).decode(errors='replace'))

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='stage-fixture-')
        self.root = Path(self.temp.name)
        self.parent = self.root / 'private'
        self.parent.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def cs(self, operation, value, boot=BOOT_A, success=True):
        payload = ''.join(json.dumps(frame) + '\n' for frame in value) if operation.startswith(('park', 'abort', 'prepare')) or operation == 'export' else json.dumps(value)
        result = subprocess.run([str(self.exe), operation, str(self.parent), boot or '-'], input=payload.encode('utf-8'), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0 if success else 2, (result.stdout + result.stderr).decode(errors='replace'))
        if not success:
            return None
        if operation == 'send':
            return [json.loads(line) for line in result.stdout.splitlines()]
        return json.loads(result.stdout) if result.stdout.strip() else None

    def test_real_normal_long_parent_park_abort_and_crosslanguage_cleanup(self):
        self.parent = self.root / ('p' * 80) / ('q' * 80) / ('r' * 80) / 'private'
        Path('\\\\?\\' + str(self.parent)).mkdir(parents=True)
        self.assertGreater(len(str(self.parent)), 260)
        for creator, cleaner in [('py', 'cs'), ('cs', 'py')]:
            with self.subTest(creator=creator, cleaner=cleaner):
                value = self.parked(creator)
                self.assertFalse(str(value['stage']).startswith('\\\\?\\'))
                self.assertEqual(Path(value['roots'][0], 'test.exe').read_bytes(), b'clipboard fixture')
                self.assertEqual(self.cleanup(cleaner, BOOT_A)['removed'], 0)
                self.assertEqual(self.cleanup(cleaner, BOOT_B)['removed'], 1)
                self.assertFalse(Path(value['stage']).exists())
                self.assertFalse(Path(value['marker']).exists())
        aborted = self.cs('abort', frames())
        self.assertFalse(Path(aborted['stage']).exists())
        self.assertFalse(Path(aborted['marker']).exists())
        receiver = self.receiver()
        ticket = stage.Cache(self.parent, BOOT_A).prepare(receiver)
        ticket.abort()
        self.assertFalse(ticket.stage.exists())
        self.assertFalse(ticket.marker.exists())

    def receiver(self):
        receiver = core.Receiver(self.parent, 'a' * 32)
        for frame in frames():
            receiver.accept(frame)
        return receiver

    def parked(self, language, boot=BOOT_A):
        if language == 'cs':
            return self.cs('park', frames(), boot)
        receiver = self.receiver()
        ticket = stage.Cache(self.parent, boot).prepare(receiver)
        value = dict(stage=str(ticket.stage), roots=ticket.roots, marker=str(ticket.marker))
        ticket.park()
        receiver.close()
        with self.assertRaises(ValueError):
            ticket.abort()
        return value

    def cleanup(self, language, boot=BOOT_B, current=()):
        return (self.cs('cleanup', None if current is None else list(current), boot) if language == 'cs'
                else stage.Cache(self.parent, boot).cleanup(current))

    def test_same_boot_unknown_boot_and_current_hdrop_preserve(self):
        for creator in ('py', 'cs'):
            with self.subTest(creator=creator):
                value = self.parked(creator)
                for cleaner, boot, current in [('py', BOOT_A, []), ('cs', BOOT_A, []),
                                               ('py', None, []), ('cs', None, []),
                                               ('py', BOOT_B, value['roots']), ('cs', BOOT_B, value['roots']),
                                               ('py', BOOT_B, None), ('cs', BOOT_B, None)]:
                    result = self.cleanup(cleaner, boot, current)
                    self.assertEqual(result['removed'], 0)
                    self.assertTrue(Path(value['roots'][0], 'test.exe').exists())
                    self.assertTrue(Path(value['marker']).exists())
                self.assertEqual(self.cleanup('py')['removed'], 1)

    def test_other_boot_cross_language_lease_cleanup_after_process_exit(self):
        for creator, cleaner in [('py', 'cs'), ('cs', 'py')]:
            with self.subTest(creator=creator, cleaner=cleaner):
                value = self.parked(creator)
                marker = json.loads(Path(value['marker']).read_bytes())
                self.assertEqual(set(marker), {'schema', 'stage', 'boot_id', 'roots', 'entries', 'marker'})
                self.assertNotIn('zone', json.dumps(marker))
                self.assertNotIn('clipboard fixture', json.dumps(marker))
                self.assertTrue(all(len(entry['volume']) == 16 and len(entry['file_id']) == 16 for entry in marker['entries']))
                self.assertEqual(self.cleanup(cleaner)['removed'], 1)
                self.assertFalse(Path(value['stage']).exists())
                self.assertFalse(Path(value['marker']).exists())

    def test_unknown_child_or_foreign_replacement_never_deleted(self):
        for cleaner in ('py', 'cs'):
            for foreign in ('child', 'replacement'):
                with self.subTest(cleaner=cleaner, foreign=foreign):
                    value = self.parked('py')
                    target = Path(value['roots'][0], 'test.exe') if foreign == 'replacement' else Path(value['stage'], 'unknown')
                    if foreign == 'replacement':
                        target.rename(target.with_name('moved-original'))
                    target.write_text('foreign sentinel')
                    result = self.cleanup(cleaner)
                    self.assertGreater(result['failed'], 0)
                    self.assertEqual(target.read_text(), 'foreign sentinel')
                    self.assertTrue(Path(value['marker']).exists())
                    # Only test-owned fixtures are removed by TemporaryDirectory.
                    shutil.rmtree(value['stage'])
                    Path(value['marker']).unlink()

    def test_marker_replacement_or_malformed_marker_retains_stage(self):
        for cleaner in ('py', 'cs'):
            value = self.parked('py')
            marker = Path(value['marker'])
            data = marker.read_bytes()
            marker.rename(marker.with_suffix('.old'))
            marker.write_bytes(data)
            self.assertGreater(self.cleanup(cleaner)['failed'], 0)
            self.assertTrue(Path(value['stage']).exists())
            marker.write_bytes(b'{"schema":1,')
            self.assertGreater(self.cleanup(cleaner)['failed'], 0)
            self.assertTrue(Path(value['stage']).exists())
            shutil.rmtree(value['stage'])
            marker.unlink()
            marker.with_suffix('.old').unlink()

    def test_abort_unpublished_only_and_partial_marker_failure(self):
        receiver = self.receiver()
        cache = stage.Cache(self.parent, BOOT_A)
        with mock.patch.object(stage.os, 'fsync', side_effect=OSError('fixture flush failure')):
            with self.assertRaises(OSError):
                cache.prepare(receiver)
        self.assertTrue(receiver.stage.exists())
        self.assertEqual(list(self.parent.glob('*.lease.json')), [])
        ticket = cache.prepare(receiver)
        ticket.abort()
        self.assertFalse(ticket.stage.exists())
        self.assertFalse(ticket.marker.exists())
        value = self.cs('abort', frames())
        self.assertFalse(Path(value['stage']).exists())
        self.assertFalse(Path(value['marker']).exists())

    def test_export_resume_both_directions_and_missing_owned_entry(self):
        receiver = self.receiver()
        lease = receiver.export_lease(BOOT_A)
        original = receiver.stage
        receiver.park()
        self.cs('resume', lease)
        self.assertFalse(original.exists())
        lease = self.cs('export', frames())
        original = self.parent / lease['stage']
        (original / 'folder/test.exe').unlink()
        core.Receiver.resume_cleanup(self.parent, lease)
        self.assertFalse(original.exists())

    def test_alias_before_last_extension_matches_across_languages(self):
        paths = []
        for index in range(2):
            folder = self.root / ('source-' + str(index))
            folder.mkdir()
            path = folder / 'setup.exe'
            path.write_bytes(bytes([index + 1]))
            paths.append(str(path))
        py_frames = list(core.Sender(paths, 'a' * 32).frames())
        cs_frames = self.cs('send', paths)
        for source in (py_frames, cs_frames):
            self.assertEqual([item['path'] for item in source if item['event'] == 'entry'], ['setup.exe', 'setup (2).exe'])
            receiver = core.Receiver(self.parent, 'a' * 32)
            try:
                for frame in source:
                    receiver.accept(frame)
                self.assertEqual([Path(path).name for path in receiver.roots], ['setup.exe', 'setup (2).exe'])
                self.assertEqual([Path(path).read_bytes() for path in receiver.roots], [b'\x01', b'\x02'])
            finally:
                receiver.release()

    def test_unknown_origin_boot_and_foreign_clipboard_keep_without_probing(self):
        value = self.parked('py', None)
        for cleaner in ('py', 'cs'):
            self.assertEqual(self.cleanup(cleaner)['removed'], 0)
            self.assertTrue(Path(value['stage']).exists())
        shutil.rmtree(value['stage'])
        Path(value['marker']).unlink()
        value = self.parked('cs')
        for cleaner in ('py', 'cs'):
            result = self.cleanup(cleaner, current=['\\\\not-a-real-host\\must-not-be-probed'])
            self.assertEqual(result['removed'], 0)
            self.assertTrue(result['cleanup_unconfirmed'])
        self.assertEqual(self.cleanup('py')['removed'], 1)

    def test_boot_query_is_explicit_bounded_and_can_be_unknown(self):
        with mock.patch.object(stage.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=(BOOT_A + '\r\n').encode())) as run:
            self.assertEqual(stage.detect_boot_id(), BOOT_A)
            self.assertEqual(run.call_args.kwargs['timeout'], 5)
        with mock.patch.object(stage.subprocess, 'run', side_effect=OSError('fixture no CIM')):
            self.assertIsNone(stage.detect_boot_id())
            result = stage.Cache(self.parent, None).cleanup([])
            self.assertEqual(result['removed'], 0)
            self.assertTrue(result['cleanup_unconfirmed'])

    def test_incomplete_transfer_cannot_export_prepare_or_park(self):
        receiver = core.Receiver(self.parent, 'a' * 32)
        try:
            for action in (lambda: receiver.export_lease(BOOT_A), receiver.park,
                           lambda: stage.Cache(self.parent, BOOT_A).prepare(receiver)):
                with self.assertRaises(ValueError):
                    action()
            self.assertEqual(list(self.parent.glob('*.lease.json')), [])
        finally:
            receiver.release()
        self.cs('park', frames()[:1], success=False)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_stage_identity_and_duplicate_marker_fields_fail_closed(self):
        for cleaner in ('py', 'cs'):
            value = self.parked('py')
            original = Path(value['stage'])
            moved = self.parent / 'moved-owned-stage'
            original.rename(moved)
            original.mkdir()
            (original / 'foreign').write_text('foreign sentinel')
            self.assertGreater(self.cleanup(cleaner)['failed'], 0)
            self.assertEqual((original / 'foreign').read_text(), 'foreign sentinel')
            self.assertEqual((moved / 'folder/test.exe').read_bytes(), b'clipboard fixture')
            shutil.rmtree(original)
            moved.rename(original)
            marker = Path(value['marker'])
            raw = marker.read_text()
            self.assertIn('"schema":1', raw)
            marker.write_text(raw.replace('"schema":1', '"schema":1,"\\u0073chema":1', 1))
            self.assertGreater(self.cleanup(cleaner)['failed'], 0)
            self.assertTrue(original.exists())
            shutil.rmtree(original)
            marker.unlink()

    def test_park_after_publication_does_not_depend_on_marker_still_existing(self):
        receiver = self.receiver()
        ticket = stage.Cache(self.parent, BOOT_A).prepare(receiver)
        ticket.marker.unlink()
        ticket.park()
        receiver.release()
        self.assertTrue((ticket.stage / 'folder/test.exe').exists())
        value = self.cs('park-marker-missing', frames())
        self.assertTrue(Path(value['roots'][0], 'test.exe').exists())
        self.assertEqual(self.cleanup('py')['removed'], 0)

    def test_abort_never_deletes_replacement_marker(self):
        receiver = self.receiver()
        try:
            ticket = stage.Cache(self.parent, BOOT_A).prepare(receiver)
            ticket.marker.unlink()
            ticket.marker.write_text('foreign marker')
            with self.assertRaises((OSError, ValueError)):
                ticket.abort()
            self.assertEqual(ticket.marker.read_text(), 'foreign marker')
            self.assertFalse(ticket.stage.exists())
            ticket.marker.unlink()
        finally:
            receiver.release()
        self.cs('abort-foreign-marker', frames(), success=False)
        marker = next(self.parent.glob('*.lease.json'))
        self.assertEqual(marker.read_text(), 'foreign marker')

    def test_malformed_leases_are_rejected_before_owned_entries_change(self):
        receiver = self.receiver()
        lease = receiver.export_lease(BOOT_A)
        root = receiver.stage
        receiver.park()
        for change in ('escape', 'zone', 'file_id', 'roots'):
            value = json.loads(json.dumps(lease))
            if change == 'escape':
                value['entries'][1]['path'] = '../outside'
            elif change == 'zone':
                value['entries'][1]['zone'] = 'not metadata'
            elif change == 'file_id':
                value['entries'][1]['file_id'] = '0' * 16
            else:
                value['roots'] = ['other']
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    core.Receiver.resume_cleanup(self.parent, value)
                self.cs('resume', value, success=False)
                self.assertTrue((root / 'folder/test.exe').exists())
        with mock.patch.object(core, 'MAX_ENTRIES', 1):
            with self.assertRaises(ValueError):
                core.Receiver.resume_cleanup(self.parent, lease)
        with mock.patch.object(stage, 'MAX_LEASE_BYTES', 32):
            value = self.parked('cs')
            self.assertGreater(stage.Cache(self.parent, BOOT_B).cleanup([])['failed'], 0)
            self.assertTrue(Path(value['stage']).exists())
        core.Receiver.resume_cleanup(self.parent, lease)

    def test_open_reader_refuses_cleanup_without_force_then_retries(self):
        for cleaner in ('py', 'cs'):
            value = self.parked('cs')
            target = Path(value['roots'][0], 'test.exe')
            with core._read_open(target) as held:
                self.assertGreater(self.cleanup(cleaner)['failed'], 0)
                self.assertEqual(held.read(), b'clipboard fixture')
                self.assertTrue(Path(value['marker']).exists())
            self.assertEqual(self.cleanup(cleaner)['removed'], 1)

    def test_prepare_collision_does_not_remove_foreign_marker(self):
        receiver = self.receiver()
        marker = Path(str(receiver.stage) + '.lease.json')
        marker.write_text('foreign marker')
        try:
            with self.assertRaises(OSError):
                stage.Cache(self.parent, BOOT_A).prepare(receiver)
            self.assertEqual(marker.read_text(), 'foreign marker')
        finally:
            receiver.release()
        marker.unlink()
        self.cs('prepare-foreign-marker', frames(), success=False)
        marker = next(self.parent.glob('*.lease.json'))
        self.assertEqual(marker.read_text(), 'foreign marker')

    def test_aliases_dotfile_directory_and_last_extension(self):
        for name, directory, alias in [('.profile', False, '.profile (2)'),
                                       ('archive.tar.gz', False, 'archive.tar (2).gz'),
                                       ('directory.exe', True, 'directory.exe (2)'),
                                       ('same', False, 'same (2)')]:
            paths = []
            for index in range(2):
                folder = self.root / (name + '-source-' + str(index))
                folder.mkdir()
                path = folder / name
                if directory:
                    path.mkdir()
                else:
                    path.write_bytes(bytes([index]))
                paths.append(str(path))
            for source in (list(core.Sender(paths, 'a' * 32).frames()), self.cs('send', paths)):
                self.assertEqual([item['path'] for item in source if item['event'] == 'entry'], [name, alias])

    def test_reparse_stage_never_reaches_foreign_target(self):
        value = self.parked('py')
        original = Path(value['stage'])
        moved = self.parent / 'moved-owned'
        original.rename(moved)
        foreign = self.root / 'foreign'
        foreign.mkdir()
        (foreign / 'sentinel').write_text('keep')
        command = "New-Item -ItemType Junction -Path '" + str(original).replace("'", "''") + "' -Target '" + str(foreign).replace("'", "''") + "' | Out-Null"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, timeout=15)
        if result.returncode:
            if original.exists():
                self.assertTrue(original.is_junction())
                original.rmdir()
            moved.rename(original)
            error = result.stderr.decode(errors='replace')
            if any(word in error for word in ('PermissionDenied', 'UnauthorizedAccessException', 'Access is denied', 'Отказано в доступе')):
                self.skipTest('Environment denies own fixture junction creation')
            self.fail(error)
        try:
            for cleaner in ('py', 'cs'):
                self.assertGreater(self.cleanup(cleaner)['failed'], 0)
                self.assertEqual((foreign / 'sentinel').read_text(), 'keep')
                self.assertTrue((moved / 'folder/test.exe').exists())
        finally:
            original.rmdir()  # Own junction itself only; never its target.

    def test_prepared_ticket_owns_lifetime_before_park(self):
        receiver = self.receiver()
        ticket = stage.Cache(self.parent, BOOT_A).prepare(receiver)
        receiver.close()
        self.assertTrue((ticket.stage / 'folder/test.exe').exists())
        with self.assertRaises(ValueError):
            receiver.accept({'id': 'a' * 32, 'event': 'end'})
        self.assertTrue((ticket.stage / 'folder/test.exe').exists())
        ticket.park()
        value = self.cs('park-after-dispose', frames())
        self.assertTrue(Path(value['roots'][0], 'test.exe').exists())


if __name__ == '__main__':
    unittest.main()
