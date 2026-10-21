import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prepare_runtime_bundle', ROOT / 'scripts/prepare_runtime_bundle.py')
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


class RuntimeBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='runtime-bundle-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        self.output = self.base / 'output'
        files = {bundle.QEMU + '/' + name: name.encode() for name in bundle.REQUIRED_QEMU}
        files.update({bundle.QEMU + '/example.dll': b'host dll', bundle.QEMU + '/share/keymaps/en-us': b'keyboard',
            bundle.QEMU + '/qemu-system-aarch64.exe': b'foreign architecture',
            bundle.QEMU + '/share/edk2-aarch64-code.fd': b'foreign firmware',
            bundle.PYTHON + '/python.exe': b'python', bundle.PYTHON + '/Lib/unchanged.py': b'full python'})
        self.records = []
        for name, content in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            self.records.append({'path': name, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
        self.write_manifest()

    def write_manifest(self):
        (self.source / 'runtime/components-manifest.json').write_text(json.dumps({'schema': 1, 'files': self.records}), encoding='utf-8')

    def test_bundle_is_exact_repair_contract_and_keeps_python(self):
        result = bundle.prepare_bundle(self.source, self.output)
        manifest = json.loads((self.output / 'runtime/components-manifest.json').read_text())
        names = {record['path'] for record in manifest['files']}
        self.assertIn(bundle.PYTHON + '/Lib/unchanged.py', names)
        self.assertIn(bundle.QEMU + '/example.dll', names)
        self.assertNotIn(bundle.QEMU + '/qemu-system-aarch64.exe', names)
        self.assertNotIn(bundle.QEMU + '/share/edk2-aarch64-code.fd', names)
        with zipfile.ZipFile(self.output / 'runtime/components-recovery.zip') as archive:
            self.assertEqual(set(archive.namelist()), names)
            for record in manifest['files']:
                content = archive.read(record['path'])
                self.assertEqual(len(content), record['bytes'])
                self.assertEqual(hashlib.sha256(content).hexdigest(), record['sha256'])
        self.assertFalse(result['boot_verified'])
        self.assertLess(result['payload_bytes_after'], result['payload_bytes_before'])
        self.assertEqual(result['archive_sha256'], hashlib.sha256((self.output / 'runtime/components-recovery.zip').read_bytes()).hexdigest())

    def test_existing_output_and_source_overlap_are_refused_without_writes(self):
        self.output.mkdir(); sentinel = self.output / 'keep'; sentinel.write_text('keep')
        for output in (self.output, self.source / 'new', self.source):
            with self.subTest(output=output):
                with self.assertRaises(ValueError): bundle.prepare_bundle(self.source, output)
        self.assertEqual(sentinel.read_text(), 'keep')
        self.assertFalse((self.source / 'new').exists())

    def test_invalid_manifest_names_duplicates_and_missing_files_fail_before_output(self):
        original = list(self.records)
        for name in ('runtime/../private/password', 'runtime/qemu-11.1.0/../private',
                     'runtime/qemu-11.1.0/file:ads', 'runtime/qemu-11.1.0/CON.txt',
                     'runtime/qemu-11.1.0/trailing.', 'runtime\\qemu-11.1.0\\evil'):
            self.records = original + [{'path': name, 'bytes': 0, 'sha256': 'a' * 64}]
            self.write_manifest()
            with self.subTest(name=name):
                with self.assertRaises(ValueError): bundle.prepare_bundle(self.source, self.output)
            self.assertFalse(self.output.exists())
        self.records = original + [original[0]]; self.write_manifest()
        with self.assertRaises(ValueError): bundle.prepare_bundle(self.source, self.output)
        self.records = original + [dict(original[0], path=original[0]['path'].upper())]; self.write_manifest()
        with self.assertRaises(ValueError): bundle.prepare_bundle(self.source, self.output)
        self.records = original[:-1]; self.write_manifest()
        with self.assertRaises(ValueError): bundle.prepare_bundle(self.source, self.output)

    def test_missing_required_and_selected_source_tamper_fail_before_output(self):
        target = self.source / bundle.QEMU / 'qemu-img.exe'
        target.write_bytes(b'tampered')
        with self.assertRaises(ValueError): bundle.prepare_bundle(self.source, self.output)
        self.assertFalse(self.output.exists())
        target.unlink()
        self.records = [r for r in self.records if r['path'] != bundle.QEMU + '/qemu-img.exe']; self.write_manifest()
        with self.assertRaises(ValueError): bundle.prepare_bundle(self.source, self.output)

    def test_reparse_ancestor_is_rejected(self):
        real = bundle._is_reparse
        marker = self.source / 'runtime' / 'qemu-11.1.0' / 'share'
        with patch.object(bundle, '_is_reparse', side_effect=lambda path, info: path == marker or real(path, info)):
            with self.assertRaises(ValueError): bundle.prepare_bundle(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_output_parent_reparse_and_untracked_python_are_rejected(self):
        real = bundle._is_reparse
        with patch.object(bundle, '_is_reparse', side_effect=lambda path, info: path == self.base or real(path, info)):
            with self.assertRaises(ValueError): bundle.prepare_bundle(self.source, self.output)
        (self.source / bundle.PYTHON / 'untracked.py').write_text('unexpected')
        with self.assertRaises(ValueError): bundle.prepare_bundle(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_manifest_schema_types_unknown_fields_and_duplicate_json_keys_are_rejected(self):
        path = self.source / 'runtime/components-manifest.json'
        for raw in ('{"schema":true,"files":[]}', '{"schema":1,"schema":1,"files":[]}',
                    '{"schema":1,"files":[],"private":"not allowed"}', '{"schema":NaN,"files":[]}'):
            path.write_text(raw, encoding='utf-8')
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError): bundle.prepare_bundle(self.source, self.output)
            self.assertFalse(self.output.exists())

    def test_mutation_between_inventory_and_copy_is_detected(self):
        real = bundle.inventory
        def tamper(root):
            result = real(root)
            (self.source / bundle.PYTHON / 'python.exe').write_bytes(b'changed after inventory')
            return result
        with patch.object(bundle, 'inventory', side_effect=tamper):
            with self.assertRaises(ValueError): bundle.prepare_bundle(self.source, self.output)
        self.assertFalse((self.output / 'runtime/components-manifest.json').exists())

    def test_inventory_is_read_only(self):
        before = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        report = bundle.inventory(self.source)
        self.assertEqual(before, {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob('*') if p.is_file()})
        self.assertGreater(report['payload_bytes_before'], report['payload_bytes_after'])
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
