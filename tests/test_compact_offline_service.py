"""Exercise offline-service path guards without mounting images or loading hives."""
import base64
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/Service-CompactMaster.ps1'
POWERSHELL = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'


class OfflineServiceGuards(unittest.TestCase):
    def run_guard_fixture(self, body):
        with tempfile.TemporaryDirectory(prefix='offline-service-guards-') as temporary:
            command = r'''
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$env:PSModulePath=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile(SOURCE,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors|Out-String)}
foreach($fn in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) {
    . ([scriptblock]::Create($fn.Extent.Text))
}
$root=ROOT
BODY
Write-Output 'PASS guards; no image mount or registry load executed'
'''.replace('SOURCE', "'" + str(SOURCE).replace("'", "''") + "'").replace(
                'ROOT', "'" + temporary.replace("'", "''") + "'").replace('BODY', body)
            encoded = base64.b64encode(command.encode('utf-16le')).decode()
            result = subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                    capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
            self.assertIn(b'PASS guards', result.stdout)

    def test_only_explicit_operation_under_compact_copy_is_allowed(self):
        self.run_guard_fixture(r'''
$nonce='a'*32
$valid=Join-Path $root ('compact-master-fixture\automatic-maintenance-'+$nonce)
$invalid=Join-Path $root ('ready-original\automatic-maintenance-'+$nonce)
$null=New-Item -ItemType Directory -Path $valid,$invalid -Force
if((Assert-OperationCopy $valid $nonce) -ne $valid){throw 'Valid copy was rejected'}
foreach($entry in @(@($invalid,$nonce),@($valid,'bad'),@('relative',$nonce))) {
    $refused=$false;try{Assert-OperationCopy $entry[0] $entry[1]}catch{$refused=$true}
    if(-not $refused){throw 'Unsafe operation accepted'}
}
''')

    def test_file_through_reparse_ancestor_is_refused(self):
        self.run_guard_fixture(r'''
$real=Join-Path $root 'real';$link=Join-Path $root 'link'
$null=New-Item -ItemType Directory -Path $real
[IO.File]::WriteAllText((Join-Path $real 'payload'),'fixture')
$null=New-Item -ItemType Junction -Path $link -Target $real
$refused=$false
try{Assert-PlainFile (Join-Path $link 'payload')}catch{$refused=$true}
if(-not $refused){throw 'Reparse ancestor was accepted'}
''')

    def test_collection_requires_successful_matching_report(self):
        self.run_guard_fixture(r'''
$nonce='a'*32;$hash='b'*64
$installed=[pscustomobject]@{schema=1;nonce=$nonce;script_sha256=$hash}
$result=[pscustomobject]@{schema=1;nonce=$nonce;script_sha256=$hash;success=$false;exit_code=1;report_file=$null}
if(Assert-MaintenanceResult $result $installed $nonce $root){throw 'Failed cleanup was accepted'}
$result.success=$true;$result.exit_code=0
$result.report_file='C:\ProgramData\WindowsClaude-compact-20261023-000001.json'
$rejected=$false;try{Assert-MaintenanceResult $result $installed $nonce $root}catch{$rejected=$true}
if(-not $rejected){throw 'Missing cleanup report was accepted'}
$report=@{schema=1;success=$true;steps=@('hibernate-disabled','component-cleanup','windows-temp-older-than-7-days','update-download-cache','trim-enabled-and-retrim-complete')}
$report | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'WindowsClaude-compact-20261023-000001.json')
if(-not (Assert-MaintenanceResult $result $installed $nonce $root)){throw 'Matching success refused'}
$result.script_sha256='c'*64
$rejected=$false;try{Assert-MaintenanceResult $result $installed $nonce $root}catch{$rejected=$true}
if(-not $rejected){throw 'Wrong script identity accepted'}
''')

    def test_hive_replacement_preserves_snapshot_and_refuses_changed_input(self):
        self.run_guard_fixture(r'''
class HiveFixture {
    [void]Save([string]$path){[IO.File]::WriteAllText($path,'maintained')}
}
$mounted=Join-Path $root 'mounted';$snapshot=Join-Path $root 'registry-before-install'
$null=New-Item -ItemType Directory -Path $mounted,$snapshot
foreach($name in @('SYSTEM','SYSTEM.LOG1')) {
    [IO.File]::WriteAllText((Join-Path $mounted $name),'original')
    Copy-Item -LiteralPath (Join-Path $mounted $name) -Destination (Join-Path $snapshot $name)
}
Save-MaintenanceHive ([HiveFixture]::new()) $root 'install' (Join-Path $mounted 'SYSTEM') $mounted ('a'*32)
if([IO.File]::ReadAllText((Join-Path $mounted 'SYSTEM')) -cne 'maintained'){throw 'Replacement failed'}
if(Test-Path -LiteralPath (Join-Path $mounted 'SYSTEM.LOG1')){throw 'Old transaction log remains active'}
if([IO.File]::ReadAllText((Join-Path $snapshot 'SYSTEM')) -cne 'original'){throw 'Snapshot was changed'}
Remove-Item -LiteralPath (Join-Path $root 'SYSTEM-maintained-install')
[IO.File]::WriteAllText((Join-Path $mounted 'SYSTEM'),'changed')
$refused=$false
try{Save-MaintenanceHive ([HiveFixture]::new()) $root 'install' (Join-Path $mounted 'SYSTEM') $mounted ('b'*32)}catch{$refused=$true}
if(-not $refused -or [IO.File]::ReadAllText((Join-Path $mounted 'SYSTEM')) -cne 'changed'){throw 'Changed mounted hive was overwritten'}
''')

    def test_resume_file_cleanup_is_bound_to_the_owned_mount(self):
        self.run_guard_fixture(r'''
$nonce='a'*32
$operation=Join-Path $root ('compact-master-fixture\automatic-maintenance-'+$nonce)
$mounted=Join-Path $operation 'mounted-windows'
$null=New-Item -ItemType Directory -Path $mounted -Force
$resume=Join-Path $mounted 'hiberfil.sys'
[IO.File]::WriteAllText($resume,'fixture, never a real hibernation file')
$global:aclCalls=0
function takeown.exe {$global:aclCalls++;$global:LASTEXITCODE=0}
function icacls.exe {$global:aclCalls++;$global:LASTEXITCODE=0}
$refused=$false
try{Remove-MaintenanceResumeFile $operation $nonce $root}catch{$refused=$true}
if(-not $refused -or $global:aclCalls -ne 0 -or -not (Test-Path -LiteralPath $resume)){throw 'Wrong mount affected files'}
Remove-MaintenanceResumeFile $operation $nonce $mounted
if((Test-Path -LiteralPath $resume) -or $global:aclCalls -ne 2){throw 'Owned resume file was not removed'}
Remove-MaintenanceResumeFile $operation $nonce $mounted
if($global:aclCalls -ne 2){throw 'Absent file triggered ACL changes'}
''')


if __name__ == '__main__':
    unittest.main()
