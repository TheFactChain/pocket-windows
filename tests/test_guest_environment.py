import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class GuestEnvironmentTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows guest helper build')
    def test_both_helpers_recognize_branded_qemu_and_refuse_the_host(self):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='guest-environment-', dir=ROOT / 'state') as temporary:
            for helper in ('files/GuestFiles.cs', 'settings/GuestSettings.cs'):
                with self.subTest(helper=helper):
                    executable = Path(temporary) / (Path(helper).stem + '.exe')
                    common = ROOT / 'guest/GuestEnvironment.cs'
                    sources = [common] if common.exists() else []
                    build = subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:x64',
                        '/main:GuestEnvironmentRegression', '/reference:System.Web.Extensions.dll',
                        '/reference:System.Windows.Forms.dll', '/reference:Microsoft.CSharp.dll',
                        '/out:' + str(executable), str(ROOT / 'guest' / helper), *map(str, sources),
                        str(ROOT / 'tests/GuestEnvironmentRegression.cs')], capture_output=True, timeout=45,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
                    check = subprocess.run([str(executable)], capture_output=True, timeout=15,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(check.returncode, 0, (check.stdout + check.stderr).decode(errors='replace'))
