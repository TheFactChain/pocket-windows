import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reference_prep', ROOT / 'scripts/reference_prep.py')
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)


def report():
    return {
        'schema': 1, 'phase': 'inventory_only', 'reference_id': 'a' * 32, 'isolated_copy_acknowledged': True,
        'cleanup_executed': False, 'sysprep_executed': False,
        'context': {'sid': 'S-1-5-21-1-2-3-1001', 'administrator': True, 'interactive': True},
        'os': {'version': '10.0.26300', 'build': '26300', 'edition': 'Professional',
               'sysprep_present': True, 'pending_reboot': False, 'setup_in_progress': False},
        'sections': {key: {'status': 'complete', 'items': [], 'error': None}
                     for key in prep.SECTION_FIELDS},
    }


class ReferencePreparationTests(unittest.TestCase):
    def test_review_is_always_required_even_with_complete_inventory(self):
        result = prep.summarize(report())
        self.assertEqual(result['decision'], 'review_required')
        self.assertFalse(result['ready_for_generalize'])
        self.assertIn('profile_mapping', result['manual_reviews'])

    def test_service_system_and_noninteractive_or_unelevated_context_rejected(self):
        for change in ({'sid': 'S-1-5-18'}, {'sid': 'S-1-5-19'}, {'sid': 'S-1-5-20'},
                       {'administrator': False}, {'interactive': False}):
            with self.subTest(change=change):
                data = report()
                data['context'].update(change)
                with self.assertRaises(ValueError):
                    prep.summarize(data)

    def test_incomplete_section_and_loaded_user_coverage_remain_explicit(self):
        data = report()
        data['sections']['user_apps'] = {'status': 'partial', 'items': [], 'error': 'unloaded_hives_not_read'}
        data['sections']['appx']['status'] = 'unavailable'
        data['sections']['appx']['error'] = 'collection_failed'
        summary = prep.summarize(data)
        self.assertEqual(summary['incomplete_sections'], ['user_apps', 'appx'])

    def test_appx_candidates_are_hints_and_not_generated_when_provisioning_is_unknown(self):
        data = report()
        data['sections']['appx']['items'] = [{'name': 'Example', 'version': '2.0', 'package': 'Example_2.0_x64',
            'framework': False, 'users': [{'sid': data['context']['sid'], 'state': 'Installed'}]}]
        data['sections']['provisioned_appx']['items'] = [{'name': 'Example', 'version': '1.0', 'package': 'Example_1.0_neutral'}]
        self.assertEqual(prep.summarize(data)['appx_review_candidates'], ['Example_2.0_x64'])
        data['sections']['provisioned_appx'].update(status='unavailable', error='collection_failed', items=[])
        self.assertEqual(prep.summarize(data)['appx_review_candidates'], [])

    def test_metadata_projection_validates_actual_report_rows(self):
        data = report()
        data['sections']['local_accounts']['items'] = [{'sid': data['context']['sid'], 'name': 'ReferenceUser', 'disabled': False}]
        data['sections']['users']['items'] = [{'sid': data['context']['sid'], 'name': 'ReferenceUser',
            'loaded': True, 'special': False, 'markers': {key: None for key in prep.MARKERS}}]
        data['sections']['machine_apps']['items'] = [{'name': 'Example', 'version': '1.0', 'publisher': 'Example publisher'}]
        data['sections']['user_apps']['items'] = [dict(data['sections']['machine_apps']['items'][0], sid=data['context']['sid'])]
        self.assertEqual(prep.summarize(data)['counts']['local_accounts'], 1)

    def test_invalid_or_secret_bearing_fields_are_rejected_without_echo(self):
        cases = []
        for key, value in [('schema', True), ('phase', 'generalize'), ('sysprep_executed', True),
                           ('isolated_copy_acknowledged', False), ('password', 'DO_NOT_ECHO')]:
            data = report(); data[key] = value; cases.append(data)
        data = report(); data['context']['sid'] = 'not a SID'; cases.append(data)
        data = report(); data['sections']['machine_apps']['items'] = [{'name': 'App', 'uninstall_string': 'DO_NOT_ECHO'}]; cases.append(data)
        data = report(); data['sections']['appx']['items'] = 'invalid'; cases.append(data)
        data = report(); data['sections']['appx'].update(status='partial', error='unloaded_hives_not_read'); cases.append(data)
        data = report(); data['sections']['machine_apps'].update(status='unavailable', error='collection_failed', items=[{'name': 'Example', 'version': '1', 'publisher': 'x'}]); cases.append(data)
        data = report(); data['sections']['users']['items'] = [{'sid': 'S-1-5-21-1', 'name': 'x', 'loaded': True, 'special': False, 'markers': {'unknown': True}}]; cases.append(data)
        for data in cases:
            with self.subTest(data=list(data)):
                with self.assertRaises(ValueError) as error:
                    prep.summarize(data)
                self.assertNotIn('DO_NOT_ECHO', str(error.exception))

    def test_json_duplicate_keys_oversize_and_nonstandard_constants_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            for raw in ('{"schema":1,"schema":1}', '{"schema":NaN}', '['):
                path.write_text(raw, encoding='utf-8')
                with self.assertRaises(ValueError): prep.read_report(path)
            path.write_bytes(b' ' * (prep.MAX_REPORT_BYTES + 1))
            with self.assertRaises(ValueError): prep.read_report(path)
            path.write_text(json.dumps(report()), encoding='utf-8')
            self.assertEqual(prep.read_report(path), report())

    def test_read_report_binds_returned_inventory_to_handoff_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            path.write_text(json.dumps(report()), encoding='utf-8')
            self.assertEqual(prep.read_report(path, expected_reference_id='a' * 32), report())
            for identity in ('b' * 32, 'a' * 31, 'A' * 32, True, 1):
                with self.subTest(identity=identity), self.assertRaises(ValueError):
                    prep.read_report(path, expected_reference_id=identity)
            data = report()
            data['reference_id'] = int('1' * 32)
            with self.assertRaises(ValueError):
                prep.validate_report(data)

    def test_handoff_is_idempotent_does_not_overwrite_and_contains_no_autorun(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'handoff'
            first = prep.prepare_handoff(destination)
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in destination.iterdir()}
            self.assertEqual(prep.prepare_handoff(destination), first)
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in destination.iterdir()})
            self.assertEqual(set(before), {'Collect-ReferenceInventory.ps1', 'Run-Inventory.cmd', 'README.md', 'handoff.json'})
            self.assertEqual(set(first['files']), {'Collect-ReferenceInventory.ps1', 'Run-Inventory.cmd', 'README.md'})
            import hashlib
            for name, digest in first['files'].items():
                self.assertEqual(hashlib.sha256((destination / name).read_bytes()).hexdigest(), digest)
            self.assertEqual(first['expected_smbios_serial'], 'WCREFERENCE-' + first['reference_id'])
            self.assertRegex(first['reference_id'], '^[0-9a-f]{32}$')
            (destination / 'README.md').write_text('existing user data', encoding='utf-8')
            with self.assertRaises(ValueError): prep.prepare_handoff(destination)
            self.assertEqual((destination / 'README.md').read_text(), 'existing user data')

    def test_existing_unknown_files_or_modified_nonce_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'handoff'
            manifest = prep.prepare_handoff(destination)
            path = destination / 'handoff.json'
            manifest['expected_smbios_serial'] = 'HOST'
            path.write_text(json.dumps(manifest), encoding='utf-8')
            before = path.read_bytes()
            with self.assertRaises(ValueError): prep.prepare_handoff(destination)
            self.assertEqual(path.read_bytes(), before)

    def test_bootstrap_guards_and_scope_are_present_without_executing_it(self):
        source = (ROOT / 'guest/reference/Collect-ReferenceInventory.ps1').read_text(encoding='utf-8-sig')
        self.assertLess(source.index('Assert-ReferenceContext'), source.index('$sections ='))
        for text in ('S-1-5-18', 'S-1-5-19', 'S-1-5-20', 'Win32_ComputerSystemProduct', 'CreateNew',
                     'Get-AppxPackage -AllUsers', 'Get-AppxProvisionedPackage -Online'):
            self.assertIn(text, source)
        for command in ('Remove-Item', 'Remove-Appx', 'Start-Process', 'reg load', 'Invoke-Expression'):
            self.assertNotIn(command, source)
        self.assertNotIn('Get-ItemProperty', source, 'Do not read entire registry values including product keys or commands')

    def test_one_action_bootstrap_checks_identity_and_hashes_before_copy_or_uac(self):
        source = (ROOT / 'guest/reference/Run-Inventory.cmd').read_text(encoding='utf-8-sig')
        guard = source.index("$serial -cne $manifest.expected_smbios_serial")
        self.assertLess(guard, source.index('[IO.Directory]::CreateDirectory'))
        self.assertLess(guard, source.index('$result = Start-ReferenceCollector $destination'))
        self.assertLess(source.index("throw 'Source hash mismatch.'"), source.index('[IO.Directory]::CreateDirectory'))
        for required in ('Sysnative', 'System32\\WindowsPowerShell', '-Verb RunAs', '-NoProfile ', '-File "',
                         '-ReturnSerial', 'ReparsePoint', 'CreateNew', 'Existing destination differs',
                         'WindowsClaudeReference', 'Is64BitProcess', 'SessionId -eq 0',
                         'S-1-5-18', 'S-1-5-19', 'S-1-5-20'):
            self.assertIn(required, source)
        self.assertEqual(source.count('-ExecutionPolicy RemoteSigned'), 1)
        for forbidden in ('-EncodedCommand', 'Bypass', 'Unrestricted', 'Unblock-File', 'Set-ExecutionPolicy',
                          'Remove-Item', 'Copy-Item', 'schtasks', 'Sysprep.exe'):
            self.assertNotIn(forbidden, source)

    def test_serial_returns_same_bounded_local_bytes_and_always_disposes_port(self):
        source = (ROOT / 'guest/reference/Collect-ReferenceInventory.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('param([switch]$ReturnSerial)', source)
        self.assertLess(source.index('$bytes.Length -gt 8388608', source.index('$report =')), source.index('[IO.File]::Open($output'))
        self.assertLess(source.index('$stream.Write($bytes'), source.index('if ($ReturnSerial)'))
        self.assertIn('Send-ReferenceReport $bytes $context.reference_id', source)
        self.assertIn("'WCREFERENCE ' + $ReferenceId + ' ' + $Bytes.Length", source)
        self.assertLess(source.index('$port.Write($header'), source.index('$port.Write($Bytes'))
        self.assertIn("[IO.Ports.SerialPort]::new('COM2', 115200", source)
        for required in ('$port.WriteTimeout = 1000', '$timer.ElapsedMilliseconds -ge 30000',
                         '$offset += 4096', '$port.Write($Bytes, $offset', '$port.Close()',
                         '$port.Dispose()', 'complete local reference-report.json is preserved'):
            self.assertIn(required, source)

    @unittest.skipUnless(os.name == 'nt', 'Harmless child-process fixtures require Windows PowerShell 5')
    def test_ps5_logged_file_child_failure_policy_block_cap_and_mocked_uac_cancel(self):
        # Extract only helper definitions: never execute the bootstrap guards or real collector on the host.
        bootstrap = str(ROOT / 'guest/reference/Run-Inventory.cmd').replace("'", "''")
        command = r"""
$ErrorActionPreference='Stop'
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Expected Windows PowerShell 5 fixture.' }
$source=[IO.File]::ReadAllText('__BOOTSTRAP__')
$marker='# WC_REFERENCE_BOOTSTRAP_BODY'
$body=$source.Substring($source.LastIndexOf($marker,[StringComparison]::Ordinal)+$marker.Length)
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseInput($body,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Bootstrap helper parse failed.'}
$names=@('Assert-PlainPath','Get-PublicBytes','Get-CollectorPolicy','New-CollectorStartInfo','Invoke-LoggedCollector','Show-ReferenceResult','Start-ReferenceCollector')
$functions=$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $names},$false)
if($functions.Count -ne 7){throw 'Expected exact fixture helper set.'}
foreach($function in $functions){. ([scriptblock]::Create($function.Extent.Text))}
$scopeNames=@('MachinePolicy','UserPolicy','Process','CurrentUser','LocalMachine')
function New-PolicyRows {
    @($scopeNames | ForEach-Object { [pscustomobject]@{Scope=$_; ExecutionPolicy=[Microsoft.PowerShell.ExecutionPolicy]::Undefined} })
}
$script:PolicyRows=New-PolicyRows
function Get-ExecutionPolicy {
    param([switch]$List)
    if($script:QueryFail){throw 'fixture policy query failure'}
    return $script:PolicyRows
}
$default=Get-CollectorPolicy
if($default.mode -ne 'RemoteSigned'){throw 'Five Undefined scopes must select process-local RemoteSigned.'}
foreach($scope in $scopeNames) {
    foreach($policyName in @('Restricted','AllSigned')) {
        $script:PolicyRows=New-PolicyRows
        ($script:PolicyRows | Where-Object Scope -eq $scope).ExecutionPolicy=[Microsoft.PowerShell.ExecutionPolicy]::$policyName
        $selection=Get-CollectorPolicy
        $info=New-CollectorStartInfo 'C:\harmless.ps1' $selection.mode
        if($selection.mode -ne 'configured' -or $info.Arguments -match '-ExecutionPolicy'){throw 'Explicit policy overridden.'}
    }
}
$script:PolicyRows=New-PolicyRows
$validRows=@($script:PolicyRows)
$malformed=@(@($validRows[0..3]), @($validRows + $validRows[0]), @($validRows[0..3] + $validRows[0]),
            @($validRows[0..3] + [pscustomobject]@{Scope='Unknown';ExecutionPolicy=[Microsoft.PowerShell.ExecutionPolicy]::Undefined}),
            @($validRows[0..3] + [pscustomobject]@{Scope='LocalMachine';ExecutionPolicy='Undefined'}))
foreach($rows in $malformed) {
    $script:PolicyRows=$rows
    try { $null=Get-CollectorPolicy; throw 'Malformed policy accepted.' } catch { if($_.Exception.Message -eq 'Malformed policy accepted.'){throw} }
}
$script:PolicyRows=New-PolicyRows
$script:OriginalStartInfo=${function:New-CollectorStartInfo}
function New-CollectorStartInfo([string]$Collector,[string]$Mode) {
    $script:StartInfoCalls++
    $info=& $script:OriginalStartInfo $Collector $Mode
    if($info.Arguments -notmatch '^-NoProfile (?:-ExecutionPolicy RemoteSigned )?-File "' -or $info.Arguments -notmatch '-ReturnSerial$'){throw 'Actual File chain changed.'}
    # Policy is scoped to this harmless child only; user/machine policy is untouched.
    if($Mode -eq 'RemoteSigned') {
        if($info.Arguments -notmatch '-ExecutionPolicy RemoteSigned -File'){throw 'Default File child lacks its process-only policy.'}
        $info.EnvironmentVariables.Remove('PSExecutionPolicyPreference')
    } else {
        if($info.Arguments -match '-ExecutionPolicy'){throw 'Configured policy unexpectedly overridden.'}
        $info.EnvironmentVariables['PSExecutionPolicyPreference']=$script:FixturePolicy
    }
    return $info
}
$root=Join-Path ([IO.Path]::GetTempPath()) ('reference-child-fixture-'+[guid]::NewGuid().ToString('N'))
$null=[IO.Directory]::CreateDirectory($root)
try {
    $fixture=Join-Path $root 'harmless.ps1'
    [IO.File]::WriteAllText($fixture, 'param([switch]$ReturnSerial); Write-Output "fixture-out"; [Console]::Error.WriteLine(([string][char]0x041e + [char]0x0448 + [char]0x0438 + [char]0x0431 + [char]0x043a + [char]0x0430)); Write-Error "fixture-error" -ErrorAction Continue; exit 7')
    $script:FixturePolicy='RemoteSigned'
    $failure=Join-Path $root 'failure'; $null=[IO.Directory]::CreateDirectory($failure)
    $code=Invoke-LoggedCollector $fixture $failure
    if($code -ne 7){throw 'Harmless File child did not preserve exit 7.'}
    $log=[IO.File]::ReadAllText((Join-Path $failure 'collector-output.log'))
    if($log -notmatch 'fixture-out' -or $log -notmatch 'fixture-error'){throw 'Both child streams were not preserved.'}
    $unicode=[string][char]0x041e + [char]0x0448 + [char]0x0438 + [char]0x0431 + [char]0x043a + [char]0x0430
    if(-not $log.Contains($unicode)){throw 'Unicode child diagnostic was not preserved.'}
    $result=Get-Content -LiteralPath (Join-Path $failure 'collector-exit.json') -Raw | ConvertFrom-Json
    if($result.stage -ne 'collector_exit' -or $result.exit_code -ne 7 -or $result.execution_mode -ne 'RemoteSigned' -or $result.execution_scopes.Process -ne 'Undefined'){throw 'Child result missing.'}
    try { $null=Invoke-LoggedCollector $fixture $failure; throw 'Overwrite accepted.' } catch { if($_.Exception.Message -eq 'Overwrite accepted.'){throw} }
    $query=Join-Path $root 'query-failure'; $null=[IO.Directory]::CreateDirectory($query)
    $beforeStartInfo=$script:StartInfoCalls
    $script:QueryFail=$true
    if((Invoke-LoggedCollector 'C:\must-not-launch.ps1' $query) -ne 1){throw 'Policy query failure did not refuse.'}
    $queryResult=Get-Content -LiteralPath (Join-Path $query 'collector-exit.json') -Raw | ConvertFrom-Json
    if($queryResult.stage -ne 'policy_query' -or $queryResult.exit_code -ne 1){throw 'Policy query diagnostic missing.'}
    # A disposed diagnostic handle permits exclusive access after failed StartInfo/policy setup.
    $handle=[IO.File]::Open((Join-Path $query 'collector-output.log'),[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    $handle.Dispose()
    $script:QueryFail=$false
    if($script:StartInfoCalls -ne $beforeStartInfo){throw 'Policy failure reached child setup.'}
    $case=0
    foreach($rows in $malformed) {
        $script:PolicyRows=$rows
        $caseDirectory=Join-Path $root ('malformed-'+$case); $case++; $null=[IO.Directory]::CreateDirectory($caseDirectory)
        if((Invoke-LoggedCollector 'C:\must-not-launch.ps1' $caseDirectory) -ne 1 -or $script:StartInfoCalls -ne $beforeStartInfo){throw 'Malformed policy reached child setup.'}
    }
    $script:FixturePolicy='Restricted'
    $script:PolicyRows=New-PolicyRows
    ($script:PolicyRows | Where-Object Scope -eq 'Process').ExecutionPolicy=[Microsoft.PowerShell.ExecutionPolicy]::Restricted
    $policy=Join-Path $root 'policy'; $null=[IO.Directory]::CreateDirectory($policy)
    $code=Invoke-LoggedCollector $fixture $policy
    $log=[IO.File]::ReadAllText((Join-Path $policy 'collector-output.log'))
    if($code -eq 0 -or $log -match 'fixture-out' -or $log -notmatch 'PSSecurityException|UnauthorizedAccess'){throw 'Restricted child refusal not preserved.'}
    $script:FixturePolicy='RemoteSigned'
    $script:PolicyRows=New-PolicyRows
    [IO.File]::WriteAllText($fixture, 'param([switch]$ReturnSerial); [Console]::Out.Write(("x"*100000)); [Console]::Error.Write(("y"*100000)); exit 9')
    $capped=Join-Path $root 'capped'; $null=[IO.Directory]::CreateDirectory($capped)
    if((Invoke-LoggedCollector $fixture $capped) -ne 9){throw 'Capped pipes did not drain to completion.'}
    if((Get-Item -LiteralPath (Join-Path $capped 'collector-output.log')).Length -ne 65536){throw 'Output cap failed.'}
    function Start-Process {
        param($FilePath,$ArgumentList,$Verb,[switch]$PassThru,[switch]$Wait)
        if($Verb -ne 'RunAs' -or $ArgumentList -notmatch '--collector' -or $FilePath -notmatch 'System32\\cmd.exe$'){throw 'Expected elevated wrapper.'}
        return [pscustomobject]@{ExitCode=7}
    }
    $failedChild=Start-ReferenceCollector $failure
    if($failedChild.stage -ne 'collector_exit' -or $failedChild.exit_code -ne 7){throw 'Parent lost child result.'}
    $display=(Show-ReferenceResult $failedChild $failure) -join "`n"
    if($display -notmatch 'fixture-out' -or -not $display.Contains($unicode) -or $display -notmatch 'exit code: 7' -or $display -notmatch 'Execution mode: RemoteSigned'){throw 'Parent window lost child output or exit.'}
    [IO.File]::WriteAllBytes((Join-Path $failure 'collector-output.log'), [byte[]]@(0x61,0xe2,0x82))
    $display=(Show-ReferenceResult $failedChild $failure) -join "`n"
    if(-not $display.Contains([string][char]0xfffd) -or $failedChild.exit_code -ne 7){throw 'Truncated UTF8 hid output or changed exit.'}
    $chain=Join-Path $root ('child space &' + [char]0x0416 + '!')
    $null=[IO.Directory]::CreateDirectory($chain)
    $fake="@echo off`r`nsetlocal DisableDelayedExpansion`r`nif not `"%~1`"==`"--collector`" exit /b 18`r`n> `"%~dp0collector-mode.txt`" echo fixture`r`nexit /b 7`r`n"
    [IO.File]::WriteAllText((Join-Path $chain 'Run-Inventory.cmd'),$fake,[Text.Encoding]::ASCII)
    [IO.File]::WriteAllText((Join-Path $chain 'collector-exit.json'),'{"schema":1,"stage":"collector_exit","exit_code":7,"execution_mode":"RemoteSigned"}')
    function Start-Process {
        param($FilePath,$ArgumentList,$Verb,[switch]$PassThru,[switch]$Wait)
        if($Verb -ne 'RunAs'){throw 'Expected UAC intent before fixture removes elevation.'}
        # Execute actual production CMD quoting; remove only real UAC from this harmless fixture.
        Microsoft.PowerShell.Management\Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -PassThru -Wait -WindowStyle Hidden
    }
    $chainResult=Start-ReferenceCollector $chain
    if($chainResult.exit_code -ne 7 -or $chainResult.stage -ne 'collector_exit' -or
       -not (Test-Path -LiteralPath (Join-Path $chain 'collector-mode.txt'))){throw 'Native CMD wrapper quoting or mode failed.'}
    function Start-Process {
        param($FilePath,$ArgumentList,$Verb,[switch]$PassThru,[switch]$Wait)
        if($Verb -ne 'RunAs' -or $ArgumentList -notmatch '--collector' -or $ArgumentList -notmatch '/v:off'){throw 'UAC wrapper chain changed.'}
        throw [ComponentModel.Win32Exception]::new(1223)
    }
    $cancelled=Start-ReferenceCollector $root
    if($cancelled.stage -ne 'uac_cancelled' -or $cancelled.exit_code -ne 1223){throw 'UAC cancellation not classified.'}
    Write-Output 'harmless-child-fixtures-passed'
} finally {
    # Only this exact newly created fixture directory is removed, using one shell throughout.
    if([IO.Path]::GetFullPath($root).StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()),[StringComparison]::OrdinalIgnoreCase) -and
       [IO.Path]::GetFileName($root) -match '^reference-child-fixture-[0-9a-f]{32}$') { Remove-Item -LiteralPath $root -Recurse -Force }
}
""".replace('__BOOTSTRAP__', bootstrap)
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace')[-2000:])
        self.assertIn(b'harmless-child-fixtures-passed', result.stdout)

    @unittest.skipUnless(os.name == 'nt', 'Typed Appx producer fixture requires Windows PowerShell 5')
    def test_typed_appx_sid_field_roundtrips_to_strict_report_without_host_inventory(self):
        source = str(ROOT / 'guest/reference/Collect-ReferenceInventory.ps1').replace("'", "''")
        command = r"""
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SOURCE__',[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Collector parse failed.'}
$names=@('Read-Section','Get-AppxUserRows')
$functions=$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $names},$false)
if($functions.Count -ne 2){throw 'Expected Appx projection helpers.'}
foreach($function in $functions){. ([scriptblock]::Create($function.Extent.Text))}
# Metadata-equivalent shape: public string fields, default Object.ToString gives only the type name.
Add-Type -TypeDefinition 'namespace Fixture { public class AppxUserSecurityId { public string Sid; public string Username; } }'
$wrapper=[Fixture.AppxUserSecurityId]::new()
$wrapper.Sid='S-1-5-21-1-2-3-1001'; $wrapper.Username='DO_NOT_EXPORT_USERNAME'
$user=[pscustomobject]@{UserSecurityId=$wrapper;InstallState='Installed'}
$valid=@(Get-AppxUserRows @($user))
$user.InstallState='FutureState'
$unknown=@(Get-AppxUserRows @($user))
$bad=@(foreach($sid in @($null,'','Fixture.AppxUserSecurityId','S-1-bad','s-1-5-18',"S-1-5-18`n",('S-1-'+('1-'*100)+'1'))) {
    $invalid=[Fixture.AppxUserSecurityId]::new(); $invalid.Sid=$sid; $invalid.Username=$wrapper.Sid
    Read-Section { Get-AppxUserRows @([pscustomobject]@{UserSecurityId=$invalid;InstallState='Installed'}) }
})
$missing=Read-Section { Get-AppxUserRows @([pscustomobject]@{UserSecurityId=[pscustomobject]@{Value=$wrapper.Sid};InstallState='Installed'}) }
$raw=Read-Section { Get-AppxUserRows @([pscustomobject]@{UserSecurityId=$wrapper.Sid;InstallState='Installed'}) }
$partial=Read-Section { Get-AppxUserRows @($user,[pscustomobject]@{UserSecurityId=$null;InstallState='Installed'}) }
@{valid=$valid;unknown=$unknown;bad=$bad;missing=$missing;raw=$raw;partial=$partial;old_sid=[string]$wrapper} | ConvertTo-Json -Depth 8 -Compress
""".replace('__SOURCE__', source)
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, 'Typed Appx projection fixture failed')
        data = json.loads(result.stdout.decode('utf-8-sig'))
        self.assertEqual(data['old_sid'], 'Fixture.AppxUserSecurityId')
        self.assertEqual(data['valid'], [{'sid': report()['context']['sid'], 'state': 'Installed'}])
        self.assertEqual(data['unknown'], [{'sid': report()['context']['sid'], 'state': 'Unknown'}])
        sample = report()
        sample['sections']['appx']['items'] = [{'name': 'Fixture', 'package': 'Fixture_1.0_x64',
            'version': '1.0', 'framework': False, 'users': data['valid']}]
        self.assertEqual(prep.validate_report(json.loads(json.dumps(sample))), sample)
        sample['sections']['appx']['items'][0]['users'][0]['sid'] = data['old_sid']
        with self.assertRaises(ValueError):
            prep.validate_report(sample)
        for section in data['bad'] + [data['missing'], data['raw'], data['partial']]:
            self.assertEqual(section, {'status': 'unavailable', 'items': [], 'error': 'collection_failed'})
        self.assertNotIn('DO_NOT_EXPORT_USERNAME', json.dumps(data))

    @unittest.skipUnless(os.name == 'nt', 'Safe diagnostic formatter fixture requires Windows PowerShell 5')
    def test_collector_fixed_stage_formatter_never_prints_exception_message(self):
        source = str(ROOT / 'guest/reference/Collect-ReferenceInventory.ps1').replace("'", "''")
        command = r"""
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('__SOURCE__',[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Collector parse failed.'}
$function=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Format-ReferenceFailure'},$false)
. ([scriptblock]::Create($function.Extent.Text))
$exception=[Exception]::new('SECRET outer message',[InvalidOperationException]::new('SECRET CIM row'))
foreach($stage in @('context','manifest','identity','hash','os_metadata','sections','report_encode','report_write','serial_return')) {
    $text=Format-ReferenceFailure $stage $exception
    if($text -notmatch ('stage='+$stage+'; exception=InvalidOperationException; hresult=-?[0-9]+') -or $text -match 'SECRET'){throw 'Unsafe or missing collector diagnostic.'}
}
$text=Format-ReferenceFailure 'SECRET stage' $exception
if($text -notmatch 'stage=unknown;' -or $text -match 'SECRET'){throw 'Untrusted stage echoed.'}
Write-Output 'safe-formatter-passed'
""".replace('__SOURCE__', source)
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, 'Safe collector formatter fixture failed')
        self.assertIn(b'safe-formatter-passed', result.stdout)

    @unittest.skipUnless(os.name == 'nt', 'Windows PowerShell parser is Windows-only')
    def test_bootstrap_parses_in_windows_powershell_without_running_it(self):
        source = str(ROOT / 'guest/reference/Collect-ReferenceInventory.ps1').replace("'", "''")
        bootstrap = str(ROOT / 'guest/reference/Run-Inventory.cmd').replace("'", "''")
        command = ("$tokens=$null; $errors=$null; $null=[Management.Automation.Language.Parser]::ParseFile('" + source + "',[ref]$tokens,[ref]$errors); if($errors.Count){exit 1}; "
                   "$body=[IO.File]::ReadAllText('" + bootstrap + "'); $marker='# WC_REFERENCE_BOOTSTRAP_BODY'; "
                   "$body=$body.Substring($body.LastIndexOf($marker,[StringComparison]::Ordinal)+$marker.Length); "
                   "$null=[Management.Automation.Language.Parser]::ParseInput($body,[ref]$tokens,[ref]$errors); if($errors.Count){exit 1}")
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, 'PowerShell parse-only verification failed')


if __name__ == '__main__':
    unittest.main()
