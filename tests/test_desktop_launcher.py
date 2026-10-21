import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class DesktopLauncherTests(unittest.TestCase):
    def test_reported_visual_regressions(self):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        for name in ('DesktopShellRegression', 'SettingsLayoutRegression', 'ConnectionLayoutRegression',
                     'DesktopPanelLifecycleRegression'):
            with self.subTest(harness=name), tempfile.TemporaryDirectory(prefix='desktop-reported-') as temporary:
                executable = Path(temporary) / (name + '.exe')
                command = [str(compiler), '/nologo', '/target:exe', '/platform:x64', '/main:' + name,
                           '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
                           '/reference:System.Web.Extensions.dll',
                           '/win32manifest:' + str(ROOT / 'ui/app.manifest'), '/out:' + str(executable),
                           *map(str, sorted((ROOT / 'ui').glob('*.cs'))), str(ROOT / 'tests' / (name + '.cs'))]
                build = subprocess.run(command, capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
                result = subprocess.run([str(executable), temporary], capture_output=True, timeout=45,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))

    def test_lifecycle_wiring_and_timeout_budgets(self):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='desktop-lifecycle-wiring-') as temporary:
            executable = Path(temporary) / 'DesktopLifecycleWiring.exe'
            command = [str(compiler), '/nologo', '/target:exe', '/platform:x64',
                       '/main:DesktopLifecycleWiring', '/reference:System.Windows.Forms.dll',
                       '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll',
                       '/win32manifest:' + str(ROOT / 'ui/app.manifest'), '/out:' + str(executable),
                       *map(str, sorted((ROOT / 'ui').glob('*.cs'))), str(ROOT / 'tests/DesktopLifecycleWiring.cs')]
            build = subprocess.run(command, capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            result = subprocess.run([str(executable)], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))

    def test_profile_mutation_refresh_preserves_committed_result(self):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='desktop-profile-refresh-') as temporary:
            executable = Path(temporary) / 'DesktopProfileRefresh.exe'
            command = [str(compiler), '/nologo', '/target:exe', '/platform:x64',
                       '/main:DesktopProfileRefresh', '/reference:System.Windows.Forms.dll',
                       '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll',
                       '/win32manifest:' + str(ROOT / 'ui/app.manifest'), '/out:' + str(executable),
                       *map(str, sorted((ROOT / 'ui').glob('*.cs'))), str(ROOT / 'tests/DesktopProfileRefresh.cs')]
            build = subprocess.run(command, capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            result = subprocess.run([str(executable)], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))

    def test_native_profile_workflow_with_real_temporary_backend(self):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='desktop-profiles-') as temporary:
            root = Path(temporary)
            backend = root / 'portable QA'
            (backend / 'scripts').mkdir(parents=True)
            for source in (ROOT / 'scripts').glob('*.py'):
                shutil.copyfile(source, backend / 'scripts' / source.name)
            shutil.copytree(ROOT / 'runtime/python-3.12.14', backend / 'runtime/python-3.12.14')
            executable = root / 'DesktopProfileIntegration.exe'
            command = [str(compiler), '/nologo', '/target:exe', '/platform:x64',
                       '/main:DesktopProfileIntegration', '/reference:System.Windows.Forms.dll',
                       '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll',
                       '/win32manifest:' + str(ROOT / 'ui/app.manifest'),
                       '/win32icon:' + str(ROOT / 'ui/AppIcon.ico'), '/out:' + str(executable),
                       *map(str, sorted((ROOT / 'ui').glob('*.cs'))), str(ROOT / 'tests/DesktopProfileIntegration.cs')]
            build = subprocess.run(command, capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            result = subprocess.run([str(executable), str(backend)], capture_output=True, timeout=45,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))

    def test_four_pages_and_profile_workflow(self):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='desktop-pages-') as temporary:
            executable = Path(temporary) / 'DesktopPagesRegression.exe'
            command = [str(compiler), '/nologo', '/target:exe', '/platform:x64',
                       '/main:DesktopPagesRegression', '/reference:System.Windows.Forms.dll',
                       '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll',
                       '/win32manifest:' + str(ROOT / 'ui/app.manifest'),
                       '/win32icon:' + str(ROOT / 'ui/AppIcon.ico'), '/out:' + str(executable),
                       *map(str, sorted((ROOT / 'ui').glob('*.cs'))), str(ROOT / 'tests/DesktopPagesRegression.cs')]
            build = subprocess.run(command, capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            result = subprocess.run([str(executable), str(ROOT / 'logs')], capture_output=True, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))

    def test_async_state_and_process_lifecycle(self):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='desktop-async-') as temporary:
            executable = Path(temporary) / 'DesktopAsyncRegression.exe'
            command = [str(compiler), '/nologo', '/target:exe', '/platform:x64',
                       '/main:DesktopAsyncRegression', '/reference:System.Windows.Forms.dll',
                       '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll',
                       '/win32manifest:' + str(ROOT / 'ui/app.manifest'),
                       '/win32icon:' + str(ROOT / 'ui/AppIcon.ico'),
                       '/out:' + str(executable), *map(str, sorted((ROOT / 'ui').glob('*.cs'))),
                       str(ROOT / 'tests/DesktopAsyncRegression.cs')]
            build = subprocess.run(command, capture_output=True, timeout=45,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            result = subprocess.run([str(executable), temporary], capture_output=True, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))

    def test_headings_and_controls_fit_enlarged_text(self):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='desktop-layout-') as temporary:
            executable = Path(temporary) / 'DesktopLayoutRegression.exe'
            command = [str(compiler), '/nologo', '/target:exe', '/platform:x64',
                       '/main:DesktopLayoutRegression', '/reference:System.Windows.Forms.dll',
                       '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll',
                       '/win32manifest:' + str(ROOT / 'ui/app.manifest'),
                       '/win32icon:' + str(ROOT / 'ui/AppIcon.ico'),
                       '/out:' + str(executable), *map(str, sorted((ROOT / 'ui').glob('*.cs'))),
                       str(ROOT / 'tests/DesktopLayoutRegression.cs')]
            build = subprocess.run(command, capture_output=True, timeout=45,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            result = subprocess.run([str(executable), str(ROOT / 'logs')], capture_output=True,
                                    timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))

    def test_compiled_exe_controls_root_and_native_argument_roundtrip(self):
        source = ROOT / 'ui/Launcher.cs'
        self.assertTrue(source.is_file(), 'Desktop launcher implementation is missing')
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        self.assertTrue(compiler.is_file(), 'Installed .NET Framework compiler is required')
        with tempfile.TemporaryDirectory(prefix='desktop portable ') as temporary:
            root = Path(temporary)
            executable = root / 'Launcher.exe'
            powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            command = [str(powershell), '-NoProfile', '-NonInteractive', '-File',
                       str(ROOT / 'scripts/Build-Launcher.ps1'), '-OutputDirectory', str(root)]
            build = subprocess.run(command, capture_output=True, timeout=45,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            report = root / 'self-test.json'
            elsewhere = root / 'different working directory'
            elsewhere.mkdir()
            result = subprocess.run([str(executable), '--self-test', str(report)], cwd=elsewhere,
                                    capture_output=True, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
            data = json.loads(report.read_text(encoding='utf-8-sig'))
            self.assertTrue(all(data['checks'].values()), data)
            self.assertEqual(Path(data['project_root']), root)
            self.assertEqual(set(data['buttons']), {'Главная', 'Подключение', 'Настройки', 'Диагностика'})
            self.assertEqual(list(elsewhere.iterdir()), [])

    def test_offscreen_ui_regressions(self):
        compiler = Path(os.environ['SystemRoot']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        with tempfile.TemporaryDirectory(prefix='desktop-ui-') as temporary:
            executable = Path(temporary) / 'DesktopUiRegression.exe'
            repair_root = Path(temporary) / 'portable без Python'
            component = 'runtime/python-3.12.14/python.exe'
            content = b'component recovery fixture'
            (repair_root / 'runtime').mkdir(parents=True)
            (repair_root / 'scripts').mkdir()
            (repair_root / 'runtime/components-manifest.json').write_text(json.dumps({
                'schema': 1, 'files': [{'path': component, 'bytes': len(content),
                                      'sha256': hashlib.sha256(content).hexdigest()}]
            }), encoding='utf-8')
            with zipfile.ZipFile(repair_root / 'runtime/components-recovery.zip', 'w') as archive:
                archive.writestr(component, content)
            shutil.copyfile(ROOT / 'scripts/Prepare-Dependencies.ps1',
                            repair_root / 'scripts/Prepare-Dependencies.ps1')
            (repair_root / 'scripts/Prepare-Host.ps1').write_text(
                'param([string]$Root)\n'
                '[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)\n'
                '@{schema=1;ok=$true;code="restart_required";message="QA only";checked_root=$Root} | ConvertTo-Json\n',
                encoding='utf-8-sig')
            command = [str(compiler), '/nologo', '/target:exe', '/platform:x64',
                       '/main:DesktopUiRegression', '/reference:System.Windows.Forms.dll',
                       '/reference:System.Drawing.dll', '/reference:System.Web.Extensions.dll',
                       '/win32manifest:' + str(ROOT / 'ui/app.manifest'),
                       '/win32icon:' + str(ROOT / 'ui/AppIcon.ico'),
                       '/out:' + str(executable), *map(str, sorted((ROOT / 'ui').glob('*.cs'))),
                       str(ROOT / 'tests/DesktopUiRegression.cs')]
            build = subprocess.run(command, capture_output=True, timeout=45,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(build.returncode, 0, (build.stdout + build.stderr).decode(errors='replace'))
            result = subprocess.run([str(executable), str(ROOT / 'logs'), str(repair_root)], capture_output=True,
                                    timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
            self.assertEqual((repair_root / component).read_bytes(), content)


if __name__ == '__main__':
    unittest.main()
