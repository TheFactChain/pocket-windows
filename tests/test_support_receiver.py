import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'support'))
import receiver

REPORT_ID = 'c' * 32


def package(report_id=REPORT_ID, what='Зависло выключение', extra=()):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('report.json', json.dumps({'report_id': report_id, 'what': what, 'contact': '@fixture'}))
        archive.writestr('manifest.json', json.dumps({'sources': [{'source': 'windows', 'status': 'unavailable'}]}))
        for name, data in extra:
            archive.writestr(name, data)
    return stream.getvalue()


class FakeGitHub:
    def __init__(self, fail_issue=0):
        self.calls, self.fail_issue = [], fail_issue

    def create_release(self, report_id):
        self.calls.append('release')
        return {'id': 7, 'html_url': 'https://github.com/o/r/releases/tag/report-' + report_id}

    def upload_asset(self, release_id, report_id, body):
        self.calls.append('asset')
        return {'id': 9}

    def create_issue(self, title, body):
        self.calls.append('issue')
        if self.fail_issue:
            self.fail_issue -= 1
            raise urllib.error.URLError('down')
        self.title, self.body = title, body
        return {'number': 42, 'html_url': 'https://github.com/o/r/issues/42'}


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='support-')
        self.addCleanup(self.temporary.cleanup)

    def test_retry_after_failure_continues_without_a_second_card(self):
        github = FakeGitHub(fail_issue=1)
        service = receiver.Receiver(self.temporary.name, github)
        with self.assertRaises(urllib.error.URLError):
            service.handle(REPORT_ID, package())
        self.assertEqual(service.handle(REPORT_ID, package())['issue_number'], 42)
        self.assertEqual(service.handle(REPORT_ID, package())['issue_number'], 42)
        self.assertEqual(github.calls, ['release', 'asset', 'issue', 'issue'])
        self.assertIn('@fixture', github.body)
        self.assertIn('windows (unavailable)', github.body)

    def test_garbage_foreign_or_unsafe_archives_are_refused(self):
        service = receiver.Receiver(self.temporary.name, FakeGitHub())
        for report_id, body in (('nothex', package()), (REPORT_ID, b'not a zip'), (REPORT_ID, package(report_id='d' * 32)),
                                (REPORT_ID, package(what='  ')), (REPORT_ID, package(extra=[('../escape.txt', 'x')]))):
            with self.subTest(report_id=report_id), self.assertRaises(receiver.Rejected):
                service.handle(report_id, body)
        self.assertEqual(list(Path(self.temporary.name).glob('*.zip')), [])

    def test_zip_bomb_and_rate_are_bounded(self):
        service = receiver.Receiver(self.temporary.name, FakeGitHub())
        with mock.patch.object(receiver, 'MAX_UNPACKED', 10), self.assertRaises(receiver.Rejected) as caught:
            service.handle(REPORT_ID, package())
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual([service.allow('1.2.3.4') for _ in range(receiver.RATE_LIMIT + 1)][-2:], [True, False])
        self.assertTrue(service.allow('5.6.7.8'))


class ReceiverHttpTests(unittest.TestCase):
    def test_post_returns_the_issue_and_errors_are_json(self):
        import threading
        import urllib.request
        with tempfile.TemporaryDirectory(prefix='support-http-') as directory:
            server = receiver.ThreadingHTTPServer(('127.0.0.1', 0), receiver.handler_for(receiver.Receiver(directory, FakeGitHub())))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown)
            base = 'http://127.0.0.1:' + str(server.server_address[1])
            body = package()
            request = urllib.request.Request(base + '/v1/reports', data=body, method='POST',
                                             headers={'X-Report-Id': REPORT_ID, 'Content-Type': 'application/zip'})
            with urllib.request.urlopen(request, timeout=10) as response:
                self.assertEqual(json.loads(response.read())['issue_number'], 42)
            wrong = urllib.request.Request(base + '/other', data=body, method='POST', headers={'X-Report-Id': REPORT_ID})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(wrong, timeout=10)
            self.assertEqual(caught.exception.code, 404)
            self.assertIn('error', json.loads(caught.exception.read()))


if __name__ == '__main__':
    unittest.main()
