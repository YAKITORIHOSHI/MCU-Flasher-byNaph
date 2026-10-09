@echo off
setlocal
rem Legacy filename: only captured processes owned by this checkout are targeted.
rem Preview by default. -Apply requires confirmation because writes may be active.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\maintenance.ps1" -Mode Stop %*
exit /b %ERRORLEVEL%
