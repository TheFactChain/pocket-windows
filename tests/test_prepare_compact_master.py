"""No VM or cleanup execution: exercise path/metadata guards and PowerShell AST."""
import base64
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import prepare_compact_master as maintenance


class MaintenanceTests(unittest.TestCase):
    def test_cache_skips_access_denied_but_preserves_unexpected_io_errors(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tests', prefix='cache-regression-') as temporary:
            code = r'''
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile(SCRIPT,[ref]$tokens,[ref]$errors)
foreach($fn in $ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) {
    . ([scriptblock]::Create($fn.Extent.Text))
}
$root=ROOT
$blocked=Join-Path $root 'protected-cache';$normal=Join-Path $root 'normal-cache'
[IO.File]::WriteAllText($blocked,'protected');[IO.File]::WriteAllText($normal,'old cache')
$global:unexpected=$false
function Remove-Item {
    [CmdletBinding()]param([string]$LiteralPath,[switch]$Force)
    if($LiteralPath -eq $blocked) {
        if($global:unexpected){throw [IO.IOException]::new('fixture unexpected disk failure')}
        throw [UnauthorizedAccessException]::new('fixture protected cache')
    }
    Microsoft.PowerShell.Management\Remove-Item -LiteralPath $LiteralPath -Force:$Force
}
$skipped=Clear-Cache $root ([datetime]::MaxValue)
if($skipped -ne 1 -or -not [IO.File]::Exists($blocked) -or [IO.File]::Exists($normal)){throw 'Cache handling lost a protected file or did not clean an accessible file'}
$fresh=Join-Path $root 'fresh-with-old-file-date'
[IO.File]::WriteAllText($fresh,'recent extraction')
[IO.File]::SetLastWriteTime($fresh,(Get-Date).AddDays(-30))
$null=Clear-Cache $root (Get-Date).AddDays(-7)
if(-not [IO.File]::Exists($fresh)){throw 'A recently created cache file was deleted because of its old embedded date'}
$global:unexpected=$true;$refused=$false
try{Clear-Cache $root ([datetime]::MaxValue)}catch{if($_.Exception.Message -notlike '*unexpected disk failure*'){throw};$refused=$true}
if(-not $refused){throw 'Unexpected IO failure was hidden'}
Write-Output 'cache handling PASS; protected files retained'
'''.replace('SCRIPT', "'" + str(ROOT / 'guest/maintenance/Compact-Windows.ps1').replace("'", "''") + "'").replace(
                'ROOT', "'" + temporary.replace("'", "''") + "'")
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                                     base64.b64encode(code.encode('utf-16le')).decode()], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('cache handling PASS', result.stdout)

    def test_original_ready_master_and_relative_paths_are_refused(self):
        with mock.patch.object(maintenance, 'plain_path', side_effect=lambda path, **kwargs: Path(path)):
            for name in ('ready-original', 'images', 'Моя версия'):
                with self.assertRaisesRegex(ValueError, 'compact-master'):
                    maintenance.validate_master(ROOT / name)
        for path in ('relative', str(ROOT / '..' / 'escape')):
            with self.assertRaises(ValueError):
                maintenance.plain_path(path)

    def test_reparse_paths_are_refused(self):
        info = SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        with mock.patch.object(Path, 'lstat', return_value=info):
            with self.assertRaisesRegex(RuntimeError, 'reparse'):
                maintenance.plain_path(ROOT)

    def test_backing_disks_are_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'standalone'):
            maintenance._validate_info({'format': 'qcow2', 'virtual-size': 1024,
                                        'backing-filename': 'original.qcow2'})

    def test_boot_arguments_use_only_the_explicit_copy_and_readonly_media(self):
        master = ROOT / 'compact-master-fixture'
        with mock.patch.object(maintenance, 'plain_path', side_effect=lambda path, **kwargs: Path(path)):
            args = maintenance.arguments(ROOT, master, {'vcpus': 2, 'work_memory_mib': 4096})
        self.assertEqual(args[args.index('-nic') + 1], 'none')
        self.assertEqual(args[args.index('-display') + 1], 'sdl,gl=off,window-close=off')
        self.assertIn('serial=WC-MAINTENANCE-COPY', next(value for value in args if 'type=1,' in value))
        self.assertTrue(any('discard=unmap,file=' + str(master / 'candidate.qcow2').replace('\\', '/') in value for value in args))
        self.assertTrue(any('readonly=on,file=fat:' in value for value in args))
        self.assertFalse(any('ready-' in value or 'workvm-empty' in value for value in args))
        self.assertNotIn('-qmp', args)

    def test_powershell_syntax_and_guard_without_cleanup_execution(self):
        script = ROOT / 'guest/maintenance/Compact-Windows.ps1'
        code = r'''
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$path = SCRIPT_PATH
$ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$guard = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-MaintenanceCopy' }, $true)
. ([scriptblock]::Create($guard.Extent.Text))
foreach ($case in @(@('HOST-PC', $true), @('WC-MAINTENANCE-COPY', $false), @('', $true))) {
    $rejected = $false
    try { Assert-MaintenanceCopy $case[0] $case[1] } catch { $rejected = $true }
    if (-not $rejected) { throw 'Unsafe guard accepted a host or missing confirmation' }
}
Assert-MaintenanceCopy 'WC-MAINTENANCE-COPY' $true
$location = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-WindowsLocation' }, $true)
. ([scriptblock]::Create($location.Extent.Text))
foreach ($case in @(@('C:\Windows','C:'),@('C:\WINDOWS','C:'),@('c:\windows','c:'))) {
    Assert-WindowsLocation $case[0] $case[1]
}
foreach ($case in @(@('D:\Windows','D:'),@('C:\Windows.old','C:'),@('C:\Windows\System32','C:'))) {
    $rejected = $false
    try { Assert-WindowsLocation $case[0] $case[1] } catch { $rejected = $true }
    if (-not $rejected) { throw 'Wrong Windows location was accepted' }
}
# Execute only the rejection path, with hardware discovery mocked before any writes.
function Get-CimInstance { [pscustomobject]@{ IdentifyingNumber = 'HOST-PC' } }
$rejected = $false
try { & ([scriptblock]::Create($ast.Extent.Text)) -ConfirmMaintenanceCopy } catch {
    if ($_.Exception.Message -notlike 'Refusing cleanup:*') { throw }
    $rejected = $true
}
if (-not $rejected) { throw 'Script did not stop on host hardware' }
Write-Output 'PS syntax and host/confirmation guards PASS; no cleanup executed'
'''.replace('SCRIPT_PATH', "'" + str(script).replace("'", "''") + "'")
        encoded = base64.b64encode(code.encode('utf-16-le')).decode('ascii')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('guards PASS', result.stdout)


if __name__ == '__main__':
    unittest.main()
