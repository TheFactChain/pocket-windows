import base64
from contextlib import contextmanager
import importlib.util
import io
import queue
import json
import hashlib
import os
from contextlib import ExitStack
from pathlib import Path
import struct
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reference_vm', ROOT / 'scripts/reference_vm.py')
vm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm)


class ReferenceVmTests(unittest.TestCase):
    def setUp(self):
        self.ref = 'a' * 32
        self.root = Path(tempfile.gettempdir()) / 'reference-project'
        self.op = self.root / 'state' / ('reference-' + 'b' * 32)
        self.proc = {'pid': 123, 'image': 'qemu.exe', 'created_filetime': 456}

    def test_arguments_only_owned_overlay_visible_offline_readonly_handoff(self):
        args = vm.arguments(self.op, self.ref, {'vcpus': 2, 'work_memory_mib': 4096})
        self.assertEqual(args[args.index('-nic') + 1], 'none')
        self.assertEqual(args[args.index('-display') + 1], 'sdl,gl=off')
        self.assertEqual([args[i + 1] for i, name in enumerate(args) if name == '-global' and args[i + 1].startswith('VGA.')],
                         ['VGA.xres=1920', 'VGA.yres=1080', 'VGA.vgamem_mb=16'])
        self.assertIn('-S', args)
        self.assertIn('-no-reboot', args)
        self.assertTrue(any(value.startswith('type=1,') and value.endswith(',serial=WCREFERENCE-' + self.ref) for value in args))
        self.assertNotIn(vm.SOURCE, ' '.join(args))
        self.assertIn('ringbuf,id=wcrefreturn,size=16777216', args)
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == '-serial'], ['none', 'chardev:wcrefreturn'])
        drives = [args[i + 1] for i, a in enumerate(args) if a == '-drive']
        self.assertEqual(len(drives), 4)
        handoff = [d for d in drives if 'file=fat:' in d]
        self.assertEqual(len(handoff), 1)
        self.assertTrue(handoff[0].startswith('if=none,id=wcrefhand,format=raw,readonly=on,file=fat:state/reference-'))
        self.assertNotIn('fat:rw:', handoff[0])
        self.assertFalse(any(d.startswith('if=ide,') and 'readonly=on' in d for d in drives))
        devices = [args[i + 1] for i, a in enumerate(args) if a == '-device']
        self.assertIn('qemu-xhci,id=wcrefusb', devices)
        self.assertIn('usb-storage,drive=wcrefhand,bus=wcrefusb.0,removable=on', devices)
        self.assertFalse(set(args) & {'-net', '-netdev', '-virtfs', '-fsdev', '-spice', '-vnc'})
        for value in args:
            value.encode('ascii')

    def test_template_boot_uses_only_approved_template_media_and_nonce(self):
        args = vm.arguments(self.op, self.ref, {'vcpus': 2, 'work_memory_mib': 3072}, template_nonce='c' * 32)
        self.assertTrue(any(value.startswith('type=1,') and value.endswith(',serial=WCMASTER-' + 'c' * 32) for value in args))
        drives = [args[i + 1] for i, name in enumerate(args) if name == '-drive']
        self.assertEqual([d for d in drives if 'file=fat:' in d],
                         ['if=none,id=wcrefhand,format=raw,readonly=on,file=fat:state/reference-' + 'b' * 32 + '/template-handoff'])
        self.assertEqual(args[args.index('-nic') + 1], 'none')
        self.assertNotIn('-netdev', args)
        with self.assertRaises(ValueError):
            vm.arguments(self.op, self.ref, {'vcpus': 2, 'work_memory_mib': 3072, 'lan_port': 33861},
                         gateway=True, template_nonce='c' * 32)

    def test_template_frame_accepts_split_current_receipt_and_rejects_inventory_protocol(self):
        payload = b'{"code":0}'
        frame = vm.ReportFrame(self.ref, template=True)
        wire = b'boot\r\nWCMASTER ' + self.ref.encode() + b' 10\n' + payload
        for byte in wire[:-1]:
            self.assertFalse(frame.feed(bytes([byte])))
        self.assertTrue(frame.feed(wire[-1:]))
        self.assertEqual(bytes(frame.payload), payload)
        self.assertEqual(frame.preamble_bytes, 6)
        for bad in (b'WCMASTER ' + b'f' * 32 + b' 10\n' + payload,
                    b'WCMASTER ' + self.ref.encode() + b' 65537\n',
                    b'WCMASTER ' + self.ref.encode() + b' 10\n' + payload + b'extra',
                    b'x' * 4097):
            with self.subTest(bad=bad[:45]), self.assertRaises(ValueError):
                vm.ReportFrame(self.ref, template=True).feed(bad)
        offline = vm.ReportFrame(self.ref)
        self.assertFalse(offline.feed(b'WCMASTER ' + self.ref.encode() + b' 10\n' + payload))

    def test_template_receipt_keeps_strict_nonce_schema_and_failure_status(self):
        receipt = {'schema': 1, 'nonce': self.ref, 'phase': 'apps_verified', 'verdict': 'review_required',
                   'code': 0, 'cleanup_allowed': False, 'generalize_allowed': False,
                   'gates': {'pending_setup': 'clear', 'pending_servicing': 'clear', 'bitlocker': 'unknown', 'efs': 'unknown'},
                   'apps': {'claude': True, 'firefox': True}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'template-receipt-1.json'
            self.assertEqual(vm.receive_template_report(path, self.ref, json.dumps(receipt).encode()), receipt)
            self.assertEqual(json.loads(path.read_bytes()), receipt)
            for bad in (dict(receipt, nonce='f' * 32), dict(receipt, generalize_allowed=True),
                        dict(receipt, apps={'claude': True, 'firefox': False})):
                target = Path(directory) / ('invalid-' + str(len(list(Path(directory).iterdir()))) + '.json')
                with self.assertRaises(ValueError):
                    vm.receive_template_report(target, self.ref, json.dumps(bad).encode())
                self.assertFalse(target.exists())
            with self.assertRaises(FileExistsError):
                vm.receive_template_report(path, self.ref, json.dumps(receipt).encode())
            duplicate = json.dumps(receipt)[:-1] + ',"code":0}'
            with self.assertRaises(ValueError):
                vm.receive_template_report(Path(directory) / 'duplicate.json', self.ref, duplicate.encode())

    def test_template_completion_requires_this_boot_final_receipt(self):
        receipt = {'schema': 1, 'nonce': self.ref, 'phase': 'apps_verified', 'verdict': 'review_required',
                   'code': 0, 'cleanup_allowed': False, 'generalize_allowed': False,
                   'gates': {'pending_setup': 'clear', 'pending_servicing': 'clear', 'bitlocker': 'unknown', 'efs': 'unknown'},
                   'apps': {'claude': True, 'firefox': True}}
        state = {'template_received': True, 'template_phase': 'apps_verified', 'template_nonce': self.ref, 'generation': 2}
        with tempfile.TemporaryDirectory() as directory:
            operation = Path(directory)
            self.assertFalse(vm.template_completed(operation, state))
            vm.receive_template_report(operation / 'template-receipt-2.json', self.ref, json.dumps(receipt).encode())
            self.assertTrue(vm.template_completed(operation, state))
            for change in ({'template_received': False}, {'template_phase': 'audit_requested'},
                           {'template_nonce': 'f' * 32}, {'generation': 1}):
                self.assertFalse(vm.template_completed(operation, state | change))

    def test_template_handoff_rejects_mutation_and_unexpected_files(self):
        with tempfile.TemporaryDirectory() as directory:
            operation = Path(directory)
            handoff = operation / 'template-handoff'
            handoff.mkdir()
            names = ('Run-Prepare.cmd', 'Prepare-Template.ps1', 'README.md', 'Claude.msix', 'Firefox.msi')
            for name in names:
                (handoff / name).write_bytes(name.encode())
            (handoff / 'handoff.json').write_text(json.dumps({'nonce': self.ref, 'expected_smbios_serial': 'WCMASTER-' + self.ref}))
            frozen = {'schema': 1, 'operation_id': 'b' * 32, 'nonce': self.ref, 'files': {
                name: {'identity': vm.file_info(handoff / name), 'sha256': hashlib.sha256((handoff / name).read_bytes()).hexdigest()}
                for name in (*names, 'handoff.json')}}
            (operation / 'template-preparation.json').write_text(json.dumps(frozen))
            with ExitStack() as held:
                self.assertEqual(vm.pin_template(held, operation, 'b' * 32), self.ref)
            extra = handoff / 'extra.txt'
            extra.write_text('unapproved')
            with ExitStack() as held, self.assertRaises(ValueError):
                vm.pin_template(held, operation, 'b' * 32)
            extra.unlink()
            path = handoff / 'Claude.msix'
            before = path.stat()
            path.write_bytes(b'x' * before.st_size)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with ExitStack() as held, self.assertRaises(ValueError):
                vm.pin_template(held, operation, 'b' * 32)

    def test_gateway_arguments_use_only_existing_work_loopback_stream(self):
        settings = {'vcpus': 2, 'work_memory_mib': 4096, 'lan_port': 31861}
        args = vm.arguments(self.op, self.ref, settings, gateway=True)
        netdevs = [args[i + 1] for i, value in enumerate(args) if value == '-netdev']
        self.assertEqual(netdevs, ['stream,id=wclan,server=off,addr.type=inet,addr.host=127.0.0.1,addr.port=31861,addr.numeric=on,addr.ipv4=on,addr.ipv6=off,reconnect-ms=1000'])
        self.assertIn('e1000,id=wcwork,netdev=wclan,mac=52:54:00:77:00:03', args)
        self.assertEqual(args[args.index('-nic') + 1], 'none')
        for forbidden in ('user,', 'hostfwd', 'bridge', 'tap,'):
            self.assertNotIn(forbidden, ' '.join(args))

    def test_gateway_required_missing_or_other_guest_refuses(self):
        for guests in ({}, {'work': {}}, {'gateway': {}, 'work': {}}):
            with self.subTest(guests=guests), patch.object(vm.runtime, 'discover_guests', return_value=guests):
                with self.assertRaises(RuntimeError):
                    vm.require_gateway(self.root)

    def test_gateway_guard_rejects_unknown_or_changed_engine_process(self):
        record = {'process': self.proc, 'nonce': 'gateway-session'}
        for processes in ({}, {123: dict(self.proc, created_filetime=999)},
                          {123: self.proc, 777: dict(self.proc, pid=777)}):
            with self.subTest(processes=processes), \
                    patch.object(vm.runtime, 'engine_processes', return_value=processes), \
                    patch.object(vm.runtime, 'gateway_ready', return_value=True):
                with self.assertRaises(RuntimeError):
                    vm.require_gateway(self.root, record)

    def test_gateway_guard_accepts_only_original_gateway_plus_own_reference(self):
        record = {'process': self.proc, 'nonce': 'gateway-session'}
        reference = dict(self.proc, pid=987, created_filetime=654)
        with patch.object(vm.runtime, 'engine_processes', return_value={123: self.proc, 987: reference}), \
                patch.object(vm.runtime, 'gateway_ready', return_value=True) as ready:
            self.assertIs(vm.require_gateway(self.root, record, reference), record)
            ready.assert_called_once_with(self.root, record)

    def test_actual_gateway_readiness_rejects_stale_test_only_nonce_and_controller_loss(self):
        record = {'process': self.proc, 'nonce': 'gateway-session', 'state': 'fixture'}
        good = {'schema': 2, 'guest': 'gateway-boot', 'test_only': False, 'status': 'running',
                'network_verified': True, 'nonce': record['nonce'], 'health_utc': datetime.now(timezone.utc).isoformat()}
        changes = ({'test_only': True}, {'nonce': 'other-session'}, {'network_verified': False},
                   {'health_utc': (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()})
        for change in changes:
            with self.subTest(change=change), \
                    patch.object(vm.runtime, 'engine_processes', return_value={123: self.proc}), \
                    patch.object(vm.runtime, 'controller_live', return_value=True), \
                    patch.object(vm.runtime, 'safe_state_path', return_value=Path('fixture')), \
                    patch.object(vm.runtime, 'read_json', return_value=dict(good, **change)):
                with self.assertRaises(RuntimeError):
                    vm.require_gateway(self.root, record)
        with patch.object(vm.runtime, 'engine_processes', return_value={123: self.proc}), \
                patch.object(vm.runtime, 'controller_live', return_value=False):
            with self.assertRaises(RuntimeError):
                vm.require_gateway(self.root, record)

    def test_gateway_is_rechecked_before_cont_and_network_filters_need_not_be_empty(self):
        record = {'process': self.proc, 'nonce': 'gateway-session'}
        commands = []
        def command(name):
            commands.append(name)
            return [{'name': 'wcwork'}] if name == 'query-rx-filter' else None
        with patch.object(vm, 'require_gateway', side_effect=RuntimeError('Gateway lost')):
            with self.assertRaises(RuntimeError):
                vm.continue_guest(self.root, record, self.proc, command)
        self.assertNotIn('cont', commands)
        commands.clear()
        with patch.object(vm, 'require_gateway', return_value=record):
            vm.continue_guest(self.root, record, self.proc, command)
        self.assertEqual(commands, ['query-rx-filter', 'cont'])
        commands.clear()
        with self.assertRaises(RuntimeError):
            vm.continue_guest(self.root, None, self.proc, command)
        self.assertNotIn('cont', commands)

    def test_gateway_installation_wait_has_no_manual_inventory_timeout(self):
        self.assertTrue(vm.inventory_timed_out(1801, received=False, gateway=False))
        self.assertFalse(vm.inventory_timed_out(1800, received=False, gateway=False))
        self.assertFalse(vm.inventory_timed_out(1801, received=True, gateway=False))
        self.assertFalse(vm.inventory_timed_out(7200, received=False, gateway=True))

    def test_operation_path_rejects_escape_other_directory_and_arbitrary_names(self):
        self.assertEqual(vm.operation_path(self.root, self.op), self.op)
        self.assertEqual(vm.operation_path(self.root, self.op.relative_to(self.root)), self.op)
        for path in (self.root / 'reference-123', self.op / '..', self.root / 'state' / 'reference-ABC',
                     self.root / 'state' / '..' / self.op.name):
            with self.subTest(path=path), self.assertRaises(ValueError):
                vm.operation_path(self.root, path)

    def test_stop_rejects_unpinnable_operation_before_any_manifest_or_state_read(self):
        @contextmanager
        def reject_junction(path, create=False):
            self.assertEqual(path, self.op)
            self.assertFalse(create)
            raise ValueError('Operation junction rejected')
            yield  # Makes the guard fail on context entry, as the real pin does.

        with patch.object(vm, '_pinned_directory', reject_junction), \
                patch.object(vm, 'read_operation') as read_operation, \
                patch.object(vm.runtime, 'read_json') as read_json, \
                patch.object(vm.runtime, 'work_session_lock') as lifecycle, \
                patch.object(vm.runtime, 'atomic_json') as write:
            with self.assertRaisesRegex(ValueError, 'junction rejected'):
                vm.stop(self.root, self.op)
            read_operation.assert_not_called()
            read_json.assert_not_called()
            lifecycle.assert_not_called()
            write.assert_not_called()

    def test_source_guard_all_stat_identity_fields_are_required(self):
        opened = SimpleNamespace(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4, st_ctime_ns=5)
        expected = vm.identity(opened)
        vm.guard_source(expected, opened, opened)
        for field in vars(opened):
            changed = SimpleNamespace(**vars(opened))
            setattr(changed, field, 999)
            with self.subTest(field=field), self.assertRaises(ValueError):
                vm.guard_source(expected, opened, changed)

    def test_qcow_header_rejects_backing_encryption_dirty_corrupt_external_data(self):
        header = bytearray(104)
        header[:4] = b'QFI\xfb'
        struct.pack_into('>I', header, 4, 3)
        vm.clean_header(header)
        for offset, fmt, value in ((4, '>I', 1), (8, '>Q', 100), (16, '>I', 1),
                                   (32, '>I', 1), (72, '>Q', 1), (72, '>Q', 2), (72, '>Q', 4)):
            changed = bytearray(header)
            struct.pack_into(fmt, changed, offset, value)
            with self.subTest(offset=offset, value=value), self.assertRaises(ValueError):
                vm.clean_header(changed)
        with self.assertRaises(ValueError):
            vm.clean_header(b'QFI\xfb')

    def test_restart_requires_guest_reset_exit_zero_same_generation_and_native_gone(self):
        event = {'event': 'SHUTDOWN', 'generation': 1, 'data': {'guest': True, 'reason': 'guest-reset'}}
        self.assertTrue(vm.reset_proven([event], 1, 0, self.proc, None))
        for events, generation, code, native in (([], 1, 0, None), ([event], 2, 0, None),
             ([event], 1, 1, None), ([event], 1, False, None), ([event], 1, 0, self.proc),
             ([dict(event, data={'guest': False, 'reason': 'guest-reset'})], 1, 0, None),
             ([dict(event, data={'guest': True, 'reason': 'guest-shutdown'})], 1, 0, None)):
            self.assertFalse(vm.reset_proven(events, generation, code, self.proc, native))

    def test_qmp_reader_preserves_sanitized_terminal_event_then_signals_eof(self):
        events, inbox = [], queue.Queue()
        wire = json.dumps({'event': 'SHUTDOWN', 'data': {'guest': True, 'reason': 'guest-reset',
                         'SECRET': 'never persist'}, 'SECRET': 'never persist'}) + '\n'
        vm.read_qmp_stream(io.StringIO(wire), inbox, events, 1)
        self.assertEqual(inbox.get_nowait(), {'eof': True})
        self.assertNotIn('SECRET', json.dumps(events))
        self.assertEqual(vm.terminal_exit_kind(EOFError(), 'ringbuf-read', events, 1, 0,
                                             self.proc, None, False), 'reset')
        self.assertEqual(vm.terminal_exit_kind(queue.Empty(), 'ringbuf-read', events, 1, 0,
                                             self.proc, None, False), 'reset')

    def test_windows_generic_oserror_transport_reset_requires_all_existing_proofs(self):
        event = {'event': 'SHUTDOWN', 'generation': 2, 'data': {'guest': True, 'reason': 'guest-reset'}}
        error = OSError('Windows pipe closed')
        self.assertEqual(vm.terminal_exit_kind(error, 'ringbuf-read', [event], 2, 0,
                                             self.proc, None, False), 'reset')
        for stage, events, code, native, earlier in (
            ('ringbuf-read', [], 0, None, False),
            ('ringbuf-read', [event], None, self.proc, False),
            ('ringbuf-read', [event], 0, self.proc, False),
            ('ringbuf-read', [event], 0, None, True),
            ('ringbuf-read', [event], 1, None, False),
            ('report-validation', [event], 0, None, False),
            ('state-save', [event], 0, None, False),
            ('frame-parse', [event], 0, None, False)):
            with self.subTest(stage=stage, code=code, earlier=earlier):
                self.assertIsNone(vm.terminal_exit_kind(error, stage, events, 2, code,
                                                       self.proc, native, earlier))

    def test_terminal_qmp_transport_shutdown_is_clean_stop(self):
        events = [{'event': 'SHUTDOWN', 'generation': 2,
                   'data': {'guest': True, 'reason': 'guest-shutdown'}}]
        self.assertEqual(vm.terminal_exit_kind(BrokenPipeError(), 'powerdown', events, 2, 0,
                                             self.proc, None, False), 'shutdown')

    def test_terminal_transport_never_hides_prior_failure_unproven_exit_or_qmp_semantic_error(self):
        event = {'event': 'SHUTDOWN', 'generation': 1, 'data': {'guest': True, 'reason': 'guest-reset'}}
        for error, stage, events, generation, code, native, earlier in (
            (EOFError(), 'ringbuf-read', [], 1, 0, None, False),
            (EOFError(), 'ringbuf-read', [event], 2, 0, None, False),
            (EOFError(), 'ringbuf-read', [event], 1, 1, None, False),
            (EOFError(), 'ringbuf-read', [event], 1, None, self.proc, False),
            (EOFError(), 'ringbuf-read', [event], 1, 0, self.proc, False),
            (EOFError(), 'ringbuf-read', [event], 1, 0, None, True),
            (ValueError('bad frame'), 'frame-parse', [event], 1, 0, None, False),
            (RuntimeError('QMP semantic error'), 'ringbuf-read', [event], 1, 0, None, False),
            (EOFError(), 'report-validation', [event], 1, 0, None, False)):
            with self.subTest(error=type(error).__name__, stage=stage, code=code, earlier=earlier):
                self.assertIsNone(vm.terminal_exit_kind(error, stage, events, generation, code,
                                                       self.proc, native, earlier))

    def test_control_requires_exact_session_controller_and_powerdown_only(self):
        state = {'operation_id': 'b' * 32, 'session_nonce': 'c' * 32, 'controller': self.proc}
        marker = dict(state, action='powerdown')
        self.assertTrue(vm.valid_control(marker, state))
        for key, value in (('action', 'quit'), ('operation_id', 'd' * 32), ('session_nonce', 'd' * 32),
                           ('controller', dict(self.proc, created_filetime=999)), ('extra', True)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                vm.valid_control(dict(marker, **{key: value}), state)

    def test_failure_diagnostic_records_first_stage_type_command_class_without_raw_details(self):
        state = {}
        vm.record_failure(state, ValueError('SECRET inventory'), 'frame-parse', 'ringbuf-read', 'GenericError')
        self.assertEqual(state['failure_diagnostic'], {'stage': 'frame-parse', 'exception_type': 'ValueError',
                         'qmp_command': 'ringbuf-read', 'qmp_error_class': 'GenericError'})
        vm.record_failure(state, OSError('SECOND SECRET'), 'state-save', 'cont', None)
        self.assertEqual(state['failure_diagnostic']['stage'], 'frame-parse')
        unknown = {}
        vm.record_failure(unknown, RuntimeError('SECRET'), 'SECRET', 'SECRET', 'SECRET')
        self.assertNotIn('SECRET', json.dumps(unknown))

    def test_empty_ringbuf_read_is_valid_idle_not_an_inventory_or_failure(self):
        frame = vm.ReportFrame(self.ref)
        for _ in range(3):
            self.assertFalse(frame.feed(base64.b64decode('', validate=True)))
        self.assertFalse(frame.complete)
        self.assertFalse(frame.failed)
        self.assertEqual(frame.header, b'')
        self.assertEqual(frame.payload, b'')

    def test_framing_chunk_boundaries_exact_length_and_one_frame(self):
        frame = vm.ReportFrame(self.ref)
        wire = ('WCREFERENCE ' + self.ref + ' 4\n').encode() + b'{}\r\n'
        for byte in wire[:-1]:
            self.assertFalse(frame.feed(bytes([byte])))
        self.assertTrue(frame.feed(wire[-1:]))
        self.assertEqual(frame.payload, b'{}\r\n')
        self.assertTrue(frame.feed(b''))
        with self.assertRaises(ValueError):
            frame.feed(b'X')
        self.assertTrue(frame.failed)
        self.assertEqual(frame.payload, b'')

    def test_framing_rejects_mismatch_oversize_bad_header_and_extra_bytes_without_echo(self):
        for raw in (b'WCREFERENCE ' + b'd' * 32 + b' 2\n{}',
                    ('WCREFERENCE ' + self.ref + ' 8388609\n').encode(),
                    b'SECRET' * 1000, ('WCREFERENCE ' + self.ref + ' 2\n{}extra').encode(),
                    ('WCREFERENCE ' + self.ref + ' 0\n').encode()):
            frame = vm.ReportFrame(self.ref)
            with self.subTest(raw=raw[:10]), self.assertRaises(ValueError) as error:
                frame.feed(raw)
            self.assertNotIn('SECRET', str(error.exception))
            self.assertLessEqual(len(frame.payload), vm.reference_prep.MAX_REPORT_BYTES)
            self.assertFalse(frame.feed(b'discard after failure'))

    def test_startup_noise_syncs_across_chunks_without_retaining_preamble(self):
        preamble = b'boot\x00port\r\nWWCnoheader\rnoise-without-newline'
        wire = preamble + ('WCREFERENCE ' + self.ref + ' 2\n{}').encode()
        for width in (1, 2, 7, len(wire)):
            frame = vm.ReportFrame(self.ref)
            for index in range(0, len(wire), width):
                frame.feed(wire[index:index + width])
            self.assertTrue(frame.complete)
            self.assertEqual(frame.payload, b'{}')
            self.assertEqual(frame.preamble_bytes, len(preamble))
            self.assertNotIn(b'boot', frame.header)

    def test_startup_noise_is_bounded_and_discarded_not_accumulated(self):
        frame = vm.ReportFrame(self.ref)
        self.assertFalse(frame.feed(b'x' * 4096))
        self.assertEqual(frame.preamble_bytes, 4096)
        self.assertEqual(frame.header, b'')
        self.assertEqual(frame.payload, b'')
        with self.assertRaises(ValueError):
            frame.feed(b'x')
        self.assertTrue(frame.failed)

    def test_large_chunk_with_header_within_noise_cap_does_not_count_payload_as_noise(self):
        frame = vm.ReportFrame(self.ref)
        payload = b'x' * 100000
        wire = b'n' * 4096 + ('WCREFERENCE ' + self.ref + ' 100000\n').encode() + payload
        self.assertTrue(frame.feed(wire))
        self.assertEqual(frame.preamble_bytes, 4096)
        self.assertEqual(frame.payload, payload)

    def test_noise_cannot_hide_stale_or_malformed_header_or_a_second_frame(self):
        header = ('WCREFERENCE ' + self.ref + ' 2\n').encode()
        for wire in (b'noise\nWCREFERENCE ' + b'd' * 32 + b' 2\n{}' + header + b'{}',
                     b'noiseWCREFERENCE broken\n' + header + b'{}',
                     b'noiseWCREFERENCE' + b'x' * 100,
                     b'noise' + header + b'{}' + header + b'{}',
                     b'noise' + header + b'{}extra'):
            frame = vm.ReportFrame(self.ref)
            with self.subTest(wire=wire[:10]), self.assertRaises(ValueError):
                frame.feed(wire)
            self.assertTrue(frame.failed)
            self.assertEqual(frame.payload, b'')

    def test_report_is_validated_with_expected_id_before_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reference-report.json'
            with patch.object(vm.reference_prep, 'read_report') as read:
                vm.receive_report(path, self.ref, b'{}')
                read.assert_called_once_with(path.with_name('report-pending.json'), expected_reference_id=self.ref)
            self.assertEqual(path.read_bytes(), b'{}')

    def test_empty_or_oversized_report_never_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reference-report.json'
            for payload in (b'', b'x' * (vm.reference_prep.MAX_REPORT_BYTES + 1)):
                with self.assertRaises(ValueError):
                    vm.receive_report(path, self.ref, payload)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_report_never_promoted_or_echoed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reference-report.json'
            with patch.object(vm.reference_prep, 'read_report', side_effect=ValueError('Invalid inventory')):
                with self.assertRaises(ValueError):
                    vm.receive_report(path, self.ref, b'{}')
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
