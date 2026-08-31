#!/usr/bin/env python3
"""
bootstrap.py — MCU Upload GUI Dependency Bootstrap
==================================================
Ensures pip, pyserial, psutil, pywin32 (Windows), pywebview, pywinpty,
websockets, esptool, certifi, PlatformIO Core, and the WebView2 runtime are
installed and verified before launching the main GUI. Online version checks
are kept out of this short-lived launcher process so a slow network cannot
leave child interpreters behind.

Called by MCU-Flash-GUI.vbs. On a fresh system this runs
once with a visible console window showing progress.
Every launch runs the Bootstrap verification before the main GUI opens.
"""

import json
import hashlib
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import urllib.request
import importlib.util
from pathlib import Path
from typing import Optional

def _configure_windows_dpi_awareness() -> None:
    """Make Tk size and position controls in physical pixels on mixed-DPI displays."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)  # Per-monitor v2
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_configure_windows_dpi_awareness()

# Configure stdout/stderr to use UTF-8 encoding on Windows to prevent UnicodeEncodeError
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            getattr(sys.stdout, "reconfigure")(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            getattr(sys.stderr, "reconfigure")(encoding="utf-8")
    except Exception:
        pass

if getattr(sys, 'frozen', False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent

def purge_python_cache(root_dir: Path | str = SCRIPT_DIR) -> None:
    """Purge all __pycache__ directories and *.pyc/*.pyo files recursively."""
    try:
        root_path = Path(root_dir).resolve()
        skip_dirs = {
            ".git", ".pio", ".mcu_flasher_build_cache",
            "node_modules", ".vscode",
        }
        for current_root, dirs, files in os.walk(root_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            if Path(current_root).name == "__pycache__":
                try:
                    shutil.rmtree(current_root, ignore_errors=True)
                except Exception:
                    pass
                continue
            for f in files:
                if f.lower().endswith((".pyc", ".pyo")):
                    try:
                        (Path(current_root) / f).unlink(missing_ok=True)
                    except Exception:
                        pass
    except Exception:
        pass

BOOTSTRAP_CONFIG_FILE = (
    SCRIPT_DIR / "src" / "dbs" / "bootstrap_config.json"
    if (SCRIPT_DIR / "src" / "dbs" / "bootstrap_config.json").exists() or not (SCRIPT_DIR / "bootstrap_config.json").exists()
    else SCRIPT_DIR / "bootstrap_config.json"
)
_ORIGINAL_PYTHON_EXECUTABLE = str(Path(sys.executable).resolve())
DEFAULT_SKIP_UPDATES = True
_BOOTSTRAP_LOG_LOCK = threading.Lock()
_BOOTSTRAP_LOG_FILE = (
    SCRIPT_DIR
    / "logs"
    / ("bootstrap-" + time.strftime('%Y%m%d-%H%M%S') + "-" + str(os.getpid()) + ".log")
)
_BOOTSTRAP_STARTUP_NOTE = ""
_BOOTSTRAP_LOG_MAX_BYTES = 10 * 1024 * 1024
_LAST_PIP_ERROR: str = ""
_gui: "BootstrapGUI | None" = None


def get_bootstrap_log_file() -> Path:
    """Return the complete log for this specific bootstrap run."""
    return _BOOTSTRAP_LOG_FILE


def _find_installer_file(filename: str) -> Path:
    """Find a bundled installer file in SCRIPT_DIR/installers or parent project root installers."""
    candidates = [
        SCRIPT_DIR / "installers" / filename,
        SCRIPT_DIR.parent / "installers" / filename,
        SCRIPT_DIR.parent.parent / "installers" / filename,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return SCRIPT_DIR / "installers" / filename


def _find_installer_dir(dirname: str) -> Path:
    """Find a bundled installer directory in SCRIPT_DIR/installers or parent project root installers."""
    candidates = [
        SCRIPT_DIR / "installers" / dirname,
        SCRIPT_DIR.parent / "installers" / dirname,
        SCRIPT_DIR.parent.parent / "installers" / dirname,
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return SCRIPT_DIR / "installers" / dirname


def _record_bootstrap_log(level: str, message: str) -> None:
    """Append one timestamped event to the durable per-run bootstrap log."""
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = str(message).replace("\r\n", "\n").replace("\r", "\n").split("\n")
        with _BOOTSTRAP_LOG_LOCK:
            _BOOTSTRAP_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _BOOTSTRAP_LOG_FILE.open("a", encoding="utf-8") as log_file:
                for line in lines:
                    log_file.write(f"{timestamp} [{level}] {line}\n")
    except Exception:
        # Logging must never prevent setup from continuing on a read-only or
        # partially copied project folder.
        pass


def _record_bootstrap_exception(context: str) -> None:
    """Record the active exception, including its traceback, without hiding it."""
    import traceback

    _record_bootstrap_log("ERROR", f"{context}\n{traceback.format_exc().rstrip()}")


def _recover_stale_update_processes() -> int:
    """Remove only orphaned update probes from older app versions.

    Older bootstraps launched one ``python -m pip show`` process per package
    from daemon threads. When the bootstrap window closed, those children
    could outlive their parent. Recovery is deliberately narrow: it requires
    a Python executable, this application's directory, and the exact pip
    ``show`` operation. Unrelated Python programs and active build processes
    are never targeted.
    """
    if sys.platform != "win32":
        return 0

    try:
        import psutil
    except Exception:
        return 0

    project_marker = str(SCRIPT_DIR).replace("/", "\\").lower()
    cleaned = 0
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.pid == os.getpid():
                continue
            name = (proc.info.get("name") or "").lower()
            if name not in {"python.exe", "pythonw.exe", "py.exe", "pyw.exe"}:
                continue
            cmdline = proc.info.get("cmdline") or []
            command = " ".join(str(part) for part in cmdline).replace("/", "\\").lower()
            if project_marker not in command:
                continue
            if "-m" not in command or "pip" not in command or "show" not in command:
                continue
            proc.kill()
            cleaned += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            continue
        except Exception:
            continue
    return cleaned

def load_bootstrap_config() -> dict:
    if BOOTSTRAP_CONFIG_FILE.is_file():
        try:
            return json.loads(BOOTSTRAP_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_bootstrap_config(cfg: dict) -> bool:
    try:
        BOOTSTRAP_CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _update_check_skip_reason(gui=None) -> str | None:
    """Return the active update-skip reason, or None when checks are enabled.

    An explicit environment setting is intentionally strongest because it is
    useful for offline deployments. Otherwise the live checkbox is the source
    of truth for this run; this lets an unchecked control recover from an old
    or unwritable JSON value instead of silently staying disabled.
    """
    env_value = os.environ.get("MCU_FLASH_GUI_SKIP_UPDATES", "").strip()
    if env_value.lower() in ("1", "true", "yes"):
        return "MCU_FLASH_GUI_SKIP_UPDATES is enabled"

    active_gui = gui if gui is not None else _gui
    skip_var = getattr(active_gui, "_skip_updates_var", None) if active_gui else None
    if skip_var is not None:
        try:
            if skip_var.get():
                return "the Skip Updates checkbox is selected"
            return None
        except Exception:
            pass

    if load_bootstrap_config().get("skip_updates", DEFAULT_SKIP_UPDATES):
        return "bootstrap_config.json has skip_updates set to true"
    return None


def _clear_editor_config_after_new_environment() -> bool:
    """Clear stale editor state only when a brand-new app environment exists.

    A copied or freshly rebuilt app can leave ``src/gui_config.json`` pointing
    at an editor that no longer matches the available runtime, which makes
    switching editors appear to do nothing.

    The fallback file is shared by app copies, so do not delete it wholesale.
    Preserve its editor-specific keys as well: they are the user's explicit
    preference and the main GUI can fall back at runtime if the native Monaco
    dependencies are unavailable.
    """
    local_config = SCRIPT_DIR / "src" / "gui_config.json"
    removed_local = False

    try:
        if local_config.is_file():
            local_config.unlink()
            removed_local = True
    except Exception:
        # A read-only/package-managed copy should not prevent the environment
        # from completing its setup.
        pass

    return removed_local

def ensure_platformio_penv_with_hook(script_dir: Path = None) -> bool:
    """
    Install the subprocess-hide hook into PlatformIO's private venv (penv)
    so that compiler subprocesses spawned by SCons don't flash console windows.
    Returns True if the hook was installed, False if penv doesn't exist yet.
    """
    if sys.platform != "win32":
        return False

    root = script_dir or SCRIPT_DIR
    pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR", "")
    if not pio_core_dir:
        return False

    penv_site = Path(pio_core_dir) / "penv" / "Lib" / "site-packages"
    if not penv_site.is_dir():
        return False  # penv not created yet — called again after first compile

    try:
        from win_subprocess_hide import install_venv_site_hook
        # Re-use the existing hook installer but targeting the penv site-packages
        hook_py  = penv_site / "mcu_flash_gui_subprocess_hook.py"
        hook_pth = penv_site / "mcu_flash_gui_subprocess_hook.pth"
        hook_py.write_text(
            f'"""Auto-installed hook: hide subprocess console windows on Windows."""\n'
            f'import sys\nfrom pathlib import Path\n\n'
            f'_root = Path({str(root)!r})\n'
            f'if str(_root) not in sys.path:\n    sys.path.insert(0, str(_root))\n\n'
            f'if sys.platform == "win32":\n'
            f'    try:\n'
            f'        from win_subprocess_hide import install\n'
            f'        install()\n'
            f'    except Exception:\n        pass\n',
            encoding="utf-8",
        )
        hook_pth.write_text("import mcu_flash_gui_subprocess_hook\n", encoding="utf-8")
        return True
    except Exception:
        return False

def safe_unlink(path: str | Path, max_retries: int = 5, backoff_ms: int = 50) -> bool:
    """Safely unlink/delete a file with retry backoff for OneDrive/Defender locks."""
    p = Path(path)
    if not p.exists():
        return True
    for attempt in range(max_retries):
        try:
            if sys.platform == "win32":
                try:
                    os.chmod(p, 0o666)
                except Exception:
                    pass
            p.unlink()
            return True
        except (PermissionError, OSError):
            if attempt < max_retries - 1:
                time.sleep((backoff_ms * (2 ** attempt)) / 1000.0)
            else:
                return False
    return False

def safe_rmtree(path: str | Path, max_retries: int = 5, backoff_ms: int = 50) -> bool:
    """Safely delete a directory tree handling Windows file locks and read-only attributes."""
    p = Path(path)
    if not p.exists():
        return True

    def _on_error(func, path_str, exc_info):
        try:
            os.chmod(path_str, 0o777)
            func(path_str)
        except Exception:
            pass

    for attempt in range(max_retries):
        try:
            shutil.rmtree(p, onerror=_on_error)
            if not p.exists():
                return True
        except (PermissionError, OSError):
            pass
        if attempt < max_retries - 1:
            time.sleep((backoff_ms * (2 ** attempt)) / 1000.0)
    return not p.exists()

def safe_replace_file(src: str | Path, dst: str | Path, max_retries: int = 5, backoff_ms: int = 50) -> bool:
    """Safely replace dst with src with retries for OneDrive / MS Defender locks."""
    src_p = Path(src)
    dst_p = Path(dst)
    for attempt in range(max_retries):
        try:
            if dst_p.exists() and sys.platform == "win32":
                try:
                    os.chmod(dst_p, 0o666)
                except Exception:
                    pass
            os.replace(src_p, dst_p)
            return True
        except (PermissionError, OSError):
            if attempt < max_retries - 1:
                time.sleep((backoff_ms * (2 ** attempt)) / 1000.0)
            else:
                try:
                    shutil.copy2(src_p, dst_p)
                    safe_unlink(src_p)
                    return True
                except Exception:
                    return False
    return False

def hide_hidden_attribute(path) -> None:
    """Hide one generated path without changing its children or system bit."""
    try:
        p = Path(path)
        if not p.exists() or sys.platform != "win32":
            return
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
        if attrs != -1:
            # Clear READONLY before generated files are rewritten, then add
            # HIDDEN only.  In particular, do not recursively touch a tree.
            ctypes.windll.kernel32.SetFileAttributesW(
                str(p), (attrs & ~0x01) | 0x02
            )
    except Exception:
        pass


def unhide_hidden_attribute(path) -> None:
    """Make one repaired path visible without changing its children."""
    try:
        p = Path(path)
        if not p.exists() or sys.platform != "win32":
            return
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
        if attrs != -1:
            ctypes.windll.kernel32.SetFileAttributesW(str(p), attrs & ~0x06)
    except Exception:
        pass


def _ensure_codebase_visible(script_dir: Path = SCRIPT_DIR) -> None:
    """Repair legacy attribute damage on the app root without walking it."""
    if sys.platform != "win32":
        return
    try:
        root = Path(script_dir).resolve(strict=False)
        # SCRIPT_DIR is the project root for both source and frozen launches.
        for path in (root, root / "src", root / "src" / "modules"):
            unhide_hidden_attribute(path)
    except Exception:
        pass


def _hide_junction(path: Path) -> None:
    """Hide the junction entry itself; /L prevents following its target."""
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["attrib", "/L", "+h", str(path)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass

def _is_windows_reparse_point(path: Path) -> bool:
    """Return True when *path* is a Windows junction or symbolic-link reparse point."""
    if sys.platform != "win32":
        return False
    try:
        if hasattr(path, "is_junction") and path.is_junction():
            return True
        if path.is_symlink():
            return True
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except Exception:
        return False


def _ensure_junction(link_path: Path, real_dir: Path) -> str | None:
    """Create or repair an app-owned directory alias for ``real_dir``.

    Existing junctions/symlinks are safe to repoint because deleting the
    reparse-point entry does not delete the target directory. A normal,
    non-empty directory is never removed, so unrelated user data is protected.
    """
    try:
        real_dir = Path(real_dir)
        real_dir.mkdir(parents=True, exist_ok=True)
        real_dir_resolved = real_dir.resolve()
    except Exception:
        return None

    try:
        # ``Path.exists`` is False for some broken reparse points, so include
        # the explicit reparse/symlink probes in the existence test.
        link_present = (
            link_path.exists()
            or link_path.is_symlink()
            or _is_windows_reparse_point(link_path)
        )
        if link_present:
            try:
                if link_path.resolve() == real_dir_resolved:
                    return str(link_path)
            except Exception:
                pass

            if sys.platform == "win32" and _is_windows_reparse_point(link_path):
                # rmdir removes only the junction/symlink entry, never its target.
                try:
                    subprocess.run(
                        ["cmd", "/c", "rmdir", str(link_path)],
                        check=True,
                        capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except Exception:
                    return None
            elif link_path.is_symlink():
                try:
                    link_path.unlink()
                except Exception:
                    return None
            elif link_path.is_dir():
                try:
                    is_empty = not any(link_path.iterdir())
                except Exception:
                    is_empty = False
                if not is_empty:
                    return None
                try:
                    os.rmdir(link_path)
                except Exception:
                    return None
            else:
                return None

        try:
            link_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        if sys.platform == "win32":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_path), str(real_dir_resolved)],
                check=True,
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            link_path.symlink_to(real_dir_resolved, target_is_directory=True)

        try:
            if link_path.resolve() != real_dir_resolved:
                return None
        except Exception:
            return None
        return str(link_path)
    except Exception:
        return None


_MCUFLASHER_APP_DIRNAME = ".mcuflasher-app"
_PLATFORMIO_ALIAS_NAME = ".platformio-mcu-gui"
_MODULES_ALIAS_NAME = ".mcuflasher-libs"


def _prepare_mcuflasher_app_root(app_root: Path) -> Path | None:
    """Create the app-owned namespace as a NORMAL hidden, writable directory.

    The parent itself must never be a symlink/junction.  If an unrelated file,
    directory reparse point, or other object already occupies the requested
    location, leave it untouched and let the caller try its fallback location.
    """
    try:
        app_root = Path(app_root)
        if (
            app_root.exists()
            or app_root.is_symlink()
            or _is_windows_reparse_point(app_root)
        ):
            if _is_windows_reparse_point(app_root) or app_root.is_symlink():
                return None
            if not app_root.is_dir():
                return None
        else:
            app_root.mkdir(parents=True, exist_ok=True)

        if _is_windows_reparse_point(app_root) or not app_root.is_dir():
            return None

        # Keep the namespace ordinary and writable.  Hidden is the only Windows
        # attribute intentionally applied to the parent directory.
        try:
            os.chmod(app_root, 0o777)
        except Exception:
            pass
        hide_hidden_attribute(app_root)
        return app_root
    except Exception:
        return None


def _mcuflasher_app_root(real_dir: Path) -> Path | None:
    r"""Return the writable ``.mcuflasher-app`` namespace for this app.

    Prefer ``<drive>:\.mcuflasher-app`` so PlatformIO/compiler paths remain
    short.  If the drive root cannot be used without touching unrelated data,
    fall back to ``%LOCALAPPDATA%\.mcuflasher-app``.  Both are normal folders;
    only their children are junctions.
    """
    if sys.platform != "win32":
        return None

    real_dir = Path(real_dir)
    drive = real_dir.drive or Path(SCRIPT_DIR).drive or "C:"
    candidates = [Path(drive + "\\.mcuflasher-app")]

    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_appdata:
        local_appdata = str(Path.home() / "AppData" / "Local")
    candidates.append(Path(local_appdata) / _MCUFLASHER_APP_DIRNAME)

    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        prepared = _prepare_mcuflasher_app_root(candidate)
        if prepared is not None:
            return prepared
    return None


def _remove_legacy_app_junction(path: Path) -> bool:
    """Remove one exact legacy MCU Flasher junction ENTRY, never its target.

    Normal files/directories are deliberately ignored.  The target is not
    inspected, adopted, migrated, or otherwise treated as related state.
    """
    if sys.platform != "win32":
        return False
    path = Path(path)
    try:
        if not _is_windows_reparse_point(path):
            return False
        # Legacy aliases were hidden in older builds.  /L changes only the
        # junction entry attributes, not the target directory.
        subprocess.run(
            ["attrib", "/L", "-h", "-s", str(path)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        subprocess.run(
            ["cmd", "/c", "rmdir", str(path)],
            check=True,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return not _is_windows_reparse_point(path)
    except Exception:
        return False


def _cleanup_legacy_app_aliases(script_dir: Path) -> None:
    r"""Remove the old top-level alias entries after the new namespace is live.

    These exact app-owned legacy names are cleaned up only when they are
    reparse points.  A real directory at any of these locations is never
    removed.  Removing a junction with ``rmdir`` deletes the link entry only.
    """
    if sys.platform != "win32":
        return
    try:
        drive = Path(script_dir).drive or Path(SCRIPT_DIR).drive or "C:"
        legacy = [
            Path(drive + "\\.mcuflasher-libs"),
            Path(drive + "\\.platformio-mcu-gui"),
        ]
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        if not local_appdata:
            local_appdata = str(Path.home() / "AppData" / "Local")
        legacy.append(Path(local_appdata) / ".platformio-mcu-gui")
        for old_alias in legacy:
            _remove_legacy_app_junction(old_alias)
    except Exception:
        pass


def _short_platformio_core_alias(real_dir: Path) -> str:
    r"""Return the app-namespace alias for one physical PlatformIO store.

    Preferred layout::

        <drive>:\.mcuflasher-app\                 (normal hidden folder)
            .platformio-mcu-gui -> <project>\src\.platformio-mcu-gui
            .mcuflasher-libs    -> <project>\src\modules

    Nesting adds only a small path prefix while retaining ample headroom below
    Windows' CreateProcess command-line limit.  If the drive-root namespace is
    unavailable, the same two-child layout is used under LOCALAPPDATA.
    """
    real_dir = Path(real_dir)
    if sys.platform != "win32":
        return str(real_dir)

    app_root = _mcuflasher_app_root(real_dir)
    if app_root is not None:
        alias = _ensure_junction(app_root / _PLATFORMIO_ALIAS_NAME, real_dir)
        if alias:
            return alias

    # Last-resort safety: preserve functionality rather than creating or
    # deleting unrelated filesystem objects when no namespace is writable.
    return str(real_dir)


def _get_safe_platformio_core_dir(script_dir: Path) -> str:
    """Return the PlatformIO core path used by Bootstrap and the GUI.

    The physical package store remains project-local at
    ``<PROJECT_FOLDER>/src/.platformio-mcu-gui``. On Windows, PlatformIO is
    pointed at a short junction to that same store so ESP32 GCC does not exceed
    the CreateProcess command-line limit. A stale app-owned junction is repaired
    automatically.
    """
    inherited = os.environ.get("PLATFORMIO_CORE_DIR", "").strip()
    if inherited:
        try:
            inherited_path = Path(os.path.expandvars(os.path.expanduser(inherited)))
            inherited_path.mkdir(parents=True, exist_ok=True)
            return _short_platformio_core_alias(inherited_path)
        except Exception:
            pass

    target_dir = script_dir / "src" / ".platformio-mcu-gui"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        return _short_platformio_core_alias(target_dir)
    except Exception:
        pass

    fallback = script_dir / "src" / "_board-frameworks" / ".platformio"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
        return _short_platformio_core_alias(fallback)
    except Exception:
        return str(fallback)


def _neutralize_conflicting_global_platformio_config() -> None:
    """Stop PlatformIO's own global config from silently overriding PLATFORMIO_CORE_DIR.

    PlatformIO reads ``<home>/.platformio/platformio.ini`` (its per-user core
    config -- NOT any project's local platformio.ini) on every invocation. If
    that file already has a ``[platformio] core_dir = ...`` line -- typically
    left over from an install that happened before this app started managing
    its own store -- some PlatformIO versions honor that file over the
    PLATFORMIO_CORE_DIR environment variable, so packages silently resolve
    from the old location no matter what we set above.

    ``Path.home()`` resolves per-user on every machine (no username is ever
    hardcoded here), so this is safe to run unconditionally for any account.
    We only ever comment out a conflicting core_dir line -- we never delete
    the old store itself, so nothing another tool relies on there is lost.
    """
    try:
        global_ini = Path.home() / ".platformio" / "platformio.ini"
        if not global_ini.is_file():
            return

        text = global_ini.read_text(encoding="utf-8", errors="replace")
        our_core_dir = os.environ.get("PLATFORMIO_CORE_DIR", "").strip()
        if not our_core_dir:
            return

        import re as _re
        changed = False
        out_lines = []
        for line in text.splitlines():
            m = _re.match(r"^(\s*)core_dir(\s*=\s*)(.+?)\s*$", line, _re.IGNORECASE)
            if m:
                existing_value = m.group(3).strip().strip('"').strip("'")
                try:
                    existing_resolved = str(Path(os.path.expandvars(os.path.expanduser(existing_value))).resolve())
                except Exception:
                    existing_resolved = existing_value
                try:
                    ours_resolved = str(Path(our_core_dir).resolve())
                except Exception:
                    ours_resolved = our_core_dir
                if existing_resolved != ours_resolved:
                    out_lines.append(f"{m.group(1)}; (disabled by Bootstrap — pointed elsewhere) core_dir{m.group(2)}{m.group(3)}")
                    changed = True
                    continue
            out_lines.append(line)

        if changed:
            global_ini.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def _ensure_modules_junction(script_dir: Path) -> str | None:
    r"""Create the modules junction INSIDE the shared MCU Flasher namespace.

    The target is always this running project's own ``src/modules`` directory.
    The namespace parent is a normal hidden/writable directory; this child is a
    junction.  No external target is searched for or adopted.
    """
    if sys.platform != "win32":
        return None
    try:
        libs_dir = (Path(script_dir) / "src" / "modules").resolve()
        app_root = _mcuflasher_app_root(libs_dir)
        if app_root is None:
            return None
        return _ensure_junction(app_root / _MODULES_ALIAS_NAME, libs_dir)
    except Exception:
        return None


def _configure_platformio_environment(script_dir: Path) -> str:
    """Ensure all PlatformIO store directories (packages, cache, temp, libraries) live on the project's drive."""
    _ensure_codebase_visible(script_dir)
    core_dir = _get_safe_platformio_core_dir(script_dir)
    os.environ["PLATFORMIO_CORE_DIR"] = core_dir

    # Build both aliases under one app-owned namespace.  Only after both
    # junctions exist do we remove the old top-level junction entries.
    modules_alias = _ensure_modules_junction(script_dir)
    try:
        core_path = Path(core_dir)
        using_new_namespace = (
            core_path.name == _PLATFORMIO_ALIAS_NAME
            and core_path.parent.name == _MCUFLASHER_APP_DIRNAME
        )
    except Exception:
        using_new_namespace = False
    if using_new_namespace and modules_alias:
        _cleanup_legacy_app_aliases(script_dir)

    try:
        c_path = Path(core_dir)
        tmp_dir = c_path / ".tmp"
        cache_dir = c_path / ".cache"
        lib_dir = c_path / "lib"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        lib_dir.mkdir(parents=True, exist_ok=True)

        os.environ["PLATFORMIO_CACHE_DIR"] = str(cache_dir)
        os.environ["PLATFORMIO_BUILD_CACHE_DIR"] = str(cache_dir / "build")
        os.environ["PLATFORMIO_GLOBALLIB_DIR"] = str(lib_dir)
        os.environ["TMP"] = str(tmp_dir)
        os.environ["TEMP"] = str(tmp_dir)
        os.environ["TMPDIR"] = str(tmp_dir)
    except Exception:
        pass
    return core_dir


# ── Pre-built PlatformIO core directory (cloud seed) ────────
# Instead of waiting for PlatformIO to download/unpack/install all board
# toolchains from scratch on first run (easily 10–30+ minutes), a pre-built
# snapshot of .platformio-mcu-gui is hosted as a release asset. When the
# local store is empty, bootstrap downloads and extracts the snapshot so
# PlatformIO finds an already-populated core directory. GitHub Releases
# provides a CDN-backed, resumable asset download. This is intentionally the
# only source: a failed or unavailable release must be reported rather than
# silently switching to a slower or different host.
_PLATFORMIO_PREBUILT_ZIP_NAME = "platformio-mcu-gui-prebuilt.zip"
_PLATFORMIO_PREBUILT_GITHUB_URL = (
    "https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/releases/download/"
    "v1.0.0-assets/platformio-mcu-gui.zip"
)
# Metadata for the GitHub Release asset. The local destination keeps the
# historical name above so existing cleanup/resume behavior is unchanged.
_PLATFORMIO_PREBUILT_EXPECTED_SIZE = 1785358455
_PLATFORMIO_PREBUILT_EXPECTED_SHA256 = (
    "b284708a25c46143827b94ec423d7fe4242729d5c39e4c3324556b9b7af8b7c2"
)


def _platformio_core_is_populated(script_dir: Path) -> bool:
    """Return True when the PlatformIO core store already has platform/package content.

    This is deliberately a shallow check: if the directories exist and contain
    at least one child entry, we treat the store as populated.  PlatformIO's own
    platform/package integrity checks will repair anything missing later.
    """
    pio_dir = script_dir / "src" / ".platformio-mcu-gui"
    if not pio_dir.is_dir():
        return False
    packages = pio_dir / "packages"
    platforms = pio_dir / "platforms"
    try:
        has_packages = packages.is_dir() and any(packages.iterdir())
        has_platforms = platforms.is_dir() and any(platforms.iterdir())
        return has_packages and has_platforms
    except Exception:
        return False


def _ensure_platformio_core_prebuilt(gui: "BootstrapGUI | None" = None) -> bool:
    """Download and extract the pre-built PlatformIO core directory if empty.

    This seeds ``src/.platformio-mcu-gui`` from a release-hosted zip so that
    PlatformIO finds an already-populated core store on first launch.  If the
    store already contains packages and platforms, this is a no-op.

    The zip is treated as a baseline: future ``pio platform install`` calls
    will add new boards into the same directory without conflict.

    Returns True if the store is populated (either already or after extraction),
    False if the GitHub download or extraction failed. The caller treats this
    as fatal so PlatformIO never silently switches to a second bootstrap source.
    """
    import zipfile

    if _platformio_core_is_populated(SCRIPT_DIR):
        ok("Pre-built PlatformIO core already populated.")
        return True

    if not _is_network_reachable(timeout=2.0):
        warn("PlatformIO core toolchains are not yet installed and cannot be downloaded while offline.")
        return False

    pio_dir = SCRIPT_DIR / "src" / ".platformio-mcu-gui"
    zip_dest = SCRIPT_DIR / "src" / _PLATFORMIO_PREBUILT_ZIP_NAME

    # ── Step 1: Download ────────────────────────────────────────────
    status("Downloading pre-built PlatformIO toolchains...")
    if gui:
        gui.set_status("Downloading pre-built PlatformIO toolchains...")

    try:
        # GitHub Releases is the only source. Its asset CDN supports HTTP
        # Range requests, so an existing .part file can continue safely.
        status(f"Downloading {zip_dest.name} from GitHub Releases...")
        _download_file(
            _PLATFORMIO_PREBUILT_GITHUB_URL,
            zip_dest,
            timeout=120,
            attempts=3,
            expected_size=_PLATFORMIO_PREBUILT_EXPECTED_SIZE,
            expected_sha256=_PLATFORMIO_PREBUILT_EXPECTED_SHA256,
        )
    except Exception as exc:
        _record_bootstrap_exception("Pre-built PlatformIO zip download failed")
        warn(f"Could not download pre-built PlatformIO zip: {exc}")
        warn("GitHub Releases is required; bootstrap cannot continue without the pre-built archive.")
        safe_unlink(zip_dest)
        return False

    if not zip_dest.is_file() or zip_dest.stat().st_size < 1024:
        warn("GitHub archive is missing or too small; bootstrap cannot continue.")
        safe_unlink(zip_dest)
        return False

    # ── Step 2: Extract ─────────────────────────────────────────────
    status("Extracting pre-built PlatformIO toolchains...")
    if gui:
        gui.set_status("Extracting pre-built PlatformIO toolchains...")
        gui.set_progress_percent(0)

    try:
        pio_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(str(zip_dest), "r") as zf:
            infos = zf.infolist()
            total_files = len(infos)
            if total_files == 0:
                raise RuntimeError("Zip archive is empty")

            total_uncompressed_bytes = sum(info.file_size for info in infos)

            # Detect whether the zip has a top-level directory wrapper.
            # e.g. all members start with ".platformio-mcu-gui/" — strip it
            # so we extract directly into pio_dir.
            first_parts = {info.filename.split("/", 1)[0] for info in infos if "/" in info.filename}
            has_wrapper = (
                len(first_parts) == 1
                and first_parts.pop().replace(".", "").replace("-", "").replace("_", "").lower()
                in (
                    "platformiomcugui",
                    "platformiomcuguiprebuilt",
                )
            )

            last_extract_time = 0.0
            extracted_bytes = 0

            for idx, info in enumerate(infos, 1):
                member = info.filename
                extracted_bytes += info.file_size

                # Skip directory entries (they're created implicitly).
                if member.endswith("/"):
                    if gui and total_files > 0:
                        gui.set_progress_percent(min(99, int(idx * 100 / total_files)))
                    continue

                if has_wrapper:
                    # Strip the top-level wrapper directory from the path.
                    _, _, relative = member.partition("/")
                    if not relative:
                        continue
                    target = pio_dir / relative
                else:
                    target = pio_dir / member

                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)

                now = time.time()
                if gui and total_files > 0 and (now - last_extract_time >= 0.15 or idx == total_files):
                    last_extract_time = now
                    pct = min(99.0, (idx / total_files) * 100.0)
                    filled = int(pct / 100.0 * 30)
                    bar = "▰" * filled + "▱" * (30 - filled)

                    ext_mb = extracted_bytes / (1024 * 1024)
                    tot_mb = total_uncompressed_bytes / (1024 * 1024)
                    size_info = f"{ext_mb:.1f}/{tot_mb:.1f} MB" if total_uncompressed_bytes > 0 else f"{idx}/{total_files} files"

                    extract_block = (
                        f"  Extracting {zip_dest.name}...\n"
                        f"  {bar}  {pct:5.1f}% ({idx}/{total_files} files • {size_info})"
                    )
                    gui.update_platformio_progress_block(extract_block)
                    gui.set_status(f"Extracting {zip_dest.name}... {idx}/{total_files} files ({pct:.1f}%)")
                    gui.set_progress_percent(pct)

        # Do not report the step as complete until the extracted store has
        # passed the same readiness check used by the caller.  The live row is
        # deliberately capped at 99% while files are being written, so the
        # final UI update must be issued after extraction *and* verification.
        core_ready = _platformio_core_is_populated(SCRIPT_DIR)
        if not core_ready:
            raise RuntimeError("Extracted PlatformIO core failed readiness verification")
        if gui:
            gui.clear_platformio_progress_block()
            gui.set_progress_percent(100)
            gui.set_status("PlatformIO toolchains ready.")
        ok("Pre-built PlatformIO core extracted successfully.")
    except Exception as exc:
        _record_bootstrap_exception("Pre-built PlatformIO zip extraction failed")
        warn(f"Failed to extract pre-built PlatformIO zip: {exc}")
        warn("GitHub archive extraction is required; bootstrap cannot continue.")
        return False
    finally:
        if gui:
            gui.clear_platformio_progress_block()
        # ── Step 3: Clean up the zip ────────────────────────────────
        safe_unlink(zip_dest)

    return _platformio_core_is_populated(SCRIPT_DIR)


# One store only: Bootstrap and every GUI subprocess must resolve the same core_dir.
# PlatformIO configuration is intentionally deferred.  Importing bootstrap is
# part of the normal launch decision, so creating junctions/directories and
# editing the user's global PlatformIO config here would put filesystem work
# back on the fast path.  The setup worker configures it before any install or
# build preparation; a normal launch receives the already-validated core path
# from the installation health snapshot instead.
os.environ["PYTHONUNBUFFERED"] = "1"

if sys.platform == "win32":
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from win_subprocess_hide import install as _install_subprocess_hide
        from win_subprocess_hide import install_venv_site_hook as _install_venv_site_hook

        _install_subprocess_hide()
        _install_venv_site_hook(SCRIPT_DIR)
    except Exception:
        pass

GUI_SCRIPT = (
    SCRIPT_DIR / "main" / "mcu_flash_gui.py"
    if (SCRIPT_DIR / "main" / "mcu_flash_gui.py").exists()
    else SCRIPT_DIR / "mcu_flash_gui.py"
)

# ── ANSI codes kept for any direct print() fallbacks ────────
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[90m"
BOLD = "\033[1m"
RESET = "\033[0m"

def _detect_system_theme() -> str:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        )
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return "light" if val == 1 else "default"
    except Exception:
        return "default"


# ── Theme colours matching MCU Flash GUI exactly ─────────────
def _resolve_bootstrap_theme() -> dict:
    palettes = {
        "default": {
            "T_BG_DARKEST": "#0a0e14",
            "T_BG_DARK": "#10151c",
            "T_BG_MID": "#161d27",
            "T_BG_LIGHT": "#1c2532",
            "T_BG_HOVER": "#243040",
            "T_BORDER": "#2a3545",
            "T_TEXT": "#c8d2dc",
            "T_TEXT_DIM": "#6b7d94",
            "T_TEXT_BRIGHT": "#e8edf3",
            "T_CYAN": "#39c5bb",
            "T_GREEN": "#5ccc6e",
            "T_YELLOW": "#e8b83a",
            "T_RED": "#f05050",
            "T_MAGENTA": "#c678dd",
        },
        "light": {
            "T_BG_DARKEST": "#f4f6f9",
            "T_BG_DARK": "#e9ecef",
            "T_BG_MID": "#ffffff",
            "T_BG_LIGHT": "#dee2e6",
            "T_BG_HOVER": "#d0d7de",
            "T_BORDER": "#c5ccd6",
            "T_TEXT": "#24292f",
            "T_TEXT_DIM": "#57606a",
            "T_TEXT_BRIGHT": "#1a1f24",
            "T_CYAN": "#0969da",
            "T_GREEN": "#1a7f37",
            "T_YELLOW": "#9a6700",
            "T_RED": "#cf222e",
            "T_MAGENTA": "#8250df",
        },
        "solarized_dark": {
            "T_BG_DARKEST": "#001b22",
            "T_BG_DARK": "#002b36",
            "T_BG_MID": "#073642",
            "T_BG_LIGHT": "#0d4a59",
            "T_BG_HOVER": "#115d70",
            "T_BORDER": "#166b80",
            "T_TEXT": "#ffffff",
            "T_TEXT_DIM": "#d0e4e8",
            "T_TEXT_BRIGHT": "#ffffff",
            "T_CYAN": "#2aa198",
            "T_GREEN": "#859900",
            "T_YELLOW": "#b58900",
            "T_RED": "#dc322f",
            "T_MAGENTA": "#d33682",
        }
    }
    mode = "default"
    for cfg in (SCRIPT_DIR / "src" / "gui_config.json", Path.home() / ".mcu_gui_config.json"):
        try:
            if cfg.exists():
                data = json.loads(cfg.read_text(encoding="utf-8"))
                shared = data.get("shared", {})
                if shared.get("theme_follow_system", False):
                    mode = _detect_system_theme()
                    break
                m = shared.get("theme_mode", "default")
                if m in ("solarized", "solarize", "solarized_dark", "solarize_dark"):
                    m = "solarized_dark"
                if m in palettes:
                    mode = m
                    break
        except Exception:
            pass
    return palettes[mode]

_T_PALETTE = _resolve_bootstrap_theme()
T_BG_DARKEST  = _T_PALETTE["T_BG_DARKEST"]
T_BG_DARK     = _T_PALETTE["T_BG_DARK"]
T_BG_MID      = _T_PALETTE["T_BG_MID"]
T_BG_LIGHT    = _T_PALETTE["T_BG_LIGHT"]
T_BG_HOVER    = _T_PALETTE["T_BG_HOVER"]
T_BORDER      = _T_PALETTE["T_BORDER"]
T_TEXT        = _T_PALETTE["T_TEXT"]
T_TEXT_DIM    = _T_PALETTE["T_TEXT_DIM"]
T_TEXT_BRIGHT = _T_PALETTE["T_TEXT_BRIGHT"]
T_CYAN        = _T_PALETTE["T_CYAN"]
T_GREEN       = _T_PALETTE["T_GREEN"]
T_YELLOW      = _T_PALETTE["T_YELLOW"]
T_RED         = _T_PALETTE["T_RED"]
T_MAGENTA     = _T_PALETTE["T_MAGENTA"]

# ── Shared GUI state (set up by BootstrapGUI) ────────────────
_gui: "BootstrapGUI | None" = None   # set when the window is live

# How long (in seconds) to wait after the final "All dependencies ready!" line
# (or any other finish/launch/launch-failed path) before the bootstrap window
# actually closes. Gives the user a beat to read the success message and see
# the spinner land on ✔ before the window disappears. 0 = close immediately.
BOOTSTRAP_CLOSE_DELAY_S: float = 2.5

# ─────────────────────────────────────────────────────────────
# BootstrapGUI — dark Tkinter window with scrollable log,
# animated spinner, and a status bar; matches MCU Flash GUI.
# ─────────────────────────────────────────────────────────────
class BootstrapGUI:
    """
    Displays bootstrap progress in a styled GUI window.
    All methods are safe to call from any thread; they use
    root.after() to marshal updates to the Tk main thread.
    """
    SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self):
        import sys
        if sys.platform == "win32":
            try:
                import ctypes
                gdi32 = ctypes.windll.gdi32
                FR_PRIVATE = 0x10
                fonts_dir = SCRIPT_DIR / "src" / "fonts" / "Montserrat" / "static"
                if not fonts_dir.exists():
                    fonts_dir = SCRIPT_DIR / "src" / "fonts" / "Montserrat"
                if fonts_dir.exists():
                    for ttf_file in fonts_dir.glob("*.ttf"):
                        path_buf = ctypes.create_unicode_buffer(str(ttf_file))
                        gdi32.AddFontResourceExW(path_buf, FR_PRIVATE, 0)
            except Exception:
                pass
        import tkinter as tk
        from tkinter import font as tkfont, ttk

        self.root = tk.Tk()
        self.root.title("MCU Uploader IDE by Naph — Setup")
        
        # Set window icon if available
        try:
            icon_path = SCRIPT_DIR / "src" / "assets" / "mcu_icon.ico"
            if not icon_path.exists():
                icon_path = SCRIPT_DIR / "src" / "mcu_icon.ico"
            if icon_path.exists():
                self.root.iconbitmap(default=str(icon_path))
                self.root.iconbitmap(str(icon_path))
            else:
                log_dir = SCRIPT_DIR / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "bootstrap_icon.log").write_text(f"Icon file does not exist at: {icon_path}\n", encoding="utf-8")
        except Exception as e:
            import traceback
            log_dir = SCRIPT_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "bootstrap_icon.log").write_text(f"Error setting icon: {e}\n{traceback.format_exc()}\n", encoding="utf-8")
        self.root.configure(bg=T_BG_DARKEST)
        # Keep the setup window at its calculated size. Resizing it can expose
        # incomplete progress rows and makes the bootstrap layout jump on
        # smaller displays.
        self.root.resizable(False, False)

        # Centre on screen
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        # Size the setup window from the current Tk screen dimensions instead
        # of using a small fixed size.  Tk reports dimensions in the display's
        # effective DPI/scaling coordinate space, so this remains about 70%
        # of the usable screen on high-DPI and low-end displays alike.
        width = min(sw - 32, max(520, int(sw * 0.70)))
        height = min(sh - 48, max(420, int(sh * 0.70)))
        self.root.minsize(min(720, width), min(480, height))
        # Apply the requested size first, let Tk account for the native frame
        # and DPI rounding, then center using the realized window dimensions.
        # This keeps the title-bar-inclusive window mathematically centered.
        self.root.geometry(f"{width}x{height}")
        self.root.update_idletasks()
        actual_width = max(1, self.root.winfo_width())
        actual_height = max(1, self.root.winfo_height())
        x = max(0, (sw - actual_width) // 2)
        y = max(0, (sh - actual_height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        # Force window always on top for 1 second (1000ms) upon launch
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(1000, self._unset_topmost)
        except Exception:
            pass

        # ── Header bar ──────────────────────────────────────
        hdr = tk.Frame(self.root, bg=T_BG_DARK, pady=10, padx=16)
        hdr.pack(fill=tk.X)
        hdr_top = tk.Frame(hdr, bg=T_BG_DARK)
        hdr_top.pack(fill=tk.X)

        fnt_title = tkfont.Font(family="Montserrat", size=13, weight="bold")
        fnt_sub   = tkfont.Font(family="Montserrat", size=9)
        fnt_timer = tkfont.Font(family="Consolas", size=10, weight="bold")

        tk.Label(hdr_top, text="⚡  MCU Uploader IDE by Naph", font=fnt_title,
                 fg=T_CYAN, bg=T_BG_DARK).pack(side=tk.LEFT)
        tk.Label(hdr, text="Setting up dependencies…", font=fnt_sub,
                 fg=T_TEXT_DIM, bg=T_BG_DARK).pack(anchor=tk.W, pady=(3, 0))

        # Top-right live elapsed timer
        import time as _t_mod
        self._start_time = _t_mod.time()
        self._timer_var = tk.StringVar(value="⏱ 00:00")
        tk.Label(hdr_top, textvariable=self._timer_var, font=fnt_timer,
                 fg=T_CYAN, bg=T_BG_DARK).pack(side=tk.RIGHT, pady=(3, 0))

        # ── Divider ─────────────────────────────────────────
        tk.Frame(self.root, bg=T_BORDER, height=1).pack(fill=tk.X)

        # ── Log area ────────────────────────────────────────
        log_frame = tk.Frame(self.root, bg=T_BG_DARKEST)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        fnt_log = tkfont.Font(family="Consolas", size=9)

        self.log = tk.Text(
            log_frame,
            font=fnt_log,
            bg=T_BG_DARKEST,
            fg=T_TEXT,
            insertbackground=T_CYAN,
            selectbackground=T_BG_HOVER,
            selectforeground=T_TEXT_BRIGHT,
            relief=tk.FLAT,
            bd=0,
            wrap=tk.WORD,
            state=tk.DISABLED,
            padx=14,
            pady=10,
        )
        self.scrollbar = ttk.Scrollbar(
            log_frame,
            orient=tk.VERTICAL,
            style="Vertical.TScrollbar",
            command=self.log.yview,
        )
        self.log.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Tag colour palette
        self.log.tag_configure("section",  foreground=T_CYAN,    font=tkfont.Font(family="Consolas", size=9, weight="bold"))
        self.log.tag_configure("ok",       foreground=T_GREEN)
        self.log.tag_configure("warn",     foreground=T_YELLOW)
        self.log.tag_configure("fail",     foreground=T_RED)
        self.log.tag_configure("dim",      foreground=T_TEXT_DIM)
        self.log.tag_configure("normal",   foreground=T_TEXT)
        self.log.tag_configure("update",   foreground=T_MAGENTA)
        self.log.tag_configure("pip_row",  foreground=T_CYAN,    font=tkfont.Font(family="Consolas", size=10))

        # ── Divider ─────────────────────────────────────────
        tk.Frame(self.root, bg=T_BORDER, height=1).pack(fill=tk.X)

        # ── Status bar (spinner + text + auto-scroll toggle) ────────────────
        sb = tk.Frame(self.root, bg=T_BG_DARK, pady=5, padx=14)
        sb.pack(fill=tk.X)

        fnt_status = tkfont.Font(family="Montserrat", size=9)

        self._spin_var = tk.StringVar(value="⠋")
        tk.Label(sb, textvariable=self._spin_var, font=fnt_status,
                 fg=T_CYAN, bg=T_BG_DARK).pack(side=tk.LEFT)

        self._status_var = tk.StringVar(value="Initialising…")
        tk.Label(sb, textvariable=self._status_var, font=fnt_status,
                 fg=T_TEXT_DIM, bg=T_BG_DARK).pack(side=tk.LEFT, padx=(6, 0))

        # Auto-scroll checkbox — right-aligned in the status bar
        self._auto_scroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            sb,
            text="Auto-Scroll",
            variable=self._auto_scroll_var,
            font=fnt_status,
            bg=T_BG_DARK,
            fg=T_TEXT_DIM,
            activebackground=T_BG_DARK,
            activeforeground=T_TEXT,
            selectcolor=T_BG_DARKEST,
            relief=tk.FLAT,
            bd=0,
            cursor="hand2",
        ).pack(side=tk.RIGHT)

        # Skip updates checkbox (for offline mode)
        cfg_init = load_bootstrap_config()
        self._skip_updates_var = tk.BooleanVar(
            value=cfg_init.get("skip_updates", DEFAULT_SKIP_UPDATES)
        )

        def _on_toggle_skip_updates():
            c = load_bootstrap_config()
            enabled = not self._skip_updates_var.get()
            c["skip_updates"] = not enabled
            if save_bootstrap_config(c):
                self.log_dim(
                    "Update checks enabled for this launch."
                    if enabled
                    else "Update checks disabled for this launch."
                )
            else:
                self.log_warn(
                    "Could not save the update preference; this launch will still use the checkbox setting."
                )

        tk.Checkbutton(
            sb,
            text="Skip Updates",
            variable=self._skip_updates_var,
            command=_on_toggle_skip_updates,
            font=fnt_status,
            bg=T_BG_DARK,
            fg=T_TEXT_DIM,
            activebackground=T_BG_DARK,
            activeforeground=T_TEXT,
            selectcolor=T_BG_DARKEST,
            relief=tk.FLAT,
            bd=0,
            cursor="hand2",
        ).pack(side=tk.RIGHT, padx=(0, 12))

        # ── Progress bar (step progress + busy/marquee for downloads) ─
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Bootstrap.Horizontal.TProgressbar",
            troughcolor=T_BG_LIGHT,
            background=T_CYAN,
            bordercolor=T_BG_DARK,
            lightcolor=T_CYAN,
            darkcolor=T_CYAN,
            thickness=9,
        )
        style.configure(
            "Vertical.TScrollbar",
            background=T_BG_MID,
            troughcolor=T_BG_DARKEST,
            bordercolor=T_BG_DARKEST,
            arrowcolor=T_TEXT_DIM,
            lightcolor=T_BG_MID,
            darkcolor=T_BG_MID,
        )
        style.map(
            "Vertical.TScrollbar",
            background=[("active", T_BG_HOVER)]
        )
        self._progress = ttk.Progressbar(
            self.root,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            style="Bootstrap.Horizontal.TProgressbar",
        )
        self._progress.pack(fill=tk.X, side=tk.BOTTOM)

        # Total number of top-level "Checking X" steps in the setup flow.
        self.TOTAL_STEPS = 9
        self._step_index = 0

        self._spin_idx = 0
        self._spinning = True
        self._closed = False          # must be set before _tick_spinner reads it
        self._tick_spinner()
        self._tick_timer()

        # Allow closing without killing the main process immediately
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # The first geometry pass occurs before the native window is mapped.
        # Recenter once Tk/Windows has created the real window so mixed-DPI
        # scaling, title-bar borders, and the taskbar work area are included.
        self.root.after_idle(self._center_bootstrap_window)

    def _center_bootstrap_window(self):
        """Center the mapped bootstrap window in its current monitor work area."""
        try:
            if getattr(self, "_closed", False):
                return
            if not self.root.winfo_viewable():
                # Tk may run the idle callback just before Windows maps the
                # top-level window. Retry after mapping instead of accepting
                # a position based on an incomplete native rectangle.
                self.root.after(50, self._center_bootstrap_window)
                return
            self.root.update_idletasks()
            left = top = 0
            right = self.root.winfo_screenwidth()
            bottom = self.root.winfo_screenheight()

            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes

                class _Rect(ctypes.Structure):
                    _fields_ = [
                        ("left", wintypes.LONG),
                        ("top", wintypes.LONG),
                        ("right", wintypes.LONG),
                        ("bottom", wintypes.LONG),
                    ]

                class _MonitorInfo(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wintypes.DWORD),
                        ("rcMonitor", _Rect),
                        ("rcWork", _Rect),
                        ("dwFlags", wintypes.DWORD),
                    ]

                hwnd = wintypes.HWND(self.root.winfo_id())
                monitor_from_window = ctypes.windll.user32.MonitorFromWindow
                monitor_from_window.argtypes = [wintypes.HWND, wintypes.DWORD]
                monitor_from_window.restype = wintypes.HANDLE
                monitor = monitor_from_window(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
                if monitor:
                    monitor_info = _MonitorInfo()
                    monitor_info.cbSize = ctypes.sizeof(_MonitorInfo)
                    get_monitor_info = ctypes.windll.user32.GetMonitorInfoW
                    get_monitor_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)]
                    get_monitor_info.restype = wintypes.BOOL
                    if get_monitor_info(monitor, ctypes.byref(monitor_info)):
                        work = monitor_info.rcWork
                        left, top = int(work.left), int(work.top)
                        right, bottom = int(work.right), int(work.bottom)

                window_rect = _Rect()
                get_window_rect = ctypes.windll.user32.GetWindowRect
                get_window_rect.argtypes = [wintypes.HWND, ctypes.POINTER(_Rect)]
                get_window_rect.restype = wintypes.BOOL
                if get_window_rect(hwnd, ctypes.byref(window_rect)):
                    window_width = max(1, int(window_rect.right - window_rect.left))
                    window_height = max(1, int(window_rect.bottom - window_rect.top))
                else:
                    window_width = max(1, self.root.winfo_width())
                    window_height = max(1, self.root.winfo_height())
            else:
                window_width = max(1, self.root.winfo_width())
                window_height = max(1, self.root.winfo_height())

            work_width = max(1, right - left)
            work_height = max(1, bottom - top)
            x = left + max(0, (work_width - window_width) // 2)
            y = top + max(0, (work_height - window_height) // 2)
            self.root.geometry(f"+{x}+{y}")
        except Exception:
            # The initial geometry remains a safe fallback if a platform API
            # is unavailable or the window is already closing.
            pass

    def _unset_topmost(self):
        try:
            if hasattr(self, "root") and self.root:
                self.root.attributes("-topmost", False)
        except Exception:
            pass

    # ── Thread-safe live PIP Table Block update ──────────────
    def update_pip_table_block(self, table_text: str):
        """Thread-safe live update of the multithreaded pip progress table block."""
        def _do():
            if self._closed:
                return
            import tkinter as tk
            self.log.configure(state="normal")
            if "pip_table_start" in self.log.mark_names():
                self.log.delete("pip_table_start", "pip_table_end")
            else:
                self.log.insert("end", "\n")
                self.log.mark_set("pip_table_start", "end-1c")
                self.log.mark_gravity("pip_table_start", tk.LEFT)
                self.log.insert("end", "\n")
                self.log.mark_set("pip_table_end", "end-1c")
                self.log.mark_gravity("pip_table_end", tk.RIGHT)

            self.log.insert("pip_table_start", table_text + "\n", "pip_row")
            self.log.configure(state="disabled")
            if getattr(self, "_auto_scroll_var", None) and self._auto_scroll_var.get():
                self.log.see("pip_table_end")
        self.root.after(0, _do)

    # ── Thread-safe live PlatformIO package progress row ──────
    def update_platformio_progress_block(self, table_text: str):
        """Replace one live PlatformIO package-progress block in-place.

        PlatformIO redraws its own download/unpack percentages with carriage
        returns.  Mirroring that behavior in the Tk log prevents stale 10%/40%
        rows from remaining visible after the package has moved to another phase.
        """
        def _do():
            if self._closed:
                return
            import tkinter as tk
            self.log.configure(state="normal")
            marks = self.log.mark_names()
            if "platformio_progress_start" in marks and "platformio_progress_end" in marks:
                self.log.delete("platformio_progress_start", "platformio_progress_end")
            else:
                self.log.insert("end", "\n")
                self.log.mark_set("platformio_progress_start", "end-1c")
                self.log.mark_gravity("platformio_progress_start", tk.LEFT)
                self.log.insert("end", "\n")
                self.log.mark_set("platformio_progress_end", "end-1c")
                self.log.mark_gravity("platformio_progress_end", tk.RIGHT)

            self.log.insert("platformio_progress_start", table_text + "\n", "pip_row")
            self.log.configure(state="disabled")
            if getattr(self, "_auto_scroll_var", None) and self._auto_scroll_var.get():
                self.log.see("platformio_progress_end")
        self.root.after(0, _do)

    def clear_platformio_progress_block(self):
        """Remove the transient PlatformIO progress row before final phase logging."""
        def _do():
            if self._closed:
                return
            self.log.configure(state="normal")
            marks = self.log.mark_names()
            if "platformio_progress_start" in marks and "platformio_progress_end" in marks:
                self.log.delete("platformio_progress_start", "platformio_progress_end")
                try:
                    self.log.mark_unset("platformio_progress_start", "platformio_progress_end")
                except Exception:
                    pass
            self.log.configure(state="disabled")
        self.root.after(0, _do)

    # ── Spinner ───────────────────────────────────────────────
    def _tick_spinner(self):
        if self._closed:
            return
        if self._spinning:
            self._spin_var.set(self.SPINNER[self._spin_idx % len(self.SPINNER)])
            self._spin_idx += 1
        self.root.after(90, self._tick_spinner)

    def _tick_timer(self):
        if self._closed:
            return
        import time as _t_mod
        elapsed = int(_t_mod.time() - self._start_time)
        m, s = divmod(elapsed, 60)
        h, m = divmod(m, 60)
        if h > 0:
            t_str = f"⏱ {h:02d}:{m:02d}:{s:02d}"
        else:
            t_str = f"⏱ {m:02d}:{s:02d}"
        self._timer_var.set(t_str)
        self.root.after(1000, self._tick_timer)

    def stop_spinner(self, done_text: str = "Done", ok: bool = True):
        self._spinning = False
        self._spin_var.set("✔" if ok else "✖")
        self._status_var.set(done_text)
        if ok:
            self.set_step_progress(self.TOTAL_STEPS, self.TOTAL_STEPS)

    # ── Progress bar ───────────────────────────────────────────
    def _total_steps_safe(self) -> int:
        return getattr(self, "TOTAL_STEPS", 14)

    def _overall_progress_from_step_pct(self, step_pct: float) -> float:
        """Map 0..100% inside the current setup step to one monotonic 0..100 bar."""
        total = max(1, self._total_steps_safe())
        step_index = max(1, min(getattr(self, "_step_index", 1), total))
        step_fraction = max(0.0, min(100.0, float(step_pct))) / 100.0
        return max(0.0, min(100.0, ((step_index - 1) + step_fraction) * 100.0 / total))

    def set_step_progress(self, current: int, total: int):
        """Set completed top-level setup steps on the single bottom progress bar."""
        def _do():
            if self._closed:
                return
            self._busy_generation = getattr(self, "_busy_generation", 0) + 1
            self._progress.stop()
            resolved_total = max(1, self._total_steps_safe())
            value = max(0.0, min(100.0, float(current) * 100.0 / resolved_total))
            self._progress.configure(mode="determinate", maximum=100)
            self._progress["value"] = value
            self._current_step_pct = 100.0 if current >= resolved_total else 0.0
        self.root.after(0, _do)

    def set_progress_percent(self, pct: int | float):
        """Set progress inside the current setup step without resetting the whole bar."""
        def _do():
            if self._closed:
                return
            step_pct = max(0.0, min(100.0, float(pct)))
            self._current_step_pct = step_pct
            self._progress.stop()
            self._progress.configure(mode="determinate", maximum=100)
            self._progress["value"] = self._overall_progress_from_step_pct(step_pct)
        self.root.after(0, _do)

    def start_busy(self, start_pct: int | float | None = None, cap_pct: int | float = 92):
        """Smoothly fill the current step while work is active.

        This is a normal left-to-right determinate bar rather than a bouncing
        marquee.  A caller may reserve a sub-range for one install operation.
        """
        def _do():
            if self._closed:
                return

            self._busy_generation = getattr(self, "_busy_generation", 0) + 1
            generation = self._busy_generation
            self._progress.stop()
            self._progress.configure(mode="determinate", maximum=100)

            current = float(getattr(self, "_current_step_pct", 0.0))
            if start_pct is not None:
                current = max(current, float(start_pct))
            current = max(0.0, min(99.0, current))
            cap = max(current + 0.5, min(99.0, float(cap_pct)))
            self._current_step_pct = current

            def _tick():
                if self._closed or generation != getattr(self, "_busy_generation", 0):
                    return
                value = float(getattr(self, "_current_step_pct", current))
                if value < cap:
                    delta = max(0.20, min(1.20, (cap - value) * 0.08))
                    value = min(cap, value + delta)
                    self._current_step_pct = value
                    self._progress["value"] = self._overall_progress_from_step_pct(value)
                self.root.after(120, _tick)

            _tick()

        self.root.after(0, _do)

    def stop_busy(self, restore_step: bool = True):
        """Stop animation and leave the bar at its latest position."""
        def _do():
            if self._closed:
                return
            self._busy_generation = getattr(self, "_busy_generation", 0) + 1
            self._progress.stop()
            self._progress.configure(mode="determinate", maximum=100)
        self.root.after(0, _do)

    # ── Thread-safe log append ────────────────────────────────
    def _append(self, text: str, tag: str = "normal"):
        _record_bootstrap_log(tag.upper(), text)
        def _do():
            if self._closed:
                return
            self.log.configure(state="normal")
            self.log.insert("end", text + "\n", tag)
            self.log.configure(state="disabled")
            if getattr(self, "_auto_scroll_var", None) and self._auto_scroll_var.get():
                self.log.see("end")
        self.root.after(0, _do)

    def set_status(self, text: str):
        def _do():
            if not self._closed:
                self._status_var.set(text)
        self.root.after(0, _do)

    # ── Public logging API ────────────────────────────────────
    def log_banner(self):
        self._append("=" * 56, "section")
        self._append("  ⚡  MCU Uploader IDE by Naph — Bootstrap", "section")
        self._append("=" * 56, "section")
        self._append("")

    def log_section(self, title: str):
        """A top-level step (e.g. 'Checking pyserial'). Advances the
        overall step progress bar."""
        self._step_index += 1
        self._append(f"\n── {title} ──", "section")
        self.set_status(title)
        self.set_progress_percent(0)

    def log_subsection(self, title: str):
        """A nested sub-step (e.g. 'Installing pyserial') — same styling
        as log_section but doesn't advance the step counter, since several
        of these can happen inside a single top-level step."""
        self._append(f"\n── {title} ──", "section")
        self.set_status(title)

    def log_pip_line(self, line: str):
        """One line of raw pip output, shown dim so it doesn't compete
        visually with our own ok/warn/fail lines."""
        self._append(f"    {line}", "dim")

    def log_status(self, msg: str):
        self._append(f"  ▸ {msg}", "normal")

    def log_ok(self, msg: str):
        self._append(f"  ✔ {msg}", "ok")

    def log_warn(self, msg: str):
        self._append(f"  ⚠ {msg}", "warn")

    def log_fail(self, msg: str):
        self._append(f"  ✖ {msg}", "fail")

    def log_update_notice(self, pkg: str, current: str, latest: str):
        self._append(f"  ↑  {pkg}: {current} → {latest}", "update")

    def log_up_to_date(self, pkg: str, version: str):
        self._append(f"  ✔ {pkg} {version} is up to date", "dim")

    def log_dim(self, msg: str):
        self._append(f"  – {msg}", "dim")

    def ask_update(self, count: int) -> bool:
        """
        Show a modal Yes/No dialog asking whether to install updates.
        Returns True if the user clicks Yes.
        Must be called from the Tk main thread (or via after()).
        """
        import tkinter.messagebox as mb

        def _ask() -> bool:
            if self._closed:
                return False
            return mb.askyesno(
                "Updates Available",
                f"{count} update(s) are available.\n\nInstall them now?",
                parent=self.root,
            )

        if threading.current_thread() is threading.main_thread():
            return _ask()

        # Update checks run in the bootstrap worker. Tk dialogs must run on
        # the UI thread, so marshal the prompt and wait for the response.
        completed = threading.Event()
        answer = [False]

        def _on_ui_thread():
            try:
                answer[0] = _ask()
            finally:
                completed.set()

        try:
            self.root.after(0, _on_ui_thread)
            completed.wait()
        except Exception:
            return False
        return answer[0]

    def show_error(self, title: str, msg: str):
        import tkinter.messagebox as mb
        mb.showerror(title, msg, parent=self.root)

    def _on_close(self):
        # Prevent closing during setup — user must wait for completion
        # or explicitly cancel via the Cancel button if provided
        import tkinter.messagebox as mb
        mb.showwarning(
            "Setup in Progress",
            "The setup process is running and cannot be closed.\n\n"
            "Please wait for it to complete, or use the Cancel button if available.",
            parent=self.root,
        )

    def pump(self):
        """Process pending Tk events without blocking."""
        try:
            self.root.update()
        except Exception:
            pass

    def mainloop_until_done(self):
        """Run Tk event loop until destroy() is called."""
        try:
            self.root.mainloop()
        except Exception:
            pass

    def close_after_delay(self, delay_s: float | None = None):
        """
        Schedule self.close() to run on the Tk main thread after a delay
        so the user has a moment to read the final status line / see the
        spinner land on ✔ before the window disappears.

        delay_s=None falls back to BOOTSTRAP_CLOSE_DELAY_S. Pass 0 to
        close immediately. Safe to call from any thread.
        """
        if delay_s is None:
            delay_s = BOOTSTRAP_CLOSE_DELAY_S
        delay_ms = max(0, int(float(delay_s) * 1000))

        def _do():
            self.close()

        try:
            self.root.after(delay_ms, _do)
        except Exception:
            # Tk is already gone (e.g. user clicked the X) — nothing to do.
            pass

    def close(self):
        if not self._closed:
            self._closed = True
            self._spinning = False
            try:
                self.root.withdraw()
            except Exception:
                pass
            # Disable window protocol to prevent callback loops during destroy
            try:
                self.root.protocol("WM_DELETE_WINDOW", lambda: None)
            except Exception:
                pass
            try:
                self.root.quit()
            except Exception:
                pass
            try:
                self.root.destroy()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
# Console helpers — proxy to GUI when live, else plain print
# ─────────────────────────────────────────────────────────────
def banner():
    if _gui:
        _gui.log_banner()
    else:
        _record_bootstrap_log("SECTION", "MCU Uploader IDE by Naph - Bootstrap")
        os.system("")
        print(f"\n{CYAN}{BOLD}{'=' * 56}")
        print("  ⚡  MCU Uploader IDE by Naph — Bootstrap")
        print(f"{'=' * 56}{RESET}\n")

def status(msg: str, color: str = CYAN):
    if _gui:
        _gui.log_status(msg)
    else:
        _record_bootstrap_log("STATUS", msg)
        print(f"  {color}▸{RESET} {msg}")

def ok(msg: str):
    if _gui:
        _gui.log_ok(msg)
    else:
        _record_bootstrap_log("OK", msg)
        print(f"  {GREEN}✔{RESET} {msg}")

def warn(msg: str):
    if _gui:
        _gui.log_warn(msg)
    else:
        _record_bootstrap_log("WARN", msg)
        print(f"  {YELLOW}⚠{RESET} {msg}")

def fail(msg: str):
    if _gui:
        _gui.log_fail(msg)
    else:
        _record_bootstrap_log("FAIL", msg)
        print(f"  {RED}✖{RESET} {msg}")

def section(title: str):
    if _gui:
        _gui.log_subsection(title)
    else:
        _record_bootstrap_log("SECTION", title)
        print(f"\n  {CYAN}{BOLD}── {title} ──{RESET}")

def update_notice(pkg: str, current: str, latest: str):
    if _gui:
        _gui.log_update_notice(pkg, current, latest)
    else:
        _record_bootstrap_log("UPDATE", f"{pkg}: {current} -> {latest}")
        print(f"  {YELLOW}↑{RESET}  {BOLD}{pkg}{RESET}: {DIM}{current}{RESET} → {GREEN}{latest}{RESET}")

def up_to_date(pkg: str, version: str):
    if _gui:
        _gui.log_up_to_date(pkg, version)
    else:
        _record_bootstrap_log("OK", f"{pkg} {version} is up to date")
        print(f"  {GREEN}✔{RESET} {pkg} {DIM}{version}{RESET} is up to date")


def dim(msg: str):
    """Log an informational line in either GUI or console mode."""
    if _gui:
        _gui.log_dim(msg)
    else:
        _record_bootstrap_log("INFO", msg)
        print(f"  {DIM}-{RESET} {msg}")


# ═══════════════════════════════════════════════════════════════
# UPDATE CHECKER
# ═══════════════════════════════════════════════════════════════

def _fetch_url(url: str, timeout: int = 8) -> str | None:
    """Fetch a URL, return body text or None on any error."""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "mcu-flash-gui-bootstrap/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _is_network_reachable(timeout: float = 2.0) -> bool:
    """Best-effort check: can we open a TCP socket to pypi.org:443 within `timeout`?

    Used to short-circuit a fan-out of online update probes when the network is
    clearly unavailable.  A single lightweight probe beats letting 8-9 probes
    each wait out their own 8-20s socket timeout."""
    try:
        import socket
        with socket.create_connection(("pypi.org", 443), timeout=timeout):
            return True
    except Exception:
        return False


def _pip_installed_version(pkg_import: str, pkg_name: str) -> str | None:
    """Return a package version without starting another Python process."""
    try:
        from importlib import metadata
        return metadata.version(pkg_name)
    except Exception:
        pass
    return None


def _pip_latest_version(pkg_name: str) -> str | None:
    """Query PyPI JSON API for the latest stable release of a package."""
    body = _fetch_url(f"https://pypi.org/pypi/{pkg_name}/json")
    if not body:
        return None
    try:
        data = json.loads(body)
        return data["info"]["version"]
    except Exception:
        return None


def _pip_upgrade(pkg_name: str) -> bool:
    """Upgrade a pip package, return True on success."""
    return _run_pip_install(["--upgrade", pkg_name])



def _version_tuple(v: str) -> tuple:
    """Convert '1.2.3' to (1, 2, 3) for comparison, ignoring non-numeric parts."""
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check_pip_package_update(pkg_name: str, pkg_import: str | None = None) -> dict:
    """
    Check one pip package for updates.
    Returns: {"name": str, "installed": str|None, "latest": str|None,
               "update_available": bool, "error": str|None}
    """
    installed = _pip_installed_version(pkg_import or pkg_name, pkg_name)
    if installed is None:
        return {"name": pkg_name, "installed": None, "latest": None,
                "update_available": False, "error": "not installed"}

    latest = _pip_latest_version(pkg_name)
    if latest is None:
        return {"name": pkg_name, "installed": installed, "latest": None,
                "update_available": False, "error": "could not reach PyPI"}

    update_available = _version_tuple(latest) > _version_tuple(installed)
    return {"name": pkg_name, "installed": installed, "latest": latest,
            "update_available": update_available, "error": None}


def _arduino_cli_installed_version() -> str | None:
    """Return the installed arduino-cli version string, e.g. '1.1.1'."""
    cli = find_arduino_cli()
    if not cli:
        return None
    try:
        result = subprocess.run(
            [cli, "version"],
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        # Output looks like: "arduino-cli  Version: 1.1.1 Commit: ..."
        import re
        m = re.search(r"Version:\s*([\d.]+)", result.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _arduino_cli_latest_version() -> str | None:
    """
    Query the latest version of arduino-cli.
    """
    # ── Strategy 1: GitHub releases redirect URL (extremely reliable & non-rate-limited) ──
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://github.com/arduino/arduino-cli/releases/latest",
            headers={"User-Agent": "mcu-flash-gui-bootstrap/1.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            final_url = r.geturl()
            # final_url looks like: https://github.com/arduino/arduino-cli/releases/tag/v1.5.1
            tag = final_url.split("/")[-1]
            if tag and tag.startswith("v"):
                return tag.lstrip("v")
    except Exception:
        pass

    # ── Strategy 2: arduino-cli upgrade (fallback) ──
    cli = find_arduino_cli()
    if cli:
        try:
            result = subprocess.run(
                [cli, "upgrade"],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            # "arduino-cli X.Y.Z -> A.B.C" or "already up to date"
            import re
            m = re.search(r"(\d+\.\d+[\.\d]*)\s*$", result.stdout)
            if m:
                return m.group(1).strip()
        except Exception:
            pass

    # ── Strategy 3: GitHub releases API (fallback) ──
    body = _fetch_url("https://api.github.com/repos/arduino/arduino-cli/releases/latest")
    if body:
        try:
            data = json.loads(body)
            tag = data.get("tag_name", "")          # e.g. "v1.1.1"
            if tag:
                return tag.lstrip("v")
        except Exception:
            pass

    return None


def check_arduino_cli_update() -> dict:
    """Check arduino-cli for updates, tolerating unreachable GitHub."""
    installed = _arduino_cli_installed_version()
    if installed is None:
        return {"name": "arduino-cli", "installed": None, "latest": None,
                "update_available": False, "error": "not installed"}

    latest = _arduino_cli_latest_version()
    if latest is None:
        # Network is simply unreachable — not an error worth printing,
        # just skip silently so the bootstrap doesn't scare users.
        return {"name": "arduino-cli", "installed": installed, "latest": None,
                "update_available": False, "error": "network unavailable — skipping"}

    update_available = _version_tuple(latest) > _version_tuple(installed)
    return {"name": "arduino-cli", "installed": installed, "latest": latest,
            "update_available": update_available, "error": None}


def _pio_installed_version() -> str | None:
    """Return the installed platformio version string, e.g. '6.1.16'."""
    try:
        from importlib import metadata
        return metadata.version("platformio")
    except Exception:
        pass

    pio = find_pio()
    if not pio:
        return None
    try:
        cmd = list(pio)
        cmd.append("--version")
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        import re
        m = re.search(r"version\s*([\d.]+)", result.stdout, re.IGNORECASE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _pio_upgrade() -> bool:
    """Upgrade PlatformIO Core using its built-in upgrade command."""
    pio = find_pio()
    if not pio:
        return False
    try:
        cmd = list(pio)
        cmd.append("upgrade")
        subprocess.check_call(
            cmd,
            stdout=sys.stdout, stderr=sys.stderr,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return True
    except Exception:
        return False


def check_pio_update() -> dict:
    """Check PlatformIO for updates."""
    installed = _pio_installed_version()
    if installed is None:
        return {"name": "platformio", "installed": None, "latest": None,
                "update_available": False, "error": "not installed"}

    latest = _pip_latest_version("platformio")
    if latest is None:
        return {"name": "platformio", "installed": installed, "latest": None,
                "update_available": False, "error": "could not reach PyPI"}

    update_available = _version_tuple(latest) > _version_tuple(installed)
    return {"name": "platformio", "installed": installed, "latest": latest,
            "update_available": update_available, "error": None}


def check_python_update() -> dict:
    """Check if a newer version of Python is available on winget."""
    import sys
    current_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    
    if sys.platform != "win32" or not shutil.which("winget"):
        return {"name": "python", "installed": current_ver, "latest": current_ver,
                "update_available": False, "error": None}
                
    try:
        # Run winget search silently
        import subprocess as sp
        res = sp.run(
            ["winget", "search", "Python.Python"],
            capture_output=True, text=True, timeout=20, shell=False,
            creationflags=sp.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        if res.returncode == 0:
            highest_minor = sys.version_info.minor
            highest_id = None
            for line in res.stdout.splitlines():
                if "Python.Python.3." in line:
                    parts = line.split()
                    for part in parts:
                        if part.startswith("Python.Python.3."):
                            try:
                                minor_ver = int(part.split(".")[-1])
                                if minor_ver > highest_minor:
                                    highest_minor = minor_ver
                                    highest_id = part
                            except Exception:
                                pass
            if highest_id:
                latest_ver_str = f"3.{highest_minor}"
                return {
                    "name": "python",
                    "installed": current_ver,
                    "latest": latest_ver_str,
                    "update_available": True,
                    "error": None,
                    "package_id": highest_id,
                }
    except FileNotFoundError:
        return {"name": "python", "installed": current_ver, "latest": current_ver,
                "update_available": False, "error": None}
    except Exception as e:
        return {"name": "python", "installed": current_ver, "latest": None,
                "update_available": False, "error": f"check failed: {e}"}
                
    return {"name": "python", "installed": current_ver, "latest": current_ver,
            "update_available": False, "error": None}


def _install_python_update(package_id: str) -> bool:
    """Install the newer Python package discovered through winget."""
    if sys.platform != "win32" or not package_id:
        return False
    status(f"Installing {package_id} with Windows Package Manager...")
    try:
        result = subprocess.run(
            [
                "winget", "install", "--id", package_id, "--exact", "--scope", "user",
                "--override", "/passive Include_tcltk=1 PrependPath=1 Include_test=0",
                "--accept-package-agreements", "--accept-source-agreements",
                "--disable-interactivity",
            ],
            capture_output=True, text=True, timeout=900,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode in (0, 2316632107, -1978335205):
            ok(f"{package_id} is installed or already up to date.")
            return True
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        warn(f"winget could not install {package_id} (exit {result.returncode}).")
        if detail:
            warn(detail[-1])
    except FileNotFoundError:
        warn("winget is not available on this Windows installation.")
    except subprocess.TimeoutExpired:
        warn("Python installation timed out; it may still be running in the background.")
    except Exception as exc:
        warn(f"Could not install Python update: {exc}")
    return False


_UPDATE_CHECK_PACKAGES = [
    "python", "pip", "pyserial", "psutil", "pywebview",
    "pywinpty", "websockets", "esptool", "platformio", "arduino-cli",
]


def _render_update_summary_block(results: dict[str, dict], *, state_fallback: str | None = None):
    """
    Render an always-shown summary block listing every managed package and
    its state (up-to-date / update-available / errored / not-installed).

    *results* maps package-key → result dict (the same shape produced by the
    *check_* functions).  When *state_fallback* is set, every package whose
    key is missing from *results* is logged with that fallback state — used so
    the skip / offline early-return paths still print a full per-package list
    instead of a single line.

    Returns the list of result dicts for which an update is available, so the
    caller can proceed to the install prompt / upgrade logic.
    """
    order = list(_UPDATE_CHECK_PACKAGES)
    if sys.platform == "win32":
        order.append("pywin32")

    updates_found: list[dict] = []

    summary_header = "Update check results:"
    if _gui:
        _gui.log_dim(summary_header)
    else:
        _record_bootstrap_log("INFO", summary_header)
        print(f"  {DIM}-{RESET} {summary_header}")

    for key in order:
        r = results.get(key)

        if r is None:
            if state_fallback:
                dim(f"{key}: {state_fallback}")
            else:
                dim(f"{key}: not checked (no result)")
            continue

        if r.get("error") == "not installed":
            dim(f"{key} is not installed; skipping its update check.")
            continue
        if r.get("error"):
            if "network unavailable" in (r["error"] or "").lower() or \
               "skipping" in (r["error"] or "").lower():
                dim(f"{key}: {r['error']}")
            else:
                warn(f"{key}: {r['error']}")
            continue
        if r.get("update_available"):
            update_notice(key, r["installed"], r["latest"])
            updates_found.append(r)
        else:
            up_to_date(key, r["installed"])

    return updates_found


def run_update_checks(auto_update: bool = False):
    """
    Check all managed utilities for updates, using a small bounded worker pool.

    Startup calls this only when the Skip Updates checkbox is clear. The work
    uses at most three network threads and reads installed package versions in
    process, so it does not recreate the older many-python-process problem.

    If auto_update=True, upgrade pip packages automatically (arduino-cli
    requires a manual MSI re-run, so we only notify for that one).
    """
    skip_reason = _update_check_skip_reason()
    if skip_reason:
        warn(f"Online update checks skipped: {skip_reason}.")
        _render_update_summary_block(
            {},
            state_fallback=f"check skipped ({skip_reason})",
        )
        return "skipped"

    section("Checking for updates")
    status("Update checks enabled. Querying PyPI, GitHub, and winget...", DIM)

    # Fast offline check: if we can't even reach pypi.org:443 in 2 seconds,
    # every probe below will just sit in its socket timeout (8-20s each).
    # Bail with a single log line so the bootstrap window proceeds.
    if not _is_network_reachable(timeout=2.0):
        dim("Offline detected; online update checks were not run.")
        _render_update_summary_block(
            {},
            state_fallback="not checked (offline)",
        )
        return "offline"

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[str, dict] = {}

    checks = [
        ("python",     lambda: check_python_update()),
        ("pip",        lambda: check_pip_package_update("pip")),
        ("pyserial",   lambda: check_pip_package_update("pyserial")),
        ("psutil",     lambda: check_pip_package_update("psutil")),
        ("pywebview",  lambda: check_pip_package_update("pywebview", "webview")),
        ("pywinpty",   lambda: check_pip_package_update("pywinpty", "winpty")),
        ("websockets", lambda: check_pip_package_update("websockets")),
        ("esptool",    lambda: check_pip_package_update("esptool")),
        ("platformio", lambda: check_pio_update()),
        ("arduino-cli",lambda: check_arduino_cli_update()),
    ]
    if sys.platform == "win32":
        checks.append(("pywin32", lambda: check_pip_package_update("pywin32")))

    # Keep this deliberately small for older/low-memory machines. Package
    # versions are read in-process, so the pool only covers network requests.
    with ThreadPoolExecutor(
        max_workers=min(3, len(checks)),
        thread_name_prefix="MCUUpdateCheck",
    ) as executor:
        futures = {executor.submit(fn): key for key, fn in checks}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                results[key] = {
                    "name": key,
                    "installed": None,
                    "latest": None,
                    "update_available": False,
                    "error": f"check failed: {exc}",
                }

    # ── Display results (always shows per-package summary block) ──
    updates_found = _render_update_summary_block(results)

    if not updates_found:
        ok("All utilities are up to date.")
        return "up_to_date"

    # ── Prompt / auto-update ─────────────────────────────────
    pip_updates  = [r for r in updates_found if r["name"] not in ("arduino-cli", "python")]
    cli_updates  = [r for r in updates_found if r["name"] == "arduino-cli"]
    python_updates = [r for r in updates_found if r["name"] == "python"]

    if auto_update:
        answer = "y"
    elif _gui:
        # GUI path: ask via modal dialog on the main thread
        answer = "y" if _gui.ask_update(len(updates_found)) else "n"
    else:
        import threading
        if threading.current_thread().name == "fast-path-update-check":
            try:
                import tkinter as tk
                import tkinter.messagebox as mb
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                ans = mb.askyesno(
                    "Updates Available",
                    f"{len(updates_found)} update(s) are available for MCU Flash GUI components.\n\n"
                    "Would you like to install them now?",
                    parent=root
                )
                answer = "y" if ans else "n"
                root.destroy()
            except Exception:
                answer = "n"
        else:
            try:
                answer = input(
                    f"  {YELLOW}▸{RESET} {len(updates_found)} update(s) available. "
                    f"Install now? [{GREEN}y{RESET}/{RED}n{RESET}]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
                dim("Update installation prompt dismissed.")

    if answer == "y":
        for r in pip_updates:
            if r["name"] == "platformio":
                status(f"Upgrading platformio {r['installed']} → {r['latest']}...")
                if _pio_upgrade():
                    ok(f"platformio upgraded to {r['latest']}")
                else:
                    warn("Failed to upgrade platformio — continuing")
            else:
                status(f"Upgrading {r['name']} {r['installed']} → {r['latest']}...")
                if _pip_upgrade(r["name"]):
                    ok(f"{r['name']} upgraded to {r['latest']}")
                else:
                    warn(f"Failed to upgrade {r['name']} — continuing")

        if python_updates:
            r = python_updates[0]
            if _install_python_update(r.get("package_id", "")):
                # Purge old env virtual environment folder so the new Python version builds a fresh env
                (SCRIPT_DIR / ".force_rebuild").touch()
                try:
                    shutil.rmtree(SCRIPT_DIR / "env", ignore_errors=True)
                except Exception:
                    pass

                ok(f"Python updated ({r['installed']} → {r['latest']}). Purged old environment.")
                
                # Release single-instance lock slot before restarting
                _release_bootstrap_slot()
                
                try:
                    import tkinter as tk
                    import tkinter.messagebox as mb
                    root = tk.Tk()
                    root.withdraw()
                    root.attributes("-topmost", True)
                    mb.showinfo(
                        "Python Updated — Restarting",
                        f"Python has been updated from {r['installed']} to {r['latest']}.\n\n"
                        "The old environment was removed and the application will now restart automatically with the new Python version.",
                        parent=root
                    )
                    root.destroy()
                except Exception:
                    pass
                
                # Relaunch runThisOnWindows.vbs and exit current process
                vbs_launcher = SCRIPT_DIR / "direct" / "runThisOnWindows.vbs"
                if not vbs_launcher.exists():
                    vbs_launcher = SCRIPT_DIR / "runThisOnWindows.vbs"
                if vbs_launcher.exists():
                    try:
                        subprocess.Popen(["wscript.exe", str(vbs_launcher)], cwd=str(SCRIPT_DIR))
                    except Exception:
                        pass
                os._exit(0)
            else:
                warn("Python was not updated; the existing environment will be kept.")

        if cli_updates:
            r = cli_updates[0]
            cli_path = find_arduino_cli()
            upgraded = False

            # ── Strategy A: arduino-cli upgrade (built-in self-updater) ──
            if cli_path:
                status(f"Running arduino-cli upgrade {r['installed']} -> {r['latest']}...")
                try:
                    subprocess.check_call(
                        [cli_path, "upgrade"],
                        stdout=sys.stdout, stderr=sys.stderr,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                    ok(f"arduino-cli upgraded to {r['latest']}")
                    upgraded = True
                except Exception:
                    warn("arduino-cli upgrade command failed — falling back to MSI re-install...")

            # ── Strategy B: download fresh MSI then msiexec ──────────────
            if not upgraded:
                msi_path = SCRIPT_DIR / "installers" / "arduino-cli.msi"
                if _refresh_bundled_msi(r["latest"]):
                    if _run_arduino_cli_msi(msi_path):
                        ok(f"arduino-cli upgraded to {r['latest']} via MSI")
                        upgraded = True
                    else:
                        warn(
                            f"MSI install failed. Download manually:\n"
                            f"    {CYAN}https://arduino.github.io/arduino-cli/latest/installation/{RESET}"
                        )
                else:
                    warn(
                        f"Could not download MSI. Download manually:\n"
                        f"    {CYAN}https://arduino.github.io/arduino-cli/latest/installation/{RESET}"
                    )

            # ── Always refresh the bundled MSI copy ───────────────────────
            # Even when the self-updater succeeded, keep installers/arduino-cli.msi
            # current so the next fresh-machine install gets the new version.
            if upgraded:
                _refresh_bundled_msi(r["latest"])
                new_cli = find_arduino_cli()
                if new_cli:
                    _cache_arduino_cli_path(new_cli)
        
        import threading
        if threading.current_thread().name == "fast-path-update-check":
            try:
                import tkinter as tk
                import tkinter.messagebox as mb
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                mb.showinfo(
                    "Updates Installed",
                    "Updates for MCU Flash GUI components have been installed successfully.\n\n"
                    "Please restart the application to apply the updates.",
                    parent=root
                )
                root.destroy()
            except Exception:
                pass
    else:
        status("Skipping updates.", DIM)

    return "updates_available"


# ═══════════════════════════════════════════════════════════════
# ENSURE FUNCTIONS (install if missing)
# ═══════════════════════════════════════════════════════════════

# ── 1. Ensure pip ────────────────────────────────────────────
def ensure_pip() -> bool:
    """Make sure pip is available in the current Python."""
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if res.returncode == 0:
            ok("pip already installed in target environment")
            return True
    except Exception:
        pass

    section("Installing pip")
    status("pip not found in target environment, bootstrapping...")

    # Fast path: copy pre-installed pip/setuptools from base Python into target venv site-packages
    try:
        base_prefix = Path(getattr(sys, "base_prefix", sys.prefix))
        exec_path = Path(sys.executable).resolve()
        
        if sys.platform == "win32":
            base_site = base_prefix / "Lib" / "site-packages"
            target_site = exec_path.parent.parent / "Lib" / "site-packages"
        else:
            base_site = base_prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
            target_site = exec_path.parent.parent / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"

        if base_site.is_dir() and target_site.is_dir() and base_site.resolve() != target_site.resolve():
            status("Copying pip packages from base Python...")
            for item in base_site.glob("*"):
                name_lower = item.name.lower()
                if any(k in name_lower for k in ("pip", "setuptools", "wheel", "distutils", "pkg_resources")):
                    t = target_site / item.name
                    if not t.exists():
                        if item.is_dir():
                            shutil.copytree(item, t, dirs_exist_ok=True)
                        else:
                            shutil.copy2(item, t)
            
            # Test if pip works now
            res = subprocess.run(
                [sys.executable, "-m", "pip", "--version"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if res.returncode == 0:
                ok("pip bootstrapped from base Python")
                return True
    except Exception:
        pass

    # Try ensurepip with strict timeout
    if _gui:
        _gui.start_busy()
    try:
        res = subprocess.run(
            [sys.executable, "-m", "ensurepip", "--upgrade"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if res.returncode == 0:
            ok("pip installed via ensurepip")
            return True
    except Exception:
        pass
    finally:
        if _gui:
            _gui.stop_busy(restore_step=True)

    # For embeddable Python: need to enable pip by editing pth file
    # and downloading get-pip.py
    python_dir = Path(sys.executable).parent
    pth_files = list(python_dir.glob("python*._pth"))
    for pth in pth_files:
        content = pth.read_text(encoding="utf-8")
        if "#import site" in content:
            status("Enabling site-packages in embeddable Python...")
            content = content.replace("#import site", "import site")
            pth.write_text(content, encoding="utf-8")
            ok("Enabled import site in " + pth.name)

    # Prepare get-pip.py
    get_pip = python_dir / "get-pip.py"
    if not get_pip.exists():
        status("Preparing get-pip.py...")
        url = "https://bootstrap.pypa.io/get-pip.py"
        try:
            _download_file(url, get_pip)
        except Exception as e:
            fail(f"Failed to prepare get-pip.py: {e}")
            return False

    if _gui:
        _gui.start_busy()
    try:
        subprocess.run(
            [sys.executable, str(get_pip)],
            stdout=sys.stdout, stderr=sys.stderr,
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            check=True,
        )
        ok("pip installed via get-pip.py")
        return True
    except Exception as e:
        fail(f"get-pip.py failed: {e}")
        return False
    finally:
        if _gui:
            _gui.stop_busy(restore_step=True)


# ── 2. Ensure pyserial ───────────────────────────────────────
def upgrade_pip() -> bool:
    """Update pip after it has been bootstrapped successfully."""
    section("Updating pip")
    status("Installing the latest compatible pip...")
    if _run_pip_install(["--upgrade", "pip"]):
        ok("pip is up to date")
        return True
    warn("Could not update pip; continuing with the installed version.")
    return False


def ensure_pyserial() -> bool:
    try:
        import serial  # noqa: F401
        import serial.tools.list_ports  # noqa: F401
        ok("pyserial already installed")
        return True
    except ImportError:
        pass

    status("pyserial not found, installing via pip...")

    section("Installing pyserial")
    try:
        if not _run_pip_install(["pyserial"]):
            raise RuntimeError("pip exited with a non-zero status")
        ok("pyserial installed successfully")
        return True
    except Exception as e:
        fail(f"Failed to install pyserial: {e}")
        return False


# ── 2b. Ensure psutil ────────────────────────────────────────
def ensure_psutil() -> bool:
    try:
        import psutil  # noqa: F401
        ok("psutil already installed")
        return True
    except ImportError:
        pass

    status("psutil not found, installing via pip...")

    section("Installing psutil")
    try:
        if not _run_pip_install(["psutil"]):
            raise RuntimeError("pip exited with a non-zero status")
        ok("psutil installed successfully")
        return True
    except Exception as e:
        fail(f"Failed to install psutil: {e}")
        return False


# ── 2c. Ensure pywin32 (Windows only) ─────────────────────────
def ensure_pywin32() -> bool:
    """pywin32 (win32gui/win32con) is used on Windows to embed the code
    editor's native window directly inside the main GUI window instead of
    it opening as a separate floating window. It is required for the supported
    Windows embedded-editor experience."""
    if sys.platform != "win32":
        return True

    try:
        import win32gui  # noqa: F401
        import win32con  # noqa: F401
        ok("pywin32 already installed")
        return True
    except ImportError:
        pass

    status("pywin32 not found, installing via pip...")

    section("Installing pywin32")
    try:
        if not _run_pip_install(["pywin32"]):
            raise RuntimeError("pip exited with a non-zero status")
        ok("pywin32 installed successfully")
        return True
    except Exception as e:
        fail(f"Failed to install pywin32: {e}")
        return False


# ── 2c. Ensure esptool ───────────────────────────────────────
def ensure_esptool() -> bool:
    try:
        # pyrefly: ignore [missing-import]
        import esptool
        ok("esptool already installed")
        return True
    except ImportError:
        pass

    status("esptool not found, installing via pip...")

    section("Installing esptool")
    try:
        if not _run_pip_install(["esptool"]):
            raise RuntimeError("pip exited with a non-zero status")
        ok("esptool installed successfully")
        return True
    except Exception as e:
        fail(f"Failed to install esptool: {e}")
        return False


# ── 2d. Ensure pywebview ───────────────────────────────────────
def ensure_pywebview() -> bool:
    try:
        # pyrefly: ignore [missing-import]
        import webview  # noqa: F401
        ok("pywebview already installed")
        return True
    except ImportError:
        pass

    status("pywebview not found, installing via pip...")

    section("Installing pywebview")
    try:
        if not _run_pip_install(["pywebview"]):
            raise RuntimeError("pip exited with a non-zero status")
        ok("pywebview installed successfully")
        return True
    except Exception as e:
        fail(f"Failed to install pywebview: {e}")
        return False


# ── 2e. Ensure PyQt5 + QScintilla ─────────────────────────────
# ── Multithreaded Parallel Pip Package Manager ────────────────
# Fast import check using importlib.util.find_spec (no actual import overhead)
def _check_spec(import_name: str) -> bool:
    return importlib.util.find_spec(import_name) is not None

def _check_import_pyserial() -> bool:
    return _check_spec("serial") and _check_spec("serial.tools.list_ports")

def _check_import_psutil() -> bool:
    return _check_spec("psutil")

def _check_import_pywin32() -> bool:
    """Verify pywin32 in a fresh interpreter so pywin32.pth is processed."""
    if sys.platform != "win32":
        return True
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import win32gui, win32con"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.returncode == 0
    except Exception:
        return False

def _check_import_esptool() -> bool:
    return _check_spec("esptool")

def _check_import_pywebview() -> bool:
    """Verify pywebview can actually import in a fresh interpreter.

    ``find_spec`` can report a package as installed even when one of its
    startup dependencies is broken.  Monaco cannot use that half-installed
    state, so Bootstrap verifies the real import before continuing.
    """
    if not _check_spec("webview"):
        return False
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            [sys.executable, "-c", "import webview; assert webview is not None"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            creationflags=creationflags,
        )
        return result.returncode == 0
    except Exception:
        return False

def _check_import_pywinpty() -> bool:
    if sys.platform != "win32":
        return True
    return _check_spec("winpty")

def _check_import_websockets() -> bool:
    return _check_spec("websockets")

def _check_import_pyqt5_qscintilla() -> bool:
    return _check_spec("PyQt5.QtWidgets") and _check_spec("PyQt5.Qsci")

def _check_import_certifi() -> bool:
    return _check_spec("certifi")

def _check_import_platformio() -> bool:
    return find_pio() is not None

PIP_PACKAGES_SPEC = [
    {
        "id": "pyserial",
        "name": "pyserial",
        "check": _check_import_pyserial,
        "pip_args": ["pyserial"],
        "critical": True,
    },
    {
        "id": "psutil",
        "name": "psutil",
        "check": _check_import_psutil,
        "pip_args": ["psutil"],
        "critical": True,
    },
    {
        "id": "pywin32",
        "name": "pywin32",
        "check": _check_import_pywin32,
        "pip_args": ["pywin32"],
        "critical": True,
    },
    {
        "id": "esptool",
        "name": "esptool",
        "check": _check_import_esptool,
        "pip_args": ["esptool"],
        "critical": True,
    },
    {
        "id": "pywebview",
        "name": "pywebview",
        "check": _check_import_pywebview,
        "pip_args": ["pywebview"],
        "critical": True,
    },
    {
        "id": "pywinpty",
        "name": "pywinpty",
        "check": _check_import_pywinpty,
        "pip_args": ["pywinpty"],
        "critical": True,
    },
    {
        "id": "websockets",
        "name": "websockets",
        "check": _check_import_websockets,
        "pip_args": ["websockets"],
        "critical": True,
    },
    {
        "id": "certifi",
        "name": "certifi",
        "check": _check_import_certifi,
        "pip_args": ["certifi"],
        "critical": True,
    },
    {
        "id": "pyqt5_qscintilla",
        "name": "PyQt5 / QScintilla",
        "check": _check_import_pyqt5_qscintilla,
        "pip_args": ["PyQt5", "QScintilla"],
        "critical": True,
    },
    {
        "id": "platformio",
        "name": "PlatformIO Core",
        "check": _check_import_platformio,
        "pip_args": ["platformio"],
        "critical": True,
    },
]


def ensure_pip_packages_parallel(
    gui: Optional[BootstrapGUI] = None,
    *,
    package_ids: set[str] | None = None,
    include_optional: bool = False,
) -> bool:
    """Install missing Python dependencies with safe adaptive parallelism.

    The bootstrap never runs multiple ``pip install`` processes against the
    same environment.  That can race on site-packages, .dist-info metadata,
    console scripts, and shared transitive dependencies.

    Instead it uses this portable strategy:

      1. check installed top-level packages in parallel;
      2. download missing packages in parallel into isolated worker folders;
      3. merge the downloaded artifacts into one temporary wheelhouse;
      4. run ONE pip install transaction against the environment;
      5. verify every package in a fresh/appropriate interpreter context;
      6. repair only packages that still fail, one at a time without cache.

    Adaptive worker count is deliberately conservative so this remains stable
    on old/low-power machines as well as modern desktops:
        1-2 logical CPUs -> 1 download worker
        3-5 logical CPUs -> 2 download workers
        6+ logical CPUs  -> 3 download workers (hard cap)

    The GUI keeps the original per-dependency 30-cell progress bars.  Raw pip
    resolver output is hidden unless an operation fails.

    ``package_ids`` is reserved for feature-triggered installs.  The complete
    bootstrap call passes ``include_optional=True`` so every declared package
    is installed and verified before the main GUI is launched.
    """
    import concurrent.futures
    import tempfile
    import threading

    if package_ids is not None:
        requested_ids = {str(value).strip().lower() for value in package_ids}
        active_specs = [
            spec for spec in PIP_PACKAGES_SPEC
            if str(spec.get("id", "")).lower() in requested_ids
            and not (spec["id"] == "pywin32" and sys.platform != "win32")
        ]
    else:
        active_specs = [
            spec for spec in PIP_PACKAGES_SPEC
            if (include_optional or spec.get("critical", True))
            and not (spec["id"] == "pywin32" and sys.platform != "win32")
        ]

    if not active_specs:
        return True

    table_lock = threading.RLock()
    pkg_states = {
        spec["id"]: {
            "name": spec["name"],
            "status": "⏳ Checking...",
            "pct": 0,
            "done": False,
            "ok": False,
        }
        for spec in active_specs
    }

    def _render_table():
        if not gui or getattr(gui, "_closed", False):
            return
        divider = "  " + "─" * 52
        rows = [divider]
        for spec in active_specs:
            state = pkg_states[spec["id"]]
            pct = max(0, min(100, int(round(state["pct"]))))
            filled = int(pct / 100 * 30)
            bar = "▰" * filled + "▱" * (30 - filled)
            rows.append(f"  {state['name']:<22}  {state['status']}")
            rows.append(f"  {bar}  {pct:3d}%")
            rows.append(divider)
        gui.update_pip_table_block("\n".join(rows))

    def _set_state(spec_id, *, status_text=None, pct=None, done=None, ok_value=None):
        changed = False
        with table_lock:
            state = pkg_states[spec_id]
            if status_text is not None and status_text != state["status"]:
                state["status"] = status_text
                changed = True
            if pct is not None:
                new_pct = max(0, min(100, int(round(pct))))
                # Never move a visible package bar backward.
                new_pct = max(new_pct, int(state["pct"]))
                if new_pct != state["pct"]:
                    state["pct"] = new_pct
                    changed = True
            if done is not None and bool(done) != state["done"]:
                state["done"] = bool(done)
                changed = True
            if ok_value is not None and bool(ok_value) != state["ok"]:
                state["ok"] = bool(ok_value)
                changed = True
            if changed:
                _render_table()

    def _set_all(specs, status_text, pct):
        with table_lock:
            changed = False
            for spec in specs:
                state = pkg_states[spec["id"]]
                if state["done"]:
                    continue
                if status_text != state["status"]:
                    state["status"] = status_text
                    changed = True
                new_pct = max(int(state["pct"]), int(pct))
                if new_pct != state["pct"]:
                    state["pct"] = new_pct
                    changed = True
            if changed:
                _render_table()

    def _check(spec):
        try:
            importlib.invalidate_caches()
            return bool(spec["check"]())
        except Exception:
            return False

    def _update_bottom_from_rows():
        if not gui or not active_specs:
            return
        with table_lock:
            # This is progress within the current top-level setup step.
            avg = sum(float(pkg_states[s["id"]]["pct"]) for s in active_specs) / len(active_specs)
        gui.set_progress_percent(max(0.0, min(100.0, avg)))

    # Show the classic dependency table immediately.
    if gui:
        gui.set_status("Checking Python package dependencies...")
        gui.set_progress_percent(2)
        _render_table()

    def _precheck(spec):
        installed = _check(spec)
        if installed:
            _set_state(
                spec["id"], status_text="✔ Installed", pct=100,
                done=True, ok_value=True,
            )
        else:
            _set_state(
                spec["id"], status_text="⏳ Waiting...", pct=0,
                done=False, ok_value=False,
            )
        return installed

    # Checks are read-only, so running these concurrently is safe.
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(active_specs))) as executor:
        installed_flags = list(executor.map(_precheck, active_specs))

    missing_specs = [spec for spec, installed in zip(active_specs, installed_flags) if not installed]

    if not missing_specs:
        if gui:
            gui.set_progress_percent(100)
            gui.log_ok("All pip package dependencies are verified!")
        return True

    # Fast offline termination: cannot download missing pip packages without internet
    if not _is_network_reachable(timeout=2.0):
        missing_names = ", ".join(s["name"] for s in missing_specs)
        for s in missing_specs:
            _set_state(s["id"], status_text="✖ Offline (missing)", pct=0, done=True, ok_value=False)
        if gui:
            gui.log_fail(f"Internet connection is required to download missing Python dependencies: {missing_names}")
        warn(f"Cannot download missing Python dependencies ({missing_names}) while offline.")
        return False

    cpu_count = max(1, int(os.cpu_count() or 1))
    if cpu_count <= 2:
        download_workers = 1
    elif cpu_count < 6:
        download_workers = 2
    else:
        download_workers = 3
    download_workers = max(1, min(download_workers, len(missing_specs)))

    status(
        f"Preparing {len(missing_specs)} Python dependencies "
        f"with {download_workers} parallel download worker"
        f"{'s' if download_workers != 1 else ''}..."
    )

    failure_details = {}
    download_failures = set()

    # One temporary root per bootstrap run.  Each package gets its own folder,
    # so concurrent pip processes never write the same artifact path.
    temp_root = Path(tempfile.mkdtemp(prefix="mcu_bootstrap_pip_"))
    wheelhouse = temp_root / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)

    # ── Stall detection & retry constants for pip download ──────────────
    _DOWNLOAD_STALL_TIMEOUT = 45   # seconds with zero output → stalled
    _DOWNLOAD_MAX_RETRIES   = 2    # retry up to 2 times (3 attempts total)

    def _download_one(spec):
        sid = spec["id"]
        worker_dir = temp_root / f"download_{sid}"
        worker_dir.mkdir(parents=True, exist_ok=True)

        max_attempts = _DOWNLOAD_MAX_RETRIES + 1

        cmd = [
            sys.executable,
            "-m",
            "pip",
            "download",
            *spec["pip_args"],
            "--dest",
            str(worker_dir),
            "--disable-pip-version-check",
            "--prefer-binary",
            "--only-binary",
            ":all:",
            "--retries",
            "3",
            "--timeout",
            "30",
            "--progress-bar",
            "off",
            "--no-input",
            # Reuse pip's content-addressed HTTP/wheel cache.  The previous
            # no-cache-dir policy forced a full redownload whenever a venv was
            # recreated, even when the same wheels were already present on the
            # machine.  Each worker still writes to its own destination; the
            # shared cache is read/write-safe because pip commits entries
            # atomically.
        ]

        # Critical bootstrap packages are small wheel installs and should
        # fail visibly rather than leave the UI in a fake 64–75% phase for
        # eight minutes.  Keep a longer budget only for an explicitly
        # requested optional feature or the PlatformIO package.
        timeout_limit = (
            600 if not spec.get("critical", True)
            else (360 if sid == "platformio" else 180)
        )

        last_detail = ""

        for attempt in range(max_attempts):
            if attempt > 0:
                # Show retry status in the per-package row.
                _set_state(
                    sid,
                    status_text=f"⟳ Retrying ({attempt + 1}/{max_attempts})...",
                    pct=5,
                )
                _update_bottom_from_rows()
                # Clean the worker dir so a partial download does not confuse
                # the next attempt.
                try:
                    for f in worker_dir.iterdir():
                        if f.is_file():
                            f.unlink(missing_ok=True)
                except Exception:
                    pass
                # Brief pause before retry to let transient issues settle.
                time.sleep(2)
            else:
                _set_state(sid, status_text="⬇ Downloading...", pct=5)
                _update_bottom_from_rows()

            output_lines = []
            phase_events = 0
            proc = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )

                import queue as _queue
                output_queue = _queue.Queue()
                reader_done = threading.Event()

                def _reader(_proc=proc):
                    try:
                        if _proc.stdout is not None:
                            for raw in iter(_proc.stdout.readline, ""):
                                output_queue.put(raw)
                    except Exception:
                        pass
                    finally:
                        try:
                            if _proc.stdout is not None:
                                _proc.stdout.close()
                        except Exception:
                            pass
                        reader_done.set()

                threading.Thread(
                    target=_reader,
                    daemon=True,
                    name=f"BootstrapPipDownloadReader-{sid}-{attempt}",
                ).start()

                started = time.time()
                last_output_time = started   # ← stall detection anchor
                timed_out = False
                stalled = False

                while True:
                    try:
                        raw = output_queue.get(timeout=0.15)
                        line = raw.rstrip("\r\n")
                        output_lines.append(line)
                        last_output_time = time.time()   # reset stall clock
                        low = line.strip().lower()
                        if low:
                            # pip does not expose a stable byte-progress API to a
                            # non-interactive subprocess, so the classic row reflects
                            # real resolver/download phases and never claims fake bytes.
                            if low.startswith("collecting "):
                                phase_events += 1
                                _set_state(sid, status_text="⚙ Resolving...", pct=min(28, 10 + phase_events * 2))
                            elif ".metadata" in low and (low.startswith("downloading ") or low.startswith("using cached ")):
                                phase_events += 1
                                _set_state(sid, status_text="⚙ Reading metadata...", pct=min(38, 24 + phase_events * 2))
                            elif low.startswith("downloading ") or low.startswith("using cached "):
                                phase_events += 1
                                _set_state(sid, status_text="⬇ Downloading...", pct=min(68, 34 + phase_events * 3))
                            elif low.startswith("saved "):
                                _set_state(sid, status_text="⬇ Saving packages...", pct=70)
                            elif low.startswith("successfully downloaded"):
                                _set_state(sid, status_text="✔ Downloaded", pct=75)
                            _update_bottom_from_rows()
                    except _queue.Empty:
                        pass

                    if proc.poll() is not None and reader_done.is_set() and output_queue.empty():
                        break

                    now = time.time()

                    # Stall detection: no output for _DOWNLOAD_STALL_TIMEOUT seconds
                    if now - last_output_time > _DOWNLOAD_STALL_TIMEOUT:
                        stalled = True
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        output_lines.append(
                            f"bootstrap: download stalled ({_DOWNLOAD_STALL_TIMEOUT}s with no output)"
                        )
                        break

                    # Absolute wall-clock timeout
                    if now - started > timeout_limit:
                        timed_out = True
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        output_lines.append("bootstrap timeout while downloading")
                        break

                try:
                    return_code = proc.wait(timeout=10)
                except Exception:
                    return_code = -1

                if timed_out or stalled:
                    return_code = -1

                if return_code == 0:
                    _set_state(sid, status_text="✔ Downloaded", pct=75)
                    _update_bottom_from_rows()
                    return sid, True, worker_dir, ""

                # Download failed on this attempt — store detail for potential retry.
                last_detail = "\n".join(output_lines[-80:]).strip()

                # If this was a stall/timeout and we have retries left, loop.
                if (stalled or timed_out) and attempt < max_attempts - 1:
                    continue

                # Non-stall failure or last attempt — give up.
                return sid, False, worker_dir, last_detail

            except Exception as exc:
                try:
                    if proc is not None and proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass
                last_detail = "\n".join(output_lines[-80:]).strip()
                if last_detail:
                    last_detail += "\n"
                last_detail += str(exc)

                # Retry on exception only if attempts remain.
                if attempt < max_attempts - 1:
                    continue
                return sid, False, worker_dir, last_detail

        # Should not reach here, but safety net.
        return sid, False, worker_dir, last_detail

    try:
        # Downloads/resolution are isolated from the environment, so this is the
        # safe place to use parallel pip processes.
        completed_downloads = 0
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=download_workers,
            thread_name_prefix="BootstrapPipDownload",
        ) as executor:
            future_map = {executor.submit(_download_one, spec): spec for spec in missing_specs}
            for future in concurrent.futures.as_completed(future_map):
                spec = future_map[future]
                sid = spec["id"]
                try:
                    result_sid, downloaded_ok, worker_dir, detail = future.result()
                except Exception as exc:
                    result_sid, downloaded_ok, worker_dir, detail = sid, False, temp_root / f"download_{sid}", str(exc)

                if downloaded_ok:
                    # Merge complete artifacts only after that worker exits.
                    for artifact in worker_dir.iterdir():
                        if not artifact.is_file():
                            continue
                        target = wheelhouse / artifact.name
                        if not target.exists():
                            try:
                                shutil.copy2(artifact, target)
                            except OSError:
                                pass
                    completed_downloads += 1
                    _set_state(
                        result_sid,
                        status_text="✔ Downloaded; waiting for remaining downloads",
                        pct=75,
                    )
                    if gui:
                        gui.set_status(
                            f"Downloaded {completed_downloads}/{len(missing_specs)} packages; "
                            "waiting for the remaining downloads..."
                        )
                else:
                    completed_downloads += 1
                    download_failures.add(result_sid)
                    failure_details[result_sid] = detail
                    # Do not abort.  The single install pass below may still
                    # satisfy this package from normal pip cache/index, so this
                    # is just a status note — not an error.  Use a neutral glyph
                    # so users don't think the install has failed while the
                    # unified pip transaction is still about to install it.
                    timed_out = "timeout" in str(detail).lower()
                    _set_state(
                        result_sid,
                        status_text=(
                            "⚠ Download timed out; retrying unified install"
                            if timed_out else "⏳ Unified install"
                        ),
                        pct=65,
                    )
                _update_bottom_from_rows()

        # Build one top-level install argument list.  Deduplicate while preserving
        # specification order.
        install_args = []
        seen = set()
        for spec in missing_specs:
            for arg in spec["pip_args"]:
                key = str(arg).lower()
                if key not in seen:
                    seen.add(key)
                    install_args.append(arg)

        _set_all(missing_specs, "⚙ Installing...", 80)
        _update_bottom_from_rows()
        if gui:
            gui.set_status("Installing prepared Python dependencies in one safe pip transaction...")

        def _install_line_callback(line: str):
            low = line.strip().lower()
            if low.startswith("installing collected packages:"):
                _set_all(missing_specs, "⚙ Installing...", 86)
            elif low.startswith("successfully installed"):
                # pip reports all successfully installed distributions on one
                # line; move rows to verification without exposing the raw text.
                _set_all(missing_specs, "⚙ Verifying...", 95)
            _update_bottom_from_rows()

        # Use the prepared wheelhouse as a fast local source, but keep the
        # normal package index available on the FIRST install.  The previous
        # offline-first design frequently had to run a second full resolver
        # transaction when a source/build dependency was not present locally.
        # This keeps the parallel prefetch benefit without paying for a normal
        # retry on successful fresh-machine installs.
        first_install_args = [
            "--no-warn-script-location",
            "--find-links",
            str(wheelhouse),
            *install_args,
        ]
        # When every download worker completed successfully, the wheelhouse
        # is a complete, deterministic install source.  Do not let pip query
        # the package index again for metadata or a newer candidate: that can
        # turn a local install back into a network-bound transaction.  If one
        # worker failed, keep the index available for the recovery path.
        if not download_failures:
            first_install_args.insert(0, "--no-index")

        install_timeout = 600 if (
            download_failures or any(not spec.get("critical", True) for spec in missing_specs)
        ) else 300
        install_ok = _run_pip_install(
            first_install_args,
            timeout=install_timeout,
            use_cache=True,
            only_binary=False,
            progress_start=80,
            progress_end=95,
            line_callback=_install_line_callback,
        )

        # Recovery is reserved for a real pip failure (for example a damaged
        # or permission-locked shared cache).  Retry once without cache rather
        # than doing an expected offline->online second pass every first run.
        if not install_ok:
            _set_all(missing_specs, "⚙ Retrying without cache...", 90)
            if gui:
                gui.set_status("Retrying Python dependency installation without pip cache...")
            # A complete wheelhouse is deliberately installed offline on the
            # first pass.  If that local transaction fails, recovery must be
            # allowed to consult the package index again; otherwise the retry
            # would repeat the same offline failure while only changing pip's
            # cache policy.
            retry_install_args = [
                arg for arg in first_install_args if str(arg).lower() != "--no-index"
            ]
            install_ok = _run_pip_install(
                retry_install_args,
                timeout=600,
                use_cache=False,
                only_binary=False,
                progress_start=90,
                progress_end=96,
                line_callback=_install_line_callback,
            )

        # Verify all missing packages after the single transaction.
        remaining = []
        for spec in missing_specs:
            sid = spec["id"]
            verified = _check(spec)
            if verified:
                _set_state(sid, status_text="✔ Installed", pct=100, done=True, ok_value=True)
            else:
                remaining.append(spec)
                _set_state(sid, status_text="⚙ Repairing...", pct=95, done=False, ok_value=False)
        _update_bottom_from_rows()

        # Targeted repair is intentionally serial.  It is used only when the
        # combined transaction succeeded incompletely or a package-specific
        # post-install/import issue remains (notably pywin32 on Windows).
        for spec in remaining:
            sid = spec["id"]
            if gui:
                gui.set_status(f"Repairing {spec['name']}...")

            repair_ok = _run_pip_install(
                [
                    "--no-warn-script-location",
                    "--force-reinstall",
                    "--find-links",
                    str(wheelhouse),
                    *spec["pip_args"],
                ],
                timeout=720 if sid == "platformio" else 480,
                use_cache=False,
                only_binary=False,
                progress_start=95,
                progress_end=98,
                line_callback=lambda line, _sid=sid: _set_state(
                    _sid,
                    status_text="⚙ Repairing...",
                    pct=97 if line.strip().lower().startswith("installing collected packages:") else 95,
                ),
            )

            importlib.invalidate_caches()
            verified = repair_ok and _check(spec)
            if verified:
                _set_state(sid, status_text="✔ Installed", pct=100, done=True, ok_value=True)
            else:
                if _LAST_PIP_ERROR:
                    failure_details[sid] = _LAST_PIP_ERROR
                _set_state(sid, status_text="✖ Install Failed", pct=100, done=True, ok_value=False)
            _update_bottom_from_rows()

        # Final fresh checks catch transitive changes made during a repair.
        final_failed = []
        for spec in active_specs:
            verified = _check(spec)
            if verified:
                _set_state(spec["id"], status_text="✔ Installed", pct=100, done=True, ok_value=True)
            else:
                _set_state(spec["id"], status_text="✖ Install Failed", pct=100, done=True, ok_value=False)
                final_failed.append(spec)

        if gui:
            gui.set_progress_percent(100)

        if final_failed:
            names = ", ".join(spec["name"] for spec in final_failed)
            if gui:
                gui.log_fail(f"Required Python dependencies failed: {names}")
                detail = next(
                    (failure_details.get(spec["id"]) for spec in final_failed if failure_details.get(spec["id"])),
                    "",
                )
                if detail:
                    gui.log_dim(f"pip: {detail.splitlines()[-1]}")
            return False

        if gui:
            gui.log_ok("All required pip package dependencies installed & verified!")
        return True

    finally:
        # Never leave temporary wheelhouses behind on the user's machine.
        try:
            shutil.rmtree(temp_root, ignore_errors=True)
        except Exception:
            pass


_OPTIONAL_PIP_FEATURE_PACKAGE_IDS: dict[str, set[str]] = {
    "qscintilla_viewer": {"pyqt5_qscintilla"},
}


def ensure_optional_pip_feature(
    feature: str,
    gui: Optional[BootstrapGUI] = None,
) -> bool:
    """Repair packages for an explicitly requested feature.

    Complete bootstrap setup already installs every declared package.  This
    compatibility helper remains available for callers that request a targeted
    repair after an installation has been damaged.
    """
    package_ids = _OPTIONAL_PIP_FEATURE_PACKAGE_IDS.get(str(feature).strip().lower())
    if not package_ids:
        return False
    return ensure_pip_packages_parallel(gui, package_ids=package_ids)


# WebView2 runtime detection.
_WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_WEBVIEW2_BOOTSTRAPPER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def check_webview2_runtime() -> bool:
    """Detect the Evergreen WebView2 Runtime using Microsoft's documented
    registry check: look up the 'pv' (product version) value under the
    WebView2 Runtime's EdgeUpdate client registration. Checked in the
    per-machine (WOW6432Node, for 64-bit Windows), per-machine (32-bit
    Windows), and per-user locations, since the runtime can be registered
    in any of the three depending on how it was installed."""
    if sys.platform != "win32":
        return True

    import winreg
    candidates = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s" % _WEBVIEW2_CLIENT_GUID),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\%s" % _WEBVIEW2_CLIENT_GUID),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\%s" % _WEBVIEW2_CLIENT_GUID),
    ]
    for hive, subkey in candidates:
        try:
            key = winreg.OpenKey(hive, subkey)
            try:
                pv, _ = winreg.QueryValueEx(key, "pv")
                if pv and pv != "0.0.0.0":
                    return True
            finally:
                winreg.CloseKey(key)
        except (FileNotFoundError, OSError):
            continue
    return False


def _wait_for_webview2_runtime(timeout: int = 45) -> bool:
    """Wait for the installer to publish its registry registration."""
    if sys.platform != "win32":
        return True
    deadline = time.time() + max(1, int(timeout))
    while time.time() < deadline:
        if check_webview2_runtime():
            return True
        time.sleep(1.0)
    return check_webview2_runtime()


def _prepare_webview2_installer() -> Path | None:
    """Find or download the official Evergreen WebView2 bootstrapper."""
    installer = _find_installer_file("MicrosoftEdgeWebview2Setup.exe")
    if _is_valid_exe(installer):
        return installer

    # A copied/incomplete app folder may not include the bundled installer.
    # Download Microsoft's official Evergreen bootstrapper into the app's
    # installer cache so the same repair is available on the next launch.
    cache_path = SCRIPT_DIR / "installers" / "MicrosoftEdgeWebview2Setup.exe"
    try:
        section("Downloading Microsoft Edge WebView2 Runtime installer")
        _download_file(
            _WEBVIEW2_BOOTSTRAPPER_URL,
            cache_path,
            timeout=60,
            attempts=3,
        )
        if _is_valid_exe(cache_path):
            ok("WebView2 Runtime installer downloaded and verified")
            return cache_path
    except Exception as exc:
        warn(f"Could not download the WebView2 Runtime installer: {exc}")

    # Read-only app folders should not prevent a repair. Keep a temporary
    # verified copy for this run if the app cache cannot be written.
    try:
        temp_path = Path(tempfile.gettempdir()) / "MCU-Flasher" / "MicrosoftEdgeWebview2Setup.exe"
        _download_file(
            _WEBVIEW2_BOOTSTRAPPER_URL,
            temp_path,
            timeout=60,
            attempts=3,
        )
        if _is_valid_exe(temp_path):
            ok("WebView2 Runtime installer downloaded to the temporary repair cache")
            return temp_path
    except Exception as exc:
        warn(f"Temporary WebView2 installer download failed: {exc}")
    return None


def ensure_webview2_runtime() -> bool:
    """Install and verify the Microsoft Edge WebView2 Runtime.

    Monaco depends on this runtime.  A successful installer exit code is not
    enough: Bootstrap waits for the documented registry registration and
    refuses to launch the GUI until the runtime is actually available.
    """
    if sys.platform != "win32":
        return True

    if check_webview2_runtime():
        ok("Microsoft Edge WebView2 Runtime is already installed")
        return True

    section("Installing Microsoft Edge WebView2 Runtime")
    installer = _prepare_webview2_installer()

    if installer is None or not _is_valid_exe(installer):
        warn("WebView2 Runtime installer is missing or invalid after the download/verification attempt.")
        return False

    status("Launching WebView2 Runtime installer (silent)...")
    try:
        # /silent suppresses all UI; /install performs the actual install
        # (as opposed to the bootstrapper's default update-check behavior).
        result = subprocess.run(
            [str(installer), "/silent", "/install"],
            capture_output=True, text=True, timeout=180,
        )
        # Do not trust the process exit code by itself. The runtime may still
        # be registering, or the bootstrapper may return an informational
        # non-zero code. Only the post-install registry check certifies it.
        if _wait_for_webview2_runtime(timeout=45):
            ok("Microsoft Edge WebView2 Runtime installed and verified successfully")
            return True
        else:
            detail = (result.stdout + result.stderr).strip().splitlines()
            tail = detail[-1] if detail else "no installer detail was returned"
            warn(
                f"WebView2 Runtime was not detected after installation "
                f"(installer exit code {result.returncode}; {tail})."
            )
            return False
    except Exception as e:
        warn(f"Failed to run WebView2 Runtime installer: {e}")
        return False


# ── 3. Ensure PlatformIO ─────────────────────────────────────
def find_pio() -> list[str] | None:
    """
    Check if PlatformIO is available. Always uses  python -m platformio
    rather than the pio.exe / platformio.exe wrapper scripts — those are
    MSVC-compiled launchers that throw 0xc0000142 when the C++ runtime
    they were built against is missing on the target machine.
    """
    _cf = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    _pio_launcher = SCRIPT_DIR / "_pio_launcher.py"

    def _pio_cmd_for(py: Path) -> list[str]:
        if sys.platform == "win32" and _pio_launcher.exists():
            return [str(py), str(_pio_launcher)]
        return [str(py), "-m", "platformio"]

    def _py_has_platformio(py: Path) -> bool:
        try:
            if py.resolve() == Path(sys.executable).resolve():
                import importlib.util
                if importlib.util.find_spec("platformio") is not None:
                    return True
        except Exception:
            pass
        try:
            res = subprocess.run(
                [str(py), "-m", "platformio", "--version"],
                capture_output=True, timeout=15, creationflags=_cf,
            )
            return res.returncode == 0
        except Exception:
            return False

    # ── 1. Current interpreter (fastest) ──────────────────────────────────
    if _py_has_platformio(Path(sys.executable)):
        return _pio_cmd_for(Path(sys.executable))

    # ── 2. python.exe siblings in the venv Scripts / bin dir ──────────────
    python_dir = Path(sys.executable).parent
    for name in ["python.exe", "python3.exe", "python", "python3"]:
        for d in [python_dir, python_dir.parent / "Scripts", python_dir.parent / "bin"]:
            py_cand = d / name
            if py_cand.exists() and py_cand.resolve() != Path(sys.executable).resolve():
                if _py_has_platformio(py_cand):
                    return _pio_cmd_for(py_cand)

    # ── 3. PlatformIO's own embedded venv (PLATFORMIO_CORE_DIR/penv) ──────
    pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR")
    if pio_core_dir:
        pio_core_path = Path(pio_core_dir)
        for scripts_dir in [pio_core_path / "penv" / "Scripts", pio_core_path / "penv" / "bin"]:
            for name in ["python.exe", "python3.exe", "python", "python3"]:
                py_cand = scripts_dir / name
                if py_cand.exists():
                    if _py_has_platformio(py_cand):
                        return _pio_cmd_for(py_cand)

    return None


def ensure_platformio() -> bool:
    pio = find_pio()
    if pio:
        ok("PlatformIO Core is already installed")
        return True

    status("PlatformIO not found, installing via pip...")
    status("This may take a few minutes on first run...")

    if _run_pip_install(["platformio"], timeout=300):
        ok("PlatformIO Core installed successfully")
        return True
    else:
        fail("Failed to install PlatformIO Core")
        return False



# ── 3b. Pre-install board toolchains ─────────────────────────
def _get_board_download_dir() -> Path:
    """Read the same download directory arduino_lib_req.py / mcu_flash_gui.py
    use, so bootstrap can see which board cores the user has downloaded via
    the Board Downloader. Mirrors mcu_flash_gui.py's _get_download_dir()."""
    default_dir = Path(os.path.expanduser("~")) / "Documents" / "_MCUFlasherByNaph_src"
    settings_file = SCRIPT_DIR / "src" / "dbs" / "arduino_browser_settings.json"
    if not settings_file.exists():
        settings_file = SCRIPT_DIR / "arduino_browser_settings.json"
    settings = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception:
            settings = {}

    if isinstance(settings, dict):
        download_dir = str(settings.get("download_dir", "") or "")
        if download_dir:
            current_dir = Path(os.path.expandvars(os.path.expanduser(download_dir)))
            if current_dir.is_dir():
                return current_dir

        # This file travels with copied projects, so its old absolute user
        # path is expected to become stale. Rewrite only this app-owned
        # setting to the current user's portable default.
        settings["download_dir"] = str(default_dir)
        try:
            temporary = settings_file.with_name(
                settings_file.name + f".tmp-{os.getpid()}"
            )
            temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            os.replace(temporary, settings_file)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass
    return default_dir


def _normalize_board_identity(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _board_name_tokens(value: object) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(value or "").lower())) - {
        "board", "module", "device", "development", "dev", "kit", "version",
        "rev", "revision", "the", "with", "for", "series",
    }


def _load_downloaded_board_records() -> list[dict]:
    """Parse downloaded Arduino board cores without assuming PlatformIO IDs."""
    boards_path = _get_board_download_dir() / "Boards"
    records: list[dict] = []
    if not boards_path.is_dir():
        return records
    for boards_file in sorted(boards_path.glob("**/boards.txt"), key=lambda x: str(x).lower()):
        props_by_id: dict[str, dict[str, str]] = {}
        try:
            lines = boards_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            left, value = line.split("=", 1)
            if "." not in left:
                continue
            board_id, key = left.split(".", 1)
            if key.startswith("menu.") or ".menu." in key:
                continue
            props_by_id.setdefault(board_id.strip(), {})[key.strip()] = value.strip()
        for board_id, props in props_by_id.items():
            name = str(props.get("name") or "").strip()
            if not name:
                continue
            hwids: set[tuple[int, int]] = set()
            pieces: dict[str, dict[str, int]] = {}
            for key, value in props.items():
                match = re.match(r"^(?:upload_port\.)?(vid|pid)\.(\d+)$", key, re.IGNORECASE)
                if not match:
                    continue
                field, index = match.groups()
                try:
                    pieces.setdefault(index, {})[field.lower()] = int(str(value), 0)
                except ValueError:
                    pass
            for pair in pieces.values():
                if "vid" in pair and "pid" in pair:
                    hwids.add((pair["vid"], pair["pid"]))
            records.append({
                "arduino_id": board_id,
                "name": name,
                "mcu": str(props.get("build.mcu") or "").lower().strip(),
                "variant": str(props.get("build.variant") or "").strip(),
                "build_board": str(props.get("build.board") or "").strip(),
                "hwids": hwids,
                "source_file": str(boards_file),
                "source_core": boards_file.parent.name,
            })
    return records


