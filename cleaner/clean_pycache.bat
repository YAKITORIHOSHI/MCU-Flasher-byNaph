@echo off
setlocal
rem Preview app-source bytecode cleanup; heavy and protected trees are pruned.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\maintenance.ps1" -Mode Bytecode %*
exit /b %ERRORLEVEL%
