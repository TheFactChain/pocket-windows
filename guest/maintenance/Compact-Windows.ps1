# Run as administrator ONLY inside the disposable maintenance copy.
# DISM /ResetBase prevents uninstalling already installed Windows updates.
[CmdletBinding()]
param([switch]$ConfirmMaintenanceCopy, [switch]$ShutdownOnSuccess)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-MaintenanceCopy([string]$Serial, [bool]$Confirmed) {
    if (-not $Confirmed -or $Serial -cne 'WC-MAINTENANCE-COPY') {
        throw 'Refusing cleanup: requires the marked maintenance VM and -ConfirmMaintenanceCopy.'
    }
}

function Assert-WindowsLocation([string]$Directory,[string]$Drive) {
    if ($Directory -ine 'C:\Windows' -or $Drive -ine 'C:') {
        throw "Expected Windows on C:\Windows (reported: $Directory, $Drive)."
    }
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable failed with exit code $LASTEXITCODE (3010 requires a manual restart, then rerun)."
    }
}

function Test-CacheBusy($Record) {
    if ($Record.CategoryInfo.Category -in @('PermissionDenied','ObjectNotFound')) { return $true }
    for ($errorObject=$Record.Exception; $null -ne $errorObject; $errorObject=$errorObject.InnerException) {
        if (($errorObject.HResult -band 65535) -in @(2,3,5,32,33)) { return $true }
    }
    return $false
}

function Clear-Cache([string]$Directory, [datetime]$Before) {
    try {
        $item = Get-Item -LiteralPath $Directory -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing reparse point: $Directory" }
        $children = @(Get-ChildItem -LiteralPath $Directory -Force)
    } catch {
        if (Test-CacheBusy $_) { return 1 }
        throw
    }
    $skipped = 0
    foreach ($child in $children) {
        if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) { $skipped++; continue }
        try {
            if ($child.PSIsContainer) {
                $skipped += (Clear-Cache $child.FullName $Before)
            } elseif ($child.LastWriteTime -lt $Before -and $child.CreationTime -lt $Before) {
                # File.Delete preserves native access/sharing errors; Remove-Item can wrap them as ArgumentException.
                [IO.File]::Delete($child.FullName)
            }
        } catch {
            if (Test-CacheBusy $_) { $skipped++; continue }
            throw
        }
    }
    return $skipped
}

# All destructive operations and report writes are below this boundary.
Assert-MaintenanceCopy (Get-CimInstance Win32_ComputerSystemProduct).IdentifyingNumber ([bool]$ConfirmMaintenanceCopy)
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run PowerShell as administrator.' }
$windows = (Get-CimInstance Win32_OperatingSystem).WindowsDirectory
Assert-WindowsLocation $windows $env:SystemDrive
if ((Get-Item -LiteralPath $windows -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Windows directory is a reparse point.'
}
$reportPath = Join-Path $env:ProgramData ('WindowsClaude-compact-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
$report = [ordered]@{ schema = 1; startedUtc = [datetime]::UtcNow.ToString('o'); success = $false;
    freeBytesBefore = (Get-Volume -DriveLetter C).SizeRemaining; steps = @(); cacheSkippedEntries = 0; error = $null }
$exitCode = 1
try {
    Invoke-Checked "$windows\System32\powercfg.exe" @('/hibernate', 'off')
    $report.steps += 'hibernate-disabled'
    Invoke-Checked "$windows\System32\dism.exe" @('/Online', '/Cleanup-Image', '/StartComponentCleanup', '/ResetBase', '/NoRestart')
    $report.steps += 'component-cleanup'
    $report.cacheSkippedEntries += (Clear-Cache "$windows\Temp" (Get-Date).AddDays(-7))
    $report.steps += 'windows-temp-older-than-7-days'
    $services = @(Get-Service -Name wuauserv, BITS)
    foreach ($service in $services) {
        if ($service.Status -notin @('Running', 'Stopped')) { throw "Service $($service.Name) is not in a stable state." }
    }
    $originalStates = @{}
    foreach ($service in $services) { $originalStates[$service.Name] = [string]$service.Status }
    $running = @($services | Where-Object Status -eq Running | Select-Object -ExpandProperty Name)
    try {
        foreach ($name in $running) {
            Stop-Service -Name $name -ErrorAction Stop
            (Get-Service -Name $name).WaitForStatus('Stopped', [timespan]::FromSeconds(60))
        }
        if (@(Get-Service -Name wuauserv, BITS | Where-Object Status -ne Stopped).Count) {
            throw 'Update services did not stop; download cache is untouched.'
        }
        $downloads = "$windows\SoftwareDistribution\Download"
        if ((Get-Item -LiteralPath "$windows\SoftwareDistribution" -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'SoftwareDistribution is a reparse point.'
        }
        if (Test-Path -LiteralPath $downloads) { $report.cacheSkippedEntries += (Clear-Cache $downloads ([datetime]::MaxValue)) }
        $report.steps += 'update-download-cache'
    } finally {
        $restoreErrors = @()
        foreach ($name in $originalStates.Keys) {
            try {
                if ($originalStates[$name] -eq 'Running') {
                    Start-Service -Name $name -ErrorAction Stop
                } else {
                    Stop-Service -Name $name -ErrorAction Stop
                }
                (Get-Service -Name $name).WaitForStatus($originalStates[$name], [timespan]::FromSeconds(60))
            } catch { $restoreErrors += $_.Exception.Message }
        }
        if ($restoreErrors.Count) { throw ('Cannot restore update services: ' + ($restoreErrors -join '; ')) }
    }
    Invoke-Checked "$windows\System32\fsutil.exe" @('behavior', 'set', 'DisableDeleteNotify', '0')
    Optimize-Volume -DriveLetter C -ReTrim -Verbose -ErrorAction Stop
    $report.steps += 'trim-enabled-and-retrim-complete'
    $report.success = $true
    $exitCode = 0
} catch {
    $report.error = $_.Exception.Message
    $report.errorCommand = [string]$_.InvocationInfo.MyCommand
    $report.errorLine = $_.InvocationInfo.ScriptLineNumber
    $report.errorCategory = [string]$_.CategoryInfo.Category
    Write-Warning $report.error
} finally {
    $report.finishedUtc = [datetime]::UtcNow.ToString('o')
    $report.freeBytesAfter = (Get-Volume -DriveLetter C).SizeRemaining
    $report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Write-Host "Report: $reportPath"
}
if ($report.success -and $ShutdownOnSuccess) {
    Invoke-Checked "$windows\System32\shutdown.exe" @('/s', '/t', '0')
}
exit $exitCode