def _load_installed_pio_board_catalog(pio_core_dir: str, platform: str = "") -> list[dict]:
    """Read canonical board JSON manifests from this exact PlatformIO core store."""
    root = Path(pio_core_dir)
    catalog: list[dict] = []
    platforms_root = root / "platforms"
    if not platforms_root.is_dir():
        return catalog
    try:
        platform_dirs = list(platforms_root.iterdir())
    except OSError:
        return catalog
    for platform_dir in platform_dirs:
        if not platform_dir.is_dir():
            continue
        platform_id = platform_dir.name.split("@", 1)[0]
        if platform and platform_id.lower() != platform.lower():
            continue
        board_dir = platform_dir / "boards"
        if not board_dir.is_dir():
            continue
        for manifest_path in board_dir.glob("*.json"):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            build = data.get("build") if isinstance(data.get("build"), dict) else {}
            frameworks = data.get("frameworks") or []
            if isinstance(frameworks, str):
                frameworks = [frameworks]
            hwids: set[tuple[int, int]] = set()
            for pair in build.get("hwids") or []:
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    try:
                        hwids.add((int(str(pair[0]), 0), int(str(pair[1]), 0)))
                    except ValueError:
                        pass
            extra_flags = build.get("extra_flags") or []
            if isinstance(extra_flags, str):
                extra_flags = [extra_flags]
            defines = {
                _normalize_board_identity(m.group(1))
                for flag in extra_flags
                for m in [re.search(r"-D\s*(ARDUINO_[A-Za-z0-9_]+)", str(flag), re.IGNORECASE)]
                if m
            }
            catalog.append({
                "id": manifest_path.stem,
                "name": str(data.get("name") or manifest_path.stem),
                "vendor": str(data.get("vendor") or ""),
                "platform": platform_id,
                "frameworks": {str(x).lower() for x in frameworks},
                "mcu": str(build.get("mcu") or "").lower().strip(),
                "variant": str(build.get("variant") or "").strip(),
                "hwids": hwids,
                "arduino_defines": defines,
            })
    return catalog


