#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import os
import shutil
import subprocess
import threading
import ctypes
from pathlib import Path


from main.core.constants import *
from main.core.theme import *
from main.core.config import *
from main.core.file_utils import *

_bootstrap_module = None
_dedicated_ai_module = None
_PIO_EXECUTABLE_CACHE: list[str] | None = None

def _get_bootstrap():
    """Import (once) and cache the bootstrap module, or return None on failure."""
    global _bootstrap_module
    if _bootstrap_module is None:
        try:
            # pyrefly: ignore [missing-import]
            import bootstrap
            _bootstrap_module = bootstrap
        except ImportError:
            _bootstrap_module = False
    return _bootstrap_module if _bootstrap_module else None


def ensure_platformio_penv_with_hook(*args, **kwargs):
    b = _get_bootstrap()
    if b is None:
        return False
    return b.ensure_platformio_penv_with_hook(*args, **kwargs)


def _bootstrap_find_arduino_cli():
    b = _get_bootstrap()
    if b is None:
        return None
    return b.find_arduino_cli()


def _bootstrap_ensure_arduino_cli():
    b = _get_bootstrap()
    if b is None:
        return None
    return b.ensure_arduino_cli()


def _bootstrap_get_last_arduino_cli_error():
    b = _get_bootstrap()
    if b is None:
        return None
    return b.get_last_arduino_cli_error()


def _platform_already_installed(pio_core_dir, platform):
    b = _get_bootstrap()
    if b is None:
        return False
    return b._platform_already_installed(pio_core_dir, platform)

def _load_dedicated_ai():
    """Load the optional AI integration only when the user needs it."""
    global _dedicated_ai_module
    if _dedicated_ai_module is None:
        try:
            # pyrefly: ignore [missing-import]
            import dedicated_AI
            _dedicated_ai_module = dedicated_AI
        except Exception:
            _dedicated_ai_module = False
    return _dedicated_ai_module if _dedicated_ai_module else None


def is_opencode_installed() -> bool:
    module = _load_dedicated_ai()
    if module is None:
        return False
    try:
        return bool(module.is_opencode_installed())
    except Exception:
        return False


# pywebview is optional and can pull in a sizeable native backend. Keep it out
# of the default Tkinter startup path; Monaco loads it explicitly in main().
webview = None
_WEBVIEW_IMPORT_ERROR = ""


def _load_webview():
    global webview, _WEBVIEW_IMPORT_ERROR
    if webview is None:
        try:
            # pyrefly: ignore [missing-import]
            import webview as _webview
            webview = _webview
        except Exception as exc:
            webview = False
            _WEBVIEW_IMPORT_ERROR = str(exc).strip() or exc.__class__.__name__
    return webview if webview else None

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
                # A broken junction can still resolve textually to the target
                # even though the target is unavailable.  It is not usable by
                # PlatformIO unless the link itself currently exists as a
                # directory, so never accept a dead reparse point here.
                if link_path.is_dir() and link_path.resolve() == real_dir_resolved:
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
            if not link_path.is_dir() or link_path.resolve() != real_dir_resolved:
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
    """Return this main project's PlatformIO core path.

    The physical package store belongs only to this application copy at
    ``<PROJECT_FOLDER>/src/.platformio-mcu-gui``. On Windows, PlatformIO is
    pointed at a short junction to that same directory so ESP32 GCC does not
    exceed the CreateProcess command-line limit.

    An inherited ``PLATFORMIO_CORE_DIR`` is accepted only when it resolves to
    this exact project-local store. Any inherited path resolving somewhere else
    is ignored; it is never adopted, migrated, or treated as related state.
    """
    target_dir = script_dir / "src" / ".platformio-mcu-gui"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target_resolved = target_dir.resolve()

        inherited = os.environ.get("PLATFORMIO_CORE_DIR", "").strip()
        if inherited:
            try:
                inherited_path = Path(os.path.expandvars(os.path.expanduser(inherited)))
                inherited_resolved = inherited_path.resolve()
                if os.path.normcase(str(inherited_resolved)) == os.path.normcase(str(target_resolved)):
                    return _short_platformio_core_alias(target_dir)
            except Exception:
                pass

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
    PLATFORMIO_CORE_DIR environment variable, so headers/toolchains silently
    resolve from the old location no matter what we set above.

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
                    # Comment it out rather than delete -- keeps the file
                    # human-diffable and reversible if a person edits it by hand.
                    out_lines.append(f"{m.group(1)}; (disabled by MCU Flasher — pointed elsewhere) core_dir{m.group(2)}{m.group(3)}")
                    changed = True
                    continue
            out_lines.append(line)

        if changed:
            global_ini.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    except Exception:
        # Never let this best-effort cleanup block app startup.
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
    try:
        unhide_hidden_attribute(Path(script_dir))
        unhide_hidden_attribute(Path(script_dir) / "src")
        unhide_hidden_attribute(Path(script_dir) / "src" / "modules")
    except Exception:
        pass
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


