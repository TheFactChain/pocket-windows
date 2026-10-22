"""Release consistency and application-only update/rollback on disposable fixtures."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_portable', ROOT / 'scripts/release_portable.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def record(name, data):
    return {'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='release fixture ')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.bundle = self.base / 'release'
        self.app = self.bundle / 'app'
        self.target = self.base / 'Личная копия'
        (self.target / 'state').mkdir(parents=True)
        self.info = {'schema': 1, 'release_id': '2026.10.23.1', 'source_commit': 'a' * 40}
        for name in release.pack.APP_FILES:
            self.write(self.app, name, b'new code: ' + name.encode())
            self.write(self.target, name, b'old code: ' + name.encode())
        pe = bytearray(134)
        pe[:2] = b'MZ'
        pe[60:64] = (128).to_bytes(4, 'little')
        pe[128:] = b'PE\0\0\x64\x86'
        self.write(self.app, 'Launcher.exe', pe)
        self.old_launcher = b'old launcher'
        self.write(self.target, 'Launcher.exe', self.old_launcher)
        self.write(self.app, 'release-info.json', json.dumps(self.info).encode())
        records = []
        for name in release.REQUIRED_RUNTIME:
            data = name.encode()
            self.write(self.app, name, data)
            records.append(record(name, data))
        self.write(self.app, release.pack.COMPONENT_FILES[0], json.dumps({'schema': 1, 'files': records}).encode())
        with zipfile.ZipFile(self.app / release.pack.COMPONENT_FILES[1], 'w') as archive:
            for row in records:
                archive.writestr(row['path'], (self.app / row['path']).read_bytes())
        self.write(self.bundle, 'Setup.exe', pe)
        self.manifest = {'schema': 1, 'release_id': self.info['release_id'], 'source_commit': self.info['source_commit'],
                         'created_utc': '2026-10-23T00:00:00+00:00', 'common_files': release.common_records(self.app),
                         'artifacts': {'Setup.exe': record('Setup.exe', bytes(pe))},
                         'master': {'work_sha256': 'b' * 64, 'vars_sha256': 'c' * 64}, 'manual_acceptance': None}
        self.save_manifest()
        self.protected = ('images/workvm-empty.qcow2', 'state/workvm-vars.fd', 'profiles/profiles.json',
                          'config/runtime.json', 'config/windows.json', 'config/viewer.json', 'config/files.json',
                          'gateway/packages/current.json')
        for name in self.protected:
            self.write(self.target, name, b'KEEP PRIVATE: ' + name.encode())
        self.before = {name: (self.target / name).read_bytes() for name in self.protected}
        guard = mock.patch.object(release, 'project_processes', return_value={})
        guard.start()
        self.addCleanup(guard.stop)

    @staticmethod
    def write(root, name, data):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def save_manifest(self):
        self.write(self.bundle, 'RELEASE.json', json.dumps(self.manifest).encode())

    def unchanged_private(self):
        self.assertEqual({name: (self.target / name).read_bytes() for name in self.protected}, self.before)

    def test_update_installs_exact_release_and_preserves_private_data(self):
        result = release.apply_update(self.bundle, self.target)
        self.assertEqual(result['release_id'], self.info['release_id'])
        for row in self.manifest['common_files']:
            self.assertEqual((self.target / row['path']).read_bytes(), (self.app / row['path']).read_bytes())
        self.assertFalse((self.target / release.JOURNAL).exists())
        self.assertEqual((Path(result['backup']) / 'old/Launcher.exe').read_bytes(), self.old_launcher)
        self.unchanged_private()

    def test_tampered_input_is_rejected_before_target_changes(self):
        (self.app / 'scripts/portable_app.py').write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            release.apply_update(self.bundle, self.target)
        self.assertEqual((self.target / 'Launcher.exe').read_bytes(), self.old_launcher)
        self.assertFalse((self.target / release.JOURNAL).exists())
        self.unchanged_private()

    def test_setup_tampering_is_rejected(self):
        (self.bundle / 'Setup.exe').write_bytes(b'changed installer')
        with self.assertRaises(ValueError):
            release.apply_update(self.bundle, self.target)
        self.assertFalse((self.target / release.JOURNAL).exists())

    def test_mismatched_application_commit_is_rejected(self):
        self.write(self.app, 'release-info.json', json.dumps(dict(self.info, source_commit='b'*40)).encode())
        self.manifest['common_files'] = release.common_records(self.app)
        self.save_manifest()
        with self.assertRaises(ValueError):
            release.verify_release(self.bundle)

    def test_busy_target_is_rejected_without_side_by_side_executables(self):
        with mock.patch.object(release, 'project_processes', return_value={123: {'pid': 123}}):
            with self.assertRaises(RuntimeError):
                release.apply_update(self.bundle, self.target)
        self.assertEqual(list(self.target.glob('Launcher*')), [self.target / 'Launcher.exe'])
        self.assertFalse((self.target / release.JOURNAL).exists())

    def test_locked_target_aborts_before_partial_install(self):
        with release.locked_read(self.target / 'Launcher.exe'):
            with self.assertRaises((OSError, ValueError, RuntimeError)):
                release.apply_update(self.bundle, self.target)
        self.assertEqual((self.target / 'scripts/portable_app.py').read_bytes(), b'old code: scripts/portable_app.py')
        self.assertFalse((self.target / release.JOURNAL).exists())
        self.unchanged_private()

    def test_mid_update_failure_rolls_back_installed_files(self):
        original = os.replace
        failed = []
        def replace(source, target):
            if Path(target) == self.target / 'scripts/portable_app.py' and not failed:
                failed.append(True)
                raise PermissionError('Synthetic late sharing violation')
            return original(source, target)
        with mock.patch.object(release.os, 'replace', side_effect=replace):
            with self.assertRaises(OSError):
                release.apply_update(self.bundle, self.target)
        self.assertTrue(failed)
        self.assertEqual((self.target / 'Launcher.exe').read_bytes(), self.old_launcher)
        self.assertEqual((self.target / 'scripts/portable_app.py').read_bytes(), b'old code: scripts/portable_app.py')
        self.assertFalse((self.target / 'release-info.json').exists())
        self.assertFalse((self.target / release.JOURNAL).exists())
        self.unchanged_private()

    def test_failed_rollback_keeps_marker_and_can_be_resumed(self):
        original = os.replace
        failed = []
        def replace(source, target):
            target = Path(target)
            if target == self.target / 'scripts/portable_app.py' and not failed:
                failed.append('install')
                raise PermissionError('Synthetic install failure')
            if target == self.target / 'Launcher.exe' and failed == ['install']:
                failed.append('rollback')
                raise PermissionError('Synthetic rollback failure')
            return original(source, target)
        with mock.patch.object(release.os, 'replace', side_effect=replace):
            with self.assertRaises(RuntimeError):
                release.apply_update(self.bundle, self.target)
        self.assertTrue((self.target / release.JOURNAL).exists())
        release.rollback_update(self.target)
        self.assertEqual((self.target / 'Launcher.exe').read_bytes(), self.old_launcher)
        self.assertFalse((self.target / release.JOURNAL).exists())
        self.unchanged_private()

    def test_interrupted_rollback_copy_can_be_retried(self):
        original_replace, original_copy = os.replace, release.copy_record
        failures = []
        def replace(source, target):
            if Path(target) == self.target / 'scripts/portable_app.py' and not failures:
                failures.append('install')
                raise PermissionError('Synthetic install failure')
            return original_replace(source, target)
        def copy(source, destination, expected):
            if 'restore' in Path(destination).parts and Path(source).name == 'Launcher.exe' and failures == ['install']:
                failures.append('copy')
                release.directory(Path(destination).parent)
                Path(destination).write_bytes(b'interrupted rollback copy')
                raise OSError('Synthetic interrupted copy')
            return original_copy(source, destination, expected)
        with mock.patch.object(release.os, 'replace', side_effect=replace), mock.patch.object(release, 'copy_record', side_effect=copy):
            with self.assertRaises(RuntimeError):
                release.apply_update(self.bundle, self.target)
        self.assertEqual(failures, ['install', 'copy'])
        release.rollback_update(self.target)
        self.assertEqual((self.target / 'Launcher.exe').read_bytes(), self.old_launcher)
        self.assertFalse((self.target / release.JOURNAL).exists())
        self.unchanged_private()

    def test_release_records_cannot_target_data_or_escape(self):
        for name in ('images/workvm-empty.qcow2', 'profiles/profiles.json', 'config/runtime.json',
                     'state/workvm-vars.fd', '../Launcher.exe', 'runtime/../profiles/private.json'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.validate_common_records([record(name, b'unsafe')])

    def test_release_info_requires_exact_valid_provenance(self):
        self.assertEqual(release.validate_release_info(self.info), self.info)
        for key, value in (('schema', True), ('source_commit', 'HEAD'), ('release_id', '../release')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                release.validate_release_info(dict(self.info, **{key: value}))

    def test_existing_marker_requires_explicit_recovery(self):
        self.write(self.target, release.JOURNAL, b'{}')
        with self.assertRaises(RuntimeError):
            release.apply_update(self.bundle, self.target)
        self.assertEqual((self.target / 'Launcher.exe').read_bytes(), self.old_launcher)

    def test_dirty_sources_cannot_be_identified_as_a_release_commit(self):
        with mock.patch.object(release, '_git', side_effect=['a' * 40, ' M ui/Launcher.cs']):
            with self.assertRaises(RuntimeError):
                release.source_commit(ROOT)
        with mock.patch.object(release, '_git', side_effect=['a' * 40, '']):
            self.assertEqual(release.source_commit(ROOT), 'a' * 40)

    def test_ignored_compiler_input_cannot_enter_a_release(self):
        self.write(self.app, 'ui/.envlocal.cs', b'class Extra {}')
        def git(source, *args):
            if args[0] == 'rev-parse':
                raise release.subprocess.CalledProcessError(128, args)
            return 'b' * 40
        with mock.patch.object(release, '_git', side_effect=git), self.assertRaises(RuntimeError):
            release.validate_build_inputs(self.app, self.info['source_commit'])

    def test_oversized_journal_refuses_update_before_file_replacement(self):
        with mock.patch.object(release, 'JSON_LIMIT', 1024, create=True), self.assertRaises(ValueError):
            release.apply_update(self.bundle, self.target)
        self.assertEqual((self.target / 'Launcher.exe').read_bytes(), self.old_launcher)
        self.assertFalse((self.target / release.JOURNAL).exists())
        self.unchanged_private()

    def test_insufficient_space_refuses_update_before_journaling(self):
        with mock.patch.object(release.shutil, 'disk_usage', return_value=mock.Mock(free=0)):
            with self.assertRaises(RuntimeError):
                release.apply_update(self.bundle, self.target)
        self.assertFalse((self.target / release.JOURNAL).exists())
        self.unchanged_private()

    def test_hardlinked_target_is_rejected(self):
        path = self.target / 'scripts/portable_app.py'
        os.link(path, self.base / 'extra-link')
        with self.assertRaises(ValueError):
            release.apply_update(self.bundle, self.target)
        self.assertEqual((self.target / 'Launcher.exe').read_bytes(), self.old_launcher)

    def test_publication_requires_owner_acceptance(self):
        with self.assertRaises(RuntimeError):
            release.publish_release(self.bundle, self.base / 'Installer')
        self.assertFalse((self.base / 'Installer').exists())


if __name__ == '__main__':
    unittest.main()