def _query_pio_board_catalog(pio: list[str], env: dict, installed_only: bool = False) -> list[dict]:
    """Ask PlatformIO for its board catalog. Used to infer platform IDs dynamically."""
    cmd = list(pio) + ["boards"]
    if installed_only:
        cmd.append("--installed")
    cmd.append("--json-output")
    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        raw = json.loads(result.stdout)
    except Exception:
        return []
    if isinstance(raw, dict):
        for key in ("boards", "items", "results"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
    if not isinstance(raw, list):
        return []
    catalog: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        board_id = row.get("id") or row.get("board") or row.get("board_id")
        platform = row.get("platform") or row.get("platform_name") or row.get("platform_id")
        if isinstance(platform, dict):
            platform = platform.get("name") or platform.get("id")
        if not board_id or not platform:
            continue
        catalog.append({
            "id": str(board_id),
            "name": str(row.get("name") or board_id),
            "vendor": str(row.get("vendor") or ""),
            "platform": str(platform),
            "frameworks": {"arduino"},
            "mcu": str(row.get("mcu") or row.get("build_mcu") or "").lower().strip(),
            "variant": str(row.get("variant") or "").strip(),
            "hwids": set(),
            "arduino_defines": set(),
        })
    return catalog


def _score_downloaded_board_match(record: dict, candidate: dict) -> tuple[float, list[str]]:
    frameworks = candidate.get("frameworks") or set()
    if frameworks and "arduino" not in frameworks:
        return -1.0, []
    rec_mcu = _normalize_board_identity(record.get("mcu"))
    pio_mcu = _normalize_board_identity(candidate.get("mcu"))
    if rec_mcu and pio_mcu and rec_mcu != pio_mcu:
        return -1.0, []
    score = 0.0
    reasons: list[str] = []
    rid = _normalize_board_identity(record.get("arduino_id"))
    rname = _normalize_board_identity(record.get("name"))
    rvariant = _normalize_board_identity(record.get("variant"))
    rbuild = _normalize_board_identity(record.get("build_board"))
    cid = _normalize_board_identity(candidate.get("id"))
    cname = _normalize_board_identity(candidate.get("name"))
    cvariant = _normalize_board_identity(candidate.get("variant"))
    if rec_mcu and pio_mcu:
        score += 45; reasons.append("mcu")
    if rid and cid and rid == cid:
        score += 170; reasons.append("id")
    if rvariant and cvariant and rvariant == cvariant:
        score += 190; reasons.append("variant")
    if rname and cname and rname == cname:
        score += 165; reasons.append("name")
    if record.get("hwids") and candidate.get("hwids") and record["hwids"] & candidate["hwids"]:
        score += 185; reasons.append("usb")
    if rbuild and rbuild in (candidate.get("arduino_defines") or set()):
        score += 135; reasons.append("arduino-define")
    elif rid and rid in (candidate.get("arduino_defines") or set()):
        score += 120; reasons.append("arduino-define")
    import difflib
    rw = _board_name_tokens(f"{record.get('name','')} {record.get('arduino_id','')}")
    cw = _board_name_tokens(f"{candidate.get('name','')} {candidate.get('id','')} {candidate.get('vendor','')}")
    if rw and cw:
        score += (len(rw & cw) / max(1, len(rw | cw))) * 70.0
    score += difflib.SequenceMatcher(
        None, str(record.get("name") or "").lower(), str(candidate.get("name") or "").lower(),
        autojunk=False,
    ).ratio() * 45.0
    return score, reasons


def _resolve_downloaded_board(record: dict, catalog: list[dict]) -> dict | None:
    ranked = []
    for candidate in catalog:
        score, reasons = _score_downloaded_board_match(record, candidate)
        if score >= 0:
            ranked.append((score, candidate, reasons))
    if not ranked:
        return None
    ranked.sort(key=lambda x: (-x[0], str(x[1].get("platform", "")), str(x[1].get("id", ""))))
    score, best, reasons = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else -999.0
    strong = any(r in reasons for r in ("id", "variant", "name", "usb", "arduino-define"))
    if score < (120.0 if strong else 105.0) or (not strong and score - second < 18.0):
        return None
    return {**best, "match_score": round(score, 2), "match_reasons": reasons}


def _fallback_platform_from_mcu(mcu: str) -> str:
    """Last-resort architecture fallback when PlatformIO catalog discovery is unavailable."""
    value = _normalize_board_identity(mcu)
    if value.startswith("esp32"):
        return "espressif32"
    if value.startswith("esp8266"):
        return "espressif8266"
    if value.startswith("atmega") or value.startswith("attiny"):
        return "atmelavr"
    if value.startswith("stm32"):
        return "ststm32"
    if value.startswith("rp2040") or value.startswith("rp2350"):
        return "raspberrypi"
    if value.startswith("samd") or value.startswith("sam"):
        return "atmelsam"
    if value.startswith("nrf"):
        return "nordicnrf52"
    return ""


def _scan_downloaded_platforms(pio: list[str] | None = None) -> set[str]:
    """Infer PlatformIO platforms from downloaded boards, primarily via PIO's own catalog."""
    records = _load_downloaded_board_records()
    if not records:
        return set()
    pio = pio or find_pio()
    env = os.environ.copy()
    env["PLATFORMIO_CORE_DIR"] = os.environ.get("PLATFORMIO_CORE_DIR") or _get_safe_platformio_core_dir(SCRIPT_DIR)
    catalog = _query_pio_board_catalog(pio, env, installed_only=False) if pio else []
    platforms: set[str] = set()
    source_counts: dict[str, dict[str, int]] = {}
    if catalog:
        for record in records:
            match = _resolve_downloaded_board(record, catalog)
            if match and match.get("platform"):
                bucket = source_counts.setdefault(record["source_file"], {})
                platform = str(match["platform"])
                bucket[platform] = bucket.get(platform, 0) + 1
        for counts in source_counts.values():
            if counts:
                platforms.add(max(counts.items(), key=lambda item: (item[1], item[0]))[0])
    if not platforms:
        for record in records:
            fallback = _fallback_platform_from_mcu(str(record.get("mcu") or ""))
            if fallback:
                platforms.add(fallback)
    return platforms


# Friendly label + rough one-time download size shown while installing.
_PLATFORM_INFO = {
    "espressif32":   ("ESP32 / ESP32-S3", "~180 MB"),
    "espressif8266": ("ESP8266",          "~60 MB"),
    "atmelavr":      ("Arduino UNO / AVR", "~30 MB"),
    "ststm32":       ("STM32",             "~120 MB"),
    "raspberrypi":   ("RP2040 / Pico",     "~80 MB"),
    "atmelsam":      ("Arduino SAMD/SAM",  "~90 MB"),
    "nordicnrf52":   ("nRF52 / nRF51",     "~70 MB"),
    "atmelmegaavr":  ("megaAVR",           "~30 MB"),
    "teensy":        ("Teensy",            "~100 MB"),
    "ch32v":         ("CH32V RISC-V",      "~50 MB"),
}

# Readiness is derived from successful environment builds and their actual package metadata.
_FULL_FAMILY_MARKER_SCHEMA = 5
_FULL_FAMILY_MARKER_DIR = ".mcu-family-complete"
_BOARD_TOOLCHAIN_MARKER_SCHEMA = 1
_BOARD_TOOLCHAIN_MARKER_DIR = ".mcu-board-ready"
_BOARD_TOOLCHAIN_PREPARE_LOCK = threading.RLock()


def _platform_manifest_path(pio_core_dir: str, platform: str) -> Path | None:
    """Return the installed platform.json for this exact PlatformIO core_dir."""
    platforms_root = Path(pio_core_dir) / "platforms"
    if not platforms_root.is_dir():
        return None
    try:
        candidates = sorted(
            (
                p for p in platforms_root.iterdir()
                if p.is_dir()
                and (p.name.lower() == platform.lower() or p.name.lower().startswith(platform.lower() + "@"))
                and (p / "platform.json").is_file()
            ),
            key=lambda p: p.name.lower(),
        )
        return (candidates[-1] / "platform.json") if candidates else None
    except Exception:
        return None


def _installed_platform_version(pio_core_dir: str, platform: str) -> str:
    manifest = _platform_manifest_path(pio_core_dir, platform)
    if not manifest:
        return ""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return str(data.get("version", "")).strip()
    except Exception:
        return ""


def _full_family_marker_path(pio_core_dir: str, platform: str) -> Path:
    return Path(pio_core_dir) / _FULL_FAMILY_MARKER_DIR / f"{platform}.json"


def _board_toolchain_marker_path(
    pio_core_dir: str, platform: str, board_id: str, framework: str = "arduino"
) -> Path:
    """Return the private readiness marker for one proven board environment."""
    identity = "\0".join((str(platform), str(board_id), str(framework)))
    key = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()
    return Path(pio_core_dir) / _BOARD_TOOLCHAIN_MARKER_DIR / f"{key}.json"


def _installed_package_dir_names(pio_core_dir: str) -> list[str]:
    """Return stable package-folder names currently present in this core store."""
    packages_root = Path(pio_core_dir) / "packages"
    if not packages_root.is_dir():
        return []
    try:
        return sorted(p.name for p in packages_root.iterdir() if p.is_dir())
    except Exception:
        return []


def _declared_platform_package_names(pio_core_dir: str, platform: str) -> list[str]:
    """Read package names dynamically from the installed platform manifest."""
    manifest = _platform_manifest_path(pio_core_dir, platform)
    if not manifest:
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        packages = data.get("packages") or {}
        if isinstance(packages, dict):
            return sorted(str(name) for name in packages.keys() if str(name).strip())
    except Exception:
        pass
    return []


def _package_name_present(name: str, installed_names: set[str]) -> bool:
    wanted = str(name).lower().strip()
    if not wanted:
        return True
    for installed in installed_names:
        low = installed.lower()
        if low == wanted or low.startswith(wanted + "@"):
            return True
    return False


def _package_names_from_metadata(value, pio_core_dir: str) -> set[str]:
    """Extract package-directory names referenced by PlatformIO project metadata.

    ``pio project metadata --json-output`` reports compiler/toolchain locations and
    include paths for one environment.  Any path under this app's shared
    ``<core>/packages/<name>`` directory is therefore a package proven necessary
    by that successful representative build.
    """
    packages_root = (Path(pio_core_dir) / "packages")
    root_norm = str(packages_root).replace("\\", "/").rstrip("/") + "/"
    root_low = root_norm.lower()
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for child in node.values():
                walk(child)
        elif isinstance(node, (list, tuple, set)):
            for child in node:
                walk(child)
        elif isinstance(node, str):
            normalized = node.replace("\\", "/")
            low = normalized.lower()
            start = 0
            while True:
                idx = low.find(root_low, start)
                if idx < 0:
                    break
                tail = normalized[idx + len(root_norm):]
                package_name = tail.split("/", 1)[0].strip()
                if package_name:
                    found.add(package_name)
                start = idx + len(root_norm)

    walk(value)
    installed = set(_installed_package_dir_names(pio_core_dir))
    return {name for name in found if _package_name_present(name, installed)}


def _collect_env_package_requirements(
    pio: list[str], dummy_dir: Path, env_name: str, env: dict, pio_core_dir: str
) -> set[str]:
    """Return shared tool/framework packages actually required by one dummy env.

    Primary source is PlatformIO's environment-specific project metadata.  A
    text ``pio pkg list -e`` fallback is retained for older 6.x builds where a
    metadata field may omit one auxiliary package.  Only names that really exist
    in the configured shared package store are retained.
    """
    required: set[str] = set()
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            list(pio) + ["project", "metadata", "-e", env_name, "--json-output"],
            cwd=str(dummy_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            creationflags=flags,
        )
        if result.returncode == 0 and result.stdout.strip():
            required.update(_package_names_from_metadata(json.loads(result.stdout), pio_core_dir))
    except Exception:
        pass

    # Environment-scoped package listing includes the development platform's
    # framework/toolchain dependencies.  Parse only package names that also
    # exist as directories in this app's shared packages store.
    try:
        result = subprocess.run(
            list(pio) + ["pkg", "list", "-e", env_name],
            cwd=str(dummy_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            creationflags=flags,
        )
        installed_names = _installed_package_dir_names(pio_core_dir)
        installed_low = {name.lower(): name for name in installed_names}
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                # Typical tree row: ``├── toolchain-xtensa-esp32s3 @ 8.4...``
                match = re.search(
                    r"(?:[A-Za-z0-9_.-]+/)?([A-Za-z0-9_.-]+)\s+@\s*[^\s]+",
                    line,
                )
                if not match:
                    continue
                wanted = match.group(1).lower()
                for low_name, actual_name in installed_low.items():
                    if low_name == wanted or low_name.startswith(wanted + "@"): 
                        required.add(actual_name)
                        break
    except Exception:
        pass

    return required


def _discover_first_use_coverage(
    pio_core_dir: str, platform: str
) -> tuple[list[str], list[str], int, int, list[str]]:
    """Resolve downloaded boards and choose one real board per toolchain/MCU family.

    Hundreds of board models can share the same framework and compiler. Compiling
    all 400+ ESP32 entries would waste many minutes without installing anything
    extra.  Instead, resolve every downloaded board to its canonical PlatformIO
    manifest, then tiny-build one board for each distinct MCU/toolchain family.
    """
    catalog = _load_installed_pio_board_catalog(pio_core_dir, platform)
    if not catalog:
        return [], [], 0, 0, []
    arduino_catalog = [
        row for row in catalog
        if not row.get("frameworks") or "arduino" in (row.get("frameworks") or set())
    ]
    records = _load_downloaded_board_records()
    resolved: list[tuple[dict, dict]] = []
    source_hits: dict[str, int] = {}
    for record in records:
        match = _resolve_downloaded_board(record, arduino_catalog)
        if match:
            resolved.append((record, match))
            source_hits[record["source_file"]] = source_hits.get(record["source_file"], 0) + 1

    relevant_sources = set(source_hits)
    relevant_records = [r for r in records if r["source_file"] in relevant_sources]
    unresolved_names = []
    resolved_keys = {(r["source_file"], r["arduino_id"]) for r, _ in resolved}
    for record in relevant_records:
        if (record["source_file"], record["arduino_id"]) not in resolved_keys:
            unresolved_names.append(str(record.get("name") or record.get("arduino_id")))

    groups: dict[str, tuple[float, str]] = {}
    for _record, match in resolved:
        mcu = _normalize_board_identity(match.get("mcu") or match.get("id")) or "generic"
        board_id = str(match.get("id") or "")
        score = float(match.get("match_score") or 0.0)
        previous = groups.get(mcu)
        # Prefer the strongest downloaded-board match, then shorter canonical ID.
        if previous is None or score > previous[0] or (score == previous[0] and len(board_id) < len(previous[1])):
            groups[mcu] = (score, board_id)

    if not groups:
        # No downloaded board matched (for example a custom/new core). Still
        # dynamically cover each Arduino MCU family exposed by the installed PIO platform.
        for row in arduino_catalog:
            mcu = _normalize_board_identity(row.get("mcu") or row.get("id")) or "generic"
            board_id = str(row.get("id") or "")
            if not board_id:
                continue
            previous = groups.get(mcu)
            if previous is None or len(board_id) < len(previous[1]):
                groups[mcu] = (0.0, board_id)

    coverage_keys = sorted(groups.keys())
    boards = [groups[key][1] for key in coverage_keys]
    return boards, coverage_keys, len(resolved), len(relevant_records), unresolved_names


def _discover_first_use_boards(pio_core_dir: str, platform: str) -> list[str]:
    return _discover_first_use_coverage(pio_core_dir, platform)[0]


def _current_platform_coverage_keys(pio_core_dir: str, platform: str) -> list[str]:
    return _discover_first_use_coverage(pio_core_dir, platform)[1]


def _full_family_marker_valid(pio_core_dir: str, platform: str) -> bool:
    """Validate a successful, environment-specific first-use prewarm marker."""
    marker = _full_family_marker_path(pio_core_dir, platform)
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        if int(data.get("schema", 0)) != _FULL_FAMILY_MARKER_SCHEMA:
            return False
        if str(data.get("platform", "")).lower() != platform.lower():
            return False

        current_version = _installed_platform_version(pio_core_dir, platform)
        if not current_version or current_version != str(data.get("platform_version", "")).strip():
            return False

        prewarm_boards = [str(x) for x in (data.get("prewarm_boards") or []) if str(x).strip()]
        if not prewarm_boards:
            return False
        stored_coverage = sorted(str(x) for x in (data.get("coverage_keys") or []) if str(x).strip())
        current_coverage = _current_platform_coverage_keys(pio_core_dir, platform)
        if current_coverage and stored_coverage != sorted(current_coverage):
            return False

        installed = set(_installed_package_dir_names(pio_core_dir))
        if not installed:
            return False

        required = [str(x) for x in (data.get("required_packages") or []) if str(x).strip()]
        if required:
            return all(_package_name_present(name, installed) for name in required)

        # Conservative fallback only if metadata extraction produced nothing.
        snapshot = [str(x) for x in (data.get("package_dirs") or []) if str(x).strip()]
        return bool(snapshot and set(snapshot).issubset(installed))
    except Exception:
        return False


def _write_full_family_marker(
    pio_core_dir: str, platform: str, prewarm_boards: list[str],
    required_packages: set[str] | list[str],
    board_requirements: dict[str, list[str]] | None = None,
    coverage_keys: list[str] | None = None,
) -> None:
    """Record exact package folders proven usable by explicit first-use builds."""
    marker = _full_family_marker_path(pio_core_dir, platform)
    marker.parent.mkdir(parents=True, exist_ok=True)

    package_dirs = _installed_package_dir_names(pio_core_dir)
    if not package_dirs:
        raise RuntimeError("PlatformIO package store is empty after first-use prewarm")

    installed = set(package_dirs)
    required = sorted({
        str(name) for name in required_packages
        if str(name).strip() and _package_name_present(str(name), installed)
    }, key=str.lower)

    payload = {
        "schema": _FULL_FAMILY_MARKER_SCHEMA,
        "platform": platform,
        "platform_version": _installed_platform_version(pio_core_dir, platform),
        "install_mode": "dynamic-catalog-one-dummy-build-per-toolchain-family",
        "coverage_keys": sorted(coverage_keys or []),
        "prewarm_boards": list(prewarm_boards),
        "required_packages": required,
        "board_requirements": board_requirements or {},
    }
    if not required:
        payload["package_dirs"] = package_dirs
    marker.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _platform_already_installed(pio_core_dir: str, platform: str) -> bool:
    """Verify a PlatformIO platform inside THIS app's configured core directory.

    Standalone Board Browser folders under Documents/Boards are intentionally not
    treated as PlatformIO installations.  Likewise, another global ~/.platformio
    cache must not make bootstrap skip packages that the main app cannot see when
    PLATFORMIO_CORE_DIR points somewhere else.
    """
    if not pio_core_dir:
        pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR") or str(SCRIPT_DIR / "env" / ".platformio")

    target_core = Path(pio_core_dir)
    platforms_root = target_core / "platforms"
    packages_root = target_core / "packages"

    platform_found = False
    if platforms_root.is_dir():
        try:
            for p_dir in platforms_root.iterdir():
                name = p_dir.name.lower()
                if (
                    p_dir.is_dir()
                    and (name == platform.lower() or name.startswith(f"{platform.lower()}@"))
                    and (p_dir / "platform.json").exists()
                ):
                    platform_found = True
                    break
        except Exception:
            pass

    if not platform_found:
        return False

    if not packages_root.is_dir() or not _installed_package_dir_names(pio_core_dir):
        return False

    # Readiness is certified by a successful tiny first-use PlatformIO build
    # and a version-bound package snapshot marker. Optional platform.json
    # packages are deliberately not treated as mandatory.
    return _full_family_marker_valid(pio_core_dir, platform)


def board_toolchain_ready(
    pio_core_dir: str, platform: str, board_id: str, framework: str = "arduino"
) -> bool:
    """Check whether a board was proven usable by a real PlatformIO build.

    The full-family bootstrap marker is accepted first.  Main-app, on-demand
    installs additionally record a board-specific marker so a newly added
    board/platform can be prepared without pretending that one board build
    covered every variant in the platform.
    """
    if not platform or not board_id:
        return False
    marker = _board_toolchain_marker_path(pio_core_dir, platform, board_id, framework)
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if int(data.get("schema", 0)) != _BOARD_TOOLCHAIN_MARKER_SCHEMA:
                return False
            if str(data.get("platform", "")).lower() != str(platform).lower():
                return False
            if str(data.get("board", "")) != str(board_id):
                return False
            if str(data.get("framework", "arduino")).lower() != str(framework).lower():
                return False
            current_version = _installed_platform_version(pio_core_dir, platform)
            if not current_version or current_version != str(data.get("platform_version", "")):
                return False
            installed = set(_installed_package_dir_names(pio_core_dir))
            snapshot = {str(x) for x in (data.get("package_dirs") or []) if str(x).strip()}
            return bool(snapshot and snapshot.issubset(installed))
        except Exception:
            return False

    # Bootstrap's family marker is a fallback only when it explicitly contains
    # this exact board in its proven prewarm set. A newly downloaded board must
    # still get its own real validation even if another board in the same MCU
    # family was prepared during first launch.
    try:
        if _platform_already_installed(pio_core_dir, platform):
            family_marker = _full_family_marker_path(pio_core_dir, platform)
            data = json.loads(family_marker.read_text(encoding="utf-8"))
            prewarm_boards = {
                str(value).strip() for value in (data.get("prewarm_boards") or [])
                if str(value).strip()
            }
            return str(board_id) in prewarm_boards
    except Exception:
        pass
    return False


def _write_board_toolchain_marker(
    pio_core_dir: str, platform: str, board_id: str, framework: str = "arduino"
) -> None:
    """Persist the successful result of one board-specific prewarm build."""
    marker = _board_toolchain_marker_path(pio_core_dir, platform, board_id, framework)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": _BOARD_TOOLCHAIN_MARKER_SCHEMA,
        "platform": str(platform),
        "platform_version": _installed_platform_version(pio_core_dir, platform),
        "board": str(board_id),
        "framework": str(framework or "arduino"),
        "install_mode": "main-app-board-specific-dummy-build",
        "package_dirs": _installed_package_dir_names(pio_core_dir),
    }
    temporary = marker.with_name(marker.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


# Cache for PlatformIO CLI query results (avoid repeated subprocess calls)
_PIO_PLATFORM_LIST_CACHE: dict[str, bool] = {}


def _platform_installed_via_pio_cli(pio_core_dir: str, platform: str) -> bool:
    """Fallback: ask PlatformIO directly if platform is installed.
    Results are cached to avoid repeated subprocess calls."""
    cache_key = f"{pio_core_dir}:{platform}"
    if cache_key in _PIO_PLATFORM_LIST_CACHE:
        return _PIO_PLATFORM_LIST_CACHE[cache_key]
    
    pio = find_pio()
    if not pio:
        _PIO_PLATFORM_LIST_CACHE[cache_key] = False
        return False
    
    try:
        # Set the core dir so pio looks in the right place
        env = os.environ.copy()
        env["PLATFORMIO_CORE_DIR"] = pio_core_dir
        cmd = list(pio) + ["platform", "list", "--json-output"]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=15,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            # data is a list of platform dicts with 'name' field
            installed = any(p.get("name") == platform for p in data if isinstance(p, dict))
            _PIO_PLATFORM_LIST_CACHE[cache_key] = installed
            return installed
    except Exception:
        pass
    
    _PIO_PLATFORM_LIST_CACHE[cache_key] = False
    return False


def _platformio_manager_item(raw_line: str) -> tuple[str, str]:
    """Return (kind, item) for a PlatformIO Tool/Platform Manager line."""
    stripped = str(raw_line or "").strip()
    low = stripped.lower()
    if "tool manager:" in low:
        kind = "Toolchain/Tool"
        body = re.sub(r"^Tool Manager:\s*", "", stripped, flags=re.IGNORECASE)
    elif "platform manager:" in low:
        kind = "Platform/Framework"
        body = re.sub(r"^Platform Manager:\s*", "", stripped, flags=re.IGNORECASE)
    else:
        return "Package", stripped

    body = re.sub(
        r"^(?:Installing|Downloading|Unpacking|Removing)\s+",
        "", body, flags=re.IGNORECASE,
    ).strip()
    body = re.split(r"\s+has been installed!?$", body, flags=re.IGNORECASE)[0].strip()
    return kind, body or "PlatformIO package"


def _platformio_output_segments(raw_line: str) -> list[str]:
    """Split one PlatformIO stdout read into the visual updates it contained.

    PlatformIO progress bars commonly redraw with ``\\r`` rather than emitting a
    fresh newline for every percentage.  Treating the entire read as one line can
    leave the first visible percentage (10%, 40%, etc.) in the log even though the
    same output chunk later reached 100%.  Strip ANSI control codes and preserve
    each carriage-return update separately so the GUI mirrors PlatformIO's real
    current state.
    """
    text = str(raw_line or "")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return [part.strip() for part in re.split(r"[\r\n]+", text) if part.strip()]


def _platformio_live_bar(label: str, phase: str, item: str, pct: int | None) -> str:
    """Render one replace-in-place package progress row using the bootstrap style."""
    safe_item = item or "PlatformIO package"
    if pct is None:
        return f"  {label}: {phase} — {safe_item}"
    pct = max(0, min(100, int(pct)))
    width = 30
    filled = int(round(width * pct / 100.0))
    bar = "▰" * filled + "▱" * (width - filled)
    return f"  {label}: {phase} — {safe_item}\n  {bar}  {pct:3d}%"


def _stream_platformio_setup(
    cmd: list[str],
    env: dict,
    *,
    cwd: Path | None = None,
    label: str = "PlatformIO",
    stage: str = "Preparing packages",
    progress_start: float = 0.0,
    progress_end: float = 100.0,
    timeout: int = 1200,
    on_download_start=None,
    on_line=None,
    on_status=None,
    on_progress=None,
    cancel_requested=None,
    on_process=None,
) -> bool:
    """Run one PlatformIO command with phase-accurate logging and a real timeout.

    PlatformIO occasionally spends a long time inside Package Manager before it
    emits another newline. Reading ``stdout.readline()`` in the setup thread made
    the old timeout ineffective because the reader itself could block forever.
    A background reader + queue keeps the setup UI responsive and lets the
    absolute command timeout work even while PlatformIO is silent.
    """
    current_item = ""
    current_kind = "Package"
    active_phase: str | None = None
    active_phase_item = ""
    active_phase_kind = "Package"
    active_pct: int | None = None
    announced_installs: set[str] = set()
    announced_done: set[str] = set()
    tail: list[str] = []
    coarse_progress = float(progress_start)

    def _set_coarse_progress(target: float):
        nonlocal coarse_progress
        target = max(float(progress_start), min(float(progress_end), float(target)))
        if target <= coarse_progress:
            return
        coarse_progress = target
        if _gui:
            _gui.set_progress_percent(coarse_progress)
        if callable(on_progress):
            try:
                on_progress(coarse_progress)
            except Exception:
                pass

    def _start_phase(phase: str, item: str, kind: str, pct: int | None):
        nonlocal active_phase, active_phase_item, active_phase_kind, active_pct
        if active_phase and (phase != active_phase or item != active_phase_item):
            _finish_phase(inferred=True)
        active_phase = phase
        active_phase_item = item or current_item or "PlatformIO package"
        active_phase_kind = kind or current_kind
        active_pct = None if pct is None else max(0, min(100, int(pct)))
        status(f"{label}: {phase} {active_phase_kind} - {active_phase_item}")
        if callable(on_status):
            try:
                on_status(
                    f"{label}: {phase} {active_phase_item}"
                    + (f"... {active_pct}%" if active_pct is not None else "...")
                )
            except Exception:
                pass
        if _gui:
            _gui.set_status(
                f"{label}: {phase} {active_phase_item}"
                + (f"... {active_pct}%" if active_pct is not None else "...")
            )
            _gui.update_platformio_progress_block(
                _platformio_live_bar(label, phase, active_phase_item, active_pct)
            )

    def _update_phase(pct: int | None):
        nonlocal active_pct
        if active_phase is None:
            return
        if pct is not None:
            active_pct = max(0, min(100, int(pct)))
        if callable(on_status):
            try:
                on_status(
                    f"{label}: {active_phase} {active_phase_item}"
                    + (f"... {active_pct}%" if active_pct is not None else "...")
                )
            except Exception:
                pass
        if _gui:
            _gui.set_status(
                f"{label}: {active_phase} {active_phase_item}"
                + (f"... {active_pct}%" if active_pct is not None else "...")
            )
            _gui.update_platformio_progress_block(
                _platformio_live_bar(label, active_phase, active_phase_item, active_pct)
            )

    def _finish_phase(*, inferred: bool = False):
        nonlocal active_phase, active_phase_item, active_phase_kind, active_pct
        if active_phase is None:
            return
        final_pct = 100 if inferred or active_pct == 100 else active_pct
        if final_pct == 100:
            past = "Downloaded" if active_phase == "Downloading" else "Unpacked"
            if _gui:
                _gui.update_platformio_progress_block(
                    _platformio_live_bar(label, active_phase, active_phase_item, 100)
                )
                _gui.clear_platformio_progress_block()
                _gui.set_status(f"{label}: {past} {active_phase_item} - 100%")
        elif _gui:
            _gui.clear_platformio_progress_block()
        active_phase = None
        active_phase_item = ""
        active_phase_kind = "Package"
        active_pct = None

    def _record_line(stripped: str):
        nonlocal current_item, current_kind
        tail.append(stripped)
        if len(tail) > 120:
            del tail[:30]
        if callable(on_line):
            try:
                on_line(stripped)
            except Exception:
                pass
        low = stripped.lower()

        if "tool manager:" in low or "platform manager:" in low:
            kind, item = _platformio_manager_item(stripped)
            if "installing" in low:
                if active_phase and item != active_phase_item:
                    _finish_phase(inferred=True)
                current_item, current_kind = item, kind
                key = f"{kind}:{item}"
                if key not in announced_installs:
                    announced_installs.add(key)
                    if _gui:
                        _gui.set_status(f"{label}: Checking {item}...")
                span = max(0.0, float(progress_end) - float(progress_start))
                _set_coarse_progress(
                    min(progress_end - 1, coarse_progress + max(0.5, span * 0.015))
                )
            elif "has been installed" in low or ("installed" in low and "installing" not in low):
                current_item, current_kind = item, kind
                if active_phase:
                    _finish_phase(inferred=True)
                key = f"{kind}:{item}"
                if key not in announced_done:
                    announced_done.add(key)
                    ok(f"{label}: Installed {kind} - {item}")
                    if _gui:
                        _gui.set_status(f"{label}: Installed {item}")
                span = max(0.0, float(progress_end) - float(progress_start))
                _set_coarse_progress(
                    min(progress_end - 0.5, coarse_progress + max(1.0, span * 0.03))
                )
            return

        if "downloading" in low or "unpacking" in low:
            if callable(on_download_start):
                try:
                    on_download_start()
                except Exception:
                    pass
            phase = "Downloading" if "downloading" in low else "Unpacking"
            item = current_item or "PlatformIO package"
            kind = current_kind
            pcts = re.findall(r"(\d+(?:\.\d+)?)%", stripped)
            pct = int(float(pcts[-1])) if pcts else None
            if active_phase != phase or active_phase_item != item:
                _start_phase(phase, item, kind, pct)
            else:
                _update_phase(pct)
            if pct is not None and pct >= 100:
                _finish_phase(inferred=False)

    def _write_failure_log(note: str = ""):
        try:
            log_dir = SCRIPT_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            body = "COMMAND: " + " ".join(map(str, cmd)) + "\n"
            if note:
                body += note.rstrip() + "\n"
            body += "\n" + "\n".join(tail) + "\n"
            (log_dir / "bootstrap_platformio.log").write_text(
                body, encoding="utf-8", errors="replace"
            )
        except Exception:
            pass

    def _notify_process(value):
        if callable(on_process):
            try:
                on_process(value)
            except Exception:
                pass

    def _terminate_tree(proc):
        if not proc or proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    if _gui:
        _gui.set_progress_percent(progress_start)
        _gui.set_status(f"{label}: {stage}")
    if callable(on_progress):
        try:
            on_progress(progress_start)
        except Exception:
            pass
    if callable(on_status):
        try:
            on_status(f"{label}: {stage}")
        except Exception:
            pass

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        _notify_process(proc)

        line_queue: queue.Queue = queue.Queue()
        reader_done = [False]

        def _reader():
            try:
                if proc.stdout:
                    buffer: list[str] = []
                    while True:
                        ch = proc.stdout.read(1)
                        if not ch:
                            if buffer:
                                line_queue.put("".join(buffer))
                            break
                        if ch in ("\r", "\n"):
                            if buffer:
                                line_queue.put("".join(buffer))
                                buffer.clear()
                        else:
                            buffer.append(ch)
            except Exception:
                pass
            finally:
                reader_done[0] = True
                line_queue.put(None)

        threading.Thread(
            target=_reader,
            daemon=True,
            name=f"Bootstrap-PIO-{label}",
        ).start()

        started_at = time.monotonic()
        last_output_at = started_at
        last_heartbeat_at = started_at

        while True:
            now = time.monotonic()
            if callable(cancel_requested):
                try:
                    if cancel_requested():
                        _terminate_tree(proc)
                        _write_failure_log("CANCELLED by caller")
                        if _gui:
                            _gui.clear_platformio_progress_block()
                        if callable(on_status):
                            try:
                                on_status(f"{label}: cancelled")
                            except Exception:
                                pass
                        _notify_process(None)
                        return False
                except Exception:
                    pass
            if now - started_at > max(1, int(timeout)):
                _terminate_tree(proc)
                raise subprocess.TimeoutExpired(cmd, timeout)

            try:
                raw = line_queue.get(timeout=0.2)
            except queue.Empty:
                if now - last_output_at >= 300.0 and proc.poll() is None:
                    _terminate_tree(proc)
                    raise RuntimeError(
                        f"PlatformIO produced no output for {int(now - last_output_at)}s while processing "
                        f"{current_item or stage}"
                    )
                # A silent Package Manager check is not necessarily frozen. Keep
                # the UI explicit while still enforcing the absolute timeout.
                if (
                    _gui and current_item and active_phase is None
                    and now - last_output_at >= 3.0
                    and now - last_heartbeat_at >= 1.0
                ):
                    last_heartbeat_at = now
                    _gui.set_status(
                        f"{label}: checking {current_item}... "
                        f"{int(now - last_output_at)}s since last PlatformIO output"
                    )
                if reader_done[0] and proc.poll() is not None:
                    break
                continue

            if raw is None:
                if reader_done[0] and proc.poll() is not None:
                    break
                continue

            last_output_at = time.monotonic()
            last_heartbeat_at = last_output_at
            for stripped in _platformio_output_segments(raw):
                _record_line(stripped)

        rc = proc.wait(timeout=15)
        if active_phase:
            _finish_phase(inferred=(rc == 0))

        if rc == 0:
            if _gui:
                _gui.clear_platformio_progress_block()
            _set_coarse_progress(progress_end)
            _notify_process(None)
            return True

        if _gui:
            _gui.clear_platformio_progress_block()
        _write_failure_log(f"EXIT CODE: {rc}")
        warn(f"{label}: {stage} failed (exit {rc}); details saved to logs/bootstrap_platformio.log")
        _notify_process(None)
        return False
    except subprocess.TimeoutExpired:
        _terminate_tree(proc)
        _notify_process(None)
        if _gui:
            _gui.clear_platformio_progress_block()
        _write_failure_log(f"TIMEOUT: {timeout}s")
        warn(
            f"{label}: {stage} timed out after {timeout}s; "
            "details saved to logs/bootstrap_platformio.log"
        )
        return False
    except Exception as exc:
        _terminate_tree(proc)
        _notify_process(None)
        if _gui:
            _gui.clear_platformio_progress_block()
        _write_failure_log(f"EXCEPTION: {exc}")
        warn(f"{label}: {stage} failed: {exc}")
        return False


def _prewarm_pio_platform(
    pio: list[str], platform: str, env: dict, label: str | None = None,
    on_download_start=None,
) -> tuple[bool, list[str], set[str], dict[str, list[str]], list[str]]:
    """Run one tiny real Compile per dynamically discovered toolchain family."""
    display = label or _PLATFORM_INFO.get(platform, (platform, ""))[0]
    pio_core_dir = env.get("PLATFORMIO_CORE_DIR") or os.environ.get("PLATFORMIO_CORE_DIR", "")
    dummy_dir = SCRIPT_DIR / f".pio_bootstrap_first_use_{platform}"
    required_packages: set[str] = set()
    board_requirements: dict[str, list[str]] = {}

    try:
        if dummy_dir.exists():
            shutil.rmtree(str(dummy_dir), ignore_errors=True)
        dummy_dir.mkdir(parents=True, exist_ok=True)
        hide_hidden_attribute(dummy_dir)

        boards, coverage_keys, resolved_count, relevant_total, unresolved = _discover_first_use_coverage(
            pio_core_dir, platform
        )
        if not boards:
            warn(f"{display}: no Arduino-compatible PlatformIO board manifests were found for first-use preparation.")
            return False, [], set(), {}, []

        env_rows: list[tuple[str, str]] = []
        ini_parts: list[str] = []
        for idx, board_id in enumerate(boards, 1):
            safe_env = re.sub(r"[^A-Za-z0-9_]+", "_", board_id).strip("_") or f"board_{idx}"
            env_name = f"prewarm_{safe_env}"
            env_rows.append((env_name, board_id))
            ini_parts.append(
                f"[env:{env_name}]\nplatform = {platform}\nboard = {board_id}\nframework = arduino\n"
            )
        (dummy_dir / "platformio.ini").write_text("\n".join(ini_parts), encoding="utf-8")
        src_dir = dummy_dir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "main.cpp").write_text(
            "#include <Arduino.h>\nvoid setup(){}\nvoid loop(){}\n", encoding="utf-8"
        )

        status(
            f"{display}: Validating toolchain coverage across {len(coverage_keys)} MCU families "
            f"({', '.join(coverage_keys)})..."
        )

        total = max(1, len(env_rows))
        overall_start, overall_end = 35.0, 96.0
        total_span = overall_end - overall_start
        failures: list[str] = []
        for index, (env_name, board_id) in enumerate(env_rows, 1):
            board_start = overall_start + total_span * ((index - 1) / total)
            board_end = overall_start + total_span * (index / total)
            if _gui:
                _gui.set_status(f"{display}: Validating {board_id} ({index}/{total})...")
            build_ok = _stream_platformio_setup(
                list(pio) + ["run", "-e", env_name],
                env,
                cwd=dummy_dir,
                label=display,
                stage=f"First-use compile validation for {board_id}",
                progress_start=board_start,
                progress_end=board_end,
                timeout=1800,
                on_download_start=on_download_start,
            )
            if not build_ok:
                failures.append(board_id)
                warn(f"{display}: {board_id} prewarm failed; continuing with remaining families.")
                continue

            env_required = _collect_env_package_requirements(
                pio, dummy_dir, env_name, env, pio_core_dir
            )
            required_packages.update(env_required)
            board_requirements[board_id] = sorted(env_required, key=str.lower)

        if failures:
            warn(f"{display}: first-use preparation incomplete for: {', '.join(failures)}")
            return False, boards, required_packages, board_requirements, coverage_keys

        ok(f"{display}: Verified toolchain coverage across all {len(env_rows)} MCU families.")

        if unresolved:
            examples = ", ".join(unresolved[:5])
            suffix = "..." if len(unresolved) > 5 else ""
            warn(
                f"{display}: {len(unresolved)} downloaded board definition(s) ({examples}{suffix}) "
                "are pending catalog updates and will become usable automatically when published."
            )

        return True, boards, required_packages, board_requirements, coverage_keys
    finally:
        try:
            if dummy_dir.exists():
                shutil.rmtree(str(dummy_dir), ignore_errors=True)
        except Exception:
            pass


def _check_and_extract_pio_zip_bundle(pio_core_dir: Path) -> bool:
    """Check if a pre-built platformio_esp32_bundle.zip or pio_bundle.zip exists in candidate paths.
    If found, extracts directly into pio_core_dir in ~5s with a live progress bar."""
    candidates = [
        SCRIPT_DIR / "platformio_esp32_bundle.zip",
        SCRIPT_DIR / "pio_bundle.zip",
        SCRIPT_DIR / "src" / "platformio_esp32_bundle.zip",
        _get_board_download_dir() / "platformio_esp32_bundle.zip",
    ]

    zip_file = None
    for c in candidates:
        if c.is_file():
            zip_file = c
            break

    if not zip_file:
        return False

    try:
        import zipfile
        status(f"Found pre-packaged ZIP bundle: {zip_file.name}")
        status("Extracting pre-built PlatformIO core & toolchains into env/.platformio...")
        pio_core_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_file, 'r') as z:
            members = z.infolist()
            total = len(members)
            for idx, member in enumerate(members, 1):
                z.extract(member, pio_core_dir)
                if idx % 100 == 0 or idx == total:
                    pct = int(idx * 100 / total)
                    if _gui:
                        _gui.set_progress_percent(pct)
                    status(f"  Extracting toolchain packages… {pct}%")

        ok("Pre-packaged PlatformIO core & toolchains extracted successfully!")
        return True
    except Exception as e:
        warn(f"Failed to extract ZIP bundle {zip_file.name}: {e}")
        return False



