@echo off
setlocal DisableDelayedExpansion
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -File "%~dp0scripts\Start-Runtime.ps1"
exit /b %errorlevel%
