import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/backup_working_basis.py'
backup = None
if SCRIPT.exists():
    spec = importlib.util.spec_from_file_location('backup_basis', SCRIPT)
    backup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def test_copies_and_verifies_exact_bytes_without_changing_source(self):
        self.assertIsNotNone(backup, 'Backup implementation is missing')
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            source = root / 'source'
            source.write_bytes(bytes(range(256)) * 4096)
            before = source.stat()
            target = root / 'backup'
            result = backup.copy_and_verify({'images/fixture.qcow2': source}, target)
            self.assertEqual((target / 'images/fixture.qcow2').read_bytes(), source.read_bytes())
            self.assertEqual(source.stat().st_mtime_ns, before.st_mtime_ns)
            self.assertTrue(result['verified'])
            self.assertEqual(result['files'][0]['bytes'], before.st_size)

    def test_existing_destination_is_never_overwritten(self):
        self.assertIsNotNone(backup, 'Backup implementation is missing')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.write_bytes(b'fixture')
            target = root / 'backup'
            target.mkdir()
            with self.assertRaises(FileExistsError):
                backup.copy_and_verify({'source': source}, target)
            self.assertEqual(list(target.iterdir()), [])

    def test_snapshot_lock_refuses_a_concurrent_writer(self):
        self.assertIsNotNone(backup, 'Backup implementation is missing')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'source'
            path.write_bytes(b'fixture')
            with backup.locked_read(path):
                with self.assertRaises(PermissionError):
                    path.open('wb')
            self.assertEqual(path.read_bytes(), b'fixture')


if __name__ == '__main__':
    unittest.main()
