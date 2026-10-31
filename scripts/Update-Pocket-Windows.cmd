@echo off
rem One-file manual update for Pocket Windows copies whose launcher cannot update itself (2026.10.25.3 to 2026.10.29.3).
rem Put this file next to Launcher.exe and run it. The PowerShell part below is read from this file as UTF-8.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$script = (Get-Content -LiteralPath '%~f0' -Encoding UTF8 | Select-Object -Skip 7) -join [char]10; & ([scriptblock]::Create($script)) '%~dp0'"
set code=%ERRORLEVEL%
if not defined POCKET_UPDATE_NO_PAUSE pause
exit /b %code%
param([string]$Root)
$ErrorActionPreference = 'Stop'
# Same trust as the launcher (launcher-wpf/AppUpdate.cs): a signed manifest decides, not the download host.
$Feeds = @('https://github.com/TheFactChain/pocket-windows/releases/latest/download/update-manifest.json',
           'https://188.130.234.243:8443/pocket-windows/update-manifest.json')
$Keys = @('RUNTMSAAAADdjP9TMHcJC1yzo+EMu+3h5v1DoWszB9oUdg5Q0Ia7oovhxPD7lBXBtq+AV8kRzLUmALZKv/5pmGAyVC2eetVx',
          'RUNTMSAAAAAc7ESHeltKr8P7wsTW09Sxg3BQTxKFbQyLR3tiNeM4Yn737ZdvxAjyEpl15JAt1aN28RNYsH8fOh6f3pUZRalx')
if ($env:POCKET_UPDATE_TEST_FEED) { $Feeds = @($env:POCKET_UPDATE_TEST_FEED) }  # a source override never weakens the signature check
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Say([string]$text) { Write-Host $text }
function Stop-Update([string]$text) { Write-Host ''; Write-Host $text -ForegroundColor Red; Write-Host 'Программа не изменена.'; exit 1 }
function Id-Parts([string]$id) { if ($id -notmatch '^\d{4}\.\d{2}\.\d{2}\.[1-9]\d{0,8}$') { return $null }; return [long[]]($id.Split('.')) }
function Is-Newer([string]$a, [string]$b) {
    $x = Id-Parts $a; $y = Id-Parts $b; if (-not $x -or -not $y) { return $false }
    for ($i = 0; $i -lt 4; $i++) { if ($x[$i] -ne $y[$i]) { return $x[$i] -gt $y[$i] } }; return $false
}
function Read-Manifest([string]$url) {
    $client = New-Object Net.WebClient; $client.Headers['User-Agent'] = 'PocketWindows-ManualUpdate'
    $envelope = $client.DownloadString($url) | ConvertFrom-Json
    $body = [Convert]::FromBase64String($envelope.manifest); $signature = [Convert]::FromBase64String($envelope.signature)
    $trusted = $false
    foreach ($key in $Keys) {
        try {
            $cng = [Security.Cryptography.CngKey]::Import([Convert]::FromBase64String($key), [Security.Cryptography.CngKeyBlobFormat]::EccPublicBlob)
            if ($signature.Length -eq 64 -and (New-Object Security.Cryptography.ECDsaCng $cng).VerifyData($body, $signature)) { $trusted = $true }
        } catch { }
    }
    if (-not $trusted) { throw 'подпись не подтверждена' }
    $manifest = [Text.Encoding]::UTF8.GetString($body) | ConvertFrom-Json
    if ($manifest.schema -ne 1 -or -not (Id-Parts $manifest.release_id) -or -not $manifest.assets.app.url) { throw 'неизвестный формат' }
    return $manifest
}

