@echo off
setlocal
rem Defaults to a Windows project reset preview. -Apply requires typed confirmation.
rem Shared Windows uninstalls require both -FullReset and -Apply.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0DELETE_EVERYTHING_DO_NOT_RUN.ps1" %*
exit /b %errorlevel%
