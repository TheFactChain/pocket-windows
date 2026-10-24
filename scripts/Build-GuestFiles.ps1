[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw 'Installed .NET Framework compiler is required.' }
$output = [IO.Path]::GetFullPath($OutputDirectory)
$exe = Join-Path $output 'HostFilesGuest.exe'
if (Test-Path -LiteralPath $exe) { throw 'Output executable already exists; use a new build directory.' }
$null = New-Item -ItemType Directory -Path $output -Force
& $compiler /nologo /target:winexe /platform:x64 /reference:System.Web.Extensions.dll /reference:Microsoft.CSharp.dll /reference:System.Windows.Forms.dll ('/win32manifest:' + (Join-Path $root 'ui\app.manifest')) ('/out:' + $exe) (Join-Path $root 'guest\files\GuestFiles.cs') (Join-Path $root 'guest\GuestEnvironment.cs') (Join-Path $root 'guest\GuestClipboard.cs')
if ($LASTEXITCODE -ne 0) { throw 'Guest files compilation failed.' }
$file = Get-Item -LiteralPath $exe
[pscustomobject]@{Executable=$exe;Bytes=$file.Length;SHA256=(Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash;BuiltUTC=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json
