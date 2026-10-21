import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
try:
    import profile_store as storage
except ImportError:
    storage = None


class ProfileStoreTests(unittest.TestCase):
    def test_selected_summary_checks_dpapi_availability_without_returning_secrets(self):
        view = self.store.save(self.config, {'auth': 'never-public'})
        self.store.select(view['profile_id'])
        with mock.patch.object(storage, '_unprotect', side_effect=storage.SecretsUnavailable('private detail')):
            summary = self.store.selected_summary()
        self.assertFalse(summary['secrets_available'])
        self.assertTrue(summary['needs_credentials'])
        self.assertNotIn('never-public', json.dumps(summary))
        self.assertNotIn('private detail', json.dumps(summary))

    def test_secretless_selected_proxy_does_not_use_dpapi(self):
        view = self.store.save({'name': 'Public proxy', 'protocol': 'socks5', 'host': 'example.com', 'port': 1080})
        self.store.select(view['profile_id'])
        with mock.patch.object(storage, '_unprotect', side_effect=AssertionError('Unnecessary DPAPI')):
            summary = self.store.selected_summary()
        self.assertTrue(summary['secrets_available'])
        self.assertFalse(summary['needs_credentials'])

    def setUp(self):
        self.assertIsNotNone(storage, 'Profile store implementation is missing')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'profiles'
        self.store = storage.ProfileStore(self.directory)
        self.config = {'name': 'Личный', 'protocol': 'hysteria2', 'host': 'example.com', 'port': 443}

    def test_dpapi_and_public_views_do_not_disclose_credentials(self):
        view = self.store.save(self.config, {'auth': 'top-secret'})
        self.assertTrue(view['auth_present'])
        self.assertTrue(view['secrets_available'])
        public = json.dumps([view, self.store.list_profiles(), self.store.get_view(view['profile_id']), self.store.export(view['profile_id'])])
        self.assertNotIn('top-secret', public)
        self.assertNotIn('top-secret', (self.directory / 'profiles.json').read_text())
        self.assertEqual(self.store.secret_config(view['profile_id'])['credentials']['auth'], 'top-secret')
        self.assertEqual(self.store.export(view['profile_id'], include_secrets=True)['credentials']['auth'], 'top-secret')

    def test_revisions_select_and_explicit_secret_replacement(self):
        first = self.store.save(self.config, {'auth': 'old'})
        self.store.select(first['profile_id'])
        second = self.store.save(self.config | {'name': 'Новый'}, profile_id=first['profile_id'])
        self.assertNotEqual(first['revision_id'], second['revision_id'])
        self.assertTrue(self.store.get_view(first['profile_id'])['selected'])
        self.assertEqual(self.store.secret_config(first['profile_id'], first['revision_id'])['credentials']['auth'], 'old')
        with self.assertRaises(ValueError):
            self.store.save(self.config, {'auth': ''}, profile_id=first['profile_id'])
        self.assertEqual(self.store.get_view(first['profile_id'])['revision_id'], second['revision_id'])

    def test_transferred_credentials_preserve_profile_and_require_reentry(self):
        view = self.store.save(self.config, {'auth': 'secret'})
        with mock.patch.object(storage, '_unprotect', side_effect=storage.SecretsUnavailable('Re-enter credentials')):
            self.assertTrue(self.store.get_view(view['profile_id'])['needs_credentials'])
            changed = self.store.save(self.config | {'name': 'Transferred'}, profile_id=view['profile_id'])
            self.assertFalse(changed['secrets_available'])
            with self.assertRaises(storage.SecretsUnavailable):
                self.store.secret_config(view['profile_id'])
        repaired = self.store.save(self.config, {'auth': 'new'}, profile_id=view['profile_id'])
        self.assertTrue(repaired['secrets_available'])

    def test_corruption_path_traversal_and_busy_lock_fail_without_overwrite(self):
        view = self.store.save(self.config, {'auth': 'secret'})
        before = (self.directory / 'profiles.json').read_bytes()
        with self.assertRaises(ValueError):
            self.store.select('../outside')
        with storage._exclusive_file(self.directory / 'profiles.lock'):
            with self.assertRaises(storage.StoreBusy):
                self.store.save(self.config, {'auth': 'another'})
        self.assertEqual(before, (self.directory / 'profiles.json').read_bytes())
        (self.directory / 'profiles.json').write_text('{broken')
        with self.assertRaises(storage.StoreError):
            self.store.save(self.config, {'auth': 'another'})
        self.assertEqual((self.directory / 'profiles.json').read_text(), '{broken')

    def test_creation_requires_hysteria_auth_but_plain_proxy_can_have_none(self):
        with self.assertRaises(ValueError):
            self.store.save(self.config)
        view = self.store.save({'protocol': 'http', 'host': 'example.com', 'port': 8080})
        self.assertFalse(view['auth_present'])
        self.assertFalse(view['needs_credentials'])
        with self.assertRaises(ValueError):
            self.store.export(view['profile_id'], include_secrets=1)

    def test_anonymous_proxy_can_change_protocol_without_nonexistent_credentials(self):
        config = {'protocol': 'http', 'host': 'example.com', 'port': 8080}
        initial = self.store.save(config)
        changed = self.store.save(config | {'protocol': 'socks5'}, profile_id=initial['profile_id'])
        self.assertEqual(changed['protocol'], 'socks5')
        self.assertFalse(changed['auth_present'])
        self.assertFalse(changed['needs_credentials'])
        with self.assertRaisesRegex(ValueError, 'Hysteria2 authentication is required'):
            self.store.save(config | {'protocol': 'hysteria2'}, profile_id=initial['profile_id'])
        self.assertEqual(self.store.get_view(initial['profile_id'])['revision_id'], changed['revision_id'])

    def test_authenticated_proxy_protocol_change_still_requires_explicit_credentials(self):
        config = {'protocol': 'http', 'host': 'example.com', 'port': 8080}
        initial = self.store.save(config, {'username': 'u', 'password': 'secret'})
        with self.assertRaisesRegex(ValueError, 'Enter credentials again'):
            self.store.save(config | {'protocol': 'socks5'}, profile_id=initial['profile_id'])
        self.assertEqual(self.store.get_view(initial['profile_id'])['revision_id'], initial['revision_id'])

    def test_links_are_rejected_before_storage_access(self):
        target = Path(self.temporary.name) / 'target'
        target.mkdir()
        self.directory.symlink_to(target, target_is_directory=True)
        with self.assertRaises(storage.StoreError):
            self.store.list_profiles()
        self.directory.unlink()
        self.directory.mkdir()
        sentinel = target / 'sentinel.json'
        sentinel.write_text('{"secret":"untouched"}')
        os.link(sentinel, self.directory / 'profiles.json')
        with self.assertRaises(storage.StoreError):
            self.store.save(self.config, {'auth': 'secret'})
        self.assertEqual(sentinel.read_text(), '{"secret":"untouched"}')

    def test_failed_replace_preserves_original_and_cleans_temporary_file(self):
        view = self.store.save(self.config, {'auth': 'secret'})
        original = (self.directory / 'profiles.json').read_bytes()
        with mock.patch.object(storage.os, 'replace', side_effect=PermissionError('fixture')):
            with self.assertRaises(storage.StoreError):
                self.store.save(self.config | {'name': 'Interrupted'}, profile_id=view['profile_id'])
        self.assertEqual((self.directory / 'profiles.json').read_bytes(), original)
        self.assertEqual(list(self.directory.glob('*.tmp')), [])

    def test_lock_is_enforced_across_processes(self):
        self.store.save(self.config, {'auth': 'secret'})
        code = ('import sys; sys.path.insert(0,sys.argv[1]); '
                'import profile_store as p; '
                'p.ProfileStore(sys.argv[2]).list_profiles()')
        with storage._exclusive_file(self.directory / 'profiles.lock'):
            result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', code,
                                     str(Path(storage.__file__).parent), str(self.directory)],
                                    capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'StoreBusy', result.stderr)

    def test_duplicate_keys_and_invalid_encrypted_blob_do_not_destroy_metadata(self):
        view = self.store.save(self.config, {'auth': 'secret'})
        path = self.directory / 'profiles.json'
        data = json.loads(path.read_text())
        revision = data['profiles'][view['profile_id']]['revisions'][view['revision_id']]
        revision['credentials_blob'] = 'bm90LWRwYXBp'
        path.write_text(json.dumps(data))
        self.assertTrue(self.store.get_view(view['profile_id'])['needs_credentials'])
        path.write_text('{"schema":1,"schema":1,"selected_profile_id":null,"profiles":{}}')
        with self.assertRaises(storage.StoreError):
            self.store.list_profiles()

    def test_export_to_is_explicit_new_file_and_reply_contains_no_secret(self):
        view = self.store.save(self.config, {'auth': 'export-secret'})
        destination = Path(self.temporary.name) / 'public.json'
        reply = self.store.export_to(view['profile_id'], destination)
        self.assertEqual(reply, {'path': str(destination), 'bytes': destination.stat().st_size,
                                 'include_secrets': False})
        self.assertNotIn('export-secret', destination.read_text(encoding='utf-8'))
        secret_file = destination.with_name('explicit-secret.json')
        secret_reply = self.store.export_to(view['profile_id'], secret_file, include_secrets=True)
        self.assertEqual(json.loads(secret_file.read_text(encoding='utf-8'))['credentials']['auth'], 'export-secret')
        self.assertNotIn('export-secret', json.dumps(secret_reply))
        with self.assertRaises(storage.StoreError):
            self.store.export_to(view['profile_id'], secret_file, include_secrets=True)
        self.assertEqual(json.loads(secret_file.read_text(encoding='utf-8'))['credentials']['auth'], 'export-secret')

    def test_export_to_rejects_unsafe_destinations_and_non_boolean_permission(self):
        view = self.store.save(self.config, {'auth': 'secret'})
        parent = Path(self.temporary.name)
        for destination in ('relative.json', parent / 'bad.txt', parent / 'absent' / 'profile.json',
                            parent / 'profiles' / '..' / 'traversal.json'):
            with self.subTest(destination=destination), self.assertRaises(storage.StoreError):
                self.store.export_to(view['profile_id'], destination)
        self.assertFalse((parent / 'absent').exists())
        with self.assertRaises(ValueError):
            self.store.export_to(view['profile_id'], parent / 'bool.json', include_secrets=1)
        (parent / 'link').symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(storage.StoreError):
            self.store.export_to(view['profile_id'], parent / 'link' / 'escaped.json')
        self.assertFalse((self.directory / 'escaped.json').exists())

    def test_export_to_write_failure_removes_only_own_file(self):
        view = self.store.save(self.config, {'auth': 'secret'})
        destination = Path(self.temporary.name) / 'partial.json'
        kernel = storage._winapi()
        original_write = kernel.WriteFile
        def fail_after_partial(handle, buffer, count, written, overlapped):
            original_write(handle, buffer, 2, written, overlapped)
            return 0
        with mock.patch.object(storage, '_winapi', return_value=kernel), mock.patch.object(kernel, 'WriteFile', side_effect=fail_after_partial):
            with self.assertRaises(storage.StoreError):
                self.store.export_to(view['profile_id'], destination, include_secrets=True)
        self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
