"""No guest is launched: all process discovery and controller starts are fakes."""
import contextlib
from datetime import datetime, timezone
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
    def setUp(self):
        base = Path.home() / 'AppData/Local/Temp'
        self.temporary = tempfile.TemporaryDirectory(prefix='runtime launcher test ', dir=base)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(base.resolve()))
        self.engine = self.root / 'runtime/qemu-11.1.0/qemu-system-x86_64.exe'
        self.engine.parent.mkdir(parents=True)
        self.engine.touch()
        self.created = 134000000000000000
        self.identities = {}

    def guest(self, role, pid=101, **overrides):
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
        def launch(root, role, receipt, save, cancelled):
            order.append(role)
            return {'gateway': gateway, 'work': work}[role]
        with mock.patch.object(runtime, 'discover_guests', side_effect=[{}, {}, {'gateway': gateway}]), \
             mock.patch.object(runtime, 'launch_guest', side_effect=launch):
            runtime.start_pair(self.root, report, lambda: None, lambda: False)
        self.assertEqual(order, ['gateway', 'work'])
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
             mock.patch.object(runtime, 'launch_guest', side_effect=AssertionError('duplicate spawn')):
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
        with mock.patch.object(runtime, 'process_identity', return_value=record['process']):
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
            '{"event":"SHUTDOWN","data":{"reason":"guest-shutdown"}}\n', encoding='utf-8')
        with mock.patch.object(runtime, 'wait_exit_state', return_value=state), \
             mock.patch.object(runtime, 'request_powerdown', return_value=True) as request, \
             mock.patch.object(runtime, 'wait_records', return_value=[]):
            runtime.finish_windows(self.root, {'guests': {'work': work, 'gateway': gateway}})
        request.assert_called_once_with(self.root, gateway)

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
             mock.patch.object(runtime, 'wait_records', side_effect=lambda root, recs: events.append(('wait', recs[0]['process']['pid'])) or []):
            runtime.stop_records(self.root, {'work': [work], 'gateway': [gateway]})
        self.assertEqual(events, [('request', 102), ('wait', 102), ('request', 101), ('wait', 101)])

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
            '{"event":"SHUTDOWN","data":{"reason":"guest-shutdown"}}\n', encoding='utf-8')
        with mock.patch.object(runtime, 'wait_exit_state', return_value=state), \
             mock.patch.object(runtime, 'request_powerdown'), \
             mock.patch.object(runtime, 'wait_records', return_value=[gateway]):
            with self.assertRaisesRegex(RuntimeError, 'retained'):
                runtime.finish_windows(self.root, {'guests': {'work': work, 'gateway': gateway}})

    def test_receipt_write_handles_spaces_and_unicode(self):
        path = self.root / 'logs/runtime-launch-проба/receipt.json'
        path.parent.mkdir(parents=True)
        runtime.atomic_json(path, {'message': 'Готово'})
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'message': 'Готово'})

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

    def test_repeat_click_waits_for_both_controller_startup_budgets(self):
        path = self.root / 'logs/runtime-launch-active/receipt.json'
        report = {'phase': 'ready', 'guests': {'gateway': {}, 'work': {}}}
        with mock.patch.object(runtime.time, 'monotonic', side_effect=[0, 100]), \
             mock.patch.object(runtime, 'active_receipt', return_value=(path, report)), \
             mock.patch.object(runtime, 'record_live', return_value=True):
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
        child.poll.return_value = None
        def spawn(command, **kwargs):
            kwargs['stdout'].write(json.dumps({'state': record['state'], 'status': 'running'}) + '\n')
            kwargs['stdout'].flush()
            return child
        with mock.patch.object(runtime.subprocess, 'Popen', side_effect=spawn) as start, \
             mock.patch.object(runtime, 'process_identity', return_value=record['process']):
            actual = runtime.launch_guest(self.root, 'gateway', report, lambda: None, lambda: False)
        self.assertEqual(actual, record)
        self.assertEqual(start.call_args.args[0], runtime.controller_command(self.root, 'gateway'))
        self.assertEqual(start.call_args.kwargs['stdin'], runtime.subprocess.DEVNULL)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_early_controller_failure_reports_logs_without_killing_it(self):
        receipt = self.root / 'logs/runtime-launch-test/receipt.json'
        receipt.parent.mkdir(parents=True)
        child = mock.Mock(pid=901)
        child.poll.return_value = 1
        with mock.patch.object(runtime.subprocess, 'Popen', return_value=child):
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
        self.assertEqual(runtime.owned_records(self.root), {'work': [record], 'gateway': []})

    def test_wrappers_elevate_without_policy_changes_and_use_bundled_runtime(self):
        for filename in ('Start-Runtime.ps1', 'Stop-Runtime.ps1'):
            text = (SCRIPT.parent / filename).read_text(encoding='utf-8')
            self.assertIn('-Verb RunAs', text)
            self.assertIn('runtime\\python-3.12.14\\python.exe', text)
            self.assertIn("'-I', '-S', '-B'", text)
            for forbidden in ('-ExecutionPolicy', 'Set-ExecutionPolicy', 'Stop-Process', 'taskkill', 'Register-ScheduledTask'):
                self.assertNotIn(forbidden.casefold(), text.casefold())

    def test_powershell_native_quoting_round_trip(self):
        def literal(value):
            return "'" + value.replace("'", "''") + "'"
        expected = ['--project', 'E:\\Проекты\\windows Claude', 'C:\\space path\\', 'say "hello"', '']
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