os.environ["PYTHONUNBUFFERED"] = "1"
os.environ["PLATFORMIO_UNBUFFERED"] = "1"

_PLATFORMIO_ENV_CONFIGURED = False
_PLATFORMIO_ENV_CONFIG_LOCK = threading.RLock()


def _ensure_platformio_environment_for_build(script_dir: Path = SCRIPT_DIR) -> None:
    """Configure PlatformIO only when a build-related operation needs it.

    Importing the GUI must remain side-effect free on the normal launch path.
    Build/upload callers reach this through
    ``_refresh_platformio_core_environment``; the setup worker performs the
    equivalent bootstrap-side preparation before installing toolchains.
    """
    global _PLATFORMIO_ENV_CONFIGURED
    if _PLATFORMIO_ENV_CONFIGURED:
        return
    with _PLATFORMIO_ENV_CONFIG_LOCK:
        if _PLATFORMIO_ENV_CONFIGURED:
            return
        _configure_platformio_environment(script_dir)
        _neutralize_conflicting_global_platformio_config()
        _PLATFORMIO_ENV_CONFIGURED = True


def _refresh_platformio_core_environment(script_dir: Path = SCRIPT_DIR) -> tuple[Path, bool]:
    """Verify the app-owned PlatformIO core alias before each build.

    The junction can become stale after a copied project is moved, a cleanup
    tool removes its target, or security software temporarily interrupts its
    creation.  PlatformIO then reports missing frameworks/toolchains even
    though the real store is present.  Re-resolve the alias, recreate the
    known app-owned target when needed, and use the direct target as a safe
    fallback if Windows refuses the junction operation.
    """
    _ensure_platformio_environment_for_build(script_dir)
    target_dir = Path(script_dir) / "src" / ".platformio-mcu-gui"
    configured_raw = os.environ.get("PLATFORMIO_CORE_DIR", "").strip()
    configured_valid = False
    target_resolved = None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target_resolved = target_dir.resolve()
        configured_path = Path(configured_raw) if configured_raw else None
        configured_valid = bool(
            configured_path
            and configured_path.is_dir()
            and configured_path.resolve() == target_resolved
        )
    except Exception:
        configured_path = None

    refreshed_raw = _get_safe_platformio_core_dir(Path(script_dir))
    refreshed_path = Path(refreshed_raw)
    refreshed_valid = False
    try:
        refreshed_valid = bool(
            refreshed_path.is_dir()
            and target_resolved is not None
            and refreshed_path.resolve() == target_resolved
        )
    except Exception:
        pass

    # A direct local path is preferable to passing a known-broken junction to
    # the compiler.  It remains on the same project drive and still contains
    # the exact app-owned PlatformIO store.
    effective_path = refreshed_path if refreshed_valid else target_dir
    try:
        effective_path.mkdir(parents=True, exist_ok=True)
        for child in ("packages", ".tmp", ".cache", "lib"):
            (effective_path / child).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    os.environ["PLATFORMIO_CORE_DIR"] = str(effective_path)
    return effective_path, (not configured_valid or not refreshed_valid)

# Serialize every read/modify/write cycle that touches a generated platformio.ini.
# Board changes, compile preparation, and the upload-speed callback can otherwise
# overlap on fast clicks and make Windows report a transient sharing violation.
_PLATFORMIO_INI_WRITE_LOCK = threading.RLock()

def _available_memory_gb() -> float | None:
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 ** 3)
    except Exception:
        return None


