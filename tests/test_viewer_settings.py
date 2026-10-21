import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import portable_app as app
import runtime_config as config


class ViewerSettingsTests(unittest.TestCase):
    def test_defaults_and_strict_boolean_boundary(self):
        load = getattr(config, 'load_viewer_settings', None)
        self.assertTrue(callable(load), 'Viewer settings must be stored independently from VM resources')
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(load(Path(temporary)),
                             {'schema': 1, 'start_fullscreen': False, 'remember_window': True})
        for value in ({'start_fullscreen': 1}, {'remember_window': 'yes'}, {'schema': True},
                      {'schema': 2}, {'password': 'private'}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.validate_viewer_settings(value)

    def test_save_viewer_settings_without_changing_hidden_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            viewer = {'schema': 1, 'start_fullscreen': True, 'remember_window': False}
            value = dict(config.DEFAULTS, gateway_memory_mib=768, lan_port=32861, viewer=viewer)
            saved = app.save_settings(root, value)
            self.assertEqual(saved['gateway_memory_mib'], 768)
            self.assertEqual(saved['lan_port'], 32861)
            self.assertEqual(json.loads((root / 'config/viewer.json').read_bytes()), viewer)
            self.assertNotIn('viewer', json.loads((root / 'config/runtime.json').read_bytes()))
            before = (root / 'config/runtime.json').read_bytes()
            with self.assertRaises(ValueError):
                app.save_settings(root, dict(value, viewer={'start_fullscreen': 'yes'}))
            self.assertEqual((root / 'config/runtime.json').read_bytes(), before)
            self.assertEqual(config.load_viewer_settings(root), viewer)

    def test_recovery_keeps_invalid_viewer_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'config/viewer.json'
            path.parent.mkdir()
            invalid = b'{invalid viewer settings\r\n'
            path.write_bytes(invalid)
            value = dict(config.DEFAULTS, viewer={'start_fullscreen': True})
            app.save_settings(root, value)
            backups = list(path.parent.glob('viewer.invalid-*.json'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), invalid)
            self.assertTrue(config.load_viewer_settings(root)['start_fullscreen'])


if __name__ == '__main__':
    unittest.main()
