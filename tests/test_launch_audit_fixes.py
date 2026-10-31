"""Stage 3 launch audit regressions. No VM, QEMU or Launcher is started."""
import ast
from collections import deque
import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime = load('launch_runtime')
controller = load('gateway_controller')
store = load('profile_store')
portable = load('portable_app')


class BrokenStream:
    """Yields lines, then fails the way a closed pipe does."""
    def __init__(self, lines):
        self.lines = list(lines)

    def __iter__(self):
        for line in self.lines:
            yield line
        raise OSError('pipe closed')


def drain(messages):
    result = []
    while True:
        try:
            result.append(messages.get_nowait())
        except queue.Empty:
            return result


class QmpReaderTests(unittest.TestCase):
    def guest_reader(self, stream, **scope):
        source = SCRIPTS / 'control_guest_install.py'
        function = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                        if isinstance(node, ast.FunctionDef) and node.name == 'read_qmp')
        inbox = queue.Queue()
        names = {'process': SimpleNamespace(stdout=stream), 'datetime': datetime, 'timezone': timezone, 'json': json,
                 'qmp_events': deque(maxlen=64), 'runtime': mock.Mock(), 'output': Path('fixture'), 'inbox': inbox,
                 'normal_runtime': False, 'work_reboot_session': False, 'clear_work_taskbar': mock.Mock()} | scope
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), names)
        return names, inbox

    def test_guest_reader_survives_failed_side_effects_and_always_queues_eof(self):
        lines = [json.dumps({'event': 'RESET', 'data': {}}), json.dumps({'event': 'SHUTDOWN', 'data': {}}),
                 json.dumps({'return': {}, 'id': 1})]
        api = mock.Mock()
        api.atomic_json.side_effect = OSError('events file locked')
        names, inbox = self.guest_reader(io.StringIO('\n'.join(lines)), runtime=api, normal_runtime=True,
                                         clear_work_taskbar=mock.Mock(side_effect=OSError('COM failure')))
        names['read_qmp']()
        self.assertEqual(drain(inbox), [{'return': {}, 'id': 1}, {'eof': True}])

    def test_guest_reader_queues_eof_when_the_pipe_itself_fails(self):
        names, inbox = self.guest_reader(BrokenStream([json.dumps({'return': {}, 'id': 1})]))
        with self.assertRaises(OSError):
            names['read_qmp']()
        self.assertEqual(drain(inbox), [{'return': {}, 'id': 1}, {'eof': True}])

    def gateway_qmp(self, stream):
        qmp = object.__new__(controller.Qmp)
        qmp.process, qmp.directory = SimpleNamespace(stdout=stream), Path('fixture')
        qmp.messages, qmp.events = queue.Queue(), []
        return qmp

    def test_gateway_reader_survives_failed_events_write_and_always_queues_eof(self):
        lines = [json.dumps({'event': 'SHUTDOWN', 'data': {'guest': True}}), json.dumps({'return': {}, 'id': 1})]
        qmp = self.gateway_qmp(io.StringIO('\n'.join(lines)))
        with mock.patch.object(controller.runtime, 'atomic_json', side_effect=OSError('events file locked')):
            qmp._read()
        self.assertEqual(drain(qmp.messages), [{'return': {}, 'id': 1}, {'eof': True}])

    def test_gateway_reader_queues_eof_when_the_pipe_itself_fails(self):
        qmp = self.gateway_qmp(BrokenStream([]))
        with self.assertRaises(OSError):
            qmp._read()
        self.assertEqual(drain(qmp.messages), [{'eof': True}])


@unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics')
class LockTests(unittest.TestCase):
    def test_wait_succeeds_when_the_lock_is_released_inside_the_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'gateway-controller.lock'
            held, release = threading.Event(), threading.Event()
            def holder():
                with store._exclusive_file(path):
                    held.set()
                    release.wait(5)
            thread = threading.Thread(target=holder)
            thread.start()
            try:
                self.assertTrue(held.wait(5))
                threading.Timer(.4, release.set).start()
                started = time.monotonic()
                with store._exclusive_file_wait(path, 5):
                    self.assertGreaterEqual(time.monotonic() - started, .3)
            finally:
                release.set()
                thread.join()

    def test_wait_still_reports_busy_when_the_lock_is_never_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'x.lock'
            with store._exclusive_file(path):
                with self.assertRaises(store.StoreBusy):
                    with store._exclusive_file_wait(path, .3):
                        pass

    def test_atomic_json_outwaits_a_reader_holding_the_target(self):
        from file_snapshot import locked_read
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.json'
            path.write_text('{"old": true}', encoding='utf-8')
            opened, release = threading.Event(), threading.Event()
            def reader():
                with locked_read(path):
                    opened.set()
                    release.wait(5)
            thread = threading.Thread(target=reader)
            thread.start()
            try:
                self.assertTrue(opened.wait(5))
                threading.Timer(.8, release.set).start()
                runtime.atomic_json(path, {'old': False})
            finally:
                release.set()
                thread.join()
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'old': False})


