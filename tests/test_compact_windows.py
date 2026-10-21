import json
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
try:
    import compact_windows as compact
except ImportError:
    compact = None


class FakeExecutor:
    def __init__(self, compare_code=0, interrupt=False):
        self.commands = []
        self.compare_code = compare_code
        self.interrupt = interrupt

    def __call__(self, command, *, cwd):
        self.commands.append(command)
        action = command[1]
        if action == 'info':
            candidate = Path(command[-1]).name == 'candidate.qcow2'
            value = {'format': 'qcow2', 'virtual-size': 8192, 'dirty-flag': False,
                     'snapshots': [] if candidate else [{'id': 'old-snapshot'}]}
        elif action == 'check':
            value = {'check-errors': 0, 'corruptions': 0, 'leaks': 0}
        elif action == 'measure':
            value = {'required': 4096, 'fully-allocated': 16384}
        elif action == 'convert':
            Path(command[-1]).write_bytes(b'candidate current contents')
            if self.interrupt:
                raise KeyboardInterrupt('fixture interruption')
            value = {}
        elif action == 'compare':
            return subprocess.CompletedProcess(command, self.compare_code, b'', b'fixture mismatch' if self.compare_code else b'')
        else:
            raise AssertionError('Unexpected external command')
        return subprocess.CompletedProcess(command, 0, json.dumps(value).encode(), b'')


class CompactWindowsTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(compact, 'Safe Windows compaction preparation is missing')
        self.temporary = tempfile.TemporaryDirectory(prefix='compact fixture ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'images').mkdir()
        self.source = self.root / 'images/workvm-empty.qcow2'
        self.source.write_bytes(b'current Windows fixture, never replace')
        self.original = self.source.read_bytes()
        self.tool = self.root / 'runtime/qemu-11.1.0/qemu-img.exe'
        self.tool.parent.mkdir(parents=True)
        self.tool.write_bytes(b'never executed fixture')
        self.digest = mock.patch.object(compact, 'QEMU_IMG_SHA256', hashlib.sha256(self.tool.read_bytes()).hexdigest())
        self.digest.start()
        self.addCleanup(self.digest.stop)
        self.executor = FakeExecutor()
        self.processes = mock.patch.object(compact.runtime, 'engine_processes', return_value={})
        self.processes.start()
        self.addCleanup(self.processes.stop)
        self.free = mock.patch.object(compact.shutil, 'disk_usage', return_value=type('Space', (), {'free': 8 * 1024**3})())
        self.free.start()
        self.addCleanup(self.free.stop)

    def prepare(self):
        return compact.prepare_compact_windows(self.root, executor=self.executor)

    def test_isolated_build_cli_can_load_local_modules_without_starting_operations(self):
        result = subprocess.run([sys.executable, '-I', '-S', '-B', compact.__file__, '--help'],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertIn(b'--root', result.stdout)

    def test_modified_tool_is_refused_before_any_disk_command(self):
        self.tool.write_bytes(b'modified executable')
        with self.assertRaisesRegex(compact.CompactionError, 'qemu-img'):
            self.prepare()
        self.assertEqual(self.executor.commands, [])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(list((self.root / 'images').glob('compact-*')), [])

    def test_preparation_compares_current_contents_and_waits_for_separate_boot_acceptance(self):
        result = self.prepare()
        self.assertTrue(result['awaiting_boot'])
        self.assertFalse(result['ready'])
        self.assertEqual(result['phase'], 'awaiting_boot')
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertTrue((self.root / result['candidate']).is_file())
        journal = json.loads((self.root / result['evidence']).read_text())
        self.assertEqual(journal['phase'], 'awaiting_boot')
        self.assertEqual(journal['source_identity']['size'], len(self.original))
        self.assertEqual(journal['candidate_identity']['virtual_size'], 8192)
        self.assertEqual([c[1] for c in self.executor.commands], ['info', 'check', 'measure', 'convert', 'info', 'check', 'compare'])
        self.assertFalse(any(flag in command for command in self.executor.commands for flag in ('-U', '-r', '-s', '--strict')))
        self.assertFalse(hasattr(compact, 'activate'), 'Activation is owned by the primary acceptance workflow')

    def test_failed_compare_preserves_source_and_records_non_ready_failure(self):
        self.executor.compare_code = 1
        with self.assertRaises(compact.CompactionError):
            self.prepare()
        self.assertEqual(self.source.read_bytes(), self.original)
        journal = json.loads(next((self.root / 'images').glob('compact-*/journal.json')).read_text())
        self.assertEqual(journal['phase'], 'failed')
        self.assertFalse(journal['ready'])
        self.assertEqual(journal['commands'][-1]['returncode'], 1)

    def test_interrupted_convert_keeps_original_and_partial_candidate_for_inspection(self):
        self.executor.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.prepare()
        self.assertEqual(self.source.read_bytes(), self.original)
        journal = json.loads(next((self.root / 'images').glob('compact-*/journal.json')).read_text())
        self.assertEqual(journal['phase'], 'interrupted')
        self.assertFalse(journal['ready'])
        self.assertTrue((self.root / journal['candidate']).is_file())

    def test_running_guest_low_space_and_existing_operation_do_not_convert(self):
        with mock.patch.object(compact.runtime, 'engine_processes', return_value={123: {'pid': 123}}):
            with self.assertRaises(compact.CompactionError):
                self.prepare()
        self.assertEqual(self.executor.commands, [])
        with mock.patch.object(compact.shutil, 'disk_usage', return_value=type('Space', (), {'free': 1})()):
            with self.assertRaises(compact.CompactionError):
                self.prepare()
        self.assertNotIn('convert', [c[1] for c in self.executor.commands])
        operation_id = 'a' * 32
        existing = self.root / ('images/compact-' + operation_id)
        existing.mkdir()
        sentinel = existing / 'candidate.qcow2'
        sentinel.write_bytes(b'user data')
        with mock.patch.object(compact.uuid, 'uuid4', return_value=type('UUID', (), {'hex': operation_id})()):
            with self.assertRaises(compact.CompactionError):
                self.prepare()
        self.assertEqual(sentinel.read_bytes(), b'user data')

    def test_source_reparse_and_project_lock_are_rejected(self):
        self.source.unlink()
        other = self.root / 'outside.qcow2'
        other.write_bytes(b'outside fixture')
        self.source.symlink_to(other)
        with self.assertRaises(compact.CompactionError):
            self.prepare()
        self.assertEqual(self.executor.commands, [])
        self.source.unlink()
        self.source.write_bytes(self.original)
        (self.root / 'state').mkdir(exist_ok=True)
        with compact.runtime.ProjectLock(self.root) as acquired:
            self.assertTrue(acquired)
            with self.assertRaises(compact.CompactionError):
                self.prepare()
        self.assertEqual(self.executor.commands, [])

    def test_malformed_check_cannot_be_treated_as_a_clean_source(self):
        original_executor = self.executor
        def bad_check(command, *, cwd):
            if command[1] == 'check':
                return subprocess.CompletedProcess(command, 0, b'{}', b'')
            return original_executor(command, cwd=cwd)
        self.executor = bad_check
        with self.assertRaises(compact.CompactionError):
            self.prepare()
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertNotIn('convert', [c[1] for c in original_executor.commands])

    def test_source_cannot_be_written_or_deleted_while_converter_runs(self):
        original_executor = self.executor
        def probe_lock(command, *, cwd):
            if command[1] == 'convert':
                with self.assertRaises(OSError):
                    self.source.write_bytes(b'forbidden source write')
                with self.assertRaises(OSError):
                    self.source.unlink()
            return original_executor(command, cwd=cwd)
        self.executor = probe_lock
        self.prepare()
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_changed_virtual_size_or_retained_snapshots_reject_candidate_before_compare(self):
        for change in ({'virtual-size': 16384}, {'snapshots': [{'id': 'must-not-remain'}]}):
            original_executor = FakeExecutor()
            def changed_candidate(command, *, cwd):
                result = original_executor(command, cwd=cwd)
                if command[1] == 'info' and Path(command[-1]).name == 'candidate.qcow2':
                    value = json.loads(result.stdout)
                    value.update(change)
                    return subprocess.CompletedProcess(command, 0, json.dumps(value).encode(), b'')
                return result
            self.executor = changed_candidate
            with self.subTest(change=change), self.assertRaises(compact.CompactionError):
                self.prepare()
            self.assertEqual(self.source.read_bytes(), self.original)
            self.assertNotIn('compare', [c[1] for c in original_executor.commands])


if __name__ == '__main__':
    unittest.main()
