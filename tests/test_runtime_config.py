import importlib.util
import inspect
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/runtime_config.py'
config = None
if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location('runtime_config', SCRIPT)
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(config, 'Portable settings implementation is missing')

    def test_defaults_preserve_current_resources_and_private_lan(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = config.load_settings(Path(temporary))
        self.assertEqual(settings['work_memory_mib'], 4096)
        self.assertEqual(settings['gateway_memory_mib'], 1024)
        self.assertEqual(settings['vcpus'], 2)
        self.assertEqual(settings['lan_port'], 31861)

    def test_public_config_cannot_contain_profile_or_secret_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'config').mkdir()
            (root / 'config/runtime.json').write_text(json.dumps({'password': 'fixture'}))
            with self.assertRaisesRegex(ValueError, 'Unknown runtime setting'):
                config.load_settings(root)

    def test_configured_port_and_memory_are_loaded_relative_to_any_root(self):
        with tempfile.TemporaryDirectory(prefix='portable space ') as temporary:
            root = Path(temporary)
            (root / 'config').mkdir()
            value = {'schema': 1, 'lan_port': 32861, 'work_memory_mib': 3072}
            (root / 'config/runtime.json').write_text(json.dumps(value))
            settings = config.load_settings(root)
            self.assertEqual(settings['lan_port'], 32861)
            self.assertEqual(settings['work_memory_mib'], 3072)

    def test_invalid_or_boolean_integer_values_are_rejected(self):
        for key, value in [('lan_port', True), ('lan_port', 80), ('vcpus', 0),
                           ('work_memory_mib', 0), ('schema', 2)]:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'config').mkdir()
                (root / 'config/runtime.json').write_text(json.dumps({key: value}))
                with self.assertRaises(ValueError):
                    config.load_settings(root)

    def test_network_arguments_accept_a_configured_loopback_port(self):
        network_file = SCRIPT.with_name('runtime_network_plan.py')
        spec = importlib.util.spec_from_file_location('network_plan', network_file)
        network = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(network)
        self.assertIn('lan_port', inspect.signature(network.arguments).parameters,
                      'Network plan still has a fixed personal session port')
        result = network.arguments('gateway', 'logs/test', lan_port=32861)
        self.assertIn('addr.port=32861', ' '.join(result))
        self.assertNotIn('addr.port=31861', ' '.join(result))
        self.assertNotIn('hostfwd', ' '.join(result))


if __name__ == '__main__':
    unittest.main()
