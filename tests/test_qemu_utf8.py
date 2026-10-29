"""Build and exercise both QEMU manifests using Unicode paths directly, without 8.3 aliases."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

import test_work_startup_native as native

ROOT = Path(__file__).resolve().parents[1]
QEMU = ROOT / 'runtime/qemu-11.1.0'
NAMES = ('qemu-system-x86_64.exe', 'qemu-img.exe')


@unittest.skipUnless(os.name == 'nt', 'requires Windows SDK and bundled QEMU')
class QemuUtf8Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='qemu-utf8-')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.work = Path(cls.temporary.name)
        cls.output = cls.work / 'candidates'
        cls.before = {name: hashlib.sha256((QEMU / name).read_bytes()).hexdigest() for name in NAMES}
        cls.build(cls.output)
        for path in QEMU.glob('*.dll'):
            shutil.copyfile(path, cls.output / path.name)

    @classmethod
    def build(cls, output):
        result = subprocess.run([shutil.which('pwsh'), '-NoProfile', '-NonInteractive', '-File',
                                 str(ROOT / 'scripts/Set-QemuUtf8Manifest.ps1'),
                                 '-InputDirectory', str(QEMU), '-OutputDirectory', str(output)],
                                capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise AssertionError((result.stdout + result.stderr).decode(errors='replace'))

    def test_build_preserves_sources_and_is_reproducible_for_both_executables(self):
        second = self.work / 'repeated'
        self.build(second)
        for name in NAMES:
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((QEMU / name).read_bytes()).hexdigest(), self.before[name])
                self.assertEqual((self.output / name).read_bytes(), (second / name).read_bytes())
                manifest = ET.parse(self.output / (name + '.manifest'))
                pages = manifest.findall('.//{http://schemas.microsoft.com/SMI/2019/WindowsSettings}activeCodePage')
                self.assertEqual([page.text for page in pages], ['UTF-8'])
        original_privilege = ET.parse(self.output / (NAMES[0] + '.manifest')).find(
            './/{urn:schemas-microsoft-com:asm.v3}requestedExecutionLevel')
        self.assertEqual(original_privilege.attrib['level'], 'asInvoker')

    def test_img_and_engine_open_exact_unicode_disk_and_directory_paths(self):
        case = self.work / 'Папка-😀'
        case.mkdir()
        disk = case / 'диск-😀.qcow2'
        image = self.output / 'qemu-img.exe'
        create = subprocess.run([str(image), 'create', '-f', 'qcow2', str(disk), '1M'],
                                capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(create.returncode, 0, create.stderr.decode(errors='replace'))
        self.assertTrue(disk.is_file())
        info = subprocess.run([str(image), 'info', '--output=json', str(disk)],
                              capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(info.returncode, 0, info.stderr.decode(errors='replace'))
        self.assertEqual(json.loads(info.stdout)['virtual-size'], 1048576)
        block = {'driver': 'qcow2', 'node-name': 'unicode-disk', 'file': {'driver': 'file', 'filename': str(disk)}}
        replies = native.WorkStartupNative().exercise('wcfilesnic', ['-blockdev', json.dumps(block)], self.output / NAMES[0])
        self.assertTrue(replies[5]['return']['running'])
        directory = {'driver': 'vvfat', 'node-name': 'unicode-directory', 'dir': str(case), 'rw': False, 'read-only': True}
        replies = native.WorkStartupNative().exercise('wcfilesnic', ['-blockdev', json.dumps(directory)], self.output / NAMES[0])
        self.assertTrue(replies[5]['return']['running'])

    def test_path_conversion_keeps_unicode_without_an_existing_parent(self):
        from file_snapshot import qemu_path
        path = self.work / 'missing-parent' / 'ещё-😀' / 'future.qcow2'
        self.assertFalse(path.parent.exists())
        self.assertEqual(qemu_path(path), str(path).replace('\\', '/'))
        self.assertIn('ещё-😀', qemu_path(path))

    def test_overlay_creation_keeps_the_callers_reserved_file_identity(self):
        base, overlay = (self.work / name for name in ('reserved-base.qcow2', 'reserved-overlay.qcow2'))
        image = self.output / 'qemu-img.exe'
        subprocess.run([str(image), 'create', '-f', 'qcow2', str(base), '1M'],
                       check=True, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        with overlay.open('xb') as stream:
            before = os.fstat(stream.fileno())
        subprocess.run([str(image), 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', str(base), str(overlay)],
                       check=True, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        after = overlay.stat()
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
        self.assertGreater(after.st_size, 0)


if __name__ == '__main__':
    unittest.main()
