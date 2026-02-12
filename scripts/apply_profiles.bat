@echo off
REM Apply tiered overclock profiles to MSI Afterburner — batch wrapper
REM Usage: apply_profiles.bat [--dry-run]

setlocal
set "SCRIPT_DIR=%~dp0"

set "PS_ARGS="
if /I "%~1"=="--dry-run" set "PS_ARGS=-DryRun"

powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%SCRIPT_DIR%apply_profiles.ps1" %PS_ARGS%

endlocal
