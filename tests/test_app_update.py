"""Exercise the .NET updater against a loopback feed, never public releases."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AppUpdateTests(unittest.TestCase):
    def test_feed_download_integrity_and_cancellation(self):
        mode = ['valid']
        payload = b'test package'
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                base = 'http://127.0.0.1:' + str(self.server.server_port)
                if self.path == '/release':
                    name = 'Pocket-Windows-2026.10.25.1-app.zip'
                    assets = [] if mode[0] == 'unavailable' else [
                        {'name': name, 'browser_download_url': base + '/app.zip', 'size': len(payload) + (1 if mode[0] == 'size' else 0)},
                        {'name': name + '.sha256', 'browser_download_url': base + '/sha'}]
                    body = json.dumps({'tag_name': 'v2026.10.25.1', 'draft': False, 'prerelease': False, 'assets': assets}).encode()
                elif self.path == '/sha':
                    body = ((('0' * 64) if mode[0] == 'corrupt' else hashlib.sha256(payload).hexdigest()) + '  app.zip\n').encode()
                else:
                    body = payload
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='app-update-test-') as temporary:
            executable = Path(temporary) / 'AppUpdateRegression.exe'
            build = subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:x64',
                                    '/reference:System.Web.Extensions.dll', '/out:' + str(executable),
                                    str(ROOT / 'launcher-wpf/AppUpdate.cs'), str(ROOT / 'launcher-wpf/Bridge.cs'),
                                    str(ROOT / 'tests/AppUpdateRegression.cs')], capture_output=True, timeout=45)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for value in ('valid', 'corrupt', 'size', 'cancel', 'unavailable', 'recovery'):
                    mode[0] = value
                    with self.subTest(value=value):
                        result = subprocess.run([str(executable), 'http://127.0.0.1:' + str(server.server_port), value],
                                                capture_output=True, timeout=30)
                        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
