#!/usr/bin/env python3
"""
launcher.py — Entry point launcher for MCU Flasher.
"""
import sys
import os
import ctypes
from pathlib import Path

# Ensure taskbar groups the windows under the custom icon
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            getattr(sys.stdout, "reconfigure")(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            getattr(sys.stderr, "reconfigure")(encoding="utf-8")
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("naph.mcuflasher.gui.v3")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent


# ─────────────────────────────────────────────────────────────
# BOOTSTRAP SINGLE-INSTANCE GATE
# ─────────────────────────────────────────────────────────────
# Flow: runThisOnWindows.vbs -> launcher.py -> bootstrap.py -> mcu_flash_gui.py.
#
# This only guards against two launcher/bootstrap processes running at the
# same time (e.g. double-clicking the .vbs twice in a row while dependency
# checks, venv setup, or toolchain installs are still in progress) -- that's
# the scenario that can actually race on shared state (the "env" venv
# folder, installers/, etc).
#
# It deliberately does NOT check whether the Main GUI itself is already
# running -- mcu_flash_gui.py already owns that check on its own (its
# _claim_gui_instance() mutex + message box), so duplicating it here would
# just be redundant.
#
# Implementation: a PID lock file rather than a named OS mutex. A named
# "Local\" mutex is normally session-wide regardless of UAC elevation, but
# two back-to-back .vbs double-clicks each self-elevate independently, and
# that path isn't worth trusting blindly when a plain file check is just as
# fast, is trivially inspectable (open the file, see the PID), and is
# immune to any kernel-object-namespace edge cases across separately
# elevated processes. The file is claimed with an atomic exclusive create
# (O_CREAT | O_EXCL) so two processes racing to create it can never both
# "win" -- exactly one O_EXCL create can ever succeed for a given filename.
# If a stale lock is found (owner PID no longer alive, e.g. a crashed
# previous run), it's reclaimed automatically.
_LAUNCHER_LOCK_FILE = SCRIPT_DIR / "logs" / "launcher.lock"


def _process_is_alive(pid: int) -> bool:
    """True if a process with this PID currently exists, hasn't exited, AND is a Python/launcher process."""
    if sys.platform != "win32":
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    try:
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            if exit_code.value != STILL_ACTIVE:
                return False

            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                exe_name = Path(buf.value).name.lower()
                return any(k in exe_name for k in ("python", "mcu", "flasher", "launcher"))
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def _try_create_lock_exclusive() -> bool:
    """Atomically create the lock file only if it doesn't already exist.
    Returns True iff THIS call created it (i.e. we own the slot now)."""
    try:
        fd = os.open(str(_LAUNCHER_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        # Don't block a launch merely because the lock file couldn't be
        # created (e.g. read-only filesystem, permissions oddity).
        return True


def _claim_launcher_slot() -> bool:
    """Claim the launcher/bootstrap-phase lock. Returns False only when
    another launcher/bootstrap process is genuinely still alive and
    holding it."""
    try:
        _LAUNCHER_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return True

    if _try_create_lock_exclusive():
        return True

    # Lock file already exists — find out whether its owner is actually
    # still running, or whether this is a stale leftover from a crash/kill.
    try:
        existing_pid = int(_LAUNCHER_LOCK_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        existing_pid = None

    if existing_pid and existing_pid != os.getpid() and _process_is_alive(existing_pid):
        return False  # a real launcher/bootstrap is genuinely in progress

    # Stale lock — reclaim it.
    for _ in range(3):
        try:
            if _LAUNCHER_LOCK_FILE.exists():
                _LAUNCHER_LOCK_FILE.unlink(missing_ok=True)
            break
        except Exception:
            import time
            time.sleep(0.05)
    return _try_create_lock_exclusive()


def _release_launcher_slot():
    """Best-effort cleanup so the lock file doesn't linger after a clean
    exit. Safe to skip on crash — the next launch's liveness check
    reclaims it automatically."""
    try:
        if _LAUNCHER_LOCK_FILE.exists():
            existing_pid = int(_LAUNCHER_LOCK_FILE.read_text(encoding="utf-8").strip())
            if existing_pid == os.getpid():
                for _ in range(3):
                    try:
                        _LAUNCHER_LOCK_FILE.unlink(missing_ok=True)
                        break
                    except Exception:
                        import time
                        time.sleep(0.05)
    except Exception:
        pass


def _notify_already_starting():
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            "MCU Uploader IDE by Naph is already starting up in another window.\n\n"
            "Please wait for it to finish loading before launching it again.",
            "MCU Uploader IDE by Naph",
            0x40,  # MB_ICONINFORMATION
        )
    except Exception:
        pass


if __name__ == "__main__":
    if "--new-window" not in sys.argv:
        if not _claim_launcher_slot():
            _notify_already_starting()
            sys.exit(0)
        import atexit
        atexit.register(_release_launcher_slot)

# ─────────────────────────────────────────────────────────────
# WINDOWS DEFENDER EXCLUSION (background, silent, non-blocking)
# ─────────────────────────────────────────────────────────────
# Adding build toolchain directories to Windows Defender exclusions prevents
# antivirus file-locking during compilation (especially the *.a archiving
# phase), which can cause the build to hang for 1-2 minutes or fail entirely.
#
# To add more directories in the future, simply append to EXCLUDED_PATHS below.
# Paths support environment variables and Path objects — both are resolved at
# runtime before being passed to PowerShell.
#
# This runs in a daemon thread so it never delays the GUI launch.
# Silently skips on non-Windows, non-admin, or PowerShell unavailable.

def _apply_defender_exclusions():
    """Silently ensure all build-critical directories are in Windows Defender exclusions.
    This is deliberately opt-in: silently weakening antivirus coverage is not
    an acceptable default and also forces avoidable UAC failures on other PCs.
    Set MCU_FLASH_GUI_CONFIGURE_DEFENDER=1 only on a machine whose owner has
    explicitly chosen this trade-off."""
    if (sys.platform != "win32" or
            os.environ.get("MCU_FLASH_GUI_CONFIGURE_DEFENDER", "").strip() != "1"):
        return
    import subprocess

    local_appdata = os.environ.get("LOCALAPPDATA", "") or str(Path.home() / "AppData" / "Local")

    # ── Directories to exclude ─────────────────────────────────────────────
    # Add new directories here as the project evolves.
    EXCLUDED_PATHS = [
        # PlatformIO LocalAppData paths (non-admin safe, non-OneDrive)
        Path(local_appdata) / ".platformio-mcu-gui",
        Path(local_appdata) / ".pio-mcu",
        # PlatformIO junction path (avoids MAX_PATH issues on Windows)
        Path("C:\\") / ".platformio-mcu-gui",
        # PlatformIO actual storage inside the app (src/_board-frameworks/.platformio)
        SCRIPT_DIR / "src" / "_board-frameworks" / ".platformio",
        # Standard user-level PlatformIO directory (~/.platformio)
        Path.home() / ".platformio",
        # The MCU Flasher app directory itself (build output, logs, etc.)
        SCRIPT_DIR,
    ]
    # ── End of exclusions list ─────────────────────────────────────────────

    # Resolve and deduplicate paths; skip ones that don't exist yet
    seen = set()
    paths_to_add = []
    for p in EXCLUDED_PATHS:
        try:
            resolved = str(Path(p).resolve())
            if resolved not in seen:
                seen.add(resolved)
                paths_to_add.append(resolved)
        except Exception:
            pass

    if not paths_to_add:
        return

    # Check which paths are already excluded (avoids redundant writes)
    try:
        result = subprocess.run(
            ["powershell", "-NonInteractive", "-NoProfile", "-Command",
             "(Get-MpPreference).ExclusionPath -join '|'"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        existing_raw = result.stdout.strip().lower()
        already_excluded = set(existing_raw.split("|")) if existing_raw else set()
    except Exception:
        already_excluded = set()

    new_paths = [p for p in paths_to_add if p.lower() not in already_excluded]
    if not new_paths:
        return  # All paths already excluded — nothing to do

    # Build a single Add-MpPreference call for all new paths at once
    ps_list = ", ".join(f'"{p}"' for p in new_paths)
    ps_cmd = f"Add-MpPreference -ExclusionPath {ps_list}"
    try:
        subprocess.run(
            ["powershell", "-NonInteractive", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass  # Non-fatal: Defender exclusion is a best-effort optimisation


if (sys.platform == "win32" and
        os.environ.get("MCU_FLASH_GUI_CONFIGURE_DEFENDER", "").strip() == "1"):
    import threading as _threading
    _threading.Thread(target=_apply_defender_exclusions, daemon=True, name="DefenderExclusions").start()

# Add src/libs to sys.path
libs_path = SCRIPT_DIR / "src" / "libs"
if str(libs_path) not in sys.path:
    sys.path.insert(0, str(libs_path))

# Run bootstrap with safety crash dialog
try:
    # pyrefly: ignore [missing-import]
    import bootstrap
    if __name__ == "__main__":
        bootstrap.main()
except Exception as e:
    import traceback
    crash_log = SCRIPT_DIR / "logs" / "launcher_crash.log"
    try:
        crash_log.parent.mkdir(parents=True, exist_ok=True)
        crash_log.write_text(traceback.format_exc(), encoding="utf-8")
    except Exception:
        pass
    msg = f"MCU Uploader IDE setup error:\n\n{e}\n\nLog: {crash_log}"
    if isinstance(e, ModuleNotFoundError) and getattr(e, "name", "") == "tkinter":
        msg = (
            "MCU Uploader IDE requires Python with Tkinter support.\n\n"
            "The current Python installation is missing Tkinter ('import tkinter' failed).\n"
            "Please launch using 'runThisOnWindows.vbs' to automatically install or select Python with Tkinter.\n\n"
            f"Log: {crash_log}"
        )
    try:
        ctypes.windll.user32.MessageBoxW(
            0,
            msg,
            "MCU Uploader IDE by Naph — Startup Error",
            0x10,
        )
    except Exception:
        pass
    sys.exit(1)
