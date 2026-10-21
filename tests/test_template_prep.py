import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('template_prep', ROOT / 'scripts/template_prep.py')
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)


def receipt():
    return {'schema': 1, 'nonce': 'a' * 32, 'phase': 'apps_verified', 'verdict': 'review_required',
            'code': 0, 'cleanup_allowed': False, 'generalize_allowed': False,
            'gates': {key: 'unknown' for key in prep.GATES}, 'apps': {'claude': True, 'firefox': True}}


def approvals(directory):
    result = {}
    for role, filename in [('claude', 'Claude.msix'), ('firefox', 'Firefox.msi')]:
        path = directory / filename
        path.write_bytes((role + '-public-fixture').encode())
        result[role] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'version': '3.1623.0.0' if role == 'claude' else '158.0.1.0',
                        'publisher': 'CN=Fixture', 'signer_thumbprint': 'B' * 40}
    result['claude']['name'] = 'Claude'
    result['firefox'].update(product_code='{1294A4C5-9977-480F-9497-C0EA1E630130}', executable='Mozilla Firefox/firefox.exe')
    return result


class TemplatePreparationTests(unittest.TestCase):
    def test_new_handoff_deterministic_public_names_exact_hashes_and_no_host_paths(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'guest/template') as directory:
            root = Path(directory)
            approved = approvals(root)
            destination = root / 'handoff'
            manifest = prep.prepare_handoff(destination, approved, nonce='a' * 32)
            self.assertEqual(manifest['expected_smbios_serial'], 'WCMASTER-' + 'a' * 32)
            self.assertEqual(set(p.name for p in destination.iterdir()), set(prep.SOURCE_FILES) | {'Claude.msix', 'Firefox.msi', 'handoff.json'})
            self.assertNotIn(str(root), json.dumps(manifest))
            self.assertEqual(prep.read_handoff(destination), manifest)
            with self.assertRaises(ValueError):
                prep.prepare_handoff(destination, approved)
            (destination / 'Firefox.msi').write_bytes(b'changed public installer')
            with self.assertRaises(ValueError):
                prep.read_handoff(destination)

    def test_source_template_reparse_is_refused_before_open_and_not_laundered(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'guest/template') as directory:
            root = Path(directory)
            approved = approvals(root)
            fake = root / 'repository'
            sources = fake / 'guest/template'
            sources.mkdir(parents=True)
            for name in prep.SOURCE_FILES:
                (sources / name).write_bytes(b'public template fixture')
            source = sources / prep.SOURCE_FILES[0]
            actual_lstat = Path.lstat
            def reparse_lstat(path, *args, **kwargs):
                info = actual_lstat(path, *args, **kwargs)
                if path == source:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            with patch.object(prep, 'ROOT', fake), patch.object(Path, 'lstat', reparse_lstat):
                with self.assertRaises(ValueError):
                    prep.prepare_handoff(root / 'refused', approved, nonce='a' * 32)
            self.assertFalse((root / 'refused' / source.name).exists())
            self.assertFalse((root / 'refused' / 'handoff.json').exists())

    def test_approval_rejects_arbitrary_paths_hash_signature_identity_and_extra_fields(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'guest/template') as directory:
            good = approvals(Path(directory))
            for role, field, value in [('firefox', 'executable', '../private.exe'), ('claude', 'version', '3.1'),
                                       ('claude', 'name', '../Claude'), ('claude', 'sha256', 'x' * 64),
                                       ('firefox', 'signer_thumbprint', 'unknown'), ('firefox', 'extra', True)]:
                changed = copy.deepcopy(good)
                changed[role][field] = value
                with self.subTest(field=field), self.assertRaises(ValueError):
                    prep.validate_approvals(**changed)

    def test_receipt_never_authorizes_cleanup_or_generalize_and_is_nonce_bound(self):
        data = receipt()
        self.assertEqual(prep.validate_receipt(data, 'a' * 32), data)
        for field, value in [('nonce', 'b' * 32), ('code', True), ('code', 1), ('generalize_allowed', True),
                             ('cleanup_allowed', True), ('verdict', 'ready'), ('unexpected', 'SECRET')]:
            changed = copy.deepcopy(data)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                prep.validate_receipt(changed, 'a' * 32)
        changed = copy.deepcopy(data)
        changed['apps']['claude'] = False
        with self.assertRaises(ValueError):
            prep.validate_receipt(changed)
        changed = copy.deepcopy(data)
        changed.update(phase='audit_requested', verdict='pending', apps={'claude': False, 'firefox': False})
        self.assertEqual(prep.validate_receipt(changed), changed)
        changed.update(phase='failed', verdict='failed', code=10)
        self.assertEqual(prep.validate_receipt(changed), changed)

    def test_receipt_duplicate_keys_oversize_and_raw_fields_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'guest/template') as directory:
            path = Path(directory) / 'receipt.json'
            for content in ('{"schema":1,"schema":1}', 'x' * (prep.MAX_RECEIPT_BYTES + 1), '{"schema":NaN}'):
                path.write_text(content, encoding='utf-8')
                with self.assertRaises(ValueError):
                    prep.read_receipt(path, 'a' * 32)
            path.write_text(json.dumps(receipt()), encoding='utf-8')
            self.assertEqual(prep.read_receipt(path, 'a' * 32), receipt())

    def test_phase_and_identity_guards_do_not_contain_account_cleanup_or_generalize(self):
        script = (ROOT / 'guest/template/Prepare-Template.ps1').read_text(encoding='utf-8-sig')
        bootstrap = (ROOT / 'guest/template/Run-Prepare.cmd').read_text(encoding='utf-8-sig')
        for forbidden in ('Remove-Appx', 'Remove-LocalUser', 'Remove-Item', '/generalize', '-EncodedCommand', 'Set-ExecutionPolicy'):
            self.assertNotIn(forbidden, script + bootstrap)
        for required in ('/audit /reboot', '!WindowsClaudeTemplate-', 'Win32_ComputerSystemProduct', 'S-1-5-18',
                         'LocalAccount=True AND SID=', 'claude_verified', 'cleanup_allowed=$false', 'generalize_allowed=$false'):
            self.assertIn(required, script)
        self.assertLess(script.index("$stage='gates'"), script.index("$stage='audit_entry'"))
        self.assertIn('WCMASTER-', bootstrap)
        self.assertIn('-ExecutionPolicy RemoteSigned', bootstrap)
        self.assertNotIn('Bypass', bootstrap)
        self.assertLess(bootstrap.index("throw 'Template source hash mismatch.'"), bootstrap.index('if($env:WC_TEMPLATE_WORKER'))

    @unittest.skipUnless(os.name == 'nt', 'Typed audit/ACL fixture requires Windows PowerShell 5')
    def test_native_audit_transition_and_readonly_acl_regressions(self):
        script = str(ROOT / 'guest/template/Prepare-Template.ps1').replace("'", "''")
        bootstrap = str(ROOT / 'guest/template/Run-Prepare.cmd').replace("'", "''")
        command = r"""
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
$function=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-AuditTransition'},$false)
if($null -eq $function){throw 'Missing audit proof guard.'};. ([scriptblock]::Create($function.Extent.Text))
$source=[IO.File]::ReadAllText('__BOOTSTRAP__');$marker='# WC_TEMPLATE_BODY'
$ast=[Management.Automation.Language.Parser]::ParseInput($source.Substring($source.LastIndexOf($marker,[StringComparison]::Ordinal)+$marker.Length),[ref]$tokens,[ref]$errors)
$function=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-TemplateReadOnlyRights'},$false)
if($null -eq $function){throw 'Missing exact ACL rights guard.'};. ([scriptblock]::Create($function.Extent.Text))
$nonce='a'*32
$journal=@{schema=1;nonce=$nonce;phase='audit_requested';audit_requested_boot=[long]100;audit_entered_boot=$null}
foreach($route in @('IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT','IMAGE_STATE_GENERALIZE_RESEAL_TO_AUDIT')){Assert-AuditTransition $journal $route 200 $nonce}
foreach($case in @(
    @{journal=$journal;route='IMAGE_STATE_COMPLETE';boot=200;nonce=$nonce},
    @{journal=$journal;route='UNVERIFIED';boot=200;nonce=$nonce},
    @{journal=$journal;route='IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT';boot=100;nonce=$nonce},
    @{journal=$journal;route='IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT';boot=200;nonce=('b'*32)},
    @{journal=@{schema=1;nonce=$nonce;phase='audit_failed';audit_requested_boot=[long]100};route='IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT';boot=200;nonce=$nonce},
    @{journal=@{schema=1;nonce=$nonce;phase='audit_requested'};route='IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT';boot=200;nonce=$nonce},
    @{journal=@{schema=1;nonce=$nonce;phase='claude_verified';audit_requested_boot=[long]100;audit_entered_boot=[long]200};route='IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT';boot=300;nonce=$nonce}
)){
    try{Assert-AuditTransition $case.journal $case.route $case.boot $case.nonce;throw 'Unverified Audit accepted.'}catch{if($_.Exception.Message -eq 'Unverified Audit accepted.'){throw}}
}
$journal.phase='claude_verified';$journal.audit_entered_boot=[long]200
Assert-AuditTransition $journal 'IMAGE_STATE_SPECIALIZE_RESEAL_TO_AUDIT' 200 $nonce
$read=[Security.AccessControl.FileSystemRights]::ReadAndExecute
if(-not (Test-TemplateReadOnlyRights $read) -or -not (Test-TemplateReadOnlyRights ($read -bor [Security.AccessControl.FileSystemRights]::Synchronize))){throw 'Readonly Users grant refused.'}
foreach($name in @('Write','Delete','ChangePermissions','TakeOwnership','DeleteSubdirectoriesAndFiles','FullControl')){
    $bad=$read -bor [Security.AccessControl.FileSystemRights]::$name
    if(Test-TemplateReadOnlyRights $bad){throw 'Dangerous Users grant accepted.'}
}
Write-Output 'audit-acl-regressions-passed'
""".replace('__SCRIPT__', script).replace('__BOOTSTRAP__', bootstrap)
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, 'Audit/ACL regression fixture failed')
        self.assertIn(b'audit-acl-regressions-passed', result.stdout)

    @unittest.skipUnless(os.name == 'nt', 'Native bootstrap helper fixture requires Windows PowerShell 5')
    def test_native_bootstrap_public_copy_and_logged_phase_file_child(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'guest/template') as directory:
            bootstrap = str(ROOT / 'guest/template/Run-Prepare.cmd').replace("'", "''")
            fixture = str(Path(directory)).replace("'", "''")
            command = r"""
$ErrorActionPreference='Stop'
$source=[IO.File]::ReadAllText('__BOOTSTRAP__');$marker='# WC_TEMPLATE_BODY'
$body=$source.Substring($source.LastIndexOf($marker,[StringComparison]::Ordinal)+$marker.Length)
$tokens=$null;$errors=$null;$ast=[Management.Automation.Language.Parser]::ParseInput($body,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Bootstrap parse failed.'}
$names=@('Assert-PlainPath','Get-PublicBytes','Get-TemplatePolicy','New-TemplateStartInfo','Invoke-LoggedTemplate','Copy-TemplatePublic','Get-PublicHashFromStream','Get-PublicFileHash')
$functions=$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $names},$false)
if($functions.Count -ne $names.Count){throw 'Expected bootstrap helper set.'}
foreach($function in $functions){. ([scriptblock]::Create($function.Extent.Text))}
$directory='__FIXTURE__'
$input=Join-Path $directory 'public.bin';$copy=Join-Path $directory 'copy.bin'
[IO.File]::WriteAllBytes($input,[byte[]]@(1,2,3,4))
$hash=(Get-PublicFileHash $input)
Copy-TemplatePublic $input $copy $hash
Copy-TemplatePublic $copy $copy $hash
try{Copy-TemplatePublic $input (Join-Path $directory 'bad.bin') ('0'*64);throw 'Bad hash accepted.'}catch{if($_.Exception.Message -eq 'Bad hash accepted.'){throw}}
if((Get-PublicFileHash $copy) -cne $hash){throw 'Public copy changed.'}
function Get-ExecutionPolicy {
    param([switch]$List)
    @('MachinePolicy','UserPolicy','Process','CurrentUser','LocalMachine')|ForEach-Object{[pscustomobject]@{Scope=$_;ExecutionPolicy=[Microsoft.PowerShell.ExecutionPolicy]::Undefined}}
}
$env:WC_TEMPLATE_PHASE='Audit'
$harmless=Join-Path $directory 'harmless.ps1'
[IO.File]::WriteAllText($harmless,'param([string]$Phase,[switch]$ReturnSerial);if($Phase -ne "Audit" -or -not $ReturnSerial){exit 19};[Console]::Error.WriteLine("template-fixture-error");exit 7')
if((Invoke-LoggedTemplate $harmless $directory) -ne 7){throw 'Phase File child exit lost.'}
$log=[IO.File]::ReadAllText((Join-Path $directory 'template-output.log'))
$result=[IO.File]::ReadAllText((Join-Path $directory 'template-exit.json'))|ConvertFrom-Json
if($log -notmatch 'template-fixture-error' -or $result.exit_code -ne 7 -or $result.execution_mode -ne 'RemoteSigned'){throw 'Visible diagnostic missing.'}
Write-Output 'template-bootstrap-fixture-passed'
""".replace('__BOOTSTRAP__', bootstrap).replace('__FIXTURE__', fixture)
            powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                                    capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace')[-1500:])
            self.assertIn(b'template-bootstrap-fixture-passed', result.stdout)

    @unittest.skipUnless(os.name == 'nt', 'Native PS5 fixture requires Windows')
    def test_native_ps5_parser_and_harmless_application_phase_fixtures(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'guest/template') as directory:
            root = Path(directory)
            with zipfile.ZipFile(root / 'Claude.msix', 'w') as archive:
                archive.writestr('AppxManifest.xml', '<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"><Identity Name="Claude" Version="3.1623.0.0" Publisher="CN=Fixture" ProcessorArchitecture="x64"/></Package>')
            script = str(ROOT / 'guest/template/Prepare-Template.ps1').replace("'", "''")
            bootstrap = str(ROOT / 'guest/template/Run-Prepare.cmd').replace("'", "''")
            fixture = str(root).replace("'", "''")
            command = r"""
$ErrorActionPreference='Stop'
if($PSVersionTable.PSVersion.Major -ne 5){throw 'Expected Windows PowerShell 5.'}
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SCRIPT__',[ref]$tokens,[ref]$errors)
if($errors.Count){throw ('Template parser refused: '+$errors[0].ErrorId)}
$source=[IO.File]::ReadAllText('__BOOTSTRAP__'); $marker='# WC_TEMPLATE_BODY'
$body=$source.Substring($source.LastIndexOf($marker,[StringComparison]::Ordinal)+$marker.Length)
$null=[Management.Automation.Language.Parser]::ParseInput($body,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ('Bootstrap parser refused: '+$errors[0].ErrorId)}
$names=@('Assert-PlainTemplatePath','Write-TemplateJson','Read-TemplateJournal','Assert-TemplateGates','Assert-TemplateSignature','Read-TemplateAppxIdentity','Assert-ClaudePackage','Get-TemplateLeafReparseTag','Assert-InstalledManifestPath','Get-TemplateFileVersion','Invoke-TemplateApplications')
$functions=$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $names},$false)
if($functions.Count -ne $names.Count){throw 'Expected helper definitions only.'}
foreach($function in $functions){. ([scriptblock]::Create($function.Extent.Text))}
$directory='__FIXTURE__'
$approval=@{name='Claude';version='3.1623.0.0';publisher='CN=Fixture';signer_thumbprint=('B'*40)}
$script:SignatureStatus='Valid'
function Get-AuthenticodeSignature { param($LiteralPath) @{Status=$script:SignatureStatus;SignerCertificate=@{Thumbprint=('B'*40);Subject='CN=Fixture'}} }
Assert-ClaudePackage (Join-Path $directory 'Claude.msix') $approval
if((Get-TemplateLeafReparseTag (Join-Path $directory 'Claude.msix')) -ne 0){throw 'Native regular-file tag query failed.'}
$script:LeafPath=Join-Path $directory 'manifest-fixture.xml'
[IO.File]::WriteAllText($script:LeafPath,'<Package/>')
function Get-Item {
    param($LiteralPath,[switch]$Force)
    if($LiteralPath -ceq $script:LeafPath){return [pscustomobject]@{PSIsContainer=$false;Attributes=[IO.FileAttributes]::ReparsePoint}}
    Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -Force
}
function Get-TemplateLeafReparseTag {param($Path) return $script:LeafTag}
$script:LeafTag=[uint32]2147483671
Assert-InstalledManifestPath $script:LeafPath
$script:LeafTag=[uint32]2684354572
try{Assert-InstalledManifestPath $script:LeafPath;throw 'Symlink leaf accepted.'}catch{if($_.Exception.Message -eq 'Symlink leaf accepted.'){throw}}
$script:SignatureStatus='UnknownError'
try{Assert-ClaudePackage (Join-Path $directory 'Claude.msix') $approval;throw 'Untrusted signature accepted.'}catch{if($_.Exception.Message -eq 'Untrusted signature accepted.'){throw}}
$script:SignatureStatus='Valid'
$bad=$approval.Clone();$bad.publisher='CN=Other'
try{Assert-ClaudePackage (Join-Path $directory 'Claude.msix') $bad;throw 'Wrong publisher accepted.'}catch{if($_.Exception.Message -eq 'Wrong publisher accepted.'){throw}}
if((Get-TemplateFileVersion @{FileMajorPart=158;FileMinorPart=0;FileBuildPart=1;FilePrivatePart=0;ProductVersion='localized-not-a-version'}) -ne '158.0.1.0'){throw 'PE version comparison changed.'}
foreach($key in @('pending_setup','pending_servicing','bitlocker')){
    $gates=@{pending_setup='clear';pending_servicing='clear';bitlocker='clear';efs='unknown'};$gates[$key]='active'
    try{Assert-TemplateGates $gates;throw 'Active gate accepted.'}catch{if($_.Exception.Message -eq 'Active gate accepted.'){throw}}
}
Assert-TemplateGates @{pending_setup='clear';pending_servicing='clear';bitlocker='unknown';efs='unknown'}
function Assert-ClaudePackage {param($Path,$Approval)}
function Assert-FirefoxInstaller {param($Path,$Approval)}
function Test-ClaudeProvisioned {param($Approval) return $script:Provisioned}
function Test-FirefoxInstalled {param($Approval) return $script:FirefoxInstalled}
function Add-AppxProvisionedPackage {
    [CmdletBinding()]param([switch]$Online,$PackagePath,[switch]$SkipLicense,$Regions)
    if(-not $Online -or -not $SkipLicense -or $Regions -ne 'all' -or [IO.Path]::GetFileName($PackagePath) -ne 'Claude.msix'){throw 'Provisioning contract changed.'}
    $script:Provisioned=$true;$script:ProvisionCalls++
}
function Start-Process {
    param($FilePath,$ArgumentList,[switch]$Wait,[switch]$PassThru)
    if([IO.Path]::GetFileName($FilePath) -ne 'msiexec.exe' -or $ArgumentList -notmatch '/qn /norestart ALLUSERS=1'){throw 'MSI contract changed.'}
    $script:MsiCalls++; if($script:MsiCode -in @(0,3010)){$script:FirefoxInstalled=$true}
    return [pscustomobject]@{ExitCode=$script:MsiCode}
}
$manifest=@{nonce=('a'*32);claude=$approval;firefox=@{version='158.0.1.0'}}
Write-TemplateJson (Join-Path $directory 'journal.json') @{schema=1;nonce=('a'*32);phase='audit_requested';audit_requested_boot=[long]100;audit_entered_boot=[long]200}
foreach($exitCode in @(0,3010)){
    $script:MsiCode=$exitCode;$script:Provisioned=$false;$script:FirefoxInstalled=$false;$script:MsiCalls=0;$script:ProvisionCalls=0
    $result=Invoke-TemplateApplications $manifest $directory (Join-Path $directory 'journal.json')
    if(-not $result.claude -or -not $result.firefox -or $script:MsiCalls -ne 1 -or $script:ProvisionCalls -ne 1){throw 'Apps not verified after accepted installer code.'}
}
$script:MsiCode=1603;$script:FirefoxInstalled=$false
try{$null=Invoke-TemplateApplications $manifest $directory (Join-Path $directory 'journal.json');throw 'MSI failure accepted.'}catch{if($_.Exception.Message -eq 'MSI failure accepted.'){throw}}
$script:MsiCalls=0;$script:ProvisionCalls=0;$script:Provisioned=$true;$script:FirefoxInstalled=$true
$result=Invoke-TemplateApplications $manifest $directory (Join-Path $directory 'journal.json')
if($script:MsiCalls -ne 0 -or $script:ProvisionCalls -ne 0){throw 'Verified app unnecessarily reinstalled.'}
$journal=Read-TemplateJournal (Join-Path $directory 'journal.json') ('a'*32)
if($journal.phase -ne 'claude_verified'){throw 'Resumable phase was not journalled.'}
if($journal.audit_requested_boot -ne 100 -or $journal.audit_entered_boot -ne 200){throw 'Application checkpoint lost Audit transition proof.'}
try{$null=Read-TemplateJournal (Join-Path $directory 'journal.json') ('b'*32);throw 'Foreign nonce accepted.'}catch{if($_.Exception.Message -eq 'Foreign nonce accepted.'){throw}}
Write-Output 'template-harmless-fixtures-passed'
""".replace('__SCRIPT__', script).replace('__BOOTSTRAP__', bootstrap).replace('__FIXTURE__', fixture)
            powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                                    capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace')[-2000:])
            self.assertIn(b'template-harmless-fixtures-passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
