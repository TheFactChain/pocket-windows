"""State and failure tests without using the user's clipboard or network."""
from pathlib import Path
import base64
import ctypes
import json
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import clipboard_bridge as clipboard

NONCE = 'c' * 32


class Clipboard:
    def __init__(self):
        self.written = []
        self.before_write = lambda: None
        self.succeeds = True

    def set_text(self, text, guard):
        self.before_write()
        if not self.succeeds or not guard():
            return False
        self.written.append(text)
        return True


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.clipboard = Clipboard()
        self.bridge = clipboard.Bridge(12345, NONCE, self.clipboard)

    def receive(self, text, sequence):
        return json.loads(self.bridge.receive(clipboard.encode(NONCE, text, sequence, 'guest')))

    def test_a_b_a_from_guest_is_not_suppressed_after_local_copy(self):
        self.assertTrue(self.receive('A', 1)['ok'])
        self.bridge.local_change('B')
        self.assertTrue(self.receive('A', 3)['ok'])
        self.assertEqual(self.clipboard.written, ['A', 'A'])

    def test_failed_apply_is_retried_until_success(self):
        self.clipboard.succeeds = False
        self.assertFalse(self.receive('A', 1)['ok'])
        self.clipboard.succeeds = True
        self.assertTrue(self.receive('A', 1)['ok'])
        self.assertEqual(self.clipboard.written, ['A'])

    def test_queued_remote_write_cannot_overwrite_newer_local_copy(self):
        self.clipboard.before_write = lambda: self.bridge.local_change('newer local')
        self.receive('old guest', 1)
        self.assertEqual(self.clipboard.written, [])
        self.assertEqual(clipboard.decode(self.bridge.pending, NONCE), 'newer local')

    def test_only_exact_success_ack_clears_pending(self):
        self.bridge.local_change('one')
        first = json.loads(self.bridge.pending)
        self.bridge.local_change('two')
        second = self.bridge.pending
        ack = dict(first, t='ack', ok=True)
        self.bridge.receive(json.dumps(ack).encode())
        self.assertEqual(self.bridge.pending, second)
        ack = dict(json.loads(second), t='ack', ok=False)
        self.bridge.receive(json.dumps(ack).encode())
        self.assertEqual(self.bridge.pending, second)
        ack['ok'] = True
        self.bridge.receive(json.dumps(ack).encode())
        self.assertIsNone(self.bridge.pending)

    def test_disconnection_retains_only_latest_state(self):
        for index in range(1000):
            self.bridge.local_change(str(index))
        pending = self.bridge.pending
        connection = mock.Mock()
        connection.sendall.side_effect = OSError('disconnected')
        with self.assertRaises(OSError):
            self.bridge.serve(connection)
        self.assertEqual(self.bridge.pending, pending)
        self.assertEqual(clipboard.decode(pending, NONCE), '999')

    def test_hello_restores_acknowledged_current_state(self):
        self.bridge.local_change('state')
        frame = json.loads(self.bridge.pending)
        self.bridge.receive(json.dumps(dict(frame, t='ack', ok=True)).encode())
        self.bridge.receive(clipboard.packet(NONCE, 'hello', o='guest', ready=True))
        self.assertEqual(clipboard.decode(self.bridge.pending, NONCE), 'state')

    def test_sensitive_or_unsupported_local_copy_cancels_old_pending_text(self):
        self.bridge.local_change('old')
        self.bridge.local_change(None)
        frame = json.loads(self.bridge.pending)
        self.assertEqual(frame['t'], 'skip')
        self.assertNotIn('d', frame)
        self.assertIsNone(clipboard.decode(self.bridge.pending, NONCE))

    def test_drop_files_round_trip_and_invalid_headers(self):
        paths = [r'C:\Копия\file.txt', r'\\server\share\folder']
        self.assertEqual(clipboard.drop_paths(clipboard.drop_data(paths)), paths)
        for bad in (b'', bytes(20), clipboard.drop_data(paths)[:-4]):
            with self.assertRaises(ValueError):
                clipboard.drop_paths(bad)
        for paths in ([], ['relative'], [r'\root'], ['C:\\bad\0name']):
            with self.assertRaises(ValueError):
                clipboard.drop_data(paths)

    def test_private_copy_advances_remote_clock_without_writing_or_sending_content(self):
        self.bridge.local_change('old')
        reply = json.loads(self.bridge.receive(clipboard.packet(NONCE, 'skip', s=10, o='guest')))
        self.assertTrue(reply['ok'])
        self.assertFalse(reply['applied'])
        self.bridge.local_change('next')
        self.assertEqual(json.loads(self.bridge.pending)['s'], 11)
        self.assertEqual(self.clipboard.written, [])

    def test_drop_files_accepts_native_nonzero_bool_and_keeps_unicode_alignment_guard(self):
        paths = [r'C:\Копия\файл😀.txt', r'\\server\share\folder']
        for flag in (1, 2, 0x7fffffff, 0x80000000, 0xffffffff):
            with self.subTest(flag=flag):
                raw = bytearray(clipboard.drop_data(paths))
                raw[16:20] = flag.to_bytes(4, 'little')
                self.assertEqual(clipboard.drop_paths(raw), paths)
                raw[0:4] = (21).to_bytes(4, 'little')
                with self.assertRaises(ValueError):
                    clipboard.drop_paths(raw)

    def test_private_copy_is_an_unknown_file_snapshot_for_cache_cleanup(self):
        self.bridge.local_change(clipboard.PRIVATE_COPY)
        self.assertFalse(self.bridge.snapshot_known)
        self.assertEqual(json.loads(self.bridge.pending)['t'], 'skip')
        self.assertIsNone(clipboard.decode(self.bridge.pending, NONCE))

    def test_malformed_deep_json_does_not_escape_decoder(self):
        self.assertIsNone(clipboard.decode(b'[' * 10000 + b'0' + b']' * 10000, NONCE))
        self.assertEqual(clipboard.decode(clipboard.encode(NONCE, 'next'), NONCE), 'next')

    def test_unauthenticated_slow_peer_has_one_total_deadline(self):
        peer = mock.Mock()
        peer.recv.return_value = b' '
        ticks = iter(index / 2 for index in range(10000))
        with mock.patch.object(clipboard.time, 'monotonic', side_effect=lambda: next(ticks)), self.assertRaises(TimeoutError):
            self.bridge.authenticate(peer)
        self.assertLess(peer.recv.call_count, 10, 'Incoming fragments must not extend the message deadline')
        self.assertLessEqual(max(call.args[0] for call in peer.settimeout.call_args_list), .2)

    def test_deflate_requires_complete_stream_without_trailing_data(self):
        frame = json.loads(clipboard.encode(NONCE, 'text' * 200))
        packed = base64.b64decode(frame['d'])
        for broken in (packed[:-1], packed + b'garbage'):
            frame['d'] = base64.b64encode(broken).decode()
            self.assertIsNone(clipboard.decode(json.dumps(frame).encode(), NONCE))


class NativeClipboardBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.window = clipboard.WindowsClipboard.__new__(clipboard.WindowsClipboard)
        self.window.hwnd = 1
        self.window.observed = 10
        self.window.exclude = 999
        self.window.on_change = mock.Mock()
        self.window.recovery = []
        self.window.reset_pending = False
        self.window.close_requested = self.window.writing = False
        self.user = mock.Mock()
        self.kernel = mock.Mock()
        self.user.GetClipboardSequenceNumber.return_value = 10
        self.user.GetClipboardOwner.return_value = 2
        self.user.OpenClipboard.return_value = True
        self.user.IsClipboardFormatAvailable.side_effect = lambda value: value == clipboard.CF_UNICODETEXT
        self.user.GetClipboardData.return_value = 1
        self.kernel.GlobalAlloc.return_value = 1
        self.kernel.GlobalSize.return_value = 20
        self.ole = mock.Mock()
        self.ole.OleDuplicateData.return_value = 2
        self.user.EnumClipboardFormats.side_effect = lambda previous: 13 if previous == 0 else 0
        self.api = mock.patch.multiple(clipboard, user32=self.user, kernel32=self.kernel,
                                      ole32=self.ole, create=True)
        self.api.start()
        self.addCleanup(self.api.stop)
        self.payloads = mock.patch.object(clipboard, '_text_payloads', create=True,
            return_value=[(13, b'n\0\0\0'), (1, b'n\0'), (7, b'n\0'), (16, b'\x09\x04\0\0')])
        self.payloads.start()
        self.addCleanup(self.payloads.stop)

    def test_all_text_formats_are_published_before_close_without_self_echo(self):
        storage = ctypes.create_string_buffer(100)
        self.kernel.GlobalLock.return_value = ctypes.addressof(storage)
        present = set()
        def empty():
            present.clear()
            self.user.GetClipboardSequenceNumber.return_value += 1
            return True
        def publish(format, handle):
            present.add(format)
            self.user.GetClipboardSequenceNumber.return_value += 1
            return handle
        def close():
            # Actual Windows Close adds the three missing text formats.
            if 13 in present:
                missing = {1, 7, 16} - present
                self.user.GetClipboardSequenceNumber.return_value += len(missing)
                present.update(missing)
            return True
        self.user.EmptyClipboard.side_effect = empty
        self.user.SetClipboardData.side_effect = publish
        self.user.CloseClipboard.side_effect = close
        self.assertTrue(self.window.write('new'))
        self.assertEqual([call.args[0] for call in self.user.SetClipboardData.call_args_list], [13, 1, 7, 16])
        self.assertEqual(self.window.observed, self.user.GetClipboardSequenceNumber())
        with mock.patch.object(self.window, 'read') as read:
            self.window.poll()
        read.assert_not_called()

    def test_reset_failure_retains_whole_backup_before_any_restore(self):
        storage = ctypes.create_string_buffer(100)
        self.kernel.GlobalLock.return_value = ctypes.addressof(storage)
        self.user.EmptyClipboard.side_effect = [True, False]
        self.user.SetClipboardData.side_effect = [1, None]
        self.assertFalse(self.window.write('new'))
        self.assertEqual([call.args[0] for call in self.user.SetClipboardData.call_args_list], [13, 1])
        self.assertEqual(self.window.recovery, [(13, 2)])
        self.assertTrue(self.window.reset_pending)
        self.assertEqual(self.window.last_error, 'CLIPBOARD_RESTORING')

    def test_all_prepared_handles_are_freed_if_a_later_allocation_fails_before_open(self):
        storage = ctypes.create_string_buffer(100)
        self.kernel.GlobalLock.return_value = ctypes.addressof(storage)
        self.kernel.GlobalAlloc.side_effect = [101, 102, None]
        self.assertFalse(self.window.write('new'))
        self.assertEqual(self.kernel.GlobalFree.call_args_list, [mock.call(101), mock.call(102)])
        self.user.OpenClipboard.assert_not_called()
        self.user.EmptyClipboard.assert_not_called()
        self.user.SetClipboardData.assert_not_called()

    def test_conversion_failure_precedes_allocation_and_clipboard_open(self):
        with mock.patch.object(clipboard, '_text_payloads', side_effect=OSError('Unusable locale')):
            self.assertFalse(self.window.write('new'))
        self.kernel.GlobalAlloc.assert_not_called()
        self.user.OpenClipboard.assert_not_called()
        self.user.EmptyClipboard.assert_not_called()

    def test_reset_retry_happens_once_before_restoring_original_formats(self):
        storage = ctypes.create_string_buffer(100)
        self.kernel.GlobalLock.return_value = ctypes.addressof(storage)
        self.user.EmptyClipboard.side_effect = [True, False, True]
        self.user.SetClipboardData.side_effect = [1, None, None, 2]
        self.assertFalse(self.window.write('new'))
        self.assertTrue(self.window.reset_pending)
        self.assertEqual(self.user.SetClipboardData.call_count, 2)
        self.assertFalse(self.window.restore_pending())  # Reset succeeds; first backup restore still fails.
        self.assertFalse(self.window.reset_pending)
        self.assertEqual(self.user.EmptyClipboard.call_count, 3)
        self.assertTrue(self.window.restore_pending())
        self.assertEqual(self.user.EmptyClipboard.call_count, 3)  # Must not erase already restored formats.
        self.assertEqual(self.window.recovery, [])

    def test_partial_restore_retry_keeps_successful_original_formats_without_another_empty(self):
        storage = ctypes.create_string_buffer(100)
        self.kernel.GlobalLock.return_value = ctypes.addressof(storage)
        self.user.EnumClipboardFormats.side_effect = lambda previous: {0: 13, 13: 49152, 49152: 0}[previous]
        self.user.GetClipboardData.side_effect = lambda format: {13: 11, 49152: 12}[format]
        self.ole.OleDuplicateData.side_effect = lambda source, format, flags: source + 100
        self.user.SetClipboardData.side_effect = [1, None, 111, None, 112]
        self.assertFalse(self.window.write('new'))
        self.assertEqual(self.window.recovery, [(49152, 112)])
        self.assertFalse(self.window.reset_pending)
        self.assertTrue(self.window.restore_pending())
        self.assertEqual(self.user.EmptyClipboard.call_count, 2)
        self.assertEqual(self.user.SetClipboardData.call_args_list,
            [mock.call(13, 1), mock.call(1, 1), mock.call(13, 111), mock.call(49152, 112), mock.call(49152, 112)])

    def test_changed_sequence_with_same_or_unknown_owner_preserves_all_backup_without_restore(self):
        for owner in (1, None, 0):
            with self.subTest(owner=owner):
                self.window.recovery = [(13, 2), (49152, 3)]
                self.window.reset_pending = True
                self.user.GetClipboardSequenceNumber.return_value = 11
                self.user.GetClipboardOwner.return_value = owner
                self.assertFalse(self.window.restore_pending())
                self.assertEqual(self.window.recovery, [(13, 2), (49152, 3)])
                self.assertTrue(self.window.reset_pending)
                self.assertEqual(self.window.last_error, 'CLIPBOARD_RESTORING')
        self.user.SetClipboardData.assert_not_called()
        self.user.EmptyClipboard.assert_not_called()
        self.kernel.GlobalFree.assert_not_called()

    def test_changed_sequence_with_confirmed_foreign_owner_retires_backup_and_captures_copy(self):
        self.window.recovery = [(13, 2), (49152, 3)]
        self.window.reset_pending = True
        self.user.GetClipboardSequenceNumber.return_value = 11
        self.user.GetClipboardOwner.return_value = 2
        with mock.patch.object(self.window, 'read', return_value=(11, 'new user copy')):
            self.window.poll()
        self.assertEqual(self.window.recovery, [])
        self.assertFalse(self.window.reset_pending)
        self.assertEqual(self.kernel.GlobalFree.call_args_list, [mock.call(2), mock.call(3)])
        self.user.SetClipboardData.assert_not_called()
        self.user.EmptyClipboard.assert_not_called()
        self.window.on_change.assert_called_once_with('new user copy')

    def test_without_backup_same_owner_does_not_suppress_a_new_local_copy(self):
        self.user.GetClipboardSequenceNumber.return_value = 11
        self.user.GetClipboardOwner.return_value = 1
        with mock.patch.object(self.window, 'read', return_value=(11, 'compat writer copy')):
            self.window.poll()
        self.window.on_change.assert_called_once_with('compat writer copy')

    def test_file_drop_still_publishes_only_its_single_format(self):
        storage = ctypes.create_string_buffer(1024)
        self.kernel.GlobalLock.return_value = ctypes.addressof(storage)
        self.assertTrue(self.window.write_files([r'C:\own-fixture.txt']))
        self.assertEqual(self.user.SetClipboardData.call_args_list, [mock.call(15, 1)])

    def test_timeout_after_native_start_waits_for_the_actual_publish_result(self):
        done, started, cancelled = mock.Mock(), mock.Mock(), mock.Mock()
        done.wait.side_effect = [False, True]
        started.is_set.return_value = True
        def posted(*args):
            self.window.pending['ok'] = True
            return True
        self.user.PostMessageW.side_effect = posted
        with mock.patch.object(clipboard.threading, 'Event', side_effect=[done, started, cancelled]):
            self.assertTrue(self.window.set_files([r'C:\own-copy.txt'], lambda: True))
        self.assertEqual(done.wait.call_args_list, [mock.call(2), mock.call()])
        cancelled.set.assert_called_once()

    def test_failed_global_lock_leaves_clipboard_untouched(self):
        self.kernel.GlobalLock.return_value = None
        with mock.patch.object(ctypes, 'memmove') as copy:
            self.assertFalse(self.window.write('new'))
        copy.assert_not_called()
        self.user.EmptyClipboard.assert_not_called()
        self.kernel.GlobalFree.assert_called_once_with(1)

    def test_stale_write_is_rejected_while_clipboard_is_locked(self):
        storage = ctypes.create_string_buffer(100)
        self.kernel.GlobalLock.return_value = ctypes.addressof(storage)
        self.user.GetClipboardSequenceNumber.return_value = 11
        self.assertFalse(self.window.write('old'))
        self.user.EmptyClipboard.assert_not_called()
        self.user.CloseClipboard.assert_called_once()

    def test_failed_commit_does_not_turn_our_empty_into_a_new_local_copy(self):
        storage = ctypes.create_string_buffer(100)
        self.kernel.GlobalLock.return_value = ctypes.addressof(storage)
        def empty():
            self.user.GetClipboardSequenceNumber.return_value += 1
            return True
        self.user.EmptyClipboard.side_effect = empty
        self.user.SetClipboardData.return_value = None
        self.assertFalse(self.window.write('retry'))
        with mock.patch.object(self.window, 'read', return_value=(11, None)) as read:
            self.window.poll()
        read.assert_not_called()
        self.window.on_change.assert_not_called()
        self.user.SetClipboardData.return_value = 1
        self.assertTrue(self.window.write('retry'))

    def test_failed_publication_restores_every_previous_format_under_the_same_lock(self):
        storage = ctypes.create_string_buffer(100)
        self.kernel.GlobalLock.return_value = ctypes.addressof(storage)
        self.user.EnumClipboardFormats.side_effect = lambda previous: {0: 13, 13: 49152, 49152: 0}[previous]
        self.user.GetClipboardData.side_effect = lambda format: {13: 11, 49152: 12}[format]
        self.ole.OleDuplicateData.side_effect = lambda source, format, flags: source + 100
        self.user.SetClipboardData.side_effect = [None, 111, 112]
        self.assertFalse(self.window.write('new'))
        self.assertEqual(self.user.SetClipboardData.call_args_list,
                         [mock.call(13, 1), mock.call(13, 111), mock.call(49152, 112)])
        self.user.CloseClipboard.assert_called_once()
        self.assertEqual(self.window.recovery, [])
        self.assertEqual(self.kernel.GlobalFree.call_args_list, [mock.call(1)] * 4)

    def test_snapshot_failure_refuses_to_empty_the_clipboard(self):
        storage = ctypes.create_string_buffer(100)
        self.kernel.GlobalLock.return_value = ctypes.addressof(storage)
        self.ole.OleDuplicateData.return_value = None
        self.assertFalse(self.window.write('new'))
        self.user.EmptyClipboard.assert_not_called()
        self.user.SetClipboardData.assert_not_called()

    def test_backup_bounds_and_unsupported_formats_preserve_the_original(self):
        for format in (0x80, 0x200, 0x300):
            with self.subTest(format=format):
                self.user.EnumClipboardFormats.side_effect = lambda previous: format
                with self.assertRaises(OSError):
                    clipboard._snapshot_clipboard()
        self.user.EnumClipboardFormats.side_effect = lambda previous: 13 if previous == 0 else 0
        for size in (0, 64 * 1024 * 1024 + 1):
            with self.subTest(size=size):
                self.kernel.GlobalSize.return_value = size
                with self.assertRaises(OSError):
                    clipboard._snapshot_clipboard()
        self.user.EmptyClipboard.assert_not_called()

    def test_cyclic_enumeration_releases_its_partial_snapshot(self):
        self.user.EnumClipboardFormats.side_effect = lambda previous: 13
        with self.assertRaises(OSError):
            clipboard._snapshot_clipboard()
        self.kernel.GlobalFree.assert_called_once_with(2)

    def test_gdi_formats_are_duplicated_and_freed_by_their_actual_handle_type(self):
        picture = clipboard._MetafilePicture()
        picture.metafile = 333
        self.user.EnumClipboardFormats.side_effect = lambda previous: {0: 2, 2: 9, 9: 14, 14: 3, 3: 0}[previous]
        self.user.GetClipboardData.side_effect = lambda format: format + 100
        self.kernel.GlobalSize.return_value = ctypes.sizeof(picture)
        self.kernel.GlobalLock.return_value = ctypes.addressof(picture)
        self.ole.OleDuplicateData.side_effect = lambda source, format, flags: source + 100
        with mock.patch.object(clipboard, 'gdi32') as gdi:
            def bitmap(handle, size, pointer):
                pointer._obj.height, pointer._obj.stride, pointer._obj.planes = 2, 8, 1
                return size
            gdi.GetObjectW.side_effect = bitmap
            gdi.GetPaletteEntries.return_value = 4
            gdi.GetEnhMetaFileBits.return_value = 20
            gdi.GetMetaFileBitsEx.return_value = 10
            gdi.CopyEnhMetaFileW.return_value = 214
            copies = clipboard._snapshot_clipboard()
            self.assertEqual(copies, [(2, 202), (9, 209), (14, 214), (3, 203)])
            for format, handle in copies:
                clipboard._free_clipboard_copy(format, handle)
            self.assertEqual(gdi.DeleteObject.call_args_list, [mock.call(202), mock.call(209)])
            gdi.DeleteEnhMetaFile.assert_called_once_with(214)
            gdi.DeleteMetaFile.assert_called_once_with(333)
            self.kernel.GlobalFree.assert_called_once_with(203)

    def test_failed_restore_is_retained_for_retry_without_overwriting_a_newer_copy(self):
        storage = ctypes.create_string_buffer(100)
        self.kernel.GlobalLock.return_value = ctypes.addressof(storage)
        self.user.SetClipboardData.return_value = None
        self.assertFalse(self.window.write('new'))
        self.assertEqual(self.window.recovery, [(13, 2)])
        self.user.SetClipboardData.return_value = 2
        self.window.poll()
        self.assertEqual(self.window.recovery, [])
        self.user.SetClipboardData.return_value = None
        self.assertFalse(self.window.write('new'))
        self.user.GetClipboardSequenceNumber.return_value = 11
        self.user.SetClipboardData.reset_mock()
        with mock.patch.object(self.window, 'read', return_value=(11, 'newer user copy')):
            self.window.poll()
        self.user.SetClipboardData.assert_not_called()
        self.assertEqual(self.window.recovery, [])
        self.window.on_change.assert_called_once_with('newer user copy')

    def test_close_retains_the_owner_window_until_pending_formats_are_restored(self):
        self.window.recovery = [(13, 2)]
        self.user.SetClipboardData.return_value = None
        self.window.window_proc(1, clipboard.WM_CLOSE, 0, 0)
        self.assertEqual(self.window.recovery, [(13, 2)])
        self.user.DestroyWindow.assert_not_called()
        self.kernel.GlobalFree.assert_not_called()
        self.user.SetClipboardData.return_value = 2
        self.window.window_proc(1, clipboard.WM_TIMER, 0, 0)
        self.assertEqual(self.window.recovery, [])
        self.user.PostMessageW.assert_called_with(1, clipboard.WM_CLOSE, 0, 0)
        self.window.window_proc(1, clipboard.WM_CLOSE, 0, 0)
        self.user.DestroyWindow.assert_called_once_with(1)
        self.kernel.GlobalFree.assert_not_called()

    def test_read_never_searches_past_native_allocation_for_terminator(self):
        storage = ctypes.create_string_buffer(b'A\0B\0\0\0')
        self.kernel.GlobalSize.return_value = 4
        self.kernel.GlobalLock.return_value = ctypes.addressof(storage)
        self.assertEqual(self.window.read(), (10, None))
        self.kernel.GlobalSize.return_value = 6
        self.assertEqual(self.window.read(), (10, 'AB'))

    def test_excluded_clipboard_never_claims_its_file_paths_are_absent(self):
        self.user.IsClipboardFormatAvailable.side_effect = lambda format: format == self.window.exclude
        self.assertEqual(self.window.read(), (10, clipboard.PRIVATE_COPY))
        self.user.GetClipboardData.assert_not_called()

    def test_busy_read_is_retried_and_own_sequence_is_not_echoed(self):
        self.window.observed = 9
        with mock.patch.object(self.window, 'read', side_effect=[None, (10, 'new')]) as read:
            self.window.poll()
            self.assertEqual(self.window.observed, 9)
            self.window.poll()
            self.window.poll()
        self.assertEqual(read.call_count, 2)
        self.window.on_change.assert_called_once_with('new')


