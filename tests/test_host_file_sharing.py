import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import shutil
import os
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import portable_app as app
import runtime_config


class FileSharingTests(unittest.TestCase):
    def test_native_protocol_store_and_guest_regressions(self):
        root = Path(__file__).resolve().parents[1]
        component = root / 'runtime/host-files'
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='file-components-') as temporary:
            output = Path(temporary)
            for name in ('SMBLibrary.dll', 'SMBLibrary.Win32.dll', 'Utilities.dll'):
                shutil.copyfile(component / name, output / name)
            for harness in ('HostFileStoreRegression', 'HostFilesServerRegression', 'HostFilesCompoundRegression',
                            'HostFilesMemoryRegression', 'GuestFilesRegression'):
                with self.subTest(harness=harness):
                    executable = output / (harness + '.exe')
                    sources = [root / 'guest/files/GuestFiles.cs', root / 'guest/GuestEnvironment.cs'] if harness == 'GuestFilesRegression' else sorted((root / 'host/files').glob('*.cs'))
                    command = [str(compiler), '/nologo', '/target:exe', '/platform:x64', '/main:' + harness,
                               '/reference:System.Web.Extensions.dll', '/reference:System.Windows.Forms.dll', '/reference:Microsoft.CSharp.dll',
                               *['/reference:' + str(output / name) for name in ('SMBLibrary.dll', 'SMBLibrary.Win32.dll', 'Utilities.dll')],
                               '/out:' + str(executable), *map(str, sources), str(root / 'tests' / (harness + '.cs'))]
                    build = subprocess.run(command, capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
                    check = subprocess.run([str(executable), str(output)], capture_output=True, timeout=90,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(check.returncode, 0, (check.stdout + check.stderr).decode(errors='replace'))

    def module(self):
        path = Path(__file__).resolve().parents[1] / 'scripts/host_file_sharing.py'
        self.assertTrue(path.exists(), 'Separate host file-sharing settings and session module required')
        spec = importlib.util.spec_from_file_location('host_file_sharing', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_settings_are_off_by_default_and_strict(self):
        files = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(files.load_settings(Path(temporary)), {'schema': 1, 'mode': 'off', 'folders': []})
        for value in ({'schema': True}, {'mode': 'everything'}, {'folders': 'C:\\'},
                      {'mode': 'selected'}, {'mode': 'selected', 'folders': ['relative']},
                      {'folders': ['\\\\server\\share']}, {'folders': ['C:\\one', 'c:/one/']},
                      {'folders': ['C:\\one:stream']}, {'password': 'secret'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                files.validate_settings(value)

    def test_save_is_separate_and_bad_input_does_not_partially_save(self):
        files = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = {'schema': 1, 'mode': 'selected', 'folders': [str(root)]}
            app.save_settings(root, dict(runtime_config.DEFAULTS, files=value))
            self.assertEqual(files.load_settings(root), value)
            self.assertNotIn('files', json.loads((root / 'config/runtime.json').read_bytes()))
            before = (root / 'config/runtime.json').read_bytes()
            with self.assertRaises(ValueError):
                app.save_settings(root, dict(runtime_config.DEFAULTS, files={'mode': 'selected'}))
            self.assertEqual((root / 'config/runtime.json').read_bytes(), before)

    def test_missing_selected_folder_is_reported_without_expanding_access(self):
        files = self.module()
        with tempfile.TemporaryDirectory(prefix='файлы ') as temporary:
            root = Path(temporary)
            missing = root / 'нет папки'
            shares, unavailable = files.resolve_shares({'schema': 1, 'mode': 'selected', 'folders': [str(root), str(missing)]})
            self.assertEqual(len(shares), 1)
            self.assertEqual(shares[0]['path'], str(root))
            self.assertEqual(len(unavailable), 1)
            self.assertNotIn('path', unavailable[0])
            self.assertEqual(files.resolve_shares({'mode': 'off', 'folders': [str(root)]}), ([], []))

    def test_program_state_and_startup_are_protected_and_reported(self):
        files = self.module()
        with tempfile.TemporaryDirectory(prefix='защита ') as temporary:
            root = Path(temporary) / 'Моя версия'
            (root / 'scripts').mkdir(parents=True)
            beside = Path(temporary) / 'рядом'
            beside.mkdir()
            protected = files.protected_paths(root)
            self.assertEqual(protected[0], str(root.resolve()))
            startup = files._known_folder(files.STARTUP_FOLDERS[0])
            if startup is not None and startup.exists():
                self.assertIn(str(startup.resolve()).casefold(), [item.casefold() for item in protected])
            self.assertTrue(all(len(item) > 3 and item[1:3] == ':\\' for item in protected), 'Drive roots and relative paths must never be protected entries')
            shares, unavailable = files.resolve_shares(
                {'mode': 'selected', 'folders': [str(root / 'scripts'), str(root), str(beside)]}, protected)
            self.assertEqual([item['path'] for item in shares], [str(beside)])
            self.assertEqual([item['status'] for item in unavailable], ['protected', 'protected'])
            self.assertTrue(all('path' not in item for item in unavailable))

    def test_static_link_request_is_an_exact_receipt_without_mappings(self):
        files = self.module()
        config = {'schema': 1, 'session_nonce': 'a' * 32, 'shares': [{'name': 'PC_test', 'label': 'ПК', 'preferred_letter': ''}]}
        request = {'schema': 1, 'session_nonce': 'a' * 32, 'success': False, 'code': files.LINK_REQUEST, 'mappings': []}
        self.assertEqual(files.validate_receipt(json.dumps(request), config)['code'], 'NIC_STATIC')
        for patch in ({'success': True}, {'session_nonce': 'b' * 32}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                files.validate_receipt(json.dumps(dict(request, **patch)), config)

    def test_file_network_is_restricted_and_has_one_loopback_forward(self):
        files = self.module()
        arguments = files.network_arguments(54321)
        line = ' '.join(arguments)
        for required in ('restrict=on', 'ipv6=off', '192.168.234.4:445-tcp:127.0.0.1:54321', '52:54:00:77:00:04'):
            self.assertIn(required, line)
        self.assertEqual(line.count('guestfwd='), 1)
        self.assertNotIn('hostfwd', line)
        for invalid in (True, 445, 65536, '54321'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                files.network_arguments(invalid)

    def test_receipt_rejects_stale_nonce_and_unrequested_or_false_success(self):
        files = self.module()
        config = {'schema': 1, 'session_nonce': 'a' * 32, 'shares': [{'name': 'PC_test', 'label': 'ПК', 'preferred_letter': ''}]}
        receipt = {'schema': 1, 'session_nonce': 'a' * 32, 'success': True, 'code': 'OK', 'mappings': [{'name': 'PC_test', 'letter': 'Z'}]}
        self.assertEqual(files.validate_receipt(json.dumps(receipt), config), receipt)
        for patch in ({'session_nonce': 'b' * 32}, {'mappings': []},
                      {'mappings': [{'name': 'PC_other', 'letter': 'Z'}]},
                      {'mappings': [{'name': 'PC_test', 'letter': 'C'}]}, {'success': 1}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                files.validate_receipt(json.dumps(dict(receipt, **patch)), config)

    def test_private_medium_is_readonly_pinned_and_removed_only_after_guest_exit(self):
        import file_share_carrier
        import session_carrier
        with tempfile.TemporaryDirectory(prefix='files-private-') as temporary:
            medium = file_share_carrier.Medium({'schema': 1, 'session_nonce': 'c' * 32, 'shares': []}, b'MZfixture',
                                              base_directory=Path(temporary) / 'private')
            acl = session_carrier.inspect_private_acl(medium.path / 'HOSTFILS.JSN')
            self.assertTrue(acl['protected'])
            with medium.hold() as arguments:
                block = json.loads(arguments[1])
                self.assertFalse(block['rw'])
                self.assertTrue(block['read-only'])
                self.assertEqual(block['label'], 'WCPFILES')
                with self.assertRaises(OSError):
                    (medium.path / 'HOSTFILS.JSN').write_bytes(b'overwrite')
                with self.assertRaises(session_carrier.CarrierError):
                    medium.cleanup(guest_stopped=True)
            with self.assertRaises(session_carrier.CarrierError):
                medium.cleanup(guest_stopped=False)
            medium.cleanup(guest_stopped=True)
            self.assertFalse(medium.path.exists())

    def test_native_pipe_owned_helper_stays_alive_and_closes_without_leaking_credentials(self):
        files = self.module()
        component = Path(__file__).resolve().parents[1] / 'runtime/host-files'
        self.assertTrue(component.is_dir(), 'Build file helpers into the local developer runtime before native checks')
        with tempfile.TemporaryDirectory(prefix='files-session-') as temporary:
            root = Path(temporary) / 'app'
            (root / 'runtime').mkdir(parents=True)
            shutil.copytree(component, root / 'runtime/host-files')
            (root / 'config').mkdir()
            shared = Path(temporary) / 'selected'
            shared.mkdir()
            (root / 'config/files.json').write_text(json.dumps({'mode': 'selected', 'folders': [str(shared)]}))
            session = files.Session(root, 'd' * 32, base_directory=Path(temporary) / 'private')
            process, medium = session.process, session.medium
            try:
                self.assertTrue(session.healthy())
                self.assertEqual(session.public['share_count'], 1)
                self.assertNotIn(session.config['password'], json.dumps(session.public) + repr(session.arguments))
                guest = json.loads((medium.path / 'HOSTFILS.JSN').read_bytes())
                self.assertNotIn(str(shared), json.dumps(guest))
                with self.assertRaises(RuntimeError):
                    session.close(guest_stopped=False)
                self.assertTrue(session.healthy())
            finally:
                session.close(guest_stopped=True)
            self.assertIsNotNone(process.poll())
            self.assertFalse(medium.path.exists())


if __name__ == '__main__':
    unittest.main()
