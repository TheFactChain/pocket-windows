# Windows PowerShell 5.1: acceptance of an already-built binary, without showing a window.
[CmdletBinding()]
param(
    [string]$Launcher = '',
    [string]$ExpectedSha256 = 'D8DB776B7C4A17FB356DE3B57EDEC8657D613519A090092C8E41475A5E947EC1'
)
$ErrorActionPreference = 'Stop'
$project = Split-Path $PSScriptRoot -Parent
if ([string]::IsNullOrWhiteSpace($Launcher)) { $Launcher = Join-Path $project 'Launcher.exe' }
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')
$evidence = Join-Path $project ('logs/acceptance-launcher-' + $stamp + '.json')
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('acceptance-launcher-' + [Guid]::NewGuid().ToString('N'))
$checks = [ordered]@{}
$actions = [Collections.Generic.List[string]]::new()
$report = [ordered]@{ schema = 1; acceptance = 'exact_binary'; started_utc = $stamp; ok = $false;
    expected_sha256 = $ExpectedSha256.ToLowerInvariant(); checks = $checks; actions = $actions;
    launcher_recompiled = $false; window_shown = $false; vm_started = $false;
    production_profiles_accessed = $false; secret_values_exposed = $false }

function Assert-Plain([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Reparse path refused.'
            }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }
        $current = $parent
    }
}

function Get-Sha([string]$Path) {
    Assert-Plain $Path
    $stream = [IO.File]::OpenRead($Path)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose(); $stream.Dispose() }
}

function Check([string]$Name, [bool]$Passed) {
    $checks[$Name] = $Passed
    if (-not $Passed) { throw 'Acceptance assertion failed.' }
}

function Copy-PlainTree([string]$Source, [string]$Destination) {
    Assert-Plain $Source
    $null = [IO.Directory]::CreateDirectory($Destination)
    foreach ($item in Get-ChildItem -LiteralPath $Source -Force) {
        Assert-Plain $item.FullName
        $target = Join-Path $Destination $item.Name
        if ($item.PSIsContainer) { Copy-PlainTree $item.FullName $target }
        else { [IO.File]::Copy($item.FullName, $target, $false) }
    }
}

function Invoke-Bridge([string]$Action, $Payload = $null, [string]$Root = $script:fixture) {
    if ($Action -notin @('get-settings', 'save-settings', 'save-profile', 'get-profile-view',
                         'select-profile', 'list-profiles', 'export-profile')) { throw 'Action refused.' }
    $actions.Add($Action)
    $value = if ($null -eq $Payload) { $null } else {
        $script:serializer.DeserializeObject(($Payload | ConvertTo-Json -Depth 12 -Compress))
    }
    $reply = $script:bridgeRun.Invoke($null, [object[]]@($Root, $Action, $value))
    $encoded = $script:serializer.Serialize($reply)
    Check ('redacted_reply_' + $actions.Count) (-not $encoded.Contains($script:fixtureSecret))
    Write-Output -NoEnumerate $reply
}

