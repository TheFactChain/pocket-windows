[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$SourceArchive,
      [Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference='Stop'
if($PSVersionTable.PSEdition -ne 'Core'){throw 'Build this developer component with PowerShell 7 (its bundled Roslyn compiler is required).'}
Add-Type -Path (Join-Path $PSHOME 'Microsoft.CodeAnalysis.dll')
Add-Type -Path (Join-Path $PSHOME 'Microsoft.CodeAnalysis.CSharp.dll')
Add-Type -AssemblyName System.IO.Compression.FileSystem
$root=Split-Path -Parent $PSScriptRoot
$localNames=@('guest/files/GuestFiles.cs','guest/GuestEnvironment.cs','guest/GuestClipboard.cs','ui/app.manifest','scripts/Build-GuestFiles.ps1','scripts/Build-HostFiles.ps1')
$localNames+=@(Get-ChildItem -LiteralPath (Join-Path $root 'host\files') -Filter '*.cs' -File | Sort-Object Name | ForEach-Object { 'host/files/'+$_.Name })
$localNames+='host/files/dependency-patches/SMBServer.Signatures.cs'
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
$target=Join-Path $root ('state\host-files-build-'+[Guid]::NewGuid().ToString('N'))
[IO.Compression.ZipFile]::ExtractToDirectory($archive,$target)
$source=Join-Path $target 'TalAloni-SMBLibrary-2edbcf3'
$null=New-Item -ItemType Directory -Path $output
& {
    $path=Join-Path $source 'SMBLibrary\Server\SMBServer.SMB2.cs'
    $script:code=[IO.File]::ReadAllText($path).Replace("`r`n","`n")
    function Replace-Source([string]$old,[string]$new,[int]$count=1){
        if(($script:code.Split(@($old),[StringSplitOptions]::None)).Count -ne $count+1){throw ('Patch context mismatch: '+$path+' '+$old)}
        $script:code=$script:code.Replace($old,$new)
    }
    Replace-Source '            FileID? fileID = null;' "            byte[] responseSigningKey = null;`n            FileID? fileID = null;"
    Replace-Source '            NTStatus? fileIDStatus = null;' "            NTStatus? fileIDStatus = null;`n            ulong relatedSessionID = 0;`n            uint relatedTreeID = 0;"
    Replace-Source "            foreach (SMB2Command request in requestChain)`n            {" @'
            foreach (SMB2Command request in requestChain)
            {
                if (request.Header.IsRelatedOperations)
                {
                    request.Header.SessionID = relatedSessionID;
                    request.Header.TreeID = relatedTreeID;
                }
                else
                {
                    fileID = null;
                    fileIDStatus = null;
                }
'@
    Replace-Source "                SMB2Command response;`n" @'
                SMB2Session originalSession = state is SMB2ConnectionState ? ((SMB2ConnectionState)state).GetSession(request.Header.SessionID) : null;
                if (responseChain.Count == 0 && originalSession != null && originalSession.SigningKey != null)
                    responseSigningKey = (byte[])originalSession.SigningKey.Clone();
                SMB2Command response;
'@
    Replace-Source '                    UpdateSMB2Header(response, request, state);' @'
                    UpdateSMB2Header(response, request, state);
                    relatedSessionID = response.Header.SessionID;
                    relatedTreeID = response.Header.TreeID;
                    if (response is LogoffResponse && originalSession != null && originalSession.SigningRequired)
                        response.Header.IsSigned = true;
'@
    Replace-Source '                EnqueueResponseChain(state, responseChain);' '                EnqueueResponseChain(state, responseChain, responseSigningKey);'
    Replace-Source 'private static void EnqueueResponseChain(ConnectionState state, List<SMB2Command> responseChain)' 'private static void EnqueueResponseChain(ConnectionState state, List<SMB2Command> responseChain, byte[] fallbackSigningKey = null)'
    Replace-Source '            byte[] signingKey = null;' '            byte[] signingKey = fallbackSigningKey;'
    [IO.File]::WriteAllText($path,$code,[Text.UTF8Encoding]::new($false))
    $path=Join-Path $source 'SMBLibrary\Server\SMBServer.cs'
    $script:code=[IO.File]::ReadAllText($path).Replace("`r`n","`n")
    Replace-Source '                    ProcessSMB2RequestChain(requestChain, ref state);' @'
                    if (!ValidateIncomingSignatures(packet.Trailer, requestChain, state))
                    {
                        state.ClientSocket.Close();
                        state.ReceiveBuffer.Dispose();
                        return;
                    }
                    ProcessSMB2RequestChain(requestChain, ref state);
'@
    [IO.File]::WriteAllText($path,$code,[Text.UTF8Encoding]::new($false))
    Copy-Item -LiteralPath (Join-Path $root 'host\files\dependency-patches\SMBServer.Signatures.cs') -Destination (Join-Path $source 'SMBLibrary\Server\SMBServer.Signatures.cs')
    $path=Join-Path $source 'SMBLibrary\Server\SMB2\NegotiateHelper.cs'
    $script:code=[IO.File]::ReadAllText($path).Replace("`r`n","`n")
    Replace-Source 'response.SecurityMode = SecurityMode.SigningEnabled;' 'response.SecurityMode = SecurityMode.SigningEnabled | SecurityMode.SigningRequired;' 2
    [IO.File]::WriteAllText($path,$code,[Text.UTF8Encoding]::new($false))
    $path=Join-Path $source 'SMBLibrary\Server\SMB2\SessionSetupHelper.cs'
    $script:code=[IO.File]::ReadAllText($path).Replace("`r`n","`n")
    Replace-Source 'bool signingRequired = (request.SecurityMode & SecurityMode.SigningRequired) > 0;' 'bool signingRequired = true;'
    [IO.File]::WriteAllText($path,$code,[Text.UTF8Encoding]::new($false))
    $path=Join-Path $source 'SMBLibrary.Win32\NTFileStore\NTDirectoryFileSystem.cs'
    $script:code=[IO.File]::ReadAllText($path).Replace("`r`n","`n")
    Replace-Source @'
            NTStatus status = NtCreateFile(out handle, (uint)desiredAccess, ref objectAttributes, out ioStatusBlock, ref allocationSize, fileAttributes, shareAccess, createDisposition, createOptions, IntPtr.Zero, 0);
            fileStatus = (FileStatus)ioStatusBlock.Information;
            return status;
'@ @'
            try
            {
                NTStatus status = NtCreateFile(out handle, (uint)desiredAccess, ref objectAttributes, out ioStatusBlock, ref allocationSize, fileAttributes, shareAccess, createDisposition, createOptions, IntPtr.Zero, 0);
                fileStatus = (FileStatus)ioStatusBlock.Information;
                return status;
            }
            finally
            {
                Marshal.FreeHGlobal(objectAttributes.ObjectName);
                objectName.Dispose();
            }
'@
    [IO.File]::WriteAllText($path,$code,[Text.UTF8Encoding]::new($false))
}
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
retain LOGOFF response signing key; release native open path buffers.
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
