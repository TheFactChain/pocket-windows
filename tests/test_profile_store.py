import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
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
        with self.store.session_profile(first['profile_id']) as (selected, profile):
            second = self.store.save(self.config | {'name': 'Новый'}, profile_id=first['profile_id'])
            self.assertNotEqual(first['revision_id'], second['revision_id'])
            self.assertTrue(self.store.get_view(first['profile_id'])['selected'])
            self.assertEqual(self.store.secret_config(first['profile_id'], first['revision_id'])['credentials']['auth'], 'old')
            self.assertEqual(selected['revision_id'], first['revision_id'])
            self.assertEqual(profile['credentials']['auth'], 'old')
        with self.assertRaises(ValueError):
            self.store.save(self.config, {'auth': ''}, profile_id=first['profile_id'])
        self.assertEqual(self.store.get_view(first['profile_id'])['revision_id'], second['revision_id'])

    def test_inactive_history_is_pruned_before_the_store_size_check(self):
        first = self.store.save(self.config, {'auth': 'first'})
        with mock.patch.object(storage, 'MAX_STORE_BYTES', 4096):
            for number in range(30):
                current = self.store.save(self.config | {'name': 'revision ' + str(number)}, profile_id=first['profile_id'])
        data = self.store._read()
        self.assertEqual(set(data['profiles'][first['profile_id']]['revisions']), {current['revision_id']})
        self.assertLess((self.directory / 'profiles.json').stat().st_size, 4096)
        with self.assertRaises(ValueError):
            self.store.secret_config(first['profile_id'], first['revision_id'])

    def test_active_revision_is_retained_until_its_session_releases_the_pin(self):
        first = self.store.save(self.config, {'auth': 'old'})
        self.store.select(first['profile_id'])
        with self.store.session_profile() as (selected, profile):
            for number in range(3):
                current = self.store.save(self.config, {'auth': str(number)}, profile_id=first['profile_id'])
            self.assertEqual(selected['revision_id'], first['revision_id'])
            self.assertEqual(profile['credentials']['auth'], 'old')
            self.assertEqual(self.store.secret_config(first['profile_id'], first['revision_id'])['credentials']['auth'], 'old')
            self.assertEqual(len(self.store._read()['profiles'][first['profile_id']]['revisions']), 2)
        current = self.store.save(self.config, profile_id=first['profile_id'])
        self.assertEqual(set(self.store._read()['profiles'][first['profile_id']]['revisions']), {current['revision_id']})
        self.assertEqual(list((self.directory / 'leases').iterdir()), [])

    def test_crashed_session_releases_its_revision_pin_without_stale_state(self):
        first = self.store.save(self.config, {'auth': 'old'})
        code = """import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from profile_store import ProfileStore
with ProfileStore(Path(sys.argv[2])).session_profile(sys.argv[3]):
    print('READY', flush=True)
    sys.stdin.read(1)
"""
        child = subprocess.Popen([sys.executable, '-X', 'utf8', '-I', '-B', '-c', code,
                                  str(Path(storage.__file__).parent), str(self.directory), first['profile_id']],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        ready = queue.Queue()
        reader = threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True)
        reader.start()
        try:
            self.assertEqual(ready.get(timeout=10), b'READY\r\n')
            self.store.save(self.config, {'auth': 'new'}, profile_id=first['profile_id'])
            self.assertEqual(self.store.secret_config(first['profile_id'], first['revision_id'])['credentials']['auth'], 'old')
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
            reader.join(timeout=2)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()
        self.store.save(self.config, profile_id=first['profile_id'])
        with self.assertRaises(ValueError):
            self.store.secret_config(first['profile_id'], first['revision_id'])
        self.assertEqual(list((self.directory / 'leases').iterdir()), [])

    def test_legacy_unsupported_endpoint_in_old_history_does_not_break_valid_profiles(self):
        first = self.store.save(self.config, {'auth': 'secret'})
        data = self.store._read()
        current = data['profiles'][first['profile_id']]['revisions'][first['revision_id']]
        data['profiles'][first['profile_id']]['revisions']['f' * 32] = {
            'config': current['config'] | {'host': '0.0.0.0'}, 'credentials_blob': current['credentials_blob']}
        self.store._write(data)
        before = (self.directory / 'profiles.json').read_bytes()
        self.assertFalse(self.store.get_view(first['profile_id'])['needs_correction'])
        self.assertEqual((self.directory / 'profiles.json').read_bytes(), before)
        self.store.save(self.config, profile_id=first['profile_id'])
        self.assertNotIn('f' * 32, self.store._read()['profiles'][first['profile_id']]['revisions'])

    def test_current_legacy_endpoint_is_editable_but_cannot_start(self):
        first = self.store.save(self.config, {'auth': 'secret'})
        other = self.store.save(self.config | {'name': 'Other'}, {'auth': 'other'})
        self.store.select(first['profile_id'])
        data = self.store._read()
        data['profiles'][first['profile_id']]['revisions'][first['revision_id']]['config']['host'] = '127.0.0.2'
        self.store._write(data)
        view = self.store.get_view(first['profile_id'])
        self.assertTrue(view['needs_correction'])
        self.assertFalse(view['needs_credentials'])
        self.assertTrue(self.store.selected_summary()['needs_correction'])
        self.assertEqual(len(self.store.list_profiles()), 2)
        self.assertEqual(self.store.get_view(other['profile_id'])['name'], 'Other')
        with self.assertRaises(ValueError), self.store.session_profile(first['profile_id']):
            self.fail('Unsupported saved endpoint cannot start a session')
        repaired = self.store.save(self.config, profile_id=first['profile_id'])
        self.assertFalse(repaired['needs_correction'])

    def test_old_tls_identity_does_not_make_the_profile_store_unreadable(self):
        first = self.store.save(self.config, {'auth': 'secret'})
        data = self.store._read()
        data['profiles'][first['profile_id']]['revisions'][first['revision_id']]['config']['tls_server_name'] = '127.0.0.2'
        self.store._write(data)
        view = self.store.get_view(first['profile_id'])
        self.assertFalse(view['needs_correction'])
        with self.store.session_profile(first['profile_id']) as (_, config):
            self.assertEqual(config['tls_server_name'], '127.0.0.2')

    def test_prepare_draft_uses_current_form_and_pinned_secrets_without_saving(self):
        saved = self.store.save(self.config, {'auth': 'saved-secret'})
        self.store.select(saved['profile_id'])
        before = (self.directory / 'profiles.json').read_bytes()
        draft = self.config | {'name': 'Unsaved', 'host': '203.0.113.77', 'port': 8443}
        with mock.patch.object(self.store, '_write', side_effect=AssertionError('draft must not persist')):
            prepared = self.store.prepare_draft(draft, saved['profile_id'])
        self.assertEqual(prepared['host'], '203.0.113.77')
        self.assertEqual(prepared['port'], 8443)
        self.assertEqual(prepared['credentials'], {'auth': 'saved-secret'})
        self.assertNotIn('credentials', draft)
        self.assertEqual((self.directory / 'profiles.json').read_bytes(), before)
        self.assertEqual(self.store.selected_summary()['revision_id'], saved['revision_id'])

    def test_prepare_draft_does_not_reuse_credentials_after_protocol_or_obfs_change(self):
        saved = self.store.save(self.config, {'auth': 'saved-secret'})
        for changed in (self.config | {'protocol': 'http'}, self.config | {'obfs': 'salamander'}):
            with self.subTest(changed=changed), mock.patch.object(storage, '_unprotect', side_effect=AssertionError('must not decrypt incompatible secrets')):
                with self.assertRaisesRegex(ValueError, 'Enter credentials again'):
                    self.store.prepare_draft(changed, saved['profile_id'])
        prepared = self.store.prepare_draft(self.config | {'credentials': {'auth': 'draft-secret'}}, saved['profile_id'])
        self.assertEqual(prepared['credentials'], {'auth': 'draft-secret'})
        self.assertEqual(self.store.secret_config(saved['profile_id'])['credentials'], {'auth': 'saved-secret'})
        with mock.patch.object(storage, '_unprotect', side_effect=AssertionError('explicit empty credentials must not reuse a secret')):
            anonymous = self.store.prepare_draft(self.config | {'protocol': 'http', 'credentials': {}}, saved['profile_id'])
        self.assertEqual(anonymous['credentials'], {})

    def test_prepare_new_draft_needs_no_store_but_requires_hysteria_auth(self):
        with mock.patch.object(self.store, '_transaction', side_effect=AssertionError('new draft must not create storage')):
            ready = self.store.prepare_draft({'protocol': 'http', 'host': '203.0.113.3', 'port': 8080})
            self.assertEqual(ready['protocol'], 'http')
            with self.assertRaisesRegex(ValueError, 'Hysteria2 authentication is required'):
                self.store.prepare_draft(self.config)
        self.assertFalse(self.directory.exists())

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

    def test_pinned_directory_really_blocks_native_rename_until_release(self):
        self.directory.mkdir()
        destination = self.directory.with_name('moved-profiles')
        with storage._pinned_directory(self.directory, create=False):
            with self.assertRaises(PermissionError):
                self.directory.rename(destination)
        self.directory.rename(destination)
        self.assertTrue(destination.is_dir())

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
