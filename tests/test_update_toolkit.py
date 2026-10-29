"""Updater toolkit boundaries and local guest build provenance."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('update_toolkit', ROOT / 'scripts/update_toolkit.py')
toolkit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(toolkit)
TEMP = ROOT.parent / 'Служебное/_temp/2026-10-27-updates/toolkit'


class ToolkitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / 'source'

    def write(self, name, data=b'content'):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def fixture(self):
        for name in toolkit.PYTHON_ROOT_FILES:
            self.write('runtime/python-3.12.14/' + name)
        for name in ('Lib/encodings/__init__.py', 'Lib/json/__init__.py', 'Lib/test/test_x.py',
                     'Lib/site-packages/untrusted.py', 'Lib/__pycache__/bad.pyc', 'DLLs/_ssl.pyd'):
            self.write('runtime/python-3.12.14/' + name)
        names = set(name for name in toolkit.pack.APP_FILES if name.endswith('.py'))
        names.update(('scripts/app_update_v2.py', 'scripts/update_toolkit.py'))
        for name in names:
            self.write(name)
        return names

    def test_archive_whitelist_inventory_and_checksum(self):
        names = self.fixture()
        self.write('docs/third-party.md', b'license notice')
        with mock.patch.object(toolkit, '_git', side_effect=lambda source, *args: '\n'.join(sorted(names)) if args[0] == 'ls-files' else 'a' * 40):
            result = toolkit.build(self.source, self.base / 'release', '2026.10.27.1')
        data = Path(result['path']).read_bytes()
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(result['bytes'], len(data))
        with zipfile.ZipFile(result['path']) as archive:
            members = set(archive.namelist())
            self.assertIn('py/python.exe', members)
            self.assertIn('scripts/app_update_v2.py', members)
            self.assertEqual(archive.read('docs/third-party.md'), b'license notice')
            self.assertFalse(any('site-packages' in name or '__pycache__' in name or '/test/' in name for name in members))
            report = json.loads(archive.read('toolkit-manifest.json'))
            self.assertEqual({row['path'] for row in report['files']}, members - {'toolkit-manifest.json'})
            for row in report['files']:
                content = archive.read(row['path'])
                self.assertEqual(row['sha256'], hashlib.sha256(content).hexdigest())
                self.assertEqual(row['bytes'], len(content))

    def test_hardlinked_runtime_rejected(self):
        names = self.fixture()
        path = self.source / 'runtime/python-3.12.14/python.exe'
        original = Path.lstat
        def linked(candidate, *args, **kwargs):
            info = original(candidate, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=2, st_file_attributes=info.st_file_attributes) if candidate == path else info
        with mock.patch.object(toolkit, '_git', return_value='\n'.join(names)), mock.patch.object(Path, 'lstat', linked):
            with self.assertRaises(ValueError):
                toolkit.build(self.source, self.base / 'release', '2026.10.27.1')

    def test_untracked_updater_rejected_before_output(self):
        names = self.fixture() - {'scripts/app_update_v2.py'}
        with mock.patch.object(toolkit, '_git', return_value='\n'.join(names)):
            with self.assertRaisesRegex(ValueError, 'tracked'):
                toolkit.build(self.source, self.base / 'release', '2026.10.27.1')
        self.assertFalse((self.base / 'release').exists())

    def test_guest_gate_rejects_old_manifest_stale_source_and_output(self):
        for name in toolkit.GUEST_INPUTS:
            self.write(name)
        executable = self.write('runtime/host-files/HostFilesGuest.exe')
        manifest_path = self.write('runtime/host-files/build-manifest.json', b'{"schema":1}')
        with self.assertRaises(ValueError):
            toolkit.validate_guest_build(self.source)
        manifest = {'schema': 1, 'component': 'host-files',
                    'local_sources': [toolkit.record(self.source, name) for name in toolkit.GUEST_INPUTS],
                    'files': [dict(toolkit.record(executable.parent, executable.name), name=executable.name)]}
        manifest['files'][0].pop('path')
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        toolkit.validate_guest_build(self.source)
        executable.write_bytes(b'changed output')
        with self.assertRaises(ValueError):
            toolkit.validate_guest_build(self.source)
        executable.write_bytes(b'content')
        self.write(toolkit.GUEST_INPUTS[0], b'changed source')
        with self.assertRaises(ValueError):
            toolkit.validate_guest_build(self.source)

    def test_full_host_gate_requires_all_inputs_and_outputs(self):
        for name in toolkit.GUEST_INPUTS:
            self.write(name)
        self.write('host/files/HostFilesServer.cs')
        self.write('host/files/ConfinedFileStore.cs')
        self.write('host/files/ConfinedFileStore.Notify.cs')
        self.write('host/files/dependency-patches/SMBServer.Signatures.cs')
        self.write('patches/smblibrary/smblibrary-1.5.8.patch')
        component = self.source/'runtime/host-files'
        manifest_path = self.write('runtime/host-files/build-manifest.json', b'{}')
        for name in toolkit.pack.FILE_COMPONENT_FILES:
            if not name.endswith('/build-manifest.json'):
                self.write(name)
        inputs = (*toolkit.GUEST_INPUTS, 'host/files/ConfinedFileStore.cs', 'host/files/ConfinedFileStore.Notify.cs',
                  'host/files/HostFilesServer.cs', 'host/files/dependency-patches/SMBServer.Signatures.cs',
                  'patches/smblibrary/smblibrary-1.5.8.patch')
        outputs = [dict(toolkit.record(component, Path(name).name), name=Path(name).name)
                   for name in toolkit.pack.FILE_COMPONENT_FILES if not name.endswith('/build-manifest.json')]
        for row in outputs:
            row.pop('path')
        report = {'schema': 1, 'component': 'host-files', 'upstream_commit': '2edbcf3161084b51dbe11b8a960b0bc7c224b851',
                  'source_archive_sha256': '2240c49f69aaffa5225e8316d5a5440ab2f20bc577bd3161091905f05b94d318',
                  'local_sources': [toolkit.record(self.source, name) for name in inputs], 'files': outputs}
        manifest_path.write_text(json.dumps(report), encoding='utf-8')
        toolkit.validate_host_build(self.source, component)
        for name in ('host/files/HostFilesServer.cs', 'runtime/host-files/HostFiles.exe', 'patches/smblibrary/smblibrary-1.5.8.patch'):
            self.write(name, b'stale')
            with self.assertRaises(ValueError):
                toolkit.validate_host_build(self.source, component)
            self.write(name)
        report['local_sources'] = report['local_sources'][:len(toolkit.GUEST_INPUTS)]
        manifest_path.write_text(json.dumps(report), encoding='utf-8')
        with self.assertRaises(ValueError):
            toolkit.validate_host_build(self.source, component)


if __name__ == '__main__':
    unittest.main()
