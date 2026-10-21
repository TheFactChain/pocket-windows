from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import launch_runtime as runtime


class SessionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='portable recovery ')
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        (self.root/'state').mkdir()
        self.identities={}
        self.inventory={}

    def identity(self,pid,engine=False):
        return {'pid':pid,'created_filetime':134000000000000000+pid,
                'image':str(self.root/('runtime/qemu-11.1.0/qemu-system-x86_64.exe' if engine else 'runtime/python-3.12.14/python.exe'))}

    def work(self):
        process=self.identity(101,True)
        controller=self.identity(201)
        folder=self.root/'logs/guest-work-boot-fixture'
        folder.mkdir(parents=True)
        state={'schema':1,'guest':'work-boot','network':'isolated-lan','normal_runtime':True,'status':'running',
               'pid':101,'process':process,'controller':controller,'session_nonce':'a'*32,'generation':1,'previous_generations':[],
               'started_utc':datetime.fromtimestamp(process['created_filetime']/10000000-runtime.FILETIME_EPOCH+.2,timezone.utc).isoformat(),
               'arguments':['-drive','if=ide,format=qcow2,file=images/workvm-empty.qcow2']}
        path=folder/'state.json'
        path.write_text(json.dumps(state))
        return path,state

    def patches(self):
        return mock.patch.multiple(runtime,engine_processes=mock.Mock(side_effect=lambda root:dict(self.inventory)),
                                   process_identity=mock.Mock(side_effect=lambda pid:self.identities.get(pid)))

    def gateway(self):
        process=self.identity(102,True)
        controller=self.identity(202)
        folder=self.root/('logs/guest-gateway-boot-'+'b'*32)
        folder.mkdir(parents=True)
        disk=folder/'gateway.qcow2'
        disk.write_bytes(b'disposable gateway fixture')
        state={'schema':2,'guest':'gateway-boot','network':'gateway','status':'shutdown-unconfirmed',
               'pid':102,'process':process,'controller':controller,'nonce':'b'*32,
               'profile_id':'c'*32,'profile_revision_id':'d'*32,'gateway_version':'test.1','test_only':False,
               'disk':disk.relative_to(self.root).as_posix(),
               'disk_identity':{'device':disk.stat().st_dev,'file_id':disk.stat().st_ino},
               'started_utc':datetime.fromtimestamp(process['created_filetime']/10000000-runtime.FILETIME_EPOCH+.2,timezone.utc).isoformat(),
               'arguments':['-drive','if=virtio,format=qcow2,file='+disk.relative_to(self.root).as_posix()]}
        path=folder/'state.json'
        path.write_text(json.dumps(state))
        self.inventory[102]=self.identities[102]=process
        return {'state':str(path),'process':process,'controller':controller,'nonce':'b'*32},state

    def test_recovery_stops_only_owned_gateway_after_windows_is_gone(self):
        self.work()
        gateway,state=self.gateway()
        state['status']='running'
        Path(gateway['state']).write_text(json.dumps(state))
        self.identities[202]=state['controller']
        def stopped(root,records):
            self.inventory.clear()
            self.identities.clear()
            return []
        with self.patches(),mock.patch.object(runtime,'wait_records',side_effect=stopped):
            result=runtime.recover_session(self.root)
        self.assertTrue(result['ok'])
        self.assertFalse(result['gateway_reset'])
        controls=list(Path(gateway['state']).parent.glob('control-runtime-*.json'))
        self.assertEqual(len(controls),1)
        self.assertEqual(json.loads(controls[0].read_text()),{'action':'powerdown'})

    def test_gateway_reset_refuses_a_windows_record_before_native_termination(self):
        path,state=self.work()
        record=runtime.state_record(path,state,state['process'])
        with self.patches(),mock.patch.object(runtime,'kernel_api',side_effect=AssertionError('must not open a termination handle')):
            with self.assertRaises(RuntimeError):runtime.reset_gateway(self.root,record)

    def test_recovery_archives_dead_session_and_unblocks_discovery(self):
        path,state=self.work()
        sentinel=self.root/'images/workvm-empty.qcow2'
        sentinel.parent.mkdir()
        sentinel.write_bytes(b'private Windows sentinel')
        before=path.read_bytes()
        with self.patches():
            with self.assertRaises(RuntimeError):runtime.discover_guests(self.root)
            self.assertTrue(runtime.recovery_snapshot(self.root)['available'])
            result=runtime.recover_session(self.root)
            self.assertTrue(result['ok'])
            self.assertEqual(runtime.discover_guests(self.root),{})
            self.assertFalse(runtime.recovery_snapshot(self.root)['available'])
        self.assertEqual(runtime.read_json(path)['status'],'interrupted')
        backups=list(path.parent.glob('state-before-recovery-*.json'))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),before)
        self.assertEqual(sentinel.read_bytes(),b'private Windows sentinel')
        self.assertFalse(result['windows_terminated'])

    def test_live_windows_or_reboot_controller_prevents_recovery(self):
        path,state=self.work()
        before=path.read_bytes()
        for identity in (state['process'],state['controller']):
            self.identities={identity['pid']:identity}
            self.inventory={101:state['process']} if identity['pid']==101 else {}
            with self.subTest(pid=identity['pid']),self.patches(),mock.patch.object(runtime,'reset_gateway',create=True) as reset:
                with self.assertRaises(RuntimeError):runtime.recover_session(self.root)
                reset.assert_not_called()
            self.assertEqual(path.read_bytes(),before)

    def test_untracked_qemu_and_native_access_failure_do_not_allow_recovery(self):
        path,_=self.work()
        before=path.read_bytes()
        self.inventory={900:self.identity(900,True)}
        with self.patches():
            with self.assertRaises(RuntimeError):runtime.recover_session(self.root)
        with mock.patch.object(runtime,'engine_processes',side_effect=PermissionError('fixture native denial')):
            with self.assertRaises(OSError):runtime.recover_session(self.root)
        self.assertEqual(path.read_bytes(),before)

    def test_active_operation_lock_prevents_recovery(self):
        path,_=self.work()
        before=path.read_bytes()
        with runtime.ProjectLock(self.root),self.patches():
            with self.assertRaises(RuntimeError):runtime.recover_session(self.root)
        self.assertEqual(path.read_bytes(),before)

    def test_pid_reuse_does_not_turn_a_dead_windows_into_a_live_owned_process(self):
        path,state=self.work()
        self.identities={101:dict(state['process'],created_filetime=state['process']['created_filetime']+1,
                                  image='C:/Windows/System32/unrelated.exe')}
        with self.patches():
            self.assertTrue(runtime.recover_session(self.root)['ok'])
        self.assertEqual(runtime.read_json(path)['status'],'interrupted')

    def test_confirmed_recovery_is_terminal_for_normal_log_budget(self):
        path,_=self.work()
        with self.patches():
            runtime.recover_session(self.root)
            descriptors=runtime.normal_log_descriptors(self.root)
        self.assertTrue(next(item for item in descriptors if item['directory']==path.parent.name)['exited'])

    def historical_work(self, state, status='exited'):
        old = json.loads(json.dumps(state))
        old_root = self.root.parent / (self.root.name + '-before-move')
        for key, pid in (('process', 301), ('controller', 401)):
            old[key]['pid'] = pid
            old[key]['image'] = str(old_root / Path(old[key]['image']).relative_to(self.root))
        old.update(status=status, pid=old['process']['pid'])
        directory = self.root/'logs/guest-work-boot-old-root'
        directory.mkdir(exist_ok=True)
        path = directory/'state.json'
        path.write_text(json.dumps(old))
        return path, old

    def test_terminal_history_from_old_root_does_not_block_current_recovery(self):
        path, state = self.work()
        for status in ('exited', 'interrupted'):
            historical, _ = self.historical_work(state, status)
            before = historical.read_bytes()
            with self.subTest(status=status), self.patches():
                snapshot = runtime.recovery_snapshot(self.root)
                self.assertEqual([row['state'] for row in snapshot['work']], [str(path)])
            self.assertEqual(historical.read_bytes(), before)

    def test_old_root_history_does_not_hide_live_processes_or_native_denial(self):
        _, state = self.work()
        _, old = self.historical_work(state)
        for key in ('process', 'controller'):
            identity = old[key]
            self.identities = {identity['pid']: identity}
            with self.subTest(key=key), self.patches():
                with self.assertRaises(RuntimeError): runtime.recovery_snapshot(self.root)
        self.identities.clear()
        with self.patches(), mock.patch.object(runtime, 'process_identity', side_effect=PermissionError('native denial')):
            with self.assertRaises(OSError): runtime.recovery_snapshot(self.root)
        self.inventory = {900: self.identity(900, True)}
        with self.patches():
            with self.assertRaises(RuntimeError): runtime.recovery_snapshot(self.root)

    def test_malformed_or_nonterminal_old_root_state_is_not_ignored(self):
        _, state = self.work()
        historical, old = self.historical_work(state, 'running')
        with self.patches():
            with self.assertRaises(RuntimeError): runtime.recovery_snapshot(self.root)
        old.update(status='exited', session_nonce='invalid')
        historical.write_text(json.dumps(old))
        with self.patches():
            with self.assertRaises(RuntimeError): runtime.recovery_snapshot(self.root)


if __name__=='__main__':unittest.main()
