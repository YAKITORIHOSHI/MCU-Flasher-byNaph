#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — ESP32 Compile, Upload & Serial Monitor
A modern dark-themed GUI tool for Arduino ESP32 development.
"""

# Add src/libs to sys.path so we can import moved utility modules
import sys
from pathlib import Path
_libs_path = Path(__file__).resolve().parent / "src" / "libs"
if str(_libs_path) not in sys.path:
    sys.path.insert(0, str(_libs_path))

import hashlib
import json
import os
import re
import subprocess
import textwrap
import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, font as tkfont
from pathlib import Path
from datetime import datetime
try:
    # pyrefly: ignore [missing-import]
    from bootstrap import ensure_platformio_penv_with_hook
except ImportError:
    def ensure_platformio_penv_with_hook(*args, **kwargs):
        return False
try:
    # pyrefly: ignore [missing-import]
    from bootstrap import find_arduino_cli as _bootstrap_find_arduino_cli
except ImportError:
    _bootstrap_find_arduino_cli = None
try:
    # pyrefly: ignore [missing-import]
    from bootstrap import ensure_arduino_cli as _bootstrap_ensure_arduino_cli
except ImportError:
    _bootstrap_ensure_arduino_cli = None
try:
    # pyrefly: ignore [missing-import]
    from bootstrap import get_last_arduino_cli_error as _bootstrap_get_last_arduino_cli_error
except ImportError:
    _bootstrap_get_last_arduino_cli_error = None

if getattr(sys, 'frozen', False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent

# pyrefly: ignore [missing-import]
try:
    # pyrefly: ignore [missing-import]
    import webview
except Exception:
    # pywebview (and its native backend deps) is only required for the
    # Monaco editor mode. The Default (Tkinter) editor mode works fine
    # without it, so don't hard-fail the whole app if it's missing.
    webview = None

# Unique title used to locate the pywebview-hosted editor's native OS
# window so it can be reparented (embedded) into the Tkinter frame below,
# instead of it floating around as its own separate window. Note: some
# pywebview backends sync the native window's title with the loaded page's
# <title> tag once it finishes loading, which can silently replace this —
# see _find_editor_hwnd() below, which doesn't rely on the title alone.
EDITOR_WINDOW_TITLE = "MCU Flasher — Embedded Code Editor (do not close)"

# Windows-only: lets us reparent the editor's native window into the
# Tkinter frame via the Win32 API. Import is best-effort — if pywin32
# isn't installed, the app still runs fine, it just falls back to the
# old "Open Editor Window" popup behavior instead of true embedding.
win32gui = None
win32con = None
win32process = None
if sys.platform == "win32":
    try:
        import win32gui
        import win32con
        import win32process
    except ImportError:
        pass


def _list_own_toplevel_hwnds() -> set:
    """Enumerate all top-level window handles belonging to this process.
    Used to spot the pywebview editor window by "what showed up" rather
    than by title, since some backends silently rewrite the window title
    to match the loaded page's <title> tag."""
    if win32gui is None or win32process is None:
        return set()
    my_pid = os.getpid()
    hwnds = []

    def _cb(hwnd, _):
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid == my_pid:
                hwnds.append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return set(hwnds)


class EditorApi:
    def __init__(self, gui):
        self._gui = gui
        self.active_file_path = None
        self.modified_files = {} # path -> is_modified

    def get_project_files(self):
        sketch_dir = self._gui.sketch_dir_path
        if not sketch_dir or not sketch_dir.exists():
            return []
        files = []
        for ext in ("*.ino", "*.cpp", "*.c", "*.h", "*.txt"):
            files.extend(sorted(sketch_dir.glob(ext)))

        order_file = sketch_dir / ".mcu_flash_tab_order.json"
        if order_file.exists():
            try:
                import json
                saved_order = json.loads(order_file.read_text(encoding="utf-8"))
                file_map = {}
                for f in files:
                    try:
                        rel = str(f.relative_to(sketch_dir))
                    except Exception:
                        rel = str(f)
                    file_map[rel] = f
                ordered_files = []
                for name in saved_order:
                    if name in file_map:
                        ordered_files.append(file_map.pop(name))
                ordered_files.extend(file_map.values())
                files = ordered_files
            except Exception:
                pass

        return [{"name": f.name, "path": str(f)} for f in files]

    def save_tab_order(self, paths):
        if not self._gui or not self._gui.sketch_dir_path:
            return {"success": False}
        order_file = self._gui.sketch_dir_path / ".mcu_flash_tab_order.json"
        try:
            import json
            normalized_paths = []
            for p in paths:
                try:
                    path_obj = Path(p)
                    if path_obj.is_absolute():
                        normalized_paths.append(str(path_obj.relative_to(self._gui.sketch_dir_path)))
                    else:
                        normalized_paths.append(p)
                except Exception:
                    normalized_paths.append(p)
            order_file.write_text(json.dumps(normalized_paths, indent=2), encoding="utf-8")
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def read_file(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            return {"content": content}
        except Exception as e:
            return {"error": str(e)}

    def save_file(self, path, content):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            # Trigger skip compile check in Tkinter GUI (thread-safe after call)
            if self._gui:
                self._gui.root.after(0, self._gui._update_skip_compile_state)
            return {"success": True}
        except Exception as e:
            return {"error": str(e), "success": False}

    def mark_modified(self, path, is_modified):
        self.modified_files[path] = is_modified
        if self._gui:
            self._gui.root.after(0, self._gui._update_skip_compile_state)

    def set_active_file(self, path):
        self.active_file_path = path

# ─── Suppress console window flashes on Windows ─────────────────────────────
if sys.platform == "win32":
    try:
        # pyrefly: ignore [missing-import]
        import win_subprocess_hide as _wsh
        _wsh.install()
        _wsh.install_venv_site_hook(SCRIPT_DIR)
    except Exception:
        pass

def is_nonfatal_pio_clean_report(text: str) -> bool:
    """True for PlatformIO's non-fatal fs.rmtree retry reports, e.g.

        [WinError 145] The directory is not empty: '...' \n Please manually remove the file `...'

    These appear when a stale build cache can't be fully deleted (a file is
    locked by another process, e.g. antivirus scanning a removable drive).
    PlatformIO retries once and then CONTINUES the build, so these lines must
    never be rendered as fatal errors by the console classifiers."""
    low = text.lower()
    return (
        ("[winerror" in low and "is not empty" in low)
        or "manually remove the file" in low
    )


def robust_rmtree(path, max_attempts: int = 5) -> bool:
    """Delete a file or directory tree as robustly as possible across every
    filesystem a sketch may live on (NTFS, exFAT, FAT32, removable drives):

      1. clears hidden/system/read-only attributes on every entry (Windows),
      2. retries the whole removal with short backoff — transient WinError 145
         ("directory is not empty") on removable/exFAT volumes is normally
         antivirus/indexer locking that clears within a second,
      3. sweeps leftover '*.trash-*' siblings from previous failed removals,
      4. as a last resort renames the tree to a hidden '*.trash-<ts>' sibling —
         rename touches only the directory entry, so it succeeds even when
         children are locked — then removes the renamed copy best-effort.

    Returns True if `path` no longer exists afterwards."""
    import stat as _stat
    target = Path(path)
    if not target.exists() and not target.is_symlink():
        return True

    def _on_rm_error(func, p, exc_info):
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(str(p), 128)  # FILE_ATTRIBUTE_NORMAL
            os.chmod(p, _stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    # 3. Sweep old trash siblings from earlier failed removals.
    try:
        import glob as _glob
        for stale in _glob.glob(str(target.parent / (target.name + ".trash-*"))):
            stale_p = Path(stale)
            if stale_p.is_dir():
                import shutil as _sh
                try:
                    _sh.rmtree(stale_p, onerror=_on_rm_error)
                except Exception:
                    pass
            else:
                try:
                    stale_p.unlink()
                except Exception:
                    pass
    except Exception:
        pass

    for attempt in range(max_attempts):
        if not target.exists():
            return True
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(str(target), 128)
            if target.is_dir():
                import shutil as _sh
                _sh.rmtree(target, onerror=_on_rm_error)
            else:
                try:
                    target.unlink()
                except OSError:
                    os.chmod(str(target), _stat.S_IWRITE)
                    target.unlink()
            return True
        except Exception:
            time.sleep(0.2 * (attempt + 1))

    # 4. Last resort: rename the locked tree aside (rename touches only the
    # directory entry, so it succeeds even when children are still locked).
    try:
        if not target.exists():
            return True
        trash = target.with_name(target.name + f".trash-{int(time.time())}")
        if target.is_dir():
            os.rename(str(target), str(trash))
            import shutil as _sh
            try:
                _sh.rmtree(trash, onerror=_on_rm_error)
            except Exception:
                pass
        else:
            try:
                target.unlink()
            except OSError:
                os.rename(str(target), str(trash))
                trash.unlink(missing_ok=True)
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(trash), 0x02)  # keep Explorer clean
        return not target.exists()
    except Exception:
        return not target.exists()


_volume_info_cache: dict = {}

def get_volume_info(path) -> tuple:
    """Return (filesystem_name, drive_type_label) for the volume containing
    `path`, e.g. ('NTFS', 'Fixed'), ('exFAT', 'Removable'), ('', '').
    Results are cached per volume."""
    try:
        drive = os.path.splitdrive(str(path))[0]
        if not drive:
            return "", ""
        root = drive + os.sep
        cached = _volume_info_cache.get(root)
        if cached is not None:
            return cached
        fs_name, type_label = "", ""
        if sys.platform == "win32":
            import ctypes
            _type_names = {2: "Removable", 3: "Fixed", 4: "Network", 5: "CD/DVD", 6: "RAM"}
            _dt = ctypes.windll.kernel32.GetDriveTypeW(root)
            type_label = _type_names.get(_dt, "")
            _buf = ctypes.create_unicode_buffer(64)
            if ctypes.windll.kernel32.GetVolumeInformationW(
                root, None, 0, None, None, None, _buf, 64
            ):
                fs_name = _buf.value
        _volume_info_cache[root] = (fs_name, type_label)
        return fs_name, type_label
    except Exception:
        return "", ""


def is_ntfs_path(path) -> bool:
    """True if the volume containing `path` is NTFS (the only volume type on
    which Windows hidden-attribute tricks are both reliable and harmless)."""
    fs_name, _ = get_volume_info(path)
    return fs_name.upper() == "NTFS"


_writability_cache: dict = {}

def is_volume_writable(path) -> bool:
    """Probe whether the volume containing `path` accepts writes.  Catches
    USB flash drives with the hardware lock switch engaged, read-only
    mounts, and volumes flagged dirty after an unsafe removal.  Result is
    cached per volume."""
    try:
        drive = os.path.splitdrive(str(path))[0]
        if not drive or drive in _writability_cache:
            return _writability_cache.get(drive, True)
        # Probe inside the nearest existing directory along the path.  Never
        # probe the volume root itself — roots are frequently unwritable for
        # standard users (e.g. C:\) even though the volume works fine.
        probe_dir = os.path.abspath(str(path))
        if not os.path.isdir(probe_dir):
            probe_dir = os.path.dirname(probe_dir)
        volume_root = drive + os.sep
        while probe_dir and not os.path.isdir(probe_dir):
            _parent = os.path.dirname(probe_dir)
            if _parent == probe_dir or os.path.normpath(_parent) == os.path.normpath(volume_root):
                break
            probe_dir = _parent
        if (
            not probe_dir
            or not os.path.isdir(probe_dir)
            or os.path.normpath(probe_dir) == os.path.normpath(volume_root)
        ):
            return True  # no sensible place to probe — don't guess False
        import tempfile as _tf
        _probe = _tf.NamedTemporaryFile(
            prefix=".mcu_fs_probe_", suffix=".tmp", dir=probe_dir, delete=False
        )
        _probe.close()
        os.unlink(_probe.name)
        result = True
    except OSError:
        result = False
    except Exception:
        result = True
    _writability_cache[drive] = result
    return result


def _get_safe_platformio_core_dir(script_dir: Path) -> str:
    local_path = script_dir / "env" / ".platformio"
    local_path_str = str(local_path)
    if sys.platform == "win32":
        junction_path = Path("C:\\") / ".platformio-mcu-gui"
        try:
            local_path.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(str(junction_path)) or junction_path.exists() or junction_path.is_symlink():
                subprocess.run(["cmd", "/c", "rmdir", str(junction_path)], creationflags=subprocess.CREATE_NO_WINDOW)
            res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction_path), local_path_str],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            if res.returncode == 0 or junction_path.exists():
                return str(junction_path)
        except Exception:
            pass
    return local_path_str

os.environ["PLATFORMIO_CORE_DIR"] = _get_safe_platformio_core_dir(SCRIPT_DIR)
os.environ["PYTHONUNBUFFERED"] = "1"

# PlatformIO bootstraps its OWN private virtualenv ("penv") under
# PLATFORMIO_CORE_DIR the first time it runs. That's a completely separate
# interpreter from the one running this GUI (and from the GUI's "env" venv
# hooked above), so every subprocess SCons spawns during compile/upload
# (compilers, esptool, helper python.exe calls) was popping its own console
# window with zero patching. Install the same hook there too. This is a
# no-op (returns False) if penv hasn't been created yet — see the repeat
# call near where PlatformIO is actually invoked, which catches that case.

# Configure stdout/stderr to use UTF-8 encoding on Windows to prevent UnicodeEncodeError
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ─── Serial library (installed by bootstrap.py) ─────────────────
import serial
import serial.tools.list_ports


# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════
DEFAULT_SKETCH_DIR = SCRIPT_DIR
DEFAULT_BAUD = 115200
DEFAULT_UPLOAD_SPEED = 460800

def _get_download_dir() -> str:
    """Read the download directory from the shared settings file.

    arduino_lib_req.py writes the user's chosen download folder to
    ``arduino_browser_settings_linux.json`` next to this script.  This helper
    reads that file so every call-site in this GUI always uses the
    same, up-to-date path — even if the user changed it while the
    Download Manager was open.
    """
    settings_file = SCRIPT_DIR / "arduino_browser_settings_linux.json"
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
            download_dir = settings.get("download_dir", "")
            if download_dir and os.path.isdir(download_dir):
                return download_dir
        except Exception:
            pass
    # Fallback to default download dir
    return os.path.join(os.path.expanduser("~"), "Documents", "_MCUFlasherByNaph_src")


def load_dynamic_boards(default_boards: dict) -> dict:
    """Scan the download directory for boards.txt platform definitions
    and load all downloaded board types dynamically into the GUI."""
    boards = default_boards.copy()
    
    download_dir = _get_download_dir()
        
    boards_path = Path(download_dir) / "Boards"
    if boards_path.is_dir():
        # Scan subfolders for boards.txt
        for p in boards_path.glob("**/boards.txt"):
            parent_name = p.parent.name.lower()
            if "esp32" in parent_name:
                platform = "espressif32"
            elif "esp8266" in parent_name:
                platform = "espressif8266"
            elif "avr" in parent_name or "uno" in parent_name:
                platform = "atmelavr"
            else:
                platform = "espressif32"
                
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                for line in content.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if ".name=" in line:
                        parts = line.split(".name=", 1)
                        if len(parts) == 2:
                            board_id = parts[0].strip()
                            if "." in board_id:
                                continue
                            display_name = parts[1].strip()
                            if display_name and display_name not in boards:
                                pio_board = board_id.lower()
                                if pio_board in ("esp32", "esp32_family"):
                                    pio_board = "esp32dev"
                                elif pio_board == "esp32s3":
                                    pio_board = "esp32-s3-devkitc-1"
                                elif pio_board == "esp32c3":
                                    pio_board = "esp32-c3-devkit-m-1"
                                elif pio_board == "esp32s2":
                                    pio_board = "esp32-s2-kaluga-1"
                                elif pio_board == "esp32c6":
                                    pio_board = "esp32-c6-devkitc-1"
                                elif pio_board == "nodemcu":
                                    pio_board = "nodemcuv2"
                                
                                boards[display_name] = {
                                    "platform": platform,
                                    "board": pio_board,
                                    "framework": "arduino"
                                }
            except Exception:
                pass
    return boards


SUPPORTED_BOARDS = load_dynamic_boards({})

# ─── Canonical chip-feature descriptions ─────────────────────────
# PlatformIO bundles its own esptool build per platform version, and older
# bundled copies print a much shorter "Features:" line (e.g. "WiFi, BLE,
# Embedded PSRAM 8MB (AP_3v3)") than a current standalone esptool CLI does
# ("Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded PSRAM 8MB
# (AP_3v3)"). Rather than depend on whichever wording that bundled version
# happens to use, fill in the well-known hardware description for the
# detected chip family ourselves, and keep only the live-detected memory
# info (PSRAM/flash) from the tool's own output since that part is
# genuinely board-specific.
_CHIP_FEATURE_TEMPLATES = {
    "ESP32-S3": "Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz",
    "ESP32-C6": "Wi-Fi 6, BT 5 (LE), 802.15.4, Dual Core (RISC-V), 160MHz",
    "ESP32-C3": "Wi-Fi, BT 5 (LE), Single Core (RISC-V), 160MHz",
    "ESP32-H2": "BT 5 (LE), 802.15.4, Single Core (RISC-V), 96MHz",
    "ESP32-S2": "Wi-Fi, Single Core, 240MHz",
    "ESP32":    "Wi-Fi, BT/BLE (Classic + LE), Dual Core, 240MHz",
}


def _enrich_chip_features(chip_model: str, raw_features: str) -> str:
    """Swap a terse esptool 'Features:' line for the fuller, canonical
    description of the detected chip family, preserving any live-detected
    PSRAM/flash mention from the tool's own output. Falls back to the raw
    string unchanged if the chip family isn't recognized."""
    if not raw_features or not chip_model:
        return raw_features
    upper_model = chip_model.upper()
    # Check longer/more-specific names first — "ESP32" is a substring of
    # "ESP32-S3", so a naive lookup would misidentify every S-series/C-series
    # chip as plain "ESP32".
    family = next(
        (name for name in sorted(_CHIP_FEATURE_TEMPLATES, key=len, reverse=True)
         if name in upper_model),
        None,
    )
    if not family:
        return raw_features
    template = _CHIP_FEATURE_TEMPLATES[family]
    mem_match = re.search(r'(embedded\s+psram.*)$', raw_features, re.IGNORECASE)
    return f"{template}, {mem_match.group(1).strip()}" if mem_match else template

# ─── USB-serial chip → board-family fingerprinting ──────────────
# Most "ESP32 vs. Arduino" port mismatches come down to which USB-serial
# bridge chip is on the board, and that's visible in the port description
# pyserial reports (e.g. "CH340", "CP210x", "Silicon Labs", "FTDI").
# Mapping each chip to the one-or-few board families it's actually sold
# with lets us catch a wrong-board selection instead of waving through
# any keyword shared across families.
#
#   CH340/CH341      → classic Arduino Uno/Nano/clones (also some ESP8266
#                       boards, but never genuine ESP32 dev modules)
#   CP210x/Silicon Labs → ESP32 dev modules (the standard Espressif bridge)
#   CH9102           → newer ESP32-S2/S3/C3 boards (native USB or WCH bridge)
#   FTDI             → both AVR boards and some ESP8266 boards; ambiguous,
#                       so it's allowed for either family
#   wch.cn / usb serial → generic/ambiguous, allowed for either family
USB_CHIP_BOARD_FAMILIES = {
    # keyword found in port description → (set of platforms it's valid for, human label)
    "ch340":        ({"atmelavr", "espressif8266"}, "CH340 (Arduino/ESP8266-style USB-serial)"),
    "ch341":        ({"atmelavr", "espressif8266"}, "CH341 (Arduino/ESP8266-style USB-serial)"),
    "cp210":        ({"espressif32", "espressif8266"}, "CP210x (Espressif USB-serial)"),
    "silicon labs":({"espressif32", "espressif8266"}, "Silicon Labs CP210x (Espressif USB-serial)"),
    "ch9102":       ({"espressif32"}, "CH9102 (ESP32-S2/S3/C3 USB-serial)"),
    "ftdi":         ({"atmelavr", "espressif8266", "espressif32"}, "FTDI (generic USB-serial)"),
    "wch.cn":       ({"atmelavr", "espressif8266", "espressif32"}, "WCH USB-serial (generic)"),
    "esp32-s3":     ({"espressif32"}, "ESP32-S3 Native USB"),
    "esp32s3":      ({"espressif32"}, "ESP32-S3 Native USB"),
    "jtag":         ({"espressif32"}, "USB JTAG/serial debug unit"),
    "usb bridge":   ({"espressif32"}, "ESP32 USB Bridge"),
}


_PIO_EXECUTABLE_CACHE: list[str] | None = None


def find_pio_executable() -> list[str] | None:
    """
    Locate the platformio command in the local project environment.

    Strategy: always prefer  `python -m platformio`  over the .exe wrapper
    scripts that pip installs (pio.exe / platformio.exe).  Those .exe files
    are thin MSVC-compiled launchers; on machines that lack the exact Visual
    C++ runtime they were built against they throw 0xc0000142
    (STATUS_DLL_INIT_FAILED) and kill the upload silently.  Invoking
    platformio as a Python module bypasses those launchers entirely and works
    on every machine that has Python and platformio installed.

    The result is cached at module level: resolving this costs a
    "python -m platformio --version" subprocess call (1-5+ seconds just to
    import PlatformIO's CLI), and the answer never changes during one run of
    this app. Only a *successful* lookup is cached — a miss stays uncached so
    installing PlatformIO mid-session is still picked up on the next call.
    """
    global _PIO_EXECUTABLE_CACHE
    if _PIO_EXECUTABLE_CACHE is not None:
        return _PIO_EXECUTABLE_CACHE

    def _probe() -> list[str] | None:
        _cf = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

        def _py_has_platformio(py: Path) -> bool:
            try:
                res = subprocess.run(
                    [str(py), "-m", "platformio", "--version"],
                    capture_output=True, timeout=5, creationflags=_cf,
                )
                return res.returncode == 0
            except Exception:
                return False

        # ── 1. Current interpreter first (fastest — already in the venv) ──
        if _py_has_platformio(Path(sys.executable)):
            return [sys.executable, "-m", "platformio"]

        # ── 2. python.exe siblings near our venv Scripts dir ───────────────
        python_dir = Path(sys.executable).parent
        for name in ["python.exe", "python3.exe", "python", "python3"]:
            for d in [python_dir, python_dir.parent / "Scripts", python_dir.parent / "bin"]:
                py_cand = d / name
                if py_cand.exists() and py_cand.resolve() != Path(sys.executable).resolve():
                    if _py_has_platformio(py_cand):
                        return [str(py_cand), "-m", "platformio"]

        # ── 3. PlatformIO's own embedded venv (PLATFORMIO_CORE_DIR/penv) ───
        pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR")
        if not pio_core_dir:
            local_pio = SCRIPT_DIR / "env" / ".platformio"
            if local_pio.exists():
                pio_core_dir = str(local_pio)

        if pio_core_dir:
            pio_core_path = Path(pio_core_dir)
            for scripts_dir in [pio_core_path / "penv" / "Scripts", pio_core_path / "penv" / "bin"]:
                for name in ["python.exe", "python3.exe", "python", "python3"]:
                    py_cand = scripts_dir / name
                    if py_cand.exists():
                        if _py_has_platformio(py_cand):
                            # penv may have just been created (e.g. first-ever
                            # run, bootstrapped moments ago by ensure_platformio).
                            # Make sure the console-hiding hook made it in before
                            # we hand this interpreter back to be used for the
                            # actual compile/upload subprocess.
                            if sys.platform == "win32":
                                try:
                                    # pyrefly: ignore [missing-import]
                                    import win_subprocess_hide as _wsh_inner
                                    _wsh_inner.install_platformio_penv_hook(pio_core_dir)
                                except Exception:
                                    pass
                            return [str(py_cand), "-m", "platformio"]

        return None

    result = _probe()
    if result is not None:
        _PIO_EXECUTABLE_CACHE = result
    return result


def ensure_platformio() -> list[str] | None:
    """Find PlatformIO or auto-install it. Returns the pio command list."""
    pio = find_pio_executable()
    if pio:
        return pio

    # Not found anywhere — try installing via pip
    print("[MCU Flasher] PlatformIO not found — installing via pip...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "platformio"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        print(f"[MCU Flasher] pip install platformio failed: {e}")
        return None

    # Try finding it again after install
    pio = find_pio_executable()
    return pio


def find_arduino_cli_executable() -> str | None:
    """Locate the arduino-cli executable."""
    import shutil
    from pathlib import Path

    # Check cached path file first
    script_dir = SCRIPT_DIR
    cached_file = script_dir / "arduino_cli_path_linux.txt"
    if cached_file.exists():
        try:
            path_str = cached_file.read_text(encoding="utf-8").strip()
            if path_str and os.path.exists(path_str):
                return path_str
        except Exception:
            pass

    cli = shutil.which("arduino-cli")
    if cli:
        return cli

    # Prefer bootstrap's broader search (checks more install dirs and,
    # on Windows, the MSI's uninstall registry entry) so this dialog
    # doesn't pop up just because the MSI landed somewhere non-standard.
    if _bootstrap_find_arduino_cli is not None:
        try:
            cli = _bootstrap_find_arduino_cli()
            if cli:
                try:
                    cached_file.write_text(cli, encoding="utf-8")
                except Exception:
                    pass
                return cli
        except Exception:
            pass

    # Check standard Windows installation directories
    for p in [r"C:\Program Files\Arduino CLI\arduino-cli.exe", r"C:\Program Files (x86)\Arduino CLI\arduino-cli.exe"]:
        if os.path.exists(p):
            return p
    return None



def center_toplevel(toplevel: tk.Toplevel, parent: tk.Tk | tk.Toplevel, width: int, height: int):
    """Center a Toplevel dialog relative to its parent window, or fallback to screen if parent is hidden."""
    parent.update_idletasks() # Ensure parent window geometry is updated
    
    if parent.winfo_viewable() and parent.winfo_ismapped() and parent.winfo_width() > 1:
        parent_w = parent.winfo_width()
        parent_h = parent.winfo_height()
        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        x = parent_x + (parent_w - width) // 2
        y = parent_y + (parent_h - height) // 2
    else:
        screen_w = toplevel.winfo_screenwidth()
        screen_h = toplevel.winfo_screenheight()
        x = (screen_w - width) // 2
        y = (screen_h - height) // 2
        
    toplevel.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")


# ═══════════════════════════════════════════════════════════════
# BOARD COMPATIBILITY DETECTOR
# ═══════════════════════════════════════════════════════════════
def find_board_for_platform(platform: str, variant_hint: str = "") -> str | None:
    """Search SUPPORTED_BOARDS for a board matching *platform*
    (e.g. "espressif32"), optionally preferring one whose PlatformIO
    board id contains *variant_hint* (e.g. "s3" to prefer an S3-specific
    entry over a generic ESP32 one when both exist).

    Replaces the old approach of hardcoding a single guaranteed-to-exist
    display name per platform/variant (e.g. always returning the literal
    string "ESP32-S3 Dev Module"). Now that board entries are populated
    by load_dynamic_boards from whatever boards.txt files are actually
    downloaded, the real display name for "an ESP32-S3 board" varies by
    what's installed and is never something this code can assume in
    advance -- so this searches the live dict by platform/board-id
    instead of returning a literal name.

    Returns None if no board of the requested platform exists at all
    (e.g. nothing has been downloaded yet) -- callers must handle that,
    same as they previously had to handle SUPPORTED_BOARDS lookups
    failing for any other reason.
    """
    variant_hint = variant_hint.lower()
    
    # Prioritize standard "ESP32 Dev Module" or "esp32dev" for plain ESP32 (no variant hint)
    if platform == "espressif32" and not variant_hint:
        for name, info in SUPPORTED_BOARDS.items():
            if info.get("platform") == "espressif32" and name.lower() == "esp32 dev module":
                return name
        for name, info in SUPPORTED_BOARDS.items():
            if info.get("platform") == "espressif32" and info.get("board") == "esp32dev":
                return name

    fallback = None
    for name, info in SUPPORTED_BOARDS.items():
        if info.get("platform") != platform:
            continue
        if variant_hint and variant_hint in info.get("board", "").lower():
            return name
        if fallback is None:
            fallback = name
        # Prefer "Dev Module" entries over generic ones (e.g. "ESP32 Family Device")
        elif "dev module" in name.lower() and "dev module" not in fallback.lower():
            fallback = name
    return fallback


def is_s3_board(p_board: str) -> bool:
    """True when *p_board* (a PlatformIO board id, e.g. from
    board_info["board"]) identifies an ESP32-S3 variant.

    Several compile-flag decisions (native-USB CDC build flags, dio
    flash mode, upload_protocol=esptool) need to apply to "whichever
    downloaded board is an S3", not to one specific hardcoded board id.
    load_dynamic_boards normalizes any boards.txt entry whose .name= key
    starts with esp32s3 to the PlatformIO id "esp32-s3-devkitc-1" -- so
    checking for the substring "s3" in the id (rather than comparing
    against that one exact string) tracks the same real distinction
    while still matching if a differently-packaged S3 board ever
    produces a differently-formatted id containing "s3", instead of
    silently failing to recognize it.
    """
    return "s3" in (p_board or "").lower()


def boards_by_platform(board_names, platforms: set[str]) -> set[str]:
    """Return the subset of *board_names* whose SUPPORTED_BOARDS platform
    is in *platforms*.

    Several rules need "every currently-known board belonging to platform
    X" (e.g. "exclude every ESP32-family board" when an AVR-exclusive
    header is found). The old code spelled that out as a fixed set of
    specific display names -- {"Arduino Uno", "ESP32 Dev Module",
    "ESP32-S3 Dev Module"} -- which only worked because those three
    names were hardcoded and therefore always exactly the boards that
    existed. Now that board entries are disk-discovered, there could be
    zero ESP32 boards downloaded, or several differently-named ones (a
    plain ESP32 dev board AND a separately-named S3 board, say) -- a
    fixed three-name list can't track either case. This looks up each
    name's actual platform in SUPPORTED_BOARDS and filters by that,
    so a rule means what it says ("every espressif32-platform board")
    regardless of how many such boards exist or what they're named.

    Board names not present in SUPPORTED_BOARDS are silently skipped
    rather than raising, since callers pass in sets that may already be
    a subset of SUPPORTED_BOARDS.keys() (e.g. mid-filter `boards`).
    """
    return {
        name for name in board_names
        if SUPPORTED_BOARDS.get(name, {}).get("platform") in platforms
    }


# ─── Board auto-detect via esptool ──────────────────────────────────────────
# Chip name returned by esptool → board family + PlatformIO variant hint,
# resolved against the live SUPPORTED_BOARDS dict via find_board_for_platform
# rather than a fixed display-name string. Only ESP chips are detectable
# this way; AVR boards use avrdude instead.
_ESPTOOL_CHIP_TO_PLATFORM_HINT: dict[str, tuple[str, str]] = {
    "ESP32-S3":   ("espressif32", "s3"),
    "ESP32-S2":   ("espressif32", "s2"),
    "ESP32-C3":   ("espressif32", "c3"),
    "ESP32-C6":   ("espressif32", "c6"),
    "ESP32-H2":   ("espressif32", "h2"),
    "ESP32":      ("espressif32", ""),
    "ESP8266EX":  ("espressif8266", ""),
    "ESP8266":    ("espressif8266", ""),
}


def detect_chip_on_port(port: str) -> tuple[str | None, str | None]:
    """Probe *port* with esptool and return (chip_name, board_display_name).

    Both values are None when detection fails (no ESP chip, port busy, etc.).
    The function must never raise — it is called from UI threads.

    Uses the esptool Python API directly (same approach as
    `_probe_chip_info`) rather than scraping CLI stdout with regexes — the
    CLI's "Detecting chip type..." / "Chip is ..." text has changed across
    esptool versions and is fragile to parse. `esp.CHIP_NAME` is a stable,
    canonical string (e.g. "ESP32-S3", "ESP32-C3", "ESP32", "ESP8266").

    Returns
    -------
    chip_name   : raw chip string from esptool, e.g. "ESP32-C3"
    board_name  : matching SUPPORTED_BOARDS key resolved via
                  find_board_for_platform, e.g. "ESP32 Dev Module" --
                  or None if no board of the matching platform has been
                  downloaded yet (find_board_for_platform found nothing)
    """
    try:
        # pyrefly: ignore [missing-import]
        import esptool

        if not hasattr(esptool, "get_default_connected_device"):
            return None, None

        esp = esptool.get_default_connected_device(
            serial_list=[port],
            port=port,
            connect_attempts=2,
            initial_baud=115200,
        )
        try:
            chip_name = getattr(esp, "CHIP_NAME", None)
        finally:
            try:
                esp._port.close()
            except Exception:
                pass

        if not chip_name:
            return None, None

        chip_name_upper = chip_name.upper()
        board = None
        # Match the longest/most specific key first (e.g. "ESP32-S3"
        # before the bare "ESP32" entry would also match as a substring)
        # so a variant-specific board is preferred whenever one has been
        # downloaded, falling back to the nearest available espressif32
        # board only when no exact variant match exists.
        for chip_key in sorted(_ESPTOOL_CHIP_TO_PLATFORM_HINT, key=len, reverse=True):
            if chip_key in chip_name_upper:
                platform, variant_hint = _ESPTOOL_CHIP_TO_PLATFORM_HINT[chip_key]
                board = find_board_for_platform(platform, variant_hint=variant_hint)
                break

        return chip_name, board
    except Exception:
        pass
def setup_combobox_place_popdown(root: tk.Widget):
    """Override Tcl ::ttk::combobox::PlacePopdown to support opening popdown lists upwards ('above')
    when requested via set_combobox_direction(combo, 'above').
    """
    tcl_override = """
    proc ::ttk::combobox::PlacePopdown {cb popdown} {
        set x [winfo rootx $cb]
        set y [winfo rooty $cb]
        set w [winfo width $cb]
        set h [winfo height $cb]
        set style [$cb cget -style]
        if { $style eq {} } {
          set style TCombobox
        }
        set postoffset [ttk::style lookup $style -postoffset {} {0 0 0 0}]
        foreach var {x y w h} delta $postoffset {
            incr $var $delta
        }

        set H [winfo reqheight $popdown]
        if {[info exists ::combobox_direction($cb)] && $::combobox_direction($cb) eq "above"} {
            set Y [expr {$y - $H}]
        } elseif {$y + $h + $H > [winfo screenheight $popdown]} {
            set Y [expr {$y - $H}]
        } else {
            set Y [expr {$y + $h}]
        }
        wm geometry $popdown ${w}x${H}+${x}+${Y}
    }
    """
    try:
        root.tk.eval(tcl_override)
    except Exception:
        pass


def set_combobox_direction(combo: ttk.Combobox, direction: str = "above"):
    """Specify popdown list orientation for a ttk.Combobox ('above' or 'below')."""
    try:
        combo.tk.call("set", f"::combobox_direction({combo})", direction)
    except Exception:
        pass


def detect_board_compatibility(sketch_dir: Path) -> tuple[set[str], list[str]]:
    """Statically analyse source files and return which of the supported
    boards this sketch is likely compatible with.

    Returns
    -------
    compatible : set of board display-names (subset of SUPPORTED_BOARDS keys)
    reasons    : list of human-readable strings explaining each exclusion
                 (empty when nothing was excluded or detected)
    """
    all_texts: list[str] = []
    for ext in ("*.ino", "*.cpp", "*.c", "*.h"):
        for f in sorted(sketch_dir.glob(ext)):
            try:
                all_texts.append(f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass
    if not all_texts:
        return set(SUPPORTED_BOARDS.keys()), []

    all_code = "\n".join(all_texts)

    # Collect normalised include names
    raw_includes = re.findall(r'#include\s*[<"]([^>"]+)[>"]', all_code, re.IGNORECASE)
    includes = {h.lower() for h in raw_includes}

    # Strip comments before scanning for API calls
    code_nc = re.sub(r'//.*?$', '', all_code, flags=re.MULTILINE)
    code_nc = re.sub(r'/\*.*?\*/', '', code_nc, flags=re.DOTALL)

    boards = set(SUPPORTED_BOARDS.keys())
    exclusions: list[str] = []

    # ── ESP8266-exclusive headers ──────────────────────────────────────────
    ESP8266_ONLY = {
        "esp8266wifi.h", "esp8266webserver.h", "esp8266httpclient.h",
        "esp8266mdns.h", "esp8266netbios.h", "esp8266ping.h",
        "esp8266wifimulti.h", "espsoftwareserial.h",
        "espconn.h", "user_interface.h",
    }
    hit = includes & ESP8266_ONLY
    if hit:
        boards = {b for b in boards if SUPPORTED_BOARDS.get(b, {}).get("platform") == "espressif8266"}
        exclusions.append(
            f"ESP8266-exclusive header(s) detected: {_fmt_hits(hit)} "
            f"→ compatible only with ESP8266 boards"
        )

    # ── ESP32-exclusive headers ────────────────────────────────────────────
    ESP32_ONLY_H = {
        "bledevice.h", "bleclient.h", "bleserver.h", "blescan.h",
        "blesecurity.h", "bleadvertising.h", "bleuuid.h",
        "nimbledevice.h",
        "nimblecharacteristic.h", "nimbleserver.h", "nimblescan.h",
        "nimbleclient.h", "nimblesecurity.h", "nimbleadvertising.h",
        "esp_bt.h", "esp_bt_main.h", "esp_gap_ble_api.h",
        "esp_gatts_api.h", "esp_gatt_common_api.h",
        "driver/ledc.h", "driver/mcpwm.h", "driver/pcnt.h",
        "driver/rmt.h", "driver/pulse_cnt.h",
        "soc/soc.h", "soc/rtc_cntl_reg.h",
        "esp_adc_cal.h", "esp_camera.h",
        "esp32servo.h", "fastaccelstepper.h",
        "wifiprov.h",
        "wifi_provisioning/manager.h",
        "wifi_provisioning/scheme_softap.h",
        "wifi_provisioning/scheme_ble.h",
        "wifi_provisioning/scheme_console.h",
    }
    hit = includes & ESP32_ONLY_H
    if hit:
        boards = {b for b in boards if SUPPORTED_BOARDS.get(b, {}).get("platform") == "espressif32"}
        exclusions.append(
            f"ESP32-exclusive header(s) detected: {_fmt_hits(hit)} "
            f"→ compatible only with ESP32 boards"
        )

    # ── ESP-family headers (ESP32 + ESP8266 only, rules out AVR) ──────────
    ESP_FAMILY = {
        "wifi.h", "wificlient.h", "wificlientsecure.h", "wifiserver.h",
        "wifiudp.h", "wifiap.h", "wifimulti.h", "wifiscan.h",
        "esp_wifi.h", "esp_event.h", "esp_log.h", "esp_system.h",
        "esp_sleep.h", "esp_partition.h", "esp_ota_ops.h",
        "nvs_flash.h", "nvs.h",
        "spiffs.h", "littlefs.h", "esp_spiffs.h", "esp_littlefs.h",
        "preferences.h", "update.h",
        "freertos/freertos.h", "freertos/task.h",
        "lwip/err.h", "lwip/sockets.h", "lwip/sys.h",
        "mbedtls/aes.h", "mbedtls/md.h",
    }
    hit = includes & ESP_FAMILY
    if hit:
        boards = {b for b in boards if SUPPORTED_BOARDS.get(b, {}).get("platform") in {"espressif32", "espressif8266"}}
        exclusions.append(
            f"ESP-family header(s) detected: {_fmt_hits(hit)} "
            f"→ not compatible with Arduino AVR (no WiFi/BT/NVS hardware)"
        )

    # ── ESP32-exclusive API calls ──────────────────────────────────────────
    ESP32_APIS = [
        (r'\bdacWrite\s*\(', "dacWrite()"),
        (r'\bledcSetup\s*\(', "ledcSetup()"),
        (r'\bledcAttachPin\s*\(', "ledcAttachPin()"),
        (r'\bledcWrite\s*\(', "ledcWrite()"),
        (r'\banalogReadMilliVolts\s*\(', "analogReadMilliVolts()"),
        (r'\bhallRead\s*\(', "hallRead()"),
        (r'\btouchRead\s*\(', "touchRead()"),
        (r'\besp_restart\s*\(', "esp_restart()"),
        (r'\bxTaskCreate\s*\(', "xTaskCreate()"),
        (r'\bxTaskCreatePinnedToCore\s*\(', "xTaskCreatePinnedToCore()"),
        (r'\bvTaskDelay\s*\(', "vTaskDelay()"),
        (r'\bpdMS_TO_TICKS\s*\(', "pdMS_TO_TICKS()"),
    ]
    api_hits = [label for pattern, label in ESP32_APIS if re.search(pattern, code_nc)]
    if api_hits:
        boards = {b for b in boards if SUPPORTED_BOARDS.get(b, {}).get("platform") == "espressif32"}
        preview = ", ".join(api_hits[:3]) + ("..." if len(api_hits) > 3 else "")
        exclusions.append(
            f"ESP32-exclusive API call(s): {preview} "
            f"→ compatible only with ESP32 boards"
        )

    # ── Serial1 / Serial2 rules out Uno ───────────────────────────────────
    if re.search(r'\bSerial[12]\b', code_nc):
        boards = {b for b in boards if SUPPORTED_BOARDS.get(b, {}).get("board") != "uno" and SUPPORTED_BOARDS.get(b, {}).get("platform") != "atmelavr"}
        exclusions.append(
            "Uses Serial1 / Serial2 — not compatible with Uno (which only has Serial)"
        )

    # ── AVR-exclusive headers (rules out both ESP boards) ─────────────────
    AVR_ONLY = {
        "avr/pgmspace.h", "avr/io.h", "avr/interrupt.h",
        "avr/wdt.h", "avr/eeprom.h",
    }
    hit = includes & AVR_ONLY
    if hit:
        boards = {b for b in boards if SUPPORTED_BOARDS.get(b, {}).get("platform") == "atmelavr"}
        exclusions.append(
            f"AVR-exclusive header(s) detected: {_fmt_hits(hit)} "
            f"→ compatible only with Arduino AVR"
        )

    # ── GPIO pin-number analysis ──────────────────────────────────────────
    gpio_result = _analyze_gpio_compatibility(sketch_dir)
    excluded_pins_summary = {}  # pins_str -> list of board names
    for board in gpio_result["excluded"]:
        if board in boards:
            boards.discard(board)
            bad = gpio_result["pin_hits"].get(board, [])
            bad_pins = sorted({pin for pin, _ in bad})
            if bad_pins:
                pin_list = ", ".join(str(p) for p in bad_pins[:6])
                if len(bad_pins) > 6:
                    pin_list += f" (+{len(bad_pins)-6} more)"
                excluded_pins_summary.setdefault(pin_list, []).append(board)

    for pins_str, board_names in excluded_pins_summary.items():
        if len(board_names) > 3:
            exclusions.append(
                f"GPIO pin(s) out of range ({pins_str}) for {len(board_names)} boards "
                f"(e.g., {', '.join(sorted(board_names)[:3])}...) → not compatible"
            )
        else:
            for board in board_names:
                exclusions.append(
                    f"GPIO pin(s) out of range for {board}: {pins_str} → not compatible"
                )

    # GPIO reserved-pin warnings (don't exclude, just caution)
    warnings_summary = {}  # (pin, ctx, msg_type) -> list of board names
    for board, pin, ctx, msg_type in gpio_result["warnings"]:
        if board in boards:
            warnings_summary.setdefault((pin, ctx, msg_type), []).append(board)

    for (pin, ctx, msg_type), board_names in warnings_summary.items():
        if len(board_names) > 3:
            exclusions.append(
                f"⚠ GPIO {pin} ({ctx}) is reserved for {msg_type} on {len(board_names)} boards "
                f"(e.g., {', '.join(sorted(board_names)[:3])}...) — may cause instability"
            )
        else:
            for board in board_names:
                exclusions.append(
                    f"⚠ GPIO {pin} ({ctx}) is reserved for {msg_type} on most {board.split()[0]} modules — may cause instability"
                )

    return boards, exclusions


def _fmt_hits(hit_set: set[str], max_show: int = 3) -> str:
    """Format a set of matched header names for display."""
    items = sorted(hit_set)
    shown = items[:max_show]
    rest  = len(items) - max_show
    result = ", ".join(shown)
    if rest > 0:
        result += f" (+{rest} more)"
    return result


def _format_compat_label(boards: set[str]) -> str:
    """Turn the compatible board set into a display string dynamically."""
    if not boards:
        return "Unknown / Incompatible"
        
    def get_short_name(b):
        if b == "Arduino Uno":
            return "Arduino Uno"
        b_upper = b.upper()
        if "ESP32-S3" in b_upper:
            return "ESP32-S3"
        elif "ESP32-C3" in b_upper:
            return "ESP32-C3"
        elif "ESP32-C6" in b_upper:
            return "ESP32-C6"
        elif "ESP32-S2" in b_upper:
            return "ESP32-S2"
        elif "ESP32" in b_upper:
            return "ESP32"
        elif "ESP8266" in b_upper or "NODEMCU" in b_upper:
            return "ESP8266"
        elif "UNO" in b_upper:
            return "Uno"
        return b
        
    ordered = sorted(list({get_short_name(b) for b in boards}))
    if len(ordered) == 1:
        return ordered[0]
    if len(ordered) == 2:
        return f"{ordered[0]} and {ordered[1]}"
    return ", ".join(ordered[:-1]) + f", and {ordered[-1]}"


def _analyze_gpio_compatibility(sketch_dir: Path) -> dict:
    """Scan all source files for GPIO function calls and resolve pin numbers.

    Detects literal integers AND #define / const-int aliases.
    Returns:
        excluded : set of board names ruled out by out-of-range GPIO usage
        warnings : list of (board_name, message) for reserved-pin cautions
        pin_hits : dict board_name -> [(pin_num, context_str), ...]
    """
    GPIO_FUNCS = [
        "pinMode", "digitalWrite", "digitalRead",
        "analogWrite", "analogRead", "analogReadResolution",
        "touchRead", "dacWrite", "ledcAttachPin",
        "pulseIn", "pulseInLong",
        "tone", "noTone",
        "attachInterrupt", "detachInterrupt",
        "shiftIn", "shiftOut",
    ]

    all_texts: list[str] = []
    for ext in ("*.ino", "*.cpp", "*.c", "*.h"):
        for f in sorted(sketch_dir.glob(ext)):
            try:
                all_texts.append(f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass
    if not all_texts:
        return {"excluded": set(), "warnings": [], "pin_hits": {}}

    all_code = "\n".join(all_texts)

    # Strip comments before scanning
    code_nc = re.sub(r'//.*?$', '', all_code, flags=re.MULTILINE)
    code_nc = re.sub(r'/\*.*?\*/', '', code_nc, flags=re.DOTALL)

    # Resolve #define NAME <int> and const int NAME = <int>
    defines: dict[str, int] = {}
    for m in re.finditer(r'#\s*define\s+(\w+)\s+(0x[0-9a-fA-F]+|\d+)\b', all_code):
        try:
            defines[m.group(1)] = int(m.group(2), 0)
        except ValueError:
            pass
    for m in re.finditer(
        r'\bconst\s+(?:int|uint8_t|uint16_t|byte)\s+(\w+)\s*=\s*(0x[0-9a-fA-F]+|\d+)\s*;',
        all_code
    ):
        try:
            defines[m.group(1)] = int(m.group(2), 0)
        except ValueError:
            pass

    # Extract pin literals from GPIO function calls
    func_pat = "|".join(re.escape(fn) for fn in GPIO_FUNCS)
    call_re  = re.compile(
        rf'\b({func_pat})\s*\(\s*([A-Za-z_]\w*|\d+)\s*[,)]',
        re.MULTILINE
    )
    pin_calls: list[tuple[int, str]] = []
    for m in call_re.finditer(code_nc):
        arg = m.group(2)
        pin = int(arg) if arg.isdigit() else defines.get(arg)
        if pin is not None:
            pin_calls.append((pin, f"{m.group(1)}({arg}…)"))

    # Classify each pin against board limits dynamically
    excluded: set[str] = set()
    warnings: list[tuple[str, str]] = []
    pin_hits: dict[str, list] = {name: [] for name in SUPPORTED_BOARDS.keys()}

    seen_reserved: set[tuple[str, int]] = set()   # avoid duplicate warnings

    for pin, ctx in pin_calls:
        for board_name, b_info in SUPPORTED_BOARDS.items():
            platform = b_info.get("platform", "")
            board_id = b_info.get("board", "").lower()
            
            # Determine maximum GPIO pin limits dynamically
            max_pin = 999
            if platform == "atmelavr":
                max_pin = 19
            elif platform == "espressif8266":
                max_pin = 16
            elif platform == "espressif32":
                if "s3" in board_id:
                    max_pin = 48
                elif "c3" in board_id:
                    max_pin = 21
                elif "c6" in board_id:
                    max_pin = 30
                elif "s2" in board_id:
                    max_pin = 46
                else:
                    max_pin = 39

            if pin > max_pin:
                pin_hits[board_name].append((pin, ctx))

            # Determine reserved flash pins
            reserved_pins = set()
            is_s3 = False
            if platform == "espressif32":
                if "s3" in board_id:
                    reserved_pins = {26, 27, 28, 29, 30, 31, 32}
                    is_s3 = True
                else:
                    reserved_pins = {6, 7, 8, 9, 10, 11}
            elif platform in ("espressif32", "espressif8266"):
                reserved_pins = {6, 7, 8, 9, 10, 11}

            if pin in reserved_pins:
                key = (board_name, pin)
                if key not in seen_reserved:
                    seen_reserved.add(key)
                    msg_type = "SPI flash/PSRAM" if is_s3 else "SPI flash"
                    warnings.append((board_name, pin, ctx, msg_type))

    for board, hits in pin_hits.items():
        if hits:
            excluded.add(board)

    return {"excluded": excluded, "warnings": warnings, "pin_hits": pin_hits}



# Known warnings from toolchain/SCons that can be ignored if needed
KNOWN_WARNINGS = []


# ═══════════════════════════════════════════════════════════════
# ANSI ESCAPE HANDLING
# ═══════════════════════════════════════════════════════════════
# Matches CSI sequences (ESC [ ... letter), e.g. \033[2J, \033[H, \033[1;31m,
# plus the bare cursor-home form \033[H with no params. Covers clear-screen,
# cursor movement, and SGR (color) codes — anything a basic terminal emitter
# like Simulation.ino's draw() would send.
ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Sequences that conventionally mean "clear the screen / reset view".
# \033[2J  = erase entire screen
# \033[3J  = erase scrollback (some terminals)
# \033[H   = cursor to home (0,0)
# Sketches commonly send \033[2J\033[H back-to-back (as Simulation.ino does)
# to clear and then home the cursor in one shot. This matches a *run* of one
# or more such codes glued together as a single unit, so a glued pair only
# triggers one clear instead of two.
ANSI_CLEAR_RE = re.compile(r"(?:\x1b\[(?:2J|3J|H))+")


def strip_ansi(text: str) -> str:
    """Remove ANSI/CSI escape sequences from a string, leaving plain text."""
    return ANSI_CSI_RE.sub("", text)



# ═══════════════════════════════════════════════════════════════
# COLOR PALETTE — Dark Cyberpunk / Robotics theme
# ═══════════════════════════════════════════════════════════════
class Theme:
    BG_DARKEST  = "#0a0e14"
    BG_DARK     = "#10151c"
    BG_MID      = "#161d27"
    BG_LIGHT    = "#1c2532"
    BG_HOVER    = "#243040"
    BORDER      = "#2a3545"
    BORDER_LIT  = "#3d5068"

    TEXT        = "#c8d2dc"
    TEXT_DIM    = "#6b7d94"
    TEXT_BRIGHT = "#e8edf3"

    CYAN        = "#39c5bb"
    CYAN_DIM    = "#1f7872"
    GREEN       = "#5ccc6e"
    GREEN_DIM   = "#2d6636"
    YELLOW      = "#e8b83a"
    YELLOW_DIM  = "#7a6020"
    RED         = "#f05050"
    RED_DIM     = "#7a2828"
    MAGENTA     = "#c678dd"
    PURPLE      = "#b388ff"
    PURPLE_DIM  = "#9d7cc4"
    BLUE        = "#61afef"
    ORANGE      = "#d19a66"

    BTN_COMPILE   = "#2d7d46"
    BTN_COMPILE_H = "#38a058"
    BTN_UPLOAD    = "#8244a0"
    BTN_UPLOAD_H  = "#a05cc0"
    BTN_FULL      = "#2077b0"
    BTN_FULL_H    = "#2899dd"
    BTN_MONITOR   = "#1a7a70"
    BTN_MONITOR_H = "#22a090"
    BTN_STOP      = "#a03030"
    BTN_STOP_H    = "#cc4444"
    BTN_CLEAR     = "#3a4555"
    BTN_CLEAR_H   = "#4a5a70"


GUI_CONFIG_FILE = Path.home() / ".mcu_gui_config_linux.json"

# Resolved at startup in main() before MCUUploadGUI is constructed. Lets the
# crash-safety revert logic (Monaco -> Default) apply even though the config
# file itself still says "monaco" until the user is warned/confirms again.
_RESOLVED_EDITOR_MODE = None

# ── Per-instance config key ─────────────────────────────────────────────────
# Each GUI window (process) gets a unique instance ID so two windows launched
# at the same time don't read/write each other's "last_sketch_dir" in the
# shared config file.  The config JSON becomes:
#   { "instances": { "<pid>": { "last_sketch_dir": "..." }, ... },
#     "shared": { ... }   ← reserved for truly-shared settings later }
# Old single-key format is migrated automatically on first load.
import os as _os
_INSTANCE_ID = str(_os.getpid())
del _os


def _load_raw_config() -> dict:
    if GUI_CONFIG_FILE.exists():
        try:
            return json.loads(GUI_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_raw_config(data: dict):
    try:
        GUI_CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _own_create_time():
    """This process's start time (seconds since epoch), or None if psutil
    is unavailable. Stashed in an instance's config entry at registration
    so later liveness checks can tell 'still the same process' apart from
    'a different process now sitting on the same recycled PID'."""
    try:
        import psutil as _psutil_check
        return _psutil_check.Process().create_time()
    except Exception:
        return None


def _get_alive_pid_create_times() -> "dict[str, float] | None":
    """Return {pid_str: create_time} for every process currently running,
    or None if psutil isn't available. Used instead of a bare PID-membership
    check: Windows recycles PIDs quickly, so a lock/registration entry left
    behind by a crashed or force-killed instance can otherwise be mistaken
    for still-alive just because some unrelated process later reused its PID."""
    try:
        import psutil as _psutil_check
        return {
            str(p.pid): p.info.get("create_time")
            for p in _psutil_check.process_iter(["create_time"])
        }
    except Exception:
        return None


def _instance_is_alive(pid: str, inst: dict, alive: "dict[str, float] | None") -> bool:
    """True if `inst` (an entry from the instances config) still belongs to
    a live MCU Flasher process. Requires both a matching PID AND a matching
    create_time, so a dead instance's stale entry can't be revived just
    because its old PID number got handed to a different process."""
    if alive is None:
        return True  # psutil unavailable — assume alive rather than falsely evict
    if pid not in alive:
        return False
    stored_ct = inst.get("create_time")
    proc_ct = alive.get(pid)
    if proc_ct is None:
        return True  # couldn't read the live process's create_time (e.g. permissions) —
                      # can't disprove it, so don't evict on uncertain grounds
    if stored_ct is None:
        # This entry predates create_time tracking, meaning it was written
        # by a build before this fix (or by a process that crashed before
        # a create_time could ever be recorded). There's no way to verify
        # it's really the same process that once owned this PID, so don't
        # give it the benefit of the doubt — treat it as stale.
        return False
    return abs(proc_ct - stored_ct) < 2.0


def get_editor_mode() -> str:
    """Return the persisted editor mode preference: 'default' or 'monaco'."""
    data = _load_raw_config()
    return data.get("shared", {}).get("editor_mode", "default")


def set_editor_mode(mode: str):
    data = _load_raw_config()
    data.setdefault("shared", {})["editor_mode"] = mode
    _save_raw_config(data)


def get_monaco_boot_pending() -> bool:
    """True if a previous Monaco launch never confirmed it started cleanly
    (i.e. the process crashed before it could clear the sentinel)."""
    data = _load_raw_config()
    return bool(data.get("shared", {}).get("monaco_boot_pending", False))


def set_monaco_boot_pending(pending: bool):
    data = _load_raw_config()
    data.setdefault("shared", {})["monaco_boot_pending"] = bool(pending)
    _save_raw_config(data)


def get_remembered_board_for_port(port_device: str) -> str:
    """Return the board type last successfully used with this physical port
    (e.g. 'COM16' -> 'ESP32 Dev Module'), or '' if nothing is on record.

    Stored in the 'shared' section (not per-instance) since a COM port is a
    physical machine-wide device — the same USB cable plugged into the same
    port should be remembered the same way regardless of which project/GUI
    instance is asking."""
    if not port_device:
        return ""
    data = _load_raw_config()
    return data.get("shared", {}).get("port_board_map", {}).get(port_device.upper(), "")


def remember_port_board(port_device: str, board_name: str):
    """Persist the (port -> board) association so the next time this exact
    physical port is attached — even after closing the app or opening a
    different project — the same board type is auto-selected instantly."""
    if not port_device or not board_name:
        return
    try:
        data = _load_raw_config()
        data.setdefault("shared", {}).setdefault("port_board_map", {})[port_device.upper()] = board_name
        _save_raw_config(data)
    except Exception:
        pass


def load_gui_config() -> dict:
    """Return this instance's config dict (creates it if absent)."""
    data = _load_raw_config()
    # Migrate old flat format {"last_sketch_dir": "..."} → new nested format
    if "instances" not in data:
        old_dir = data.get("last_sketch_dir", "")
        data = {"instances": {}, "shared": {}}
        if old_dir:
            data["instances"][_INSTANCE_ID] = {"last_sketch_dir": old_dir}
            data["shared"] = {"last_sketch_dir": old_dir}
        _save_raw_config(data)
    
    # Initialize the current PID's config block using fallback from shared or other active configs
    if _INSTANCE_ID not in data.get("instances", {}):
        if "instances" not in data:
            data["instances"] = {}
        fallback_dir = ""
        if "shared" in data and "last_sketch_dir" in data["shared"]:
            fallback_dir = data["shared"]["last_sketch_dir"]
        else:
            for inst in data.get("instances", {}).values():
                if inst.get("last_sketch_dir"):
                    fallback_dir = inst["last_sketch_dir"]
                    break
        data["instances"][_INSTANCE_ID] = {"last_sketch_dir": fallback_dir}
        _save_raw_config(data)

    # Backfill create_time if this entry doesn't have one yet (covers both
    # the freshly-created case above and instances migrated from the old
    # flat format, which predate create_time tracking).
    inst = data["instances"].get(_INSTANCE_ID)
    if inst is not None and "create_time" not in inst:
        inst["create_time"] = _own_create_time()
        _save_raw_config(data)

    return data["instances"].get(_INSTANCE_ID, {})


def save_gui_config(config: dict):
    """Persist this instance's config dict without touching other instances."""
    data = _load_raw_config()
    if "instances" not in data:
        data = {"instances": {}, "shared": {}}
    data["instances"][_INSTANCE_ID] = config
    
    # Also update the shared config so new instances can inherit it
    if "shared" not in data:
        data["shared"] = {}
    if "last_sketch_dir" in config:
        data["shared"]["last_sketch_dir"] = config["last_sketch_dir"]
        
    # Prune stale instance entries (processes that no longer exist, or whose
    # PID has since been recycled by an unrelated process)
    alive = _get_alive_pid_create_times()
    if alive is not None:
        data["instances"] = {
            k: v for k, v in data["instances"].items()
            if k == _INSTANCE_ID or _instance_is_alive(k, v, alive)
        }
    _save_raw_config(data)


def load_recent_projects() -> list[str]:
    """Load and return the list of recently opened project paths (up to 5),
    automatically filtering out folders that no longer exist on disk."""
    data = _load_raw_config()
    recent = data.get("shared", {}).get("recent_projects", [])
    valid_recent = []
    changed = False
    for p in recent:
        try:
            if Path(p).is_dir():
                valid_recent.append(p)
            else:
                changed = True
        except Exception:
            changed = True
    if changed:
        if "shared" not in data:
            data["shared"] = {}
        data["shared"]["recent_projects"] = valid_recent
        _save_raw_config(data)
    return valid_recent


def add_recent_project(path: str):
    """Add a project folder path to the recent list (max 5 folders),
    bumping it to the top of the list if it already exists."""
    data = _load_raw_config()
    if "shared" not in data:
        data["shared"] = {}
    recent = data["shared"].get("recent_projects", [])
    try:
        path = str(Path(path).resolve())
    except Exception:
        path = str(path)
    if path in recent:
        recent.remove(path)
    recent.insert(0, path)
    recent = recent[:5]
    data["shared"]["recent_projects"] = recent
    _save_raw_config(data)


def get_occupied_ports() -> set[str]:
    """Retrieve set of COM port devices currently selected by other active instances."""
    data = _load_raw_config()
    occupied = set()
    
    # Get currently alive PIDs (with create_time) to filter out stale instances
    alive = _get_alive_pid_create_times()

    instances = data.get("instances", {})
    for pid, inst in instances.items():
        if pid == _INSTANCE_ID:
            continue
        if not _instance_is_alive(pid, inst, alive):
            continue
        port = inst.get("selected_port")
        if port:
            occupied.add(port)
    return occupied


def get_occupied_folders() -> dict[str, str]:
    """Retrieve project folders currently active in other live instances.

    Returns {resolved_folder_path: owner_pid}, mirroring get_occupied_ports()
    so the same "another window already has this" pattern covers both the
    COM port and the sketch-folder resource.
    """
    data = _load_raw_config()
    occupied: dict[str, str] = {}

    alive = _get_alive_pid_create_times()

    instances = data.get("instances", {})
    for pid, inst in instances.items():
        if pid == _INSTANCE_ID:
            continue
        if not _instance_is_alive(pid, inst, alive):
            continue
        folder = inst.get("active_sketch_dir")
        if folder:
            try:
                resolved = str(Path(folder).resolve())
            except Exception:
                resolved = folder
            occupied[resolved] = pid
    return occupied


def folder_lock_owner(path) -> str | None:
    """Return the owning PID (as a string) if `path` is locked by another
    live instance, else None. Accepts a str or Path; resolves before
    comparing so trailing slashes / '.' segments / case-on-Windows don't
    cause false negatives."""
    try:
        resolved = str(Path(path).resolve())
    except Exception:
        resolved = str(path)
    occupied = get_occupied_folders()
    # Path.resolve() is already case-normalized on Windows, but compare
    # case-insensitively there too in case one side couldn't resolve.
    if sys.platform == "win32":
        resolved_l = resolved.lower()
        for folder, pid in occupied.items():
            if folder.lower() == resolved_l:
                return pid
        return None
    return occupied.get(resolved)

# ═══════════════════════════════════════════════════════════════
# STARTUP PROJECT SELECTOR
# ═══════════════════════════════════════════════════════════════
# Shown before the main window: choose an existing project folder, or
# scaffold a brand-new one (.ino + optional .h/.cpp pair, wired together
# with #pragma once / #include automatically).
_VALID_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _make_dialog_btn(parent, text, command, bg, bg_hover, font, width=None) -> tk.Button:
    """Standalone flat-button factory (selector window runs before
    MCUUploadGUI exists, so it can't reuse the instance-bound helpers)."""
    btn = tk.Button(
        parent, text=text, command=command,
        font=font, fg=Theme.TEXT_BRIGHT, bg=bg,
        activebackground=bg_hover, activeforeground=Theme.TEXT_BRIGHT,
        relief=tk.FLAT, borderwidth=0, padx=14, pady=6, cursor="hand2",
    )
    if width:
        btn.configure(width=width)
    btn.bind("<Enter>", lambda e, b=btn, c=bg_hover: b.configure(bg=c))
    btn.bind("<Leave>", lambda e, b=btn, c=bg: b.configure(bg=c))
    return btn


def _scaffold_new_project(dest_parent: Path, project_name: str,
                           include_h: bool, include_cpp: bool) -> Path:
    """Create <dest_parent>/<project_name>/ with the main .ino and the
    optional .h / .cpp pair, fully wired with #pragma once / #include.
    Returns the new project folder path. Raises on failure (caller catches)."""
    project_dir = dest_parent / project_name
    project_dir.mkdir(parents=True, exist_ok=False)

    ino_includes = ""
    if include_h:
        ino_includes += f'#include "{project_name}.h"\n'

    ino_content = (
        f"{ino_includes}\n"
        f"void setup() {{\n"
        f"  \n"
        f"}}\n\n"
        f"void loop() {{\n"
        f"  \n"
        f"}}\n"
    )
    (project_dir / f"{project_name}.ino").write_text(ino_content, encoding="utf-8")

    if include_h:
        h_content = "#pragma once\n\n"
        (project_dir / f"{project_name}.h").write_text(h_content, encoding="utf-8")

    if include_cpp:
        cpp_includes = f'#include "{project_name}.h"\n\n' if include_h else ""
        (project_dir / f"{project_name}.cpp").write_text(cpp_includes, encoding="utf-8")

    return project_dir


class ProjectSelectorDialog:
    """Modal startup window: pick an existing project folder, or build a
    new one from scratch. Call `.run()`; returns a Path or None (cancelled)."""

    def __init__(self, root: tk.Tk, initial_dir: str = ""):
        self.root = root
        self.result: Path | None = None
        self.initial_dir = initial_dir

        self.win = tk.Toplevel(root)
        self.win.title("MCU Flasher by Naph — Select Project")
        self.win.resizable(False, False)

        # Center the window on the screen
        try:
            screen_w = self.win.winfo_screenwidth()
            screen_h = self.win.winfo_screenheight()
        except Exception:
            screen_w, screen_h = 1920, 1080

        if screen_w < 1400 or screen_h < 800:
            width = 640
            height = 580
            self.win.minsize(640, 580)
        else:
            width = 660
            height = 700
            self.win.minsize(660, 700)

        x = (screen_w - width) // 2
        y = ((screen_h - height) // 2) - 30
        self.win.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        self.win.configure(bg=Theme.BG_DARKEST)
        self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)
        
        # Force window to the front and focus
        self.win.lift()
        self.win.focus_force()
        self.win.attributes("-topmost", True)
        self.win.after(500, self._unset_selector_topmost)

    def _unset_selector_topmost(self):
        try:
            if hasattr(self, "win") and self.win:
                self.win.attributes("-topmost", False)
        except Exception:
            pass

        f_title = tkfont.Font(family="Montserrat", size=15, weight="bold")
        f_sub = tkfont.Font(family="Montserrat", size=9)
        f_label = tkfont.Font(family="Montserrat", size=10)
        f_btn = tkfont.Font(family="Montserrat", size=10, weight="bold")
        f_mono = tkfont.Font(family="Consolas", size=9)
        self._fonts = (f_title, f_sub, f_label, f_btn, f_mono)

        tk.Label(self.win, text="⚡ MCU Flasher by Naph", font=f_title,
                 fg=Theme.CYAN, bg=Theme.BG_DARKEST).pack(pady=(18, 0))
        tk.Label(self.win, text="Open an existing project, or create a new one",
                 font=f_sub, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(pady=(2, 14))

        # ── Mode tabs (Existing / New) ───────────────────────────────
        tab_bar = tk.Frame(self.win, bg=Theme.BG_DARKEST)
        tab_bar.pack(fill=tk.X, padx=24)

        self.btn_tab_existing = _make_dialog_btn(
            tab_bar, "📂 Existing Project", lambda: self._switch_tab("existing"),
            Theme.BTN_FULL, Theme.BTN_FULL_H, f_btn)
        self.btn_tab_existing.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))

        self.btn_tab_new = _make_dialog_btn(
            tab_bar, "✨ New Project", lambda: self._switch_tab("new"),
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, f_btn)
        self.btn_tab_new.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(4, 0))

        tk.Frame(self.win, bg=Theme.BORDER, height=2).pack(fill=tk.X, padx=24, pady=(12, 0))

        # ── Body container (swapped per tab) ─────────────────────────
        self.body = tk.Frame(self.win, bg=Theme.BG_DARKEST)
        self.body.pack(fill=tk.BOTH, expand=True, padx=24, pady=16)

        self._build_existing_tab()
        self._build_new_tab()
        self._switch_tab("existing")

        self.win.bind("<Escape>", lambda e: self._on_cancel())

        # Re-apply the centered position once the window is fully built and
        # mapped. self.root (the dialog's parent) is withdrawn at this point
        # in the startup flow, and Windows frequently ignores the earlier
        # geometry request once the WM actually places a freshly-mapped
        # Toplevel whose parent has no on-screen position of its own --
        # the dialog would land near a screen edge instead of centered.
        # Re-issuing the same centered coordinates after update_idletasks()
        # (now that real content exists and the window has been mapped)
        # makes the placement stick.
        self.win.update_idletasks()
        try:
            screen_w = self.win.winfo_screenwidth()
            screen_h = self.win.winfo_screenheight()
        except Exception:
            screen_w, screen_h = 1920, 1080
        if screen_w < 1400 or screen_h < 800:
            width, height = 640, 580
        else:
            width, height = 660, 700
        x = (screen_w - width) // 2
        y = (screen_h - height) // 2
        self.win.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.win.lift()
        self.win.focus_force()
        self.win.grab_set()

    # ── Tab switching ────────────────────────────────────────────────────
    def _switch_tab(self, which: str):
        self.mode = which
        if which == "existing":
            self.btn_tab_existing.configure(bg=Theme.BTN_FULL)
            self.btn_tab_new.configure(bg=Theme.BTN_CLEAR)
            self.frame_new.pack_forget()
            self.frame_existing.pack(fill=tk.BOTH, expand=True)
        else:
            self.btn_tab_new.configure(bg=Theme.BTN_FULL)
            self.btn_tab_existing.configure(bg=Theme.BTN_CLEAR)
            self.frame_existing.pack_forget()
            self.frame_new.pack(fill=tk.BOTH, expand=True)

    def _get_project_files(self, folder_path: Path | str) -> list[str]:
        """Scan a project directory for .cpp, .ino, .h, .hpp, .txt files."""
        valid_exts = {".cpp", ".ino", ".h", ".hpp", ".txt"}
        found_files: list[tuple[int, str]] = []
        try:
            p = Path(folder_path)
            if not p.exists() or not p.is_dir():
                return []
            
            dirs_to_check = [p]
            src_dir = p / "src"
            if src_dir.exists() and src_dir.is_dir():
                dirs_to_check.append(src_dir)

            seen = set()
            for d in dirs_to_check:
                try:
                    for item in d.iterdir():
                        if item.is_file():
                            ext = item.suffix.lower()
                            if ext in valid_exts:
                                rel_path = item.name if d == p else f"src/{item.name}"
                                if rel_path not in seen:
                                    seen.add(rel_path)
                                    prio = 0 if ext == ".ino" else (1 if ext in (".h", ".hpp") else (2 if ext == ".cpp" else 3))
                                    found_files.append((prio, rel_path))
                except Exception:
                    pass
        except Exception:
            pass
        
        found_files.sort(key=lambda x: (x[0], x[1].lower()))
        return [name for _, name in found_files]

    # ── Existing-project tab ────────────────────────────────────────────
    def _build_existing_tab(self):
        f_title, f_sub, f_label, f_btn, f_mono = self._fonts
        self.frame_existing = tk.Frame(self.body, bg=Theme.BG_DARKEST)

        # Action Buttons row packed at the bottom FIRST so it is always visible and never cut off
        btn_row = tk.Frame(self.frame_existing, bg=Theme.BG_DARKEST)
        btn_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        _make_dialog_btn(btn_row, "Cancel", self._on_cancel,
                          Theme.BTN_STOP, Theme.BTN_STOP_H, f_btn).pack(side=tk.RIGHT, padx=(8, 0))
        _make_dialog_btn(btn_row, "Open Project ▶", self._on_open_existing,
                          Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, f_btn).pack(side=tk.RIGHT)

        tk.Label(self.frame_existing,
                 text="Pick a folder that already contains your sketch\n"
                      "(.ino / .cpp / .h / .txt files and, optionally, platformio.ini).",
                 font=f_label, fg=Theme.TEXT, bg=Theme.BG_DARKEST,
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(8, 12))

        self.existing_path_var = tk.StringVar(value=self.initial_dir)
        path_row = tk.Frame(self.frame_existing, bg=Theme.BG_DARKEST)
        path_row.pack(fill=tk.X, pady=(0, 8))

        entry = tk.Entry(path_row, textvariable=self.existing_path_var,
                          font=f_mono, bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
                          insertbackground=Theme.CYAN, borderwidth=0,
                          highlightthickness=1, highlightcolor=Theme.CYAN_DIM,
                          highlightbackground=Theme.BORDER)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), ipady=4)

        browse_btn = _make_dialog_btn(path_row, "Browse…", self._browse_existing,
                                       Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, f_label)
        browse_btn.pack(side=tk.LEFT)

        # Live Preview of files inside selected project folder
        preview_frame = tk.Frame(self.frame_existing, bg=Theme.BG_DARK, padx=10, pady=6,
                                 highlightthickness=1, highlightbackground=Theme.BORDER)
        preview_frame.pack(fill=tk.X, pady=(0, 12))

        tk.Label(preview_frame, text="Folder Contents (.cpp / .ino / .h / .txt):",
                 font=f_sub, fg=Theme.CYAN, bg=Theme.BG_DARK, anchor=tk.W).pack(anchor=tk.W)

        self.existing_contents_lbl = tk.Label(
            preview_frame, text="Select a folder to view files...", font=f_mono,
            fg=Theme.TEXT_BRIGHT, bg=Theme.BG_DARK, anchor=tk.W, justify=tk.LEFT, wraplength=580
        )
        self.existing_contents_lbl.pack(anchor=tk.W, pady=(2, 0))

        def _update_existing_contents(*args):
            raw = self.existing_path_var.get().strip()
            if not raw:
                self.existing_contents_lbl.config(text="No folder selected", fg=Theme.TEXT_DIM)
                return
            p = Path(raw)
            if not p.exists() or not p.is_dir():
                self.existing_contents_lbl.config(text="⚠️ Folder does not exist", fg=Theme.RED)
                return
            files = self._get_project_files(p)
            if files:
                file_str = "  •  ".join(files[:10])
                if len(files) > 10:
                    file_str += f"   (+{len(files) - 10} more)"
                self.existing_contents_lbl.config(text=f"📄 {file_str}", fg=Theme.GREEN)
            else:
                self.existing_contents_lbl.config(text="⚠️ No .cpp, .ino, .h, or .txt files found in this folder", fg=Theme.YELLOW)

        self.existing_path_var.trace_add("write", _update_existing_contents)
        _update_existing_contents()

        # Recent Projects list
        recent_list = load_recent_projects()
        if self.initial_dir:
            try:
                curr_resolved = str(Path(self.initial_dir).resolve())
                recent_list = [p for p in recent_list if str(Path(p).resolve()) != curr_resolved]
            except Exception:
                recent_list = [p for p in recent_list if p != self.initial_dir]

        if recent_list:
            tk.Label(
                self.frame_existing, text="Recent Projects (double-click to open):", font=f_label,
                fg=Theme.CYAN, bg=Theme.BG_DARKEST, anchor=tk.W
            ).pack(anchor=tk.W, pady=(8, 4))
            
            recent_frame = tk.Frame(self.frame_existing, bg=Theme.BG_DARKEST)
            recent_frame.pack(fill=tk.BOTH, expand=True)
            
            for path in recent_list:
                p_path = Path(path)
                owner_pid = folder_lock_owner(p_path)
                is_locked = owner_pid is not None
                files = self._get_project_files(p_path)
                if files:
                    files_str = "📄 " + "  •  ".join(files[:6]) + (f" (+{len(files)-6} more)" if len(files) > 6 else "")
                    files_color = Theme.CYAN_DIM
                else:
                    files_str = "📄 (No .cpp / .ino / .h / .txt files found)"
                    files_color = Theme.TEXT_DIM

                if is_locked:
                    btn_text = f" 🔒 {p_path.name}  —  {path}   (in use — PID {owner_pid})"
                    fg_color = Theme.RED
                    bg_idle = Theme.BG_DARK
                    bg_hover = Theme.BG_DARK  # no hover highlight; it's not openable
                    cursor = "no" if sys.platform == "win32" else "X_cursor"
                else:
                    btn_text = f" 📁 {p_path.name}  —  {path}"
                    fg_color = Theme.TEXT_BRIGHT
                    bg_idle = Theme.BG_DARK
                    bg_hover = Theme.BG_HOVER
                    cursor = "hand2"

                lbl = tk.Label(
                    recent_frame, text=btn_text, font=f_mono,
                    fg=fg_color, bg=bg_idle, anchor=tk.W,
                    padx=10, pady=6, cursor=cursor
                )
                lbl.pack(fill=tk.X, pady=2)

                def _make_handlers(w=lbl, p=path, bg_h=bg_hover, bg_i=bg_idle):
                    w.bind("<Enter>", lambda e: w.configure(bg=bg_h))
                    w.bind("<Leave>", lambda e: w.configure(bg=bg_i))
                    w.bind("<Button-1>", lambda e: self.existing_path_var.set(p))
                    # Double-click always routes through _on_open_existing(), which
                    # re-checks the lock and shows "Project In Use" if it's still
                    # held elsewhere — so a locked row can't be force-opened here.
                    w.bind("<Double-Button-1>", lambda e: (self.existing_path_var.set(p), self._on_open_existing()))
                _make_handlers()
        else:
            # Fallback spacer
            tk.Frame(self.frame_existing, bg=Theme.BG_DARKEST).pack(fill=tk.BOTH, expand=True)

    def _browse_existing(self):
        from tkinter import filedialog
        init_dir = self.existing_path_var.get().strip() or str(Path.home())
        if init_dir:
            try:
                p_init = Path(init_dir)
                if p_init.is_file():
                    init_dir = str(p_init.parent)
            except Exception:
                pass

        selected = filedialog.askopenfilename(
            initialdir=init_dir,
            title="Select Existing Sketch / Project File (.ino, .cpp, .h, .txt)",
            parent=self.win,
            filetypes=[
                ("Project Files & Sketches (*.ino, *.cpp, *.h, *.txt)", "*.ino;*.cpp;*.h;*.hpp;*.txt;platformio.ini"),
                ("Arduino Sketches (*.ino)", "*.ino"),
                ("C/C++ Source & Headers (*.cpp, *.h)", "*.cpp;*.h;*.hpp"),
                ("Text Files (*.txt)", "*.txt"),
                ("All Files (*.*)", "*.*")
            ]
        )
        if selected:
            p = Path(selected)
            folder = p.parent if p.is_file() else p
            self.existing_path_var.set(str(folder))

    def _on_open_existing(self):
        import tkinter.messagebox as mb
        raw = self.existing_path_var.get().strip()
        if not raw:
            mb.showwarning("No Folder Selected", "Please choose a project folder first.", parent=self.win)
            return
        p = Path(raw)
        if not p.exists() or not p.is_dir():
            mb.showerror("Invalid Folder", f"This folder does not exist:\n{p}", parent=self.win)
            return
        owner_pid = folder_lock_owner(p)
        if owner_pid is not None:
            mb.showerror(
                "Project In Use",
                f"This project folder is already open in another MCU Flasher window "
                f"(PID {owner_pid}):\n{p}\n\n"
                "Close that window first, or choose a different project.",
                parent=self.win,
            )
            return
        self.result = p
        self.win.destroy()

    # ── New-project tab ─────────────────────────────────────────────────
    def _build_new_tab(self):
        f_title, f_sub, f_label, f_btn, f_mono = self._fonts
        self.frame_new = tk.Frame(self.body, bg=Theme.BG_DARKEST)

        # Action Buttons row packed at the bottom FIRST so it is always visible and never cut off
        btn_row = tk.Frame(self.frame_new, bg=Theme.BG_DARKEST)
        btn_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        _make_dialog_btn(btn_row, "Cancel", self._on_cancel,
                          Theme.BTN_STOP, Theme.BTN_STOP_H, f_btn).pack(side=tk.RIGHT, padx=(8, 0))
        _make_dialog_btn(btn_row, "Create Project ▶", self._on_create_new,
                          Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, f_btn).pack(side=tk.RIGHT)

        tk.Label(self.frame_new, text="Files to include", font=f_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(anchor=tk.W)

        files_row = tk.Frame(self.frame_new, bg=Theme.BG_DARKEST)
        files_row.pack(fill=tk.X, pady=(6, 16))

        # .ino — always included, shown as a disabled/checked indicator
        ino_chip = tk.Frame(files_row, bg=Theme.BG_LIGHT, padx=10, pady=6)
        ino_chip.pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(ino_chip, text="✔ <name>.ino", font=f_label,
                 fg=Theme.GREEN, bg=Theme.BG_LIGHT).pack()
        tk.Label(files_row, text="(always created)", font=f_sub,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(side=tk.LEFT)

        check_row = tk.Frame(self.frame_new, bg=Theme.BG_DARKEST)
        check_row.pack(fill=tk.X, pady=(0, 4))

        self.var_include_h = tk.BooleanVar(value=False)
        self.var_include_cpp = tk.BooleanVar(value=False)

        def _styled_check(parent, text, var):
            cb = tk.Checkbutton(
                parent, text=text, variable=var,
                font=f_label, fg=Theme.TEXT_BRIGHT, bg=Theme.BG_DARKEST,
                activebackground=Theme.BG_DARKEST, activeforeground=Theme.CYAN,
                selectcolor=Theme.BG_LIGHT, borderwidth=0, highlightthickness=0,
                cursor="hand2", anchor=tk.W,
            )
            return cb

        cb_h = _styled_check(check_row, "  Include <name>.h    →  #pragma once", self.var_include_h)
        cb_h.pack(anchor=tk.W, pady=2)
        cb_cpp = _styled_check(check_row, "  Include <name>.cpp  →  #include \"<name>.h\" (if .h included)", self.var_include_cpp)
        cb_cpp.pack(anchor=tk.W, pady=2)

        tk.Frame(self.frame_new, bg=Theme.BORDER, height=1).pack(fill=tk.X, pady=14)

        # ── Project name ──
        tk.Label(self.frame_new, text="Project name", font=f_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(anchor=tk.W)
        self.new_name_var = tk.StringVar(value="")
        name_entry = tk.Entry(self.frame_new, textvariable=self.new_name_var,
                               font=f_mono, bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
                               insertbackground=Theme.CYAN, borderwidth=0,
                               highlightthickness=1, highlightcolor=Theme.CYAN_DIM,
                               highlightbackground=Theme.BORDER)
        name_entry.pack(fill=tk.X, pady=(6, 14), ipady=4)

        # ── Destination folder ──
        tk.Label(self.frame_new, text="Create inside", font=f_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(anchor=tk.W)
        dest_row = tk.Frame(self.frame_new, bg=Theme.BG_DARKEST)
        dest_row.pack(fill=tk.X, pady=(6, 4))

        default_dest = str(Path.home())
        if self.initial_dir:
            try:
                p = Path(self.initial_dir)
                if p.exists():
                    default_dest = str(p.parent)
            except Exception:
                pass
        self.new_dest_var = tk.StringVar(value=default_dest)
        dest_entry = tk.Entry(dest_row, textvariable=self.new_dest_var,
                               font=f_mono, bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
                               insertbackground=Theme.CYAN, borderwidth=0,
                               highlightthickness=1, highlightcolor=Theme.CYAN_DIM,
                               highlightbackground=Theme.BORDER)
        dest_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), ipady=4)
        _make_dialog_btn(dest_row, "Browse…", self._browse_dest,
                          Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, f_label).pack(side=tk.LEFT)

        # Live preview of final path
        self.new_preview_var = tk.StringVar(value="")
        tk.Label(self.frame_new, textvariable=self.new_preview_var, font=f_sub,
                 fg=Theme.CYAN_DIM, bg=Theme.BG_DARKEST, anchor=tk.W,
                 justify=tk.LEFT, wraplength=480).pack(anchor=tk.W, pady=(4, 0))

        self.new_name_var.trace_add("write", lambda *a: self._update_new_preview())
        self.new_dest_var.trace_add("write", lambda *a: self._update_new_preview())
        self._update_new_preview()
        tk.Frame(self.frame_new, bg=Theme.BG_DARKEST).pack(fill=tk.BOTH, expand=True)

    def _update_new_preview(self):
        name = self.new_name_var.get().strip()
        dest = self.new_dest_var.get().strip()
        if name and dest:
            self.new_preview_var.set(f"Will create:  {Path(dest) / name}")
        else:
            self.new_preview_var.set("")

    def _browse_dest(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory(
            initialdir=self.new_dest_var.get() or str(Path.home()),
            title="Select Destination Folder",
            parent=self.win,
        )
        if folder:
            self.new_dest_var.set(folder)

    def _on_create_new(self):
        import tkinter.messagebox as mb
        name = self.new_name_var.get().strip()
        dest_raw = self.new_dest_var.get().strip()

        if not name:
            mb.showwarning("Project Name Required", "Please enter a project name.", parent=self.win)
            return
        if not _VALID_NAME_RE.match(name):
            mb.showerror(
                "Invalid Project Name",
                "Project name must start with a letter or underscore, and contain "
                "only letters, numbers, and underscores (no spaces or symbols).\n\n"
                "This name is reused for the .ino / .h / .cpp filenames.",
                parent=self.win,
            )
            return
        if not dest_raw:
            mb.showwarning("Destination Required", "Please choose a destination folder.", parent=self.win)
            return

        dest_parent = Path(dest_raw)
        if not dest_parent.exists() or not dest_parent.is_dir():
            mb.showerror("Invalid Destination", f"This folder does not exist:\n{dest_parent}", parent=self.win)
            return

        target = dest_parent / name
        if target.exists():
            mb.showerror(
                "Folder Already Exists",
                f"A folder named '{name}' already exists at:\n{dest_parent}\n\n"
                "Choose a different name or destination.",
                parent=self.win,
            )
            return

        try:
            project_dir = _scaffold_new_project(
                dest_parent, name,
                include_h=self.var_include_h.get(),
                include_cpp=self.var_include_cpp.get(),
            )
        except Exception as e:
            mb.showerror("Could Not Create Project", f"Failed to create project:\n{e}", parent=self.win)
            return

        self.result = project_dir
        self.win.destroy()

    def _on_cancel(self):
        self.result = None
        self.win.destroy()

    def run(self) -> Path | None:
        self.win.wait_window()
        return self.result


def show_project_selector(root: tk.Tk, initial_dir: str = "") -> Path | None:
    """Show the startup project selector and return the chosen/created
    project folder, or None if the user cancelled."""
    dlg = ProjectSelectorDialog(root, initial_dir)
    return dlg.run()


# ═══════════════════════════════════════════════════════════════
# CUSTOM FILTERABLE DROPDOWN (replaces ttk.Combobox for board select)
# ═══════════════════════════════════════════════════════════════
class FilterableBoardEntry(tk.Frame):
    """A typeable, filterable dropdown that never hands keyboard focus to its popup.

    Built from a plain tk.Entry plus a manually-managed, undecorated
    tk.Toplevel for the suggestion list -- no ttk.Combobox, no
    ttk::combobox::Post anywhere in this class. The popup is shown with
    overrideredirect(1) and its rows are selected via mouse-button
    bindings only; nothing in this class ever calls .focus() / focus_set()
    on the popup or any row inside it, so there is no focus-grab for the
    entry to lose focus to in the first place. This sidesteps the bug
    rather than racing it: Post's internal focus behavior -- whatever it
    actually is on the platform this was reported on -- simply never runs.

    Drop-in contract with the rest of this file: writes to the same
    tk.StringVar passed in as `textvariable` (so every other call site
    that does self.board_var.get() keeps working unchanged), and supports
    .configure(state="disabled"/"normal") like the ttk widget it replaces.
    """

    def __init__(self, parent, textvariable, options, on_select,
                 width=25, popup_width_chars=22, font=None, max_visible_rows=8,
                 popup_height_px=None, placeholder=None, **kwargs):
        super().__init__(parent, bg=Theme.BG_MID)

        self._var = textvariable
        self._all_options = list(options)
        self._on_select = on_select
        self._state = "normal"
        self._popup = None
        self._row_labels = []
        self._highlighted = -1
        self._font = font
        self._max_visible_rows = max_visible_rows
        # Explicit pixel override, separate from max_visible_rows. The
        # previous height fix worked in row-count units (max_visible_rows
        # * row_height_px, floored at 3 rows) -- a literal pixel target
        # doesn't fit that unit cleanly, so this is a direct px value
        # that wins outright when set, instead of being reverse-derived
        # through row math.
        self._popup_height_px_override = popup_height_px
        self._popup_width_chars = popup_width_chars
        self._placeholder_text = placeholder

        self.entry = tk.Entry(
            self,
            textvariable=self._var,
            width=width,
            font=font,
            bg=Theme.BG_LIGHT,
            fg=Theme.TEXT_BRIGHT,
            justify="center",
            insertbackground=Theme.TEXT_BRIGHT,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=Theme.BORDER,
            highlightcolor=Theme.CYAN,
        )
        self.entry.pack(fill=tk.BOTH, expand=True)

        self.entry.bind("<KeyRelease>", self._on_keyrelease)
        self.entry.bind("<Button-1>", self._on_entry_click)
        self.entry.bind("<FocusOut>", self._on_entry_focusout)
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Escape>", lambda e: self._hide_popup())
        self.entry.bind("<Down>", self._on_arrow_down)
        self.entry.bind("<Up>", self._on_arrow_up)

        # ── Placeholder overlay ──────────────────────────────────────────
        # Deliberately implemented as a separate Label layered on top of
        # the entry via .place(), rather than by inserting the placeholder
        # text into self._var itself -- self._var IS self.board_var, read
        # directly by ~25 other call sites in this file as "the selected
        # board name". Writing placeholder text into it would make an
        # empty/unselected state indistinguishable from an actual (if odd)
        # typed board name. This overlay is purely visual and never
        # touches self._var.
        if self._placeholder_text:
            self._placeholder_label = tk.Label(
                self, text=self._placeholder_text, font=font,
                bg=Theme.BG_LIGHT, fg=Theme.TEXT_DIM, anchor="w",
            )
            self._placeholder_label.bind("<Button-1>", self._on_placeholder_click)
            self._var.trace_add("write", self._sync_placeholder_visibility)
            self.entry.bind("<FocusIn>", self._on_focus_in, add="+")
            self.entry.bind("<FocusOut>", self._sync_placeholder_visibility, add="+")
            self._sync_placeholder_visibility()
        else:
            self._placeholder_label = None

        toplevel = self.winfo_toplevel()
        toplevel.bind_all("<Button-1>", self._on_global_click, add="+")
        toplevel.bind("<Configure>", self._on_toplevel_configure, add="+")

    def _on_placeholder_click(self, event):
        if self._state == "disabled":
            return
        if self._placeholder_label:
            self._placeholder_label.place_forget()
        self.entry.focus_set()
        self._on_entry_click(event)

    def _on_focus_in(self, event=None):
        if self._state == "disabled":
            return
        if self._placeholder_label:
            self._placeholder_label.place_forget()
        val = self._var.get()
        if self._placeholder_text and val.strip().lower() == self._placeholder_text.lower():
            self._var.set("")
        matches = self._matches()
        if matches:
            self._show_popup(matches)

    def _on_global_click(self, event):
        # Fires for a click on ANY widget in the app, including this
        # entry and rows inside this popup -- so this must positively
        # confirm the click landed OUTSIDE both before hiding, or
        # selecting a row (a legitimate click inside the popup) would
        # fight with the row's own <ButtonRelease-1> selection handler.
        if self._popup is None or not self._popup.winfo_viewable():
            return
        clicked = event.widget
        # A click on the entry itself is handled by _on_entry_click
        # already (it re-shows/refilters); don't hide out from under it.
        if clicked is self.entry:
            return
        # Walk up the clicked widget's own ancestor chain to check
        # whether it's the popup Toplevel or anything inside it (canvas,
        # scrollbar, row labels). Using the popup as the stopping point
        # rather than comparing against every row Label individually,
        # since new Label widgets are destroyed/recreated on every
        # keystroke and a fixed list would go stale immediately.
        w = clicked
        while w is not None:
            if w is self._popup:
                return
            w = getattr(w, "master", None)
        self._hide_popup()

    def _on_toplevel_configure(self, event):
        # <Configure> fires for resize AND move (Tk does not distinguish
        # them at this event level -- a window move reports the same
        # event type as a resize), so one binding covers both halves of
        # the request. Guarded on winfo_viewable() so this is a no-op
        # whenever the popup isn't actually open -- without that guard,
        # <Configure> firing for unrelated reasons (including this
        # popup's own self._popup.geometry(...) call during normal
        # positioning) could close the popup the instant it opens, before
        # the user ever sees it.
        #
        # _suppress_next_configure additionally guards against a platform
        # -dependent case I can't verify without a live Tk/window-manager
        # here: deiconify()/lift() mapping a brand-new Toplevel can, on
        # some window managers, itself generate a <Configure> echo on the
        # root window in the same event-processing cycle. Without this,
        # that echo would pass the viewable check above (deiconify()
        # already ran) and immediately hide the popup that was just
        # shown. The flag is set right before deiconify() and cleared on
        # the next idle cycle, so it only swallows that one immediate
        # echo -- a genuine resize/move a moment later still goes through
        # normally.
        if getattr(self, "_suppress_next_configure", False):
            return
        if self._popup is not None and self._popup.winfo_viewable():
            self._hide_popup()

    # ── public, ttk-compatible surface ──────────────────────────────────
    def pack(self, *args, **kwargs):
        super().pack(*args, **kwargs)
        return self

    def configure(self, **kwargs):
        if "state" in kwargs:
            self._state = kwargs["state"]
            entry_state = tk.DISABLED if self._state == "disabled" else tk.NORMAL
            self.entry.configure(state=entry_state)
            if self._state == "disabled":
                self._hide_popup()
        # Unknown kwargs are intentionally ignored rather than raised --
        # the old ttk widget also silently accepted values= via this same
        # call path; that usage is gone now that filtering is internal.

    config = configure

    def update_options(self, new_options):
        """Update the list of options in the dropdown dynamically."""
        self._all_options = list(new_options)

    # ── filtering / popup ────────────────────────────────────────────────
    def _sync_placeholder_visibility(self, *args):
        """Show the 'Select Board' overlay only while the field is truly
        empty and doesn't have focus (so it never covers the cursor or
        text being typed)."""
        if self._placeholder_label is None:
            return
        try:
            top = self.winfo_toplevel()
            has_focus = (top.focus_get() is self.entry) or (self._popup is not None and top.focus_get() is getattr(self, "_listbox", None))
        except Exception:
            has_focus = False
        val = self._var.get()
        is_placeholder = bool(self._placeholder_text and val.strip().lower() == self._placeholder_text.lower())
        if (not val or is_placeholder) and not has_focus:
            self._placeholder_label.place(in_=self.entry, x=6, rely=0.5, anchor="w")
        else:
            self._placeholder_label.place_forget()

    def _matches(self):
        val = self._var.get()
        if not val or (self._placeholder_text and val.strip().lower() == self._placeholder_text.lower()):
            return list(self._all_options)
        query = val.lower()
        return [b for b in self._all_options if query in b.lower()]

    def _on_keyrelease(self, event):
        if self._state == "disabled":
            return
        if event.keysym in ("Up", "Down", "Return", "Escape",
                             "Shift_L", "Shift_R", "Control_L", "Control_R",
                             "Alt_L", "Alt_R", "Tab"):
            return
        matches = self._matches()
        if matches:
            self._show_popup(matches)
        else:
            self._hide_popup()

    def _on_entry_click(self, event):
        if self._state == "disabled":
            return
        if self._placeholder_label:
            self._placeholder_label.place_forget()
        val = self._var.get()
        if self._placeholder_text and val.strip().lower() == self._placeholder_text.lower():
            self._var.set("")
        matches = self._matches()
        if matches:
            self._show_popup(matches)

    def _show_popup(self, matches):
        # Flush any pending pack()-scheduled geometry recalculation
        # before reading self.entry.winfo_width()/winfo_rootx()/
        # winfo_rooty() below and further down (positioning). Tk's
        # geometry manager queues relayout after a window resize/restore
        # rather than applying it the instant the resize happens -- so
        # without this, winfo_* calls made shortly after a resize can
        # return the entry's PRE-resize size/position, putting the popup
        # at stale coordinates from the old window size instead of
        # tracking the entry to its current location. One call here
        # covers both read sites below: nothing between here and the
        # later positioning code touches self.entry's own geometry (only
        # this popup's own children -- canvas/scrollbar/labels -- are
        # built in between), so the flush stays valid for both reads.
        self.update_idletasks()

        if self._popup is None:
            self._popup = tk.Toplevel(self)
            self._popup.overrideredirect(1)
            self._popup.configure(bg=Theme.BORDER)
            # Deliberately never call focus_set()/focus_force() on this
            # Toplevel or anything inside it -- that's the entire fix
            # from the previous round.

            # Scrollable container: previously rows were packed straight
            # into a plain Frame with no height cap and no scrollbar, so
            # a long board list either ran off-screen or had no way to
            # reach entries past the visible area -- there was nothing to
            # scroll WITHIN, since the Frame just grew to fit every row.
            # Canvas + Scrollbar is the standard Tk pattern for a
            # height-capped scrollable area; plain Frame/pack has no
            # native scroll support to retrofit onto.
            self._canvas = tk.Canvas(
                self._popup, bg=Theme.BG_LIGHT, highlightthickness=0,
                bd=0,
            )
            self._scrollbar = tk.Scrollbar(
                self._popup, orient=tk.VERTICAL, command=self._canvas.yview,
                bg=Theme.BG_MID, troughcolor=Theme.BG_DARKEST,
                activebackground=Theme.BORDER_LIT,
                bd=0, relief=tk.FLAT, highlightthickness=0,
                elementborderwidth=0,
            )
            self._canvas.configure(yscrollcommand=self._scrollbar.set)

            self._list_frame = tk.Frame(self._canvas, bg=Theme.BG_LIGHT)
            self._list_frame_window = self._canvas.create_window(
                (0, 0), window=self._list_frame, anchor="nw"
            )

            def _on_list_frame_configure(event):
                self._canvas.configure(scrollregion=self._canvas.bbox("all"))
            self._list_frame.bind("<Configure>", _on_list_frame_configure)

            def _on_mousewheel(event):
                # Windows/macOS deliver delta directly; X11 uses
                # Button-4/5 instead, handled by the separate binds below.
                self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

            # PREVIOUS ROUND'S BUG: these were bound on self._canvas only.
            # Tk delivers <MouseWheel> to whichever widget the pointer is
            # directly over, and the canvas is almost entirely covered by
            # _list_frame and the row Labels packed inside it -- so with
            # the pointer anywhere over an actual row (i.e. exactly where
            # someone scrolling a list keeps their pointer), the event
            # landed on that Label, which had no wheel binding, and went
            # nowhere. The canvas-only binding could only ever fire in
            # whatever thin unlabeled margin happened to be left, which
            # for a list filling the popup could be effectively zero.
            # This is why it was reported unscrollable regardless of the
            # height/row-count math -- a separate root cause from the
            # sizing bug below, not the same bug re-explained.
            #
            # Binding on self._popup (the Toplevel that contains
            # everything -- canvas, frame, and every row label) means Tk
            # delivers the wheel event up through that bind no matter
            # which specific child the pointer sits over, since the
            # Toplevel is the common ancestor of all of them.
            self._popup.bind("<MouseWheel>", _on_mousewheel)
            self._popup.bind("<Button-4>", lambda e: self._canvas.yview_scroll(-1, "units"))
            self._popup.bind("<Button-5>", lambda e: self._canvas.yview_scroll(1, "units"))

        # Match the popup width to the entry's CURRENT on-screen width
        # (not the fixed popup_width_chars constructor arg, which was a
        # static char-count that never changed when the entry itself
        # stretched via fill=tk.X, expand=True on window resize -- that
        # mismatch is why the popup used to stay narrow even as the
        # combobox grew with the GUI window). winfo_width() reflects
        # whatever the entry is actually rendered at right now, so the
        # popup tracks every resize instead of being pinned to the width
        # implied by the constructor's width= char count.
        font_obj = self._font if isinstance(self._font, tkfont.Font) else tkfont.Font(font=self._font)
        entry_width_px = self.entry.winfo_width()
        if entry_width_px <= 1:
            # winfo_width() can report 1px before the entry has been
            # mapped/laid out by the geometry manager yet (e.g. a popup
            # triggered programmatically before first paint). Fall back
            # to the char-count estimate only for that edge case, rather
            # than showing a 1px-wide popup.
            sample_char_width = font_obj.measure("0")
            popup_width_px = sample_char_width * self._popup_width_chars
        else:
            popup_width_px = entry_width_px
        # NOTE: "linespace" is, to my knowledge, a real key returned by
        # tkfont.Font.metrics() -- but I can't verify this against a
        # running Tk in this environment either (still no tkinter/network
        # access here), so treat this as unverified the same way the
        # earlier Post-focus-timing claims were. If row height looks
        # wrong, this is the first thing to check; metrics() also exposes
        # "ascent"/"descent" as a fallback (ascent + descent is an
        # equivalent way to get full line height if "linespace" turns
        # out not to behave as expected here).
        row_height_px = font_obj.metrics("linespace") + 4  # + row pady

        # Pin the canvas-window (and so _list_frame, and so every row
        # packed with fill=tk.X inside it) to this width explicitly.
        # Without this, a Label with no width= and fill=tk.X has nothing
        # concrete to expand against here -- it would size to its own
        # content, leaving short board names narrower than the popup and
        # making row highlight-on-hover look inconsistent row to row.
        self._canvas.itemconfigure(self._list_frame_window, width=popup_width_px)

        for lbl in self._row_labels:
            lbl.destroy()
        self._row_labels = []
        self._highlighted = -1

        for i, name in enumerate(matches):
            display_text = name
            # Truncate (rather than let the row/popup grow) when content
            # is wider than the shared popup width -- this is what
            # "narrower listbox, same text" has to mean in practice;
            # without truncation, long board names would just force the
            # row wider again and undo the width-matching above.
            while font_obj.measure(display_text + "…") > popup_width_px - 12 and len(display_text) > 1:
                display_text = display_text[:-1]
            if display_text != name:
                display_text += "…"

            lbl = tk.Label(
                self._list_frame, text=display_text, anchor="w",
                font=self._font,
                bg=Theme.BG_LIGHT, fg=Theme.TEXT,
                padx=6, pady=2,
            )
            # No explicit width= here on purpose: text is already
            # pixel-truncated above against the same font_obj.measure()
            # used for popup_width_px, and the canvas clips visible width
            # regardless. Adding tk.Label's own width= here would size
            # against Tk's internal avg-char-width estimate for this
            # font -- a second, separate measurement that could drift
            # from the pixel-based one above, which is the exact
            # two-independent-numbers problem this fix is meant to undo.
            lbl.pack(fill=tk.X)
            # ButtonRelease (not Button-1 press) so a click-drag off the
            # row still cancels cleanly, matching normal listbox feel.
            # Bind against `name` (the full text), not display_text, so
            # truncated rows still select the correct full board name.
            lbl.bind("<ButtonRelease-1>", lambda e, n=name: self._select(n))
            lbl.bind("<Enter>", lambda e, w=lbl: w.configure(bg=Theme.BG_HOVER))
            lbl.bind("<Leave>", lambda e, w=lbl: w.configure(bg=Theme.BG_LIGHT))
            # Belt-and-suspenders alongside the self._popup-level binding
            # above: I can't verify in this environment (no tkinter/
            # network access) whether a Toplevel-level <MouseWheel> bind
            # actually receives events that occur over its children by
            # default, versus needing each child bound individually --
            # both are documented approaches to this exact Tk limitation
            # depending on version/platform, and I'd rather bind both
            # than gamble the whole fix on one untested mechanism after
            # the unscrollable report already burned one round on an
            # under-verified assumption.
            lbl.bind("<MouseWheel>", lambda e: self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
            lbl.bind("<Button-4>", lambda e: self._canvas.yview_scroll(-1, "units"))
            lbl.bind("<Button-5>", lambda e: self._canvas.yview_scroll(1, "units"))
            self._row_labels.append(lbl)

        # Cap visible height to max_visible_rows; beyond that, scroll.
        # PREVIOUS ROUND'S BUG: with few matches (e.g. SUPPORTED_BOARDS
        # only has 5 static entries before load_dynamic_boards adds
        # anything found on disk), visible_rows could end up small enough
        # that the resulting popup was, per the report, "barely" visible
        # -- and needs_scrollbar (len(matches) > max_visible_rows) was
        # False in exactly that same small-list case, so the one widget
        # that could have made a too-small popup recoverable was the one
        # being hidden. The scrollbar is kept always shown rather than
        # conditionally hidden -- a visible scrollbar costs little screen
        # space and removes any ambiguity about whether the list can
        # scroll at all.
        #
        # NEXT ROUND'S BUG (now fixed): the original fix for the above
        # applied the minimum to the POPUP height directly --
        # max(visible_rows * row_height_px, row_height_px * 3) -- which
        # padded the canvas to 3 rows tall even when only 1-2 rows of
        # actual content existed (e.g. filtering down to 2 matches). The
        # canvas was then taller than what _on_list_frame_configure's
        # bbox("all") measured as scrollregion, since scrollregion
        # follows the real row Labels in _list_frame, not the floor. A
        # Scrollbar's thumb length is proportional to
        # (visible canvas height / total scrollregion height) -- so a
        # canvas padded taller than its own scrollregion produces a thumb
        # that's an oversized fraction of the track, with leftover track
        # space the thumb can never actually occupy. Flooring the PER-ROW
        # height instead keeps the canvas exactly row-count-sized (no
        # padding beyond real content) while still keeping a 1-2 row
        # popup tall enough to read comfortably.
        min_row_height_px = 22
        effective_row_height_px = max(row_height_px, min_row_height_px)

        if self._popup_height_px_override is not None:
            popup_height_px = self._popup_height_px_override
        else:
            visible_rows = min(len(matches), self._max_visible_rows)
            popup_height_px = visible_rows * effective_row_height_px

        self._canvas.configure(width=popup_width_px, height=popup_height_px)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._scrollbar.grid(row=0, column=1, sticky="ns")
        self._popup.grid_rowconfigure(0, weight=1)
        self._popup.grid_columnconfigure(0, weight=1)

        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height()
        self._popup.geometry(f"+{x}+{y}")

        # See _on_toplevel_configure: guards against a possible same-
        # cycle <Configure> echo from the deiconify()/lift() below on
        # some window managers. after_idle clears it once this event
        # cycle has fully drained, so it's a one-shot suppression of
        # just the open itself, not a standing filter.
        self._suppress_next_configure = True
        self.after_idle(lambda: setattr(self, "_suppress_next_configure", False))

        self._popup.deiconify()
        self._popup.lift()
        # Defensive, not strictly verified: deiconify() mapping a Toplevel
        # is documented as not requesting focus when overrideredirect(1)
        # is set, but given that earlier fix attempts in this file were
        # built on unverified Tk-internals assumptions and failed, I'm
        # not relying on "documented" alone here. Explicitly re-asserting
        # focus on the entry right after mapping costs nothing if
        # deiconify() was already focus-neutral, and closes the gap if
        # it wasn't.
        self.entry.focus_set()

    def _hide_popup(self):
        if self._popup is not None:
            self._popup.withdraw()

    def _select(self, name):
        self._var.set(name)
        self._last_valid_board_sync()
        self._hide_popup()
        self.entry.focus_set()
        if self._on_select:
            self._on_select()

    def _last_valid_board_sync(self):
        # Best-effort: keep the owning GUI's _last_valid_board in step,
        # mirroring what the old _on_board_combobox_selected did. Done
        # via getattr/setattr on the parent chain rather than a hard
        # reference so this widget stays reusable on its own.
        owner = self.master
        while owner is not None and not hasattr(owner, "_last_valid_board"):
            owner = getattr(owner, "master", None)
        if owner is not None:
            owner._last_valid_board = self._var.get()

    def _on_return(self, event):
        if self._highlighted >= 0 and self._highlighted < len(self._row_labels):
            # A row was reached via arrow keys -- confirm that one, not
            # whatever the literal text currently says. Previously this
            # branch was missing entirely: arrow keys repainted the
            # highlight but Enter ignored it and fell through to
            # first-match, which is a real keyboard-navigation bug, not
            # just a style gap.
            self._select(self._row_labels[self._highlighted].cget("text"))
            return
        val = self._var.get()
        if val in self._all_options:
            self._select(val)
            return
        matches = self._matches()
        if matches:
            self._select(matches[0])
        else:
            self._var.set(getattr(self, "_revert_to", val))
            self._hide_popup()

    def _on_arrow_down(self, event):
        if not self._row_labels:
            return
        self._highlighted = min(self._highlighted + 1, len(self._row_labels) - 1)
        self._refresh_highlight()

    def _on_arrow_up(self, event):
        if not self._row_labels:
            return
        self._highlighted = max(self._highlighted - 1, 0)
        self._refresh_highlight()

    def _refresh_highlight(self):
        for i, lbl in enumerate(self._row_labels):
            lbl.configure(bg=Theme.BG_HOVER if i == self._highlighted else Theme.BG_LIGHT)
        if 0 <= self._highlighted < len(self._row_labels):
            # At a short popup height (e.g. 40px, under two rows), arrow
            # navigation moves the highlight off-screen almost
            # immediately with no visual feedback unless the canvas
            # scrolls to follow it. Wasn't needed as urgently at the
            # previous 8-row cap, but is a real gap now -- caught while
            # reviewing this change rather than after another round.
            self._row_labels[self._highlighted].update_idletasks()
            bbox = self._canvas.bbox(self._list_frame_window)
            label_y = self._row_labels[self._highlighted].winfo_y()
            label_h = self._row_labels[self._highlighted].winfo_height()
            content_h = self._list_frame.winfo_height()
            if content_h > 0:
                visible_top = self._canvas.canvasy(0)
                visible_bottom = visible_top + self._canvas.winfo_height()
                if label_y < visible_top:
                    self._canvas.yview_moveto(label_y / content_h)
                elif (label_y + label_h) > visible_bottom:
                    self._canvas.yview_moveto((label_y + label_h - self._canvas.winfo_height()) / content_h)

    def _on_entry_focusout(self, event):
        # If focus is moving to our own popup, this would be the place to
        # special-case it -- but nothing in this class ever sets focus
        # into the popup, so that case can't occur. Any FocusOut here is
        # a real blur (user tabbed away or clicked outside both the entry
        # and the popup's labels, which would have already handled the
        # click via their own ButtonRelease binding before this fires).
        self.after(120, self._settle_on_blur)

    def _settle_on_blur(self):
        # Deferred slightly so a click that landed on a popup row gets to
        # run its own ButtonRelease handler (_select) first; that handler
        # calls entry.focus_set(), which means by the time this runs,
        # focus is back on the entry and the early-return below fires --
        # so a real row click never triggers the revert-on-blur below.
        if self.entry.focus_get() is self.entry:
            return
        val = self._var.get()
        # Empty is always a valid intentional state — the user cleared the
        # field deliberately. Only snap back when the user typed a partial
        # or mismatched string (non-empty, not in the options list).
        if val and val not in self._all_options:
            owner = self.master
            while owner is not None and not hasattr(owner, "_last_valid_board"):
                owner = getattr(owner, "master", None)
            if owner is not None:
                self._var.set(owner._last_valid_board)
        self._hide_popup()


# ─────────────────────────────────────────────────────────────
# TOOLTIP COMPONENT
# ─────────────────────────────────────────────────────────────
class ToolTip:
    """Creates a custom floating tooltip when hovering over a widget."""
    def __init__(self, widget, text_func):
        self.widget = widget
        self.text_func = text_func
        self.tip_window = None
        self.widget.bind("<Enter>", self.show_tip)
        self.widget.bind("<Leave>", self.hide_tip)

    def show_tip(self, event=None):
        if self.tip_window or not self.text_func():
            return
        
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        
        # Styled tooltip matching the theme palette
        label = tk.Label(
            tw, text=self.text_func(), justify=tk.LEFT,
            background=Theme.BG_LIGHT, foreground=Theme.TEXT_BRIGHT,
            relief=tk.SOLID, borderwidth=1,
            highlightbackground=Theme.BORDER,
            padx=8, pady=4,
            font=("Montserrat", 9)
        )
        label.pack(ipadx=1)

        # Force geometry calculation to get accurate window width
        tw.update_idletasks()
        
        # Calculate screen/widget-relative tooltip position
        widget_x = self.widget.winfo_rootx()
        widget_w = self.widget.winfo_width()
        widget_h = self.widget.winfo_height()
        tip_w = tw.winfo_width()
        
        x = widget_x + 10
        y = self.widget.winfo_rooty() + widget_h + 5
        
        # Check if tooltip extends beyond screen width
        try:
            screen_w = self.widget.winfo_screenwidth()
            if x + tip_w > screen_w:
                x = widget_x + widget_w - tip_w
        except Exception:
            pass
            
        if x < 0:
            x = 10
            
        tw.wm_geometry(f"+{x}+{y}")

    def hide_tip(self, event=None):
        tw = self.tip_window
        self.tip_window = None
        if tw:
            tw.destroy()


# ═══════════════════════════════════════════════════════════════
# MAIN APPLICATION
# ═══════════════════════════════════════════════════════════════
class MCUUploadGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.editor_mode = globals().get("_RESOLVED_EDITOR_MODE") or get_editor_mode()
        import concurrent.futures
        self._bg_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="MCUBgExecutor"
        )
        self.root.title("MCU Flasher by Naph — ESP32 Upload & Monitor")
        try:
            self.screen_w = self.root.winfo_screenwidth()
            self.screen_h = self.root.winfo_screenheight()
        except Exception:
            self.screen_w, self.screen_h = 1920, 1080

        if self.screen_w < 1400 or self.screen_h < 800:
            self.root.minsize(800, 450)
            self._editor_height = 200
            self._editor_minsize = 120
            self._bottom_height = 180
            self._bottom_minsize = 100
        else:
            self.root.minsize(900, 520)
            self._editor_height = 400
            self._editor_minsize = 200
            self._bottom_height = 250
            self._bottom_minsize = 150
        self.root.configure(bg=Theme.BG_DARKEST)

        # ── State ──
        self.serial_conn: serial.Serial | None = None
        self.serial_thread: threading.Thread | None = None
        self.serial_running = False
        self._monitor_should_run = False   # intent flag: True = keep reconnecting
        self._monitor_paused = False       # True = keep reading the port, but hold back display
        self.process: subprocess.Popen | None = None
        self._download_managers: list[subprocess.Popen] = []
        self.is_busy = False
        self._board_port_confirmed = False  # True only once esptool's live probe (or a known chip signature) confirms what's on the port
        self.sketch_dir_path = DEFAULT_SKETCH_DIR
        self._last_known_ports: set = set()  # for USB hotplug detection
        self._auto_start_after_id = None
        self._last_conn_attempt = {"port": "", "baud": 0, "board": "", "time": 0.0}
        self._first_run = True
        self._last_monitor_error = ""
        self._focus_tab_on_unlock = None  # one-shot: tab index to select when busy state next clears

        # ── Compile cache ──
        # Tracks whether sources have changed since the last successful compile.
        # _compile_cache_hash is the hash saved after the last successful compile
        # for the current project. It is invalidated whenever the folder changes.
        self._compile_cache_hash: str | None = None
        # Track which board the last successful compile was for so we can tell
        # the user "board changed → full rebuild" instead of "first-time compile".
        self._last_compiled_board: str | None = None
        # Per-board hash cache: {board_name: hash}. Lets each board remember its
        # own "sources unchanged, safe to skip recompile" state independently,
        # so switching back to a previously-built board doesn't force a
        # needless rebuild just because a different board was compiled in between.
        self._compile_cache_by_board: dict[str, str] = {}
        self._compat_warnings_approved_hash: str | None = None
        self._stop_requested: bool = False
        self._op_session_id: int = 0

        # ── Startup project selector ──
        # Prompt for a project (existing folder, or scaffold a new one)
        # before the main window appears.
        self.root.withdraw()  # hide main window until project is chosen
        config = load_gui_config()
        last_dir = config.get("last_sketch_dir") or ""
        if last_dir and not Path(last_dir).exists():
            last_dir = ""

        project_dir = show_project_selector(self.root, initial_dir=last_dir)
        if not project_dir:
            self.root.destroy()
            return

        self.sketch_dir_path = project_dir
        config["last_sketch_dir"] = str(self.sketch_dir_path)
        save_gui_config(config)

        self.root.deiconify()  # show main window now
        self.root.lift()
        self.root.focus_force()
        self.root.attributes("-topmost", True)
        self.root.after(500, self._unset_main_topmost)

    def _unset_main_topmost(self):
        try:
            if hasattr(self, "root") and self.root:
                self.root.attributes("-topmost", False)
        except Exception:
            pass

        # ── Icon (set a simple colored icon) ──
        try:
            self.root.iconbitmap(default="")
        except Exception:
            pass

        # ── Fonts & button scaling ──
        # Continuous scale factor based on screen resolution instead of a
        # hard small/large cutoff, so sizing adapts smoothly across monitors
        # (1.0 == a 1920x1080 reference display). Also trimmed down slightly
        # across the board since the old fixed sizes ran a bit large.
        self._ui_scale = max(0.65, min(1.0, min(self.screen_w / 1920.0, self.screen_h / 1080.0)))
        self._last_applied_scale = self._ui_scale
        self._scalable_buttons: list[tk.Button] = []

        def _sz(base: float, floor: int) -> int:
            return max(floor, round(base * self._ui_scale))

        self.font_title    = tkfont.Font(family="Montserrat", size=_sz(15, 11), weight="bold")
        self.font_subtitle = tkfont.Font(family="Montserrat", size=_sz(9, 8))
        self.font_label    = tkfont.Font(family="Montserrat", size=_sz(9, 8))
        self.font_btn      = tkfont.Font(family="Montserrat", size=_sz(9, 7), weight="bold")
        self.font_mono     = tkfont.Font(family="Consolas", size=_sz(10, 8))
        self.font_mono_sm  = tkfont.Font(family="Consolas", size=_sz(9, 8))
        self.font_status   = tkfont.Font(family="Montserrat", size=_sz(9, 8))
        self._btn_padx = _sz(10, 6)
        self._btn_pady = _sz(3, 2)

        self._build_ui()
        # Re-tune button padding/font live as the window is resized, so
        # buttons keep shrinking a bit further if the user makes the window
        # smaller than the screen (not just at startup).
        self.root.bind("<Configure>", self._on_root_configure)
        # Refresh skip-compile readiness whenever the main window regains focus
        # (e.g. after the embedded editor has auto-saved files on the side).
        self.root.bind("<FocusIn>", lambda _e: self.root.after(
            200, self._update_skip_compile_state), add="+")
        # Apply initial upload-speed visibility (hides combo for Arduino Uno)
        self._on_board_changed()
        self._refresh_ports()
        self._update_hardware_action_buttons()
        self._start_port_polling()

        # ── Cleanup on close ──
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._first_run = False

    def _get_sketch_display_name(self) -> str:
        folder_name = self.sketch_dir_path.name
        if (self.screen_w < 1400 or self.screen_h < 800) and len(folder_name) > 30:
            return folder_name[:27] + "..."
        return folder_name

    # ──────────────────────────────────────────────────────────
    # UI CONSTRUCTION
    # ──────────────────────────────────────────────────────────
    def _build_ui(self):
        # Use the 'clam' ttk theme for the whole window right from the start.
        # Native themes (vista/xpnative on Windows, aqua on macOS) render
        # Notebook tabs, Comboboxes, and Scrollbars using the OS's own visual
        # styling engine and silently ignore ttk style.configure() background/
        # foreground overrides. 'clam' draws everything with ttk's own
        # generic engine, which is the only way our dark colorway (Theme.*)
        # actually reaches these widgets. This must happen before any ttk
        # widget in this window is created or styled.
        ttk.Style().theme_use("clam")

        # ── Title Bar ──
        title_pady = 6 if (self.screen_w < 1400 or self.screen_h < 800) else 10
        title_padx = 10 if (self.screen_w < 1400 or self.screen_h < 800) else 16
        title_frame = tk.Frame(self.root, bg=Theme.BG_DARK, pady=title_pady, padx=title_padx)
        title_frame.pack(fill=tk.X)

        title_left = tk.Frame(title_frame, bg=Theme.BG_DARK)
        title_left.pack(side=tk.LEFT)

        # Logo text
        tk.Label(
            title_left, text="⚡ MCU Flasher by Naph",
            font=self.font_title, fg=Theme.CYAN, bg=Theme.BG_DARK,
        ).pack(side=tk.LEFT)

        if not (self.screen_w < 1400 or self.screen_h < 800):
            tk.Label(
                title_left, text="   ESP32 Upload & Serial Monitor",
                font=self.font_subtitle, fg=Theme.TEXT_DIM, bg=Theme.BG_DARK,
            ).pack(side=tk.LEFT, pady=(4, 0))

        # Sketch path on the right (packed first to lock it to the far right)
        sketch_frame = tk.Frame(title_frame, bg=Theme.BG_DARK)
        sketch_frame.pack(side=tk.RIGHT)

        self.lbl_sketch = tk.Label(
            sketch_frame, text=f"📁 {self._get_sketch_display_name()}",
            font=self.font_mono_sm, fg=Theme.TEXT_DIM, bg=Theme.BG_DARK,
            cursor="hand2"
        )
        self.lbl_sketch.pack(side=tk.LEFT)
        self.lbl_sketch.bind("<Button-1>", lambda e: self._open_sketch_in_explorer())
        self.lbl_sketch.bind("<Button-3>", lambda e: self._select_sketch_folder())
        ToolTip(self.lbl_sketch, lambda: f"{self.sketch_dir_path}  •  Left-click: open in Explorer  •  Right-click: change project folder")

        self.btn_new_project = self._make_icon_btn(
            sketch_frame, "📁", self._new_project,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, width=3
        )
        self.btn_new_project.pack(side=tk.LEFT, padx=(4, 0))

        download_btn_text = "⬇ Download" if (self.screen_w < 1400 or self.screen_h < 800) else "⬇ Download Boards/Libraries"
        self.btn_download_mgr = self._make_btn(
            sketch_frame, download_btn_text, self._open_download_manager,
            Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, font=self.font_label
        )
        self.btn_download_mgr.pack(side=tk.LEFT, padx=(8, 0))

        # Centered action buttons container with indicator
        actions_frame = tk.Frame(title_frame, bg=Theme.BG_DARK)
        actions_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner_actions = tk.Frame(actions_frame, bg=Theme.BG_DARK)
        inner_actions.pack(expand=True)

        tk.Label(
            inner_actions, text="ACTIONS", font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARK
        ).pack(pady=(0, 2))

        btn_row = tk.Frame(inner_actions, bg=Theme.BG_DARK)
        btn_row.pack()

        self.btn_compile = self._make_btn(btn_row, "⚙ Compile", self._do_compile,
                                           Theme.BTN_COMPILE, Theme.BTN_COMPILE_H)
        self.btn_compile.pack(side=tk.LEFT, padx=3)

        self.btn_upload = self._make_btn(btn_row, "⚡ Upload", self._do_upload,
                                          Theme.BTN_FULL, Theme.BTN_FULL_H)
        self.btn_upload.pack(side=tk.LEFT, padx=3)

        self.btn_stop = self._make_btn(btn_row, "■ Stop", self._do_stop,
                                        Theme.BTN_STOP, Theme.BTN_STOP_H)
        self.btn_stop.pack(side=tk.LEFT, padx=3)
        self.btn_stop.configure(state=tk.DISABLED)



        self.btn_clean = self._make_btn(btn_row, "🧹 Clean", self._do_clean,
                                         Theme.BTN_CLEAR, Theme.BTN_CLEAR_H)
        self.btn_clean.pack(side=tk.LEFT, padx=3)

        # ── Separator between build actions and file actions ──
        tk.Frame(btn_row, bg=Theme.BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        self.btn_save = self._make_btn(
            btn_row, "💾 Save", lambda: self._save_current_editor_file(),
            Theme.BTN_COMPILE, Theme.BTN_COMPILE_H
        )
        self.btn_save.pack(side=tk.LEFT, padx=3)

        self.btn_save_all = self._make_btn(
            btn_row, "💾 Save All", lambda: self._save_all_editor_files(),
            Theme.BTN_FULL, Theme.BTN_FULL_H
        )
        self.btn_save_all.pack(side=tk.LEFT, padx=3)

        self.btn_reload_file = self._make_btn(
            btn_row, "↺ Reload", lambda: self._reload_current_editor_file(),
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H
        )
        self.btn_reload_file.pack(side=tk.LEFT, padx=3)

        # ── Separator between file-editing actions and modify-files ──
        tk.Frame(btn_row, bg=Theme.BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        self.btn_modify_files = self._make_btn(
            btn_row, "🛠 Modify", self._open_modify_files_dialog,
            Theme.BTN_MONITOR, Theme.BTN_MONITOR_H
        )
        self.btn_modify_files.pack(side=tk.LEFT, padx=3)

        # ── Separator ──
        tk.Frame(self.root, bg=Theme.CYAN_DIM, height=2).pack(fill=tk.X)

        # ── Controls Bar ──
        ctrl_frame = tk.Frame(self.root, bg=Theme.BG_MID, pady=10, padx=16)
        ctrl_frame.pack(fill=tk.X)

        # Board selection
        board_group = tk.Frame(ctrl_frame, bg=Theme.BG_MID)
        board_group.pack(side=tk.LEFT, padx=(0, 12), fill=tk.X, expand=True)

        tk.Label(board_group, text="BOARD", font=self.font_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_MID).pack(anchor=tk.W)

        board_row = tk.Frame(board_group, bg=Theme.BG_MID)
        board_row.pack(fill=tk.X)

        self._build_board_dropdown(board_row)

        # Port selection
        port_group = tk.Frame(ctrl_frame, bg=Theme.BG_MID)
        port_group.pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(port_group, text="PORT", font=self.font_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_MID).pack(anchor=tk.W)

        port_row = tk.Frame(port_group, bg=Theme.BG_MID)
        port_row.pack()

        self.port_var = tk.StringVar()
        # Make the width of the port combobox dynamic to prevent overflow on smaller screens (like 1366x768)
        port_width = 30 if (self.screen_w < 1400 or self.screen_h < 800) else 45
        self.port_combo = ttk.Combobox(
            port_row, textvariable=self.port_var, width=port_width,
            font=self.font_mono_sm, state="readonly", justify="left",
            postcommand=self._refresh_ports,
        )
        self.port_combo.pack(side=tk.LEFT, padx=(0, 4))
        self.port_combo.bind("<<ComboboxSelected>>", lambda e: self._on_port_changed())

        self._marquee_dir = 1
        self._marquee_pause = 0
        self._start_marquee()

        # Baud rate
        baud_group = tk.Frame(ctrl_frame, bg=Theme.BG_MID)
        baud_group.pack(side=tk.LEFT, padx=(0, 20))

        tk.Label(baud_group, text="BAUD", font=self.font_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_MID).pack(anchor=tk.W)

        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
        self.baud_combo = ttk.Combobox(
            baud_group, textvariable=self.baud_var, width=10,
            font=self.font_mono_sm, state="readonly",
            values=["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"],
        )
        self.baud_combo.pack()
        self.baud_combo.bind("<<ComboboxSelected>>", lambda e: self._on_baud_changed())

        # Upload speed (used for flashing; independent of serial monitor baud)
        upload_spd_group = tk.Frame(ctrl_frame, bg=Theme.BG_MID)
        upload_spd_group.pack(side=tk.LEFT, padx=(0, 20))
        self._upload_spd_group = upload_spd_group  # saved for show/hide

        lbl_upload_spd = tk.Label(upload_spd_group, text="UPLOAD SPD", font=self.font_label,
                                  fg=Theme.TEXT_DIM, bg=Theme.BG_MID)
        lbl_upload_spd.pack(anchor=tk.W)

        self.upload_speed_var = tk.StringVar(value=str(DEFAULT_UPLOAD_SPEED))
        self.upload_speed_combo = ttk.Combobox(
            upload_spd_group, textvariable=self.upload_speed_var, width=10,
            font=self.font_mono_sm, state="readonly", justify="center",
            values=["115200", "230400", "460800", "512000", "921600"],
        )
        self.upload_speed_combo.pack()
        self.upload_speed_combo.bind(
            "<<ComboboxSelected>>", lambda e: self._on_upload_speed_changed()
        )

        def _get_upload_speed_tip():
            board_name = self.board_var.get()
            board_info = SUPPORTED_BOARDS.get(board_name, {})
            if board_info.get("platform", "") == "atmelavr":
                return "115200 is the only upload speed supported by this board."
            return None

        ToolTip(upload_spd_group, _get_upload_speed_tip)
        ToolTip(lbl_upload_spd, _get_upload_speed_tip)
        ToolTip(self.upload_speed_combo, _get_upload_speed_tip)

        # Action buttons moved to title bar

        # Right side: clear + autoscroll
        right_group = tk.Frame(ctrl_frame, bg=Theme.BG_MID)
        right_group.pack(side=tk.RIGHT)
        self._right_ctrl_group = right_group  # saved for upload spd re-packing

        tk.Label(right_group, text="OPTIONS", font=self.font_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_MID).pack(anchor=tk.E)

        opt_row = tk.Frame(right_group, bg=Theme.BG_MID)
        opt_row.pack(anchor=tk.E)

        self.autoscroll_var = tk.BooleanVar(value=True)
        self.cb_autoscroll = tk.Checkbutton(
            opt_row, text="Auto-scroll", variable=self.autoscroll_var,
            font=self.font_label, fg=Theme.TEXT, bg=Theme.BG_MID,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_MID,
            activeforeground=Theme.TEXT,
        )
        self.cb_autoscroll.pack(side=tk.LEFT, padx=(0, 8))

        self.timestamp_var = tk.BooleanVar(value=False)
        self.cb_timestamp = tk.Checkbutton(
            opt_row, text="Timestamps", variable=self.timestamp_var,
            command=self._toggle_timestamps,
            font=self.font_label, fg=Theme.TEXT, bg=Theme.BG_MID,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_MID,
            activeforeground=Theme.TEXT,
        )
        self.cb_timestamp.pack(side=tk.LEFT, padx=(0, 8))

        self.skip_compile_var = tk.BooleanVar(value=False)
        self.cb_skip_compile = tk.Checkbutton(
            opt_row, text="Skip recompile", variable=self.skip_compile_var,
            font=self.font_label, fg=Theme.TEXT, bg=Theme.BG_MID,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_MID,
            activeforeground=Theme.TEXT,
        )
        self.cb_skip_compile.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_settings = self._make_btn(
            opt_row, "⚙ Settings", self._open_settings,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_label
        )
        self.btn_settings.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_toggle_editor = self._make_btn(
            opt_row, "🗖 Hide Editor", self._toggle_editor_pane,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_label
        )
        self.btn_toggle_editor.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_toggle_monitors = self._make_btn(
            opt_row, "🗖 Hide Monitors", self._toggle_monitors_pane,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_label
        )
        self.btn_toggle_monitors.pack(side=tk.LEFT, padx=(0, 8))

        # ── Separator ──
        tk.Frame(self.root, bg=Theme.BORDER, height=1).pack(fill=tk.X)

        # ═══════════════════════════════════════════════════════
        # REDESIGNED ARDUINO-IDE STYLE LAYOUT
        # ═══════════════════════════════════════════════════════
        # style Bottom.TNotebook for dark theme
        try:
            style = ttk.Style()
            style.configure("Bottom.TNotebook",
                            background=Theme.BG_DARK,
                            borderwidth=0,
                            tabmargins=[2, 4, 0, 0])
            style.configure("Bottom.TNotebook.Tab",
                            background=Theme.BG_MID,
                            foreground=Theme.TEXT_DIM,
                            padding=[10, 4],
                            font=("Consolas", 9))
            style.map("Bottom.TNotebook.Tab",
                      background=[("selected", Theme.BG_HOVER), ("active", Theme.BG_LIGHT)],
                      foreground=[("selected", Theme.TEXT_BRIGHT), ("active", Theme.TEXT)])
        except Exception:
            pass

        try:
            raw_data = _load_raw_config()
            graphics_accel = raw_data.get("shared", {}).get("graphics_acceleration", "ON") != "OFF"
        except Exception:
            graphics_accel = True

        self.main_pane = tk.PanedWindow(
            self.root, orient=tk.VERTICAL,
            bg=Theme.BORDER, sashwidth=3, sashrelief=tk.FLAT,
            borderwidth=0,
            opaqueresize=not graphics_accel,
        )
        self.main_pane.pack(fill=tk.BOTH, expand=True)

        # ── TOP: Embedded Code Editor ──
        editor_frame = tk.Frame(self.main_pane, bg=Theme.BG_DARKEST)
        self.editor_frame = editor_frame
        self._build_editor(editor_frame)
        self.main_pane.add(editor_frame, minsize=self._editor_minsize, height=self._editor_height)

        # ── BOTTOM: Build Console + Serial Monitor Tabs ──
        bottom_frame = tk.Frame(self.main_pane, bg=Theme.BG_DARK)
        self.bottom_frame = bottom_frame
        self.bottom_notebook = ttk.Notebook(bottom_frame, style="Bottom.TNotebook")
        self.bottom_notebook.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        # ── TAB 1: Build Console ──
        build_console_frame = tk.Frame(self.bottom_notebook, bg=Theme.BG_DARKEST)

        # Build Console Header
        console_header = tk.Frame(build_console_frame, bg=Theme.BG_MID, pady=6, padx=10)
        console_header.pack(fill=tk.X)

        tk.Label(
            console_header, text="⚙ BUILD CONSOLE",
            font=tkfont.Font(family="Montserrat", size=10, weight="bold"),
            fg=Theme.CYAN, bg=Theme.BG_MID,
        ).pack(side=tk.LEFT)

        btn_clear_console = self._make_btn(
            console_header, "🗑 Clear", self._clear_console,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_mono_sm
        )
        btn_clear_console.pack(side=tk.RIGHT)

        def _copy_console():
            text = self.console.get("1.0", tk.END).strip()
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            btn_copy_console.config(text="✔ Copied!")
            self.root.after(1500, lambda: btn_copy_console.config(text="⧉ Copy"))

        btn_copy_console = self._make_btn(
            console_header, "⧉ Copy", _copy_console,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_mono_sm
        )
        btn_copy_console.pack(side=tk.RIGHT, padx=(0, 6))

        tk.Frame(build_console_frame, bg=Theme.BORDER, height=1).pack(fill=tk.X)

        self.console = tk.Text(
            build_console_frame,
            bg=Theme.BG_DARKEST,
            fg=Theme.TEXT,
            font=self.font_mono,
            insertbackground=Theme.CYAN,
            selectbackground=Theme.BG_HOVER,
            selectforeground=Theme.TEXT_BRIGHT,
            borderwidth=0,
            highlightthickness=0,
            padx=12,
            pady=8,
            wrap=tk.WORD,
            state=tk.DISABLED,
            cursor="arrow",
        )

        scrollbar = ttk.Scrollbar(
            build_console_frame, orient=tk.VERTICAL, command=self.console.yview
        )
        self.console.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Console text tags for coloring
        for widget in [self.console]:  # tag setup helper
            widget.tag_configure("info",    foreground=Theme.BLUE)
            widget.tag_configure("success", foreground=Theme.GREEN)
            widget.tag_configure("warning", foreground=Theme.YELLOW)
            widget.tag_configure("error",   foreground=Theme.RED)
            widget.tag_configure("system",  foreground=Theme.CYAN)
            widget.tag_configure("dim",     foreground=Theme.TEXT_DIM)
            widget.tag_configure("magenta", foreground=Theme.MAGENTA)
            widget.tag_configure("orange",  foreground=Theme.ORANGE)
            widget.tag_configure("bold",    font=tkfont.Font(family="Consolas", size=10, weight="bold"))
            widget.tag_configure("port_highlight", foreground="#ff3fa4",
                                 font=tkfont.Font(family="Consolas", size=10, weight="bold"))
            widget.tag_configure("header",  foreground=Theme.CYAN,
                                 font=tkfont.Font(family="Consolas", size=11, weight="bold"))
            widget.tag_configure("purple_header", foreground=Theme.PURPLE,
                                 font=tkfont.Font(family="Consolas", size=self.font_mono.actual("size"), weight="bold"))
            widget.tag_configure("purple_info", foreground=Theme.PURPLE_DIM)
            widget.tag_configure("purple_value", foreground=Theme.TEXT_BRIGHT)
            widget.tag_configure("sent",    foreground=Theme.MAGENTA)
            widget.tag_configure("timestamp", foreground=Theme.TEXT_DIM, elide=True)

        self.bottom_notebook.add(build_console_frame, text="⚡ Build Console")

        # ── TAB 2: Serial Monitor Panel ──
        serial_monitor_frame = tk.Frame(self.bottom_notebook, bg=Theme.BG_DARKEST)

        # Serial monitor header
        serial_header = tk.Frame(serial_monitor_frame, bg=Theme.BG_MID, pady=6, padx=10)
        serial_header.pack(fill=tk.X)

        tk.Label(
            serial_header, text="📡 SERIAL MONITOR",
            font=tkfont.Font(family="Montserrat", size=10, weight="bold"),
            fg=Theme.CYAN, bg=Theme.BG_MID,
        ).pack(side=tk.LEFT)

        btn_reset_mcu = self._make_btn(
            serial_header, "↺ Reset", self._reset_mcu_from_monitor,
            "#8B5E3C", "#A0724F", font=self.font_mono_sm
        )
        btn_reset_mcu.pack(side=tk.LEFT, padx=(10, 0))
        self.btn_reset_mcu = btn_reset_mcu

        # Baud rate selector in Serial Monitor tab
        self.serial_baud_group = tk.Frame(serial_header, bg=Theme.BG_MID)
        self.serial_baud_group.pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(self.serial_baud_group, text="BAUD", font=self.font_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_MID).pack(anchor=tk.W)
        self.serial_baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
        self.serial_baud_combo = ttk.Combobox(
            self.serial_baud_group, textvariable=self.serial_baud_var, width=10,
            font=self.font_mono_sm, state="readonly",
            values=["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"],
        )
        self.serial_baud_combo.pack()
        self.serial_baud_combo.bind("<<ComboboxSelected>>", lambda e: self._on_serial_baud_changed())

        btn_pause_serial = self._make_btn(
            serial_header, "⏸ Pause", self._toggle_serial_pause,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_mono_sm
        )
        btn_pause_serial.pack(side=tk.LEFT, padx=(6, 0))
        self.btn_pause_serial = btn_pause_serial

        self.serial_status = tk.Label(
            serial_header, text="● Disconnected", font=self.font_status,
            fg=Theme.RED, bg=Theme.BG_MID, anchor=tk.E,
        )
        self.serial_status.pack(side=tk.RIGHT)

        btn_clear_serial = self._make_btn(
            serial_header, "🗑 Clear", self._clear_serial_console,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_mono_sm
        )
        btn_clear_serial.pack(side=tk.RIGHT, padx=(0, 10))

        def _copy_serial():
            text = self.serial_console.get("1.0", tk.END).strip()
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            btn_copy_serial.config(text="✔ Copied!")
            self.root.after(1500, lambda: btn_copy_serial.config(text="⧉ Copy"))

        btn_copy_serial = self._make_btn(
            serial_header, "⧉ Copy", _copy_serial,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_mono_sm
        )
        btn_copy_serial.pack(side=tk.RIGHT, padx=(0, 6))

        self.ansi_clear_var = tk.BooleanVar(value=False)
        self.cb_ansi_clear = tk.Checkbutton(
            serial_header, text="Clear-screen", variable=self.ansi_clear_var,
            font=self.font_mono_sm, fg=Theme.TEXT, bg=Theme.BG_MID,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_MID,
            activeforeground=Theme.TEXT,
        )
        self.cb_ansi_clear.pack(side=tk.RIGHT, padx=(0, 10))

        tk.Frame(serial_monitor_frame, bg=Theme.CYAN_DIM, height=1).pack(fill=tk.X)

        # Serial output text
        serial_console_frame = tk.Frame(serial_monitor_frame, bg=Theme.BG_DARKEST)
        serial_console_frame.pack(fill=tk.BOTH, expand=True)

        self.serial_console = tk.Text(
            serial_console_frame,
            bg=Theme.BG_DARKEST,
            fg=Theme.TEXT,
            font=self.font_mono,
            insertbackground=Theme.CYAN,
            selectbackground=Theme.BG_HOVER,
            selectforeground=Theme.TEXT_BRIGHT,
            borderwidth=0,
            highlightthickness=0,
            padx=10,
            pady=6,
            wrap=tk.WORD,
            state=tk.DISABLED,
            cursor="arrow",
        )

        serial_scrollbar = ttk.Scrollbar(
            serial_console_frame, orient=tk.VERTICAL, command=self.serial_console.yview
        )
        self.serial_console.configure(yscrollcommand=serial_scrollbar.set)

        serial_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.serial_console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Serial console text tags
        for widget in [self.serial_console]:
            widget.tag_configure("info",    foreground=Theme.BLUE)
            widget.tag_configure("success", foreground=Theme.GREEN)
            widget.tag_configure("warning", foreground=Theme.YELLOW)
            widget.tag_configure("error",   foreground=Theme.RED)
            widget.tag_configure("system",  foreground=Theme.CYAN)
            widget.tag_configure("dim",     foreground=Theme.TEXT_DIM)
            widget.tag_configure("magenta", foreground=Theme.MAGENTA)
            widget.tag_configure("orange",  foreground=Theme.ORANGE)
            widget.tag_configure("purple_header", foreground=Theme.PURPLE,
                                 font=tkfont.Font(family="Consolas", size=self.font_mono.actual("size"), weight="bold"))
            widget.tag_configure("purple_info", foreground=Theme.PURPLE_DIM)
            widget.tag_configure("purple_value", foreground=Theme.TEXT_BRIGHT)
            widget.tag_configure("sent",    foreground=Theme.MAGENTA)
            widget.tag_configure("timestamp", foreground=Theme.TEXT_DIM, elide=True)

        # Serial input bar (inside serial monitor tab)
        tk.Frame(serial_monitor_frame, bg=Theme.BORDER, height=1).pack(fill=tk.X)

        input_frame = tk.Frame(serial_monitor_frame, bg=Theme.BG_DARK, pady=6, padx=10)
        input_frame.pack(fill=tk.X, side=tk.BOTTOM)

        tk.Label(
            input_frame, text="SEND ▸", font=self.font_label,
            fg=Theme.CYAN, bg=Theme.BG_DARK,
        ).pack(side=tk.LEFT, padx=(0, 6))

        self.serial_input = tk.Entry(
            input_frame,
            bg=Theme.BG_LIGHT,
            fg=Theme.TEXT_BRIGHT,
            font=self.font_mono,
            insertbackground=Theme.CYAN,
            selectbackground=Theme.CYAN_DIM,
            borderwidth=0,
            highlightthickness=1,
            highlightcolor=Theme.CYAN_DIM,
            highlightbackground=Theme.BORDER,
        )
        self.serial_input.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6), ipady=4)
        self.serial_input.bind("<Return>", self._send_serial)

        # Console click -> focus input, hold/drag -> select text
        _serial_press = {"time": 0.0, "x": 0, "y": 0, "timer": None}

        def _on_serial_console_click(event):
            self.serial_console.focus_set()
            if _serial_press["timer"] is not None:
                try:
                    self.serial_console.after_cancel(_serial_press["timer"])
                except Exception:
                    pass
                _serial_press["timer"] = None
            _serial_press["time"] = time.time()
            _serial_press["x"] = event.x
            _serial_press["y"] = event.y

        def _on_serial_console_release(event):
            dt = time.time() - _serial_press["time"]
            dx = abs(event.x - _serial_press["x"])
            dy = abs(event.y - _serial_press["y"])

            if dx > 4 or dy > 4 or dt > 0.25:
                return

            def _redirect_focus():
                _serial_press["timer"] = None
                try:
                    if bool(self.serial_console.tag_ranges("sel")):
                        return
                    self.serial_input.focus_set()
                except Exception:
                    pass

            if _serial_press["timer"] is not None:
                try:
                    self.serial_console.after_cancel(_serial_press["timer"])
                except Exception:
                    pass

            _serial_press["timer"] = self.serial_console.after(180, _redirect_focus)

        self.serial_console.bind("<Button-1>", _on_serial_console_click, add="+")
        self.serial_console.bind("<ButtonRelease-1>", _on_serial_console_release, add="+")

        self.line_ending_var = tk.StringVar(value="\\r\\n")
        le_combo = ttk.Combobox(
            input_frame, textvariable=self.line_ending_var, width=8,
            font=self.font_mono_sm, state="readonly",
            values=["None", "\\n", "\\r", "\\r\\n"],
        )
        set_combobox_direction(le_combo, "above")
        le_combo.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_send = self._make_btn(input_frame, "Send", self._send_serial,
                                        Theme.BTN_MONITOR, Theme.BTN_MONITOR_H)
        self.btn_send.pack(side=tk.LEFT)

        self.bottom_notebook.add(serial_monitor_frame, text="📡 Serial Monitor")

        self.main_pane.add(bottom_frame, minsize=self._bottom_minsize, height=self._bottom_height)

        # ── Editor / Monitors show-hide state ──
        self.editor_pane_visible = True
        self.monitors_pane_visible = True
        self._update_pane_toggle_buttons()

        # ── Status Bar ──
        tk.Frame(self.root, bg=Theme.BORDER, height=1).pack(fill=tk.X)

        status_frame = tk.Frame(self.root, bg=Theme.BG_DARK, pady=4, padx=12)
        status_frame.pack(fill=tk.X)

        self.status_label = tk.Label(
            status_frame, text="Ready", font=self.font_status,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARK, anchor=tk.W,
        )
        self.status_label.pack(side=tk.LEFT)

        # Style ttk widgets (comboboxes, scrollbars)
        style = ttk.Style()
        setup_combobox_place_popdown(self.root)
        style.configure("TCombobox",
                         fieldbackground=Theme.BG_LIGHT,
                         background=Theme.BG_HOVER,
                         foreground=Theme.TEXT_BRIGHT,
                         selectbackground=Theme.CYAN_DIM,
                         selectforeground=Theme.TEXT_BRIGHT,
                         bordercolor=Theme.BORDER,
                         arrowcolor=Theme.TEXT_DIM)
        style.map("TCombobox",
                   fieldbackground=[("readonly", Theme.BG_LIGHT)],
                   selectbackground=[("readonly", Theme.CYAN_DIM)],
                   selectforeground=[("readonly", Theme.TEXT_BRIGHT)])

        self.root.option_add("*TCombobox*Listbox.justify", "left")
        self.root.option_add("*Combobox*Listbox.justify", "left")
        self.root.option_add("*Listbox.justify", "left")

        style.configure("Vertical.TScrollbar",
                        background=Theme.BG_MID,
                        troughcolor=Theme.BG_DARKEST,
                        bordercolor=Theme.BG_DARKEST,
                        arrowcolor=Theme.TEXT_DIM,
                        lightcolor=Theme.BG_MID,
                        darkcolor=Theme.BG_MID)
        style.map("Vertical.TScrollbar",
                  background=[("active", Theme.BORDER_LIT)])

        # Welcome message
        self._print_welcome()

    def _make_btn(self, parent, text, command, bg, bg_hover, width=None, font=None) -> tk.Button:
        """Create a styled flat button."""
        btn_font = font if font is not None else self.font_btn
        btn = tk.Button(
            parent, text=text, command=command,
            font=btn_font, fg=Theme.TEXT_BRIGHT, bg=bg,
            activebackground=bg_hover, activeforeground=Theme.TEXT_BRIGHT,
            relief=tk.FLAT, borderwidth=0, padx=self._btn_padx, pady=self._btn_pady, cursor="hand2",
        )
        if width:
            btn.configure(width=width)
        btn.bind("<Enter>", lambda e, b=btn, c=bg_hover: b.configure(bg=c))
        btn.bind("<Leave>", lambda e, b=btn, c=bg: b.configure(bg=c))
        self._scalable_buttons.append(btn)
        return btn

    def _make_icon_btn(self, parent, text, command, bg, bg_hover, width=3) -> tk.Button:
        """Create a small icon button."""
        btn = tk.Button(
            parent, text=text, command=command,
            font=self.font_btn, fg=Theme.TEXT, bg=bg,
            activebackground=bg_hover, activeforeground=Theme.TEXT_BRIGHT,
            relief=tk.FLAT, borderwidth=0, width=width, cursor="hand2",
        )
        btn.bind("<Enter>", lambda e, b=btn, c=bg_hover: b.configure(bg=c))
        btn.bind("<Leave>", lambda e, b=btn, c=bg: b.configure(bg=c))
        return btn

    def _on_root_configure(self, event):
        """Debounced handler for live window-resize rescaling of buttons."""
        if event.widget is not self.root:
            return
        if getattr(self, "_resize_after_id", None):
            try:
                self.root.after_cancel(self._resize_after_id)
            except Exception:
                pass
        self._resize_after_id = self.root.after(150, self._apply_dynamic_button_scale)

    def _apply_dynamic_button_scale(self):
        """Shrink (or restore) button font/padding based on the current
        window width, on top of the static screen-resolution scale computed
        at startup. Lets buttons keep auto-resizing as the user resizes the
        window, not just once when the app launches."""
        self._resize_after_id = None
        try:
            width = self.root.winfo_width()
        except Exception:
            return
        if width <= 1:
            return

        # How much the current window width has shrunk relative to a
        # reasonable reference width for this screen.
        ref_width = max(900, min(self.screen_w, 1920))
        dyn_factor = max(0.75, min(1.0, width / ref_width))
        scale = max(0.6, min(1.0, self._ui_scale * dyn_factor))

        if abs(scale - self._last_applied_scale) < 0.03:
            return  # avoid churn for tiny resize deltas
        self._last_applied_scale = scale

        new_btn_size = max(7, round(9 * scale))
        self.font_btn.configure(size=new_btn_size)

        self._btn_padx = max(6, round(10 * scale))
        self._btn_pady = max(2, round(3 * scale))
        for btn in self._scalable_buttons:
            try:
                btn.configure(padx=self._btn_padx, pady=self._btn_pady)
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    # CONSOLE OUTPUT
    # ──────────────────────────────────────────────────────────
    def _toggle_timestamps(self):
        show = self.timestamp_var.get()
        self.console.tag_configure("timestamp", elide=not show)
        self.serial_console.tag_configure("timestamp", elide=not show)
        if self.autoscroll_var.get():
            self.console.see(tk.END)
            self.serial_console.see(tk.END)

    def _append(self, text: str, tag: str = "", newline: bool = True):
        """Append text to console (thread-safe)."""
        def _do():
            self.console.configure(state=tk.NORMAL)
            if newline and text.strip():
                ts = datetime.now().strftime("%H:%M:%S")
                self.console.insert(tk.END, f"[{ts}] ", "timestamp")
            self.console.insert(tk.END, text + ("\n" if newline else ""), tag)
            total_lines = int(self.console.index("end-1c").split(".")[0])
            if total_lines > 2000:
                self.console.delete("1.0", f"{total_lines - 2000 + 1}.0")
            self.console.configure(state=tk.DISABLED)
            if self.autoscroll_var.get():
                self.console.see(tk.END)
        self.root.after(0, _do)

    def _append_segments(self, segments, newline: bool = True):
        """Append a single line built from multiple (text, tag) segments,
        e.g. a dim label followed by a bright value, sharing one timestamp.
        Thread-safe, mirrors _append()."""
        def _do():
            self.console.configure(state=tk.NORMAL)
            has_content = any(seg_text.strip() for seg_text, _ in segments)
            if newline and has_content:
                ts = datetime.now().strftime("%H:%M:%S")
                self.console.insert(tk.END, f"[{ts}] ", "timestamp")
            for seg_text, seg_tag in segments:
                self.console.insert(tk.END, seg_text, seg_tag)
            if newline:
                self.console.insert(tk.END, "\n")
            total_lines = int(self.console.index("end-1c").split(".")[0])
            if total_lines > 2000:
                self.console.delete("1.0", f"{total_lines - 2000 + 1}.0")
            self.console.configure(state=tk.DISABLED)
            if self.autoscroll_var.get():
                self.console.see(tk.END)
        self.root.after(0, _do)

    def _console_box_columns(self, min_cols: int = 50, default_cols: int = 100) -> int:
        """Return how many monospace character columns actually fit on one
        visual line of the build console right now, so boxed panels (like
        the chip info panel) can size themselves to the window instead of
        being cut mid-line by the console's own word-wrap.

        Accounts for the console's left/right padding and the width of the
        "[HH:MM:SS] " timestamp prefix that _append()/_append_segments()
        insert before every line. Falls back to default_cols if the widget
        hasn't been drawn yet (winfo_width() not yet meaningful)."""
        try:
            self.console.update_idletasks()
            widget_px = self.console.winfo_width()
            if widget_px <= 1:
                return default_cols
            padx = int(self.console.cget("padx") or 0)
            char_px = self.font_mono.measure("0")
            if char_px <= 0:
                return default_cols
            ts_px = self.font_mono.measure("[00:00:00] ")
            usable_px = widget_px - (padx * 2) - ts_px - 4  # small safety margin
            cols = usable_px // char_px
            return max(int(cols), min_cols)
        except Exception:
            return default_cols

    def _append_progress(self, text: str, tag: str = ""):
        """Append progress line, replacing the previous progress line if it exists to avoid scrolling spam."""
        def _do():
            self.console.configure(state=tk.NORMAL)
            # Check the last line content (excluding trailing newline)
            last_line = self.console.get("end-2c linestart", "end-2c lineend")
            if "downloading [" in last_line.lower() or "unpacking [" in last_line.lower():
                # Get the timestamp from the last line (which would be at start: e.g., "[15:22:35] ")
                ts_match = re.match(r'^\[\d+:\d+:\d+\]\s*', last_line)
                ts_prefix = ts_match.group(0) if ts_match else ""
                
                # Delete the last line
                self.console.delete("end-2c linestart", "end-1c")
                # Insert again with the same timestamp prefix
                self.console.insert(tk.END, ts_prefix, "timestamp")
                self.console.insert(tk.END, text + "\n", tag)
            else:
                # Normal append with new timestamp
                ts = datetime.now().strftime("%H:%M:%S")
                self.console.insert(tk.END, f"[{ts}] ", "timestamp")
                self.console.insert(tk.END, text + "\n", tag)
            total_lines = int(self.console.index("end-1c").split(".")[0])
            if total_lines > 2000:
                self.console.delete("1.0", f"{total_lines - 2000 + 1}.0")
            self.console.configure(state=tk.DISABLED)
            if self.autoscroll_var.get():
                self.console.see(tk.END)
        self.root.after(0, _do)

    def _append_serial(self, text: str, tag: str = "", newline: bool = True, is_start: bool = True):
        """Append text to the serial monitor panel (thread-safe).
        If is_start is True and newline is True, prepend a timestamp.
        If is_start is False, this is a continuation of a partial line.
        """
        def _do():
            self.serial_console.configure(state=tk.NORMAL)
            if is_start and newline and text.strip():
                ts = datetime.now().strftime("%H:%M:%S")
                self.serial_console.insert(tk.END, f"[{ts}] ", "timestamp")
            self.serial_console.insert(tk.END, text + ("\n" if newline else ""), tag)
            total_lines = int(self.serial_console.index("end-1c").split(".")[0])
            if total_lines > 1500:
                self.serial_console.delete("1.0", f"{total_lines - 1500 + 1}.0")
            self.serial_console.configure(state=tk.DISABLED)
            if self.autoscroll_var.get():
                self.serial_console.see(tk.END)
        self.root.after(0, _do)

    def _append_tagged_line(self, line: str, is_newline: bool = True):
        """Parse and color-code a serial monitor line → serial panel.
        If is_newline is True, the line ends with a newline. If False, it's
        a partial line (e.g., progress dots) that should be displayed immediately.
        """
        low = line.lower()
        if "error" in low or "fatal" in low or "fail" in low:
            tag = "error"
        elif "warning" in low or "warn" in low:
            tag = "warning"
        elif any(k in low for k in ["ok", "success", "done", "ready", "established"]):
            tag = "success"
        elif "[debug]" in low:
            tag = "dim"
        elif line.startswith("[") and "]" in line:
            tag = "system"
        else:
            tag = ""
        self._append_serial(line, tag, newline=is_newline, is_start=is_newline)

    def _clear_console(self):
        self.console.configure(state=tk.NORMAL)
        self.console.delete("1.0", tk.END)
        self.console.configure(state=tk.DISABLED)

    def _clear_serial_console(self):
        def _do():
            self.serial_console.configure(state=tk.NORMAL)
            self.serial_console.delete("1.0", tk.END)
            self.serial_console.configure(state=tk.DISABLED)
        self.root.after(0, _do)

    def _toggle_serial_pause(self):
        """Pause/resume the Serial Monitor's live display. While paused, the
        port keeps being read in the background (so nothing backs up or
        drops the connection) — only the printing to the panel is held
        back. Anything that arrives while paused is not queued or replayed;
        it simply isn't shown, and display picks back up on resume."""
        self._monitor_paused = not self._monitor_paused
        if self._monitor_paused:
            self.btn_pause_serial.config(text="▶ Resume")
            self._append_serial("  ⏸ Serial monitoring paused — the port is still connected.", "dim")
        else:
            self.btn_pause_serial.config(text="⏸ Pause")
            self._append_serial("  ▶ Serial monitoring resumed.", "dim")

    def _send_serial(self, event=None):
        """Send the text in the serial input box to the connected board,
        appending the selected line ending, then clear the input box."""
        text = self.serial_input.get()
        if not text:
            return

        if not (self.serial_conn and self.serial_conn.is_open) or not self.serial_running:
            self._append_serial("  ✖ Not connected — open the Serial Monitor first.", "error")
            return

        ending_map = {
            "None": "",
            "\\n": "\n",
            "\\r": "\r",
            "\\r\\n": "\r\n",
        }
        ending = ending_map.get(self.line_ending_var.get(), "\r\n")

        try:
            self.serial_conn.write((text + ending).encode("utf-8", errors="replace"))
            self._append_serial(f"  » {text}", "dim")
        except Exception as exc:
            self._append_serial(f"  ✖ Send failed: {exc}", "error")
            return

        self.serial_input.delete(0, tk.END)

    def _set_status(self, text: str, color: str = Theme.TEXT_DIM):
        def _do():
            self.status_label.configure(text=text, fg=color)
        self.root.after(0, _do)

    def _set_serial_status(self, connected: bool):
        def _do():
            if connected:
                self.serial_status.configure(text="● Connected", fg=Theme.GREEN)
            else:
                self.serial_status.configure(text="● Disconnected", fg=Theme.RED)
        self.root.after(0, _do)

    def _set_buttons_busy(self, busy: bool):
        """Disable/enable action buttons during operations (legacy helper).
        Delegates to _set_buttons_state with operation='any'."""
        self._set_buttons_state(busy, operation="any")

    def _set_buttons_state(self, busy: bool, operation: str = "any"):
        self._active_operation = operation if busy else None
        def _do():
            if busy:
                
                self.btn_compile.configure(state=tk.DISABLED)
                self.btn_upload.configure(state=tk.DISABLED)
                self.btn_new_project.configure(state=tk.DISABLED)
                self.btn_settings.configure(state=tk.DISABLED)
                if hasattr(self, "btn_reset_mcu") and self.btn_reset_mcu:
                    try:
                        if operation in ("upload", "flash", "reset"):
                            self.btn_reset_mcu.configure(state=tk.DISABLED)
                        else:
                            can_reset = bool(self.board_var.get()) and self._is_board_recognized()
                            self.btn_reset_mcu.configure(state=tk.NORMAL if can_reset else tk.DISABLED)
                    except Exception:
                        pass

                self.board_combo.configure(state="disabled")
                self.port_combo.configure(state="disabled")
                self.baud_combo.configure(state="disabled")
                self.upload_speed_combo.configure(state="disabled")
                self.btn_clean.configure(state=tk.DISABLED)
                
                if operation in ("upload", "reset"):
                    self.btn_stop.configure(state=tk.DISABLED)
                    if hasattr(self, "bottom_notebook"):
                        self.bottom_notebook.select(0)
                        self.bottom_notebook.tab(1, state="disabled")
                    # Both write directly to flash — Hard Reset even rewrites
                    # the bootloader. An interrupted close mid-write is how
                    # boards get bricked, so lock the window shut for real.
                    self._set_window_closable(False)
                else:
                    self.btn_stop.configure(state=tk.NORMAL)
                    if hasattr(self, "bottom_notebook"):
                        self.bottom_notebook.tab(1, state="normal")

                self.lbl_sketch.configure(cursor="arrow")

                # Visual label so the user knows which op is active
                if operation == "compile":
                    self.btn_compile.configure(text="⚙ Compiling...")
                    self.btn_upload.configure(text="⡿ Upload")
                elif operation == "upload":
                    self.btn_compile.configure(text="⚙ Compile")
                    self.btn_upload.configure(text="⡿ Uploading...")
                elif operation == "reset":
                    self.btn_compile.configure(text="⚙ Compile")
                    self.btn_upload.configure(text="⡿ Resetting...")
            else:
                self._framework_download_active = False
                self.btn_compile.configure(state=tk.NORMAL, text="⚙ Compile")
                self.btn_upload.configure(state=tk.NORMAL, text="⡿ Upload")
                self.btn_new_project.configure(state=tk.NORMAL)
                self.btn_settings.configure(state=tk.NORMAL)
                self.btn_stop.configure(state=tk.DISABLED, text="■ Stop")
                if hasattr(self, "btn_reset_mcu") and self.btn_reset_mcu:
                    try:
                        can_reset = bool(self.board_var.get()) and self._is_board_recognized()
                        self.btn_reset_mcu.configure(state=tk.NORMAL if can_reset else tk.DISABLED)
                    except Exception:
                        pass
                if hasattr(self, "bottom_notebook"):
                    # If the tab was disabled (i.e. we just finished an
                    # upload or reset that locked it), decide where the
                    # selection should land now that it's unlocking:
                    #   - if something requested a specific tab (e.g. a
                    #     successful upload wants to jump to Serial Monitor),
                    #     honor that one-shot request.
                    #   - otherwise force it back to Build Console explicitly
                    #     rather than trusting Tk to leave the current
                    #     selection alone when a previously-disabled tab
                    #     flips back to "normal".
                    was_locked = self.bottom_notebook.tab(1, "state") == "disabled"
                    self.bottom_notebook.tab(1, state="normal")
                    target_tab = self._focus_tab_on_unlock
                    self._focus_tab_on_unlock = None  # one-shot, always consume
                    if target_tab is not None:
                        self.bottom_notebook.select(target_tab)
                    elif was_locked:
                        self.bottom_notebook.select(0)
                
                # Re-enable board/ports/baud selection
                self.board_combo.configure(state="readonly")
                self.port_combo.configure(state="readonly")
                self.baud_combo.configure(state="readonly")
                
                # If board is AVR, keep upload speed combo disabled, else readonly
                board_name = self.board_var.get()
                board_info = SUPPORTED_BOARDS.get(board_name, {})
                is_avr = (board_info.get("platform", "") == "atmelavr")
                if is_avr:
                    self.upload_speed_combo.configure(state="disabled")
                else:
                    self.upload_speed_combo.configure(state="readonly")
                    
                self.btn_clean.configure(state=tk.NORMAL)
                
                self.lbl_sketch.configure(cursor="hand2")
                
                # Always safe to restore closability once we're back to idle
                self._set_window_closable(True)

                # The block above unconditionally re-enabled Compile/Upload;
                # re-apply the board-selected (and, for Upload, hardware-
                # recognized) gating now that is_busy is back to False.
                self._update_hardware_action_buttons()
                
        self.root.after(0, _do)

    def _toggle_editor_pane(self):
        """Show/hide the embedded code editor pane. When hidden, the
        Monitors pane (Build/Serial notebook) expands to fill the space.

        Both toggle buttons stay enabled at all times. If Editor is the only
        pane currently visible (Monitors already hidden), hiding it swaps
        panes instead of being blocked: Editor hides and Monitors reappears,
        so the window is never left blank."""
        if self.editor_pane_visible:
            self.main_pane.forget(self.editor_frame)
            self.editor_pane_visible = False
            self.btn_toggle_editor.configure(text="🗖 Show Editor")
            self._set_embedded_editor_visible(False)
            if not self.monitors_pane_visible:
                # Editor was the last visible pane — bring Monitors back
                # so the window never goes blank.
                self.main_pane.add(self.bottom_frame, minsize=self._bottom_minsize, height=self._bottom_height)
                self.monitors_pane_visible = True
                self.btn_toggle_monitors.configure(text="🗖 Hide Monitors")
        else:
            if self.monitors_pane_visible:
                self.main_pane.add(self.editor_frame, before=self.bottom_frame,
                                    minsize=self._editor_minsize, height=self._editor_height)
            else:
                self.main_pane.add(self.editor_frame, minsize=self._editor_minsize, height=self._editor_height)
            self.editor_pane_visible = True
            self.btn_toggle_editor.configure(text="🗖 Hide Editor")
            self._set_embedded_editor_visible(True)
        self._update_pane_toggle_buttons()

    def _toggle_monitors_pane(self):
        """Show/hide the Monitors pane (Build Console / Serial Monitor
        notebook). When hidden, the code editor expands to fill the space.

        Both toggle buttons stay enabled at all times. If Monitors is the
        only pane currently visible (Editor already hidden), hiding it swaps
        panes instead of being blocked: Monitors hides and Editor reappears,
        so the window is never left blank."""
        if self.monitors_pane_visible:
            self.main_pane.forget(self.bottom_frame)
            self.monitors_pane_visible = False
            self.btn_toggle_monitors.configure(text="🗖 Show Monitors")
            if not self.editor_pane_visible:
                # Monitors was the last visible pane — bring Editor back
                # so the window never goes blank.
                self.main_pane.add(self.editor_frame, minsize=self._editor_minsize, height=self._editor_height)
                self.editor_pane_visible = True
                self.btn_toggle_editor.configure(text="🗖 Hide Editor")
                self._set_embedded_editor_visible(True)
        else:
            if self.editor_pane_visible:
                self.main_pane.add(self.bottom_frame, after=self.editor_frame,
                                    minsize=self._bottom_minsize, height=self._bottom_height)
            else:
                self.main_pane.add(self.bottom_frame, minsize=self._bottom_minsize, height=self._bottom_height)
            self.monitors_pane_visible = True
            self.btn_toggle_monitors.configure(text="🗖 Hide Monitors")
        self._update_pane_toggle_buttons()

    def _update_pane_toggle_buttons(self):
        """Both toggle buttons remain clickable at all times — neither pane
        can ever be permanently locked out, since hiding the last remaining
        pane now swaps to the other one instead of being blocked."""
        self.btn_toggle_editor.configure(state=tk.NORMAL)
        self.btn_toggle_monitors.configure(state=tk.NORMAL)

    def _print_welcome(self):
        self._append("=" * 56, "header")
        self._append("⚡ MCU Flasher by Naph — ESP32 Upload & Monitor (PIO)", "header")
        self._append("=" * 56, "header")
        self._append("")
        # Kick off a project-folder scan immediately so the user sees
        # detected libs, ini status, and source files right on startup.
        self._on_folder_changed()
        # Auto-start serial monitor if a port was detected
        self._schedule_auto_start_monitor(1000)

    def _run_bg_task(self, task_func, *args, on_success=None, on_error=None):
        """Submit a task to the central ThreadPoolExecutor.
        Executes when CPU is ready, keeping the UI thread 100% smooth.
        Callbacks are marshaled back to the main UI thread via root.after().
        """
        def _worker():
            try:
                result = task_func(*args)
                if on_success and callable(on_success):
                    if hasattr(self, "root") and self.root and self.root.winfo_exists():
                        self.root.after(0, lambda: on_success(result))
                return result
            except Exception as exc:
                if on_error and callable(on_error):
                    if hasattr(self, "root") and self.root and self.root.winfo_exists():
                        self.root.after(0, lambda: on_error(exc))
                return None

        if hasattr(self, "_bg_executor") and self._bg_executor:
            try:
                return self._bg_executor.submit(_worker)
            except Exception:
                pass
        import threading
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

    # ──────────────────────────────────────────────────────────
    # PORT MANAGEMENT
    # ──────────────────────────────────────────────────────────
    def _save_selected_port(self, port_name: str | None):
        """Save the currently selected COM port in the instance config asynchronously on thread pool."""
        def _do_save():
            try:
                config = load_gui_config()
                config["selected_port"] = port_name or ""
                save_gui_config(config)
            except Exception:
                pass
        self._run_bg_task(_do_save)

    def _refresh_ports(self, force_select_port=None, called_from_hotplug=False):
        """Scan serial ports and update the combobox, hiding ports that
        another live instance already has selected.
        If force_select_port is specified, it will select that port immediately.
        """
        ports = serial.tools.list_ports.comports()

        # Computed once and reused for the whole pass, so the dropdown
        # contents, current-selection check, and auto-select fallback all
        # agree on the same snapshot of what's taken. This instance's own
        # claimed port is never in here (get_occupied_ports() excludes
        # _INSTANCE_ID), so it can never hide itself from its own dropdown.
        occupied_ports = get_occupied_ports()
        visible_ports = []
        for p in ports:
            if p.device in occupied_ports:
                continue
            desc = (p.description or "").lower()
            hwid = (p.hwid or "").lower()
            
            # Filter out standard Bluetooth serial links to avoid COM name collision/overlap
            if "bluetooth" in desc or "bthenum" in hwid:
                continue
            visible_ports.append(p)

        port_list = [f"{p.device}  -  {p.description or ''}" for p in visible_ports]

        # Update last-known port set for hotplug detection. This tracks the
        # *physical* device set (not the occupancy-filtered view) using (device, hwid)
        # tuples so that port name collisions (e.g. sharing COM3) are detected properly.
        self._last_known_ports = {(p.device, p.hwid or "") for p in ports}

        self.port_combo["values"] = port_list

        # If the dropdown popup is currently visible AND we are handling a hotplug event,
        # dismiss and re-post it so the user sees the updated list without having to close/reopen.
        if called_from_hotplug:
            try:
                popdown_name = self.port_combo.tk.call("ttk::combobox::PopdownWindow", str(self.port_combo))
                popdown = self.root.nametowidget(popdown_name)
                if popdown.winfo_ismapped():
                    self.port_combo.event_generate('<Escape>')
                    self.root.after(30, lambda: self.port_combo.event_generate('<Button-1>'))
            except Exception:
                pass

        # Check if the current selection is still connected, visible, and valid
        current_val = self.port_var.get()
        current_device = ""
        if current_val:
            match = re.match(r"(COM\d+|/dev/\S+)", current_val)
            current_device = match.group(1) if match else current_val.split()[0]

        # If a force_select_port is requested (e.g. newly plugged known MCU on hotplug), select it immediately
        if force_select_port:
            for p in visible_ports:
                if p.device == force_select_port:
                    target_val = f"{p.device}  -  {p.description or ''}"
                    self.port_combo.set(target_val)
                    self._save_selected_port(p.device)
                    self._board_port_confirmed = False
                    self._update_hardware_action_buttons()
                    self._auto_select_board(show_msg=False)
                    if p.device and not self._port_is_avr_only():
                        threading.Thread(
                            target=self._auto_detect_board_from_port,
                            args=(p.device,),
                            daemon=True,
                        ).start()
                    return

        is_current_valid = False
        if current_val and current_val in port_list:
            if current_device.upper() == "COM1":
                # Check if there is another unoccupied port that is an MCU.
                # If so, we bypass the COM1 selection to auto-select the MCU instead.
                has_other_mcu = False
                mcu_keywords = ["cp210", "ch34", "ch91", "ftdi", "esp32", "silicon labs", "wch", "jtag", "usb bridge", "usb", "serial", "arduino", "mcu"]
                for p in visible_ports:
                    if p.device.upper() != "COM1":
                        combined = f"{p.description} {p.hwid}".lower()
                        if any(kw in combined for kw in mcu_keywords):
                            has_other_mcu = True
                            break
                if not has_other_mcu:
                    is_current_valid = True
            else:
                is_current_valid = True

        if is_current_valid:
            # Current selection is still valid, keep it and update config
            self._save_selected_port(current_device)
            self._auto_select_board(show_msg=False)
            # Always let esptool have the final word unless the port's USB
            # chip is one that can never be an ESP board (e.g. CH340/CH341).
            # Do NOT gate this on the heuristic's board guess — that guess is
            # exactly what the probe needs to be able to correct.
            if current_device and not self._port_is_avr_only():
                threading.Thread(
                    target=self._auto_detect_board_from_port,
                    args=(current_device,),
                    daemon=True,
                ).start()
            return

        # If the current selection is invalid, empty, or was just hidden
        # because another window claimed it, select a new one from what's
        # still visible:
        unoccupied_ports = visible_ports

        mcu_keywords = ["cp210", "ch34", "ch91", "ftdi", "esp32", "silicon labs", "wch", "jtag", "usb bridge", "usb", "serial", "arduino", "mcu"]
        auto_port = None
        auto_port_device = None

        # 1. Search for MCU port first (excluding COM1)
        for p in unoccupied_ports:
            if p.device.upper() == "COM1":
                continue
            combined = f"{p.description} {p.hwid}".lower()
            if any(kw in combined for kw in mcu_keywords):
                auto_port = f"{p.device}  —  {p.description}"
                auto_port_device = p.device
                break

        # 2. Search for any non-COM1 port if no MCU port found
        if not auto_port:
            for p in unoccupied_ports:
                if p.device.upper() != "COM1":
                    auto_port = f"{p.device}  —  {p.description}"
                    auto_port_device = p.device
                    break

        # 3. Fall back to COM1 if it is the only unoccupied port
        if not auto_port:
            for p in unoccupied_ports:
                if p.device.upper() == "COM1":
                    auto_port = f"{p.device}  —  {p.description}"
                    auto_port_device = p.device
                    break

        if auto_port:
            self.port_combo.set(auto_port)
            self._save_selected_port(auto_port_device)
        else:
            self.port_combo.set("")
            self._save_selected_port("")

        # A different (or no) port just got selected — any prior "known
        # board" confirmation belonged to the old port and no longer
        # applies (hotplug swap case).
        self._board_port_confirmed = False
        self._update_hardware_action_buttons()

        # Run auto-selection based on the selected port
        self._auto_select_board(show_msg=False)

        # Kick off chip auto-detection if we auto-selected an unoccupied port.
        # Same rule as above: only skip the probe for confirmed-AVR-only chips.
        final_device = auto_port_device or (unoccupied_ports[0].device if unoccupied_ports else None)
        if final_device and not self._port_is_avr_only():
            threading.Thread(
                target=self._auto_detect_board_from_port,
                args=(final_device,),
                daemon=True,
            ).start()

    def _start_port_polling(self):
        """Start background thread to poll USB plug/unplug, and separately
        detect when another instance claims or releases a port, every 2 seconds."""
        self._last_known_occupied_ports: set[str] = get_occupied_ports()

        def _poll_thread_worker():
            while True:
                try:
                    time.sleep(0.8)
                    current_ports = {(p.device, p.hwid or "") for p in serial.tools.list_ports.comports()}
                    hardware_changed = current_ports != self._last_known_ports

                    current_occupied = get_occupied_ports()
                    occupancy_changed = current_occupied != self._last_known_occupied_ports

                    if hardware_changed or occupancy_changed:
                        try:
                            self.root.after(0, self._handle_port_change, current_ports, current_occupied)
                        except Exception:
                            pass
                except Exception as e:
                    try:
                        self.root.after(0, lambda: self._append(f"  ✖ Port poll thread error: {e}", "error"))
                    except Exception:
                        pass

        threading.Thread(target=_poll_thread_worker, daemon=True).start()

    def _handle_port_change(self, current_ports, current_occupied):
        """Handle serial port additions, removals, and occupancy changes on the main Tk thread."""
        try:
            old = self._last_known_ports  # Set of (device, hwid)
            
            # Map them back to devices to detect added/removed COM ports
            old_devices = {dev for dev, hwid in old}
            new_devices = {dev for dev, hwid in current_ports}
            
            added_devs = new_devices - old_devices
            removed_devs = old_devices - new_devices

            # Check if any of the added devices is a known MCU
            has_new_known_mcu = False
            new_mcu_device = None
            if added_devs:
                mcu_keywords = ["cp210", "ch34", "ch91", "ftdi", "esp32", "silicon labs", "wch", "jtag", "usb bridge", "usb", "serial", "arduino", "mcu"]
                ports_info = serial.tools.list_ports.comports()
                for p in ports_info:
                    if p.device in added_devs:
                        combined = f"{p.description} {p.hwid}".lower()
                        if any(kw in combined for kw in mcu_keywords):
                            has_new_known_mcu = True
                            new_mcu_device = p.device
                            break
            
            self._last_known_occupied_ports = current_occupied
            self._last_known_ports = current_ports

            if added_devs:
                for p in added_devs:
                    self._append(f"  🔌 USB device connected: {p}", "success")
            if removed_devs:
                for p in removed_devs:
                    self._append(f"  ⚠ USB device disconnected: {p}", "warning")

            # Auto-switch to newly connected MCU only if current port is not recognized
            current_port = self._get_port()
            is_recognized = getattr(self, "_board_port_confirmed", False) and current_port and current_port.upper() != "COM1"
            
            force_select_port = None
            if has_new_known_mcu and not is_recognized:
                force_select_port = new_mcu_device

            self._refresh_ports(force_select_port=force_select_port, called_from_hotplug=True)

            # If a new device appeared, auto-start monitor on it
            if added_devs and not self.serial_running:
                self._schedule_auto_start_monitor(500)

            # If the currently-monitored port was removed, stop monitor
            if removed_devs and self.serial_running:
                current_port = self._get_port()
                if current_port in removed_devs:
                    self.serial_running = False
        except Exception as e:
            self._append(f"  ✖ Error handling port change: {e}", "error")

    def _start_marquee(self):
        try:
            if self.port_combo.winfo_exists():
                pos = self.port_combo.xview()
                
                # If text fits perfectly, do nothing and reset pause
                if pos[0] == 0.0 and pos[1] >= 0.999:
                    self._marquee_dir = 1
                    self._marquee_pause = 0
                else:
                    if getattr(self, '_marquee_pause', 0) > 0:
                        self._marquee_pause -= 1
                    else:
                        if pos[1] >= 0.999:
                            self._marquee_dir = -1
                            self._marquee_pause = 8  # pause at end
                        elif pos[0] <= 0.0:
                            self._marquee_dir = 1
                            self._marquee_pause = 8  # pause at start
                            
                        self.port_combo.xview_scroll(self._marquee_dir, "units")
        except Exception:
            pass
        self.root.after(150, self._start_marquee)

    # ── Custom filterable dropdown (replaces ttk.Combobox for board select) ──
    #
    # Three rounds of patching ttk::combobox::Post's focus-grab timing
    # (ismapped-guard, then debounce, then after_idle refocus) produced
    # zero observable change to the reported symptom -- not "improved",
    # not "different", literally unchanged each time. That's a strong
    # signal the theory of the mechanism was wrong, not that the next
    # timing tweak will land. Compounding that: there's no tkinter or
    # network access in the environment these fixes were authored in, so
    # every one of those three attempts was reasoning about Tk's internal
    # event queue without ever being able to watch it run -- exactly the
    # situation where switching strategy beats a fourth guess.
    #
    # This replaces ttk.Combobox + Post entirely with a plain tk.Entry
    # (for typing) and a manually-managed tk.Toplevel popup (for the
    # filtered list), wired with ordinary Python event bindings only.
    # There is no Post call anywhere in this replacement, so Post's
    # internal focus grab -- whatever it actually does on this platform,
    # which was never confirmed -- cannot run, and cannot fight for focus
    # with the entry. The class still drives self.board_var (a
    # tk.StringVar) on every change and still calls _on_board_changed()
    # on confirmed selection, so all ~25 other call sites in this file
    # that read self.board_var.get() need no changes.
    def _build_board_dropdown(self, parent):
        """Constructs the custom board selector and assigns self.board_combo / self.board_var."""
        # Start unselected — the user must explicitly pick a board. Previously
        # this auto-picked "Arduino Uno" (or "ESP32 Dev Module") on startup,
        # which silently locked in a board the user hadn't actually chosen
        # yet and could lead to compiling/uploading against the wrong board
        # before they'd even looked at the dropdown.
        initial_board = ""

        self.board_var = tk.StringVar(value=initial_board)
        self._last_valid_board = initial_board

        self.board_combo = FilterableBoardEntry(
            parent,
            textvariable=self.board_var,
            options=list(SUPPORTED_BOARDS.keys()),
            on_select=self._on_board_changed,
            width=25,
            max_visible_rows=8,
            font=self.font_mono_sm,
            placeholder="Select Board",
        )
        self.board_combo.pack(side=tk.LEFT, padx=(0, 4), fill=tk.X, expand=True)

    def _on_board_changed(self):
        """Handle board selection change."""
        old_board = getattr(self, "_last_valid_board", "")
        board_name = self.board_var.get()
        self._last_valid_board = board_name

        if not board_name:
            # Nothing selected (yet) — nothing to configure or report.
            self._board_changed_no_port_msg = None
            self._update_hardware_action_buttons()
            return

        if old_board and old_board != board_name:
            self._board_changed_no_port_msg = f"Board Changed: {old_board} >>> {board_name} | No port selected!"
            self._append(f"  🔀 Board changed: \"{old_board}\" → \"{board_name}\"", "info")
        else:
            self._board_changed_no_port_msg = None
            if not old_board:
                self._append(f"  >>> Board set to \"{board_name}\" <<<", "success")

        self._set_status(f"Board changed to {board_name}", Theme.CYAN)

        # Configure UPLOAD SPD based on platform:
        #   - AVR (Arduino Uno): force 115200 and lock the combobox (disabled)
        #   - ESP32: set to highest speed (921600) but keep combobox editable
        #   - Others: just show the combobox normally
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        platform = board_info.get("platform", "")
        is_avr = (platform == "atmelavr")
        is_esp32 = (platform == "espressif32")
        if is_avr:
            self.upload_speed_var.set("115200")
            self.upload_speed_combo.configure(state="disabled")
        else:
            if is_esp32:
                self.upload_speed_var.set("921600")
            self.upload_speed_combo.configure(state="readonly")

        self._restart_monitor(f"board → {board_name}")
        self._update_skip_compile_state()
        self._update_hardware_action_buttons()

        # Remember this (port -> board) pairing for next time, as long as a
        # real physical port is currently selected. Harmless to re-write the
        # same value every time this fires.
        port_device = self._extract_port_device(self.port_var.get())
        if port_device:
            remember_port_board(port_device, board_name)

    def _on_port_changed(self):
        """Handle port selection change — stop the current monitor,
        reconnect on the new port, and kick off esptool chip detection."""
        port_label = self.port_var.get()
        self._board_port_confirmed = False
        self._update_hardware_action_buttons()
        if not port_label:
            self._save_selected_port("")
            self._set_status("Port cleared", Theme.CYAN)
            self._restart_monitor("port cleared")
            return

        # Extract just the device name for the status message
        match = re.match(r"(COM\d+|/dev/\S+)", port_label)
        port_name = match.group(1) if match else port_label.split()[0]
        
        # Save to config
        self._save_selected_port(port_name)

        self._set_status(f"Port changed to {port_name}", Theme.CYAN)
        self._restart_monitor(f"port → {port_name}")
        
        # Fast local auto-select based on project files + port chip description
        self._auto_select_board(show_msg=True)

        # Only skip the esptool probe when the port's chip is confirmed
        # AVR-only (e.g. CH340/CH341). For anything else — including cases
        # where the static heuristic above guessed wrong and left the board
        # on "Arduino Uno" — let esptool make the real determination.
        if self._port_is_avr_only() or not port_name:
            return
            
        # Kick off chip auto-detection in background — non-blocking
        threading.Thread(
            target=self._auto_detect_board_from_port,
            args=(port_name,),
            daemon=True,
        ).start()

    def _auto_detect_board_from_port(self, port: str, _attempt: int = 1, _max_attempts: int = 4):
        """Background worker: probe *port* with esptool and auto-select board.

        Runs off the main thread so the UI stays responsive.  Results are
        dispatched back to the Tk event loop via root.after().

        The esptool handshake can transiently fail right at app launch —
        the COM port may still be settling from USB enumeration, DTR/RTS
        timing can be flaky the instant the port opens, etc. Previously
        this probe only ran once, so a single bad handshake left
        _board_port_confirmed permanently False (blocking Upload/Soft
        Reset/Hard Reset with "board on this port hasn't been recognized
        yet") until the user restarted the whole app to get a second
        attempt. Now we retry a few times with a short backoff before
        giving up and falling back to the static heuristic.
        """
        # Bail out early if the user has since switched to a different
        # port — no point continuing to retry probing a stale target.
        if self._extract_port_device(self.port_var.get()) != port:
            return

        chip_name, board_name = detect_chip_on_port(port)
        if not chip_name and _attempt < _max_attempts:
            delay = 1.5 * _attempt
            timer = threading.Timer(
                delay,
                self._auto_detect_board_from_port,
                args=(port, _attempt + 1, _max_attempts),
            )
            timer.daemon = True
            timer.start()
            return
        if chip_name and board_name and board_name in SUPPORTED_BOARDS:
            def _apply():
                current = self.board_var.get()
                if current != board_name:
                    self.board_var.set(board_name)
                    self._on_board_changed()
                    self._append(
                        f"  🔍 Auto-detected chip on {port}: "
                        f"{chip_name} → board set to \"{board_name}\"",
                        "success",
                    )
                    self._set_status(
                        f"Auto-detected: {chip_name} ({board_name})", Theme.GREEN
                    )
                else:
                    # Board already correct — just confirm
                    self._append(
                        f"  🔍 Auto-detected chip on {port}: {chip_name} "
                        f"(board already \"{board_name}\")",
                        "dim",
                    )
                self._board_port_confirmed = True
                self._update_hardware_action_buttons()
                # Re-verify and start the monitor since the probe is done and the port is free!
                self._schedule_auto_start_monitor(100)
            self.root.after(0, _apply)
        else:
            # Fall back to the static + chip descriptor matching, and start the monitor
            def _fallback():
                self._auto_select_board(show_msg=True)
                self._update_hardware_action_buttons()
                self._schedule_auto_start_monitor(100)
            self.root.after(0, _fallback)
        # If chip_name is None, detection silently skipped (not an ESP, or port busy)

    def _on_baud_changed(self):
        """Handle baud rate selection change."""
        baud = self.baud_var.get()
        self._set_status(f"Baud rate changed to {baud}", Theme.CYAN)
        self._restart_monitor(f"baud → {baud}")

    def _on_serial_baud_changed(self):
        """Handle serial monitor tab baud rate change."""
        baud = self.serial_baud_var.get()
        self._set_status(f"Serial monitor baud rate: {baud}", Theme.CYAN)
        # Also sync the main baud var
        self.baud_var.set(baud)
        self._restart_monitor(f"baud → {baud}")

    def _on_upload_speed_changed(self):
        """Handle upload speed selection change — updates platformio.ini asynchronously on a background thread."""
        speed = self.upload_speed_var.get()

        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        is_avr = (board_info.get("platform", "") == "atmelavr")
        if is_avr and speed != "115200":
            self._append(
                f"  ⚠ {board_name} (AVR) requires upload_speed = 115200 — ignoring {speed}.",
                "warning",
            )
            self.upload_speed_var.set("115200")
            speed = "115200"

        self._set_status(f"Upload speed changed to {speed}", Theme.CYAN)

        def _update_ini_async():
            ini_path = self.sketch_dir_path / "platformio.ini"
            if ini_path.exists():
                try:
                    content = ini_path.read_text(encoding="utf-8")
                    if re.search(r"^upload_speed\s*=", content, re.MULTILINE):
                        content = re.sub(
                            r"^upload_speed\s*=.*",
                            f"upload_speed = {speed}",
                            content,
                            flags=re.MULTILINE,
                        )
                    else:
                        # Inject after [env:...] header if key is missing
                        content = re.sub(
                            r"(\[env:[^\]]*\]\n)",
                            r"\1" + f"upload_speed = {speed}\n",
                            content,
                            count=1,
                        )
                    ini_path.write_text(content, encoding="utf-8")
                    self.root.after(0, lambda: self._append(f"  ⚙  upload_speed set to {speed} in platformio.ini", "info"))
                except Exception as exc:
                    self.root.after(0, lambda: self._append(f"  ⚠ Could not update upload_speed in platformio.ini: {exc}", "warning"))

        threading.Thread(target=_update_ini_async, daemon=True).start()

    def _auto_select_board(self, show_msg: bool = True) -> str | None:
        """If this physical port has a board type on record from a previous
        session (see remember_port_board / get_remembered_board_for_port),
        select it immediately instead of waiting on the slower esptool
        probe. The probe (kicked off alongside this call by the caller)
        remains authoritative and will correct this guess if the hardware
        on the port has changed since it was last remembered."""
        port_device = self._extract_port_device(self.port_var.get())
        if not port_device:
            return None

        remembered = get_remembered_board_for_port(port_device)
        if not remembered or remembered not in SUPPORTED_BOARDS:
            return None

        if self.board_var.get() == remembered:
            return remembered  # already selected — nothing to do

        self.board_var.set(remembered)
        self._on_board_changed()
        if show_msg:
            self._append(
                f"  🧠 Remembered board for {port_device}: \"{remembered}\" (used last time on this port)",
                "info"
            )
        return remembered

    def _detect_port_chip(self) -> tuple[str, set, str] | None:
        """Identify which known USB-serial chip the selected port reports,
        if any. Returns (matched_keyword, allowed_platforms_set, human_label)
        or None if the port description doesn't match any known chip."""
        val = self.port_var.get().lower()
        if not val:
            return None
        for keyword, (allowed_platforms, label) in USB_CHIP_BOARD_FAMILIES.items():
            if keyword in val:
                return (keyword, allowed_platforms, label)

    def _port_is_avr_only(self) -> bool:
        """True only when the selected port's USB-serial chip is one that can
        NEVER be an ESP board (e.g. classic CH340/CH341 on a Uno/Nano clone).
        """
        chip = self._detect_port_chip()
        if not chip:
            return False
        keyword, allowed_platforms, _label = chip
        esp_platforms = {"espressif32", "espressif8266"}
        return not (allowed_platforms & esp_platforms)

    def _is_board_recognized(self) -> bool:
        """True only once we have genuine confirmation of what's attached.
        However, we now disregard the 'unrecognized port' block to allow the user
        to compile/upload/reset to any selected port.
        """
        if not self.port_var.get():
            return False
        return True

    def _update_hardware_action_buttons(self):
        """Compile only needs a board *type* selected — it never talks to
        the physical port, so it's gated on board_var alone. Upload and the
        hardware Reset button additionally need the board on the selected
        port to actually be recognized."""
        if self.is_busy:
            return  # the running operation's own state machine owns button states right now
        board_selected = bool(self.board_var.get())

        btn_compile = getattr(self, "btn_compile", None)
        if btn_compile is not None:
            try:
                btn_compile.configure(state=tk.NORMAL if board_selected else tk.DISABLED)
            except Exception:
                pass

        state = tk.NORMAL if (board_selected and self._is_board_recognized()) else tk.DISABLED
        for attr in ("btn_upload", "btn_reset_mcu"):
            btn = getattr(self, attr, None)
            if btn is not None:
                try:
                    btn.configure(state=state)
                except Exception:
                    pass

    def _is_native_usb_port(self) -> bool:
        port_label = self.port_var.get().lower()
        if not port_label:
            return False
        native_keywords = ["esp32-s3", "esp32s3", "jtag", "usb bridge", "otg", "native"]
        uart_keywords = ["ch340", "ch341", "ch342", "ch343", "cp210", "silicon labs", "ftdi", "uart", "wch"]
        has_native = any(k in port_label for k in native_keywords)
        has_uart = any(k in port_label for k in uart_keywords)
        return has_native and not has_uart

    def _is_valid_port(self) -> bool:
        """Check if the selected port's USB-serial chip is actually sold
        with the currently-selected board's platform."""
        val = self.port_var.get().lower()
        if not val:
            return False
        if "communications port" in val or val.strip() == "com1":
            return False

        board_name = self.board_var.get()
        if not board_name or board_name not in SUPPORTED_BOARDS:
            return True

        chip = self._detect_port_chip()
        if chip is None:
            generic_keywords = ["usb serial", "usb-serial", "uart", "usb", "serial", "jtag", "bridge"]
            return any(kw in val for kw in generic_keywords)

        _keyword, allowed_platforms, _label = chip
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        board_platform = board_info.get("platform", "")
        return board_platform in allowed_platforms

    def _port_mismatch_reason(self) -> str:
        """Build a human-readable explanation of why the selected port
        doesn't match the selected board, for error messages."""
        board_name = self.board_var.get()
        chip = self._detect_port_chip()
        if chip is None:
            return "port doesn't match any recognized USB-serial chip for this board"
        _keyword, allowed_platforms, label = chip
        platform_labels = {
            "atmelavr": "Arduino AVR",
            "espressif32": "ESP32",
            "espressif8266": "ESP8266"
        }
        allowed = " or ".join(platform_labels.get(p, p) for p in sorted(allowed_platforms))
        return f"{label} detected — that's a {allowed} board, not \"{board_name}\""

    def _extract_port_device(self, port_label: str) -> str:
        """Pull just the device name (e.g. 'COM16') out of a port combobox
        label like 'COM16  —  Silicon Labs CP210x...'. Unlike _get_port(),
        this never logs an error for an empty/missing label — it's used for
        passive lookups (e.g. the remembered port→board cache) where a
        blank port is completely normal and shouldn't be reported."""
        if not port_label:
            return ""
        match = re.match(r"(COM\d+|/dev/\S+)", port_label)
        return match.group(1) if match else port_label.split()[0]

    def _get_port(self) -> str | None:
        """Extract COM port name from the combobox."""
        val = self.port_var.get()
        if not val or val.startswith("─"):
            return None
        # Extract COMx from "COM8  —  Silicon Labs..."
        match = re.match(r"(COM\d+|/dev/\S+)", val)
        return match.group(1) if match else val.split()[0]

    # ──────────────────────────────────────────────────────────
    # ACTIONS
    # ──────────────────────────────────────────────────────────
    def _perform_clean(self) -> tuple[list[str], list[str]]:
        """Core clean execution: delete SCons and PlatformIO temporary files."""
        import shutil as _sh
        sketch = self.sketch_dir_path
        removed: list[str] = []
        errors:  list[str] = []

        targets = [
            sketch / ".pio",
            sketch / "src",
            sketch / "platformio.ini",
        ]

        for target in targets:
            if not target.exists():
                continue
            try:
                if target.is_dir():
                    robust_rmtree(target)
                else:
                    target.unlink()
                removed.append(target.name)
            except Exception as exc:
                errors.append(f"{target.name}: {exc}")

        # Reset compile-state tracking so the next build starts clean
        self._last_compiled_board = None
        self._compile_cache_by_board = {}
        return removed, errors

    def _perform_clean_current_board(self) -> tuple[list[str], list[str]]:
        """Clean only the CURRENTLY selected board's own build output —
        .pio/build/<env> and .pio/libdeps/<env> — leaving every other
        board's cached build (and the shared toolchain/platform packages)
        untouched. This is what runs automatically before every actual
        recompile, so a fresh build always starts clean for that board
        without throwing away a different board's build when you switch
        back and forth."""
        import shutil as _sh
        sketch = self.sketch_dir_path
        env_name = self._pio_env_name()
        removed: list[str] = []
        errors: list[str] = []

        targets = [
            sketch / ".pio" / "build" / env_name,
            sketch / ".pio" / "libdeps" / env_name,
        ]
        for target in targets:
            if not target.exists():
                continue
            try:
                robust_rmtree(target)
                removed.append(f"{target.parent.name}/{target.name}")
            except Exception as exc:
                errors.append(f"{target.parent.name}/{target.name}: {exc}")

        # This board no longer has a valid cached hash once its build is wiped.
        board_name = self.board_var.get()
        if board_name:
            self._compile_cache_by_board.pop(board_name, None)
        return removed, errors

    def _do_clean(self):
        """Delete all generated/cached files, keeping only source files.

        Removes:
          • .pio/          — PlatformIO build cache, toolchain downloads, libdeps
          • src/           — synced hard-link copies of sketch files
          • platformio.ini — regenerated fresh on next compile/upload

        Keeps everything else (*.ino, *.cpp, *.h, *.c, and any user files).
        Safe to call at any time (not during a busy operation).
        """
        if self.is_busy:
            self._set_status("Busy — stop the current operation first", Theme.RED)
            return

        self._clean_retry_in_progress = True
        removed, errors = self._perform_clean()

        self._append("")
        self._append("  🧹 CLEAN", "header")
        if removed:
            self._append(f"  Removed: {', '.join(removed)}", "success")
        else:
            self._append("  Nothing to remove — project already clean.", "info")
        if errors:
            for e in errors:
                self._append(f"  ⚠ Could not remove {e}", "warning")
        self._append("  Ready. Compile or Upload to rebuild from scratch.", "dim")
        self._set_status("Project cleaned — ready to rebuild", Theme.GREEN)
        
        # Uncheck and disable "Skip recompile"
        self.skip_compile_var.set(False)
        self.cb_skip_compile.configure(state=tk.DISABLED)

    def _do_clean_then_compile(self):
        """Clean the build cache and immediately start a fresh compile."""
        self._clean_retry_in_progress = True
        self._do_clean()
        self._do_compile()

    def _do_compile(self):
        if self.is_busy:
            # Auto-recover: if no subprocess is actually running, clear stale busy flag
            if not self.process or self.process.poll() is not None:
                self.is_busy = False
                self._set_buttons_state(False)
                self._append("  ℹ Stale busy state cleared — proceeding with compile.", "info")
            else:
                return

        # Auto-save editor files before compiling so the compiler sees the latest source.
        if hasattr(self, "_save_all_editor_files") and callable(self._save_all_editor_files):
            try:
                self._save_all_editor_files()
            except Exception:
                pass

        if not self.board_var.get():
            self._append_notif("  ✖ Compile failed: No board selected! Choose a board before compiling.", "warning")
            return

        # Pre-check: detect board/sketch mismatch so _run_compile can display the warning
        # inside the COMPILING section header (not before it).
        compat_boards, compat_reasons = detect_board_compatibility(self.sketch_dir_path)
        selected_board = self.board_var.get()
        self._board_mismatch_detected = (
            bool(compat_boards) and selected_board not in compat_boards
        )
        # Store reasons so _run_compile can emit them after the COMPILING header
        self._pending_compat_reasons = compat_reasons if compat_reasons else []

        # Auto-detect and set correct board before compilation begins
        self._auto_select_board(show_msg=True)

        self.is_busy = True
        self._set_buttons_state(True, operation="compile")

        # ── Smart compile check ──────────────────────────────────────────────
        # "Skip recompile" checkbox behaviour:
        #   UNCHECKED → always recompile, no cache check at all.
        #   CHECKED   → skip recompile if (a) a prior build exists on disk AND
        #               (b) sources/board haven't changed since that build.
        #               If no prior build exists, compile normally even when checked.
        if self.skip_compile_var.get():
            if self._has_prior_build():
                recompile_needed, reason = self._needs_recompile()
                if not recompile_needed:
                    self._append("")
                    self._append("=" * 50, "header")
                    self._append("  ⚙  COMPILE CHECK", "header")
                    self._append("=" * 50, "header")
                    self._append("")
                    self._append("  ✔ Already compiled — sources unchanged.", "success")
                    self._append("  No recompilation needed. Cached build is up-to-date.", "dim")
                    self._append("  (Uncheck 'Skip recompile' or edit a source file to force rebuild)", "dim")
                    self._set_status("Compile skipped — sources unchanged", Theme.GREEN)
                    self.is_busy = False
                    self._set_buttons_state(False)
                    return
                # Prior build exists but sources changed — fall through to recompile
            # No prior build at all — fall through to compile normally

        def _safe_compile():
            try:
                self._run_compile()
            except Exception as e:
                import traceback
                try:
                    with open("error_log.txt", "w", encoding="utf-8") as f:
                        traceback.print_exc(file=f)
                except Exception:
                    pass
                self._append(f"  ✖ Internal error in compile thread: {e}", "error")
                self._set_status("Compile FAILED", Theme.RED)
            finally:
                # Guarantee busy state is always cleared, even on unhandled exceptions
                self.is_busy = False
                self._set_buttons_state(False)

        threading.Thread(target=_safe_compile, daemon=True).start()

    def _do_upload(self):
        if self.is_busy:
            # Auto-recover: if no subprocess is actually running, clear stale busy flag
            if not self.process or self.process.poll() is not None:
                self.is_busy = False
                self._set_buttons_state(False)
                self._append("  ℹ Stale busy state cleared — proceeding with upload.", "info")
            else:
                return

        # Auto-save editor files before uploading so the compiler sees the latest source.
        if hasattr(self, "_save_all_editor_files") and callable(self._save_all_editor_files):
            try:
                self._save_all_editor_files()
            except Exception:
                pass
        port = self._get_port()
        if not port:
            self._append_notif("  ✖ Upload failed: No serial port selected! Please select a port.", "warning")
            return

        if not self.board_var.get():
            self._append_notif("  ✖ Upload failed: No board selected! Choose a board before uploading.", "warning")
            return

        if not self._is_board_recognized():
            self._append("  ✖ Upload rejected: board on this port hasn't been recognized yet.", "error")
            return

        # Auto-detect and set correct board before upload mismatch guard check
        self._auto_select_board(show_msg=True)

        if not self._is_valid_port():
            self._append(f"  ✖ Upload rejected: {self._port_mismatch_reason()}.", "error")
            return

        def _safe_run():
            try:
                self._run_upload(port)
            except Exception as e:
                import traceback
                try:
                    with open("error_log.txt", "w", encoding="utf-8") as f:
                        traceback.print_exc(file=f)
                except Exception:
                    pass
                self.is_busy = False
                self._set_buttons_state(False)
                self._set_status("Upload FAILED", Theme.RED)
                self._append(f"  ✖ Internal error in upload thread: {e}", "error")

        self.is_busy = True
        self._set_buttons_state(True, operation="upload")
        threading.Thread(target=_safe_run, daemon=True).start()

    def _schedule_auto_start_monitor(self, delay_ms: int):
        """Schedule _auto_start_monitor while canceling any previously scheduled attempts."""
        if getattr(self, "_auto_start_after_id", None):
            try:
                self.root.after_cancel(self._auto_start_after_id)
            except Exception:
                pass
        self._auto_start_after_id = self.root.after(delay_ms, self._auto_start_monitor)

    def _auto_start_monitor(self):
        """Start serial monitor if a port is selected and not already running."""
        if self.is_busy:
            return
        if self.serial_thread and self.serial_thread.is_alive():
            # Previous thread is still cleaning up/closing the port.
            # Reschedule and check again shortly to prevent port access clashes.
            self._schedule_auto_start_monitor(100)
            return
        if self.serial_running:
            return
        # Check silently — this is a routine background check (fires on
        # startup, after loading a project, after switching boards, etc.)
        # and it's completely normal for no port to be selected yet at
        # those times. _get_port() itself always logs "No port selected!"
        # as an error, which is correct for user-initiated actions (Upload,
        # Reset...) but was noisy and misleading here, since it made a
        # totally expected "nothing plugged in yet" state look like a
        # failure right in the middle of the project-load log.
        port_raw = self.port_var.get()
        self._board_changed_no_port_msg = None
        if not port_raw or port_raw.startswith("─"):
            return
        port = self._get_port()
        if not port:
            return
        if not SUPPORTED_BOARDS:
            err_msg = "No boards are currently installed."
            if self._last_monitor_error != err_msg:
                self._last_monitor_error = err_msg
                self._append_serial("  ✖ Monitor blocked: No boards are currently installed.", "error")
                self._append_serial("    Please download a board framework first via the 'Download Boards/Libraries' manager.", "error")
            return
        # NOTE: unlike Upload, the Serial Monitor doesn't care whether the
        # selected board *type* matches the chip actually on this port —
        # it just opens the raw serial port at the chosen baud rate, so any
        # MCU attached can be monitored regardless of the board dropdown.
        self._last_monitor_error = "" # Clear error since it started successfully!
        self._monitor_should_run = True
        self.serial_running = True  # Prevent duplicate thread spawns
        baud = int(self.baud_var.get())
        self.serial_thread = threading.Thread(target=self._run_monitor, args=(port, baud), daemon=True)
        self.serial_thread.start()

    def _do_stop(self):
        """Stop compile/upload process (does NOT stop serial monitor)."""
        self._stop_requested = True
        session_id = getattr(self, "_op_session_id", 0)
        if self.process and self.process.poll() is None:
            self.btn_stop.configure(text="■ Stopping...", state=tk.DISABLED)
            self._set_status("Stopping process...", Theme.YELLOW)
            proc_to_kill = self.process
            
            def _kill():
                try:
                    if sys.platform == "win32":
                        import subprocess
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc_to_kill.pid)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW
                        )
                    else:
                        proc_to_kill.kill()
                    self._append("  ■ Process killed.", "warning")
                except Exception as e:
                    self._append(f"  ⚠ Failed to stop process: {e}", "warning")
                # ── Failsafe: if the background thread doesn't clear is_busy
                # within 5 seconds after the process was killed, force-clear it
                # so the UI never gets permanently stuck — but ONLY if no new
                # operation session has started in the meantime.
                time.sleep(5)
                if self.is_busy and getattr(self, "_op_session_id", 0) == session_id:
                    self.is_busy = False
                    self._set_buttons_state(False)
                    self._set_status("Stopped (failsafe)", Theme.YELLOW)
                    self._append("  ⚠ Busy state cleared by failsafe timer.", "warning")

            threading.Thread(target=_kill, daemon=True).start()
        elif self.is_busy:
            # Process is already dead but is_busy is stuck — clear it immediately
            self.is_busy = False
            self._set_buttons_state(False)
            self._set_status("Ready", Theme.GREEN)
            self._append("  ℹ Busy state was stale — cleared.", "info")

    def _pause_monitor(self) -> bool:
        """Pause serial monitor temporarily for uploading."""
        was_running = self.serial_running
        if was_running:
            self.serial_running = False
            if self.serial_conn and self.serial_conn.is_open:
                try:
                    self.serial_conn.close()
                except Exception:
                    pass
            if self.serial_thread and self.serial_thread.is_alive():
                self.serial_thread.join(timeout=1.0)
            self._set_serial_status(False)
            self._append_serial("  ⏸ Paused for upload…", "dim")
        return was_running

    def _resume_monitor(self):
        """Resume serial monitor after upload completes, triggering MCU reset so setup() output is captured."""
        if not self._monitor_should_run:
            return
        self._manual_reset_pending = True
        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        is_avr = (board_info.get("platform", "") == "atmelavr")
        delay = 0.3 if is_avr else 0.4
        time.sleep(delay)

        # Drain any leftover bytes from the upload tool so the monitor
        # starts with a clean buffer (this is the root cause of the
        # "need to change baud rate to reset" symptom).
        port = self._get_port()
        if port:
            try:
                with serial.Serial(port=port, baudrate=int(self.baud_var.get()),
                                   timeout=0.05, dsrdtr=False, rtscts=False) as tmp:
                    tmp.reset_input_buffer()
                    tmp.reset_output_buffer()
            except Exception:
                pass  # port may still be releasing; _run_monitor will retry

        self._schedule_auto_start_monitor(0)

    def _restart_monitor(self, reason: str):
        """Stop and relaunch the serial monitor with the current port/baud.
        Used when a setting that affects the live connection changes
        (baud rate, board selection, port selection) while the monitor
        is connected or idle."""
        def _do():
            was_running = self.serial_running
            self.serial_running = False
            if self.serial_conn and self.serial_conn.is_open:
                try:
                    self.serial_conn.close()
                except Exception:
                    pass
            if self.serial_thread and self.serial_thread.is_alive():
                self.serial_thread.join(timeout=1.0)
            if was_running:
                self._append_serial(f"  ↻ Restarting monitor — {reason}…", "dim")
                # Separator so the new session is visually distinct
                self._append_serial("─" * 40, "dim")
            self._schedule_auto_start_monitor(100)
        self.root.after(0, _do)

    def _open_sketch_in_explorer(self):
        """Open the current project folder in Windows File Explorer."""
        try:
            if self.sketch_dir_path and self.sketch_dir_path.is_dir():
                import os
                os.startfile(str(self.sketch_dir_path))
            else:
                from tkinter import messagebox
                messagebox.showwarning(
                    "Folder Not Found",
                    f"Project folder not found or missing:\n{self.sketch_dir_path}",
                    parent=self.root,
                )
        except Exception as e:
            from tkinter import messagebox
            messagebox.showerror(
                "Cannot Open Folder",
                f"Failed to open folder in Explorer:\n{e}",
                parent=self.root,
            )

    def _select_sketch_folder(self):
        if self.is_busy:
            return
        from tkinter import filedialog
        folder = filedialog.askdirectory(
            initialdir=str(self.sketch_dir_path),
            title="Select Sketch Folder"
        )
        if folder:
            new_path = Path(folder)
            if new_path.resolve() != self.sketch_dir_path.resolve():
                from tkinter import messagebox
                owner_pid = folder_lock_owner(new_path)
                if owner_pid is not None:
                    messagebox.showerror(
                        "Project In Use",
                        f"This project folder is already open in another MCU Flasher window "
                        f"(PID {owner_pid}):\n{new_path}\n\n"
                        "Close that window first, or choose a different project.",
                        parent=self.root
                    )
                    self.is_busy = False
                    self._set_buttons_busy(False)
                    return
                self.sketch_dir_path = new_path
                config = load_gui_config()
                config["last_sketch_dir"] = str(self.sketch_dir_path)
                save_gui_config(config)
                self._on_folder_changed()
            else:
                from tkinter import messagebox
                messagebox.showinfo(
                    "Project Active",
                    "✔ This project is already currently active.",
                    parent=self.root
                )

        self.is_busy = False
        self._set_buttons_busy(False)

    def _new_project(self):
        """Open the project selector dialog mid-session.
        On success, switches the active sketch folder to the chosen or
        freshly scaffolded project — same effect as startup project selection."""
        if self.is_busy:
            return
        dlg = ProjectSelectorDialog(self.root, str(self.sketch_dir_path))
        project_dir = dlg.run()
        if project_dir:
            if project_dir.resolve() != self.sketch_dir_path.resolve():
                self.sketch_dir_path = project_dir
                config = load_gui_config()
                config["last_sketch_dir"] = str(self.sketch_dir_path)
                save_gui_config(config)
                self._on_folder_changed()
            else:
                from tkinter import messagebox
                messagebox.showinfo(
                    "Project Active",
                    "✔ This project is already currently active.",
                    parent=self.root
                )

        self.is_busy = False
        self._set_buttons_busy(False)

    def _open_modify_files_dialog(self):
        """Open a tabbed modal for managing project source files:
          • Add    — create a new blank file (.ino / .h / .cpp / .txt only)
          • Rename — rename an existing project file
          • Delete — permanently remove an existing project file

        Renaming/deleting operates on whatever's currently on disk in the
        sketch folder (so it also covers .c files placed there manually).
        Any successful action refreshes the open editor tabs immediately.
        """
        if self.is_busy:
            return
        if not self.sketch_dir_path or not self.sketch_dir_path.exists():
            from tkinter import messagebox
            messagebox.showerror(
                "No Project Folder",
                "No active project folder to modify.",
                parent=self.root
            )
            return

        from tkinter import messagebox

        ALLOWED_NEW_EXTS = (".ino", ".h", ".cpp", ".txt")
        LISTABLE_GLOBS = ("*.ino", "*.h", "*.cpp", "*.c", "*.txt")
        INVALID_CHARS = '\\/:*?"<>|'

        def _list_project_files():
            names = []
            for pattern in LISTABLE_GLOBS:
                names.extend(f.name for f in self.sketch_dir_path.glob(pattern))
            return sorted(names)

        dlg = tk.Toplevel(self.root)
        dlg.title("Modify Project Files")
        dlg.configure(bg=Theme.BG_DARKEST)
        dlg.resizable(False, False)
        center_toplevel(dlg, self.root, 460, 400)
        dlg.transient(self.root)
        dlg.grab_set()

        dlg_title_font = tkfont.Font(family="Montserrat", size=13, weight="bold")
        dlg_btn_font = tkfont.Font(family="Montserrat", size=10, weight="bold")
        dlg_action_font = tkfont.Font(family="Montserrat", size=11, weight="bold")

        tk.Label(
            dlg, text="Modify Project Files", font=dlg_title_font,
            fg=Theme.CYAN, bg=Theme.BG_DARKEST
        ).pack(pady=(14, 10))

        # ── Sub-tab bar (Add / Rename / Delete) ─────────────────────────
        tab_bar = tk.Frame(dlg, bg=Theme.BG_DARKEST)
        tab_bar.pack(fill=tk.X, padx=20)

        body = tk.Frame(dlg, bg=Theme.BG_DARKEST)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(12, 0))

        err_lbl = tk.Label(
            dlg, text="", font=self.font_label, fg=Theme.RED, bg=Theme.BG_DARKEST,
            wraplength=400, justify=tk.CENTER
        )
        err_lbl.pack(pady=(8, 0))

        current_tab = ["add"]
        frames = {}
        tab_btns = {}

        def _clear_err():
            err_lbl.config(text="")

        # ── Add sub-panel ────────────────────────────────────────────────
        add_frame = tk.Frame(body, bg=Theme.BG_DARKEST)
        frames["add"] = add_frame

        tk.Label(
            add_frame,
            text="Enter a filename with one of these extensions:\n.ino   .h   .cpp   .txt",
            font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST,
            justify=tk.CENTER
        ).pack(pady=(10, 10))

        add_name_var = tk.StringVar(value="")
        add_entry = tk.Entry(
            add_frame, textvariable=add_name_var, font=self.font_mono,
            bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
            insertbackground=Theme.CYAN, borderwidth=0,
            highlightthickness=1, highlightcolor=Theme.CYAN_DIM,
            highlightbackground=Theme.BORDER, justify=tk.CENTER,
        )
        add_entry.pack(fill=tk.X, ipady=5)

        # ── Browse separator ──────────────────────────────────────────────
        sep_row = tk.Frame(add_frame, bg=Theme.BG_DARKEST)
        sep_row.pack(fill=tk.X, pady=(8, 0))
        tk.Frame(sep_row, bg=Theme.BORDER, height=1).pack(fill=tk.X)
        tk.Label(
            add_frame,
            text="— or —",
            font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST,
        ).pack(pady=(4, 0))

        tk.Button(
            add_frame,
            text="📂  Browse & Copy Existing File…",
            font=self.font_label,
            bg=Theme.BTN_CLEAR, fg=Theme.TEXT_BRIGHT,
            activebackground=Theme.BTN_CLEAR_H,
            activeforeground=Theme.TEXT_BRIGHT,
            relief=tk.FLAT, cursor="hand2", bd=0,
            command=lambda: _do_add_browse(),
        ).pack(pady=(4, 0), ipadx=6, ipady=4)

        def _do_add_browse():
            from tkinter import filedialog
            import shutil as _shutil
            src = filedialog.askopenfilename(
                title="Select a file to add to the project",
                filetypes=[("Sketch/Header/Text files", "*.ino *.cpp *.h *.txt"),
                           ("All files", "*.*")],
                parent=dlg,
            )
            if not src:
                return
            src_path = Path(src)
            if src_path.suffix.lower() not in ALLOWED_NEW_EXTS:
                err_lbl.config(text="Only .ino, .h, .cpp, .txt are allowed.")
                return
            dest = self.sketch_dir_path / src_path.name
            if dest.resolve() == src_path.resolve():
                err_lbl.config(text="That file is already in the project folder.")
                return
            if dest.exists():
                overwrite = messagebox.askyesno(
                    "File Already Exists",
                    f"\"{src_path.name}\" already exists in the project folder.\n\n"
                    "Overwrite it with the selected file?",
                    parent=dlg,
                )
                if not overwrite:
                    return
            try:
                _shutil.copy2(str(src_path), str(dest))
            except Exception as e:
                err_lbl.config(text=f"Could not copy file: {e}")
                return
            self._append(f"  ➕ Added file to project (copied): {src_path.name}", "success")
            _refresh_editor()
            dlg.destroy()

        def _do_add():

            name = add_name_var.get().strip()
            if not name:
                err_lbl.config(text="Please enter a filename.")
                return
            if any(c in name for c in INVALID_CHARS) or name in (".", ".."):
                err_lbl.config(text="Filename contains invalid characters.")
                return

            suffix = Path(name).suffix.lower()
            if suffix not in ALLOWED_NEW_EXTS:
                err_lbl.config(text="Only .ino, .h, .cpp, .txt are allowed.")
                return
            if not Path(name).stem:
                err_lbl.config(text="Please enter a name before the extension.")
                return

            dest = self.sketch_dir_path / name
            if dest.exists():
                overwrite = messagebox.askyesno(
                    "File Already Exists",
                    f"\"{name}\" already exists in the project folder.\n\n"
                    "Overwrite it with a blank file?",
                    parent=dlg
                )
                if not overwrite:
                    return

            try:
                dest.write_text("", encoding="utf-8")
            except Exception as e:
                err_lbl.config(text=f"Could not create file: {e}")
                return

            self._append(f"  ➕ Added file to project: {name}", "success")
            _refresh_editor()
            dlg.destroy()

        # ── Rename sub-panel ─────────────────────────────────────────────
        rename_frame = tk.Frame(body, bg=Theme.BG_DARKEST)
        frames["rename"] = rename_frame

        tk.Label(
            rename_frame, text="Select a file to rename:", font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor=tk.W
        ).pack(fill=tk.X, pady=(10, 4))

        rename_select_var = tk.StringVar()
        rename_combo = ttk.Combobox(
            rename_frame, textvariable=rename_select_var, state="readonly",
            font=self.font_label, values=_list_project_files()
        )
        rename_combo.pack(fill=tk.X)

        tk.Label(
            rename_frame, text="New filename:", font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor=tk.W
        ).pack(fill=tk.X, pady=(14, 4))

        rename_new_var = tk.StringVar()
        rename_entry = tk.Entry(
            rename_frame, textvariable=rename_new_var, font=self.font_mono,
            bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
            insertbackground=Theme.CYAN, borderwidth=0,
            highlightthickness=1, highlightcolor=Theme.CYAN_DIM,
            highlightbackground=Theme.BORDER, justify=tk.CENTER,
        )
        rename_entry.pack(fill=tk.X, ipady=5)

        def _on_rename_select(event=None):
            # Pre-fill the new-name box with the current name so the user
            # only has to tweak part of it.
            if rename_select_var.get():
                rename_new_var.set(rename_select_var.get())
        rename_combo.bind("<<ComboboxSelected>>", _on_rename_select)

        def _do_rename():
            old_name = rename_select_var.get()
            if not old_name:
                err_lbl.config(text="Please select a file to rename.")
                return
            new_name = rename_new_var.get().strip()
            if not new_name:
                err_lbl.config(text="Please enter a new filename.")
                return
            if any(c in new_name for c in INVALID_CHARS) or new_name in (".", ".."):
                err_lbl.config(text="Filename contains invalid characters.")
                return

            suffix = Path(new_name).suffix.lower()
            if suffix not in ALLOWED_NEW_EXTS:
                err_lbl.config(text="Only .ino, .h, .cpp, .txt are allowed.")
                return
            if not Path(new_name).stem:
                err_lbl.config(text="Please enter a name before the extension.")
                return

            src = self.sketch_dir_path / old_name
            dest = self.sketch_dir_path / new_name

            if not src.exists():
                err_lbl.config(text="Selected file no longer exists.")
                return
            try:
                same_file = src.resolve() == dest.resolve()
            except Exception:
                same_file = old_name == new_name
            if same_file:
                err_lbl.config(text="New name is the same as the current name.")
                return
            if dest.exists():
                err_lbl.config(text=f"\"{new_name}\" already exists.")
                return

            confirm = messagebox.askyesno(
                "Rename File",
                f"Rename \"{old_name}\" to \"{new_name}\"?\n\n"
                "Any unsaved changes open in its editor tab will be lost.",
                parent=dlg
            )
            if not confirm:
                return

            try:
                src.rename(dest)
            except Exception as e:
                err_lbl.config(text=f"Could not rename file: {e}")
                return

            self._append(f"  ✎ Renamed file: {old_name} → {new_name}", "success")
            _refresh_editor()
            dlg.destroy()

        # ── Delete sub-panel ─────────────────────────────────────────────
        delete_frame = tk.Frame(body, bg=Theme.BG_DARKEST)
        frames["delete"] = delete_frame

        tk.Label(
            delete_frame, text="Select a file to remove from the project:",
            font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, anchor=tk.W
        ).pack(fill=tk.X, pady=(10, 4))

        delete_select_var = tk.StringVar()
        delete_combo = ttk.Combobox(
            delete_frame, textvariable=delete_select_var, state="readonly",
            font=self.font_label, values=_list_project_files()
        )
        delete_combo.pack(fill=tk.X)

        tk.Label(
            delete_frame,
            text="This permanently deletes the file from disk\nand closes its tab in the editor.",
            font=self.font_label, fg=Theme.ORANGE, bg=Theme.BG_DARKEST, justify=tk.CENTER
        ).pack(pady=(16, 0))

        def _do_delete():
            name = delete_select_var.get()
            if not name:
                err_lbl.config(text="Please select a file to delete.")
                return
            target = self.sketch_dir_path / name
            if not target.exists():
                err_lbl.config(text="Selected file no longer exists.")
                return

            confirm = messagebox.askyesno(
                "Delete File",
                f"Permanently delete \"{name}\"?\n\nThis cannot be undone.",
                parent=dlg
            )
            if not confirm:
                return

            try:
                target.unlink()
            except Exception as e:
                err_lbl.config(text=f"Could not delete file: {e}")
                return

            self._append(f"  🗑 Removed file from project: {name}", "warning")
            _refresh_editor()
            dlg.destroy()

        def _refresh_editor():
            if callable(getattr(self, "_load_editor_files", None)):
                self._load_editor_files()
            try:
                self._update_skip_compile_state()
            except Exception:
                pass

        ACTIONS = {
            "add":    ("Add",    Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, _do_add),
            "rename": ("Rename", Theme.BTN_UPLOAD,  Theme.BTN_UPLOAD_H,  _do_rename),
            "delete": ("Delete", Theme.BTN_STOP,     Theme.BTN_STOP_H,   _do_delete),
        }

        def _do_cancel():
            dlg.destroy()

        # ── Bottom Cancel / dynamic action button ───────────────────────
        btn_row = tk.Frame(dlg, bg=Theme.BG_DARKEST)
        btn_row.pack(pady=(16, 14))

        btn_cancel = tk.Button(
            btn_row, text="Cancel", command=_do_cancel,
            font=dlg_action_font, fg=Theme.TEXT_BRIGHT, bg=Theme.BTN_STOP,
            activebackground=Theme.BTN_STOP_H, activeforeground=Theme.TEXT_BRIGHT,
            relief=tk.FLAT, borderwidth=0, padx=20, pady=8, width=10, cursor="hand2",
        )
        btn_cancel.pack(side=tk.LEFT, padx=8)
        btn_cancel.bind("<Enter>", lambda e: btn_cancel.configure(bg=Theme.BTN_STOP_H))
        btn_cancel.bind("<Leave>", lambda e: btn_cancel.configure(bg=Theme.BTN_STOP))

        btn_action = tk.Button(
            btn_row, text="Add", command=_do_add,
            font=dlg_action_font, fg=Theme.TEXT_BRIGHT, bg=Theme.BTN_COMPILE,
            activebackground=Theme.BTN_COMPILE_H, activeforeground=Theme.TEXT_BRIGHT,
            relief=tk.FLAT, borderwidth=0, padx=20, pady=8, width=10, cursor="hand2",
        )
        btn_action.pack(side=tk.LEFT, padx=8)
        btn_action._bg_idle = Theme.BTN_COMPILE
        btn_action._bg_hover = Theme.BTN_COMPILE_H
        btn_action.bind("<Enter>", lambda e: btn_action.configure(bg=btn_action._bg_hover))
        btn_action.bind("<Leave>", lambda e: btn_action.configure(bg=btn_action._bg_idle))

        def _switch(which):
            current_tab[0] = which
            for f in frames.values():
                f.pack_forget()
            frames[which].pack(fill=tk.BOTH, expand=True)
            _clear_err()

            for key, btn in tab_btns.items():
                btn.configure(bg=Theme.BTN_FULL if key == which else Theme.BTN_CLEAR)

            text, bg, bg_hover, cmd = ACTIONS[which]
            btn_action.configure(text=text, bg=bg, activebackground=bg_hover, command=cmd)
            btn_action._bg_idle = bg
            btn_action._bg_hover = bg_hover

        tab_btns["add"] = self._make_btn(
            tab_bar, "➕ Add", lambda: _switch("add"), Theme.BTN_FULL, Theme.BTN_FULL_H, font=dlg_btn_font
        )
        tab_btns["add"].pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 3))

        tab_btns["rename"] = self._make_btn(
            tab_bar, "✎ Rename", lambda: _switch("rename"), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=dlg_btn_font
        )
        tab_btns["rename"].pack(side=tk.LEFT, expand=True, fill=tk.X, padx=3)

        tab_btns["delete"] = self._make_btn(
            tab_bar, "🗑 Delete", lambda: _switch("delete"), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=dlg_btn_font
        )
        tab_btns["delete"].pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(3, 0))

        dlg.bind("<Escape>", lambda e: _do_cancel())
        dlg.protocol("WM_DELETE_WINDOW", _do_cancel)
        add_entry.bind("<Return>", lambda e: _do_add())
        rename_entry.bind("<Return>", lambda e: _do_rename())

        _switch("add")
        add_entry.focus_set()

    def _save_active_folder(self, folder_path):
        """Save the currently active sketch folder in the instance config,
        so other windows can see (and avoid) it via get_occupied_folders().
        Mirrors _save_selected_port()."""
        try:
            config = load_gui_config()
            config["active_sketch_dir"] = str(Path(folder_path).resolve()) if folder_path else ""
            save_gui_config(config)
        except Exception:
            pass

    def _on_folder_changed(self):
        """Called whenever sketch_dir_path is set to a new folder.
        Updates the UI label, invalidates the compile cache, then scans
        includes in a background thread so the console gets an instant
        project-summary report."""
        self._clear_console()
        self._clear_serial_console()
        self.lbl_sketch.config(text=f"📁 {self._get_sketch_display_name()}")
        self._set_status(f"Project: {self.sketch_dir_path.name}", Theme.CYAN)
        self._save_active_folder(self.sketch_dir_path)
        mode = getattr(self, "editor_mode", "default")
        if mode == "qscintilla":
            # Restart QScintilla so it launches in the new project directory
            try:
                self._cleanup_active_editor()
                self._build_editor(self.editor_frame)
            except Exception:
                pass
        else:
            if hasattr(self, "_load_editor_files"):
                try:
                    self._load_editor_files()
                except Exception:
                    pass
        try:
            add_recent_project(str(self.sketch_dir_path))
        except Exception:
            pass
        
        # Auto-detect and set correct board based on new project files.
        # If a port is already selected, re-run the esptool probe so that
        # switching projects (without unplugging) still picks the right board
        # (e.g. going from Arduino R3 → ESP32-S3 with the same port connected).
        port = self.port_var.get()
        if port and not port.startswith("─"):
            threading.Thread(
                target=self._auto_detect_board_from_port,
                args=(port,),
                daemon=True,
            ).start()
        else:
            self._auto_select_board(show_msg=True)

        self._compat_warnings_approved_hash = None
        self._load_compile_cache()
        self._update_skip_compile_state()

        threading.Thread(target=self._report_project_includes, daemon=True).start()

        # Trigger MCU reset shortly after folder change (giving auto-detect time to finish)
        # Skip on startup (first run) to avoid conflicts with initial auto-start
        if not getattr(self, "_first_run", True):
            self._silent_reset = True
            self.root.after(500, self._reset_mcu_from_monitor)

    def _report_project_includes(self):
        """Background task: scan includes in the new folder and print a summary."""
        self._append("")
        self._append("=" * 50, "header")
        self._append(f"  📁  PROJECT LOADED", "header")
        self._append("=" * 50, "header")
        self._append(f"  Path : {self.sketch_dir_path}", "dim")

        # ── Drive / volume health report ──────────────────────────────────
        # Sketches may live on any volume type (NTFS, exFAT, FAT32, flash
        # drives, external disks, network shares).  Surface the facts that
        # actually affect compile/upload reliability so users on problem
        # volumes get an immediate, actionable hint instead of a cryptic
        # failure later.
        try:
            fs_name, type_label = get_volume_info(self.sketch_dir_path)
            writable = is_volume_writable(self.sketch_dir_path)
            if fs_name or type_label:
                self._append(f"  💾 Drive : {fs_name or '?'} ({type_label or '?'})", "dim")
            if not writable:
                self._append(
                    "  ✖ Volume is write-protected or read-only — compiling and "
                    "uploading will FAIL. Check the USB drive's lock switch or "
                    "its read-only status.",
                    "error",
                )
            elif fs_name.upper() in ("EXFAT", "FAT32", "FAT", "FAT16", "FAT12"):
                self._append(
                    "  ℹ Flash/external drive (FAT/exFAT) detected: builds run "
                    "slower than on an internal disk, and stale .pio caches may "
                    "need extra clean passes. This is normal for removable media.",
                    "info",
                )
            if sys.platform == "win32":
                import ctypes
                _probe = self.sketch_dir_path / "platformio.ini"
                if not _probe.exists():
                    _probe = self.sketch_dir_path
                _a = ctypes.windll.kernel32.GetFileAttributesW(str(_probe))
                if _a != -1 and (_a & 0x1000):  # FILE_ATTRIBUTE_OFFLINE
                    self._append(
                        "  ⚠ Sketch is inside a cloud-synced folder (OneDrive/"
                        "Dropbox placeholder files). Copy it to a local or USB "
                        "drive for reliable builds.",
                        "warning",
                    )
        except Exception:
            pass

        ini_path = self.sketch_dir_path / "platformio.ini"
        if ini_path.exists():
            self._append("  ✔ platformio.ini found", "success")
        else:
            self._append("  ⚠ No platformio.ini — will be created on first compile", "warning")

        # Scan source files
        source_files = []
        for ext in ["*.ino", "*.cpp", "*.h", "*.c", "*.txt"]:
            source_files.extend(self.sketch_dir_path.glob(ext))

        if not source_files:
            self._append("  ⚠ No .ino / .cpp / .h / .c / .txt files found in this folder", "warning")
            self._append("")
            return

        self._append(f"  Source files ({len(source_files)}):", "dim")
        for f in sorted(source_files):
            self._append(f"    • {f.name}", "dim")

        # Detect libraries from includes
        detected = self._scan_includes_for_libs()
        if detected:
            self._append(f"  Detected lib dependencies ({len(detected)}):", "info")
            for lib in detected:
                # Check whether each one is already listed in platformio.ini
                # Strip symlink:// prefix for comparison and display
                lib_for_check = lib.replace("symlink://", "") if lib.startswith("symlink://") else lib
                in_ini = False
                if ini_path.exists():
                    try:
                        ini_content = ini_path.read_text(encoding="utf-8")
                        base = lib_for_check.split('/')[-1].split('@')[0].strip()
                        in_ini = base.lower() in ini_content.lower()
                    except Exception:
                        pass
                status_icon = "✔" if in_ini else "+"
                status_color = "success" if in_ini else "warning"
                display_lib = lib_for_check.split('/')[-1] if '/' in lib_for_check else lib_for_check
                self._append(f"    {status_icon} {display_lib}  (symlink)", status_color)
            if ini_path.exists():
                missing = [
                    lib for lib in detected
                    if lib.split('/')[-1].split('@')[0].strip().lower()
                    not in ini_path.read_text(encoding="utf-8", errors="replace").lower()
                ]
                if missing:
                    self._append(
                        f"  ⚠ {len(missing)} dep(s) not yet in platformio.ini"
                        " — will be added automatically on compile.", "warning"
                    )
                else:
                    self._append("  ✔ All detected deps already in platformio.ini", "success")
        else:
            self._append("  No known library #includes detected", "dim")

        self._append("")
        self._append("  Ready. Click an action to begin.", "info")

        port_raw = self.port_var.get()
        board_raw = self.board_var.get()
        no_port = not port_raw or port_raw.startswith("─")
        no_board = not board_raw
        if no_port and no_board:
            self._append_notif("  ✖ No board/port selected — choose a board and plug in your device to enable Compile/Upload/Monitor.", "warning")
        elif no_port:
            self._append_notif("  ✖ No port selected — plug in your device and pick a port to enable Upload/Monitor.", "warning")
        elif no_board:
            self._append_notif("  ✖ No board selected — choose a board to enable Compile/Upload.", "warning")

        self._append("")
        self._set_status(f"Project ready — {self.sketch_dir_path.name}", Theme.GREEN)

    # ──────────────────────────────────────────────────────────
    # COMPILE CACHE
    # ──────────────────────────────────────────────────────────
    def _get_cache_file_path(self) -> Path:
        return self.sketch_dir_path / ".mcu_gui_cache_linux.json"

    def _hash_sources(self) -> str:
        """Return a single MD5 digest over the content of every source file
        in the current sketch folder (.ino, .cpp, .h, .c) and platformio.ini.
        Also factors in the currently selected board.
        Files are sorted by name so the hash is order-stable."""
        h = hashlib.md5()
        h.update(self.board_var.get().encode())
        source_files = []
        for ext in ["*.ino", "*.cpp", "*.h", "*.c", "platformio.ini"]:
            source_files.extend(self.sketch_dir_path.glob(ext))
        for f in sorted(source_files):
            try:
                h.update(f.name.encode())                    # include filename
                h.update(f.read_bytes())                     # include content
            except Exception:
                pass
        return h.hexdigest()

    def _save_compile_cache(self):
        """Snapshot source hashes after a successful compile and save to disk,
        keyed by board so each board keeps its own independent cache entry."""
        self._compile_cache_hash = self._hash_sources()
        self._last_compiled_board = self.board_var.get()
        if self._last_compiled_board:
            self._compile_cache_by_board[self._last_compiled_board] = self._compile_cache_hash
        if not hasattr(self, "_just_created_envs") or not isinstance(self._just_created_envs, set):
            self._just_created_envs = set()
        # Remove current env / board from newly created envs since compilation succeeded
        self._just_created_envs.discard(self._pio_env_name())
        if self._last_compiled_board:
            self._just_created_envs.discard(self._last_compiled_board)
        try:
            cache_data = {
                # Legacy single-slot fields kept for backward compatibility
                # with any older cache file / external readers.
                "hash": self._compile_cache_hash,
                "board": self._last_compiled_board,
                "boards": self._compile_cache_by_board,
                "just_created_envs": list(self._just_created_envs),
            }
            self._get_cache_file_path().write_text(json.dumps(cache_data), encoding="utf-8")
        except Exception:
            pass

    def _load_compile_cache(self):
        """Load the compile cache from disk."""
        try:
            cache_file = self._get_cache_file_path()
            if cache_file.exists():
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                self._compile_cache_hash = data.get("hash")
                self._last_compiled_board = data.get("board")
                self._compile_cache_by_board = data.get("boards") or {}
                self._just_created_envs = set(data.get("just_created_envs") or [])
                # Migrate an old single-slot cache file (no "boards" key yet)
                # into the per-board dict so it isn't silently discarded.
                if not self._compile_cache_by_board and self._last_compiled_board and self._compile_cache_hash:
                    self._compile_cache_by_board = {self._last_compiled_board: self._compile_cache_hash}
                if self._has_prior_build():
                    recompile_needed, _ = self._needs_recompile()
                    if not recompile_needed:
                        self._set_symbol_cache_compiled_state(True)
            else:
                self._compile_cache_hash = None
                self._last_compiled_board = None
                self._compile_cache_by_board = {}
                self._just_created_envs = set()
        except Exception:
            self._compile_cache_hash = None
            self._last_compiled_board = None
            self._compile_cache_by_board = {}
            self._just_created_envs = set()

    def _needs_recompile(self) -> tuple[bool, str]:
        """Return (needs_recompile, reason_string).
        False  → binary is up-to-date for the CURRENTLY selected board, safe to skip compile.
        True   → sources changed, framework missing, env just created, board never compiled,
                 or firmware binary missing from disk (e.g. cleaned manually or by board switch)."""
        board_name = self.board_var.get()
        env_name = self._pio_env_name()

        # 1. Check if board framework is downloaded
        if not self._is_framework_downloaded(board_name):
            return True, "core framework / toolchain for board is not downloaded yet"

        # 2. Check if environment was just created
        if hasattr(self, "_just_created_envs") and (env_name in self._just_created_envs or board_name in self._just_created_envs):
            return True, "environment was just created — initial compilation required"

        # 3. Check if a compiled firmware binary actually exists on disk for this board.
        #    The hash cache may say "sources unchanged" from a previous session, but if
        #    the .pio directory was cleaned (or this is a different machine), the binary
        #    is gone and we must recompile regardless.
        if not self._has_prior_build():
            return True, "no firmware binary found for this board (build folder may have been cleaned)"

        cached_hash = self._compile_cache_by_board.get(board_name)
        if cached_hash is None:
            return True, "no previous compile for this board"
        current = self._hash_sources()
        if current != cached_hash:
            return True, "source files have changed since this board was last compiled"
        return False, "sources unchanged"

    def _update_skip_compile_state(self):
        """Auto-detect if the project was already compiled.
        If yes, enable the 'Skip recompile' checkbox and check it.
        If no, disable the checkbox and uncheck it."""
        if not hasattr(self, "cb_skip_compile"):
            return
        if self._has_prior_build():
            needs_recompile, reason = self._needs_recompile()
            if not needs_recompile:
                self.skip_compile_var.set(True)
                self.cb_skip_compile.configure(state=tk.NORMAL)
                return
        self.skip_compile_var.set(False)
        self.cb_skip_compile.configure(state=tk.DISABLED)

    def _has_prior_build(self) -> bool:
        """Return True if a compiled firmware binary exists for the CURRENTLY
        selected board specifically. Used by the Skip-recompile logic to guard
        against skipping a never-built project. Each board gets its own
        .pio/build/<env> folder (see _pio_env_name), so this only reports
        True when THIS board has actually been built before — a different
        board's cached build won't false-positive this check."""
        build_dir = self.sketch_dir_path / ".pio" / "build" / self._pio_env_name()
        return (
            build_dir.exists() and (
                (build_dir / "firmware.elf").exists() or
                (build_dir / "firmware.hex").exists()
            )
        )

    # ──────────────────────────────────────────────────────────
    # COMPILE
    # ──────────────────────────────────────────────────────────
    def _get_installed_libraries_map(self) -> tuple[dict[str, str], dict[str, str]]:
        """Scan our downloaded libraries directory for installed libraries and header files.

        Returns
        -------
        libs_map   : normalized_name -> local install_dir path
        header_map : header_filename -> local install_dir path
        """
        libs_map = {}
        header_map = {}

        def normalize(name: str) -> str:
            return "".join(c for c in name.lower() if c.isalnum())

        download_dir = _get_download_dir()

        libs_dir = Path(download_dir) / "Libs"
        if not libs_dir.exists():
            return libs_map, header_map

        # Scan each subdirectory in Libs
        try:
            for item in libs_dir.iterdir():
                if item.is_dir():
                    # Auto-heal double nested folder if present
                    try:
                        subdirs = [p for p in item.iterdir() if p.is_dir()]
                        files = [p for p in item.iterdir() if p.is_file()]
                        if len(subdirs) == 1 and len(files) == 0:
                            nested = subdirs[0]
                            import shutil
                            for nested_item in nested.iterdir():
                                shutil.move(str(nested_item), str(item))
                            nested.rmdir()
                    except Exception:
                        pass

                    lib_path = str(item).replace("\\", "/")
                    lib_name = item.name
                    
                    # Try reading library.properties for correct library name if exists
                    props = item / "library.properties"
                    if props.exists():
                        try:
                            content = props.read_text(encoding="utf-8", errors="replace")
                            for line in content.splitlines():
                                if line.startswith("name="):
                                    lib_name = line.split("=", 1)[1].strip()
                                    break
                        except Exception:
                            pass
                            
                    slug = "symlink://" + lib_path
                    libs_map[normalize(lib_name)] = slug
                    
                    # Scan headers in the root directory and inside 'src'
                    search_dirs = [item, item / "src"]
                    for s_dir in search_dirs:
                        if s_dir.exists() and s_dir.is_dir():
                            for h_file in s_dir.glob("*.h"):
                                header_map[h_file.name] = slug
                                # Also map sub-directory headers (e.g. NimBLEDevice.h inside src/)
                                for h_file_deep in s_dir.rglob("*.h"):
                                    header_map[h_file_deep.name] = slug
        except Exception as e:
            self._append(f"  ⚠ Error scanning downloaded libraries: {e}", "warning")

        return libs_map, header_map

    def _get_core_headers(self, platform: str) -> set[str]:
        """Dynamically detect built-in headers for the selected platform
        by scanning the platform core files in the Boards directory.
        """
        core_headers = {
            # Standard C / C++ library
            "vector", "string", "map", "set", "list", "algorithm", "cmath",
            "cstdio", "cstdlib", "cstring", "iostream", "sstream", "memory",
            "utility", "stdint.h", "stdlib.h", "string.h", "math.h", "stdio.h",
            "stdbool.h", "time.h", "limits.h", "assert.h", "stddef.h",
            "stdarg.h", "ctype.h", "inttypes.h", "cstdint", "cstddef", "climits",
            "arduino.h", "pins_arduino.h", "pgmspace.h",
        }

        download_dir = _get_download_dir()

        boards_path = Path(download_dir) / "Boards"
        if not boards_path.is_dir():
            return core_headers

        platform_dirs = []
        for p in boards_path.glob("**/boards.txt"):
            parent_dir = p.parent
            parent_name = parent_dir.name.lower()
            
            p_platform = None
            if "esp32" in parent_name:
                p_platform = "espressif32"
            elif "esp8266" in parent_name:
                p_platform = "espressif8266"
            elif "avr" in parent_name or "uno" in parent_name:
                p_platform = "atmelavr"
                
            if p_platform == platform:
                platform_dirs.append(parent_dir)

        for p_dir in platform_dirs:
            # 1. Scan cores/
            cores_dir = p_dir / "cores"
            if cores_dir.exists() and cores_dir.is_dir():
                for h_file in cores_dir.rglob("*.h"):
                    core_headers.add(h_file.name.lower())
                    try:
                        rel = h_file.relative_to(cores_dir)
                        core_headers.add(rel.as_posix().lower())
                    except Exception:
                        pass

            # 2. Scan libraries/
            libs_dir = p_dir / "libraries"
            if libs_dir.exists() and libs_dir.is_dir():
                for h_file in libs_dir.rglob("*.h"):
                    core_headers.add(h_file.name.lower())
                    try:
                        parts = h_file.relative_to(libs_dir).parts
                        if len(parts) > 1:
                            lib_root = libs_dir / parts[0]
                            if (lib_root / "src").exists():
                                try:
                                    core_headers.add(h_file.relative_to(lib_root / "src").as_posix().lower())
                                except Exception:
                                    pass
                            try:
                                core_headers.add(h_file.relative_to(lib_root).as_posix().lower())
                            except Exception:
                                pass
                    except Exception:
                        pass

        return core_headers

    def _scan_includes_for_libs(self) -> list[str]:
        """Scan sketch files for #include statements and resolve them against
        whatever libraries are actually installed on this machine via arduino-cli.
        No hardcoded library table — the CLI output is the single source of truth.
        """
        # Resolve board platform
        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        platform = board_info.get("platform", "espressif32")
        
        # Headers that are part of the core platform or standard library —
        # they never need a lib_deps entry.
        CORE_HEADERS = self._get_core_headers(platform)

        detected_libs: list[str] = []
        if not self.sketch_dir_path.exists():
            return detected_libs

        # Collect local project header filenames so we don't chase them as libs
        local_files: set[str] = set()
        try:
            for f in self.sketch_dir_path.rglob("*"):
                if f.is_file():
                    if any(p.startswith(".") for p in
                           f.relative_to(self.sketch_dir_path).parts):
                        continue
                    local_files.add(f.name.lower())
        except Exception:
            pass

        # Ask arduino-cli once for everything installed on this machine
        installed_libs_map, installed_header_map = self._get_installed_libraries_map()

        def normalize(name: str) -> str:
            return "".join(c for c in name.lower() if c.isalnum())

        for ext in ["*.ino", "*.cpp", "*.h", "*.c"]:
            for file_path in self.sketch_dir_path.glob(ext):
                try:
                    content = file_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue

                for header in re.findall(r'#include\s*[<"]([^>"]+)[>"]', content):
                    h_lower = header.lower()

                    # Skip core/stdlib headers and local project files
                    if h_lower in CORE_HEADERS:
                        continue
                    if h_lower.startswith("esp_") or h_lower.startswith("driver/") or h_lower.startswith("soc/") or h_lower.startswith("hal/") or h_lower.startswith("freertos/") or h_lower.startswith("rom/") or h_lower.startswith("lwip/") or h_lower.startswith("mbedtls/"):
                        continue
                    if not h_lower.endswith(".h"):
                        continue
                    if h_lower in local_files:
                        continue

                    # 1. Exact header match from provides_includes (most reliable)
                    slug = installed_header_map.get(header)

                    # 2. Normalised name match (handles case variation)
                    if not slug:
                        slug = installed_libs_map.get(normalize(header[:-2]))

                    if slug and slug not in detected_libs:
                        detected_libs.append(slug)

        return detected_libs

    def _verify_board_variant_exists(self, p_platform: str, p_board: str) -> tuple[bool, str]:
        """Verify the installed framework package actually contains the
        pin-mapping header (pins_arduino.h) this board needs.

        Fully dynamic: reads PlatformIO's own board JSON manifest
        (<core_dir>/platforms/<platform>/boards/<board>.json) to find the
        board's declared 'variant', then checks every installed
        framework-* package for that variant's pins_arduino.h. Nothing
        board/platform-specific is hardcoded — this works the same for
        an ESP32-S3, an ESP8266, a Cardputer, or a board that doesn't
        exist yet.

        Returns (ok, variant_name). ok=True whenever we can't positively
        confirm a problem (missing manifest, no 'variant' key, platform
        not installed yet, etc.) — this check must never block a compile
        just because it wasn't able to look something up.
        """
        try:
            core_dir_str = os.environ.get("PLATFORMIO_CORE_DIR")
            if not core_dir_str:
                return True, ""
            core_dir = Path(core_dir_str)

            board_json = core_dir / "platforms" / p_platform / "boards" / f"{p_board}.json"
            if not board_json.exists():
                return True, ""  # nothing to verify against

            data = json.loads(board_json.read_text(encoding="utf-8", errors="replace"))
            variant = (data.get("build") or {}).get("variant")
            if not variant:
                return True, ""  # this board/platform doesn't use per-board variants

            packages_dir = core_dir / "packages"
            if not packages_dir.is_dir():
                return True, ""

            # Check if the framework package for this specific platform is installed yet.
            # If not, let PlatformIO download it automatically during the first compile.
            platform_keyword = p_platform.lower()
            if platform_keyword.startswith("atmel"):
                platform_keyword = platform_keyword[5:]  # e.g., "atmelavr" -> "avr"
            
            framework_installed = False
            for d in packages_dir.glob("framework-*"):
                if platform_keyword in d.name.lower():
                    framework_installed = True
                    break
            
            if not framework_installed:
                return True, ""  # not installed yet, let PlatformIO handle it

            # Search every installed framework-* package rather than assuming
            # which specific package name provides variants for this platform.
            found = any(packages_dir.glob(f"framework-*/variants/{variant}/pins_arduino.h"))
            return (True, "") if found else (False, variant)
        except Exception:
            return True, ""  # never block a compile due to our own check failing

    def _attempt_framework_repair(self, pio_path: list[str], p_platform: str) -> bool:
        """Force PlatformIO to reinstall the given platform + framework
        package via its own CLI. p_platform is whatever the resolved
        board actually specifies — never a hardcoded platform name — so
        this repairs any corrupted/incomplete platform, not just ESP32."""
        self._append(f"  🔄 Reinstalling '{p_platform}' platform (this can take a few minutes)...", "info")
        try:
            result = subprocess.run(
                pio_path + ["platform", "install", p_platform, "--force"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            for line in (result.stdout or "").splitlines():
                if line.strip():
                    self._append(f"    {line}", "dim")
            if result.returncode != 0:
                for line in (result.stderr or "").splitlines():
                    if line.strip():
                        self._append(f"    {line}", "error")
                return False
            return True
        except Exception as e:
            self._append(f"  ✖ Repair attempt failed: {e}", "error")
            return False

    def _resolve_board_info(self) -> dict:
        """Resolve the currently selected board name to its PlatformIO
        platform/board/framework info, with the same stale-name fallback
        used everywhere else. Centralized here so every caller (ini
        generation, framework-repair check, etc.) stays in sync instead
        of re-implementing the fallback logic — nothing about a specific
        board/platform is hardcoded, it's whatever SUPPORTED_BOARDS
        (populated dynamically from disk) currently contains.
        """
        board_name = self.board_var.get()
        # SUPPORTED_BOARDS["ESP32 Dev Module"] used to be a safe literal
        # fallback because that key was hardcoded and always present.
        # Now that ESP32/S3/8266 entries come purely from disk discovery
        # (load_dynamic_boards), that key may not exist at all -- e.g. on
        # a fresh install before "Download Boards/Libraries" has ever
        # been run. Fall back to whatever board info IS actually
        # available instead of a name that's no longer guaranteed to be
        # there: prefer any board other than Arduino Uno first (since an
        # unrecognized board_name during normal use is far more likely
        # to be a slightly-stale ESP-family name than a stale Uno one),
        # then Arduino Uno itself as the final guaranteed-present fallback.
        if board_name in SUPPORTED_BOARDS:
            return SUPPORTED_BOARDS[board_name]
        elif SUPPORTED_BOARDS:
            return next(iter(SUPPORTED_BOARDS.values()))
        else:
            return {"platform": "atmelavr", "board": "uno", "framework": "arduino"}

    def _pio_env_name(self, board_name: str | None = None) -> str:
        """Stable PlatformIO [env:...] name / .pio/build subfolder for a
        given board. Each board gets its own slug-based env name so their
        compiled outputs (and lib deps) live in separate .pio/build/<id>
        and .pio/libdeps/<id> folders instead of overwriting each other —
        switching boards and back no longer throws away the other board's
        build."""
        name = board_name if board_name is not None else self.board_var.get()
        if not name:
            return "mcu_flash"
        slug = re.sub(r'[^a-zA-Z0-9]+', '_', name).strip('_').lower()
        return f"mcu_flash_{slug}" if slug else "mcu_flash"

    def _ensure_platformio_ini(self) -> bool:
        """Ensure platformio.ini exists and has all required library dependencies."""
        ini_path = self.sketch_dir_path / "platformio.ini"

        board_info = self._resolve_board_info()
        p_platform = board_info["platform"]
        p_board = board_info["board"]
        p_framework = board_info["framework"]
        
        # 1. Scan for required libraries based on includes
        detected_libs = self._scan_includes_for_libs()
        
        # Check if WiFiProv / BLE / large-stack libraries are used, OR if the
        # board is an ESP32 / ESP32-S3 (which benefits from huge_app by default).
        # WiFiProv.h is ESP32-only and requires wifi_provisioning headers that
        # do not exist on ESP8266 — so we also gate huge_app on ESP32 platforms.
        _LARGE_STACK_HEADERS = {
            "WiFiProv.h",
            "wifi_provisioning/manager.h",
            "wifi_provisioning/scheme_softap.h",
            "wifi_provisioning/scheme_ble.h",
            "BLEDevice.h",
            "SimpleBLE.h",
            "BluetoothSerial.h",
        }
        needs_huge_app = False
        # Default huge_app for ESP32 / ESP32-S3 boards — their base firmware
        # already consumes most of the default 1.25 MB app partition, and any
        # WiFiProv / BLE sketch will overflow it at link time.
        if p_platform == "espressif32":
            needs_huge_app = True
        # Also scan source files in case the board is not yet selected but the
        # headers make the intent clear.
        if not needs_huge_app and self.sketch_dir_path.exists():
            for ext in ["*.ino", "*.cpp", "*.h", "*.c"]:
                for file_path in self.sketch_dir_path.glob(ext):
                    try:
                        content = file_path.read_text(encoding="utf-8", errors="replace")
                        if any(h in content for h in _LARGE_STACK_HEADERS):
                            needs_huge_app = True
                            break
                    except Exception:
                        pass
                if needs_huge_app:
                    break
        
        # 2. Get Arduino user libraries directory from local settings
        arduino_lib_dir = ""
        try:
            download_dir = _get_download_dir()
            arduino_lib_dir = os.path.join(download_dir, "Libs").replace("\\", "/")
        except Exception:
            pass
        
        if not ini_path.exists():
            self._append(f"  📝 platformio.ini not found in {self.sketch_dir_path}.", "warning")
            self._append("  Creating a default platformio.ini with detected dependencies...", "info")
            
            lib_deps_str = ""
            if detected_libs:
                lib_deps_str = "\nlib_deps =\n" + "\n".join(f"    {lib}" for lib in detected_libs)
                
            lib_extra_dirs_str = f"\nlib_extra_dirs = {arduino_lib_dir}" if arduino_lib_dir else ""

            # ESP32-S3 requires extra build flags for USB-Serial to work correctly.
            # Without these the Serial monitor is silent even when the sketch runs.
            #   -DARDUINO_USB_MODE=1        → use TinyUSB (not ROM CDC)
            #   -DARDUINO_USB_CDC_ON_BOOT=1 → enable CDC-over-USB on boot
            # board_build.flash_mode = dio is required on most S3 modules;
            # qio can cause boot loops on boards that don't support it.
            
            build_flags_list = [
                "-D NETWORK_PROV_SCHEME_SOFTAP=WIFI_PROV_SCHEME_SOFTAP",
                "-D NETWORK_PROV_SCHEME_HANDLER_NONE=WIFI_PROV_SCHEME_HANDLER_NONE",
                "-D NETWORK_PROV_SECURITY_1=WIFI_PROV_SECURITY_1"
            ]
            
            s3_extra = ""
            _uspd = self.upload_speed_var.get() if hasattr(self, "upload_speed_var") else "921600"
            # Arduino Uno's optiboot/stk500 bootloader only syncs at 115200 baud.
            # The upload_speed combobox is meant for esptool boards (ESP32/S3/8266) —
            # honoring a higher value here makes avrdude retry sync for ~100s before
            # giving up with "Error 1" / "avrdude: stk500_recv(): programmer is not
            # responding". Force the bootloader-correct speed for AVR instead.
            if p_board == "uno":
                _uspd = "115200"
            upload_speed_line = f"\nupload_speed = {_uspd}"
            flash_size = board_info.get("flash_size")
            has_psram = board_info.get("psram")
            if has_psram:
                build_flags_list.extend([
                    "-D BOARD_HAS_PSRAM",
                    "-mfix-esp32-psram-cache-issue"
                ])

            if is_s3_board(p_board):
                is_native = self._is_native_usb_port()
                upload_speed_line = "" if is_native else f"\nupload_speed = {_uspd}"
                if is_native:
                    build_flags_list.extend([
                        "-DARDUINO_USB_MODE=1",
                        "-DARDUINO_USB_CDC_ON_BOOT=1"
                    ])
                s3_extra = (
                    f"\nupload_protocol = esptool"
                    f"\nboard_build.flash_mode = dio"
                )

            build_flags_str = "build_flags =\n" + "\n".join(f"    {flag}" for flag in build_flags_list)
            partition_str = "board_build.partitions = huge_app.csv\n" if needs_huge_app else ""

            # Build the [env:mcu_flash] body line-by-line so no key ever gets
            # concatenated onto the tail of another key's value line.
            env_lines: list[str] = [
                f"platform = {p_platform}",
                f"board = {p_board}",
                f"framework = {p_framework}",
                "monitor_speed = 115200",
            ]
            if flash_size:
                env_lines.append(f"board_build.flash_size = {flash_size}")
                env_lines.append(f"board_upload.flash_size = {flash_size}")
            if has_psram:
                env_lines.append("board_build.arduino.memory_type = qio_opi")

            # upload_speed only for non-native-USB boards (upload_speed_line is
            # "\nupload_speed = {speed}" when set, "" when skipped)
            if upload_speed_line:
                env_lines.append(f"upload_speed = {_uspd}")
            if s3_extra:
                # s3_extra is "\nupload_protocol = esptool\nboard_build.flash_mode = dio"
                for extra_line in s3_extra.strip().splitlines():
                    env_lines.append(extra_line.strip())
            env_lines.append(build_flags_str)
            if lib_extra_dirs_str:
                env_lines.append(f"lib_extra_dirs = {arduino_lib_dir}")
            if lib_deps_str:
                env_lines.append(lib_deps_str.strip())
            if partition_str:
                env_lines.append(partition_str.strip())

            # Join with a blank line between every key block so Python's
            # configparser never treats build_flags' indented -D lines as a
            # multi-line continuation of board_build.flash_mode.  A blank line
            # always terminates a multi-line value in configparser semantics.
            env_body = "\n\n".join(env_lines)

            content = f"""; PlatformIO Project Configuration File
; Generated automatically by MCU Flasher by Naph

[platformio]
default_envs = {self._pio_env_name()}

[env:{self._pio_env_name()}]
{env_body}
"""
            try:
                ini_path.write_text(content, encoding="utf-8")
                self._append("  ✔ Created default platformio.ini successfully.", "success")
                self._append_notif(
                    f"  📄 platformio.ini created for {self.board_var.get()} in {self.sketch_dir_path}",
                    tag="success", category="pio_ini", title="platformio.ini Created"
                )
                if detected_libs:
                    self._append(f"  Detected libraries: {', '.join(detected_libs)}", "info")
                return True
            except Exception as e:
                self._append(f"  ✖ Failed to create platformio.ini: {e}", "error")
                return False
        else:
            # platformio.ini already exists. Validate it first, then update.
            try:
                content = ini_path.read_text(encoding="utf-8")
                old_content = content

                # ── Per-board env rename ────────────────────────────────────────
                # The ini historically kept editing platform=/board= in place
                # inside the SAME [env:mcu_flash] section no matter which board
                # was selected, so every board switch overwrote one shared
                # .pio/build/mcu_flash folder. Instead, rename the section (and
                # default_envs) to this board's own env name so its build
                # output lives in its own .pio/build/<env> folder and a
                # previously-built board's folder is left untouched on disk.
                target_env = self._pio_env_name()
                _env_hdr_match = re.search(r"^\[env:([^\]]*)\]", content, re.MULTILINE)
                if _env_hdr_match and _env_hdr_match.group(1) != target_env:
                    old_env = _env_hdr_match.group(1)
                    content = re.sub(
                        rf"^\[env:{re.escape(old_env)}\]",
                        f"[env:{target_env}]",
                        content, count=1, flags=re.MULTILINE
                    )
                    content = re.sub(
                        r"^default_envs\s*=.*",
                        f"default_envs = {target_env}",
                        content, count=1, flags=re.MULTILINE
                    )

                # ── Corruption guard ───────────────────────────────────────────
                # A previously malformed ini may have bare build-flag lines
                # injected immediately after [env:...] (no key = value format),
                # or monitor_speed whose value trails into build_flags text,
                # or -D / -I / -W lines that appear in the file but have no
                # preceding "build_flags =" key to own them.
                # Detect any of these symptoms and regenerate from scratch.
                _env_hdr_junk = re.compile(
                    r"^\[env:[^\]]*\]\s*\n(?:[ \t]+-[^\n]+\n)+", re.MULTILINE
                )
                _monitor_junk = re.compile(
                    r"^monitor_speed\s*=\s*\d+\s*\n[ \t]+-D\s", re.MULTILINE
                )
                # Orphaned flag lines: a -D/-I/-W/-O line at column 0 (no indent)
                # means a flag escaped from build_flags entirely.
                # NOTE: indented -D lines under build_flags are VALID and must NOT be
                # flagged — the old check caused false positives when upload_speed or
                # any other key appeared immediately before the build_flags block.
                # The _missing_build_flags check below already catches the case where
                # -D lines exist but build_flags is absent.
                _orphan_flags = re.compile(
                    r"^-[DIWOfdm]", re.MULTILINE
                )
                # -D lines present but build_flags key is completely absent
                _has_defines     = bool(re.search(r"^\s+-D\s", content, re.MULTILINE))
                _has_build_flags = bool(re.search(r"^build_flags\s*=", content, re.MULTILINE))
                _missing_build_flags = _has_defines and not _has_build_flags
                _has_platform = bool(re.search(r"^platform\s*=", content, re.MULTILINE))
                _has_board    = bool(re.search(r"^board\s*=",    content, re.MULTILINE))
                if (
                    _env_hdr_junk.search(content)
                    or _monitor_junk.search(content)
                    or _orphan_flags.search(content)
                    or _missing_build_flags
                    or not (_has_platform and _has_board)
                ):
                    self._append("  ⚠ platformio.ini is malformed — regenerating from scratch.", "warning")
                    ini_path.unlink(missing_ok=True)
                    # Also wipe the stale build cache — it was compiled against the
                    # wrong board/flags and will cause "no input files" on next build.
                    _stale_build = self.sketch_dir_path / ".pio" / "build" / target_env
                    if _stale_build.exists():
                        try:
                            robust_rmtree(_stale_build)
                            self._append(f"  🗑 Cleared stale build cache (.pio/build/{target_env}).", "warning")
                        except Exception:
                            pass
                    return self._ensure_platformio_ini()

                # Remove src_dir=. if present.  With src_dir=., PlatformIO's
                # InoToCPPConverter writes the intermediate .ino.cpp into the
                # sketch root, but SCons expects it under .pio/build/<env>/src/.
                # The mismatch leaves g++ with no input file.  Dropping src_dir
                # lets PlatformIO use its default (src/), which the GUI keeps
                # synced below via _sync_src_dir().
                content = re.sub(r"^src_dir\s*=.*\n?", "", content, flags=re.MULTILINE)

                # Update platform and board
                if re.search(r"^platform\s*=", content, re.MULTILINE):
                    content = re.sub(r"^platform\s*=.*", f"platform = {p_platform}", content, flags=re.MULTILINE)
                if re.search(r"^board\s*=", content, re.MULTILINE):
                    content = re.sub(r"^board\s*=.*", f"board = {p_board}", content, flags=re.MULTILINE)

                # Update upload_speed based on board type
                if p_board == "uno":
                    if re.search(r"^upload_speed\s*=", content, re.MULTILINE):
                        content = re.sub(r"^upload_speed\s*=.*", "upload_speed = 115200", content, flags=re.MULTILINE)
                    else:
                        content = re.sub(
                            r"(\[env:[^\]]*\]\n)",
                            r"\1upload_speed = 115200\n",
                            content, count=1
                        )
                elif is_s3_board(p_board) and self._is_native_usb_port():
                    # Native USB removes upload_speed below, so we do nothing here
                    pass
                else:
                    current_speed = self.upload_speed_var.get() if hasattr(self, "upload_speed_var") else "921600"
                    if re.search(r"^upload_speed\s*=", content, re.MULTILINE):
                        content = re.sub(r"^upload_speed\s*=.*", f"upload_speed = {current_speed}", content, flags=re.MULTILINE)
                    else:
                        content = re.sub(
                            r"(\[env:[^\]]*\]\n)",
                            r"\1upload_speed = {current_speed}\n",
                            content, count=1
                        )
                    
                if arduino_lib_dir:
                    if re.search(r"^lib_extra_dirs\s*=", content, re.MULTILINE):
                        content = re.sub(r"^lib_extra_dirs\s*=.*$", f"lib_extra_dirs = {arduino_lib_dir}", content, flags=re.MULTILINE)
                    else:
                        if not content.endswith("\n"):
                            content += "\n"
                        content += f"lib_extra_dirs = {arduino_lib_dir}\n"

                # Ensure Arduino core v3 compatibility defines are present in build_flags.
                # These work around an ESP32 Arduino-core v3 API rename in WiFiProv —
                # they don't exist on AVR and must never be injected for Uno/atmelavr.
                if p_platform == "espressif32":
                    compat_flags = [
                        "-D NETWORK_PROV_SCHEME_SOFTAP=WIFI_PROV_SCHEME_SOFTAP",
                        "-D NETWORK_PROV_SCHEME_HANDLER_NONE=WIFI_PROV_SCHEME_HANDLER_NONE",
                        "-D NETWORK_PROV_SECURITY_1=WIFI_PROV_SECURITY_1"
                    ]
                    existing_build_flags = re.search(r"^build_flags\s*=", content, re.MULTILINE)
                    if existing_build_flags:
                        # Append any missing flags to the existing build_flags block
                        for flag in compat_flags:
                            if flag not in content:
                                content = re.sub(
                                    r"(^build_flags\s*=.*(?:\n[ \t]+\S.*)*)",
                                    lambda m, f=flag: m.group(0).rstrip() + f"\n    {f}",
                                    content, count=1, flags=re.MULTILINE
                                )
                    else:
                        # No build_flags at all — append at end of file
                        flags_block = "build_flags =\n" + "".join(f"    {f}\n" for f in compat_flags)
                        if not content.endswith("\n"):
                            content += "\n"
                        content += flags_block
                else:
                    # Non-ESP32 board (e.g. Arduino Uno) — strip these defines out
                    # if a previously-generated ini already has them, since they
                    # only existed there because of this same unconditional-inject bug.
                    for _flag in (
                        "-D NETWORK_PROV_SCHEME_SOFTAP=WIFI_PROV_SCHEME_SOFTAP",
                        "-D NETWORK_PROV_SCHEME_HANDLER_NONE=WIFI_PROV_SCHEME_HANDLER_NONE",
                        "-D NETWORK_PROV_SECURITY_1=WIFI_PROV_SECURITY_1",
                    ):
                        content = content.replace(f"    {_flag}\n", "")
                    # If that emptied out the build_flags block entirely, drop the key too.
                    # The negative lookahead prevents stripping `build_flags =` when
                    # indented continuation lines (the actual flags) follow on the next line.
                    content = re.sub(r"^build_flags[ \t]*=[ \t]*$(?:\n[ \t]*$)*(?!\n[ \t]+\S)", "", content, flags=re.MULTILINE)

                # Ensure huge_app.csv partitions are selected when needed to accommodate larger libraries
                if needs_huge_app:
                    if not re.search(r"^board_build\.partitions\s*=", content, re.MULTILINE):
                        content = re.sub(
                            r"(\[env:[^\]]*\]\n)",
                            r"\1board_build.partitions = huge_app.csv\n",
                            content, count=1
                        )

                # Inject ESP32-S3 required build flags if missing.
                # These are needed for USB-Serial (Serial monitor) to work and
                # to avoid boot loops caused by wrong flash mode on S3 modules.
                if is_s3_board(p_board):
                    is_native = self._is_native_usb_port()
                    if is_native:
                        s3_flags = ["-DARDUINO_USB_MODE=1", "-DARDUINO_USB_CDC_ON_BOOT=1"]
                        existing_build_flags = re.search(r"^build_flags\s*=", content, re.MULTILINE)
                        if existing_build_flags:
                            # Append any missing flags to the existing build_flags block
                            for flag in s3_flags:
                                if flag not in content:
                                    content = re.sub(
                                        r"(^build_flags\s*=.*(?:\n[ \t]+\S.*)*)",
                                        lambda m, f=flag: m.group(0).rstrip() + f"\n    {f}",
                                        content, count=1, flags=re.MULTILINE
                                    )
                        else:
                            # No build_flags at all — add the whole block after [env:...]
                            flags_block = (
                                "build_flags =\n"
                                "    -DARDUINO_USB_MODE=1\n"
                                "    -DARDUINO_USB_CDC_ON_BOOT=1\n"
                            )
                            content = re.sub(
                                r"(\[env:[^\]]*\]\n)",
                                r"\1" + flags_block,
                                content, count=1
                            )
                    else:
                        # Remove USB CDC build flags to ensure Serial goes to the hardware UART port (CH343/CP2102)
                        content = re.sub(r"^[ \t]*-DARDUINO_USB_MODE=.*\n?", "", content, flags=re.MULTILINE)
                        content = re.sub(r"^[ \t]*-DARDUINO_USB_CDC_ON_BOOT=.*\n?", "", content, flags=re.MULTILINE)
                        # If build_flags block is empty, clean it up.
                        # The negative lookahead prevents stripping `build_flags =` when
                        # indented continuation lines (the actual flags) follow on the next line.
                        content = re.sub(r"^build_flags[ \t]*=[ \t]*$(?:\n[ \t]*$)*(?!\n[ \t]+\S)", "", content, flags=re.MULTILINE)
                    if not re.search(r"^board_build\.flash_mode\s*=", content, re.MULTILINE):
                        content = re.sub(
                            r"(\[env:[^\]]*\]\n)",
                            r"\1board_build.flash_mode = dio\n",
                            content, count=1
                        )
                    # Inject or remove PSRAM configurations based on selected board properties
                    if board_info.get("psram"):
                        if not re.search(r"^board_build\.arduino\.memory_type\s*=", content, re.MULTILINE):
                            content = re.sub(
                                r"(\[env:[^\]]*\]\n)",
                                r"\1board_build.arduino.memory_type = qio_opi\n",
                                content, count=1
                            )
                        else:
                            content = re.sub(r"^board_build\.arduino\.memory_type\s*=.*", "board_build.arduino.memory_type = qio_opi", content, flags=re.MULTILINE)
                        
                        existing_build_flags = re.search(r"^build_flags\s*=", content, re.MULTILINE)
                        psram_flags = ["-D BOARD_HAS_PSRAM", "-mfix-esp32-psram-cache-issue"]
                        if existing_build_flags:
                            for flag in psram_flags:
                                if flag not in content:
                                    content = re.sub(
                                        r"(^build_flags\s*=.*(?:\n[ \t]+\S.*)*)",
                                        lambda m, f=flag: m.group(0).rstrip() + f"\n    {f}",
                                        content, count=1, flags=re.MULTILINE
                                    )
                        else:
                            flags_block = "build_flags =\n" + "".join(f"    {f}\n" for f in psram_flags)
                            content = re.sub(
                                r"(\[env:[^\]]*\]\n)",
                                r"\1" + flags_block,
                                content, count=1
                            )
                    else:
                        # Actively remove board_build.arduino.memory_type if it is set to qio_opi to prevent boot loops on modules without Octal PSRAM
                        content = re.sub(r"^board_build\.arduino\.memory_type\s*=\s*qio_opi\s*$", "", content, flags=re.MULTILINE)
                        content = re.sub(r"^[ \t]*-D\s*BOARD_HAS_PSRAM\n?", "", content, flags=re.MULTILINE)
                        content = re.sub(r"^[ \t]*-mfix-esp32-psram-cache-issue\n?", "", content, flags=re.MULTILINE)

                    # Inject or remove custom flash size configuration based on board info
                    flash_size = board_info.get("flash_size")
                    if flash_size:
                        if not re.search(r"^board_build\.flash_size\s*=", content, re.MULTILINE):
                            content = re.sub(
                                r"(\[env:[^\]]*\]\n)",
                                f"\\1board_build.flash_size = {flash_size}\nboard_upload.flash_size = {flash_size}\n",
                                content, count=1
                            )
                        else:
                            content = re.sub(r"^board_build\.flash_size\s*=.*", f"board_build.flash_size = {flash_size}", content, flags=re.MULTILINE)
                            content = re.sub(r"^board_upload\.flash_size\s*=.*", f"board_upload.flash_size = {flash_size}", content, flags=re.MULTILINE)
                    else:
                        content = re.sub(r"^board_build\.flash_size\s*=.*\n?", "", content, flags=re.MULTILINE)
                        content = re.sub(r"^board_upload\.flash_size\s*=.*\n?", "", content, flags=re.MULTILINE)

                    # Inject upload_protocol (forced to esptool to avoid OpenOCD JTAG driver failures)
                    # and remove upload_resetmethod.
                    if re.search(r"^upload_protocol\s*=\s*(?:esp-builtin|esp-usb-jtag)\b", content, re.MULTILINE):
                        content = re.sub(r"^upload_protocol\s*=.*", "upload_protocol = esptool", content, flags=re.MULTILINE)
                    elif not re.search(r"^upload_protocol\s*=", content, re.MULTILINE):
                        content = re.sub(
                            r"(\[env:[^\]]*\]\n)",
                            r"\1upload_protocol = esptool\n",
                            content, count=1
                        )
                    # Remove upload_resetmethod as it is JTAG-specific
                    content = re.sub(r"^upload_resetmethod\s*=.*\n?", "", content, flags=re.MULTILINE)
                    # Remove upload_speed for native USB — baud rate is irrelevant
                    # and can interfere with the USB reset signaling.
                    content = re.sub(r"^upload_speed\s*=.*\n?", "", content, flags=re.MULTILINE)

                def _lib_key(slug: str) -> str:
                    # 'dhrubasaha08/DHT11 @ ^2.1.0'                       -> 'dht11'
                    # 'symlink://C:/Users/.../DHT11'                       -> 'dht11'
                    # 'C:/Users/napht/Documents/Arduino/libraries/DHT11'  -> 'dht11'
                    # Strip symlink:// prefix before extracting the key
                    s = slug
                    if s.startswith("symlink://"):
                        s = s[len("symlink://"):]
                    tail = s.replace("\\", "/").rstrip("/").split("/")[-1]
                    tail = tail.split("@")[0].strip()
                    return tail.lower()

                lib_deps_block_re = re.compile(r"^lib_deps\s*=[ \t]*\n?(?:[ \t]+\S.*\n?)*", re.MULTILINE)
                existing_match = lib_deps_block_re.search(content)
                old_entries = []
                if existing_match:
                    for line in existing_match.group(0).splitlines()[1:]:
                        s = line.strip()
                        if s:
                            old_entries.append(s)

                old_keys = {_lib_key(e) for e in old_entries}
                new_keys = {_lib_key(e) for e in detected_libs}

                rebuild_needed = (old_keys != new_keys) or any(
                    e not in detected_libs for e in old_entries if _lib_key(e) in new_keys
                )

                if rebuild_needed:
                    if detected_libs:
                        new_block = "lib_deps =\n" + "\n".join(f"    {lib}" for lib in detected_libs) + "\n"
                    else:
                        new_block = ""

                    if existing_match:
                        content = lib_deps_block_re.sub(new_block, content, count=1)
                    elif new_block:
                        if not content.endswith("\n"):
                            content += "\n"
                        content += "\n" + new_block

                    stale = [e for e in old_entries if _lib_key(e) in old_keys - new_keys]
                    added = [lib for lib in detected_libs if _lib_key(lib) in new_keys - old_keys]
                    if stale:
                        self._append(f"  📝 Removing stale/incorrect dependencies: {', '.join(stale)}", "warning")
                    if added:
                        self._append(f"  📝 Adding dependencies: {', '.join(added)}", "warning")
                    self._append("  Rebuilding lib_deps in platformio.ini...", "info")

                if content != old_content:
                    ini_path.write_text(content, encoding="utf-8")
                    if "upload_protocol = esptool" in content and "upload_protocol = esptool" not in old_content:
                        pio_dir = self.sketch_dir_path / ".pio"
                        if pio_dir.exists():
                            try:
                                robust_rmtree(pio_dir)
                                self._append("  📝 Cleared SCons build cache (.pio) to apply the new serial upload protocol.", "info")
                            except Exception:
                                pass
                else:
                    pass
                if rebuild_needed or (arduino_lib_dir and "lib_extra_dirs" in content):
                    self._append("  ✔ Updated platformio.ini successfully.", "success")
                return True
            except Exception as e:
                self._append(f"  ⚠ Failed to inspect/update existing platformio.ini: {e}", "warning")
                return True

    def _check_libraries_installed(self) -> bool:
        """Query arduino-cli for installed libraries and check if required ones are missing."""
        cli_path = find_arduino_cli_executable()
        if not cli_path:
            self._append("  ✖ Arduino-CLI not found on the computer!", "error")
            self._append("  Please install Arduino-CLI first.", "info")
            return False

        # 1. Query installed libraries map from arduino-cli
        installed_libs_map, installed_header_map = self._get_installed_libraries_map()

        # 2. Extract includes from sketch files
        required_headers = []
        if self.sketch_dir_path.exists():
            for ext in ["*.ino", "*.cpp", "*.h", "*.c"]:
                for file_path in self.sketch_dir_path.glob(ext):
                    try:
                        content = file_path.read_text(encoding="utf-8", errors="replace")
                        includes = re.findall(r'#include\s*[<"]([^>"]+)[>"]', content)
                        for header in includes:
                            if header not in required_headers:
                                required_headers.append(header)
                    except Exception:
                        pass

        # Builtin/standard libraries to ignore
        BUILTIN_AND_STD = {
            # Standard C/C++ headers
            "vector", "string", "map", "set", "list", "algorithm", "cmath", "cstdio", "cstdlib",
            "cstring", "iostream", "sstream", "memory", "utility", "stdint.h", "stdlib.h",
            "string.h", "math.h", "stdio.h", "stdbool.h", "time.h", "limits.h", "assert.h",
            "stddef.h", "stdarg.h", "ctype.h", "inttypes.h", "cstdint", "cstddef", "climits",
            # Arduino/ESP32 core standard builtins
            "arduino.h", "wifi.h", "wire.h", "spi.h", "fs.h", "sd.h", "bledevice.h",
            "update.h", "webserver.h", "wificlient.h", "wificlientsecure.h", "ticker.h",
            "spiffs.h", "littlefs.h", "eeprom.h", "preferences.h", "soc/soc.h",
            "driver/adc.h", "esp_adc_cal.h", "soc/rtc_cntl_reg.h", "pgmspace.h",
            "freertos/freertos.h", "freertos/task.h", "esp_system.h", "esp_spi_flash.h",
            "esp_partition.h", "esp_ota_ops.h", "nvs_flash.h", "nvs.h", "pins_arduino.h",
            "esp_bt.h", "esp_bt_main.h", "esp_gap_ble_api.h", "esp_gatts_api.h",
            "esp_gatt_common_api.h", "esp_bt_device.h", "esp_gap_bt_api.h",
            "hardwareserial.h",
            # softwareserial.h is NOT in CORE_HEADERS: on ESP32/ESP8266 it
            # requires the EspSoftwareSerial lib_dep — let the scanner find it.
            "sd_mmc.h", "wifiap.h",
            "wifimulti.h", "wifiscan.h", "wifiserver.h", "ethernet.h", "client.h",
            "server.h", "stream.h", "print.h", "printable.h", "wstring.h",
            "ipaddress.h", "ipv6address.h", "wifiudp.h",
            "wifiprov.h", "netbios.h", "espmdns.h", "simpleble.h", "bluetoothserial.h", "httpclient.h",
            # ESP32 specific / built-in standard sub-headers
            "http_client.h", "wifiudp.h", "dnsserver.h", "esp_wifi.h", "esp_event.h",
            "esp_log.h", "lwip/err.h", "lwip/sockets.h", "lwip/sys.h", "lwip/netdb.h",
            "lwip/dns.h", "mbedtls/aes.h", "mbedtls/entropy.h", "mbedtls/ctr_drbg.h",
            "mbedtls/md.h", "mbedtls/sha256.h", "rom/ets_sys.h", "esp_sleep.h",
            "hal/gpio_hal.h", "driver/gpio.h", "driver/ledc.h", "driver/uart.h",
            "driver/spi_master.h", "driver/i2c.h", "driver/timer.h", "driver/pcnt.h",
            "driver/mcpwm.h", "driver/rmt.h", "driver/pulse_cnt.h", "driver/sdspi_host.h",
            "driver/sdmmc_host.h", "sdmmc_cmd.h", "esp_vfs.h", "esp_vfs_fat.h",
            "fatfs_vfs.h", "ff.h", "diskio.h", "esp_spiffs.h", "esp_littlefs.h",
            "esp_camera.h", "fd_forward.h", "fr_forward.h", "image_util.h"
        }

        # Override mappings from header file to library name (internal key format for matching)
        HEADER_TO_LIB_NAME = {
            "ESP32Servo.h": "ESP32Servo",
            "FastAccelStepper.h": "FastAccelStepper",
            "HX711.h": "HX711",
            "NimBLEDevice.h": "NimBLE-Arduino",
            "OneWire.h": "OneWire",
            "DallasTemperature.h": "DallasTemperature",
            "Adafruit_Sensor.h": "Adafruit Unified Sensor",
            "DHT.h": "DHT sensor library",
            "PubSubClient.h": "PubSubClient",
            "ArduinoJson.h": "ArduinoJson",
            "TFT_eSPI.h": "TFT_eSPI",
            "TinyGsmClient.h": "TinyGSM",
            "FirebaseESP32.h": "Firebase ESP32 Client",
            "addons/TokenHelper.h": "Firebase ESP32 Client",
            "addons/RTDBHelper.h": "Firebase ESP32 Client",
        }

        # Collect local files to avoid false alarms on local headers
        local_files = []
        try:
            for f in self.sketch_dir_path.rglob("*"):
                if f.is_file():
                    # Skip files in hidden directories (like .pio or .git)
                    if any(part.startswith(".") for part in f.relative_to(self.sketch_dir_path).parts):
                        continue
                    local_files.append(f.name.lower())
        except Exception:
            pass

        missing_libs = []
        def normalize_lib_name(name: str) -> str:
            return "".join(c for c in name.lower() if c.isalnum())

        for header in required_headers:
            h_lower = header.lower()
            # Skip C++ standard libraries, built-in ESP32 core libraries, and local files
            if h_lower in BUILTIN_AND_STD:
                continue
            if h_lower.startswith("esp_") or h_lower.startswith("driver/") or h_lower.startswith("soc/") or h_lower.startswith("hal/") or h_lower.startswith("freertos/") or h_lower.startswith("rom/") or h_lower.startswith("lwip/") or h_lower.startswith("mbedtls/"):
                continue
            if not h_lower.endswith(".h") or h_lower in local_files:
                continue

            # Determine the display name and search key
            lib_display_name = HEADER_TO_LIB_NAME.get(header, header[:-2]) # remove .h if not in override map
            norm_key = normalize_lib_name(lib_display_name)
            
            # Check if this library is in the list of installed libraries
            if header in installed_header_map:
                continue

            if norm_key not in installed_libs_map:
                # Also try checking if the header itself without .h matches (just in case of overrides mismatch)
                alt_norm_key = normalize_lib_name(header[:-2])
                if alt_norm_key not in installed_libs_map:
                    missing_libs.append((lib_display_name, header))

        if missing_libs:
            self._append("  ✖ Cannot compile: Missing required Arduino libraries!", "error")
            for lib_name, header in missing_libs:
                self._append(f"    • Library '{lib_name}' is not installed (required by #include <{header}>)", "warning")
            self._append("  Please install the missing libraries via the Download Boards/Libraries manager at the top-right of the window.", "info")
            return False

        return True

    def _validate_entry_points(self) -> bool:
        """Check that the project defines setup() and loop() exactly once.

        Arduino/ESP32 compiles all .ino files in the sketch folder into a
        single translation unit, so across the entire set of .ino files there
        must be EXACTLY ONE setup() and EXACTLY ONE loop().

        Rules:
        • Project-wide (all .ino files combined): exactly 1 setup(), 1 loop().
          - 0 of either  → error: missing entry point.
          - 2+ of either → error: duplicate definition, linker will reject it.
        • .cpp / .c files: checked project-wide only when no .ino files exist
          (pure C++ PlatformIO project).
        • .h files: never required to carry these; ignored for entry-point check.
        • A completely blank file (or whitespace-only) is flagged as a warning
          regardless of type.
        """
        # Matches a function *definition* (has opening brace), not a forward
        # declaration.  Allows optional whitespace everywhere.
        SETUP_RE = re.compile(r'\bvoid\s+setup\s*\(\s*\)\s*\{', re.MULTILINE)
        LOOP_RE  = re.compile(r'\bvoid\s+loop\s*\(\s*\)\s*\{', re.MULTILINE)

        ino_files = sorted(self.sketch_dir_path.glob("*.ino"))
        cpp_files = sorted(self.sketch_dir_path.glob("*.cpp"))
        c_files   = sorted(self.sketch_dir_path.glob("*.c"))
        h_files   = sorted(self.sketch_dir_path.glob("*.h"))

        all_source = ino_files + cpp_files + c_files + h_files

        if not all_source:
            self._append("  ✖ No source files found in project folder.", "error")
            return False

        # ── Read all files ───────────────────────────────────────────────────
        file_contents: dict[Path, str] = {}
        blank_files:   list[Path]      = []

        for f in all_source:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                self._append(f"  ⚠ Could not read {f.name}: {e}", "warning")
                continue
            file_contents[f] = text
            if not text.strip():
                blank_files.append(f)

        # Warn about blank files (informational, not a hard stop on its own)
        for bf in blank_files:
            self._append(f"  ⚠ {bf.name} is empty — no code inside.", "warning")

        problems_found = False

        # ── .ino path: count definitions across ALL .ino files combined ──────
        if ino_files:
            # Track which file each definition lives in for useful error messages
            setup_owners: list[str] = []
            loop_owners:  list[str] = []

            for f in ino_files:
                text = file_contents.get(f, "")
                if SETUP_RE.search(text):
                    setup_owners.append(f.name)
                if LOOP_RE.search(text):
                    loop_owners.append(f.name)

            # ── setup() ─────────────────────────────────────────────────────
            if len(setup_owners) == 0:
                problems_found = True
                self._append("  ✖ No void setup() {} found in any .ino file.", "error")
                self._append(
                    "     Arduino requires exactly one setup() across all sketch files.",
                    "warning"
                )
                self._append("     Add this to your main .ino file:", "info")
                self._append("       void setup() { }", "dim")

            elif len(setup_owners) > 1:
                problems_found = True
                self._append(
                    f"  ✖ void setup() defined in {len(setup_owners)} files — only one is allowed:",
                    "error"
                )
                for fname in setup_owners:
                    self._append(f"       • {fname}", "warning")
                self._append(
                    "     Remove setup() from all but one file.", "info"
                )

            # ── loop() ──────────────────────────────────────────────────────
            if len(loop_owners) == 0:
                problems_found = True
                self._append("  ✖ No void loop() {} found in any .ino file.", "error")
                self._append(
                    "     Arduino requires exactly one loop() across all sketch files.",
                    "warning"
                )
                self._append("     Add this to your main .ino file:", "info")
                self._append("       void loop()  { }", "dim")

            elif len(loop_owners) > 1:
                problems_found = True
                self._append(
                    f"  ✖ void loop() defined in {len(loop_owners)} files — only one is allowed:",
                    "error"
                )
                for fname in loop_owners:
                    self._append(f"       • {fname}", "warning")
                self._append(
                    "     Remove loop() from all but one file.", "info"
                )

            # All good — show confirmation
            if not problems_found:
                owner_set = set(setup_owners) | set(loop_owners)
                owners_str = ", ".join(sorted(owner_set))
                ino_count  = len(ino_files)
                self._append(
                    f"  ✔ Entry points OK — setup()/loop() found in: {owners_str}"
                    + (f"  ({ino_count} .ino files total)" if ino_count > 1 else ""),
                    "success"
                )
                self._append_notif(
                    f"  ✔ Entry points OK — setup()/loop() found in: {owners_str}",
                    tag="success", category="entry_points", title="Entry Points Verified"
                )

        # ── Pure C++/C path (no .ino files) ─────────────────────────────────
        else:
            cpp_c_files   = cpp_files + c_files
            all_cpp_text  = "\n".join(file_contents.get(f, "") for f in cpp_c_files)
            project_has_setup = bool(SETUP_RE.search(all_cpp_text))
            project_has_loop  = bool(LOOP_RE.search(all_cpp_text))

            if not project_has_setup or not project_has_loop:
                problems_found = True
                missing = []
                if not project_has_setup:
                    missing.append("void setup() {}")
                if not project_has_loop:
                    missing.append("void loop() {}")
                self._append(
                    f"  ✖ Project is missing: {', '.join(missing)}", "error"
                )
                self._append(
                    "     No .ino file found; Arduino entry points must be defined "
                    "in a .cpp or .c source file.", "warning"
                )

        if problems_found:
            self._append("")
            self._append("  ✖ Fix the issues above before compiling.", "error")
            return False

        return True

    def _sync_src_dir(self) -> None:
        """Keep sketch_dir/src/ in sync with the sketch source files.

        PlatformIO's default src_dir is 'src/'.  We abandoned src_dir=. because
        InoToCPPConverter writes the intermediate .ino.cpp next to the .ino,
        but SCons looks for it under .pio/build/<env>/src/ — a path mismatch that
        causes 'no input files' from g++.  Using the default src/ layout fixes
        that: PlatformIO finds the .ino in src/, writes .ino.cpp there, and SCons
        correctly variant-copies it into the build tree.

        We hard-link each .ino/.cpp/.h/.c from the sketch root into src/ so that:
          • Edits to the originals are immediately visible (hard-link, same inode).
          • PlatformIO never touches files outside its own src/ + build dirs.
          • No stale files accumulate (any file in src/ that no longer has a match
            in the sketch root is removed).
        Falls back to a plain copy on systems that don't support hard links.
        """
        src_dir = self.sketch_dir_path / "src"
        src_dir.mkdir(exist_ok=True)

        sketch_files: dict[str, Path] = {}
        for ext in ("*.ino", "*.cpp", "*.c", "*.h"):
            for f in self.sketch_dir_path.glob(ext):
                sketch_files[f.name] = f

        # Add / update links for current sketch files
        for name, src_path in sketch_files.items():
            dst_path = src_dir / name
            needs_update = not dst_path.exists()
            if not needs_update:
                try:
                    needs_update = dst_path.stat().st_ino != src_path.stat().st_ino
                except OSError:
                    needs_update = True
            if needs_update:
                dst_path.unlink(missing_ok=True)
                try:
                    os.link(src_path, dst_path)
                except OSError:
                    import shutil as _sh
                    _sh.copy2(src_path, dst_path)

        # Remove any stale entries that no longer have a source file
        for dst_path in list(src_dir.iterdir()):
            if dst_path.name not in sketch_files:
                try:
                    dst_path.unlink()
                except OSError:
                    pass

        # ── Interrupted-compile recovery ──────────────────────────────────
        # After a mid-compile kill PlatformIO may leave behind:
        #   (a) .ino.cpp MISSING  + .sconsign.dblite PRESENT
        #       → SCons trusts its cache, skips InoToCPPConverter, g++ gets no input
        #   (b) .ino.cpp PRESENT but corrupt/partial
        #       → g++ produces "Error 1" with no error message at ~1.7 s
        #
        # The correct trigger is: .sconsign.dblite exists BUT a corresponding
        # .ino.cpp is absent — that combination is only possible after a kill.
        # On a clean first-run neither file exists, so we do nothing.
        # On a normal re-compile both exist and are consistent — also do nothing.
        # Only the mismatched state (db present, cpp absent) needs recovery.
        #
        # Recovery: delete the stale .sconsign.dblite so SCons rebuilds its
        # dependency graph from scratch, and delete any partial .ino.cpp so
        # InoToCPPConverter regenerates it cleanly.
        sconsign = (
            self.sketch_dir_path / ".pio" / "build" / self._pio_env_name() / ".sconsign.dblite"
        )
        ino_files = [f for f in sketch_files.values() if f.suffix == ".ino"]
        interrupted = sconsign.exists() and any(
            not (src_dir / (f.name + ".cpp")).exists() for f in ino_files
        )
        if interrupted:
            # Wipe the stale SCons signature DB
            try:
                sconsign.unlink()
            except OSError:
                pass
            # Delete any partial/corrupt .ino.cpp files left in src/
            for f in list(src_dir.iterdir()):
                if f.suffix == ".cpp" and f.stem.endswith(".ino"):
                    try:
                        f.unlink()
                    except OSError:
                        pass

    def _run_compile(self, is_upload: bool = False) -> bool:
        self._stop_requested = False
        self._op_session_id = getattr(self, "_op_session_id", 0) + 1
        is_clean_retry = getattr(self, "_clean_retry_in_progress", False)
        self._clean_retry_in_progress = False
        ensure_platformio_penv_with_hook()

        current_board = self.board_var.get() if hasattr(self, 'board_var') else None

        # Clean compile dir only when switching boards to keep incremental
        # builds fast. Each board's build lives in its own .pio/build/<env>
        # folder — so switching back to a previously-compiled board reuses
        # its cached build. Only wipe when the new board has never been built.
        if self._last_compiled_board != current_board:
            if self._has_prior_build():
                self._append("  ♻ Board switch — reusing cached build for this board.", "info")
                self._append("  ℹ Incremental build enabled (only changed files will recompile).", "info")
            else:
                removed, clean_errors = self._perform_clean_current_board()
                self._append("")
                self._append("  🧹 Clean (pre-compile due to board switch)", "header")
                if removed:
                    self._append(f"  Removed: {', '.join(removed)}", "success")
                    self._append_notif(
                        f"  🧹 Pre-compile clean: removed {len(removed)} build artifact(s) (board: {current_board})",
                        tag="info", category="clean", title="Build Cache Cleared"
                    )
                else:
                    self._append("  Nothing to remove — this board already clean.", "info")
                    self._append_notif(
                        f"  🧹 Pre-compile clean: nothing to remove — {current_board} already clean.",
                        tag="dim", category="clean", title="Build Cache Already Clean"
                    )
                for e in clean_errors:
                    self._append(f"  ⚠ Could not remove {e}", "warning")
        else:
            self._append("  ℹ Keeping cached build artifacts (incremental build enabled).", "info")

        if not self._ensure_platformio_ini():
            self.is_busy = False
            self._set_buttons_state(False)
            self._set_status("Error: platformio.ini missing", Theme.RED)
            return False

        # Check if all required libraries are installed on the computer
        if not self._check_libraries_installed():
            self.is_busy = False
            self._set_buttons_state(False)
            self._set_status("Error: Missing libraries", Theme.RED)
            return False

        # Validate that setup() and loop() are defined (catches blank/incomplete files)
        if not self._validate_entry_points():
            self.is_busy = False
            self._set_buttons_state(False)
            self._set_status("Error: Missing setup() / loop()", Theme.RED)
            return False

        self.is_busy = True
        self._framework_download_active = False
        self._set_buttons_state(True, operation="compile")
        self._set_status("Compiling...", Theme.YELLOW)

        self._append("")
        self._append("=" * 50, "header")
        self._append("  ⚙  COMPILING (PlatformIO)", "header")
        self._append("=" * 50, "header")
        self._append(f"  Sketch : {self.sketch_dir_path}", "dim")
        self._append(f"  Tool   : PlatformIO Core", "dim")
        self._append("")

        # Emit compatibility warning inside the COMPILING section
        pending_compat = getattr(self, "_pending_compat_reasons", [])
        if pending_compat:
            self._append("  ⚠ Compatibility warning — board/sketch mismatch or warnings detected!", "warning")
            for r in pending_compat:
                self._append(f"    ℹ {r}", "warning")
            self._append("")
            self._pending_compat_reasons = []  # consumed


        pio_path = find_pio_executable()
        if not pio_path:
            self._append("  ⚠ PlatformIO not found — installing automatically...", "warning")
            self._set_status("Installing PlatformIO...", Theme.YELLOW)
            pio_path = ensure_platformio()
            if not pio_path:
                self._append("  ✖ Failed to install PlatformIO!", "error")
                self._append("  Try manually: pip install platformio", "info")
                self.is_busy = False
                self._set_buttons_busy(False)
                self._set_status("Error: PlatformIO not found", Theme.RED)
                return False
            self._append(f"  ✔ PlatformIO installed: {' '.join(pio_path)}", "success")

        # ── Self-heal: verify the framework package actually has this
        # board's pin-mapping file before we spend time compiling only to
        # hit "pins_arduino.h: No such file" deep into the build. Fully
        # dynamic — reads the board's own manifest, no board/platform
        # names hardcoded here.
        board_info = self._resolve_board_info()
        variant_ok, missing_variant = self._verify_board_variant_exists(
            board_info["platform"], board_info["board"]
        )
        if not variant_ok:
            self._append(
                f"  ⚠ Framework package looks incomplete — missing pin-mapping for variant '{missing_variant}'.",
                "warning"
            )
            self._append("  This usually means a previous framework download was interrupted or corrupted.", "dim")
            if self._attempt_framework_repair(pio_path, board_info["platform"]):
                variant_ok, _ = self._verify_board_variant_exists(
                    board_info["platform"], board_info["board"]
                )
                if variant_ok:
                    self._append("  ✔ Framework repaired successfully — continuing compile.", "success")
                else:
                    self._append(
                        "  ✖ Repair did not resolve the issue — the installed platform may not "
                        "support this board/variant.", "error"
                    )
                    self.is_busy = False
                    self._set_buttons_state(False)
                    self._set_status("Error: framework repair failed", Theme.RED)
                    return False
            else:
                self._append("  ✖ Automatic repair failed. Try deleting the platform's package folder manually and recompiling.", "error")
                self.is_busy = False
                self._set_buttons_state(False)
                self._set_status("Error: framework repair failed", Theme.RED)
                return False

        jobs = self._get_cpu_cores_jobs()
        self._append(f"  ⚡ Running parallel compilation on {jobs} cores...", "info")

        # The pre-compile clean above already wiped this board's own
        # .pio/build/<env>, so this is always effectively a fresh build for
        # whichever board is currently selected — other boards' cached
        # builds are untouched and will be picked up as-is if/when you
        # switch back to them with an unchanged sketch.
        self._append("  ℹ Fresh build for this board.", "info")
        self._append("    PlatformIO will build the core framework from scratch, which takes longer.", "dim")
        self._append("    Subsequent uploads without source changes can reuse it.", "dim")
        self._append("")

        # Sync sketch files into src/ so PlatformIO uses its default src_dir.
        # Without this, src_dir=. causes InoToCPPConverter to write .ino.cpp
        # into the sketch root while SCons expects it under .pio/build/<env>/src/,
        # resulting in g++ receiving no input file.
        self._sync_src_dir()

        cmd = pio_path + [
            "run",
            "-j", str(jobs)
        ]

        import os
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PLATFORMIO_UNBUFFERED"] = "1"

        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
                cwd=str(self.sketch_dir_path),
                env=env,
            )
        except FileNotFoundError:
            self._append("  ✖ PlatformIO executable not found at: " + ' '.join(pio_path), "error")
            self._append("  Try manually: pip install platformio", "info")
            self.is_busy = False
            self._set_buttons_busy(False)
            self._set_status("Error: PlatformIO not found", Theme.RED)
            return False

        output_lines = []
        line_count = 0
        compile_start = time.time()
        _build_start   = [None]   # set to time.time() the instant framework download finishes
        spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        was_killed = False

        # Read the merged stdout+stderr stream via a queue so the main
        # loop can process lines with a timeout (for the spinner).
        # stderr=subprocess.STDOUT ensures GCC diagnostics are never lost.
        import queue as _queue
        import threading as _threading

        _line_queue: _queue.Queue = _queue.Queue()

        def _reader(stream, q):
            try:
                for line in iter(stream.readline, ''):
                    if line:
                        q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)  # sentinel

        _t_out = _threading.Thread(target=_reader, args=(self.process.stdout, _line_queue), daemon=True)
        _t_out.start()

        # ── Spinner thread ────────────────────────────────────────────────────
        # Drive the spinner from a dedicated timer thread so it keeps ticking
        # even when GCC produces no output for several seconds (e.g. during
        # LTO, framework archive linking, or slow first-time builds).
        _spinner_active = [True]
        _spin_frame     = [0]

        def _spin_loop():
            while _spinner_active[0]:
                if getattr(self, "_framework_download_active", False):
                    elapsed = int(time.time() - compile_start)
                    frame   = spinner[_spin_frame[0] % len(spinner)]
                    _spin_frame[0] += 1
                    self._set_status(
                        f"{frame} Downloading Framework... ({elapsed}s elapsed)",
                        Theme.YELLOW,
                    )
                else:
                    # Only show the pure build timer — downloading is done and accounted for
                    build_elapsed = int(time.time() - (_build_start[0] or compile_start))
                    frame   = spinner[_spin_frame[0] % len(spinner)]
                    _spin_frame[0] += 1
                    self._set_status(
                        f"{frame} Compiling... ({build_elapsed}s)",
                        Theme.YELLOW,
                    )
                time.sleep(0.08)

        _spin_thread = _threading.Thread(target=_spin_loop, daemon=True)
        _spin_thread.start()

        _sentinels_remaining = 1           # only one reader thread (merged stdout+stderr)
        _process_exited_at = None          # timestamp when we first see poll()!=None

        _in_error_block = [False]

        # Tracks the last displayed SCons progress text so consecutive identical
        # lines (one "Archiving <lib>.a" per library) don't show up as repeated
        # redundant rows in the console.
        _last_progress_text = [None]

        # One-time hint flag for PlatformIO's non-fatal build-cache clean
        # failures (WinError 145 etc.) — see is_nonfatal_pio_clean_report.
        _clean_warn_shown = [False]
        _error_block_type = ["error"]

        def _classify_and_display(stripped):
            """Classify a single PlatformIO / GCC output line and display it using a stateful parser."""
            low = stripped.lower()

            # When a board mismatch was detected pre-compile, suppress all
            # error / warning / context noise — only let progress lines through.
            if getattr(self, '_board_mismatch_detected', False):
                # Still show SCons progress (Compiling, Building, Linking, result)
                if any(kw in low for kw in ('compiling', 'building', 'linking',
                                             'archiving', 'took')):
                    pass  # fall through to normal progress handling below
                else:
                    return  # suppress everything else

            # Patterns that indicate a real linker/compiler error even without
            # the word "error" in the line (e.g. ld's "undefined reference to")
            LINKER_ERROR_HINTS = (
                "undefined reference to",
                "multiple definition of",
                "cannot find -l",
                "undefined symbol",
                "duplicate symbol",
                "ld returned",
                "collect2",
            )
            is_linker_error = any(hint in low for hint in LINKER_ERROR_HINTS)

            # GCC/Clang diagnostic format: <file>:<line>:<col>: error: <msg>
            is_gcc_diagnostic = bool(re.search(r':\d+:\d+:\s+(error|warning|note|fatal error)\s*:', low))

            # ── SCons make-error wrapper: "*** [...] Error N" ─────────────────
            is_scons_wrapper = bool(re.search(r'^\*\*\*\s+\[', stripped))

            # SCons or PlatformIO administrative/progress markers that should terminate
            # a compiler error tracking block.
            is_scons_progress = (
                "compiling" in low or
                "archiving" in low or
                "linking" in low or
                "building" in low or
                "took" in low or
                low.startswith("platform:") or
                low.startswith("hardware:") or
                low.startswith("package") or
                low.startswith("embedded") or
                low.startswith("configuration") or
                low.startswith("sdk") or
                "ram:" in low or
                "flash:" in low or
                stripped.startswith("===") or
                stripped.startswith("---") or
                is_scons_wrapper
            )

            if is_scons_progress:
                _in_error_block[0] = False
                # Real build progress (compiling/linking/archiving/building/took)
                # means the download/install phase is over. Clear the flag so the
                # status bar and spinner switch back to "Compiling..." instead of
                # staying stuck on "Framework Downloading/Installing...".
                if getattr(self, "_framework_download_active", False):
                    self._framework_download_active = False
                    # Start the pure-build timer NOW
                    if _build_start[0] is None:
                        _build_start[0] = time.time()
                    try:
                        self.btn_stop.configure(state=tk.NORMAL)
                    except Exception:
                        pass

            # Context headers indicate where the error occurred (e.g., function, file stack)
            is_context_header = (
                "in function" in low or
                "in member function" in low or
                "in constructor" in low or
                "in destructor" in low or
                "at global scope" in low or
                "in file included from" in low or
                low.startswith("from ") or
                low.startswith("in file included")
            )

            if is_gcc_diagnostic or is_context_header:
                _in_error_block[0] = True
                if "warning" in low and "error" not in low:
                    _error_block_type[0] = "warning"
                elif "note" in low:
                    _error_block_type[0] = "info"
                else:
                    _error_block_type[0] = "error"

            # Show meaningful lines to user
            if is_nonfatal_pio_clean_report(stripped):
                # PlatformIO's rmtree retry report (e.g. "[WinError 145] The
                # directory is not empty") — non-fatal, build continues.
                _in_error_block[0] = False
                self._append(f"  ⚠ {stripped}", "warning")
                if not _clean_warn_shown[0]:
                    _clean_warn_shown[0] = True
                    self._append(
                        "  ℹ Non-fatal: a stale build cache could not be fully cleaned "
                        "(file in use or antivirus scan). PlatformIO will continue and "
                        "rebuild the affected parts automatically.",
                        "info",
                    )
            elif is_linker_error:
                self._append(f"  ✖ {stripped}", "error")
                _in_error_block[0] = False
            elif is_gcc_diagnostic:
                if _error_block_type[0] == "info":
                    self._append(f"  ℹ {stripped}", "info")
                elif _error_block_type[0] == "warning":
                    self._append(f"  ⚠ {stripped}", "warning")
                else:
                    self._append(f"  ✖ {stripped}", "error")
            elif _in_error_block[0]:
                # Indent non-header context/caret/code lines for cleaner hierarchy
                prefix = "    " if is_context_header else "      "
                self._append(f"{prefix}{stripped}", _error_block_type[0])
            elif is_scons_wrapper:
                pass  # swallow — SCons wrapper contains no diagnostic detail
            elif "error" in low and "werror" not in low:
                self._append(f"  ✖ {stripped}", "error")
            elif "warning" in low:
                self._append(f"  ⚠ {stripped}", "warning")
            elif any(kw in low for kw in ("tool manager:", "platform manager:", "library manager:", "downloading", "unpacking", "installing", "removing", "processing")):
                # "Library Manager:" lines just mean PlatformIO is syncing this
                # project's lib_deps (ESP32Servo, NimBLE-BLE, HX711, etc.) from
                # the local Libs folder into .pio/libdeps. That's a normal,
                # fast, purely-local copy that happens on basically every
                # compile once a project has any lib_deps — it is NOT touching
                # the core framework/toolchain, so it should not trip the
                # "do NOT stop the compile" safety banner. Only Tool Manager
                # (toolchains/compilers) and Platform Manager (framework/board
                # packages) downloads are the risky, network-heavy operations
                # that banner is meant to protect against.
                is_library_manager_line = "library manager:" in low
                is_core_framework_event = (
                    "tool manager:" in low or "platform manager:" in low
                )

                # Check if we should activate framework download safety mode
                if is_core_framework_event:
                    if not getattr(self, "_framework_download_active", False):
                        self._framework_download_active = True
                        self._append("", "")
                        self._append("  ⚠ CRITICAL: Downloading/Installing core framework or tools...", "warning")
                        self._append("    This might take a while and is highly important. Please do NOT stop the compile", "info")
                        self._append("    or close the app to prevent PlatformIO core/toolchain corruption.", "warning")
                        self._append("", "")
                        
                        # Disable the stop button to prevent interruption
                        self.btn_stop.configure(state=tk.DISABLED)
                elif is_library_manager_line:
                    # Quiet, informational only — no critical banner, no stop-button lock.
                    self._append(f"    {stripped}", "info")
                    return

                # Format progress bar nicely
                if "downloading" in low or "unpacking" in low:
                    pcts = re.findall(r'(\d+)%', stripped)
                    if pcts:
                        pct = int(pcts[-1])
                        bar_len = 30
                        filled = int(pct / 100 * bar_len)
                        empty = bar_len - filled
                        bar = "=" * filled + " " * empty
                        prefix = "Downloading" if "downloading" in low else "Unpacking"
                        progress_text = f"    {prefix} [{bar}] {pct}%"
                        self._append_progress(progress_text, "success")
                        return

                self._append(f"    {stripped}", "info")
            elif is_scons_progress:
                _prog_text = None
                _prog_tag = "dim"
                if "linking" in low:
                    _prog_text, _prog_tag = "  🔗 Linking...", "dim"
                elif "compiling" in low:
                    match = re.search(r'compiling\s+(.+)$', low)
                    if match:
                        filename = Path(match.group(1)).name
                        _prog_text = f"  ⚙ Compiling {filename}..."
                        _prog_tag = "info"
                    else:
                        _prog_text = f"  ⚙ {stripped}"
                        _prog_tag = "info"
                elif "archiving" in low:
                    _prog_text, _prog_tag = "  📦 Archiving...", "dim"
                elif "building" in low:
                    _prog_text, _prog_tag = "  ⚙ Building...", "info"
                elif "took" in low and ("success" in low or "failed" in low):
                    _prog_text = f"  {stripped}"
                    _prog_tag = "success" if "success" in low else "error"

                if _prog_text is None:
                    return
                # Suppress consecutive duplicate progress rows
                if _prog_text == _last_progress_text[0]:
                    return
                _last_progress_text[0] = _prog_text
                self._append(_prog_text, _prog_tag)

        while _sentinels_remaining > 0:
            try:
                line = _line_queue.get(timeout=0.1)
            except _queue.Empty:
                # After the process exits, give the reader threads a generous
                # window to flush any remaining data (especially stderr which
                # may contain the actual GCC error messages).  Only bail out
                # after 5 s of no new data post-exit.
                if self.process.poll() is not None:
                    if _process_exited_at is None:
                        _process_exited_at = time.time()
                    elif time.time() - _process_exited_at > 5:
                        break
                continue

            if line is None:
                _sentinels_remaining -= 1
                continue

            # Reset the post-exit timer whenever we receive real data,
            # so we wait a full 5 s after the *last* line, not after exit.
            _process_exited_at = None

            stripped = line.rstrip()
            if not stripped:
                continue
            output_lines.append(stripped)
            line_count += 1
            _classify_and_display(stripped)

        # Wait for reader thread to finish flushing any remaining data.
        _t_out.join(timeout=5)

        # ── Drain any lines that arrived after the main loop exited ──────────
        # This catches late-arriving stderr content (GCC error messages) that
        # the reader threads enqueued after we broke out of the loop above.
        while not _line_queue.empty():
            try:
                line = _line_queue.get_nowait()
            except _queue.Empty:
                break
            if line is None:
                continue
            stripped = line.rstrip()
            if not stripped:
                continue
            output_lines.append(stripped)
            _classify_and_display(stripped)

        # Stop the spinner thread before reading the final return code
        _spinner_active[0] = False
        _spin_thread.join(timeout=1)

        self.process.wait()
        total_sec   = round(time.time() - compile_start, 1)
        if _build_start[0] is not None:
            build_sec  = max(0.0, min(round(time.time() - _build_start[0], 1), total_sec))
            tool_dl_sec = max(0.0, round(total_sec - build_sec, 1))
        else:
            build_sec   = total_sec
            tool_dl_sec = 0.0
        elapsed = int(total_sec)

        # Check if killed by user (returncode is negative or very large on kill, or _stop_requested set)
        rc = self.process.returncode
        was_killed = getattr(self, "_stop_requested", False) or (rc < 0) or (rc == 1 and not any(
            "error" in l.lower() and "werror" not in l.lower()
            for l in output_lines
        ) and elapsed < 5)

        if rc == 0:
            # Show advanced memory usage summary
            for line in output_lines:
                if "ram:" in line.lower() or "flash:" in line.lower():
                    self._append(f"  {line.strip()}", "success")
            self._append("")
            if tool_dl_sec >= 1.5:
                breakdown_fields = [
                    ("Framework & Tool Download", f"{tool_dl_sec}s"),
                    ("Code Build & Compilation", f"{build_sec}s"),
                    ("Total Elapsed Time", f"{total_sec}s"),
                ]
                self._print_info_box("Compilation Time Breakdown", breakdown_fields)
                self._append(f"  ✔ Compilation successful! (Build: {build_sec}s | Tools Download: {tool_dl_sec}s | Total: {total_sec}s)", "success")
                self._set_status(f"Compile OK (Build: {build_sec}s, Tools: {tool_dl_sec}s)", Theme.GREEN)
            else:
                breakdown_fields = [
                    ("Code Build & Compilation", f"{build_sec}s"),
                    ("Total Elapsed Time", f"{total_sec}s"),
                ]
                self._print_info_box("Compilation Time Breakdown", breakdown_fields)
                self._append(f"  ✔ Compilation successful! ({build_sec}s)", "success")
                self._set_status(f"Compile OK ({build_sec}s)", Theme.GREEN)

            # ── Board compatibility label ───────────────────────────────────
            compat_boards, compat_reasons = detect_board_compatibility(self.sketch_dir_path)
            compat_label = _format_compat_label(compat_boards)
            self._append("")
            self._append("  " + "─" * 48, "dim")
            self._append(f"  [COMPATIBLE DEVICES]  {compat_label}", "system")
            if not compat_reasons and len(compat_boards) == len(SUPPORTED_BOARDS):
                self._append(
                    "    ℹ No platform-specific APIs detected — "
                    "likely portable across all supported boards.", "dim"
                )
            self._append("  " + "─" * 48, "dim")

            self._set_status(f"Compile OK ({elapsed}s)", Theme.GREEN)
            self._save_compile_cache()  # snapshot so upload can skip recompile
            # Automatically update the Skip Compile checkbox on successful standalone compilation
            if not is_upload:
                self.root.after(0, self._update_skip_compile_state)
        elif was_killed:
            self._append("")
            self._append("  ■ Compilation stopped by user.", "warning")
            self._set_status("Compile stopped", Theme.YELLOW)
            if current_board:
                self._last_compiled_board = current_board
            self.root.after(0, self._update_skip_compile_state)
        else:
            self.root.after(0, self._update_skip_compile_state)
            self._append("")
            self._append(f"  ✖ Compilation FAILED after {elapsed}s:", "error")

            # Parse compiler error lines
            parsed_errors = []
            error_lines = []
            # Regex handles Windows absolute/relative paths and error/warning/note severity levels
            err_pattern = re.compile(
                r'^(?:([a-zA-Z]:[\\/][^:]+)|([^:]+)):(\d+):(\d+):\s+(fatal\s+error|error|warning|note):\s+(.+)$',
                re.IGNORECASE
            )
            for line in output_lines:
                match = err_pattern.match(line.strip())
                if match:
                    file_path = match.group(1) or match.group(2)
                    line_num = match.group(3)
                    col_num = match.group(4)
                    error_type = match.group(5)
                    error_msg = match.group(6)
                    try:
                        file_name = Path(file_path).name
                    except Exception:
                        file_name = file_path
                    parsed_errors.append({
                        "file": file_name,
                        "line": line_num,
                        "col": col_num,
                        "type": error_type,
                        "msg": error_msg
                    })

            if parsed_errors:
                # Group by file name and deduplicate identical compilation issues
                errors_by_file = {}
                seen_issues = set()
                for err in parsed_errors:
                    severity = err["type"].lower().strip()
                    # Deduplicate based on file name, line, column, severity, and message content
                    key = (err["file"], err["line"], err["col"], severity, err["msg"].strip())
                    if key in seen_issues:
                        continue
                    seen_issues.add(key)
                    errors_by_file.setdefault(err["file"], []).append(err)

                # ── Check if this is a board-mismatch rather than a real code bug ──
                try:
                    compat_boards, compat_reasons = detect_board_compatibility(self.sketch_dir_path)
                    selected_board = self.board_var.get()
                    is_board_mismatch = bool(compat_boards) and selected_board not in compat_boards
                except Exception:
                    is_board_mismatch = False
                    compat_boards = set()

                if is_board_mismatch:
                    # ── Board mismatch: show a single, clear message ──
                    # Summarise by platform family instead of listing 200+ board names
                    _platforms = {
                        SUPPORTED_BOARDS.get(b, {}).get("platform", "")
                        for b in compat_boards
                    }
                    _plat_labels = {
                        "espressif32": "ESP32-family",
                        "espressif8266": "ESP8266-family",
                        "atmelavr": "Arduino AVR (Uno, Nano, Mega …)",
                    }
                    family_names = sorted(
                        _plat_labels.get(p, p) for p in _platforms if p
                    )
                    compat_summary = ", ".join(family_names) if family_names else "other boards"
                    self._append("")
                    self._append("  ⚠ This sketch is NOT compatible with the selected board.", "warning")
                    self._append(f"     Selected board : {selected_board}", "warning")
                    self._append(f"     Compatible with: {compat_summary}", "info")
                    self._append("")
                    self._append("  💡 Please select a compatible board and try again.", "info")
                else:
                    # ── Real code issues: show the detailed per-file listing ──
                    # Pick header color: red only if real errors exist, yellow for warnings-only
                    has_real_errors = any(
                        "error" in e["type"].lower() and "warning" not in e["type"].lower()
                        for e in parsed_errors
                    )
                    header_tag = "error" if has_real_errors else "warning"
                    self._append("⚠️  ISSUES LISTED BY FILE:", header_tag)
                    self._append("─" * 50, header_tag)

                    for file_name, errs in errors_by_file.items():
                        self._append(f"  📁 {file_name}", "warning")
                        for err in errs:
                            severity = err["type"].lower().strip()
                            tag = "info" if "note" in severity else ("warning" if "warning" in severity else "error")
                            bullet = "•" if tag == "error" else ("⚠" if tag == "warning" else "ℹ")
                            self._append(f"     {bullet} Line {err['line']} (Col {err['col']}): {severity} — {err['msg']}", tag)
                    self._append("─" * 50, header_tag)
            else:
                self._append("─" * 50, "error")
                # Show actual error lines — GCC diagnostics + linker errors.
                # Explicitly exclude bare SCons make-error wrappers like
                #   "*** [.pio/build/.../foo.o] Error 1"
                # because those contain "error" but are NOT the real compiler
                # message — the actual g++ diagnostic appeared earlier and
                # showing only the wrapper hides the root cause from the user.
                LINKER_HINTS = (
                    "undefined reference to",
                    "multiple definition of",
                    "cannot find -l",
                    "undefined symbol",
                    "duplicate symbol",
                    "ld returned",
                    "collect2",
                )
                _scons_re = re.compile(r'^\*\*\*\s+\[')
                _gcc_context_re = re.compile(r'^\s*\d*\s*\|(?!--)')
                error_lines = [
                    l for l in output_lines
                    if (
                        # GCC/Clang diagnostic: file:line:col: error/note: msg
                        bool(re.search(r':\d+:\d+:\s+(error|fatal error|note)\s*:', l, re.IGNORECASE))
                        # Linker errors (no file:line:col prefix)
                        or any(hint in l.lower() for hint in LINKER_HINTS)
                        # GCC source-context lines (source, carets, suggestions)
                        or bool(_gcc_context_re.match(l.strip()))
                        # Generic "error" lines, but NOT SCons *** [...] Error N wrappers
                        or ("error" in l.lower() and "werror" not in l.lower()
                            and not _scons_re.match(l.strip()))
                    )
                ]
                if error_lines:
                    for line in error_lines[-20:]:
                        self._append(f"  {line}", "error")
                else:
                    # Nothing matched — dump raw tail so user sees something useful
                    for line in output_lines[-20:]:
                        self._append(f"  {line}", "error")
                self._append("─" * 50, "error")

            self._set_status("Compile FAILED", Theme.RED)

            if parsed_errors:
                # If there are syntax/compilation issues in files, do NOT prompt for clean build cache
                self.is_busy = False
                self._set_buttons_busy(False)
            else:
                # Detect the "silent Error 1" pattern (no real compiler diagnostics, only
                # SCons wrapper lines) — this is the signature of a stale board-switch build
                # cache.  When uploading, retry automatically with a full .pio wipe so the
                # user never sees a silent dead-end after switching boards.
                _is_silent_error = not error_lines and not was_killed
                if is_upload and _is_silent_error:
                    self._append(
                        "  ⚠ First compile attempt produced no diagnostic output — retrying with "
                        "a full clean (likely stale board-switch cache)…",
                        "warning"
                    )
                    # Wipe the entire .pio directory (not just build/libdeps) to clear
                    # SCons dependency graph + any partially-installed package metadata.
                    pio_dir = self.sketch_dir_path / ".pio"
                    if pio_dir.exists():
                        try:
                            robust_rmtree(pio_dir)
                            self._append("  ♻ Full .pio directory cleared.", "dim")
                        except Exception as _e:
                            self._append(f"  ⚠ Could not clear .pio: {_e}", "warning")
                    # Reset busy state so _run_compile can reset it properly on re-entry
                    self.is_busy = False
                    self._set_buttons_busy(False)
                    # Return None to signal "please retry" to _run_upload
                    return None  # type: ignore[return-value]
                # Offer clean + recompile via a messagebox on the main thread since it's likely a tool/cache error.
                # The dialog callback owns is_busy / _set_buttons_busy for this path.
                def _ask_clean_recompile():
                    from tkinter import messagebox
                    if messagebox.askyesno(
                        "Compilation Failed",
                        "Compilation failed.\n\nClean the build cache and recompile?",
                        icon="warning",
                        parent=self.root,
                    ):
                        self.is_busy = False
                        self._set_buttons_busy(False)
                        self._do_clean_then_compile()
                    else:
                        self.is_busy = False
                        self._set_buttons_busy(False)
                self.root.after(0, _ask_clean_recompile)
            return rc == 0

        self.is_busy = False
        self._set_buttons_busy(False)
        return rc == 0

    # ──────────────────────────────────────────────────────────
    # CHIP INFO PROBE
    # ──────────────────────────────────────────────────────────
    def _probe_chip_info(self, port: str) -> None:
        """Connect to the chip via esptool and print hardware info to the
        build console.  Called just before PlatformIO's upload so the port
        is free (monitor already paused).  Never raises — failures are shown
        as a warning and upload continues normally."""
        esp = None
        try:
            # pyrefly: ignore [missing-import]
            import esptool

            # esptool ≥ 4.x exposes get_default_connected_device(); older
            # versions don't.  Fall back gracefully when it's absent.
            if not hasattr(esptool, "get_default_connected_device"):
                self._append("  ⚠ esptool version too old for chip probe (need ≥ 4.x).", "warning")
                return

            # One-shot retry on the initial connect: a failure here right
            # after the monitor port was closed is often a transient
            # reset-timing hiccup (corrupted DTR/RTS pulse from reopening
            # the port too quickly) rather than a real hardware problem —
            # give it a second chance before giving up.
            try:
                esp = esptool.get_default_connected_device(
                    serial_list=[port],
                    port=port,
                    connect_attempts=3,
                    initial_baud=115200,
                )
            except Exception:
                time.sleep(0.5)
                esp = esptool.get_default_connected_device(
                    serial_list=[port],
                    port=port,
                    connect_attempts=3,
                    initial_baud=115200,
                )

            # Upload the stub firmware so that flash-access commands
            # (detect_flash_size / flash_id) are available.  Without the stub
            # those calls silently fail on most ESP32 variants because the ROM
            # loader doesn't implement the underlying SPI commands.
            # run_stub() returns the stub object; fall back to the ROM loader
            # if it's unavailable (very old esptool or unsupported chip rev).
            try:
                esp = esp.run_stub()
            except Exception:
                pass  # keep the ROM-loader object and try anyway

            chip_model = getattr(esp, "CHIP_NAME", "Unknown")

            try:
                features = ", ".join(esp.get_chip_features())
            except Exception:
                features = "N/A"

            try:
                mac_bytes = esp.read_mac()
                mac = ":".join(f"{b:02X}" for b in mac_bytes)
            except Exception:
                mac = "N/A"

            try:
                crystal = esp.get_crystal_freq()
                crystal_str = f"{crystal} MHz"
            except Exception:
                crystal_str = "N/A"

            try:
                # In esptool 5.x, flash size is read via esptool.cmds.detect_flash_size(esp),
                # a module-level function (NOT a method on esp).
                # It requires attach_flash(esp) first to enable SPI access on the stub.
                # Fallback: esp.flash_id() returns a 24-bit int where bits[23:16] are
                # the capacity/size_id looked up in DETECTED_FLASH_SIZES.
                # bits[7:0] are the vendor ID — the old code read the wrong byte.
                flash_str = "N/A"
                try:
                    # pyrefly: ignore [missing-import]
                    from esptool.cmds import detect_flash_size, attach_flash
                    attach_flash(esp)                 # arms SPI flash access on stub
                    detected = detect_flash_size(esp) # returns e.g. "8MB" or None
                    if detected:
                        flash_str = detected
                except Exception:
                    pass

                if flash_str == "N/A":
                    # Fallback: decode JEDEC size_id from bits[23:16] of flash_id()
                    try:
                        # pyrefly: ignore [missing-import]
                        from esptool.cmds import DETECTED_FLASH_SIZES
                        raw = esp.flash_id()
                        size_id = (raw >> 16) & 0xFF   # capacity byte is the HIGH byte
                        flash_str = DETECTED_FLASH_SIZES.get(size_id, "N/A")
                    except Exception:
                        pass
            except Exception:
                flash_str = "N/A"

            # ── Print to build console as a boxed info panel ───────────────
            self._print_chip_info_box(chip_model, [
                ("Chip Model",  chip_model),
                ("Features",    features),
                ("MAC Address", mac),
                ("Crystal",     crystal_str),
                ("Flash Size",  flash_str),
            ])

        except Exception as e:
            self._append(f"  ⚠ Chip probe failed: {e}", "warning")
            self._append("  (Continuing with upload anyway...)", "dim")
        finally:
            if esp is not None:
                try:
                    esp._port.close()
                except Exception:
                    pass
            # Same port-release race as before the probe: give Windows a
            # moment to fully free the handle before PlatformIO's esptool
            # subprocess reopens it for the actual upload.
            time.sleep(0.3)

    def _print_info_box(self, title: str, fields: list[tuple[str, str]]) -> None:
        """
        Render a boxed information panel into the build console using double-line border.
        """
        if not fields:
            return
        label_width = max(len(label) for label, _ in fields)
        label_col_width = label_width + 3  # "label" + " : "

        available_cols = self._console_box_columns()
        max_inner_width = max(available_cols - 4, label_col_width + 10, len(title))
        value_max_width = max(max_inner_width - label_col_width, 10)

        rows = []  # list of (label_field, value_chunk) — one per physical line
        for label, value in fields:
            chunks = textwrap.wrap(str(value), value_max_width) or [""]
            for i, chunk in enumerate(chunks):
                label_field = f"{label:<{label_width}} : " if i == 0 else " " * label_col_width
                rows.append((label_field, chunk))

        inner_width = max(len(title), max(len(lf) + len(v) for lf, v in rows))

        top    = "╔" + "═" * (inner_width + 2) + "╗"
        title_row = "║ " + title.center(inner_width) + " ║"
        sep    = "╠" + "═" * (inner_width + 2) + "╣"
        bottom = "╚" + "═" * (inner_width + 2) + "╝"

        self._append("")
        self._append(top, "purple_header")
        self._append(title_row, "purple_header")
        self._append(sep, "purple_header")
        for label_field, value_chunk in rows:
            value_field = value_chunk.ljust(inner_width - len(label_field))
            self._append_segments([
                ("║ ", "purple_header"),
                (label_field, "purple_info"),
                (value_field, "purple_value"),
                (" ║", "purple_header"),
            ])
        self._append(bottom, "purple_header")
        self._append("")

    def _print_chip_info_box(self, chip_model: str, fields: list[tuple[str, str]]) -> None:
        """Render a boxed chip-info panel into the build console."""
        self._print_info_box(f"{chip_model} Information", fields)

    # ──────────────────────────────────────────────────────────
    # UPLOAD
    # ──────────────────────────────────────────────────────────
    def _run_upload(self, port: str) -> bool:
        self._stop_requested = False
        self._op_session_id = getattr(self, "_op_session_id", 0) + 1
        # ── Smart compile check (upload path) ──────────────────────────────
        need_compile = True
        if self.skip_compile_var.get() and self._has_prior_build():
            recompile_needed, reason = self._needs_recompile()
            if not recompile_needed:
                self._append("")
                self._append("  ✔ Sources unchanged — skipping recompile", "success")
                self._set_status("Sources up-to-date, uploading...", Theme.CYAN)
                need_compile = False
            else:
                self._append("")
                self._append(f"  🔄 Recompile needed ({reason})", "warning")
        elif not self.skip_compile_var.get():
            self._append("")
            self._append("  🔄 Skip recompile unchecked — recompiling before upload.", "info")

        if need_compile:
            compile_result = self._run_compile(is_upload=True)
            if compile_result is None:
                # Silent-error auto-retry: _run_compile wiped .pio, retry compile once.
                self._append("  🔄 Retrying compilation after full clean…", "info")
                compile_result = self._run_compile(is_upload=True)
            if not compile_result:
                return False

        # Pause monitor here, right before upload attempts to execute (so monitor keeps running during compile)
        was_monitoring = self._pause_monitor()
        if was_monitoring:
            # Windows doesn't always finish releasing a COM port handle the
            # instant pyserial's close() returns. Reopening it immediately
            # (esptool's chip probe, next) can catch the port mid-release and
            # get a corrupted DTR/RTS reset pulse, which reads to the chip as
            # "wrong boot mode" even though nothing is actually wrong with
            # the board. A short settle delay avoids the race.
            time.sleep(0.3)

        # ── Sketch-vs-board compatibility guard (run right before actual upload) ──
        selected_board = self.board_var.get()
        compat_boards, compat_reasons = detect_board_compatibility(self.sketch_dir_path)
        
        has_warnings = False
        warnings_list = []
        if compat_boards and selected_board not in compat_boards:
            has_warnings = True
            compat_label = _format_compat_label(compat_boards)
            warnings_list.append(f"Selected board \"{selected_board}\" is not officially supported by this sketch.")
            warnings_list.append(f"This sketch is only compatible with: {compat_label}")
        
        current_hash = self._hash_sources()
        if compat_reasons and self._compat_warnings_approved_hash != current_hash:
            relevant_reasons = []
            for r in compat_reasons:
                if selected_board in compat_boards:
                    # If the selected board is compatible, only keep actual warnings (starting with ⚠)
                    # that are relevant to this board type
                    if r.startswith("⚠"):
                        board_prefix = selected_board.split()[0].lower()
                        if board_prefix in r.lower() or selected_board.lower() in r.lower():
                            relevant_reasons.append(r)
                else:
                    # If selected board is not compatible, all reasons are relevant
                    relevant_reasons.append(r)
            
            if relevant_reasons:
                has_warnings = True
                for r in relevant_reasons:
                    warnings_list.append(r)

        if has_warnings:
            # Show interactive confirmation dialog box (thread-safe on Windows)
            proceed = [None]
            def _prompt():
                reasons_text = "\n".join(f"- {r}" for r in warnings_list)
                from tkinter import messagebox
                msg = (
                    f"Warning: This project has compatibility warnings/exclusions:\n\n"
                    f"{reasons_text}\n\n"
                    "It may be dangerous to proceed and could affect MCU operation.\n"
                    "Do you still want to proceed with the upload?"
                )
                proceed[0] = messagebox.askyesno(
                    "Compatibility Warning",
                    msg,
                    icon="warning",
                    parent=self.root
                )
            self.root.after(0, _prompt)
            while proceed[0] is None:
                time.sleep(0.05)
                
            if not proceed[0]:
                # Log warnings to the console ONLY upon cancellation so the user knows why it was stopped
                self._append("", "")
                self._append("  ⚠ Compatibility warning — board/sketch mismatch or warnings detected!", "warning")
                for reason in warnings_list:
                    self._append(f"    ℹ {reason}", "warning")
                self._append("", "")
                self._append("  ⚠ Upload cancelled by user due to compatibility warnings.", "warning")
                self.is_busy = False
                self._set_buttons_state(False)
                if was_monitoring:
                    self._resume_monitor()
                return False
            
            if compat_reasons:
                self._compat_warnings_approved_hash = current_hash
        # ─────────────────────────────────────────────────────────────────────────

        self.is_busy = True
        self._set_buttons_state(True, operation="upload")
        self._set_status(f"Uploading to {port}...", Theme.MAGENTA)

        self._append("")
        self._append("=" * 50, "header")
        self._append("  ⬆  UPLOADING (PlatformIO)", "header")
        self._append("=" * 50, "header")
        upload_speed = self.upload_speed_var.get() if hasattr(self, "upload_speed_var") else "460800"
        self._append(f"  Port : {port} | Upload Speed : {upload_speed}", "port_highlight")
        if self._detect_port_chip() is None:
            self._append("  ⚠ Unrecognized USB-serial port — proceeding anyway.", "warning")

        # Desktop vs Laptop tip
        try:
            # pyrefly: ignore [missing-import]
            from detector import is_laptop
            system_is_laptop = is_laptop()
        except Exception:
            system_is_laptop = False

        if not system_is_laptop:
            self._append("  💡 Tip: On Desktop PCs, some ESP32 modules need their 'BOOT' button held down during connection to upload successfully.", "dim")

        self._append("")

        # NOTE: we deliberately do NOT run a separate esptool chip-info probe
        # here anymore. It used to open its own esptool connection (connect,
        # upload the stub flasher, read flash size/MAC/crystal) purely to
        # print a pretty info box, then close the port and hand it to
        # PlatformIO's own upload — which immediately reconnects and does the
        # exact same handshake again. That's two full reset+reconnect cycles
        # per upload instead of one. On boards with native USB (e.g. this
        # ESP32-S3), each reset pulse can cause the port to re-enumerate,
        # so the extra round trip was also a real source of intermittent
        # "failed to connect" upload issues, not just wasted time.
        # PlatformIO's own upload output already contains the same chip
        # info (Chip is ..., Features:, Crystal is ..., flash size) — it's
        # just filtered out below as noise instead of boxed up nicely.
        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        is_avr = (board_info.get("platform", "") == "atmelavr")

        pio_path = find_pio_executable()
        if not pio_path:
            self._append("  ⚠ PlatformIO not found — installing automatically...", "warning")
            self._set_status("Installing PlatformIO...", Theme.YELLOW)
            pio_path = ensure_platformio()
            if not pio_path:
                self._append("  ✖ Failed to install PlatformIO!", "error")
                self._append("  Try manually: pip install platformio", "info")
                self.is_busy = False
                self._set_buttons_busy(False)
                return False
            self._append(f"  ✔ PlatformIO installed: {' '.join(pio_path)}", "success")

        jobs = self._get_cpu_cores_jobs()
        cmd = pio_path + [
            "run",
            "-t", "upload",
            "-j", str(jobs),
            "--upload-port", port
        ]

        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
                cwd=str(self.sketch_dir_path),
            )
        except FileNotFoundError:
            self._append("  ✖ PlatformIO executable not found at: " + ' '.join(pio_path), "error")
            self.is_busy = False
            self._set_buttons_busy(False)
            return False

        last_pct = -1
        img_count = 0
        has_jtag_error = False

        # ── Upload spinner thread ─────────────────────────────────────────────
        # State string written by the reader loop; the spinner thread just
        # animates the prefix so the status bar stays alive between esptool
        # ── Upload spinner thread ───────────────────────────────────────────────────
        # Phase-ordered display: each distinct upload step gets a numbered label
        # so the user always knows where they are in the pipeline.
        # Phases in order: 1 Connecting → 2 Erasing → 3 Writing → 4 Verifying → 5 Resetting
        _UPLOAD_PHASES = [
            ("Initialising",  "Starting PlatformIO uploader..."),
            ("Connecting",    "Connecting to board..."),
            ("Erasing",       "Erasing flash memory..."),
            ("Writing",       "Writing firmware to flash..."),
            ("Verifying",     "Verifying written data..."),
            ("Resetting",     "Resetting board..."),
            ("Done",          "Upload complete"),
        ]
        _PHASE_KEYS = [p[0] for p in _UPLOAD_PHASES]
        _upload_phase_idx = [0]   # index into _UPLOAD_PHASES
        _upload_active    = [True]
        _upload_frame     = [0]
        _upload_start     = time.time()
        _upload_spinner   = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

        def _set_upload_phase(key: str):
            """Advance to a named phase, never going backwards."""
            if key in _PHASE_KEYS:
                idx = _PHASE_KEYS.index(key)
                if idx > _upload_phase_idx[0]:
                    _upload_phase_idx[0] = idx
                    # Clear connection timeout once we move past Connecting
                    if key in ("Erasing", "Writing", "Verifying", "Resetting", "Done"):
                        nonlocal _connecting_since
                        _connecting_since = None

        def _upload_spin_loop():
            while _upload_active[0] and not getattr(self, "_stop_requested", False):
                elapsed = int(time.time() - _upload_start)
                frame   = _upload_spinner[_upload_frame[0] % len(_upload_spinner)]
                _upload_frame[0] += 1
                idx     = _upload_phase_idx[0]
                key, label = _UPLOAD_PHASES[idx]
                # Total visible steps = all except "Initialising" and "Done"
                visible_total = len(_UPLOAD_PHASES) - 2   # 5
                visible_step  = max(0, idx - 1)           # 0-based relative to step 1
                step_str      = f"[{visible_step}/{visible_total}] " if visible_step > 0 else ""
                self._set_status(
                    f"{frame} {step_str}{label} ({elapsed}s)",
                    Theme.MAGENTA,
                )
                time.sleep(0.08)

        import threading as _threading_up
        _upload_spin_thread = _threading_up.Thread(target=_upload_spin_loop, daemon=True)
        _upload_spin_thread.start()

        # ── Chip-info capture ────────────────────────────────────────────────
        # PlatformIO's own esptool invocation already prints chip model,
        # features, crystal, MAC, and flash size while it connects — we used
        # to open a second, separate esptool connection just to redisplay
        # this same info in a nice box, which meant every upload did two full
        # connect/reset handshakes with the chip. Instead we harvest these
        # fields straight out of the single upload connection PlatformIO is
        # already making, and show the box once they're all in.
        _chip_info: dict[str, str] = {}
        _chip_info_shown = [False]

        # Lines that would normally print BEFORE the chip-info box (build
        # stats, protocol config, connection status) are held here instead,
        # so the box can appear right under the "Port :" line — matching
        # where the user actually wants to see it — with everything else
        # flushed immediately after it in its original order. For AVR
        # boards there is no chip-info box at all, so buffering is skipped
        # entirely and lines print immediately as before.
        _pending_pre_box: list = []

        def _buffered_append(text: str, tag: str = ""):
            if is_avr or _chip_info_shown[0]:
                self._append(text, tag)
            else:
                _pending_pre_box.append((text, tag))

        def _maybe_show_chip_info_box():
            if not _chip_info_shown[0] and _chip_info.get("Chip Model"):
                if _chip_info.get("Features"):
                    _chip_info["Features"] = _enrich_chip_features(
                        _chip_info["Chip Model"], _chip_info["Features"]
                    )
                if "Flash Size" not in _chip_info:
                    configured_flash = board_info.get("flash_size")
                    if configured_flash:
                        _chip_info["Flash Size"] = f"{configured_flash} (configured, not auto-detected)"
                self._print_chip_info_box(_chip_info["Chip Model"], list(_chip_info.items()))
                _chip_info_shown[0] = True
                for _text, _tag in _pending_pre_box:
                    self._append(_text, _tag)
                _pending_pre_box.clear()

        # ── Queue-based reader (same pattern as _run_compile) ─────────────
        # The upload subprocess (PlatformIO → esptool) can hang indefinitely
        # if the chip stops responding (e.g. ESP32-S3 needs manual BOOT press).
        # A blocking for-line-in-iter(readline) loop would freeze the entire
        # GUI forever in that case.  Instead we read via a daemon thread +
        # Queue with a 0.1 s timeout so we can detect process exit, honour
        # the Stop button, and drain late-arriving output after the process
        # terminates.
        import queue as _queue
        import threading as _threading

        _line_queue: _queue.Queue = _queue.Queue()

        def _reader(stream, q):
            try:
                for line in iter(stream.readline, ''):
                    if line:
                        q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)  # sentinel

        _t_out = _threading.Thread(target=_reader, args=(self.process.stdout, _line_queue), daemon=True)
        _t_out.start()

        _sentinels_remaining = 1
        _process_exited_at = None
        _connecting_since = None   # track when "Connecting" phase began for timeout

        # ── Connection timeout ──────────────────────────────────────────
        # ESP32-S3 / native-USB boards can hang esptool indefinitely if the
        # chip doesn't enter bootloader mode (e.g. user forgot to press BOOT).
        # After 30 s stuck in the "Connecting" phase with no output, we kill
        # the process and show a helpful message instead of freezing the GUI.
        _CONNECT_TIMEOUT = 30  # seconds

        while _sentinels_remaining > 0:
            try:
                line = _line_queue.get(timeout=0.1)
            except _queue.Empty:
                # After the process exits, give the reader thread a generous
                # window to flush any remaining data. Only bail out after 5 s
                # of no new data post-exit.
                if self.process.poll() is not None:
                    if _process_exited_at is None:
                        _process_exited_at = time.time()
                    elif time.time() - _process_exited_at > 5:
                        break
                # Allow the Stop button to interrupt a hung upload
                if getattr(self, "_stop_requested", False):
                    break
                # Connection timeout: if we've been stuck in "Connecting"
                # phase for too long with no output, kill the process.
                if (_connecting_since is not None
                        and _upload_phase_idx[0] <= _PHASE_KEYS.index("Connecting")
                        and time.time() - _connecting_since > _CONNECT_TIMEOUT):
                    self._append("", "")
                    self._append(f"  ⚠ Connection timed out after {_CONNECT_TIMEOUT}s — board not responding.", "warning")
                    self._append("  💡 ESP32-S3 / native-USB boards: hold BOOT, press RESET, release BOOT.", "info")
                    self._append("  💡 Or: unplug & replug the USB cable, then try again.", "info")
                    try:
                        self.process.kill()
                    except Exception:
                        pass
                    break
                continue

            if line is None:
                _sentinels_remaining -= 1
                continue

            # Reset the post-exit timer whenever we receive real data,
            # so we wait a full 5 s after the *last* line, not after exit.
            _process_exited_at = None

            stripped = line.rstrip()
            if not stripped:
                continue
            low = stripped.lower()
            if any(x in low for x in ("error", "failed", "unable", "cannot")) and any(x in low for x in ("esp_usb_jtag", "openocd", "jtag")):
                has_jtag_error = True

            # Opportunistic chip-info capture (see note above) — runs on every
            # line regardless of how it's otherwise classified below, and
            # never affects what gets displayed/suppressed for that line.
            if not is_avr:
                # esptool < 5.x prints "Chip is ESP32-S3 ..."; esptool >= 5.x
                # switched to a column-aligned "Chip type:          ESP32-S3
                # (QFN56) (revision v0.2)" format instead. Match both so the
                # chip model (and its package/revision detail) always makes
                # it into the info box regardless of esptool version.
                m = re.search(r'chip (?:is|type)\s*:?\s+(.+)$', stripped, re.IGNORECASE)
                if m:
                    _chip_info["Chip Model"] = m.group(1).strip()
                m = re.search(r'features\s*:\s*(.+)$', stripped, re.IGNORECASE)
                if m:
                    _chip_info["Features"] = m.group(1).strip()
                # Same old/new split as chip model above: "Crystal is 40MHz"
                # (old) vs "Crystal frequency:  40MHz" (new, esptool >= 5.x).
                m = re.search(r'crystal (?:is|frequency)\s*:?\s+(.+)$', stripped, re.IGNORECASE)
                if m:
                    _chip_info["Crystal"] = m.group(1).strip()
                m = re.search(r'^\s*mac\s*:\s*(.+)$', stripped, re.IGNORECASE)
                if m:
                    _chip_info["MAC Address"] = m.group(1).strip()
                m = re.search(r'(?:auto-detected\s+)?flash size\s*:\s*(.+)$', stripped, re.IGNORECASE)
                if m:
                    _chip_info["Flash Size"] = m.group(1).strip()

            # ── PlatformIO build-info lines (shown for all boards) ──────────
            if low.startswith("platform:"):
                continue
            if low.startswith("hardware:"):
                continue
            if low.startswith("debug:"):
                _buffered_append(f"  {stripped}", "dim")
                continue
            if "ram:" in low or "flash:" in low:
                _buffered_append(f"  {stripped}", "success")
                continue
            # Upload protocol config lines
            if re.match(r"\s*(configuring upload protocol|available:|current:)", low):
                _buffered_append(f"  {stripped}", "dim")
                continue
            # PIO result line  "=== [SUCCESS] Took X.XX seconds ==="
            pio_result = re.search(r'=+\s*\[(SUCCESS|FAILED)\]\s*Took\s*([\d.]+)\s*seconds', stripped, re.IGNORECASE)
            if pio_result:
                verdict = pio_result.group(1).upper()
                secs    = pio_result.group(2)
                if verdict == "SUCCESS":
                    self._append(f"  {stripped}", "success")
                else:
                    self._append(f"  {stripped}", "error")
                continue

            # ── Suppress low-level protocol noise ───────────────────────────
            _NOISE = (
                "auto-detected:", "chip is", "chip type:", "features:",
                "crystal is", "crystal frequency:", "mac:",
                "uploading stub", "running stub", "stub running",
                "changing baud", "compressed", "leaving...",
                "warning: espcomm", "esptool.py v", "serial port",
                "v2.", "v3.", "v4.", "v5.",   # version lines
            )
            if any(n in low for n in _NOISE):
                # Still update spinner state silently
                if "connecting" in low:
                    _set_upload_phase("Connecting")
                elif "erasing" in low or "erase" in low:
                    _set_upload_phase("Erasing")
                elif "writing" in low:
                    _set_upload_phase("Writing")
                elif "verifying" in low or "hash" in low:
                    _set_upload_phase("Verifying")
                elif "leaving" in low or "hard reset" in low:
                    _set_upload_phase("Resetting")
                continue

            if is_avr:
                # avrdude output — map to clean structured lines
                if "avr device initialized" in low or "device signature" in low:
                    _set_upload_phase("Connecting")
                    self._append("  🔌 Connected to Arduino Uno", "system")
                elif "writing flash" in low or "writing eeprom" in low:
                    _set_upload_phase("Writing")
                    # show just once
                elif "verifying flash" in low or "verifying eeprom" in low:
                    _set_upload_phase("Verifying")
                elif "avrdude done" in low or "bytes of flash" in low or "bytes written" in low:
                    _set_upload_phase("Done")
                    self._append(f"  {stripped}", "success")
                elif "error" in low or "failed" in low:
                    self._append(f"  {stripped}", "error")
                # suppress everything else (avrdude progress dots, etc.)
            else:
                # ESP boards
                if "connecting" in low:
                    _set_upload_phase("Connecting")
                    if _connecting_since is None:
                        _connecting_since = time.time()
                    _buffered_append("  🔌 Connecting to ESP board...", "info")
                elif "erasing" in low:
                    if _upload_phase_idx[0] <= _PHASE_KEYS.index("Connecting"):
                        _buffered_append("  ✔ ESP Board Connected", "success")
                        _maybe_show_chip_info_box()
                    _set_upload_phase("Erasing")
                elif re.search(r'\d+\s*%', stripped):
                    if _upload_phase_idx[0] <= _PHASE_KEYS.index("Connecting"):
                        _buffered_append("  ✔ ESP Board Connected", "success")
                        _maybe_show_chip_info_box()
                    _set_upload_phase("Writing")
                    # suppress — spinner shows progress
                elif "verifying" in low:
                    _set_upload_phase("Verifying")
                elif "hard resetting" in low:
                    _set_upload_phase("Resetting")
                    _maybe_show_chip_info_box()
                    _buffered_append(f"  {stripped}", "success")
                elif "wrote" in low or ("success" in low and "image" not in low):
                    _set_upload_phase("Done")
                    self._append(f"  {stripped}", "success")
                elif "successfully created" in low and "image" in low:
                    chip_match = re.search(r'successfully created (\w+) image', low)
                    chip_name = chip_match.group(1).upper() if chip_match else "MCU"
                    img_count += 1
                    label = "Bootloader" if img_count == 1 else "Application"
                    self._append(f"  ✔ Successfully created {chip_name} image ({label})", "success")
                elif "error" in low or "failed" in low:
                    self._append(f"  {stripped}", "error")

        # Drain any lines that arrived after the main loop exited
        while not _line_queue.empty():
            try:
                line = _line_queue.get_nowait()
            except _queue.Empty:
                break
            if line is None:
                continue
            stripped = line.rstrip()
            if not stripped:
                continue
            # Minimal re-processing for late-arriving error/success lines
            low = stripped.lower()
            if "error" in low or "failed" in low:
                self._append(f"  {stripped}", "error")
            elif "success" in low or "wrote" in low or "hard resetting" in low:
                self._append(f"  {stripped}", "success")

        _t_out.join(timeout=5)

        _upload_active[0] = False
        _upload_spin_thread.join(timeout=1)

        self.process.wait()
        ok = self.process.returncode == 0

        if ok:
            # Replay the captured protocol-config block once, RAM/Flash green.
            if _pending_pre_box:
                for _text, _tag in _pending_pre_box:
                    self._append(_text, _tag)
                _pending_pre_box.clear()
            self._append("")
            board_label = self.board_var.get()
            self._append(f"  ✔ Upload successful! {board_label} is resetting...", "success")
            self._set_status(f"Upload OK — {board_label} resetting", Theme.GREEN)
            
            # Enable and check Skip Compile after successful compile and upload
            self.root.after(0, self._update_skip_compile_state)

            # Jump to the Serial Monitor tab once the lock clears below, so
            # the user immediately sees the board's fresh boot output.
            self._focus_tab_on_unlock = 1
        else:
            self._append("")
            # Replay the captured protocol-config block once, RAM/Flash RED
            # on failure (green when flushed by the success path above).
            if _pending_pre_box:
                for _text, _tag in _pending_pre_box:
                    if _tag == "success" and ("ram:" in _text.lower() or "flash:" in _text.lower()):
                        self._append(_text, "error")
                    else:
                        self._append(_text, _tag)
                _pending_pre_box.clear()
            self._append("")
            self._append("  ✖ Upload FAILED.", "error")
            self._set_status("Upload FAILED", Theme.RED)
            
            # Update Skip Compile on failure
            self.root.after(0, self._update_skip_compile_state)

            if has_jtag_error:
                pio_dir = self.sketch_dir_path / ".pio"
                if pio_dir.exists():
                    try:
                        robust_rmtree(pio_dir)
                        self._append("  📝 Cleared SCons build cache (.pio) to resolve the JTAG config conflict.", "warning")
                        self._append("  👉 Please click UPLOAD again! It will now compile and upload cleanly over serial.", "info")
                    except Exception as e:
                        self._append(f"  ⚠ Failed to auto-clear .pio folder: {e}", "warning")

        self.is_busy = False
        self._set_buttons_busy(False)
        
        # Trigger hardware reset if monitor is not going to resume
        if ok and not was_monitoring:
            self._trigger_actual_board_reset(port)

        # Resume monitor if we paused it
        if was_monitoring:
            self._resume_monitor()

        return ok

    def _trigger_actual_board_reset(self, port: str):
        """Briefly open the port and toggle DTR/RTS to trigger a hardware reset."""
        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        is_uno = (board_info.get("platform", "") == "atmelavr")
        self._append(f"  🔄 Triggering hardware reset on {port}...", "info")
        try:
            with serial.Serial(port, baudrate=115200, timeout=0.1, dsrdtr=False, rtscts=False) as conn:
                if is_uno:
                    conn.dtr = False
                    time.sleep(0.05)
                    conn.dtr = True
                    time.sleep(0.05)
                    conn.dtr = False
                elif self._is_native_usb_port():
                    # Native USB-CDC: RTS=1, DTR=0 -> Reset; RTS=0, DTR=0 -> Run
                    conn.dtr = False
                    conn.rts = True
                    time.sleep(0.1)
                    conn.rts = False
                    conn.dtr = False
                else:
                    # Standard ESP32 auto-reset: pulse RTS while DTR=False
                    # RTS=True (assert/low) -> EN=HIGH, IO0=LOW (bootloader)
                    # RTS=False (de-assert/high) -> EN=HIGH, IO0=HIGH (run)
                    conn.dtr = False
                    conn.rts = True
                    time.sleep(0.1)
                    conn.rts = False
                    conn.dtr = False
                time.sleep(0.05)
            self._append("  ✔ Hardware reset triggered.", "success")
        except Exception as e:
            self._append(f"  ⚠ Could not trigger hardware reset: {e}", "warning")

    # ──────────────────────────────────────────────────────────
    # SERIAL MONITOR (always-on, right panel)
    # ──────────────────────────────────────────────────────────
    def _reset_mcu_from_monitor(self):
        """Reset the MCU via DTR/RTS pulse and restart the serial monitor."""
        port = self._get_port()
        if not port:
            self._append_serial("  ⚠ No port selected — cannot reset.", "warning")
            return
        if self.is_busy and getattr(self, "_active_operation", None) in ("upload", "flash", "reset"):
            # Auto-recover: if no subprocess is actually running, clear stale busy flag
            if not self.process or self.process.poll() is not None:
                self.is_busy = False
                self._set_buttons_state(False)
            else:
                self._append_serial("  ⚠ Cannot reset — an upload or reset operation is in progress.", "warning")
                return

        if not self._is_board_recognized():
            if not getattr(self, "_silent_reset", False):
                self._append_serial("  ⚠ Reset blocked — board on this port hasn't been recognized yet.", "warning")
            return

        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        is_uno = (board_info.get("platform", "") == "atmelavr")
        
        silent = getattr(self, "_silent_reset", False)
        if not silent:
            self._append_serial(f"  ↺ Resetting MCU on {port}…", "dim")

        # Stop current monitor so we can use the port
        was_monitoring = self.serial_running
        if was_monitoring:
            self.serial_running = False
            if self.serial_conn and self.serial_conn.is_open:
                try:
                    self.serial_conn.close()
                except Exception:
                    pass
            if self.serial_thread and self.serial_thread.is_alive():
                self.serial_thread.join(timeout=1.0)
            self._set_serial_status(False)

        # Pulse DTR/RTS to trigger the hardware reset
        try:
            with serial.Serial(port, baudrate=115200, timeout=0.1,
                               dsrdtr=False, rtscts=False) as conn:
                if is_uno:
                    conn.dtr = False
                    time.sleep(0.05)
                    conn.dtr = True
                    time.sleep(0.05)
                    conn.dtr = False
                elif self._is_native_usb_port():
                    # Native USB-CDC: RTS=1, DTR=0 -> Reset; RTS=0, DTR=0 -> Run
                    conn.dtr = False
                    conn.rts = True
                    time.sleep(0.1)
                    conn.rts = False
                    conn.dtr = False
                else:
                    # Standard ESP32 auto-reset: pulse RTS while DTR=False
                    # RTS=True (assert/low) -> EN=HIGH, IO0=LOW (bootloader)
                    # RTS=False (de-assert/high) -> EN=HIGH, IO0=HIGH (run)
                    conn.dtr = False
                    conn.rts = True
                    time.sleep(0.1)
                    conn.rts = False
                    conn.dtr = False
                time.sleep(0.05)
            if not silent:
                self._append_serial("  ✔ MCU reset triggered.", "success")
        except Exception as e:
            if not silent:
                self._append_serial(f"  ⚠ Reset failed: {e}", "warning")

        # Restart monitor to capture the fresh boot output
        self._schedule_auto_start_monitor(300)

    def _run_monitor(self, port: str, baud: int):
        silent = getattr(self, "_silent_reset", False)
        self._set_serial_status(False)

        # ── Board-aware pre-connect strategy ──────────────────────────────
        # ESP32/ESP8266: boot takes ~500 ms–2 s after the port opens, so a
        # short delay is fine — we won't miss anything important.
        #
        # Arduino UNO (ATmega328P): opening the port asserts DTR which
        # IMMEDIATELY resets the MCU.  The bootloader runs for ~2 s and then
        # hands off to setup().  If we wait even 1 s before reading, the
        # entire setup() block is gone.
        #
        # Strategy for UNO:
        #  1. Open the port with DTR *de-asserted* (dtr=False) so we don't
        #     accidentally trigger an extra reset when the port opens.
        #  2. Pulse DTR low→high→low to reset the board ourselves, at a
        #     moment we control.
        #  3. Start reading immediately — the bootloader and setup() output
        #     will both be captured.
        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        is_uno = (board_info.get("platform", "") == "atmelavr")

        # Determine if this connection attempt is a rapid duplicate of a failed attempt
        current_time = time.time()
        is_duplicate = (
            getattr(self, "_last_conn_attempt", {}).get("port") == port
            and getattr(self, "_last_conn_attempt", {}).get("baud") == baud
            and getattr(self, "_last_conn_attempt", {}).get("board") == board_name
            and (current_time - getattr(self, "_last_conn_attempt", {}).get("time", 0.0)) < 4.0
        )

        if not is_duplicate:
            if is_uno:
                self._append_serial(f"  Connecting to {port} @ {baud} (Arduino AVR mode)…", "dim")
            else:
                self._append_serial(f"  Connecting to {port} @ {baud}…", "dim")

        # Save this attempt details
        self._last_conn_attempt = {
            "port": port,
            "baud": baud,
            "board": board_name,
            "time": current_time,
        }

        if not is_uno:
            time.sleep(1.0)   # ESP32/ESP8266: brief pause while board boots

        # Try to open the port with retries to handle transient OS/driver locks
        max_attempts = 5
        attempt = 0
        while attempt < max_attempts:
            try:
                self.serial_conn = serial.Serial(
                    port=port, baudrate=baud, timeout=0.1,
                    write_timeout=2,
                    dsrdtr=False,  # never let pyserial manage DTR automatically
                    rtscts=False,
                )
                break
            except serial.SerialException as e:
                attempt += 1
                if attempt < max_attempts:
                    time.sleep(0.2)
                else:
                    if not is_duplicate:
                        self._append_serial(f"  ✖ Cannot open {port}: {e}", "error")
                    self.serial_running = False
                    self._set_serial_status(False)
                    self._silent_reset = False
                    return

        if is_uno:
            # DTR pulse: assert (low) → de-assert (high) triggers the reset.
            # We keep DTR de-asserted *before* opening so the board hasn't
            # already reset by the time we get here.
            self.serial_conn.dtr = False   # make sure it starts de-asserted
            time.sleep(0.05)
            self.serial_conn.dtr = True    # assert → pulls RESET low
            time.sleep(0.01)
            self.serial_conn.dtr = False   # release → RESET goes high, MCU boots
            # Flush any garbage that arrived during the pulse
            self.serial_conn.reset_input_buffer()
            if not silent:
                self._append_serial("  ↺ DTR pulse sent — Arduino is resetting…", "dim")
        else:
            # For ESP32/ESP8266 (including ESP32-S3 native USB-CDC):
            try:
                # Pulse DTR/RTS to reset the board
                if self._is_native_usb_port():
                    # Native USB-CDC: RTS=1, DTR=0 -> Reset; RTS=0, DTR=0 -> Run
                    self.serial_conn.dtr = False
                    self.serial_conn.rts = True
                    time.sleep(0.1)
                    self.serial_conn.rts = False
                    self.serial_conn.dtr = False
                else:
                    # Standard ESP32 auto-reset: pulse RTS while DTR=False
                    # RTS=True (assert/low) -> EN=HIGH, IO0=LOW (bootloader)
                    # RTS=False (de-assert/high) -> EN=HIGH, IO0=HIGH (run)
                    self.serial_conn.dtr = False
                    self.serial_conn.rts = True
                    time.sleep(0.1)
                    self.serial_conn.rts = False
                    self.serial_conn.dtr = False
                # Note: We do NOT call reset_input_buffer() here,
                # because the ESP32 starts booting immediately and we want
                # to read its startup output (like ROM boot logs and setup())
                # without discarding it.
                if not silent:
                    self._append_serial("  ↺ Reset pulse sent — ESP board is resetting…", "dim")
            except Exception:
                try:
                    if self._is_native_usb_port():
                        self.serial_conn.dtr = False
                        self.serial_conn.rts = False
                    else:
                        # Safe idle state: DTR=False (high), RTS=False (high) -> EN=HIGH, IO0=HIGH
                        self.serial_conn.dtr = False
                        self.serial_conn.rts = False
                except Exception:
                    pass

        self.serial_running = True
        self._monitor_should_run = True
        self._set_serial_status(True)
        if is_uno:
            self._append_serial(f"  ✔ Connected — {port} @ {baud}  [Output captured]", "success")
        else:
            if not silent:
                self._append_serial(f"  ✔ Connected — {port} @ {baud}", "success")

        # _silent_reset is a one-shot flag (set by folder-switch auto-reset);
        # clear it here so future connects/messages aren't silenced forever.
        self._silent_reset = False

        # ── ESP boot-loop banner suppression ────────────────────────────────
        # After a bootloader burn (or any reset with no valid sketch flashed),
        # an ESP32/ESP8266 has nothing to run and just resets over and over,
        # re-printing its ROM banner (ESP-ROM:, Build:, rst:0x..., load:0x...,
        # entry 0x...) every cycle — which floods the monitor with identical
        # noise forever. We detect the banner repeating and, once confirmed,
        # show a single friendly notice and swallow further banner lines
        # instead of letting them scroll the panel indefinitely.
        _boot_banner_seen = [0]
        _boot_loop_notified = [False]
        _BOOT_BANNER_PREFIXES = (
            "build:", "rst:0x", "saved pc:", "spiwp:", "mode:",
            "load:0x", "entry 0x",
        )

        def _is_boot_loop_noise(raw_text: str) -> bool:
            low_t = raw_text.strip().lower()
            # Check for boot loop banner markers (even in partial text)
            boot_markers = ("esp-rom:", "build:", "rst:0x", "load:0x", "entry 0x",
                           "spiwp:", "mode:", "saved pc:", "clk div:",
                           "flash size", "chip is", "features:", "crystal is")
            
            if low_t.startswith("esp-rom:"):
                _boot_banner_seen[0] += 1
                if _boot_banner_seen[0] >= 2:
                    if not _boot_loop_notified[0]:
                        _boot_loop_notified[0] = True
                        self._append_serial("", "")
                        self._append_serial(
                            f"  ⚠ {board_name}'s bootloader was burned, but no sketch is "
                            "running — the chip is stuck resetting in a loop.",
                            "warning",
                        )
                        self._append_serial("  📤 Please upload your sketch to continue.", "info")
                    return True
                return False
            
            # For partial text (no newline), check if it contains boot markers
            # This handles the case where bootloader output has no newlines
            if any(m in low_t for m in boot_markers):
                if _boot_banner_seen[0] >= 2 or _boot_loop_notified[0]:
                    return True
            
            return _boot_loop_notified[0] and low_t.startswith(_BOOT_BANNER_PREFIXES)

        def _filter_boot_loop_from_buffer(byte_buf: bytes) -> bytes:
            """Remove ESP32 boot loop banner patterns from raw byte buffer.
            Returns filtered buffer with boot loop noise removed."""
            if not byte_buf:
                return byte_buf
            text = byte_buf.decode("utf-8", errors="replace")
            low_text = text.lower()
            
            # If we've already notified about boot loop, aggressively filter all banner parts
            if _boot_loop_notified[0]:
                # If buffer contains boot loop patterns, just return empty
                if any(p in low_text for p in ("esp-rom:", "build:", "rst:0x", "load:0x", "entry 0x")):
                    if _boot_banner_seen[0] >= 2:
                        return b""
            return byte_buf

        # ── Read loop ────────────────────────────────────────────────────
        # This is the actual monitor: keep pulling bytes off the port and
        # pushing complete lines into the Serial Monitor panel until either
        # the connection drops or something (pause/restart/port-removal)
        # flips serial_running/_monitor_should_run off.
        buf = b""
        try:
            while self.serial_running and self._monitor_should_run:
                try:
                    n = self.serial_conn.in_waiting
                    chunk = self.serial_conn.read(n if n else 1)
                except (serial.SerialException, OSError) as e:
                    if self.serial_running:
                        self._append_serial(f"  ✖ Serial connection lost: {e}", "error")
                    break

                if not chunk:
                    continue

                buf += chunk
                # Filter boot loop noise from buffer before processing
                buf = _filter_boot_loop_from_buffer(buf)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").rstrip("\r")
                    if self._monitor_paused:
                        continue  # keep draining the port, just don't display
                    if not _is_boot_loop_noise(text):
                        self._append_tagged_line(text, is_newline=True)
                # Display any partial line (no newline yet) immediately
                if buf:
                    partial = buf.decode("utf-8", errors="replace").rstrip("\r")
                    if not self._monitor_paused and not _is_boot_loop_noise(partial):
                        self._append_tagged_line(partial, is_newline=False)
        finally:
            # Flush any trailing partial line that never got a newline.
            if buf:
                text = buf.decode("utf-8", errors="replace").rstrip("\r")
                if text and not self._monitor_paused and not _is_boot_loop_noise(text):
                    self._append_tagged_line(text, is_newline=True)

            self.serial_running = False
            self._set_serial_status(False)
            try:
                if self.serial_conn and self.serial_conn.is_open:
                    self.serial_conn.close()
            except Exception:
                pass

    def _build_editor(self, parent_frame):
        """Dispatch to the active editor implementation based on the
        configured editor mode ('default' Tkinter editor, 'monaco', or 'qscintilla')."""
        mode = getattr(self, "editor_mode", "default")
        if mode == "monaco":
            self._build_editor_monaco(parent_frame)
        elif mode == "qscintilla":
            self._build_editor_qscintilla(parent_frame)
        else:
            self._build_editor_default(parent_frame)

    def _build_editor_monaco(self, parent_frame):
        """Host the Monaco code editor pane.

        On Windows, the pywebview-hosted editor is a genuinely separate
        native OS window under the hood. Rather than let it float on its
        own, we reparent its native window handle (via the Win32 API) so
        it renders natively inside this Tkinter frame — a single window
        overall. If that isn't possible (non-Windows, or pywin32 missing),
        we fall back to a button that opens the editor as its own window,
        which is exactly the old behavior.
        """
        parent_frame.configure(bg=Theme.BG_DARKEST)

        self._editor_embed_frame = parent_frame
        self._editor_hwnd = None
        self._editor_embedded = False
        self._editor_reparent_attempts = 0

        # Placeholder / fallback UI. Hidden automatically once the editor
        # is successfully embedded; stays visible (with the popup button)
        # if embedding isn't available on this platform/setup.
        placeholder = tk.Frame(parent_frame, bg=Theme.BG_DARKEST)
        placeholder.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self._editor_placeholder = placeholder
        
        spinner = tk.Canvas(
            placeholder, width=48, height=48,
            bg=Theme.BG_DARKEST, highlightthickness=0
        )
        spinner.pack(pady=(0, 6))
        self._editor_spinner_canvas = spinner
        self._editor_spinner_angle = 0
        self._editor_spinner_job = None
        self._editor_content_loaded = False 
        self._animate_editor_spinner()

        status_lbl = tk.Label(
            placeholder,
            text="📝 Loading code editor…",
            font=tkfont.Font(family="Montserrat", size=16, weight="bold"),
            fg=Theme.CYAN, bg=Theme.BG_DARKEST
        )
        status_lbl.pack(pady=10)
        self._editor_status_lbl = status_lbl

        desc_lbl = tk.Label(
            placeholder,
            text="Attaching the editor to this window…",
            font=tkfont.Font(family="Montserrat", size=10),
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST
        )
        desc_lbl.pack(pady=5)
        self._editor_desc_lbl = desc_lbl

        def open_editor_win():
            if hasattr(self, "editor_window"):
                self.editor_window.show()
                self.editor_window.restore()

        self._editor_fallback_btn = self._make_btn(
            placeholder, "Open Editor Window", open_editor_win,
            Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, font=tkfont.Font(family="Montserrat", size=10, weight="bold")
        )
        # Only packed (shown) if/when embedding fails — see
        # _try_embed_editor_window below.

        # Keep the embedded editor window sized to match this frame.
        parent_frame.bind("<Configure>", self._resize_embedded_editor)

        # Initialize callbacks to evaluate_js on the webview window
        self._load_editor_files = lambda: self.editor_window.evaluate_js("loadProject()") if hasattr(self, "editor_window") else None
        self._save_all_editor_files = lambda: self.editor_window.evaluate_js("saveAllFiles()") if hasattr(self, "editor_window") else None
        self._save_current_editor_file = lambda: self.editor_window.evaluate_js("saveActiveFile()") if hasattr(self, "editor_window") else None
        self._reload_current_editor_file = lambda: self.editor_window.evaluate_js("reloadActiveFile()") if hasattr(self, "editor_window") else None

    def _build_editor_qscintilla(self, parent_frame):
        """Launch the QScintilla editor subprocess and embed its native window
        inside the Tkinter frame — identical architecture to Monaco/pywebview.

        The subprocess runs as a completely independent OS process so a crash
        inside Qt can never take down the main Tkinter thread.  We reparent its
        HWND into the Tk frame once it appears on-screen (retried every 150ms
        for up to ~12 s, like Monaco).  Cross-process communication for
        Save-All / Reload-All uses registered Win32 messages (WM_MCU_*) which
        the QScintilla subprocess already handles via nativeEvent().
        """
        parent_frame.configure(bg=Theme.BG_DARKEST)
        self._editor_embed_frame = parent_frame
        self._editor_hwnd = None
        self._editor_embedded = False
        self._editor_reparent_attempts = 0
        self._qsci_proc = None

        # ── Placeholder / loading UI ─────────────────────────────────────────
        placeholder = tk.Frame(parent_frame, bg=Theme.BG_DARKEST)
        placeholder.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        self._editor_placeholder = placeholder

        spinner = tk.Canvas(
            placeholder, width=48, height=48,
            bg=Theme.BG_DARKEST, highlightthickness=0
        )
        spinner.pack(pady=(0, 6))
        self._editor_spinner_canvas = spinner
        self._editor_spinner_angle = 0
        self._editor_spinner_job = None
        self._editor_content_loaded = False
        self._animate_editor_spinner()

        status_lbl = tk.Label(
            placeholder,
            text="📝 Loading QScintilla editor…",
            font=tkfont.Font(family="Montserrat", size=16, weight="bold"),
            fg=Theme.CYAN, bg=Theme.BG_DARKEST
        )
        status_lbl.pack(pady=10)
        self._editor_status_lbl = status_lbl

        desc_lbl = tk.Label(
            placeholder,
            text="Attaching the editor to this window…",
            font=tkfont.Font(family="Montserrat", size=10),
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST
        )
        desc_lbl.pack(pady=5)
        self._editor_desc_lbl = desc_lbl

        self._editor_fallback_btn = self._make_btn(
            placeholder, "Open Editor Window", lambda: None,
            Theme.BTN_COMPILE, Theme.BTN_COMPILE_H,
            font=tkfont.Font(family="Montserrat", size=10, weight="bold")
        )

        # Keep embedded window sized to the Tk frame.
        parent_frame.bind("<Configure>", self._resize_embedded_editor)

        # ── Win32 registered messages (cross-process control) ────────────────
        _wm_save_all = 0
        _wm_reload_all = 0
        if sys.platform == "win32":
            try:
                import ctypes as _ct
                _wm_save_all   = _ct.windll.user32.RegisterWindowMessageW("MCU_Flash_Save_All")
                _wm_reload_all = _ct.windll.user32.RegisterWindowMessageW("MCU_Flash_Reload_All")
            except Exception:
                pass

        def _send_wm(msg_id):
            """PostMessage to the embedded HWND if we have one."""
            if not msg_id or win32gui is None:
                return
            hwnd = getattr(self, "_editor_hwnd", None)
            if hwnd:
                try:
                    import ctypes as _ct
                    _ct.windll.user32.PostMessageW(hwnd, msg_id, 0, 0)
                except Exception:
                    pass

        self._save_all_editor_files    = lambda: _send_wm(_wm_save_all)
        self._reload_all_editor_files  = lambda: _send_wm(_wm_reload_all)
        self._load_editor_files        = self._reload_all_editor_files
        self._save_current_editor_file = self._save_all_editor_files  # best effort

        # ── Launch subprocess ────────────────────────────────────────────────
        # Delay the subprocess launch slightly to let the main GUI finish rendering,
        # maximizing, and settling down first. This prevents startup CPU contention
        # and ensures the Tk frame has a valid native handle.
        delay_ms = 800
        if win32gui is None or win32con is None:
            self._append("  ⚠ pywin32 not available — QScintilla editor will open in its own window.", "warning")
            self.root.after(delay_ms, lambda: self._start_qsci_subprocess(standalone=True))
            return

        self.root.after(delay_ms, lambda: self._start_qsci_subprocess(standalone=False))

    def _start_qsci_subprocess(self, standalone: bool = False):
        """Launch qscintilla_editor.py in a subprocess.
        standalone=True → normal window, no embedding attempt.
        standalone=False → --embedded flag, then try to reparent its HWND.
        """
        qsci_script = str(Path(__file__).parent / "src" / "qscintilla_editor.py")
        if not Path(qsci_script).exists():
            self._append(f"  ✖ QScintilla editor script not found: {qsci_script}", "error")
            return

        sketch_dir = str(getattr(self, "sketch_dir_path", "") or "")
        cmd = [sys.executable, qsci_script]
        if not standalone:
            import random
            self._qsci_session_id = f"sess_{random.randint(100000, 999999)}"
            cmd.extend(["--embedded", "--session", self._qsci_session_id])
        if sketch_dir:
            cmd.extend(["--dir", sketch_dir])

        try:
            import subprocess as _sp
            self._qsci_proc = _sp.Popen(
                cmd,
                cwd=sketch_dir or str(Path(__file__).parent),
                creationflags=_sp.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception as e:
            self._append(f"  ✖ Failed to start QScintilla editor: {e}", "error")
            return

        if not standalone:
            self._editor_reparent_attempts = 0
            self.root.after(400, self._try_embed_qsci_window)

    def _try_embed_qsci_window(self):
        """Retry loop: find the QScintilla subprocess window and reparent it
        into the Tkinter editor frame. Mirrors _try_embed_editor_window for
        Monaco — same pattern, same safety guards.
        """
        if win32gui is None or win32con is None:
            return
        if getattr(self, "_editor_embedded", False):
            return

        # Ensure main window is viewable (mapped) before embedding
        if not self.root.winfo_exists() or not self.root.winfo_viewable():
            self.root.after(150, self._try_embed_qsci_window)
            return

        proc = getattr(self, "_qsci_proc", None)
        if proc is None or proc.poll() is not None:
            self._append("  ✖ QScintilla subprocess exited unexpectedly.", "error")
            self._show_editor_fallback_button()
            return

        target_session = getattr(self, "_qsci_session_id", None)
        found_hwnd = []

        def _enum_cb(hwnd, _):
            try:
                title = win32gui.GetWindowText(hwnd)
                if "Embedded QScintilla Editor" in title:
                    if target_session and f"Session: {target_session}" in title:
                        found_hwnd.append(hwnd)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(_enum_cb, None)
        except Exception:
            pass

        if not found_hwnd:
            self._editor_reparent_attempts += 1
            if self._editor_reparent_attempts < 80:  # ~12 s
                self.root.after(150, self._try_embed_qsci_window)
            else:
                self._append("  ✖ Could not locate the QScintilla window after 12 s — "
                              "falling back to a separate window.", "error")
                self._show_editor_fallback_button()
            return

        hwnd = found_hwnd[0]
        try:
            frame = self._editor_embed_frame
            frame.update_idletasks()
            tk_hwnd = frame.winfo_id()

            # Strip decorations → plain child window
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            style &= ~(win32con.WS_CAPTION | win32con.WS_THICKFRAME |
                       win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX |
                       win32con.WS_SYSMENU | win32con.WS_POPUP | win32con.WS_BORDER)
            style |= win32con.WS_CHILD
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)

            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex_style &= ~(win32con.WS_EX_DLGMODALFRAME | win32con.WS_EX_APPWINDOW |
                          win32con.WS_EX_WINDOWEDGE | win32con.WS_EX_CLIENTEDGE)
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

            # Re-parent under the Tk frame
            win32gui.SetParent(hwnd, tk_hwnd)

            w = max(frame.winfo_width(), 50)
            h = max(frame.winfo_height(), 50)
            # SWP_ASYNCWINDOWPOS keeps the Tk thread non-blocking during placement
            win32gui.SetWindowPos(
                hwnd, 0, 0, 0, w, h,
                win32con.SWP_FRAMECHANGED | win32con.SWP_NOZORDER |
                win32con.SWP_SHOWWINDOW | 0x4000
            )

            self._editor_hwnd = hwnd
            self._editor_embedded = True
            self._append("  ✓ QScintilla editor embedded into the main window.", "success")

            # Hide the placeholder spinner / loading UI
            try:
                self._editor_status_lbl.configure(text="📝 QScintilla Editor Ready")
                self._editor_desc_lbl.configure(text="")
                if hasattr(self, "_editor_spinner_job") and self._editor_spinner_job:
                    self.root.after_cancel(self._editor_spinner_job)
                    self._editor_spinner_job = None
                self._editor_placeholder.place_forget()
            except Exception:
                pass

        except Exception as e:
            self._append(f"  ✖ Failed to embed QScintilla window: {e}", "error")
            self._show_editor_fallback_button()

    def _build_editor_default(self, parent_frame):
        """Build the embedded tabbed code-editor container showing all .ino / .cpp / .h
        source files in the current sketch directory.
        """
        if not hasattr(self, "editor_font"):
            self.editor_font = tkfont.Font(family="Consolas", size=10)
        if not hasattr(self, "editor_font_sm"):
            self.editor_font_sm = tkfont.Font(family="Consolas", size=9)
        if not hasattr(self, "editor_font_bold"):
            self.editor_font_bold = tkfont.Font(family="Consolas", size=10, weight="bold")
        if not hasattr(self, "editor_font_italic"):
            self.editor_font_italic = tkfont.Font(family="Consolas", size=10, slant="italic")

        sketch_dir = self.sketch_dir_path

        # ── Syntax-highlight token specs ──────────────────────────────────
        # Each entry: (tag_name, compiled_regex)
        # Tags are applied in order; later tags overwrite earlier ones for
        # the same character range — so comments / strings come last and win.
        C_KEYWORDS = (
            r"\b(?:void|int|long|unsigned|float|double|char|bool|byte|boolean|"
            r"short|uint8_t|uint16_t|uint32_t|int8_t|int16_t|int32_t|size_t|"
            r"String|const|static|volatile|class|struct|enum|typedef|namespace|"
            r"public|private|protected|new|delete|this|nullptr|NULL|true|false|"
            r"return|if|else|for|while|do|switch|case|break|continue|default|"
            r"auto|inline|explicit|virtual|override|template|typename)\b"
        )
        ARDUINO_KEYWORDS = (
            r"\b(?:setup|loop|pinMode|digitalWrite|digitalRead|analogWrite|"
            r"analogRead|delay|millis|micros|Serial|Serial1|Serial2|Wire|SPI|"
            r"HIGH|LOW|INPUT|OUTPUT|INPUT_PULLUP|LED_BUILTIN|A0|A1|A2|A3|A4|A5|"
            r"digitalPinToInterrupt|attachInterrupt|detachInterrupt|CHANGE|RISING|FALLING|"
            r"map|constrain|min|max|abs|sqrt|pow|sin|cos|tan|random|randomSeed|"
            r"String|strlen|strcmp|strcpy|sprintf|memset|memcpy|sizeof|"
            r"xTaskCreate|xTaskCreatePinnedToCore|vTaskDelay|pdMS_TO_TICKS|"
            r"portMAX_DELAY|configTICK_RATE_HZ|uxTaskGetStackHighWaterMark)\b"
        )
        SYN_SPECS = [
            ("syn_preproc",  re.compile(r"(?m)^[ \t]*#\w+[^\n]*")),
            ("syn_number",   re.compile(r"\b0x[0-9A-Fa-f]+\b|\b\d+\.?\d*(?:[eE][+-]?\d+)?[fFuUlL]*\b")),
            ("syn_kw",       re.compile(C_KEYWORDS)),
            ("syn_arduino",  re.compile(ARDUINO_KEYWORDS)),
            ("syn_string",   re.compile(r'"(?:[^"\n\\]|\\.)*"')),
            ("syn_char",     re.compile(r"'(?:[^'\n\\]|\\.)*'")),
            ("syn_comment1", re.compile(r"//[^\n]*")),
            ("syn_comment2", re.compile(r"/\*.*?\*/", re.DOTALL)),
        ]
        SYN_COLORS = {
            "syn_preproc":  Theme.MAGENTA,
            "syn_number":   Theme.ORANGE,
            "syn_kw":       Theme.BLUE,
            "syn_arduino":  Theme.CYAN,
            "syn_string":   Theme.GREEN,
            "syn_char":     Theme.GREEN,
            "syn_comment1": Theme.TEXT_DIM,
            "syn_comment2": Theme.TEXT_DIM,
        }

        # ── Notebook (tabs) ───────────────────────────────────────────────
        style = ttk.Style()
        # Style the notebook for dark theme
        try:
            style.configure("Editor.TNotebook",
                            background=Theme.BG_DARKEST,
                            borderwidth=0,
                            tabmargins=[2, 4, 0, 0])
            style.configure("Editor.TNotebook.Tab",
                            background=Theme.BG_MID,
                            foreground=Theme.TEXT_DIM,
                            padding=[10, 4],
                            font=("Consolas", 9))
            style.map("Editor.TNotebook.Tab",
                      background=[("selected", Theme.BG_HOVER), ("active", Theme.BG_LIGHT)],
                      foreground=[("selected", Theme.TEXT_BRIGHT), ("active", Theme.TEXT)])
        except Exception:
            pass  # style may fail on some Tk versions; continue without custom style

        nb = ttk.Notebook(parent_frame, style="Editor.TNotebook")
        nb.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.editor_notebook = nb

        # ── Status bar ────────────────────────────────────────────────────
        # Not packed into parent_frame — the file-path / cursor-position /
        # save-status strip was reclaimed as usable editor space per user
        # request. The widgets are still created (unattached) so the
        # existing update logic elsewhere (cursor tracker, tab-switch,
        # save/reload handlers) keeps working without needing further edits.
        status_bar = tk.Frame(parent_frame, bg=Theme.BG_MID, pady=3)

        lbl_filepath = tk.Label(
            status_bar, text="", font=self.font_status,
            fg=Theme.TEXT_DIM, bg=Theme.BG_MID, anchor=tk.W,
        )

        lbl_cursor = tk.Label(
            status_bar, text="Ln 1, Col 1", font=self.font_status,
            fg=Theme.TEXT_DIM, bg=Theme.BG_MID,
        )

        lbl_editor_status = tk.Label(
            status_bar, text="", font=self.font_status,
            fg=Theme.GREEN, bg=Theme.BG_MID,
        )

        # ── Per-tab state ─────────────────────────────────────────────────
        # tab_data[tab_frame] = {"path": Path, "text": Text, "modified": BooleanVar,
        #                         "original": str, "lineno_text": Text}
        tab_data = {}
        self.editor_tab_data = tab_data

        # ── Zoom functionality ────────────────────────────────────────────
        def _zoom_in(event=None):
            size = self.editor_font.cget("size")
            if size < 40:
                self.editor_font.configure(size=size + 1)
                self.editor_font_sm.configure(size=max(6, size))
                self.editor_font_bold.configure(size=size + 1)
                self.editor_font_italic.configure(size=size + 1)
            return "break"

        def _zoom_out(event=None):
            size = self.editor_font.cget("size")
            if size > 6:
                self.editor_font.configure(size=size - 1)
                self.editor_font_sm.configure(size=max(5, size - 2))
                self.editor_font_bold.configure(size=size - 1)
                self.editor_font_italic.configure(size=size - 1)
            return "break"

        def _zoom_wheel(event):
            if event.delta > 0:
                _zoom_in()
            else:
                _zoom_out()
            return "break"

        # ── Toggle Comment/Uncomment ──────────────────────────────────────
        def _toggle_comment(event=None):
            cur = nb.select()
            if not cur:
                return "break"
            frame = parent_frame.nametowidget(cur)
            txt = tab_data[frame]["text"]
            try:
                sel_start = txt.index(tk.SEL_FIRST)
                sel_end = txt.index(tk.SEL_LAST)
                start_row = int(sel_start.split(".")[0])
                end_row = int(sel_end.split(".")[0])
                if end_row > start_row and sel_end.split(".")[1] == "0":
                    end_row -= 1
            except tk.TclError:
                start_row = end_row = int(txt.index(tk.INSERT).split(".")[0])
            txt.edit_separator()
            should_uncomment = True
            lines_to_process = []
            for row in range(start_row, end_row + 1):
                line = txt.get(f"{row}.0", f"{row}.end")
                lines_to_process.append((row, line))
                stripped = line.strip()
                if stripped and not stripped.startswith("//"):
                    should_uncomment = False
            for row, line in lines_to_process:
                if should_uncomment:
                    stripped = line.lstrip(" ")
                    if stripped.startswith("//"):
                        leading_spaces = len(line) - len(stripped)
                        del_len = 2
                        if len(stripped) > 2 and stripped[2] == ' ':
                            del_len = 3
                        txt.delete(f"{row}.{leading_spaces}", f"{row}.{leading_spaces + del_len}")
                else:
                    leading_spaces = len(line) - len(line.lstrip(" "))
                    txt.insert(f"{row}.{leading_spaces}", "// ")
            _highlight_after(txt)
            _mark_modified(frame, txt, tab_data[frame]["path"])
            _sync_linenos(txt, tab_data[frame]["lineno_text"])
            return "break"

        # ── Line highlighting tracker ─────────────────────────────────────
        def _update_line_highlight(text_widget: tk.Text):
            text_widget.tag_remove("active_line", "1.0", tk.END)
            text_widget.tag_add("active_line", "insert linestart", "insert lineend + 1c")

        # ── Find & Replace Panel & Logic ──────────────────────────────────
        find_panel = tk.Frame(parent_frame, bg=Theme.BG_MID, pady=6, padx=12)
        find_panel.columnconfigure(1, weight=1)
        find_panel.columnconfigure(3, weight=1)
        
        tk.Label(find_panel, text="Find:", font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_MID).grid(row=0, column=0, sticky=tk.W, pady=2)
        find_ent = tk.Entry(find_panel, width=25, font=self.font_mono_sm, bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT, insertbackground=Theme.CYAN, borderwidth=0, highlightthickness=1, highlightcolor=Theme.CYAN_DIM, highlightbackground=Theme.BORDER)
        find_ent.grid(row=0, column=1, padx=6, pady=2, sticky=tk.EW)
        
        def _on_find_change(event=None):
            for data in tab_data.values():
                data["text"].tag_remove("search_match", "1.0", tk.END)
                data["text"].tag_remove("search_match_active", "1.0", tk.END)
            query = find_ent.get()
            if not query:
                return
            cur = nb.select()
            if not cur:
                return
            frame = parent_frame.nametowidget(cur)
            txt = tab_data.get(frame)
            if txt:
                txt = txt["text"]
                start = "1.0"
                while True:
                    pos = txt.search(query, start, nocase=True, stopindex=tk.END)
                    if not pos:
                        break
                    end = f"{pos} +{len(query)}c"
                    txt.tag_add("search_match", pos, end)
                    start = end

        find_ent.bind("<KeyRelease>", _on_find_change)

        def _find_match(forward=True):
            query = find_ent.get()
            if not query:
                return
            cur = nb.select()
            if not cur:
                return
            frame = parent_frame.nametowidget(cur)
            txt = tab_data[frame]["text"]
            insert_pos = txt.index(tk.INSERT)
            if forward:
                pos = txt.search(query, f"{insert_pos} +1c", nocase=True, stopindex=tk.END)
                if not pos:
                    pos = txt.search(query, "1.0", nocase=True, stopindex=tk.END)
            else:
                pos = txt.search(query, insert_pos, nocase=True, stopindex="1.0", backwards=True)
                if not pos:
                    pos = txt.search(query, tk.END, nocase=True, stopindex="1.0", backwards=True)
            if pos:
                end = f"{pos} +{len(query)}c"
                txt.tag_remove("search_match_active", "1.0", tk.END)
                txt.tag_add("search_match_active", pos, end)
                txt.mark_set(tk.INSERT, pos)
                txt.tag_remove("sel", "1.0", tk.END)
                txt.tag_add("sel", pos, end)
                txt.see(pos)

        btn_find_prev = self._make_btn(find_panel, "◀ Prev", lambda: _find_match(forward=False), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_label)
        btn_find_prev.grid(row=0, column=2, padx=2, pady=2)
        
        btn_find_next = self._make_btn(find_panel, "▶ Next", lambda: _find_match(forward=True), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_label)
        btn_find_next.grid(row=0, column=3, padx=2, pady=2, sticky=tk.W)

        tk.Label(find_panel, text="Replace:", font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_MID).grid(row=1, column=0, sticky=tk.W, pady=2)
        replace_ent = tk.Entry(find_panel, width=25, font=self.font_mono_sm, bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT, insertbackground=Theme.CYAN, borderwidth=0, highlightthickness=1, highlightcolor=Theme.CYAN_DIM, highlightbackground=Theme.BORDER)
        replace_ent.grid(row=1, column=1, padx=6, pady=2, sticky=tk.EW)

        def _replace_match():
            query = find_ent.get()
            if not query:
                return
            cur = nb.select()
            if not cur:
                return
            frame = parent_frame.nametowidget(cur)
            txt = tab_data[frame]["text"]
            try:
                sel_start = txt.index(tk.SEL_FIRST)
                sel_end = txt.index(tk.SEL_LAST)
                sel_text = txt.get(sel_start, sel_end)
                if sel_text.lower() == query.lower():
                    txt.delete(sel_start, sel_end)
                    txt.insert(sel_start, replace_ent.get())
                    _highlight_after(txt)
                    _mark_modified(frame, txt, tab_data[frame]["path"])
                    _sync_linenos(txt, tab_data[frame]["lineno_text"])
            except tk.TclError:
                pass
            _find_match(forward=True)

        def _replace_all():
            query = find_ent.get()
            if not query:
                return
            replace_val = replace_ent.get()
            cur = nb.select()
            if not cur:
                return
            frame = parent_frame.nametowidget(cur)
            txt = tab_data[frame]["text"]
            count = 0
            start = "1.0"
            txt.edit_separator()
            while True:
                pos = txt.search(query, start, nocase=True, stopindex=tk.END)
                if not pos:
                    break
                end = f"{pos} +{len(query)}c"
                txt.delete(pos, end)
                txt.insert(pos, replace_val)
                start = f"{pos} +{len(replace_val)}c"
                count += 1
            if count:
                _highlight_after(txt)
                _mark_modified(frame, txt, tab_data[frame]["path"])
                _sync_linenos(txt, tab_data[frame]["lineno_text"])
                _set_editor_status(f"✔ Replaced {count} occurrence(s)")
                _on_find_change()

        btn_rep = self._make_btn(find_panel, "Replace", _replace_match, Theme.BTN_MONITOR, Theme.BTN_MONITOR_H, font=self.font_label)
        btn_rep.grid(row=1, column=2, padx=2, pady=2)
        
        btn_rep_all = self._make_btn(find_panel, "Replace All", _replace_all, Theme.BTN_FULL, Theme.BTN_FULL_H, font=self.font_label)
        btn_rep_all.grid(row=1, column=3, padx=2, pady=2, sticky=tk.W)

        def _toggle_find_panel(event=None, show=None):
            if show is None:
                show = not find_panel.winfo_ismapped()
            if show:
                find_panel.pack(before=nb, fill=tk.X, padx=10, pady=5)
                find_ent.focus_set()
                find_ent.select_range(0, tk.END)
                _on_find_change()
            else:
                find_panel.pack_forget()
                for data in tab_data.values():
                    data["text"].tag_remove("search_match", "1.0", tk.END)
                    data["text"].tag_remove("search_match_active", "1.0", tk.END)
                cur = nb.select()
                if cur:
                    tab_data[parent_frame.nametowidget(cur)]["text"].focus_set()
            return "break"

        btn_close = self._make_btn(find_panel, "✕", lambda: _toggle_find_panel(show=False), Theme.BTN_STOP, Theme.BTN_STOP_H, font=self.font_label)
        btn_close.grid(row=0, column=4, rowspan=2, padx=(10, 0), pady=2, sticky=tk.NS)

        def _set_editor_status(msg: str, color: str = Theme.GREEN, ms: int = 2500):
            lbl_editor_status.config(text=msg, fg=color)
            self.root.after(ms, lambda: lbl_editor_status.config(text=""))

        # ── Syntax highlighter ────────────────────────────────────────────
        def _highlight(text_widget: tk.Text):
            """Re-apply syntax colours to the entire contents of text_widget."""
            # Remove old tags first
            for tag in SYN_COLORS:
                text_widget.tag_remove(tag, "1.0", tk.END)
            content = text_widget.get("1.0", tk.END)
            for tag, pattern in SYN_SPECS:
                for m in pattern.finditer(content):
                    start_idx = f"1.0+{m.start()}c"
                    end_idx   = f"1.0+{m.end()}c"
                    text_widget.tag_add(tag, start_idx, end_idx)

        def _highlight_after(text_widget: tk.Text, delay_ms: int = 120):
            """Schedule a debounced highlight pass so typing stays snappy."""
            attr = f"_hl_after_{id(text_widget)}"
            existing = getattr(self.root, attr, None)
            if existing:
                self.root.after_cancel(existing)
            job = self.root.after(delay_ms, lambda: _highlight(text_widget))
            setattr(self.root, attr, job)

        # ── Line-number gutter sync ───────────────────────────────────────
        def _sync_linenos(text_widget: tk.Text, lineno_widget: tk.Text):
            """Rebuild the line-number gutter to match the editor content."""
            tab_frame = None
            for tf, d in tab_data.items():
                if d["text"] == text_widget:
                    tab_frame = tf
                    break
                    
            folded_blocks = {}
            if tab_frame and "folded_blocks" in tab_data[tab_frame]:
                folded_blocks = tab_data[tab_frame]["folded_blocks"]

            line_count = int(text_widget.index(tk.END).split(".")[0]) - 1
            lineno_widget.config(state=tk.NORMAL)
            lineno_widget.delete("1.0", tk.END)
            
            lines = []
            for i in range(1, line_count + 1):
                if i in folded_blocks:
                    hidden_count = folded_blocks[i][0] - i
                    lines.append(f"+{hidden_count} {i}")
                else:
                    line_text = text_widget.get(f"{i}.0", f"{i}.end")
                    if "{" in line_text:
                        lines.append(f"- {i}")
                    else:
                        lines.append(f"  {i}")
                        
            lineno_widget.insert("1.0", "\n".join(lines))
            
            # Apply elision to line numbers in the gutter for folded blocks, and
            # make the "+N" marker itself stand out (bold + accent colour) so a
            # collapsed block is obvious at a glance instead of blending in with
            # the plain line numbers -- previously there was no visual cue at
            # all that lines were hidden, which was confusing when a fold was
            # toggled and a chunk of the file silently vanished from view.
            lineno_widget.tag_configure(
                "gutter_folded", foreground=Theme.ORANGE, font=self.editor_font_bold
            )
            for start_line, (end_line, _, _indicator_tag) in folded_blocks.items():
                gutter_tag = f"fold_{start_line}"
                lineno_widget.tag_configure(gutter_tag, elide=True)
                lineno_widget.tag_add(gutter_tag, f"{start_line + 1}.0", f"{end_line}.0")
                lineno_widget.tag_add("gutter_folded", f"{start_line}.0", f"{start_line}.end")

            lineno_widget.config(state=tk.DISABLED)
            # Sync scroll position
            lineno_widget.yview_moveto(text_widget.yview()[0])

        # ── Cursor position tracker ───────────────────────────────────────
        def _update_cursor_label(text_widget: tk.Text):
            pos = text_widget.index(tk.INSERT)
            ln, col = pos.split(".")
            lbl_cursor.config(text=f"Ln {ln}, Col {int(col)+1}")

        # ── Code folding toggler ──────────────────────────────────────────
        def _toggle_fold(text_widget: tk.Text, line_num: int, tf):
            data = tab_data.get(tf)
            if not data:
                return
            if "folded_blocks" not in data:
                data["folded_blocks"] = {}  # start_line -> (end_line, tag_name, indicator_tag)
            
            folded_blocks = data["folded_blocks"]
            
            if line_num in folded_blocks:
                end_line, tag_name, indicator_tag = folded_blocks[line_num]
                text_widget.tag_remove(tag_name, f"{line_num}.0", f"{end_line + 1}.0")
                text_widget.tag_delete(indicator_tag)
                del folded_blocks[line_num]
                _sync_linenos(text_widget, data["lineno_text"])
                return
                
            line_text = text_widget.get(f"{line_num}.0", f"{line_num}.end")
            if "{" not in line_text:
                return
                
            # Scan forward to find the matching '}'
            balance = 0
            found_start = False
            end_line = -1
            
            total_lines = int(text_widget.index(tk.END).split(".")[0])
            for r in range(line_num, total_lines):
                r_text = text_widget.get(f"{r}.0", f"{r}.end")
                r_text_clean = re.sub(r'//.*|/\*.*?\*/', '', r_text)
                
                for char in r_text_clean:
                    if char == '{':
                        balance += 1
                        found_start = True
                    elif char == '}':
                        balance -= 1
                        if found_start and balance == 0:
                            end_line = r
                            break
                if end_line != -1:
                    break
                    
            if end_line != -1 and end_line > line_num:
                tag_name = f"fold_{line_num}"
                text_widget.tag_configure(tag_name, elide=True)

                # Elide only the interior lines (line_num+1 .. end_line-1),
                # keeping both the opening '{' line and the closing '}' line
                # visible as their own rows. This must match _sync_linenos'
                # gutter elision range exactly, or the gutter and editor
                # disagree on how many rows a fold removes and every line
                # number after the fold drifts out of alignment.
                start_idx = f"{line_num + 1}.0"
                end_idx = f"{end_line}.0"

                text_widget.tag_add(tag_name, start_idx, end_idx)

                # Visible-in-editor cue: highlight the '{' line itself so the
                # user can see, right there in the code, that this line is
                # hiding a collapsed block beneath it. Before this, folding a
                # block left no trace at all in the editor pane -- the code
                # just stopped and jumped straight to whatever came after the
                # matching '}', which read as content having gone missing
                # rather than being intentionally collapsed.
                indicator_tag = f"fold_marker_{line_num}"
                text_widget.tag_configure(
                    indicator_tag, background=Theme.YELLOW_DIM, foreground=Theme.TEXT_BRIGHT
                )
                text_widget.tag_add(indicator_tag, f"{line_num}.0", f"{line_num}.end")

                folded_blocks[line_num] = (end_line, tag_name, indicator_tag)
                _sync_linenos(text_widget, data["lineno_text"])

        # ── Modified tracker ──────────────────────────────────────────────
        def _mark_modified(frame, text_widget: tk.Text, path: Path):
            data = tab_data.get(frame)
            if data is None:
                return
            current = text_widget.get("1.0", tk.END)
            changed = (current != data["original"])
            if changed != data["modified"]:
                data["modified"] = changed
                tab_title = ("* " if changed else "") + path.name
                nb.tab(frame, text=tab_title)
            
            any_modified = any(d["modified"] for d in tab_data.values())
            if any_modified:
                self.skip_compile_var.set(False)
                self.cb_skip_compile.configure(state=tk.DISABLED)
            else:
                self._update_skip_compile_state()

        # ── Save helpers ──────────────────────────────────────────────────
        def _save_tab(frame):
            data = tab_data.get(frame)
            if data is None:
                return
            content = data["text"].get("1.0", tk.END)
            # Strip the trailing newline Tk always appends
            if content.endswith("\n"):
                content = content[:-1]
            try:
                data["path"].write_text(content, encoding="utf-8")
                data["original"] = content + "\n"   # match Tk's representation
                data["modified"] = False
                nb.tab(frame, text=data["path"].name)
                _set_editor_status(f"✔ Saved — {data['path'].name}")
                # Invalidate compile cache so the GUI knows sources changed
                self._compile_cache_hash = None
                self._update_skip_compile_state()
            except Exception as exc:
                _set_editor_status(f"✖ Save failed: {exc}", color=Theme.RED, ms=5000)

        def _save_current():
            cur = nb.select()
            if not cur:
                return
            frame = parent_frame.nametowidget(cur)
            _save_tab(frame)

        def _save_all():
            saved = 0
            for frame, data in tab_data.items():
                if data["modified"]:
                    _save_tab(frame)
                    saved += 1
            if saved:
                _set_editor_status(f"✔ Saved {saved} file(s)")
            else:
                _set_editor_status("Nothing to save — all files up to date.", Theme.TEXT_DIM)

        def _reload_current():
            cur = nb.select()
            if not cur:
                return
            frame = parent_frame.nametowidget(cur)
            data = tab_data.get(frame)
            if data is None:
                return
            try:
                content = data["path"].read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                _set_editor_status(f"✖ Reload failed: {exc}", Theme.RED, 5000)
                return
            txt = data["text"]
            txt.delete("1.0", tk.END)
            txt.insert("1.0", content)
            data["original"] = txt.get("1.0", tk.END)
            data["modified"] = False
            nb.tab(frame, text=data["path"].name)
            _highlight(txt)
            _sync_linenos(txt, data["lineno_text"])
            _set_editor_status(f"✔ Reloaded — {data['path'].name}")
            self._update_skip_compile_state()

        # ── Smart indent helpers (shared across all tabs) ─────────────────
        INDENT = "  "   # 2 spaces — VS Code C/Arduino default
        INDENT_N = len(INDENT)

        AUTO_PAIRS = {"(": ")", "[": "]", "{": "}"}

        def _get_line_text(t: tk.Text, index: str = tk.INSERT) -> str:
            """Return the full text of the line containing *index*."""
            row = t.index(index).split(".")[0]
            return t.get(f"{row}.0", f"{row}.end")

        def _leading_spaces(line: str) -> int:
            """Count leading space characters in *line*."""
            return len(line) - len(line.lstrip(" "))

        def _on_return(event, t: tk.Text) -> str:
            """Smart Enter: match current indent + extra level after '{'."""
            cur_line = _get_line_text(t)
            indent_lvl = _leading_spaces(cur_line)
            stripped = cur_line.rstrip()

            # Check whether the cursor is sitting between { and }
            # e.g.  void setup() {|}   where | is cursor
            cursor_col = int(t.index(tk.INSERT).split(".")[1])
            char_before = t.get(f"{t.index(tk.INSERT)} -1c", tk.INSERT) if cursor_col > 0 else ""
            char_after  = t.get(tk.INSERT, f"{t.index(tk.INSERT)} +1c")

            t.edit_separator()  # make this one undo step

            if char_before == "{" and char_after == "}":
                # Cursor is between braces — expand into two lines with inner indent
                inner = " " * (indent_lvl + INDENT_N)
                outer = " " * indent_lvl
                t.insert(tk.INSERT, f"\n{inner}\n{outer}")
                # Move cursor to the inner (middle) line
                row = int(t.index(tk.INSERT).split(".")[0])
                t.mark_set(tk.INSERT, f"{row - 1}.end")
            else:
                extra = INDENT if stripped.endswith("{") else ""
                new_indent = " " * indent_lvl + extra
                t.insert(tk.INSERT, "\n" + new_indent)

            t.see(tk.INSERT)
            return "break"

        def _on_tab(event, t: tk.Text) -> str:
            """Tab key → insert INDENT spaces instead of a real tab char."""
            # If there's a selection, indent every selected line
            try:
                sel_start = t.index(tk.SEL_FIRST)
                sel_end   = t.index(tk.SEL_LAST)
                start_row = int(sel_start.split(".")[0])
                end_row   = int(sel_end.split(".")[0])
                t.edit_separator()
                for row in range(start_row, end_row + 1):
                    t.insert(f"{row}.0", INDENT)
                return "break"
            except tk.TclError:
                pass
            t.edit_separator()
            t.insert(tk.INSERT, INDENT)
            return "break"

        def _on_shift_tab(event, t: tk.Text) -> str:
            """Shift+Tab → dedent by one INDENT level."""
            try:
                sel_start = t.index(tk.SEL_FIRST)
                sel_end   = t.index(tk.SEL_LAST)
                start_row = int(sel_start.split(".")[0])
                end_row   = int(sel_end.split(".")[0])
            except tk.TclError:
                start_row = end_row = int(t.index(tk.INSERT).split(".")[0])

            t.edit_separator()
            for row in range(start_row, end_row + 1):
                line = t.get(f"{row}.0", f"{row}.end")
                spaces = min(_leading_spaces(line), INDENT_N)
                if spaces:
                    t.delete(f"{row}.0", f"{row}.{spaces}")
            return "break"

        def _on_closing_brace(event, t: tk.Text) -> str:
            """}  key: dedent closing brace to align with its opening line."""
            cur_line = _get_line_text(t)
            # Only auto-dedent when the line so far is all spaces
            # (user hasn't typed any non-space on this line yet)
            if cur_line.strip() == "":
                cur_indent = _leading_spaces(cur_line)
                new_indent = max(0, cur_indent - INDENT_N)
                row = t.index(tk.INSERT).split(".")[0]
                t.edit_separator()
                t.delete(f"{row}.0", f"{row}.{cur_indent}")
                t.insert(f"{row}.0", " " * new_indent)
                t.insert(tk.INSERT, "}")
                t.see(tk.INSERT)
                return "break"
            return None   # fall through to normal insertion

        def _on_backspace(event, t: tk.Text) -> str:
            """Backspace: delete whole indent chunk when cursor is on spaces."""
            # If there's a selection, let default behaviour handle it
            try:
                t.index(tk.SEL_FIRST)
                return None
            except tk.TclError:
                pass

            pos = t.index(tk.INSERT)
            row, col = pos.split(".")
            col = int(col)
            if col == 0:
                return None  # at line start — normal behaviour (delete newline)

            line_start = t.get(f"{row}.0", pos)
            # If everything to the left of the cursor is spaces, delete one indent level
            if line_start and line_start == " " * col:
                delete_n = ((col - 1) % INDENT_N) + 1   # 1..INDENT_N spaces
                t.edit_separator()
                t.delete(f"{row}.{col - delete_n}", pos)
                return "break"
            return None

        def _on_open_pair(event, t: tk.Text, open_ch: str) -> str:
            """Auto-close (, [, { with the matching closing character."""
            close_ch = AUTO_PAIRS[open_ch]
            t.edit_separator()
            t.insert(tk.INSERT, open_ch + close_ch)
            # Move cursor to between the pair
            t.mark_set(tk.INSERT, f"{t.index(tk.INSERT)} -1c")
            t.see(tk.INSERT)
            return "break"

        def _on_close_pair(event, t: tk.Text, close_ch: str) -> str:
            """Skip over an already-present closing char instead of doubling it."""
            next_char = t.get(tk.INSERT, f"{t.index(tk.INSERT)} +1c")
            if next_char == close_ch:
                t.mark_set(tk.INSERT, f"{t.index(tk.INSERT)} +1c")
                t.see(tk.INSERT)
                return "break"
            return None

        def _get_next_word_index(t: tk.Text, idx: str) -> str:
            line_end = t.index(f"{idx} lineend")
            if t.compare(idx, "==", line_end):
                return t.index(f"{idx} +1c")
            char_content = t.get(idx, line_end)
            if not char_content:
                return t.index(f"{idx} +1c")
            first_char = char_content[0]
            if first_char.isalnum() or first_char == '_':
                for i, c in enumerate(char_content):
                    if not (c.isalnum() or c == '_'):
                        return t.index(f"{idx} +{i}c")
                return line_end
            elif first_char.isspace():
                for i, c in enumerate(char_content):
                    if not c.isspace():
                        return t.index(f"{idx} +{i}c")
                return line_end
            else:
                for i, c in enumerate(char_content):
                    if c.isalnum() or c == '_' or c.isspace():
                        return t.index(f"{idx} +{i}c")
                return line_end

        def _get_prev_word_index(t: tk.Text, idx: str) -> str:
            line_start = t.index(f"{idx} linestart")
            if t.compare(idx, "==", line_start):
                return t.index(f"{idx} -1c")
            char_content = t.get(line_start, idx)
            if not char_content:
                return t.index(f"{idx} -1c")
            last_char = char_content[-1]
            if last_char.isalnum() or last_char == '_':
                for i in range(len(char_content) - 1, -1, -1):
                    c = char_content[i]
                    if not (c.isalnum() or c == '_'):
                        return t.index(f"{line_start} +{i+1}c")
                return line_start
            elif last_char.isspace():
                for i in range(len(char_content) - 1, -1, -1):
                    c = char_content[i]
                    if not c.isspace():
                        return t.index(f"{line_start} +{i+1}c")
                return line_start
            else:
                for i in range(len(char_content) - 1, -1, -1):
                    c = char_content[i]
                    if c.isalnum() or c == '_' or c.isspace():
                        return t.index(f"{line_start} +{i+1}c")
                return line_start

        def _on_ctrl_right(event, t: tk.Text) -> str:
            next_idx = _get_next_word_index(t, tk.INSERT)
            t.mark_set(tk.INSERT, next_idx)
            t.tag_remove("sel", "1.0", tk.END)
            t.see(tk.INSERT)
            return "break"

        def _on_ctrl_left(event, t: tk.Text) -> str:
            prev_idx = _get_prev_word_index(t, tk.INSERT)
            t.mark_set(tk.INSERT, prev_idx)
            t.tag_remove("sel", "1.0", tk.END)
            t.see(tk.INSERT)
            return "break"

        def _on_ctrl_shift_right(event, t: tk.Text) -> str:
            try:
                has_sel = t.tag_ranges("sel")
            except Exception:
                has_sel = False
            if not has_sel:
                t.mark_set("anchor", tk.INSERT)
            next_idx = _get_next_word_index(t, tk.INSERT)
            t.mark_set(tk.INSERT, next_idx)
            t.tag_remove("sel", "1.0", tk.END)
            if t.compare("anchor", "<", tk.INSERT):
                t.tag_add("sel", "anchor", tk.INSERT)
            else:
                t.tag_add("sel", tk.INSERT, "anchor")
            t.see(tk.INSERT)
            return "break"

        def _on_ctrl_shift_left(event, t: tk.Text) -> str:
            try:
                has_sel = t.tag_ranges("sel")
            except Exception:
                has_sel = False
            if not has_sel:
                t.mark_set("anchor", tk.INSERT)
            prev_idx = _get_prev_word_index(t, tk.INSERT)
            t.mark_set(tk.INSERT, prev_idx)
            t.tag_remove("sel", "1.0", tk.END)
            if t.compare("anchor", "<", tk.INSERT):
                t.tag_add("sel", "anchor", tk.INSERT)
            else:
                t.tag_add("sel", tk.INSERT, "anchor")
            t.see(tk.INSERT)
            return "break"

        def _on_double_click(event, t: tk.Text) -> str:
            click_idx = t.index(f"@{event.x},{event.y}")
            line_start = t.index(f"{click_idx} linestart")
            line_end = t.index(f"{click_idx} lineend")
            
            # Get the line content and relative column of the click index
            col = int(click_idx.split(".")[1])
            line_content = t.get(line_start, line_end)
            if not line_content or col >= len(line_content):
                return "break"
                
            click_char = line_content[col]
            
            if click_char.isalnum() or click_char == '_':
                start_col = col
                while start_col > 0 and (line_content[start_col - 1].isalnum() or line_content[start_col - 1] == '_'):
                    start_col -= 1
                end_col = col
                while end_col < len(line_content) and (line_content[end_col].isalnum() or line_content[end_col] == '_'):
                    end_col += 1
            elif click_char.isspace():
                start_col = col
                while start_col > 0 and line_content[start_col - 1].isspace():
                    start_col -= 1
                end_col = col
                while end_col < len(line_content) and line_content[end_col].isspace():
                    end_col += 1
            else:
                # Select a run of the same character type (e.g. punctuation run)
                start_col = col
                while start_col > 0 and line_content[start_col - 1] == click_char:
                    start_col -= 1
                end_col = col
                while end_col < len(line_content) and line_content[end_col] == click_char:
                    end_col += 1
                
            row = click_idx.split(".")[0]
            start_idx = f"{row}.{start_col}"
            end_idx = f"{row}.{end_col}"
            
            # Select the word
            t.tag_remove("sel", "1.0", tk.END)
            t.tag_add("sel", start_idx, end_idx)
            # Set cursor and anchor
            t.mark_set(tk.INSERT, end_idx)
            t.mark_set("anchor", start_idx)
            return "break"

        def _build_tab(file_path: Path, defer_highlight=False):
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                content = f"# Error reading file: {exc}\n"

            tab_frame = tk.Frame(nb, bg=Theme.BG_DARKEST)
            nb.add(tab_frame, text=file_path.name)

            # ── Editor area with line numbers ─────────────────────────────
            editor_area = tk.Frame(tab_frame, bg=Theme.BG_DARKEST)
            editor_area.pack(fill=tk.BOTH, expand=True)

            # Line number gutter
            lineno_text = tk.Text(
                editor_area,
                width=7, padx=4, pady=4,
                font=self.editor_font,
                bg=Theme.BG_MID, fg=Theme.TEXT_DIM,
                bd=0, relief=tk.FLAT,
                state=tk.DISABLED,
                takefocus=False,
                cursor="arrow",
            )
            lineno_text.pack(side=tk.LEFT, fill=tk.Y)

            # Separator between gutter and editor
            tk.Frame(editor_area, bg=Theme.BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y)

            # Main editor widget
            txt = tk.Text(
                editor_area,
                font=self.editor_font,
                bg=Theme.BG_DARKEST,
                fg=Theme.TEXT,
                insertbackground=Theme.CYAN,
                selectbackground=Theme.BG_HOVER,
                selectforeground=Theme.TEXT_BRIGHT,
                bd=0, relief=tk.FLAT,
                padx=8, pady=4,
                undo=True,
                maxundo=-1,
                wrap=tk.NONE,   # horizontal scroll for long lines
                tabs="16p",     # ~2-space tab stop width in Consolas 10
            )
            txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            # Scrollbars
            vsb = tk.Scrollbar(
                editor_area, orient=tk.VERTICAL,
                command=lambda *a: [txt.yview(*a), lineno_text.yview(*a)],
                bg=Theme.TEXT_DIM,  # Highly visible flat grey-blue handle
                troughcolor=Theme.BG_DARKEST,
                activebackground=Theme.CYAN,  # Glow cyan when hovered or active
                bd=0,
                elementborderwidth=0,
                width=14,  # Custom width for optimal visibility & clickability
                highlightthickness=0,
            )
            vsb.pack(side=tk.RIGHT, fill=tk.Y)
            if sys.platform == "win32":
                try:
                    import ctypes
                    ctypes.windll.uxtheme.SetWindowTheme(vsb.winfo_id(), "", "")
                except Exception:
                    pass
            txt.config(yscrollcommand=lambda f, l: (vsb.set(f, l), lineno_text.yview_moveto(f)))

            # Click on line number to toggle fold
            def _on_lineno_click(event, t=txt, ln=lineno_text, tf=tab_frame):
                index = ln.index(f"@{event.x},{event.y}")
                line_num = int(index.split(".")[0])
                _toggle_fold(t, line_num, tf)
                return "break"
            lineno_text.bind("<Button-1>", _on_lineno_click)

            # Scrolling directly over the gutter (mouse wheel / trackpad) must
            # drive the main editor's view too. Previously only the editor's
            # yscrollcommand pushed its position into the gutter (one-way
            # sync), so scrolling with the cursor over the line numbers left
            # the code pane exactly where it was -- the gutter and the code
            # drifted apart and line numbers no longer matched their lines.
            # Here we redirect the gutter's own wheel scroll into the editor;
            # the editor's existing yscrollcommand then pulls the gutter back
            # into sync automatically, keeping a single source of truth.
            def _on_lineno_scroll(event, t=txt):
                if getattr(event, "delta", 0):
                    t.yview_scroll(int(-1 * (event.delta / 120)), "units")
                elif getattr(event, "num", None) == 4:
                    t.yview_scroll(-3, "units")
                elif getattr(event, "num", None) == 5:
                    t.yview_scroll(3, "units")
                return "break"
            lineno_text.bind("<MouseWheel>", _on_lineno_scroll)
            lineno_text.bind("<Button-4>", _on_lineno_scroll)   # Linux scroll up
            lineno_text.bind("<Button-5>", _on_lineno_scroll)   # Linux scroll down

            # Configure syntax-highlight tag colours
            for tag, color in SYN_COLORS.items():
                txt.tag_configure(tag, foreground=color)
            txt.tag_configure("syn_comment1", foreground=Theme.TEXT_DIM, font=self.editor_font_italic)
            txt.tag_configure("syn_comment2", foreground=Theme.TEXT_DIM, font=self.editor_font_italic)
            txt.tag_configure("syn_string", foreground=Theme.GREEN)
            txt.tag_configure("syn_kw", foreground=Theme.BLUE, font=self.editor_font_bold)
            txt.tag_configure("active_line", background="#1e2430")
            txt.tag_configure("search_match", background="#b07b00", foreground="#ffffff")
            txt.tag_configure("search_match_active", background="#ff9f00", foreground="#000000")
            txt.tag_raise("search_match", "active_line")
            txt.tag_raise("search_match_active", "search_match")
            txt.tag_raise("sel", "active_line")

            # Load content
            txt.insert("1.0", content)
            original_snapshot = txt.get("1.0", tk.END)

            tab_data[tab_frame] = {
                "path":       file_path,
                "text":       txt,
                "lineno_text": lineno_text,
                "modified":   False,
                "original":   original_snapshot,
            }

            # Initial highlight + line numbers + line highlight
            # When defer_highlight is True, skip the expensive passes —
            # _reload_files will schedule them incrementally via after_idle.
            if not defer_highlight:
                _highlight(txt)
                _sync_linenos(txt, lineno_text)
            _update_line_highlight(txt)

            # ── Event bindings ─────────────────────────────────────────────
            def _on_key(event, f=tab_frame, t=txt, p=file_path, ln=lineno_text):
                self.root.after(1, lambda: (
                    _mark_modified(f, t, p),
                    _sync_linenos(t, ln),
                    _highlight_after(t),
                    _update_cursor_label(t),
                    _update_line_highlight(t),
                ))

            def _on_click(event, t=txt, ln=lineno_text):
                self.root.after(1, lambda: (
                    _update_cursor_label(t),
                    _update_line_highlight(t),
                    lineno_text.yview_moveto(t.yview()[0]),
                ))

            # ── Smart-indent key bindings (bound before <KeyRelease>) ──────
            txt.bind("<Return>",        lambda e, t=txt: _on_return(e, t))
            txt.bind("<Tab>",           lambda e, t=txt: _on_tab(e, t))
            txt.bind("<Shift-Tab>",     lambda e, t=txt: _on_shift_tab(e, t))
            txt.bind("<ISO_Left_Tab>",  lambda e, t=txt: _on_shift_tab(e, t))  # Linux
            txt.bind("<BackSpace>",     lambda e, t=txt: _on_backspace(e, t))
            txt.bind("<braceleft>",     lambda e, t=txt: _on_open_pair(e, t, "{"))
            txt.bind("<braceright>",    lambda e, t=txt: _on_closing_brace(e, t))
            txt.bind("<parenleft>",     lambda e, t=txt: _on_open_pair(e, t, "("))
            txt.bind("<parenright>",    lambda e, t=txt: _on_close_pair(e, t, ")"))
            txt.bind("<bracketleft>",   lambda e, t=txt: _on_open_pair(e, t, "["))
            txt.bind("<bracketright>",  lambda e, t=txt: _on_close_pair(e, t, "]"))

            # Word navigation bindings (Arduino IDE style)
            txt.bind("<Control-Right>",         lambda e, t=txt: _on_ctrl_right(e, t))
            txt.bind("<Control-Left>",          lambda e, t=txt: _on_ctrl_left(e, t))
            txt.bind("<Control-Shift-Right>",   lambda e, t=txt: _on_ctrl_shift_right(e, t))
            txt.bind("<Control-Shift-Left>",    lambda e, t=txt: _on_ctrl_shift_left(e, t))
            txt.bind("<Double-Button-1>",       lambda e, t=txt: _on_double_click(e, t))

            # General after-key refresh (highlight, line-nos, dirty flag, cursor)
            txt.bind("<KeyRelease>",        _on_key)
            txt.bind("<ButtonRelease-1>",   _on_click)
            txt.bind("<Control-s>",         lambda e, f=tab_frame: (_save_tab(f), "break")[1])
            txt.bind("<Control-S>",         lambda e, f=tab_frame: (_save_tab(f), "break")[1])
            txt.bind("<Control-slash>",     _toggle_comment)

        def _reload_files():
            for tab in nb.tabs():
                nb.forget(tab)
            tab_data.clear()

            # Track tabs that need deferred highlighting
            _deferred_highlight_tabs = []

            sketch_dir = self.sketch_dir_path
            files = []
            for ext in ("*.ino", "*.cpp", "*.c", "*.h", "*.txt"):
                files.extend(sorted(sketch_dir.glob(ext)))

            order_file = sketch_dir / ".mcu_flash_tab_order.json"
            if order_file.exists():
                try:
                    import json
                    saved_order = json.loads(order_file.read_text(encoding="utf-8"))
                    file_map = {}
                    for f in files:
                        try:
                            rel = str(f.relative_to(sketch_dir))
                        except Exception:
                            rel = str(f)
                        file_map[rel] = f
                    ordered_files = []
                    for name in saved_order:
                        if name in file_map:
                            ordered_files.append(file_map.pop(name))
                    ordered_files.extend(file_map.values())
                    files = ordered_files
                except Exception:
                    pass
            
            if not files:
                placeholder_frame = tk.Frame(nb, bg=Theme.BG_DARKEST)
                nb.add(placeholder_frame, text="Empty")
                lbl_empty = tk.Label(
                    placeholder_frame,
                    text="No source files (.ino / .cpp / .c / .h / .txt) found in project folder.",
                    font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST
                )
                lbl_empty.pack(expand=True)
                lbl_filepath.config(text="")
                lbl_cursor.config(text="")
                return

            for idx, f in enumerate(files):
                # Only highlight the first (visible) tab immediately;
                # defer the rest so the GUI paints instantly.
                _build_tab(f, defer_highlight=(idx > 0))
                if idx > 0:
                    tab_id = nb.tabs()[-1]
                    _deferred_highlight_tabs.append(tab_id)

            if nb.tabs():
                first = parent_frame.nametowidget(nb.tabs()[0])
                data = tab_data.get(first)
                if data:
                    lbl_filepath.config(text=str(data["path"]))
                    lbl_cursor.config(text="Ln 1, Col 1")
                    lbl_editor_status.config(text="")

            # Schedule deferred highlighting for background tabs one at a time
            # so the event loop stays responsive between each tab.
            def _highlight_deferred(remaining):
                if not remaining:
                    return
                tab_id = remaining[0]
                try:
                    frame = parent_frame.nametowidget(tab_id)
                    d = tab_data.get(frame)
                    if d:
                        _highlight(d["text"])
                        _sync_linenos(d["text"], d["lineno_text"])
                except Exception:
                    pass
                # Schedule next tab after a tiny delay to let the UI breathe
                if len(remaining) > 1:
                    self.root.after(10, lambda: _highlight_deferred(remaining[1:]))

            if _deferred_highlight_tabs:
                self.root.after_idle(lambda: _highlight_deferred(_deferred_highlight_tabs))

        self._load_editor_files = _reload_files
        self._save_all_editor_files = _save_all
        self._save_current_editor_file = _save_current
        self._reload_current_editor_file = _reload_current
        _reload_files()

        # ── Tab switch: update status bar ─────────────────────────────────
        def _on_tab_changed(event):
            cur = nb.select()
            if not cur:
                return
            frame = parent_frame.nametowidget(cur)
            data = tab_data.get(frame)
            if data:
                lbl_filepath.config(text=str(data["path"]))
                _update_cursor_label(data["text"])
                _update_line_highlight(data["text"])
                data["text"].focus_set()

        nb.bind("<<NotebookTabChanged>>", _on_tab_changed)

        # ── Tab Drag-and-Drop Reordering ─────────────────────────────────
        self._dragged_tab_idx = None

        def _save_default_editor_tab_order():
            if not self.sketch_dir_path:
                return
            paths = []
            for tab_id in nb.tabs():
                widget = nb.nametowidget(tab_id)
                data = tab_data.get(widget)
                if data and "path" in data:
                    try:
                        rel = str(data["path"].relative_to(self.sketch_dir_path))
                    except Exception:
                        rel = str(data["path"])
                    paths.append(rel)
            order_file = self.sketch_dir_path / ".mcu_flash_tab_order.json"
            try:
                import json
                order_file.write_text(json.dumps(paths, indent=2), encoding="utf-8")
            except Exception:
                pass

        def _on_tab_drag_start(event):
            widget = event.widget
            if widget.identify(event.x, event.y) != "label":
                return
            try:
                self._dragged_tab_idx = widget.index(f"@{event.x},{event.y}")
            except Exception:
                self._dragged_tab_idx = None

        def _on_tab_drag_motion(event):
            if self._dragged_tab_idx is None:
                return
            widget = event.widget
            if widget.identify(event.x, event.y) != "label":
                return
            try:
                target_idx = widget.index(f"@{event.x},{event.y}")
                if target_idx != self._dragged_tab_idx:
                    active_tab = widget.select()
                    widget.unbind("<<NotebookTabChanged>>")
                    tab_child = widget.tabs()[self._dragged_tab_idx]
                    widget.insert(target_idx, tab_child)
                    widget.select(active_tab)
                    widget.bind("<<NotebookTabChanged>>", _on_tab_changed)
                    self._dragged_tab_idx = target_idx
                    _save_default_editor_tab_order()
            except Exception:
                pass

        def _on_tab_drag_release(event):
            self._dragged_tab_idx = None

        nb.bind("<ButtonPress-1>", _on_tab_drag_start, add="+")
        nb.bind("<B1-Motion>", _on_tab_drag_motion, add="+")
        nb.bind("<ButtonRelease-1>", _on_tab_drag_release, add="+")

        # Window-level keyboard bindings bound to root
        self.root.bind("<Control-f>", _toggle_find_panel)
        self.root.bind("<Control-F>", _toggle_find_panel)
        self.root.bind("<Control-slash>", _toggle_comment)
        self.root.bind("<Control-Key-equal>", _zoom_in)
        self.root.bind("<Control-Key-plus>", _zoom_in)
        self.root.bind("<Control-Key-minus>", _zoom_out)
        self.root.bind("<Control-MouseWheel>", _zoom_wheel)

    def _find_editor_hwnd(self):
        """Locate the pywebview editor's native window handle.

        Searches via EnumWindows for the unique substring in the window title.
        This is extremely robust against title re-writes and WebView2 subprocess boundaries
        which otherwise fail simple PID checks.
        """
        if win32gui is None:
            return None

        # 1. Substring search for unique editor identifier
        found = []
        def _cb(hwnd, _):
            try:
                title = win32gui.GetWindowText(hwnd)
                if "Embedded Code Editor" in title:
                    found.append(hwnd)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception:
            pass

        if found:
            return found[0]

        # 2. Fallback to exact match
        hwnd = win32gui.FindWindow(None, EDITOR_WINDOW_TITLE)
        if hwnd:
            return hwnd

        before = getattr(self, "_editor_pre_create_hwnds", None)
        if before is None:
            return None

        root_hwnd = None
        try:
            root_hwnd = self.root.winfo_id()
        except Exception:
            pass

        current = _list_own_toplevel_hwnds()
        new_hwnds = [h for h in (current - before) if h != root_hwnd]
        if len(new_hwnds) == 1:
            return new_hwnds[0]
        if len(new_hwnds) > 1:
            # Ambiguous — prefer one that still reports our title text
            # somewhere, otherwise just take the first candidate.
            for h in new_hwnds:
                try:
                    if EDITOR_WINDOW_TITLE.split(" —")[0] in win32gui.GetWindowText(h):
                        return h
                except Exception:
                    pass
            return new_hwnds[0]
        return None

    def _animate_editor_spinner(self):
        """Rotating-arc spinner — keeps the panel visibly 'alive' while
        loading instead of looking like a dead/blank box."""
        canvas = getattr(self, "_editor_spinner_canvas", None)
        if not canvas or not canvas.winfo_exists():
            self._editor_spinner_job = None
            return
        canvas.delete("all")
        angle = self._editor_spinner_angle
        canvas.create_arc(
            4, 4, 44, 44,
            start=angle, extent=120,
            outline=Theme.CYAN, width=4, style=tk.ARC
        )
        self._editor_spinner_angle = (angle + 20) % 360
        self._editor_spinner_job = self.root.after(50, self._animate_editor_spinner)

    def _stop_editor_spinner(self):
        job = getattr(self, "_editor_spinner_job", None)
        if job:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
            self._editor_spinner_job = None

    def _reveal_editor_if_ready(self):
        """Only hide the loading placeholder once BOTH the native window
        is reparented+shown AND the page itself reports it finished
        loading. Doing it in one step (right after .show()) is what left
        a blank gap before — the window was visible but empty."""
        if getattr(self, "_editor_embedded", False) and getattr(self, "_editor_content_loaded", False):
            self._stop_editor_spinner()
            self._editor_placeholder.place_forget()

    def _try_embed_editor_window(self):
        """Reparent the pywebview native window into the Tkinter editor
        frame (Windows only, requires pywin32). Retries briefly since the
        native window may not exist yet the first time this fires — it's
        created asynchronously once webview.start() takes over the main
        thread. Falls back to the 'Open Editor Window' button if pywin32
        is unavailable or embedding doesn't succeed after several tries.
        """
        if win32gui is None or win32con is None:
            self._append("  ⚠ pywin32 not available — editor will open in its own window.", "warning")
            self._show_editor_fallback_button()
            return

        if getattr(self, "_editor_embedded", False):
            return

        # Ensure main window is viewable (mapped) before embedding
        if not self.root.winfo_exists() or not self.root.winfo_viewable():
            self.root.after(150, self._try_embed_editor_window)
            return

        hwnd = self._find_editor_hwnd()
        if not hwnd:
            self._editor_reparent_attempts += 1
            if self._editor_reparent_attempts < 80:  # ~8 seconds of retrying
                self.root.after(100, self._try_embed_editor_window)
            else:
                self._append("  ✖ Could not locate the editor's window to embed it after 8s — "
                              "falling back to a separate window.", "error")
                self._show_editor_fallback_button()
            return

        try:
            frame = self._editor_embed_frame
            frame.update_idletasks()
            tk_hwnd = frame.winfo_id()

            # Strip title bar / borders / system menu so the window
            # behaves like a plain child control rather than a floating
            # top-level window.
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            style &= ~(win32con.WS_CAPTION | win32con.WS_THICKFRAME |
                       win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX |
                       win32con.WS_SYSMENU | win32con.WS_POPUP | win32con.WS_BORDER)
            style |= win32con.WS_CHILD
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)

            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex_style &= ~(win32con.WS_EX_DLGMODALFRAME | win32con.WS_EX_APPWINDOW |
                          win32con.WS_EX_WINDOWEDGE | win32con.WS_EX_CLIENTEDGE)
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

            # Re-parent it under the Tkinter frame.
            win32gui.SetParent(hwnd, tk_hwnd)

            # The editor window (WebView2/pywebview) lives on the main
            # thread, while this Tk frame lives on tk_thread. Reparenting
            # across threads without linking their input queues leaves the
            # child's message routing broken — Windows/Chromium ends up
            # marking the app "Not Responding" as soon as the editor tries
            # to paint/receive input in its new parent. AttachThreadInput
            # links the two threads' input state so messages route
            # correctly after the SetParent call above.
            #
            # CAVEAT: this call itself is the highest-risk step in this
            # whole embedding path. It only works cleanly if BOTH threads
            # are already pumping Windows messages at the moment it's
            # called. On a freshly rebuilt env, WebView2 has to do its
            # one-time first-run initialization (spawning its own browser
            # process, writing a fresh user-data folder) which can leave
            # its host thread mid-init — not yet pumping — right when our
            # 100ms polling loop first spots its hwnd. Attaching input
            # queues at that exact moment is what produces a hard freeze
            # of the ENTIRE app (not just the editor), because Windows
            # serializes input across attached threads and one side isn't
            # servicing it. ctypes also does not raise on failure here by
            # default, so a silent AttachThreadInput failure previously
            # looked identical to success — the `except` below never fired
            # even when the call plainly did not succeed.
            self._editor_attach_ok = False
            try:
                editor_tid, _pid = win32process.GetWindowThreadProcessId(hwnd)
                import ctypes
                tk_tid = ctypes.windll.kernel32.GetCurrentThreadId()
                if editor_tid and editor_tid != tk_tid:
                    ctypes.set_last_error(0)
                    attach_res = ctypes.windll.user32.AttachThreadInput(tk_tid, editor_tid, True)
                    if not attach_res:
                        err = ctypes.get_last_error()
                        self._append(
                            f"  ⚠ AttachThreadInput reported failure (Win32 error {err}) — "
                            f"editor may still freeze the app.", "warning"
                        )
                    else:
                        self._editor_attached_threads = (tk_tid, editor_tid)
                        self._editor_attach_ok = True
                        # Self-healing watchdog: if this attach turns out to
                        # freeze the app (see caveat above), detect it and
                        # detach automatically instead of requiring the user
                        # to force-kill the process from Task Manager.
                        self._start_editor_hang_watchdog(tk_hwnd, tk_tid, editor_tid)
            except Exception as e:
                self._append(f"  ⚠ AttachThreadInput failed (editor may still freeze): {e}", "warning")

            w = max(frame.winfo_width(), 50)
            h = max(frame.winfo_height(), 50)
            win32gui.SetWindowPos(
                hwnd, 0, 0, 0, w, h,
                win32con.SWP_FRAMECHANGED | win32con.SWP_NOZORDER | win32con.SWP_SHOWWINDOW
            )

            # Raw SetWindowPos(SWP_SHOWWINDOW) only flips the native HWND's
            # visible bit. WebView2's own Controller.IsVisible flag tracks
            # the managed WinForms control's Visible property instead, which
            # only flips when pywebview's own show() runs. Skip this and the
            # HWND is visible but the browser control never paints — you get
            # a blank white rectangle instead of content.
            try:
                if hasattr(self, "editor_window"):
                    self.editor_window.show()
            except Exception as e:
                self._append(f"  ⚠ editor_window.show() failed: {e}", "warning")

            self._editor_hwnd = hwnd
            self._editor_embedded = True
            self._append("  ✓ Code editor embedded into the main window.", "success")
            self._editor_status_lbl.configure(text="📝 Rendering editor…")
            self._editor_desc_lbl.configure(text="Waiting for the editor page to finish loading…")
            self._reveal_editor_if_ready()
            
        except Exception as e:
            self._append(f"  ✖ Failed to embed editor window: {e}", "error")
            self._show_editor_fallback_button()

    def _start_editor_hang_watchdog(self, tk_hwnd, tk_tid, editor_tid):
        """Watch for the exact failure mode AttachThreadInput can cause:
        the whole app going 'Not Responding' because the editor's thread
        wasn't pumping messages yet when we attached input queues to it
        (see the long comment in _try_embed_editor_window).

        This runs on its own plain Python thread — NOT through Tk's
        after()/mainloop — specifically so it keeps running even if the
        Tk thread itself is the one that's stuck. Windows exposes exactly
        the classification we need via IsHungAppWindow(), so we poll that
        for a few seconds after attaching; if it fires, we immediately
        detach the input queues ourselves (self-healing) instead of
        requiring the user to force-kill the process from Task Manager.
        """
        import ctypes

        def _watch():
            user32 = ctypes.windll.user32
            # Give WebView2 a realistic window to finish first-run/first-
            # paint init before we start judging responsiveness; a cold
            # (freshly rebuilt env) first launch is slower than normal.
            time.sleep(1.5)
            detached = False
            for _ in range(20):  # ~10s of checking, every 0.5s
                try:
                    if not getattr(self, "_editor_embedded", False):
                        return  # embed path already failed/changed elsewhere
                    if user32.IsHungAppWindow(tk_hwnd):
                        try:
                            user32.AttachThreadInput(tk_tid, editor_tid, False)
                        except Exception:
                            pass
                        detached = True
                        break
                except Exception:
                    return
                time.sleep(0.5)

            if detached:
                self._editor_attach_ok = False
                self._editor_attached_threads = None
                # Tk's Tcl notifier should start servicing its message
                # queue again immediately after detaching — schedule the
                # notice through root.after rather than touching widgets
                # directly from this background thread.
                def _notify():
                    self._append(
                        "  ⚠ Detected the editor freezing the app right after embedding — "
                        "automatically detached it to recover. The editor will open in its "
                        "own window instead this session.", "warning"
                    )
                    self._editor_embedded = False
                    self._editor_hwnd = None
                    self._show_editor_fallback_button()
                try:
                    self.root.after(0, _notify)
                except Exception:
                    pass

        threading.Thread(target=_watch, daemon=True).start()

    def _cleanup_active_editor(self):
        """Clean up the active editor by terminating QScintilla subprocesses,
        hiding Monaco pywebview windows, detaching Windows handles/hooks,
        canceling pending after jobs, and destroying all Tk widgets inside the editor frame."""
        # 1. Terminate QScintilla subprocess
        proc = getattr(self, "_qsci_proc", None)
        if proc and proc.poll() is None:
            # Flush unsaved files if possible
            if hasattr(self, "_save_all_editor_files") and callable(self._save_all_editor_files):
                try:
                    self._save_all_editor_files()
                    import time as _t; _t.sleep(0.1)
                except Exception:
                    pass
            try:
                if sys.platform == "win32":
                    import subprocess
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                else:
                    proc.terminate()
                    proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._qsci_proc = None

        # 2. Detach and hide Monaco pywebview window
        hwnd = getattr(self, "_editor_hwnd", None)
        if hwnd and win32gui is not None:
            try:
                # Reparent back to desktop/no parent so it doesn't get destroyed
                # when the Tk frame children are destroyed.
                win32gui.SetParent(hwnd, 0)
                if win32con is not None:
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            except Exception:
                pass
        
        if hasattr(self, "editor_window") and self.editor_window:
            try:
                self.editor_window.hide()
            except Exception:
                pass

        # 3. Detach thread inputs if we attached them
        pair = getattr(self, "_editor_attached_threads", None)
        if pair and win32gui is not None:
            try:
                import ctypes
                ctypes.windll.user32.AttachThreadInput(pair[0], pair[1], False)
            except Exception:
                pass
            self._editor_attached_threads = None

        # 4. Cancel pending Tk after jobs
        for job_attr in ("_editor_spinner_job", "_editor_resize_job"):
            job = getattr(self, job_attr, None)
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
                setattr(self, job_attr, None)

        # 5. Clear layout
        if hasattr(self, "editor_frame") and self.editor_frame:
            try:
                self.editor_frame.unbind("<Configure>")
            except Exception:
                pass
            for child in self.editor_frame.winfo_children():
                try:
                    child.destroy()
                except Exception:
                    pass

        # 6. Reset layout properties
        self._editor_hwnd = None
        self._editor_embedded = False
        self._editor_content_loaded = False
        self._editor_reparent_attempts = 0
        self.editor_notebook = None
        if hasattr(self, "editor_tab_data"):
            self.editor_tab_data.clear()

    def _show_editor_fallback_button(self):
        """Embedding isn't available — swap the 'loading' placeholder for
        the 'Open Editor Window' popup button so the editor is still
        reachable."""
        if getattr(self, "_editor_embedded", False):
            return
        mode = getattr(self, "editor_mode", "default")
        if mode == "qscintilla":
            name = "QScintilla"
        else:
            name = "Monaco"
        self._editor_status_lbl.configure(text=f"📝 {name} Editor Active")
        self._editor_desc_lbl.configure(text="The editor is running in a separate window.")
        self._editor_fallback_btn.pack(pady=15)

    def _resize_embedded_editor(self, event=None):
        """Keep the embedded editor window's size in sync with the Tk
        frame hosting it — called on every <Configure> of that frame.
        Debounced and uses SWP_ASYNCWINDOWPOS to prevent freezes during
        rapid resizing, maximization, or fullscreen transitions on any device.
        """
        if not getattr(self, "_editor_embedded", False) or not self._editor_hwnd or win32gui is None:
            return
        
        if hasattr(self, "_editor_resize_job") and self._editor_resize_job:
            try:
                self.root.after_cancel(self._editor_resize_job)
            except Exception:
                pass
            self._editor_resize_job = None

        def _do_resize():
            self._editor_resize_job = None
            try:
                if not getattr(self, "_editor_embedded", False) or not self._editor_hwnd or win32gui is None:
                    return
                w = max(self._editor_embed_frame.winfo_width(), 10)
                h = max(self._editor_embed_frame.winfo_height(), 10)
                
                last_w = getattr(self, "_last_editor_w", 0)
                last_h = getattr(self, "_last_editor_h", 0)
                if w == last_w and h == last_h:
                    return
                    
                self._last_editor_w = w
                self._last_editor_h = h
                # SWP_ASYNCWINDOWPOS (0x4000) prevents blocking the calling
                # (Tk) thread while the other thread processes the resize —
                # this is the key to freeze-free resizing and fullscreen toggles.
                win32gui.SetWindowPos(
                    self._editor_hwnd, 0, 0, 0, w, h,
                    win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | 0x4000
                )
            except Exception:
                pass

        try:
            self._editor_resize_job = self.root.after(30, _do_resize)
        except Exception:
            pass

    def _set_embedded_editor_visible(self, visible: bool):
        """Show/hide the embedded editor window when its pane is toggled
        via Hide/Show Editor. A reparented native window doesn't
        automatically follow its Tk parent frame's mapped state, so this
        has to be done explicitly."""
        if not getattr(self, "_editor_embedded", False) or not self._editor_hwnd or win32gui is None:
            return
        try:
            win32gui.ShowWindow(
                self._editor_hwnd,
                win32con.SW_SHOW if visible else win32con.SW_HIDE
            )
        except Exception:
            pass

    def _open_download_manager(self):
        import subprocess, sys
        from pathlib import Path
        script_dir = SCRIPT_DIR
        script_path = script_dir / "src" / "libs" / "arduino_lib_req.py"
        
        # Grab our own native window handle so the child process can make
        # itself an "owned" window of this one (stays on top of us, hides
        # when we minimize, no separate taskbar entry).
        parent_hwnd = None
        if sys.platform == "win32":
            try:
                self.root.update_idletasks()
                parent_hwnd = self.root.winfo_id()
            except Exception:
                parent_hwnd = None
        
        # Check if we are running as a PyInstaller executable
        is_frozen = getattr(sys, 'frozen', False)
        import os
        env = os.environ.copy()
        env["MCU_PREF_DIR"] = str(SCRIPT_DIR)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        for k in ["_MEIPASS", "_MEIPASS2", "PYTHONHOME", "PYTHONPATH"]:
            env.pop(k, None)
            
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            # Clean up the parent's _MEIPASS folder from the PATH
            path_val = env.get("PATH", "")
            paths = path_val.split(os.pathsep)
            cleaned_paths = [p for p in paths if p != meipass]
            env["PATH"] = os.pathsep.join(cleaned_paths)
            
        # We always launch the python script using the local virtualenv python interpreter.
        # This completely avoids any PyInstaller DLL packaging/LoadLibrary conflicts.
        try:
            venv_dir = SCRIPT_DIR / "env"
            if sys.platform == "win32":
                python_exe = venv_dir / "Scripts" / "pythonw.exe"
                if not python_exe.exists():
                    python_exe = venv_dir / "Scripts" / "python.exe"
            else:
                python_exe = venv_dir / "bin" / "python"

            if python_exe.exists() and script_path.exists():
                cmd = [str(python_exe), str(script_path)]
                if parent_hwnd:
                    cmd += ["--parent-hwnd", str(parent_hwnd)]
                p = subprocess.Popen(cmd, env=env)
            else:
                python_exe = sys.executable
                if sys.platform == "win32" and not getattr(sys, 'frozen', False):
                    pythonw = Path(python_exe).parent / "pythonw.exe"
                    if pythonw.exists():
                        python_exe = str(pythonw)
                cmd = [str(python_exe), str(script_path)]
                if parent_hwnd:
                    cmd += ["--parent-hwnd", str(parent_hwnd)]
                p = subprocess.Popen(cmd, env=env)
            self._download_managers.append(p)
            self.root.after(1000, self._check_downloader_running)
            self._append("  ℹ Launching Download Boards/Libraries Manager.", "info")
        except Exception as e:
            self._append(f"  ✖ Failed to launch download manager: {e}", "error")

    def _check_downloader_running(self):
        # Filter completed processes
        still_running = []
        any_finished = False
        for p in self._download_managers:
            if p.poll() is None:
                still_running.append(p)
            else:
                any_finished = True
        
        self._download_managers = still_running
        
        if any_finished:
            self._reload_supported_boards()
        
        # While the download manager is still open, periodically check
        # for filesystem changes (new board installed / deleted) so the
        # board dropdown updates live without waiting for the manager to
        # close.
        if self._download_managers: 
            self._check_boards_dir_changed()
            self.root.after(2000, self._check_downloader_running)

    def _check_boards_dir_changed(self):
        """Detect changes in the Boards download directory and reload if needed.

        Watches both the download directory path itself (in case the user
        changed it in the Download Manager) and the actual boards.txt
        files on disk.
        """
        try:
            download_dir = _get_download_dir()

            boards_path = Path(download_dir) / "Boards"
            if boards_path.is_dir():
                # Build a snapshot of board folder names + boards.txt mtimes
                current_snapshot = {("__download_dir__", download_dir)}
                for p in boards_path.glob("**/boards.txt"):
                    try:
                        current_snapshot.add((str(p), os.path.getmtime(p)))
                    except OSError:
                        current_snapshot.add((str(p), 0))
            else:
                current_snapshot = {("__download_dir__", download_dir)}

            prev = getattr(self, "_boards_dir_snapshot", None)
            if prev is None:
                # First check — store baseline, no reload needed
                self._boards_dir_snapshot = current_snapshot
            elif current_snapshot != prev:
                # Something changed on disk or the directory moved — reload boards
                self._boards_dir_snapshot = current_snapshot
                self._reload_supported_boards()
        except Exception:
            pass

    def _reload_supported_boards(self):
        """Reload the dynamic boards from disk and refresh the dropdown list.
        Runs the heavy disk scan in a background thread to keep the GUI
        responsive, then applies the result on the main thread."""
        def _bg_load():
            new_boards = load_dynamic_boards({})
            # Schedule the UI update on the main thread
            self.root.after(0, lambda: self._apply_reloaded_boards(new_boards))
        threading.Thread(target=_bg_load, daemon=True).start()

    def _apply_reloaded_boards(self, new_boards: dict):
        """Apply the reloaded board list on the main (UI) thread."""
        global SUPPORTED_BOARDS
        SUPPORTED_BOARDS = new_boards
        
        if hasattr(self, 'board_combo') and self.board_combo:
            # Update the combobox's underlying option list
            self.board_combo.update_options(list(SUPPORTED_BOARDS.keys()))
            
            # If the current selected board is no longer in SUPPORTED_BOARDS
            curr = self.board_var.get()
            if curr not in SUPPORTED_BOARDS and SUPPORTED_BOARDS:
                new_board = next((b for b in SUPPORTED_BOARDS if b.lower() == "arduino uno"), next(iter(SUPPORTED_BOARDS.keys())))
                self.board_var.set(new_board)
                self._last_valid_board = new_board
                self._on_board_changed()
            elif not curr and SUPPORTED_BOARDS:
                new_board = next((b for b in SUPPORTED_BOARDS if b.lower() == "arduino uno"), next(iter(SUPPORTED_BOARDS.keys())))
                self.board_var.set(new_board)
                self._last_valid_board = new_board
                self._on_board_changed()
                
            self._append("  ℹ Reloaded supported boards list from disk.", "info")

    def _get_cpu_cores_jobs(self) -> int:
        try:
            data = _load_raw_config()
            setting = data.get("shared", {}).get("cpu_multithreading", "HIGH")
        except Exception:
            setting = "HIGH"
        
        total_cores = os.cpu_count() or 4
        if setting == "LOW":
            return 1
        elif setting == "MEDIUM":
            return max(1, total_cores // 2)
        else:
            return total_cores

    def _open_settings(self):
        # Create dialog
        dlg = tk.Toplevel(self.root)
        dlg.title("MCU Flasher Settings")
        
        board_name = self.board_var.get()
        board_info = SUPPORTED_BOARDS.get(board_name, {})
        platform = board_info.get("platform", "").lower()
        port_desc = self.port_var.get().lower()
        is_esp = ("espressif" in platform or "esp" in board_name.lower() or 
                  "esp" in port_desc or "cp210" in port_desc or "ch9102" in port_desc)

        width = 500
        height = 560 if is_esp else 510
        center_toplevel(dlg, self.root, width, height)

        dlg.configure(bg=Theme.BG_DARKEST)
        dlg.minsize(width, 460)
        dlg.resizable(False, True)
        dlg.transient(self.root)
        dlg.grab_set()

        # Section: CPU Cores MultiThreading
        tk.Label(dlg, text="Performance Settings", font=self.font_title, fg=Theme.CYAN, bg=Theme.BG_DARKEST).pack(pady=(12, 5))

        cpu_frame = tk.Frame(dlg, bg=Theme.BG_DARKEST)
        cpu_frame.pack(fill=tk.X, padx=25, pady=5)

        tk.Label(cpu_frame, text="CPU Cores Multithreading:", font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(side=tk.LEFT, padx=(0, 10))
        
        total_cores = os.cpu_count() or 4
        half_cores = max(1, total_cores // 2)
        
        low_val = "LOW (1 Core)"
        med_val = f"MEDIUM ({half_cores} Cores)"
        high_val = f"HIGH ({total_cores} Cores)"
        
        try:
            current_setting = _load_raw_config().get("shared", {}).get("cpu_multithreading", "HIGH")
        except Exception:
            current_setting = "HIGH"
            
        default_combo_val = high_val
        if current_setting == "LOW":
            default_combo_val = low_val
        elif current_setting == "MEDIUM":
            default_combo_val = med_val
            
        cpu_var = tk.StringVar(value=default_combo_val)
        cpu_combo = ttk.Combobox(
            cpu_frame, textvariable=cpu_var, font=self.font_label, state="readonly",
            values=[low_val, med_val, high_val], width=22
        )
        cpu_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        if self.is_busy:
            cpu_combo.configure(state="disabled")

        try:
            current_g_setting = _load_raw_config().get("shared", {}).get("graphics_acceleration", "ON")
        except Exception:
            current_g_setting = "ON"

        g_var = tk.BooleanVar(value=(current_g_setting == "ON"))
        
        g_frame = tk.Frame(dlg, bg=Theme.BG_DARKEST)
        g_frame.pack(fill=tk.X, padx=25, pady=5)

        cb_g_accel = tk.Checkbutton(
            g_frame, text="Graphics Acceleration (Smooth sash resize)", variable=g_var,
            font=self.font_label, fg=Theme.TEXT, bg=Theme.BG_DARKEST,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_DARKEST,
            activeforeground=Theme.TEXT,
        )
        cb_g_accel.pack(side=tk.LEFT)
        if self.is_busy:
            cb_g_accel.configure(state="disabled")

        # Horizontal separator
        sep_editor = tk.Frame(dlg, bg=Theme.BORDER, height=1)
        sep_editor.pack(fill=tk.X, padx=25, pady=10)

        # Section: File Editor
        tk.Label(dlg, text="File Editor", font=self.font_title, fg=Theme.CYAN, bg=Theme.BG_DARKEST).pack(pady=(0, 5))

        editor_frame = tk.Frame(dlg, bg=Theme.BG_DARKEST)
        editor_frame.pack(fill=tk.X, padx=25, pady=5)

        tk.Label(editor_frame, text="Editor:", font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(side=tk.LEFT, padx=(0, 10))

        default_label = "Default (Tkinter, lightweight)"
        monaco_label = "Monaco (VS Code-style, heavier)"
        qsci_label   = "QScintilla (Qt5-based, fast C++)"
        current_editor_mode = getattr(self, "editor_mode", None) or get_editor_mode()

        if current_editor_mode == "monaco":
            _start_val = monaco_label
        elif current_editor_mode == "qscintilla":
            _start_val = qsci_label
        else:
            _start_val = default_label
        editor_var = tk.StringVar(value=_start_val)

        editor_combo = ttk.Combobox(
            editor_frame, textvariable=editor_var, font=self.font_label, state="readonly",
            values=[default_label, qsci_label, monaco_label], width=32
        )
        editor_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        if self.is_busy:
            editor_combo.configure(state="disabled")

        editor_note = tk.Label(
            dlg, text="Changing the editor takes effect the next time the app is started.",
            font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST, wraplength=440, justify=tk.LEFT
        )
        editor_note.pack(fill=tk.X, padx=25, pady=(0, 5))

        # Track whether the user has confirmed the Monaco crash-risk warning
        # during this dialog session, so we don't nag them repeatedly if they
        # flip the combobox back and forth before hitting Save.
        editor_var._monaco_confirmed = (current_editor_mode == "monaco")

        def _on_editor_choice(event=None):
            if editor_var.get() == monaco_label and not getattr(editor_var, "_monaco_confirmed", False):
                from tkinter import messagebox
                proceed = messagebox.askyesno(
                    "Monaco Editor Warning",
                    "The Monaco editor is a heavier, browser-based editor.\n\n"
                    "On low-spec devices, it may cause the application to "
                    "freeze or crash on startup.\n\n"
                    "Do you want to continue selecting Monaco?",
                    parent=dlg
                )
                if proceed:
                    editor_var._monaco_confirmed = True
                else:
                    editor_var.set(default_label)

        editor_combo.bind("<<ComboboxSelected>>", _on_editor_choice)

        # Horizontal separator
        sep = tk.Frame(dlg, bg=Theme.BORDER, height=1)
        sep.pack(fill=tk.X, padx=25, pady=10)

        # Reset & Recovery frame
        reset_frame = tk.LabelFrame(
            dlg, text="Hardware Reset Operations", font=self.font_label,
            fg=Theme.CYAN, bg=Theme.BG_DARKEST, bd=1, relief=tk.SOLID,
            padx=10, pady=10
        )
        reset_frame.pack(fill=tk.X, padx=25, pady=5)

        def run_hard_reset():
            self._do_hard_reset(dlg)

        def run_soft_reset():
            self._do_soft_reset(dlg)

        if is_esp:
            btn_hard = self._make_btn(
                reset_frame, "⚡ Hard Reset (Bootloader)", run_hard_reset,
                Theme.BTN_STOP, Theme.BTN_STOP_H, font=self.font_label
            )
            btn_hard.pack(side=tk.LEFT, padx=10, expand=True, fill=tk.X)

            btn_soft = self._make_btn(
                reset_frame, "🔄 Soft Reset (Reset Flash)", run_soft_reset,
                Theme.BTN_MONITOR, Theme.BTN_MONITOR_H, font=self.font_label
            )
            btn_soft.pack(side=tk.LEFT, padx=10, expand=True, fill=tk.X)
        else:
            btn_hard = None
            btn_soft = self._make_btn(
                reset_frame, "🔄 Soft Reset (Reset Flash)", run_soft_reset,
                Theme.BTN_MONITOR, Theme.BTN_MONITOR_H, font=self.font_label
            )
            btn_soft.pack(fill=tk.X, padx=10)
        
        if not self._is_board_recognized():
            reset_disabled_state = tk.DISABLED
            if btn_hard is not None:
                btn_hard.configure(state=reset_disabled_state)
            btn_soft.configure(state=reset_disabled_state)
            tk.Label(
                reset_frame,
                text="⚠ Board on this port hasn't been recognized yet.",
                font=self.font_status, fg=Theme.YELLOW, bg=Theme.BG_DARKEST
            ).pack(side=tk.BOTTOM, pady=(6, 0))

        btn_frame = tk.Frame(dlg, bg=Theme.BG_DARKEST)
        btn_frame.pack(fill=tk.X, pady=10, padx=25)
        
        def reset_settings():
            cpu_var.set(high_val)
            g_var.set(True)
            editor_var.set(default_label)
            editor_var._monaco_confirmed = False
            self._append("  ℹ Settings reset to default values. Click Save to apply.", "info")
            
        reset_btn = self._make_btn(btn_frame, "Reset Defaults", reset_settings, Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_label)
        reset_btn.pack(side=tk.LEFT, padx=5)

        def save_settings():
            cpu_sel = cpu_var.get()
            if "LOW" in cpu_sel:
                cpu_key = "LOW"
            elif "MEDIUM" in cpu_sel:
                cpu_key = "MEDIUM"
            else:
                cpu_key = "HIGH"
            
            g_val = "ON" if g_var.get() else "OFF"
            
            try:
                data = _load_raw_config()
                if "shared" not in data:
                    data["shared"] = {}
                data["shared"]["cpu_multithreading"] = cpu_key
                data["shared"]["graphics_acceleration"] = g_val

                if editor_var.get() == monaco_label:
                    new_editor_mode = "monaco"
                elif editor_var.get() == qsci_label:
                    new_editor_mode = "qscintilla"
                else:
                    new_editor_mode = "default"
                mode_changed = new_editor_mode != current_editor_mode
                
                if mode_changed and current_editor_mode == "monaco" and new_editor_mode in ("qscintilla", "default"):
                    from tkinter import messagebox
                    proceed = messagebox.askokcancel(
                        "Dispose Monaco Editor",
                        "Switching to another editor will dispose the Monaco editor.\n\n"
                        "To use Monaco again, you will need to restart the application.\n\n"
                        "Do you want to proceed?",
                        parent=dlg
                    )
                    if not proceed:
                        return

                data["shared"]["editor_mode"] = new_editor_mode
                if mode_changed:
                    # Fresh choice — clear any stale crash sentinel from a
                    # previous mode so the next boot check starts clean.
                    data["shared"]["monaco_boot_pending"] = False

                _save_raw_config(data)
                if cpu_key != current_setting:
                    self._append(f"  ✔ CPU multithreading set to {cpu_key}.", "success")
                if g_val != current_g_setting:
                    self._append(f"  ✔ Graphics acceleration set to {g_val}.", "success")
                if mode_changed:
                    if new_editor_mode == "monaco":
                        # Monaco requires pywebview running on the main thread —
                        # it cannot be hot-swapped at runtime. Always require a
                        # restart, regardless of whether editor_window still
                        # exists from a previous Monaco session (it's orphaned
                        # and cannot be re-embedded after cleanup).
                        from tkinter import messagebox
                        messagebox.showinfo(
                            "Restart Required",
                            "Switching to the Monaco editor requires restarting the application.\n\n"
                            "The setting has been saved, and Monaco will be active the next time you start the app.",
                            parent=dlg
                        )
                        self.editor_mode = new_editor_mode
                        self._append("  ✔ Editor mode set to Monaco (requires restart to load).", "info")
                    else:
                        self._cleanup_active_editor()
                        self.editor_mode = new_editor_mode
                        self._build_editor(self.editor_frame)
                        self._append(f"  ✔ File editor switched to {new_editor_mode.capitalize()} instantly.", "success")

                # Apply change immediately to main_pane
                if hasattr(self, "main_pane"):
                    self.main_pane.configure(opaqueresize=not g_var.get())
            except Exception as e:
                self._append(f"  ✖ Failed to save settings: {e}", "error")

            dlg.destroy()

        save_btn = self._make_btn(btn_frame, "Save", save_settings, Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, font=self.font_label)
        save_btn.pack(side=tk.RIGHT, padx=5)

        cancel_btn = self._make_btn(btn_frame, "Cancel", dlg.destroy, Theme.BTN_STOP, Theme.BTN_STOP_H, font=self.font_label)
        cancel_btn.pack(side=tk.RIGHT, padx=5)

    def _do_hard_reset(self, parent_dlg):
        # 0. Block if the board is in a boot loop — Hard Reset would make it worse
        if getattr(self, '_boot_loop_active', False):
            from tkinter import messagebox
            messagebox.showwarning(
                "Boot Loop Detected",
                "The board is currently in a boot loop.\n\n"
                "Hard Reset will not help — it erases the flash and leaves the board "
                "without an application, which causes the same loop.\n\n"
                "Use Soft Reset instead to flash a minimal sketch and restore normal operation.",
                parent=parent_dlg
            )
            return

        # 1. Check if busy (with auto-recovery for stale busy flag)
        if self.is_busy:
            if not self.process or self.process.poll() is not None:
                self.is_busy = False
                self._set_buttons_state(False)
                self._append("  ℹ Stale busy state cleared — proceeding.", "info")
            else:
                from tkinter import messagebox
                messagebox.showwarning("Busy", "The programmer is currently busy with another operation.", parent=parent_dlg)
                return

        if not self.board_var.get():
            from tkinter import messagebox
            messagebox.showwarning(
                "No Board Selected",
                "Please choose a board in the main window before performing a Hard Reset.",
                parent=parent_dlg
            )
            return

        port = self._get_port()
        if not port:
            from tkinter import messagebox
            messagebox.showwarning("No Port Selected", "Please select a serial port in the main window before resetting.", parent=parent_dlg)
            return

        if not self._is_board_recognized():
            from tkinter import messagebox
            messagebox.showwarning(
                "Board Not Recognized",
                "The board on this port hasn't been recognized yet.\n\n"
                "Wait for auto-detect to finish, or verify the correct board/port is selected.",
                parent=parent_dlg
            )
            return

        # 2. Show confirmation
        from tkinter import messagebox
        confirm = messagebox.askyesno(
            "Hard Reset Confirmation",
            "WARNING: Burning the bootloader is a direct hardware write operation. "
            "It will overwrite the boot portion of the MCU memory.\n\n"
            "Do you want to proceed?",
            parent=parent_dlg
        )
        if not confirm:
            return

        # Close settings dialog to let the user see the console output
        parent_dlg.destroy()

        # Run hard reset in background thread
        self._active_reset_kind = "hard"
        self.is_busy = True
        self._set_buttons_state(True, operation="reset")
        threading.Thread(target=self._run_hard_reset, args=(port,), daemon=True).start()

    def _run_hard_reset(self, port: str):
        try:
            self._run_hard_reset_inner(port)
        except Exception as e:
            import traceback
            try:
                with open("error_log.txt", "w", encoding="utf-8") as f:
                    traceback.print_exc(file=f)
            except Exception:
                pass
            self._append(f"  ✖ Internal error in hard reset thread: {e}", "error")
            self._set_status("Hard Reset FAILED", Theme.RED)
        finally:
            # Guarantee busy state is always cleared
            self.is_busy = False
            self._set_buttons_state(False)
            self._set_window_closable(True)

    def _run_hard_reset_inner(self, port: str):
        # Pause monitor
        was_monitoring = self._pause_monitor()

        self._append("")
        self._append("=" * 50, "header")
        self._append("  🔥 BURNING BOOTLOADER (Hard Reset)", "header")
        self._append("=" * 50, "header")
        self._append(f"  Port  : {port}", "port_highlight")
        self._append(f"  Board : {self.board_var.get()}", "dim")
        if self._detect_port_chip() is None:
            self._append("  ⚠ Unrecognized USB-serial port — proceeding anyway.", "warning")
        self._append("")
        # PlatformIO's "-t bootloader" target is AVR-only (avrdude writes fuse
        # bits on Uno/Nano).  For ESP32/ESP8266 the bootloader is bundled into
        # every normal upload, so the correct equivalent of "burn bootloader" is:
        #   1. Erase the entire flash with esptool  (wipes any bad core-dump,
        #      corrupted NVS, or stale partition table)
        #   2. Re-upload the sketch so the chip has a valid bootloader + app again
        # For Arduino Uno we fall back to the original PIO bootloader target.
        board_name = self.board_var.get()
        is_avr = SUPPORTED_BOARDS.get(board_name, {}).get("platform", "") == "atmelavr"

        ok = False

        # ── Spinner thread for Hard Reset ──────────────────────────────────────
        _hr_state   = ["Initializing"]
        _hr_active  = [True]
        _hr_frame   = [0]
        _hr_start   = time.time()
        _hr_spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

        def _hr_spin_loop():
            while _hr_active[0] and self.is_busy:
                elapsed = int(time.time() - _hr_start)
                frame   = _hr_spinner[_hr_frame[0] % len(_hr_spinner)]
                _hr_frame[0] += 1
                self._set_status(
                    f"{frame} Hard Reset: {_hr_state[0]}... ({elapsed}s)",
                    Theme.RED,
                )
                time.sleep(0.08)

        import threading as _threading_hr
        _hr_spin_thread = _threading_hr.Thread(target=_hr_spin_loop, daemon=True)
        _hr_spin_thread.start()

        if is_avr:
            # ── AVR path: PlatformIO "-t bootloader" works fine ────────────────
            self._append("  Board: AVR — using PlatformIO bootloader target.", "dim")
            _hr_state[0] = "Burning bootloader (AVR)"

            if not self._ensure_platformio_ini():
                self._append("  ✖ Failed to verify/create platformio.ini for Hard Reset.", "error")
                self.is_busy = False
                self._set_buttons_busy(False)
                if was_monitoring:
                    self._resume_monitor()
                return

            pio_path = find_pio_executable()
            if not pio_path:
                self._append("  ⚠ PlatformIO not found — installing automatically...", "warning")
                self._set_status("Installing PlatformIO...", Theme.YELLOW)
                pio_path = ensure_platformio()
                if not pio_path:
                    self._append("  ✖ Failed to install PlatformIO!", "error")
                    self.is_busy = False
                    self._set_buttons_busy(False)
                    if was_monitoring:
                        self._resume_monitor()
                    return

            # Using global os module
            jobs = self._get_cpu_cores_jobs()
            cmd = pio_path + ["run", "-t", "bootloader", "-j", str(jobs), "--upload-port", port]
            try:
                self.process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    cwd=str(self.sketch_dir_path),
                )
            except Exception as e:
                self._append(f"  ✖ Failed to execute bootloader target: {e}", "error")
                self.is_busy = False
                self._set_buttons_busy(False)
                if was_monitoring:
                    self._resume_monitor()
                return

            for line in iter(self.process.stdout.readline, ""):
                stripped = line.rstrip()
                if stripped:
                    low = stripped.lower()
                    if "error" in low or "failed" in low:
                        self._append(f"  {stripped}", "error")
                    elif any(kw in low for kw in ["success", "done", "writing", "erasing", "verified"]):
                        self._append(f"  {stripped}", "success")
                    else:
                        self._append(f"  {stripped}", "dim")

            self.process.wait()
            ok = self.process.returncode == 0

        else:
            # ── ESP32 / ESP8266 path ────────────────────────────────────────────
            # Mirrors exactly what Arduino IDE's "Burn Bootloader" does via
            # esptool_py programmer: erase the chip then write the three
            # bootloader-layer binaries at their fixed flash addresses.
            #
            #   0x1000  bootloader_dio_80m.bin  — 2nd-stage bootloader
            #   0x8000  default.csv / default_8MB.csv — partition table
            #   0xe000  boot_app0.bin            — OTA select / boot_app data
            #
            # These files live inside the framework-arduinoespressif32 package
            # that PlatformIO already downloaded, so no extra download is needed.
            # We locate them via the PlatformIO packages directory.

            import shutil as _shutil

            # ── Resolve esptool command ─────────────────────────────────────────
            esptool_exe = _shutil.which("esptool.py") or _shutil.which("esptool")
            if esptool_exe:
                esptool_cmd_base = [esptool_exe]
            else:
                esptool_cmd_base = [sys.executable, "-m", "esptool"]

            # ── Locate PlatformIO Arduino ESP32 framework package ───────────────
            pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR")
            if pio_core_dir:
                pio_packages = Path(pio_core_dir) / "packages"
            else:
                local_pio = SCRIPT_DIR / "env" / ".platformio"
                if local_pio.exists():
                    pio_packages = local_pio / "packages"
                else:
                    pio_packages = Path.home() / ".platformio" / "packages"
            framework_dir = None
            for candidate in sorted(pio_packages.glob("framework-arduinoespressif32*"), reverse=True):
                if candidate.is_dir():
                    framework_dir = candidate
                    break

            if framework_dir is None:
                self._append("  ✖ Cannot find framework-arduinoespressif32 in PlatformIO packages.", "error")
                self._append("    Run a normal compile/upload first so PlatformIO downloads the framework.", "dim")
                self.is_busy = False
                self._set_buttons_busy(False)
                if was_monitoring:
                    self._resume_monitor()
                return

            self._append(f"  Framework : {framework_dir.name}", "dim")

            # ── Locate the precompiled ESP32 libs package (arduino-esp32 3.x) ───
            # arduino-esp32 3.x (PlatformIO espressif32 ≥ 7.0) splits the build
            # into TWO packages:
            #   • framework-arduinoespressif32            — source + tools
            #   • framework-arduino-esp32-libs-<target>  — precompiled IDF libs
            #     └── esp32/bin/bootloader_dio_80m.bin   ← lives HERE in 3.x
            #
            # The older 2.x layout kept everything inside framework-arduinoespressif32
            # under tools/sdk/esp32/bin/.  We search both so either version works.
            libs_package_dir = None
            for candidate in sorted(pio_packages.glob("framework-arduino-esp32-libs*"), reverse=True):
                if candidate.is_dir():
                    libs_package_dir = candidate
                    break
            if libs_package_dir:
                self._append(f"  Libs pkg  : {libs_package_dir.name}", "dim")

            # ── Locate the three bootloader-layer binaries ──────────────────────
            # Layout differs between Arduino-ESP32 core versions:
            #
            #   2.x (espressif32 platform ≤ 6.x):
            #     bootloader : <framework>/tools/sdk/esp32/bin/bootloader_<mode>_<freq>.bin
            #     partitions : <framework>/tools/partitions/default.bin
            #     boot_app0  : <framework>/tools/partitions/boot_app0.bin
            #
            #   3.x (espressif32 platform ≥ 7.x) — SPLIT packages:
            #     bootloader : <libs_pkg>/esp32/bin/bootloader_<mode>_<freq>.bin
            #                  OR <framework>/tools/esp32-arduino-libs/esp32/bin/…
            #     partitions : <framework>/tools/partitions/default.bin   (unchanged)
            #     boot_app0  : <framework>/tools/partitions/boot_app0.bin (unchanged)
            #
            # IMPORTANT: the framework also ships per-board bootloaders under
            # variants/<board_name>/bootloader_tinyuf2.bin etc — those are
            # board-specific UF2 bootloaders, NOT the ESP-IDF 2nd-stage bootloader.
            # We must NEVER pick files from the variants/ subtree.

            tools_dir = framework_dir / "tools"

            def _is_not_variant(p: Path) -> bool:
                """Return True if path is NOT inside the variants/ subtree."""
                return "variants" not in p.parts

            bootloader_bin = None
            partitions_bin = None
            boot_app0_bin = None

            # ── Search in project's compile/build directory first ───────────────────
            # Since PlatformIO (using ESP-IDF under the hood) compiles custom
            # bootloader and partition binaries specifically for the project,
            # we should look in the project's build directory first.
            build_dir = self.sketch_dir_path / ".pio" / "build"
            if build_dir.is_dir():
                # Locate bootloader.bin (most recently modified first)
                bootloader_hits = sorted(
                    list(build_dir.rglob("bootloader.bin")),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True
                )
                if bootloader_hits:
                    bootloader_bin = bootloader_hits[0]
                    try:
                        rel = bootloader_bin.relative_to(self.sketch_dir_path)
                    except ValueError:
                        rel = bootloader_bin
                    self._append(f"  ✔ Located compiled bootloader in build dir: {rel}", "success")

                # Locate partitions.bin (most recently modified first)
                partitions_hits = sorted(
                    list(build_dir.rglob("partitions.bin")),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True
                )
                if partitions_hits:
                    partitions_bin = partitions_hits[0]
                    try:
                        rel = partitions_bin.relative_to(self.sketch_dir_path)
                    except ValueError:
                        rel = partitions_bin
                    self._append(f"  ✔ Located compiled partition table in build dir: {rel}", "success")

                # Locate boot_app0.bin (if compiled/copied to build folder)
                boot_app_hits = sorted(
                    list(build_dir.rglob("boot_app0.bin")),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True
                )
                if boot_app_hits:
                    boot_app0_bin = boot_app_hits[0]
                    try:
                        rel = boot_app0_bin.relative_to(self.sketch_dir_path)
                    except ValueError:
                        rel = boot_app0_bin
                    self._append(f"  ✔ Located compiled boot_app0 in build dir: {rel}", "success")

            # ── Fallback: Locate in PlatformIO framework/packages if not found ────
            if bootloader_bin is None:
                # 1. bootloader binary — prefer dio/80m (default for ESP32 dev module)
                #    Priority: separate libs package (3.x) → inline tools/ paths (2.x & 3.x)
                _bootloader_explicit = []
                # 3.x — separate libs package (framework-arduino-esp32-libs-esp32)
                if libs_package_dir:
                    _bootloader_explicit += [
                        libs_package_dir / "esp32" / "bin" / "bootloader_dio_80m.bin",
                        libs_package_dir / "esp32" / "bin" / "bootloader_qio_80m.bin",
                        libs_package_dir / "esp32" / "bin" / "bootloader_dout_40m.bin",
                    ]
                # 3.x — inline inside framework (older espressif32 7.x layout)
                _bootloader_explicit += [
                    tools_dir / "esp32-arduino-libs" / "esp32" / "bin" / "bootloader_dio_80m.bin",
                    tools_dir / "esp32-arduino-libs" / "esp32" / "bin" / "bootloader_qio_80m.bin",
                    tools_dir / "esp32-arduino-libs" / "esp32" / "bin" / "bootloader_dout_40m.bin",
                    # 2.x path (espressif32 ≤ 6.x)
                    tools_dir / "sdk" / "esp32" / "bin" / "bootloader_dio_80m.bin",
                    tools_dir / "sdk" / "esp32" / "bin" / "bootloader_qio_80m.bin",
                    tools_dir / "sdk" / "esp32" / "bin" / "bootloader_dout_40m.bin",
                ]
                for candidate in _bootloader_explicit:
                    if candidate.exists():
                        bootloader_bin = candidate
                        break
                if bootloader_bin is None:
                    # Broad fallback: rglob both search roots, never variants/
                    _search_roots = [tools_dir]
                    if libs_package_dir:
                        _search_roots.append(libs_package_dir)
                    hits = [p for r in _search_roots for p in r.rglob("bootloader_dio_80m.bin") if _is_not_variant(p)]
                    if not hits:
                        hits = [p for r in _search_roots for p in r.rglob("bootloader_*.bin") if _is_not_variant(p)]
                    if hits:
                        bootloader_bin = sorted(hits)[0]

            if partitions_bin is None:
                # 2. partition table — pre-compiled default.bin (tools/partitions/)
                _partitions_explicit = [
                    tools_dir / "partitions" / "default.bin",
                    framework_dir / "partitions" / "default.bin",
                ]
                for candidate in _partitions_explicit:
                    if candidate.exists():
                        partitions_bin = candidate
                        break
                if partitions_bin is None:
                    # Fallback: any default.bin that is small enough to be a partition table (<10 KB)
                    hits = [p for p in framework_dir.rglob("default.bin")
                            if _is_not_variant(p) and p.stat().st_size < 10_000]
                    if hits:
                        partitions_bin = sorted(hits)[0]

            if boot_app0_bin is None:
                # 3. boot_app0 OTA data binary (tools/partitions/)
                _boot_app0_explicit = [
                    tools_dir / "partitions" / "boot_app0.bin",
                    framework_dir / "partitions" / "boot_app0.bin",
                ]
                for candidate in _boot_app0_explicit:
                    if candidate.exists():
                        boot_app0_bin = candidate
                        break
                if boot_app0_bin is None:
                    hits = [p for p in framework_dir.rglob("boot_app0.bin") if _is_not_variant(p)]
                    if hits:
                        boot_app0_bin = sorted(hits)[0]

            missing = []
            if not bootloader_bin:
                missing.append("bootloader (bootloader.bin / bootloader_dio_80m.bin)")
            if not partitions_bin:
                missing.append("partitions (partitions.bin / default.bin)")
            if not boot_app0_bin:
                missing.append("boot_app0.bin")

            if missing:
                self._append("  \u2139 Hard Reset does not compile the project \u2014 run Compile or Upload first, then retry Hard Reset.", "warning")
                self._append(f"  \u2716 Could not locate required bootloader files:", "error")
                for m in missing:
                    self._append(f"      \u2022 {m}", "error")
                self._append(f"  Searched inside build dir: {build_dir}", "dim")
                self._append(f"  Searched inside: {framework_dir}", "dim")
                if libs_package_dir:
                    self._append(f"               + {libs_package_dir}", "dim")
                else:
                    self._append("  (no framework-arduino-esp32-libs-* package found — may be needed for arduino-esp32 3.x)", "dim")
                self._append("", "dim")
                self._append("  \U0001f4c2 All .bin files found in framework (for diagnosis):", "warning")
                try:
                    all_bins = sorted(framework_dir.rglob("*.bin"))
                    if libs_package_dir:
                        all_bins = sorted(all_bins + list(libs_package_dir.rglob("*.bin")))
                    if all_bins:
                        for b in all_bins[:40]:
                            try:
                                rel = b.relative_to(framework_dir)
                                self._append(f"      {rel}", "dim")
                            except ValueError:
                                # file is in libs_package_dir, not framework_dir
                                if libs_package_dir:
                                    try:
                                        rel = b.relative_to(libs_package_dir)
                                        self._append(f"      [libs] {rel}", "dim")
                                    except ValueError:
                                        self._append(f"      {b}", "dim")
                        if len(all_bins) > 40:
                            self._append(f"      ... and {len(all_bins) - 40} more", "dim")
                    else:
                        self._append("      (no .bin files found at all)", "dim")
                except Exception as diag_e:
                    self._append(f"      (diagnostic scan failed: {diag_e})", "dim")
                _hr_active[0] = False
                _hr_spin_thread.join(timeout=1)
                self.is_busy = False
                self._set_buttons_busy(False)
                if was_monitoring:
                    self._resume_monitor()
                return

            self._append(f"  Bootloader : {bootloader_bin.name}", "dim")
            self._append(f"  Partitions : {partitions_bin.name}", "dim")
            self._append(f"  boot_app0  : {boot_app0_bin.name}", "dim")
            self._append("")

            # ── Step 1: erase flash ─────────────────────────────────────────────
            self._append("  Step 1/2 — Erasing flash...", "dim")
            _hr_state[0] = "Erasing flash"

            erase_cmd = esptool_cmd_base + [
                "--port", port,
                "--before", "default-reset",
                "--after", "no-reset",
                "--connect-attempts", "3",
                "erase-flash",
            ]
            self._append(f"  $ {' '.join(str(x) for x in erase_cmd)}", "dim")

            _HR_WATCHDOG_SECS = 120  # abort if a single esptool step takes longer
            try:
                self.process = subprocess.Popen(
                    erase_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                _erase_start = time.time()
                for line in iter(self.process.stdout.readline, ""):
                    # Watchdog: kill subprocess if it's been running too long
                    if time.time() - _erase_start > _HR_WATCHDOG_SECS:
                        self._append(f"  ⚠ Erase timed out after {_HR_WATCHDOG_SECS}s — aborting.", "warning")
                        self._do_stop()
                        break
                    stripped = line.rstrip()
                    if stripped:
                        low = stripped.lower()
                        if "error" in low or "failed" in low:
                            self._append(f"  {stripped}", "error")
                        elif any(kw in low for kw in ["erase", "done", "chip", "connecting", "stub", "running"]):
                            self._append(f"  {stripped}", "success")
                        else:
                            self._append(f"  {stripped}", "dim")
                self.process.wait(timeout=10)
                erase_ok = self.process.returncode == 0
            except subprocess.TimeoutExpired:
                self._append("  ⚠ Erase process did not exit cleanly — force killing.", "warning")
                self._do_stop()
                erase_ok = False
            except Exception as e:
                self._append(f"  ⚠ Erase failed: {e}", "warning")
                erase_ok = False

            if erase_ok:
                self._append("  ✔ Flash erased.", "success")
            else:
                self._append("  ⚠ Erase returned non-zero — attempting write anyway.", "warning")

            self._append("")

            # Determine bootloader offset address dynamically based on selected board name
            board_name = self.board_var.get().upper()
            if any(x in board_name for x in ["S3", "C3", "C6", "H2"]):
                bootloader_addr = "0x0"
            else:
                bootloader_addr = "0x1000"

            self._append(f"  Target Board: {self.board_var.get()} -> Bootloader Addr: {bootloader_addr}", "dim")

            # ── Step 2: write bootloader + partitions + boot_app0 ──────────────
            self._append("  Step 2/2 — Writing bootloader files...", "dim")
            _hr_state[0] = "Writing bootloader"

            write_cmd = esptool_cmd_base + [
                "--port", port,
                "--baud", "460800",
                "--before", "default-reset",
                "--after", "hard-reset",
                "--connect-attempts", "3",
                "write-flash",
                "--flash-mode", "keep",
                "--flash-freq", "keep",
                "--flash-size", "detect",
                bootloader_addr,  str(bootloader_bin),
                "0x8000",  str(partitions_bin),
                "0xe000",  str(boot_app0_bin),
            ]
            self._append(f"  $ {' '.join(str(x) for x in write_cmd)}", "dim")

            try:
                self.process = subprocess.Popen(
                    write_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception as e:
                self._append(f"  ✖ Failed to launch esptool write: {e}", "error")
                self.is_busy = False
                self._set_buttons_busy(False)
                if was_monitoring:
                    self._resume_monitor()
                return

            _write_start = time.time()
            for line in iter(self.process.stdout.readline, ""):
                # Watchdog: kill subprocess if it's been running too long
                if time.time() - _write_start > _HR_WATCHDOG_SECS:
                    self._append(f"  ⚠ Write timed out after {_HR_WATCHDOG_SECS}s — aborting.", "warning")
                    self._do_stop()
                    break
                stripped = line.rstrip()
                if stripped:
                    low = stripped.lower()
                    if "error" in low or "failed" in low:
                        self._append(f"  {stripped}", "error")
                    elif any(kw in low for kw in ["writing", "wrote", "done", "verified",
                                                   "hash", "leaving", "hard reset", "compressed"]):
                        self._append(f"  {stripped}", "success")
                    else:
                        self._append(f"  {stripped}", "dim")

            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._append("  ⚠ Write process did not exit cleanly — force killing.", "warning")
                self._do_stop()
            ok = self.process.returncode == 0

        # Stop spinner
        _hr_active[0] = False
        _hr_spin_thread.join(timeout=1)

        # ── Result ──────────────────────────────────────────────────────────────
        if ok:
            self._append("")
            self._append("  ✔ Bootloader burn successful! (Hard Reset OK)", "success")
            self._set_status("Hard Reset Successful", Theme.GREEN)
        else:
            self._append("")
            self._append("  ✖ Bootloader burn FAILED.", "error")
            self._set_status("Hard Reset FAILED", Theme.RED)

        self.is_busy = False
        self._set_buttons_busy(False)

        if ok and not was_monitoring:
            self._trigger_actual_board_reset(port)

        if was_monitoring:
            self._resume_monitor()

    def _locate_esp32_boot_app0(self) -> Path | None:
        """
        Find boot_app0.bin inside PlatformIO's installed Arduino-ESP32
        framework package. Cheap directory lookup (no subprocess) used by the
        Soft Reset esptool fast path.
        """
        pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR")
        if pio_core_dir:
            pio_packages = Path(pio_core_dir) / "packages"
        else:
            local_pio = SCRIPT_DIR / "env" / ".platformio"
            pio_packages = (local_pio / "packages") if local_pio.exists() else Path.home() / ".platformio" / "packages"

        framework_dir = None
        try:
            for candidate in sorted(pio_packages.glob("framework-arduinoespressif32*"), reverse=True):
                if candidate.is_dir():
                    framework_dir = candidate
                    break
        except Exception:
            return None
        if framework_dir is None:
            return None

        tools_dir = framework_dir / "tools"
        for candidate in (tools_dir / "partitions" / "boot_app0.bin", framework_dir / "partitions" / "boot_app0.bin"):
            if candidate.exists():
                return candidate

        hits = [p for p in framework_dir.rglob("boot_app0.bin") if "variants" not in p.parts]
        return sorted(hits)[0] if hits else None

    def _locate_soft_reset_fast_binaries(self, project_dir: Path, board_name: str, p_platform: str) -> dict | None:
        """
        Check whether a previously compiled Soft Reset build is still usable
        for a direct esptool flash, skipping "pio run" entirely. Returns None
        if any required binary can't be found — the caller then falls back to
        the normal "pio run -t upload" path.
        """
        build_dir = project_dir / ".pio" / "build" / "mcu_flash"
        if not build_dir.is_dir():
            return None

        firmware_bin = build_dir / "firmware.bin"
        if not firmware_bin.exists():
            return None

        if p_platform == "espressif8266":
            # ESP8266's Arduino core produces a single merged image flashed at 0x0.
            return {
                "platform": p_platform,
                "firmware": firmware_bin,
                "bootloader": None,
                "partitions": None,
                "boot_app0": None,
                "bootloader_addr": "0x0",
            }

        if p_platform != "espressif32":
            return None

        bootloader_bin = build_dir / "bootloader.bin"
        partitions_bin = build_dir / "partitions.bin"
        if not (bootloader_bin.exists() and partitions_bin.exists()):
            return None

        boot_app0_bin = self._locate_esp32_boot_app0()
        if boot_app0_bin is None:
            return None

        upper = board_name.upper()
        bootloader_addr = "0x0" if any(x in upper for x in ("S3", "C3", "C6", "H2")) else "0x1000"

        return {
            "platform": p_platform,
            "firmware": firmware_bin,
            "bootloader": bootloader_bin,
            "partitions": partitions_bin,
            "boot_app0": boot_app0_bin,
            "bootloader_addr": bootloader_addr,
        }

    def _soft_reset_esptool_write(self, fast_bins: dict, port: str) -> tuple[bool, str]:
        """
        Write the cached Soft Reset binaries straight to flash with esptool.
        This performs the exact same upload "pio run -t upload" would have
        done, just invoked directly so PlatformIO's project-scan overhead is
        skipped entirely. Returns (ok, err_msg).
        """
        import shutil as _shutil_sr
        esptool_exe = _shutil_sr.which("esptool.py") or _shutil_sr.which("esptool")
        esptool_cmd_base = [esptool_exe] if esptool_exe else [sys.executable, "-m", "esptool"]

        write_cmd = esptool_cmd_base + [
            "--port", port,
            "--baud", "460800",
            "--before", "default-reset",
            "--after", "hard-reset",
            "--connect-attempts", "3",
            "write-flash",
            "--flash-mode", "keep",
            "--flash-freq", "keep",
            "--flash-size", "detect",
        ]

        if fast_bins["platform"] == "espressif32":
            write_cmd += [
                fast_bins["bootloader_addr"], str(fast_bins["bootloader"]),
                "0x8000", str(fast_bins["partitions"]),
                "0xe000", str(fast_bins["boot_app0"]),
                "0x10000", str(fast_bins["firmware"]),
            ]
        else:
            # espressif8266 — single merged image at 0x0
            write_cmd += ["0x0", str(fast_bins["firmware"])]

        self._append(f"  $ {' '.join(str(x) for x in write_cmd)}", "dim")

        _WATCHDOG_SECS = 90
        try:
            self.process = subprocess.Popen(
                write_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            _start = time.time()
            for line in iter(self.process.stdout.readline, ""):
                if time.time() - _start > _WATCHDOG_SECS:
                    self._append(f"  ⚠ Upload timed out after {_WATCHDOG_SECS}s — aborting.", "warning")
                    self._do_stop()
                    break
                stripped = line.rstrip()
                if stripped:
                    low = stripped.lower()
                    if "error" in low or "failed" in low:
                        self._append(f"  {stripped}", "error")
                    elif any(kw in low for kw in ["writing", "wrote", "done", "verified",
                                                   "hash", "leaving", "hard reset", "compressed",
                                                   "connecting", "chip is"]):
                        self._append(f"  {stripped}", "success")
                    else:
                        self._append(f"  {stripped}", "dim")
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._append("  ⚠ Process did not exit cleanly — force killing.", "warning")
                self._do_stop()
                return False, "Process did not exit cleanly"
            ok = self.process.returncode == 0
            return ok, ("" if ok else "esptool exited with a non-zero status")
        except Exception as e:
            return False, str(e)

    def _do_soft_reset(self, parent_dlg):
        # 0. Block if the board is already running the soft-reset sketch
        if getattr(self, '_soft_reset_sketch_active', False):
            from tkinter import messagebox
            messagebox.showwarning(
                "Already Reset",
                "The board is already running the soft-reset sketch.\n\n"
                "There is nothing to reset — upload your very own project sketch to continue.",
                parent=parent_dlg
            )
            return

        # 1. Check if busy (with auto-recovery for stale busy flag)
        if self.is_busy:
            if not self.process or self.process.poll() is not None:
                self.is_busy = False
                self._set_buttons_state(False)
                self._append("  ℹ Stale busy state cleared — proceeding.", "info")
            else:
                from tkinter import messagebox
                messagebox.showwarning("Busy", "The programmer is currently busy with another operation.", parent=parent_dlg)
                return

        if not self.board_var.get():
            from tkinter import messagebox
            messagebox.showwarning(
                "No Board Selected",
                "Please choose a board in the main window before performing a Soft Reset.",
                parent=parent_dlg
            )
            return

        port = self._get_port()
        if not port:
            from tkinter import messagebox
            messagebox.showwarning("No Port Selected", "Please select a serial port in the main window before resetting.", parent=parent_dlg)
            return

        if not self._is_board_recognized():
            from tkinter import messagebox
            messagebox.showwarning(
                "Board Not Recognized",
                "The board on this port hasn't been recognized yet.\n\n"
                "Wait for auto-detect to finish, or verify the correct board/port is selected.",
                parent=parent_dlg
            )
            return

        # Close settings dialog to let the process start
        parent_dlg.destroy()

        # Run soft reset in background thread
        self._active_reset_kind = "soft"
        self.is_busy = True
        self._set_buttons_state(True, operation="reset")
        threading.Thread(target=self._run_soft_reset, args=(port,), daemon=True).start()

    def _run_soft_reset(self, port: str):
        try:
            self._run_soft_reset_inner(port)
        except Exception as e:
            import traceback
            try:
                with open("error_log.txt", "w", encoding="utf-8") as f:
                    traceback.print_exc(file=f)
            except Exception:
                pass
            self._append(f"  ✖ Internal error in soft reset thread: {e}", "error")
            self._set_status("Soft Reset FAILED", Theme.RED)
        finally:
            # Guarantee busy state is always cleared
            self.is_busy = False
            self._set_buttons_state(False)
            self._set_window_closable(True)

    def _run_soft_reset_inner(self, port: str):
        # Pause monitor (so port isn't blocked)
        was_monitoring = self._pause_monitor()

        self._append("")
        self._append("=" * 50, "header")
        self._append("  🔄 SOFT RESET (Clearing Flash Memory)", "header")
        self._append("=" * 50, "header")
        self._append(f"  Port  : {port}", "port_highlight")
        self._append(f"  Board : {self.board_var.get()}", "dim")
        if self._detect_port_chip() is None:
            self._append("  ⚠ Unrecognized USB-serial port — proceeding anyway.", "warning")
        self._append("")
        self._set_status("Soft Reset: Initializing...", Theme.YELLOW)

        pio_path = find_pio_executable()
        if not pio_path:
            self._append("  ✖ PlatformIO executable not found!", "error")
            self._append("  Try manually: pip install platformio", "info")
            self.is_busy = False
            self._set_buttons_busy(False)
            self._set_status("Soft Reset: Failed", Theme.RED)
            if was_monitoring:
                self._resume_monitor()
            from tkinter import messagebox
            messagebox.showerror("Soft Reset Error", "PlatformIO not found! Could not perform soft reset.", parent=self.root)
            return

        # ── Use a persistent project directory inside the app folder ───────────
        # This preserves the PlatformIO build cache (.pio/build/) between runs
        # so subsequent soft resets skip compilation (upload-only ≈ 5s vs 30-60s).
        import shutil

        self._set_status("Soft Reset: Preparing project files...", Theme.YELLOW)
        project_dir = SCRIPT_DIR / "soft_reset_project"
        try:
            project_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._append(f"  ✖ Failed to create soft reset project directory:\n  {e}", "error")
            self.is_busy = False
            self._set_buttons_busy(False)
            self._set_status("Soft Reset: Failed", Theme.RED)
            if was_monitoring:
                self._resume_monitor()
            from tkinter import messagebox
            messagebox.showerror("Soft Reset Error", f"Failed to create project directory:\n{e}", parent=self.root)
            return

        # Write platformio.ini and main.cpp
        board_name = self.board_var.get()
        # See _ensure_platformio_ini for why this can no longer fall back
        # to a literal SUPPORTED_BOARDS["ESP32 Dev Module"] key.
        if board_name in SUPPORTED_BOARDS:
            board_info = SUPPORTED_BOARDS[board_name]
        elif SUPPORTED_BOARDS:
            board_info = next(iter(SUPPORTED_BOARDS.values()))
        else:
            board_info = {"platform": "atmelavr", "board": "uno", "framework": "arduino"}
        p_platform = board_info["platform"]
        p_board = board_info["board"]
        p_framework = board_info["framework"]

        # ESP32/ESP8266 can handle much faster upload speeds than AVR
        is_avr = p_platform == "atmelavr"
        upload_speed = "115200" if is_avr else "921600"

        ini_content = f"""; PlatformIO Project Configuration File for Soft Reset
[platformio]
src_dir = .
default_envs = mcu_flash

[env:mcu_flash]
platform = {p_platform}
board = {p_board}
framework = {p_framework}
monitor_speed = 115200
upload_speed = {upload_speed}
"""

        cpp_content = """#include <Arduino.h>
void setup() {
  Serial.begin(115200);
  Serial.println(">>>   ──   <<<");
}

void loop() {
  
}
"""

        # ── Board-aware caching: only rewrite files if content changed ─────────
        # When the board changes, the ini_content changes → we detect that,
        # clear the build cache, and rewrite.  Otherwise we skip writing
        # entirely and PlatformIO will see "nothing changed" → upload only.
        ini_path = project_dir / "platformio.ini"
        cpp_path = project_dir / "main.cpp"
        build_dir = project_dir / ".pio" / "build"

        files_changed = False
        try:
            existing_ini = ini_path.read_text(encoding="utf-8") if ini_path.exists() else ""
            existing_cpp = cpp_path.read_text(encoding="utf-8") if cpp_path.exists() else ""

            if existing_ini != ini_content or existing_cpp != cpp_content:
                files_changed = True
                env_build_dir = project_dir / ".pio" / "build" / "mcu_flash"
                is_first_time = not env_build_dir.exists()
                # Board or content changed — clear build cache to force recompile
                if build_dir.exists():
                    self._append("  ↻ Board changed — clearing cached build...", "dim")
                    robust_rmtree(build_dir)
                ini_path.write_text(ini_content, encoding="utf-8")
                cpp_path.write_text(cpp_content, encoding="utf-8")
                if is_first_time:
                    self._append("  🔧 First-time setup for this board — this may take a minute. Subsequent Soft Resets will be instant.", "warning")
                else:
                    self._append("  ✔ Project files updated (will compile).", "dim")
            else:
                self._append("  ✔ Using cached build (no recompilation needed).", "success")
        except Exception as e:
            self._append(f"  ✖ Failed to write project files:\n  {e}", "error")
            self.is_busy = False
            self._set_buttons_busy(False)
            self._set_status("Soft Reset: Failed", Theme.RED)
            if was_monitoring:
                self._resume_monitor()
            from tkinter import messagebox
            messagebox.showerror("Soft Reset Error", f"Failed to write project files:\n{e}", parent=self.root)
            return

        # Run parallel build + upload
        jobs = self._get_cpu_cores_jobs()

        # ── FAST PATH: nothing changed + ESP32/ESP8266 board ────────────────────
        # "pio run -t upload" always pays PlatformIO's SCons project-scan cost
        # (dependency graph, board/package checks, timestamp hashing) even when
        # there's nothing to compile — for an "almost empty sketch" that scan is
        # the whole delay, not the upload itself. When the cached build is still
        # valid we skip PlatformIO entirely and hand the already-compiled
        # binaries straight to esptool, the same way Hard Reset already does for
        # the bootloader burn.
        is_esp = p_platform in ("espressif32", "espressif8266")
        fast_bins = None
        if not files_changed and is_esp:
            fast_bins = self._locate_soft_reset_fast_binaries(project_dir, board_name, p_platform)

        self._append("  🔨 Resetting Flash Memory...", "info")

        img_count = 0
        output_lines = []
        ok = False
        err_msg = ""

        # ── Spinner thread for Soft Reset ──────────────────────────────────────
        _sr_state   = ["Uploading" if not files_changed else "Compiling"]
        _sr_active  = [True]
        _sr_frame   = [0]
        _sr_start   = time.time()
        _sr_spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

        def _sr_spin_loop():
            while _sr_active[0] and self.is_busy:
                elapsed = int(time.time() - _sr_start)
                frame   = _sr_spinner[_sr_frame[0] % len(_sr_spinner)]
                _sr_frame[0] += 1
                self._set_status(
                    f"{frame} Soft Reset: {_sr_state[0]}... ({elapsed}s)",
                    Theme.YELLOW,
                )
                time.sleep(0.08)

        import threading as _threading_sr
        _sr_spin_thread = _threading_sr.Thread(target=_sr_spin_loop, daemon=True)
        _sr_spin_thread.start()

        if fast_bins is not None:
            # ── Fast path: write cached binaries directly with esptool ─────────
            self._append("  ⚡ Cached build found — flashing directly with esptool (skipping PlatformIO).", "success")
            ok, err_msg = self._soft_reset_esptool_write(fast_bins, port)
        else:
            cmd = pio_path + [
                "run",
                "-t", "upload",
                "-j", str(jobs),
                "--upload-port", port
            ]
            try:
                self.process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    cwd=str(project_dir),
                )
                for line in iter(self.process.stdout.readline, ""):
                    stripped = line.rstrip()
                    if not stripped:
                        continue
                    output_lines.append(stripped)
                    low = stripped.lower()

                    # Suppress percentage upload lines — spinner shows progress
                    if re.search(r'\d+\s*%', stripped):
                        _sr_state[0] = "Uploading"
                        continue

                    LINKER_ERROR_HINTS = (
                        "undefined reference to",
                        "multiple definition of",
                        "cannot find -l",
                        "undefined symbol",
                        "duplicate symbol",
                        "ld returned",
                        "collect2",
                    )
                    is_linker_error = any(hint in low for hint in LINKER_ERROR_HINTS)

                    if is_nonfatal_pio_clean_report(stripped):
                        self._append(f"  ⚠ {stripped}", "warning")
                    elif is_linker_error:
                        self._append(f"  ✖ {stripped}", "error")
                    elif "error" in low and "werror" not in low:
                        self._append(f"  ✖ {stripped}", "error")
                    elif "warning" in low:
                        self._append(f"  ⚠ {stripped}", "warning")
                    elif "connecting" in low:
                        _sr_state[0] = "Connecting"
                        self._append("  🔌 Connecting to board...", "info")
                    elif "successfully created" in low and "image" in low:
                        chip_match = re.search(r'successfully created (\w+) image', low)
                        chip_name = chip_match.group(1).upper() if chip_match else "MCU"
                        img_count += 1
                        label = "Bootloader" if img_count == 1 else "Application"
                        self._append(f"  ✔ Successfully created {chip_name} image ({label})", "success")
                    elif any(kw in low for kw in ["hard resetting", "leaving", "wrote", "success"]):
                        _sr_state[0] = "Done"
                        self._append(f"  {stripped}", "success")

                self.process.wait()
                ok = self.process.returncode == 0
            except Exception as e:
                ok = False
                err_msg = str(e)
                self._append(f"  ✖ Execution error: {err_msg}", "error")

        # Stop spinner
        _sr_active[0] = False
        _sr_spin_thread.join(timeout=1)

        # NOTE: Do NOT delete project_dir — preserving it is the whole point.
        # The .pio/build/ cache inside it makes subsequent soft resets instant.

        self.is_busy = False
        self._set_buttons_busy(False)

        if ok and not was_monitoring:
            self._trigger_actual_board_reset(port)

        if was_monitoring:
            self._resume_monitor()

        from tkinter import messagebox
        if ok:
            self._append("")
            self._append("  ✔ Soft Reset completed successfully!", "success")
            self._set_status("Soft Reset: SUCCESS", Theme.GREEN)
            messagebox.showinfo("Soft Reset Success", "Soft Reset (flash memory reset) completed successfully!", parent=self.root)
        else:
            self._append("")
            self._append("  ✖ Soft Reset FAILED.", "error")
            self._set_status("Soft Reset: FAILED", Theme.RED)
            messagebox.showerror("Soft Reset Failed", f"Soft Reset failed.\n{err_msg}\nCheck if the device is connected to {port}.", parent=self.root)

    # ──────────────────────────────────────────────────────────
    # CLEANUP
    # ──────────────────────────────────────────────────────────
    def _on_close(self):
        if getattr(self, "_framework_download_active", False):
            from tkinter import messagebox
            messagebox.showwarning(
                "Framework Download in Progress",
                "A critical framework/tool download is currently in progress. "
                "Closing the application now may corrupt your PlatformIO core installation.\n\n"
                "Please wait for the download to finish.",
                parent=self.root
            )
            return

        if self.is_busy:
            # Auto-recover: if no subprocess is actually running, clear stale busy flag
            if not self.process or self.process.poll() is not None:
                self.is_busy = False
                self._set_buttons_state(False)
                self._set_window_closable(True)
                # Fall through to normal close logic
            else:
                from tkinter import messagebox
                kind = getattr(self, "_active_reset_kind", None)
                if kind == "hard":
                    msg = ("A Hard Reset (bootloader burn) is in progress.\n\n"
                           "Interrupting this can permanently brick the board. "
                           "Please wait for it to finish.")
                elif kind == "soft":
                    msg = ("A Soft Reset (flash rewrite) is in progress.\n\n"
                           "Interrupting this can leave the board in a broken state. "
                           "Please wait for it to finish.")
                else:
                    msg = ("An upload, reset, or compilation operation is currently in progress. "
                           "Please wait for it to complete or stop it before closing the application.")
                messagebox.showwarning("Operation in Progress", msg, parent=self.root)
                return

        # Check if there are unsaved changes in the active editor
        if getattr(self, "editor_mode", "default") == "monaco":
            if hasattr(self, "editor_api") and self.editor_api.modified_files:
                unsaved = [Path(p).name for p, is_modified in self.editor_api.modified_files.items() if is_modified]
                if unsaved:
                    import tkinter.messagebox as mb
                    names = "\n  • ".join(unsaved)
                    answer = mb.askyesnocancel(
                        "Unsaved Changes",
                        f"The following files have unsaved changes:\n\n  • {names}\n\n"
                        "Save before closing?",
                        parent=self.root,
                    )
                    if answer is None:       # Cancel closing
                        return
                    if answer:               # Yes — save all
                        if hasattr(self, "_save_all_editor_files"):
                            self._save_all_editor_files()
        else:
            tab_data = getattr(self, "editor_tab_data", None)
            if tab_data:
                unsaved = [d["path"].name for d in tab_data.values() if d.get("modified")]
                if unsaved:
                    import tkinter.messagebox as mb
                    names = "\n  • ".join(unsaved)
                    answer = mb.askyesnocancel(
                        "Unsaved Changes",
                        f"The following files have unsaved changes:\n\n  • {names}\n\n"
                        "Save before closing?",
                        parent=self.root,
                    )
                    if answer is None:       # Cancel closing
                        return
                    if answer:               # Yes — save all
                        if hasattr(self, "_save_all_editor_files"):
                            self._save_all_editor_files()

        # Terminate embedded QScintilla subprocess on exit
        if hasattr(self, "_qsci_proc") and self._qsci_proc:
            # Flush unsaved files before killing the process
            if hasattr(self, "_save_all_editor_files") and callable(self._save_all_editor_files):
                try:
                    self._save_all_editor_files()
                    import time as _t; _t.sleep(0.15)  # brief flush window
                except Exception:
                    pass
            try:
                if sys.platform == "win32":
                    import subprocess
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self._qsci_proc.pid)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                else:
                    self._qsci_proc.terminate()
                    self._qsci_proc.wait(timeout=1.5)
            except Exception:
                try:
                    self._qsci_proc.kill()
                except Exception:
                    pass
            self._qsci_proc = None

        # Kill any active compile/upload/reset subprocess synchronously on exit
        if self.process and self.process.poll() is None:
            try:
                if sys.platform == "win32":
                    import subprocess
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                else:
                    self.process.kill()
            except Exception:
                pass

        # Kill any launched library manager subprocesses
        for p in getattr(self, "_download_managers", []):
            if p.poll() is None:
                try:
                    if sys.platform == "win32":
                        import subprocess
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW
                        )
                    else:
                        p.kill()
                except Exception:
                    pass

        self._do_stop()
        if hasattr(self, "_bg_executor") and self._bg_executor:
            try:
                self._bg_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        # Stop serial monitor on close
        self._monitor_should_run = False
        self.serial_running = False
        if self.serial_conn and self.serial_conn.is_open:
            try:
                self.serial_conn.close()
            except Exception:
                pass
        if self.serial_thread and self.serial_thread.is_alive():
            self.serial_thread.join(timeout=1.0)

        # Clean up this instance configuration from the shared file
        try:
            data = _load_raw_config()
            if "instances" in data and _INSTANCE_ID in data["instances"]:
                del data["instances"][_INSTANCE_ID]
                _save_raw_config(data)
        except Exception:
            pass

        self.root.destroy()
        os._exit(0)

    def _set_window_closable(self, closable: bool):
        """Grey out (or restore) the window's native [X] close button and
        Alt+F4 at the OS level. This sits on top of the existing is_busy
        check in _on_close() as a stronger guarantee — Hard/Soft Reset
        write directly to flash and, for Hard Reset, the bootloader itself;
        an interrupted write there can brick the board or leave it in an
        unrecoverable boot loop, so during that window we don't want the
        close path reachable at all, not just intercepted-and-warned.
        """
        if win32gui is None or win32con is None:
            return  # non-Windows or pywin32 missing — the is_busy dialog in _on_close is still the backstop
        try:
            hwnd = self.root.winfo_id()
            menu = win32gui.GetSystemMenu(hwnd, False)
            flag = win32con.MF_ENABLED if closable else win32con.MF_GRAYED
            win32gui.EnableMenuItem(menu, win32con.SC_CLOSE, win32con.MF_BYCOMMAND | flag)
        except Exception:
            pass

# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════
def main():
    # If not run from bootstrap, launch the VBS launcher to check for updates and setup dependencies
    if "--from-bootstrap" not in sys.argv:
        import subprocess
        import os
        vbs_launcher = SCRIPT_DIR / "runThisOnWindows.vbs"
        if vbs_launcher.exists():
            try:
                # Sanitize PyInstaller environment variables so they don't pollute the VBS launcher
                env = os.environ.copy()
                env.pop("_MEIPASS", None)
                env.pop("_MEIPASS2", None)
                env.pop("PYTHONHOME", None)
                env.pop("PYTHONPATH", None)
                env.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
                meipass = getattr(sys, '_MEIPASS', None)
                if meipass:
                    path_val = env.get("PATH", "")
                    paths = path_val.split(os.pathsep)
                    cleaned_paths = [p for p in paths if p != meipass]
                    env["PATH"] = os.pathsep.join(cleaned_paths)
                
                subprocess.Popen(["wscript.exe", str(vbs_launcher)], cwd=str(SCRIPT_DIR), env=env)
                sys.exit(0)
            except Exception:
                pass

    if not find_arduino_cli_executable():
        import tkinter.messagebox as mb
        import tkinter.filedialog as fd
        
        root = tk.Tk()
        root.withdraw()
        root.lift()
        root.attributes("-topmost", True)
        root.attributes("-topmost", False)

        msi_bundled = (SCRIPT_DIR / "installers" / "arduino-cli.msi").exists()
        can_auto_install = _bootstrap_ensure_arduino_cli is not None and sys.platform == "win32"

        if can_auto_install:
            install_hint = (
                "An arduino-cli.msi installer is bundled with this app.\n\n"
                if msi_bundled else
                "This app can download and install arduino-cli automatically.\n\n"
            )
            ans = mb.askyesnocancel(
                "Arduino-CLI Not Found",
                "Arduino-CLI is not installed on this computer, or its location was not detected.\n\n"
                + install_hint +
                "Yes = Install it automatically now\n"
                "No = Manually locate an existing 'arduino-cli.exe'\n"
                "Cancel = Exit",
                parent=root
            )
            if ans is True:  # Yes: install automatically via bundled/downloaded MSI
                mb.showinfo(
                    "Installing Arduino-CLI",
                    "Installing now — this may take a moment and could prompt for elevation.",
                    parent=root
                )
                installed = False
                try:
                    installed = _bootstrap_ensure_arduino_cli()
                except Exception as e:
                    mb.showerror("Install Failed", f"Automatic install failed:\n{e}", parent=root)
                if installed:
                    cli = find_arduino_cli_executable()
                    if cli:
                        mb.showinfo("Success", f"Arduino-CLI installed:\n{cli}", parent=root)
                        root.destroy()
                    else:
                        reason = ""
                        if _bootstrap_get_last_arduino_cli_error is not None:
                            try:
                                reason = _bootstrap_get_last_arduino_cli_error()
                            except Exception:
                                reason = ""
                        mb.showerror(
                            "Install Finished, Still Not Found",
                            "The installer ran but arduino-cli.exe could not be located afterward.\n"
                            + (f"\n{reason}\n\n" if reason else "\n") +
                            "You can try locating it manually instead.",
                            parent=root
                        )
                        root.destroy()
                        sys.exit(1)
                else:
                    reason = ""
                    if _bootstrap_get_last_arduino_cli_error is not None:
                        try:
                            reason = _bootstrap_get_last_arduino_cli_error()
                        except Exception:
                            reason = ""
                    mb.showerror(
                        "Install Failed",
                        (f"Automatic install did not succeed:\n\n{reason}\n\n"
                         if reason else
                         "Automatic install did not succeed.\n\n") +
                        "You can try locating an existing arduino-cli.exe manually, "
                        "or check your internet connection and retry.",
                        parent=root
                    )
                    root.destroy()
                    sys.exit(1)
            elif ans is False:  # No: manually locate
                selected_path = fd.askopenfilename(
                    title="Select Arduino CLI Executable",
                    filetypes=[("Arduino CLI Executable", "arduino-cli.exe;arduino-cli"), ("All Files", "*.*")],
                    parent=root
                )
                if selected_path:
                    try:
                        script_dir = SCRIPT_DIR
                        cached_file = script_dir / "arduino_cli_path_linux.txt"
                        cached_file.write_text(selected_path, encoding="utf-8")
                        mb.showinfo("Success", f"Arduino CLI path saved successfully:\n{selected_path}", parent=root)
                        root.destroy()
                    except Exception as e:
                        mb.showerror("Error", f"Failed to save path: {e}", parent=root)
                        root.destroy()
                        sys.exit(1)
                else:
                    root.destroy()
                    sys.exit(1)
            else:  # Cancel
                root.destroy()
                sys.exit(1)
        else:
            ans = mb.askyesno(
                "Arduino-CLI Not Found",
                "Arduino-CLI is not installed on this computer, or its location was not detected.\n\n"
                "Would you like to manually locate 'arduino-cli.exe'?",
                parent=root
            )
            if ans is True: # Yes: manually locate
                selected_path = fd.askopenfilename(
                    title="Select Arduino CLI Executable",
                    filetypes=[("Arduino CLI Executable", "arduino-cli.exe;arduino-cli"), ("All Files", "*.*")],
                    parent=root
                )
                if selected_path:
                    try:
                        # Save to arduino_cli_path_linux.txt
                        script_dir = SCRIPT_DIR
                        cached_file = script_dir / "arduino_cli_path_linux.txt"
                        cached_file.write_text(selected_path, encoding="utf-8")
                        mb.showinfo("Success", f"Arduino CLI path saved successfully:\n{selected_path}", parent=root)
                        root.destroy()
                    except Exception as e:
                        mb.showerror("Error", f"Failed to save path: {e}", parent=root)
                        root.destroy()
                        sys.exit(1)
                else:
                    root.destroy()
                    sys.exit(1)
            else:
                root.destroy()
                sys.exit(1)


    import threading

    global _RESOLVED_EDITOR_MODE
    requested_mode = get_editor_mode()
    monaco_crashed_last_time = False
    if requested_mode == "monaco" and get_monaco_boot_pending():
        # The previous launch set the "about to try Monaco" sentinel and
        # never cleared it — meaning the process died before Monaco could
        # confirm it started cleanly. Revert to the safe default.
        requested_mode = "default"
        monaco_crashed_last_time = True
        set_editor_mode("default")
        set_monaco_boot_pending(False)

    _RESOLVED_EDITOR_MODE = requested_mode

    if requested_mode == "monaco":
        # pyrefly: ignore [missing-import]
        import webview

    root_ready = threading.Event()
    root_val = None
    app_val = None

    def run_tk():
        nonlocal root_val, app_val
        root_val = tk.Tk()

        # Set window icon if available
        icon_path = SCRIPT_DIR / "src" / "mcu_icon.ico"
        if icon_path.exists():
            try:
                root_val.iconbitmap(str(icon_path))
            except Exception:
                pass

        # DPI awareness on Windows
        try:
            from ctypes import windll, create_unicode_buffer
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        # Load Montserrat custom fonts (kept independent of DPI awareness above
        # so a failure there can never silently skip font loading too)
        try:
            from ctypes import windll, create_unicode_buffer
            gdi32 = windll.gdi32
            FR_PRIVATE = 0x10
            fonts_dir = SCRIPT_DIR / "src" / "fonts" / "Montserrat" / "static"
            if not fonts_dir.exists():
                fonts_dir = SCRIPT_DIR / "src" / "fonts" / "Montserrat"
            if fonts_dir.exists():
                for ttf_file in fonts_dir.glob("*.ttf"):
                    path_buf = create_unicode_buffer(str(ttf_file))
                    gdi32.AddFontResourceExW(path_buf, FR_PRIVATE, 0)
        except Exception:
            pass

        try:
            screen_w = root_val.winfo_screenwidth()
            screen_h = root_val.winfo_screenheight()
        except Exception:
            screen_w, screen_h = 1920, 1080

        # Start maximized or fallback to a suitable default based on display size
        if screen_w < 1400 or screen_h < 800:
            root_val.geometry("1000x600")
        else:
            root_val.geometry("1350x720")

        app_val = MCUUploadGUI(root_val)

        # If we just auto-reverted from a crashed Monaco session, tell the
        # user once the window is up rather than blocking startup on it.
        if monaco_crashed_last_time:
            def _notify_monaco_reverted():
                from tkinter import messagebox
                messagebox.showwarning(
                    "Editor Reverted to Default",
                    "The Monaco editor did not start cleanly last time "
                    "(the app closed unexpectedly during startup), so the "
                    "File Editor has been reset to Default.\n\n"
                    "You can re-enable Monaco from MCU Flasher Settings.",
                    parent=root_val
                )
            root_val.after(500, _notify_monaco_reverted)

        # Maximize after the widget tree is built so geometry() inside __init__
        # doesn't override it. after(0) runs once the mainloop starts.
        def _maximize():
            if sys.platform == "win32":
                root_val.state("zoomed")
            else:
                root_val.attributes("-zoomed", True)
        root_val.after(0, _maximize)

        # Intercept Tkinter window closure to exit the entire app
        def on_tk_close():
            try:
                pair = getattr(app_val, "_editor_attached_threads", None)
                if pair:
                    import ctypes
                    ctypes.windll.user32.AttachThreadInput(pair[0], pair[1], False)
            except Exception:
                pass
            root_val.destroy()
            import os
            os._exit(0)
        root_val.protocol("WM_DELETE_WINDOW", on_tk_close)

        root_ready.set()
        root_val.mainloop()

    tk_thread = threading.Thread(target=run_tk, daemon=True)
    tk_thread.start()

    # Wait for Tkinter to initialize
    root_ready.wait()

    if requested_mode != "monaco":
        # Default (Tkinter) editor — no separate webview process needed at
        # all. Just block the main thread until the Tk mainloop (running on
        # tk_thread) exits.
        tk_thread.join()
        return

    # ── Monaco mode ─────────────────────────────────────────────────────
    # Mark that we're about to attempt Monaco startup. If the process dies
    # anywhere between here and the confirmation callback below, this flag
    # stays set on disk and the *next* launch will detect it and revert to
    # Default automatically instead of crash-looping.
    set_monaco_boot_pending(True)

    def _confirm_monaco_booted():
        set_monaco_boot_pending(False)
    # A few seconds of uneventful running is our signal that Monaco came up
    # cleanly rather than hanging/crashing during initialization.
    root_val.after(3000, _confirm_monaco_booted)

    # Now run pywebview on the main thread
    api = EditorApi(app_val)
    app_val.editor_api = api

    html_path = SCRIPT_DIR / "src" / "editor" / "index.html"

    # Snapshot this process's top-level windows *before* creating the
    # editor window, so we can later spot "whatever new window appeared"
    # even if its title gets rewritten by the page's <title> tag.
    app_val._editor_pre_create_hwnds = _list_own_toplevel_hwnds()

    editor_window = webview.create_window(
        title=EDITOR_WINDOW_TITLE,
        url=str(html_path),
        js_api=api,
        width=1000,
        height=700,
        min_size=(600, 400),
        hidden=True,
        background_color="#151922",   # matches Theme.BG_DARKEST — no white flash
    )
    app_val.editor_window = editor_window
    
    def _on_editor_page_loaded():
        # Fires on pywebview's own GUI thread — marshal back to the Tk thread.
        root_val.after(0, lambda: (
            setattr(app_val, "_editor_content_loaded", True),
            app_val._reveal_editor_if_ready()
        ))
    editor_window.events.loaded += _on_editor_page_loaded
    
    root_val.after(300, app_val._try_embed_editor_window)

    # Kick off the embed attempt now. It polls until the native window
    # actually exists, since pywebview creates it asynchronously once
    # webview.start() below takes over the GUI loop. On non-Windows
    # platforms, or if pywin32 isn't installed, this just falls back to
    # the "Open Editor Window" popup button automatically.
    root_val.after(300, app_val._try_embed_editor_window)

    def on_closing():
        if getattr(app_val, "_editor_embedded", False):
            # It's embedded in the main window now — there's nowhere
            # separate for it to "close" to, so just ignore the close.
            return False
        editor_window.hide()
        return False  # Intercept close and just hide

    editor_window.events.closing += on_closing
    webview.start(debug=False)
    # If webview.start() returns normally (window closed cleanly), make sure
    # the sentinel is cleared so a later launch doesn't misread this as a crash.
    set_monaco_boot_pending(False)



if __name__ == "__main__":
    # ── Crash guard ──────────────────────────────────────────────────────────
    # When launched via pythonw.exe or CREATE_NO_WINDOW, there is no console
    # for tracebacks to appear in — any unhandled exception just silently
    # kills the process and the user sees nothing.  This guard catches every
    # unhandled exception, writes it to gui_crash.log next to this script,
    # AND shows a tkinter error dialog (or a ctypes MessageBox if Tk itself
    # failed to initialise) so the user is never left staring at a blank screen.
    import traceback as _tb
    import os as _os

    _logs_dir = _os.path.join(str(SCRIPT_DIR), "logs")
    try:
        _os.makedirs(_logs_dir, exist_ok=True)
    except Exception:
        pass
    _crash_log = _os.path.join(_logs_dir, "gui_crash.log")

    try:
        main()
    except Exception:
        _err = _tb.format_exc()
        # Write to log file
        try:
            with open(_crash_log, "w", encoding="utf-8") as _f:
                _f.write(_err)
        except Exception:
            pass
        # Try to show a Tk error dialog
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _r = _tk.Tk()
            _r.withdraw()
            _mb.showerror(
                "MCU Flasher by Naph — Crash",
                f"The GUI crashed before it could start.\n\n"
                f"{_err[:1200]}\n\n"
                f"Full log: {_crash_log}"
            )
            _r.destroy()
        except Exception:
            # Tk itself failed — fall back to a Win32 MessageBox
            try:
                import ctypes as _ct
                _ct.windll.user32.MessageBoxW(
                    0,
                    f"GUI crashed:\n\n{_err[:800]}\n\nLog: {_crash_log}",
                    "MCU Flasher by Naph — Crash",
                    0x10,   # MB_ICONERROR
                )
            except Exception:
                pass
        raise SystemExit(1)