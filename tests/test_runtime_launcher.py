"""No guest is launched: all process discovery and controller starts are fakes."""
import contextlib
from datetime import datetime, timezone, timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'launch_runtime.py'
spec = importlib.util.spec_from_file_location('launch_runtime', SCRIPT)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeTests(unittest.TestCase):
    def work_session(self):
        record,state=self.guest('work',102)
        controller={'pid':901,'created_filetime':self.created,'image':str(self.root/'runtime/python-3.12.14/python.exe')}
        state.update(schema=1,process=record['process'],controller=controller,session_nonce='e'*32,generation=1,previous_generations=[])
        record.update(controller=controller,session_nonce='e'*32,generation=1)
        Path(record['state']).write_text(json.dumps(state))
        return record,state

    def test_verified_reboot_advances_owned_pid_then_normal_shutdown(self):
        record,state=self.work_session()
        old=record['process'].copy()
        new=dict(old,pid=103,created_filetime=old['created_filetime']+10000000)
        proof={'generation':1,'process':old,'exit_code':0,'reason':'guest-reset','gone':True}
        state.update(generation=2,pid=103,process=new,previous_generations=[proof],
            started_utc=datetime.fromtimestamp(new['created_filetime']/10000000-runtime.FILETIME_EPOCH+.2,timezone.utc).isoformat())
        Path(record['state']).write_text(json.dumps(state))
        self.identities={103:new,901:state['controller']}
        with mock.patch.object(runtime,'process_identity',side_effect=lambda pid:self.identities.get(pid)):
            self.assertTrue(runtime.record_live(self.root,record))
            self.assertEqual(record['process'],new)
            self.assertEqual(record['generation'],2)
            self.identities.pop(103)
            state.update(status='exited',exit_code=0)
            Path(record['state']).write_text(json.dumps(state))
            Path(record['state']).with_name('qmp-events.json').write_text(json.dumps([
                {'event':'SHUTDOWN','generation':2,'data':{'guest':True,'reason':'guest-shutdown'}}]))
            self.assertFalse(runtime.record_live(self.root,record))
            runtime.confirm_windows_shutdown(self.root,record)

    def test_reboot_requires_next_generation_reset_exit_zero_gone_and_same_controller(self):
        for change in ({'exit_code':1},{'reason':'guest-shutdown'},{'gone':False}):
            record,state=self.work_session()
            old=record['process'].copy()
            new=dict(old,pid=103,created_filetime=old['created_filetime']+10000000)
            proof={'generation':1,'process':old,'exit_code':0,'reason':'guest-reset','gone':True}|change
            state.update(generation=2,pid=103,process=new,previous_generations=[proof],
                started_utc=datetime.fromtimestamp(new['created_filetime']/10000000-runtime.FILETIME_EPOCH+.2,timezone.utc).isoformat())
            Path(record['state']).write_text(json.dumps(state))
            with mock.patch.object(runtime,'process_identity',side_effect=lambda pid:state['controller'] if pid==901 else new if pid==103 else None):
                with self.assertRaises(RuntimeError):
                    runtime.record_live(self.root,record)
            self.assertEqual(record['process'],old)

    def test_restarting_gap_is_owned_and_stop_marker_cancels_relaunch(self):
        record,state=self.work_session()
        proof={'generation':1,'process':record['process'],'exit_code':0,'reason':'guest-reset','gone':True}
        state.update(status='restarting',reset_proof=proof)
        Path(record['state']).write_text(json.dumps(state))
        self.identities={901:state['controller']}
        with mock.patch.object(runtime,'process_identity',side_effect=lambda pid:self.identities.get(pid)), \
             mock.patch.object(runtime,'engine_processes',return_value={}):
            self.assertTrue(runtime.record_live(self.root,record))
            self.assertEqual(runtime.discover_guests(self.root),{'work':record})
            self.assertTrue(runtime.request_powerdown(self.root,record))
            marker=runtime.read_json(Path(record['state']).with_name('stop-session.json'))
            self.assertEqual(marker['session_nonce'],state['session_nonce'])
            state.update(status='exited',exit_code=0,stop_requested=True)
            Path(record['state']).write_text(json.dumps(state))
            self.assertFalse(runtime.record_live(self.root,record))
            runtime.confirm_windows_shutdown(self.root,record)

    def test_lost_controller_does_not_adopt_a_replacement_pid(self):
        record,state=self.work_session()
        old=record['process'].copy()
        new=dict(old,pid=103,created_filetime=old['created_filetime']+10000000)
        state.update(generation=2,pid=103,process=new,previous_generations=[
            {'generation':1,'process':old,'exit_code':0,'reason':'guest-reset','gone':True}],
            started_utc=datetime.fromtimestamp(new['created_filetime']/10000000-runtime.FILETIME_EPOCH+.2,timezone.utc).isoformat())
        Path(record['state']).write_text(json.dumps(state))
        with mock.patch.object(runtime,'process_identity',side_effect=lambda pid:new if pid==103 else None):
            with self.assertRaises(RuntimeError):
                runtime.record_live(self.root,record)
        self.assertEqual(record['process'],old)

    def test_recycled_pid_without_new_generation_is_never_adopted(self):
        record,state=self.work_session()
        old=record['process'].copy()
        state['process']=dict(old,created_filetime=old['created_filetime']+10000000)
        state['started_utc']=datetime.fromtimestamp(state['process']['created_filetime']/10000000-runtime.FILETIME_EPOCH+.2,timezone.utc).isoformat()
        Path(record['state']).write_text(json.dumps(state))
        with mock.patch.object(runtime,'process_identity',return_value=state['process']):
            with self.assertRaisesRegex(RuntimeError,'without a verified reboot'):
                runtime.record_live(self.root,record)
        self.assertEqual(record['process'],old)

    def test_work_session_cannot_change_guest_or_network(self):
        for change in ({'guest':'work-install'},{'network':'none'},{'schema':True}):
            record,state=self.work_session()
            Path(record['state']).write_text(json.dumps(state|change))
            with mock.patch.object(runtime,'process_identity',return_value=record['process']):
                with self.assertRaises(RuntimeError):
                    runtime.record_live(self.root,record)

    def test_restarting_existing_session_blocks_second_windows_start(self):
        work,state=self.work_session()
        state.update(status='restarting',reset_proof={'generation':1,'process':work['process'],
            'exit_code':0,'reason':'guest-reset','gone':True})
        Path(work['state']).write_text(json.dumps(state))
        gateway,_=self.gateway2()
        with mock.patch.object(runtime,'discover_guests',return_value={'gateway':gateway,'work':work}), \
             mock.patch.object(runtime,'gateway_ready',return_value=True), \
             mock.patch.object(runtime,'controller_live',return_value=True), \
             mock.patch.object(runtime,'launch_guest') as launch:
            with self.assertRaises(RuntimeError):
                runtime.start_pair(self.root,{'guests':{}},lambda:None,lambda:False)
        launch.assert_not_called()

    def test_explicit_stop_can_reconcile_only_terminal_verified_reset_after_controller_exit(self):
        record,state=self.work_session()
        old=record['process'].copy()
        new=dict(old,pid=103,created_filetime=old['created_filetime']+10000000)
        state.update(generation=2,pid=103,process=new,status='exited',exit_code=0,
            error='Windows reboot safety limit reached; gateway retained',
            previous_generations=[{'generation':1,'process':old,'exit_code':0,'reason':'guest-reset','gone':True}],
            reset_proof={'generation':2,'process':new,'exit_code':0,'reason':'guest-reset','gone':True},
            started_utc=datetime.fromtimestamp(new['created_filetime']/10000000-runtime.FILETIME_EPOCH+.2,timezone.utc).isoformat())
        Path(record['state']).write_text(json.dumps(state))
        with mock.patch.object(runtime,'process_identity',return_value=None):
            with self.assertRaises(RuntimeError):
                runtime.refresh_work_record(self.root,record,state)
            runtime.refresh_work_record(self.root,record,state,allow_terminal_reset=True)
            runtime.confirm_windows_shutdown(self.root,record,allow_reset_stop=True)
        self.assertEqual(record['generation'],2)

    def setUp(self):
        base = Path(os.environ['TEMP'])
        self.temporary = tempfile.TemporaryDirectory(prefix='runtime launcher test ', dir=base)
        self.addCleanup(self.temporary.cleanup)
        def settle():
            for drain in runtime._CONTROLLER_DRAINS:
                drain.close()
            runtime._CONTROLLER_DRAINS.clear()
            for writer in runtime._CONTROLLER_WRITERS:
                writer.close()
            runtime._CONTROLLER_WRITERS.clear()
        self.addCleanup(settle)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(base.resolve()))
        self.engine = self.root / 'runtime/qemu-11.1.0/qemu-system-x86_64.exe'
        self.engine.parent.mkdir(parents=True)
        self.engine.touch()
        self.created = 134000000000000000
        self.identities = {}

    def guest(self, role, pid=101, **overrides):
        if role == 'gateway':
            return self.gateway2(pid, **overrides)
        guest = role + '-boot'
        directory = self.root / 'logs' / ('guest-' + guest + '-test-' + str(pid))
        directory.mkdir(parents=True, exist_ok=True)
        identity = {'pid': pid, 'image': str(self.engine),
                    'created_filetime': self.created + pid}
        created_seconds = identity['created_filetime'] / 10_000_000 - 11644473600
        state = {'guest': guest, 'pid': pid, 'status': 'running',
                 'network': runtime.NETWORKS[role],
                 'started_utc': datetime.fromtimestamp(created_seconds + .2, timezone.utc).isoformat(),
                 'arguments': ['-drive', 'if=ide,format=qcow2,file=' + runtime.DISKS[role]]}
        state.update(overrides)
        path = directory / 'state.json'
        path.write_text(json.dumps(state), encoding='utf-8')
        self.identities[pid] = identity
        return {'state': str(path), 'process': identity}, state

    def gateway2(self, pid=101, **overrides):
        nonce = 'a' * 32
        directory = self.root / 'logs' / ('guest-gateway-boot-' + nonce)
        directory.mkdir(parents=True, exist_ok=True)
        identity = {'pid': pid, 'image': str(self.engine), 'created_filetime': self.created + pid}
        controller = {'pid': 901, 'image': str(self.root / 'runtime/python-3.12.14/python.exe'), 'created_filetime': self.created}
        disk = (directory / 'gateway.qcow2').relative_to(self.root).as_posix()
        (directory / 'gateway.qcow2').write_bytes(b'fixture ephemeral disk')
        started = identity['created_filetime'] / 10_000_000 - runtime.FILETIME_EPOCH
        state = {'schema': 2, 'guest': 'gateway-boot', 'network': 'gateway', 'status': 'running',
                 'pid': pid, 'process': identity, 'controller': controller, 'nonce': nonce,
                 'profile_id': 'b'*32, 'profile_revision_id': 'c'*32, 'gateway_version': 'fixture1',
                 'disk': disk, 'test_only': False, 'network_verified': True,
                 'health_utc': datetime.now(timezone.utc).isoformat(),
                 'started_utc': datetime.fromtimestamp(started+.2, timezone.utc).isoformat(),
                 'arguments': ['-drive', 'if=none,id=wcroot,format=qcow2,file='+disk]}
        state.update(overrides)
        path = directory / 'state.json'
        path.write_text(json.dumps(state))
        self.identities[pid] = identity
        return {'state': str(path), 'process': identity, 'controller': controller, 'nonce': nonce}, state

    def test_gateway_command_uses_session_controller(self):
        command = runtime.controller_command(self.root, 'gateway')
        self.assertEqual(command[4:], [str(self.root/'scripts/gateway_controller.py'), '--project', str(self.root)])

    def test_legacy_gateway_cannot_be_adopted_as_ready(self):
        record, state = self.gateway2()
        state.pop('schema')
        Path(record['state']).write_text(json.dumps(state))
        with mock.patch.object(runtime, 'engine_processes', return_value=self.identities):
            with self.assertRaisesRegex(RuntimeError, 'Untracked'):
                runtime.discover_guests(self.root)

    def test_unverified_gateway_never_starts_windows(self):
        gateway, _ = self.gateway2(network_verified=False)
        with mock.patch.object(runtime, 'discover_guests', side_effect=[{}, {}]), \
             mock.patch.object(runtime, 'launch_guest', return_value=gateway) as launch, \
             mock.patch.object(runtime, 'gateway_ready', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'freshly verified'):
                runtime.start_pair(self.root, {'guests': {}}, lambda: None, lambda: False)
        self.assertEqual(launch.call_count, 1)

    def test_dead_controller_cannot_receive_shutdown(self):
        record, _ = self.gateway2()
        with mock.patch.object(runtime, 'process_identity', side_effect=lambda pid: self.identities.get(pid)):
            with self.assertRaisesRegex(RuntimeError, 'controller is unavailable'):
                runtime.request_powerdown(self.root, record)
        self.assertEqual(list(Path(record['state']).parent.glob('control-*.json')), [])

    def test_owned_failed_and_test_gateway_are_discoverable_stoppable_but_never_adopted(self):
        for change in ({'status':'connection-failed', 'network_verified':False}, {'test_only':True}):
            record, state = self.gateway2(**change)
            native = lambda pid: state['controller'] if pid == 901 else self.identities.get(pid)
            with mock.patch.object(runtime, 'engine_processes', return_value=self.identities), \
                 mock.patch.object(runtime, 'process_identity', side_effect=native):
                self.assertEqual(runtime.discover_guests(self.root), {'gateway':record})
                self.assertTrue(runtime.controller_live(self.root, record))
                self.assertFalse(runtime.gateway_ready(self.root, record))
                with self.assertRaisesRegex(RuntimeError, 'normal session'):
                    runtime.start_pair(self.root, {'guests':{}}, lambda:None, lambda:False)
                with mock.patch.object(runtime, 'require_runtime'), \
                     mock.patch.object(runtime, 'active_receipt', side_effect=FileNotFoundError()), \
                     mock.patch.object(runtime, 'wait_records', return_value=[]), \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(runtime.run_stop(self.root), 0)
                self.assertTrue(list(Path(record['state']).parent.glob('control-runtime-*.json')))

    def test_budget_protects_live_stream_and_only_prunes_explicit_exited_normal_session(self):
        from runtime_logs import MAX_LOG_BYTES, NORMAL_PAIR_RESERVE
        record, state = self.guest('work', 102)
        state.update(normal_runtime=True, process=record['process'], controller={
            'pid':901,'created_filetime':self.created,'image':str(self.root/'runtime/python-3.12.14/python.exe')})
        state_path = Path(record['state'])
        state_path.write_text(json.dumps(state))
        stream = state_path.with_name('stderr.txt')
        with stream.open('wb') as output:
            output.truncate(MAX_LOG_BYTES-NORMAL_PAIR_RESERVE+1)
        historical = self.root/'logs/guest-work-boot-historical123'
        historical.mkdir()
        sentinel = historical/'stderr.txt'
        sentinel.write_bytes(b'preserve historical evidence')
        with mock.patch.object(runtime, 'process_identity', return_value=record['process']):
            with self.assertRaisesRegex(RuntimeError, '50 MiB limit'):
                runtime.prepare_log_budget(self.root)
        self.assertTrue(stream.exists())
        state['status']='exited'
        state_path.write_text(json.dumps(state))
        with mock.patch.object(runtime, 'process_identity', return_value=None):
            budget=runtime.prepare_log_budget(self.root)
        self.assertTrue(budget['budget_met'])
        self.assertEqual(budget['reserved_bytes'], NORMAL_PAIR_RESERVE)
        self.assertFalse(stream.exists())
        self.assertEqual(sentinel.read_bytes(), b'preserve historical evidence')

    def test_gateway_readiness_requires_fresh_health_current_nonce_and_live_controller(self):
        record, state = self.gateway2()
        native = lambda pid: state['controller'] if pid == 901 else self.identities.get(pid)
        with mock.patch.object(runtime, 'process_identity', side_effect=native):
            self.assertTrue(runtime.gateway_ready(self.root, record))
            self.assertTrue(runtime.controller_live(self.root, record))
            for change in ({'network_verified': False}, {'nonce': 'd'*32},
                           {'health_utc': (datetime.now(timezone.utc)-timedelta(seconds=46)).isoformat()}):
                Path(record['state']).write_text(json.dumps(state | change))
                self.assertFalse(runtime.gateway_ready(self.root, record))
            Path(record['state']).write_text(json.dumps(state))
        with mock.patch.object(runtime, 'process_identity', side_effect=lambda pid: self.identities.get(pid)):
            self.assertFalse(runtime.gateway_ready(self.root, record))
            self.assertFalse(runtime.controller_live(self.root, record))

    def test_stop_crashed_windows_does_not_request_gateway_shutdown(self):
        gateway, _ = self.guest('gateway')
        work, state = self.guest('work', 102, status='exited', exit_code=1)
        with mock.patch.object(runtime, 'request_powerdown') as request, \
             mock.patch.object(runtime, 'wait_records', return_value=[]), \
             mock.patch.object(runtime, 'wait_exit_state', return_value=state):
            with self.assertRaises(RuntimeError):
                runtime.stop_records(self.root, {'work':[work], 'gateway':[gateway]})
        self.assertNotIn(mock.call(self.root, gateway), request.call_args_list)

    def test_stop_without_receipts_cannot_claim_success_for_untracked_qemu(self):
        with mock.patch.object(runtime, 'require_runtime'), \
             mock.patch.object(runtime, 'active_receipt', side_effect=FileNotFoundError()), \
             mock.patch.object(runtime, 'owned_records', return_value={'work':[],'gateway':[]}), \
             mock.patch.object(runtime, 'engine_processes', return_value={101:{'pid':101}}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'Untracked'):
                runtime.run_stop(self.root)

    def test_normal_runtime_does_not_require_administrator(self):
        with mock.patch.object(runtime.sys, 'executable', str(self.root/'runtime/python-3.12.14/python.exe')), \
             mock.patch.object(runtime.ctypes.windll.shell32, 'IsUserAnAdmin', return_value=0):
            runtime.require_runtime(self.root)

    def test_project_lock_rejects_reparse_file_without_touching_sentinel(self):
        state = self.root / 'state'
        state.mkdir()
        sentinel = self.root / 'sentinel'
        sentinel.write_bytes(b'preserve')
        (state/'runtime.lock').symlink_to(sentinel)
        with self.assertRaises(RuntimeError):
            with runtime.ProjectLock(self.root):
                pass
        self.assertEqual(sentinel.read_bytes(), b'preserve')

    def test_state_reader_and_writer_reject_reparse_parent(self):
        actual = self.root / 'actual'
        actual.mkdir()
        sentinel = actual / 'state.json'
        sentinel.write_text('{"preserve":true}')
        link = self.root / 'linked'
        link.symlink_to(actual, target_is_directory=True)
        for action in (lambda: runtime.read_json(link/'state.json'),
                       lambda: runtime.atomic_json(link/'state.json', {'changed': True})):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(json.loads(sentinel.read_text()), {'preserve':True})

    def test_plan_has_no_process_or_write_side_effects(self):
        before = set(self.root.rglob('*'))
        with mock.patch.object(runtime.subprocess, 'Popen', side_effect=AssertionError('spawn')), \
             mock.patch.object(runtime, 'process_identity', side_effect=AssertionError('process read')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runtime.main(['--project', str(self.root), '--plan']), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['start_order'], ['gateway', 'work'])
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_commands_use_only_bundled_python_and_fixed_controller_flags(self):
        command = runtime.controller_command(self.root, 'work')
        self.assertEqual(command, [str(self.root / 'runtime/python-3.12.14/python.exe'),
                                  '-I', '-S', '-B', str(self.root / 'scripts/control_guest_install.py'),
                                  '--project', str(self.root), '--guest', 'work-boot',
                                  '--network', 'isolated-lan'])

    def test_creation_time_and_image_are_both_required(self):
        record, state = self.guest('work')
        self.assertTrue(runtime.state_matches(self.root, Path(record['state']), state, record['process']))
        reused = dict(record['process'], created_filetime=self.created + 60 * 10_000_000)
        self.assertFalse(runtime.state_matches(self.root, Path(record['state']), state, reused))
        other = dict(record['process'], image=str(self.root / 'other/qemu-system-x86_64.exe'))
        self.assertFalse(runtime.state_matches(self.root, Path(record['state']), state, other))

    def test_diagnostic_usb_is_explicit_and_windows_only(self):
        normal = runtime.controller_command(self.root, 'work')
        diagnostic = runtime.controller_command(self.root, 'work', work_test_media=True)
        self.assertEqual(diagnostic, normal + ['--work-test-media'])
        self.assertEqual(runtime.controller_command(self.root, 'gateway', work_test_media=True),
                         runtime.controller_command(self.root, 'gateway'))

    def test_diagnostic_plan_preserves_fixed_isolated_network(self):
        before = set(self.root.rglob('*'))
        with mock.patch.object(runtime.subprocess, 'Popen', side_effect=AssertionError('spawn')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runtime.main(['--project', str(self.root), '--plan', '--work-test-media']), 0)
        plan = json.loads(output.getvalue())
        self.assertIn('--work-test-media', plan['commands']['work'])
        self.assertNotIn('--work-test-media', plan['commands']['gateway'])
        self.assertEqual(plan['commands']['work'][-3:], ['--network', 'isolated-lan', '--work-test-media'])
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_discovery_reuses_valid_guests(self):
        gateway, _ = self.guest('gateway')
        work, _ = self.guest('work', 102)
        with mock.patch.object(runtime, 'engine_processes', return_value=self.identities):
            self.assertEqual(runtime.discover_guests(self.root), {'gateway': gateway, 'work': work})

    def test_untracked_live_engine_blocks_new_disk_open(self):
        record, _ = self.guest('work')
        self.identities[101] = dict(record['process'], created_filetime=self.created + 600_000_000)
        with mock.patch.object(runtime, 'engine_processes', return_value=self.identities):
            with self.assertRaisesRegex(RuntimeError, 'Untracked'):
                runtime.discover_guests(self.root)

    def test_install_or_wrong_network_guest_is_not_adopted(self):
        self.guest('work', guest='work-install', network='none')
        with mock.patch.object(runtime, 'engine_processes', return_value=self.identities):
            with self.assertRaisesRegex(RuntimeError, 'normal boot'):
                runtime.discover_guests(self.root)

    def test_duplicate_live_disk_states_block_start(self):
        self.guest('work', 101)
        self.guest('work', 102)
        with mock.patch.object(runtime, 'engine_processes', return_value=self.identities):
            with self.assertRaisesRegex(RuntimeError, 'Multiple'):
                runtime.discover_guests(self.root)

    def test_gateway_starts_before_windows(self):
        gateway, _ = self.guest('gateway')
        work, _ = self.guest('work', 102)
        report = {'guests': {}}
        order = []
        deadlines = []
        def launch(root, role, receipt, save, cancelled, startup_deadline):
            order.append(role)
            deadlines.append(startup_deadline)
            return {'gateway': gateway, 'work': work}[role]
        with mock.patch.object(runtime, 'discover_guests', side_effect=[{}, {}, {'gateway': gateway}]), \
             mock.patch.object(runtime, 'launch_guest', side_effect=launch), \
             mock.patch.object(runtime, 'gateway_ready', return_value=True), \
             mock.patch.object(runtime, 'controller_live', return_value=True):
            runtime.start_pair(self.root, report, lambda: None, lambda: False)
        self.assertEqual(order, ['gateway', 'work'])
        self.assertEqual(deadlines[0], deadlines[1])
        self.assertEqual(report['guests'], {'gateway': gateway, 'work': work})

    def test_gateway_must_still_be_live_before_windows_starts(self):
        gateway, _ = self.guest('gateway')
        with mock.patch.object(runtime, 'discover_guests', return_value={}), \
             mock.patch.object(runtime, 'launch_guest', return_value=gateway) as launch:
            with self.assertRaisesRegex(RuntimeError, 'Gateway'):
                runtime.start_pair(self.root, {'guests': {}}, lambda: None, lambda: False)
        self.assertEqual(launch.call_count, 1)

    def test_adoption_does_not_spawn_duplicate_guests(self):
        gateway, _ = self.guest('gateway')
        work, _ = self.guest('work', 102)
        with mock.patch.object(runtime, 'discover_guests', return_value={'gateway': gateway, 'work': work}), \
             mock.patch.object(runtime, 'launch_guest', side_effect=AssertionError('duplicate spawn')), \
             mock.patch.object(runtime, 'gateway_ready', return_value=True), \
             mock.patch.object(runtime, 'controller_live', return_value=True):
            runtime.start_pair(self.root, {'guests': {}}, lambda: None, lambda: False)

    def test_windows_without_gateway_blocks_start(self):
        work, _ = self.guest('work')
        with mock.patch.object(runtime, 'discover_guests', return_value={'work': work}), \
             mock.patch.object(runtime, 'launch_guest', side_effect=AssertionError('spawn')):
            with self.assertRaisesRegex(RuntimeError, 'without'):
                runtime.start_pair(self.root, {'guests': {}}, lambda: None, lambda: False)

    def test_stop_during_start_does_not_start_windows(self):
        gateway, _ = self.guest('gateway')
        checks = iter([False, True])
        with mock.patch.object(runtime, 'discover_guests', return_value={}), \
             mock.patch.object(runtime, 'launch_guest', return_value=gateway) as launch:
            with self.assertRaisesRegex(RuntimeError, 'Stop requested'):
                runtime.start_pair(self.root, {'guests': {}}, lambda: None, lambda: next(checks))
        self.assertEqual(launch.call_count, 1)

    def test_powerdown_is_the_only_control_and_is_identity_bound(self):
        record, _ = self.guest('work')
        with mock.patch.object(runtime, 'process_identity', return_value=record['process']), \
             mock.patch.object(runtime, 'controller_live', return_value=True):
            self.assertTrue(runtime.request_powerdown(self.root, record))
        controls = list(Path(record['state']).parent.glob('control-*.json'))
        self.assertEqual(len(controls), 1)
        self.assertEqual(json.loads(controls[0].read_text(encoding='utf-8')), {'action': 'powerdown'})

    def test_stale_pid_is_never_sent_shutdown(self):
        record, _ = self.guest('work')
        other = dict(record['process'], created_filetime=self.created + 600_000_000)
        with mock.patch.object(runtime, 'process_identity', return_value=other):
            self.assertFalse(runtime.request_powerdown(self.root, record))
        self.assertEqual(list(Path(record['state']).parent.glob('control-*.json')), [])

    def test_state_outside_project_is_rejected(self):
        record, _ = self.guest('work')
        record['state'] = str(self.root.parent / 'unrelated/state.json')
        with self.assertRaisesRegex(RuntimeError, 'state path'):
            runtime.request_powerdown(self.root, record)

    def test_clean_windows_exit_requests_gateway_shutdown(self):
        gateway, _ = self.guest('gateway')
        work, state = self.guest('work', 102, status='exited', exit_code=0)
        Path(work['state']).with_name('qmp-events.jsonl').write_text(
            '{"event":"SHUTDOWN","data":{"reason":"guest-shutdown","guest":true}}\n', encoding='utf-8')
        with mock.patch.object(runtime, 'wait_exit_state', return_value=state), \
             mock.patch.object(runtime, 'request_powerdown', return_value=True) as request, \
             mock.patch.object(runtime, 'wait_records', return_value=[]):
            runtime.finish_windows(self.root, {'guests': {'work': work, 'gateway': gateway}})
        request.assert_called_once_with(self.root, gateway)

    def test_new_qmp_ring_confirms_shutdown_without_legacy_jsonl(self):
        work, state = self.guest('work', 102, status='exited', exit_code=0)
        Path(work['state']).with_name('qmp-events.json').write_text(json.dumps([
            {'event':'SHUTDOWN','data':{'reason':'guest-shutdown','guest':True}}]))
        with mock.patch.object(runtime, 'process_identity', return_value=None), \
             mock.patch.object(runtime, 'wait_exit_state', return_value=state):
            runtime.confirm_windows_shutdown(self.root, work)

    def test_existing_controller_log_is_refused_before_child_is_started(self):
        directory = self.root/'logs/runtime-launch-existing'
        directory.mkdir(parents=True)
        sentinel = directory/'gateway-controller.stderr.log'
        sentinel.write_bytes(b'preserve existing')
        with mock.patch.object(runtime.subprocess, 'Popen') as spawn:
            with self.assertRaises(FileExistsError):
                runtime.launch_guest(self.root, 'gateway', {'receipt':str(directory/'receipt.json')}, lambda:None, lambda:False)
        spawn.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b'preserve existing')

    def test_zero_exit_without_guest_shutdown_event_retains_gateway(self):
        gateway, _ = self.guest('gateway')
        work, state = self.guest('work', 102, status='exited', exit_code=0)
        for content in ('', '{"event":"SHUTDOWN","data":{"reason":"host-ui"}}\n'):
            Path(work['state']).with_name('qmp-events.jsonl').write_text(content, encoding='utf-8')
            with mock.patch.object(runtime, 'wait_exit_state', return_value=state), \
                 mock.patch.object(runtime, 'request_powerdown') as request:
                with self.assertRaisesRegex(RuntimeError, 'not confirmed'):
                    runtime.finish_windows(self.root, {'guests': {'work': work, 'gateway': gateway}})
            request.assert_not_called()

    def test_abnormal_windows_exit_retains_gateway(self):
        gateway, _ = self.guest('gateway')
        work, state = self.guest('work', 102, status='exited', exit_code=1)
        with mock.patch.object(runtime, 'wait_exit_state', return_value=state), \
             mock.patch.object(runtime, 'request_powerdown') as request:
            with self.assertRaisesRegex(RuntimeError, 'normally'):
                runtime.finish_windows(self.root, {'guests': {'work': work, 'gateway': gateway}})
        request.assert_not_called()

    def test_stop_waits_for_windows_before_gateway(self):
        gateway, _ = self.guest('gateway')
        work, _ = self.guest('work', 102)
        events = []
        with mock.patch.object(runtime, 'request_powerdown', side_effect=lambda root, rec: events.append(('request', rec['process']['pid']))), \
             mock.patch.object(runtime, 'wait_records', side_effect=lambda root, recs: events.append(('wait', recs[0]['process']['pid'])) or []), \
             mock.patch.object(runtime, 'confirm_windows_shutdown', side_effect=lambda root, rec, **kwargs: events.append(('proof', rec['process']['pid']))):
            runtime.stop_records(self.root, {'work': [work], 'gateway': [gateway]})
        self.assertEqual(events, [('request', 102), ('wait', 102), ('proof', 102), ('request', 101), ('wait', 101)])

    def test_windows_timeout_retains_both_and_does_not_request_gateway(self):
        gateway, _ = self.guest('gateway')
        work, _ = self.guest('work', 102)
        with mock.patch.object(runtime, 'request_powerdown') as request, \
             mock.patch.object(runtime, 'wait_records', return_value=[work]):
            with self.assertRaisesRegex(RuntimeError, 'retained'):
                runtime.stop_records(self.root, {'work': [work], 'gateway': [gateway]})
        request.assert_called_once_with(self.root, work)

    def test_gateway_timeout_retains_guest(self):
        gateway, _ = self.guest('gateway')
        work, state = self.guest('work', 102, status='exited', exit_code=0)
        Path(work['state']).with_name('qmp-events.jsonl').write_text(
            '{"event":"SHUTDOWN","data":{"reason":"guest-shutdown","guest":true}}\n', encoding='utf-8')
        with mock.patch.object(runtime, 'wait_exit_state', return_value=state), \
             mock.patch.object(runtime, 'request_powerdown'), \
             mock.patch.object(runtime, 'wait_records', return_value=[gateway]):
            with self.assertRaisesRegex(RuntimeError, 'retained'):
                runtime.finish_windows(self.root, {'guests': {'work': work, 'gateway': gateway}})

    def test_receipt_write_handles_spaces_and_unicode(self):
        path = self.root / 'logs/runtime-launch-Р В Р’В Р РЋРІР‚вЂќР В Р Р‹Р В РІР‚С™Р В Р’В Р РЋРІР‚СћР В Р’В Р вЂ™Р’В±Р В Р’В Р вЂ™Р’В°/receipt.json'
        path.parent.mkdir(parents=True)
        runtime.atomic_json(path, {'message': 'Р В Р’В Р Р†Р вЂљРЎС™Р В Р’В Р РЋРІР‚СћР В Р Р‹Р Р†Р вЂљРЎв„ўР В Р’В Р РЋРІР‚СћР В Р’В Р В РІР‚В Р В Р’В Р РЋРІР‚Сћ'})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'message': 'Р В Р’В Р Р†Р вЂљРЎС™Р В Р’В Р РЋРІР‚СћР В Р Р‹Р Р†Р вЂљРЎв„ўР В Р’В Р РЋРІР‚СћР В Р’В Р В РІР‚В Р В Р’В Р РЋРІР‚Сћ'})

    def test_receipt_write_retries_a_brief_windows_sharing_violation(self):
        path = self.root / 'receipt.json'
        original = runtime.os.replace
        calls = []
        def replace(source, destination):
            calls.append(destination)
            if len(calls) == 1:
                raise PermissionError('Reader temporarily holds the old receipt')
            return original(source, destination)
        with mock.patch.object(runtime.os, 'replace', side_effect=replace), \
             mock.patch.object(runtime.time, 'sleep'):
            runtime.atomic_json(path, {'phase': 'ready'})
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'phase': 'ready'})

    def test_repeat_click_waits_for_shared_startup_budget(self):
        path = self.root / 'logs/runtime-launch-active/receipt.json'
        report = {'phase': 'ready', 'guests': {'gateway': {}, 'work': {}}}
        with mock.patch.object(runtime.time, 'monotonic', side_effect=[0, 100]), \
             mock.patch.object(runtime, 'active_receipt', return_value=(path, report)), \
             mock.patch.object(runtime, 'record_live', return_value=True), \
             mock.patch.object(runtime, 'gateway_ready', return_value=True), \
             mock.patch.object(runtime, 'controller_live', return_value=True):
            self.assertEqual(runtime.join_existing(self.root), path)

    def test_marker_requires_actual_true_and_zero_bypass_packets(self):
        path = self.root / 'state/gateway-network-accepted.json'
        path.parent.mkdir()
        for value in ({'native_tests_passed': False, 'wan_bypass_packets': 0},
                      {'native_tests_passed': True, 'wan_bypass_packets': 1},
                      {'native_tests_passed': True, 'wan_bypass_packets': False}):
            path.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'acceptance'):
                runtime.check_acceptance(self.root)
        path.write_text('{"native_tests_passed":true,"wan_bypass_packets":0}', encoding='utf-8')
        runtime.check_acceptance(self.root)

    def test_stop_handles_receipt_before_supervisor_identity_is_written(self):
        path = self.root / 'logs/runtime-launch-test/receipt.json'
        path.parent.mkdir(parents=True)
        with mock.patch.object(runtime, 'require_runtime'), \
             mock.patch.object(runtime, 'active_receipt', return_value=(path, {'phase': 'starting'})), \
             mock.patch.object(runtime, 'read_json', return_value={'phase': 'error'}), \
             mock.patch.object(runtime, 'owned_records', return_value={}), \
             mock.patch.object(runtime, 'engine_processes', return_value={}), \
             mock.patch.object(runtime, 'process_identity', side_effect=AssertionError('invalid PID lookup')), \
             mock.patch.object(runtime, 'stop_records'), \
             mock.patch.object(runtime.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.run_stop(self.root), 0)

    def test_project_lock_excludes_a_second_launcher(self):
        with runtime.ProjectLock(self.root) as first:
            self.assertTrue(first)
            with runtime.ProjectLock(self.root) as second:
                self.assertFalse(second)
        with runtime.ProjectLock(self.root) as after_release:
            self.assertTrue(after_release)

    def test_unfinished_app_update_blocks_vm_start_even_from_console(self):
        (self.root / 'state').mkdir(exist_ok=True)
        (self.root / 'state/app-update.json').write_text('{}')
        receipt = self.root / 'logs/update-block/receipt.json'
        with mock.patch.object(runtime, 'require_runtime'), mock.patch.object(runtime, 'start_pair') as start:
            self.assertEqual(runtime.run_start(self.root, receipt), 1)
        start.assert_not_called()
        self.assertIn('application update', json.loads(receipt.read_text())['message'])

    def test_native_identity_for_this_test_process(self):
        identity = runtime.process_identity(os.getpid())
        self.assertEqual(identity['pid'], os.getpid())
        self.assertTrue(runtime.same_path(identity['image'], os.sys.executable))
        self.assertGreater(identity['created_filetime'], 0)

    def test_launch_guest_reads_controller_receipt_without_a_live_child(self):
        record, _ = self.guest('gateway')
        receipt = self.root / 'logs/runtime-launch-test/receipt.json'
        receipt.parent.mkdir(parents=True)
        report = {'receipt': str(receipt)}
        child = mock.Mock(pid=901)
        child.stdout = io.BytesIO((json.dumps({'state':record['state']})+'\n').encode())
        child.stderr = io.BytesIO()
        child.poll.return_value = None
        def spawn(command, **kwargs):
            return child
        with mock.patch.object(runtime.subprocess, 'Popen', side_effect=spawn) as start, \
             mock.patch.object(runtime, 'process_identity', side_effect=lambda pid: record['controller'] if pid == 901 else record['process']):
            actual = runtime.launch_guest(self.root, 'gateway', report, lambda: None, lambda: False)
        self.assertEqual(actual, record)
        self.assertEqual(start.call_args.args[0], runtime.controller_command(self.root, 'gateway'))
        self.assertEqual(start.call_args.kwargs['stdin'], runtime.subprocess.DEVNULL)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def cancel_launch(self, *, before=False, no_pid=False, foreign=False, missing=False,
                      pair=False, ready_race=False, early_exit=False):
        import runtime_logs
        record, state = self.gateway2(network_verified=False, status='running' if ready_race else 'starting')
        if no_pid:
            state.pop('pid')
            state.pop('process')
        if foreign:
            state['controller'] = dict(record['controller'], created_filetime=self.created + 1)
        runtime.atomic_json(Path(record['state']), state)
        directory = self.root / ('logs/runtime-launch-cancel-' + ('missing' if missing else 'state'))
        directory.mkdir()
        report = {'receipt': str(directory / 'receipt.json'), 'guests': {}}
        child = mock.Mock(pid=901)
        child.stdout = io.BytesIO(b'' if missing else (json.dumps({'state': record['state']})+'\n').encode())
        child.stderr = io.BytesIO()
        child.poll.return_value = None
        clock, spawned, saves, early, ready_cancel = [0.], [], [], [], [False]
        def drain(source, destination):
            destination.write(source.read())
            return mock.Mock()
        def sleep(seconds):
            clock[0] += seconds
            controls = list(Path(record['state']).parent.glob('control-runtime-*.json'))
            early.extend(controls)
            if no_pid and controls:
                if early_exit:
                    state.update(status='exited', exit_code=0)
                else:
                    state.update(pid=record['process']['pid'], process=record['process'])
                runtime.atomic_json(Path(record['state']), state)
        def ready(*unused):
            ready_cancel[0] = True
            return True
        def powerdown(root, owned):
            self.assertEqual(owned, record)
            self.assertEqual(saves[-1], {'gateway': record})
            return True
        with mock.patch.object(runtime.subprocess, 'Popen', side_effect=lambda *a, **k: spawned.append(child) or child), \
             mock.patch.object(runtime, 'process_identity', side_effect=lambda pid: record['controller'] if pid == 901 else record['process']), \
             mock.patch.object(runtime, 'gateway_ready', side_effect=ready if ready_race else lambda *a: False), \
             mock.patch.object(runtime.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(runtime.time, 'sleep', side_effect=sleep), \
             mock.patch.object(runtime_logs, 'start_capped_drain', side_effect=drain), \
             mock.patch.object(runtime, 'discover_guests', return_value={}), \
             mock.patch.object(runtime, 'request_powerdown', side_effect=powerdown) as request:
            with self.assertRaisesRegex(RuntimeError, 'Stop requested' if before or not (foreign or missing) else 'unknown'):
                save = lambda: saves.append(dict(report['guests']))
                cancel = lambda: before or (ready_cancel[0] if ready_race else bool(spawned))
                if pair:
                    runtime.start_pair(self.root, report, save, cancel)
                else:
                    runtime.launch_guest(self.root, 'gateway', report, save, cancel, startup_deadline=1.)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
        return report, spawned, request.call_count, early, clock[0]

    def test_launch_cancel_before_start_never_spawns_controller(self):
        report, spawned, requests, early, elapsed = self.cancel_launch(before=True)
        self.assertEqual((spawned, requests, early, elapsed), ([], 0, [], 0.))
        self.assertFalse(report['guests'])

    def test_launch_cancel_between_log_setup_and_popen_never_spawns(self):
        directory = self.root / 'logs/runtime-launch-race'
        directory.mkdir(parents=True)
        checks = iter((False, True))
        with mock.patch.object(runtime.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'Stop requested'):
                runtime.launch_guest(self.root, 'gateway', {'receipt': str(directory/'receipt.json')},
                                     lambda: None, lambda: next(checks))
        spawn.assert_not_called()

    def test_start_pair_cancel_inside_gateway_poll_never_starts_windows(self):
        report, spawned, requests, early, elapsed = self.cancel_launch(pair=True)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(requests, 1)
        self.assertEqual(set(report['guests']), {'gateway'})

    def test_launch_cancel_after_state_records_before_graceful_request_without_ready(self):
        report, spawned, requests, early, elapsed = self.cancel_launch()
        self.assertEqual(requests, 1)
        self.assertIn('gateway', report['guests'])
        self.assertLess(elapsed, .4)

    def test_cancelled_launch_record_remains_available_to_run_stop(self):
        report, spawned, requests, early, elapsed = self.cancel_launch()
        report['phase'] = 'error'
        record = report['guests']['gateway']
        records = {'work': [], 'gateway': [record]}
        with mock.patch.object(runtime, 'require_runtime'), \
             mock.patch.object(runtime, 'active_receipt', return_value=(Path(report['receipt']), report)), \
             mock.patch.object(runtime, 'recovery_snapshot', return_value={'available': False}), \
             mock.patch.object(runtime, 'owned_records', return_value=records), \
             mock.patch.object(runtime, 'discover_guests', return_value={}), \
             mock.patch.object(runtime, 'engine_processes', return_value={record['process']['pid']: record['process']}), \
             mock.patch.object(runtime, 'stop_records') as stop, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.run_stop(self.root), 0)
        stop.assert_called_once_with(self.root, records)

    def test_launch_cancel_racing_ready_check_still_records_and_requests_shutdown(self):
        report, spawned, requests, early, elapsed = self.cancel_launch(ready_race=True)
        self.assertEqual(requests, 1)
        self.assertIn('gateway', report['guests'])
        self.assertEqual(elapsed, 0.)

    def test_launch_cancel_before_pid_requests_owned_session_then_accounts_child(self):
        report, spawned, requests, early, elapsed = self.cancel_launch(no_pid=True)
        self.assertEqual(requests, 1)
        self.assertTrue(early)
        self.assertEqual(runtime.read_json(early[0]), {'action': 'powerdown'})
        self.assertIn('gateway', report['guests'])
        self.assertLess(elapsed, .6)

    def test_launch_cancel_before_pid_waits_for_controller_confirmed_exit(self):
        report, spawned, requests, early, elapsed = self.cancel_launch(no_pid=True, early_exit=True)
        self.assertEqual(requests, 0)
        self.assertTrue(early)
        self.assertFalse(report['guests'])
        self.assertGreaterEqual(elapsed, .2)
        self.assertLess(elapsed, .6)

    def test_launch_cancel_foreign_state_or_missing_state_remains_unknown_bounded(self):
        for foreign in (True, False):
            with self.subTest(foreign=foreign):
                # Each simulation uses fresh log sinks.
                if foreign:
                    report, spawned, requests, early, elapsed = self.cancel_launch(foreign=True)
                else:
                    report, spawned, requests, early, elapsed = self.cancel_launch(missing=True)
                self.assertFalse(report['guests'])
                self.assertEqual((requests, early), (0, []))
                self.assertGreaterEqual(elapsed, 1.)

    def test_stop_never_claims_stopped_while_unknown_startup_controller_is_alive(self):
        record, _ = self.gateway2()
        receipt = self.root/'logs/runtime-launch-cancel/receipt.json'
        report = {'phase': 'error', 'guests': {},
                  'controllers': {'gateway': {'pid': 901, 'identity': record['controller']}}}
        with mock.patch.object(runtime, 'require_runtime'), \
             mock.patch.object(runtime, 'active_receipt', return_value=(receipt, report)), \
             mock.patch.object(runtime, 'atomic_json'), \
             mock.patch.object(runtime, 'process_identity', return_value=record['controller']), \
             mock.patch.object(runtime, 'stop_records') as stop:
            with self.assertRaisesRegex(RuntimeError, 'unknown guest ownership'):
                runtime.run_stop(self.root)
        stop.assert_not_called()

    def test_reused_controller_pid_does_not_block_stopping_recorded_guests(self):
        record, _ = self.gateway2()
        saved = record['controller']
        foreign = dict(saved,created_filetime=saved['created_filetime']+1,image=str(self.root/'foreign.exe'))
        receipt = self.root/'logs/runtime-launch-cancel/receipt.json'
        report = {'phase':'error','guests':{},'controllers':{'gateway':{'pid':saved['pid'],'identity':saved}}}
        with mock.patch.object(runtime,'require_runtime'), \
                mock.patch.object(runtime,'active_receipt',return_value=(receipt,report)), \
                mock.patch.object(runtime,'atomic_json'), \
                mock.patch.object(runtime,'process_identity',return_value=foreign), \
                mock.patch.object(runtime,'recovery_snapshot',return_value={'available':False}), \
                mock.patch.object(runtime,'owned_records',return_value={'work':[],'gateway':[]}), \
                mock.patch.object(runtime,'discover_guests',return_value={}), \
                mock.patch.object(runtime,'engine_processes',return_value={}), \
                mock.patch.object(runtime,'stop_records') as stop, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runtime.run_stop(self.root),0)
        stop.assert_called_once()

    def test_missing_physical_network_reports_reason_before_guest_pid_exists(self):
        record, state = self.gateway2()
        state.pop('pid', None)
        state.update(status='exited', error='DIRECT_NETWORK_UNAVAILABLE')
        Path(record['state']).write_text(json.dumps(state))
        receipt = self.root / 'logs/runtime-launch-test/receipt.json'
        receipt.parent.mkdir(parents=True)
        child = mock.Mock(pid=901)
        child.stdout = io.BytesIO((json.dumps({'state': record['state']}) + '\n').encode())
        child.stderr = io.BytesIO()
        child.poll.return_value = None
        with mock.patch.object(runtime.subprocess, 'Popen', return_value=child), \
             mock.patch.object(runtime, 'process_identity', return_value=record['controller']):
            with self.assertRaisesRegex(RuntimeError, 'прямое подключение'):
                runtime.launch_guest(self.root, 'gateway', {'receipt': str(receipt)}, lambda: None, lambda: False)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_early_controller_failure_reports_logs_without_killing_it(self):
        receipt = self.root / 'logs/runtime-launch-test/receipt.json'
        receipt.parent.mkdir(parents=True)
        child = mock.Mock(pid=901)
        child.stdout, child.stderr = io.BytesIO(), io.BytesIO()
        child.poll.return_value = 1
        with mock.patch.object(runtime.subprocess, 'Popen', return_value=child), \
             mock.patch.object(runtime, 'process_identity', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'stderr.log'):
                runtime.launch_guest(self.root, 'gateway', {'receipt': str(receipt)}, lambda: None, lambda: False)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_stop_only_loads_receipts_for_this_project(self):
        record, _ = self.guest('work')
        path = self.root / 'logs/runtime-launch-test/receipt.json'
        path.parent.mkdir(parents=True)
        report = {'schema': 1, 'project': str(self.root / 'unrelated'), 'guests': {'work': record}}
        path.write_text(json.dumps(report), encoding='utf-8')
        self.assertEqual(runtime.owned_records(self.root), {'work': [], 'gateway': []})
        report['project'] = str(self.root)
        path.write_text(json.dumps(report), encoding='utf-8')
        with mock.patch.object(runtime, 'engine_processes', return_value=self.identities):
            self.assertEqual(runtime.owned_records(self.root), {'work': [record], 'gateway': []})

    def test_old_completed_pair_is_ignored_but_dead_windows_of_live_gateway_is_retained(self):
        gateway, _ = self.gateway2()
        work, _ = self.guest('work', 102, status='exited', exit_code=1)
        path = self.root / 'logs/runtime-launch-old/receipt.json'
        path.parent.mkdir()
        path.write_text(json.dumps({'schema': 1, 'project': str(self.root), 'guests': {'work': work, 'gateway': gateway}}))
        with mock.patch.object(runtime, 'engine_processes', return_value={}):
            self.assertEqual(runtime.owned_records(self.root), {'work': [], 'gateway': []})
        with mock.patch.object(runtime, 'engine_processes', return_value={101:gateway['process']}):
            self.assertEqual(runtime.owned_records(self.root), {'work':[work], 'gateway':[gateway]})

    def test_wrappers_use_normal_user_without_policy_changes_and_bundled_runtime(self):
        for filename in ('Start-Runtime.ps1', 'Stop-Runtime.ps1'):
            text = (SCRIPT.parent / filename).read_text(encoding='utf-8')
            self.assertNotIn('-Verb RunAs', text)
            self.assertIn('runtime\\python-3.12.14\\python.exe', text)
            self.assertIn("'-I', '-S', '-B'", text)
            for forbidden in ('-ExecutionPolicy', 'Set-ExecutionPolicy', 'Stop-Process', 'taskkill', 'Register-ScheduledTask'):
                self.assertNotIn(forbidden.casefold(), text.casefold())

    def test_powershell_native_quoting_round_trip(self):
        def literal(value):
            return "'" + value.replace("'", "''") + "'"
        expected = ['--project', 'E:\\Р В Р’В Р РЋРЎСџР В Р Р‹Р В РІР‚С™Р В Р’В Р РЋРІР‚СћР В Р’В Р вЂ™Р’ВµР В Р’В Р РЋРІР‚СњР В Р Р‹Р Р†Р вЂљРЎв„ўР В Р Р‹Р Р†Р вЂљРІвЂћвЂ“\\windows Claude', 'C:\\space path\\', 'say "hello"', '']
        output_path = self.root / 'argument-probe.json'
        code = 'import json,sys; print(json.dumps(sys.argv[1:]))'
        script = '''
            $tokens = $null; $parseErrors = $null
            $ast = [System.Management.Automation.Language.Parser]::ParseFile(WRAPPER, [ref]$tokens, [ref]$parseErrors)
            $function = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Quote-NativeArgument' }, $true)
            . ([scriptblock]::Create($function.Extent.Text))
            $values = ConvertFrom-Json VALUES
            $arguments = @('-I', '-S', '-B', '-c', (Quote-NativeArgument CODE))
            foreach ($value in $values) { $arguments += Quote-NativeArgument $value }
            $null = Start-Process -FilePath PYTHON -ArgumentList ($arguments -join ' ') -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput OUTPUT
            Get-Content -LiteralPath OUTPUT -Raw -Encoding UTF8
        '''
        for key, value in {'WRAPPER': str(SCRIPT.parent / 'Start-Runtime.ps1'),
                           'VALUES': json.dumps(expected), 'CODE': code,
                           'PYTHON': os.sys.executable, 'OUTPUT': str(output_path)}.items():
            script = script.replace(key, literal(value))
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = runtime.subprocess.run([str(powershell), '-NoProfile', '-Command', script],
                                        capture_output=True, text=True, encoding='utf-8', timeout=20,
                                        creationflags=runtime.subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)


if __name__ == '__main__':
    unittest.main()
