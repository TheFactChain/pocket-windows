@echo off
setlocal DisableDelayedExpansion
set "WC_TEMPLATE_BOOTSTRAP=%~f0"
set "WC_TEMPLATE_PHASE=Bootstrap"
set "WC_TEMPLATE_WORKER=0"
if "%~1"=="--worker" set "WC_TEMPLATE_WORKER=1"
if "%~1"=="--audit" set "WC_TEMPLATE_PHASE=Audit"
if "%~1"=="--audit" set "WC_TEMPLATE_WORKER=1"
set "WC_TEMPLATE_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "WC_TEMPLATE_PS=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
"%WC_TEMPLATE_PS%" -NoProfile -Command "$ErrorActionPreference='Stop'; try {$source=[IO.File]::ReadAllText($env:WC_TEMPLATE_BOOTSTRAP);$marker='# WC_TEMPLATE_BODY';$offset=$source.LastIndexOf($marker,[StringComparison]::Ordinal);if($offset -lt 0 -or $source.Length -gt 131072){throw 'Invalid bootstrap.'};& ([scriptblock]::Create($source.Substring($offset+$marker.Length)))} catch {Write-Error 'Template bootstrap refused. Preserve local diagnostics; no cleanup or generalize was authorized.' -ErrorAction Continue;exit 1}"
set "WC_TEMPLATE_EXIT=%ERRORLEVEL%"
if "%WC_TEMPLATE_PHASE%"=="Audit" if not "%WC_TEMPLATE_EXIT%"=="0" pause
if "%WC_TEMPLATE_WORKER%"=="1" exit /b %WC_TEMPLATE_EXIT%
pause
exit /b %WC_TEMPLATE_EXIT%
# WC_TEMPLATE_BODY
function Assert-PlainPath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $entry = Get-Item -LiteralPath $current -Force
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse point refused.' }
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
}
function Get-PublicBytes([string]$Path, [int]$Limit) {
    Assert-PlainPath $Path
    $entry = Get-Item -LiteralPath $Path -Force
    if ($entry.PSIsContainer -or $entry.Length -gt $Limit) { throw 'Invalid source file.' }
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -gt $Limit) { throw 'Invalid source file.' }
    return ,$bytes
}
function Get-PublicHash([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
function Get-TemplatePolicy {
    $names = @('MachinePolicy', 'UserPolicy', 'Process', 'CurrentUser', 'LocalMachine')
    $rows = @(Get-ExecutionPolicy -List -ErrorAction Stop)
    if ($rows.Count -ne 5) { throw 'Execution policy query refused.' }
    $scopes = [ordered]@{}
    foreach ($row in $rows) {
        $name = [string]$row.Scope
        if ($name -cnotin $names -or $scopes.Contains($name) -or
            $row.ExecutionPolicy -isnot [Microsoft.PowerShell.ExecutionPolicy] -or
            -not [Enum]::IsDefined([Microsoft.PowerShell.ExecutionPolicy], $row.ExecutionPolicy)) {
            throw 'Execution policy query refused.'
        }
        $scopes[$name] = [string]$row.ExecutionPolicy
    }
    $mode = 'configured'
    if (@($scopes.Values | Where-Object { $_ -cne 'Undefined' }).Count -eq 0) { $mode = 'RemoteSigned' }
    return @{mode = $mode; scopes = $scopes}
}
function New-TemplateStartInfo([string]$Template, [string]$Mode) {
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if ($Mode -notin @('configured', 'RemoteSigned')) { throw 'Execution mode refused.' }
    $policyArgument = if ($Mode -eq 'RemoteSigned') { '-ExecutionPolicy RemoteSigned ' } else { '' }
    $info.Arguments = '-NoProfile ' + $policyArgument + '-File "' + $Template + '" -Phase ' + $env:WC_TEMPLATE_PHASE + ' -ReturnSerial'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [Console]::OutputEncoding
    $info.StandardErrorEncoding = [Console]::OutputEncoding
    return $info
}
function Invoke-LoggedTemplate([string]$Template, [string]$Directory) {
    $logPath = Join-Path $Directory 'template-output.log'
    $resultPath = Join-Path $Directory 'template-exit.json'
    Assert-PlainPath $logPath
    Assert-PlainPath $resultPath
    if ((Test-Path -LiteralPath $logPath) -or (Test-Path -LiteralPath $resultPath)) { throw 'Existing diagnostics refused.' }
    $log = [IO.File]::Open($logPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $process = $null
    $code = 1
    $stage = 'policy_query'
    $selection = @{mode = 'unavailable'; scopes = $null}
    $remaining = 65536
    try {
        $selection = Get-TemplatePolicy
        $summary = 'Execution policy mode=' + $selection.mode + '; ' + (($selection.scopes.GetEnumerator() | ForEach-Object { $_.Key + '=' + $_.Value }) -join '; ') + [Environment]::NewLine
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($summary)
        $log.Write($bytes, 0, $bytes.Length)
        $remaining -= $bytes.Length
        $process = [Diagnostics.Process]::new()
        $process.StartInfo = New-TemplateStartInfo $Template $selection.mode
        $stage = 'template_start'
        $null = $process.Start()
        $stage = 'template_output'
        $streams = @($process.StandardOutput, $process.StandardError)
        $buffers = @([char[]]::new(4096), [char[]]::new(4096))
        $reads = @($streams[0].ReadAsync($buffers[0], 0, 4096), $streams[1].ReadAsync($buffers[1], 0, 4096))
        while ($null -ne $reads[0] -or $null -ne $reads[1]) {
            for ($i = 0; $i -lt 2; $i++) {
                if ($null -eq $reads[$i] -or -not $reads[$i].IsCompleted) { continue }
                $count = $reads[$i].GetAwaiter().GetResult()
                if ($count -eq 0) { $reads[$i] = $null; continue }
                $bytes = [Text.UTF8Encoding]::new($false).GetBytes($buffers[$i], 0, $count)
                $take = [Math]::Min($remaining, $bytes.Length)
                if ($take -gt 0) { $log.Write($bytes, 0, $take); $remaining -= $take }
                # Drain both pipes after the cap so diagnostics cannot stall the template.
                $reads[$i] = $streams[$i].ReadAsync($buffers[$i], 0, 4096)
            }
            Start-Sleep -Milliseconds 20
        }
        $process.WaitForExit()
        $code = $process.ExitCode
        $stage = 'template_exit'
    } catch {
        $bytes = [Text.Encoding]::ASCII.GetBytes(('Template refused at stage=' + $stage + '.' + [Environment]::NewLine))
        $take = [Math]::Min($remaining, $bytes.Length)
        if ($take -gt 0) { $log.Write($bytes, 0, $take); $remaining -= $take }
    } finally {
        $log.Dispose()
        $result = @{schema = 1; stage = $stage; exit_code = $code; execution_mode = $selection.mode; execution_scopes = $selection.scopes; output_limit_bytes = 65536; output_capped = ($remaining -eq 0)} | ConvertTo-Json -Compress
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($result)
        $stream = [IO.File]::Open($resultPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
        if ($null -ne $process) { $process.Dispose() }
    }
    return $code
}
function Show-TemplateResult($Result, [string]$Directory) {
    Write-Output ('Inventory stage: ' + $Result.stage + '; exit code: ' + $Result.exit_code)
    Write-Output ('Execution mode: ' + $Result.execution_mode)
    Write-Output ('Local diagnostics: ' + (Join-Path $Directory 'template-output.log'))
    Write-Output ('Local result: ' + (Join-Path $Directory 'template-exit.json'))
    if ($Result.exit_code -ne 0 -and $Result.stage -in @('policy_query', 'template_start', 'template_output', 'template_exit')) {
        try {
            $bytes = Get-PublicBytes (Join-Path $Directory 'template-output.log') 65536
            Write-Output 'Child PowerShell output (bounded local diagnostic):'
            # A capped log can end inside a UTF-8 character; show a replacement rather than hide the error.
            Write-Output ([Text.UTF8Encoding]::new($false, $false).GetString($bytes))
        } catch { Write-Output 'Local diagnostic could not be read; the child exit code is preserved.' }
    }
}
function Test-TemplateReadOnlyRights([Security.AccessControl.FileSystemRights]$Rights) {
    $allowed=[long]([Security.AccessControl.FileSystemRights]::ReadAndExecute -bor [Security.AccessControl.FileSystemRights]::Synchronize)
    return (([long]$Rights -band (-bnot $allowed)) -eq 0)
}
function Assert-TemplateDirectory([string]$Directory,[switch]$Create) {
    Assert-PlainPath $Directory
    if (-not (Test-Path -LiteralPath $Directory)) {
        if (-not $Create) { throw 'Local template directory missing.' }
        $security=[Security.AccessControl.DirectorySecurity]::new()
        $security.SetAccessRuleProtection($true,$false)
        $security.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
        foreach($id in @('S-1-5-32-544','S-1-5-18','S-1-5-32-545')) {
            $rights=if($id -eq 'S-1-5-32-545'){'ReadAndExecute'}else{'FullControl'}
            $rule=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($id),$rights,'ContainerInherit,ObjectInherit','None','Allow')
            $security.AddAccessRule($rule)
        }
        $null=[IO.Directory]::CreateDirectory($Directory,$security)
    }
    Assert-PlainPath $Directory
    $acl=Get-Acl -LiteralPath $Directory
    if (-not $acl.AreAccessRulesProtected -or $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin @('S-1-5-32-544','S-1-5-18')) { throw 'Untrusted template directory owner.' }
    foreach($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
        $id=$rule.IdentityReference.Value
        if ($rule.AccessControlType -ne 'Allow' -or $id -notin @('S-1-5-32-544','S-1-5-18','S-1-5-32-545') -or
            ($id -eq 'S-1-5-32-545' -and -not (Test-TemplateReadOnlyRights $rule.FileSystemRights))) { throw 'Untrusted template directory access.' }
    }
}
function Get-PublicFileHash([string]$Path) {
    Assert-PlainPath $Path
    $stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $sha=[Security.Cryptography.SHA256]::Create()
    try { return Get-PublicHashFromStream $stream $sha } finally { $stream.Dispose();$sha.Dispose() }
}
function Copy-TemplatePublic([string]$Source,[string]$Destination,[string]$Hash) {
    Assert-PlainPath $Source
    Assert-PlainPath $Destination
    $input=[IO.File]::Open($Source,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $sha=[Security.Cryptography.SHA256]::Create()
    try {
        if ($input.Length -gt 536870912 -or (Get-PublicHashFromStream $input $sha) -cne $Hash) { throw 'Public payload changed.' }
        if (Test-Path -LiteralPath $Destination) {
            if ((Get-PublicFileHash $Destination) -cne $Hash) { throw 'Existing template payload differs.' }
        } else {
            $input.Position=0
            $output=[IO.File]::Open($Destination,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
            try { $input.CopyTo($output);$output.Flush($true) } finally { $output.Dispose() }
        }
    } finally {$input.Dispose();$sha.Dispose()}
}
function Get-PublicHashFromStream($Stream,$Sha) {
    [BitConverter]::ToString($Sha.ComputeHash($Stream)).Replace('-','').ToLowerInvariant()
}
$sourceDirectory=[IO.Path]::GetDirectoryName($env:WC_TEMPLATE_BOOTSTRAP)
if (-not [Environment]::Is64BitProcess -or -not [Environment]::UserInteractive -or [Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) {throw 'Interactive 64-bit USER required.'}
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if($sid -in @('S-1-5-18','S-1-5-19','S-1-5-20')){throw 'Service account refused.'}
$manifestBytes=Get-PublicBytes (Join-Path $sourceDirectory 'handoff.json') 16384
$manifest=[Text.UTF8Encoding]::new($false,$true).GetString($manifestBytes) | ConvertFrom-Json
$names=@('Run-Prepare.cmd','Prepare-Template.ps1','README.md','Claude.msix','Firefox.msi')
if($manifest.schema -ne 1 -or $manifest.phase -cne 'audit_app_provisioning' -or $manifest.nonce -cnotmatch '^[0-9a-f]{32}$' -or
   $manifest.expected_smbios_serial -cne ('WCMASTER-'+$manifest.nonce) -or
   (($manifest.files.PSObject.Properties.Name|Sort-Object)-join ',') -cne (($names|Sort-Object)-join ',')){throw 'Invalid template handoff.'}
if((Get-CimInstance -ClassName Win32_ComputerSystemProduct -OperationTimeoutSec 15).IdentifyingNumber -cne $manifest.expected_smbios_serial){throw 'Template SMBIOS mismatch.'}
foreach($name in $names){
    $path=Join-Path $sourceDirectory $name;Assert-PlainPath $path
    if($manifest.files.$name -cnotmatch '^[0-9a-f]{64}$' -or (Get-PublicFileHash $path) -cne $manifest.files.$name){throw 'Template source hash mismatch.'}
}
$destination=Join-Path (Join-Path $env:ProgramData 'WindowsClaudeTemplate') $manifest.nonce
Assert-PlainPath $destination
if($env:WC_TEMPLATE_WORKER -eq '1'){
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    if(-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Elevated USER required.'}
    if($env:WC_TEMPLATE_PHASE -eq 'Audit' -and [IO.Path]::GetFullPath($sourceDirectory) -cne [IO.Path]::GetFullPath($destination)){throw 'Audit callback must use trusted local copy.'}
    Assert-TemplateDirectory $destination -Create
    foreach($name in $names){Copy-TemplatePublic (Join-Path $sourceDirectory $name) (Join-Path $destination $name) $manifest.files.$name}
    $path=Join-Path $destination 'handoff.json'
    if(Test-Path -LiteralPath $path){if((Get-PublicHash (Get-PublicBytes $path 16384)) -cne (Get-PublicHash $manifestBytes)){throw 'Local manifest differs.'}}
    else{$stream=[IO.File]::Open($path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None);try{$stream.Write($manifestBytes,0,$manifestBytes.Length)}finally{$stream.Dispose()}}
    $attempt=Join-Path $destination ('attempt-'+$env:WC_TEMPLATE_PHASE.ToLowerInvariant()+'-'+[guid]::NewGuid().ToString('N'))
    $null=[IO.Directory]::CreateDirectory($attempt)
    $exitCode=Invoke-LoggedTemplate (Join-Path $destination 'Prepare-Template.ps1') $attempt
    $result=[IO.File]::ReadAllText((Join-Path $attempt 'template-exit.json'))|ConvertFrom-Json
    Show-TemplateResult $result $attempt
    exit $exitCode
}
if($sourceDirectory -match '[%"\r\n]'){throw 'Unsupported UAC command path.'}
$arguments='/d /v:off /s /c ""'+$env:WC_TEMPLATE_BOOTSTRAP+'" --worker"'
try{$process=Start-Process -FilePath (Join-Path $env:WINDIR 'System32\cmd.exe') -ArgumentList $arguments -Verb RunAs -PassThru -Wait}
catch [ComponentModel.Win32Exception]{if($_.Exception.NativeErrorCode -eq 1223){Write-Output 'Template preparation cancelled at UAC (1223).';exit 1223};throw}
Assert-PlainPath $destination
$attempts=@(Get-ChildItem -LiteralPath $destination -Directory -Filter 'attempt-bootstrap-*' | Sort-Object LastWriteTimeUtc -Descending)
if($attempts.Count -lt 1 -or $attempts[0].Name -cnotmatch '^attempt-bootstrap-[0-9a-f]{32}$'){Write-Output ('Template worker exit code: '+$process.ExitCode);exit $process.ExitCode}
$attempt=$attempts[0].FullName;Assert-PlainPath $attempt
$result=[IO.File]::ReadAllText((Join-Path $attempt 'template-exit.json'))|ConvertFrom-Json
if($result.exit_code -isnot [int] -or $result.exit_code -ne $process.ExitCode){throw 'Invalid template child result.'}
Show-TemplateResult $result $attempt
exit $result.exit_code
