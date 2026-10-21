# Run only as an interactive administrator inside this marked, isolated template VM.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][ValidateSet('Bootstrap','Audit')][string]$Phase, [switch]$ReturnSerial)
$ErrorActionPreference = 'Stop'
$stage = 'context'
$code = 1
$manifest = $null
$trustedLocal = $false
$gates = @{pending_setup='unknown'; pending_servicing='unknown'; bitlocker='unknown'; efs='unknown'}
$apps = @{claude=$false; firefox=$false}

function Assert-PlainTemplatePath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Template path refused.' }
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
}
function Write-TemplateJson([string]$Path, $Value) {
    Assert-PlainTemplatePath $Path
    $temporary = $Path + '.tmp-' + [guid]::NewGuid().ToString('N')
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 8 -Compress))
    if ($bytes.Length -gt 65536) { throw 'Template record too large.' }
    $stream = [IO.File]::Open($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try { $stream.Write($bytes,0,$bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary,$Path,[NullString]::Value) }
    else { [IO.File]::Move($temporary,$Path) }
}
function Read-TemplateJournal([string]$Path,[string]$Nonce) {
    Assert-PlainTemplatePath $Path
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    if ((Get-Item -LiteralPath $Path).Length -gt 65536) { throw 'Invalid template journal.' }
    $journal = [IO.File]::ReadAllText($Path) | ConvertFrom-Json
    if ($journal.schema -ne 1 -or $journal.nonce -cne $Nonce -or
        $journal.phase -notin @('prepared','audit_requested','claude_verified','apps_verified','failed','audit_failed')) { throw 'Invalid template journal.' }
    return $journal
}
function Get-TemplateGates {
    $result = @{pending_setup='unknown'; pending_servicing='unknown'; bitlocker='unknown'; efs='unknown'}
    try {
        $key = Get-Item -LiteralPath 'HKLM:\SYSTEM\Setup'
        $result.pending_setup = if ([int]$key.GetValue('SystemSetupInProgress',0) -ne 0 -or [int]$key.GetValue('OOBEInProgress',0) -ne 0) {'active'} else {'clear'}
    } catch { }
    try {
        $pending = (Test-Path -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or
                   (Test-Path -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')
        $key = Get-Item -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager'
        $result.pending_servicing = if ($pending -or ($key.GetValueNames() -contains 'PendingFileRenameOperations')) {'active'} else {'clear'}
    } catch { }
    try {
        $volume = @(Get-BitLockerVolume -MountPoint $env:SystemDrive -ErrorAction Stop)
        if ($volume.Count -eq 1) {
            $result.bitlocker = if ([string]$volume[0].VolumeStatus -eq 'FullyDecrypted' -and [string]$volume[0].ProtectionStatus -eq 'Off') {'clear'} else {'active'}
        }
    } catch { }
    # No EFS contents or profiles are scanned; unknown remains a later cleanup or generalize blocker.
    return $result
}
function Assert-TemplateGates($Gates) {
    if ($Gates.pending_setup -ne 'clear' -or $Gates.pending_servicing -ne 'clear' -or $Gates.bitlocker -eq 'active') {
        throw 'OS setup, servicing or BitLocker gate refused.'
    }
}
function Get-TemplateBootStamp {
    $boot=(Get-CimInstance -ClassName Win32_OperatingSystem -OperationTimeoutSec 15).LastBootUpTime
    if ($boot -isnot [datetime]) { throw 'Audit boot is UNVERIFIED.' }
    return $boot.ToUniversalTime().Ticks
}
function Assert-AuditTransition($Journal,[string]$ImageState,[long]$CurrentBoot,[string]$Nonce) {
    if ($null -eq $Journal -or $Journal.schema -ne 1 -or $Journal.nonce -cne $Nonce -or
        $Journal.phase -notin @('audit_requested','claude_verified','apps_verified','failed') -or
        $ImageState -cnotin @('IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT','IMAGE_STATE_GENERALIZE_RESEAL_TO_AUDIT') -or
        $Journal.audit_requested_boot -isnot [long] -or $Journal.audit_requested_boot -le 0 -or
        $CurrentBoot -le $Journal.audit_requested_boot) { throw 'Current Audit transition is UNVERIFIED.' }
    if ($null -ne $Journal.audit_entered_boot -and
        ($Journal.audit_entered_boot -isnot [long] -or $Journal.audit_entered_boot -ne $CurrentBoot)) {
        throw 'Current Audit pass is UNVERIFIED.'
    }
}
function Assert-TemplateIdentity($Manifest,[string]$CurrentPhase) {
    if ($CurrentPhase -notin @('Bootstrap','Audit')) { throw 'Explicit template phase required.' }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $sid = $identity.User.Value
    if (-not [Environment]::Is64BitProcess -or -not [Environment]::UserInteractive -or
        [Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0 -or $sid -in @('S-1-5-18','S-1-5-19','S-1-5-20') -or
        -not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Interactive administrator USER required.'
    }
    if ((Get-CimInstance -ClassName Win32_ComputerSystemProduct -OperationTimeoutSec 15).IdentifyingNumber -cne $Manifest.expected_smbios_serial) { throw 'Template machine nonce mismatch.' }
    if ($CurrentPhase -eq 'Audit') {
        if ($sid -cnotmatch '^S-1-5-21-(?:[0-9]+-){3}500$' -or
            @(Get-CimInstance -ClassName Win32_UserAccount -Filter ("LocalAccount=True AND SID='"+$sid+"'") -OperationTimeoutSec 15).Count -ne 1) {
            throw 'Local built-in Audit Administrator required.'
        }
    }
}
function Assert-TemplatePayload([string]$Directory,$Manifest) {
    if ($Manifest.schema -ne 1 -or $Manifest.phase -cne 'audit_app_provisioning' -or
        $Manifest.nonce -cnotmatch '^[0-9a-f]{32}$' -or $Manifest.expected_smbios_serial -cne ('WCMASTER-'+$Manifest.nonce)) { throw 'Invalid template manifest.' }
    $names = @('Run-Prepare.cmd','Prepare-Template.ps1','README.md','Claude.msix','Firefox.msi')
    if ((($Manifest.files.PSObject.Properties.Name | Sort-Object) -join ',') -cne (($names | Sort-Object) -join ',')) { throw 'Invalid public file list.' }
    foreach ($name in $names) {
        $path = Join-Path $Directory $name
        Assert-PlainTemplatePath $path
        if ($Manifest.files.$name -cnotmatch '^[0-9a-f]{64}$' -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Manifest.files.$name) { throw 'Template payload hash mismatch.' }
    }
    if ($Manifest.claude.file -cne 'Claude.msix' -or $Manifest.firefox.file -cne 'Firefox.msi' -or
        $Manifest.claude.sha256 -cne $Manifest.files.'Claude.msix' -or $Manifest.firefox.sha256 -cne $Manifest.files.'Firefox.msi' -or
        $Manifest.firefox.executable -cne 'Mozilla Firefox/firefox.exe' -or $Manifest.claude.version -cnotmatch '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' -or
        $Manifest.firefox.product_code -cnotmatch '^\{[0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}\}$') { throw 'Invalid app approval.' }
}
function Assert-TemplateSignature([string]$Path,$Approval) {
    $signature=Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Thumbprint -cne $Approval.signer_thumbprint -or
        $signature.SignerCertificate.Subject -cne $Approval.publisher) { throw 'Approved payload signature mismatch.' }
}
function Read-TemplateAppxIdentity($Stream) {
    $settings=[Xml.XmlReaderSettings]::new(); $settings.DtdProcessing=[Xml.DtdProcessing]::Prohibit; $settings.XmlResolver=$null
    $reader=[Xml.XmlReader]::Create($Stream,$settings)
    try { $document=[Xml.XmlDocument]::new(); $document.XmlResolver=$null; $document.Load($reader); return $document.Package.Identity }
    finally { $reader.Dispose() }
}
function Assert-ClaudePackage([string]$Path,$Approval) {
    Assert-TemplateSignature $Path $Approval
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive=[IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $entry=$archive.GetEntry('AppxManifest.xml')
        if ($null -eq $entry -or $entry.Length -gt 1048576) { throw 'Invalid signed MSIX manifest.' }
        $stream=$entry.Open()
        try { $id=Read-TemplateAppxIdentity $stream } finally { $stream.Dispose() }
        if ($id.Name -cne $Approval.name -or $id.Version -cne $Approval.version -or $id.Publisher -cne $Approval.publisher -or $id.ProcessorArchitecture -cne 'x64') { throw 'MSIX identity mismatch.' }
    } finally { $archive.Dispose() }
}
function Assert-FirefoxInstaller([string]$Path,$Approval) {
    Assert-TemplateSignature $Path $Approval
    $installer=New-Object -ComObject WindowsInstaller.Installer
    try {
        $database=$installer.OpenDatabase($Path,0)
        $values=@{}
        foreach($name in @('ProductCode','ProductVersion')) {
            $view=$database.OpenView(('SELECT `Value` FROM `Property` WHERE `Property`='''+$name+''''))
            try { $view.Execute(); $record=$view.Fetch(); $values[$name]=[string]$record.StringData(1) } finally { $view.Close() }
        }
        if ($values.ProductCode -cne $Approval.product_code -or $values.ProductVersion -cne $Approval.version) { throw 'MSI identity mismatch.' }
    } finally { $null=[Runtime.InteropServices.Marshal]::FinalReleaseComObject($installer) }
}
function Get-TemplateLeafReparseTag([string]$Path) {
    if (-not ('WindowsClaudeTemplate.FileTags' -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
namespace WindowsClaudeTemplate {
    public static class FileTags {
        [StructLayout(LayoutKind.Sequential)] struct AttributeTag { public uint Attributes; public uint Tag; }
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        static extern SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern bool GetFileInformationByHandleEx(SafeFileHandle file, int informationClass, out AttributeTag information, uint size);
        public static uint Read(string path) {
            using (SafeFileHandle file=CreateFileW(path,0x80,1,IntPtr.Zero,3,0x02200000,IntPtr.Zero)) {
                if(file.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
                AttributeTag information;
                if(!GetFileInformationByHandleEx(file,9,out information,(uint)Marshal.SizeOf(typeof(AttributeTag)))) throw new Win32Exception(Marshal.GetLastWin32Error());
                return information.Tag;
            }
        }
    }
}
"@
    }
    return [WindowsClaudeTemplate.FileTags]::Read($Path)
}
function Assert-InstalledManifestPath([string]$Path) {
    Assert-PlainTemplatePath ([IO.Path]::GetDirectoryName($Path))
    $entry=Get-Item -LiteralPath $Path -Force
    if ($entry.PSIsContainer) { throw 'Installed manifest is not a file.' }
    # WOF is inline compressed data, not a path redirection; every ancestor stays plain.
    if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -and
        (Get-TemplateLeafReparseTag $Path) -ne [uint32]2147483671) { throw 'Installed manifest reparse tag refused.' }
}
function Test-ClaudeProvisioned($Approval) {
    $packages=@(Get-AppxProvisionedPackage -Online -ErrorAction Stop | Where-Object { $_.DisplayName -ceq $Approval.name -and [string]$_.Version -ceq $Approval.version })
    if ($packages.Count -ne 1 -or $packages[0].PackageName -cnotmatch '^[A-Za-z0-9._-]{1,255}$') { return $false }
    $path=Join-Path (Join-Path (Join-Path $env:ProgramFiles 'WindowsApps') $packages[0].PackageName) 'AppxManifest.xml'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    Assert-InstalledManifestPath $path
    if ((Get-Item -LiteralPath $path).Length -gt 1048576) { return $false }
    $stream=[IO.File]::OpenRead($path)
    try { $identity=Read-TemplateAppxIdentity $stream } finally { $stream.Dispose() }
    return ($identity.Name -ceq $Approval.name -and $identity.Version -ceq $Approval.version -and $identity.Publisher -ceq $Approval.publisher -and $identity.ProcessorArchitecture -ceq 'x64')
}
function Get-TemplateFileVersion($Info) {
    return ('{0}.{1}.{2}.{3}' -f $Info.FileMajorPart,$Info.FileMinorPart,$Info.FileBuildPart,$Info.FilePrivatePart)
}
function Test-FirefoxInstalled($Approval) {
    $path=Join-Path $env:ProgramFiles $Approval.executable.Replace('/','\')
    Assert-PlainTemplatePath $path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
        -not (Test-Path -LiteralPath ('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\'+$Approval.product_code))) { return $false }
    $version=[Diagnostics.FileVersionInfo]::GetVersionInfo($path)
    $signature=Get-AuthenticodeSignature -LiteralPath $path
    return ((Get-TemplateFileVersion $version) -ceq $Approval.version -and $signature.Status -eq 'Valid' -and
            $signature.SignerCertificate.Subject -ceq $Approval.publisher -and $signature.SignerCertificate.Thumbprint -ceq $Approval.signer_thumbprint)
}
function Invoke-TemplateApplications($Manifest,[string]$Directory,[string]$JournalPath) {
    Assert-ClaudePackage (Join-Path $Directory 'Claude.msix') $Manifest.claude
    Assert-FirefoxInstaller (Join-Path $Directory 'Firefox.msi') $Manifest.firefox
    if (-not (Test-ClaudeProvisioned $Manifest.claude)) {
        $null=Add-AppxProvisionedPackage -Online -PackagePath (Join-Path $Directory 'Claude.msix') -SkipLicense -Regions all -ErrorAction Stop
    }
    if (-not (Test-ClaudeProvisioned $Manifest.claude)) { throw 'Claude provisioning not confirmed.' }
    $checkpoint=Read-TemplateJournal $JournalPath $Manifest.nonce
    if ($null -eq $checkpoint) { throw 'Audit checkpoint missing.' }
    $checkpoint.phase='claude_verified'
    Write-TemplateJson $JournalPath $checkpoint
    if (-not (Test-FirefoxInstalled $Manifest.firefox)) {
        $arguments='/i "'+(Join-Path $Directory 'Firefox.msi')+'" /qn /norestart ALLUSERS=1'
        $process=Start-Process -FilePath (Join-Path $env:WINDIR 'System32\msiexec.exe') -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -notin @(0,3010)) { throw 'Firefox MSI failed.' }
    }
    if (-not (Test-FirefoxInstalled $Manifest.firefox)) { throw 'Firefox machine executable not verified.' }
    return @{claude=$true;firefox=$true}
}
function Send-TemplateReceipt([byte[]]$Bytes,[string]$Nonce) {
    if ($Bytes.Length -gt 65536 -or $Nonce -cnotmatch '^[0-9a-f]{32}$') { throw 'Invalid template receipt.' }
    $port=[IO.Ports.SerialPort]::new('COM2',115200,[IO.Ports.Parity]::None,8,[IO.Ports.StopBits]::One)
    $port.Handshake=[IO.Ports.Handshake]::None; $port.WriteTimeout=1000
    $header=[Text.Encoding]::ASCII.GetBytes(('WCMASTER '+$Nonce+' '+$Bytes.Length+"`n"))
    try { $port.Open(); $port.Write($header,0,$header.Length); $port.Write($Bytes,0,$Bytes.Length) }
    finally { try {if($port.IsOpen){$port.Close()}} finally{$port.Dispose()} }
}

try {
    Assert-PlainTemplatePath $PSScriptRoot
    $manifestPath=Join-Path $PSScriptRoot 'handoff.json'
    if ((Get-Item -LiteralPath $manifestPath).Length -gt 16384) { throw 'Manifest too large.' }
    $manifest=[IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
    Assert-TemplatePayload $PSScriptRoot $manifest
    Assert-TemplateIdentity $manifest $Phase
    $expected=Join-Path (Join-Path $env:ProgramData 'WindowsClaudeTemplate') $manifest.nonce
    if ([IO.Path]::GetFullPath($PSScriptRoot) -cne [IO.Path]::GetFullPath($expected)) { throw 'Trusted local staging directory required.' }
    $trustedLocal=$true
    $journalPath=Join-Path $PSScriptRoot 'journal.json'
    $journal=Read-TemplateJournal $journalPath $manifest.nonce
    if ($Phase -eq 'Bootstrap' -and $null -ne $journal -and $journal.phase -in @('audit_requested','claude_verified','apps_verified','failed')) {
        Assert-TemplateIdentity $manifest 'Audit'
        $Phase='Audit'
    }
    $stage='gates'; $gates=Get-TemplateGates; Assert-TemplateGates $gates
    if ($Phase -eq 'Bootstrap') {
        if ($null -ne $journal -and $journal.phase -ne 'prepared') { throw 'Existing template journal requires Audit continuation review.' }
        $stage='audit_entry'
        $requestBoot=Get-TemplateBootStamp
        Write-TemplateJson $journalPath @{schema=1;nonce=$manifest.nonce;phase='prepared';audit_requested_boot=$requestBoot;audit_entered_boot=$null}
        $callback='"'+(Join-Path $env:WINDIR 'System32\cmd.exe')+'" /d /v:off /s /c ""'+(Join-Path $PSScriptRoot 'Run-Prepare.cmd')+'" --audit"'
        $runonce=[Microsoft.Win32.Registry]::LocalMachine.CreateSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce',[Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree)
        try {
            $name='!WindowsClaudeTemplate-'+$manifest.nonce
            if ($null -ne $runonce.GetValue($name,$null) -and $runonce.GetValue($name) -cne $callback) { throw 'Conflicting RunOnce callback exists.' }
            $runonce.SetValue($name,$callback,[Microsoft.Win32.RegistryValueKind]::String)
            if ($runonce.GetValue($name) -cne $callback) { throw 'RunOnce callback not confirmed.' }
        } finally { $runonce.Dispose() }
        Write-TemplateJson $journalPath @{schema=1;nonce=$manifest.nonce;phase='audit_requested';audit_requested_boot=$requestBoot;audit_entered_boot=$null}
        $receipt=@{schema=1;nonce=$manifest.nonce;phase='audit_requested';verdict='pending';code=0;cleanup_allowed=$false;generalize_allowed=$false;gates=$gates;apps=$apps}
        Write-TemplateJson (Join-Path $PSScriptRoot 'receipt.json') $receipt
        $process=Start-Process -FilePath (Join-Path $env:WINDIR 'System32\Sysprep\Sysprep.exe') -ArgumentList '/audit /reboot' -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw 'Audit request failed.' }
    } else {
        if ($null -eq $journal -or $journal.phase -notin @('audit_requested','claude_verified','apps_verified','failed')) { throw 'Audit continuation phase refused.' }
        $stage='audit_transition'
        $imageState=(Get-Item -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\State').GetValue('ImageState')
        $currentBoot=Get-TemplateBootStamp
        Assert-AuditTransition $journal $imageState $currentBoot $manifest.nonce
        $journal.audit_entered_boot=$currentBoot
        Write-TemplateJson $journalPath $journal
        $stage='applications'
        $apps=Invoke-TemplateApplications $manifest $PSScriptRoot $journalPath
        $gates=Get-TemplateGates
        $journal.phase='apps_verified'
        Write-TemplateJson $journalPath $journal
        $receipt=@{schema=1;nonce=$manifest.nonce;phase='apps_verified';verdict='review_required';code=0;cleanup_allowed=$false;generalize_allowed=$false;gates=$gates;apps=$apps}
        Write-TemplateJson (Join-Path $PSScriptRoot 'receipt.json') $receipt
        if($ReturnSerial){Send-TemplateReceipt ([IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'receipt.json'))) $manifest.nonce}
    }
    Write-Output 'Template phase completed. Audit remains; cleanup and generalize are not allowed.'
    exit 0
} catch {
    # Fixed phase and numeric code only: never export a raw installer, registry or CIM exception.
    Write-Error ('Template preparation failed at stage='+$stage+'. No profile cleanup or generalize was performed.') -ErrorAction Continue
    if($trustedLocal -and $null -ne $manifest -and $manifest.nonce -cmatch '^[0-9a-f]{32}$') {
        if ($Phase -eq 'Bootstrap' -and $stage -eq 'audit_entry') {
            try { Write-TemplateJson $journalPath @{schema=1;nonce=$manifest.nonce;phase='audit_failed';audit_requested_boot=$requestBoot;audit_entered_boot=$null} } catch { }
        }
        $receipt=@{schema=1;nonce=$manifest.nonce;phase='failed';verdict='failed';code=$code;cleanup_allowed=$false;generalize_allowed=$false;gates=$gates;apps=$apps}
        try { Write-TemplateJson (Join-Path $PSScriptRoot 'receipt.json') $receipt; if($ReturnSerial){Send-TemplateReceipt ([IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'receipt.json'))) $manifest.nonce} } catch { }
    }
    exit 1
}
