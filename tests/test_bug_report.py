import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bug_report

REPORT_ID = 'a' * 32


class BugReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bug-report-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'Pocket Windows'
        self.logs = self.root / 'logs'
        launch = self.logs / 'runtime-launch-0123456789abcdef'
        guest = self.logs / 'guest-work-boot-20261031T000000Z'
        launch.mkdir(parents=True)
        guest.mkdir()
        (launch / 'receipt.json').write_text(json.dumps({'project': str(self.root), 'phase': 'error',
            'guests': {'work': {'state': str(guest / 'state.json')}}}), encoding='utf-8')
        (guest / 'state.json').write_text(json.dumps({'status': 'exited', 'arguments': ['-drive', 'file=secret-disk'],
            'process': {'image': str(self.root / 'runtime/qemu.exe')}, 'note': 'link hy2://top-secret-value@vps'}), encoding='utf-8')
        (guest / 'qmp-events.json').write_text('{broken', encoding='utf-8')
        (self.logs / 'events.jsonl').write_text(json.dumps({'code': 'stop_failed', 'message': 'C:\\Users\\Fixture\\x',
            'share': 'D:\\Клиенты\\Иванов', 'system': 'C:\\Windows\\System32\\kernel32.dll'}) + '\n', encoding='utf-8')
        (self.root / 'release-info.json').write_text(json.dumps({'release_id': '2026.10.31.2', 'source_commit': 'b' * 40}), encoding='utf-8')
        self.environment = mock.patch.dict(os.environ, {'USERPROFILE': 'C:\\Users\\Fixture', 'USERNAME': 'Fixture', 'COMPUTERNAME': 'FIXTURE-PC'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def collect(self, **request):
        with mock.patch.object(bug_report, 'connection_secrets', return_value={'top-secret-value'}):
            return bug_report.collect(self.root, {'report_id': REPORT_ID, 'what': 'Зависло выключение', **request}, {'ok': False, 'message': 'x'})

    def read(self, status):
        with zipfile.ZipFile(self.root / status['zip']) as archive:
            return {name: archive.read(name).decode('utf-8', errors='replace') for name in archive.namelist()}

    def test_package_keeps_history_but_no_secret_path_or_arguments(self):
        files = self.read(self.collect())
        everything = '\n'.join(files.values())
        for private in ('top-secret-value', 'secret-disk', 'C:\\Users\\Fixture', str(self.root), 'FIXTURE-PC', 'Иванов'):
            self.assertNotIn(private, everything)
        self.assertIn('kernel32.dll', everything)
        self.assertIn('stop_failed', files['logs/events.jsonl'])
        self.assertIn('%USERPROFILE%', files['logs/events.jsonl'])
        state = json.loads(files['logs/guest-work-boot-20261031T000000Z/state.json'])
        self.assertEqual(state['arguments'], '<removed>')
        self.assertEqual(json.loads(files['report.json'])['what'], 'Зависло выключение')

    def test_manifest_tells_missing_broken_and_unavailable_apart(self):
        manifest = json.loads(self.read(self.collect())['manifest.json'])
        status = {item['source']: item['status'] for item in manifest['sources']}
        self.assertEqual(status['logs/events.1.jsonl'], 'missing')
        self.assertEqual(status['logs/guest-work-boot-20261031T000000Z/qmp-events.json'], 'ok')  # kept as redacted text
        self.assertTrue(any(item['status'] == 'error' and 'invalid json' in item.get('reason', '') for item in manifest['sources']))
        self.assertEqual(status['windows'], 'unavailable')
        self.assertTrue(all(len(item['sha256']) == 64 for item in manifest['files']))

    def test_limit_skips_sources_and_names_them(self):
        with mock.patch.object(bug_report, 'MAX_REPORT_BYTES', 600):
            manifest = json.loads(self.read(self.collect())['manifest.json'])
        self.assertLessEqual(manifest['raw_bytes'], 600)
        self.assertTrue(any(item['status'] == 'skipped_limit' for item in manifest['sources']))

    def test_description_is_required_and_only_three_reports_stay(self):
        with self.assertRaises(ValueError):
            bug_report.collect(self.root, {'report_id': REPORT_ID, 'what': '  '})
        for number in range(5):
            bug_report.collect(self.root, {'report_id': str(number) * 32, 'what': 'x'})
        self.assertEqual(len(list((self.logs / 'reports').iterdir())), bug_report.KEEP_REPORTS)

    def test_images_must_be_small_png_or_jpeg(self):
        text = self.root / 'note.txt'
        text.write_text('not an image')
        picture = self.root / 'shot.png'
        picture.write_bytes(b'\x89PNG fixture')
        files = self.read(self.collect(images=[str(text), str(picture)]))
        self.assertIn('images/2.png', files)
        self.assertNotIn('images/1.txt', files)


if __name__ == '__main__':
    unittest.main()
