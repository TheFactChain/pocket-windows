[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$WindowsRoot,
    [Parameter(Mandatory=$true)][string]$HelperPath,
    [Parameter(Mandatory=$true)][string]$ImagePath
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest

function Get-LocalAbsolutePath([string]$Path) {
    if($Path -notmatch '^[A-Za-z]:\\' -or $Path.IndexOf(':',2) -ge 0){throw 'An absolute local Windows path is required.'}
    return [IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Assert-PlainPath([string]$Path,[string]$Mount='') {
    $full=Get-LocalAbsolutePath $Path
    if($Mount -and $full -ne $Mount -and -not $full.StartsWith($Mount+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Path escapes mounted Windows.'}
    for($current=$full;$current;$current=Split-Path $current -Parent) {
        if(Test-Path -LiteralPath $current) {
            $item=Get-Item -LiteralPath $current -Force
            if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -and $current -ne $Mount){throw 'Reparse path refused.'}
        }
    }
}

function Assert-OfflineMount([string]$Root,[string]$Image) {
    $mount=Get-LocalAbsolutePath $Root
    $imageFile=Get-LocalAbsolutePath $Image
    if($mount -ine (Join-Path (Split-Path $imageFile -Parent) 'mounted-windows') -or
        $mount -eq $env:SystemDrive -or $mount -eq $env:SystemRoot){throw 'WindowsRoot must be mounted-windows beside the explicit VHDX copy.'}
    if([IO.Path]::GetExtension($imageFile) -ine '.vhdx'){throw 'Image must be an explicit local VHDX copy.'}
    Assert-PlainPath $imageFile
    if(-not (Test-Path -LiteralPath $imageFile -PathType Leaf)){throw 'Image copy does not exist.'}
    Assert-PlainPath $mount $mount
    $images=@(Get-DiskImage -ImagePath $imageFile)
    if($images.Count -ne 1 -or -not $images[0].Attached){throw 'The VHDX copy must already be attached.'}
    $disks=@($images[0] | Get-Disk)
    if($disks.Count -ne 1 -or $disks[0].IsBoot -or $disks[0].IsSystem){throw 'Host/system disks are refused.'}
    $parts=@(Get-Partition -DiskNumber $disks[0].Number | Where-Object {
        $_.AccessPaths -contains ($mount+'\')
    })
    if($parts.Count -ne 1 -or $parts[0].IsBoot -or $parts[0].IsSystem -or
        $parts[0].GptType -ine '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}'){throw 'Mount must identify one Windows data partition on the attached copy.'}
    $system=Join-Path $mount 'Windows\System32\config\SYSTEM'
    Assert-PlainPath $system $mount
    if(-not (Test-Path -LiteralPath $system -PathType Leaf)){throw 'Offline Windows SYSTEM hive is missing.'}
    return $mount
}

function Assert-OwnedStartup($Shell,[string]$Startup,[string]$GuestDirectory,[string]$Mount) {
    Assert-PlainPath $Startup $Mount
    if(-not (Test-Path -LiteralPath $Startup)){return ''}
    $link=$Shell.CreateShortcut($Startup)
    try {
        $target=[string]$link.TargetPath
        if($link.Arguments -cne '--apply' -or
            [IO.Path]::GetDirectoryName($target) -ine $GuestDirectory -or
            [IO.Path]::GetFileName($target) -inotmatch '^HostFilesGuest(?:-[A-F0-9]{64})?\.exe$'){throw 'SHORTCUT_NAME_CONFLICT'}
        Assert-PlainPath (Join-Path $Mount $target.Substring(3)) $Mount
        return $target
    } finally {[Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) | Out-Null}
}

function Install-OfflineProfiles([string]$Mount,[string]$Helper) {
    Assert-PlainPath $Helper
    if(-not (Test-Path -LiteralPath $Helper -PathType Leaf)){throw 'Built helper is missing.'}
    $hash=(Get-FileHash -LiteralPath $Helper -Algorithm SHA256).Hash
    $users=Join-Path $Mount 'Users'
    Assert-PlainPath $users $Mount
    $common=Join-Path $Mount 'ProgramData\Microsoft\Windows\Start Menu\Programs\Startup\WindowsClaude Host Files.lnk'
    Assert-PlainPath $common $Mount
    if(Test-Path -LiteralPath $common){throw 'Common startup already exists; refusing duplicate per-user handlers.'}
    $shell=New-Object -ComObject WScript.Shell
    try {
        $plans=@(foreach($profile in Get-ChildItem -LiteralPath $users -Directory -Force) {
            if($profile.Name -in @('Default','Public','Default User','All Users')){continue}
            $hive=Join-Path $profile.FullName 'NTUSER.DAT'
            if(-not (Test-Path -LiteralPath $hive -PathType Leaf)){continue}
            Assert-PlainPath $profile.FullName $Mount
            Assert-PlainPath $hive $Mount
            $directory=Join-Path $profile.FullName 'AppData\Local\WindowsClaude\HostFiles'
            $guestDirectory='C:\Users\'+$profile.Name+'\AppData\Local\WindowsClaude\HostFiles'
            $startup=Join-Path $profile.FullName 'AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\WindowsClaude Host Files.lnk'
            Assert-PlainPath $directory $Mount
            $oldTarget=Assert-OwnedStartup $shell $startup $guestDirectory $Mount
            $destination=Join-Path $directory ('HostFilesGuest-'+$hash+'.exe')
            Assert-PlainPath $destination $Mount
            if((Test-Path -LiteralPath $destination) -and (Get-FileHash -LiteralPath $destination).Hash -cne $hash){throw 'EXISTING_HELPER_CONFLICT'}
            [pscustomobject]@{Directory=$directory;Startup=$startup;Destination=$destination;Target=$guestDirectory+'\'+[IO.Path]::GetFileName($destination);OldTarget=$oldTarget}
        })
        if(-not $plans.Count){throw 'No existing ordinary local profiles with NTUSER.DAT found.'}
        foreach($plan in $plans) {
            Assert-PlainPath $plan.Directory $Mount
            $null=New-Item -ItemType Directory -Path $plan.Directory -Force
            Assert-PlainPath $plan.Directory $Mount
            if(-not (Test-Path -LiteralPath $plan.Destination)) {
                $temporary=Join-Path $plan.Directory ([Guid]::NewGuid().ToString('N')+'.tmp')
                try {
                    [IO.File]::Copy($Helper,$temporary,$false)
                    if((Get-FileHash -LiteralPath $temporary).Hash -cne $hash){throw 'HELPER_COPY_FAILED'}
                    [IO.File]::Move($temporary,$plan.Destination)
                } finally {if(Test-Path -LiteralPath $temporary){Remove-Item -LiteralPath $temporary}}
            }
            if($plan.OldTarget -ieq $plan.Target){continue}
            Assert-PlainPath $plan.Startup $Mount
            $null=New-Item -ItemType Directory -Path (Split-Path $plan.Startup) -Force
            $temporary=Join-Path (Split-Path $plan.Startup) ([Guid]::NewGuid().ToString('N')+'.lnk')
            $link=$null
            try {
                $link=$shell.CreateShortcut($temporary);$link.TargetPath=$plan.Target;$link.Arguments='--apply'
                $link.Description='Windows Claude host files';$link.Save()
                $null=Assert-OwnedStartup $shell $plan.Startup ([IO.Path]::GetDirectoryName($plan.Target)) $Mount
                if(Test-Path -LiteralPath $plan.Startup){[IO.File]::Replace($temporary,$plan.Startup,[Management.Automation.Language.NullString]::Value)}
                else {[IO.File]::Move($temporary,$plan.Startup)}
            } finally {
                if($null -ne $link){[Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) | Out-Null}
                if(Test-Path -LiteralPath $temporary){Remove-Item -LiteralPath $temporary}
            }
        }
        Write-Output ('Installed helper '+$hash+' for '+$plans.Count+' local profile(s). Previous binaries retained.')
    } finally {[Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) | Out-Null}
}

$mount=Assert-OfflineMount $WindowsRoot $ImagePath
Install-OfflineProfiles $mount (Get-LocalAbsolutePath $HelperPath)
