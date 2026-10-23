from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import verify_master_helper as verify


class ReceiptParsingTests(unittest.TestCase):
    def test_firmware_text_on_com1_is_skipped(self):
        seen = []
        text = ('\x1b[2J\x1b[01;01HBdsDxe: loading Boot0005 "Windows Boot Manager"\r\n'
                'BdsDxe: starting Boot0005\r\n{"schema":1,"success":true}\n{"schema"')
        tail, receipt = verify.take_receipt(text, lambda line: seen.append(line) or {'line': line})
        self.assertEqual(seen, ['{"schema":1,"success":true}'])
        self.assertEqual(receipt, {'line': '{"schema":1,"success":true}'})
        self.assertEqual(tail, '{"schema"')

    def test_no_receipt_without_a_complete_json_line(self):
        self.assertEqual(verify.take_receipt('BdsDxe: x\r\n{"par', lambda line: line), ('{"par', None))

    def test_invalid_receipt_is_not_swallowed(self):
        def refuse(line):
            raise ValueError('Invalid guest settings receipt')
        with self.assertRaises(ValueError):
            verify.take_receipt('{"schema":2}\n', refuse)


if __name__ == '__main__':
    unittest.main()