def _system_reserved_cpu_count(total_cpus: int) -> int:
    """Reserve more UI/system headroom on midrange and faster processors."""
    total_cpus = max(1, int(total_cpus or 1))
    if total_cpus >= 8:
        return 2
    if total_cpus > 1:
        return 1
    return 0


def _resource_safe_worker_count(mode: str = "HIGH", total_cpus: int | None = None,
                                available_gb: float | None = None) -> int:
    """Return a build/background concurrency level that will not swamp small PCs.

    Compiler processes are memory-heavy, so CPU count alone is not a safe
    multiplier. Reserve one logical CPU on low-end/midrange systems or two on systems
    with 8+ logical CPUs for Tk/WebView/serial handling, then cap workers by
    currently available RAM (~450 MB per compiler job).
    """
    cpus = max(1, int(total_cpus or os.cpu_count() or 2))
    memory_gb = _available_memory_gb() if available_gb is None else available_gb
    cpu_budget = max(1, cpus - _system_reserved_cpu_count(cpus))
    if memory_gb is not None:
        if memory_gb < 0.5:
            memory_budget = 1
        else:
            memory_budget = max(1, int((memory_gb - 0.25) / 0.45))
        cpu_budget = min(cpu_budget, memory_budget)

    normalized = str(mode or "HIGH").upper()
    if normalized == "LOW":
        return max(1, min(max(1, cpus // 2), cpu_budget))
    if normalized == "MEDIUM":
        return max(1, min(cpu_budget, max(2, (cpus + 1) // 2)))
    if normalized in ("ULTRA", "MAX", "MAXIMUM"):
        return max(1, min(cpu_budget, cpus, 16))
    return max(1, min(cpu_budget, cpus, 12))

_max_cpu_jobs = str(_resource_safe_worker_count("HIGH"))

os.environ["PLATFORMIO_BUILD_JOBS"] = _max_cpu_jobs
os.environ["PLATFORMIO_SETTING_ENABLE_CACHE"] = "true"
os.environ["SCONSFLAGS"] = f"-j{_max_cpu_jobs}"

# PlatformIO bootstraps its OWN private virtualenv ("penv") under
# PLATFORMIO_CORE_DIR the first time it runs. That's a completely separate

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
    cached_file = script_dir / "src" / "dbs" / "arduino_cli_path.txt"
    if not cached_file.exists() and (script_dir / "arduino_cli_path.txt").exists():
        cached_file = script_dir / "arduino_cli_path.txt"
    if cached_file.exists():
        try:
            path_str = cached_file.read_text(encoding="utf-8").strip()
            if path_str and os.path.exists(path_str):
                return path_str
        except Exception:
            pass
        try:
            cached_file.unlink(missing_ok=True)
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


__all__ = [
    "_MCUFLASHER_APP_DIRNAME",
    "_MODULES_ALIAS_NAME",
    "_PLATFORMIO_ALIAS_NAME",
    "_PLATFORMIO_ENV_CONFIGURED",
    "_PLATFORMIO_ENV_CONFIG_LOCK",
    "_PLATFORMIO_INI_WRITE_LOCK",
    "_WEBVIEW_IMPORT_ERROR",
    "_available_memory_gb",
    "_bootstrap_ensure_arduino_cli",
    "_bootstrap_find_arduino_cli",
    "_bootstrap_get_last_arduino_cli_error",
    "_bootstrap_module",
    "_cleanup_legacy_app_aliases",
    "_configure_platformio_environment",
    "_dedicated_ai_module",
    "_ensure_junction",
    "_ensure_modules_junction",
    "_ensure_platformio_environment_for_build",
    "_get_bootstrap",
    "_get_safe_platformio_core_dir",
    "_is_windows_reparse_point",
    "_load_dedicated_ai",
    "_load_webview",
    "_max_cpu_jobs",
    "_mcuflasher_app_root",
    "_neutralize_conflicting_global_platformio_config",
    "_platform_already_installed",
    "_prepare_mcuflasher_app_root",
    "_refresh_platformio_core_environment",
    "_remove_legacy_app_junction",
    "_resource_safe_worker_count",
    "_short_platformio_core_alias",
    "_system_reserved_cpu_count",
    "ensure_platformio",
    "ensure_platformio_penv_with_hook",
    "find_arduino_cli_executable",
    "find_pio_executable",
    "is_opencode_installed",
    "webview"
]
