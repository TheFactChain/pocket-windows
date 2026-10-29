[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$SourceArchive,
      [Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference='Stop'
if($PSVersionTable.PSEdition -ne 'Core'){throw 'Build this developer component with PowerShell 7 (its bundled Roslyn compiler is required).'}
Add-Type -Path (Join-Path $PSHOME 'Microsoft.CodeAnalysis.dll')
Add-Type -Path (Join-Path $PSHOME 'Microsoft.CodeAnalysis.CSharp.dll')
Add-Type -AssemblyName System.IO.Compression.FileSystem
$root=Split-Path -Parent $PSScriptRoot
$localNames=@('guest/files/GuestFiles.cs','guest/GuestEnvironment.cs','guest/GuestClipboard.cs','guest/ClipboardFiles.cs','guest/ClipboardStage.cs','guest/ClipboardProtocol.cs','guest/ClipboardTransport.cs','ui/app.manifest','scripts/Build-GuestFiles.ps1','scripts/Build-HostFiles.ps1')
$localNames+=@(Get-ChildItem -LiteralPath (Join-Path $root 'host\files') -Filter '*.cs' -File | Sort-Object Name | ForEach-Object { 'host/files/'+$_.Name })
$localNames+='host/files/dependency-patches/SMBServer.Signatures.cs'
$localNames+='patches/smblibrary/smblibrary-1.5.8.patch'
function Get-LocalSources {
    @($localNames | ForEach-Object {
        $item=Get-Item -LiteralPath (Join-Path $root $_)
        [ordered]@{path=$_;bytes=$item.Length;sha256=(Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
    })
}
$localSources=Get-LocalSources
$archive=[IO.Path]::GetFullPath($SourceArchive)
if((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne '2240C49F69AAFFA5225E8316D5A5440AB2F20BC577BD3161091905F05B94D318'){throw 'Source archive hash mismatch'}
$output=[IO.Path]::GetFullPath($OutputDirectory)
if(Test-Path -LiteralPath $output){throw 'Output already exists; use a new build directory.'}
$target=Join-Path (Split-Path -Parent $output) ('host-files-source-'+[Guid]::NewGuid().ToString('N'))
[IO.Compression.ZipFile]::ExtractToDirectory($archive,$target)
$source=Join-Path $target 'TalAloni-SMBLibrary-2edbcf3'
$null=New-Item -ItemType Directory -Path $output
$patch=Join-Path $root 'patches\smblibrary\smblibrary-1.5.8.patch'
$git=Get-Command git -CommandType Application -ErrorAction Stop | Select-Object -First 1
& $git.Source -C $source apply --check --ignore-space-change --whitespace=nowarn $patch
if($LASTEXITCODE -ne 0){throw 'Pinned SMBLibrary patch does not apply cleanly.'}
& $git.Source -C $source apply --ignore-space-change --whitespace=nowarn $patch
if($LASTEXITCODE -ne 0){throw 'Pinned SMBLibrary patch failed.'}
Copy-Item -LiteralPath (Join-Path $root 'host\files\dependency-patches\SMBServer.Signatures.cs') -Destination (Join-Path $source 'SMBLibrary\Server\SMBServer.Signatures.cs')
$framework=Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319'
$baseReferences=@('mscorlib.dll','System.dll','System.Core.dll','System.Security.dll','System.Xml.dll') | ForEach-Object { [Microsoft.CodeAnalysis.MetadataReference]::CreateFromFile((Join-Path $framework $_)) }
function Compile-Library([string]$name,[string[]]$references){
    [Microsoft.CodeAnalysis.SyntaxTree[]]$trees=@(Get-ChildItem -LiteralPath (Join-Path $source $name) -Filter '*.cs' -Recurse -File | ForEach-Object {[Microsoft.CodeAnalysis.CSharp.CSharpSyntaxTree]::ParseText([IO.File]::ReadAllText($_.FullName))})
    [Microsoft.CodeAnalysis.MetadataReference[]]$refs=@($baseReferences)+@($references | ForEach-Object {[Microsoft.CodeAnalysis.MetadataReference]::CreateFromFile((Join-Path $output $_))})
    $options=[Microsoft.CodeAnalysis.CSharp.CSharpCompilationOptions]::new([Microsoft.CodeAnalysis.OutputKind]::DynamicallyLinkedLibrary).WithDeterministic($true)
    $compilation=[Microsoft.CodeAnalysis.CSharp.CSharpCompilation]::Create($name,$trees,$refs,$options)
    $stream=[IO.File]::Open((Join-Path $output ($name+'.dll')),[IO.FileMode]::CreateNew)
    try {$result=$compilation.Emit($stream)} finally {$stream.Dispose()}
    if(-not $result.Success){$result.Diagnostics | ForEach-Object ToString; throw ('Compilation failed: '+$name)}
}
Compile-Library 'Utilities' @()
Compile-Library 'SMBLibrary' @('Utilities.dll')
Compile-Library 'SMBLibrary.Win32' @('Utilities.dll','SMBLibrary.dll')
$compiler=Join-Path $framework 'csc.exe'
$arguments=@('/nologo','/target:exe','/platform:x64','/main:HostFilesServer','/reference:System.Web.Extensions.dll',
    ('/reference:'+(Join-Path $output 'Utilities.dll')),('/reference:'+(Join-Path $output 'SMBLibrary.dll')),
    ('/reference:'+(Join-Path $output 'SMBLibrary.Win32.dll')),('/out:'+(Join-Path $output 'HostFiles.exe')))
$arguments+=@(Get-ChildItem -LiteralPath (Join-Path $root 'host\files') -Filter '*.cs' -File | Sort-Object Name | ForEach-Object FullName)
& $compiler @arguments
if($LASTEXITCODE -ne 0){throw 'Host files helper compilation failed.'}
& (Join-Path $PSScriptRoot 'Build-GuestFiles.ps1') -OutputDirectory $output | Out-Null
Copy-Item -LiteralPath (Join-Path $source 'License.txt') -Destination (Join-Path $output 'SMBLibrary.LICENSE.txt')
[IO.Compression.ZipFile]::CreateFromDirectory($source,(Join-Path $output 'SMBLibrary-source.zip'))
@'
SMBLibrary v1.5.8, Tal Aloni and contributors, LGPL-3.0-or-later.
Pinned upstream commit: 2edbcf3161084b51dbe11b8a960b0bc7c224b851
https://github.com/TalAloni/SMBLibrary/tree/v1.5.8
Local changes: require/verify SMB2 request signatures before dispatch;
reject authentication compounds; inherit related session/tree identifiers;
retain LOGOFF and async response identities/signing keys; bound pending notifications;
CANCEL completes only the original request; refresh directory scans and report truncated
first records; release native open and directory-pattern buffers.
NTLMv1 is disabled. SMB1 is disabled in HostFiles.exe.
Corresponding modified source and upstream license are included beside the DLLs.
Build procedure: scripts/Build-HostFiles.ps1 in the Windows Claude source tree.
'@ | Set-Content -LiteralPath (Join-Path $output 'NOTICE.txt') -Encoding utf8
if(($localSources | ConvertTo-Json -Depth 5 -Compress) -ne ((Get-LocalSources) | ConvertTo-Json -Depth 5 -Compress)){throw 'Local build source changed during compilation.'}
$files=@(Get-ChildItem -LiteralPath $output -File | Where-Object Name -ne 'build-manifest.json' | Sort-Object Name | ForEach-Object {
    [ordered]@{name=$_.Name;bytes=$_.Length;sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
})
$report=[ordered]@{schema=1;component='host-files';upstream_commit='2edbcf3161084b51dbe11b8a960b0bc7c224b851';source_archive_sha256='2240c49f69aaffa5225e8316d5a5440ab2f20bc577bd3161091905f05b94d318';local_sources=$localSources;files=$files}
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $output 'build-manifest.json') -Encoding utf8
$report | ConvertTo-Json -Depth 5
