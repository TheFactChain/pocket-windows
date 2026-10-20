[CmdletBinding()]
param(
    [string]$Project = '',
    [switch]$PlanOnly,
    [switch]$WorkTestMedia,
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'

function Quote-NativeArgument([string]$Value) {
    '"' + ([regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1')) + '"'
}

try {
    if ([string]::IsNullOrWhiteSpace($Project)) {
        $Project = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
    }
    $Project = (Resolve-Path -LiteralPath $Project).ProviderPath
    $python = Join-Path $Project 'runtime\python-3.12.14\python.exe'
    $supervisor = Join-Path $Project 'scripts\launch_runtime.py'
    foreach ($path in @($python, $supervisor)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
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
        # This foreground console can show a startup error and wait for the user.
        $child = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -Verb RunAs `
            -ArgumentList $arguments -WindowStyle Normal -Wait -PassThru
        exit $child.ExitCode
    }

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
            Write-Host $report.message
            exit 0
        }
        if ($report -and $report.phase -eq 'error') {
            throw ($report.message + "`nReceipt: " + $receipt)
        }
        if ($supervisorProcess.HasExited) {
            throw "The supervisor exited before startup was confirmed. Read: $directory"
        }
        Start-Sleep -Milliseconds 200
    }
    throw "Startup was not confirmed within 170 seconds. Any running guests were retained. Read: $directory"
}
catch {
    Write-Host ("Workspace startup failed.`n" + $_.Exception.Message) -ForegroundColor Red
    $null = Read-Host 'Press Enter to close'
    exit 1
}
