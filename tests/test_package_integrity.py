import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pack_existing_runtime as pack
try:
    import package_integrity as integrity
except ModuleNotFoundError:
    integrity = None


class InitialIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(integrity, 'Initial package integrity check is missing')
        self.temporary = tempfile.TemporaryDirectory(prefix='package-integrity-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.engine = mock.patch.object(integrity.runtime, 'engine_processes', return_value={}).start()
        self.addCleanup(mock.patch.stopall)
        self.files = {name: b'fixture' for name in (*pack.DATA_FILES, *pack.APP_FILES,
                     *pack.PUBLIC_FILES, *pack.COMPONENT_FILES)}
        self.files['config/runtime.json'] = json.dumps({'schema': 1, 'work_memory_mib': 8192,
            'gateway_memory_mib': 768, 'vcpus': 4, 'lan_port': 42345}).encode()
        engines = ('runtime/python-3.12.14/python.exe', 'runtime/qemu-11.1.0/qemu-system-x86_64.exe',
                   'runtime/qemu-11.1.0/qemu-img.exe', 'runtime/qemu-11.1.0/share/edk2-x86_64-code.fd')
        self.files.update({name: name.encode() for name in engines})
        self.files[pack.COMPONENT_FILES[0]] = json.dumps({'schema': 1, 'files': [self.row(name)
            for name in engines]}).encode()
        self.files['gateway/packages/current.json'] = b'{"schema":1,"current":"v1","previous":null}'
        self.files['gateway/packages/releases/v1/gateway.qcow2'] = b'gateway fixture'
        self.files['gateway/packages/releases/v1/manifest.json'] = b'gateway manifest fixture'
        for name, value in self.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        self.manifest = {'schema': 2, 'kind': 'public_distribution', 'created_utc': 'fixture',
            'encrypted_profiles_included': False, 'gateway_recovery_included': False,
            'credentials_portability': 'No connection profiles included.',
            'vm_started_for_packaging': False, 'second_pc_tested': False,
            'files': [self.row(name) for name in sorted(self.files)]}
        self.metadata()

    def row(self, name):
        value = self.files[name]
        return {'path': name, 'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}

    def metadata(self, raw=None):
        raw = json.dumps(self.manifest).encode() if raw is None else raw
        (self.root / 'MANIFEST.json').write_bytes(raw)
        sums = ''.join(row['sha256'] + '  ' + row['path'] + '\n' for row in self.manifest['files'])
        (self.root / 'MANIFEST.sha256').write_bytes((sums + hashlib.sha256(raw).hexdigest()
            + '  MANIFEST.json\n').encode())

    def test_hashes_disk_before_any_start_and_caches_only_success(self):
        (self.root / pack.DATA_FILES[0]).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'images/workvm-empty.qcow2'):
            integrity.verify_initial(self.root)
        self.assertFalse((self.root / 'state/package-initial-check.json').exists())

    def test_marker_survives_windows_changes_and_active_vm(self):
        result = integrity.verify_initial(self.root)
        self.assertTrue(result['ok'])
        marker = json.loads((self.root / 'state/package-initial-check.json').read_text())
        self.assertEqual(marker['schema'], 1)
        self.assertGreater(marker['checked_files'], 10)
        (self.root / pack.DATA_FILES[0]).write_bytes(b'Windows changed after first boot')
        (self.root / pack.DATA_FILES[1]).write_bytes(b'UEFI changed after first boot')
        self.engine.return_value = {'work': object()}
        cached = integrity.verify_initial(self.root)
        self.assertTrue(cached['ok'])
        self.assertEqual(cached['code'], 'cached')

    def test_active_vm_without_marker_is_busy(self):
        self.engine.return_value = {'work': object()}
        result = integrity.verify_initial(self.root)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'busy')

    def test_busy_project_lock_without_marker_does_not_verify(self):
        with integrity.runtime.ProjectLock(self.root) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(integrity.verify_initial(self.root)['code'], 'busy')
        self.assertFalse((self.root / 'state/package-initial-check.json').exists())

    def test_changed_manifest_requires_a_new_check(self):
        self.assertTrue(integrity.verify_initial(self.root)['ok'])
        (self.root / pack.DATA_FILES[0]).write_bytes(b'corrupt')
        self.manifest['created_utc'] = 'new manifest'
        self.metadata()
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            integrity.verify_initial(self.root)

    def test_duplicate_json_fields_are_rejected(self):
        raw = json.dumps(self.manifest).replace('"schema": 2', '"schema": 2, "schema": 2').encode()
        self.metadata(raw)
        with self.assertRaisesRegex(ValueError, 'Duplicate metadata field'):
            integrity.verify_initial(self.root)

    def test_gateway_pointer_requires_its_release_members(self):
        self.manifest['files'] = [row for row in self.manifest['files']
            if row['path'] != 'gateway/packages/releases/v1/gateway.qcow2']
        self.metadata()
        with self.assertRaisesRegex(ValueError, 'gateway release is incomplete'):
            integrity.verify_initial(self.root)

    def test_absent_metadata_allows_legacy_but_partial_pair_is_rejected(self):
        for name in ('MANIFEST.json', 'MANIFEST.sha256'):
            with self.subTest(missing=name):
                self.metadata()
                (self.root / name).unlink()
                with self.assertRaises(ValueError):
                    integrity.verify_initial(self.root)
        (self.root / 'MANIFEST.json').unlink()
        self.assertEqual(integrity.verify_initial(self.root)['code'], 'legacy')

    def missing_new_metadata(self):
        for name in ('MANIFEST.json', 'MANIFEST.sha256'):
            (self.root / name).unlink()
        (self.root / 'release-info.json').write_text(json.dumps({'schema': 1,
            'release_id': '2026.10.23.2', 'source_commit': 'a' * 40}))

    def test_new_release_without_both_manifests_is_incomplete(self):
        self.missing_new_metadata()
        with self.assertRaisesRegex(ValueError, 'manifest'):
            integrity.verify_initial(self.root)

    def test_explicit_old_format_declaration_allows_updated_legacy_without_attestation(self):
        self.missing_new_metadata()
        marker = self.root / 'state/package-legacy-format.json'
        marker.write_text('{"schema":1,"legacy_format":true}')
        before = marker.read_bytes()
        result = integrity.verify_initial(self.root)
        self.assertTrue(result['ok'])
        self.assertEqual(result['code'], 'legacy')
        self.assertEqual(marker.read_bytes(), before)
        self.assertFalse((self.root / integrity.MARKER).exists())
        (self.root / 'MANIFEST.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'pair is incomplete'):
            integrity.verify_initial(self.root)

    def test_malformed_legacy_declaration_is_rejected(self):
        self.missing_new_metadata()
        marker = self.root / 'state/package-legacy-format.json'
        for raw in ('{}', '{"schema":true,"legacy_format":true}',
                    '{"schema":1,"legacy_format":false}',
                    '{"schema":1,"schema":1,"legacy_format":true}', ' ' * 4097):
            with self.subTest(raw=raw[:60]):
                marker.write_text(raw)
                with self.assertRaises(ValueError):
                    integrity.verify_initial(self.root)

    def test_duplicate_casefold_and_unsafe_paths_are_refused(self):
        original = list(self.manifest['files'])
        for name in ('Launcher.exe', 'launcher.exe', '../outside', 'runtime/CON', 'runtime/x:stream', '/absolute'):
            with self.subTest(name=name):
                self.manifest['files'] = original + [dict(original[0], path=name)]
                self.metadata()
                with self.assertRaises(ValueError):
                    integrity.verify_initial(self.root)

    def test_missing_required_member_is_refused(self):
        self.manifest['files'] = [row for row in self.manifest['files'] if row['path'] != pack.DATA_FILES[1]]
        self.metadata()
        with self.assertRaises(ValueError):
            integrity.verify_initial(self.root)

    def test_old_package_with_used_disk_is_explicitly_legacy_without_writes(self):
        new_modules = {'scripts/package_integrity.py', 'scripts/pack_existing_runtime.py'}
        self.manifest['files'] = [row for row in self.manifest['files'] if row['path'] not in new_modules]
        for name in new_modules:
            (self.root / name).unlink()
        self.metadata()
        (self.root / pack.DATA_FILES[0]).write_bytes(b'Windows changed in an old release')
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        self.engine.side_effect = AssertionError('Legacy check must not inspect VM state')
        result = integrity.verify_initial(self.root)
        self.assertTrue(result['ok'])
        self.assertEqual(result['code'], 'legacy')
        self.assertIn('распакуй ZIP нового выпуска', result['message'])
        self.assertEqual(before, {str(path.relative_to(self.root)): path.read_bytes()
                                 for path in self.root.rglob('*') if path.is_file()})
        self.assertFalse((self.root / integrity.MARKER).exists())

    def test_new_package_missing_pack_module_is_rejected(self):
        name = 'scripts/pack_existing_runtime.py'
        self.manifest['files'] = [row for row in self.manifest['files'] if row['path'] != name]
        (self.root / name).unlink()
        self.metadata()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            integrity.verify_initial(self.root)

    def test_new_app_modules_do_not_invalidate_the_original_package_receipt(self):
        self.assertTrue(integrity.verify_initial(self.root)['ok'])
        with mock.patch.object(pack, 'APP_FILES', (*pack.APP_FILES, 'scripts/future_module.py')):
            self.assertEqual(integrity.verify_initial(self.root)['code'], 'cached')

    def test_hard_link_is_refused(self):
        os.link(self.root / pack.DATA_FILES[0], self.root / 'alias.qcow2')
        with self.assertRaises(ValueError):
            integrity.verify_initial(self.root)

    def test_editable_config_is_validated_but_not_compared_to_original_hash(self):
        path = self.root / 'config/runtime.json'
        value = json.loads(path.read_bytes())
        value['work_memory_mib'] = 4096
        path.write_text(json.dumps(value))
        self.assertTrue(integrity.verify_initial(self.root)['ok'])

    def test_invalid_config_blocks_initial_check(self):
        (self.root / 'config/runtime.json').write_bytes(b'{"schema":1,"vcpus":"invalid"}')
        with self.assertRaises(ValueError):
            integrity.verify_initial(self.root)

    def test_checksum_metadata_corruption_and_oversize_are_refused(self):
        (self.root / 'MANIFEST.sha256').write_text('wrong')
        with self.assertRaises(ValueError):
            integrity.verify_initial(self.root)
        self.metadata(raw=b' ' * (pack.MAX_METADATA + 1))
        with self.assertRaises(ValueError):
            integrity.verify_initial(self.root)


if __name__ == '__main__':
    unittest.main()
