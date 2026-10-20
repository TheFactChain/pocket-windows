import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/profile_templates.py'
profiles = None
if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location('profile_templates', SCRIPT)
    profiles = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(profiles)


class ProfileTemplateTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(profiles, 'Profile/template separation is missing')

    def test_explicit_endpoint_renders_a_closed_guest_policy(self):
        value = {'schema': 1, 'endpoint_ipv4': '93.184.216.34', 'endpoint_port': 4443}
        result = profiles.render_guest_files(ROOT / 'gateway', value)
        firewall = result['nftables.vpn.nft']
        self.assertEqual(firewall.count('policy drop;'), 3)
        self.assertIn('ip daddr 93.184.216.34 udp dport 4443', firewall)
        self.assertNotIn('{{', firewall)
        self.assertIn('93.184.216.34/32', result['interfaces'])
        self.assertNotIn('default', result['interfaces'])
        public = json.loads(result['profile-public.json'])
        self.assertEqual(public['endpoint_port'], 4443)
        self.assertNotIn('password', public)

    def test_missing_or_unsafe_endpoint_is_rejected_without_fallback(self):
        for value in ({}, {'schema': 1, 'endpoint_ipv4': '127.0.0.1', 'endpoint_port': 443},
                      {'schema': 1, 'endpoint_ipv4': '93.184.216.34; accept', 'endpoint_port': 443},
                      {'schema': 1, 'endpoint_ipv4': '93.184.216.34', 'endpoint_port': True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                profiles.render_guest_files(ROOT / 'gateway', value)

    def test_no_default_endpoint_or_secret_is_embedded_in_templates(self):
        firewall = (ROOT / 'gateway/nftables.vpn.template.nft').read_text()
        self.assertIn('{{VPS_IPV4}}', firewall)
        self.assertIn('{{VPS_PORT}}', firewall)
        self.assertNotIn('password', firewall.lower())


if __name__ == '__main__':
    unittest.main()
