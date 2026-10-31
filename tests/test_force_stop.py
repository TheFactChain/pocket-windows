from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import launch_runtime as runtime


class ForceStopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='force-stop-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'state').mkdir()
        folder = self.root / 'logs/guest-work-boot-fixture'
        folder.mkdir(parents=True)
        self.path = folder / 'state.json'
        self.process = {'pid':101, 'created_filetime':134000000000000101,
                        'image':str(runtime.engine_path(self.root))}
        self.controller = {'pid':201, 'created_filetime':134000000000000201,
                           'image':str(self.root/'runtime/python-3.12.14/python.exe')}
        self.state = {'schema':1, 'guest':'work-boot', 'network':'isolated-lan', 'status':'running',
                      'normal_runtime':True, 'pid':101, 'process':self.process, 'controller':self.controller,
                      'session_nonce':'a'*32, 'generation':1, 'previous_generations':[],
                      'started_utc':datetime.fromtimestamp(self.process['created_filetime']/10000000-runtime.FILETIME_EPOCH+.2, timezone.utc).isoformat(),
                      'arguments':['-drive','if=ide,format=qcow2,file=images/workvm-empty.qcow2'],
                      'stop_requested':True,
                      'acpi_powerdown_requested_utc':(datetime.now(timezone.utc)-timedelta(seconds=runtime.SHUTDOWN_TIMEOUT+10)).isoformat()}
        self.save()
        self.identities = {101:self.process, 201:self.controller}
        self.kernel = mock.Mock()
        self.kernel.OpenProcess.return_value = 123
        self.kernel.WaitForSingleObject.return_value = 0
        def terminate(handle, code):
            self.assertEqual(handle, 123)
            self.assertEqual(code, 1)
            marker = json.loads(self.path.with_name('stop-session.json').read_text())
            self.assertEqual(marker['session_nonce'], 'a'*32)
            self.identities.clear()
            return True
        self.kernel.TerminateProcess.side_effect = terminate
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(runtime, 'engine_processes', side_effect=lambda root: {k:v for k,v in self.identities.items() if k == 101}).start()
        mock.patch.object(runtime, 'process_identity', side_effect=lambda pid:self.identities.get(pid)).start()
        mock.patch.object(runtime, 'kernel_api', return_value=self.kernel).start()
        mock.patch.object(runtime, '_handle_identity', return_value=self.process).start()
        mock.patch.object(runtime, '_handle_arguments', return_value=[self.process['image'],*self.state['arguments']]).start()

    def save(self):
        self.path.write_text(json.dumps(self.state))

    def request(self):
        return {'confirmed':True, 'session':runtime.force_stop_offer(self.root)}

    def test_force_requires_expired_graceful_request_or_failed_control(self):
        self.assertIsNotNone(runtime.force_stop_offer(self.root))
        self.state['acpi_powerdown_requested_utc'] = datetime.now(timezone.utc).isoformat()
        self.save()
        self.assertIsNone(runtime.force_stop_offer(self.root))
        self.state.update(status='control-unavailable-guest-running', stop_requested=False)
        self.save()
        self.assertIsNotNone(runtime.force_stop_offer(self.root))

    def test_confirmation_is_required_and_no_process_is_opened(self):
        for request in ({}, {'confirmed':False}, {'confirmed':True, 'session':None}):
            with self.subTest(request=request), self.assertRaises((ValueError, RuntimeError)):
                runtime.force_stop(self.root, request)
        self.kernel.OpenProcess.assert_not_called()

    def test_force_stops_exact_handle_blocks_reboot_and_records_abnormal_exit(self):
        result = runtime.force_stop(self.root, self.request())
        self.assertTrue(result['ok'])
        self.assertEqual(result['code'], 'forced_stopped')
        self.assertTrue(result['forced'])
        self.assertEqual(json.loads(self.path.read_text())['status'], 'interrupted')
        receipt = json.loads(self.path.with_name('force-stop.json').read_text())
        self.assertEqual(receipt['phase'], 'terminated')
        self.assertEqual(receipt['session']['process'], self.process)
        self.assertEqual(self.kernel.TerminateProcess.call_count, 1)
        self.kernel.CloseHandle.assert_called_once_with(123)

    def test_stale_generation_or_pid_reuse_cannot_cut_power(self):
        request = self.request()
        for change in ({'generation':2}, {'session_nonce':'b'*32}):
            stale = {'confirmed':True, 'session':dict(request['session'], **change)}
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                runtime.force_stop(self.root, stale)
        with mock.patch.object(runtime, '_handle_identity', return_value=dict(self.process, created_filetime=1)):
            with self.assertRaises(RuntimeError): runtime.force_stop(self.root, request)
        self.kernel.TerminateProcess.assert_not_called()

    def test_untracked_process_and_changed_arguments_are_preserved(self):
        request = self.request()
        with mock.patch.object(runtime, 'engine_processes', return_value={101:self.process, 999:dict(self.process, pid=999)}):
            with self.assertRaises(RuntimeError): runtime.force_stop(self.root, request)
        with mock.patch.object(runtime, '_handle_arguments', return_value=[self.process['image'],'-drive','file=other.qcow2']):
            with self.assertRaises(RuntimeError): runtime.force_stop(self.root, request)
        self.kernel.TerminateProcess.assert_not_called()

    def test_native_denial_does_not_claim_shutdown(self):
        self.kernel.OpenProcess.return_value = 0
        with self.assertRaises(OSError): runtime.force_stop(self.root, self.request())
        self.assertIn(101, self.identities)
        self.kernel.TerminateProcess.assert_not_called()

    def test_force_waits_for_supervisor_to_release_operation_lock(self):
        request = self.request()
        lock = mock.MagicMock()
        lock.__enter__.side_effect = [False, False, True, True]
        with mock.patch.object(runtime, 'ProjectLock', return_value=lock), mock.patch.object(runtime.time, 'sleep'):
            result = runtime.force_stop(self.root, request)
        self.assertEqual(result['code'], 'forced_stopped')
        self.assertEqual(lock.__enter__.call_count, 4)


if __name__ == '__main__':
    unittest.main()
