@echo off
cd /d "%~dp0"

:: Check if the project virtual environment 'env' exists in the root folder
set "ENV_DIR=..\..\..\env"

if not exist "%ENV_DIR%" (
    echo [ERROR] Virtual environment 'env' was not found at:
    echo %CD%\%ENV_DIR%
    pause
    exit /b 1
)

echo [1/2] Installing/verifying dependencies in 'env'...
"%ENV_DIR%\Scripts\pip.exe" install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

echo [2/2] Starting Monaco Mini-IDE...
"%ENV_DIR%\Scripts\python.exe" main.py
