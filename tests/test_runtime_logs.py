import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import file_snapshot
import runtime_logs


def identity(path):
    info = path.stat()
    return {'device': info.st_dev, 'file_id': info.st_ino}


@unittest.skipUnless(os.name == 'nt', 'Native Windows exact-handle deletion')
class OwnedRemovalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='owned-removal-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'owned-overlay.qcow2'
        self.path.write_bytes(b'owned ephemeral file')

    def test_matching_identity_deletes_and_missing_returns_false(self):
        expected = identity(self.path)
        self.assertTrue(file_snapshot.remove_owned_file(self.path, expected))
        self.assertFalse(self.path.exists())
        self.assertFalse(file_snapshot.remove_owned_file(self.path, expected))

    def test_replaced_file_and_invalid_identity_preserve_sentinel(self):
        expected = identity(self.path)
        moved = self.root / 'retained-original'
        self.path.rename(moved)
        self.path.write_bytes(b'sentinel')
        for value in (expected, {'device': True, 'file_id': 1}, {'device': 0}, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError): file_snapshot.remove_owned_file(self.path, value)
        self.assertEqual(self.path.read_bytes(), b'sentinel')
        self.assertEqual(moved.read_bytes(), b'owned ephemeral file')

    def test_hardlink_and_directory_are_refused(self):
        second = self.root / 'sentinel-alias'
        os.link(self.path, second)
        with self.assertRaises(ValueError): file_snapshot.remove_owned_file(self.path, identity(self.path))
        self.assertTrue(second.exists())
        with self.assertRaises(ValueError): file_snapshot.remove_owned_file(self.root, identity(self.root))

    def test_active_snapshot_handle_prevents_delete(self):
        with file_snapshot.locked_read(self.path):
            with self.assertRaises(OSError): file_snapshot.remove_owned_file(self.path, identity(self.path))
        self.assertTrue(self.path.exists())

    def test_final_reparse_link_is_refused_and_target_is_preserved(self):
        target = self.root / 'sentinel'
        target.write_bytes(b'sentinel')
        link = self.root / 'symbolic-overlay.qcow2'
        try:
            link.symlink_to(target)
        except OSError as error:
            self.skipTest('Native symlink creation unavailable: ' + str(error.winerror))
        with self.assertRaises(ValueError): file_snapshot.remove_owned_file(link, identity(target))
        self.assertEqual(target.read_bytes(), b'sentinel')
        self.assertTrue(link.is_symlink())

    def test_native_handle_blocks_target_replacement_until_disposition(self):
        kernel = file_snapshot.ctypes.WinDLL('kernel32', use_last_error=True)
        sentinel = self.root / 'sentinel'
        sentinel.write_bytes(b'keep sentinel')
        blocked = []
        class Proxy:
            def __getattr__(self, name):
                return getattr(kernel, name)
        proxy = Proxy()
        def query(*args):
            actual = kernel.GetFileInformationByHandleEx
            actual.argtypes = query.argtypes
            actual.restype = query.restype
            result = actual(*args)
            try:
                os.replace(sentinel, self.path)
            except OSError:
                blocked.append(True)
            return result
        proxy.GetFileInformationByHandleEx = query
        with patch.object(file_snapshot.ctypes, 'WinDLL', return_value=proxy):
            self.assertTrue(file_snapshot.remove_owned_file(self.path, identity(self.path)))
        self.assertEqual(blocked, [True])
        self.assertFalse(self.path.exists())
        self.assertEqual(sentinel.read_bytes(), b'keep sentinel')


class RuntimeLogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='runtime-logs-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'logs').mkdir()
        self.pins = []

    @contextlib.contextmanager
    def pin(self, directory):
        self.pins.append(directory)
        yield

    def session(self, name='guest-gateway-boot-12345678', exited=True):
        directory = self.root / 'logs' / name
        directory.mkdir(exist_ok=True)
        return {'directory': name, 'normal': True, 'exited': exited,
                'processes': [{'pid': 123, 'created_filetime': 456, 'image': 'qemu-system-x86_64.exe'}]}

    def write(self, session, name, size=100):
        path = self.root / 'logs' / session['directory'] / name
        path.write_bytes(b'x' * size)
        return path

    def prune(self, sessions, gone=lambda _: True, **kwargs):
        return runtime_logs.prune_logs(self.root, sessions, process_gone=gone, pin_directory=self.pin, budget_bytes=100, **kwargs)

    def test_capped_drain_consumes_every_byte_but_writes_only_limit(self):
        source = io.BytesIO(b'abcd'*40000)
        path = self.root/'logs/capped.log'
        drain = runtime_logs.start_capped_drain(source, path, limit=1025)
        self.assertTrue(drain.join(timeout=5))
        self.assertEqual(drain.received_bytes, 160000)
        self.assertEqual(drain.written_bytes, 1025)
        self.assertEqual(path.read_bytes(), (b'abcd'*40000)[:1025])
        self.assertTrue(source.closed)
        self.assertFalse(drain.failed)

    def test_pipe_drain_publishes_short_state_before_child_eof(self):
        read_fd, write_fd = os.pipe()
        source = os.fdopen(read_fd, 'rb')
        path = self.root/'logs/live-pipe.log'
        drain = runtime_logs.start_capped_drain(source, path, limit=1024)
        try:
            os.write(write_fd, b'{"state":"fixture"}\n')
            deadline=time.monotonic()+3
            while path.stat().st_size == 0 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(path.read_bytes(), b'{"state":"fixture"}\n')
            self.assertFalse(drain.close(timeout=0))
        finally:
            os.close(write_fd)
            self.assertTrue(drain.close(timeout=3))

    def test_capped_writer_counts_dropped_bytes_and_never_overwrites(self):
        path = self.root/'logs/capped.log'
        with runtime_logs.CappedWriter(path, limit=7) as writer:
            writer.write(b'12345')
            writer.write(b'67890')
            self.assertEqual(writer.received_bytes, 10)
            self.assertEqual(writer.written_bytes, 7)
        self.assertEqual(path.read_bytes(), b'1234567')
        with self.assertRaises(FileExistsError):
            runtime_logs.CappedWriter(path)
        self.assertEqual(path.read_bytes(), b'1234567')

    def test_two_child_generations_share_one_cap_and_caller_owns_writer(self):
        path=self.root/'logs/session-stderr.log'
        with runtime_logs.CappedWriter(path,limit=9) as writer:
            first=runtime_logs.start_capped_drain(io.BytesIO(b'first123'),writer)
            self.assertTrue(first.join(timeout=3))
            second=runtime_logs.start_capped_drain(io.BytesIO(b'second456'),writer)
            self.assertTrue(second.join(timeout=3))
            self.assertFalse(second.failed)
            self.assertEqual(writer.received_bytes,17)
            self.assertEqual(writer.written_bytes,9)
        self.assertEqual(path.read_bytes(),b'first123s')

    def test_capped_drain_continues_after_writer_failure(self):
        source = io.BytesIO(b'x'*200000)
        path = self.root/'logs/capped.log'
        with patch.object(runtime_logs.CappedWriter, 'write', side_effect=OSError('fixture disk failure')):
            drain = runtime_logs.start_capped_drain(source, path, limit=1024)
            self.assertTrue(drain.close(timeout=5))
        self.assertEqual(drain.received_bytes, 200000)
        self.assertTrue(drain.failed)
        self.assertTrue(source.closed)

    @unittest.skipUnless(os.name == 'nt', 'Native guarded deletion')
    def test_only_allowlisted_exited_logs_are_pruned_and_history_disks_evidence_survive(self):
        session = self.session()
        ordinary = [self.write(session, name) for name in ('stderr.txt', 'runtime-serial.log')]
        protected = [self.write(session, name, 1) for name in ('state.json', 'receipt.json', 'gateway.qcow2',
                         'wan.pcap', 'qmp-events.jsonl', 'acceptance.json', 'stage-serial.log')]
        outside = self.root / 'logs' / 'baseline-overlay.qcow2'; outside.write_bytes(b'baseline')
        report = self.prune([session])
        self.assertEqual(sum(path.exists() for path in ordinary), 0)
        self.assertTrue(all(path.exists() for path in protected))
        self.assertEqual(outside.read_bytes(), b'baseline')
        self.assertTrue(report['budget_met'])
        self.assertEqual(report['remaining_bytes'], 2)  # state and QMP history count, though protected.
        self.assertTrue(self.pins)

    def test_protected_receipts_and_state_count_and_can_make_budget_unachievable(self):
        guest = self.session(); state = self.write(guest, 'state.json', 60)
        launch = self.session('runtime-launch-12345678'); receipt = self.write(launch, 'receipt.json', 60)
        report = self.prune([guest, launch])
        self.assertEqual(report['considered_bytes'], 120)
        self.assertEqual(report['protected_bytes'], 120)
        self.assertFalse(report['budget_met'])
        self.assertTrue(state.exists() and receipt.exists())

    @unittest.skipUnless(os.name == 'nt', 'Native guarded deletion')
    def test_controller_logs_prune_but_receipt_stop_and_qmp_remain(self):
        session = self.session('runtime-launch-12345678')
        ordinary = self.write(session, 'work-controller.stdout.log', 200)
        protected = self.write(session, 'stop-request.json', 1)
        report = self.prune([session])
        self.assertFalse(ordinary.exists())
        self.assertTrue(protected.exists())
        self.assertEqual(report['remaining_bytes'], 1)
        self.assertTrue(report['budget_met'])

    def test_live_unknown_or_incomplete_process_proof_is_protected(self):
        session = self.session(); path = self.write(session, 'stderr.txt', 200)
        for gone in (lambda _: False, lambda _: None, lambda _: (_ for _ in ()).throw(OSError('unknown'))):
            report = self.prune([session], gone)
            self.assertFalse(report['budget_met'])
            self.assertEqual(report['removed_files'], [])
            self.assertEqual(path.stat().st_size, 200)
        session['exited'] = False
        self.assertFalse(self.prune([session])['budget_met'])
        session['exited'] = True; session['processes'] = []
        self.assertFalse(self.prune([session])['budget_met'])

    def test_invalid_paths_build_sessions_and_duplicates_refuse_before_deletion(self):
        for name in ('../state', 'baseline-overlay', 'guest-work-install-12345678',
                     'guest-gateway-boot-1234/../outside', 'guest-gateway-boot-12345678:ads'):
            with self.subTest(name=name):
                session = {'directory': name, 'normal': True, 'exited': True, 'processes': []}
                with self.assertRaises(ValueError): self.prune([session])
        session = self.session(); self.write(session, 'stderr.txt', 200)
        with self.assertRaises(ValueError): self.prune([session, session])
        session['normal'] = False
        with self.assertRaises(ValueError): self.prune([session])

    def test_budget_cannot_exceed_fifty_mib_and_requires_pin(self):
        for budget in (True, -1, 50 * 1024 * 1024 + 1):
            with self.assertRaises(ValueError):
                runtime_logs.prune_logs(self.root, [], process_gone=lambda _: True,
                                        pin_directory=self.pin, budget_bytes=budget)
        with self.assertRaises(ValueError):
            runtime_logs.prune_logs(self.root, [], process_gone=lambda _: True, pin_directory=None)

    def test_replacement_between_scan_and_delete_is_retained(self):
        session = self.session(); path = self.write(session, 'stderr.txt', 200)
        original = file_snapshot.remove_owned_file
        def replace(target, expected):
            target.rename(target.with_name('preserved-original'))
            target.write_bytes(b'sentinel')
            return original(target, expected)
        with patch.object(runtime_logs, 'remove_owned_file', side_effect=replace):
            report = self.prune([session])
        self.assertEqual(path.read_bytes(), b'sentinel')
        self.assertFalse(report['budget_met'])
        self.assertTrue(report['failures'])


if __name__ == '__main__':
    unittest.main()
