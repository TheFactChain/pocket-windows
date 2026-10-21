import io
import ctypes
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
try:
    import session_carrier as carriers
except ImportError:
    carriers = None


class SessionCarrierTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(carriers, 'Private session carrier implementation is missing')
        self.temporary = tempfile.TemporaryDirectory(prefix='private carrier fixture ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / 'sessions'
        self.session = {'schema': 1, 'nonce': 'a' * 32, 'revision': 'b' * 32,
                        'gateway_version': 'fixture-1', 'endpoint_ipv4': '203.0.113.10',
                        'profile': {'protocol': 'hysteria2', 'host': 'example.com', 'port': 443,
                                    'credentials': {'auth': 'fixture-only-secret'}}}

    def create(self):
        return carriers.create_carrier(self.session, base_directory=self.base)

    def test_rawtar_has_one_session_member_and_protected_private_acl(self):
        carrier = self.create()
        with carrier.hold() as media:
            self.assertEqual(media['serial'], 'WCSESSION')
            self.assertEqual(media['format'], 'raw')
            self.assertTrue(media['readonly'])
            self.assertNotIn('fixture-only-secret', json.dumps(media))
            path = Path(media['path'])
            with tarfile.open(path) as archive:
                self.assertEqual(archive.getnames(), ['session.json'])
                member = archive.getmember('session.json')
                self.assertTrue(member.isfile())
                self.assertLessEqual(member.size, 65536)
                value = json.load(archive.extractfile(member))
                self.assertEqual(value['nonce'], self.session['nonce'])
                self.assertEqual(value['profile']['credentials']['auth'], 'fixture-only-secret')
            for protected in (self.base, path.parent, path, path.parent / 'carrier.json'):
                summary = carriers.inspect_private_acl(protected)
                self.assertTrue(summary['protected'])
                self.assertEqual(set(summary['allowed_sids']), {summary['owner_sid'], 'S-1-5-18'})

    def test_hold_denies_writers_and_never_deletes_without_explicit_guest_shutdown_proof(self):
        carrier = self.create()
        with carrier.hold() as media:
            path = Path(media['path'])
            with self.assertRaises(OSError):
                path.write_bytes(b'forbidden')
            with self.assertRaises(carriers.CarrierError):
                carrier.cleanup(guest_stopped=True)
        self.assertTrue(path.exists())
        for proof in (False, 1, None):
            with self.assertRaises(carriers.CarrierError):
                carrier.cleanup(guest_stopped=proof)
        self.assertTrue(path.exists())
        carrier.cleanup(guest_stopped=True)
        self.assertFalse(path.parent.exists())
        self.assertTrue(self.base.exists())

    def test_orphan_can_be_reopened_only_by_exact_path_nonce_and_valid_identity(self):
        carrier = self.create()
        with carrier.hold() as media:
            path = Path(media['path'])
        reopened = carriers.open_carrier(path, self.session['nonce'], base_directory=self.base)
        reopened.cleanup(guest_stopped=True)
        self.assertFalse(path.exists())

    def test_duplicate_nonce_does_not_overwrite_existing_media(self):
        carrier = self.create()
        with carrier.hold() as media:
            path = Path(media['path'])
            original = path.read_bytes()
        with self.assertRaises(carriers.CarrierError):
            self.create()
        self.assertEqual(path.read_bytes(), original)

    def test_reparse_base_and_public_existing_base_are_rejected_without_acl_changes(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.base.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(carriers.CarrierError):
            self.create()
        self.assertEqual(list(outside.iterdir()), [])
        self.base.unlink()
        self.base.mkdir()
        with self.assertRaises(carriers.CarrierError):
            self.create()
        self.assertEqual(list(self.base.iterdir()), [])

    def test_tampered_media_hardlinks_and_extra_files_block_cleanup(self):
        carrier = self.create()
        with carrier.hold() as media:
            path = Path(media['path'])
        extra = path.parent / 'user-file.txt'
        extra.write_text('do not remove')
        with self.assertRaises(carriers.CarrierError):
            carrier.cleanup(guest_stopped=True)
        self.assertTrue(path.exists())
        self.assertEqual(extra.read_text(), 'do not remove')
        extra.unlink()
        link = self.root / 'media-hardlink'
        os.link(path, link)
        with self.assertRaises(carriers.CarrierError):
            carriers.open_carrier(path, self.session['nonce'], base_directory=self.base)
        link.unlink()
        path.write_bytes(b'tampered data')
        with self.assertRaises(carriers.CarrierError):
            carrier.cleanup(guest_stopped=True)
        self.assertEqual(path.read_bytes(), b'tampered data')

    def test_default_location_is_local_appdata_and_nonce_is_exactly_32_hex(self):
        with mock.patch.object(carriers, '_local_app_data', return_value=self.root):
            carrier = carriers.create_carrier(self.session)
            with carrier.hold() as media:
                self.assertEqual(Path(media['path']).parent.parent, self.root / 'WindowsClaudePortable/sessions')
            carrier.cleanup(guest_stopped=True)
        for nonce in ('../outside', 'a' * 64, 'A' * 32):
            with self.subTest(nonce=nonce), self.assertRaises(carriers.CarrierError) as caught:
                carriers.create_carrier(self.session | {'nonce': nonce}, base_directory=self.base)
            self.assertNotIn('fixture-only-secret', str(caught.exception))

    def test_marker_creation_failure_rolls_back_only_this_new_carrier(self):
        sentinel = self.root / 'sentinel.txt'
        sentinel.write_bytes(b'unrelated user data')
        original_write = carriers._write_new
        def fail_marker(path, value):
            if path.name == 'carrier.json':
                raise carriers.CarrierError('fixture marker creation failure')
            return original_write(path, value)
        with mock.patch.object(carriers, '_write_new', side_effect=fail_marker):
            with self.assertRaises(carriers.CarrierError):
                self.create()
        self.assertFalse((self.base / self.session['nonce']).exists())
        self.assertEqual(sentinel.read_bytes(), b'unrelated user data')

    def test_partial_native_write_failure_leaves_no_secret_orphan(self):
        sentinel = self.root / 'sentinel.txt'
        sentinel.write_bytes(b'preserve')
        kernel, security = carriers._api()
        original_write = kernel.WriteFile
        for failure_call in (1, 2):
            calls = []
            def partial_write(handle, value, size, written, overlapped):
                calls.append(handle)
                if len(calls) != failure_call:
                    return original_write(handle, value, size, written, overlapped)
                original_write(handle, value, min(size, 5000), written, overlapped)
                return 0
            with self.subTest(failure_call=failure_call), \
                    mock.patch.object(carriers, '_api', return_value=(kernel, security)), \
                    mock.patch.object(kernel, 'WriteFile', side_effect=partial_write):
                with self.assertRaises(carriers.CarrierError):
                    self.create()
            self.assertFalse((self.base / self.session['nonce']).exists())
        self.assertEqual(sentinel.read_bytes(), b'preserve')

    def test_rollback_does_not_remove_an_unexpected_file_inside_new_directory(self):
        original_write = carriers._write_new
        sentinel = self.base / self.session['nonce'] / 'sentinel.txt'
        def fail_marker(path, value):
            if path.name == 'carrier.json':
                sentinel.write_bytes(b'preserve unexpected member')
                raise carriers.CarrierError('fixture marker failure')
            return original_write(path, value)
        with mock.patch.object(carriers, '_write_new', side_effect=fail_marker):
            with self.assertRaises(carriers.CarrierError):
                self.create()
        self.assertEqual(sentinel.read_bytes(), b'preserve unexpected member')
        self.assertFalse(sentinel.with_name('session.tar').exists())
        self.assertFalse(sentinel.with_name('carrier.json').exists())


if __name__ == '__main__':
    unittest.main()