$Root = [IO.Path]::GetFullPath($Root.TrimEnd('\'))
Say "Pocket Windows: ручное обновление"
Say "Папка: $Root"
if (-not (Test-Path -LiteralPath (Join-Path $Root 'Launcher.exe')) -or -not (Test-Path -LiteralPath (Join-Path $Root 'release-info.json'))) {
    Stop-Update 'Положи этот файл в папку Pocket Windows рядом с Launcher.exe и запусти снова.'
}
$current = (Get-Content -LiteralPath (Join-Path $Root 'release-info.json') -Raw -Encoding UTF8 | ConvertFrom-Json).release_id
Say "Установлена версия: $current"
if (Test-Path -LiteralPath (Join-Path $Root 'state\app-update.json')) {
    Stop-Update 'Есть незавершённое обновление. Открой Launcher.exe и выбери восстановление.'
}
$busy = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase) })
if ($busy.Count) {
    Stop-Update ('Сначала штатно выключи Windows и закрой лаунчер (работает: ' + (($busy | ForEach-Object Name | Sort-Object -Unique) -join ', ') + ').')
}

$manifest = $null
foreach ($feed in $Feeds) {
    try { $candidate = Read-Manifest $feed; if (-not $manifest -or (Is-Newer $candidate.release_id $manifest.release_id)) { $manifest = $candidate } }
    catch { Say "Источник недоступен: $feed ($($_.Exception.Message))" }
}
if (-not $manifest) { Stop-Update 'Не удалось получить подписанные сведения об обновлении. Проверь интернет и повтори.' }
if (-not (Is-Newer $manifest.release_id $current)) { Say "Уже установлена последняя версия ($current). Ничего делать не нужно."; exit 0 }
$app = $manifest.assets.app
Say "Доступна версия $($manifest.release_id), пакет $([math]::Round($app.bytes / 1MB)) МБ."

# Download beside TEMP if it has room, otherwise next to the Pocket Windows folder.
$need = [long]$app.bytes * 3 + 512MB
$parent = $null
foreach ($candidate in @($env:TEMP, (Split-Path -Parent $Root))) {
    try { if ((New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($candidate))).AvailableFreeSpace -ge $need) { $parent = $candidate; break } } catch { }
}
if (-not $parent) { Stop-Update ("Не хватает места: нужно около $([math]::Ceiling($need / 1GB)) ГБ свободного места на диске.") }
$work = Join-Path $parent ('PocketWindows-update-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    $zip = Join-Path $work 'app.zip'
    Say 'Скачиваем пакет, это может занять несколько минут...'
    $client = New-Object Net.WebClient; $client.Headers['User-Agent'] = 'PocketWindows-ManualUpdate'
    $client.DownloadFile($app.url, $zip)
    if ((Get-Item -LiteralPath $zip).Length -ne [long]$app.bytes -or (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $app.sha256.ToUpperInvariant()) {
        Stop-Update 'Скачанный пакет не совпал с подписанной контрольной суммой.'
    }
    Say 'Пакет проверен. Распаковываем...'
    $stage = Join-Path $work 'stage'
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $stage)
    Remove-Item -LiteralPath $zip -Force
    $python = Get-ChildItem -LiteralPath (Join-Path $stage 'app\runtime') -Directory -Filter 'python-*' | ForEach-Object { Join-Path $_.FullName 'python.exe' } | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $python) { Stop-Update 'В пакете нет среды установки.' }
    Say 'Устанавливаем. Диск Windows, настройки и подключения не меняются...'
    & $python -X utf8 -I -B (Join-Path $stage 'app\scripts\release_portable.py') update --release $stage --target $Root
    if ($LASTEXITCODE -ne 0) { Stop-Update 'Установка не завершилась, изменения откатаны. Сохрани текст выше и напиши в Issues проекта.' }
    $installed = (Get-Content -LiteralPath (Join-Path $Root 'release-info.json') -Raw -Encoding UTF8 | ConvertFrom-Json).release_id
    Write-Host ''; Write-Host "Готово: установлена версия $installed. Открой Launcher.exe; дальше обновления приходят кнопкой «Обновить»." -ForegroundColor Green
    exit 0
}
finally { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
