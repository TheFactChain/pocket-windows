import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import portable_app as app


@unittest.skipUnless(os.name == 'nt', 'Native Windows supervisor lifetime regression')
class GuiStartNativeTests(unittest.TestCase):
    def test_direct_start_returns_while_supervisor_lives_under_restricted_policy(self):
        with tempfile.TemporaryDirectory(prefix='portable startup Тест ') as temporary:
            root = Path(temporary)
            (root / 'scripts').mkdir()
            shutil.copytree(ROOT / 'runtime/python-3.12.14', root / 'runtime/python-3.12.14')
            shutil.copyfile(ROOT / 'scripts/launch_runtime.py', root / 'scripts/runtime_probe.py')
            shutil.copyfile(ROOT / 'scripts/runtime_config.py', root / 'scripts/runtime_config.py')
            release = root / 'release'
            script = """import argparse,json,os,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import runtime_probe
p=argparse.ArgumentParser();p.add_argument('--project');p.add_argument('--receipt');a=p.parse_args()
root=Path(a.project)
(root/'child.pid').write_text(str(os.getpid()))
r={'schema':1,'project':str(root),'phase':'ready','supervisor':runtime_probe.process_identity(os.getpid())}
tmp=Path(a.receipt+'.tmp');tmp.write_text(json.dumps(r));os.replace(tmp,a.receipt)
print('x'*100000,flush=True)
end=time.monotonic()+20
while not (root/'release').exists() and time.monotonic()<end:time.sleep(.05)
"""
            (root / 'scripts/launch_runtime.py').write_text(script, encoding='utf-8')
            before = {'ok': True, 'network_verified': False, 'network_acceptance_scope': 'unverified',
                      'network_warning': '', 'connection': {}, 'runtime': {}}
            with mock.patch.object(app, 'preflight', return_value=before), \
                    mock.patch.dict(os.environ, {'PSExecutionPolicyPreference': 'Restricted'}), \
                    concurrent.futures.ThreadPoolExecutor(1) as pool:
                pending = pool.submit(app.start, root)
                try:
                    result = pending.result(timeout=8)
                    self.assertTrue(result['ok'], result)
                    self.assertEqual(result['code'], 'ready')
                    pid = int((root / 'child.pid').read_text())
                    identity = app.runtime.process_identity(pid)
                    self.assertIsNotNone(identity, 'Startup must return before its supervisor exits')
                    self.assertFalse(release.exists())
                finally:
                    release.write_text('release')
                    pending.result(timeout=15)
                    if (root / 'child.pid').exists():
                        pid = int((root / 'child.pid').read_text())
                        deadline = time.monotonic() + 10
                        while app.runtime.process_identity(pid) and time.monotonic() < deadline:
                            time.sleep(.05)
                        self.assertIsNone(app.runtime.process_identity(pid))


if __name__ == '__main__':
    unittest.main()
