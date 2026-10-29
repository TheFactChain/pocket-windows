"""Chunk update v2: bounded ranges, persistent verified cache and offline staging."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('app_update_v2', ROOT/'scripts/app_update_v2.py')
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)


class ChunkUpdateTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT.parent/'Служебное/_temp/2026-10-27-updates/chunks'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.target = self.base/'target'
        self.target.mkdir()
        self.release = self.base/'release'
        (self.release/'app').mkdir(parents=True)
        self.info = {'schema': 1, 'release_id': '2026.10.27.1', 'source_commit': 'a'*40}
        self.files = {'release-info.json': json.dumps(self.info).encode(),
                      'scripts/portable_app.py': b'print("updated")',
                      'scripts/same.py': b'print("updated")', 'scripts/empty.py': b'',
                      'runtime/python-3.12.14/data.bin': b'x'*update.CHUNK+b'tail',
                      'runtime/components-manifest.json': b'{"schema":1,"files":[]}'}
        rows = []
        for name, data in self.files.items():
            path = self.release/'app'/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            rows.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        self.toolkit = self.base/'updater.zip'
        self.toolkit.write_bytes(b'toolkit')
        manifest = dict(self.info, common_files=rows)
        with mock.patch.object(update.release, 'verify_release', return_value=manifest):
            self.assets = update.build(self.release, self.toolkit)
        self.index = self.assets['index']
        self.digest = self.assets['index_sha256']
        self.value = update.read_index(self.index, self.digest)
        self.raw = self.assets['archive'].read_bytes()
        self.requests = []
        self.status = 206
        self.corrupt = False

    def response(self, request, timeout):
        start, end = map(int, request.get_header('Range')[6:].split('-'))
        self.requests.append((start, end))
        response = io.BytesIO((b'!'*(end-start+1)) if self.corrupt else self.raw[start:end+1])
        response.status, response.url = self.status, request.full_url
        response.headers = {'Content-Range': f'bytes {start}-{end}/{len(self.raw)}',
                            'Content-Length': str(end-start+1)}
        return response

    def prepare(self, name='stage'):
        with mock.patch.object(update, '_open', side_effect=self.response):
            return update.prepare(self.index, self.digest, self.target, self.base/name)

    def save_index(self):
        self.index.write_bytes(json.dumps(self.value).encode())
        self.digest = hashlib.sha256(self.index.read_bytes()).hexdigest()

    def test_build_chunks_before_compression_and_deduplicates(self):
        row = next(row for row in self.value['files'] if row['path'].endswith('data.bin'))
        self.assertEqual(len(row['chunks']), 2)
        self.assertEqual(max(row['bytes'] for row in self.value['chunks']), update.CHUNK)
        self.assertLess(len(self.value['chunks']), sum(len(row['chunks']) for row in self.value['files']))
        with zipfile.ZipFile(self.assets['archive']) as archive:
            self.assertEqual(set(archive.namelist()), {row['sha256'] for row in self.value['chunks']})

    def test_prepare_then_offline_repair_and_plan_is_read_only(self):
        for name, data in self.files.items():
            path = self.target/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        result = update.plan(self.index, self.digest, self.target)
        self.assertEqual(result['download_bytes'], 0)
        self.assertFalse((self.target/'state').exists())
        result = self.prepare()
        self.assertEqual(result['downloaded_bytes'], 0)
        self.assertEqual(result['changed_files'], 0)
        missing = 'runtime/python-3.12.14/data.bin'
        (self.target/missing).unlink()
        with mock.patch.object(update, '_open', side_effect=AssertionError('offline')):
            result = update.prepare(self.index, self.digest, self.target, self.base/'repair')
        self.assertEqual((self.base/'repair/app'/missing).read_bytes(), self.files[missing])
        self.assertEqual(result['changed_files'], 1)

    def test_downloads_strict_ranges_and_checks_assembled_hash(self):
        result = self.prepare()
        self.assertEqual(result['downloaded_bytes'], sum(row['compressed_bytes'] for row in self.value['chunks']))
        for name, data in self.files.items():
            self.assertEqual((self.base/'stage/app'/name).read_bytes(), data)
        self.assertEqual(json.loads((self.base/'stage/prepared.json').read_bytes())['schema'], 2)
        self.assertFalse((self.base/'stage/app.zip').exists())

    def test_cached_unchanged_update_reserves_only_remaining_operations(self):
        for name, data in self.files.items():
            path = self.target/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        update.seed_cache(self.value, self.target, self.target)
        planned = update.plan(self.index, self.digest, self.target)
        self.assertEqual(planned['changed_files'], 0)
        self.assertEqual(planned['download_bytes'], 0)
        self.assertLessEqual(planned['required_free_bytes'], 80 * 1024**2)
        usage = mock.Mock(free=80 * 1024**2)
        with mock.patch.object(update.shutil, 'disk_usage', return_value=usage), \
             mock.patch.object(update, '_fetch', side_effect=AssertionError('no download needed')):
            result = update.prepare(self.index, self.digest, self.target, self.base/'unchanged-stage')
        self.assertEqual(result['changed_files'], 0)
        self.assertFalse((self.base/'unchanged-stage/app').exists())

    def test_small_replacement_keeps_space_for_the_larger_previous_file(self):
        for name, data in self.files.items():
            path = self.target/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        update.seed_cache(self.value, self.target, self.target)
        (self.target/'scripts/portable_app.py').write_bytes(b'old' * 1024**2)
        planned = update.plan(self.index, self.digest, self.target)
        self.assertEqual(planned['changed_files'], 1)
        self.assertGreaterEqual(planned['required_free_bytes'], 86 * 1024**2)
        with mock.patch.object(update.shutil, 'disk_usage', return_value=mock.Mock(free=80 * 1024**2)):
            with self.assertRaisesRegex(RuntimeError, 'space'):
                update.prepare(self.index, self.digest, self.target, self.base/'rollback-space')

    def test_space_is_combined_on_shared_volumes_and_separated_on_other_volumes(self):
        space = {'apply': 30, 'stage': 20, 'cache': 10}
        paths = [SimpleNamespace(stat=lambda: SimpleNamespace(st_dev=1)) for _ in range(3)]
        with mock.patch.object(update.shutil, 'disk_usage', return_value=mock.Mock(free=64 * 1024**2 + 59)):
            with self.assertRaisesRegex(RuntimeError, 'space'):
                update._check_space(*paths, space)
        for index, path in enumerate(paths):
            path.stat = lambda number=index: SimpleNamespace(st_dev=number)
        with mock.patch.object(update.shutil, 'disk_usage', return_value=mock.Mock(free=64 * 1024**2 + 30)):
            update._check_space(*paths, space)

    def test_http_200_corruption_and_invalid_cache_are_not_trusted(self):
        self.status = 200
        with self.assertRaises(ValueError):
            self.prepare('badstatus')
        self.status, self.corrupt = 206, True
        with self.assertRaises((ValueError, update.zlib.error)):
            self.prepare('badbytes')
        self.corrupt = False
        self.prepare()
        cache = self.target/'state/update-cache/chunks'
        next(cache.iterdir()).write_bytes(b'broken')
        self.requests.clear()
        self.prepare('repair')
        self.assertEqual(len(self.requests), 1)

    def test_rejects_overlap_traversal_and_unreferenced_chunks(self):
        original = json.loads(json.dumps(self.value))
        for mutate in (lambda: self.value['files'][0].update(path='state/profiles.json'),
                       lambda: self.value['chunks'][1].update(offset=self.value['chunks'][0]['offset']),
                       lambda: self.value['chunks'][0].update(bytes=update.CHUNK+1),
                       lambda: self.value['files'][0].update(chunks=[])):
            self.value = json.loads(json.dumps(original))
            mutate()
            self.save_index()
            with self.assertRaises(ValueError):
                update.read_index(self.index, self.digest)

    def test_verified_chunks_survive_interruption(self):
        count = [0]
        original = self.response
        def interrupt(request, timeout):
            count[0] += 1
            if count[0] == 2:
                raise KeyboardInterrupt()
            return original(request, timeout)
        with mock.patch.object(update, '_open', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                update.prepare(self.index, self.digest, self.target, self.base/'interrupted')
        cached = list((self.target/'state/update-cache/chunks').iterdir())
        self.assertEqual(len(cached), 1)
        self.requests.clear()
        self.prepare('resumed')
        self.assertEqual(len(self.requests), len(self.value['chunks'])-1)

    def test_reuses_changed_file_chunks_and_legacy_components_zip(self):
        path = self.target/'runtime/python-3.12.14/data.bin'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'x'*update.CHUNK+b'old')
        recovery = self.target/'runtime/components-recovery.zip'
        with zipfile.ZipFile(recovery, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in self.files.items():
                if name.startswith('runtime/python-'):
                    archive.writestr(name, data)
        result = update.plan(self.index, self.digest, self.target)
        runtime_ids = next(row['chunks'] for row in self.value['files'] if row['path'].endswith('data.bin'))
        expected = sum(row['compressed_bytes'] for row in self.value['chunks'] if row['sha256'] not in runtime_ids)
        self.assertEqual(result['download_bytes'], expected)
        self.prepare()
        self.assertEqual((self.base/'stage/app/runtime/python-3.12.14/data.bin').read_bytes(), self.files['runtime/python-3.12.14/data.bin'])
        self.assertTrue(recovery.exists())

    def test_gateway_raw_image_is_split_before_compression(self):
        import struct
        raw = bytearray(update.CHUNK+100)
        struct.pack_into('>4sI', raw, 0, b'QFI\xfb', 2)
        struct.pack_into('>Q', raw, 24, 16*1024**2)
        data = bytes(raw)
        gateway = {'schema': 1, 'session_schema': 1, 'version': 'test-gateway',
                   'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'virtual_bytes': 16*1024**2}
        path = self.release/'app/runtime/gateway-recovery.zip'
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', json.dumps(gateway).encode())
            archive.writestr('gateway.qcow2', data)
        rows = [update.release.file_record(self.release/'app'/name, name) for name in self.files]
        rows.append(update.release.file_record(path, 'runtime/gateway-recovery.zip'))
        release2 = self.base/'gateway-release'
        release2.mkdir()
        (release2/'app').mkdir()
        for name in [*self.files, 'runtime/gateway-recovery.zip']:
            destination = release2/'app'/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((self.release/'app'/name).read_bytes())
        with mock.patch.object(update.release, 'verify_release', return_value=dict(self.info, common_files=rows)):
            assets = update.build(release2, self.toolkit)
        value = update.read_index(assets['index'], assets['index_sha256'])
        image = next(row for row in value['files'] if row['path'].endswith('/gateway.qcow2'))
        self.assertEqual(len(image['chunks']), 2)
        self.assertEqual(value['gateway'], gateway)
        self.assertFalse(any(row['path'].startswith('runtime/gateway-recovery') for row in value['files']))
        target_recovery = self.target/'runtime/gateway-recovery.zip'
        target_recovery.parent.mkdir()
        target_recovery.write_bytes(path.read_bytes())
        result = update.plan(assets['index'], assets['index_sha256'], self.target)
        gateway_ids = set(image['chunks'])
        self.assertGreaterEqual(result['reused_chunks'], len(gateway_ids))
        with mock.patch.object(update, '_open', side_effect=AssertionError('seed must be offline')):
            report = update.seed_cache(value, release2/'app', self.target)
        self.assertEqual(report['chunks'], len(value['chunks']))

    def test_range_headers_digest_fail_hard_and_network_retries(self):
        original = self.response
        for header in ('Content-Range', 'Content-Length', 'Content-Encoding'):
            self.requests.clear()
            def response(request, timeout):
                result = original(request, timeout)
                result.headers[header] = 'wrong'
                return result
            with mock.patch.object(update, '_open', side_effect=response):
                with self.assertRaises(ValueError):
                    update.prepare(self.index, self.digest, self.target, self.base/('bad-'+header))
            self.assertEqual(len(self.requests), 1)
        with mock.patch.object(update, '_open', side_effect=ConnectionError('offline')) as opener, mock.patch.object(update.time, 'sleep'):
            with self.assertRaises(ConnectionError):
                update.prepare(self.index, self.digest, self.target, self.base/'offline')
            self.assertEqual(opener.call_count, 3)
        self.assertFalse(update._final_url(self.value['archive']['url'], 'https://evil.example/payload'))
        request = update.urllib.request.Request(self.value['archive']['url'])
        with self.assertRaises(ValueError):
            update._Redirect().redirect_request(request, None, 302, '', {}, 'https://evil.example/payload')

    def test_safe_internal_stage_and_application_data_boundary(self):
        with mock.patch.object(update, '_open', side_effect=self.response):
            result = update.prepare(self.index, self.digest, self.target, self.target/'state'/('b'*32))
        self.assertEqual(result['phase'], 'prepared')
        self.assertFalse((self.target/'scripts').exists())
        for stage in (self.target/'scripts/new', self.target/'state/update-cache', self.target):
            with self.assertRaises(ValueError):
                update.prepare(self.index, self.digest, self.target, stage)

    def test_case_duplicates_unknown_chunks_and_bad_sha_fail(self):
        original = json.loads(json.dumps(self.value))
        for mutate in (lambda: self.value['files'].append(dict(self.value['files'][0], path='RELEASE-INFO.JSON')),
                       lambda: self.value['files'][0].update(chunks=['f'*64]),
                       lambda: self.value['archive'].update(bytes=True),
                       lambda: self.value['chunks'][0].update(offset=2**64),
                       lambda: self.value['chunks'][0].update(method=99),
                       lambda: self.value['release'].update(source_commit='not-a-sha')):
            self.value = json.loads(json.dumps(original))
            mutate()
            self.save_index()
            with self.assertRaises(ValueError):
                update.read_index(self.index, self.digest)
        with self.assertRaises(ValueError):
            update.read_index(self.index, '0'*64)

    def test_partial_cache_is_ignored_and_final_file_sha_is_checked(self):
        cache = self.target/'state/update-cache/chunks'
        cache.mkdir(parents=True)
        (cache/(self.value['chunks'][0]['sha256'] + '.part')).write_bytes(b'incomplete')
        result = update.plan(self.index, self.digest, self.target)
        self.assertEqual(result['download_bytes'], sum(row['compressed_bytes'] for row in self.value['chunks']))
        self.value['files'][0]['sha256'] = '0'*64
        self.save_index()
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse((self.base/'stage/prepared.json').exists())
        self.assertFalse((self.target/'release-info.json').exists())

    def test_hardlinks_cannot_be_local_sources_or_cache(self):
        import os
        source = self.base/'other'
        source.write_bytes(self.files['release-info.json'])
        try:
            os.link(source, self.target/'release-info.json')
        except PermissionError:
            self.skipTest('Sandbox refuses creation of a test hard link')
        with self.assertRaises(ValueError):
            update.plan(self.index, self.digest, self.target)
        self.assertEqual(source.read_bytes(), self.files['release-info.json'])

    def test_parent_child_file_collision_is_rejected(self):
        row = next(row for row in self.value['files'] if row['path'].endswith('data.bin'))
        for name in ('runtime/python-3.12.14/data.bin/a', 'runtime/python-3.12.14/data.bin-extra'):
            self.value['files'].append(dict(row, path=name))
        self.save_index()
        with self.assertRaises(ValueError):
            update.read_index(self.index, self.digest)

    def test_verify_complete_cache_before_legacy_cleanup(self):
        self.prepare()
        result = update.verify_cache(self.value, self.target)
        self.assertEqual(result['chunks'], len(self.value['chunks']))
        self.assertEqual(result['bytes'], sum(row['bytes'] for row in self.value['chunks']))
        chunk = self.target/'state/update-cache/chunks'/self.value['chunks'][0]['sha256']
        for payload in (b'broken', update.zlib.compress(b'wrong', wbits=-15)):
            chunk.write_bytes(payload)
            with self.assertRaises(ValueError):
                update.verify_cache(self.value, self.target)
        chunk.unlink()
        with self.assertRaises(ValueError):
            update.verify_cache(self.value, self.target)

    def test_cache_gc_only_lists_unreferenced_sha_names(self):
        self.prepare()
        cache = self.target/'state/update-cache/chunks'
        stale = cache/('f'*64)
        stale.write_bytes(b'unreferenced')
        (cache/'keep.part').write_bytes(b'partial')
        referenced = {row['sha256'] for row in self.value['chunks']}
        self.assertEqual(update.cache_gc_candidates(self.target, referenced), [stale])
        self.assertTrue(stale.exists())
        with self.assertRaises(ValueError):
            update.cache_gc_candidates(self.target, {'../unsafe'})

    def test_vm_off_guard_rejects_live_and_unconfirmed_runtime_before_writes(self):
        for state in ({42: {'pid': 42, 'image': 'qemu-system-x86_64.exe'}}, None, False):
            with mock.patch.object(update.release.runtime, 'engine_processes', return_value=state):
                with self.assertRaises(RuntimeError):
                    update.plan(self.index, self.digest, self.target)
                with self.assertRaises(RuntimeError):
                    update.prepare(self.index, self.digest, self.target, self.base/'refused')
            self.assertFalse((self.base/'refused').exists())
            self.assertFalse((self.target/'state').exists())
        with mock.patch.object(update.release.runtime, 'engine_processes', side_effect=OSError('snapshot denied')):
            with self.assertRaises(RuntimeError):
                update.plan(self.index, self.digest, self.target)

    def test_v1_size_caps_are_retained(self):
        for key in ('archive', 'toolkit'):
            self.value[key]['bytes'] = 8*1024**3+1
            self.save_index()
            with self.assertRaises(ValueError):
                update.read_index(self.index, self.digest)
            self.value[key]['bytes'] = len(self.raw)

    def test_offline_missing_cache_never_requests_network(self):
        with mock.patch.object(update, '_open', side_effect=AssertionError('network forbidden')) as opener:
            with self.assertRaisesRegex(RuntimeError, 'Offline'):
                update.prepare(self.index, self.digest, self.target, self.base/'offline-missing', offline=True)
            opener.assert_not_called()
        self.prepare()
        cache = self.target/'state/update-cache/chunks'
        victim = cache/self.value['chunks'][0]['sha256']
        victim.unlink()
        retained = {path.name for path in cache.iterdir()}
        with mock.patch.object(update, '_open', side_effect=AssertionError('network forbidden')) as opener:
            with self.assertRaisesRegex(RuntimeError, 'Offline'):
                update.prepare(self.index, self.digest, self.target, self.base/'offline-partial', offline=True)
            opener.assert_not_called()
        self.assertEqual({path.name for path in cache.iterdir()}, retained)
        with mock.patch.object(update, '_open', side_effect=self.response):
            update.prepare(self.index, self.digest, self.target, self.base/'refill')
        with mock.patch.object(update, '_open', side_effect=AssertionError('network forbidden')) as opener:
            result = update.prepare(self.index, self.digest, self.target, self.base/'offline-full', offline=True)
            opener.assert_not_called()
        self.assertEqual(result['downloaded_bytes'], 0)

    def test_optional_old_gateway_missing_or_corrupt_metadata_is_skipped(self):
        import struct
        old = self.target/'gateway/packages/releases/old-version'
        old.mkdir(parents=True)
        raw = bytearray(update.CHUNK)
        struct.pack_into('>4sI', raw, 0, b'QFI\xfb', 2)
        struct.pack_into('>Q', raw, 24, 16*1024**2)
        disk = bytes(raw) + b'tail'
        (old/'gateway.qcow2').write_bytes(disk)
        metadata = old/'manifest.json'
        wrong_digest = {'schema': 1, 'session_schema': 1, 'version': 'old-version',
                        'bytes': len(disk), 'sha256': '0'*64, 'virtual_bytes': 16*1024**2}
        expected = sum(row['compressed_bytes'] for row in self.value['chunks'])
        for payload in (None, b'invalid JSON', b'{}', json.dumps(wrong_digest).encode()):
            if payload is not None:
                metadata.write_bytes(payload)
            with mock.patch.object(update, '_open', side_effect=AssertionError('plan is read only')) as opener:
                result = update.plan(self.index, self.digest, self.target)
                opener.assert_not_called()
            self.assertEqual(result['download_bytes'], expected)
        self.assertFalse((self.target/'state').exists())

    def test_full_source_seeds_cache_then_prepares_offline_without_changing_app(self):
        damaged = self.target/'scripts/portable_app.py'
        damaged.parent.mkdir(parents=True)
        damaged.write_bytes(b'damaged installed script')
        with mock.patch.object(update, '_open', side_effect=AssertionError('network forbidden')) as opener:
            report = update.seed_cache(self.value, self.release/'app', self.target)
            result = update.prepare(self.index, self.digest, self.target, self.base/'seeded', offline=True)
            opener.assert_not_called()
        self.assertEqual(report['chunks'], len(self.value['chunks']))
        self.assertEqual(result['downloaded_bytes'], 0)
        self.assertEqual(damaged.read_bytes(), b'damaged installed script')
        self.assertFalse((self.release/'app/state').exists())
        for name, data in self.files.items():
            self.assertEqual((self.base/'seeded/app'/name).read_bytes(), data)

    def test_incomplete_seed_fails_without_network_or_application_change(self):
        source = self.base/'incomplete'
        source.mkdir()
        (source/'release-info.json').write_bytes(self.files['release-info.json'])
        with mock.patch.object(update, '_open', side_effect=AssertionError('network forbidden')) as opener:
            with self.assertRaises(ValueError):
                update.seed_cache(self.value, source, self.target)
            opener.assert_not_called()
        self.assertFalse((self.target/'scripts').exists())


if __name__ == '__main__':
    unittest.main()
