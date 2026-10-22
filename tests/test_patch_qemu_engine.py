import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('patch_qemu_engine', ROOT / 'scripts/patch_qemu_engine.py')
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)
import pack_existing_runtime

ORIGINAL = b'MZ\0 - Press Ctrl-Alt-G to exit grab' + b''.join(original + b'\x90' for _, original, _ in engine.PATCHES) + b'QEMU (%s)\0'


class PatchQemuEngine(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='patch-qemu-')
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        files = {engine.ENGINE: ORIGINAL, engine.bundle.PYTHON + '/python.exe': b'python'}
        rows = []
        for name, content in files.items():
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            rows.append({'path': name, 'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
        (self.project / engine.bundle.MANIFEST).write_text(json.dumps({'schema': 1, 'files': rows}), encoding='utf-8')
        with zipfile.ZipFile(self.project / engine.bundle.RECOVERY, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)

    def snapshot(self):
        return {name: (self.project / name).read_bytes() for name in (engine.ENGINE, engine.bundle.MANIFEST, engine.bundle.RECOVERY)}

    def test_all_sites_patched_same_size_and_repair_contract_consistent(self):
        result = engine.update_project(self.project)
        data = (self.project / engine.ENGINE).read_bytes()
        self.assertTrue(result['changed'])
        self.assertEqual(len(data), len(ORIGINAL))
        for _, original, replacement in engine.PATCHES:
            self.assertNotIn(original, data)
            self.assertEqual(data.count(replacement), 1)
        self.assertIn(b'QEMU (%s)\0', data, 'Only the listed sites change')
        records = json.loads((self.project / engine.bundle.MANIFEST).read_text())['files']
        with (self.project / engine.bundle.RECOVERY).open('rb') as reader:
            pack_existing_runtime._validate_component_recovery(reader, records)
        self.assertEqual((Path(result['backup']) / 'qemu-system-x86_64.exe').read_bytes(), ORIGINAL)
        self.assertFalse(list(self.project.rglob('*.patching')))

    def test_second_run_changes_nothing_and_partial_patch_is_completed(self):
        partial = engine.PATCHES[0][1:]
        self.assertEqual(engine.patch(ORIGINAL.replace(*partial)), engine.patch(ORIGINAL))
        engine.update_project(self.project)
        before = self.snapshot()
        self.assertFalse(engine.update_project(self.project)['changed'])
        self.assertEqual(self.snapshot(), before)

    def test_unknown_or_tampered_engine_is_refused_without_writes(self):
        _, original, replacement = engine.PATCHES[1]
        for content in (ORIGINAL.replace(original, b'x' * len(original)), ORIGINAL + original, ORIGINAL + replacement):
            with self.subTest(content=content[-12:]):
                with self.assertRaises(ValueError):
                    engine.patch(content)
        (self.project / engine.ENGINE).write_bytes(ORIGINAL.replace(b'MZ', b'XZ'))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            engine.update_project(self.project)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(list(self.project.glob('state/qemu-engine-backup-*')))

    def test_running_engine_or_leftover_attempt_is_refused_without_writes(self):
        before = self.snapshot()
        with mock.patch.object(engine.runtime, 'engine_processes', return_value=[1234]):
            with self.assertRaises(RuntimeError):
                engine.update_project(self.project)
        (self.project / (engine.ENGINE + '.patching')).write_bytes(b'left by an interrupted run')
        with self.assertRaises(FileExistsError):
            engine.update_project(self.project)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.project / (engine.ENGINE + '.patching')).read_bytes(), b'left by an interrupted run')


class PinnedEngine(unittest.TestCase):
    def test_every_pinned_engine_hash_is_the_same_patched_engine(self):
        pinned = set()
        for name in ('portable_app.py', 'control_guest_install.py', 'reference_vm.py'):
            text = (ROOT / 'scripts' / name).read_text(encoding='utf-8')
            line = next(line for line in text.splitlines() if 'ENGINE_SHA256 =' in line or 'assert sha(engine)' in line)
            pinned.update(re.findall('[0-9a-f]{64}', line))
        self.assertEqual(len(pinned), 1, 'Launcher, controller and reference VM must accept the same engine')
        manifest = ROOT / engine.bundle.MANIFEST
        if manifest.exists():  # runtime/ is not in Git; checked whenever a developer copy has it
            rows = json.loads(manifest.read_text(encoding='utf-8-sig'))['files']
            self.assertEqual(pinned, {next(r['sha256'] for r in rows if r['path'] == engine.ENGINE)})


if __name__ == '__main__':
    unittest.main()
