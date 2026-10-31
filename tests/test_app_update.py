"""Exercise the .NET updater against loopback signed manifests, never public releases."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release_portable as release


class AppUpdateTests(unittest.TestCase):
    def test_signed_manifest_download_recovery_and_bootstrap(self):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='app-update-test-') as temporary:
            executable = Path(temporary) / 'AppUpdateRegression.exe'
            build = subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:x64',
                                    '/reference:System.Web.Extensions.dll', '/reference:System.IO.Compression.dll',
                                    '/reference:System.IO.Compression.FileSystem.dll', '/out:' + str(executable),
                                    str(ROOT / 'launcher-wpf/AppUpdate.cs'), str(ROOT / 'launcher-wpf/Bridge.cs'),
                                    str(ROOT / 'tests/AppUpdateRegression.cs')], capture_output=True, timeout=45)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            for mode in ([], ['recovery'], ['manifest'], ['v2-bootstrap']):
                with self.subTest(mode=mode):
                    result = subprocess.run([str(executable), *mode], capture_output=True, timeout=60)
                    self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
            # The release signer (Windows CNG through PowerShell 5.1) and the launcher verifier must agree byte for byte.
            key = 'PocketWindowsUpdateTest-' + uuid.uuid4().hex
            public = release.keygen(key)['public']
            try:
                manifest = {'schema': 1, 'release_id': '2026.10.31.1', 'assets': {'app': {
                    'url': 'https://mirror.example/app.zip', 'bytes': 1, 'sha256': '0' * 64}}}
                data, signer = release.envelope(manifest, key)
                self.assertEqual(signer, public)
                path = Path(temporary) / 'update-manifest.json'
                path.write_bytes(data)
                verified = subprocess.run([str(executable), 'verify', str(path), public], capture_output=True, timeout=60)
                self.assertEqual((verified.returncode, verified.stdout), (0, b'2026.10.31.1'), verified.stderr.decode(errors='replace'))
                value = json.loads(data)
                value['manifest'] = base64.b64encode(base64.b64decode(value['manifest']).replace(b'2026.10.31.1', b'2026.10.31.2')).decode()
                path.write_text(json.dumps(value))
                forged = subprocess.run([str(executable), 'verify', str(path), public], capture_output=True, timeout=60)
                self.assertNotEqual(forged.returncode, 0, 'An edited manifest must fail its signature')
            finally:
                release._cng(release.DELETE_KEY, key)

    def test_pinned_keys_match_release_sources(self):
        source = (ROOT / 'launcher-wpf/AppUpdate.cs').read_text(encoding='utf-8')
        sources = release.update_sources()
        self.assertEqual(release.release_url('2026.10.31.1', 'a.zip'),
                         sources['release_base'].replace('{release_id}', '2026.10.31.1') + 'a.zip')
        for key in sources['keys']:
            self.assertIn('"' + key + '"', source, 'Launcher and release signer must pin the same keys')
        for feed in sources['feeds']:
            self.assertIn(feed.rsplit('/', 1)[0], source, 'Built-in feeds follow update_sources.json')


if __name__ == '__main__':
    unittest.main()
