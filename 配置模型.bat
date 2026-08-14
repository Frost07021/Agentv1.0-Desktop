@echo off
setlocal
cd /d "%~dp0"
set "FURA_PWSH=C:\Program Files\PowerShell\7\pwsh.exe"
if exist "%FURA_PWSH%" (
  "%FURA_PWSH%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\configure-model.ps1" %*
) else (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\configure-model.ps1" %*
)
if errorlevel 1 (
  echo.
  echo Model configuration failed.
  pause
)
endlocal