class SupervisorTests(unittest.TestCase):
    @contextlib.contextmanager
    def supervised(self, live):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('runtime/qemu-11.1.0/qemu-system-x86_64.exe', 'runtime/python-3.12.14/python.exe',
                         'scripts/control_guest_install.py', 'scripts/gateway_controller.py', 'images/workvm-empty.qcow2'):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(b'')
            (root / 'state').mkdir()
            def pair(root, report, save, cancelled):
                report['guests'] = {'work': {'state': 'work', 'process': {'created_filetime': 1}}, 'gateway': {'state': 'gateway'}}
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(runtime, 'require_runtime'))
                stack.enter_context(mock.patch.object(runtime, 'process_identity', return_value={'pid': 1}))
                stack.enter_context(mock.patch.object(runtime, 'atomic_json'))
                stack.enter_context(mock.patch.object(runtime, 'prepare_log_budget', return_value={}))
                stack.enter_context(mock.patch.object(runtime, 'start_pair', side_effect=pair))
                stack.enter_context(mock.patch.object(runtime, 'safe_state_path', side_effect=Path))
                stack.enter_context(mock.patch.object(runtime, 'read_json', return_value={'status': 'running', 'generation': 1}))
                stack.enter_context(mock.patch.object(runtime, 'record_live', side_effect=live))
                stack.enter_context(mock.patch.object(runtime, 'finish_windows'))
                stack.enter_context(mock.patch.object(runtime.time, 'sleep'))
                yield root

    def test_one_transient_read_error_does_not_end_supervision(self):
        calls = iter([OSError('state file busy'), True, True, False])
        def live(root, record):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value
        with self.supervised(live) as root:
            self.assertEqual(runtime.run_start(root, root / 'logs/runtime-launch-x/receipt.json'), 0)
            runtime.finish_windows.assert_called_once()

    def test_sustained_read_errors_still_end_supervision_with_an_error_receipt(self):
        with self.supervised(OSError('state file busy')) as root:
            self.assertEqual(runtime.run_start(root, root / 'logs/runtime-launch-x/receipt.json'), 1)
            runtime.finish_windows.assert_not_called()
            self.assertEqual(runtime.record_live.call_count, 20)


class StopAndJoinTests(unittest.TestCase):
    def test_gateway_stop_waits_for_its_controller_to_exit(self):
        controller_identity = {'pid': 7, 'created_filetime': 5, 'image': 'python.exe'}
        gateway = {'controller': controller_identity, 'nonce': 'n'}
        answers = iter([controller_identity, controller_identity, None])
        with mock.patch.object(runtime, 'process_identity', side_effect=lambda pid: next(answers)) as identity, \
                mock.patch.object(runtime.time, 'sleep'):
            runtime.wait_controllers([gateway])
        self.assertEqual(identity.call_count, 3)

    def test_wait_records_checks_controllers_only_once_nothing_is_live_and_skips_windows(self):
        gateway, work = {'controller': {'pid': 7}}, {'controller': {'pid': 8}, 'session_nonce': 's'}
        with mock.patch.object(runtime, 'record_live', return_value=False), \
                mock.patch.object(runtime, 'wait_controllers') as waited:
            self.assertEqual(runtime.wait_records(Path('x'), [gateway, work]), [])
        waited.assert_called_once_with([gateway])

    def test_stale_receipt_of_a_dead_supervisor_reports_busy_instead_of_its_old_message(self):
        report = {'phase': 'error', 'message': 'old session failed', 'supervisor': {'pid': 9, 'created_filetime': 1, 'image': 'p'}}
        clock = iter(range(0, 1000, 6))
        with mock.patch.object(runtime, 'active_receipt', return_value=(Path('receipt.json'), report)), \
                mock.patch.object(runtime, 'process_identity', return_value=None), \
                mock.patch.object(runtime.time, 'monotonic', side_effect=lambda: next(clock)), \
                mock.patch.object(runtime.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'try again'):
                runtime.join_existing(Path('x'))

    def test_timeouts_and_child_python_flags(self):
        self.assertEqual(runtime.SHUTDOWN_TIMEOUT, 300)
        self.assertEqual(controller.GATEWAY_SHUTDOWN_TIMEOUT, 120)
        for role in ('gateway', 'work'):
            command = runtime.controller_command(Path('fixture'), role)
            self.assertEqual(command[1:5], ['-X', 'utf8', '-I', '-S'])


class ConnectionErrorTests(unittest.TestCase):
    def startup(self, receipt):
        facts = {'windows': True, 'x64': True, 'whpx': True, 'ram_total_mib': 16384, 'ram_available_mib': 8192, 'admin': False}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(portable.runtime, 'active_receipt', return_value=(root / 'r.json', receipt)), \
                    mock.patch.object(portable.runtime, 'process_identity', return_value=None):
                return portable.preflight(root, host=facts)['startup']

    def test_recent_failed_launch_names_its_gateway_error(self):
        startup = self.startup({'phase': 'error', 'gateway_error': 'CONNECTION_FAILED',
                                'observed_utc': datetime.now(timezone.utc).isoformat(),
                                'supervisor': {'pid': 9, 'created_filetime': 1, 'image': 'p'}})
        self.assertEqual(startup['gateway_error'], 'CONNECTION_FAILED')
        self.assertEqual(startup['gateway_error_message'], portable.CONNECTION_ERRORS['CONNECTION_FAILED'])

    def test_old_failed_launch_is_not_reported(self):
        startup = self.startup({'phase': 'error', 'gateway_error': 'CONNECTION_FAILED',
                                'observed_utc': '2020-01-01T00:00:00+00:00'})
        self.assertNotIn('gateway_error', startup)


class SourceTests(unittest.TestCase):
    def test_host_files_bind_retries_reserved_port_ranges(self):
        # No C# harness here: the server needs the SMB libraries, so a source check guards the retry condition.
        source = (ROOT / 'host/files/HostFilesServer.cs').read_text(encoding='utf-8')
        self.assertIn('SocketError.AccessDenied', source)
        self.assertIn('attempt<32', source)


if __name__ == '__main__':
    unittest.main()
