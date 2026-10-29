"""Native ADS/capability regressions against the production store."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT.parent / 'Служебное/_temp/2026-10-27-fix-audit/capabilities'


@unittest.skipUnless(os.name == 'nt', 'Windows native confinement proof')
class CapabilitiesTests(unittest.TestCase):
    def test_native_regressions(self):
        main = ROOT / 'host/files/ConfinedFileStore.cs'
        before = main.read_bytes()
        TEMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='native-', dir=TEMP) as temporary:
            output = Path(temporary)
            snapshot = output / 'ConfinedFileStore.cs'
            snapshot.write_bytes(before)
            libraries = ('SMBLibrary.dll', 'SMBLibrary.Win32.dll', 'Utilities.dll')
            for name in libraries:
                shutil.copyfile(ROOT / 'runtime/host-files' / name, output / name)
            executable = output / 'HostFilesCapabilitiesRegression.exe'
            compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
            command = [str(compiler), '/nologo', '/target:exe', '/platform:x64', '/main:HostFilesCapabilitiesRegression',
                       *['/reference:' + str(output / name) for name in libraries], '/out:' + str(executable),
                       str(snapshot), str(ROOT / 'host/files/ConfinedFileStore.Notify.cs'),
                       str(ROOT / 'host/files/ConfinedFileStore.Capabilities.cs'),
                       str(ROOT / 'tests/HostFilesCapabilitiesRegression.cs')]
            build = subprocess.run(command, capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            run = subprocess.run([str(executable), str(output)], capture_output=True, timeout=45,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            evidence = (run.stdout + run.stderr).decode(errors='replace')
            (TEMP / 'native-evidence.txt').write_text(evidence, encoding='utf-8')
            self.assertEqual(run.returncode, 0, evidence)
            self.assertIn('BLOCKER: native CreateHardLink succeeds', evidence)
            self.assertIn('PASS: staged ADS', evidence)
        self.assertEqual(main.read_bytes(), before, 'Tests must never modify the production store')
        (TEMP / 'main-sha256.txt').write_text(hashlib.sha256(before).hexdigest() + '\n', encoding='ascii')


if __name__ == '__main__':
    unittest.main()
