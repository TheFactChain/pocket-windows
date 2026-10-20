import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/portable_app.py'
app = None
if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location('portable_app', SCRIPT)
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)


class PortableAppTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(app, 'EXE backend is missing')

    def facts(self, available=8192):
        return {'windows': True, 'x64': True, 'whpx': True, 'ram_total_mib': 16384,
                'ram_available_mib': available, 'admin': False}

    def test_clean_empty_copy_is_blocked_without_writing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = list(root.rglob('*'))
            report = app.preflight(root, host=self.facts())
            self.assertFalse(report['ok'])
            self.assertFalse(report['checks']['runtime_files']['passed'])
            self.assertFalse(report['checks']['guest_files']['passed'])
            self.assertEqual(list(root.rglob('*')), before)

    def test_images_without_acceptance_do_not_enable_networked_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'images').mkdir()
            for name in ('workvm-empty.qcow2', 'gateway-empty.qcow2'):
                (root / 'images' / name).write_bytes(b'fixture')
            report = app.preflight(root, host=self.facts())
            self.assertFalse(report['checks']['network_acceptance']['passed'])
            self.assertFalse(report['ok'])

    def test_low_available_memory_is_an_explicit_blocker(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = app.preflight(Path(temporary), host=self.facts(512))
            self.assertFalse(report['checks']['memory']['passed'])
            self.assertIn('6144', report['checks']['memory']['detail'])

    def test_diagnostics_never_expose_saved_profile_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / 'private/profile/sing-box.json'
            profile.parent.mkdir(parents=True)
            original = json.dumps({'password': 'fixture-for-leak-check'}).encode()
            profile.write_bytes(original)
            report = app.preflight(root, host=self.facts())
            self.assertNotIn('fixture-for-leak-check', json.dumps(report))
            self.assertEqual(profile.read_bytes(), original)

    def test_settings_roundtrip_at_a_root_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='portable app ') as temporary:
            root = Path(temporary)
            saved = app.save_settings(root, {'schema': 1, 'lan_port': 32861,
                                             'work_memory_mib': 3072, 'gateway_memory_mib': 1024,
                                             'vcpus': 2})
            self.assertEqual(saved['lan_port'], 32861)
            self.assertEqual(app.load_settings(root), saved)

    def test_invalid_settings_do_not_replace_existing_public_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app.save_settings(root, {'schema': 1, 'lan_port': 32861})
            path = root / 'config/runtime.json'
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                app.save_settings(root, {'schema': 1, 'lan_port': 80})
            self.assertEqual(path.read_bytes(), before)

    def test_blocked_start_never_invokes_a_guest_or_powershell(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(app.subprocess, 'Popen', side_effect=AssertionError('spawn')):
                result = app.start(Path(temporary))
            self.assertFalse(result['ok'])

    def test_settings_are_refused_while_startup_lock_is_held_without_any_qemu(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with app.runtime.ProjectLock(root) as acquired:
                self.assertTrue(acquired)
                with self.assertRaisesRegex(ValueError, 'запуск|startup'):
                    app.save_settings(root, {'schema': 1, 'lan_port': 32861})
            self.assertFalse((root / 'config/runtime.json').exists())


if __name__ == '__main__':
    unittest.main()
