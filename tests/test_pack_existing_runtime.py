import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/pack_existing_runtime.py'
spec = importlib.util.spec_from_file_location('pack_runtime', SCRIPT)
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)


class PackagingTests(unittest.TestCase):
    def test_roundtrip_exact_bytes_and_existing_output_refused(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            payload = root / 'payload'
            payload.write_bytes(bytes(range(256)) * 1024)
            target = root / 'clone.zip'
            manifest = pack.write_archive({'fixture.bin': payload}, target, 'a' * 40)
            self.assertEqual(pack.verify_archive(target), manifest)
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(archive.read(pack.PREFIX + 'fixture.bin'), payload.read_bytes())
            before = target.read_bytes()
            with self.assertRaises(FileExistsError):
                pack.write_archive({'fixture.bin': payload}, target, 'a' * 40)
            self.assertEqual(target.read_bytes(), before)

    def test_unexpected_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            payload = root / 'payload'
            payload.write_bytes(b'fixture')
            target = root / 'clone.zip'
            pack.write_archive({'fixture.bin': payload}, target, 'a' * 40)
            with zipfile.ZipFile(target, 'a') as archive:
                archive.writestr(pack.PREFIX + 'unexpected.txt', b'extra')
            with self.assertRaisesRegex(ValueError, 'member set'):
                pack.verify_archive(target)

    def test_unsafe_names_are_rejected(self):
        for name in ('../escape', '/absolute', 'C:/drive', 'folder\\escape'):
            self.assertFalse(pack.safe_name(name))
        self.assertTrue(pack.safe_name('images/workvm-empty.qcow2'))

    def test_source_read_lock_blocks_a_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'locked'
            path.write_bytes(b'fixture')
            with pack.locked_read(path) as reader:
                self.assertEqual(reader.read(), b'fixture')
                with self.assertRaises(PermissionError):
                    path.open('wb')
            self.assertEqual(path.read_bytes(), b'fixture')


if __name__ == '__main__':
    unittest.main()
