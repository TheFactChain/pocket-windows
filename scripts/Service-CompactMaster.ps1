[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Install','Collect')][string]$Mode,
    [Parameter(Mandatory=$true)][string]$Operation,
    [Parameter(Mandatory=$true)][string]$Nonce,
    [string]$MaintenanceScript
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest

function Assert-OperationCopy([string]$Directory,[string]$Id) {
    if($Id -cnotmatch '^[a-f0-9]{32}$' -or -not [IO.Path]::IsPathRooted($Directory) -or $Directory.StartsWith('\\')) {
        throw 'An absolute local maintenance operation and nonce are required.'
    }
    $full=[IO.Path]::GetFullPath($Directory)
    $item=Get-Item -LiteralPath $full -Force
    if(-not $item.PSIsContainer -or $item.Name -cne ('automatic-maintenance-'+$Id) -or
        -not $item.Parent.Name.StartsWith('compact-master-', [StringComparison]::Ordinal)) {
        throw 'Only an explicit automatic-maintenance operation in a compact-master copy is allowed.'
    }
    for($parent=$item;$null -ne $parent;$parent=$parent.Parent) {
        if($parent.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Operation ancestors cannot be reparse points.'}
    }
    return $full
}

function Assert-PlainFile([string]$Path,[string]$MountRoot='') {
    $item=Get-Item -LiteralPath $Path -Force
    if($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {throw 'A plain file is required.'}
    if($MountRoot -and -not $item.FullName.StartsWith($MountRoot+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Guest file escapes its mounted copy.'}
    if($item.PSObject.Properties['LinkType'] -and $item.LinkType -eq 'HardLink'){throw 'Hard-linked payloads are refused.'}
    for($parent=$item.Directory;$null -ne $parent;$parent=$parent.Parent) {
        if($MountRoot -and $parent.FullName -eq $MountRoot){break}
        if($parent.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'File ancestors cannot be reparse points.'}
    }
}

function Remove-MaintenanceResumeFile([string]$Root,[string]$Id,[string]$MountRoot) {
    $checked=Assert-OperationCopy $Root $Id
    if($MountRoot -cne (Join-Path $checked 'mounted-windows')){throw 'Resume-file cleanup requires this operation mount.'}
    $path=Join-Path $MountRoot 'hiberfil.sys'
    if(-not (Test-Path -LiteralPath $path)){return}
    Assert-PlainFile $path $MountRoot
    # A hybrid resume restores the old kernel cache and discards offline servicing changes.
    & takeown.exe /F $path /A | Out-Null
    if($LASTEXITCODE -ne 0){throw 'Cannot take ownership of the copy resume file.'}
    & icacls.exe $path /grant '*S-1-5-32-544:F' | Out-Null
    if($LASTEXITCODE -ne 0){throw 'Cannot remove the copy resume file safely.'}
    Remove-Item -LiteralPath $path -Force
}

function Assert-MaintenanceResult($Result,$Installed,[string]$Id,[string]$Reports) {
    if(($Result.schema -isnot [int] -and $Result.schema -isnot [long]) -or $Result.schema -ne 1 -or
        $Result.nonce -cne $Id -or $Installed.nonce -cne $Id -or
        $Result.script_sha256 -cnotmatch '^[a-f0-9]{64}$' -or $Result.script_sha256 -cne $Installed.script_sha256 -or
        $Result.success -isnot [bool] -or ($Result.exit_code -isnot [int] -and $Result.exit_code -isnot [long])) {
        throw 'Invalid or mismatched maintenance result.'
    }
    if(-not $Result.success){return $false}
    if($Result.exit_code -ne 0 -or $Result.report_file -cnotmatch '^C:\\ProgramData\\WindowsClaude-compact-[0-9]{8}-[0-9]{6}\.json$') {
        throw 'Successful maintenance needs exit code zero and a matching report path.'
    }
    $reportPath=Join-Path $Reports ([IO.Path]::GetFileName($Result.report_file))
    Assert-PlainFile $reportPath
    if((Get-Item -LiteralPath $reportPath).Length -gt 65536){throw 'Cleanup report is oversized.'}
    $report=Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
    if($report.schema -ne 1 -or $report.success -isnot [bool] -or -not $report.success){throw 'Cleanup report is not successful.'}
    foreach($step in @('hibernate-disabled','component-store-preserved','windows-temp-older-than-7-days','update-download-cache','trim-enabled-and-retrim-complete')) {
        if($report.steps -cnotcontains $step){throw ('Cleanup report lacks step: '+$step)}
    }
    return $true
}

function New-RecoveredHive([string]$Root,[string]$Phase,[string]$MountedHive,[string]$MountRoot) {
    $snapshot=Join-Path $Root ('registry-before-'+$Phase)
    if(Test-Path -LiteralPath $snapshot){throw 'Registry snapshot already exists; inspect it before retrying.'}
    $null=New-Item -ItemType Directory -Path $snapshot
    foreach($name in @('SYSTEM','SYSTEM.LOG','SYSTEM.LOG1','SYSTEM.LOG2')) {
        $path=Join-Path (Split-Path $MountedHive -Parent) $name
        if(Test-Path -LiteralPath $path) {
            Assert-PlainFile $path $MountRoot
            Copy-Item -LiteralPath $path -Destination (Join-Path $snapshot $name)
        }
    }
    $python=Join-Path (Split-Path $PSScriptRoot -Parent) 'runtime\python-3.12.14\python.exe'
    & $python -X utf8 -I -S -B (Join-Path $PSScriptRoot 'recover_compact_hive.py') --operation $Root --phase $Phase | Out-Host
    if($LASTEXITCODE -ne 0){throw 'Registry transaction replay failed; the mounted hive is untouched.'}
    return (Join-Path $snapshot 'SYSTEM.recovered')
}

function Save-MaintenanceHive($Registry,[string]$Root,[string]$Phase,[string]$MountedHive,[string]$MountRoot,[string]$Id) {
    $saved=Join-Path $Root ('SYSTEM-maintained-'+$Phase)
    $Registry.Save($saved)
    Assert-PlainFile $saved
    $snapshot=Join-Path $Root ('registry-before-'+$Phase)
    foreach($name in @('SYSTEM','SYSTEM.LOG','SYSTEM.LOG1','SYSTEM.LOG2')) {
        $path=Join-Path (Split-Path $MountedHive -Parent) $name
        $before=Join-Path $snapshot $name
        if((Test-Path -LiteralPath $path) -ne (Test-Path -LiteralPath $before)){throw 'Mounted registry inputs changed.'}
        if(Test-Path -LiteralPath $path) {
            Assert-PlainFile $path $MountRoot
            if((Get-FileHash -LiteralPath $path).Hash -cne (Get-FileHash -LiteralPath $before).Hash){throw 'Mounted registry inputs changed.'}
        }
    }
    $temporary=$MountedHive+'.wcp-'+$Id
    if(Test-Path -LiteralPath $temporary){throw 'Temporary hive replacement already exists.'}
    Copy-Item -LiteralPath $saved -Destination $temporary
    [IO.File]::Replace($temporary,$MountedHive,[System.Management.Automation.Language.NullString]::Value)
    foreach($name in @('SYSTEM.LOG','SYSTEM.LOG1','SYSTEM.LOG2')) {
        $path=Join-Path (Split-Path $MountedHive -Parent) $name
        if(Test-Path -LiteralPath $path){Remove-Item -LiteralPath $path -Force}
    }
}

$operationRoot=Assert-OperationCopy $Operation $Nonce
$image=Join-Path $operationRoot $(if($Mode -eq 'Install'){'bootstrap.vhdx'}else{'result.vhdx'})
Assert-PlainFile $image
if((Get-Item -LiteralPath $image).Attributes -band ([IO.FileAttributes]::SparseFile -bor [IO.FileAttributes]::Compressed -bor [IO.FileAttributes]::Encrypted)) {
    throw 'VHDX must be non-sparse, uncompressed and unencrypted before native attachment.'
}
$mount=Join-Path $operationRoot 'mounted-windows'
if(Test-Path -LiteralPath $mount){throw 'Mount directory already exists; inspect the previous operation first.'}
if($Mode -eq 'Install') {
    Assert-PlainFile (Join-Path $operationRoot 'MaintenanceService.exe')
    Assert-PlainFile $MaintenanceScript
}
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Administrator required to attach this copy.'}
$attached=$false;$accessAdded=$false;$registry=$null;$collectionFailed=$false
$helper=Join-Path $PSScriptRoot 'OfflineMaintenanceRegistry.cs'
Assert-PlainFile $helper
Add-Type -Path $helper
try {
    $null=New-Item -ItemType Directory -Path $mount
    $vhd=Mount-VHD -Path $image -NoDriveLetter -Passthru
    $attached=$true
    $disk=Get-Disk -Number $vhd.DiskNumber
    $parts=@(Get-Partition -DiskNumber $disk.Number | Where-Object {$_.GptType -eq '{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}' -and $_.Size -gt 5GB})
    if($parts.Count -ne 1){throw 'Expected one Windows data partition in this copy.'}
    $part=$parts[0]
    $part | Add-PartitionAccessPath -AccessPath ($mount+'\')
    $accessAdded=$true
    $systemHive=Join-Path $mount 'Windows\System32\config\SYSTEM'
    Assert-PlainFile $systemHive $mount
    if($Mode -eq 'Install'){Remove-MaintenanceResumeFile $operationRoot $Nonce $mount}
    $phase=$Mode.ToLowerInvariant()
    $recovered=New-RecoveredHive $operationRoot $phase $systemHive $mount
    $registry=[OfflineMaintenanceRegistry]::Open($recovered)
    $programData=Join-Path $mount 'ProgramData'
    if((Get-Item -LiteralPath $programData -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'ProgramData cannot be a reparse point.'}
    $guestFolder=Join-Path $programData 'WindowsClaudeMaintenance'
    if($Mode -eq 'Install') {
        if($registry.ServiceExists() -or (Test-Path -LiteralPath $guestFolder)){throw 'Maintenance service or payload already exists.'}
        $null=New-Item -ItemType Directory -Path $guestFolder
        Copy-Item -LiteralPath (Join-Path $operationRoot 'MaintenanceService.exe') -Destination (Join-Path $guestFolder 'MaintenanceService.exe')
        Copy-Item -LiteralPath $MaintenanceScript -Destination (Join-Path $guestFolder 'Compact-Windows.ps1')
        $sha=(Get-FileHash -LiteralPath $MaintenanceScript -Algorithm SHA256).Hash.ToLowerInvariant()
        $registry.Install($Nonce,$sha)
        Save-MaintenanceHive $registry $operationRoot $phase $systemHive $mount $Nonce
        [ordered]@{schema=1;nonce=$Nonce;script_sha256=$sha;disk_number=$disk.Number;partition_number=$part.PartitionNumber;offline_service_installed=$true} |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $operationRoot 'bootstrap-installed.json') -Encoding UTF8
    } else {
        if(-not (Test-Path -LiteralPath $guestFolder)){throw 'Maintenance payload/report folder is missing.'}
        if((Get-Item -LiteralPath $guestFolder -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Maintenance payload folder is a reparse point.'}
        $reports=Join-Path $operationRoot 'guest-results'
        $null=New-Item -ItemType Directory -Path $reports
        foreach($file in Get-ChildItem -LiteralPath $guestFolder -Force) {
            Assert-PlainFile $file.FullName $mount
            if($file.Length -gt 2MB -and $file.Extension -ne '.exe'){throw 'Unexpected oversized maintenance file.'}
            Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $reports $file.Name)
        }
        foreach($file in Get-ChildItem -LiteralPath $programData -Filter 'WindowsClaude-compact-*.json' -File) {
            Assert-PlainFile $file.FullName $mount
            if($file.Length -gt 1MB){throw 'Unexpected oversized cleanup report.'}
            Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $reports $file.Name)
        }
        $resultPath=Join-Path $reports 'result.json'
        if(-not (Test-Path -LiteralPath $resultPath)){throw 'Service result is missing; collected evidence is retained.'}
        $result=Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
        $installed=Get-Content -LiteralPath (Join-Path $operationRoot 'bootstrap-installed.json') -Raw | ConvertFrom-Json
        $collectionFailed=-not (Assert-MaintenanceResult $result $installed $Nonce $reports)
        if($registry.Remove($Nonce,$installed.script_sha256)) {
            Save-MaintenanceHive $registry $operationRoot $phase $systemHive $mount $Nonce
        }
        foreach($file in Get-ChildItem -LiteralPath $guestFolder -Force) {Assert-PlainFile $file.FullName $mount;Remove-Item -LiteralPath $file.FullName -Force}
        Remove-Item -LiteralPath $guestFolder -Force
        foreach($file in Get-ChildItem -LiteralPath $programData -Filter 'WindowsClaude-compact-*.json' -File) {Assert-PlainFile $file.FullName $mount;Remove-Item -LiteralPath $file.FullName -Force}
        [ordered]@{schema=1;nonce=$Nonce;guest_success=$result.success;bootstrap_removed=$true;disk_number=$disk.Number;partition_number=$part.PartitionNumber} |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $operationRoot 'bootstrap-removed.json') -Encoding UTF8
    }
} finally {
    try {
        if($registry){$registry.Dispose()}
        if($accessAdded){Write-VolumeCache -Path (($part | Get-Volume).Path)}
    } finally {
        if($accessAdded){$part | Remove-PartitionAccessPath -AccessPath ($mount+'\')}
        if($attached){Dismount-VHD -Path $image}
        if(Test-Path -LiteralPath $mount){Remove-Item -LiteralPath $mount -Force}
    }
}
if($collectionFailed){throw 'Guest cleanup failed. Evidence was collected and bootstrap removed; inspect guest-results before proceeding.'}
