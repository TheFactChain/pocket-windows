"""Exercise offreg only against an empty fixture hive created by this regression."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class OfflineMaintenanceRegistryTests(unittest.TestCase):
    def test_fixture_hive_install_remove_and_roundtrip(self):
        source = ROOT / 'scripts/OfflineMaintenanceRegistry.cs'
        self.assertTrue(source.is_file(), 'Offline maintenance registry helper missing')
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='offreg-regression-', dir=ROOT / 'tests') as directory:
            target = Path(directory) / 'OfflineMaintenanceRegistryRegression.exe'
            build = subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:x64',
                                    '/out:' + str(target), str(source),
                                    str(ROOT / 'tests/OfflineMaintenanceRegistryRegression.cs')],
                                   capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(target), directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('checks PASS; no host registry or real SYSTEM hive used', result.stdout)


if __name__ == '__main__':
    unittest.main()
