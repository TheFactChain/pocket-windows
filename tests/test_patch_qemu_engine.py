import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('patch_qemu_engine', ROOT / 'scripts/patch_qemu_engine.py')
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
import pack_existing_runtime

ORIGINAL = b'MZ\0original fixture engine'
APPROVED = b'MZ\0rebuilt fixture engine with a different size and fixed SDL backend'
APPROVED_SHA256 = hashlib.sha256(APPROVED).hexdigest()


class PatchQemuEngine(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='patch-qemu-')
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.candidate = self.project / 'approved.exe'
        self.candidate.write_bytes(APPROVED)
        approved = mock.patch.object(engine, 'ENGINE_SHA256', APPROVED_SHA256, create=True)
        approved.start(); self.addCleanup(approved.stop)
        processes = mock.patch.object(engine.runtime, 'engine_processes', return_value=[])
        processes.start(); self.addCleanup(processes.stop)
        files = {engine.ENGINE: ORIGINAL, engine.bundle.PYTHON + '/python.exe': b'python'}
        rows = []
        for name, content in files.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            rows.append({'path': name, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
        (self.project / engine.bundle.MANIFEST).write_text(json.dumps({'schema': 1, 'files': rows}), encoding='utf-8')
        with zipfile.ZipFile(self.project / engine.bundle.RECOVERY, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)

    def snapshot(self):
        return {name: (self.project / name).read_bytes() for name in (engine.ENGINE, engine.bundle.MANIFEST, engine.bundle.RECOVERY)}

    def test_approved_rebuild_different_size_and_repair_contract_consistent(self):
        before = self.snapshot()
        replaced = []
        replace = engine.os.replace
        def record_replace(source, target):
            replaced.append(Path(target).name)
            return replace(source, target)
        with mock.patch.object(engine.os, 'replace', side_effect=record_replace):
            result = engine.update_project(self.project, engine_source=self.candidate)
        data = (self.project / engine.ENGINE).read_bytes()
        self.assertTrue(result['changed'])
        self.assertNotEqual(len(data), len(ORIGINAL))
        self.assertEqual(data, APPROVED)
        self.assertEqual(result['sha256'], APPROVED_SHA256)
        self.assertEqual(result['previous_sha256'], hashlib.sha256(ORIGINAL).hexdigest())
        self.assertEqual(replaced, [Path(engine.bundle.RECOVERY).name, Path(engine.bundle.MANIFEST).name, Path(engine.ENGINE).name])
        records = json.loads((self.project / engine.bundle.MANIFEST).read_text())['files']
        row = next(row for row in records if row['path'] == engine.ENGINE)
        self.assertEqual((row['bytes'], row['sha256']), (len(APPROVED), APPROVED_SHA256))
        with (self.project / engine.bundle.RECOVERY).open('rb') as reader:
            pack_existing_runtime._validate_component_recovery(reader, records)
        with zipfile.ZipFile(self.project / engine.bundle.RECOVERY) as archive:
            self.assertEqual(archive.read(engine.bundle.PYTHON + '/python.exe'), b'python')
        for name, content in before.items():
            self.assertEqual((Path(result['backup']) / Path(name).name).read_bytes(), content)
        self.assertFalse(list(self.project.rglob('*.patching')))

    def test_second_run_without_candidate_changes_nothing(self):
        backup_root = self.project / 'backups'
        engine.update_project(self.project, backup_root, engine_source=self.candidate)
        before = self.snapshot()
        backups = list(backup_root.iterdir())
        self.assertFalse(engine.update_project(self.project)['changed'])
        self.assertFalse(engine.update_project(self.project, engine_source=self.candidate)['changed'])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(backup_root.iterdir()), backups)

    def assert_no_update_artifacts(self, before):
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.project.glob('state/qemu-engine-backup-*')))
        self.assertFalse(list(self.project.rglob('*.patching')))

    def test_candidate_required_and_unapproved_candidate_refused_without_writes(self):
        before = self.snapshot()
        with self.assertRaises(ValueError):
            engine.update_project(self.project)
        self.assert_no_update_artifacts(before)
        for content in (ORIGINAL, APPROVED + b'tampered', b'XZ' + APPROVED[2:]):
            with self.subTest(content=content):
                self.candidate.write_bytes(content)
                with self.assertRaises(ValueError):
                    engine.update_project(self.project, engine_source=self.candidate)
                self.assert_no_update_artifacts(before)

    def test_non_mz_directory_and_oversized_candidates_refused(self):
        before = self.snapshot()
        self.candidate.write_bytes(b'XZfixture')
        with mock.patch.object(engine, 'ENGINE_SHA256', hashlib.sha256(b'XZfixture').hexdigest()):
            with self.assertRaises(ValueError):
                engine.update_project(self.project, engine_source=self.candidate)
        with self.assertRaises(ValueError):
            engine.update_project(self.project, engine_source=self.project)
        self.candidate.write_bytes(APPROVED)
        original_stat = Path.stat
        def candidate_stat(path, *args, **kwargs):
            value = original_stat(path, *args, **kwargs)
            if path == self.candidate and kwargs.get('follow_symlinks', True):
                fields = list(value); fields[6] = 256 * 1024 * 1024 + 1
                return engine.os.stat_result(fields)
            return value
        with mock.patch.object(Path, 'stat', candidate_stat):
            with self.assertRaises(ValueError):
                engine.update_project(self.project, engine_source=self.candidate)
        self.assert_no_update_artifacts(before)

    def test_tampered_current_engine_is_refused_without_writes(self):
        (self.project / engine.ENGINE).write_bytes(ORIGINAL.replace(b'MZ', b'XZ'))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            engine.update_project(self.project, engine_source=self.candidate)
        self.assert_no_update_artifacts(before)

    def test_candidate_hardlink_is_refused_without_writes(self):
        before = self.snapshot()
        original_lstat = Path.lstat
        def candidate_lstat(path, *args, **kwargs):
            value = original_lstat(path, *args, **kwargs)
            if path == self.candidate:
                return mock.Mock(st_mode=value.st_mode, st_nlink=2, st_file_attributes=0)
            return value
        with mock.patch.object(Path, 'lstat', candidate_lstat):
            with self.assertRaises(ValueError):
                engine.update_project(self.project, engine_source=self.candidate)
        self.assert_no_update_artifacts(before)

    def test_recovery_member_set_and_other_member_hash_must_match(self):
        for members in ({engine.ENGINE: ORIGINAL}, {engine.ENGINE: ORIGINAL, engine.bundle.PYTHON + '/python.exe': b'corrupt'}):
            with self.subTest(members=list(members)):
                with zipfile.ZipFile(self.project / engine.bundle.RECOVERY, 'w') as archive:
                    for name, content in members.items():
                        archive.writestr(name, content)
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    engine.update_project(self.project, engine_source=self.candidate)
                self.assert_no_update_artifacts(before)

    def test_running_engine_or_leftover_attempt_is_refused_without_writes(self):
        before = self.snapshot()
        with mock.patch.object(engine.runtime, 'engine_processes', return_value=[1234]):
            with self.assertRaises(RuntimeError):
                engine.update_project(self.project, engine_source=self.candidate)
        with mock.patch.object(engine.runtime.ProjectLock, '__enter__', return_value=False):
            with self.assertRaises(RuntimeError):
                engine.update_project(self.project, engine_source=self.candidate)
        for name in (engine.ENGINE, engine.bundle.MANIFEST, engine.bundle.RECOVERY):
            leftover = self.project / (name + '.patching')
            leftover.write_bytes(b'left by an interrupted run')
            with self.assertRaises(FileExistsError):
                engine.update_project(self.project, engine_source=self.candidate)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(leftover.read_bytes(), b'left by an interrupted run')
            leftover.unlink()


class PinnedEngine(unittest.TestCase):
    def test_every_pinned_engine_hash_is_the_same_patched_engine(self):
        pinned = set()
        for name in ('portable_app.py', 'control_guest_install.py', 'reference_vm.py', 'patch_qemu_engine.py'):
            text = (ROOT / 'scripts' / name).read_text(encoding='utf-8')
            line = next(line for line in text.splitlines() if 'ENGINE_SHA256 =' in line or 'assert sha(engine)' in line)
            pinned.update(re.findall('[0-9a-f]{64}', line))
        self.assertEqual(len(pinned), 1, 'Launcher, controller and reference VM must accept the same engine')
        manifest = ROOT / engine.bundle.MANIFEST
        if manifest.exists():  # runtime/ is not in Git; checked whenever a developer copy has it
            rows = json.loads(manifest.read_text(encoding='utf-8-sig'))['files']
            self.assertEqual(pinned, {next(r['sha256'] for r in rows if r['path'] == engine.ENGINE)})


if __name__ == '__main__':
    unittest.main()
