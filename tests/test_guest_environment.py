import os
import base64
import json
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLIPBOARD = [ROOT / 'guest' / name for name in ('GuestClipboard.cs', 'ClipboardFiles.cs', 'ClipboardStage.cs', 'ClipboardProtocol.cs', 'ClipboardTransport.cs')]


class GuestEnvironmentTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows guest helper build')
    def test_both_helpers_recognize_branded_qemu_and_refuse_the_host(self):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='guest-environment-') as temporary:
            for helper in ('files/GuestFiles.cs', 'settings/GuestSettings.cs'):
                with self.subTest(helper=helper):
                    executable = Path(temporary) / (Path(helper).stem + '.exe')
                    common = ROOT / 'guest/GuestEnvironment.cs'
                    sources = [common, *CLIPBOARD] if common.exists() else []
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

    @unittest.skipUnless(os.name == 'nt', 'Windows guest helper build')
    def test_guest_clipboard_protocol(self):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='guest-clipboard-') as temporary:
            executable = Path(temporary) / 'GuestClipboardRegression.exe'
            build = subprocess.run([str(compiler), '/nologo', '/target:exe', '/platform:x64', '/main:GuestClipboardRegression',
                '/reference:System.Web.Extensions.dll', '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
                '/reference:Microsoft.CSharp.dll', '/out:' + str(executable), *map(str, CLIPBOARD), str(ROOT / 'tests/GuestClipboardRegression.cs')],
                capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            check = subprocess.run([str(executable)], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(check.returncode, 0, (check.stdout + check.stderr).decode(errors='replace'))

            sys.path.insert(0, str(ROOT / 'scripts'))
            import clipboard_bridge
            nonce = 'f' * 32
            for text in ('Привет 😀\r\nnext', 'x' * 100000):
                with self.subTest(text_length=len(text)):
                    line = clipboard_bridge.encode(nonce, text, 5000000000, 'host')
                    decoded = subprocess.run([str(executable), '--decode', nonce], input=line,
                                             capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(decoded.returncode, 0, decoded.stderr.decode(errors='replace'))
                    self.assertEqual(base64.b64decode(decoded.stdout).decode('utf-8'), text)
                    encoded = subprocess.run([str(executable), '--encode', nonce, 'guest', '5000000001'],
                                             input=base64.b64encode(text.encode('utf-8')), capture_output=True,
                                             timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(encoded.returncode, 0, encoded.stderr.decode(errors='replace'))
                    self.assertEqual(clipboard_bridge.decode(encoded.stdout, nonce), text)
                    self.assertEqual(json.loads(encoded.stdout)['s'], 5000000001)
