[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw 'Installed .NET Framework compiler is required.' }
$output = [IO.Path]::GetFullPath($OutputDirectory)
$exe = Join-Path $output 'Setup.exe'
if (Test-Path -LiteralPath $exe) { throw 'Output executable already exists; use a new build directory.' }
$null = New-Item -ItemType Directory -Path $output -Force
$arguments = @('/nologo', '/define:SETUP', '/target:winexe', '/platform:x64',
    '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
    '/reference:System.Web.Extensions.dll', '/reference:System.IO.Compression.dll',
    '/reference:System.IO.Compression.FileSystem.dll',
    ('/win32manifest:' + (Join-Path $root 'ui\app.manifest')),
    ('/win32icon:' + (Join-Path $root 'ui\AppIcon.ico')),
    ('/out:' + $exe), (Join-Path $root 'ui\Setup.cs'))
& $compiler @arguments
if ($LASTEXITCODE -ne 0) { throw 'Setup compilation failed.' }
$file = Get-Item -LiteralPath $exe
$algorithm = [Security.Cryptography.SHA256]::Create()
$stream = [IO.File]::OpenRead($exe)
try { $sha256 = [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '') }
finally { $stream.Dispose(); $algorithm.Dispose() }
[pscustomobject]@{Executable=$exe;Bytes=$file.Length;SHA256=$sha256;BuiltUTC=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json
