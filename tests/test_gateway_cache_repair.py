"""Offline raw gateway recovery uses only the installed component catalog."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import app_update_v2 as update
import gateway_package as package
import portable_app
import update_storage as storage


class GatewayCacheRepairTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT.parent/'Служебное/_temp/2026-10-27-updates/guest'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root/'gateway/packages'
        self.directory = self.base/'releases/v1'
        self.directory.mkdir(parents=True)
        self.disk = self.directory/'gateway.qcow2'
        raw = bytearray(4096)
        struct.pack_into('>4sI', raw, 0, b'QFI\xfb', 3)
        struct.pack_into('>Q', raw, 24, 4*1024**3)
        self.raw = bytes(raw)
        self.manifest = dict(schema=1, session_schema=1, version='v1', bytes=len(raw),
                             sha256=hashlib.sha256(raw).hexdigest(), virtual_bytes=4*1024**3)
        self.disk.write_bytes(b'damaged-current')
        (self.directory/'manifest.json').write_text(json.dumps(self.manifest))
        (self.base/'current.json').write_text(json.dumps(dict(schema=1, current='v1', previous=None)))
        self.info = dict(schema=1, release_id='2026.10.27.1', source_commit='a'*40)
        (self.root/'release-info.json').write_text(json.dumps(self.info))
        cache = self.root/'state/update-cache/chunks'
        cache.mkdir(parents=True)
        files, chunks = [], []
        payload = {'release-info.json': json.dumps(self.info).encode(),
                   'gateway/packages/releases/v1/manifest.json': json.dumps(self.manifest).encode(),
                   'gateway/packages/releases/v1/gateway.qcow2': self.raw}
        offset = 0
        for name, data in payload.items():
            sha = hashlib.sha256(data).hexdigest()
            compressed = zlib.compress(data, 1, wbits=-15)
            (cache/sha).write_bytes(compressed)
            chunks.append(dict(sha256=sha, bytes=len(data), offset=offset,
                               compressed_bytes=len(compressed), method=8))
            offset += len(compressed)
            files.append(dict(path=name, bytes=len(data), sha256=sha, chunks=[sha]))
        url = 'https://github.com/nekontritce11123-lab/pocket-windows/releases/download/v2026.10.27.1/Pocket-Windows-2026.10.27.1'
        self.index = dict(schema=2, min_updater=2, release=self.info,
                          archive=dict(url=url+'-chunks.zip', bytes=offset, sha256='a'*64),
                          toolkit=dict(url=url+'-updater.zip', bytes=1, sha256='b'*64),
                          files=files, chunks=chunks, gateway=self.manifest)
        raw_index = json.dumps(self.index).encode()
        self.sha = hashlib.sha256(raw_index).hexdigest()
        catalogs = self.root/'state/components/catalogs'
        catalogs.mkdir(parents=True)
        self.index_path = catalogs/(self.sha+'.json')
        self.index_path.write_bytes(raw_index)
        (catalogs.parent/'current.json').write_text(json.dumps(dict(schema=2, catalog=self.sha+'.json',
                                                                  sha256=self.sha, release_id=self.info['release_id'])))
        for name, content in (('images/workvm.qcow2', b'private-windows'), ('private/profiles.json', b'private-profile')):
            path = self.root/name
            path.parent.mkdir(parents=True)
            path.write_bytes(content)
        for target, value in (('ProjectLock', True), ('engine_processes', [])):
            patch = mock.patch.object(package.runtime, target)
            started = patch.start()
            self.addCleanup(patch.stop)
            if target == 'ProjectLock':
                started.return_value.__enter__.return_value = value
            else:
                started.return_value = value
        network = mock.patch.object(update, '_open', side_effect=AssertionError('repair must be offline'))
        network.start()
        self.addCleanup(network.stop)

    def test_corrupt_gateway_repairs_offline_and_retains_personal_data(self):
        result = portable_app.repair_gateway(self.root)
        self.assertEqual(result['code'], 'gateway_repaired')
        self.assertEqual(self.disk.read_bytes(), self.raw)
        self.assertEqual((self.root/'images/workvm.qcow2').read_bytes(), b'private-windows')
        self.assertEqual((self.root/'private/profiles.json').read_bytes(), b'private-profile')
        self.assertEqual(next((self.base/'quarantine').glob('*/gateway.qcow2')).read_bytes(), b'damaged-current')

    def test_corrupt_cache_refuses_before_gateway_mutation(self):
        chunk = self.root/'state/update-cache/chunks'/self.index['chunks'][-1]['sha256']
        chunk.write_bytes(b'invalid')
        before = {str(path.relative_to(self.base)): path.read_bytes() for path in self.base.rglob('*') if path.is_file()}
        with self.assertRaises(ValueError):
            package.repair_cached(self.root, self.index_path, self.sha)
        after = {str(path.relative_to(self.base)): path.read_bytes() for path in self.base.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_running_vm_or_busy_lock_refuses(self):
        with mock.patch.object(package.runtime, 'engine_processes', return_value=[{}]):
            with self.assertRaises(RuntimeError):
                package.repair_cached(self.root, self.index_path, self.sha)
        with mock.patch.object(package.runtime, 'ProjectLock') as lock:
            lock.return_value.__enter__.return_value = False
            with self.assertRaises(RuntimeError):
                package.repair_cached(self.root, self.index_path, self.sha)
        self.assertEqual(self.disk.read_bytes(), b'damaged-current')

    def test_catalog_source_must_match_installed_release(self):
        (self.root/'release-info.json').write_text(json.dumps(dict(self.info, source_commit='b'*40)))
        with self.assertRaises(ValueError):
            package.repair_cached(self.root, self.index_path, self.sha)
        self.assertEqual(self.disk.read_bytes(), b'damaged-current')

    def test_valid_previous_gateway_remains_rollback_candidate(self):
        previous = self.base/'releases/v0'
        previous.mkdir()
        (previous/'gateway.qcow2').write_bytes(self.raw)
        (previous/'manifest.json').write_text(json.dumps(dict(self.manifest, version='v0')))
        (self.base/'current.json').write_text(json.dumps(dict(schema=1, current='v1', previous='v0')))
        result = package.repair_cached(self.root, self.index_path, self.sha)
        self.assertEqual(result['previous'], 'v0')
        self.assertEqual((previous/'gateway.qcow2').read_bytes(), self.raw)

    def test_interrupted_promotion_resumes_same_catalog(self):
        rename = Path.rename
        def fail_promotion(path, destination):
            if path.name.startswith('.staging-'):
                raise OSError('interrupted gateway promotion')
            return rename(path, destination)
        with mock.patch.object(Path, 'rename', autospec=True, side_effect=fail_promotion):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                package.repair_cached(self.root, self.index_path, self.sha)
        self.assertEqual(json.loads((self.base/'repair.json').read_text())['phase'], 'prepared')
        package.repair_cached(self.root, self.index_path, self.sha)
        self.assertEqual(self.disk.read_bytes(), self.raw)
        self.assertEqual(json.loads((self.base/'repair.json').read_text())['phase'], 'committed')

    def test_logical_file_sha_checked_after_chunk_sha(self):
        self.index['files'][1]['sha256'] = 'c'*64
        raw_index = json.dumps(self.index).encode()
        sha = hashlib.sha256(raw_index).hexdigest()
        path = self.index_path.parent/(sha+'.json')
        path.write_bytes(raw_index)
        (self.root/storage.CATALOG).write_text(json.dumps(dict(schema=2, catalog=path.name,
                                                              sha256=sha, release_id=self.info['release_id'])))
        with self.assertRaisesRegex(ValueError, 'Assembled'):
            package.repair_cached(self.root, path, sha)
        self.assertEqual(self.disk.read_bytes(), b'damaged-current')
        self.assertFalse((self.base/'repair.json').exists())

    def test_legacy_zip_fallback_remains_available_without_catalog(self):
        (self.root/storage.CATALOG).unlink()
        runtime = self.root/'runtime'
        runtime.mkdir()
        (runtime/'gateway-recovery.zip').write_bytes(b'legacy')
        (runtime/'gateway-recovery.json').write_text(json.dumps(dict(schema=1, file='gateway-recovery.zip', sha256='a'*64)))
        with mock.patch.object(package, 'repair_package', return_value={'version': 'legacy'}) as repair:
            self.assertEqual(portable_app.repair_gateway(self.root)['gateway_version'], 'legacy')
        repair.assert_called_once()


if __name__ == '__main__':
    unittest.main()
