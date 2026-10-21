import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/Prepare-Host.ps1'
POWERSHELL = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'


class HostPreparationTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SOURCE.is_file(), 'Host preparation script is missing')
        self.temporary = tempfile.TemporaryDirectory(prefix='host preparation ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'scripts').mkdir()
        self.script = self.root / 'scripts/Prepare-Host.ps1'
        self.script.write_bytes(SOURCE.read_bytes())

    def run_fixture(self, fixture):
        path = str(self.script).replace("'", "''")
        root = str(self.root).replace("'", "''")
        command = """
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$tokens=$null;$parseErrors=$null
$null=[Management.Automation.Language.Parser]::ParseFile('SCRIPT',[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'PowerShell 5.1 parse failure'}
. 'SCRIPT'
function Get-HostPreparationFacts { @{supported=$true;whpx=$false} }
function Get-HostPreparationBootId { '11111111111111111111111111111111' }
function Test-HostPreparationAdministrator { $true }
function Wait-HostPreparationProcess { param($Process) $true }
function Start-ElevatedHostPreparation {
    param($Root,$OperationId)
    $null=Invoke-HostPreparationElevated -Root $Root -OperationId $OperationId
    @{fixture=$true}
}
FIXTURE
$result=Invoke-HostPreparation -Root 'ROOT'
@{result=$result;files=@(Get-ChildItem -LiteralPath 'ROOT' -Recurse -File | ForEach-Object {$_.FullName})}|ConvertTo-Json -Depth 6 -Compress
""".replace('SCRIPT', path).replace('ROOT', root).replace('FIXTURE', fixture)
        encoded = base64.b64encode(command.encode('utf-16le')).decode('ascii')
        result = subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
        return json.loads(result.stdout.decode('utf-8-sig'))

    def test_dot_source_is_safe_and_ready_host_does_not_elevate_or_create_state(self):
        data = self.run_fixture("""
function Get-HostPreparationFacts { @{supported=$true;whpx=$true} }
function Start-ElevatedHostPreparation { throw 'must not elevate' }
function Invoke-HostPreparationDism { throw 'must not change Windows' }
""")
        self.assertEqual(data['result']['code'], 'ready')
        self.assertTrue(data['result']['ok'])
        self.assertFalse(data['result']['restart_required'])
        self.assertFalse((self.root / 'state').exists())

    def test_uac_cancellation_has_distinct_safe_result(self):
        data = self.run_fixture("""
function Start-ElevatedHostPreparation {
    throw [InvalidOperationException]::new('fixture secret',[ComponentModel.Win32Exception]::new(1223))
}
""")
        self.assertEqual(data['result']['code'], 'uac_cancelled')
        self.assertFalse(data['result']['ok'])
        self.assertNotIn('fixture secret', json.dumps(data))

    def test_dism_failure_and_restart_required_are_not_reported_as_ready(self):
        for exit_code, expected, okay in [(50, 'dism_failed', False), (3010, 'restart_required', True)]:
            with self.subTest(exit_code=exit_code):
                data = self.run_fixture('function Invoke-HostPreparationDism { ' + str(exit_code) + ' }')
                self.assertEqual(data['result']['code'], expected)
                self.assertEqual(data['result']['ok'], okay)
                self.assertEqual(data['result']['restart_required'], exit_code == 3010)

    def test_zero_exit_requires_actual_whpx_availability(self):
        data = self.run_fixture('function Invoke-HostPreparationDism { 0 }')
        self.assertEqual(data['result']['code'], 'feature_unavailable')
        self.assertFalse(data['result']['ok'])

    def test_dism_uses_only_system_tool_expected_feature_and_no_restart(self):
        data = self.run_fixture("""
function Start-Process {
    param($FilePath,$ArgumentList,$WindowStyle,[switch]$PassThru)
    if($FilePath -cne (Join-Path ([PortableHostPreparationNative]::WindowsDirectory()) 'System32/dism.exe')) {throw 'wrong executable'}
    if(($ArgumentList -join ' ') -cne '/Online /Enable-Feature /FeatureName:HypervisorPlatform /All /NoRestart') {throw 'wrong DISM arguments'}
    if($WindowStyle -ne 'Hidden' -or -not $PassThru) {throw 'wrong process options'}
    $process=[pscustomobject]@{ExitCode=3010}
    $process|Add-Member ScriptMethod WaitForExit {param($milliseconds) $true}
    $process|Add-Member ScriptMethod Dispose {}
    return $process
}
""")
        self.assertEqual(data['result']['code'], 'restart_required')

    def test_result_reparse_and_hardlinks_cannot_supply_success(self):
        for link_kind in ('SymbolicLink', 'HardLink'):
            fixture = """
function Start-ElevatedHostPreparation {
    param($Root,$OperationId)
    $outside=Join-Path $Root 'foreign-result.json'
    Write-HostPreparationFile $outside @{operation_id=$OperationId;result=(New-HostPreparationResult 'ready')}
    $path=Join-Path $Root ('state/host-prepare-'+$OperationId+'/result.json')
    $null=New-Item -ItemType LINKKIND -Path $path -Target $outside
    @{fixture=$true}
}
""".replace('LINKKIND', link_kind)
            with self.subTest(link_kind=link_kind):
                foreign = self.root / 'foreign-result.json'
                if foreign.exists():
                    foreign.unlink()
                data = self.run_fixture(fixture)
                self.assertEqual(data['result']['code'], 'invalid_result')

    def test_success_requires_whpx_ready_after_zero_exit(self):
        data = self.run_fixture("""
$script:probes=0
function Get-HostPreparationFacts {
    $script:probes++
    @{supported=$true;whpx=($script:probes -ge 3)}
}
function Invoke-HostPreparationDism { 0 }
""")
        self.assertEqual(data['result']['code'], 'ready')

    def test_malformed_or_wrong_operation_result_is_rejected(self):
        for contents in ('{broken', '{"operation_id":"00000000000000000000000000000000","result":{}}'):
            fixture = """
function Start-ElevatedHostPreparation {
    param($Root,$OperationId)
    $path=Join-Path $Root ('state/host-prepare-'+$OperationId+'/result.json')
    [IO.File]::WriteAllText($path,'CONTENTS')
    @{fixture=$true}
}
""".replace('CONTENTS', contents)
            with self.subTest(contents=contents):
                data = self.run_fixture(fixture)
                self.assertEqual(data['result']['code'], 'invalid_result')
                self.assertFalse(data['result']['ok'])

    def test_timeout_preserves_operation_and_never_terminates_child(self):
        data = self.run_fixture("""
function Start-ElevatedHostPreparation { @{fixture=$true} }
function Wait-HostPreparationProcess { param($Process) $false }
function Stop-Process { throw 'must not terminate preparation' }
""")
        self.assertEqual(data['result']['code'], 'outcome_unknown')
        self.assertTrue(any(Path(path).name == 'request.json' for path in data['files']))

    def test_timeout_survives_process_restart_and_does_not_launch_a_second_child(self):
        first = self.run_fixture("""
function Start-ElevatedHostPreparation { @{fixture=$true} }
function Wait-HostPreparationProcess { param($Process) $false }
""")
        self.assertEqual(first['result']['code'], 'outcome_unknown')
        second = self.run_fixture("""
function Start-ElevatedHostPreparation {
    param($Root,$OperationId)
    [IO.File]::WriteAllText((Join-Path $Root 'duplicate-launch.txt'),'unexpected')
    @{fixture=$true}
}
function Wait-HostPreparationProcess { param($Process) $false }
""")
        self.assertEqual(second['result']['code'], 'outcome_unknown')
        self.assertFalse((self.root / 'duplicate-launch.txt').exists())
        self.assertEqual(len([p for p in (self.root / 'state').glob('host-prepare-*') if p.is_dir()]), 1)

    def test_terminal_failure_allows_fresh_operation_on_explicit_retry(self):
        first = self.run_fixture('function Invoke-HostPreparationDism { 50 }')
        self.assertEqual(first['result']['code'], 'dism_failed')
        second = self.run_fixture('function Invoke-HostPreparationDism { 3010 }')
        self.assertEqual(second['result']['code'], 'restart_required')
        self.assertEqual(len([p for p in (self.root / 'state').glob('host-prepare-*') if p.is_dir()]), 2)

    def test_active_operation_lock_prevents_concurrent_creation(self):
        data = self.run_fixture("""
Initialize-HostPreparationNative
$state=Join-Path 'LOCKROOT' 'state'
$null=[IO.Directory]::CreateDirectory($state)
$script:heldActive=[PortableHostPreparationNative]::OpenActiveOperation((Join-Path $state 'host-prepare-active.json'))
function Start-ElevatedHostPreparation { throw 'must not elevate while locked' }
""".replace('LOCKROOT', str(self.root).replace("'", "''")))
        self.assertEqual(data['result']['code'], 'outcome_unknown')
        self.assertEqual([p for p in (self.root / 'state').glob('host-prepare-*') if p.is_dir()], [])

    def test_foreign_active_pointer_is_preserved_and_never_overwritten(self):
        state = self.root / 'state'
        state.mkdir()
        active = state / 'host-prepare-active.json'
        active.write_text('user data, not an operation')
        data = self.run_fixture("function Start-ElevatedHostPreparation { throw 'must not elevate' }")
        self.assertEqual(data['result']['code'], 'unsafe_path')
        self.assertEqual(active.read_text(), 'user data, not an operation')

    def test_new_boot_permits_explicit_retry_after_unknown_or_restart_required(self):
        for prior_code in ('outcome_unknown', 'restart_required', 'invalid_result'):
            with self.subTest(prior_code=prior_code):
                if prior_code == 'restart_required':
                    first_fixture = 'function Invoke-HostPreparationDism { 3010 }'
                elif prior_code == 'invalid_result':
                    first_fixture = """
function Start-ElevatedHostPreparation {
    param($Root,$OperationId)
    [IO.File]::WriteAllText((Join-Path $Root ('state/host-prepare-'+$OperationId+'/result.json')),'{broken')
    @{fixture=$true}
}
"""
                else:
                    first_fixture = """
function Start-ElevatedHostPreparation { @{fixture=$true} }
function Wait-HostPreparationProcess { param($Process) $false }
"""
                first = self.run_fixture(first_fixture)
                self.assertEqual(first['result']['code'], prior_code)
                second = self.run_fixture("""
function Get-HostPreparationBootId { '22222222222222222222222222222222' }
function Invoke-HostPreparationDism { 3010 }
""")
                self.assertEqual(second['result']['code'], 'restart_required')
                active = json.loads((self.root / 'state/host-prepare-active.json').read_text())
                self.assertEqual(active['boot_id'], '22222222222222222222222222222222')
                # Independent fixture scenario; preserve operation history but clear its test pointer.
                (self.root / 'state/host-prepare-active.json').unlink()

    def test_unavailable_boot_identifier_cannot_unlock_unknown_operation(self):
        first = self.run_fixture("""
function Start-ElevatedHostPreparation { @{fixture=$true} }
function Wait-HostPreparationProcess { param($Process) $false }
""")
        self.assertEqual(first['result']['code'], 'outcome_unknown')
        second = self.run_fixture("""
function Get-HostPreparationBootId { $null }
function Start-ElevatedHostPreparation {
    param($Root,$OperationId)
    [IO.File]::WriteAllText((Join-Path $Root 'unexpected-recovery.txt'),'must not launch')
    @{fixture=$true}
}
""")
        self.assertEqual(second['result']['code'], 'outcome_unknown')
        self.assertFalse((self.root / 'unexpected-recovery.txt').exists())

    def test_late_result_from_pending_operation_is_returned_without_another_launch(self):
        first = self.run_fixture("""
function Start-ElevatedHostPreparation { @{fixture=$true} }
function Wait-HostPreparationProcess { param($Process) $false }
""")
        self.assertEqual(first['result']['code'], 'outcome_unknown')
        operation = next(p for p in (self.root / 'state').glob('host-prepare-*') if p.is_dir())
        operation_id = operation.name.removeprefix('host-prepare-')
        fixture = """
Write-HostPreparationFile (Join-Path 'ROOTFIXTURE' 'state/host-prepare-OPERATION/result.json') @{operation_id='OPERATION';result=(New-HostPreparationResult 'restart_required')}
function Start-ElevatedHostPreparation { throw 'must not launch again' }
""".replace('ROOTFIXTURE', str(self.root).replace("'", "''")).replace('OPERATION', operation_id)
        second = self.run_fixture(fixture)
        self.assertEqual(second['result']['code'], 'restart_required')

    def test_unsupported_os_and_reparse_root_are_rejected_before_elevation(self):
        data = self.run_fixture("""
function Get-HostPreparationFacts { @{supported=$false;whpx=$false} }
function Start-ElevatedHostPreparation { throw 'must not elevate' }
""")
        self.assertEqual(data['result']['code'], 'unsupported_host')
        target = self.root / 'outside'
        target.mkdir()
        (self.root / 'state').symlink_to(target, target_is_directory=True)
        data = self.run_fixture("function Start-ElevatedHostPreparation { throw 'must not elevate' }")
        self.assertEqual(data['result']['code'], 'unsafe_path')
        self.assertEqual(list(target.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
