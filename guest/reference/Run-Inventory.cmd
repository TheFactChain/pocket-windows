@echo off
setlocal DisableDelayedExpansion
set "WC_REFERENCE_COLLECTOR_MODE="
if "%~1"=="--collector" set "WC_REFERENCE_COLLECTOR_MODE=1"
set "WC_REFERENCE_BOOTSTRAP=%~f0"
set "WC_REFERENCE_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "WC_REFERENCE_PS=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
"%WC_REFERENCE_PS%" -NoProfile -Command "$ErrorActionPreference='Stop'; try { $source=[IO.File]::ReadAllText($env:WC_REFERENCE_BOOTSTRAP); $marker='# WC_REFERENCE_BOOTSTRAP_BODY'; $offset=$source.LastIndexOf($marker,[StringComparison]::Ordinal); if($offset -lt 0 -or $source.Length -gt 131072){throw 'Invalid bootstrap.'}; & ([scriptblock]::Create($source.Substring($offset+$marker.Length))) } catch { Write-Error ('Reference bootstrap refused at stage: ' + $script:ReferenceStage + '. Check trusted files, destination and UAC.') -ErrorAction Continue; exit 1 }"
set "WC_REFERENCE_EXIT=%ERRORLEVEL%"
if "%WC_REFERENCE_COLLECTOR_MODE%"=="1" exit /b %WC_REFERENCE_EXIT%
if not "%WC_REFERENCE_EXIT%"=="0" echo Inventory did not complete. Preserve any local report; persistent execution policy was not changed.
pause
exit /b %WC_REFERENCE_EXIT%
# WC_REFERENCE_BOOTSTRAP_BODY
$script:ReferenceStage = 'trusted_files'
$names = @('Collect-ReferenceInventory.ps1', 'Run-Inventory.cmd', 'README.md')
$sourceDirectory = [IO.Path]::GetDirectoryName($env:WC_REFERENCE_BOOTSTRAP)
function Assert-PlainPath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $entry = Get-Item -LiteralPath $current -Force
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse point refused.' }
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
}
function Get-PublicBytes([string]$Path, [int]$Limit) {
    Assert-PlainPath $Path
    $entry = Get-Item -LiteralPath $Path -Force
    if ($entry.PSIsContainer -or $entry.Length -gt $Limit) { throw 'Invalid source file.' }
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -gt $Limit) { throw 'Invalid source file.' }
    return ,$bytes
}
function Get-PublicHash([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
function Get-CollectorPolicy {
    $names = @('MachinePolicy', 'UserPolicy', 'Process', 'CurrentUser', 'LocalMachine')
    $rows = @(Get-ExecutionPolicy -List -ErrorAction Stop)
    if ($rows.Count -ne 5) { throw 'Execution policy query refused.' }
    $scopes = [ordered]@{}
    foreach ($row in $rows) {
        $name = [string]$row.Scope
        if ($name -cnotin $names -or $scopes.Contains($name) -or
            $row.ExecutionPolicy -isnot [Microsoft.PowerShell.ExecutionPolicy] -or
            -not [Enum]::IsDefined([Microsoft.PowerShell.ExecutionPolicy], $row.ExecutionPolicy)) {
            throw 'Execution policy query refused.'
        }
        $scopes[$name] = [string]$row.ExecutionPolicy
    }
    $mode = 'configured'
    if (@($scopes.Values | Where-Object { $_ -cne 'Undefined' }).Count -eq 0) { $mode = 'RemoteSigned' }
    return @{mode = $mode; scopes = $scopes}
}
function New-CollectorStartInfo([string]$Collector, [string]$Mode) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if ($Mode -notin @('configured', 'RemoteSigned')) { throw 'Execution mode refused.' }
    $policyArgument = if ($Mode -eq 'RemoteSigned') { '-ExecutionPolicy RemoteSigned ' } else { '' }
    $info.Arguments = '-NoProfile ' + $policyArgument + '-File "' + $Collector + '" -ReturnSerial'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [Console]::OutputEncoding
    $info.StandardErrorEncoding = [Console]::OutputEncoding
    return $info
}
function Invoke-LoggedCollector([string]$Collector, [string]$Directory) {
    $logPath = Join-Path $Directory 'collector-output.log'
    $resultPath = Join-Path $Directory 'collector-exit.json'
    Assert-PlainPath $logPath
    Assert-PlainPath $resultPath
    if ((Test-Path -LiteralPath $logPath) -or (Test-Path -LiteralPath $resultPath)) { throw 'Existing diagnostics refused.' }
    $log = [IO.File]::Open($logPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $process = $null
    $code = 1
    $stage = 'policy_query'
    $selection = @{mode = 'unavailable'; scopes = $null}
    $remaining = 65536
    try {
        $selection = Get-CollectorPolicy
        $summary = 'Execution policy mode=' + $selection.mode + '; ' + (($selection.scopes.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value }) -join '; ') + [Environment]::NewLine
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($summary)
        $log.Write($bytes, 0, $bytes.Length)
        $remaining -= $bytes.Length
        $process = [Diagnostics.Process]::new()
        $process.StartInfo = New-CollectorStartInfo $Collector $selection.mode
        $stage = 'collector_start'
        $null = $process.Start()
        $stage = 'collector_output'
        $streams = @($process.StandardOutput, $process.StandardError)
        $buffers = @([char[]]::new(4096), [char[]]::new(4096))
        $reads = @($streams[0].ReadAsync($buffers[0], 0, 4096), $streams[1].ReadAsync($buffers[1], 0, 4096))
        while ($null -ne $reads[0] -or $null -ne $reads[1]) {
            for ($i = 0; $i -lt 2; $i++) {
                if ($null -eq $reads[$i] -or -not $reads[$i].IsCompleted) { continue }
                $count = $reads[$i].GetAwaiter().GetResult()
                if ($count -eq 0) { $reads[$i] = $null; continue }
                $bytes = [Text.UTF8Encoding]::new($false).GetBytes($buffers[$i], 0, $count)
                $take = [Math]::Min($remaining, $bytes.Length)
                if ($take -gt 0) { $log.Write($bytes, 0, $take); $remaining -= $take }
                # Drain both pipes after the cap so diagnostics cannot stall the collector.
                $reads[$i] = $streams[$i].ReadAsync($buffers[$i], 0, 4096)
            }
            Start-Sleep -Milliseconds 20
        }
        $process.WaitForExit()
        $code = $process.ExitCode
        $stage = 'collector_exit'
    } catch {
        $bytes = [Text.Encoding]::ASCII.GetBytes(('Collector refused at stage=' + $stage + '.' + [Environment]::NewLine))
        $take = [Math]::Min($remaining, $bytes.Length)
        if ($take -gt 0) { $log.Write($bytes, 0, $take); $remaining -= $take }
    } finally {
        $log.Dispose()
        $result = @{schema = 1; stage = $stage; exit_code = $code; execution_mode = $selection.mode; execution_scopes = $selection.scopes; output_limit_bytes = 65536; output_capped = ($remaining -eq 0)} | ConvertTo-Json -Compress
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($result)
        $stream = [IO.File]::Open($resultPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
        if ($null -ne $process) { $process.Dispose() }
    }
    return $code
}
function Show-ReferenceResult($Result, [string]$Directory) {
    Write-Output ('Inventory stage: ' + $Result.stage + '; exit code: ' + $Result.exit_code)
    Write-Output ('Execution mode: ' + $Result.execution_mode)
    Write-Output ('Local diagnostics: ' + (Join-Path $Directory 'collector-output.log'))
    Write-Output ('Local result: ' + (Join-Path $Directory 'collector-exit.json'))
    if ($Result.exit_code -ne 0 -and $Result.stage -in @('policy_query', 'collector_start', 'collector_output', 'collector_exit')) {
        try {
            $bytes = Get-PublicBytes (Join-Path $Directory 'collector-output.log') 65536
            Write-Output 'Child PowerShell output (bounded local diagnostic):'
            # A capped log can end inside a UTF-8 character; show a replacement rather than hide the error.
            Write-Output ([Text.UTF8Encoding]::new($false, $false).GetString($bytes))
        } catch { Write-Output 'Local diagnostic could not be read; the child exit code is preserved.' }
    }
}
function Start-ReferenceCollector([string]$Directory) {
    if ($Directory -match '[%"\r\n]') { throw 'Unsupported command path.' }
    $command = Join-Path $Directory 'Run-Inventory.cmd'
    $arguments = '/d /v:off /s /c ""' + $command + '" --collector"'
    try {
        $process = Start-Process -FilePath (Join-Path $env:WINDIR 'System32\cmd.exe') -ArgumentList $arguments -Verb RunAs -PassThru -Wait
    } catch [ComponentModel.Win32Exception] {
        if ($_.Exception.NativeErrorCode -eq 1223) { return @{stage = 'uac_cancelled'; exit_code = 1223; execution_mode = 'not_started'} }
        throw
    }
    $stage = 'child_bootstrap'
    $mode = 'unavailable'
    $path = Join-Path $Directory 'collector-exit.json'
    if (Test-Path -LiteralPath $path) {
        $result = [Text.UTF8Encoding]::new($false, $true).GetString((Get-PublicBytes $path 8192)) | ConvertFrom-Json
        if ($result.schema -ne 1 -or $result.stage -notin @('policy_query', 'collector_start', 'collector_output', 'collector_exit') -or
            $result.exit_code -isnot [int] -or $result.exit_code -ne $process.ExitCode -or
            $result.execution_mode -cnotin @('unavailable', 'configured', 'RemoteSigned')) { throw 'Invalid collector result.' }
        $stage = $result.stage
        $mode = $result.execution_mode
    }
    return @{stage = $stage; exit_code = $process.ExitCode; execution_mode = $mode}
}
if (-not [Environment]::Is64BitProcess -or -not [Environment]::UserInteractive -or
    [Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) { throw 'Interactive 64-bit user required.' }
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($sid -in @('S-1-5-18', 'S-1-5-19', 'S-1-5-20')) { throw 'Service account refused.' }
$manifestBytes = Get-PublicBytes (Join-Path $sourceDirectory 'handoff.json') 8192
$manifest = [Text.UTF8Encoding]::new($false, $true).GetString($manifestBytes) | ConvertFrom-Json
if ((($manifest.PSObject.Properties.Name | Sort-Object) -join ',') -cne 'expected_smbios_serial,files,phase,reference_id,schema' -or
    $manifest.schema -isnot [int] -or $manifest.schema -ne 1 -or $manifest.phase -cne 'inventory_only' -or
    $manifest.reference_id -cnotmatch '^[0-9a-f]{32}$' -or
    $manifest.expected_smbios_serial -cne ('WCREFERENCE-' + $manifest.reference_id) -or
    (($manifest.files.PSObject.Properties.Name | Sort-Object) -join ',') -cne (($names | Sort-Object) -join ',')) {
    throw 'Invalid handoff.'
}
# Check the actual isolated reference VM before copying anything or requesting UAC.
$serial = (Get-CimInstance -ClassName Win32_ComputerSystemProduct -OperationTimeoutSec 15).IdentifyingNumber
if ($serial -cne $manifest.expected_smbios_serial) { throw 'Not the marked isolated reference VM.' }
$payload = @{'handoff.json' = $manifestBytes}
foreach ($name in $names) {
    $bytes = Get-PublicBytes (Join-Path $sourceDirectory $name) 131072
    if ($manifest.files.$name -cnotmatch '^[0-9a-f]{64}$' -or
        (Get-PublicHash $bytes) -cne $manifest.files.$name) { throw 'Source hash mismatch.' }
    $payload[$name] = $bytes
}
if ($env:WC_REFERENCE_COLLECTOR_MODE -eq '1') {
    # The elevated copy verifies the same package before launching the policy-governed -File child.
    exit (Invoke-LoggedCollector (Join-Path $sourceDirectory 'Collect-ReferenceInventory.ps1') $sourceDirectory)
}
$script:ReferenceStage = 'local_copy'
$local = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
if (-not $local -or -not [IO.Path]::IsPathRooted($local)) { throw 'Local application data unavailable.' }
$destination = Join-Path (Join-Path $local 'WindowsClaudeReference') $manifest.reference_id
Assert-PlainPath $destination
if (Test-Path -LiteralPath $destination) {
    $directory = Get-Item -LiteralPath $destination -Force
    if (-not $directory.PSIsContainer) { throw 'Destination is not a directory.' }
    foreach ($entry in Get-ChildItem -LiteralPath $destination -Force) {
        if ($entry.Name -notin @($payload.Keys) -or $entry.PSIsContainer -or
            ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Existing destination refused.' }
    }
    foreach ($name in $payload.Keys) {
        $existing = Get-PublicBytes (Join-Path $destination $name) 131072
        if ((Get-PublicHash $existing) -cne (Get-PublicHash $payload[$name])) { throw 'Existing destination differs.' }
    }
} else {
    $null = [IO.Directory]::CreateDirectory($destination)
    Assert-PlainPath $destination
    foreach ($name in $payload.Keys) {
        $path = Join-Path $destination $name
        $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($payload[$name], 0, $payload[$name].Length) }
        finally { $stream.Dispose() }
    }
}
# UAC elevates the trusted copied wrapper; only its child redirects output, preserving -File policy.
$script:ReferenceStage = 'uac'
$result = Start-ReferenceCollector $destination
Show-ReferenceResult $result $destination
if ($result.exit_code -ne 0) { exit $result.exit_code }
Write-Output ('Inventory saved in ' + $destination + '. Manual review required.')
