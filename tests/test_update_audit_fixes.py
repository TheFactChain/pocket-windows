"""Regressions from the 31.10.2026 update audit that need no network or VM."""
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import release_portable as release


def sharing_violation():
    error = PermissionError('in use')
    error.winerror = 32
    return error


class ReplaceRetryTests(unittest.TestCase):
    def test_transient_lock_is_waited_out(self):
        with mock.patch.object(release.os, 'replace', side_effect=[sharing_violation(), sharing_violation(), None]) as replace, \
                mock.patch.object(release.time, 'sleep'):
            release._replace('a', 'b')
        self.assertEqual(replace.call_count, 3)

    def test_persistent_lock_and_other_errors_still_fail(self):
        with mock.patch.object(release.os, 'replace', side_effect=sharing_violation()), \
                mock.patch.object(release.time, 'sleep'), self.assertRaises(PermissionError):
            release._replace('a', 'b', seconds=0)
        with mock.patch.object(release.os, 'replace', side_effect=FileNotFoundError()) as replace, self.assertRaises(FileNotFoundError):
            release._replace('a', 'b')
        self.assertEqual(replace.call_count, 1)


class SourceTests(unittest.TestCase):
    def test_release_refuses_placeholder_keys(self):
        self.assertEqual(len(release.pinned_keys()), 2)
        for keys in (['PENDING-KEYGEN-A'], ['QUJD']):
            with mock.patch.object(release, 'update_sources', return_value={'keys': keys}), self.assertRaises(ValueError):
                release.pinned_keys()

    def test_runtime_versions_are_named_once(self):
        import runtime_config
        bridge = (ROOT / 'launcher-wpf/Bridge.cs').read_text(encoding='utf-8')
        self.assertIn('PythonDir = "' + runtime_config.PYTHON.replace('/', '\\\\') + '"', bridge)
        for path in [*(ROOT / 'scripts').glob('*.py'), *(ROOT / 'launcher-wpf').glob('*.cs')]:
            if path.name not in ('runtime_config.py', 'Bridge.cs'):
                text = path.read_text(encoding='utf-8')
                for literal in ('python-3.12.14', 'qemu-11.1.0'):
                    self.assertNotIn(literal, text, path.name + ' must use runtime_config / Bridge.PythonDir')

    def test_release_urls_come_from_data(self):
        for name in ('scripts/release_portable.py', 'scripts/app_update_v2.py', 'scripts/app_download.py'):
            self.assertNotIn('github.com/', (ROOT / name).read_text(encoding='utf-8'), name + ' must read hosts from update_sources.json')
        self.assertTrue(release.asset_url('https://188.130.234.243:8443/x'))
        for url in ('http://github.com/x', 'https://u:p@github.com/x', 'https://github.com/x#y', None):
            self.assertFalse(release.asset_url(url))


if __name__ == '__main__':
    unittest.main()
