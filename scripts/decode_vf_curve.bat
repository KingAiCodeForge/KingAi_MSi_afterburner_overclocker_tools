@echo off
REM Decode MSI Afterburner VF curves — batch wrapper for PowerShell script
REM Usage: decode_vf_curve.bat [--all]
REM
REM Pass --all to show every VF point instead of just inflection points.

setlocal
set "SCRIPT_DIR=%~dp0"

set "PS_ARGS="
if /I "%~1"=="--all" set "PS_ARGS=-ShowAll"

powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%SCRIPT_DIR%decode_vf_curve.ps1" %PS_ARGS%

endlocal
