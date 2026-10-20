import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PowerShellGuiResultTests(unittest.TestCase):
    def test_ps51_parses_wrappers_and_serializes_only_public_result_fields(self):
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        with tempfile.TemporaryDirectory(prefix='gui result ') as temporary:
            root = Path(temporary)
            (root / 'logs').mkdir()
            source = str(ROOT / 'scripts/Start-Runtime.ps1').replace("'", "''")
            project = str(root).replace("'", "''")
            script = r"""
$ErrorActionPreference='Stop'
$source='SOURCE'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'PS5.1 parser errors'}
foreach($name in @('Stop-Runtime.ps1','Build-Launcher.ps1')){
    $parse=[Management.Automation.Language.Parser]::ParseFile((Join-Path (Split-Path -Parent $source) $name),[ref]$tokens,[ref]$errors)
    if($errors.Count){throw 'PS5.1 parser errors'}
}
$functions=$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in @('Get-NativeStartupError','Write-GuiStartupResult')},$false)
if($functions.Count -ne 2){throw 'Typed startup result functions missing'}
foreach($function in $functions){. ([ScriptBlock]::Create($function.Extent.Text))}
$Project='PROJECT'
$guiPath=Join-Path $Project 'logs\gui-start-00000000000000000000000000000000.json'
$receipt=Join-Path $Project 'logs\runtime-launch-fixture\receipt.json'
$wrapped=[InvalidOperationException]::new('synthetic test',[ComponentModel.Win32Exception]::new(1223))
$native=Get-NativeStartupError $wrapped
Write-GuiStartupResult 'uac_cancelled' $receipt $native
$result=Get-Content -LiteralPath $guiPath -Raw -Encoding UTF8|ConvertFrom-Json
@{powershell_major=$PSVersionTable.PSVersion.Major;native_error=$native;result=$result}|ConvertTo-Json -Depth 4 -Compress
""".replace('SOURCE', source).replace('PROJECT', project)
            encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
            result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                    capture_output=True, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).decode(errors='replace'))
            data = json.loads(result.stdout.decode('utf-8-sig'))
            self.assertEqual(data['powershell_major'], 5)
            self.assertEqual(data['native_error'], 1223)
            self.assertEqual(data['result']['code'], 'uac_cancelled')
            self.assertEqual(data['result']['receipt'], 'logs/runtime-launch-fixture/receipt.json')
            self.assertEqual(set(data['result']), {'schema', 'code', 'receipt', 'native_error'})


if __name__ == '__main__':
    unittest.main()