class NativeTextPayloadTests(unittest.TestCase):
    def test_declared_locale_controls_codepages_and_explicit_utf16_units_include_nul(self):
        kernel = mock.Mock()
        kernel.GetThreadLocale.return_value = 0x0419
        text = 'A😀B'
        raw = (text + '\0').encode('utf-16-le')
        def locale_info(locale, kind, target, count):
            self.assertEqual(locale, 0x0419)
            self.assertEqual(count, 2)
            self.assertIn(kind, (0x20001004, 0x2000000B))
            # Zero is a valid native CP_ACP fallback, not an unusable locale.
            ctypes.cast(target, ctypes.POINTER(ctypes.c_uint32))[0] = 0 if kind == 0x20001004 else 866
            return 2
        def convert(codepage, flags, source, units, output, size, default, used):
            self.assertEqual(units, len(raw) // 2)
            self.assertEqual(ctypes.string_at(source, units * 2), raw)
            self.assertEqual(flags, 0)
            self.assertIsNone(default)
            self.assertIsNone(used)
            result = b'ansi\0' if codepage == 0 else b'oem\0'
            if output is not None:
                self.assertEqual(size, len(result))
                ctypes.memmove(output, result, len(result))
            return len(result)
        kernel.GetLocaleInfoW.side_effect = locale_info
        kernel.WideCharToMultiByte.side_effect = convert
        with mock.patch.object(clipboard, 'kernel32', kernel):
            result = clipboard._text_payloads(text)
        self.assertEqual(result, [(13, raw), (1, b'ansi\0'), (7, b'oem\0'), (16, b'\x19\x04\0\0')])
        self.assertEqual([call.args[0] for call in kernel.WideCharToMultiByte.call_args_list], [0, 0, 866, 866])
        kernel.GlobalAlloc.assert_not_called()

    def test_unusable_locale_or_conversion_refuses_without_any_clipboard_operation(self):
        for failure in ('locale', 'info', 'count', 'conversion', 'oversized'):
            with self.subTest(failure=failure):
                kernel = mock.Mock()
                kernel.GetThreadLocale.return_value = 0 if failure == 'locale' else 0x0409
                def info(locale, kind, target, count):
                    ctypes.cast(target, ctypes.POINTER(ctypes.c_uint32))[0] = 0
                    return 0 if failure == 'info' else 2
                def convert(codepage, flags, source, units, output, count, default, used):
                    if output is None:
                        return 0 if failure == 'count' else clipboard.MAX_CHARS * 4 + 2 if failure == 'oversized' else 2
                    return 0 if failure == 'conversion' else 2
                kernel.GetLocaleInfoW.side_effect = info
                kernel.WideCharToMultiByte.side_effect = convert
                with mock.patch.object(clipboard, 'kernel32', kernel), mock.patch.object(clipboard, 'user32') as user:
                    with self.assertRaises(OSError):
                        clipboard._text_payloads('x')
                kernel.GlobalAlloc.assert_not_called()
                user.OpenClipboard.assert_not_called()
                user.EmptyClipboard.assert_not_called()


class OwnedClipboardAllocationTests(unittest.TestCase):
    def test_native_duplicate_copies_our_allocation_without_reading_the_clipboard(self):
        kernel, ole = clipboard.kernel32, clipboard.ole32
        self.assertGreaterEqual(ole.OleInitialize(None), 0)
        source = copy = None
        try:
            value = 'private allocation 😀\0'.encode('utf-16-le')
            source = kernel.GlobalAlloc(clipboard.GMEM_MOVEABLE, len(value))
            self.assertTrue(source)
            pointer = kernel.GlobalLock(source)
            self.assertTrue(pointer)
            try:
                ctypes.memmove(pointer, value, len(value))
            finally:
                kernel.GlobalUnlock(source)
            copy = ole.OleDuplicateData(source, clipboard.CF_UNICODETEXT, clipboard.GMEM_MOVEABLE)
            self.assertTrue(copy)
            self.assertNotEqual(source, copy)
            pointer = kernel.GlobalLock(copy)
            self.assertTrue(pointer)
            try:
                self.assertEqual(ctypes.string_at(pointer, len(value)), value)
            finally:
                kernel.GlobalUnlock(copy)
        finally:
            for handle in (copy, source):
                if handle:
                    clipboard._free_clipboard_copy(clipboard.CF_UNICODETEXT, handle)
            ole.OleUninitialize()


if __name__ == '__main__':
    unittest.main()
