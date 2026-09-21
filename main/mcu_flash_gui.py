#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.mcu_flash_gui — PySide6 GUI Entry Point for MCU Flasher by Naph.
Boots the native Qt desktop application powered by Python and PySide6.
"""
from __future__ import annotations

import sys
import os
import ctypes
import traceback
from pathlib import Path
from typing import Optional, Any

# ── Path hierarchy ────────────────────────────────────────────────────────────
_this_file = Path(__file__).resolve()
_project_root = _this_file.parent.parent if _this_file.parent.name == "main" else _this_file.parent
_modules_path = _project_root / "src" / "modules"
_main_path    = _project_root / "main"

for _p in (_project_root, _modules_path, _main_path):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Strict enforcement: NEVER run under desktop/system Python
from src.modules.private_python_guard import enforce_private_python
enforce_private_python()

# Hide background subprocess consoles on Windows
if sys.platform == "win32":
    try:
        from win_subprocess_hide import install as _install_subprocess_hide, install_venv_site_hook as _install_site_hook
        _install_subprocess_hide()
        _install_site_hook(_project_root)
    except Exception:
        pass

SCRIPT_DIR = _project_root
MINIMUM_LOGICAL_CORES = 4

from main.core.constants import is_application_codebase_dir
from main.core.config import (
    find_project_window, focus_project_window,
    set_instance_hwnd, set_active_sketch_dir,
    clean_instance_config,
)
from main.web_bridge import MCUWebBackendAPI

# ── Crash Detection & Session Tracking ────────────────────────────────────────
try:
    from src.modules.crash_detector import (
        mark_session_started,
        mark_session_clean_exit,
        record_crash_event,
    )
except ImportError:
    try:
        from crash_detector import (
            mark_session_started,
            mark_session_clean_exit,
            record_crash_event,
        )
    except ImportError:
        def mark_session_started(pid: int | None = None) -> None: pass
        def mark_session_clean_exit(pid: int | None = None) -> None: pass
        def record_crash_event(*args: Any, **kwargs: Any) -> None: pass

# ── PySide6 GUI Components ───────────────────────────────────────────────────
try:
    # pyrefly: ignore [missing-import]
    from PySide6.QtCore import QTimer
    # pyrefly: ignore [missing-import]
    from PySide6.QtGui import QIcon, QFont
    # pyrefly: ignore [missing-import]
    from PySide6.QtWidgets import QApplication, QDialog
    from main.qt.signals import signals as _global_signals
    from main.qt.main_window import MCUMainWindow
    from main.qt.project_dialog import ProjectDialog
    from main.qt.theme import register_fonts, build_stylesheet
    _PYSIDE6_AVAILABLE = True
except ImportError:
    QTimer = None  # type: ignore
    QIcon = None  # type: ignore
    QApplication = None  # type: ignore
    QDialog = None  # type: ignore
    _global_signals = None  # type: ignore
    MCUMainWindow = None  # type: ignore
    ProjectDialog = None  # type: ignore
    register_fonts = None  # type: ignore
    build_stylesheet = None  # type: ignore
    _PYSIDE6_AVAILABLE = False


def _enforce_minimum_cpu_requirement() -> bool:
    """Enforce minimum 4 logical cores requirement per project specification."""
    try:
        cores = os.cpu_count()
    except Exception:
        cores = None

    if cores is None or cores >= MINIMUM_LOGICAL_CORES:
        return True

    msg = (
        "MCU Flasher by Naph cannot run reliably on this computer.\n\n"
        f"Detected logical CPU cores/threads: {cores}\n"
        f"Minimum required: {MINIMUM_LOGICAL_CORES}\n\n"
        "The editor, serial monitor, toolchain, and background services "
        "require at least 4 logical CPU cores/threads."
    )
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(
                0, msg, "MCU Flasher by Naph — Unsupported Hardware", 0x10
            )
        except Exception:
            pass
    print(msg, file=sys.stderr)
    return False


def _configure_windows_environment() -> None:
    """Set high-DPI scaling, Per-Monitor V2 DPI awareness, and Windows taskbar application grouping."""
    if sys.platform != "win32":
        return
    try:
        # Qt 6 handles DPI scaling natively; set env vars before QApplication is created.
        os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
        os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    except Exception:
        pass
    try:
        # Declare Windows Per-Monitor V2 DPI awareness to prevent virtualization and ensure
        # synchronized pixel scaling across mixed-DPI monitors and child Win32 HWNDs.
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if not ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            try:
                # PROCESS_PER_MONITOR_DPI_AWARE = 2
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("naph.mcuflasher.gui.v3")
    except Exception:
        pass


def main() -> int:
    """Main application entry point."""
    if not _enforce_minimum_cpu_requirement():
        return 1

    _configure_windows_environment()

    # ── Multi-instance support (Arduino IDE pattern) ──────────────────────────
    # Multiple windows are allowed to run concurrently. Per-project isolation
    # ensures an active sketch can only be open in one window at a time.

    try:
        mark_session_started(os.getpid())

        # Global exception hook to catch uncaught background and UI exceptions
        _orig_excepthook = sys.excepthook
        def _crash_excepthook(exc_type, exc_val, exc_tb):
            try:
                tb_str = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
                record_crash_event(
                    crash_type="unhandled_python_exception",
                    details=tb_str,
                    exc_type=getattr(exc_type, "__name__", "Exception"),
                    pid=os.getpid(),
                )
            except Exception:
                pass
            _orig_excepthook(exc_type, exc_val, exc_tb)
        sys.excepthook = _crash_excepthook

        # ── PySide6 availability check ────────────────────────────────────────
        if not _PYSIDE6_AVAILABLE or QApplication is None:
            record_crash_event("missing_dependency", "PySide6 is not installed", exc_type="ImportError", pid=os.getpid())
            msg = (
                "MCU Flasher error: PySide6 is not installed.\n\n"
                "Please run bootstrap or runThisOnWindows.vbs to install all dependencies."
            )
            if sys.platform == "win32":
                try:
                    ctypes.windll.user32.MessageBoxW(0, msg, "MCU Flasher — Error", 0x10)
                except Exception:
                    pass
            print(msg, file=sys.stderr)
            return 1

        # ── Qt Application ────────────────────────────────────────────────────
        app = QApplication.instance() or QApplication(sys.argv)
        app.setApplicationName("MCU Flasher by Naph")
        app.setOrganizationName("Naph")
        app.setApplicationVersion("3.0")

        # ── Fonts & Global Stylesheet ─────────────────────────────────────────
        from main.core.config import get_theme_mode
        from main.core.theme import Theme
        active_theme = get_theme_mode()
        Theme.apply_theme(active_theme)
        register_fonts()
        if QFont:
            app_font = QFont("Montserrat", 10)
            app_font.setStyleHint(QFont.StyleHint.SansSerif)
            app.setFont(app_font)
        app.setStyleSheet(build_stylesheet(active_theme))

        # ── Application icon ──────────────────────────────────────────────────
        icon_path = SCRIPT_DIR / "src" / "assets" / "mcu_icon.ico"
        if icon_path.exists():
            app.setWindowIcon(QIcon(str(icon_path)))

        # ── Create backend ────────────────────────────────────────────────────
        api = MCUWebBackendAPI()

        # ── Handle project command-line argument or startup selector ──────────
        proj_arg: Optional[str] = None
        if "--project" in sys.argv:
            try:
                idx = sys.argv.index("--project")
                if idx + 1 < len(sys.argv):
                    candidate = Path(sys.argv[idx + 1]).resolve()
                    if not is_application_codebase_dir(candidate):
                        proj_arg = str(candidate)
            except Exception:
                pass
        if not proj_arg:
            for arg in sys.argv[1:]:
                if not arg.startswith("-"):
                    candidate = Path(arg).resolve()
                    if candidate.exists() and not is_application_codebase_dir(candidate):
                        proj_arg = str(candidate)
                        break

        # Pre-warm holder for main window
        window_holder: dict[str, Any] = {"window": None}

        if proj_arg:
            try:
                candidate = Path(proj_arg).resolve()
                if is_application_codebase_dir(candidate):
                    proj_arg = None
                else:
                    target_dir = candidate.parent if candidate.is_file() else candidate
                    # Arduino IDE behavior: if project is already active in another window, focus it and exit
                    owner = find_project_window(target_dir)
                    if owner and owner.get("pid") != os.getpid():
                        focus_project_window(owner.get("hwnd", 0), owner.get("pid", 0))
                        mark_session_clean_exit(os.getpid())
                        return 0

                    if candidate.is_file():
                        if not is_application_codebase_dir(candidate.parent):
                            api.open_project(str(candidate.parent), active_file=str(candidate))
                        else:
                            proj_arg = None
                    elif candidate.is_dir():
                        api.open_project(str(candidate))
            except Exception:
                proj_arg = None
        if not proj_arg:
            # Show Project Selector dialog before showing the main window
            dlg = ProjectDialog(backend=api)
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            if sys.platform == "win32":
                try:
                    hwnd = int(dlg.winId())
                    if hwnd:
                        set_instance_hwnd(hwnd)
                        ctypes.windll.user32.ShowWindow(hwnd, 1)  # SW_SHOWNORMAL
                        ctypes.windll.user32.SetForegroundWindow(hwnd)
                except Exception:
                    pass

            def _prewarm_main_window():
                if window_holder["window"] is None and not dlg.isHidden():
                    try:
                        win = MCUMainWindow(backend=api)
                        win.hide()
                        window_holder["window"] = win
                    except Exception:
                        pass

            # Pre-warm MCUMainWindow in background during idle time while user views dialog
            if QTimer is not None:
                QTimer.singleShot(120, _prewarm_main_window)

            if dlg.exec() != QDialog.DialogCode.Accepted or not api.sketch_dir_path:
                # User cancelled project selection -> clean exit
                if window_holder["window"] is not None:
                    try:
                        window_holder["window"].close()
                    except Exception:
                        pass
                mark_session_clean_exit(os.getpid())
                return 0

        # ── Main window ───────────────────────────────────────────────────────
        window = window_holder.get("window")
        if window is None:
            window = MCUMainWindow(backend=api)
        else:
            window._on_startup()

        window.show()
        window.raise_()
        window.activateWindow()
        if sys.platform == "win32":
            try:
                hwnd = int(window.winId())
                if hwnd:
                    set_instance_hwnd(hwnd)
                    api._hwnd = hwnd
                    if api.sketch_dir_path:
                        set_active_sketch_dir(str(api.sketch_dir_path), hwnd=hwnd)
                    ctypes.windll.user32.ShowWindow(hwnd, 1)  # SW_SHOWNORMAL
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass

        # ── Event loop ────────────────────────────────────────────────────────
        ret = app.exec()
        if ret == 0:
            mark_session_clean_exit(os.getpid())
        return ret

    except Exception as e:
        err_msg = traceback.format_exc()
        try:
            record_crash_event(
                crash_type="startup_exception",
                details=err_msg,
                exc_type=type(e).__name__,
                pid=os.getpid(),
            )
        except Exception:
            pass

        crash_log = SCRIPT_DIR / "logs" / "gui_crash.log"
        try:
            crash_log.parent.mkdir(parents=True, exist_ok=True)
            crash_log.write_text(err_msg, encoding="utf-8")
        except Exception:
            pass

        if sys.platform == "win32":
            try:
                ctypes.windll.user32.MessageBoxW(
                    0,
                    f"MCU Flasher crashed before it could start.\n\n{err_msg[:800]}\n\nLog: {crash_log}",
                    "MCU Flasher by Naph — Crash",
                    0x10,
                )
            except Exception:
                pass
        print(err_msg, file=sys.stderr)
        return 1
    finally:
        clean_instance_config()


if __name__ == "__main__":
    sys.exit(main())
