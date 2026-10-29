"""Gateway replacement ownership and controller flow, without a VM or network."""
from contextlib import ExitStack, nullcontext, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import base64
import io
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import gateway_controller as controller
import launch_runtime as runtime


class GatewayOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='gateway-refresh-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'logs' / ('guest-gateway-boot-' + 'a' * 32) / 'state.json'
        self.path.parent.mkdir(parents=True)
        self.now = datetime.now(timezone.utc)
        def identity(pid, image, delta=0):
            return {'pid':pid,'created_filetime':int((self.now.timestamp()+runtime.FILETIME_EPOCH+delta)*10000000),'image':str(image)}
        self.owner = identity(900, self.root/'runtime/python-3.12.14/python.exe')
        self.first = identity(100, runtime.engine_path(self.root))
        self.second = identity(101, runtime.engine_path(self.root), 1)
        self.processes = {900:self.owner,101:self.second}
        self.old = {'schema':2,'guest':'gateway-boot','network':'gateway','normal_runtime':True,'test_only':False,
                    'nonce':'a'*32,'profile_id':'b'*32,'profile_revision_id':'c'*32,'gateway_version':'fixture',
                    'disk':self.path.parent.relative_to(self.root).as_posix()+'/gateway.qcow2',
                    'controller':self.owner,'pid':100,'process':self.first,'status':'running',
                    'generation':1,'previous_generations':[], 'started_utc':self.now.isoformat()}
        self.old['arguments'] = ['-drive','if=none,file='+self.old['disk']]
        self.record = runtime.state_record(self.path, self.old, self.first)
        self.proof = {'generation':1,'process':self.first,'exit_code':0,'guest_shutdown':True,'gone':True,'reason':'connection-refresh'}
        self.state = dict(self.old, generation=2, previous_generations=[self.proof], pid=101, process=self.second,
                          started_utc=datetime.fromtimestamp(self.now.timestamp()+1,timezone.utc).isoformat(),
                          network_verified=True,health_utc=self.now.isoformat())
        self.native = mock.patch.object(runtime,'process_identity',side_effect=lambda pid:self.processes.get(pid))
        self.native.start()
        self.addCleanup(self.native.stop)

    def save(self):
        self.path.write_text(json.dumps(self.state))

    def test_proved_successor_is_adopted_with_same_controller_and_nonce(self):
        self.save()
        self.assertTrue(runtime.record_live(self.root,self.record))
        self.assertEqual(self.record['process'],self.second)
        self.assertEqual(self.record['generation'],2)
        self.assertTrue(runtime.gateway_ready(self.root,self.record))

    def test_restart_gap_remains_owned_but_never_ready(self):
        self.state = dict(self.old,status='reconnecting',refresh_requested=True,guest_shutdown=True,exit_code=0)
        self.save()
        self.assertTrue(runtime.record_live(self.root,self.record))
        self.assertFalse(runtime.gateway_ready(self.root,self.record))
        with mock.patch.object(runtime,'atomic_json') as write:
            self.assertTrue(runtime.request_powerdown(self.root,self.record))
        self.assertEqual(write.call_args.args[1],{'action':'powerdown'})

    def test_pre_vm_next_generation_gap_uses_previous_proof(self):
        self.state.update(status='reconnecting',process=self.first,pid=100,refresh_proof=self.proof)
        self.save()
        self.assertTrue(runtime.record_live(self.root,self.record))
        with mock.patch.object(runtime,'engine_processes',return_value={}):
            current = runtime.discover_guests(self.root)
        self.assertEqual(current['gateway']['generation'],1)
        self.assertFalse(runtime.gateway_ready(self.root,current['gateway']))

    def test_discovery_rejects_forged_gap_envelope_even_when_controller_pid_is_alive(self):
        gap = dict(self.old,status='reconnecting',refresh_requested=True,guest_shutdown=True,exit_code=0)
        for field, value in (('controller',dict(self.owner,image=r'C:\foreign\python.exe')),('network','direct'),
                             ('disk','outside.qcow2'),('nonce','d'*32),('profile_id','invalid')):
            with self.subTest(field=field):
                self.state = deepcopy(gap)
                self.state[field] = value
                if field == 'controller':
                    self.processes[900] = value
                else:
                    self.processes[900] = self.owner
                self.save()
                with mock.patch.object(runtime,'engine_processes',return_value={}):
                    self.assertEqual(runtime.discover_guests(self.root),{})

    def test_failed_or_forged_proof_never_adopts_a_new_process(self):
        original = deepcopy(self.state)
        for kind in ('failed-exit','not-gone','foreign-controller','foreign-nonce','overlap'):
            with self.subTest(kind=kind):
                self.state = deepcopy(original)
                self.processes.pop(100,None)
                if kind == 'failed-exit': self.state['previous_generations'][0]['exit_code'] = 1
                if kind == 'not-gone': self.state['previous_generations'][0]['gone'] = False
                if kind == 'foreign-controller': self.state['controller']['pid'] = 999
                if kind == 'foreign-nonce': self.state['nonce'] = 'd'*32
                if kind == 'overlap': self.processes[100] = self.first
                record = runtime.state_record(self.path,self.old,self.first)
                self.save()
                try:
                    live = runtime.record_live(self.root,record)
                except RuntimeError:
                    live = False
                self.assertFalse(live)
                self.assertEqual(record['process'],self.first)


