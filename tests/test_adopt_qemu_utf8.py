"""Exercise the nine-file adoption only in temporary project fixtures."""
import hashlib
import importlib.util
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('adopt_qemu_utf8', ROOT / 'scripts/adopt_qemu_utf8.py')
adoption = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adoption)

OLD_ENGINE = b'MZ\0old fixture engine'
NEW_ENGINE = b'MZ\0new UTF8 fixture engine'
OLD_IMG = b'MZ\0old fixture image tool'
NEW_IMG = b'MZ\0new UTF8 fixture image tool'


def sha(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(os.name == 'nt', 'requires the real Windows project lock')
class AdoptQemuUtf8(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='qemu-adopt-fixture-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.work = self.base / 'prepared'
        self.engine = self.base / 'new-engine.exe'
        self.img = self.base / 'new-img.exe'
        self.engine.write_bytes(NEW_ENGINE)
        self.img.write_bytes(NEW_IMG)
        hashes = mock.patch.multiple(adoption, OLD_ENGINE_SHA256=sha(OLD_ENGINE), NEW_ENGINE_SHA256=sha(NEW_ENGINE),
                                     OLD_IMG_SHA256=sha(OLD_IMG), NEW_IMG_SHA256=sha(NEW_IMG))
        hashes.start(); self.addCleanup(hashes.stop)
        processes = mock.patch.object(adoption, '_project_processes', return_value=[])
        processes.start(); self.addCleanup(processes.stop)
        members = {adoption.patch.ENGINE: OLD_ENGINE, adoption.patch.IMG: OLD_IMG,
                   adoption.bundle.PYTHON + '/python.exe': b'untouched python fixture'}
        rows = []
        for name, data in members.items():
            destination = self.project / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            rows.append({'path': name, 'bytes': len(data), 'sha256': sha(data)})
        (self.project / adoption.bundle.MANIFEST).write_text(json.dumps({'schema': 1, 'files': rows}), encoding='utf-8')
        with zipfile.ZipFile(self.project / adoption.bundle.RECOVERY, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        for name in adoption.ENGINE_SOURCES:
            destination = self.project / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(("# preserve this exact prefix\r\nENGINE_SHA256 = '" + sha(OLD_ENGINE) + "'  # strict\r\nother = 'unchanged'\r\n").encode())
        (self.project / adoption.IMG_SOURCE).write_bytes(("QEMU_IMG_SHA256 = '" + sha(OLD_IMG) + "'\r\n\r\ndef qemu_path(path):\r\n    return path  # untouched\r\n").encode())

    def snapshot(self):
        return {name: (self.project / name).read_bytes() for name in adoption.FILES}

    def prepare(self):
        return adoption.prepare(self.project, self.work, engine_source=self.engine, qemu_img_source=self.img)

    def test_prepare_isolated_then_adopt_keeps_all_nine_files_coherent(self):
        before = self.snapshot()
        prepared = self.prepare()
        self.assertEqual(prepared['state'], 'prepared')
        self.assertEqual(self.snapshot(), before, 'preparation must not modify the project')
        result = adoption.adopt(self.project, self.work)
        self.assertEqual(result['state'], 'committed')
        self.assertEqual(adoption._snapshot(self.project), prepared['after'])
        for name, data in before.items():
            self.assertEqual((self.work / 'backup' / name).read_bytes(), data)
        for name in adoption.ENGINE_SOURCES:
            self.assertEqual((self.project / name).read_bytes(), before[name].replace(sha(OLD_ENGINE).encode(), sha(NEW_ENGINE).encode()))
        self.assertEqual((self.project / adoption.IMG_SOURCE).read_bytes(), before[adoption.IMG_SOURCE].replace(sha(OLD_IMG).encode(), sha(NEW_IMG).encode()))
        adoption._runtime_contract(self.project, {adoption.patch.ENGINE: sha(NEW_ENGINE), adoption.patch.IMG: sha(NEW_IMG)})
        journal = (self.work / adoption.JOURNAL).read_text(encoding='utf-8')
        self.assertNotIn('unchanged', journal, 'journal contains hashes, not source contents')
        self.assertEqual(json.loads(journal)['replaced'], list(adoption.FILES))

    def test_bad_candidate_or_unknown_pin_has_zero_project_writes(self):
        before = self.snapshot()
        self.img.write_bytes(NEW_IMG + b'tampered')
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.work.exists())
        self.img.write_bytes(NEW_IMG)
        path = self.project / adoption.ENGINE_SOURCES[0]
        path.write_bytes(path.read_bytes().replace(sha(OLD_ENGINE).encode(), b'0' * 64))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.work.exists())

    def test_processes_or_changed_sources_refuse_adoption_before_backup(self):
        self.prepare()
        before = self.snapshot()
        with mock.patch.object(adoption, '_project_processes', return_value=[123]), self.assertRaises(RuntimeError):
            adoption.adopt(self.project, self.work)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.work / 'backup').exists())
        path = self.project / adoption.ENGINE_SOURCES[0]
        path.write_bytes(path.read_bytes() + b'# a new owner edit\n')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            adoption.adopt(self.project, self.work)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.work / 'backup').exists())

    def test_insufficient_space_for_backup_and_full_rollback_refuses_before_writes(self):
        plan = self.prepare()
        before = self.snapshot()
        journal = (self.work / adoption.JOURNAL).read_bytes()
        backup_bytes = sum(row['bytes'] for row in plan['before'].values())
        required = 2 * backup_bytes + 16 * 1024 * 1024
        with mock.patch.object(adoption.shutil, 'disk_usage', return_value=mock.Mock(free=required - 1)), \
                self.assertRaisesRegex(OSError, 'backup and rollback'):
            adoption.adopt(self.project, self.work)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.work / 'backup').exists())
        self.assertFalse((self.work / 'rollback').exists())
        self.assertEqual((self.work / adoption.JOURNAL).read_bytes(), journal)

    def test_short_name_cannot_place_nonexistent_work_inside_project(self):
        parent = self.project / 'long adoption work parent'
        parent.mkdir()
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetShortPathNameW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        kernel.GetShortPathNameW.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        size = kernel.GetShortPathNameW(str(parent), buffer, len(buffer))
        if not size or size >= len(buffer) or os.path.normcase(buffer.value) == os.path.normcase(str(parent)):
            self.skipTest('8.3 short names are unavailable for this fixture')
        work = Path(buffer.value) / 'not-created'
        self.assertEqual(work.parent.resolve(), parent.resolve())
        with self.assertRaisesRegex(ValueError, 'separate work directory'):
            adoption._paths(self.project, work)
        self.assertFalse((parent / 'not-created').exists())

    def test_prepared_source_tampering_cannot_change_more_than_pin_literals(self):
        self.prepare()
        target = self.work / 'stage' / adoption.ENGINE_SOURCES[0]
        target.write_bytes(target.read_bytes() + b'# unexpected source edit\n')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            adoption.adopt(self.project, self.work)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.work / 'backup').exists())

    def test_prepared_runtime_cannot_adopt_an_unrelated_component_change(self):
        plan = self.prepare()
        stage = self.work / 'stage'
        python = adoption.bundle.PYTHON + '/python.exe'
        rows = json.loads((stage / adoption.bundle.MANIFEST).read_text(encoding='utf-8'))['files']
        with zipfile.ZipFile(stage / adoption.bundle.RECOVERY) as archive:
            members = {member.filename: archive.read(member) for member in archive.infolist()}
        members[python] = b'unexpected new python'
        for row in rows:
            if row['path'] == python:
                row.update(bytes=len(members[python]), sha256=sha(members[python]))
        (stage / adoption.bundle.MANIFEST).write_text(json.dumps({'schema': 1, 'files': rows}), encoding='utf-8')
        with zipfile.ZipFile(stage / adoption.bundle.RECOVERY, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        plan['after'] = adoption._snapshot(stage)
        adoption._journal(self.work, plan, 'prepared')
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'unrelated components'):
            adoption.adopt(self.project, self.work)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.work / 'backup').exists())

    def interruption(self, target_name, *, exact_space=False):
        plan = self.prepare()
        before = self.snapshot()
        replace = adoption.os.replace
        interrupted, rollback_checked = [], []
        def failure(source, target):
            target = Path(target)
            if target == self.project / target_name and not interrupted:
                interrupted.append(True)
                raise OSError('fixture replace failure')
            if target == self.work / adoption.JOURNAL:
                state = json.loads(Path(source).read_text(encoding='utf-8'))['state']
                if state == 'committed' and target_name == 'commit-journal' and not interrupted:
                    interrupted.append(True)
                    raise OSError('fixture replace failure')
                if state == 'rolled_back':
                    self.assertEqual(self.snapshot(), before, 'all nine files must be restored before unlocking')
                    with adoption.runtime.ProjectLock(self.project) as available:
                        self.assertFalse(available, 'rollback must finish while the original lock remains held')
                    rollback_checked.append(True)
            return replace(source, target)
        free = 2 * sum(row['bytes'] for row in plan['before'].values()) + 16 * 1024 * 1024
        usage = mock.patch.object(adoption.shutil, 'disk_usage', return_value=mock.Mock(free=free)) if exact_space else mock.patch.object(adoption.shutil, 'disk_usage', wraps=adoption.shutil.disk_usage)
        with usage, mock.patch.object(adoption.os, 'replace', side_effect=failure), self.assertRaisesRegex(OSError, 'fixture replace failure'):
            adoption.adopt(self.project, self.work)
        self.assertEqual(rollback_checked, [True])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(json.loads((self.work / adoption.JOURNAL).read_text(encoding='utf-8'))['state'], 'rolled_back')
        for name, data in before.items():
            self.assertEqual((self.work / 'backup' / name).read_bytes(), data)
        with adoption.runtime.ProjectLock(self.project) as available:
            self.assertTrue(available, 'lock releases only after verified rollback')

    def test_mid_runtime_replace_failure_rolls_back_all_nine_before_unlock(self):
        self.interruption(adoption.bundle.MANIFEST)

    def test_reserved_budget_allows_rollback_after_runtime_replacement(self):
        self.interruption(adoption.bundle.MANIFEST, exact_space=True)

    def test_mid_source_replace_failure_rolls_back_all_nine_before_unlock(self):
        self.interruption(adoption.ENGINE_SOURCES[1])

    def test_final_journal_failure_rolls_back_all_nine_before_unlock(self):
        self.interruption('commit-journal')

    def test_recover_requires_explicit_interrupted_state_and_preserved_backup(self):
        plan = self.prepare()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            adoption.recover(self.project, self.work)
        backup = self.work / 'backup'
        for name in adoption.FILES:
            destination = backup / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(before[name])
        (self.project / adoption.patch.ENGINE).write_bytes(NEW_ENGINE)
        adoption._journal(self.work, plan, 'committing', [adoption.patch.ENGINE])
        self.assertEqual(adoption.recover(self.project, self.work)['state'], 'recovered')
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
