[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
# WPF launcher (launcher-wpf): Visual Studio Build Tools MSBuild; WPF-UI comes from NuGet and is embedded into Launcher.exe.
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) { throw 'Visual Studio Build Tools are required to build Launcher.exe.' }
$msbuild = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild -find 'MSBuild\**\Bin\amd64\MSBuild.exe' | Select-Object -First 1
if (-not $msbuild) { throw 'MSBuild from Visual Studio Build Tools was not found.' }
$output = [IO.Path]::GetFullPath($OutputDirectory)
$exe = Join-Path $output 'Launcher.exe'
if (Test-Path -LiteralPath $exe) { throw 'Output executable already exists; use a new build directory.' }
$null = New-Item -ItemType Directory -Path $output -Force
$project = Join-Path $root 'launcher-wpf\Launcher.Wpf.csproj'
$staging = Join-Path ([IO.Path]::GetTempPath()) ('wcp-launcher-' + [Guid]::NewGuid().ToString('N'))
try
{
    & $msbuild $project -t:Restore -nologo -v:minimal
    if ($LASTEXITCODE -ne 0) { throw 'Launcher package restore failed.' }
    & $msbuild $project -t:Rebuild -p:Configuration=Release ('-p:OutDir=' + $staging + '\') -nologo -v:minimal
    if ($LASTEXITCODE -ne 0) { throw 'Desktop launcher compilation failed.' }
    Copy-Item -LiteralPath (Join-Path $staging 'Launcher.exe') -Destination $exe
}
finally { if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force } }
$file = Get-Item -LiteralPath $exe
$algorithm = [Security.Cryptography.SHA256]::Create()
$stream = [IO.File]::OpenRead($exe)
try { $sha256 = [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '') }
finally { $stream.Dispose(); $algorithm.Dispose() }
[pscustomobject]@{Executable=$exe;Bytes=$file.Length;SHA256=$sha256;BuiltUTC=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json
