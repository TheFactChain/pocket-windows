"""Exercise the .NET updater against a loopback feed, never public releases."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AppUpdateTests(unittest.TestCase):
    def test_feed_download_integrity_and_cancellation(self):
        mode = ['valid']
        payload = b'test package'
        requests = []
        index = [b'']
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append(self.path)
                base = 'http://127.0.0.1:' + str(self.server.server_port)
                if self.path == '/release':
                    name = 'Pocket-Windows-2026.10.25.1-app.zip'
                    assets = [] if mode[0] == 'unavailable' else [
                        {'name': name, 'browser_download_url': base + '/app.zip', 'size': len(payload) + (1 if mode[0] == 'size' else 0)},
                        {'name': name + '.sha256', 'browser_download_url': base + '/sha'}]
                    if mode[0].startswith('indexed-') or mode[0] == 'index-missing':
                        index_name = 'Pocket-Windows-2026.10.25.1-update.json'
                        assets.append({'name': index_name, 'browser_download_url': base + '/index', 'size': len(index[0])})
                        if mode[0] != 'index-missing':
                            assets.append({'name': index_name + '.sha256', 'browser_download_url': base + '/index-sha', 'size': 80})
                    body = json.dumps({'tag_name': 'v2026.10.25.1', 'draft': False, 'prerelease': False, 'assets': assets}).encode()
                elif self.path == '/sha':
                    body = ((('0' * 64) if mode[0] == 'corrupt' else hashlib.sha256(payload).hexdigest()) + '  app.zip\n').encode()
                elif self.path == '/index':
                    body = index[0]
                elif self.path == '/index-sha':
                    body = ((('0' * 64) if mode[0] == 'indexed-corrupt' else hashlib.sha256(index[0]).hexdigest()) + '  update.json\n').encode()
                else:
                    body = payload
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='app-update-test-') as temporary:
            executable = Path(temporary) / 'AppUpdateRegression.exe'
            installed = Path(temporary) / 'installed'
            shutil.copytree(ROOT / 'runtime/python-3.12.14', installed / 'runtime/python-3.12.14')
            (installed / 'scripts').mkdir()
            (installed / 'scripts/app_download.py').write_text('''import argparse, hashlib, json, pathlib, sys, time
p = argparse.ArgumentParser()
for name in ('index', 'index-sha256', 'target', 'output'): p.add_argument('--' + name, required=True)
a = p.parse_args()
assert hashlib.sha256(pathlib.Path(a.index).read_bytes()).hexdigest() == a.index_sha256
value = json.loads(pathlib.Path(a.index).read_text(encoding='utf-8'))
pathlib.Path(a.target, 'helper-ran.txt').write_text('ran')
mode = value['test_mode']
if mode == 'indexed-failure': raise SystemExit(2)
if mode == 'indexed-noise': print('x' * 10000, flush=True); time.sleep(30)
if mode == 'indexed-cancel': print(json.dumps({'progress': 0}), flush=True); time.sleep(30)
data = b'test package'
pathlib.Path(a.output).write_bytes(data)
print(json.dumps({'progress': 50}), flush=True)
print(json.dumps({'sha256': '0' * 64 if mode == 'indexed-package-corrupt' else hashlib.sha256(data).hexdigest(), 'bytes': len(data)}), flush=True)
''', encoding='utf-8')
            build = subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:x64',
                                    '/reference:System.Web.Extensions.dll', '/out:' + str(executable),
                                    str(ROOT / 'launcher-wpf/AppUpdate.cs'), str(ROOT / 'launcher-wpf/Bridge.cs'),
                                    str(ROOT / 'tests/AppUpdateRegression.cs')], capture_output=True, timeout=45)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for value in ('valid', 'corrupt', 'size', 'cancel', 'unavailable', 'recovery', 'index-missing',
                              'indexed-valid', 'indexed-corrupt', 'indexed-version', 'indexed-failure',
                              'indexed-package-corrupt', 'indexed-noise', 'indexed-cancel'):
                    mode[0] = value
                    base = 'http://127.0.0.1:' + str(server.server_port)
                    index[0] = json.dumps({'schema': 1,
                        'release': {'release_id': 'wrong' if value == 'indexed-version' else '2026.10.25.1'},
                        'archive': {'url': base + '/app.zip', 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}, 'test_mode': value}).encode()
                    requests.clear()
                    marker = installed / 'helper-ran.txt'
                    marker.unlink(missing_ok=True)
                    with self.subTest(value=value):
                        result = subprocess.run([str(executable), base, value, str(installed)],
                                                capture_output=True, timeout=30)
                        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
                        if value.startswith('indexed-'):
                            self.assertNotIn('/app.zip', requests, 'Indexed failure must never silently download the full package')
                        if value in ('indexed-corrupt', 'indexed-version'):
                            self.assertFalse(marker.exists(), 'Unverified index must not reach the helper')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
