@echo off
setlocal
rem Preview first. Explicit -Apply confirms before requesting elevation.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0remove_mcu_drivers.ps1" %*
exit /b %ERRORLEVEL%