def _log_platformio_first_install_warning(label: str, size_hint: str) -> None:
    """Show the interruption warning only when this platform is actually missing."""
    msg = f"{label}: Downloading core framework & toolchains ({size_hint}). Please do NOT interrupt setup."
    if _gui:
        _gui.log_warn(msg)
    else:
        warn(msg)

def ensure_board_toolchains() -> bool:
    """Prepare PlatformIO packages exactly the way the main app's first Compile does.

    Bootstrap installs the development platform, then runs a tiny temporary
    no-upload project. For ESP32 it dynamically chooses one Arduino board for
    each distinct MCU family exposed by the installed platform. The resulting
    frameworks/toolchains stay in the single shared PLATFORMIO_CORE_DIR, while
    the temporary build workspace is deleted.

    This is intentionally different from requiring every package listed in
    platform.json: many of those entries are optional alternate frameworks,
    legacy cores, or debug tools and are not required by normal Arduino builds.
    """
    pio = find_pio()
    if not pio:
        warn("PlatformIO not found - skipping board toolchain pre-install.")
        return False

    platforms = _scan_downloaded_platforms(pio)
    if not platforms:
        # Bootstrap itself prepares the default Arduino AVR + ESP32 Board Browser
        # cores before this step, so retain them only as a last-resort fallback
        # if PlatformIO's global board catalog could not be queried.
        platforms = {"espressif32", "atmelavr"}
    pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR") or _get_safe_platformio_core_dir(SCRIPT_DIR)
    os.environ["PLATFORMIO_CORE_DIR"] = pio_core_dir
    status(f"Shared PlatformIO package store: {pio_core_dir}")

    platforms_to_prepare: list[tuple[str, str, str]] = []
    already_ready_labels: list[str] = []
    for platform in sorted(platforms):
        label, size_hint = _PLATFORM_INFO.get(platform, (platform, "a one-time"))
        if _platform_already_installed(pio_core_dir, platform):
            already_ready_labels.append(label)
        else:
            platforms_to_prepare.append((platform, label, size_hint))

    if already_ready_labels:
        ok("First-use board environments already prepared (" + ", ".join(already_ready_labels) + ")")

    if not platforms_to_prepare:
        return True

    if not _is_network_reachable(timeout=2.0):
        missing_labels = ", ".join(lbl for _, lbl, _ in platforms_to_prepare)
        warn(f"Required board toolchains ({missing_labels}) are not yet installed and cannot be prepared while offline.")
        return False

    _check_and_extract_pio_zip_bundle(Path(pio_core_dir))

    all_ok = True
    for platform, label, size_hint in platforms_to_prepare:
        if _platform_already_installed(pio_core_dir, platform):
            ok(f"{label}: first-use environment already prepared.")
            continue

        section(f"Preparing {label} Framework & Toolchain")
        status(f"Verifying package readiness for {label}...")

        env = os.environ.copy()
        env["PLATFORMIO_CORE_DIR"] = pio_core_dir
        env["PLATFORMIO_NO_TELEMETRY"] = "1"
        env["PLATFORMIO_DISABLE_TELEMETRY"] = "1"

        warning_shown = [False]
        def _warn_once():
            if warning_shown[0]:
                return
            warning_shown[0] = True
            _log_platformio_first_install_warning(label, size_hint)

        # Ensure the development platform itself exists first. This command is
        # idempotent and normally returns almost immediately when already present.
        platform_ok = _stream_platformio_setup(
            list(pio) + ["platform", "install", platform],
            env,
            label=label,
            stage=f"Checking PlatformIO platform {platform}",
            progress_start=5,
            progress_end=32,
            timeout=1800,
            on_download_start=_warn_once,
        )
        if not platform_ok:
            warn(f"{label}: PlatformIO platform preparation did not complete.")
            all_ok = False
            continue

        prewarm_ok, prewarm_boards, required_packages, board_requirements, coverage_keys = _prewarm_pio_platform(
            pio, platform, env, label=label, on_download_start=_warn_once
        )
        if not prewarm_ok:
            warn(
                f"{label}: first-use dummy compile did not complete. "
                "Details are saved to logs/bootstrap_platformio.log."
            )
            all_ok = False
            continue

        _PIO_PLATFORM_LIST_CACHE.clear()
        try:
            _write_full_family_marker(
                pio_core_dir, platform, prewarm_boards,
                required_packages, board_requirements, coverage_keys,
            )
        except Exception as exc:
            warn(f"{label}: packages built successfully, but readiness marker could not be saved ({exc}).")
            all_ok = False
            continue

        if _platform_already_installed(pio_core_dir, platform):
            if _gui:
                _gui.set_progress_percent(100)
            ok(f"{label}: first-use framework/toolchain preparation is complete.")
        else:
            warn(f"{label}: first-use build succeeded but readiness verification did not persist.")
            all_ok = False

    return all_ok