class GatewayRefreshFlowTests(unittest.TestCase):
    def test_repeated_dns_timeout_keeps_one_outstanding_lookup(self):
        pending = {}
        profile = {'host':'proxy.example','port':8080}
        with mock.patch.object(controller.threading,'Thread') as thread, mock.patch.object(controller.queue,'Queue') as queue:
            queue.return_value.get.side_effect = controller.queue.Empty
            for _ in range(3):
                with self.assertRaisesRegex(ValueError,'timed out'):
                    controller.resolve_endpoint(profile,pending)
            self.assertEqual(thread.call_count,1)
            self.assertEqual(queue.call_count,1)

    def test_replacement_requires_the_original_live_supervisor_and_windows_session(self):
        owner = {'pid':900,'created_filetime':1,'image':r'C:\fixture\python.exe'}
        supervisor = dict(owner,pid=901)
        work = {'controller':dict(owner,pid=902),'session_nonce':'f'*32}
        pair = {'phase':'ready','supervisor':supervisor,'guests':{'gateway':{'controller':owner},'work':deepcopy(work)}}
        with mock.patch.object(runtime,'active_receipt',return_value=(None,pair)), \
                mock.patch.object(runtime,'process_identity',return_value=supervisor), \
                mock.patch.object(runtime,'discover_guests',return_value={'work':work,'gateway':{'controller':owner}}), \
                mock.patch.object(runtime,'record_live',return_value=True):
            self.assertEqual(controller.continuing_work(Path('C:/fixture'),None,owner),work)
            with self.assertRaises(RuntimeError):
                controller.continuing_work(Path('C:/fixture'),dict(work,session_nonce='foreign'),owner)
            pair['phase'] = 'stopping'
            with self.assertRaises(RuntimeError):
                controller.continuing_work(Path('C:/fixture'),None,owner)

    def test_refresh_reresolves_dns_and_interface_after_fencing_without_restarting_windows(self):
        self.exercise()

    def test_dns_and_route_failures_remain_closed_then_retry_the_pinned_hostname(self):
        self.exercise(transient=True)

    def test_stop_during_dns_prevents_the_next_gateway_generation(self):
        self.exercise(stop_during_dns=True)

    def test_missing_shutdown_proof_never_starts_another_generation(self):
        self.exercise(unproved_exit=True)

    def exercise(self, transient=False, stop_during_dns=False, unproved_exit=False):
        from test_gateway_controller import GatewayControllerTests
        fixture = GatewayControllerTests()
        fixture.setUp()
        with fixture.startup_fixture() as (root,directory,spawn), ExitStack() as held, redirect_stdout(io.StringIO()):
            held.enter_context(mock.patch.object(controller,'QEMU_IMG_SHA256',hashlib.sha256(b'fixture image tool').hexdigest()))
            session = controller.validate_session({'schema':1,'nonce':'a'*32,'revision':'b'*32,'gateway_version':'1',
                'endpoint_ipv4':'203.0.113.5','profile':{'protocol':'http','host':'proxy.example','port':8080}})
            held.enter_context(mock.patch.object(controller,'make_session',return_value=({'profile_id':'c'*32},session)))
            held.enter_context(mock.patch.object(controller,'load_settings',return_value={'lan_port':31861}))
            held.enter_context(mock.patch.object(controller,'arguments',side_effect=lambda *args:['-name','gateway-fixture']))
            lookups = []
            def resolve(host,*args):
                lookups.append(host)
                if transient and len(lookups) == 1:
                    raise OSError('owned DNS failure')
                if stop_during_dns:
                    (directory/'control-runtime-stop-during-dns.json').write_text('{"action":"powerdown"}')
                return [(2,1,6,'',('203.0.113.6',8080))]
            held.enter_context(mock.patch.object(controller.socket,'getaddrinfo',side_effect=resolve))
            uplinks = []
            def uplink(endpoint):
                uplinks.append(endpoint)
                if transient and endpoint.endswith('.6') and uplinks.count(endpoint) == 1:
                    raise RuntimeError('owned physical interface absent')
                return {'interface_index':4 if endpoint.endswith('.5') else 9}
            held.enter_context(mock.patch.object(controller,'select_uplink',side_effect=uplink))
            windows = {'session_nonce':'f'*32,'process':{'pid':700},'controller':{'pid':701}}
            before = deepcopy(windows)
            held.enter_context(mock.patch.object(controller,'continuing_work',return_value=windows))
            clock = [0]
            held.enter_context(mock.patch.object(controller.time,'monotonic',side_effect=lambda:clock[0]))
            held.enter_context(mock.patch.object(controller.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+max(seconds,5))))
            children, calls, carriers = [], [], []
            def child(*args,**kwargs):
                value = SimpleNamespace(pid=100+len(children),returncode=None,stderr=SimpleNamespace(buffer=io.BytesIO()),
                                        poll=lambda:None,wait=lambda:0)
                value.poll = lambda:value.returncode
                value.wait = lambda:setattr(value,'returncode',0) or 0
                children.append(value)
                return value
            spawn.side_effect = child
            def carrier(value):
                carriers.append(deepcopy(value))
                return SimpleNamespace(path=root/'private.tar',hold=lambda:nullcontext({'fixture':True}),cleanup=lambda **kwargs:None)
            held.enter_context(mock.patch.object(controller,'create_carrier',side_effect=carrier))
            held.enter_context(mock.patch.object(controller,'start_capped_drain',return_value=SimpleNamespace(join=lambda:None)))
            class Qmp:
                def __init__(self,process,*args):
                    self.process,self.reads,self.events = process,0,[]
                    self.reader = SimpleNamespace(join=lambda **kwargs:None,is_alive=lambda:False)
                def command(self,name,arguments=None):
                    calls.append((self.process.pid,name,arguments))
                    if name == 'system_powerdown':
                        if not unproved_exit:
                            self.events.append({'event':'SHUTDOWN','data':{'guest':True,'reason':'guest-shutdown'}})
                        self.process.returncode = 0
                    if name != 'ringbuf-read': return {}
                    self.reads += 1
                    event = 'FIREWALL_READY' if self.reads == 1 else 'READY' if self.reads == 2 else 'RECONNECTING' if self.reads == 3 and self.process.pid == 100 else None
                    if self.process.pid == 101 and self.reads == 2:
                        (directory/'control-runtime-owner-stop.json').write_text('{"action":"powerdown"}')
                    raw = b'' if event is None else (json.dumps({key:session[key] for key in ('nonce','revision','gateway_version')}|{'event':event})+'\n').encode()
                    return base64.b64encode(raw).decode()
            held.enter_context(mock.patch.object(controller,'Qmp',Qmp))
            attached = []
            def attach(qmp,status,port,index):
                attached.append((qmp.process.pid,index))
                status.network_attached = True
            held.enter_context(mock.patch.object(controller,'attach_network',side_effect=attach))
            self.assertEqual(controller.run(root),1 if unproved_exit else 0)
            replaced = not (stop_during_dns or unproved_exit)
            self.assertEqual(len(children),2 if replaced else 1)
            self.assertEqual([value['endpoint_ipv4'] for value in carriers],['203.0.113.5','203.0.113.6'] if replaced else ['203.0.113.5'])
            self.assertEqual(attached,[(100,4),(101,9)] if replaced else [(100,4)])
            self.assertTrue(all(name == 'proxy.example' for name in lookups))
            stop_index = next(i for i,c in enumerate(calls) if c[0] == 100 and c[1] == 'system_powerdown')
            fence = calls[:stop_index]
            self.assertTrue(any(c[1:] == ('set_link',{'name':'wcniclan','up':False}) for c in fence))
            self.assertTrue(any(c[1:] == ('set_link',{'name':'wcnicwan','up':False}) for c in fence))
            self.assertEqual(windows,before)
            state = json.loads((directory/'state.json').read_text())
            self.assertEqual(state['generation'],2 if replaced else 1)
            if replaced:
                self.assertEqual(state['previous_generations'][0]['reason'],'connection-refresh')
            self.assertTrue(state['overlay_removed'])
            self.assertNotIn('profile',state)


if __name__ == '__main__':
    unittest.main()
