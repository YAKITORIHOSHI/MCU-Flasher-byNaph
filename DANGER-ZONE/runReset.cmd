@echo off
rem Full Windows dependency reset. The PowerShell script still requires an
rem exact typed confirmation before it changes the machine.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0DELETE_EVERYTHING_DO_NOT_RUN.ps1" -FullReset %*
