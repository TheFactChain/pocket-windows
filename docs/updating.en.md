[Русский](updating.md) | [English](updating.en.md) · [Home](../README.en.md)

# Updating Pocket Windows

An application update does not replace the Windows disk, applications installed inside it, saved connections or personal settings. The full bundle for a first installation and an application update are different archives.

Find your installed version in **«Обновления» (Updates)** in the launcher. In an older copy, open `release-info.json` next to `Launcher.exe` and read `release_id`. The current published release is on the [Latest page](https://github.com/TheFactChain/pocket-windows/releases/latest).

## Before updating

1. Save your work and shut down guest Windows normally. Wait until it stops.
2. To make a backup, copy the entire stopped Pocket Windows folder to another disk. It includes your personal data; store it accordingly.
3. Keep the folder writable and check free disk space. The launcher also checks space for downloading, installation and rollback.
4. Choose the instructions **for your starting version** below. Do not extract `app` over the installation manually or replace the Windows disk with another image.

## Version 2026.10.25.3 and later

The full **2026.10.30.1** bundle on Google Drive already includes the updater. Use this normal flow for it, including immediately after the first extraction.

1. Open **«Обновления» (Updates)** in the launcher.
2. Click **«Проверить» (Check)**, then **«Обновить» (Update)** and wait for completion. The panel may close and reopen during installation.
3. Check the new version number, then start guest Windows. Its helper updates at the next sign-in.

The new updater checks file contents and downloads changed chunks with resume support. The amount depends on your starting version and cache. If a separate complete download is offered, read its size before confirming. You do not need to download `-chunks.zip`, `-update*.json` or `-updater.zip` manually.

These steps cover a normal update of a healthy installation. An older operation that was interrupted or moved between folders may need separate recovery; do not overwrite files manually while it is unresolved.

## Version 2026.10.24.13

The verified transition uses the **external updater to install 2026.10.25.7**. Do not use the old built-in update button for this transition: it does not fully replace the active gateway. After the transition, open the new launcher and use its normal update flow to reach the current release.

1. Shut down guest Windows normally and close every launcher panel for that copy.
2. Download `Pocket-Windows-2026.10.25.7-app.zip` from [release 2026.10.25.7](https://github.com/TheFactChain/pocket-windows/releases/tag/v2026.10.25.7).
3. Open PowerShell. Change the paths **in the first two lines** to your installed folder (the one containing `Launcher.exe`) and downloaded ZIP. Run the rest of the block unchanged:

```powershell
$installed = 'D:\Pocket Windows'
$archive = 'C:\Users\YOUR_NAME\Downloads\Pocket-Windows-2026.10.25.7-app.zip'
$ErrorActionPreference = 'Stop'
$expected = '70379972667e923383c35519c73812072b009cb2448bf9710e8ef89e13798c72'
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) {
    throw 'SHA256 mismatch. The archive will not be used.'
}
$stage = Join-Path (Split-Path -Parent $archive) ('PocketWindows-update-' + [Guid]::NewGuid().ToString('N'))
Expand-Archive -LiteralPath $archive -DestinationPath $stage
& "$stage\app\runtime\python-3.12.14\python.exe" -X utf8 -I -B "$stage\app\scripts\release_portable.py" update --release $stage --target $installed
if ($LASTEXITCODE -ne 0) { throw 'Update did not finish. Keep the error and recovery files.' }
```

After success, open `Launcher.exe` **in your installed folder**, then open Updates. These steps reproduce the published 24.13 to 25.7 transition: 990 application files and preservation of the disk, UEFI and settings were checked on a separate copy. That does not verify a transition from 24.12 or live acceptance of every later feature.

## If the update does not finish

- Do not run multiple installations at once. Keep the error text and starting version.
- If the launcher opens, return to Updates and follow its message about resuming or recovery. Previously verified chunks may be reused.
- Do not delete `state`, the cache, journal or saved updater toolkit: they may be needed to resume or roll back.
- If the launcher does not open or recovery fails, open an [issue](https://github.com/TheFactChain/pocket-windows/issues/new) with the starting and target versions. Remove secrets and personal paths from screenshots and logs before posting.

Application rollback does not restore a damaged Windows disk. Your data needs a separate backup.
