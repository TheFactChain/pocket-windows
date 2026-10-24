"""Execute the production controller loop with canned QMP children, never QEMU."""
import ast
from collections import deque
from contextlib import nullcontext, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import launch_runtime as runtime


class WorkRebootSequence(unittest.TestCase):
    def exercise(self, reasons=('guest-reset','guest-shutdown'), first_code=0, guest=True, stop_gap=False, gateway_outage=False, close_error=False,
                 files_network=False, link_error=False):
        with tempfile.TemporaryDirectory(prefix='work-reboot-sequence-') as temporary:
            root=Path(temporary)
            output=root/'logs/guest-work-boot-fixture123'
            output.mkdir(parents=True)
            engine=root/'runtime/qemu-11.1.0/qemu-system-x86_64.exe'
            controller={'pid':901,'created_filetime':134000000000000000,'image':str(root/'runtime/python-3.12.14/python.exe')}
            children=[]
            class Thread:
                def __init__(self,target,**kwargs):self.target=target
                def start(self):self.target()
                def join(self,**kwargs):pass
                def is_alive(self):return False
            def spawn(command,**kwargs):
                index=len(children)
                child=mock.Mock(pid=100+index)
                child.identity={'pid':child.pid,'created_filetime':134000000010000000+index*10000000,'image':str(engine)}
                child.alive=True
                child.poll.return_value=first_code if index==0 else 0
                child.returncode=child.poll.return_value
                child.wait.side_effect=lambda **kwargs:setattr(child,'alive',False) or child.returncode
                messages=[{'QMP':{'version':{'fixture':True}}}]+[{'id':i,'return':[]} for i in range(1,7 if files_network else 6)]
                if link_error:
                    messages[5]={'id':5,'error':{'class':'GenericError','desc':'fixture'}}
                messages += [{'event':'SHUTDOWN','data':{'guest':guest,'reason':reasons[index]}}]
                child.stdout=io.StringIO('\n'.join(map(json.dumps,messages)))
                child.sent=[]
                class Input(io.StringIO):
                    def write(self,text):
                        child.sent.append(text)
                        return super().write(text)
                    def close(self):
                        super().close()
                        if close_error:
                            raise OSError(22, 'Closed QMP pipe fixture')
                child.stdin=Input()
                child.stderr=SimpleNamespace(buffer=io.BytesIO())
                child.command=command
                children.append(child)
                return child
            def native(pid):
                if pid==901:return controller
                return next((child.identity for child in children if child.pid==pid and child.alive),None)
            def sleep(seconds):
                if stop_gap:
                    runtime.atomic_json(output/'stop-session.json',{'session_nonce':'a'*32,'controller':controller})
            tree=ast.parse((ROOT/'scripts/control_guest_install.py').read_text(encoding='utf-8'))
            names={'spawn_generation','save','read_qmp','command','work_reset_proof','work_stop_requested'}
            functions=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
            driver=next(node for node in tree.body if isinstance(node,ast.Try) and any(isinstance(child,ast.While) for child in node.body))
            final=tree.body[-1]
            writer=mock.Mock()
            files=SimpleNamespace(arguments=[],public={'status':'pending','share_count':1},medium=None,network=files_network,
                                  healthy=lambda:True,close=mock.Mock())
            file_api=SimpleNamespace(Session=mock.Mock(return_value=files))
            api=SimpleNamespace(Popen=mock.Mock(side_effect=spawn),PIPE=subprocess.PIPE,CREATE_NO_WINDOW=subprocess.CREATE_NO_WINDOW)
            scope={'runtime':runtime,'normal_runtime':True,'work_reboot_session':True,'output':output,'root':root,'engine':engine,'env':{},
                'display_settings':None, 'protect_work_window':mock.Mock(return_value=True),
                'show_work_window':mock.Mock(return_value=False),'pointer_settled':mock.Mock(return_value=True),
                'work_startup_info':mock.Mock(return_value=None),
                'prepare_taskbar_icon':mock.Mock(return_value='test-icon.ico'),
                'clear_work_taskbar':mock.Mock(),
                'file_sharing':file_api,'files_session':None,'clipboard':None,'clipboard_port':None,
                'pointer_thread':SimpleNamespace(is_alive=lambda:True),
                'arguments':['-drive','file=images/workvm-empty.qcow2','-no-reboot'],
                'session_nonce':'a'*32,'controller_identity':controller,'generation':1,'previous_generations':[],
                'stderr_writer':writer,'stderr':subprocess.PIPE,'start_capped_drain':mock.Mock(return_value=mock.Mock()),
                'subprocess':api,'threading':SimpleNamespace(Thread=Thread),'queue':queue,'deque':deque,
                'datetime':datetime,'timezone':timezone,'json':json,'controls':queue.Queue(),
                'args':SimpleNamespace(guest='work-boot',network='isolated-lan'),'gateway':{'fixture':True},
                'time':SimpleNamespace(monotonic=time.monotonic,sleep=sleep)}
            failure=None
            with mock.patch.object(runtime,'process_identity',side_effect=native), \
                 mock.patch.object(runtime,'gateway_ready',side_effect=[True,False,True] if gateway_outage else None,return_value=True), \
                 mock.patch.object(runtime,'record_live',return_value=True), \
                 mock.patch.object(runtime,'controller_live',return_value=True), \
                 mock.patch.object(runtime,'work_session_lock',side_effect=lambda directory:nullcontext()), \
                 redirect_stdout(io.StringIO()):
                exec(compile(ast.Module(body=functions,type_ignores=[]),'controller-functions','exec'),scope)
                try:
                    exec(compile(ast.Module(body=[driver,final],type_ignores=[]),'controller-loop','exec'),scope)
                except SystemExit as error:
                    failure=error.code
            report=runtime.read_json(output/'state.json')
            file_api.Session.assert_called_once_with(root,'a'*32)
            files.close.assert_called_once_with(guest_stopped=True)
            self.assertTrue(all(not child.alive for child in children), 'File helper must outlive all Windows generations')
            return report,children,writer,failure

    def test_pipe_close_error_still_saves_confirmed_shutdown(self):
        report,children,_,failure=self.exercise(reasons=('guest-shutdown',),close_error=True)
        self.assertIsNone(failure)
        self.assertEqual(report['status'],'exited')
        self.assertEqual(report['exit_code'],0)
        self.assertEqual(len(children),1)

    def test_pipe_close_error_does_not_break_proven_reboot(self):
        report,children,_,failure=self.exercise(close_error=True)
        self.assertIsNone(failure)
        self.assertEqual(report['status'],'exited')
        self.assertEqual(report['generation'],2)
        self.assertEqual(len(children),2)

    def test_guest_reset_cold_relaunch_then_guest_shutdown_same_disk_and_session(self):
        report,children,writer,failure=self.exercise()
        self.assertIsNone(failure)
        self.assertEqual(len(children),2)
        self.assertEqual(children[0].command,children[1].command)
        self.assertEqual(report['generation'],2)
        self.assertEqual(report['session_nonce'],'a'*32)
        self.assertEqual(report['previous_generations'][0]['process'],children[0].identity)
        self.assertTrue(report['previous_generations'][0]['gone'])
        self.assertEqual(report['process'],children[1].identity)
        self.assertEqual(report['status'],'exited')
        writer.close.assert_called_once_with()
        for child in children:
            child.kill.assert_not_called()
            child.terminate.assert_not_called()

    def test_nonzero_unknown_or_no_guest_evidence_never_relaunches(self):
        for kwargs in ({'first_code':1},{'guest':False},{'reasons':('host-qmp-quit',)}):
            with self.subTest(kwargs=kwargs):
                report,children,_,_=self.exercise(**kwargs)
                self.assertEqual(len(children),1)
                self.assertNotIn('reset_proof',report)

    def test_stop_in_verified_gap_cancels_relaunch(self):
        report,children,_,failure=self.exercise(reasons=('guest-reset',),stop_gap=True)
        self.assertIsNone(failure)
        self.assertEqual(len(children),1)
        self.assertEqual(report['status'],'exited')
        self.assertTrue(report['stop_requested'])
        self.assertEqual(report['reset_proof']['reason'],'guest-reset')

    def test_file_network_is_unplugged_before_each_generation_runs(self):
        report,children,_,failure=self.exercise(files_network=True)
        self.assertIsNone(failure)
        self.assertEqual(len(children),2)
        for child in children:
            sent=[json.loads(line) for line in child.sent if line.strip()]
            names=[message['execute'] for message in sent]
            self.assertLess(names.index('set_link'),names.index('cont'),'Windows must not run before the file NIC is unplugged')
            self.assertEqual(sent[names.index('set_link')]['arguments'],{'name':'wcfilesnic','up':False})
        self.assertEqual(report['host_files']['link'],'down')
        self.assertEqual(report['host_files']['status'],'pending')

    def test_unplug_failure_is_reported_and_windows_still_starts(self):
        report,children,_,failure=self.exercise(reasons=('guest-shutdown',),files_network=True,link_error=True)
        self.assertIsNone(failure)
        self.assertEqual(report['host_files']['code'],'LINK_CONTROL_FAILED')
        self.assertIn('cont',[json.loads(line)['execute'] for line in children[0].sent if line.strip()])

    def test_without_file_network_no_link_command_is_sent(self):
        _,children,_,_=self.exercise(reasons=('guest-shutdown',))
        self.assertNotIn('set_link',[json.loads(line)['execute'] for line in children[0].sent if line.strip()])

    def test_static_nic_receipt_connects_link_then_mapping_receipt_marks_ready(self):
        import host_file_sharing
        tree=ast.parse((ROOT/'scripts/control_guest_install.py').read_text(encoding='utf-8'))
        function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='read_file_receipt')
        config={'schema':1,'session_nonce':'a'*32,'shares':[{'name':'PC_test','label':'ПК','preferred_letter':''}]}
        line=lambda code,success,mappings:json.dumps({'schema':1,'session_nonce':'a'*32,'success':success,'code':code,'mappings':mappings})+'\n'
        incoming=[line('NIC_STATIC',False,[]),line('OK',True,[{'name':'PC_test','letter':'Z'}])]
        calls=[]
        def command(name,parameters=None):
            calls.append((name,parameters))
            return incoming.pop(0) if name=='ringbuf-read' else {}
        for network in (True,False):
            with self.subTest(network=network):
                incoming[:]=[line('NIC_STATIC',False,[]),line('OK',True,[{'name':'PC_test','letter':'Z'}])];calls.clear()
                scope={'report':{'host_files':{'status':'pending','share_count':1}},'files_buffer':'','command':command,'save':mock.Mock(),
                       'file_sharing':host_file_sharing,'queue':queue,
                       'files_session':SimpleNamespace(healthy=lambda:True,medium=object(),network=network,config=config,public={'status':'pending'})}
                exec(compile(ast.Module(body=[function],type_ignores=[]),'read-file-receipt','exec'),scope)
                scope['read_file_receipt']()
                state=scope['report']['host_files']
                if network:
                    self.assertIn(('set_link',{'name':'wcfilesnic','up':True}),calls)
                    self.assertEqual((state['status'],state['link']),('connecting','up'))
                else:
                    self.assertNotIn('set_link',[name for name,_ in calls])
                    self.assertEqual(state['status'],'failed')
                scope['read_file_receipt']()
                self.assertEqual(scope['report']['host_files']['status'],'ready')

    def test_reboot_waits_for_same_gateway_outage_to_recover(self):
        report,children,_,failure=self.exercise(gateway_outage=True)
        self.assertIsNone(failure)
        self.assertEqual(len(children),2)
        self.assertEqual(report['generation'],2)


if __name__=='__main__':
    unittest.main()
