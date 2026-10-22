import ast
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import windows_hardware as hardware
import reference_vm


class WindowsHardwareTests(unittest.TestCase):
    def test_reference_serial_is_preserved_in_a_single_system_record(self):
        nonce = 'a' * 32
        for template in (None, 'b' * 32):
            with self.subTest(template=template):
                args = reference_vm.arguments(ROOT / ('state/reference-' + nonce), nonce,
                                              {'vcpus': 2, 'work_memory_mib': 2048}, template_nonce=template)
                system = [args[i + 1] for i, value in enumerate(args) if value == '-smbios'
                          and args[i + 1].startswith('type=1,')]
                serial = 'WCMASTER-' + template if template else 'WCREFERENCE-' + nonce
                self.assertEqual(len(system), 1)
                self.assertIn(',serial=' + serial, system[0])
                self.assertEqual(args[args.index('-cpu') + 1].split(',')[0], 'Westmere')
                self.assertEqual(args[args.index('-smp') + 1], '2')
                self.assertEqual(args[args.index('-m') + 1], '2048')
                self.assertTrue(any(value.startswith('if=ide,index=0,format=qcow2,file=') for value in args))

    def test_system_and_disk_labels_do_not_replace_devices(self):
        args = hardware.arguments()
        self.assertIn('ide-hd.model=ST3160815AS', args)
        self.assertIn('Precision WorkStation T5500', args[1])
        self.assertNotIn('-device', args)
        self.assertNotIn('-drive', args)
        self.assertNotIn('serial=', args[1])
        self.assertFalse(any('uuid=' in value for value in args))

    def test_serial_cannot_inject_another_smbios_field(self):
        for value in ('', 'x,uuid=123', 'x\n', 'a' * 65, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hardware.arguments(value)

    def test_gateway_cpu_is_not_relabelled(self):
        tree = ast.parse((ROOT / 'scripts/control_guest_install.py').read_text(encoding='utf-8'))
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(name, ast.Name) and name.id == 'arguments' for name in node.targets))
        cpu = next(value for value in assignment.value.elts if isinstance(value, ast.IfExp)
                   and isinstance(value.body, ast.Attribute) and value.body.attr == 'CPU_MODEL')
        self.assertEqual(ast.unparse(cpu.test), 'work')
        self.assertEqual(cpu.orelse.value, 'Westmere')


if __name__ == '__main__':
    unittest.main()
