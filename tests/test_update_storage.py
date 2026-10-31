"""Catalog migration and conservative cache collection on disposable installs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('update_storage', ROOT/'scripts/update_storage.py')
storage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(storage)


class StorageTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT.parent/'Служебное/_temp/2026-10-27-updates/toolkit'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.target = self.base/'installed'
        self.target.mkdir()

    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def index(self, number):
        version = '2026.10.27.' + str(number)
        raw = json.dumps({'schema': 1, 'release_id': version, 'source_commit': 'a'*40}).encode()
        sha = hashlib.sha256(raw).hexdigest()
        encoder = zlib.compressobj(wbits=-15)
        compressed = encoder.compress(raw) + encoder.flush()
        self.put(self.target/'state/update-cache/chunks'/sha, compressed)
        url = 'https://github.com/TheFactChain/pocket-windows/releases/download/v' + version + '/Pocket-Windows-' + version
        value = {'schema': 2, 'min_updater': 2, 'release': json.loads(raw),
                 'archive': {'url': url+'-chunks.zip', 'bytes': len(compressed)+40, 'sha256': 'b'*64},
                 'toolkit': {'url': url+'-updater.zip', 'bytes': 1, 'sha256': 'c'*64},
                 'files': [{'path': 'release-info.json', 'bytes': len(raw), 'sha256': sha, 'chunks': [sha]}],
                 'chunks': [{'sha256': sha, 'bytes': len(raw), 'offset': 30, 'compressed_bytes': len(compressed), 'method': 8}],
                 'gateway': None}
        data = json.dumps(value).encode()
        index_sha = hashlib.sha256(data).hexdigest()
        path = self.put(self.base/('index-'+str(number)+'.json'), data)
        return path, index_sha, value, sha

    def install_catalog(self, number):
        path, sha, value, chunk = self.index(number)
        self.put(self.target/('state/components/catalogs/'+sha+'.json'), path.read_bytes())
        self.put(self.target/storage.CATALOG, json.dumps({'schema': 2, 'catalog': sha+'.json', 'sha256': sha,
                                                       'release_id': value['release']['release_id']}).encode())
        return path, sha, value, chunk

    def journal(self, name, phase, index):
        path, sha, value, chunk = index
        relative = 'state/components/catalogs/'+sha+'.json'
        self.put(self.target/relative, path.read_bytes())
        row = storage.release.file_record(self.target/relative, relative)
        journal = {'schema': 2, 'target': str(self.target), 'target_id': storage.release.identity(self.target),
                   'transaction': name, 'phase': phase,
                   'files': [{'path': relative, 'old': None, 'new': row,
                              'new_id': storage.release.identity(self.target/relative), 'phase': 'installed'}]}
        self.put(self.target/'state'/name/'journal.json', json.dumps(journal).encode())
        return journal

    def test_catalog_created_only_after_complete_cache_validation(self):
        path, sha, value, chunk = self.index(1)
        generated = self.base/'generated'
        result = storage.build_catalog(path, sha, self.target, generated)
        self.assertEqual((generated/('state/components/catalogs/'+sha+'.json')).read_bytes(), path.read_bytes())
        self.assertEqual({row['path'] for row in result}, {storage.CATALOG, 'state/components/catalogs/'+sha+'.json'})
        self.put(self.target/'state/update-cache/chunks'/chunk, b'corrupt')
        rejected = self.base/'rejected'
        with self.assertRaises(ValueError):
            storage.build_catalog(path, sha, self.target, rejected)
        self.assertFalse(rejected.exists())

    def test_current_pointer_rejects_path_and_release_mismatch(self):
        _, sha, value, _ = self.install_catalog(1)
        self.assertEqual(storage.current_index(self.target)[1:], (sha, value))
        pointer = json.loads((self.target/storage.CATALOG).read_bytes())
        for change in ({'catalog': '../'+sha+'.json'}, {'release_id': '2026.10.27.99'}, {'sha256': 'f'*64}):
            self.put(self.target/storage.CATALOG, json.dumps(dict(pointer, **change)).encode())
            with self.assertRaises(ValueError):
                storage.current_index(self.target)

    def test_references_keep_current_rollback_pending_and_toolkit_chunks(self):
        current = self.install_catalog(1)
        rollback, pending, tools = self.index(2), self.index(3), self.index(4)
        keep = 'app-update-'+'1'*32
        self.journal(keep, 'committed', rollback)
        pending_journal = self.journal('app-update-'+'2'*32, 'prepared', pending)
        self.put(self.target/'state/app-update.json', json.dumps(pending_journal).encode())
        self.put(self.target/('state/app-updater-'+'3'*32)/'index.json', tools[0].read_bytes())
        self.assertEqual(storage.referenced_chunks(self.target, keep_transactions=(keep,)),
                         {current[3], rollback[3], pending[3], tools[3]})

    def test_retained_rollback_old_pointer_keeps_previous_catalog(self):
        current = self.install_catalog(1)
        keep = 'app-update-'+'1'*32
        journal = self.journal(keep, 'committed', self.index(2))
        previous = self.index(3)
        self.put(self.target/('state/components/catalogs/'+previous[1]+'.json'), previous[0].read_bytes())
        old_pointer = self.put(self.target/'state'/keep/'old'/storage.CATALOG,
                              json.dumps({'schema': 2, 'catalog': previous[1]+'.json', 'sha256': previous[1],
                                          'release_id': previous[2]['release']['release_id']}).encode())
        journal['files'].append({'path': storage.CATALOG, 'new': storage.release.file_record(self.target/storage.CATALOG, storage.CATALOG),
                                 'old': storage.release.file_record(old_pointer, storage.CATALOG),
                                 'new_id': storage.release.identity(self.target/storage.CATALOG), 'phase': 'installed'})
        self.put(self.target/'state'/keep/'journal.json', json.dumps(journal).encode())
        references = storage.referenced_chunks(self.target, keep_transactions=(keep,))
        self.assertIn(previous[3], references)
        self.assertIn(current[3], references)

    def test_hardlinked_gc_candidate_blocks_deletion(self):
        self.install_catalog(1)
        keep = 'app-update-'+'1'*32
        self.journal(keep, 'committed', self.index(2))
        candidate = self.put(self.target/'state/update-cache/chunks'/('f'*64), b'orphan')
        original = Path.lstat
        def linked(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=2, st_file_attributes=info.st_file_attributes) if path == candidate else info
        with mock.patch.object(storage.release, 'project_processes', return_value={}), mock.patch.object(Path, 'lstat', linked):
            with self.assertRaises(ValueError):
                storage.cleanup(self.target, keep)
        self.assertTrue(candidate.exists())

    def test_cleanup_preserves_unknown_and_corrupt_before_any_deletion(self):
        self.install_catalog(1)
        keep = 'app-update-'+'1'*32
        self.journal(keep, 'committed', self.index(2))
        garbage = self.put(self.target/'state/update-cache/chunks'/('f'*64), b'orphan')
        self.put(self.target/'state/app-update-corrupt.json', b'{')
        with mock.patch.object(storage.release, 'project_processes', return_value={}):
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                storage.cleanup(self.target, keep)
        self.assertTrue(garbage.exists())
        self.assertTrue((self.target/'state'/keep/'journal.json').exists())

    def test_cleanup_removes_only_old_completed_and_unreferenced(self):
        current = self.install_catalog(1)
        keep, old = 'app-update-'+'1'*32, 'app-update-'+'2'*32
        rollback, retired = self.index(2), self.index(3)
        self.journal(keep, 'committed', rollback)
        self.journal(old, 'committed', retired)
        unknown = self.put(self.target/'state/unrelated-owner/data.txt', b'keep me')
        with mock.patch.object(storage.release, 'project_processes', return_value={}):
            storage.cleanup(self.target, keep)
        self.assertTrue((self.target/'state'/keep).exists())
        self.assertFalse((self.target/'state'/old).exists())
        self.assertTrue(unknown.exists())
        cache = self.target/'state/update-cache/chunks'
        self.assertTrue((cache/current[3]).exists())
        self.assertTrue((cache/rollback[3]).exists())
        self.assertFalse((cache/retired[3]).exists())

    def test_unknown_transaction_subdirectory_preserves_transaction_and_chunks(self):
        self.install_catalog(1)
        keep, old = 'app-update-'+'1'*32, 'app-update-'+'2'*32
        self.journal(keep, 'committed', self.index(2))
        retired = self.index(3)
        self.journal(old, 'committed', retired)
        unknown = self.target/'state'/old/'foreign-empty-directory'
        unknown.mkdir()
        with mock.patch.object(storage.release, 'project_processes', return_value={}):
            result = storage.cleanup(self.target, keep)
        self.assertIn(old, result['preserved'])
        self.assertTrue(unknown.exists())
        self.assertTrue((self.target/'state/update-cache/chunks'/retired[3]).exists())

    def stage(self, name, index, extra=False):
        path, sha, value, _ = index
        root = self.target/'state'/name
        self.put(root/'index.json', path.read_bytes())
        self.put(root/'prepared.json', json.dumps({'schema': 2, 'index_sha256': sha, 'omitted': []}).encode())
        self.put(root/'app/release-info.json', json.dumps(value['release']).encode())
        if extra:
            self.put(root/'personal.txt', b'keep')
        return root

    def tools(self, name, index):
        path, _, value, _ = index
        root = self.target/'state'/name
        self.put(root/'index.json', path.read_bytes())
        rows = []
        for relative in ('py/python.exe', 'scripts/app_update_v2.py', 'scripts/release_portable.py'):
            output = self.put(root/relative, b'owned toolkit')
            rows.append(storage.release.file_record(output, relative))
        self.put(root/'toolkit-manifest.json', json.dumps({'schema': 1, 'release_id': value['release']['release_id'],
                 'source_commit': value['release']['source_commit'],
                 'source_inputs': [dict(row, path=row['path'].replace('py/', 'runtime/python-3.12.14/', 1) if row['path'].startswith('py/') else row['path']) for row in rows],
                 'files': rows}).encode())
        return root

    def test_collects_only_proven_old_ephemeral_and_keeps_recovery_and_unknown(self):
        current = self.install_catalog(1)
        keep = 'app-update-'+'1'*32
        self.journal(keep, 'committed', self.index(2))
        old = self.index(3)
        stage = self.stage('a'*32, old)
        tools = self.tools('app-updater-'+'b'*32, old)
        current_tools = self.tools('app-updater-'+'c'*32, current)
        unknown = self.stage('d'*32, old, extra=True)
        recovery = self.tools('app-updater-'+'e'*32, old)
        self.put(self.target/'state/update-recovery.json', json.dumps({'schema': 2, 'work': recovery.name}).encode())
        with mock.patch.object(storage.release, 'project_processes', return_value={}):
            result = storage.cleanup(self.target, keep)
        self.assertFalse(stage.exists())
        self.assertFalse(tools.exists())
        self.assertTrue(current_tools.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(recovery.exists())
        self.assertIn(stage.name, result['deleted_stages'])
        self.assertIn(tools.name, result['deleted_tools'])

    def test_only_receipted_probe_process_is_allowed(self):
        current = self.install_catalog(1)
        keep = 'app-update-'+'1'*32
        journal = self.journal(keep, 'committed', current)
        probe = {'nonce': 'f'*32, 'pid': 123, 'created': '456'}
        journal.update(schema=3, release=current[2]['release'], probe=probe)
        self.put(self.target/'state'/keep/'journal.json', json.dumps(journal).encode())
        receipt = dict(schema=1, committed=True, transaction=keep, root=str(self.target),
                       release_id=current[2]['release']['release_id'], source_commit='a'*40, **probe)
        self.put(self.target/'state/update-accepted.json', json.dumps(receipt).encode())
        process = {'pid': 123, 'created_filetime': 456, 'image': str(self.target/'Launcher.exe')}
        with mock.patch.object(storage.release, 'project_processes', return_value={123: process}), mock.patch.object(storage.release.runtime, 'process_identity', return_value=process):
            storage.cleanup(self.target, keep, allowed_probe=probe)
        for mutate in ({'created_filetime': 999}, {'image': str(self.target/'other.exe')}):
            with mock.patch.object(storage.release, 'project_processes', return_value={123: process}), mock.patch.object(storage.release.runtime, 'process_identity', return_value=dict(process, **mutate)):
                with self.assertRaises((ValueError, RuntimeError)):
                    storage.cleanup(self.target, keep, allowed_probe=probe)
        with mock.patch.object(storage.release, 'project_processes', return_value={123: process, 999: process}), mock.patch.object(storage.release.runtime, 'process_identity', return_value=process):
            with self.assertRaises(RuntimeError):
                storage.cleanup(self.target, keep, allowed_probe=probe)
        self.put(self.target/'state/update-accepted.json', json.dumps(dict(receipt, nonce='a'*32)).encode())
        with mock.patch.object(storage.release, 'project_processes', return_value={123: process}), mock.patch.object(storage.release.runtime, 'process_identity', return_value=process):
            with self.assertRaises(ValueError):
                storage.cleanup(self.target, keep, allowed_probe=probe)

    def test_unindexed_legacy_toolkit_preserved_without_blocking_chunk_collection(self):
        self.install_catalog(1)
        keep = 'app-update-'+'1'*32
        self.journal(keep, 'committed', self.index(2))
        legacy = self.put(self.target/('state/app-updater-'+'a'*32)/'app.zip', b'legacy download')
        orphan = self.put(self.target/'state/update-cache/chunks'/('f'*64), b'orphan')
        with mock.patch.object(storage.release, 'project_processes', return_value={}):
            storage.cleanup(self.target, keep)
        self.assertTrue(legacy.exists())
        self.assertFalse(orphan.exists())

    def test_present_malformed_toolkit_index_still_blocks_collection(self):
        self.install_catalog(1)
        keep = 'app-update-'+'1'*32
        self.journal(keep, 'committed', self.index(2))
        self.put(self.target/('state/app-updater-'+'a'*32)/'index.json', b'{')
        orphan = self.put(self.target/'state/update-cache/chunks'/('f'*64), b'orphan')
        with mock.patch.object(storage.release, 'project_processes', return_value={}):
            with self.assertRaises(ValueError):
                storage.cleanup(self.target, keep)
        self.assertTrue(orphan.exists())


if __name__ == '__main__':
    unittest.main()
