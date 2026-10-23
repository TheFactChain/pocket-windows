"""Offline helper fixtures; never attach a disk or execute a guest payload."""
import base64
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/Install-GuestHelper.ps1'
POWERSHELL = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'


class InstallGuestHelperTests(unittest.TestCase):
    def fixture(self, body):
        with tempfile.TemporaryDirectory(prefix='guest-helper-') as temporary:
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
$mount=Join-Path $root 'mounted-windows'
$profile=Join-Path $mount 'Users\Alice'
$null=New-Item -ItemType Directory -Path (Join-Path $mount 'Windows\System32\config'),$profile -Force
[IO.File]::WriteAllText((Join-Path $mount 'Windows\System32\config\SYSTEM'),'fixture')
[IO.File]::WriteAllText((Join-Path $profile 'NTUSER.DAT'),'fixture')
$helper=Join-Path $root 'HostFilesGuest.exe'
[IO.File]::WriteAllText($helper,'helper v1')
$directory=Join-Path $profile 'AppData\Local\WindowsClaude\HostFiles'
$startup=Join-Path $profile 'AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\WindowsClaude Host Files.lnk'
BODY
Write-Output 'PASS offline fixtures'
'''.replace('SOURCE', "'" + str(SOURCE).replace("'", "''") + "'").replace(
                'ROOT', "'" + temporary.replace("'", "''") + "'").replace('BODY', body)
            result = subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-EncodedCommand',
                                     base64.b64encode(command.encode('utf-16le')).decode()],
                                    capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
            self.assertIn(b'PASS offline fixtures', result.stdout)

    def test_install_update_and_repeat_preserve_old_binary_and_guest_paths(self):
        self.fixture(r'''
