import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DesktopLauncherTests(unittest.TestCase):
    def test_compiled_exe_controls_root_and_native_argument_roundtrip(self):
        source = ROOT / 'ui/Launcher.cs'
        self.assertTrue(source.is_file(), 'Desktop launcher implementation is missing')
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        self.assertTrue(compiler.is_file(), 'Installed .NET Framework compiler is required')
        with tempfile.TemporaryDirectory(prefix='desktop portable ') as temporary:
            root = Path(temporary)
            executable = root / 'Launcher.exe'
            command = [str(compiler), '/nologo', '/target:winexe', '/platform:x64',
                       '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
                       '/reference:System.Web.Extensions.dll',
                       '/win32manifest:' + str(ROOT / 'ui/app.manifest'),
                       '/out:' + str(executable), str(source)]
            build = subprocess.run(command, capture_output=True, timeout=45,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            report = root / 'self-test.json'
            elsewhere = root / 'different working directory'
            elsewhere.mkdir()
            result = subprocess.run([str(executable), '--self-test', str(report)], cwd=elsewhere,
                                    capture_output=True, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
            data = json.loads(report.read_text(encoding='utf-8-sig'))
            self.assertTrue(all(data['checks'].values()), data)
            self.assertEqual(Path(data['project_root']), root)
            self.assertEqual(set(data['buttons']), {'Запустить', 'Настройки', 'Диагностика'})
            self.assertEqual(list(elsewhere.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
