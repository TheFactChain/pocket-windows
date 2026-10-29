import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import shutil
import os
import subprocess
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import portable_app as app
import runtime_config
CLIPBOARD_SOURCES = [Path(__file__).resolve().parents[1] / 'guest' / name for name in
                     ('GuestClipboard.cs', 'ClipboardFiles.cs', 'ClipboardStage.cs', 'ClipboardProtocol.cs', 'ClipboardTransport.cs')]


class FileSharingTests(unittest.TestCase):
    def test_clipboard_session_uses_private_nonce_without_starting_smb(self):
        files = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component = root / 'runtime/host-files'
            component.mkdir(parents=True)
            helper = b'MZfixture-clipboard'
            (component / 'HostFilesGuest.exe').write_bytes(helper)
            (component / 'build-manifest.json').write_text(json.dumps({'files': [
                {'name': 'HostFilesGuest.exe', 'sha256': hashlib.sha256(helper).hexdigest()}]}))
            medium = mock.MagicMock()
            medium.hold.return_value.__enter__.return_value = []
            private_nonce = 'f' * 32
            with mock.patch.object(files, 'load_settings', return_value=files.DEFAULTS), \
                    mock.patch('file_share_carrier.Medium', return_value=medium) as carrier, \
                    mock.patch.object(files.subprocess, 'Popen') as process:
                session = files.Session(root, 'a' * 32, clipboard_port=45678, clipboard_nonce=private_nonce)
                try:
                    self.assertEqual(session.config['clipboard'], {'enabled': True, 'port': 27844, 'nonce': private_nonce})
                    self.assertTrue(session.network)
                    self.assertNotIn(private_nonce, json.dumps(session.public) + repr(session.arguments))
                    self.assertNotIn(private_nonce.encode(), files.serialize_server_request(session.config, [], []))
                    carrier.assert_called_once_with(session.config, helper, base_directory=None)
                    process.assert_not_called()
                finally:
                    session.close(guest_stopped=True)

    def test_clipboard_session_rejects_missing_public_or_malformed_nonce_before_writes(self):
        files = self.module()
        with mock.patch.object(files, 'load_settings', return_value=files.DEFAULTS), \
                mock.patch('file_share_carrier.Medium') as carrier, \
                mock.patch.object(files.subprocess, 'Popen') as process:
            for nonce in (None, 'a' * 32, 'F' * 32, 'f' * 31, 'f' * 33, 123):
                with self.subTest(nonce=nonce), self.assertRaisesRegex(ValueError, 'private handshake nonce'):
                    files.Session(Path('unused-fixture'), 'a' * 32, clipboard_port=45678, clipboard_nonce=nonce)
            carrier.assert_not_called()
            process.assert_not_called()

    def test_cleanup_session_requires_only_verified_guest_helper(self):
        files = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component = root / 'runtime/host-files'
            component.mkdir(parents=True)
            helper = b'MZfixture-guest-cleanup'
            (component / 'HostFilesGuest.exe').write_bytes(helper)
            (component / 'HostFiles.exe').write_bytes(b'broken-unused-server')
            (component / 'build-manifest.json').write_text(json.dumps({'files': [
                {'name': 'HostFilesGuest.exe', 'sha256': hashlib.sha256(helper).hexdigest()},
                {'name': 'HostFiles.exe', 'sha256': '0' * 64}]}))
            medium = mock.MagicMock()
            medium.hold.return_value.__enter__.return_value = ['-drive', 'fixture-carrier']
            with mock.patch.object(files, 'load_settings', return_value=files.DEFAULTS), \
                    mock.patch('file_share_carrier.Medium', return_value=medium) as carrier, \
                    mock.patch.object(files.subprocess, 'Popen') as process:
                session = files.Session(root, 'a' * 32)
                self.assertEqual(session.config['shares'], [])
                self.assertFalse(session.network)
                self.assertIn('fixture-carrier', session.arguments)
                self.assertIn('ringbuf,id=wcfilesreceipt,size=32768', session.arguments)
                carrier.assert_called_once_with(session.config, helper, base_directory=None)
                process.assert_not_called()
                session.close(guest_stopped=True)
                medium.cleanup.assert_called_once_with(guest_stopped=True)
                (component / 'HostFilesGuest.exe').write_bytes(b'MZtampered')
                with self.assertRaisesRegex(ValueError, 'integrity'):
                    files.Session(root, 'b' * 32)
                (component / 'HostFilesGuest.exe').unlink()
                with self.assertRaises(OSError):
                    files.Session(root, 'c' * 32)

    def test_nonempty_session_requires_all_server_components(self):
        files = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component = root / 'runtime/host-files'
            component.mkdir(parents=True)
            binaries = {name: ('MZfixture-' + name).encode('ascii') for name in
                        ('HostFiles.exe', 'HostFilesGuest.exe', 'SMBLibrary.dll', 'SMBLibrary.Win32.dll', 'Utilities.dll')}
            for name, data in binaries.items():
                (component / name).write_bytes(data)
            (component / 'build-manifest.json').write_text(json.dumps({'files': [
                {'name': name, 'sha256': hashlib.sha256(data).hexdigest()} for name, data in binaries.items()]}))
            share = {'name': 'PC_test', 'path': 'C:\\fixture', 'label': 'Fixture', 'preferred_letter': ''}
            with mock.patch.object(files, 'load_settings', return_value={'mode': 'selected', 'folders': ['C:\\fixture']}), \
                    mock.patch.object(files, 'protected_paths', return_value=['C:\\protected']), \
                    mock.patch.object(files, 'resolve_shares', return_value=([share], [])), \
                    mock.patch('file_share_carrier.Medium') as carrier, \
                    mock.patch.object(files.subprocess, 'Popen') as process:
                for name, data in binaries.items():
                    with self.subTest(missing=name):
                        (component / name).unlink()
                        try:
                            with self.assertRaises(OSError):
                                files.Session(root, 'a' * 32)
                        finally:
                            (component / name).write_bytes(data)
                carrier.assert_not_called()
                process.assert_not_called()

    def test_oversized_escaped_server_request_is_refused_before_save(self):
        files = app.file_sharing
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(app.runtime, 'engine_processes', return_value=[]), \
                    mock.patch.object(files, 'protected_paths', return_value=['C:\\fixture-program']):
                app.save_settings(root, dict(runtime_config.DEFAULTS, files={'mode': 'off'}))
                before = {path.name: path.read_bytes() for path in (root / 'config').iterdir()}
                oversized = {'mode': 'selected', 'folders': ['C:\\' + str(index) + '-' + 'Ж' * 180 for index in range(64)]}
                with self.assertRaisesRegex(ValueError, 'велик'):
                    app.save_settings(root, dict(runtime_config.DEFAULTS, work_memory_mib=3072, files=oversized))
                self.assertEqual({path.name: path.read_bytes() for path in (root / 'config').iterdir()}, before)

    def test_local_drives_include_ram_but_not_network_or_optical(self):
        files = self.module()
        kernel = mock.Mock()
        kernel.GetLogicalDrives.return_value = (1 << 6) - 1
        kernel.GetDriveTypeW.side_effect = lambda path: dict(A=2, B=3, C=4, D=5, E=6, F=0)[path[0]]
        with mock.patch.object(files.ctypes, 'WinDLL', return_value=kernel):
            self.assertEqual(files.local_drives(), ['A:\\', 'B:\\', 'E:\\'])

    def test_server_request_boundary_counts_escaped_unicode_and_all_fields(self):
        files = self.module()
        protected = ['C:\\fixture-program', 'C:\\fixture-private', 'C:\\fixture-startup', 'C:\\Общий запуск']
        config = {'schema': 1, 'session_nonce': 'b' * 32, 'username': 'WCP_' + 'c' * 24, 'password': 'd' * 43}
        folders = ['C:\\' + str(index) + '-' + 'Ж' * 155 for index in range(64)]
        def payload():
            return files.serialize_server_request(config, [{'name': files._share_name(path), 'path': path} for path in folders], protected)
        available = 65536 - len(payload())
        self.assertGreater(available, 0)
        for index in range(available):
            folders[index % 64] += 'x'
        self.assertTrue(all(len(path) < 255 for path in folders))
        self.assertEqual(len(payload()), 65536)
        with mock.patch.object(files, 'protected_paths', return_value=protected), \
                mock.patch.object(files, 'local_drives', side_effect=AssertionError('No drive scan during save')):
            value = {'mode': 'selected', 'folders': folders}
            self.assertEqual(files.validate_saved_settings(Path('C:/fixture'), value)['folders'], folders)
            decoded = json.loads(payload())
            self.assertEqual(decoded['protected'], protected)
            self.assertEqual(len(decoded['shares']), 64)
            self.assertEqual(decoded['username'], config['username'])
            with tempfile.TemporaryDirectory() as temporary, \
                    mock.patch.object(app.file_sharing, 'protected_paths', return_value=protected), \
                    mock.patch.object(app.runtime, 'engine_processes', return_value=[]):
                root = Path(temporary)
                app.save_settings(root, dict(runtime_config.DEFAULTS, files=value))
                self.assertEqual(files.load_settings(root)['folders'], folders)
            folders[0] += 'x'
            with self.assertRaisesRegex(ValueError, 'велик'):
                files.validate_saved_settings(Path('C:/fixture'), value)
        with mock.patch.object(files, 'protected_paths', return_value=['C:\\' + 'Ж' * 11000]):
            with self.assertRaisesRegex(ValueError, 'велик'):
                files.validate_saved_settings(Path('C:/fixture'), {'mode': 'all'})

    def test_pure_settings_validation_does_not_read_root_or_protected_folders(self):
        files = self.module()
        with mock.patch.object(files, 'protected_paths', side_effect=AssertionError('Pure validation has no root')):
            self.assertEqual(files.validate_settings({'mode': 'selected', 'folders': ['C:\\fixture']})['folders'], ['C:\\fixture'])
            self.assertEqual(files.validate_saved_settings(Path('C:/fixture'), {'mode': 'off'}), files.DEFAULTS)

    def test_unicode_medium_round_trips_through_guest_reader(self):
        import file_share_carrier
        root = Path(__file__).resolve().parents[1]
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='unicode-files-') as temporary:
            output = Path(temporary)
            executable = output / 'GuestFilesRegression.exe'
            command = [str(compiler), '/nologo', '/target:exe', '/platform:x64', '/main:GuestFilesRegression',
                       '/reference:System.Web.Extensions.dll', '/reference:System.Windows.Forms.dll',
                       '/reference:Microsoft.CSharp.dll', '/out:' + str(executable),
                       *map(str, [root / 'guest/files/GuestFiles.cs', root / 'guest/GuestEnvironment.cs',
                                  *CLIPBOARD_SOURCES, root / 'tests/GuestFilesRegression.cs'])]
            build = subprocess.run(command, capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            for label, clipboard_enabled in ((label, enabled) for label in ('Я' * 128, '📁' * 128) for enabled in (False, True)):
                config = {'schema': 1, 'session_nonce': 'c' * 32, 'username': 'WCP_session',
                          'password': 'a' * 128, 'shares': [
                              {'name': 'PC_' + str(index), 'label': label, 'preferred_letter': ''}
                              for index in range(64)]}
                if clipboard_enabled:
                    config['clipboard'] = {'enabled': True, 'port': 27844, 'nonce': 'f' * 32}
                with self.subTest(label=label[:1], clipboard=clipboard_enabled):
                    medium = file_share_carrier.Medium(config, b'MZfixture', base_directory=output / 'private')
                    try:
                        with medium.hold():
                            path = medium.path / 'HOSTFILS.JSN'
                            full = json.dumps(config, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                            legacy_config = {key: value for key, value in config.items() if key != 'clipboard'}
                            legacy_bytes = json.dumps(legacy_config, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                            if len(full) > 32768 or clipboard_enabled:
                                self.assertLessEqual(path.stat().st_size, 32768)
                                if len(legacy_bytes) > 32768:
                                    legacy_config['shares'] = []
                                self.assertEqual(json.loads(path.read_bytes()), legacy_config)
                                self.assertEqual((medium.path / 'HOSTFIL2.JSN').read_bytes(), full)
                                if not legacy_config['shares']:
                                    legacy = subprocess.run([str(executable), str(path), '--legacy'], capture_output=True, timeout=90,
                                                            creationflags=subprocess.CREATE_NO_WINDOW)
                                    self.assertEqual(legacy.returncode, 0, (legacy.stdout + legacy.stderr).decode(errors='replace'))
                            else:
                                self.assertEqual(path.read_bytes(), full)
                                self.assertFalse((medium.path / 'HOSTFIL2.JSN').exists())
                            check = subprocess.run([str(executable), str(path), *(['--clipboard'] if clipboard_enabled else [])], capture_output=True, timeout=90,
                                                   creationflags=subprocess.CREATE_NO_WINDOW)
                            self.assertEqual(check.returncode, 0, (check.stdout + check.stderr).decode(errors='replace'))
                    finally:
                        medium.cleanup(guest_stopped=True)
            oversized = output / 'oversized.json'
            oversized.write_bytes(b' ' * 65537)
            check = subprocess.run([str(executable), str(oversized), '--reject'], capture_output=True, timeout=90,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(check.returncode, 0, (check.stdout + check.stderr).decode(errors='replace'))

    def test_oversized_medium_is_rejected_before_private_writes(self):
        import file_share_carrier
        config = {'session_nonce': 'c' * 32, 'shares': [], 'padding': 'я' * 32769}
        with mock.patch.object(file_share_carrier.private, '_api') as api, \
             mock.patch.object(file_share_carrier.private, '_private_base') as private_base:
            with self.assertRaisesRegex(ValueError, 'too large'):
                file_share_carrier.Medium(config, b'MZfixture', base_directory=Path(tempfile.gettempdir()) / 'unused-private')
            api.assert_not_called()
            private_base.assert_not_called()

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
                    sources = [root / 'guest/files/GuestFiles.cs', root / 'guest/GuestEnvironment.cs', *CLIPBOARD_SOURCES] if harness == 'GuestFilesRegression' else sorted((root / 'host/files').glob('*.cs'))
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
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(app.file_sharing, 'protected_paths', return_value=['C:\\fixture-program']), \
                mock.patch.object(app.runtime, 'engine_processes', return_value=[]):
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

    def test_missing_protected_folders_keep_their_paths(self):
        files = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            startup = root / 'missing-startup'
            with mock.patch.object(files, '_known_folder', return_value=startup):
                self.assertIn(str(startup), files.protected_paths(root))
            self.assertFalse(startup.exists())

    def test_unknown_protected_folder_stops_sharing(self):
        files = self.module()
        with mock.patch.object(files, '_known_folder', return_value=None):
            with self.assertRaisesRegex(ValueError, 'защищ'):
                files.protected_paths(Path(tempfile.gettempdir()))

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
            config = {'schema': 1, 'session_nonce': 'c' * 32, 'shares': [],
                      'clipboard': {'enabled': True, 'port': 27844, 'nonce': 'f' * 32}}
            medium = file_share_carrier.Medium(config, b'MZfixture',
                                              base_directory=Path(temporary) / 'private')
            acl = session_carrier.inspect_private_acl(medium.path / 'HOSTFILS.JSN')
            self.assertTrue(acl['protected'])
            self.assertTrue(session_carrier.inspect_private_acl(medium.path / 'HOSTFIL2.JSN')['protected'])
            self.assertEqual(json.loads((medium.path / 'HOSTFIL2.JSN').read_bytes()), config)
            self.assertNotIn('clipboard', json.loads((medium.path / 'HOSTFILS.JSN').read_bytes()))
            self.assertNotIn(('f' * 32).encode(), (medium.path / 'HOSTFILS.JSN').read_bytes())
            with medium.hold() as arguments:
                self.assertNotIn('f' * 32, repr(arguments))
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

    def test_qemu_opens_file_medium_below_unicode_directory(self):
        import file_share_carrier
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='qemu-files-') as temporary:
            medium = file_share_carrier.Medium({'schema': 1, 'session_nonce': 'd' * 32, 'shares': []}, b'MZfixture',
                                              base_directory=Path(temporary) / 'Папка-😀')
            try:
                with medium.hold() as arguments:
                    check = subprocess.run([str(root / 'runtime/qemu-11.1.0/qemu-img.exe'), 'info', '--output=json',
                                            'json:' + arguments[1]], capture_output=True, timeout=20,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(check.returncode, 0, (check.stdout + check.stderr).decode(errors='replace'))
                    self.assertEqual(json.loads(check.stdout)['format'], 'vvfat')
            finally:
                medium.cleanup(guest_stopped=True)

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
