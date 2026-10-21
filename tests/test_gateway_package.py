import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import gateway_package as package


class GatewayPackageTests(unittest.TestCase):
    def test_inspection_reads_only_header_and_does_not_claim_checksum_verification(self):
        package.install_package(self.root, *self.archive('v1'))
        original = package.locked_read
        reads = []
        @contextlib.contextmanager
        def header_only(path):
            with original(path) as stream:
                if Path(path).name != 'gateway.qcow2':
                    yield stream
                    return
                class Reader:
                    def read(self, size=-1):
                        if size != 104: raise AssertionError('Diagnostics read past the image header')
                        reads.append(size)
                        return stream.read(size)
                    def fileno(self): return stream.fileno()
                yield Reader()
        with mock.patch.object(package.hashlib, 'file_digest', side_effect=AssertionError('Full hash in diagnostics')), \
                mock.patch.object(package, 'locked_read', side_effect=header_only):
            inspected = package.inspect_current(self.root)
        self.assertEqual(reads, [104])
        self.assertEqual(inspected['version'], 'v1')
        self.assertFalse(inspected['hash_verified'])
        self.assertEqual(inspected['verification'], 'on_start')

    def test_light_inspection_accepts_same_sized_payload_change_but_full_open_rejects(self):
        package.install_package(self.root, *self.archive('v1'))
        disk = self.root / 'gateway/packages/releases/v1/gateway.qcow2'
        content = bytearray(disk.read_bytes()); content[-1] ^= 1; disk.write_bytes(content)
        self.assertFalse(package.inspect_current(self.root)['hash_verified'])
        with self.assertRaisesRegex(ValueError, 'checksum'):
            with package.open_current(self.root):
                pass

    def test_inspection_rejects_wrong_size_or_invalid_header(self):
        package.install_package(self.root, *self.archive('v1'))
        disk = self.root / 'gateway/packages/releases/v1/gateway.qcow2'
        original = disk.read_bytes()
        for content in (original + b'x', b'BAD!' + original[4:]):
            disk.write_bytes(content)
            with self.assertRaises(ValueError): package.inspect_current(self.root)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'state').mkdir()
        self.lock = mock.patch.object(package.runtime, 'ProjectLock')
        self.lock.start().return_value.__enter__.return_value = True
        self.addCleanup(self.lock.stop)
        processes = mock.patch.object(package.runtime, 'engine_processes', return_value=[])
        processes.start()
        self.addCleanup(processes.stop)

    def archive(self, version, changes=None, extra=None, disk=None):
        if disk is None:
            disk = bytearray(4096)
            struct.pack_into('>4sI', disk, 0, b'QFI\xfb', 3)
            struct.pack_into('>I', disk, 20, 16)
            struct.pack_into('>Q', disk, 24, 4 * 1024**3)
            struct.pack_into('>I', disk, 100, 104)
            disk = bytes(disk)
        manifest = dict(schema=1, session_schema=1, version=version, bytes=len(disk),
                        sha256=hashlib.sha256(disk).hexdigest(), virtual_bytes=4 * 1024**3)
        manifest.update(changes or {})
        path = self.root / (version + '.zip')
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('manifest.json', json.dumps(manifest))
            archive.writestr('gateway.qcow2', disk)
            if extra:
                archive.writestr(extra, b'wrong')
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_install_open_and_retain_one_previous_without_touching_user_data(self):
        sentinel = self.root / 'windows-user-data'
        sentinel.write_bytes(b'private')
        for version in ('v1', 'v2', 'v3'):
            package.install_package(self.root, *self.archive(version))
        with package.open_current(self.root) as current:
            self.assertEqual(current['version'], 'v3')
            with self.assertRaises(OSError):
                current['disk'].write_bytes(b'corruption')
        pointer = json.loads((self.root / 'gateway/packages/current.json').read_text())
        self.assertEqual(pointer['previous'], 'v2')
        self.assertFalse((self.root / 'gateway/packages/releases/v1').exists())
        self.assertEqual(sentinel.read_bytes(), b'private')

    def test_untrusted_hash_extra_member_and_bad_disk_never_activate(self):
        for kwargs in ({'extra': '../escape'}, {'changes': {'bytes': 1}}, {'disk': b'bad'}):
            source, digest = self.archive('v1', **kwargs)
            with self.assertRaises(ValueError):
                package.install_package(self.root, source, digest)
            self.assertFalse((self.root / 'gateway/packages/current.json').exists())
        source, _ = self.archive('v1')
        with self.assertRaises(ValueError):
            package.install_package(self.root, source, '0' * 64)

    def test_same_version_mismatch_and_busy_guard(self):
        source, digest = self.archive('v1')
        package.install_package(self.root, source, digest)
        source, digest = self.archive('v1', {'virtual_bytes': 8 * 1024**3})
        with self.assertRaises(ValueError):
            package.install_package(self.root, source, digest)
        with mock.patch.object(package.runtime, 'engine_processes', return_value=[{}]):
            with self.assertRaises(RuntimeError):
                package.install_package(self.root, source, digest)

    def test_hardlink_manifest_does_not_modify_sentinel(self):
        source, digest = self.archive('v1')
        package.install_package(self.root, source, digest)
        pointer = self.root / 'gateway/packages/current.json'
        pointer.unlink()
        sentinel = self.root / 'sentinel'
        sentinel.write_bytes(b'untouched')
        os.link(sentinel, pointer)
        source, digest = self.archive('v2')
        with self.assertRaises(ValueError):
            package.install_package(self.root, source, digest)
        self.assertEqual(sentinel.read_bytes(), b'untouched')

    def test_interruption_after_pointer_commit_preserves_active_disk_and_recovers(self):
        package.install_package(self.root, *self.archive('v1'))
        original = package._atomic
        def fail_commit(path, value):
            if path.name == 'update.json' and value.get('phase') == 'committed':
                raise OSError('simulated power loss')
            return original(path, value)
        with mock.patch.object(package, '_atomic', side_effect=fail_commit):
            with self.assertRaises(OSError):
                package.install_package(self.root, *self.archive('v2'))
        with package.open_current(self.root) as active:
            self.assertEqual(active['version'], 'v2')
        package.install_package(self.root, *self.archive('v3'))
        with package.open_current(self.root) as active:
            self.assertEqual(active['version'], 'v3')
        self.assertTrue((self.root / 'gateway/packages/releases/v2/gateway.qcow2').exists())

    def test_unknown_journal_refuses_update_without_deleting_existing_versions(self):
        package.install_package(self.root, *self.archive('v1'))
        journal = self.root / 'gateway/packages/update.json'
        journal.write_text('{"schema":1,"phase":"unknown"}')
        with self.assertRaises(ValueError):
            package.install_package(self.root, *self.archive('v2'))
        with package.open_current(self.root) as active:
            self.assertEqual(active['version'], 'v1')
        self.assertTrue((self.root / 'gateway/packages/releases/v1/gateway.qcow2').exists())

    def test_junction_package_directory_is_rejected(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        sentinel = elsewhere / 'sentinel'
        sentinel.write_bytes(b'unchanged')
        link = self.root / 'gateway'
        result = package.runtime.subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(elsewhere)],
                                                capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.addCleanup(lambda: os.rmdir(link))
        with self.assertRaises(ValueError):
            package.install_package(self.root, *self.archive('v1'))
        self.assertEqual(sentinel.read_bytes(), b'unchanged')

    def test_symlink_zip_member_and_unsafe_versions_are_rejected(self):
        for version in ('../v1', 'con', 'v1.', 'V1'):
            with self.assertRaises(ValueError):
                package._version(version)
        source, _ = self.archive('v1')
        with zipfile.ZipFile(source) as archive:
            metadata = archive.read('manifest.json')
            disk = archive.read('gateway.qcow2')
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('manifest.json', metadata)
            entry = zipfile.ZipInfo('gateway.qcow2')
            entry.create_system = 3
            entry.external_attr = (0o120777 << 16)
            archive.writestr(entry, disk)
        with self.assertRaises(ValueError):
            package.install_package(self.root, source, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_explicit_repair_restores_missing_or_corrupt_current_and_preserves_user_data(self):
        for state in ('missing', 'corrupt'):
            with self.subTest(state=state):
                root = self.root / state
                root.mkdir()
                source, digest = self.archive('v1')
                package.install_package(root, source, digest)
                windows = root / 'images/workvm.qcow2'
                windows.parent.mkdir()
                windows.write_bytes(b'windows-data')
                profiles = root / 'private/profiles.json'
                profiles.parent.mkdir()
                profiles.write_bytes(b'profile-secrets')
                disk = root / 'gateway/packages/releases/v1/gateway.qcow2'
                disk.unlink()
                if state == 'corrupt':
                    disk.write_bytes(b'corrupt')
                with self.assertRaises((ValueError, OSError)):
                    package.install_package(root, source, digest)
                result = package.repair_package(root, source, digest)
                self.assertIsNone(result['previous'])
                with package.open_current(root) as active:
                    self.assertEqual(active['version'], 'v1')
                self.assertEqual(windows.read_bytes(), b'windows-data')
                self.assertEqual(profiles.read_bytes(), b'profile-secrets')
                quarantine = root / 'gateway/packages/quarantine'
                self.assertEqual(len(list(quarantine.iterdir())), 1)

    def test_repair_quarantines_corrupt_previous_and_retains_only_valid_rollback(self):
        package.install_package(self.root, *self.archive('v1'))
        package.install_package(self.root, *self.archive('v2'))
        damaged = self.root / 'gateway/packages/releases/v1/gateway.qcow2'
        damaged.write_bytes(b'bad-previous')
        source, digest = self.archive('v3')
        with self.assertRaises(ValueError):
            package.install_package(self.root, source, digest)
        result = package.repair_package(self.root, source, digest)
        self.assertEqual(result['previous'], 'v2')
        self.assertFalse(damaged.exists())
        self.assertEqual(next((self.root / 'gateway/packages/quarantine').glob('*/gateway.qcow2')).read_bytes(), b'bad-previous')

    def test_interrupted_repair_requires_same_verified_package_and_resumes(self):
        source, digest = self.archive('v1')
        package.install_package(self.root, source, digest)
        damaged = self.root / 'gateway/packages/releases/v1/gateway.qcow2'
        damaged.write_bytes(b'bad-current')
        original = Path.rename
        def fail_promotion(path, destination):
            if path.name.startswith('.staging-'):
                raise OSError('interrupted after quarantine')
            return original(path, destination)
        with mock.patch.object(Path, 'rename', autospec=True, side_effect=fail_promotion):
            with self.assertRaises(OSError):
                package.repair_package(self.root, source, digest)
        with self.assertRaises(RuntimeError):
            package.install_package(self.root, source, digest)
        other, other_digest = self.archive('v2')
        with self.assertRaises(ValueError):
            package.repair_package(self.root, other, other_digest)
        package.repair_package(self.root, source, digest)
        with package.open_current(self.root) as active:
            self.assertEqual(active['version'], 'v1')
        self.assertEqual(next((self.root / 'gateway/packages/quarantine').glob('*/gateway.qcow2')).read_bytes(), b'bad-current')

    def test_invalid_incoming_repair_does_not_quarantine_or_change_current(self):
        package.install_package(self.root, *self.archive('v1'))
        disk = self.root / 'gateway/packages/releases/v1/gateway.qcow2'
        disk.write_bytes(b'bad-current')
        source, digest = self.archive('v1', disk=b'bad-incoming')
        with self.assertRaises(ValueError):
            package.repair_package(self.root, source, digest)
        self.assertEqual(disk.read_bytes(), b'bad-current')
        self.assertFalse((self.root / 'gateway/packages/repair.json').exists())

    def test_corrupt_previous_repair_resumes_after_promotion_before_pointer_write(self):
        package.install_package(self.root, *self.archive('v1'))
        package.install_package(self.root, *self.archive('v2'))
        (self.root / 'gateway/packages/releases/v1/gateway.qcow2').write_bytes(b'bad-previous')
        source, digest = self.archive('v3')
        original = package._atomic
        def fail_pointer(path, value):
            if path.name == 'current.json':
                raise OSError('interrupted before pointer')
            return original(path, value)
        with mock.patch.object(package, '_atomic', side_effect=fail_pointer):
            with self.assertRaises(OSError):
                package.repair_package(self.root, source, digest)
        with package.open_current(self.root) as active:
            self.assertEqual(active['version'], 'v2')
        result = package.repair_package(self.root, source, digest)
        self.assertEqual(result['previous'], 'v2')
        with package.open_current(self.root) as active:
            self.assertEqual(active['version'], 'v3')

    def test_bad_current_can_only_keep_a_verified_previous_as_rollback(self):
        package.install_package(self.root, *self.archive('v1'))
        package.install_package(self.root, *self.archive('v2'))
        (self.root / 'gateway/packages/releases/v2/gateway.qcow2').write_bytes(b'bad-current')
        source, digest = self.archive('v3')
        result = package.repair_package(self.root, source, digest)
        self.assertEqual(result['previous'], 'v1')
        self.assertFalse((self.root / 'gateway/packages/releases/v2').exists())
        with mock.patch.object(package.runtime, 'engine_processes', return_value=[{}]):
            with self.assertRaises(RuntimeError):
                package.repair_package(self.root, source, digest)


if __name__ == '__main__':
    unittest.main()