try {
    Assert-Plain $Launcher
    Check 'supplied_binary_sha256' ((Get-Sha $Launcher) -ceq $ExpectedSha256.ToLowerInvariant())
    $report.launcher = [IO.Path]::GetFullPath($Launcher)
    $report.actual_sha256 = Get-Sha $Launcher
    $null = [IO.Directory]::CreateDirectory($temporary)
    $script:fixture = Join-Path $temporary 'portable fixture'
    $null = [IO.Directory]::CreateDirectory((Join-Path $fixture 'scripts'))
    foreach ($source in Get-ChildItem -LiteralPath (Join-Path $project 'scripts') -Filter '*.py' -File) {
        Assert-Plain $source.FullName
        [IO.File]::Copy($source.FullName, (Join-Path $fixture ('scripts/' + $source.Name)), $false)
    }
    Copy-PlainTree (Join-Path $project 'runtime/python-3.12.14') (Join-Path $fixture 'runtime/python-3.12.14')
    $null = [IO.Directory]::CreateDirectory((Join-Path $fixture 'state'))
    Check 'fixture_has_no_disks_or_private_data' (-not (Test-Path -LiteralPath (Join-Path $fixture 'images')) -and
        -not (Test-Path -LiteralPath (Join-Path $fixture 'private')) -and -not (Test-Path -LiteralPath (Join-Path $fixture 'profiles')))
    $hashes = [ordered]@{}
    foreach ($name in @('portable_app.py', 'profile_store.py', 'connection_profile.py', 'runtime_config.py', 'launch_runtime.py')) {
        $hashes[$name] = Get-Sha (Join-Path $fixture ('scripts/' + $name))
        Check ('fixture_copy_' + $name) ($hashes[$name] -ceq (Get-Sha (Join-Path $project ('scripts/' + $name))))
    }
    $report.fixture_script_sha256 = $hashes
    $null = [Reflection.Assembly]::Load('System.Web.Extensions, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35')
    $script:serializer = [Web.Script.Serialization.JavaScriptSerializer]::new()
    $assembly = [Reflection.Assembly]::LoadFrom([IO.Path]::GetFullPath($Launcher))
    $script:bridgeRun = $assembly.GetType('Bridge', $true).GetMethod('Run', [Reflection.BindingFlags]'Static, NonPublic')
    Check 'exact_binary_bridge_loaded' ($null -ne $bridgeRun -and $assembly.Location -eq [IO.Path]::GetFullPath($Launcher))
    $script:fixtureSecret = 'acceptance-only-' + [Guid]::NewGuid().ToString('N')

    $settings = Invoke-Bridge 'get-settings'
    Check 'settings_read' ($settings['ok'] -is [bool] -and $settings['ok'])
    $desired = $serializer.DeserializeObject($serializer.Serialize($settings['settings']))
    $desired['lan_port'] = 33861
    $saved = Invoke-Bridge 'save-settings' $desired
    $loaded = Invoke-Bridge 'get-settings'
    Check 'settings_stdin_roundtrip' ($saved['ok'] -and $loaded['ok'] -and $loaded['settings']['lan_port'] -eq 33861)

    $saved = Invoke-Bridge 'save-profile' @{ profile = @{ name = 'Acceptance profile'; protocol = 'hysteria2';
        host = 'example.com'; port = 443; credentials = @{ auth = $fixtureSecret } } }
    Check 'profile_saved_real_dpapi' ($saved['ok'] -and $saved['profile']['auth_present'] -and $saved['profile']['secrets_available'])
    $id = [string]$saved['profile']['profile_id']
    $revision = [string]$saved['profile']['revision_id']
    Check 'profile_identifiers' ($id -cmatch '^[a-f0-9]{32}$' -and $revision -cmatch '^[a-f0-9]{32}$')
    $storePath = Join-Path $fixture 'profiles/profiles.json'
    Check 'encrypted_store_has_no_plaintext_password' (-not [IO.File]::ReadAllText($storePath).Contains($fixtureSecret))
    $view = Invoke-Bridge 'get-profile-view' @{ profile_id = $id }
    Check 'profile_view_is_public' ($view['ok'] -and -not $view['profile'].ContainsKey('credentials') -and -not $view['profile']['needs_credentials'])
    $updated = Invoke-Bridge 'save-profile' @{ profile_id = $id; profile = @{ name = 'Updated acceptance profile';
        protocol = 'hysteria2'; host = 'updated.example.com'; port = 443 } }
    Check 'update_retains_omitted_dpapi_credentials' ($updated['ok'] -and $updated['profile']['host'] -eq 'updated.example.com' -and
        $updated['profile']['revision_id'] -ne $revision -and $updated['profile']['auth_present'] -and $updated['profile']['secrets_available'])
    $selected = Invoke-Bridge 'select-profile' @{ profile_id = $id }
    Check 'selection_is_pending_not_verified' ($selected['ok'] -and $selected['profile']['selected'] -and
        $selected['profile']['state'] -eq 'pending' -and -not $selected['profile']['verified'] -and $null -eq $selected['profile']['applied_revision_id'])
    $listed = Invoke-Bridge 'list-profiles'
    Check 'selected_profile_listed' ($listed['ok'] -and $listed['profiles'].Count -eq 1 -and
        $listed['profiles'][0]['profile_id'] -eq $id -and $listed['profiles'][0]['selected'])
    $publicPath = Join-Path $fixture 'public-export.json'
    $exported = Invoke-Bridge 'export-profile' @{ profile_id = $id; destination = $publicPath; include_secrets = $false }
    Check 'public_export_redacted' ($exported['ok'] -and -not [IO.File]::ReadAllText($publicPath).Contains($fixtureSecret))
    Check 'updated_store_still_encrypted' (-not [IO.File]::ReadAllText($storePath).Contains($fixtureSecret))

    $missingRoot = Join-Path $temporary 'missing runtime'
    $null = [IO.Directory]::CreateDirectory($missingRoot)
    $missing = Invoke-Bridge 'get-settings' $null $missingRoot
    Check 'missing_python_offers_preparation' (-not $missing['ok'] -and $missing['blockers'][0]['action'] -eq 'prepare')

    $selfReport = Join-Path $temporary 'binary-self-test.json'
    $info = [Diagnostics.ProcessStartInfo]::new([IO.Path]::GetFullPath($Launcher), ('--self-test "' + $selfReport + '"'))
    $info.UseShellExecute = $false; $info.CreateNoWindow = $true; $info.WorkingDirectory = $temporary
    $process = [Diagnostics.Process]::Start($info)
    try { Check 'exact_binary_self_test_exit' ($process.WaitForExit(30000) -and $process.ExitCode -eq 0) }
    finally { $process.Dispose() }
    $self = $serializer.DeserializeObject([IO.File]::ReadAllText($selfReport))
    Check 'exact_binary_self_test_checks' ($self['checks'].Count -ge 4 -and @($self['checks'].Values | Where-Object { $_ -ne $true }).Count -eq 0)
    $report.self_test_checks = $self['checks']
    Check 'binary_unchanged' ((Get-Sha $Launcher) -ceq $ExpectedSha256.ToLowerInvariant())
    $report.ok = $true
} catch {
    # No raw exception/reply is emitted: it could include a fixture secret on a broken boundary.
    $report.error = 'Exact binary acceptance failed; inspect named boolean checks.'
} finally {
    if (Test-Path -LiteralPath $temporary) {
        try {
            $resolved = [IO.Path]::GetFullPath($temporary)
            $tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
            if (-not $resolved.StartsWith($tempParent, [StringComparison]::OrdinalIgnoreCase) -or
                [IO.Path]::GetFileName($resolved) -notmatch '^acceptance-launcher-[a-f0-9]{32}$') { throw 'Cleanup path refused.' }
            Assert-Plain $resolved
            Remove-Item -LiteralPath $resolved -Recurse -Force
            $report.fixture_removed = $true
        } catch { $report.fixture_removed = $false; $report.ok = $false }
    }
    Assert-Plain (Split-Path $evidence -Parent)
    $json = $report | ConvertTo-Json -Depth 12
    $stream = [IO.File]::Open($evidence, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json); $stream.Write($bytes, 0, $bytes.Length) }
    finally { $stream.Dispose() }
}
[Console]::WriteLine(($report.ok.ToString() + ' ' + $evidence))
if (-not $report.ok) { exit 1 }
