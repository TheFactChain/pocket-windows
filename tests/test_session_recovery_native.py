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
        root=self.root/'проверка шлюза'
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
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        def finish():
            try:
                if child.poll() is None:
                    child.stdin.write(b'{"execute":"qmp_capabilities"}\n{"execute":"quit"}\n')
                    child.stdin.flush()
                child.wait(timeout=10)
            finally:
                child.stdin.close()
                child.stderr.close()
        self.addCleanup(finish)
        time.sleep(.5)
        if child.poll() is not None:
            self.fail('Disposable QEMU exited before reset: ' + child.stderr.read().decode('utf-8',errors='replace'))
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


@unittest.skipUnless(os.name=='nt','Windows exact-handle force stop')
class NativeForceStop(unittest.TestCase):
    def test_force_stop_pins_real_process_and_rejects_changed_arguments(self):
        with tempfile.TemporaryDirectory(prefix='force stop native ') as temporary:
            root=Path(temporary)/'пустая тестовая VM'
            engine_dir=root/'runtime/qemu-11.1.0'
            shutil.copytree(ROOT/'runtime/qemu-11.1.0',engine_dir)
            (root/'state').mkdir()
            disk=root/'images/workvm-empty.qcow2'
            disk.parent.mkdir()
            subprocess.run([str(engine_dir/'qemu-img.exe'),'create','-f','qcow2',str(disk),'16M'],
                check=True,capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
            argv=['-machine','pc','-accel','tcg','-m','32','-L','runtime/qemu-11.1.0/share','-display','none','-nic','none',
                  '-monitor','none','-qmp','stdio','-S',
                  '-drive','if=none,format=qcow2,file=images/workvm-empty.qcow2']
            child=subprocess.Popen([str(engine_dir/'qemu-system-x86_64.exe'),*argv],cwd=root,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                self.assertIn(b'QMP',child.stdout.readline())
                time.sleep(.2)
                self.assertIsNone(child.poll(),child.stderr.read() if child.poll() is not None else '')
                identity=runtime.process_identity(child.pid)
                folder=root/('logs/guest-work-boot-'+'f'*32)
                folder.mkdir(parents=True)
                path=folder/'state.json'
                state={'schema':1,'guest':'work-boot','network':'isolated-lan',
                       'status':'control-unavailable-guest-running','normal_runtime':True,
                       'pid':child.pid,'process':identity,'started_utc':runtime.utc_now(),
                       'controller':{'pid':2147483640,'created_filetime':1,
                                     'image':str(root/'runtime/python-3.12.14/python.exe')},
                       'session_nonce':'f'*32,'generation':1,'previous_generations':[],
                       'arguments':argv+['-no-reboot']}
                path.write_text(json.dumps(state))
                request={'confirmed':True,'session':runtime.force_stop_offer(root)}
                self.assertIsNotNone(request['session'])
                with self.assertRaises(RuntimeError):runtime.force_stop(root,request)
                self.assertIsNone(child.poll())
                self.assertFalse(path.with_name('stop-session.json').exists())
                state['arguments']=argv
                path.write_text(json.dumps(state))
                result=runtime.force_stop(root,request)
                self.assertEqual(result['code'],'forced_stopped')
                self.assertTrue(result['forced'])
                self.assertEqual(child.wait(timeout=10),1)
                self.assertEqual(json.loads(path.read_text())['status'],'interrupted')
                check=subprocess.run([str(engine_dir/'qemu-img.exe'),'check',str(disk)],
                    capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(check.returncode,0,check.stderr)
            finally:
                if child.poll() is None:
                    child.stdin.write(b'{"execute":"qmp_capabilities"}\n{"execute":"quit"}\n')
                    child.stdin.flush()
                    child.wait(timeout=10)
                child.stdin.close()
                child.stdout.close()
                child.stderr.close()


if __name__=='__main__':unittest.main()
