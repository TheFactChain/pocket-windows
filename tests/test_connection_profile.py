import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
try:
    import connection_profile as profiles
except ImportError:
    profiles = None


class ConnectionProfileTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(profiles, 'Connection profile implementation is missing')

    def test_manual_and_uri_have_same_normalized_result(self):
        manual = profiles.validate_profile({'name': 'Мой сервер', 'protocol': 'hysteria2',
            'host': 'EXAMPLE.com.', 'port': 443, 'tls_server_name': 'TLS.EXAMPLE.COM',
            'obfs': 'salamander', 'credentials': {'auth': 'user:p@ss', 'obfs_password': 'a&b'}})
        uri = profiles.import_profile_uri('hy2://user%3Ap%40ss@EXAMPLE.com/?sni=TLS.EXAMPLE.COM&obfs=salamander&obfs-password=a%26b#%D0%9C%D0%BE%D0%B9%20%D1%81%D0%B5%D1%80%D0%B2%D0%B5%D1%80')
        self.assertEqual(manual, uri)
        self.assertEqual(manual['host'], 'example.com')
        self.assertTrue(manual['udp_supported'])

    def test_tcp_proxies_and_escaped_credentials(self):
        for protocol in ('socks5', 'http', 'https'):
            value = profiles.import_profile_uri(protocol + '://name%40x:p%3Ass@127.0.0.1:1080#Proxy')
            self.assertFalse(value['udp_supported'])
            self.assertEqual(value['credentials'], {'username': 'name@x', 'password': 'p:ss'})
            self.assertEqual(value['tls_verify'], protocol == 'https')

    def test_bad_input_never_echoes_secret(self):
        for suffix in ('?insecure=1', '?sni=a&sni=b', '?pinSHA256=secret', '?obfs=gecko', '/extra', '?unknown=secret', '?sni=%FF', '?sni=%xx'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError) as caught:
                profiles.import_profile_uri('hy2://top-secret@example.com' + suffix)
            self.assertNotIn('top-secret', str(caught.exception))
        for uri in ('ss://top-secret@example.com', 'hy2://secret@[::1]:443', 'hy2://secret@host:1,2', 'http://host:80?x=y'):
            with self.assertRaises(ValueError):
                profiles.import_profile_uri(uri)

    def test_strict_bounds_and_allowlist(self):
        base = {'protocol': 'http', 'host': 'example.com', 'port': 8080}
        for change in ({'port': True}, {'port': '8080'}, {'port': 0}, {'port': 65536},
                       {'host': 'x/y'}, {'host': '127.01.0.1'}, {'host': '[::1]'},
                       {'command': 'secret'}, {'udp_supported': True}, {'tls_verify': True},
                       {'credentials': {'username': 'only'}}, {'name': 'x' * 129}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                profiles.validate_profile(base | change)
        with self.assertRaises(ValueError):
            profiles.import_profile_uri('hy2://' + 'x' * 8193)
        with self.assertRaises(ValueError):
            profiles.validate_profile(base | {'credentials': {'username': 'u', 'password': 'x' * 65536}})

    def test_idna_and_no_auth_proxy(self):
        value = profiles.validate_profile({'protocol': 'socks5', 'host': 'пример.рф', 'port': 1080})
        self.assertEqual(value['host'], 'xn--e1afmkfd.xn--p1ai')
        self.assertEqual(profiles.import_profile_uri('https://example.com')['port'], 443)

    def test_ambiguous_or_control_character_links_are_rejected(self):
        for uri in ('\x00http://example.com', 'http://example.com:', 'http://example.com...',
                    'http://u:p@x@example.com'):
            with self.subTest(uri=repr(uri)), self.assertRaises(ValueError):
                profiles.import_profile_uri(uri)


if __name__ == '__main__':
    unittest.main()
