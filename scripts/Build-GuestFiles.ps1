[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$localNames = @('guest/files/GuestFiles.cs', 'guest/GuestEnvironment.cs', 'guest/GuestClipboard.cs', 'guest/ClipboardFiles.cs', 'guest/ClipboardStage.cs', 'guest/ClipboardProtocol.cs', 'guest/ClipboardTransport.cs', 'ui/app.manifest', 'scripts/Build-GuestFiles.ps1', 'scripts/Build-HostFiles.ps1')
function Get-LocalSources {
    @($localNames | ForEach-Object {
        $item = Get-Item -LiteralPath (Join-Path $root $_)
        [ordered]@{path=$_;bytes=$item.Length;sha256=(Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
    })
}
$localSources = Get-LocalSources
$compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw 'Installed .NET Framework compiler is required.' }
$output = [IO.Path]::GetFullPath($OutputDirectory)
$exe = Join-Path $output 'HostFilesGuest.exe'
if (Test-Path -LiteralPath $exe) { throw 'Output executable already exists; use a new build directory.' }
$null = New-Item -ItemType Directory -Path $output -Force
& $compiler /nologo /target:winexe /platform:x64 /reference:System.Web.Extensions.dll /reference:Microsoft.CSharp.dll /reference:System.Windows.Forms.dll /reference:System.Drawing.dll ('/win32manifest:' + (Join-Path $root 'ui\app.manifest')) ('/out:' + $exe) ($localNames | Where-Object { $_.EndsWith('.cs') } | ForEach-Object { Join-Path $root $_ })
if ($LASTEXITCODE -ne 0) { throw 'Guest files compilation failed.' }
if (($localSources | ConvertTo-Json -Depth 5 -Compress) -ne ((Get-LocalSources) | ConvertTo-Json -Depth 5 -Compress)) { throw 'Guest build source changed during compilation.' }
$file = Get-Item -LiteralPath $exe
$manifest = [ordered]@{schema=1;component='host-files';local_sources=$localSources;files=@([ordered]@{name=$file.Name;bytes=$file.Length;sha256=(Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()})}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $output 'build-manifest.json') -Encoding utf8
[pscustomobject]@{Executable=$exe;Bytes=$file.Length;SHA256=(Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash;BuiltUTC=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json
