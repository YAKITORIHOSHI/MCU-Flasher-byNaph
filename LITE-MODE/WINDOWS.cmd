@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0WINDOWS.exe" goto launch
where cargo >nul 2>nul
if errorlevel 1 (
  echo Build Lite with Rust first: py -3 tools\build.py
  pause
  exit /b 1
)
cargo build --locked --release -j 4 --manifest-path "%~dp0Cargo.toml"
if errorlevel 1 (
  pause
  exit /b 1
)
copy /y "%~dp0target\release\mcu-flasher-lite.exe" "%~dp0WINDOWS.exe" >nul
:launch
start "" /wait "%~dp0WINDOWS.exe" %*
