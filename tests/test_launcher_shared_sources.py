import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LauncherSharedSourcesTests(unittest.TestCase):
    """Native checks of the C# files that the WPF launcher compiles from ui/."""

    def test_release_info_and_portable_entry(self):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        for name in ('ReleaseInfoRegression', 'PortableEntryRegression'):
            with self.subTest(harness=name), tempfile.TemporaryDirectory(prefix='launcher-shared-') as temporary:
                executable = Path(temporary) / (name + '.exe')
                command = [str(compiler), '/nologo', '/target:exe', '/platform:x64', '/main:' + name,
                           '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
                           '/reference:System.Web.Extensions.dll',
                           '/win32manifest:' + str(ROOT / 'ui/app.manifest'), '/out:' + str(executable),
                           *map(str, sorted((ROOT / 'ui').glob('*.cs'))), str(ROOT / 'tests' / (name + '.cs'))]
                build = subprocess.run(command, capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
                result = subprocess.run([str(executable), temporary], capture_output=True, timeout=45,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))


if __name__ == '__main__':
    unittest.main()