def prepare_platformio_board_toolchain(
    platform: str,
    board_id: str,
    framework: str = "arduino",
    label: str | None = None,
    *,
    on_line=None,
    on_status=None,
    on_progress=None,
    on_download_start=None,
    cancel_requested=None,
    on_process=None,
) -> bool:
    """Install and prove one board environment on demand from the main app.

    This deliberately shares the bootstrap PlatformIO command runner and the
    app-owned ``PLATFORMIO_CORE_DIR``.  The platform install is idempotent;
    the temporary compile is what makes framework/toolchain readiness real for
    the selected board, including packages that PlatformIO resolves only after
    it has inspected that board's manifest.
    """
    platform = str(platform or "").strip()
    board_id = str(board_id or "").strip()
    framework = str(framework or "arduino").strip() or "arduino"
    display = str(label or board_id or platform).strip() or platform
    if not platform or not board_id:
        if callable(on_status):
            try:
                on_status("Board toolchain preparation cannot start: board metadata is incomplete.")
            except Exception:
                pass
        return False

    with _BOARD_TOOLCHAIN_PREPARE_LOCK:
        pio = find_pio()
        if not pio:
            if callable(on_status):
                try:
                    on_status("PlatformIO Core not found; installing it first...")
                except Exception:
                    pass
            if not ensure_platformio():
                return False
            pio = find_pio()
        if not pio:
            return False

        pio_core_dir = os.environ.get("PLATFORMIO_CORE_DIR") or str(
            _get_safe_platformio_core_dir(SCRIPT_DIR)
        )
        os.environ["PLATFORMIO_CORE_DIR"] = pio_core_dir
        if board_toolchain_ready(pio_core_dir, platform, board_id, framework):
            if callable(on_status):
                try:
                    on_status(f"{display}: board framework/toolchain is already ready.")
                except Exception:
                    pass
            if callable(on_progress):
                try:
                    on_progress(100)
                except Exception:
                    pass
            return True
        env = os.environ.copy()
        env["PLATFORMIO_CORE_DIR"] = pio_core_dir
        env["PLATFORMIO_NO_TELEMETRY"] = "1"
        env["PLATFORMIO_DISABLE_TELEMETRY"] = "1"

        if callable(on_status):
            try:
                on_status(f"Preparing {display} Framework & Toolchain...")
            except Exception:
                pass

        try:
            _check_and_extract_pio_zip_bundle(Path(pio_core_dir))
        except Exception:
            # A bundled archive is optional. PlatformIO can still download the
            # missing package from its registry, so do not block the normal path.
            pass

        warning_sent = [False]

        def _announce_download():
            if not warning_sent[0]:
                warning_sent[0] = True
                _log_platformio_first_install_warning(
                    display,
                    _PLATFORM_INFO.get(platform, (platform, "a one-time"))[1],
                )
            if callable(on_download_start):
                try:
                    on_download_start()
                except Exception:
                    pass

        platform_ok = _stream_platformio_setup(
            list(pio) + ["platform", "install", platform],
            env,
            label=display,
            stage=f"Checking PlatformIO platform {platform}",
            progress_start=0,
            progress_end=30,
            timeout=1800,
            on_download_start=_announce_download,
            on_line=on_line,
            on_status=on_status,
            on_progress=on_progress,
            cancel_requested=cancel_requested,
            on_process=on_process,
        )
        if not platform_ok:
            return False

        temporary_root = Path(tempfile.mkdtemp(prefix="mcu_flasher_toolchain_"))
        try:
            safe_board = re.sub(r"[^A-Za-z0-9_]+", "_", board_id).strip("_") or "board"
            env_name = f"prepare_{safe_board[:48]}"
            (temporary_root / "platformio.ini").write_text(
                f"[env:{env_name}]\n"
                f"platform = {platform}\n"
                f"board = {board_id}\n"
                f"framework = {framework}\n",
                encoding="utf-8",
            )
            source_dir = temporary_root / "src"
            source_dir.mkdir(parents=True, exist_ok=True)
            (source_dir / "main.cpp").write_text(
                "#include <Arduino.h>\nvoid setup(){}\nvoid loop(){}\n",
                encoding="utf-8",
            )

            if callable(on_status):
                try:
                    on_status(f"{display}: validating framework and toolchain for {board_id}...")
                except Exception:
                    pass
            build_ok = _stream_platformio_setup(
                list(pio) + ["run", "-e", env_name],
                env,
                cwd=temporary_root,
                label=display,
                stage=f"First-use compile validation for {board_id}",
                progress_start=30,
                progress_end=100,
                timeout=1800,
                on_download_start=_announce_download,
                on_line=on_line,
                on_status=on_status,
                on_progress=on_progress,
                cancel_requested=cancel_requested,
                on_process=on_process,
            )
            if not build_ok:
                return False

            try:
                _write_board_toolchain_marker(pio_core_dir, platform, board_id, framework)
            except Exception as exc:
                if callable(on_status):
                    try:
                        on_status(
                            f"{display}: packages are installed, but readiness could not be cached ({exc})."
                        )
                    except Exception:
                        pass
            return True
        finally:
            shutil.rmtree(str(temporary_root), ignore_errors=True)


ESP32_BOARD_INDEX_URL = "https://espressif.github.io/arduino-esp32/package_esp32_index.json"
ESP32_BOARD_INDEX_MIRROR_URL = "https://jihulab.com/esp-mirror/espressif/arduino-esp32/-/raw/gh-pages/package_esp32_index_cn.json"


def _archive_folder_name(archive_name: str) -> str:
    lower = archive_name.lower()
    for ext in (".tar.bz2", ".tar.gz", ".tar.xz", ".zip", ".tgz", ".tbz2", ".txz"):
        if lower.endswith(ext):
            return archive_name[:-len(ext)]
    return Path(archive_name).stem


def _normalize_sha256(value: object) -> str:
    """Return a lowercase 64-hex SHA-256 from Boards Manager metadata."""
    match = re.search(r"([0-9a-fA-F]{64})", str(value or ""))
    return match.group(1).lower() if match else ""


def _esp32_release_from_index(data: object, target_version: str | None = None) -> dict | None:
    """Select one stable ESP32 platform entry and retain integrity metadata."""
    if not isinstance(data, dict):
        return None

    target_base = re.sub(r"-cn$", "", str(target_version or "").strip(), flags=re.IGNORECASE)
    candidates: list[dict] = []
    for package in data.get("packages", []):
        if not isinstance(package, dict) or str(package.get("name", "")).lower() != "esp32":
            continue
        for platform in package.get("platforms", []):
            if not isinstance(platform, dict):
                continue
            if str(platform.get("architecture", "")).lower() != "esp32":
                continue

            version = str(platform.get("version", "")).strip()
            version_base = re.sub(r"-cn$", "", version, flags=re.IGNORECASE)
            url = str(platform.get("url", "")).strip()
            archive = str(platform.get("archiveFileName", "")).strip() or url.rsplit("/", 1)[-1]
            if not version or not url or not archive:
                continue
            if re.search(r"(?:alpha|beta|rc|dev)", version, re.IGNORECASE):
                continue
            if target_base and version_base.lower() != target_base.lower():
                continue

            try:
                expected_size = int(platform.get("size", 0) or 0)
            except (TypeError, ValueError):
                expected_size = 0
            candidates.append({
                "version": version,
                "version_base": version_base,
                "url": url,
                "archive": archive,
                "size": max(0, expected_size),
                "sha256": _normalize_sha256(platform.get("checksum", "")),
            })

    if not candidates:
        return None
    candidates.sort(key=lambda item: _version_tuple(item["version_base"]), reverse=True)
    return candidates[0]


def _load_esp32_index(url: str, cache_name: str, timeout: int = 25) -> dict | None:
    """Fetch one official ESP32 index with a validated local JSON fallback."""
    cache_dir = SCRIPT_DIR / "index_json"
    cache_file = cache_dir / cache_name
    data = None

    body = _fetch_url(url, timeout=timeout)
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                data = parsed
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(body, encoding="utf-8")
        except Exception:
            data = None

    if data is None and cache_file.is_file():
        try:
            parsed = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            safe_unlink(cache_file)

    return data if isinstance(data, dict) else None


def _load_latest_esp32_board_release() -> dict | None:
    """Resolve the latest stable Arduino-ESP32 board-core archive.

    The canonical source is Espressif's stable Boards Manager index.  Size and
    SHA-256 metadata are retained so an interrupted/resumed download is never
    accepted merely because the HTTP connection happened to finish.
    """
    data = _load_esp32_index(
        ESP32_BOARD_INDEX_URL,
        "package_esp32_index.json",
        timeout=25,
    )
    return _esp32_release_from_index(data)


def _load_esp32_mirror_release(version: str) -> dict | None:
    """Resolve the matching release from Espressif's documented JihuLab mirror."""
    data = _load_esp32_index(
        ESP32_BOARD_INDEX_MIRROR_URL,
        "package_esp32_index_cn.json",
        timeout=30,
    )
    return _esp32_release_from_index(data, target_version=version)

