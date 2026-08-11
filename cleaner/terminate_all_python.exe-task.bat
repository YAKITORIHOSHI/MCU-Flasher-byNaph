@echo off
setlocal

rem Recover only MCU Flasher processes. Do not terminate unrelated Python work.
set "APP_ROOT=%~dp0.."

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root=(Resolve-Path -LiteralPath '%APP_ROOT%').Path.TrimEnd('\').ToLowerInvariant();" ^
  "$targets=Get-CimInstance Win32_Process | Where-Object {" ^
  "  $_.Name -match '(?i)^pythonw?\.exe$' -and" ^
  "  (([string]$_.ExecutablePath).ToLowerInvariant().StartsWith($root) -or ([string]$_.CommandLine).ToLowerInvariant().Contains($root)) -and" ^
  "  ([string]$_.CommandLine) -match '(?i)(launcher\.py|bootstrap\.py|mcu_flash_gui\.py|-m\s+pip\s+show)'" ^
  "};" ^
  "foreach($p in $targets){ try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Write-Host ('Stopped MCU Flasher PID ' + $p.ProcessId) } catch {} }"

echo.
echo MCU Flasher recovery scan complete. Unrelated Python processes were left running.
pause
