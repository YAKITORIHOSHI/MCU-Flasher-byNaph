@echo off
setlocal
rem Preview only by default. -Apply requests exact typed confirmation before UAC.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0PC_Crash_Diagnostic_and_Fix.ps1" %*
exit /b %errorlevel%
