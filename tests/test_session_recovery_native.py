import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import launch_runtime as runtime
from gateway_controller import arguments
from runtime_config import validate_settings


@unittest.skipUnless(os.name=='nt','Windows exact-handle recovery')
class NativeGatewayRecovery(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='gateway recovery native ')
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        (self.root/'state').mkdir()
        engine_dir=self.root/'runtime/qemu-11.1.0'
        shutil.copytree(ROOT/'runtime/python-3.12.14',engine_dir)
        self.engine=engine_dir/'qemu-system-x86_64.exe'
        shutil.copyfile(engine_dir/'python.exe',self.engine)
        self.release=self.root/'release'
        script='import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nend=time.monotonic()+20\nwhile not p.exists() and time.monotonic()<end:time.sleep(.05)'
        self.child=subprocess.Popen([str(self.engine),'-I','-S','-B','-c',script,str(self.release)],
                                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(self.finish_child)
        identity=runtime.process_identity(self.child.pid)
        self.assertIsNotNone(identity)
        folder=self.root/('logs/guest-gateway-boot-'+'b'*32)
        folder.mkdir(parents=True)
        disk=folder/'gateway.qcow2'
        disk.write_bytes(b'fake disposable disk; no VM is started')
        carrier=folder/'session.tar'
        carrier.write_bytes(b'fixture')
        self.path=folder/'state.json'
        self.state={'schema':2,'guest':'gateway-boot','network':'gateway','status':'shutdown-unconfirmed',
                    'pid':self.child.pid,'process':identity,'controller':{'pid':2147483640,'created_filetime':1,'image':str(self.root/'runtime/python-3.12.14/python.exe')},
                    'nonce':'b'*32,'profile_id':'c'*32,'profile_revision_id':'d'*32,'gateway_version':'test.1','test_only':False,
                    'disk':disk.relative_to(self.root).as_posix(),
                    'disk_identity':{'device':disk.stat().st_dev,'file_id':disk.stat().st_ino},
                    'started_utc':runtime.utc_now(),'carrier':str(carrier),
                    'arguments':arguments(self.root,folder,{'path':carrier,'serial':'WCSESSION','readonly':True},validate_settings({}))}
        self.path.write_text(json.dumps(self.state))
        self.record=runtime.state_record(self.path,self.state,identity)

    def finish_child(self):
        self.release.write_text('release')
        self.child.wait(timeout=10)

    def test_forged_gateway_state_cannot_reset_a_process_with_other_arguments(self):
        disk=self.root/'images/workvm-empty.qcow2'
        disk.parent.mkdir()
        disk.write_bytes(b'private Windows remains unchanged')
        with self.assertRaises(RuntimeError):runtime.recover_session(self.root)
        self.assertIsNone(self.child.poll())
        self.assertEqual(disk.read_bytes(),b'private Windows remains unchanged')

    def test_changed_creation_time_cannot_reset_even_same_live_pid(self):
        self.state['process']['created_filetime']+=1
        self.path.write_text(json.dumps(self.state))
        with self.assertRaises(RuntimeError):runtime.reset_gateway(self.root,self.record)
        self.assertIsNone(self.child.poll())

    def test_reset_of_a_real_disposable_qemu_without_a_windows_disk(self):
        root=self.root/'real disposable gateway'
        engine_dir=root/'runtime/qemu-11.1.0'
        shutil.copytree(ROOT/'runtime/qemu-11.1.0',engine_dir)
        (root/'state').mkdir()
        folder=root/('logs/guest-gateway-boot-'+'e'*32)
        folder.mkdir(parents=True)
        disk=folder/'gateway.qcow2'
        subprocess.run([str(engine_dir/'qemu-img.exe'),'create','-f','qcow2',str(disk),'16M'],
                       check=True,capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
        carrier=folder/'session.tar'
        carrier.write_bytes(bytes(5120))
        argv=arguments(root,folder,{'path':carrier,'serial':'WCSESSION','readonly':True},
                       validate_settings({'gateway_memory_mib':256,'vcpus':1}))
        child=subprocess.Popen([str(engine_dir/'qemu-system-x86_64.exe'),*argv],cwd=root,
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        def finish():
            try:
                if child.poll() is None:
                    child.stdin.write(b'{"execute":"qmp_capabilities"}\n{"execute":"quit"}\n')
                    child.stdin.flush()
                child.wait(timeout=10)
            finally:
                child.stdin.close()
        self.addCleanup(finish)
        time.sleep(.5)
        self.assertIsNone(child.poll(),'Disposable QEMU must be alive before reset')
        identity=runtime.process_identity(child.pid)
        state=dict(self.state,pid=child.pid,process=identity,nonce='e'*32,
                   disk=disk.relative_to(root).as_posix(),arguments=argv,carrier=str(carrier),
                   disk_identity={'device':disk.stat().st_dev,'file_id':disk.stat().st_ino},
                   controller={'pid':2147483640,'created_filetime':1,'image':str(root/'runtime/python-3.12.14/python.exe')},
                   started_utc=runtime.utc_now())
        path=folder/'state.json'
        path.write_text(json.dumps(state))
        result=runtime.recover_session(root)
        self.assertTrue(result['ok'])
        self.assertTrue(result['gateway_reset'])
        self.assertFalse(result['windows_terminated'])
        self.assertIsNotNone(child.poll())


if __name__=='__main__':unittest.main()
