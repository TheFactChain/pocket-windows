import importlib.util
from pathlib import Path
import struct
import unittest


class RecoveryGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'scripts/recover_compact_hive.py'
        spec = importlib.util.spec_from_file_location('hive_recovery', source)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_transaction_sequence_must_be_complete(self):
        header = b'regf' + struct.pack('<II', 832, 831) + bytes(36)
        self.assertFalse(self.module.clean_header(header))
        self.assertTrue(self.module.clean_header(b'regf' + struct.pack('<II', 840, 840) + bytes(36)))

    def test_invalid_hive_is_refused(self):
        for data in (b'', bytes(48), b'regf'):
            with self.assertRaises(ValueError):
                self.module.clean_header(data)

    def test_only_isolated_copy_can_be_recovered(self):
        with self.assertRaises(ValueError):
            self.module.recover(Path(__file__).resolve().parent, 'install')


if __name__ == '__main__':
    unittest.main()
