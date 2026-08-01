# Port Stable Windows Code to Linux (bootstrap + GUI)

## Background
The Windows versions ([bootstrap.py](file:///media/naphtali/Naphtali/PERSONAL-PROJECT/ESP%20Flash%20GUI/_MCU_Flash_GUI_V7.5-byNAPH/bootstrap.py) and [mcu_flash_gui.py](file:///media/naphtali/Naphtali/PERSONAL-PROJECT/ESP%20Flash%20GUI/_MCU_Flash_GUI_V7.5-byNAPH/mcu_flash_gui.py)) are the stable, feature-complete source of truth. The existing Linux files ([bootstrap_linux.py](file:///media/naphtali/Naphtali/PERSONAL-PROJECT/ESP%20Flash%20GUI/_MCU_Flash_GUI_V7.5-byNAPH/bootstrap_linux.py) and [mcu_flash_gui_linux.py](file:///media/naphtali/Naphtali/PERSONAL-PROJECT/ESP%20Flash%20GUI/_MCU_Flash_GUI_V7.5-byNAPH/mcu_flash_gui_linux.py)) are older, stripped-down ports with missing features and fixes.

The goal is to **overwrite** the Linux files with fresh ports from the stable Windows code, adapting only the platform-specific parts.

> [!IMPORTANT]
> The existing Linux files will be **completely replaced** with new ports based on the Windows source. Any Linux-specific patches already in the old files will be manually reviewed and merged.

## Key Differences Summary (Windows → Linux)

| Windows Feature | Linux Adaptation |
|---|---|
| `subprocess.CREATE_NO_WINDOW` | Remove (use `0` or omit entirely) |
| `win_subprocess_hide` module | Remove entirely (not needed on Linux) |
| `sys.stdout.reconfigure(encoding="utf-8")` | Remove (Linux is already UTF-8) |
| `C:\.platformio-mcu-gui` junction path | Use `~/.cache/mcu_flash_gui/.platformio` or `~/.platformio` |
| `Segoe UI` font | Use `Ubuntu`, `DejaVu Sans`, or system default |
| `Consolas` font | Use `Ubuntu Mono`, `DejaVu Sans Mono`, or `monospace` |
| `windll.shcore.SetProcessDpiAwareness(1)` | Remove (X11/Wayland handles DPI differently) |
| `root.state("zoomed")` | `root.attributes("-zoomed", True)` |
| `winreg` registry checks (CP210x, Arduino IDE) | Replace with Linux path checks / `shutil.which()` |
| `msiexec` installer | Not applicable — remove MSI references |
| Arduino-CLI MSI download/install | Download `.tar.gz` and extract |
| Arduino IDE detection (registry + Program Files) | `shutil.which()` + `/usr/bin` + Snap/Flatpak paths |
| CP210x driver detection (registry) | Kernel module check (`lsmod \| grep cp210x`) |
| `python.exe` / `pythonw.exe` / `Scripts` dir | `python3` / `bin` dir |
| `.ico` icon files | `.png` / `.xbm` icon files (Tk limitation) |
| `ctypes.windll.user32.MessageBoxW` crash fallback | Print to stderr fallback |
| `ensure_platformio_penv_with_hook` import | Remove (Windows-only hook) |
| `_pio_launcher.py` helper | Remove (Windows-only workaround) |
| `DETACHED_PROCESS` / `STARTUPINFO` | Remove (Linux uses `&` / `setsid`) |
| `env/Scripts/python.exe` venv path | `venv/bin/python3` venv path |

---

## Proposed Changes

### 1. bootstrap_linux.py — Full Rewrite from [bootstrap.py](file:///media/naphtali/Naphtali/PERSONAL-PROJECT/ESP%20Flash%20GUI/_MCU_Flash_GUI_V7.5-byNAPH/bootstrap.py)

#### [MODIFY] [bootstrap_linux.py](file:///media/naphtali/Naphtali/PERSONAL-PROJECT/ESP%20Flash%20GUI/_MCU_Flash_GUI_V7.5-byNAPH/bootstrap_linux.py)

Starting from `bootstrap.py` (1907 lines), the following adaptations will be made:

**Remove entirely:**
- `ensure_platformio_penv_with_hook()` (Win32-only hook)
- `_get_safe_platformio_core_dir()` (junction/symlink workaround for spaces in paths)
- `win_subprocess_hide` import and usage
- `sys.stdout.reconfigure(encoding="utf-8")` block
- Windows MSI functions: `_arduino_cli_msi_url()`, `_refresh_bundled_msi()`, `_run_arduino_cli_msi()`
- Arduino IDE check (`check_arduino_ide_installed()` in bootstrap — moved to GUI)
- VBS/embeddable-Python `.pth` file patching in `ensure_pip()`
- `_is_env_healthy()` fast-path (references `env/Scripts/python.exe`)
- `_relaunch_visible_if_hidden()` and venv re-launch logic

**Adapt:**
- `BootstrapGUI` fonts: `Segoe UI` → `Ubuntu` / `DejaVu Sans`, `Consolas` → `Ubuntu Mono` / `monospace`
- `GUI_SCRIPT` → `SCRIPT_DIR / "mcu_flash_gui_linux.py"`
- `find_pio()`: Return `list[str]` like Windows version (for `python -m platformio` style), check `venv/bin/pio` AND `~/.cache/mcu_flash_gui_venv/bin/pio`
- `find_arduino_cli()`: Check `SCRIPT_DIR/bin/`, `~/.local/bin/`, `/usr/local/bin/`, `/usr/bin/`
- `ensure_arduino_cli()`: Download `.tar.gz` from GitHub, extract to `SCRIPT_DIR/bin/`
- `ensure_cp210x()`: Kernel-native on Linux — just log OK
- Add `check_dialout_group()` and `ensure_dialout_group()` (from existing Linux version)
- `ensure_esp32_toolchain()`: Port over from Windows (shared logic, just remove `creationflags`)
- `_spawn_main_gui()`: Simple `subprocess.Popen([sys.executable, str(GUI_SCRIPT)])` without Windows-specific `STARTUPINFO`/`DETACHED_PROCESS`
- `main()`: Keep the GUI bootstrap window (BootstrapGUI) from Windows version, with fonts adapted
- All `creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0` → remove the kwarg entirely

**Preserve from existing [bootstrap_linux.py](file:///media/naphtali/Naphtali/PERSONAL-PROJECT/ESP%20Flash%20GUI/_MCU_Flash_GUI_V7.5-byNAPH/bootstrap_linux.py):**
- `check_dialout_group()` and `ensure_dialout_group()` functions (Linux-specific)

---

### 2. mcu_flash_gui_linux.py — Full Rewrite from [mcu_flash_gui.py](file:///media/naphtali/Naphtali/PERSONAL-PROJECT/ESP%20Flash%20GUI/_MCU_Flash_GUI_V7.5-byNAPH/mcu_flash_gui.py)

#### [MODIFY] [mcu_flash_gui_linux.py](file:///media/naphtali/Naphtali/PERSONAL-PROJECT/ESP%20Flash%20GUI/_MCU_Flash_GUI_V7.5-byNAPH/mcu_flash_gui_linux.py)

Starting from `mcu_flash_gui.py` (6815 lines), the following adaptations:

**Remove entirely:**
- `from bootstrap import ensure_platformio_penv_with_hook` import block
- `win_subprocess_hide` import and install block (lines 28-35)
- `_get_safe_platformio_core_dir()` junction logic (lines 37-55)
- Windows UTF-8 reconfigure block (lines 69-75)
- Windows registry-based `check_arduino_ide_installed()` function (lines 6583-6665)
- Windows DPI awareness block (`windll.shcore.SetProcessDpiAwareness`)
- Windows crash dialog fallback (`ctypes.windll.user32.MessageBoxW`)

**Adapt:**
- `find_pio_executable()`: Simplify — use `python -m platformio` style like Windows, but without Windows paths. Check `sys.executable`, venv `bin/`, `~/.cache/mcu_flash_gui_venv/bin/`, `~/.platformio/penv/bin/`
- `find_arduino_cli_executable()`: Remove Windows `Program Files` paths, add Linux paths (`SCRIPT_DIR/bin/`, `~/.local/bin/`, etc.)
- `ensure_platformio()`: Return `list[str]` (command list) like Windows version, remove `creationflags`
- `PLATFORMIO_CORE_DIR` setup: Simple `SCRIPT_DIR / ".platformio"` or `~/.platformio` (no junction needed)
- All `creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0` → remove
- All `Segoe UI` → `Ubuntu` / `DejaVu Sans`
- All `Consolas` → `Ubuntu Mono` / `monospace`
- `main()`: Remove Windows DPI block, use `-zoomed` for maximize, remove MSI installer flow for Arduino IDE
- `check_arduino_ide_installed()`: Use `shutil.which("arduino-ide")` + `/usr/bin/arduino` + Snap/Flatpak paths
- `.ico` icon → `.png` or `.xbm` icon
- Crash guard fallback: Print to stderr instead of `MessageBoxW`

**Preserve/port from Windows:**
- The full `MCUUploadGUI` class with all its features (the Windows version has ~1555 more lines of features)
- `ProjectSelectorDialog` class
- Board compatibility detection (`detect_board_compatibility`, `_analyze_gpio_compatibility`) — Windows version has enhanced GPIO analysis with per-chip reserved pins
- `detect_chip_on_port()` — Windows version has improved esptool API usage
- All config management (`load_gui_config`, `save_gui_config`, etc.)
- `DEFAULT_UPLOAD_SPEED = 921600` (missing from Linux version)

---

## Open Questions

> [!IMPORTANT]
> **Arduino IDE requirement on Linux:** The Windows version blocks startup if Arduino IDE isn't found (with MSI install option). On Linux, Arduino IDE is commonly installed via Snap/Flatpak/AppImage or not at all. Should the Linux version:
> 1. **Still require it** and just show a warning with install instructions (`sudo snap install arduino`)?  
> 2. **Make it optional** — warn but don't block? (The current Linux version doesn't check at all.)

---

## Verification Plan

### Manual Verification
1. Run `./runThisOnLinux.sh` and confirm the bootstrap GUI window appears (with Ubuntu fonts)
2. Confirm all dependencies are checked/installed correctly  
3. Confirm the main GUI launches after bootstrap completes
4. Confirm serial port listing works (`/dev/ttyUSB*`, `/dev/ttyACM*`)
5. Confirm compile and upload workflows function with a connected ESP32
6. Test the Project Selector dialog (new project creation + existing project selection)
