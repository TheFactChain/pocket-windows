import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/pack_existing_runtime.py'
spec = importlib.util.spec_from_file_location('pack_runtime', SCRIPT)
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)


def qcow():
    content = bytearray(4096)
    content[:4] = b'QFI\xfb'
    struct.pack_into('>I', content, 4, 3)
    struct.pack_into('>Q', content, 24, 1024 * 1024)
    return bytes(content)


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='portable archive ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'Проект с пробелами'
        self.project.mkdir()
        inventory = mock.patch.object(pack.runtime, 'engine_processes', return_value={})
        inventory.start()
        self.addCleanup(inventory.stop)
        self.fixture()

    def write(self, name, content):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def fixture(self):
        for name in (*pack.DATA_FILES, *pack.APP_FILES):
            self.write(name, qcow() if name.endswith('.qcow2') else b'fixture')
        executable = bytearray(134)
        executable[:2] = b'MZ'
        executable[60:64] = (128).to_bytes(4, 'little')
        executable[128:] = b'PE\0\0\x64\x86'
        self.write('Launcher.exe', executable)
        self.write('config/runtime.json', json.dumps({
            'schema': 1, 'work_memory_mib': 8192, 'gateway_memory_mib': 768,
            'vcpus': 4, 'lan_port': 42345}).encode())
        self.write('scripts/Prepare-Dependencies.ps1', SCRIPT.with_name('Prepare-Dependencies.ps1').read_bytes())
        records = []
        for name in ('runtime/python-3.12.14/python.exe', 'runtime/qemu-11.1.0/qemu-system-x86_64.exe',
                     'runtime/qemu-11.1.0/qemu-img.exe', 'runtime/qemu-11.1.0/share/edk2-x86_64-code.fd'):
            content = name.encode()
            self.write(name, content)
            records.append({'path': name, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
        self.write(pack.COMPONENT_FILES[0], json.dumps({'schema': 1, 'files': records}).encode())
        with zipfile.ZipFile(self.project / pack.COMPONENT_FILES[1], 'w') as archive:
            for row in records:
                archive.writestr(row['path'], (self.project / row['path']).read_bytes())
        self.write('gateway/packages/current.json',
                   b'{"schema":1,"current":"v2","previous":"v1"}')
        for version in ('v1', 'v2'):
            disk = qcow()
            prefix = 'gateway/packages/releases/' + version + '/'
            self.write(prefix + 'gateway.qcow2', disk)
            self.write(prefix + 'manifest.json', json.dumps({
                'schema': 1, 'session_schema': 1, 'version': version, 'bytes': len(disk),
                'sha256': hashlib.sha256(disk).hexdigest(), 'virtual_bytes': 1024 * 1024}).encode())
        self.profile_id, self.revision = 'a' * 32, 'b' * 32
        self.profiles = {
            'schema': 1, 'selected_profile_id': self.profile_id,
            'profiles': {self.profile_id: {'current_revision_id': self.revision, 'revisions': {
                self.revision: {'config': {'schema': 1, 'name': 'Мой сервер', 'protocol': 'socks5',
                    'host': 'proxy.example', 'port': 1080, 'tls_verify': False, 'udp_supported': False},
                    'credentials_blob': base64.b64encode(b'opaque Windows DPAPI fixture').decode()}}}}}
        self.write(pack.PROFILE_FILE, json.dumps(self.profiles).encode())
        for name in ('private/gohost-user1/sing-box.json', 'logs/secret.log',
                     '.beads/db', 'build/old.iso', 'tests/not-shipped.py', 'ui/not-shipped.cs',
                     'runtime/accidental-secret.txt', 'profiles/export.json', 'profiles/profiles-old.tmp'):
            self.write(name, b'LEGACY_PLAINTEXT_SENTINEL')

    def archive(self, name='personal.zip', mode='personal'):
        entries = pack.collect(self.project, mode=mode)
        destination = self.root / name
        manifest = pack.write_archive(entries, destination)
        self.assertEqual(pack.verify_archive(destination), manifest)
        return destination, manifest

    def test_file_access_components_ship_but_public_archive_excludes_host_paths(self):
        files = getattr(pack, 'FILE_COMPONENT_FILES', ())
        self.assertIn('runtime/host-files/HostFiles.exe', files, 'File helpers must be explicitly packaged')
        for name in files:
            self.write(name, b'compiled component fixture')
        self.write('config/files.json', json.dumps({'schema': 1, 'mode': 'selected', 'folders': ['C:\\private-host-folder']}).encode())
        self.write('config/viewer.json', b'{"schema":1,"start_fullscreen":true,"remember_window":false}')
        destination, _ = self.archive('files-public.zip', mode='public')
        with zipfile.ZipFile(destination) as archive:
            for name in files:
                self.assertIn(pack.PREFIX + name, archive.namelist())
            self.assertNotIn(pack.PREFIX + 'config/files.json', archive.namelist())
            self.assertIn(pack.PREFIX + 'config/viewer.json', archive.namelist())

    def test_public_archive_omits_gateway_duplicate_and_stores_disks(self):
        payload = b'verified recovery fixture'
        self.write(pack.GATEWAY_RECOVERY[1], payload)
        self.write(pack.GATEWAY_RECOVERY[0], json.dumps({
            'schema': 1, 'file': 'gateway-recovery.zip', 'sha256': hashlib.sha256(payload).hexdigest()}).encode())
        destination, manifest = self.archive('public-compact.zip', mode='public')
        self.assertFalse(manifest['gateway_recovery_included'])
        with zipfile.ZipFile(destination) as archive:
            for name in pack.GATEWAY_RECOVERY:
                self.assertNotIn(pack.PREFIX + name, archive.namelist())
            for member in archive.infolist():
                if member.filename.endswith('.qcow2'):
                    self.assertEqual(member.compress_type, zipfile.ZIP_STORED)

    def test_zstd_windows_disk_is_accepted_but_dirty_or_unknown_features_are_not(self):
        disk = bytearray(qcow())
        struct.pack_into('>Q', disk, 72, 8)
        struct.pack_into('>I', disk, 100, 112)
        disk[104] = 1
        self.write(pack.DATA_FILES[0], disk)
        self.archive('zstd.zip', mode='public')
        for features, algorithm in ((9, 1), (10, 1), (16, 1), (8, 0), (8, 2), (0, 1)):
            struct.pack_into('>Q', disk, 72, features)
            disk[104] = algorithm
            self.write(pack.DATA_FILES[0], disk)
            with self.subTest(features=features, algorithm=algorithm), self.assertRaises(ValueError):
                pack.collect(self.project, mode='public')

    def test_public_zip_keeps_its_instructions_and_component_notice_inside(self):
        self.write('НАЧАТЬ.txt', 'Открой Launcher.exe'.encode('utf-8'))
        self.write('third-party.md', b'component notice fixture')
        destination, _ = self.archive('with-instructions.zip', mode='public')
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(archive.read(pack.PREFIX + 'НАЧАТЬ.txt'), 'Открой Launcher.exe'.encode('utf-8'))
            self.assertEqual(archive.read(pack.PREFIX + 'third-party.md'), b'component notice fixture')

    def test_public_archive_preserves_release_provenance(self):
        info = {'schema': 1, 'release_id': '2026.10.23.1', 'source_commit': 'a' * 40}
        self.write('release-info.json', json.dumps(info).encode())
        destination, manifest = self.archive('versioned-public.zip', mode='public')
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(json.loads(archive.read(pack.PREFIX + 'release-info.json')), info)
        self.assertIn('release-info.json', {row['path'] for row in manifest['files']})

    def test_malformed_release_provenance_is_not_packaged(self):
        for value in (b'{"schema":true,"release_id":"2026.10.23.1","source_commit":"' + b'a'*40 + b'"}',
                      b'{"schema":1,"schema":1,"release_id":"2026.10.23.1","source_commit":"' + b'a'*40 + b'"}'):
            self.write('release-info.json', value)
            with self.assertRaises(ValueError):
                pack.collect(self.project, mode='public')

    def test_public_archive_excludes_profiles_without_reading_them(self):
        self.write(pack.PROFILE_FILE, b'{broken private data')
        with mock.patch.object(pack.ProfileStore, '_read', side_effect=AssertionError('private read')):
            destination, manifest = self.archive('public.zip', mode='public')
        self.assertEqual(manifest['kind'], 'public_distribution')
        self.assertFalse(manifest['encrypted_profiles_included'])
        self.assertFalse(manifest['second_pc_tested'])
        with zipfile.ZipFile(destination) as archive:
            self.assertFalse(any('/profiles/' in name or '/private/' in name for name in archive.namelist()))
            self.assertEqual(archive.read(pack.PREFIX + pack.DATA_FILES[0]), qcow())

    def test_public_mode_refuses_a_profile_injected_after_inventory(self):
        public = pack.collect(self.project, mode='public')
        personal = pack.collect(self.project)
        public[pack.PROFILE_FILE] = personal[pack.PROFILE_FILE]
        public.expected[pack.PROFILE_FILE] = personal.expected[pack.PROFILE_FILE]
        output = self.root / 'refused-public.zip'
        with self.assertRaisesRegex(ValueError, 'must not include profiles'):
            pack.write_archive(public, output)
        self.assertFalse(output.exists())

    def test_public_cli_sets_kind_and_rejects_unknown_mode(self):
        destination = self.root / 'public-cli.zip'
        arguments = ['packer', '--mode', 'public', '--project', str(self.project), '--output', str(destination)]
        with mock.patch.object(sys, 'argv', arguments), contextlib.redirect_stdout(io.StringIO()) as output:
            pack.main()
        self.assertEqual(json.loads(output.getvalue())['kind'], 'public_distribution')
        with self.assertRaises(ValueError):
            pack.collect(self.project, mode='unknown')

    def test_personal_archive_preserves_selected_encrypted_history_and_excludes_legacy_data(self):
        with mock.patch('profile_store._unprotect', side_effect=AssertionError('Packaging must not decrypt')):
            destination, manifest = self.archive()
        self.assertEqual(manifest['kind'], 'personal_copy')
        self.assertTrue(manifest['encrypted_profiles_included'])
        self.assertIn('re-entry', manifest['credentials_portability'])
        with zipfile.ZipFile(destination) as archive:
            copied = json.loads(archive.read(pack.PREFIX + pack.PROFILE_FILE))
            self.assertEqual(copied, self.profiles)
            self.assertEqual(copied['selected_profile_id'], self.profile_id)
            self.assertFalse(any(name.startswith(pack.PREFIX + prefix)
                for name in archive.namelist() for prefix in ('private/', 'logs/', '.beads/', 'build/', 'tests/', 'ui/')))
            self.assertFalse(any(b'LEGACY_PLAINTEXT_SENTINEL' in archive.read(name)
                                 for name in archive.namelist()))
            self.assertEqual(json.loads(archive.read(pack.PREFIX + 'config/runtime.json'))['lan_port'], 42345)
            self.assertIn(pack.PREFIX + 'gateway/packages/releases/v1/gateway.qcow2', archive.namelist())
            archive.extractall(self.root / 'unpacked')
        unpacked = self.root / 'unpacked/Windows-Claude'
        repeated = pack.collect(unpacked)
        self.assertEqual(set(repeated), {row['path'] for row in manifest['files']})
        self.assertFalse((unpacked / '.git').exists())

    def test_cli_needs_no_git_source_or_commit_and_never_overwrites_output(self):
        destination = self.root / 'cli.zip'
        arguments = ['packer', '--project', str(self.project), '--output', str(destination)]
        with mock.patch.object(sys, 'argv', arguments), contextlib.redirect_stdout(io.StringIO()) as output:
            pack.main()
        self.assertTrue(json.loads(output.getvalue())['crc_and_sha256_verified'])
        original = destination.read_bytes()
        with mock.patch.object(sys, 'argv', arguments), self.assertRaises(FileExistsError):
            pack.main()
        self.assertEqual(destination.read_bytes(), original)
        with self.assertRaisesRegex(ValueError, 'separate source'):
            pack.collect(self.project, self.root / 'excluded-unread-source')

    def test_component_repair_still_works_after_standalone_unpack(self):
        destination, _ = self.archive()
        with zipfile.ZipFile(destination) as archive:
            archive.extractall(self.root / 'repair')
        extracted = self.root / 'repair/Windows-Claude'
        name = 'runtime/python-3.12.14/python.exe'
        original = (extracted / name).read_bytes()
        (extracted / name).unlink()
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                                 str(extracted / 'scripts/Prepare-Dependencies.ps1'),
                                 '-Project', str(extracted)], capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(json.loads(result.stdout.decode('utf-8-sig'))['code'], 'repaired')
        self.assertEqual((extracted / name).read_bytes(), original)

    def test_cli_verification_failure_removes_its_exact_partial_only(self):
        destination = self.root / 'rejected.zip'
        sentinel = self.root / 'keep.zip'
        sentinel.write_bytes(b'existing unrelated archive')
        arguments = ['packer', '--project', str(self.project), '--output', str(destination)]
        with mock.patch.object(sys, 'argv', arguments), \
             mock.patch.object(pack, 'verify_archive', side_effect=ValueError('corrupt archive')), \
             self.assertRaisesRegex(ValueError, 'corrupt'):
            pack.main()
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_suffix('.zip.partial').exists())
        self.assertEqual(sentinel.read_bytes(), b'existing unrelated archive')

    def test_invalid_launcher_settings_or_profile_are_refused(self):
        cases = [
            ('Launcher.exe', b'not PE'),
            ('config/runtime.json', b'{"lan_port":80}'),
            ('config/runtime.json', b'{"password":"must not be included"}'),
            (pack.PROFILE_FILE, b'{"schema":1,"selected_profile_id":"missing","profiles":{}}')]
        for name, content in cases:
            path = self.project / name
            old = path.read_bytes()
            with self.subTest(name=name):
                path.write_bytes(content)
                with self.assertRaises((ValueError, RuntimeError)):
                    pack.collect(self.project)
                path.write_bytes(old)

    def test_x86_and_disks_with_backing_or_dirty_bits_are_refused(self):
        launcher = self.project / 'Launcher.exe'
        original = launcher.read_bytes()
        changed = bytearray(original)
        changed[132:134] = b'\x4c\x01'
        launcher.write_bytes(changed)
        with self.assertRaises(ValueError):
            pack.collect(self.project)
        launcher.write_bytes(original)
        disk = self.project / pack.DATA_FILES[0]
        for offset, value in ((8, 104), (72, 1), (32, 1)):
            changed = bytearray(qcow())
            struct.pack_into('>Q' if offset != 32 else '>I', changed, offset, value)
            disk.write_bytes(changed)
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                pack.collect(self.project)

    def test_changed_settings_cleanup_only_the_new_partial_archive(self):
        entries = pack.collect(self.project)
        self.write('config/runtime.json', b'{"private_key":"changed"}')
        destination = self.root / 'failed.partial'
        sentinel = self.root / 'untouched'
        sentinel.write_bytes(b'must remain')
        with self.assertRaises(ValueError):
            pack.write_archive(entries, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(sentinel.read_bytes(), b'must remain')

    def test_same_size_corrupt_component_rejected_even_with_original_timestamp(self):
        entries = pack.collect(self.project)
        name = 'runtime/python-3.12.14/python.exe'
        path = self.project / name
        before = path.stat()
        path.write_bytes(b'x' * before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        target = self.root / 'corrupt.partial'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            pack.write_archive(entries, target)
        self.assertFalse(target.exists())

    def test_running_vm_and_contended_lifecycle_lock_refuse_archive(self):
        entries = pack.collect(self.project)
        with mock.patch.object(pack.runtime, 'engine_processes', return_value={123: {}}):
            with self.assertRaisesRegex(RuntimeError, 'Shut down'):
                pack.write_archive(entries, self.root / 'running.zip')
        with pack.runtime.ProjectLock(self.project) as acquired:
            self.assertTrue(acquired)
            with self.assertRaisesRegex(RuntimeError, 'Shut down'):
                pack.write_archive(entries, self.root / 'locked.zip')

    def test_existing_archive_and_source_read_locks_are_preserved(self):
        destination, _ = self.archive()
        original = destination.read_bytes()
        with self.assertRaises(FileExistsError):
            pack.write_archive(pack.collect(self.project), destination)
        self.assertEqual(destination.read_bytes(), original)
        source = self.project / pack.DATA_FILES[0]
        with pack.locked_read(source), self.assertRaises(PermissionError):
            source.open('wb')
        self.assertEqual(source.read_bytes(), qcow())

    def test_hardlink_and_reparse_source_paths_refused(self):
        path = self.project / 'Launcher.exe'
        outside = self.root / 'outside.exe'
        outside.write_bytes(path.read_bytes())
        path.unlink()
        os.link(outside, path)
        with self.assertRaises((ValueError, RuntimeError)):
            pack.collect(self.project)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises((ValueError, RuntimeError)):
            pack.collect(self.project)
        path.unlink()
        self.assertEqual(outside.read_bytes()[:2], b'MZ')

    def test_archive_extra_symlink_and_corrupt_members_are_refused(self):
        destination, _ = self.archive()
        with zipfile.ZipFile(destination, 'a') as archive:
            info = zipfile.ZipInfo(pack.PREFIX + 'runtime/python-3.12.14/linked')
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, '../../outside')
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            pack.verify_archive(destination)
        clean, _ = self.archive('clean.zip')
        corrupt = self.root / 'corrupt.zip'
        with zipfile.ZipFile(clean) as source, zipfile.ZipFile(corrupt, 'w') as target:
            for item in source.infolist():
                payload = source.read(item)
                if item.filename.endswith('Launcher.exe'):
                    payload = b'x' * len(payload)
                target.writestr(item, payload)
        with self.assertRaisesRegex(ValueError, 'hash'):
            pack.verify_archive(corrupt)
        with zipfile.ZipFile(clean, 'a') as archive:
            archive.writestr(pack.PREFIX + 'unexpected.txt', b'extra')
        with self.assertRaisesRegex(ValueError, 'member set'):
            pack.verify_archive(clean)

    def test_gateway_recovery_pair_must_be_complete_and_hash_pinned(self):
        self.write(pack.GATEWAY_RECOVERY[0], b'{"schema":1,"file":"gateway-recovery.zip","sha256":"' + b'0' * 64 + b'"}')
        with self.assertRaisesRegex(ValueError, 'both'):
            pack.collect(self.project)
        self.write(pack.GATEWAY_RECOVERY[1], b'corrupt recovery')
        entries = pack.collect(self.project)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            pack.write_archive(entries, self.root / 'bad-recovery.zip')
        payload = b'verified recovery fixture'
        self.write(pack.GATEWAY_RECOVERY[1], payload)
        self.write(pack.GATEWAY_RECOVERY[0], json.dumps({
            'schema': 1, 'file': 'gateway-recovery.zip', 'sha256': hashlib.sha256(payload).hexdigest()}).encode())
        _, manifest = self.archive('recovery.zip')
        self.assertTrue(manifest['gateway_recovery_included'])

    def test_no_profile_is_a_valid_copy_but_broken_profile_is_not_silently_omitted(self):
        path = self.project / pack.PROFILE_FILE
        path.unlink()
        _, manifest = self.archive()
        self.assertFalse(manifest['encrypted_profiles_included'])
        path.write_bytes(b'{broken')
        with self.assertRaises((ValueError, RuntimeError)):
            pack.collect(self.project)

    def test_dangling_profile_file_or_directory_never_silently_loses_profiles(self):
        path = self.project / pack.PROFILE_FILE
        original = path.read_bytes()
        path.unlink()
        path.symlink_to(self.root / 'missing-profile.json')
        try:
            with self.assertRaises((ValueError, RuntimeError)):
                pack.collect(self.project)
        finally:
            path.unlink()
            path.write_bytes(original)
        directory = path.parent
        saved = self.project / 'saved-profiles'
        self.assertEqual(directory.resolve().parent, self.project.resolve())
        directory.rename(saved)
        directory.symlink_to(self.root / 'missing-profile-directory', target_is_directory=True)
        try:
            with self.assertRaises((ValueError, RuntimeError)):
                pack.collect(self.project)
        finally:
            directory.unlink()
            saved.rename(directory)

    def test_recovery_archive_requires_exact_safe_members_sizes_and_hashes(self):
        catalog = json.loads((self.project / pack.COMPONENT_FILES[0]).read_bytes())['files']
        original = (self.project / pack.COMPONENT_FILES[1]).read_bytes()
        package = self.project / pack.COMPONENT_FILES[1]
        for failure in ('not-zip', 'missing', 'extra-secret', 'duplicate', 'unsafe', 'symlink',
                        'directory-type', 'encrypted', 'comment', 'size', 'hash'):
            with self.subTest(failure=failure):
                if failure == 'not-zip':
                    package.write_bytes(b'not a ZIP archive')
                else:
                    with zipfile.ZipFile(package, 'w') as archive:
                        for index, row in enumerate(catalog):
                            if failure == 'missing' and index == 0:
                                continue
                            info = zipfile.ZipInfo(row['path'])
                            data = (self.project / row['path']).read_bytes()
                            if index == 0:
                                if failure == 'symlink': info.external_attr = (stat.S_IFLNK | 0o777) << 16
                                if failure == 'directory-type': info.external_attr = (stat.S_IFDIR | 0o755) << 16
                                if failure == 'size': data += b'x'
                                if failure == 'hash': data = b'x' * len(data)
                            archive.writestr(info, data)
                        if failure == 'extra-secret': archive.writestr('profiles/password.txt', b'SECRET_SENTINEL')
                        if failure == 'unsafe': archive.writestr('../escape', b'unsafe')
                        if failure == 'duplicate': archive.writestr(catalog[0]['path'].upper(), b'duplicate')
                        if failure == 'comment': archive.comment = b'PRIVATE_COMMENT_SENTINEL'
                    if failure == 'encrypted':
                        content = bytearray(package.read_bytes())
                        offset = content.index(b'PK\x01\x02')
                        flags = struct.unpack_from('<H', content, offset + 8)[0]
                        struct.pack_into('<H', content, offset + 8, flags | 1)
                        package.write_bytes(content)
                try:
                    with self.assertRaises((ValueError, RuntimeError)):
                        pack.collect(self.project)
                finally:
                    package.write_bytes(original)

    def test_names_cannot_escape_or_create_windows_special_files(self):
        for name in ('../escape', '/absolute', 'C:/drive', 'folder\\escape', './file',
                     'a//b', 'NUL.txt', 'folder/end.', 'folder/end ', ''):
            with self.subTest(name=name):
                self.assertFalse(pack.safe_name(name))
        self.assertTrue(pack.safe_name('images/workvm-empty.qcow2'))
        with self.assertRaisesRegex(ValueError, 'archive filename'):
            pack.write_archive(pack.collect(self.project), self.root / 'keep.txt:stream.zip')
        self.assertFalse((self.root / 'keep.txt').exists())


if __name__ == '__main__':
    unittest.main()
