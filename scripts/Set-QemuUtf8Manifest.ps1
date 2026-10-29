[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$InputDirectory,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [string]$ManifestTool
)
$ErrorActionPreference = 'Stop'
if (-not ('QemuManifestResource' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class QemuManifestResource {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr LoadLibraryExW(string path, IntPtr file, uint flags);
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr FindResourceW(IntPtr module, IntPtr name, IntPtr type);
    [DllImport("kernel32.dll")] static extern bool FreeLibrary(IntPtr module);
    public static bool HasManifest(string path) {
        IntPtr module=LoadLibraryExW(path, IntPtr.Zero, 2);
        if (module==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
        try {
            if (FindResourceW(module, new IntPtr(1), new IntPtr(24))!=IntPtr.Zero) return true;
            int error=Marshal.GetLastWin32Error();
            if (error==1813 || error==1814) return false;
            throw new Win32Exception(error);
        } finally { FreeLibrary(module); }
    }
}
'@
}
$inputRoot = [IO.Path]::GetFullPath($InputDirectory)
$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $outputRoot) { throw 'Use a new output directory for the QEMU candidates.' }
if (-not $ManifestTool) {
    $sdk = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    $ManifestTool = Get-ChildItem -LiteralPath $sdk -Directory |
        Where-Object { $_.Name -match '^10\.\d+\.\d+\.\d+$' } |
        Sort-Object { [version]$_.Name } -Descending |
        ForEach-Object { Join-Path $_.FullName 'x64\mt.exe' } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
}
if (-not $ManifestTool -or -not (Test-Path -LiteralPath $ManifestTool -PathType Leaf)) { throw 'Windows SDK mt.exe is required.' }
$names = @('qemu-system-x86_64.exe', 'qemu-img.exe')
foreach ($name in $names) {
    $source = Get-Item -LiteralPath (Join-Path $inputRoot $name)
    if ($source.PSIsContainer -or ($source.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Not a regular QEMU executable: $name" }
    $stream = [IO.File]::OpenRead($source.FullName)
    try { if ($stream.ReadByte() -ne 77 -or $stream.ReadByte() -ne 90) { throw "Not an MZ executable: $name" } }
    finally { $stream.Dispose() }
}
$null = New-Item -ItemType Directory -Path $outputRoot
$records = @()
foreach ($name in $names) {
    $source = Join-Path $inputRoot $name
    $candidate = Join-Path $outputRoot $name
    $manifestPath = Join-Path $outputRoot ($name + '.manifest')
    Copy-Item -LiteralPath $source -Destination $candidate
    if ([QemuManifestResource]::HasManifest($candidate)) {
        & $ManifestTool '-nologo' ('-inputresource:' + $candidate + ';#1') ('-out:' + $manifestPath)
        if ($LASTEXITCODE -ne 0) { throw "Cannot extract the original manifest: $name" }
    } else {
        '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0" />' |
            Set-Content -LiteralPath $manifestPath -Encoding utf8
    }
    $document = New-Object Xml.XmlDocument
    $document.XmlResolver = $null
    $document.Load($manifestPath)
    $namespaces = New-Object Xml.XmlNamespaceManager($document.NameTable)
    $namespaces.AddNamespace('asm', 'urn:schemas-microsoft-com:asm.v1')
    $namespaces.AddNamespace('asm3', 'urn:schemas-microsoft-com:asm.v3')
    $namespaces.AddNamespace('w2019', 'http://schemas.microsoft.com/SMI/2019/WindowsSettings')
    $application = $document.SelectSingleNode('/asm:assembly/asm3:application', $namespaces)
    if (-not $application) {
        $application = $document.CreateElement('application', 'urn:schemas-microsoft-com:asm.v3')
        $null = $document.DocumentElement.AppendChild($application)
    }
    $settings = $application.SelectSingleNode('asm3:windowsSettings', $namespaces)
    if (-not $settings) {
        $settings = $document.CreateElement('windowsSettings', 'urn:schemas-microsoft-com:asm.v3')
        $null = $application.AppendChild($settings)
    }
    $codePage = $settings.SelectSingleNode('w2019:activeCodePage', $namespaces)
    if (-not $codePage) {
        $codePage = $document.CreateElement('activeCodePage', 'http://schemas.microsoft.com/SMI/2019/WindowsSettings')
        $null = $settings.AppendChild($codePage)
    }
    $codePage.InnerText = 'UTF-8'
    $document.Save($manifestPath)
    & $ManifestTool '-nologo' '-manifest' $manifestPath ('-outputresource:' + $candidate + ';#1')
    if ($LASTEXITCODE -ne 0) { throw "Cannot embed the UTF-8 manifest: $name" }
    & $ManifestTool '-nologo' ('-inputresource:' + $candidate + ';#1') ('-out:' + $manifestPath)
    if ($LASTEXITCODE -ne 0) { throw "Cannot verify the embedded manifest: $name" }
    $document.Load($manifestPath)
    $actual = $document.SelectNodes('/asm:assembly/asm3:application/asm3:windowsSettings/w2019:activeCodePage', $namespaces)
    if ($actual.Count -ne 1 -or $actual[0].InnerText -ne 'UTF-8') { throw "UTF-8 manifest verification failed: $name" }
    $records += [ordered]@{name=$name; source_sha256=(Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant();
        bytes=(Get-Item -LiteralPath $candidate).Length; sha256=(Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()}
}
[ordered]@{schema=1; active_code_page='UTF-8'; files=$records} | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath (Join-Path $outputRoot 'qemu-utf8-build.json') -Encoding utf8
$records | ConvertTo-Json
