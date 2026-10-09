@echo off
setlocal
rem Preview Windows env/package cleanup. Use -Apply for confirmed maintenance.
rem Settings, recovery, protected sketch caches and Ubuntu resources are retained.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\maintenance.ps1" -Mode Fresh %*
exit /b %ERRORLEVEL%