def _extract_board_archive_folder_only(archive_path: Path, folder_path: Path) -> bool:
    """Extract one board archive into a folder and remove the archive afterward."""
    import tarfile
    import zipfile

    try:
        if folder_path.exists():
            safe_rmtree(folder_path)
        folder_path.mkdir(parents=True, exist_ok=True)

        if archive_path.name.lower().endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as zf:
                members = zf.infolist()
                total = max(1, len(members))
                for idx, member in enumerate(members, 1):
                    zf.extract(member, folder_path)
                    if _gui and (idx == total or idx % max(1, total // 100) == 0):
                        _gui.set_progress_percent(int(idx * 100 / total))
        else:
            mode = "r:*"
            with tarfile.open(archive_path, mode) as tf:
                members = tf.getmembers()
                total = max(1, len(members))
                for idx, member in enumerate(members, 1):
                    tf.extract(member, folder_path)
                    if _gui and (idx == total or idx % max(1, total // 100) == 0):
                        _gui.set_progress_percent(int(idx * 100 / total))

        # Board archives commonly contain one top-level directory.  Flatten it
        # so Boards/<archive-name>/boards.txt matches the Board Browser's
        # "Folder Only" layout.
        try:
            while True:
                entries = list(folder_path.iterdir())
                subdirs = [p for p in entries if p.is_dir()]
                files = [p for p in entries if p.is_file()]
                if len(subdirs) != 1 or files:
                    break
                nested = subdirs[0]
                for item in list(nested.iterdir()):
                    shutil.move(str(item), str(folder_path / item.name))
                nested.rmdir()
        except Exception:
            pass

        try:
            archive_path.unlink(missing_ok=True)
        except OSError:
            pass

        return any(folder_path.rglob("boards.txt"))
    except Exception as exc:
        warn(f"Failed to extract {archive_path.name}: {exc}")
        return False


def ensure_esp32_board_folder() -> bool:
    """Download the latest stable Arduino-ESP32 board core as a folder.

    Large GitHub release assets are downloaded resumably and verified against
    Espressif's Boards Manager size/SHA-256 metadata.  If the primary release
    asset remains unreachable, try Espressif's documented JihuLab mirror and,
    finally, the official GitHub source tag archive.  PlatformIO remains a
    separate package store; this is the app's portable Board Browser folder.
    """
    dest_dir = _get_board_download_dir() / "Boards"
    if dest_dir.is_dir():
        try:
            for boards_txt in dest_dir.glob("**/boards.txt"):
                rel = "/".join(part.lower() for part in boards_txt.relative_to(dest_dir).parts)
                if "esp32" in rel:
                    ok(f"ESP32 boards core is already downloaded as a folder ({boards_txt.parent.name}).")
                    return True
        except Exception:
            pass

    if not _is_network_reachable(timeout=2.0):
        warn("ESP32 board core is not yet downloaded and cannot be prepared while offline.")
        return False

    section("Preparing ESP32 Boards (Folder)")
    status("Resolving latest stable ESP32 board core from Espressif...")
    release = _load_latest_esp32_board_release()
    if not release:
        # A regional mirror can also supply the stable index itself.  This is
        # especially useful where GitHub Pages is slow or blocked.
        mirror_data = _load_esp32_index(
            ESP32_BOARD_INDEX_MIRROR_URL,
            "package_esp32_index_cn.json",
            timeout=30,
        )
        release = _esp32_release_from_index(mirror_data)
        if release:
            release["source"] = "Espressif JihuLab mirror"
        else:
            warn("Could not resolve the ESP32 board-core archive from either official index. PlatformIO ESP32 support can still be prepared separately.")
            return False

    release.setdefault("source", "Espressif stable index")
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive_path = dest_dir / release["archive"]
    folder_path = dest_dir / _archive_folder_name(release["archive"])
    partial_path = archive_path.with_name(archive_path.name + ".part")

    primary_error = None
    try:
        status(f"Downloading ESP32 Boards {release['version']} (Folder Only)...")
        _download_file(
            release["url"],
            archive_path,
            timeout=300,
            attempts=5,
            expected_size=release.get("size", 0),
            expected_sha256=release.get("sha256", ""),
        )
    except Exception as exc:
        primary_error = exc

        # Do not append bytes from a different mirror/archive to the primary
        # partial file. Each source starts with a clean partial artifact.
        safe_unlink(partial_path)
        safe_unlink(archive_path)

        mirror = _load_esp32_mirror_release(release["version_base"])
        if mirror and mirror.get("url") and mirror.get("url") != release.get("url"):
            try:
                status(f"Primary ESP32 download was interrupted; trying Espressif mirror for {release['version_base']}...")
                _download_file(
                    mirror["url"],
                    archive_path,
                    timeout=300,
                    attempts=4,
                    expected_size=mirror.get("size", 0),
                    expected_sha256=mirror.get("sha256", ""),
                )
                primary_error = None
            except Exception as mirror_exc:
                primary_error = RuntimeError(f"primary source: {exc}; mirror: {mirror_exc}")
                safe_unlink(partial_path)
                safe_unlink(archive_path)

        # Last source-only fallback. This is still Espressif's official GitHub
        # repository, but unlike the Boards Manager release asset it travels via
        # GitHub's tag/codeload path and therefore avoids the release-assets CDN.
        if primary_error is not None:
            source_version = release["version_base"]
            source_url = f"https://github.com/espressif/arduino-esp32/archive/refs/tags/{source_version}.zip"
            try:
                status(f"Trying official ESP32 source archive for {source_version}...")
                _download_file(
                    source_url,
                    archive_path,
                    timeout=300,
                    attempts=4,
                )
                primary_error = None
            except Exception as source_exc:
                primary_error = RuntimeError(f"{primary_error}; source archive: {source_exc}")

    if primary_error is not None:
        warn(f"Failed to download ESP32 Boards folder from all official sources: {primary_error}")
        return False

    try:
        status(f"Unpacking ESP32 Boards {release['version_base']} into {folder_path.name}...")
        if _gui:
            _gui.set_progress_percent(0)
        if not _extract_board_archive_folder_only(archive_path, folder_path):
            warn("ESP32 board-core archive was downloaded, but boards.txt could not be verified after extraction.")
            safe_rmtree(folder_path)
            return False
        ok(f"ESP32 Boards {release['version_base']} downloaded as folder: Boards/{folder_path.name}")
        return True
    except Exception as exc:
        warn(f"Failed to prepare ESP32 Boards folder: {exc}")
        return False

def ensure_arduino_avr_board() -> bool:
    """Pre-download and extract Arduino AVR Boards framework if not present."""
    dest_dir = _get_board_download_dir() / "Boards"
    if dest_dir.is_dir():
        for p in dest_dir.glob("**/boards.txt"):
            parent_name = p.parent.name.lower()
            if "avr" in parent_name or "uno" in parent_name:
                ok("Arduino AVR boards framework is already downloaded.")
                return True

    if not _is_network_reachable(timeout=2.0):
        warn("Arduino AVR boards framework is not yet downloaded and cannot be prepared while offline.")
        return False

    section("Preparing Arduino AVR Boards")
    status("Preparing Arduino AVR Boards core (v1.8.6) to enable AVR compilation...")

    candidate_urls = [
        "https://downloads.arduino.cc/cores/staging/avr-1.8.6.tar.bz2",
        "https://downloads.arduino.cc/cores/avr-1.8.6.tar.bz2",
        "https://downloads.arduino.cc/cores/avr-1.8.5.tar.bz2",
    ]

    dest_dir.mkdir(parents=True, exist_ok=True)
    download_success = False
    filepath = None
    last_err = None

    for url in candidate_urls:
        archive_name = url.rsplit("/", 1)[-1]
        candidate_path = dest_dir / archive_name
        try:
            _download_file(url, candidate_path)
            download_success = True
            filepath = candidate_path
            break
        except Exception as e:
            last_err = e

    if not download_success or not filepath:
        warn(f"Failed to setup/extract Arduino AVR Boards: {last_err}")
        return False

    try:
        status("Extracting Arduino AVR Boards...")
        folder_path = dest_dir / "avr-1.8.6"
        folder_path.mkdir(parents=True, exist_ok=True)

        import tarfile
        with tarfile.open(str(filepath), 'r:bz2') as tar_ref:
            tar_ref.extractall(str(folder_path))

        # Self-heal / flatten double nesting if present
        try:
            subdirs = [p for p in folder_path.iterdir() if p.is_dir()]
            files = [p for p in folder_path.iterdir() if p.is_file()]
            if len(subdirs) == 1 and len(files) == 0:
                nested = subdirs[0]
                for item in nested.iterdir():
                    shutil.move(str(item), str(folder_path))
                nested.rmdir()
        except Exception:
            pass

        try:
            filepath.unlink()
        except OSError:
            pass

        ok("Arduino AVR Boards framework configured and extracted successfully.")
        return True
    except Exception as e:
        warn(f"Failed to setup/extract Arduino AVR Boards: {e}")
        return False


# ── Download helper ───────────────────────────────────────────
def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().lower()


def _download_matches_expectations(
    path: Path,
    expected_size: int = 0,
    expected_sha256: str = "",
) -> tuple[bool, str]:
    """Validate a completed/partial artifact against official metadata."""
    if not path.is_file():
        return False, "file does not exist"

    size = path.stat().st_size
    if size <= 0:
        return False, "file is empty"
    if expected_size and size != expected_size:
        return False, f"size mismatch ({size} of {expected_size} bytes)"

    expected_hash = _normalize_sha256(expected_sha256)
    if expected_hash:
        actual_hash = _file_sha256(path)
        if actual_hash != expected_hash:
            return False, f"SHA-256 mismatch ({actual_hash[:12]}... != {expected_hash[:12]}...)"
    return True, "verified"


def _curl_resume_download(
    url: str,
    partial: Path,
    *,
    timeout: int,
    expected_size: int = 0,
) -> None:
    """Use Windows/system curl as a second HTTP stack when urllib keeps dropping."""
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise RuntimeError("curl is not available")

    def _run(resume: bool) -> subprocess.CompletedProcess:
        cmd = [
            curl,
            "--location",
            "--fail",
            "--silent",
            "--show-error",
            "--retry", "4",
            "--retry-delay", "2",
            "--retry-all-errors",
            "--connect-timeout", "20",
            "--speed-time", "45",
            "--speed-limit", "1024",
            "--output", str(partial),
        ]
        if resume and partial.exists() and partial.stat().st_size > 0:
            cmd.extend(["--continue-at", "-"])
        cmd.append(url)
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(180, timeout * 2),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

    if _gui:
        _gui.start_busy()

    had_partial = partial.exists() and partial.stat().st_size > 0
    result = _run(resume=had_partial)
    if result.returncode != 0 and had_partial:
        # Some CDNs reject Range even though a normal full transfer works.
        safe_unlink(partial)
        result = _run(resume=False)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"curl exit {result.returncode}").strip()
        raise RuntimeError(detail[-500:])

    if expected_size and partial.stat().st_size != expected_size:
        raise OSError(f"curl returned {partial.stat().st_size} of {expected_size} bytes")


class _RangeDownloadUnsupported(RuntimeError):
    """Raised when the GitHub/CDN endpoint does not honor byte ranges."""


def _parallel_range_download(
    url: str,
    partial: Path,
    *,
    expected_size: int,
    display_name: str,
    timeout: int,
) -> None:
    """Download a large artifact using a few resumable HTTP range requests.

    Each range has its own checkpoint, so a dropped connection only costs the
    affected segment. The final ``.part`` file is assembled without a second
    full-size copy and is still validated by ``_download_file`` before
    promotion to the target.
    """
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if expected_size <= 0:
        raise ValueError("parallel download requires an expected size")

    parts_dir = partial.with_name(partial.name + ".parts")
    parts_dir.mkdir(parents=True, exist_ok=True)
    assemble_path = partial.with_name(partial.name + ".assemble")
    # Clean up an abandoned temporary from an older interrupted implementation.
    safe_unlink(assemble_path)

    prefix_size = partial.stat().st_size if partial.is_file() else 0
    if prefix_size > expected_size:
        safe_unlink(partial)
        prefix_size = 0

    chunk_size = 64 * 1024 * 1024
    chunk_count = (expected_size + chunk_size - 1) // chunk_size
    cpu_count = os.cpu_count() or 1
    worker_count = 1 if cpu_count <= 1 else (2 if cpu_count <= 5 else 3)
    worker_count = min(worker_count, chunk_count)

    chunk_specs: list[tuple[int, int, Path, int]] = []
    for index in range(chunk_count):
        chunk_start = index * chunk_size
        chunk_end = min(expected_size, chunk_start + chunk_size) - 1
        chunk_path = parts_dir / f"{index:05d}.part"

        # A pre-existing single-stream prefix owns these bytes.  Discard an
        # overlapping checkpoint because its original byte offset is unknown.
        if chunk_end < prefix_size:
            safe_unlink(chunk_path)
            continue
        effective_start = max(chunk_start, prefix_size)
        if chunk_start < prefix_size:
            safe_unlink(chunk_path)

        expected_chunk_size = chunk_end - effective_start + 1
        current_size = chunk_path.stat().st_size if chunk_path.is_file() else 0
        if current_size > expected_chunk_size:
            safe_unlink(chunk_path)
            current_size = 0
        chunk_specs.append((index, effective_start, chunk_path, expected_chunk_size))

    bytes_by_chunk = [0] * chunk_count
    for index, _start, chunk_path, expected_chunk_size in chunk_specs:
        current_size = chunk_path.stat().st_size if chunk_path.is_file() else 0
        bytes_by_chunk[index] = min(current_size, expected_chunk_size)

    progress_lock = threading.Lock()
    started_at = time.time()
    last_ui_update = 0.0
    initial_received = prefix_size + sum(bytes_by_chunk)

    def report_progress(force: bool = False) -> None:
        nonlocal last_ui_update
        if not _gui:
            return
        now = time.time()
        with progress_lock:
            if not force and now - last_ui_update < 0.20:
                return
            received = prefix_size + sum(bytes_by_chunk)
            last_ui_update = now
            elapsed = max(0.001, now - started_at)
            speed_bps = max(0, received - initial_received) / elapsed
            speed_str = (
                f"{speed_bps / (1024 * 1024):.2f} MB/s"
                if speed_bps >= 1024 * 1024
                else f"{speed_bps / 1024:.1f} KB/s"
            )
            pct = min(99.0, (received / expected_size) * 100.0)
            filled = int(pct / 100.0 * 30)
            bar = "▰" * filled + "▱" * (30 - filled)
            rec_mb = received / (1024 * 1024)
            total_mb = expected_size / (1024 * 1024)
            progress_block = (
                f"  Downloading {display_name} (parallel)...\n"
                f"  {bar}  {pct:5.1f}% ({rec_mb:.2f} MB / {total_mb:.2f} MB) • {speed_str}"
            )
            _gui.set_progress_percent(pct)
            _gui.set_status(
                f"Downloading {display_name}... {rec_mb:.1f}/{total_mb:.1f} MB ({pct:.1f}%) • {speed_str}"
            )
            _gui.update_platformio_progress_block(progress_block)

    def download_chunk(spec: tuple[int, int, Path, int]) -> None:
        index, start, chunk_path, expected_chunk_size = spec
        existing_size = chunk_path.stat().st_size if chunk_path.is_file() else 0
        if existing_size >= expected_chunk_size:
            with progress_lock:
                bytes_by_chunk[index] = expected_chunk_size
            report_progress()
            return

        request_start = start + existing_size
        headers = {
            "User-Agent": "MCU-Flasher-by-Naph/1.0 (Windows; ESP32 bootstrap)",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "Range": f"bytes={request_start}-{start + expected_chunk_size - 1}",
        }
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = int(getattr(response, "status", response.getcode()) or 200)
            content_range = str(response.headers.get("Content-Range", "") or "")
            match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range, re.IGNORECASE)
            if status_code != 206 or not match or int(match.group(1)) != request_start:
                raise _RangeDownloadUnsupported(
                    f"range request was not honored for bytes {request_start}-{start + expected_chunk_size - 1}"
                )
            if match.group(3) != "*" and int(match.group(3)) != expected_size:
                raise _RangeDownloadUnsupported("range response reported an unexpected total size")

            with open(chunk_path, "ab" if existing_size else "wb") as output:
                received = existing_size
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    output.write(block)
                    received += len(block)
                    with progress_lock:
                        bytes_by_chunk[index] = min(received, expected_chunk_size)
                    report_progress()
                output.flush()

        final_size = chunk_path.stat().st_size if chunk_path.is_file() else 0
        if final_size != expected_chunk_size:
            raise OSError(f"incomplete range {index} ({final_size} of {expected_chunk_size} bytes)")
        with progress_lock:
            bytes_by_chunk[index] = expected_chunk_size
        report_progress()

    if _gui:
        _gui.start_busy()
    report_progress(force=True)

    executor = ThreadPoolExecutor(max_workers=max(1, worker_count), thread_name_prefix="mcu-download")
    try:
        futures = [executor.submit(download_chunk, spec) for spec in chunk_specs]
        for future in as_completed(futures):
            future.result()
    finally:
        executor.shutdown(wait=True)

    if prefix_size + sum(bytes_by_chunk) != expected_size:
        raise OSError("parallel download did not produce all expected bytes")

    # All ranges are complete. Append them in byte order to the contiguous
    # prefix. If the process stops during this step, the resulting partial is
    # still a valid prefix and can be continued on the next launch.
    with open(partial, "ab") as output:
        for index in range(chunk_count):
            chunk_path = parts_dir / f"{index:05d}.part"
            if chunk_path.is_file():
                with open(chunk_path, "rb") as chunk:
                    shutil.copyfileobj(chunk, output, length=1024 * 1024)
        output.flush()

    if partial.stat().st_size != expected_size:
        raise OSError(f"assembled download is {partial.stat().st_size} of {expected_size} bytes")
    safe_rmtree(parts_dir)


def _download_file(
    url: str,
    dest: Path,
    timeout: int = 45,
    attempts: int = 3,
    expected_size: int | str | None = None,
    expected_sha256: str | None = None,
):
    """Download atomically, resumably, and (when metadata exists) cryptographically verify it.

    Interrupted transfers keep ``.part`` and resume with HTTP Range instead of
    restarting a large ESP32 archive from byte zero.  If urllib repeatedly
    encounters a remote disconnect, curl is tried as an independent HTTP/TLS
    implementation against the same official GitHub source.
    """
    import urllib.error
    import urllib.request

    try:
        expected_size_i = max(0, int(expected_size or 0))
    except (TypeError, ValueError):
        expected_size_i = 0
    expected_hash = _normalize_sha256(expected_sha256 or "")

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    last_error = None

    # Reuse an already completed file only when it passes the same validation
    # that a new network transfer would receive.
    if dest.is_file():
        verified, reason = _download_matches_expectations(dest, expected_size_i, expected_hash)
        if verified:
            ok(f"Using verified {dest.name}")
            if _gui:
                _gui.set_progress_percent(100)
            return
        safe_unlink(dest)

    if partial.is_file() and expected_size_i and partial.stat().st_size > expected_size_i:
        safe_unlink(partial)

    if _gui:
        _gui.set_progress_percent(0)

    # GitHub release assets support byte ranges.  Use a small number of
    # segments so low-end machines get better throughput without creating a
    # large CPU, memory, or connection burden.  The normal resumable stream
    # below remains available when a CDN/proxy does not honor ranges.
    parallel_parts = partial.with_name(partial.name + ".parts")
    if expected_size_i >= 256 * 1024 * 1024 and "github.com" in url.lower():
        try:
            status(f"Downloading {dest.name} from GitHub using parallel ranges...")
            _parallel_range_download(
                url,
                partial,
                expected_size=expected_size_i,
                display_name=dest.name,
                timeout=timeout,
            )
            verified, reason = _download_matches_expectations(
                partial,
                expected_size_i,
                expected_hash,
            )
            if not verified:
                safe_unlink(partial)
                raise OSError(reason)
            if not safe_replace_file(partial, dest):
                raise OSError(f"Could not replace '{partial}' with '{dest}'")
            safe_rmtree(parallel_parts)
            if _gui:
                _gui.clear_platformio_progress_block()
                _gui.set_progress_percent(100)
            ok(f"Saved and verified {dest.name}")
            return
        except _RangeDownloadUnsupported as exc:
            # Keep the same GitHub URL and use the existing resumable stream.
            # This is a protocol compatibility path, not a source fallback.
            safe_rmtree(parallel_parts)
            last_error = exc
            status(f"GitHub range download unavailable; continuing {dest.name} with a resumable stream...")
        except Exception as exc:
            # Preserve completed range checkpoints so a later invocation can
            # resume them.  The single-stream retry loop remains the safety net.
            last_error = exc
            if _gui:
                _gui.clear_platformio_progress_block()

    try:
        for attempt in range(1, max(1, attempts) + 1):
            resume_from = partial.stat().st_size if partial.is_file() else 0
            try:
                if attempt == 1:
                    if resume_from:
                        status(f"Resuming {dest.name} from {resume_from / 1048576:.1f} MB...")
                    else:
                        status(f"Downloading {dest.name}...")
                else:
                    status(f"Retrying {dest.name} ({attempt}/{attempts}) — keeping partial download...")

                headers = {
                    "User-Agent": "MCU-Flasher-by-Naph/1.0 (Windows; ESP32 bootstrap)",
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                }
                if resume_from > 0:
                    headers["Range"] = f"bytes={resume_from}-"

                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    status_code = int(getattr(response, "status", response.getcode()) or 200)
                    content_range = str(response.headers.get("Content-Range", "") or "")

                    # A correct resume must be HTTP 206 and start exactly at the
                    # local partial length. If a CDN ignores Range (HTTP 200),
                    # safely restart this response from byte zero instead.
                    write_mode = "ab" if resume_from > 0 and status_code == 206 else "wb"
                    base_received = resume_from if write_mode == "ab" else 0
                    if write_mode == "ab":
                        match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range, re.IGNORECASE)
                        if match and int(match.group(1)) != resume_from:
                            raise OSError(
                                f"server resumed at byte {match.group(1)} instead of {resume_from}"
                            )

                    response_length = int(response.headers.get("Content-Length", "0") or 0)
                    response_total = 0
                    range_match = re.search(r"/(\d+)$", content_range)
                    if range_match:
                        response_total = int(range_match.group(1))
                    elif response_length:
                        response_total = base_received + response_length
                    total = expected_size_i or response_total

                    with open(partial, write_mode) as output:
                        received = base_received
                        start_time = time.time()
                        last_update_time = 0.0
                        while True:
                            block = response.read(128 * 1024)
                            if not block:
                                break
                            output.write(block)
                            received += len(block)

                            now = time.time()
                            if _gui and (now - last_update_time >= 0.15 or (total and received >= total)):
                                last_update_time = now
                                elapsed = now - start_time
                                speed_bps = (received - base_received) / elapsed if elapsed > 0 else 0
                                if speed_bps >= 1024 * 1024:
                                    speed_str = f"{speed_bps / (1024 * 1024):.2f} MB/s"
                                else:
                                    speed_str = f"{speed_bps / 1024:.1f} KB/s"

                                if total > 0:
                                    pct = min(99.0, (received / total) * 100.0)
                                    filled = int(pct / 100.0 * 30)
                                    bar = "▰" * filled + "▱" * (30 - filled)
                                    rec_mb = received / (1024 * 1024)
                                    tot_mb = total / (1024 * 1024)
                                    progress_block = (
                                        f"  Downloading {dest.name}...\n"
                                        f"  {bar}  {pct:5.1f}% ({rec_mb:.2f} MB / {tot_mb:.2f} MB) • {speed_str}"
                                    )
                                    _gui.set_progress_percent(pct)
                                    _gui.set_status(f"Downloading {dest.name}... {rec_mb:.1f}/{tot_mb:.1f} MB ({pct:.1f}%) • {speed_str}")
                                else:
                                    rec_mb = received / (1024 * 1024)
                                    progress_block = (
                                        f"  Downloading {dest.name}...\n"
                                        f"  {rec_mb:.2f} MB downloaded • {speed_str}"
                                    )
                                    _gui.start_busy()
                                    _gui.set_status(f"Downloading {dest.name}... {rec_mb:.1f} MB • {speed_str}")

                                _gui.update_platformio_progress_block(progress_block)
                        output.flush()

                    if _gui:
                        _gui.clear_platformio_progress_block()

                # If the server advertised a complete size, treat an early EOF
                # as an interrupted request and retain the partial for Range retry.
                current_size = partial.stat().st_size if partial.is_file() else 0
                final_total = expected_size_i or response_total
                if final_total and current_size != final_total:
                    raise OSError(f"incomplete download ({current_size} of {final_total} bytes)")
                if current_size <= 0:
                    raise OSError("download produced an empty file")

                verified, reason = _download_matches_expectations(
                    partial,
                    expected_size_i,
                    expected_hash,
                )
                if not verified:
                    # A hash mismatch is not resumable: the accumulated bytes
                    # are wrong, so discard them and retry cleanly.
                    if "SHA-256 mismatch" in reason:
                        safe_unlink(partial)
                    raise OSError(reason)

                if not safe_replace_file(partial, dest):
                    raise OSError(f"Could not replace '{partial}' with '{dest}'")
                safe_rmtree(parallel_parts)
                if _gui:
                    _gui.clear_platformio_progress_block()
                    _gui.set_progress_percent(100)
                ok(f"Saved and verified {dest.name}" if (expected_size_i or expected_hash) else f"Saved to {dest.name}")
                return

            except urllib.error.HTTPError as exc:
                last_error = exc
                # HTTP 416 can mean the partial already reached the expected
                # length. Verify before throwing away potentially complete data.
                if exc.code == 416 and partial.is_file():
                    verified, _ = _download_matches_expectations(partial, expected_size_i, expected_hash)
                    if verified:
                        if not safe_replace_file(partial, dest):
                            raise OSError(f"Could not replace '{partial}' with '{dest}'")
                        safe_rmtree(parallel_parts)
                        if _gui:
                            _gui.clear_platformio_progress_block()
                            _gui.set_progress_percent(100)
                        ok(f"Saved and verified {dest.name}")
                        return
                    safe_unlink(partial)
            except Exception as exc:
                last_error = exc

            if attempt < attempts:
                # 1, 3, 7, 12... seconds: long enough for a throttled/CDN route
                # to recover without making the bootstrap spin aggressively.
                delay = min(12, (2 ** attempt) - 1)
                time.sleep(delay)

        # urllib is the most portable path, but Windows ships curl on supported
        # releases. A second HTTP/TLS implementation handles machines where
        # urllib repeatedly sees RemoteDisconnected on GitHub release assets.
        try:
            status(f"Python download was interrupted repeatedly; trying system curl for {dest.name}...")
            _curl_resume_download(
                url,
                partial,
                timeout=timeout,
                expected_size=expected_size_i,
            )
            verified, reason = _download_matches_expectations(partial, expected_size_i, expected_hash)
            if not verified:
                safe_unlink(partial)
                raise OSError(reason)
            if not safe_replace_file(partial, dest):
                raise OSError(f"Could not replace '{partial}' with '{dest}'")
            safe_rmtree(parallel_parts)
            if _gui:
                _gui.clear_platformio_progress_block()
                _gui.set_progress_percent(100)
            ok(f"Saved and verified {dest.name}" if (expected_size_i or expected_hash) else f"Saved to {dest.name}")
            return
        except Exception as curl_exc:
            last_error = RuntimeError(f"urllib: {last_error}; curl: {curl_exc}")

    finally:
        if _gui:
            _gui.clear_platformio_progress_block()
            _gui.stop_busy(restore_step=True)

    # Keep a valid-looking partial so a later app launch can continue it. Only
    # corrupt hash data and cross-source switches explicitly delete .part.
    raise RuntimeError(f"download failed after resumable retries: {last_error}")


# ── pip install helper (quiet output + drives the progress bar) ─────
_LAST_PIP_ERROR = ""


def _run_pip_install(
    args: list,
    quiet_ok_msg: str | None = None,
    timeout: int = 180,
    use_cache: bool = True,
    only_binary: bool = False,
    progress_start: int | float | None = None,
    progress_end: int | float | None = None,
    line_callback=None,
) -> bool:
    """Run one quiet pip transaction and optionally report live output phases.

    pip's raw text is never appended to the normal GUI log.  ``line_callback``
    receives each pip output line so the dependency table can update its old
    30-cell progress bars without exposing resolver noise to the user.
    """
    global _LAST_PIP_ERROR

    if _gui:
        if progress_start is not None:
            _gui.set_progress_percent(progress_start)
        busy_cap = progress_end if progress_end is not None else 92
        _gui.start_busy(progress_start, busy_cap)

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        *args,
        "--disable-pip-version-check",
        "--prefer-binary",
        "--upgrade-strategy",
        "only-if-needed",
        "--retries",
        "3",
        "--timeout",
        "30",
        "--progress-bar",
        "off",
        "--no-input",
    ]
    if not use_cache:
        cmd.append("--no-cache-dir")
    if only_binary:
        cmd.extend(["--only-binary", ":all:"])

    proc = None
    output_lines = []
    timed_out = False

    try:
        import queue as _queue

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

        q = _queue.Queue()
        reader_done = threading.Event()

        def _reader():
            try:
                if proc.stdout is not None:
                    for raw_line in iter(proc.stdout.readline, ""):
                        q.put(raw_line)
            except Exception:
                pass
            finally:
                try:
                    if proc.stdout is not None:
                        proc.stdout.close()
                except Exception:
                    pass
                reader_done.set()

        threading.Thread(target=_reader, daemon=True, name="BootstrapPipReader").start()

        started = time.time()
        last_output_time = started  # ← stall detection anchor
        # Stall threshold for unified install is longer than download (90s vs
        # 45s) because pip's dependency resolver can legitimately spend a
        # minute between output lines on first-run machines.
        _INSTALL_STALL_TIMEOUT = 90
        while True:
            try:
                raw_line = q.get(timeout=0.15)
                line = raw_line.rstrip("\r\n")
                output_lines.append(line)
                last_output_time = time.time()   # reset stall clock
                if line_callback is not None:
                    try:
                        line_callback(line)
                    except Exception:
                        pass
            except _queue.Empty:
                pass

            if proc.poll() is not None and reader_done.is_set() and q.empty():
                break

            now = time.time()

            # Stall detection: no output for _INSTALL_STALL_TIMEOUT seconds
            if now - last_output_time > _INSTALL_STALL_TIMEOUT:
                timed_out = True
                output_lines.append(
                    f"bootstrap: pip install stalled ({_INSTALL_STALL_TIMEOUT}s with no output)"
                )
                try:
                    proc.kill()
                except Exception:
                    pass
                break

            # Absolute wall-clock timeout
            if now - started > timeout:
                timed_out = True
                try:
                    proc.kill()
                except Exception:
                    pass
                break

        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

        # Drain lines that arrived between poll() and reader shutdown.
        while True:
            try:
                raw_line = q.get_nowait()
            except _queue.Empty:
                break
            line = raw_line.rstrip("\r\n")
            output_lines.append(line)
            if line_callback is not None:
                try:
                    line_callback(line)
                except Exception:
                    pass

        output = "\n".join(output_lines)

        if timed_out:
            _LAST_PIP_ERROR = f"pip timed out after {timeout} seconds"
            return False

        returncode = proc.returncode if proc.returncode is not None else -1
        if returncode == 0:
            _LAST_PIP_ERROR = ""
            if _gui and progress_end is not None:
                _gui.set_progress_percent(progress_end)
            if quiet_ok_msg:
                ok(quiet_ok_msg)
            return True

        lines = [line.strip() for line in output_lines if line.strip()]
        error_lines = [line for line in lines if line.lower().startswith("error:")]
        _LAST_PIP_ERROR = (
            error_lines[-1]
            if error_lines
            else (lines[-1] if lines else f"pip exited with code {returncode}")
        )

        try:
            log_dir = SCRIPT_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "bootstrap_pip.log").open("a", encoding="utf-8") as stream:
                stream.write("\n=== pip failure ===\n")
                stream.write("command: " + subprocess.list2cmdline(cmd) + "\n")
                stream.write(output)
                if output and not output.endswith("\n"):
                    stream.write("\n")
        except Exception:
            pass
        return False

    except Exception as exc:
        _LAST_PIP_ERROR = f"pip could not start: {exc}"
        try:
            if proc is not None and proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        return False
    finally:
        if _gui:
            _gui.stop_busy(restore_step=False)


# Arduino CLI cache shared with the main GUI.
ARDUINO_CLI_CACHE_FILE = (
    SCRIPT_DIR / "src" / "dbs" / "arduino_cli_path.txt"
    if (SCRIPT_DIR / "src" / "dbs" / "arduino_cli_path.txt").exists() or not (SCRIPT_DIR / "arduino_cli_path.txt").exists()
    else SCRIPT_DIR / "arduino_cli_path.txt"
)


def _cache_arduino_cli_path(path: str) -> None:
    """Persist a known-good arduino-cli path so mcu_flash_gui.py finds it instantly."""
    try:
        ARDUINO_CLI_CACHE_FILE.write_text(path, encoding="utf-8")
    except Exception:
        pass


def _search_arduino_cli_install_dirs() -> str | None:
    """
    Look in every directory arduino-cli's Windows MSI is known to install
    into. The MSI's default target has varied across releases (plain
    Program Files vs. a per-user LOCALAPPDATA\\Programs folder), so a
    single hardcoded path isn't reliable — check them all.
    """
    if sys.platform != "win32":
        return None

    candidates = [
        r"C:\Program Files\Arduino CLI\arduino-cli.exe",
        r"C:\Program Files (x86)\Arduino CLI\arduino-cli.exe",
    ]

    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        candidates += [
            str(Path(local_app) / "Programs" / "Arduino CLI" / "arduino-cli.exe"),
            str(Path(local_app) / "Arduino CLI" / "arduino-cli.exe"),
            str(Path(local_app) / "Programs" / "arduino-cli" / "arduino-cli.exe"),
        ]

    for p in candidates:
        if Path(p).exists():
            return p

    # Last resort: ask Windows Installer directly where it put the product.
    # The MSI registers an install location under the uninstall registry
    # key even when the app isn't on PATH or in a "standard" folder.
    try:
        import winreg
        uninstall_roots = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, subkey in uninstall_roots:
            try:
                key = winreg.OpenKey(hive, subkey)
            except OSError:
                continue
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    sub_name = winreg.EnumKey(key, i)
                    sub = winreg.OpenKey(key, sub_name)
                    name = winreg.QueryValueEx(sub, "DisplayName")[0]
                    if "arduino cli" not in name.lower():
                        continue
                    install_loc = winreg.QueryValueEx(sub, "InstallLocation")[0]
                    exe = Path(install_loc) / "arduino-cli.exe"
                    if exe.exists():
                        return str(exe)
                except OSError:
                    continue
    except Exception:
        pass

    return None


def find_arduino_cli() -> str | None:
    """Check if Arduino-CLI is already available."""
    # Fastest path: a location either bootstrap or the GUI already confirmed.
    if ARDUINO_CLI_CACHE_FILE.exists():
        try:
            cached = ARDUINO_CLI_CACHE_FILE.read_text(encoding="utf-8").strip()
            if cached and Path(cached).exists():
                return cached
        except Exception:
            pass
        # This cache stores an absolute path and is commonly stale after a
        # project copy. Remove only this known app-owned marker so discovery
        # can continue without carrying the old account/machine path forward.
        safe_unlink(ARDUINO_CLI_CACHE_FILE)

    cli = shutil.which("arduino-cli")
    if cli:
        return cli

    cli = _search_arduino_cli_install_dirs()
    if cli:
        return cli
    return None


def _arduino_cli_msi_url(version: str | None = None) -> str:
    """
    Return the direct GitHub download URL for the Windows arduino-cli MSI.

    If *version* is None the /latest/ redirect is used, which always
    resolves to the newest release without needing to know the tag up-front.
    Architecture is detected at runtime: arm64 gets the ARM64 build,
    everything else gets the 64-bit x86 build (the vast majority of PCs).
    """
    import platform
    import urllib.request
    machine = platform.machine().lower()
    if machine == "arm64":
        arch_label = "Windows_ARM64"
    else:
        arch_label = "Windows_64bit"

    if not version:
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/arduino/arduino-cli/releases/latest",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                version = data.get("tag_name", "").lstrip("v")
        except Exception:
            pass

    if version:
        v = version.lstrip("v")
        return (
            f"https://github.com/arduino/arduino-cli/releases/download/"
            f"v{v}/arduino-cli_{v}_{arch_label}.msi"
        )
    else:
        return (
            f"https://github.com/arduino/arduino-cli/releases/download/"
            f"v1.5.1/arduino-cli_1.5.1_{arch_label}.msi"
        )


def _refresh_bundled_msi(version: str | None = None) -> bool:
    """
    Download the latest (or a specific) arduino-cli MSI into
    SCRIPT_DIR/installers/arduino-cli.msi, replacing whatever is there.

    Returns True on success, False on any network/IO error.
    This keeps the bundled installer in sync so the next fresh-machine
    install gets the current version without a separate manual download.
    """
    msi_dir  = SCRIPT_DIR / "installers"
    msi_path = msi_dir / "arduino-cli.msi"
    url      = _arduino_cli_msi_url(version)

    try:
        msi_dir.mkdir(parents=True, exist_ok=True)
        status(f"Refreshing bundled MSI from GitHub{' v' + version if version else ' (latest)'}...")
        status(f"  {DIM}{url}{RESET}")
        _download_file(url, msi_path)
        if not _is_valid_msi(msi_path):
            raise RuntimeError("downloaded file is not a valid MSI package")
        ok(f"Bundled MSI updated → {msi_path.name}")
        return True
    except Exception as e:
        warn(f"Could not refresh bundled MSI: {e}")
        return False


def _is_valid_msi(path: Path) -> bool:
    """Reject empty, partial, or non-MSI downloads before invoking msiexec."""
    try:
        # MSI files are OLE compound documents and begin with this signature.
        if not path.is_file() or path.stat().st_size <= 1024 * 1024:
            return False
        with path.open("rb") as installer_file:
            return installer_file.read(8) == bytes.fromhex("D0CF11E0A1B11AE1")
    except OSError:
        return False


def _is_valid_exe(path: Path) -> bool:
    """Reject a truncated or substituted Windows executable before running it."""
    try:
        if not path.is_file() or path.stat().st_size < 64 * 1024:
            return False
        with path.open("rb") as installer_file:
            return installer_file.read(2) == b"MZ"
    except OSError:
        return False


_MSIEXEC_ERROR_CODES = {
    -2: "The Administrator permission (UAC) prompt was declined.",
    -1: "Could not launch or elevate msiexec.",
    5: "Access denied — the install needs to run elevated (as Administrator).",
    1601: "Windows Installer service could not be accessed.",
    1602: "User cancelled the installation.",
    1603: "Fatal error during installation (often: already installed, or a locked file).",
    1618: "Another installation is already in progress. Wait for it to finish and retry.",
    1619: "The installation package could not be opened — the .msi file may be missing or corrupt.",
    1620: "The installation package could not be opened — invalid or damaged .msi.",
    1622: "Windows Installer could not open its diagnostic log file.",
    1633: "This installation package is not supported on this platform (check 32-bit vs 64-bit / ARM64).",
    3010: "Install succeeded but a reboot is required to finish.",
}


_LAST_ARDUINO_CLI_ERROR = ""


def get_last_arduino_cli_error() -> str:
    """Return the reason the most recent ensure_arduino_cli() call failed, if any."""
    return _LAST_ARDUINO_CLI_ERROR


def _new_msi_log_path() -> Path | None:
    """Return a unique, writable MSI log path without sharing another run's log.

    A fixed ``%TEMP%\\arduino_cli_msi_install.log`` can be locked, read-only,
    or owned by a different elevated launch. Windows Installer then aborts
    before it even evaluates the MSI with exit code 1622.
    """
    import tempfile
    import uuid

    try:
        log_dir = Path(tempfile.gettempdir()) / "mcu_flasher_msi_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        probe = log_dir / f".write-probe-{os.getpid()}-{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="ascii")
        probe.unlink(missing_ok=True)
        return log_dir / f"arduino-cli-{os.getpid()}-{uuid.uuid4().hex}.log"
    except Exception:
        return None


def _run_msiexec(args: list[str]) -> tuple[int, str]:
    """
    Run msiexec with a verbose log file and return (exit_code, log_tail).

    On Windows this launches msiexec via ShellExecuteEx with the "runas"
    verb, which triggers the real UAC consent prompt. This matters because
    bootstrap/the GUI run hidden via pythonw.exe — a plain subprocess.run()
    call has no interactive desktop for Windows to silently elevate against,
    so an install that needs admin rights fails with Access Denied (return 5)
    and a rollback instead of ever prompting the user.
    """
    log_path = _new_msi_log_path()
    full_args = list(args)
    if log_path:
        full_args.extend(["/L*V", str(log_path)])

    def _launch(msi_args: list[str]) -> int:
        if sys.platform == "win32":
            return _shell_execute_elevated_wait("msiexec", msi_args)
        result = subprocess.run(
            ["msiexec", *msi_args],
            capture_output=True,
            text=True,
        )
        return result.returncode

    try:
        code = _launch(full_args)
    except PermissionError as e:
        if str(e).startswith("UAC_DECLINED"):
            return -2, "The Administrator permission prompt (UAC) was declined."
        return -1, str(e)
    except Exception as e:
        return -1, f"Could not launch msiexec: {e}"

    log_warning = ""
    if code == 1622 and log_path:
        # Logging is diagnostic-only. Do not let a blocked/invalid Temp log
        # make the Arduino CLI installer unusable; retry the same MSI once
        # without /L*V using the already-approved process token.
        log_warning = "Windows Installer could not open its log; retried once without diagnostic logging."
        try:
            code = _launch(list(args))
        except PermissionError as e:
            if str(e).startswith("UAC_DECLINED"):
                return -2, "The Administrator permission prompt (UAC) was declined."
            return -1, str(e)
        except Exception as e:
            return -1, f"Could not retry msiexec without logging: {e}"

    log_tail = ""
    try:
        if log_path and log_path.exists():
            text = log_path.read_text(encoding="utf-16", errors="replace")
            # Pull out the lines that actually explain the failure rather than
            # dumping the whole (often huge) verbose log.
            interesting = [
                ln.strip() for ln in text.splitlines()
                if ("error" in ln.lower() or "return value 3" in ln.lower())
                and ln.strip()
            ]
            log_tail = "\n".join(interesting[-8:])
    except Exception:
        pass

    if log_warning:
        log_tail = "\n".join(part for part in (log_warning, log_tail) if part)

    return code, log_tail


