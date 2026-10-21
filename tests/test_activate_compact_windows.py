from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import struct
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import activate_compact_windows as activation
import compact_windows as compact


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.operation = self.root / 'images' / ('compact-' + 'a' * 32)
        self.operation.mkdir(parents=True)
        (self.root / 'state').mkdir()
        self.source = self.root / 'images/workvm-empty.qcow2'
        self.candidate = self.operation / 'candidate.qcow2'
        self.source.write_bytes(b'original-windows-data')
        self.candidate.write_bytes(b'compact-windows-data')
        self.source_id = compact._identity(self.source.stat())
        self.candidate_id = compact._identity(self.candidate.stat())
        self.prepare = self.operation / 'journal.json'
        now = datetime.now(timezone.utc)
        commands = []
        tool = self.root / 'runtime/qemu-11.1.0/qemu-img.exe'
        for arguments, stdout in [(['check', '--output=json', '-f', 'qcow2', str(self.source)], {'check-errors': 0}),
                                   (['check', '--output=json', '-f', 'qcow2', str(self.candidate)], {'check-errors': 0}),
                                   (['compare', '-f', 'qcow2', '-F', 'qcow2', str(self.source), str(self.candidate)], None)]:
            commands.append({'argv': [str(tool), *arguments], 'status': 'finished', 'returncode': 0,
                             'stdout': json.dumps(stdout) if stdout else ''})
        self.prepared = {'schema': 1, 'operation_id': 'a' * 32, 'kind': 'compact-windows-prepare',
                         'phase': 'awaiting_boot', 'awaiting_boot': True, 'activated': False,
                         'source': 'images/workvm-empty.qcow2', 'candidate': self.candidate.relative_to(self.root).as_posix(),
                         'source_identity': self.source_id, 'candidate_identity': self.candidate_id | {'virtual_size': 80 * 1024**3},
                         'source_virtual_size': 80 * 1024**3, 'source_write_denied': True,
                         'tool_sha256': compact.QEMU_IMG_SHA256, 'commands': commands,
                         'updated_utc': (now - timedelta(seconds=20)).isoformat()}
        self.prepare.write_text(json.dumps(self.prepared))
        self.boot = self.root / 'logs/compact-boot/boot-result.json'
        self.boot.parent.mkdir(parents=True)
        self.report = {'source': str(self.candidate.relative_to(self.root)), 'network': 'none',
                       'passed': True, 'phase': 'completed', 'source_write_denied': True,
                       'source_metadata_unchanged': True, 'process_still_running': False, 'exit_code': 0,
                       'started_utc': (now - timedelta(seconds=10)).isoformat(), 'finished_utc': now.isoformat(),
                       'events': [{'event': 'SHUTDOWN', 'data': {'guest': True, 'reason': 'guest-shutdown'}}]}
        self.boot.write_text(json.dumps(self.report))
        self.overlay = self.boot.parent / 'work-test.qcow2'
        name = str(self.candidate).encode('utf-8')
        self.overlay.write_bytes(struct.pack('>IIQI', 0x514649FB, 3, 104, len(name)).ljust(104, b'\0') + name)
        self.report['command'] = [str(self.root / 'runtime/qemu-11.1.0/qemu-system-x86_64.exe'),
                                  '-drive', 'if=ide,index=0,format=qcow2,file=' + self.overlay.relative_to(self.root).as_posix()]
        self.report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        self.boot.write_text(json.dumps(self.report))
        guard = mock.patch.object(activation.runtime, 'ProjectLock')
        guard.start().return_value.__enter__.return_value = True
        self.addCleanup(guard.stop)
        processes = mock.patch.object(activation.runtime, 'engine_processes', return_value=[])
        processes.start()
        self.addCleanup(processes.stop)

    def activate(self):
        return activation.activate_compact(self.root, self.prepare, self.boot)

    def test_activates_exact_files_and_retains_original(self):
        result = self.activate()
        self.assertTrue(result['activated'])
        self.assertEqual(self.source.read_bytes(), b'compact-windows-data')
        self.assertEqual((self.operation / 'original.qcow2').read_bytes(), b'original-windows-data')
        self.assertFalse(self.candidate.exists())

    def test_interrupt_before_and_after_each_move_resumes_only_forward(self):
        for checkpoint in ('before_original', 'after_original', 'before_candidate', 'after_candidate'):
            with self.subTest(checkpoint=checkpoint):
                self.setUp()
                original = activation._rename_verified
                calls = 0
                def interrupted(source, target, expected, *args):
                    nonlocal calls
                    calls += 1
                    selected = calls == (1 if 'original' in checkpoint else 2)
                    if selected and checkpoint.startswith('before'):
                        raise OSError('interrupted')
                    original(source, target, expected, *args)
                    if selected and checkpoint.startswith('after'):
                        raise OSError('interrupted')
                with mock.patch.object(activation, '_rename_verified', side_effect=interrupted):
                    with self.assertRaises(OSError):
                        self.activate()
                activation.resume_compact(self.root, self.prepare, self.boot)
                self.assertEqual(self.source.read_bytes(), b'compact-windows-data')
                self.assertEqual((self.operation / 'original.qcow2').read_bytes(), b'original-windows-data')

    def test_bad_identity_boot_and_live_process_are_rejected_without_moves(self):
        for mutation in ('identity', 'stale', 'fake', 'live'):
            with self.subTest(mutation=mutation):
                self.setUp()
                if mutation == 'identity':
                    self.candidate.write_bytes(b'tampered')
                elif mutation == 'stale':
                    self.report['started_utc'] = '2020-01-01T00:00:00+00:00'
                    self.boot.write_text(json.dumps(self.report))
                elif mutation == 'fake':
                    self.boot.write_text('{"passed":true}')
                with mock.patch.object(activation.runtime, 'engine_processes', return_value=[{}] if mutation == 'live' else []):
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.activate()
                self.assertEqual(self.source.read_bytes(), b'original-windows-data')

    def test_hardlink_and_existing_backup_never_overwrite_sentinel(self):
        sentinel = self.root / 'sentinel'
        sentinel.write_bytes(b'untouched')
        backup = self.operation / 'original.qcow2'
        os.link(sentinel, backup)
        with self.assertRaises((ValueError, RuntimeError, FileExistsError)):
            self.activate()
        self.assertEqual(sentinel.read_bytes(), b'untouched')
        backup.unlink()
        os.link(self.candidate, self.operation / 'candidate-alias')
        with self.assertRaises((ValueError, RuntimeError)):
            self.activate()

    def test_resume_never_rolls_back_after_user_writes(self):
        self.activate()
        self.source.write_bytes(b'new-user-data')
        with self.assertRaises((ValueError, RuntimeError)):
            activation.resume_compact(self.root, self.prepare, self.boot)
        self.assertEqual(self.source.read_bytes(), b'new-user-data')

    def test_native_no_replace_even_if_destination_appears_after_initial_check(self):
        destination = self.operation / 'sentinel'
        original = activation._native_file
        from contextlib import contextmanager
        @contextmanager
        def raced(path):
            with original(path) as handles:
                destination.write_bytes(b'untouched')
                yield handles
        with mock.patch.object(activation, '_native_file', side_effect=raced):
            with self.assertRaises(OSError):
                activation._rename_verified(self.candidate, destination, self.candidate_id)
        self.assertEqual(destination.read_bytes(), b'untouched')
        self.assertEqual(self.candidate.read_bytes(), b'compact-windows-data')

    def test_creationtime_tunneling_interruption_resumes_without_weakening_file_identity(self):
        original = activation._restore_creation
        count = 0
        def interrupted(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('interrupted after rename before creationtime preservation')
            return original(*args)
        with mock.patch.object(activation, '_restore_creation', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.activate()
        activation.resume_compact(self.root, self.prepare, self.boot)
        self.assertEqual(activation._identity(self.source), self.candidate_id)

    def test_junction_operation_is_rejected_without_following_it(self):
        relocated = self.operation.with_name('real-operation')
        self.operation.rename(relocated)
        result = activation.runtime.subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(self.operation), str(relocated)],
                                                   capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.addCleanup(lambda: os.rmdir(self.operation))
        with self.assertRaises((ValueError, RuntimeError)):
            self.activate()
        self.assertEqual(self.source.read_bytes(), b'original-windows-data')
        self.assertEqual((relocated / 'candidate.qcow2').read_bytes(), b'compact-windows-data')

    def test_wrong_candidate_unfinished_checks_and_wrong_shutdown_are_rejected(self):
        for mutation in ('different-source', 'check-error', 'guest-reset'):
            with self.subTest(mutation=mutation):
                self.setUp()
                if mutation == 'different-source':
                    self.report['source'] = 'images/workvm-empty.qcow2'
                elif mutation == 'check-error':
                    self.prepared['commands'][0]['returncode'] = 1
                else:
                    self.report['events'][0]['data']['reason'] = 'guest-reset'
                self.prepare.write_text(json.dumps(self.prepared))
                self.boot.write_text(json.dumps(self.report))
                with self.assertRaises((ValueError, RuntimeError)):
                    self.activate()
                self.assertEqual(self.source.read_bytes(), b'original-windows-data')

    def test_prior_overlay_evidence_is_rejected_even_when_source_claims_candidate(self):
        for mutation in ('backing-evidence', 'empty-backing-evidence', 'indirect-header', 'wrong-drive'):
            with self.subTest(mutation=mutation):
                self.setUp()
                if mutation == 'backing-evidence':
                    self.report['backing_evidence'] = 'logs/prior-boot'
                elif mutation == 'empty-backing-evidence':
                    self.report['backing_evidence'] = ''
                elif mutation == 'indirect-header':
                    prior = str(self.root / 'logs/prior-boot/work-test.qcow2').encode('utf-8')
                    self.overlay.write_bytes(struct.pack('>IIQI', 0x514649FB, 3, 104, len(prior)).ljust(104, b'\0') + prior)
                else:
                    self.report['command'][-1] = 'if=ide,index=0,format=qcow2,file=logs/prior-boot/work-test.qcow2'
                self.report['finished_utc'] = datetime.now(timezone.utc).isoformat()
                self.boot.write_text(json.dumps(self.report))
                with self.assertRaises((ValueError, RuntimeError)):
                    self.activate()
                self.assertEqual(self.source.read_bytes(), b'original-windows-data')
                self.assertFalse((self.operation / 'activation.json').exists())


if __name__ == '__main__':
    unittest.main()
