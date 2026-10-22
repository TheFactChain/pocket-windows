param([Parameter(Mandatory=$true)][string]$Project)
$ErrorActionPreference = 'Stop'
$null=[Reflection.Assembly]::Load('System.Web.Extensions, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35')
$Project = [IO.Path]::GetFullPath($Project).TrimEnd('\')
$operation = Join-Path $Project ('logs/components-acceptance-' + [Guid]::NewGuid().ToString('N'))
$fixture = Join-Path $operation 'fixture'
$report = [ordered]@{ schema=1; passed=$false; windows_attached=$false; fixture_removed=$false }
function Hash-File([string]$Path) {
    $stream=[IO.File]::OpenRead($Path); $sha=[Security.Cryptography.SHA256]::Create()
    try { [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose(); $stream.Dispose() }
}
function Assert-Plain([string]$Path) {
    $current=[IO.Path]::GetFullPath($Path)
    while ($current) {
        if ((Test-Path -LiteralPath $current) -and ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Redirected acceptance path' }
        $current=[IO.Path]::GetDirectoryName($current)
    }
}
Assert-Plain $operation
[IO.Directory]::CreateDirectory((Join-Path $fixture 'runtime')) | Out-Null
[IO.Directory]::CreateDirectory((Join-Path $fixture 'scripts')) | Out-Null
try {
    $manifestPath=Join-Path $Project 'runtime/components-manifest.json'
    $archivePath=Join-Path $Project 'runtime/components-recovery.zip'
    $manifest=Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $engineName='runtime/qemu-11.1.0/qemu-system-x86_64.exe'
    $engineRecords=@($manifest.files | Where-Object { $_.path -ceq $engineName })
    if ($engineRecords.Count -ne 1 -or (Hash-File (Join-Path $Project $engineName)) -ne $engineRecords[0].sha256) { throw 'Engine differs from the component manifest' }
    $report.qemu_engine_sha256=$engineRecords[0].sha256
    foreach ($relative in @('runtime/components-manifest.json','runtime/components-recovery.zip','scripts/Prepare-Dependencies.ps1')) {
        Assert-Plain (Join-Path $Project $relative)
        [IO.File]::Copy((Join-Path $Project $relative),(Join-Path $fixture $relative),$false)
    }
    $python=Join-Path $fixture 'runtime/python-3.12.14/python.exe'
    $report.started_without_python = -not [IO.File]::Exists($python)
    $binary=Join-Path $Project 'Launcher.exe'
    $report.launcher_sha256=Hash-File $binary
    $assembly=[Reflection.Assembly]::LoadFrom($binary)
    $bridge=$assembly.GetType('Bridge',$true)
    $run=$bridge.GetMethod('Run',[Reflection.BindingFlags]'NonPublic,Static')
    $report.stage='missing-python'
    $missing=$run.Invoke($null,[object[]]@([string]$fixture,'get-settings',$null))
    $report.missing_python_offers_repair = (-not $missing['ok'] -and $missing['blockers'][0]['action'] -eq 'prepare')
    $report.stage='repair'
    $reply=$run.Invoke($null,[object[]]@([string]$fixture,'prepare-components',$null))
    $report.repair_result=$reply
    if (-not $reply['ok'] -or $reply['code'] -ne 'repaired' -or $reply['repaired_files'] -ne $manifest.files.Count) { throw 'Actual EXE component recovery failed' }
    foreach ($entry in $manifest.files) {
        $file=Join-Path $fixture $entry.path
        Assert-Plain $file
        if ((Get-Item -LiteralPath $file).Length -ne $entry.bytes -or (Hash-File $file) -ne $entry.sha256) { throw 'Restored component mismatch' }
    }
    $report.verified_files=$manifest.files.Count
    $pythonVersion=& $python --version 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Restored Python did not execute' }
    $imageVersion=& (Join-Path $fixture 'runtime/qemu-11.1.0/qemu-img.exe') --version 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Restored qemu-img did not execute' }
    $engineVersion=& (Join-Path $fixture $engineName) --version 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'Restored QEMU did not execute' }
    $report.python_version=[string]$pythonVersion
    $report.qemu_img_version=[string]($imageVersion | Select-Object -First 1)
    $report.qemu_version=[string]($engineVersion | Select-Object -First 1)
    $report.passed=$report.started_without_python -and $report.missing_python_offers_repair
} catch {
    $report.failure_type=$_.Exception.GetType().Name
    if ($_.Exception.InnerException) {
        $report.failure_inner_type=$_.Exception.InnerException.GetType().Name
        $report.failure_inner_message=$_.Exception.InnerException.Message.Substring(0,[Math]::Min(2048,$_.Exception.InnerException.Message.Length))
    }
} finally {
    Assert-Plain $fixture
    $resolved=[IO.Path]::GetFullPath($fixture)
    $prefix=[IO.Path]::GetFullPath($operation).TrimEnd('\')+'\'
    if (-not $resolved.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) -or
        @(Get-ChildItem -LiteralPath $fixture -Force -Recurse | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) { throw 'Unsafe acceptance cleanup' }
    Remove-Item -LiteralPath $fixture -Recurse -Force
    $report.fixture_removed=-not (Test-Path -LiteralPath $fixture)
    $report.passed=$report.passed -and $report.fixture_removed
    $json=$report | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText((Join-Path $operation 'result.json'),$json,[Text.UTF8Encoding]::new($false))
}
$json
if (-not $report.passed) { exit 1 }
