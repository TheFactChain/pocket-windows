import importlib.util
import io
import json
from pathlib import Path
import queue
from collections import deque
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/run_compact_vm.py'


class CompactVmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('compact_vm', SOURCE)
        cls.vm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.vm)

    def test_shutdown_requires_guest_event_and_exit_zero(self):
        event = {'event': 'SHUTDOWN', 'data': {'guest': True, 'reason': 'guest-shutdown'}}
        self.assertTrue(self.vm.shutdown_confirmed([event], 0))
        for events, code in (([], 0), ([event], 1), ([event], False),
                             ([{'event': 'SHUTDOWN', 'data': {'guest': False, 'reason': 'host-qmp-quit'}}], 0),
                             ([{'event': 'SHUTDOWN', 'data': {'guest': True, 'reason': 'guest-reset'}}], 0)):
            with self.subTest(events=events, code=code):
                self.assertFalse(self.vm.shutdown_confirmed(events, code))

    def test_headless_vm_has_no_network_or_shared_host_files(self):
        args = self.vm.arguments(Path('project'), Path('operation'), 'baseline')
        self.assertEqual(args[args.index('-nic') + 1], 'none')
        self.assertEqual(args[args.index('-display') + 1], 'none')
        self.assertEqual([args[i + 1] for i, name in enumerate(args) if name == '-global' and args[i + 1].startswith('VGA.')],
                         ['VGA.xres=1920', 'VGA.yres=1080', 'VGA.vgamem_mb=16'])
        self.assertEqual(args[args.index('-qmp') + 1], 'stdio')
        self.assertEqual(args[args.index('-m') + 1], '4096')
        self.assertIn('discard=unmap', ' '.join(args))
        self.assertIn('file=operation/baseline.qcow2', ' '.join(args))
        self.assertIn('serial=WC-MAINTENANCE-COPY', ' '.join(args))
        self.assertFalse(set(args) & {'-netdev', '-virtfs', '-fsdev', '-vnc', '-spice'})

    def test_configured_memory_and_cpus_match_normal_runtime(self):
        with mock.patch.object(self.vm, 'load_settings', return_value={'vcpus': 4, 'work_memory_mib': 6144}):
            args = self.vm.arguments(Path('project'), Path('operation'), 'verification')
        self.assertEqual(args[args.index('-m') + 1], '6144')
        self.assertEqual(args[args.index('-smp') + 1], '4')

    def test_recoverable_errors_leave_acpi_reachable_and_preserve_shutdown_proof(self):
        inbox, events = queue.Queue(), deque()
        inbox.put({'QMP': {}})
        state = {'nonce': 'a'*32}
        commands = []
        clock = [0]
        process = SimpleNamespace(code=None, queries=0)
        process.poll = lambda: process.code
        def write(line):
            value = json.loads(line)
            command = value['execute']
            commands.append(command)
            if command == 'query-status':
                process.queries += 1
            if command == 'screendump' or (command == 'query-status' and process.queries == 1):
                inbox.put({'id': value['id'], 'error': {'class': 'GenericError', 'desc': 'fixture refusal'}})
            else:
                inbox.put({'id': value['id'], 'return': {'status': 'running'}})
            if command == 'system_powerdown':
                events.append({'event': 'SHUTDOWN', 'data': {'guest': True, 'reason': 'guest-shutdown'}})
                process.code = 0
        process.stdin = SimpleNamespace(write=write, flush=lambda: None)
        save_failures = []
        def save():
            if state.get('capture_error') and not save_failures:
                save_failures.append(True)
                raise OSError('fixture journal unavailable')
        def exists(path):
            return path.name == 'powerdown.json' and process.queries >= 2
        with mock.patch.object(Path, 'exists', autospec=True, side_effect=exists), \
             mock.patch.object(self.vm, '_plain'), \
             mock.patch.object(self.vm, 'plain_path', side_effect=lambda path: path), \
             mock.patch.object(self.vm.runtime, 'read_json', side_effect=[{'nonce': 'wrong', 'action': 'powerdown'},
                                                                       {'nonce': 'a'*32, 'action': 'powerdown'}]), \
             mock.patch.object(self.vm.time, 'monotonic', side_effect=lambda: clock[0]), \
             mock.patch.object(self.vm.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0]+10)):
            self.vm.supervise(process, Path('fixture'), state, inbox, save)
        self.assertLess(commands.index('screendump'), commands.index('system_powerdown'))
        self.assertIn('query_error', state)
        self.assertIn('capture_error', state)
        self.assertIn('journal_error', state)
        self.assertIn('request_error', state)
        self.assertTrue(state['powerdown_requested'])
        self.assertTrue(self.vm.shutdown_confirmed(events, process.code))

    def test_true_qmp_eof_does_not_invent_shutdown_proof(self):
        inbox = queue.Queue()
        inbox.put({'QMP': {}})
        inbox.put({'eof': True})
        process = SimpleNamespace(poll=lambda: None,
                                  stdin=SimpleNamespace(write=lambda line: None, flush=lambda: None))
        state = {'nonce': 'a'*32}
        self.vm.supervise(process, Path('fixture'), state, inbox, lambda: None)
        self.assertIn('QmpDisconnected', state['control_error'])
        self.assertFalse(self.vm.shutdown_confirmed([], 0))

    def test_applied_capabilities_with_lost_reply_are_detected_by_status_probe(self):
        inbox = queue.Queue()
        inbox.put({'QMP': {}})
        state = {'nonce': 'a'*32}
        process = SimpleNamespace(code=None)
        process.poll = lambda: process.code
        commands = []
        def write(line):
            value = json.loads(line)
            name = value['execute']
            commands.append(name)
            if name == 'qmp_capabilities':
                if commands.count(name) > 1:
                    raise AssertionError('Controller repeated already applied capabilities instead of probing status')
                return  # Applied, but its reply missed the command deadline.
            inbox.put({'id': value['id'], 'return': {'status': 'running'}})
            if name == 'system_powerdown':
                process.code = 0
        process.stdin = SimpleNamespace(write=write, flush=lambda: None)
        original_get = inbox.get
        def get(timeout):
            if commands == ['qmp_capabilities']:
                raise queue.Empty()
            return original_get(timeout=timeout)
        with mock.patch.object(inbox, 'get', side_effect=get), \
             mock.patch.object(Path, 'exists', return_value=True), \
             mock.patch.object(self.vm, '_plain'), \
             mock.patch.object(self.vm, 'plain_path', side_effect=lambda path: path), \
             mock.patch.object(self.vm.runtime, 'read_json', return_value={'nonce': 'a'*32, 'action': 'powerdown'}):
            self.vm.supervise(process, Path('fixture'), state, inbox, lambda: None)
        self.assertEqual(commands, ['qmp_capabilities', 'query-status', 'system_powerdown'])
        self.assertTrue(state['powerdown_requested'])

    def test_preflight_journal_and_postspawn_failure_keep_protected_lifetime(self):
        nonce = 'b'*32
        operation = Path('compact-master-fixture') / ('automatic-maintenance-' + nonce)
        for failure in ('preflight', 'reader-start'):
            with self.subTest(failure=failure):
                process = mock.Mock(pid=123)
                process.wait.return_value = 0
                reader = mock.Mock(ident=None)
                reader.start.side_effect = RuntimeError('fixture reader unavailable')
                saved = []
                def save(path, state):
                    if failure == 'preflight':
                        raise OSError('fixture journal unavailable')
                    saved.append(json.loads(json.dumps(state)))
                with mock.patch.object(self.vm, 'plain_path', side_effect=lambda path, **kwargs: path), \
                     mock.patch.object(self.vm, '_pinned_directory', return_value=nullcontext()), \
                     mock.patch.object(self.vm.runtime, 'ProjectLock', return_value=nullcontext(True)), \
                     mock.patch.object(self.vm.runtime, 'read_json', return_value={'nonce': nonce}), \
                     mock.patch.object(self.vm.runtime, 'engine_path', return_value=Path('fake-engine')), \
                     mock.patch.object(Path, 'open', return_value=io.BytesIO(b'fixture')), \
                     mock.patch.object(Path, 'mkdir'), \
                     mock.patch.object(self.vm.hashlib, 'file_digest', return_value=SimpleNamespace(hexdigest=lambda: self.vm.ENGINE_SHA256)), \
                     mock.patch.object(self.vm.runtime, 'process_identity', return_value={'pid': 12}), \
                     mock.patch.object(self.vm.runtime, 'atomic_json', side_effect=save), \
                     mock.patch.object(self.vm.runtime, 'qemu_environment', return_value={}), \
                     mock.patch.object(self.vm, 'arguments', return_value=[]), \
                     mock.patch.object(self.vm, 'CappedWriter'), \
                     mock.patch.object(self.vm.threading, 'Thread', return_value=reader), \
                     mock.patch.object(self.vm.subprocess, 'Popen', return_value=process) as spawn, \
                     mock.patch('builtins.print'):
                    if failure == 'preflight':
                        with self.assertRaises(OSError):
                            self.vm.run(Path('project'), operation, 'maintenance')
                        spawn.assert_not_called()
                    else:
                        self.assertEqual(self.vm.run(Path('project'), operation, 'maintenance'), 2)
                        process.wait.assert_called_once_with()
                        self.assertEqual(saved[-1]['exit_code'], 0)
                        self.assertFalse(saved[-1]['guest_shutdown_confirmed'])
                        self.assertIn('RuntimeError', saved[-1]['control_error'])

    def test_control_request_is_bound_to_the_current_run(self):
        request = {'nonce': 'a'*32, 'action': 'powerdown'}
        self.assertTrue(self.vm.powerdown_request(request, 'a'*32))
        for value in (dict(request, nonce='b'*32), dict(request, action='quit'), dict(request, extra=1)):
            with self.assertRaises(ValueError):
                self.vm.powerdown_request(value, 'a'*32)


if __name__ == '__main__':
    unittest.main()
