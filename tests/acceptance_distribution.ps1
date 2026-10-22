[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Setup,
    [Parameter(Mandatory=$true)][string]$Payload,
    [Parameter(Mandatory=$true)][string]$Output,
    [Parameter(Mandatory=$true)][string]$Evidence,
    [Parameter(Mandatory=$true,HelpMessage='Expected Setup SHA256 from the approved RELEASE.json.')]
    [ValidatePattern('(?-i)\A[a-f0-9]{64}\z')][string]$ExpectedSetupSHA256
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $PSScriptRoot))).TrimEnd('\')
function WorkspacePath([string]$value) {
    $full = [IO.Path]::GetFullPath($value)
    if (-not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'All paths must be inside the portable project workspace.' }
    for ($item = $full; $item; $item = [IO.Path]::GetDirectoryName($item)) {
        if ((Test-Path -LiteralPath $item) -and ((Get-Item -LiteralPath $item -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Reparse paths are forbidden.' }
    }
    return $full
}
$Setup = WorkspacePath $Setup
$Payload = WorkspacePath $Payload
$Output = WorkspacePath $Output
$Evidence = WorkspacePath $Evidence
foreach ($file in @($Setup, $Payload, ($Payload + '.sha256'))) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Required input missing: $file" }
}
foreach ($destination in @($Output, $Evidence)) {
    if (Test-Path -LiteralPath $destination) { throw "Destination must be absent: $destination" }
    if (-not (Test-Path -LiteralPath (Split-Path -Parent $destination) -PathType Container)) { throw 'Destination parent must already exist.' }
}
if ($Output.StartsWith($Evidence + '\', [StringComparison]::OrdinalIgnoreCase) -or $Evidence.StartsWith($Output + '\', [StringComparison]::OrdinalIgnoreCase) -or $Output -eq $Evidence) { throw 'Output and Evidence must be separate directories.' }
$compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw 'Installed .NET Framework compiler is required.' }
$null = New-Item -ItemType Directory -Path $Evidence
$previousTemp, $previousTmp = $env:TEMP, $env:TMP
try {
    $env:TEMP = $env:TMP = $Evidence
    $harness = Join-Path $Evidence 'DistributionHarness.exe'
    & $compiler /nologo /target:exe /platform:x64 /reference:System.Web.Extensions.dll /reference:System.IO.Compression.dll /reference:System.IO.Compression.FileSystem.dll ('/out:' + $harness) (Join-Path $PSScriptRoot 'DistributionHarness.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Distribution harness compilation failed.' }
    & $harness --self-test
    if ($LASTEXITCODE -ne 0) { throw 'Distribution harness self-check failed.' }
    $stdout = Join-Path $Evidence 'progress.log'
    $stderr = Join-Path $Evidence 'stderr.log'
    $arguments = @($Setup, $Payload, $Output, (Join-Path $Evidence 'result.json'), $ExpectedSetupSHA256) | ForEach-Object { '"' + $_ + '"' }
    $process = Start-Process -FilePath $harness -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    # Keep a process handle so Windows PowerShell retains ExitCode after a fast exit.
    $null = $process.Handle
    while (-not $process.WaitForExit(15000)) {
        $line = Get-Content -LiteralPath $stdout -Encoding UTF8 -Tail 1 -ErrorAction SilentlyContinue
        Write-Host ('Acceptance running: ' + $line)
    }
    $process.WaitForExit()
    Get-Content -LiteralPath $stdout -Encoding UTF8 -Tail 20
    if ($process.ExitCode -ne 0) { throw ('Distribution acceptance failed: ' + (Get-Content -LiteralPath $stderr -Encoding UTF8 -Raw)) }
    Get-Content -LiteralPath (Join-Path $Evidence 'result.json') -Encoding UTF8 -Raw
}
finally { $env:TEMP = $previousTemp; $env:TMP = $previousTmp }
