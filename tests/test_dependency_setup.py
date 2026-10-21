import hashlib
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/Prepare-Dependencies.ps1'


class DependencySetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.files = {'runtime/python-3.12.14/python.exe': b'python fixture',
                      'runtime/qemu-11.1.0/qemu-system-x86_64.exe': b'qemu fixture'}
        manifest = {'files': [{'path': name, 'bytes': len(data),
                               'sha256': hashlib.sha256(data).hexdigest()}
                              for name, data in self.files.items()]}
        (self.root / 'runtime').mkdir()
        (self.root / 'runtime/components-manifest.json').write_text(json.dumps(manifest))
        self.package()

    def package(self, entries=None):
        with zipfile.ZipFile(self.root / 'runtime/components-recovery.zip', 'w') as archive:
            for name, data in (entries or self.files).items():
                archive.writestr(name, data)

    def run_setup(self):
        self.assertTrue(SCRIPT.exists(), 'offline component preparation script is missing')
        script = self.root / 'Prepare-Dependencies.ps1'
        script.write_bytes(SCRIPT.read_bytes())
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                                 str(script), '-Project', str(self.root)],
                                capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return json.loads(result.stdout.decode('utf-8-sig'))

    def test_missing_and_corrupt_components_are_repaired_then_ready_without_rewrites(self):
        target = self.root / next(iter(self.files))
        target.parent.mkdir(parents=True)
        target.write_bytes(b'corrupt')
        report = self.run_setup()
        self.assertEqual(report['code'], 'repaired')
        self.assertEqual(report['repaired_files'], 2)
        timestamps = {}
        for name, data in self.files.items():
            self.assertEqual((self.root / name).read_bytes(), data)
            timestamps[name] = (self.root / name).stat().st_mtime_ns
        report = self.run_setup()
        self.assertEqual(report['code'], 'ready')
        self.assertEqual(report['repaired_files'], 0)
        self.assertEqual(timestamps, {name: (self.root / name).stat().st_mtime_ns for name in self.files})

    def test_corrupted_package_cannot_publish_even_one_component(self):
        entries = dict(self.files)
        entries[list(entries)[1]] = b'broken'
        self.package(entries)
        self.assertEqual(self.run_setup()['code'], 'invalid_package')
        self.assertFalse((self.root / next(iter(self.files))).exists())
        self.assertEqual(list((self.root / 'runtime').glob('.repair-*')), [])

    def test_unsafe_or_duplicate_archive_entries_are_rejected(self):
        for extra in ('../outside.txt', 'runtime/python-3.12.14/../../outside.txt',
                      'runtime/python-3.12.14/PYTHON.exe'):
            with self.subTest(extra=extra):
                self.package(dict(self.files, **{extra: b'bad'}))
                self.assertEqual(self.run_setup()['code'], 'invalid_package')
                self.assertFalse((self.root / next(iter(self.files))).exists())

    def test_existing_launcher_lock_refuses_repairs(self):
        state = self.root / 'state'
        state.mkdir()
        with (state / 'runtime.lock').open('w+b') as stream:
            stream.write(b'\0')
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                self.assertEqual(self.run_setup()['code'], 'runtime_busy')
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        self.assertFalse((self.root / next(iter(self.files))).exists())

    def test_missing_package_reports_actionable_code(self):
        (self.root / 'runtime/components-recovery.zip').unlink()
        self.assertEqual(self.run_setup()['code'], 'package_missing')

    def test_standalone_runtime_manifest_repairs_without_baseline_or_private_entries(self):
        self.assertFalse((self.root / 'BASELINE-MANIFEST.json').exists())
        manifest = json.loads((self.root / 'runtime/components-manifest.json').read_text())
        self.assertEqual({record['path'] for record in manifest['files']}, set(self.files))
        self.assertEqual(self.run_setup()['code'], 'repaired')

    def test_missing_standalone_manifest_requests_complete_distribution(self):
        (self.root / 'runtime/components-manifest.json').unlink()
        self.assertEqual(self.run_setup()['code'], 'package_missing')

    def test_symlink_archive_entry_is_rejected_without_repairs(self):
        name = next(iter(self.files))
        with zipfile.ZipFile(self.root / 'runtime/components-recovery.zip', 'w') as archive:
            entry = zipfile.ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            archive.writestr(entry, self.files[name])
            other = list(self.files)[1]
            archive.writestr(other, self.files[other])
        self.assertEqual(self.run_setup()['code'], 'invalid_package')
        self.assertFalse((self.root / name).exists())

    def test_junction_destination_is_rejected_and_outside_file_stays_intact(self):
        outside = self.root / 'outside'
        outside.mkdir()
        sentinel = outside / 'python.exe'
        sentinel.write_bytes(b'keep outside')
        link = self.root / 'runtime/python-3.12.14'
        result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(outside)],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        try:
            self.assertFalse(self.run_setup()['ok'])
            self.assertEqual(sentinel.read_bytes(), b'keep outside')
        finally:
            os.rmdir(link)

    def test_running_process_from_this_runtime_refuses_repair(self):
        executable = self.root / 'runtime/qemu-11.1.0/qemu-system-x86_64.exe'
        executable.parent.mkdir(parents=True)
        source = 'class BusyFixture { static void Main() { System.Threading.Thread.Sleep(60000); } }'
        assembly = str(executable).replace("'", "''")
        compile_result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                         "Add-Type -TypeDefinition '" + source + "' -OutputAssembly '" +
                                         assembly + "' -OutputType ConsoleApplication"],
                                        capture_output=True, timeout=30)
        self.assertEqual(compile_result.returncode, 0, compile_result.stderr.decode(errors='replace'))
        process = subprocess.Popen([str(executable)], creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.assertEqual(self.run_setup()['code'], 'runtime_busy')
            self.assertFalse((self.root / next(iter(self.files))).exists())
        finally:
            process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
