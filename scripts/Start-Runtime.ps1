[CmdletBinding()]
param(
    [string]$Project = '',
    [switch]$PlanOnly,
    [switch]$WorkTestMedia,
    [switch]$Elevated,
    [switch]$Gui,
    [string]$GuiResultPath = ''
)

$ErrorActionPreference = 'Stop'
$guiPath = $null
$receipt = $null
$failureCode = 'wrapper_failed'

function Quote-NativeArgument([string]$Value) {
    '"' + ([regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1')) + '"'
}

function Get-NativeStartupError([Exception]$Failure) {
    $current = $Failure
    while ($current) {
        if ($current -is [ComponentModel.Win32Exception]) { return $current.NativeErrorCode }
        $current = $current.InnerException
    }
    return $null
}

function Write-GuiStartupResult([string]$Code, [string]$ReceiptPath = '', $NativeError = $null) {
    if (-not $guiPath) { return }
    $result = @{schema=1;code=$Code}
    if ($ReceiptPath) {
        $basePrefix = $Project.TrimEnd('\') + '\'
        $prefix = $basePrefix + 'logs\'
        if ($ReceiptPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            $result.receipt = $ReceiptPath.Substring($basePrefix.Length).Replace('\', '/')
        }
    }
    if ($null -ne $NativeError) { $result.native_error = [int]$NativeError }
    try { [IO.File]::WriteAllText($guiPath, ($result | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false)) }
    catch { Write-Output 'Structured startup result could not be written.' }
}

try {
    if ([string]::IsNullOrWhiteSpace($Project)) {
        $Project = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
    }
    $Project = (Resolve-Path -LiteralPath $Project).ProviderPath
    if ($GuiResultPath -and -not $PlanOnly) {
        $candidate = [IO.Path]::GetFullPath($GuiResultPath)
        $expectedParent = Join-Path $Project 'logs'
        if ((Split-Path -Parent $candidate) -ine $expectedParent -or
            (Split-Path -Leaf $candidate) -notmatch '^gui-start-[0-9a-f]{32}\.json$' -or
            (Test-Path -LiteralPath $candidate)) { throw 'Invalid or existing GUI startup result path.' }
        $guiPath = $candidate
    }
    $python = Join-Path $Project 'runtime\python-3.12.14\python.exe'
    $supervisor = Join-Path $Project 'scripts\launch_runtime.py'
    foreach ($path in @($python, $supervisor)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $failureCode = 'missing_runtime'
            throw "Required runtime file is missing: $path"
        }
    }
    if ($PlanOnly) {
        $planArguments = @('-I', '-S', '-B', $supervisor, '--project', $Project, '--plan')
        if ($WorkTestMedia) { $planArguments += '--work-test-media' }
        & $python @planArguments
        exit $LASTEXITCODE
    }
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        if ($Elevated) { throw 'Administrator approval was not granted.' }
        $arguments = @('-NoProfile', '-File', (Quote-NativeArgument $PSCommandPath),
                       '-Project', (Quote-NativeArgument $Project), '-Elevated') -join ' '
        if ($WorkTestMedia) { $arguments += ' -WorkTestMedia' }
        if ($Gui) { $arguments += ' -Gui' }
        if ($guiPath) { $arguments += ' -GuiResultPath ' + (Quote-NativeArgument $guiPath) }
        # This foreground console can show a startup error and wait for the user.
        $window = if ($Gui) { 'Hidden' } else { 'Normal' }
        $failureCode = 'elevation_failed'
        $child = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -Verb RunAs `
            -ArgumentList $arguments -WindowStyle $window -PassThru
        # Wait for this wrapper, not its long-running supervisor/guest descendants.
        $child.WaitForExit()
        $child.Refresh()
        if ($guiPath -and -not (Test-Path -LiteralPath $guiPath)) {
            Write-GuiStartupResult 'elevation_failed' '' $child.ExitCode
        }
        exit $child.ExitCode
    }

    $failureCode = 'runtime_error'
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
    $directory = Join-Path $Project ('logs\runtime-launch-' + $stamp)
    $null = New-Item -ItemType Directory -Path $directory
    $receipt = Join-Path $directory 'receipt.json'
    $arguments = @('-I', '-S', '-B', (Quote-NativeArgument $supervisor),
                   '--project', (Quote-NativeArgument $Project), '--receipt', (Quote-NativeArgument $receipt)) -join ' '
    if ($WorkTestMedia) { $arguments += ' --work-test-media' }
    $supervisorProcess = Start-Process -FilePath $python -ArgumentList $arguments `
        -WorkingDirectory $Project -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $directory 'supervisor.stdout.log') `
        -RedirectStandardError (Join-Path $directory 'supervisor.stderr.log')
    $deadline = [DateTime]::UtcNow.AddSeconds(170)
    while ([DateTime]::UtcNow -lt $deadline) {
        $report = $null
        if (Test-Path -LiteralPath $receipt -PathType Leaf) {
            try { $report = Get-Content -LiteralPath $receipt -Raw -Encoding UTF8 | ConvertFrom-Json }
            catch { $report = $null }
        }
        if ($report -and $report.phase -in @('ready', 'stopped')) {
            Write-GuiStartupResult $report.phase $receipt
            Write-Host $report.message
            exit 0
        }
        if ($report -and $report.phase -eq 'error') {
            throw ($report.message + "`nReceipt: " + $receipt)
        }
        if ($supervisorProcess.HasExited) {
            $failureCode = 'supervisor_exited'
            throw "The supervisor exited before startup was confirmed. Read: $directory"
        }
        Start-Sleep -Milliseconds 200
    }
    $failureCode = 'startup_timeout'
    throw "Startup was not confirmed within 170 seconds. Any running guests were retained. Read: $directory"
}
catch {
    $nativeError = Get-NativeStartupError $_.Exception
    if ($nativeError -eq 1223) { $failureCode = 'uac_cancelled' }
    Write-GuiStartupResult $failureCode $receipt $nativeError
    if ($Gui) { Write-Output 'Workspace startup failed; consult the structured result channel.' }
    else { Write-Host ("Workspace startup failed.`n" + $_.Exception.Message) -ForegroundColor Red }
    if (-not $Gui) { $null = Read-Host 'Press Enter to close' }
    exit 1
}