def _is_process_elevated() -> bool:
    """Return True when the current Windows process already has an elevated token."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _shell_execute_elevated_wait(exe: str, args: list[str], timeout: float = 300.0) -> int:
    """
    Launch exe with args via ShellExecuteEx(verb='runas') so Windows shows
    the UAC consent prompt, then block until it exits and return its exit
    code.

    Raises PermissionError (message prefixed "UAC_DECLINED:") if the user
    clicked "No" on the elevation prompt, or OSError for any other failure
    to even launch the elevated process.
    """
    import ctypes
    from ctypes import wintypes

    # A first-run privileged helper may already be elevated.  In that case
    # launch the child directly so Windows does not show a second UAC prompt.
    if sys.platform == "win32" and _is_process_elevated():
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                [exe, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            try:
                return proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
                return 1
        except Exception as exc:
            raise OSError(f"Could not launch elevated child {exe}: {exc}") from exc

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hKeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    params = subprocess.list2cmdline(args)
    sei = SHELLEXECUTEINFO()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFO)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.hwnd = None
    sei.lpVerb = "runas"
    sei.lpFile = exe
    sei.lpParameters = params
    sei.lpDirectory = None
    sei.nShow = SW_HIDE
    sei.hInstApp = None

    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)):
        err = ctypes.windll.kernel32.GetLastError()
        ERROR_CANCELLED = 1223
        if err == ERROR_CANCELLED:
            raise PermissionError(
                "UAC_DECLINED: the elevation (Administrator) prompt was declined."
            )
        raise OSError(f"ShellExecuteEx failed to launch {exe} (Win32 error {err}).")

    WAIT_TIMEOUT = 0x00000102
    result = ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, int(timeout * 1000))
    if result == WAIT_TIMEOUT:
        ctypes.windll.kernel32.TerminateProcess(sei.hProcess, 1)
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
        return 1

    exit_code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(exit_code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return exit_code.value


def _run_arduino_cli_msi(msi_path: Path) -> bool:
    """
    Run msiexec silently on the given MSI. Returns True on success.

    If the install fails with 1603 (the generic "fatal error" code — most
    often meaning Windows Installer already has this product registered
    even though the actual files are missing or damaged), automatically
    retry as a repair install before giving up. That single retry resolves
    the large majority of real-world 1603s without any user action.
    """
    global _LAST_ARDUINO_CLI_ERROR
    if _is_process_elevated():
        status("Running Arduino-CLI installer with existing Administrator permission...")
    else:
        status("Running Arduino-CLI installer (requesting Administrator permission if needed)...")

    code, detail = _run_msiexec(["/i", str(msi_path), "/quiet", "/norestart"])

    if code == 0:
        return True
    if code == 3010:
        warn("arduino-cli installed, but Windows wants a reboot to finish cleanly.")
        return True
    if code == -2:
        _LAST_ARDUINO_CLI_ERROR = (
            "The install needs Administrator permission. A Windows prompt should have "
            "appeared asking to allow this — please click 'Yes' when it shows up, then "
            "try again."
        )
        fail(_LAST_ARDUINO_CLI_ERROR)
        return False

    if code == 1603:
        warn("Install failed (1603) — product may already be registered with missing "
             "files. Retrying as a repair install...")
        # /fa = reinstall all files regardless of checksum/version, fixing the
        # "registered but files gone" case without needing a manual uninstall first.
        repair_code, repair_detail = _run_msiexec(["/fa", str(msi_path), "/quiet", "/norestart"])
        if repair_code in (0, 3010):
            ok("Repair install succeeded.")
            return True
        detail = repair_detail or detail
        code = repair_code if repair_code not in (0,) else code

    reason = _MSIEXEC_ERROR_CODES.get(code, "Unknown msiexec error.")
    _LAST_ARDUINO_CLI_ERROR = f"msiexec exit code {code}: {reason}"
    if detail:
        _LAST_ARDUINO_CLI_ERROR += f"\n{detail[:800]}"
    if code == 1603:
        _LAST_ARDUINO_CLI_ERROR += (
            "\n\nA repair install was attempted and also failed. Windows Installer "
            "likely still has an old arduino-cli registration pointing at files that "
            "no longer exist. Fix: open 'Add or Remove Programs', search for "
            "'Arduino CLI', remove it if listed, then retry. If it's not listed there, "
            "check Task Manager / antivirus isn't holding arduino-cli.exe locked, "
            "close it, and retry."
        )
    fail(f"msiexec failed (exit code {code}): {reason}")
    if detail:
        fail(f"  msiexec log: {detail[:800]}")
    return False


def _prewarm_arduino_cli_cores(cli_path: str):
    """Pre-install Arduino AVR core (and update core index) via arduino-cli
    inside bootstrap setup so compiling with Arduino-CLI never stalls in the GUI."""
    try:
        status("Checking Arduino-CLI core packages...")
        _cf = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        # Fast path: check if arduino:avr is already installed locally to avoid network delays on every start
        res = subprocess.run([cli_path, "core", "list"], capture_output=True, text=True, timeout=10, creationflags=_cf)
        if res.returncode == 0 and "arduino:avr" in res.stdout:
            ok("Arduino AVR core is already installed.")
            return

        subprocess.run([cli_path, "core", "update-index"], capture_output=True, timeout=30, creationflags=_cf)
        res = subprocess.run([cli_path, "core", "install", "arduino:avr"], capture_output=True, text=True, timeout=120, creationflags=_cf)
        if res.returncode == 0:
            ok("Arduino AVR core pre-installed via Arduino-CLI.")
    except Exception:
        pass


def ensure_arduino_cli() -> bool:
    """
    Make sure Arduino-CLI is installed.

    Priority:
      1. Already on PATH / known install dirs → nothing to do.
      2. Bundled MSI in installers/arduino-cli.msi → run it.
      3. No bundled MSI → download latest from GitHub, run it, then
         keep the downloaded copy as the new bundled MSI.
    """
    global _LAST_ARDUINO_CLI_ERROR
    _LAST_ARDUINO_CLI_ERROR = ""

    cli = find_arduino_cli()
    if cli:
        ok("Arduino-CLI is already installed")
        _cache_arduino_cli_path(cli)
        _prewarm_arduino_cli_cores(cli)
        return True

    section("Installing Arduino-CLI")
    msi_path = SCRIPT_DIR / "installers" / "arduino-cli.msi"

    if not _is_valid_msi(msi_path):
        warn("Bundled MSI is missing or invalid — preparing latest from GitHub...")
        if not _refresh_bundled_msi():          # download into installers/
            _LAST_ARDUINO_CLI_ERROR = (
                "Could not prepare arduino-cli installer. "
                "Check your internet connection or place arduino-cli.msi "
                f"in {SCRIPT_DIR / 'installers'}."
            )
            fail(_LAST_ARDUINO_CLI_ERROR)
            return False

    if not _run_arduino_cli_msi(msi_path):
        # _LAST_ARDUINO_CLI_ERROR already set by _run_arduino_cli_msi with the real reason
        return False

    cli = find_arduino_cli()
    if cli:
        ok(f"Arduino-CLI installed successfully: {cli}")
        _cache_arduino_cli_path(cli)
        _prewarm_arduino_cli_cores(cli)
        return True
    else:
        _LAST_ARDUINO_CLI_ERROR = (
            "msiexec reported success, but arduino-cli.exe still couldn't be located "
            "afterward. It may have installed to a non-standard folder — try 'Manually "
            "locate' and browse to it, or check %LOCALAPPDATA%\\Programs and "
            "C:\\Program Files for an 'Arduino CLI' folder."
        )
        fail("Arduino-CLI installation finished but executable could not be found.")
        return False


# ── 5. CP210x Driver ─────────────────────────────────────────
_CP210X_SENTINEL = SCRIPT_DIR / "logs" / ".cp210x_installed"


def _get_machine_id() -> str:
    """Return a stable identifier for the current machine so sentinel files
    written on one device are not mistakenly accepted on another when the
    project folder is copied/deployed across machines."""
    import platform
    return platform.node()


def _cp210x_sentinel_valid() -> bool:
    """Return True only if the sentinel file exists AND was written on this
    same machine (checked via hostname stored inside the file)."""
    try:
        if not _CP210X_SENTINEL.exists():
            return False
        content = _CP210X_SENTINEL.read_text(encoding="utf-8", errors="replace")
        # The sentinel's first line is "machine:<hostname>"
        for line in content.splitlines():
            if line.startswith("machine:"):
                return line.split(":", 1)[1].strip() == _get_machine_id()
        # Legacy sentinel without a machine line — treat as invalid so the
        # check runs properly on this device.
        return False
    except Exception:
        return False


def _cp210x_driver_in_store() -> bool:
    """Check if a CP210x driver package is staged in the Windows Driver Store.
    DPInst-based installers stage the .inf into the store; the actual .sys
    is only extracted into System32\\drivers when a matching device is
    plugged in and the OS loads the driver. So this check covers the
    'installed but no device connected yet' case."""
    try:
        import winreg
        # Walk HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Setup\PnpLockdownFiles
        # and the OEM*.inf files in %SystemRoot%\INF for 'silabser'
        windir = os.environ.get("SystemRoot", "C:\\Windows")
        inf_dir = Path(windir) / "INF"
        if inf_dir.is_dir():
            for inf in inf_dir.glob("oem*.inf"):
                try:
                    text = inf.read_text(encoding="utf-8", errors="replace")
                    if "silabser" in text.lower():
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def check_cp210x_driver() -> bool:
    """Check if the Silicon Labs CP210x VCP driver is installed, staged, or
    was previously installed successfully by this bootstrap."""
    if sys.platform != "win32":
        return True

    # Fast path: a previous bootstrap run on *this machine* confirmed the install
    if _cp210x_sentinel_valid():
        return True

    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\silabser")

        # Check if the service is marked for deletion
        try:
            delete_flag, _ = winreg.QueryValueEx(key, "DeleteFlag")
            if delete_flag == 1:
                winreg.CloseKey(key)
                return False
        except FileNotFoundError:
            pass

        try:
            driver_delete, _ = winreg.QueryValueEx(key, "DriverDelete")
            if driver_delete == 1:
                winreg.CloseKey(key)
                return False
        except FileNotFoundError:
            pass

        # Check if the driver binary file actually exists on disk
        sys_file_exists = False
        try:
            image_path, _ = winreg.QueryValueEx(key, "ImagePath")
            winreg.CloseKey(key)

            if image_path:
                resolved_path = image_path
                if resolved_path.lower().startswith(r"\systemroot"):
                    windir = os.environ.get("SystemRoot", "C:\\Windows")
                    resolved_path = resolved_path.replace(r"\SystemRoot", windir).replace(r"\systemroot", windir)
                elif resolved_path.lower().startswith("system32"):
                    windir = os.environ.get("SystemRoot", "C:\\Windows")
                    resolved_path = os.path.join(windir, resolved_path)

                resolved_path = os.path.expandvars(resolved_path)
                sys_file_exists = os.path.exists(resolved_path)
        except Exception:
            windir = os.environ.get("SystemRoot", "C:\\Windows")
            default_sys_file = os.path.join(windir, "System32", "drivers", "silabser.sys")
            sys_file_exists = os.path.exists(default_sys_file)

        if sys_file_exists:
            return True

        # Registry key exists but .sys is missing — this is normal when the
        # driver package was staged by DPInst but no CP210x device has been
        # connected yet (Windows only copies the .sys on first device plug).
        # Check the Driver Store for a staged .inf package.
        if _cp210x_driver_in_store():
            return True

        return False
    except FileNotFoundError:
        return False


def ensure_cp210x() -> bool:
    """Make sure Silicon Labs CP210x VCP driver is installed."""
    if check_cp210x_driver():
        ok("CP210x driver is already installed")
        return True

    section("Installing CP210x Driver")
    driver_dir = _find_installer_dir("CP210x")

    import platform
    is_64bit = platform.machine().endswith("64") or sys.maxsize > 2**32
    installer = driver_dir / ("CP210xVCPInstaller_x64.exe" if is_64bit else "CP210xVCPInstaller_x86.exe")

    if not _is_valid_exe(installer):
        fail(f"CP210x driver installer is missing or invalid: {installer}")
        return False

    status("Launching CP210x driver installer...")
    try:
        exit_code = None
        if sys.platform == "win32":
            # Run DPInst elevated and silently. Suppress dialogs using /q /se
            exit_code = _shell_execute_elevated_wait(str(installer), ["/q", "/se"])
            # DPInst uses a bitfield return code:
            # - Bit 31 (0x80000000) is set on failure
            # - Other bits indicate successfully installed packages (e.g. exit code 1 or 2),
            #   copied packages (e.g. exit code 256), or reboot required (3010).
            is_success = False
            if exit_code in (0, 3010):
                is_success = True
            elif exit_code is not None and (exit_code & 0x80000000) == 0:
                is_success = True

            if not is_success:
                fail(f"CP210x installer exited with code {exit_code}.")
                return False
        else:
            proc = subprocess.run([str(installer)], check=True)
            exit_code = proc.returncode

        # Write sentinel so future bootstrap runs skip the installer.
        # DPInst stages the driver package into the Driver Store; the actual
        # .sys is only copied into System32\drivers when a CP210x device is
        # first plugged in, so check_cp210x_driver() may still return False
        # even after a fully successful install.
        try:
            _CP210X_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
            _CP210X_SENTINEL.write_text(
                f"machine:{_get_machine_id()}\n"
                f"CP210x driver installer completed successfully (exit code {exit_code}).\n",
                encoding="utf-8",
            )
        except Exception:
            pass

        if check_cp210x_driver():
            ok("CP210x driver installed successfully")
            return True
        else:
            # Sentinel was written — next run will skip. Inform the user.
            ok("CP210x driver package staged successfully")
            status("The driver will activate automatically when a CP210x device is connected.")
            return True
    except Exception as e:
        fail(f"Failed to run CP210x driver installer: {e}")
        return False


def _ensure_bundled_windows_installers(
    *, machine_only_arduino: bool = False
) -> dict[str, bool]:
    """Install every Windows prerequisite intentionally bundled with the app.

    This explicit allowlist covers the Windows components the application
    actually depends on: WebView2, Arduino CLI, and the Silicon Labs CP210x
    USB serial driver.
    """
    if sys.platform != "win32":
        return {}

    results: dict[str, bool] = {}
    installers = (
        ("webview2", "Microsoft Edge WebView2 Runtime", ensure_webview2_runtime),
        (
            "arduino_cli",
            "Arduino-CLI",
            _install_arduino_cli_machine_only if machine_only_arduino else ensure_arduino_cli,
        ),
        ("cp210x", "CP210x USB serial driver", ensure_cp210x),
    )
    for key, label, install in installers:
        try:
            results[key] = bool(install())
        except Exception as exc:
            results[key] = False
            warn(f"Bundled {label} installer could not run: {exc}")
    return results


def _cp210x_driver_status_message(
    driver_available: bool,
    bootstrap_is_elevated: bool,
    direct_result: bool,
    privileged_setup_ok: bool,
) -> tuple[bool, str]:
    """Describe the CP210x result so the bootstrap step always has output."""
    if driver_available:
        return True, "CP210x USB serial driver is installed or staged in the Windows Driver Store."
    if bootstrap_is_elevated:
        detail = (
            "The bundled CP210x installer was run but Windows did not stage the driver."
            if not direct_result
            else "The bundled CP210x package was staged; Windows will activate it when a matching device is connected."
        )
    elif not privileged_setup_ok:
        detail = "Administrator setup did not complete, so the bundled CP210x installer could not be run."
    else:
        detail = "The bundled CP210x installer completed but Windows has not reported the driver yet."
    return False, f"CP210x driver is not yet available. {detail}"


def check_opencode_cli() -> Optional[str]:
    """Check if opencode CLI executable is installed on system or npm path."""
    exe = shutil.which("opencode") or shutil.which("opencode.cmd") or shutil.which("opencode.exe")
    if exe:
        return exe

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            for candidate in [
                Path(appdata) / "npm" / "opencode.cmd",
                Path(appdata) / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe",
                Path(appdata) / "npm" / "opencode.exe",
                Path(appdata) / "npm" / "opencode",
            ]:
                if candidate.exists() and candidate.stat().st_size > 0:
                    return str(candidate)

        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            for candidate in [
                Path(local_app) / "Programs" / "opencode" / "opencode.exe",
                Path(local_app) / "opencode" / "opencode.exe",
                Path(local_app) / "npm" / "opencode.cmd",
                Path(local_app) / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe",
            ]:
                if candidate.exists() and candidate.stat().st_size > 0:
                    return str(candidate)

        user_prof = os.environ.get("USERPROFILE", "")
        if user_prof:
            for candidate in [
                Path(user_prof) / "AppData" / "Roaming" / "npm" / "opencode.cmd",
                Path(user_prof) / "AppData" / "Roaming" / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe",
                Path(user_prof) / "AppData" / "Roaming" / "npm" / "opencode.exe",
            ]:
                if candidate.exists() and candidate.stat().st_size > 0:
                    return str(candidate)

        for candidate in [
            Path(r"C:\Program Files\nodejs\opencode.cmd"),
            Path(r"C:\Program Files\nodejs\node_modules\opencode-ai\bin\opencode.exe"),
        ]:
            if candidate.exists() and candidate.stat().st_size > 0:
                return str(candidate)

    return None


def _hidden_subprocess_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _summarize_process_output(result: subprocess.CompletedProcess, limit: int = 800) -> str:
    text = "\n".join(
        part.strip()
        for part in (getattr(result, "stdout", "") or "", getattr(result, "stderr", "") or "")
        if part and part.strip()
    )
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > limit:
        text = text[-limit:].lstrip()
    return text


def _prepend_existing_paths(paths: list[Path | str]) -> None:
    current_parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    known = {os.path.normcase(os.path.normpath(p)) for p in current_parts}
    for path in reversed(paths):
        if not path:
            continue
        p = Path(path)
        if not p.exists():
            continue
        path_str = str(p)
        key = os.path.normcase(os.path.normpath(path_str))
        if key not in known:
            current_parts.insert(0, path_str)
            known.add(key)
    os.environ["PATH"] = os.pathsep.join(current_parts)


def _windows_node_dirs() -> list[Path]:
    if sys.platform != "win32":
        return []
    dirs = [
        Path(r"C:\Program Files\nodejs"),
        Path(r"C:\Program Files (x86)\nodejs"),
    ]
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        dirs.append(Path(appdata) / "npm")
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        dirs.append(Path(local_app) / "npm")
        dirs.append(Path(local_app) / "Programs" / "nodejs")
    user_prof = os.environ.get("USERPROFILE", "")
    if user_prof:
        dirs.append(Path(user_prof) / "AppData" / "Roaming" / "npm")
    return dirs


def _refresh_node_npm_path() -> None:
    """Make a just-installed Node/npm visible to the current bootstrap process."""
    _prepend_existing_paths(_windows_node_dirs())


def _tool_runs(candidate: str | Path, args: list[str], timeout: int = 15) -> bool:
    try:
        result = subprocess.run(
            [str(candidate), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_hidden_subprocess_flags(),
            timeout=timeout,
        )
        return result.returncode == 0
    except Exception:
        return False


def _find_executable_candidate(names: list[str], directories: list[Path]) -> Optional[str]:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for directory in directories:
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
    return None


def _find_usable_node_cmd() -> Optional[str]:
    _refresh_node_npm_path()
    candidate = _find_executable_candidate(["node.exe", "node"], _windows_node_dirs())
    if candidate and _tool_runs(candidate, ["--version"]):
        return candidate
    return None


def _find_usable_npm_cmd() -> Optional[str]:
    _refresh_node_npm_path()
    if not _find_usable_node_cmd():
        return None

    npm_candidates: list[str] = []
    if sys.platform == "win32":
        for directory in _windows_node_dirs():
            npm_candidates.append(str(directory / "npm.cmd"))
    for name in ("npm.cmd", "npm"):
        found = shutil.which(name)
        if found:
            npm_candidates.append(found)

    seen: set[str] = set()
    for candidate in npm_candidates:
        key = os.path.normcase(os.path.normpath(candidate))
        if key in seen:
            continue
        seen.add(key)
        if Path(candidate).exists() and _tool_runs(candidate, ["--version"]):
            return candidate
    return None


def _find_winget_cmd() -> Optional[str]:
    winget_bin = shutil.which("winget") or shutil.which("winget.exe")
    if winget_bin:
        return winget_bin
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            w_candidate = Path(local_app) / "Microsoft" / "WindowsApps" / "winget.exe"
            if w_candidate.exists():
                return str(w_candidate)
    return None


def _install_nodejs_lts_with_winget() -> bool:
    winget_bin = _find_winget_cmd()
    if not winget_bin:
        warn("winget is unavailable; falling back to direct installer.")
        return False

    try:
        cmd = [
            winget_bin, "install", "-e", "--id", "OpenJS.NodeJS.LTS",
            "--accept-source-agreements", "--accept-package-agreements",
            "--silent", "--disable-interactivity", "--no-upgrade",
        ]
        status("Running: winget install -e --id OpenJS.NodeJS.LTS...")
        # Use Popen with explicit timeout kill guard instead of subprocess.run
        # to prevent winget from hanging indefinitely on MSIX registration.
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_hidden_subprocess_flags(),
        )
        try:
            stdout, stderr = proc.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            warn("winget Node.js install timed out after 90s; terminating...")
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            _refresh_node_npm_path()
            return _find_usable_npm_cmd() is not None

        _refresh_node_npm_path()
        # Check if npm is usable *before* inspecting the return code.
        # winget returns 0x8A150061 (PACKAGE_ALREADY_INSTALLED) when
        # Node.js is already present — that's a non-zero code but means
        # Node *is* installed.  Checking npm first avoids a false
        # negative that would waste time falling through to the MSI.
        if _find_usable_npm_cmd():
            if proc.returncode == 0:
                ok("Node.js LTS installed successfully via winget.")
            else:
                ok("Node.js LTS is already present (winget confirmed).")
            return True
        output = "\n".join(part.strip() for part in (stdout or "", stderr or "") if part and part.strip())
        if output:
            warn(f"winget Node.js install exited with code {proc.returncode}: {output[:400]}")
        else:
            warn(f"winget Node.js install exited with code {proc.returncode}.")
    except Exception as e:
        warn(f"winget execution failed or timed out: {e}")
    return _find_usable_npm_cmd() is not None


def _resolve_node_lts_msi_url() -> str:
    """Pick the correct Node.js LTS MSI URL for this machine's architecture.

    Queries the official Node.js release index to find the current LTS
    version.  Falls back to a hardcoded version if the query fails.
    Selects x64 or arm64 based on ``platform.machine()``.
    """
    import platform as _platform
    import urllib.request

    # ── Architecture mapping (x64-only project, arm64 for native perf) ──
    machine = _platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        arch = "x64"

    # ── Dynamic version discovery ───────────────────────────────────
    fallback_version = "v22.16.0"
    version = fallback_version
    try:
        req = urllib.request.Request(
            "https://nodejs.org/dist/index.json",
            headers={"User-Agent": "MCU-Flash-GUI-Bootstrap/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            entries = json.loads(resp.read().decode("utf-8"))
        for entry in entries:
            if entry.get("lts"):
                version = entry["version"]   # e.g. "v22.16.0"
                break
    except Exception:
        pass  # network down / timeout — use fallback

    return f"https://nodejs.org/dist/{version}/node-{version}-{arch}.msi"


def _install_nodejs_lts_direct() -> bool:
    """Fallback installer: download official Node.js LTS MSI and install silently."""
    if sys.platform != "win32":
        return False

    import tempfile
    import urllib.request

    msi_url = _resolve_node_lts_msi_url()
    status(f"Downloading Node.js LTS installer ({msi_url.rsplit('/', 1)[-1]})...")
    temp_dir = Path(tempfile.gettempdir()) / "mcu_flash_node_install"
    temp_dir.mkdir(parents=True, exist_ok=True)
    msi_path = temp_dir / "node_lts.msi"

    try:
        req = urllib.request.Request(
            msi_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        response = urllib.request.urlopen(req, timeout=60)
        try:
            total = int(response.headers.get("Content-Length", 0) or 0)
            if total:
                status(f"Downloading Node.js LTS ({total / 1048576:.1f} MB)...")
            downloaded = 0
            chunk_size = 256 * 1024  # 256 KB
            with open(msi_path, "wb") as out_file:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    out_file.write(chunk)
                    downloaded += len(chunk)
                    if total and _gui:
                        pct = min(int(downloaded * 100 / total), 100)
                        _gui.root.after(0, lambda p=pct: _gui.update_step(
                            f"Downloading Node.js LTS... {p}%"
                        ) if hasattr(_gui, 'update_step') else None)
        finally:
            response.close()

        if not msi_path.exists() or msi_path.stat().st_size < 1000000:
            warn("Downloaded Node.js installer appears incomplete.")
            return False

        # Use ShellExecuteEx/runas for msiexec so Windows shows the UAC
        # consent dialog.  A plain subprocess.run() with CREATE_NO_WINDOW
        # cannot trigger UAC — the install silently fails with Access
        # Denied (exit 5) or hangs indefinitely on standard-user PCs.
        status("Installing Node.js LTS via msiexec (may request Administrator)...")
        msi_args = ["/i", str(msi_path), "/qn", "/norestart"]
        try:
            exit_code = _shell_execute_elevated_wait("msiexec", msi_args, timeout=180)
        except PermissionError:
            warn("Administrator permission was declined for Node.js MSI install.")
            return _find_usable_npm_cmd() is not None
        except Exception as msi_exc:
            warn(f"Could not launch Node.js MSI installer: {msi_exc}")
            return _find_usable_npm_cmd() is not None

        _refresh_node_npm_path()
        if exit_code == 0 and _find_usable_npm_cmd():
            ok("Node.js LTS installed successfully via MSI.")
            return True
        else:
            warn(f"msiexec exited with code {exit_code}.")
    except Exception as exc:
        warn(f"Direct Node.js MSI installation failed: {exc}")
    finally:
        try:
            if msi_path.exists():
                msi_path.unlink()
        except Exception:
            pass

    return _find_usable_npm_cmd() is not None


def _install_nodejs_lts() -> bool:
    """Attempt installing Node.js LTS via winget first, falling back to direct MSI download."""
    if _install_nodejs_lts_with_winget():
        return True
    status("Trying direct Node.js installer fallback...")
    return _install_nodejs_lts_direct()


def ensure_opencode_cli() -> bool:
    """Ensure OpenCode CLI is installed and usable."""

    def _cli_works(path: str | None) -> bool:
        if not path:
            return False
        p = Path(path)
        if not p.exists() and not shutil.which(path):
            return False
        try:
            is_script = str(path).lower().endswith((".cmd", ".bat"))
            result = subprocess.run(
                [path, "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=is_script if sys.platform == "win32" else False,
                creationflags=_hidden_subprocess_flags(),
                timeout=15,
            )
            return result.returncode == 0
        except Exception:
            return False

    existing_cli = check_opencode_cli()
    if _cli_works(existing_cli):
        ok(f"OpenCode AI Assistant is already installed ({existing_cli})")
        return True

    section("Installing OpenCode AI Assistant")
    npm_cmd = _find_usable_npm_cmd()

    if not npm_cmd:
        status("Node.js / npm not usable. Installing Node.js LTS...")
        if _gui:
            _gui.start_busy()
        try:
            _install_nodejs_lts()
        finally:
            if _gui:
                _gui.stop_busy(restore_step=True)
        npm_cmd = _find_usable_npm_cmd()

    if not npm_cmd:
        fail("Node.js / npm could not be installed or found on PATH.")
        return False

    def _save_npm_failure(label: str, command: list[str], output: str) -> None:
        try:
            log_dir = SCRIPT_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "bootstrap_opencode.log").open("a", encoding="utf-8") as stream:
                stream.write(f"\n=== {label} ===\n")
                stream.write("command: " + subprocess.list2cmdline(command) + "\n")
                stream.write(output or "")
                if output and not output.endswith("\n"):
                    stream.write("\n")
        except Exception:
            pass

    status("Refreshing OpenCode AI Assistant installation via npm...")
    if _gui:
        _gui.start_busy()

    def _npm_run_with_kill_guard(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
        """Run an npm command with explicit timeout kill guard to prevent hangs."""
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_hidden_subprocess_flags(),
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            warn(f"npm command timed out after {timeout}s; terminating...")
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="(timed out)")
        return subprocess.CompletedProcess(cmd, returncode=proc.returncode, stdout=stdout or "")

    try:
        uninstall_cmd = [
            npm_cmd,
            "uninstall",
            "-g",
            "--no-audit",
            "--no-fund",
            "--no-update-notifier",
            "opencode-ai",
        ]
        try:
            _npm_run_with_kill_guard(uninstall_cmd, timeout=15)
        except Exception:
            pass

        install_cmd = [
            npm_cmd,
            "install",
            "-g",
            "--no-audit",
            "--no-fund",
            "--no-update-notifier",
            "--loglevel=error",
            "--fetch-retries=2",
            "--fetch-retry-mintimeout=2000",
            "--fetch-retry-maxtimeout=10000",
            "--allow-scripts=opencode-ai",
            "opencode-ai",
        ]
        status("Installing OpenCode AI Assistant (this may take a moment)...")
        install_res = _npm_run_with_kill_guard(install_cmd, timeout=90)

        _refresh_node_npm_path()
        cli_path = check_opencode_cli()

        if install_res.returncode == 0 and _cli_works(cli_path):
            ok(f"OpenCode AI Assistant installed successfully ({cli_path})")
            return True

        # When npm install succeeds (returncode==0) but the --version
        # probe timed out, treat as soft success — the binary exists but
        # its first-run initialization is slow. Don't waste time retrying.
        if install_res.returncode == 0 and cli_path:
            ok(f"OpenCode AI Assistant installed ({cli_path}); first-run probe was slow.")
            _save_npm_failure("npm install (soft success)", install_cmd, install_res.stdout)
            return True

        # Only retry with a user-level prefix when the global install
        # returned a non-zero exit code (usually EACCES / EPERM on a
        # non-admin account).  When returncode==0 but _cli_works failed,
        # the binary exists but its --version probe timed out (e.g. a
        # Go binary phoning home on first run).  Retrying with --prefix
        # won't help that case and would waste another 90 seconds.
        if install_res.returncode != 0:
            appdata = os.environ.get("APPDATA", "")
            if appdata and sys.platform == "win32":
                user_npm_dir = Path(appdata) / "npm"
                user_npm_dir.mkdir(parents=True, exist_ok=True)
                status("Retrying OpenCode installation with user prefix...")
                user_install_cmd = [
                    npm_cmd,
                    "install",
                    "-g",
                    f"--prefix={user_npm_dir}",
                    "--no-audit",
                    "--no-fund",
                    "--no-update-notifier",
                    "--loglevel=error",
                    "--fetch-retries=2",
                    "--allow-scripts=opencode-ai",
                    "opencode-ai",
                ]
                user_install_res = _npm_run_with_kill_guard(user_install_cmd, timeout=90)
                _refresh_node_npm_path()
                cli_path = check_opencode_cli()
                if user_install_res.returncode == 0 and _cli_works(cli_path):
                    ok(f"OpenCode AI Assistant installed successfully ({cli_path})")
                    return True
                # Soft success for user-prefix install too
                if user_install_res.returncode == 0 and cli_path:
                    ok(f"OpenCode AI Assistant installed ({cli_path}); first-run probe was slow.")
                    return True
                # Use the retry result and command for failure logging
                install_res = user_install_res
                install_cmd = user_install_cmd

        output = install_res.stdout or ""
        _save_npm_failure("npm install", install_cmd, output)
        detail = _summarize_process_output(install_res)
        if detail:
            fail(f"OpenCode installation failed: {detail}")
        else:
            fail(f"OpenCode installation failed (npm exit {install_res.returncode}).")
        return False

    except Exception as exc:
        fail(f"OpenCode installation failed: {exc}")
        return False
    finally:
        if _gui:
            _gui.stop_busy(restore_step=True)

def _heal_private_python_runtime() -> bool:
    """Auto-heal the private Python runtime at src/_python/.

    If the runtime is missing or broken and a bundled Python installer is
    available under ``installers/.handsoff/``, reinstall it silently to the
    same location, then re-apply the Windows Hidden attribute so the folder
    stays invisible to Windows Search and Explorer.

    Returns True if the runtime is healthy (either already fine or
    successfully healed), False if healing was needed but failed.
    """
    if sys.platform != "win32":
        return True

    private_python_dir = SCRIPT_DIR / "src" / "_python"
    private_python_exe = private_python_dir / "python.exe"

    # ── Quick health check ─────────────────────────────────────────
    def _is_runtime_healthy() -> bool:
        if not private_python_exe.exists():
            return False
        try:
            res = subprocess.run(
                [str(private_python_exe), "-c", "import sys, encodings; print(sys.version)"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=10,
            )
            return res.returncode == 0
        except Exception:
            return False

    if _is_runtime_healthy():
        return True

    # ── Locate the bundled Python installer ─────────────────────────
    handsoff_dir = None
    for candidate_dir in [
        SCRIPT_DIR / "installers" / ".handsoff",
        SCRIPT_DIR.parent / "installers" / ".handsoff",
        SCRIPT_DIR.parent.parent / "installers" / ".handsoff",
    ]:
        if candidate_dir.is_dir():
            handsoff_dir = candidate_dir
            break

    if not handsoff_dir:
        warn("Private Python runtime is broken but no bundled installer directory found.")
        return False

    # Auto-detect any python-*-amd64.exe in the .handsoff directory
    installer_candidates = sorted(
        handsoff_dir.glob("python-*-amd64.exe"),
        key=lambda p: p.name,
        reverse=True,
    )
    if not installer_candidates:
        warn("Private Python runtime is broken but no python-*-amd64.exe installer found.")
        return False

    installer_path = installer_candidates[0]
    status(f"Auto-healing private Python runtime from {installer_path.name}...")

    # ── Clean up old broken installation ─────────────────────────────
    if private_python_dir.exists():
        try:
            # Remove hidden attribute before deletion
            subprocess.run(
                ["attrib", "-h", str(private_python_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=5,
            )
        except Exception:
            pass
        try:
            shutil.rmtree(private_python_dir, ignore_errors=True)
        except Exception:
            pass

    # ── Run the Python installer silently ────────────────────────────
    log_path = SCRIPT_DIR / "logs" / "python_heal_install.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    install_args = [
        "/quiet",
        f"TargetDir={private_python_dir}",
        "InstallAllUsers=0",
        "PrependPath=0",
        "Include_test=0",
        "Include_doc=0",
        "Include_launcher=0",
        "/log", str(log_path),
    ]

    try:
        status("Installing private Python runtime (may request Administrator)...")
        exit_code = _shell_execute_elevated_wait(
            str(installer_path), install_args, timeout=180.0
        )
    except PermissionError:
        warn("Administrator permission was declined for Python runtime repair.")
        return False
    except Exception as exc:
        fail(f"Could not launch Python installer: {exc}")
        return False

    if exit_code != 0:
        fail(f"Python installer exited with code {exit_code}. Check log: {log_path}")
        return False

    # ── Post-install: apply hidden attribute ────────────────────────
    if private_python_dir.exists():
        try:
            subprocess.run(
                ["attrib", "+h", str(private_python_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=5,
            )
        except Exception:
            pass

    # ── Verify the repair ───────────────────────────────────────────
    if _is_runtime_healthy():
        ok(f"Private Python runtime healed successfully from {installer_path.name}.")
        return True
    else:
        fail("Python installer completed but the runtime still fails health check.")
        return False


def ensure_python_system_environment() -> bool:
    """
    Make the private runtime available to child processes for this launch.

    The app no longer writes its movable ``env`` directory into HKLM/HKCU
    PATH. Such entries become stale as soon as the folder is copied to another
    PC and should never affect unrelated Python installations.
    """
    current_python = Path(sys.executable).resolve()
    py_dir = current_python.parent
    scripts_dir = py_dir / "Scripts"

    venv_dir = SCRIPT_DIR / "env"
    venv_scripts = venv_dir / "Scripts" if sys.platform == "win32" else venv_dir / "bin"

    dirs_to_add = [str(py_dir)]
    if scripts_dir.is_dir():
        dirs_to_add.append(str(scripts_dir))
    if venv_scripts.is_dir():
        dirs_to_add.append(str(venv_scripts.resolve()))

    current_parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    known = {os.path.normcase(os.path.normpath(p)) for p in current_parts}
    for directory in reversed(dirs_to_add):
        key = os.path.normcase(os.path.normpath(directory))
        if key not in known:
            current_parts.insert(0, directory)
            known.add(key)
    os.environ["PATH"] = os.pathsep.join(current_parts)
    ok("Private Python runtime configured for this launch.")
    return True

    if sys.platform != "win32":
        ok("Python environment configuration checked.")
        return True

    status("Configuring Python System Environment Variables (Global / All Users)...")

    hklm_key_path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    hklm_updated = False

    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, hklm_key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
        try:
            current_path, path_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current_path, path_type = "", winreg.REG_EXPAND_SZ

        existing_parts = [p.strip() for p in current_path.split(";") if p.strip()]
        existing_parts_lower = [p.lower() for p in existing_parts]

        added_any = False
        new_parts = list(existing_parts)

        for d in dirs_to_add:
            if d.lower() not in existing_parts_lower:
                new_parts.append(d)
                existing_parts_lower.append(d.lower())
                added_any = True

        if added_any:
            new_path = ";".join(new_parts)
            winreg.SetValueEx(key, "Path", 0, path_type, new_path)
            ok("Python directory permanently added to System Path (HKLM - Global / All Users)")
        else:
            ok("Python directory is already present in System Path (HKLM - Global / All Users)")

        # Set PYTHON_HOME system variable in HKLM for global tools
        try:
            winreg.SetValueEx(key, "PYTHON_HOME", 0, winreg.REG_SZ, str(py_dir))
        except Exception:
            pass

        winreg.CloseKey(key)
        hklm_updated = True
    except PermissionError:
        warn("Direct HKLM registry access requires Administrator privileges; updating User environment (HKCU).")
    except Exception as reg_err:
        warn(f"Error checking HKLM registry: {reg_err}")

    # Fallback to HKCU if HKLM modification could not be performed
    if not hklm_updated:
        try:
            import winreg
            hkcu_key_path = r"Environment"
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, hkcu_key_path, 0, winreg.KEY_READ | winreg.KEY_WRITE)
            try:
                current_path, path_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current_path, path_type = "", winreg.REG_EXPAND_SZ

            existing_parts = [p.strip() for p in current_path.split(";") if p.strip()]
            existing_parts_lower = [p.lower() for p in existing_parts]

            added_any = False
            new_parts = list(existing_parts)

            for d in dirs_to_add:
                if d.lower() not in existing_parts_lower:
                    new_parts.append(d)
                    existing_parts_lower.append(d.lower())
                    added_any = True

            if added_any:
                new_path = ";".join(new_parts)
                winreg.SetValueEx(key, "Path", 0, path_type, new_path)
                ok("Python directory added to User Path (HKCU - Current User Fallback)")
            else:
                ok("Python directory is already present in User Path (HKCU)")

            winreg.CloseKey(key)
        except Exception as hkcu_err:
            warn(f"Could not update User environment variable: {hkcu_err}")

    # Broadcast WM_SETTINGCHANGE so system and active windows pick up environment updates immediately
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(result)
        )
    except Exception:
        pass

    return True





# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
def _relaunch_visible_if_hidden():
    """
    No-op: the VBS now launches bootstrap via pythonw.exe and the GUI
    provides its own visible window — no console re-launch is needed.
    Kept for compatibility in case it is called elsewhere.
    """
    pass


def _is_env_healthy() -> bool:
    """
    Diagnostic check for whether the venv and required imports are healthy.
    This check is never used as a reason to skip bootstrap setup.
    """
    venv_dir = SCRIPT_DIR / "env"
    try:
        current_exe = Path(sys.executable).resolve()
        if venv_dir.resolve() not in current_exe.parents:
            return False
    except Exception:
        return False

    try:
        import serial  # noqa: F401
        import serial.tools.list_ports  # noqa: F401
        # pyrefly: ignore [missing-import]
        import platformio  # noqa: F401
        # pyrefly: ignore [missing-import]
        import esptool  # noqa: F401
        # pyrefly: ignore [missing-import]
        import webview  # noqa: F401
        # pyrefly: ignore [missing-import]
        import psutil  # noqa: F401
        # pyrefly: ignore [missing-import]
        import certifi  # noqa: F401
        # pyrefly: ignore [missing-import]
        import websockets  # noqa: F401
        # pyrefly: ignore [missing-import]
        import PyQt5.QtWidgets  # noqa: F401
        # pyrefly: ignore [missing-import]
        import PyQt5.Qsci  # noqa: F401
    except ImportError:
        return False

    return True


# ─────────────────────────────────────────────────────────────
# Bootstrap health snapshot (diagnostics only)
# ─────────────────────────────────────────────────────────────
# The bootstrap process remains the mandatory repair/setup entry point. This
# local manifest records the result for troubleshooting, but it is never used
# to bypass dependency, board-toolchain, driver, or external-tool checks.
STARTUP_HEALTH_SCHEMA = 2


def _user_startup_state_dir() -> Path:
    """Return a writable, per-user state location for the launch manifest.

    Deployed builds may live below ``Program Files`` or on a read-only network
    share.  Startup health is user state, not application content, so never
    assume that ``SCRIPT_DIR`` is writable and never share one user's snapshot
    with another user on the same machine.
    """
    override = os.environ.get("MCU_FLASHER_STATE_DIR", "").strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))

    base = (
        os.environ.get("LOCALAPPDATA", "").strip()
        or os.environ.get("APPDATA", "").strip()
        or str(Path.home() / "AppData" / "Local")
    )
    return Path(base) / "MCUFlasherByNaph"


STARTUP_HEALTH_FILE = _user_startup_state_dir() / "startup_health.json"
_STARTUP_REQUIRED_PACKAGE_DIRS = (
    "serial",
    "webview",
    "platformio",
    "esptool",
    "requests",
    "urllib3",
    "winpty",
    "win32",
    "websockets",
    "psutil",
    "yaml",
    "PyQt5",
)


def _startup_app_fingerprint() -> str:
    """Return a cheap fingerprint for files that define the launch contract."""
    gui_target = (
        SCRIPT_DIR / "main" / "mcu_flash_gui.py"
        if (SCRIPT_DIR / "main" / "mcu_flash_gui.py").exists()
        else SCRIPT_DIR / "mcu_flash_gui.py"
    )
    tracked = (
        gui_target,
        SCRIPT_DIR / "src" / "modules" / "bootstrap.py",
        SCRIPT_DIR / "src" / "modules" / "launcher.py",
    )
    rows = []
    for path in tracked:
        try:
            stat = path.stat()
            rows.append((str(path.relative_to(SCRIPT_DIR)), stat.st_size, stat.st_mtime_ns))
        except OSError:
            rows.append((str(path.relative_to(SCRIPT_DIR)), None, None))
    return json.dumps(rows, separators=(",", ":"), sort_keys=True)


def _startup_installation_identity() -> str:
    """Return a case-insensitive identity for this deployed app instance."""
    try:
        return str(SCRIPT_DIR.resolve()).casefold()
    except OSError:
        return str(SCRIPT_DIR).casefold()


def _startup_site_packages_dir() -> Path | None:
    """Resolve the current venv site-packages directory without importing it."""
    current = Path(sys.executable).resolve()
    venv_dir = SCRIPT_DIR / "env"
    try:
        if venv_dir.resolve() not in current.parents:
            return None
    except Exception:
        return None
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = (
        venv_dir / "Lib" / "site-packages",
        venv_dir / "lib" / version / "site-packages",
        venv_dir / "lib" / "site-packages",
    )
    for site in candidates:
        if site.is_dir():
            return site
    return None


def _startup_required_paths() -> list[Path]:
    """Return only files needed to construct the current main GUI shell."""
    site = _startup_site_packages_dir()
    paths = [Path(sys.executable), GUI_SCRIPT]
    if site is not None:
        paths.extend(site / name for name in _STARTUP_REQUIRED_PACKAGE_DIRS)
    return paths


def _read_startup_health_snapshot() -> dict | None:
    """Return a valid cached health snapshot, or ``None`` for repair mode."""
    try:
        if _startup_site_packages_dir() is None:
            return None
        data = json.loads(STARTUP_HEALTH_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if int(data.get("schema", 0)) != STARTUP_HEALTH_SCHEMA:
            return None
        if data.get("installation_root") != _startup_installation_identity():
            return None
        if data.get("python_executable") != str(Path(sys.executable).resolve()).casefold():
            return None
        if data.get("app_fingerprint") != _startup_app_fingerprint():
            return None
        current_python_version = ".".join(str(x) for x in sys.version_info[:3])
        if data.get("python_version") != current_python_version:
            return None
        if not bool(data.get("critical_ready")):
            return None
        if not all(path.exists() for path in _startup_required_paths()):
            return None
        core_dir = str(data.get("platformio_core_dir") or "").strip()
        if core_dir and not Path(core_dir).exists():
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def _write_startup_health_snapshot() -> bool:
    """Persist the successful setup result atomically for diagnostics."""
    temporary: Path | None = None
    try:
        if _startup_site_packages_dir() is None:
            return False
        STARTUP_HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": STARTUP_HEALTH_SCHEMA,
            "installation_root": _startup_installation_identity(),
            "python_executable": str(Path(sys.executable).resolve()).casefold(),
            "app_fingerprint": _startup_app_fingerprint(),
            "python_version": ".".join(str(x) for x in sys.version_info[:3]),
            "python_executable_name": Path(sys.executable).name,
            "critical_ready": all(path.exists() for path in _startup_required_paths()),
            "platformio_core_dir": os.environ.get("PLATFORMIO_CORE_DIR", ""),
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if not payload["critical_ready"]:
            return False
        temporary = STARTUP_HEALTH_FILE.with_name(
            f"{STARTUP_HEALTH_FILE.name}.tmp-{os.getpid()}"
        )
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, STARTUP_HEALTH_FILE)
        return True
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass
        return False


def _try_fast_normal_launch() -> bool:
    """Keep the legacy fast-launch hook disabled.

    Every launch must pass through the complete bootstrap pipeline.  A cached
    health snapshot is useful for diagnostics, but it cannot prove that a
    dependency, board package, driver, or external tool was not removed after
    the snapshot was written.
    """
    return False


def _explicit_setup_requested() -> bool:
    """Return true when the caller intentionally requested repair/setup."""
    requested = {"--repair", "--setup", "--force-setup", "--force-repair"}
    if any(str(arg).lower() in requested for arg in sys.argv[1:]):
        return True
    return (SCRIPT_DIR / ".force_rebuild").exists()


def _spawn_main_gui() -> "tuple[subprocess.Popen | None, Path | None]":
    """
    Launch MCU Flasher.exe (if exists) or mcu_flash_gui.py as a detached process.
    Returns a tuple: (Popen object or None, Log Path or None)
    """
    import subprocess as sp

    # Skip running the wrapper EXE to prevent relaunch loop; always run Python GUI script


    if not GUI_SCRIPT.exists():
        return None, None

    # Preserve a project passed through an editor-mode restart. Bootstrap is
    # now part of that restart path, so the user should return to the same
    # project instead of being sent back to the project picker.
    gui_args = ["--from-bootstrap"]
    if "--project" in sys.argv:
        try:
            project_index = sys.argv.index("--project")
            if project_index + 1 < len(sys.argv):
                project_candidate = Path(sys.argv[project_index + 1]).resolve(strict=False)
                if project_candidate.is_dir():
                    gui_args.extend(["--project", str(project_candidate)])
        except Exception:
            pass
    if "--new-window" in sys.argv:
        gui_args.append("--new-window")

    logs_dir = SCRIPT_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    gui_log = logs_dir / "gui_crash.log"
    log_fh = None

    if sys.platform == "win32":
        # Prefer venv python (env/Scripts/pythonw.exe or python.exe) where all pip dependencies live
        venv_dir = SCRIPT_DIR / "env"
        venv_pythonw = venv_dir / "Scripts" / "pythonw.exe"
        venv_python  = venv_dir / "Scripts" / "python.exe"

        if venv_pythonw.exists():
            python_exe = venv_pythonw
        elif venv_python.exists():
            python_exe = venv_python
        else:
            python_exe = Path(sys.executable).parent / "pythonw.exe"
            if not python_exe.exists():
                python_exe = Path(sys.executable).parent / "python.exe"
            if not python_exe.exists():
                python_exe = Path(sys.executable)

        # Try to find a non-locked log file name to support multiple concurrent windows
        for i in range(10):
            suffix = "" if i == 0 else f"_{i}"
            candidate_log = logs_dir / f"gui_crash{suffix}.log"
            try:
                candidate_log.write_text("", encoding="utf-8")
                log_fh = open(candidate_log, "w", encoding="utf-8")
                gui_log = candidate_log
                break
            except Exception:
                continue

        if log_fh is None:
            try:
                log_fh = open(os.devnull, "w", encoding="utf-8")
            except Exception:
                log_fh = open(os.devnull, "w")
        
        # Override inherited hidden window state to ensure the spawned GUI displays normally
        startupinfo = sp.STARTUPINFO()
        startupinfo.dwFlags |= sp.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL
        
        launch_env = os.environ.copy()
        launch_env["PLATFORMIO_CORE_DIR"] = os.environ.get(
            "PLATFORMIO_CORE_DIR", _get_safe_platformio_core_dir(SCRIPT_DIR)
        )

        try:
            proc = sp.Popen(
                [str(python_exe), str(GUI_SCRIPT), *gui_args],
                cwd=str(SCRIPT_DIR),
                env=launch_env,
                stdin=sp.DEVNULL,   # never inherit a dead/closed handle from pythonw
                stderr=log_fh,
                stdout=log_fh,
                startupinfo=startupinfo,
                # DETACHED_PROCESS breaks the parent-console inheritance chain
                # so the main GUI is a fully independent top-level process.
                creationflags=sp.DETACHED_PROCESS,
            )
        finally:
            log_fh.close()
    else:
        launch_env = os.environ.copy()
        launch_env["PLATFORMIO_CORE_DIR"] = os.environ.get(
            "PLATFORMIO_CORE_DIR", _get_safe_platformio_core_dir(SCRIPT_DIR)
        )
        proc = sp.Popen(
            [sys.executable, str(GUI_SCRIPT), *gui_args],
            cwd=str(SCRIPT_DIR),
            env=launch_env,
        )

    return proc, gui_log


def _regenerate_venv_console_scripts(venv_dir: Path, venv_python: Path) -> bool:
    """Force pip to rewrite this venv's console-script launcher stubs (.exe files
    like pio.exe, esptool.exe, platformio.exe) so they point at the venv's
    CURRENT interpreter path.

    Those .exe files are thin, pip-generated wrappers with the absolute path
    to python.exe baked into the compiled binary at install time -- not a
    text file, so pyvenv.cfg/activate-script repair (see
    _repair_venv_in_place) never touches them. When the whole project folder
    is copied or moved to a different drive/username (e.g. handed off to a
    different user, or a copy left over on another drive), those stubs still
    point at the OLD location and fail with:
        "Fatal error in launcher: Unable to create process ..."
    even though `venv_python -c "import sys"` succeeds and the interpreter
    itself is perfectly fine. Re-running pip install --force-reinstall
    --no-deps on the packages that ship console scripts regenerates every
    stub against the venv's actual current path -- no re-download of
    dependencies, just a fast local rewrite of the entry-point wrappers.

    This is username/drive-agnostic: venv_dir and venv_python are always
    passed in as whatever this project's *current* location resolves to
    (SCRIPT_DIR / "env"), never a hardcoded path.
    """
    if not venv_python.exists():
        return False

    # Packages known to install their own .exe console-script wrappers that
    # this project actually invokes directly (pio.exe / platformio.exe /
    # esptool.exe). Extend this list if a future dependency adds one.
    console_script_packages = ["platformio", "esptool"]

    try:
        subprocess.run(
            [str(venv_python), "-m", "pip", "install",
             "--force-reinstall", "--no-deps", "--no-warn-script-location",
             *console_script_packages],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return True
    except Exception:
        return False


def _venv_console_scripts_are_stale(venv_dir: Path) -> bool:
    """Best-effort check: does pio.exe's own on-disk location differ from
    where this venv currently lives? A moved/copied venv's .exe stubs still
    have their ORIGINAL creation path baked in, but we can't read that
    without executing them (which is exactly what fails) -- so instead we
    just try running pio.exe directly and see whether Windows can even
    launch it. A clean, fast, side-effect-free probe."""
    scripts_dir = venv_dir / "Scripts" if sys.platform == "win32" else venv_dir / "bin"
    pio_exe = scripts_dir / ("pio.exe" if sys.platform == "win32" else "pio")
    if not pio_exe.exists():
        return False  # nothing to regenerate yet -- first-ever install path handles this
    try:
        res = subprocess.run(
            [str(pio_exe), "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return res.returncode != 0
    except Exception:
        return True  # launch itself failed (e.g. the "Unable to create process" case) -> stale


def _repair_venv_in_place(venv_dir: Path, host_python: Path) -> bool:
    """Repair pyvenv.cfg and activation scripts in-place when venv is moved to a new device or path."""
    try:
        cfg_path = venv_dir / "pyvenv.cfg"
        if not cfg_path.is_file():
            return False

        host_py_resolved = host_python.resolve()
        host_dir = str(host_py_resolved.parent)
        host_exe = str(host_py_resolved)
        abs_venv_dir = str(venv_dir.resolve())

        # 1. Update pyvenv.cfg
        lines = cfg_path.read_text(encoding="utf-8", errors="replace").splitlines()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.lower().startswith("home =") or stripped.lower().startswith("home="):
                new_lines.append(f"home = {host_dir}")
            elif stripped.lower().startswith("executable =") or stripped.lower().startswith("executable="):
                new_lines.append(f"executable = {host_exe}")
            elif stripped.lower().startswith("base-prefix =") or stripped.lower().startswith("base-prefix="):
                new_lines.append(f"base-prefix = {host_dir}")
            elif stripped.lower().startswith("base-exec-prefix =") or stripped.lower().startswith("base-exec-prefix="):
                new_lines.append(f"base-exec-prefix = {host_dir}")
            elif stripped.lower().startswith("base-executable =") or stripped.lower().startswith("base-executable="):
                new_lines.append(f"base-executable = {host_exe}")
            else:
                new_lines.append(line)
        cfg_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        # 2. Update activation scripts
        if sys.platform == "win32":
            act_bat = venv_dir / "Scripts" / "activate.bat"
            if act_bat.is_file():
                bat_lines = act_bat.read_text(encoding="utf-8", errors="replace").splitlines()
                new_bat_lines = []
                for b_line in bat_lines:
                    s_b = b_line.strip().lower()
                    if s_b.startswith('set "virtual_env=') or s_b.startswith('set virtual_env='):
                        new_bat_lines.append(f'set "VIRTUAL_ENV={abs_venv_dir}"')
                    else:
                        new_bat_lines.append(b_line)
                act_bat.write_text("\n".join(new_bat_lines) + "\n", encoding="utf-8")

            act_ps1 = venv_dir / "Scripts" / "Activate.ps1"
            if act_ps1.is_file():
                ps1_lines = act_ps1.read_text(encoding="utf-8", errors="replace").splitlines()
                new_ps1_lines = []
                for p_line in ps1_lines:
                    if "$env:VIRTUAL_ENV" in p_line:
                        new_ps1_lines.append(f'$env:VIRTUAL_ENV="{abs_venv_dir}"')
                    else:
                        new_ps1_lines.append(p_line)
                act_ps1.write_text("\n".join(new_ps1_lines) + "\n", encoding="utf-8")
        else:
            act_sh = venv_dir / "bin" / "activate"
            if act_sh.is_file():
                sh_lines = act_sh.read_text(encoding="utf-8", errors="replace").splitlines()
                new_sh_lines = []
                for s_line in sh_lines:
                    if s_line.strip().startswith('VIRTUAL_ENV='):
                        new_sh_lines.append(f'VIRTUAL_ENV="{abs_venv_dir}"')
                    else:
                        new_sh_lines.append(s_line)
                act_sh.write_text("\n".join(new_sh_lines) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def _install_arduino_cli_machine_only() -> bool:
    """Install Arduino-CLI itself without prewarming per-user board cores.

    This remains available for the one-shot helper compatibility path.  The
    normal launcher now elevates the complete bootstrap chain, so board/core
    preparation can use the same Administrator token without nesting helpers.
    """
    if sys.platform != "win32":
        return True
    if find_arduino_cli():
        return True

    msi_path = SCRIPT_DIR / "installers" / "arduino-cli.msi"
    if not _is_valid_msi(msi_path):
        if not _refresh_bundled_msi():
            return False
    if not _run_arduino_cli_msi(msi_path):
        return False
    return find_arduino_cli() is not None


def _run_privileged_first_run_tasks() -> bool:
    """Run machine-level first-run installers under one elevated process.

    This compatibility entry point remains for older launchers.  The current
    VBS/Python launch chain elevates before bootstrap starts, so normal setup
    already runs under the same token and does not need this helper.
    """
    global _gui
    if sys.platform != "win32":
        return True
    if not _is_process_elevated():
        return False

    # The helper is commonly launched with pythonw.exe, where stdout/stderr may
    # not exist.  Route bootstrap logging/progress calls to a no-op sink instead
    # of allowing print() fallbacks to crash the hidden elevated process.
    class _SilentPrivilegedGUI:
        _closed = False
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    _gui = _SilentPrivilegedGUI()
    critical_ok = True

    # Install the complete Windows bundle once while this helper owns the
    # elevated token. Board/core setup is handled by the main elevated
    # bootstrap after this compatibility helper returns.
    installer_results = _ensure_bundled_windows_installers(
        machine_only_arduino=True
    )
    critical_ok = bool(installer_results.get("arduino_cli", False))

    # Best effort: if Node/npm is absent, let the already-elevated helper
    # install Node LTS now (winget first, then direct MSI fallback).  OpenCode
    # itself is still installed later by the NORMAL user process so npm -g
    # targets that user's APPDATA.
    try:
        if not _find_usable_npm_cmd():
            _install_nodejs_lts()
    except Exception:
        pass

    return critical_ok


def _request_first_run_privileged_setup(gui: Optional[BootstrapGUI] = None) -> bool:
    """Request at most one UAC consent prompt for first-run system installers.

    The normal launcher requests elevation before bootstrap starts.  This
    function remains defensive for direct bootstrap invocation and avoids
    spawning a second elevated helper when the current process already owns
    the Administrator token.
    """
    if sys.platform != "win32":
        return True
    if _is_process_elevated():
        # The VBS launcher or an administrator shortcut already supplied a
        # full token. Spawning this helper again only duplicates installer
        # work and can make concurrent MSI logging fail.
        status("Setup is already running as Administrator; no extra elevation helper is needed.")
        return True

    needs_privileged_work = (
        not check_webview2_runtime()
        or find_arduino_cli() is None
        or not check_cp210x_driver()
        or _find_usable_npm_cmd() is None
    )
    if not needs_privileged_work:
        return True

    if gui:
        gui.set_status("Requesting Administrator permission once for first-run system components...")
        gui.start_busy(start_pct=5, cap_pct=92)
    status("First run: requesting Administrator permission once for system components...")

    try:
        # Execute this same bootstrap module in a special helper mode.  Use the
        # original host interpreter captured before sys.executable was pointed at
        # the new venv.
        helper_python = _ORIGINAL_PYTHON_EXECUTABLE
        helper_script = str(Path(__file__).resolve())
        exit_code = _shell_execute_elevated_wait(
            helper_python,
            [helper_script, "--privileged-first-run"],
            timeout=1200,
        )
        if exit_code == 0:
            ok("First-run system components prepared with one Administrator approval.")
            return True
        warn(f"First-run elevated helper exited with code {exit_code}; setup will re-check each component normally.")
        return False
    except PermissionError:
        warn("Administrator permission was declined; system-level components may require approval later.")
        return False
    except Exception as exc:
        warn(f"Could not complete one-time elevated setup: {exc}")
        return False
    finally:
        if gui:
            gui.stop_busy(restore_step=True)


def _prepare_bundled_windows_installers(
    gui: Optional[BootstrapGUI] = None,
) -> tuple[bool, bool, dict[str, bool]]:
    """Prepare the Windows installer bundle without nesting elevation.

    Returns ``(already_elevated, privileged_setup_ok, direct_results)``.
    When a VBS/admin shortcut already supplied elevation, the bundle runs in
    this bootstrap process. Otherwise the one-shot elevated helper owns it.
    """
    if sys.platform != "win32":
        return False, True, {}

    already_elevated = _is_process_elevated()
    privileged_setup_ok = _request_first_run_privileged_setup(gui)
    direct_results = (
        _ensure_bundled_windows_installers() if already_elevated else {}
    )
    return already_elevated, privileged_setup_ok, direct_results


def _activate_bootstrap_venv(venv_dir: Path, venv_python: Path) -> bool:
    """Point this bootstrap process at an existing venv without reopening its UI."""
    if not venv_python.exists():
        return False

    # A copied/malformed venv can point pyvenv.cfg back to its own
    # env\Scripts\python.exe. Python then recursively launches itself before
    # any application code gets a chance to repair it. Repair from the host
    # interpreter first, while this process is still healthy.
    try:
        current_exe = Path(_ORIGINAL_PYTHON_EXECUTABLE).resolve()
        if venv_dir.resolve() not in current_exe.parents:
            _repair_venv_in_place(venv_dir, current_exe)
    except Exception:
        pass

    functional = False
    try:
        res = subprocess.run(
            [str(venv_python), "-c", "import sys"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if res.returncode == 0:
            functional = True
    except Exception:
        pass

    if not functional:
        # Attempt in-place repair using host Python
        _repair_venv_in_place(venv_dir, Path(sys.executable))
        try:
            res = subprocess.run(
                [str(venv_python), "-c", "import sys"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if res.returncode == 0:
                functional = True
        except Exception:
            pass

    if not functional:
        return False

    # The `import sys` probe above only proves the raw interpreter works --
    # it says nothing about pio.exe/esptool.exe, whose launch path is baked
    # in separately at install time (see _regenerate_venv_console_scripts).
    # A venv copied here from a different drive/username can pass that probe
    # while pio.exe is still completely dead, so check it explicitly.
    if _venv_console_scripts_are_stale(venv_dir):
        _regenerate_venv_console_scripts(venv_dir, venv_python)

    try:
        if sys.platform == "win32":
            site_packages = venv_dir / "Lib" / "site-packages"
            scripts_dir = venv_dir / "Scripts"
        else:
            try:
                site_packages = next((venv_dir / "lib").glob("python*/site-packages"))
            except StopIteration:
                site_packages = venv_dir / "lib" / "site-packages"
            scripts_dir = venv_dir / "bin"
        if site_packages.is_dir():
            site_path = str(site_packages)
            if site_path in sys.path:
                sys.path.remove(site_path)
            sys.path.insert(0, site_path)
        os.environ["VIRTUAL_ENV"] = str(venv_dir)
        os.environ["PATH"] = str(scripts_dir) + os.pathsep + os.environ.get("PATH", "")
        # All bootstrap subprocesses use this value, so pip and the final
        # GUI launch now target env without starting a second bootstrap GUI.
        sys.executable = str(venv_python)
        return True
    except Exception:
        return False



def _run_setup_in_thread(gui: BootstrapGUI):
    """
    Runs all dependency checks on a background thread so the Tk event loop
    stays responsive (spinner keeps animating, window stays draggable).

    When complete, posts a callback to the main thread to close the
    bootstrap window and launch the main GUI.
    """
    global _gui

    try:
        def _log_worker_start():
            gui.log_banner()
            gui.log_dim(f"Detailed run log: {get_bootstrap_log_file()}")
            gui.log_status("Bootstrap worker started; every setup result is being recorded.")
            if _BOOTSTRAP_STARTUP_NOTE:
                gui.log_dim(_BOOTSTRAP_STARTUP_NOTE)

        gui.root.after(0, _log_worker_start)

        # PlatformIO setup is a repair/install concern, not an import-time
        # concern.  Keep directory/junction/configuration work on this worker
        # so a normal launch can skip it entirely.
        _configure_platformio_environment(SCRIPT_DIR)
        _neutralize_conflicting_global_platformio_config()

        if sys.platform == "win32":
            try:
                from win_subprocess_hide import install_venv_site_hook
                install_venv_site_hook(SCRIPT_DIR)
            except Exception:
                pass

        # ── Auto-heal Private Python Runtime & Environment ─────────────
        if sys.platform == "win32":
            gui.root.after(0, lambda: gui.log_section("Checking Python Runtime Environment"))
            heal_result = _heal_private_python_runtime()
            if not heal_result:
                gui.root.after(0, lambda: gui.log_warn(
                    "Private Python runtime could not be healed. The app will "
                    "attempt to continue with the current interpreter."
                ))
            if not ensure_python_system_environment():
                gui.root.after(0, lambda: gui.log_warn("Could not permanently update System environment variables."))

        # ── Venv setup ────────────────────────────────────────────────
        venv_dir = SCRIPT_DIR / "env"
        if sys.platform == "win32":
            venv_python = venv_dir / "Scripts" / "python.exe"
        else:
            venv_python = venv_dir / "bin" / "python"

        current_python = Path(sys.executable).resolve()
        target_python = venv_python.resolve() if venv_python.exists() else None

        is_in_venv = False
        venv_created_this_run = False
        try:
            is_in_venv = venv_dir.resolve() in current_python.parents
        except Exception:
            pass

        # When bootstrap was started by a system Python but env already
        # exists, keep this window and switch its child commands to env.
        if not is_in_venv and venv_python.exists():
            gui.root.after(0, lambda: gui.log_section("Python Environment"))
            if _activate_bootstrap_venv(venv_dir, venv_python):
                gui.root.after(0, lambda: gui.log_ok("Existing env folder found; using it."))
                is_in_venv = True
            else:
                gui.root.after(0, lambda: gui.log_warn(
                    "Existing env could not be activated; it will be recreated."
                ))

        if not is_in_venv:
            is_portable = ((SCRIPT_DIR / "src" / "_python").resolve() in current_python.parents or (SCRIPT_DIR / "_python").resolve() in current_python.parents)

            gui.root.after(0, lambda: gui.log_section("Setting up Python Environment"))

            # Remove any leftover corrupted env folder so venv.create()
            # doesn't hang on a half-baked directory (most common cause of
            # "stuck at Creating virtual environment" reports).
            if venv_dir.exists():
                gui.root.after(0, lambda: gui.log_status("Removing stale env folder…"))
                try:
                    shutil.rmtree(venv_dir, ignore_errors=True)
                except Exception:
                    pass

            venv_created = False
            try:
                import venv as _venv
                gui.root.after(0, lambda: gui.log_status("Creating virtual environment in 'env'…"))

                # Create skeleton first with with_pip=False (fast ~100ms, avoids ensurepip hangs on Windows/Python 3.14)
                _venv.create(str(venv_dir), with_pip=False, clear=True,
                             symlinks=sys.platform != "win32")

                # Pre-seed pip, setuptools, wheel instantly from base Python site-packages (~0.05s)
                try:
                    base_prefix = Path(getattr(sys, "base_prefix", sys.prefix))
                    if sys.platform == "win32":
                        base_site = base_prefix / "Lib" / "site-packages"
                        venv_site = venv_dir / "Lib" / "site-packages"
                    else:
                        base_site = base_prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
                        venv_site = venv_dir / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"

                    if base_site.is_dir() and venv_site.is_dir():
                        for item in base_site.glob("*"):
                            name_lower = item.name.lower()
                            if any(k in name_lower for k in ("pip", "setuptools", "wheel", "distutils", "pkg_resources")):
                                target = venv_site / item.name
                                if not target.exists():
                                    if item.is_dir():
                                        shutil.copytree(item, target, dirs_exist_ok=True)
                                    else:
                                        shutil.copy2(item, target)

                except Exception:
                    pass

                gui.root.after(0, lambda: gui.log_ok("Virtual environment created."))
                venv_created = True
                venv_created_this_run = True
            except Exception as venv_error:
                gui.root.after(0, lambda venv_error=venv_error: gui.log_warn(
                    f"Built-in venv setup failed ({venv_error}); trying the Python command fallback."
                ))
                # Clean up partial/broken env before the subprocess attempt
                try:
                    shutil.rmtree(venv_dir, ignore_errors=True)
                except Exception:
                    pass
                if is_portable:
                    gui.root.after(0, lambda: gui.log_status("Embeddable Python — installing directly…"))
                else:
                    try:
                        gui.root.after(0, lambda: gui.log_status("Creating venv via subprocess…"))
                        subprocess.run(
                            [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL,
                            timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                            check=True,
                        )
                        gui.root.after(0, lambda: gui.log_ok("Virtual environment created (subprocess fallback)."))
                        venv_created = True
                        venv_created_this_run = True
                    except Exception:
                        gui.root.after(0, lambda: gui.log_fail("Could not create virtual environment."))
                        if not is_portable:
                            gui.root.after(0, lambda: gui.log_warn(
                                "Installing into global Python (recommend Python with venv support)."))
                        else:
                            gui.root.after(0, lambda: gui.log_ok("Proceeding with portable Python."))

            if venv_created:
                if _clear_editor_config_after_new_environment():
                    gui.root.after(0, lambda: gui.log_ok(
                        "Fresh env detected; removed stale app-local editor metadata; saved editor preference preserved."
                    ))

                if sys.platform == "win32":
                    try:
                        from win_subprocess_hide import install_venv_site_hook
                        install_venv_site_hook(SCRIPT_DIR)
                    except Exception:
                        pass

                if not venv_python.exists():
                    def _no_venv():
                        gui.log_fail(f"venv Python not found at {venv_python}")
                        gui.stop_spinner("Setup failed", ok=False)
                        gui.show_error("MCU Upload GUI — Error",
                                       f"Virtual environment Python not found:\n{venv_python}")
                        gui.close_after_delay()
                    gui.root.after(0, _no_venv)
                    return

                if _activate_bootstrap_venv(venv_dir, venv_python):
                    gui.root.after(0, lambda: gui.log_ok(
                        "env folder created; continuing setup in this window."
                    ))
                else:
                    def _venv_activate_fail():
                        gui.log_fail(f"Could not activate env at {venv_python}")
                        gui.stop_spinner("Setup failed", ok=False)
                        gui.show_error("MCU Upload GUI — Error",
                                       f"Virtual environment could not be activated:\n{venv_python}")
                        gui.close_after_delay()
                    gui.root.after(0, _venv_activate_fail)
                    return

        # On Windows, batch machine-level installs (WebView2, Arduino CLI,
        # CP210x, Node LTS) into one elevated helper. If the VBS launcher was
        # already run as Administrator, install the bundled Windows set right
        # here with the existing token instead of spawning a second helper.
        (
            bootstrap_is_elevated,
            privileged_setup_ok,
            bundled_installer_results,
        ) = _prepare_bundled_windows_installers(gui)

        def _fail_and_exit(component: str, detail: str = ""):
            def _on_gui():
                gui.log_fail(f"Installation/Setup failed for {component}.")
                if detail:
                    gui.log_fail(f"  {detail}")
                gui.log_fail("Setup aborted. Application cannot proceed.")
                gui.stop_spinner("Setup failed", ok=False)
                err_msg = f"Failed to download/install {component}.\n\n"
                if detail:
                    err_msg += f"Details: {detail}\n\n"
                err_msg += "Setup cannot proceed. Please resolve the error and try again."
                gui.show_error("MCU Uploader IDE by Naph — Setup Error", err_msg)
                gui.close_after_delay()
            gui.root.after(0, _on_gui)

        # ── pip & Python Dependencies (Multithreaded Parallel Install) ─────
        gui.root.after(0, lambda: gui.log_section("Checking pip & Python Dependencies"))
        if not ensure_pip():
            _fail_and_exit("pip", "pip could not be installed.")
            return

        # Install every declared dependency during this bootstrap run.  Do not
        # defer feature packages to the first time a user opens that feature.
        if not ensure_pip_packages_parallel(gui, include_optional=True):
            _fail_and_exit("Python Dependencies", "One or more required pip packages failed to install.")
            return

        # ── Monaco runtime (required for the selected editor) ──────────
        gui.root.after(0, lambda: gui.log_section("Checking Microsoft Edge WebView2 Runtime"))
        if not _check_import_pywebview():
            gui.root.after(0, lambda: gui.log_warn(
                "pywebview failed its fresh-interpreter verification."
            ))
            _fail_and_exit(
                "pywebview",
                "The Monaco WebView dependency could not be imported after installation. "
                "Bootstrap will not launch the GUI with an incomplete dependency set.",
            )
            return
        elif sys.platform == "win32" and not ensure_webview2_runtime():
            gui.root.after(0, lambda: gui.log_warn(
                "WebView2 Runtime installation did not pass post-install verification."
            ))
            _fail_and_exit(
                "Microsoft Edge WebView2 Runtime",
                "The runtime is required by Monaco and was not detected after the repair attempt. "
                "Bootstrap will retry after the installation problem is resolved.",
            )
            return

        # ── PlatformIO + Board Toolchains (combined step) ───────────────
        gui.root.after(0, lambda: gui.log_section("Checking PlatformIO & Board Toolchains"))

        # Seed the PlatformIO core directory from a pre-built zip if it's
        # empty.  This avoids the very long first-run download/unpack/install
        # wait by providing a ready-made baseline that PlatformIO will accept
        # and incrementally update when new boards are added later.
        if not _ensure_platformio_core_prebuilt(gui):
            _fail_and_exit(
                "PlatformIO Pre-built Toolchains",
                "GitHub Releases could not provide the required PlatformIO archive. "
                "Bootstrap stopped without using another download source.",
            )
            return

        # Check PlatformIO
        if not ensure_platformio():
            _fail_and_exit("PlatformIO Core", "Failed to install PlatformIO Core.")
            return

        # Prepare portable board-core folders used by the Board Browser.
        if not ensure_arduino_avr_board():
            _fail_and_exit(
                "Arduino AVR Board Core",
                "The required Arduino AVR board folder could not be prepared.",
            )
            return

        if not ensure_esp32_board_folder():
            _fail_and_exit(
                "ESP32 Board Core",
                "The required ESP32 board folder could not be prepared.",
            )
            return

        # Pre-install PlatformIO frameworks/toolchains (ESP32 + AVR + downloaded boards).
        # Runs serially because all PlatformIO package operations share one core directory.
        if not ensure_board_toolchains():
            _fail_and_exit(
                "PlatformIO Board Toolchains",
                "One or more required board toolchains could not be prepared.",
            )
            return

        # ── Arduino-CLI ──────────────────────────────────────────────
        gui.root.after(0, lambda: gui.log_section("Checking Arduino-CLI"))
        if not ensure_arduino_cli():
            _fail_and_exit("Arduino-CLI", "Failed to install Arduino-CLI.")
            return

        # ── CP210x Driver ─────────────────────────────────────────────
        gui.root.after(0, lambda: gui.log_section("Checking CP210x Driver"))
        if sys.platform == "win32":
            cp_ok, cp_message = _cp210x_driver_status_message(
                check_cp210x_driver(),
                bootstrap_is_elevated,
                bundled_installer_results.get("cp210x", False),
                privileged_setup_ok,
            )
            gui.root.after(
                0,
                lambda: gui.log_ok(cp_message) if cp_ok else gui.log_warn(cp_message),
            )
            if not cp_ok:
                _fail_and_exit(
                    "CP210x USB Serial Driver",
                    cp_message,
                )
                return

        gui.root.after(0, lambda: gui.log_section("Checking OpenCode AI Assistant"))
        if sys.platform == "win32":
            # If already installed and working, pass immediately without network dependency
            if check_opencode_cli() and ensure_opencode_cli():
                gui.root.after(0, lambda: gui.log_ok("OpenCode AI Assistant is verified and ready."))
            else:
                # Missing — internet connection is required to install it
                if not _is_network_reachable(timeout=2.0):
                    _fail_and_exit(
                        "OpenCode AI Assistant",
                        "Internet connection is required to download and install OpenCode AI Assistant. Setup cannot proceed.",
                    )
                    return
                elif not ensure_opencode_cli():
                    _fail_and_exit(
                        "OpenCode AI Assistant",
                        "The required OpenCode AI Assistant is missing or could not be installed automatically.",
                    )
                    return


        # ── Update checks ─────────────────────────────────────────────
        # An enabled update check uses a bounded pool of network requests,
        # not one Python subprocess per package. The skip policy and any
        # override are logged inside run_update_checks().
        run_update_checks(auto_update=False)

        # ── Summary & launch ─────────────────────────────────────────
        def _finish():
            gui.log_ok("All dependencies ready!")

            # Keep a diagnostic record of the completed setup.  It is never a
            # launch gate: every subsequent launch repeats the full checks.
            if _write_startup_health_snapshot():
                gui.log_ok("Bootstrap health snapshot saved.")

            exe_path = SCRIPT_DIR / "MCU Flasher.exe"
            if not exe_path.exists() and not GUI_SCRIPT.exists():
                gui.log_fail(f"Application target not found in {SCRIPT_DIR}")
                gui.stop_spinner("GUI target missing", ok=False)
                gui.show_error("MCU Uploader IDE by Naph — Error",
                               f"Target application not found in:\n{SCRIPT_DIR}")
                gui.close_after_delay()
                return

            gui.log_status("Launching MCU Uploader IDE by Naph…")
            gui.stop_spinner("Launching…", ok=True)

        gui.root.after(0, _finish)

        # Hide Bootstrap before the main process is spawned. The old flow
        # waited for the main GUI's crash check before closing this window,
        # which let the Project Selector appear while Bootstrap was still
        # visibly disposing. Keep the crash check, but remove that overlap.
        #
        # The window is intentionally kept visible for BOOTSTRAP_CLOSE_DELAY_S
        # seconds so the user can read the final summary block (dependency
        # check results, update status, etc.) before it is withdrawn.
        bootstrap_hidden = threading.Event()

        def _hide_bootstrap_before_launch():
            def _do_hide():
                try:
                    gui.root.withdraw()
                finally:
                    bootstrap_hidden.set()
            gui.root.after(int(BOOTSTRAP_CLOSE_DELAY_S * 1000), _do_hide)

        gui.root.after(0, _hide_bootstrap_before_launch)
        bootstrap_hidden.wait(timeout=BOOTSTRAP_CLOSE_DELAY_S + 5.0)

        proc, gui_log = _spawn_main_gui()

        def _launch_done(proc=proc, gui_log=gui_log):
            if proc is None:
                gui.log_fail("Could not start the MCU Uploader IDE process.")
                gui.stop_spinner("GUI target missing", ok=False)
                gui.show_error("MCU Uploader IDE by Naph — Error",
                               f"Target application not found in:\n{SCRIPT_DIR}")
                gui.close_after_delay()
                return

            # Brief wait — check if it exited right away
            for _ in range(4):
                time.sleep(0.5)
                if proc.poll() is not None:
                    break

            exit_code = proc.poll()
            if exit_code is not None:
                # Exit code 0 means clean exit (e.g. user cancelled project selector).
                # Non-zero exit means an actual crash.
                if exit_code == 0:
                    _record_bootstrap_log("FINISH", "Main GUI exited cleanly during bootstrap handoff.")
                    # Clean exit — just close the bootstrap window quietly
                    try:
                        if gui_log and gui_log.exists() and gui_log.stat().st_size == 0:
                            gui_log.unlink()
                    except Exception:
                        pass
                    gui.close_after_delay(0.5)
                    return

                # Read crash log
                try:
                    crash_text = gui_log.read_text(encoding="utf-8", errors="replace").strip() if gui_log else ""
                except Exception:
                    crash_text = ""

                def _show_crash(crash_text=crash_text, code=exit_code, gui_log=gui_log):
                    try:
                        gui.root.deiconify()
                        gui.root.lift()
                    except Exception:
                        pass
                    gui.log_fail(f"MCU GUI crashed immediately (exit code {code}).")
                    if crash_text:
                        gui.log_section("Crash output")
                        for ln in crash_text.splitlines()[:30]:
                            gui.log_fail(f"  {ln}")
                    gui.stop_spinner("GUI crashed", ok=False)
                    gui.show_error(
                        "MCU Uploader IDE by Naph — Crash",
                        f"The GUI crashed immediately (code {code}).\n\n"
                        + (crash_text[:600] if crash_text else "(no output captured)")
                        + f"\n\nLog: {gui_log}",
                    )
                    gui.close_after_delay()

                gui.root.after(0, _show_crash)
                return

            # GUI alive — clean up empty log and close bootstrap window
            _record_bootstrap_log(
                "FINISH",
                "Main GUI started successfully after mandatory Bootstrap verification.",
            )
            try:
                if gui_log and gui_log.exists() and gui_log.stat().st_size == 0:
                    gui_log.unlink()
            except Exception:
                pass

            gui.close_after_delay(BOOTSTRAP_CLOSE_DELAY_S)

        _launch_done()

    except Exception as exc:
        _record_bootstrap_exception("Unhandled exception in bootstrap setup worker")

        def _err(exc=exc):
            gui.log_fail(f"Unexpected error: {exc}")
            gui.log_fail(f"Detailed run log: {get_bootstrap_log_file()}")
            gui.stop_spinner("Error", ok=False)
            gui.show_error(
                "MCU Uploader IDE by Naph — Error",
                f"Unexpected error:\n{exc}\n\nDetailed log:\n{get_bootstrap_log_file()}",
            )
            gui.close_after_delay()
        gui.root.after(0, _err)


# ─────────────────────────────────────────────────────────────
# SELF-GUARD: same launcher/bootstrap-phase lock launcher.py uses
# ─────────────────────────────────────────────────────────────
# launcher.py already guards the normal .vbs -> launcher.py -> bootstrap.py
# flow with a PID lock file. These mirror that exact mechanism (same lock
# file, same stale-owner reclaim logic) so bootstrap.py is *also*
# self-guarding when it's launched some other way (double-clicked directly,
# run from a dev shortcut, etc.) instead of only ever being safe when
# invoked through launcher.py. Sharing the one lock file means either entry
# point blocks the other.
_BOOTSTRAP_LOCK_FILE = STARTUP_HEALTH_FILE.parent / "launcher.lock"


def _bootstrap_process_is_alive(pid: int) -> bool:
    """True if a process with this PID currently exists, hasn't exited, AND is a Python/launcher process."""
    if sys.platform != "win32":
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    try:
        import ctypes
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
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def _bootstrap_try_create_lock_exclusive() -> bool:
    """Atomically create the lock file only if it doesn't already exist."""
    try:
        fd = os.open(str(_BOOTSTRAP_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
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


def _claim_bootstrap_slot() -> bool:
    """Claim the launcher/bootstrap-phase lock. Returns False only when
    another launcher/bootstrap process is genuinely still alive and
    holding it."""
    try:
        _BOOTSTRAP_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return True

    if _bootstrap_try_create_lock_exclusive():
        return True

    # Lock file already exists — find out whether its owner is actually
    # still running, or whether this is a stale leftover from a crash/kill.
    try:
        existing_pid = int(_BOOTSTRAP_LOCK_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        existing_pid = None

    if existing_pid and existing_pid != os.getpid() and _bootstrap_process_is_alive(existing_pid):
        return False  # a real launcher/bootstrap is genuinely in progress

    # Stale lock — reclaim it.
    try:
        _BOOTSTRAP_LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return _bootstrap_try_create_lock_exclusive()


def _release_bootstrap_slot():
    """Best-effort cleanup so the lock file doesn't linger after a clean
    exit. Safe to skip on crash — the next launch's liveness check
    reclaims it automatically."""
    try:
        if _BOOTSTRAP_LOCK_FILE.exists():
            existing_pid = int(_BOOTSTRAP_LOCK_FILE.read_text(encoding="utf-8").strip())
            if existing_pid == os.getpid():
                _BOOTSTRAP_LOCK_FILE.unlink()
    except Exception:
        pass


def _notify_bootstrap_already_running():
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            "MCU Uploader IDE by Naph is already starting up in another window.\n\n"
            "Please wait for it to finish loading before launching it again.",
            "MCU Uploader IDE by Naph",
            0x40,  # MB_ICONINFORMATION
        )
    except Exception:
        pass


def _is_main_gui_running() -> bool:
    """Check (without claiming it) whether the Main GUI's single-instance
    mutex is currently held by a live process. mcu_flash_gui.py claims
    "Local\\MCUFlasherByNaph.MainGUI" via CreateMutexW as soon as it starts;
    opening (rather than creating) that same name here lets bootstrap tell
    the Main GUI is already up without racing it for ownership."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenMutexW(SYNCHRONIZE, False, "Local\\MCUFlasherByNaph.MainGUI")
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except Exception:
        return False


def _verify_storage_drive_type():
    """Ensure MCU Flasher is running from an internal fixed drive (SSD/HDD), not a USB flash drive or removable media."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        drive_root = SCRIPT_DIR.anchor  # e.g., "D:\\" or "C:\\"
        DRIVE_REMOVABLE = 2
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(drive_root))
        if drive_type == DRIVE_REMOVABLE:
            msg = (
                f"MCU Uploader IDE by Naph cannot be run directly from a USB flash drive or removable disk ({drive_root}).\n\n"
                f"Current Path: {SCRIPT_DIR}\n\n"
                "High-speed disk access (SSD/HDD) is required for toolchain compilation and workspace storage.\n\n"
                "Please copy the entire MCU Flasher folder to an internal SSD or HDD drive (e.g. C:\\ or D:\\ drive) "
                "and launch it from there."
            )
            try:
                ctypes.windll.user32.MessageBoxW(
                    0,
                    msg,
                    "MCU Uploader IDE by Naph — Storage Location Notice",
                    0x10,  # MB_ICONERROR
                )
            except Exception:
                pass
            sys.exit(1)
    except Exception:
        pass


def main():
    global _gui, _BOOTSTRAP_STARTUP_NOTE
    if sys.platform != "win32":
        raise SystemExit("MCU Flasher setup requires Windows 10 or newer.")
    _record_bootstrap_log(
        "START",
        "Bootstrap started "
        f"(pid={os.getpid()}, python={sys.executable}, project={SCRIPT_DIR}, args={sys.argv[1:]})",
    )
    _verify_storage_drive_type()

    # Existing main GUI owns the user-facing singleton.  Check this before any
    # recovery/update work so a second launcher invocation stays cheap.
    if _is_main_gui_running():
        _record_bootstrap_log("FINISH", "Existing main GUI detected; bootstrap did not run setup.")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "MCU Uploader IDE by Naph is already running.\n\n"
                "Switch to the existing window instead of starting a new one.",
                "MCU Uploader IDE by Naph",
                0x40,
            )
        except Exception:
            pass
        return

    # Bootstrap setup and verification is mandatory and must run on every launch.
    _record_bootstrap_log("START", "Executing mandatory Bootstrap verification pipeline.")

    # Clean up the narrow class of orphaned update probes produced by older
    # releases before checking the normal GUI instance. This is best-effort
    # and never touches unrelated Python processes.
    recovered_updates = _recover_stale_update_processes()
    if recovered_updates:
        _record_bootstrap_log(
            "RECOVERY",
            f"Stopped {recovered_updates} stale update probe process(es) from an older release.",
        )

    # ── Launch Bootstrap GUI Window ─────────────────────────────────
    # Bootstrap setup is mandatory and runs its complete verification/install
    # pipeline before launching the main GUI.
    import threading

    gui = BootstrapGUI()
    _gui = gui

    # Run setup on a background thread; Tk mainloop stays on main thread
    t = threading.Thread(target=_run_setup_in_thread, args=(gui,), daemon=True)
    t.start()

    gui.mainloop_until_done()
    sys.exit(0)


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("MCU Flasher setup requires Windows 10 or newer.")
    # Special one-shot elevated helper used by a fresh normal-user bootstrap.
    # It deliberately bypasses the normal launcher/bootstrap lock and GUI.
    if "--privileged-first-run" in sys.argv:
        _record_bootstrap_log("START", "Elevated first-run installer helper started.")
        helper_ok = _run_privileged_first_run_tasks()
        _record_bootstrap_log(
            "FINISH",
            "Elevated first-run installer helper completed successfully."
            if helper_ok
            else "Elevated first-run installer helper failed.",
        )
        raise SystemExit(0 if helper_ok else 1)

    # Self-guard against a second concurrent bootstrap when this script is
    # invoked directly rather than through launcher.py (e.g. a dev shortcut,
    # or manual testing). Shares launcher.py's own lock file, so whichever
    # of the two entry points got there first blocks the other one.
    if "--new-window" not in sys.argv:
        if not _claim_bootstrap_slot():
            _notify_bootstrap_already_running()
            sys.exit(0)
        import atexit
        atexit.register(_release_bootstrap_slot)

    # The VBS launcher deliberately hides its console.  Keep a startup
    # failure visible and diagnosable instead of letting pythonw.exe exit
    # silently before the BootstrapGUI can be created.
    import traceback

    crash_log = SCRIPT_DIR / "logs" / "bootstrap_crash.log"
    try:
        main()
    except Exception:
        error_text = traceback.format_exc()
        _record_bootstrap_log("ERROR", f"Bootstrap failed before setup could finish.\n{error_text.rstrip()}")
        try:
            crash_log.parent.mkdir(parents=True, exist_ok=True)
            crash_log.write_text(error_text, encoding="utf-8")
        except Exception:
            pass

        message = (
            "MCU Uploader IDE setup could not start.\n\n"
            f"{error_text[:1200]}\n\n"
            f"Full log: {crash_log}"
        )
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("MCU Uploader IDE by Naph - Setup Error", message, parent=root)
            root.destroy()
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0, message, "MCU Uploader IDE by Naph - Setup Error", 0x10
                )
            except Exception:
                pass
        raise SystemExit(1)