foreach($name in @('Default','Public')) {
    $special=Join-Path $mount ('Users\'+$name);$null=New-Item -ItemType Directory -Path $special
    [IO.File]::WriteAllText((Join-Path $special 'NTUSER.DAT'),'fixture')
}
$null=New-Item -ItemType Directory -Path (Join-Path $mount 'ProgramData') -Force
$null=New-Item -ItemType Junction -Path (Join-Path $mount 'Users\Все пользователи') -Target (Join-Path $mount 'ProgramData')
Install-OfflineProfiles $mount $helper
foreach($name in @('Default','Public')) {
    if(Test-Path (Join-Path $mount ('Users\'+$name+'\AppData'))){throw 'Special profile changed'}
}
$first=@(Get-ChildItem -LiteralPath $directory -Filter '*.exe')
if($first.Count -ne 1){throw 'Expected one binary'}
$shell=New-Object -ComObject WScript.Shell
$link=$shell.CreateShortcut($startup)
if($link.TargetPath -ne ('C:\Users\Alice\AppData\Local\WindowsClaude\HostFiles\'+$first[0].Name) -or $link.Arguments -cne '--apply'){throw 'Wrong guest startup'}
$stamp=(Get-Item -LiteralPath $startup).LastWriteTimeUtc
Install-OfflineProfiles $mount $helper
if((Get-Item -LiteralPath $startup).LastWriteTimeUtc -ne $stamp){throw 'Idempotent invocation rewrote startup'}
[IO.File]::WriteAllText($helper,'helper v2')
Install-OfflineProfiles $mount $helper
if(@(Get-ChildItem -LiteralPath $directory -Filter '*.exe').Count -ne 2 -or [IO.File]::ReadAllText($first[0].FullName) -cne 'helper v1'){throw 'Previous binary lost'}
$link=$shell.CreateShortcut($startup)
if($link.TargetPath.EndsWith($first[0].Name)){throw 'Startup was not updated'}
if(Test-Path (Join-Path $mount 'ProgramData\Microsoft\Windows\Start Menu\Programs\Startup\WindowsClaude Host Files.lnk')){throw 'Duplicate common startup'}
''')

    def test_foreign_shortcut_refused_before_any_profile_is_modified(self):
        self.fixture(r'''
$null=New-Item -ItemType Directory -Path (Split-Path $startup) -Force
$shell=New-Object -ComObject WScript.Shell
$link=$shell.CreateShortcut($startup);$link.TargetPath='C:\Windows\notepad.exe';$link.Save()
$before=(Get-FileHash -LiteralPath $startup).Hash
$refused=$false;try{Install-OfflineProfiles $mount $helper}catch{$refused=$true}
if(-not $refused -or (Get-FileHash -LiteralPath $startup).Hash -cne $before -or (Test-Path $directory)){throw 'Foreign startup was modified'}
''')

    def test_reparse_profile_and_destination_are_refused(self):
        self.fixture(r'''
$outside=Join-Path $root 'outside';$null=New-Item -ItemType Directory -Path $outside
$null=New-Item -ItemType Junction -Path (Join-Path $profile 'AppData') -Target $outside
$refused=$false;try{Install-OfflineProfiles $mount $helper}catch{$refused=$true}
if(-not $refused -or @(Get-ChildItem $outside).Count){throw 'Reparse destination accepted'}
''')

    def test_mount_is_bound_to_attached_non_host_image(self):
        self.fixture(r'''
$image=Join-Path $root 'copy.vhdx';[IO.File]::WriteAllText($image,'fixture')
$global:attached=$true;$global:hostDisk=$false;$global:access=$mount+'\'
function Get-DiskImage {param($ImagePath) [pscustomobject]@{Attached=$global:attached}}
function Get-Disk {param([Parameter(ValueFromPipeline=$true)]$InputObject) process {[pscustomobject]@{Number=100;IsBoot=$global:hostDisk;IsSystem=$false}}}
function Get-Partition {param($DiskNumber) [pscustomobject]@{AccessPaths=@($global:access);IsBoot=$false;IsSystem=$false;GptType='{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}'}}
if((Assert-OfflineMount $mount $image) -ne $mount){throw 'Valid native mapping refused'}
foreach($badRoot in @($env:SystemRoot,($env:SystemDrive+'\'),'relative',$root)) {
    $refused=$false;try{Assert-OfflineMount $badRoot $image}catch{$refused=$true}
    if(-not $refused){throw 'Unsafe root accepted'}
}
$global:attached=$false
$refused=$false;try{Assert-OfflineMount $mount $image}catch{$refused=$true}
if(-not $refused){throw 'Detached image accepted'}
$global:attached=$true;$global:hostDisk=$true
$refused=$false;try{Assert-OfflineMount $mount $image}catch{$refused=$true}
if(-not $refused){throw 'Host disk accepted'}
$global:hostDisk=$false;$global:access='Z:\unrelated\'
$refused=$false;try{Assert-OfflineMount $mount $image}catch{$refused=$true}
if(-not $refused){throw 'Unrelated partition accepted'}
$otherMount=Join-Path $root 'other\mounted-windows'
$global:access=$otherMount+'\'
$refused=$false;try{Assert-OfflineMount $otherMount $image}catch{$refused=$true}
if(-not $refused){throw 'Mount outside image directory accepted'}
''')

    def test_wrong_owned_arguments_and_corrupt_existing_payload_refused(self):
        self.fixture(r'''
Install-OfflineProfiles $mount $helper
$payload=Get-ChildItem -LiteralPath $directory -Filter '*.exe'
[IO.File]::WriteAllText($payload.FullName,'corrupted')
$refused=$false;try{Install-OfflineProfiles $mount $helper}catch{$refused=$true}
if(-not $refused -or [IO.File]::ReadAllText($payload.FullName) -cne 'corrupted'){throw 'Conflicting payload overwritten'}
[IO.File]::WriteAllText($payload.FullName,'helper v1')
$shell=New-Object -ComObject WScript.Shell
$link=$shell.CreateShortcut($startup);$link.Arguments='--install';$link.Save()
$refused=$false;try{Install-OfflineProfiles $mount $helper}catch{$refused=$true}
if(-not $refused){throw 'Wrong arguments accepted'}
''')

    def test_common_startup_is_not_duplicated(self):
        self.fixture(r'''
$common=Join-Path $mount 'ProgramData\Microsoft\Windows\Start Menu\Programs\Startup\WindowsClaude Host Files.lnk'
$null=New-Item -ItemType Directory -Path (Split-Path $common) -Force
[IO.File]::WriteAllText($common,'existing common handler')
$refused=$false;try{Install-OfflineProfiles $mount $helper}catch{$refused=$true}
if(-not $refused -or (Test-Path $directory) -or [IO.File]::ReadAllText($common) -cne 'existing common handler'){throw 'Common startup duplicated or changed'}
''')


if __name__ == '__main__':
    unittest.main()
