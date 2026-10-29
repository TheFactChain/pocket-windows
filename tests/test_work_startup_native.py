"""Check QEMU runstate on a diskless fixture, without booting Windows."""
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
    def exercise(self, nic, extra=(), engine=None):
        commands = [
            ('qmp_capabilities', {}), ('query-status', {}),
            ('set_link', {'name': nic, 'up': False}), ('query-status', {}),
        ]
        if nic == 'wcfilesnic':
            commands += [('cont', {}), ('query-status', {}), ('stop', {})]
        commands += [('quit', {})]
        process = subprocess.Popen([
            str(engine or ROOT / 'runtime/qemu-11.1.0/qemu-system-x86_64.exe'),
            '-machine', 'pc', '-accel', 'tcg', '-m', '32', '-S',
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
