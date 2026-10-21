# Phase 1 only. Manually run as an elevated user inside the marked reference VM.
[CmdletBinding()]
param([switch]$ReturnSerial)
$ErrorActionPreference = 'Stop'
$script:ReferenceStage = 'context'

function Format-ReferenceFailure([string]$Stage, [Exception]$Exception) {
    if ($Stage -notin @('context', 'manifest', 'identity', 'hash', 'os_metadata', 'sections',
                       'report_encode', 'report_write', 'serial_return')) { $Stage = 'unknown' }
    $base = $Exception.GetBaseException()
    $name = $base.GetType().Name
    if ($name -cnotmatch '^[A-Za-z][A-Za-z0-9]{0,95}$') { $name = 'Exception' }
    return ('Reference inventory failed; stage=' + $Stage + '; exception=' + $name +
            '; hresult=' + [int]$base.HResult + '. No cleanup or Sysprep was run.')
}

function Assert-ReferenceContext {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    $sid = $identity.User.Value
    if ($sid -in @('S-1-5-18', 'S-1-5-19', 'S-1-5-20') -or
        -not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -or
        -not [Environment]::UserInteractive -or [Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) {
        throw 'Elevated interactive USER required; service accounts are refused.'
    }
    if (-not [Environment]::Is64BitProcess) { throw 'Use the built-in 64-bit Windows PowerShell.' }
    $script:ReferenceStage = 'manifest'
    $manifestPath = Join-Path $PSScriptRoot 'handoff.json'
    if ((Get-Item -LiteralPath $manifestPath).Length -gt 8192) { throw 'Invalid handoff.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.schema -ne 1 -or $manifest.phase -ne 'inventory_only' -or
        $manifest.reference_id -cnotmatch '^[0-9a-f]{32}$' -or
        $manifest.expected_smbios_serial -cne ('WCREFERENCE-' + $manifest.reference_id)) { throw 'Invalid handoff.' }
    # This checks the actual type 1 SMBIOS serial, not a local acknowledgement constant.
    $script:ReferenceStage = 'identity'
    $serial = (Get-CimInstance -ClassName Win32_ComputerSystemProduct).IdentifyingNumber
    if ($serial -cne $manifest.expected_smbios_serial) { throw 'Not the marked isolated reference VM.' }
    $script:ReferenceStage = 'hash'
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [IO.File]::ReadAllBytes($PSCommandPath)
        $hash = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
    if ($manifest.files.'Collect-ReferenceInventory.ps1' -cne $hash) { throw 'Bootstrap hash mismatch.' }
    @{ reference_id = [string]$manifest.reference_id; sid = $sid; administrator = $true; interactive = $true }
}

function Read-Section {
    param([scriptblock]$Collect)
    try { @{ status = 'complete'; items = @(& $Collect); error = $null } }
    catch { @{ status = 'unavailable'; items = @(); error = 'collection_failed' } }
}

function Get-AppxUserRows([object[]]$Users) {
    foreach ($user in $Users) {
        $sid = $user.UserSecurityId.Sid
        if ($sid -isnot [string] -or $sid.Length -gt 184 -or
            $sid -cnotmatch '^S-1-(?:[0-9]+-){1,14}[0-9]+\z') { throw 'Invalid Appx user SID.' }
        $state = [string]$user.InstallState
        if ($state -notin @('Installed', 'Staged', 'NotPresent')) { $state = 'Unknown' }
        @{ sid = $sid; state = $state }
    }
}

function Get-AppRows {
    param([string]$RegistryRoot, [string]$Sid = '')
    foreach ($suffix in @('Software\Microsoft\Windows\CurrentVersion\Uninstall',
                         'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')) {
        $path = $RegistryRoot + '\' + $suffix
        if (-not (Test-Path -LiteralPath $path)) { continue }
        foreach ($entry in Get-ChildItem -LiteralPath $path) {
            $name = [string]$entry.GetValue('DisplayName', '')
            if ([string]::IsNullOrWhiteSpace($name)) { continue }
            # Read only public metadata values, not uninstall commands or registry keys wholesale.
            $row = @{ name = $name; version = [string]$entry.GetValue('DisplayVersion', ''); publisher = [string]$entry.GetValue('Publisher', '') }
            if ($Sid) { $row.sid = $Sid }
            $row
        }
    }
}

function Get-UserRows {
    $names = @{}
    foreach ($user in Get-CimInstance -ClassName Win32_UserAccount -Filter 'LocalAccount=True') {
        $names[[string]$user.SID] = [string]$user.Name
    }
    $known = @{
        claude_config = 'AppData\Roaming\Claude'; claude_cli = '.claude';
        chrome_profile = 'AppData\Local\Google\Chrome\User Data';
        edge_profile = 'AppData\Local\Microsoft\Edge\User Data';
        credential_store = 'AppData\Local\Microsoft\Credentials';
        vault = 'AppData\Local\Microsoft\Vault'; dpapi = 'AppData\Roaming\Microsoft\Protect';
        temp_cache = 'AppData\Local\Temp'
    }
    foreach ($profile in Get-CimInstance -ClassName Win32_UserProfile) {
        $markers = @{}
        foreach ($key in $known.Keys) {
            $markers[$key] = $null
            if (-not $profile.Special -and $profile.LocalPath) {
                try { $markers[$key] = [bool](Test-Path -LiteralPath (Join-Path $profile.LocalPath $known[$key]) -ErrorAction Stop) }
                catch { $markers[$key] = $null }
            }
        }
        @{ sid = [string]$profile.SID; name = [string]$names[[string]$profile.SID];
           loaded = [bool]$profile.Loaded; special = [bool]$profile.Special; markers = $markers }
    }
}

function Send-ReferenceReport {
    param([byte[]]$Bytes, [string]$ReferenceId)
    if ($Bytes.Length -gt 8388608 -or $ReferenceId -cnotmatch '^[0-9a-f]{32}$') { throw 'Invalid public report.' }
    $header = [Text.Encoding]::ASCII.GetBytes(('WCREFERENCE ' + $ReferenceId + ' ' + $Bytes.Length + "`n"))
    $port = [IO.Ports.SerialPort]::new('COM2', 115200, [IO.Ports.Parity]::None, 8, [IO.Ports.StopBits]::One)
    $port.Handshake = [IO.Ports.Handshake]::None
    $port.ReadTimeout = 1000
    $port.WriteTimeout = 1000
    $timer = [Diagnostics.Stopwatch]::StartNew()
    try {
        $port.Open()
        $port.Write($header, 0, $header.Length)
        for ($offset = 0; $offset -lt $Bytes.Length; $offset += 4096) {
            if ($timer.ElapsedMilliseconds -ge 30000) { throw 'Serial deadline reached.' }
            $port.Write($Bytes, $offset, [Math]::Min(4096, $Bytes.Length - $offset))
        }
        while ($port.BytesToWrite -gt 0) {
            if ($timer.ElapsedMilliseconds -ge 30000) { throw 'Serial deadline reached.' }
            Start-Sleep -Milliseconds 20
        }
    } finally {
        try { if ($port.IsOpen) { $port.Close() } }
        finally { $port.Dispose(); $timer.Stop() }
    }
}

try {
    $context = Assert-ReferenceContext
    $script:ReferenceStage = 'report_write'
    $output = Join-Path $PSScriptRoot 'reference-report.json'
    if (Test-Path -LiteralPath $output) { throw 'Report already exists; review it before another run.' }
    $script:ReferenceStage = 'os_metadata'
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    $version = Get-Item -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $setup = Get-Item -LiteralPath 'HKLM:\SYSTEM\Setup'
    $pending = (Test-Path -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or
               (Test-Path -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')
    $sessionManager = Get-Item -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager'
    $pending = $pending -or ($sessionManager.GetValueNames() -contains 'PendingFileRenameOperations')
    $script:ReferenceStage = 'sections'
    $sections = [ordered]@{}
    $sections.local_accounts = Read-Section {
        foreach ($user in Get-CimInstance -ClassName Win32_UserAccount -Filter 'LocalAccount=True') {
            @{ sid = [string]$user.SID; name = [string]$user.Name; disabled = [bool]$user.Disabled }
        }
    }
    $sections.users = Read-Section { Get-UserRows }
    $sections.machine_apps = Read-Section { Get-AppRows 'Registry::HKEY_LOCAL_MACHINE' }
    $sections.user_apps = Read-Section {
        if ($sections.users.status -ne 'complete') { throw 'User inventory unavailable.' }
        foreach ($profile in $sections.users.items) {
            if ($profile.special) { continue }
            $hive = 'Registry::HKEY_USERS\' + $profile.sid
            if (Test-Path -LiteralPath $hive) { Get-AppRows $hive $profile.sid }
        }
    }
    if ($sections.user_apps.status -eq 'complete' -and @($sections.users.items | Where-Object {
        -not $_.special -and (-not $_.loaded -or -not (Test-Path -LiteralPath ('Registry::HKEY_USERS\' + $_.sid)))
    }).Count -gt 0) {
        $sections.user_apps.status = 'partial'
        $sections.user_apps.error = 'unloaded_hives_not_read'
    }
    $sections.appx = Read-Section {
        foreach ($package in Get-AppxPackage -AllUsers) {
            $users = @(Get-AppxUserRows $package.PackageUserInformation)
            @{ name = [string]$package.Name; package = [string]$package.PackageFullName;
               version = [string]$package.Version; framework = [bool]$package.IsFramework; users = $users }
        }
    }
    $sections.provisioned_appx = Read-Section {
        foreach ($package in Get-AppxProvisionedPackage -Online) {
            @{ name = [string]$package.DisplayName; package = [string]$package.PackageName; version = [string]$package.Version }
        }
    }
    $script:ReferenceStage = 'report_encode'
    $report = [ordered]@{
        schema = 1; phase = 'inventory_only'; reference_id = $context.reference_id;
        isolated_copy_acknowledged = $true; cleanup_executed = $false; sysprep_executed = $false;
        context = @{ sid = $context.sid; administrator = $true; interactive = $true };
        os = @{ version = [string]$os.Version; build = [string]$os.BuildNumber; edition = [string]$version.GetValue('EditionID', '');
                sysprep_present = [bool](Test-Path -LiteralPath (Join-Path $env:WINDIR 'System32\Sysprep\Sysprep.exe'));
                pending_reboot = [bool]$pending; setup_in_progress = [bool]$setup.GetValue('SystemSetupInProgress', 0) };
        sections = $sections
    }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($report | ConvertTo-Json -Depth 12))
    if ($bytes.Length -gt 8388608) { throw 'Report exceeds the public inventory bound.' }
    $script:ReferenceStage = 'report_write'
    $stream = [IO.File]::Open($output, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    if ($ReturnSerial) {
        $script:ReferenceStage = 'serial_return'
        try { Send-ReferenceReport $bytes $context.reference_id }
        catch {
            Write-Warning ('Serial return failed. The complete local reference-report.json is preserved for manual transfer. ' +
                           (Format-ReferenceFailure $script:ReferenceStage $_.Exception))
            exit 2
        }
    }
    Write-Output 'Inventory saved: reference-report.json. Manual review required. No cleanup or Sysprep was performed.'
} catch {
    # Do not print raw registry, CIM or Appx exceptions into a shareable log.
    Write-Error (Format-ReferenceFailure $script:ReferenceStage $_.Exception) -ErrorAction Continue
    exit 1
}
