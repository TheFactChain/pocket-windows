"""Provisional installs must prove the new app or restore the previous files."""
import hashlib
import json
import shutil
import uuid
from pathlib import Path
import unittest
from unittest import mock

import test_release_portable as fixtures

release = fixtures.release


class UpdateTransactionTests(unittest.TestCase):
    setUp = fixtures.ReleaseTests.setUp
    write = staticmethod(fixtures.ReleaseTests.write)
    save_manifest = fixtures.ReleaseTests.save_manifest
    unchanged_private = fixtures.ReleaseTests.unchanged_private
    recovery_fixture = fixtures.ReleaseTests.recovery_fixture

    def test_probe_precedes_commit_and_keeps_recovery_available(self):
        def accept(target, journal):
            active = release.read_json(target / release.JOURNAL)
            self.assertEqual(active['schema'], 3)
            self.assertEqual(active['phase'], 'awaiting_probe')
            self.assertEqual(active['release'], self.info)
            self.assertEqual((target / 'Launcher.exe').read_bytes(), (self.app / 'Launcher.exe').read_bytes())
            self.assertTrue((target / 'state' / journal['transaction'] / 'old/Launcher.exe').is_file())
        with mock.patch.object(release, 'confirm_probe', side_effect=accept):
            result = release.apply_update(self.bundle, self.target, probe=True)
        self.assertFalse((self.target / release.JOURNAL).exists())
        self.assertEqual(release.read_json(Path(result['backup']) / 'journal.json')['phase'], 'committed')
        self.unchanged_private()

    def test_failed_probe_rolls_back_application(self):
        with mock.patch.object(release, 'confirm_probe', side_effect=TimeoutError('new panel crashed')):
            with self.assertRaisesRegex(TimeoutError, 'crashed'):
                release.apply_update(self.bundle, self.target, probe=True)
        self.assertEqual((self.target / 'Launcher.exe').read_bytes(), self.old_launcher)
        self.assertFalse((self.target / release.JOURNAL).exists())
        self.unchanged_private()

    def test_result_log_failure_does_not_prevent_restart(self):
        package = release.package_app(self.bundle)
        actual = release.atomic_json
        def fail_result(path, value):
            if path.name == 'update-result.json':
                raise OSError('disk full')
            return actual(path, value)
        with mock.patch.object(release, 'wait_for_launcher'), \
                mock.patch.object(release, 'atomic_json', side_effect=fail_result), \
                mock.patch.object(release.subprocess, 'Popen') as launch:
            result = release.run_app_update(self.target, 123, 456, package['package'], package['sha256'], self.info['release_id'])
        self.assertEqual(result['status'], 'committed')
        launch.assert_called_once()

    def test_completed_owned_download_is_removed_but_release_is_kept(self):
        package = release.package_app(self.bundle)
        work = self.target / 'state' / ('app-updater-' + 'f' * 32)
        self.write(work, 'app.zip', Path(package['package']).read_bytes())
        self.write(work, 'download.json', json.dumps(dict(schema=1, release_id=self.info['release_id'], sha256=package['sha256'])).encode())
        with mock.patch.object(release, 'wait_for_launcher'), mock.patch.object(release.subprocess, 'Popen'):
            result = release.run_app_update(self.target, 123, 456, work / 'app.zip', package['sha256'], self.info['release_id'])
        self.assertEqual(result['status'], 'committed')
        self.assertFalse(work.exists())
        self.assertTrue(Path(package['package']).exists())

    def test_recovery_accepts_provisional_schema_and_preserves_data(self):
        journal = self.recovery_fixture(2, 'installed')
        journal.update(schema=3, phase='awaiting_probe', release=self.info,
                       probe={'nonce': 'd' * 32, 'pid': 123, 'created': '456'})
        release.atomic_json(self.target / release.JOURNAL, journal)
        release.rollback_update(self.target)
        self.assertEqual((self.target / 'Launcher.exe').read_bytes(), self.old_launcher)
        self.unchanged_private()

    def test_probe_refuses_runtime_that_cannot_execute(self):
        installed = release.apply_update(self.bundle, self.target)
        journal = release.read_json(Path(installed['backup']) / 'journal.json')
        journal.update(schema=3, phase='awaiting_probe', release=self.info,
                       probe={'nonce': 'd' * 32, 'pid': 123, 'created': '456'})
        release.atomic_json(self.target / release.JOURNAL, journal)
        process = dict(pid=123, created_filetime=456, image=str(self.target / 'Launcher.exe'))
        with mock.patch.object(release.runtime, 'process_identity', return_value=process), \
                mock.patch.object(release.subprocess, 'run', return_value=mock.Mock(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, 'runtime'):
                release.probe_app(self.target, 'd' * 32)

    def v2_stage(self):
        import app_update_v2 as parts
        toolkit = self.bundle / 'toolkit.zip'
        toolkit.write_bytes(b'fixture toolkit')
        result = parts.build(self.bundle, toolkit)
        index = parts.read_index(result['index'], result['index_sha256'])
        parts._inventory(index, self.app, lambda value: None, populate=True)
        shutil.copytree(self.app / 'state/update-cache', self.target / 'state/update-cache')
        stage = self.target / 'state' / uuid.uuid4().hex
        parts.prepare(result['index'], result['index_sha256'], self.target, stage, offline=True)
        return stage, result['index_sha256']

    def test_prepared_payload_installs_without_consuming_release_files(self):
        stage, digest = self.v2_stage()
        source_bytes = (self.app / 'Launcher.exe').read_bytes()
        with mock.patch.object(release, 'confirm_probe'):
            result = release.apply_update(stage, self.target, probe=True, prepared_sha256=digest)
        self.assertEqual((self.target / 'Launcher.exe').read_bytes(), source_bytes)
        self.assertEqual((self.app / 'Launcher.exe').read_bytes(), source_bytes)
        self.assertFalse((stage / 'app/Launcher.exe').exists())
        self.assertTrue((self.target / 'state/components/current.json').is_file())
        self.assertEqual(release.read_json(Path(result['backup']) / 'journal.json')['phase'], 'committed')
        self.unchanged_private()

    def test_external_release_cannot_be_consumed_as_prepared_stage(self):
        with self.assertRaisesRegex(ValueError, 'disposable'):
            release.prepared_release(self.bundle, 'a' * 64, self.target)
        self.assertTrue((self.app / 'Launcher.exe').exists())

    def test_full_fallback_disables_stale_v2_catalog(self):
        stage, digest = self.v2_stage()
        release.apply_update(stage, self.target, prepared_sha256=digest)
        self.assertTrue((self.target / 'state/components/current.json').exists())
        result = release.apply_update(self.bundle, self.target)
        self.assertFalse((self.target / 'state/components/current.json').exists())
        self.assertTrue((Path(result['backup']) / 'old/state/components/current.json').exists())

    def test_full_v2_fallback_populates_offline_cache_and_uses_probe(self):
        stage, digest = self.v2_stage()
        shutil.rmtree(self.target / 'state/update-cache')
        package = release.package_app(self.bundle)
        catalog = self.bundle / ('Pocket-Windows-' + self.info['release_id'] + '-update-v2.json')
        with mock.patch.object(release, 'confirm_probe') as confirm:
            result = release.install_app(package['package'], package['sha256'], self.target,
                                         probe=True, catalog=catalog, index_sha256=digest)
        confirm.assert_called_once()
        self.assertTrue((self.target / 'state/components/current.json').is_file())
        self.assertEqual(result['release_id'], self.info['release_id'])
        self.unchanged_private()

    def test_bridge_freezes_components_from_verified_base(self):
        for name in (*release.pack.FILE_COMPONENT_FILES, *release.pack.GATEWAY_RECOVERY):
            self.write(self.app, name, b'frozen component')
        self.manifest['common_files'] = release.common_records(self.app)
        self.save_manifest()
        source = self.base / 'source'
        shutil.copytree(self.app, source)
        self.write(source, release.REQUIRED_RUNTIME[0], b'unaccepted runtime must not enter bridge')
        engine_hash = hashlib.sha256((self.app / release.REQUIRED_RUNTIME[1]).read_bytes()).hexdigest()
        for name in ('portable_app.py', 'control_guest_install.py', 'reference_vm.py', 'patch_qemu_engine.py'):
            self.write(source, 'scripts/' + name, ('ENGINE_SHA256 = ' + repr(engine_hash)).encode())
        output = self.base / 'bridge'
        rows = release.read_json(self.app / release.pack.COMPONENT_FILES[0])['files']
        def build(args, root, log):
            self.write(output, 'app/Launcher.exe', (self.app / 'Launcher.exe').read_bytes())
        with mock.patch.object(release, 'source_commit', return_value=self.info['source_commit']), \
                mock.patch.object(release, 'validate_build_inputs'), mock.patch.object(release, 'master_files'), \
                mock.patch.object(release.bundle, 'inventory', return_value={'files': rows}), \
                mock.patch.object(release, 'run_logged', side_effect=build), \
                mock.patch('update_toolkit.validate_host_build') as fresh_gate:
            manifest = release.prepare_release(source, output, self.base / 'master', '2026.10.27.1', bridge=self.bundle)
        fresh_gate.assert_not_called()
        self.assertEqual((output / 'app' / release.REQUIRED_RUNTIME[0]).read_bytes(), (self.app / release.REQUIRED_RUNTIME[0]).read_bytes())
        self.assertEqual(manifest['bridge_base']['source_commit'], self.info['source_commit'])


if __name__ == '__main__':
    unittest.main()
