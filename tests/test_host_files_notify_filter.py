"""Only native operations on own new fixture directories; no SMB, VM or clipboard."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
SERVICE = SOURCE.parent / 'Служебное' / '_temp' / '2026-10-27-fix-audit'
DLLS = SERVICE / 'smblibrary-p4' / 'build-p4-2'


@unittest.skipUnless(os.name == 'nt', 'Windows native file API')
class NotifyFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = Path(tempfile.mkdtemp(prefix='hostfiles-notify-filter-', dir=SERVICE))
        sources = ('host/files/ConfinedFileStore.cs', 'host/files/ConfinedFileStore.Notify.cs', 'host/files/ConfinedFileStore.Capabilities.cs',
                   'tests/HostFilesNotifyFilterRegression.cs')
        manifest, paths = {}, []
        for index, name in enumerate(sources):
            data = (SOURCE / name).read_bytes()
            path = cls.build / (str(index) + '-' + Path(name).name)
            path.write_bytes(data); paths.append(str(path))
            manifest[name] = hashlib.sha256(data).hexdigest()
        references = []
        for name in ('Utilities.dll', 'SMBLibrary.dll', 'SMBLibrary.Win32.dll'):
            path = cls.build / name; shutil.copyfile(DLLS / name, path)
            references.append('/r:' + str(path)); manifest[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        cls.exe = cls.build / 'HostFilesNotifyFilterRegression.exe'
        compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        result = subprocess.run([str(compiler), '/nologo', '/platform:x64', '/main:HostFilesNotifyFilterRegression',
                                 '/out:' + str(cls.exe), *references, *paths], capture_output=True)
        (cls.build / 'compile.stdout').write_bytes(result.stdout)
        (cls.build / 'compile.stderr').write_bytes(result.stderr)
        (cls.build / 'baseline.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        if result.returncode: raise AssertionError(result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace'))

    def run_native(self, mode):
        result = subprocess.run([str(self.exe), mode], capture_output=True, timeout=20)
        (self.build / (mode[2:] + '.stdout')).write_bytes(result.stdout)
        (self.build / (mode[2:] + '.stderr')).write_bytes(result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace'))

    def test_native_protected_tree_and_current_watched_path(self): self.run_native('--native')
    def test_synthetic_strict_records_and_rename_pairs(self): self.run_native('--synthetic')


if __name__ == '__main__': unittest.main()
