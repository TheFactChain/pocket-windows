"""Check QEMU runstate on a diskless fixture, without booting Windows."""
import ast
import json
import os
from pathlib import Path
import subprocess
import queue
import threading
import time
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


@unittest.skipUnless(os.name == 'nt', 'requires bundled Windows QEMU')
class WorkStartupNative(unittest.TestCase):
    def exercise(self, nic, extra=(), engine=None, checks=(), machine='pc', accelerator='tcg'):
        commands = [
            ('qmp_capabilities', {}), ('query-status', {}),
            ('set_link', {'name': nic, 'up': False}), ('query-status', {}),
        ]
        if nic == 'wcfilesnic':
            commands += [('cont', {}), ('query-status', {}), ('stop', {})]
        commands += list(checks) + [('quit', {})]
        process = subprocess.Popen([
            str(engine or ROOT / 'runtime/qemu-11.1.0/qemu-system-x86_64.exe'),
            '-machine', machine, '-accel', accelerator, '-m', '32', '-S',
            '-L', 'runtime/qemu-11.1.0/share', '-display', 'none', '-nic', 'none',
            '-monitor', 'none', '-serial', 'none', '-qmp', 'stdio',
            '-netdev', 'user,id=wcfiles,restrict=on,ipv6=off',
            '-device', 'e1000,id=wcfilesnic,netdev=wcfiles',
            *extra,
        ], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
        inbox = queue.Queue()
        def read():
            for line in process.stdout:
                inbox.put(json.loads(line))
            inbox.put({'eof': True})
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            self.assertIn('QMP', inbox.get(timeout=10))
            replies = {}
            for index, (name, args) in enumerate(commands):
                process.stdin.write(json.dumps({'execute': name, 'arguments': args, 'id': index}) + '\n')
                process.stdin.flush()
                until = time.monotonic() + 10
                while True:
                    reply = inbox.get(timeout=max(0.01, until - time.monotonic()))
                    self.assertNotIn('eof', reply)
                    if reply.get('id') == index:
                        replies[index] = reply
                        break
            process.wait(timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr.read())
            return replies
        finally:
            if process.poll() is None:
                process.kill()  # This fixture has no disks or guest OS.
            process.wait(timeout=10)
            reader.join(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_cpu_stays_paused_until_link_is_disabled_and_cont_is_sent(self):
        replies = self.exercise('wcfilesnic')
        for index in (1, 3):
            self.assertFalse(replies[index]['return']['running'])
            self.assertIn(replies[index]['return']['status'], ('prelaunch', 'paused'))
        self.assertIn('return', replies[2])
        self.assertTrue(replies[5]['return']['running'])

    def test_failed_link_setup_can_exit_without_running_guest_cpu(self):
        replies = self.exercise('missing-nic')
        self.assertIn('error', replies[2])
        self.assertFalse(replies[3]['return']['running'])
        self.assertIn(replies[4].get('return'), ({}, []))

    def test_production_audio_devices_load_without_changing_the_existing_nic(self):
        tree = ast.parse((ROOT / 'scripts/control_guest_install.py').read_text(encoding='utf-8'))
        audio = []
        for node in ast.walk(tree):
            if isinstance(node, ast.AugAssign) and isinstance(node.value, ast.List):
                values = [item.value for item in node.value.elts if isinstance(item, ast.Constant)]
                if '-audiodev' in values:
                    audio = ast.literal_eval(node.value)
        checks = [('query-pci', {}), ('human-monitor-command', {'command-line': 'info qtree'})]
        before = self.exercise('wcfilesnic', checks=checks, machine='q35', accelerator='whpx')
        after = self.exercise('wcfilesnic', audio, checks=checks, machine='q35', accelerator='whpx')
        self.assertIn('dev: intel-hda', after[8]['return'])
        self.assertIn('dev: hda-output', after[8]['return'])
        self.assertNotIn('dev: hda-duplex', after[8]['return'])
        def nic_slot(reply):
            return next((bus['bus'], device['slot'], device['function'])
                        for bus in reply[7]['return'] for device in bus['devices']
                        if device.get('qdev_id') == 'wcfilesnic')
        self.assertEqual(nic_slot(before), nic_slot(after))

    def test_qemu_starts_with_unicode_file_medium_and_files_disabled(self):
        import file_share_carrier
        with tempfile.TemporaryDirectory(prefix='startup-medium-') as temporary:
            medium = file_share_carrier.Medium({'schema': 1, 'session_nonce': 'e' * 32, 'shares': []}, b'MZfixture',
                                              base_directory=Path(temporary) / 'Папка-😀')
            try:
                with medium.hold() as arguments:
                    replies = self.exercise('wcfilesnic', arguments)
                    self.assertTrue(replies[5]['return']['running'])
            finally:
                medium.cleanup(guest_stopped=True)


if __name__ == '__main__':
    unittest.main()
