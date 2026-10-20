[CmdletBinding()]
param(
    [string]$Project = '',
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
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        if ($Elevated) { throw 'Administrator approval was not granted.' }
        $arguments = @('-NoProfile', '-File', (Quote-NativeArgument $PSCommandPath),
                       '-Project', (Quote-NativeArgument $Project), '-Elevated') -join ' '
        # This is an interactive shutdown console, including any retained-guest error.
        $child = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -Verb RunAs `
            -ArgumentList $arguments -WindowStyle Normal -Wait -PassThru
        exit $child.ExitCode
    }
    Write-Host 'Requesting graceful Windows shutdown, then gateway shutdown. Each guest may take up to two minutes.'
    $arguments = @('-I', '-S', '-B')
    & $python @arguments $supervisor '--project' $Project '--stop'
    if ($LASTEXITCODE -ne 0) {
        throw 'Graceful shutdown was not confirmed. Running guests were retained. Read the error above and the runtime receipts under logs.'
    }
}
catch {
    Write-Host ("Workspace shutdown incomplete.`n" + $_.Exception.Message) -ForegroundColor Red
    $null = Read-Host 'Press Enter to close'
    exit 1
}
