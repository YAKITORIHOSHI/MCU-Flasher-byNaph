@echo off
REM clean_pycache.bat — Delete all __pycache__ dirs and .pyc/.pyo files
REM in this project (excluding venv/ and other heavy dirs).
REM
REM Usage: Just double-click or run from a terminal in the project root.
REM
REM This is a one-time maintenance script.  The app itself now runs with
REM PYTHONDONTWRITEBYTECODE=1 so .pyc files are never created at startup;
REM this script is only needed if you want to manually clear stale bytecode
REM after a Python upgrade or after copying the project.

setlocal enabledelayedexpansion

REM Resolve the script's own directory
set "SCRIPT_DIR=%~dp0"
REM Remove trailing backslash
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

echo Cleaning Python bytecode caches in: %SCRIPT_DIR%
echo.

set "COUNT_DELETED=0"
set "COUNT_DIRS=0"

REM Walk subdirectories, skipping venv and other heavy/known dirs
for /f "delims=" %%D in ('dir /a:d /b /s "%SCRIPT_DIR%"') do (
    set "DIRNAME=%%~nD"
    set "DIRPATH=%%~fD"
    if /i not "!DIRNAME!"=="env" if /i not "!DIRNAME!"==".git" if /i not "!DIRNAME!"==".pio" if /i not "!DIRNAME!"=="node_modules" if /i not "!DIRNAME!"==".vscode" (
        if /i "!DIRNAME!"=="__pycache__" (
            rd /s /q "!DIRPATH!" 2>nul && set /a COUNT_DIRS+=1
        )
    )
)

echo Deleted %COUNT_DIRS% __pycache__ directories.

REM Delete stray .pyc / .pyo files in the project root and src tree
for /f "delims=" %%F in ('dir /a:-d /b /s "%SCRIPT_DIR%\*.pyc" 2^>nul') do (
    set "FPATH=%%~fF"
    set "PARENT=%%~dpF"
    echo !PARENT! | findstr /i "env" >nul
    if errorlevel 1 (
        del /q "!FPATH!" 2>nul && set /a COUNT_DELETED+=1
    )
)

for /f "delims=" %%F in ('dir /a:-d /b /s "%SCRIPT_DIR%\*.pyo" 2^>nul') do (
    set "FPATH=%%~fF"
    set "PARENT=%%~dpF"
    echo !PARENT! | findstr /i "env" >nul
    if errorlevel 1 (
        del /q "!FPATH!" 2>nul && set /a COUNT_DELETED+=1
    )
)

echo Deleted %COUNT_DELETED% stray .pyc/.pyo files.
echo.
echo Done.
pause
