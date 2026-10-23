"""Compile/run helper regressions only; never install or start a service."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MaintenanceServiceTests(unittest.TestCase):
    def test_guards_hash_report_and_bounded_output(self):
        source = ROOT / 'guest/maintenance/MaintenanceService.cs'
        self.assertTrue(source.is_file(), 'Maintenance service implementation missing')
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='maintenance-regression-', dir=ROOT / 'tests') as directory:
            target = Path(directory) / 'MaintenanceServiceRegression.exe'
            build = subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:x64',
                                    '/main:MaintenanceServiceRegression', '/out:' + str(target),
                                    '/reference:System.ServiceProcess.dll', '/reference:System.Management.dll',
                                    '/reference:System.Web.Extensions.dll', str(source),
                                    str(ROOT / 'tests/MaintenanceServiceRegression.cs')],
                                   capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(target)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('checks PASS; no service execution', result.stdout)


if __name__ == '__main__':
    unittest.main()
