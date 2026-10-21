param([string]$Project = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$result = [ordered]@{ ok = $false; code = 'error'; message = 'Component preparation failed. No user data was changed.'; checked_files = 0; repaired_files = 0 }
$lock = $null
$archive = $null
$stage = $null

function Assert-PlainPath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Reparse paths are not supported.'
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }
        $current = $parent
    }
}

function Assert-RuntimeName([string]$Name) {
    if ($Name -notmatch '^runtime/(python-3\.12\.14|qemu-11\.1\.0)/[^/]+(/[^/]+)*$' -or
        $Name.Contains('\') -or $Name.Contains(':') -or
        @($Name.Split('/') | Where-Object { $_ -in @('.', '..') -or $_.EndsWith('.') -or $_.EndsWith(' ') -or
            $_ -match '[<>"|?*\x00-\x1f]' -or $_ -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)' }).Count) {
        throw 'Invalid component path.'
    }
}

function Test-Component([string]$Path, $Record) {
    Assert-PlainPath $Path
    if (-not [IO.File]::Exists($Path)) { return $false }
    if ((Get-Item -LiteralPath $Path).Length -ne [long]$Record.bytes) { return $false }
    $stream = [IO.File]::OpenRead($Path)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '')
        return $hash -eq $Record.sha256
    } finally {
        $hasher.Dispose()
        $stream.Dispose()
    }
}

try {
    $Project = [IO.Path]::GetFullPath($Project)
    Assert-PlainPath $Project
    $manifestPath = Join-Path $Project 'runtime/components-manifest.json'
    Assert-PlainPath $manifestPath
    if (-not [IO.File]::Exists($manifestPath)) {
        $result.code = 'package_missing'
        $result.message = 'Restore the complete portable distribution including runtime/components-manifest.json, then retry.'
        throw 'Component manifest missing.'
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $records = @{}
    foreach ($record in $manifest.files) {
        Assert-RuntimeName $record.path
        if ($records.ContainsKey($record.path) -or $record.sha256 -notmatch '^[0-9a-fA-F]{64}$' -or [long]$record.bytes -lt 0) { throw 'Invalid manifest.' }
        $records[$record.path] = $record
    }
    if ($records.Count -eq 0) { throw 'Empty component manifest.' }
    $state = Join-Path $Project 'state'
    $lockPath = Join-Path $state 'runtime.lock'
    Assert-PlainPath $lockPath
    [IO.Directory]::CreateDirectory($state) | Out-Null
    try {
        $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
        $lock.Lock(0, 1)
    } catch {
        $result.code = 'runtime_busy'
        $result.message = 'Another runtime operation is active. Wait and try again.'
        throw 'Runtime locked.'
    }
    $needed = @()
    foreach ($name in $records.Keys) {
        if (-not (Test-Component (Join-Path $Project $name) $records[$name])) { $needed += $name }
        $result.checked_files++
    }
    if ($needed.Count -eq 0) {
        $result.ok = $true
        $result.code = 'ready'
        $result.message = 'Python and QEMU are verified and ready.'
    } else {
        $runtime = Join-Path $Project 'runtime'
        Assert-PlainPath $runtime
        $runtimePrefix = [IO.Path]::GetFullPath($runtime).TrimEnd('\') + '\'
        $processes = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'qemu%'")
        foreach ($process in $processes) {
            if (-not $process.ExecutablePath -or $process.ExecutablePath.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase)) {
                $result.code = 'runtime_busy'
                $result.message = 'Shut down the virtual machines before repairing components.'
                throw 'QEMU is active.'
            }
        }
        $package = Join-Path $runtime 'components-recovery.zip'
        Assert-PlainPath $package
        if (-not [IO.File]::Exists($package)) {
            $result.code = 'package_missing'
            $result.message = 'Restore runtime/components-recovery.zip from the portable distribution, then retry.'
            throw 'Recovery package missing.'
        }
        $result.code = 'invalid_package'
        $result.message = 'The recovery package is invalid. Restore it from the portable distribution.'
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [IO.Compression.ZipFile]::OpenRead($package)
        $entries = @{}
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName
            Assert-RuntimeName $name
            $unixType = ($entry.ExternalAttributes -shr 16) -band 61440
            if ($entries.ContainsKey($name) -or -not $records.ContainsKey($name) -or
                $unixType -eq 40960 -or ($entry.ExternalAttributes -band 1024) -ne 0 -or
                $entry.Length -ne [long]$records[$name].bytes) { throw 'Invalid archive entry.' }
            $entries[$name] = $entry
        }
        if ($entries.Count -ne $records.Count) { throw 'Incomplete recovery package.' }
        $stage = Join-Path $runtime ('.repair-' + [Guid]::NewGuid().ToString('N'))
        Assert-PlainPath $stage
        [IO.Directory]::CreateDirectory($stage) | Out-Null
        foreach ($name in $needed) {
            $destination = Join-Path $stage $name
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entries[$name], $destination, $false)
            if (-not (Test-Component $destination $records[$name])) { throw 'Component verification failed.' }
        }
        $result.code = 'error'
        $result.message = 'Component repair could not finish. Retry after closing programs that use this runtime.'
        foreach ($name in $needed) {
            $destination = Join-Path $Project $name
            Assert-PlainPath $destination
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
            $source = Join-Path $stage $name
            if ([IO.File]::Exists($destination)) { [IO.File]::Replace($source, $destination, [NullString]::Value) }
            else { [IO.File]::Move($source, $destination) }
            $result.repaired_files++
        }
        $result.ok = $true
        $result.code = 'repaired'
        $result.message = 'Python and QEMU components were restored and verified.'
    }
} catch {
    # Return only the public result; exception details can contain private host paths.
} finally {
    if ($archive) { $archive.Dispose() }
    if ($stage -and [IO.Directory]::Exists($stage)) {
        try {
            Assert-PlainPath $stage
            if ([IO.Path]::GetDirectoryName($stage) -ne (Join-Path $Project 'runtime') -or
                [IO.Path]::GetFileName($stage) -notmatch '^\.repair-[0-9a-f]{32}$') { throw 'Invalid staging directory.' }
            Remove-Item -LiteralPath $stage -Recurse -Force
        } catch { }
    }
    if ($lock) { $lock.Dispose() }
}
$result | ConvertTo-Json -Compress
