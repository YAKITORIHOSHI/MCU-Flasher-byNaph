#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — ESP32 Compile, Upload & Serial Monitor
A modern dark-themed GUI tool for Arduino ESP32 development.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

# Add project root and src/modules to sys.path so modules resolve anywhere
_this_file = Path(__file__).resolve()
_project_root = _this_file.parent.parent if _this_file.parent.name == "main" else _this_file.parent
_modules_path = _project_root / "src" / "modules"
_main_path = _project_root / "main"

for _p in (_project_root, _modules_path, _main_path):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

SCRIPT_DIR = _project_root

import subprocess
import threading
import ctypes
import traceback
import tkinter as tk
from tkinter import messagebox

# Import all core constants, theme, config, file utilities, toolchains, catalogs, and compatibilities
from main.core.constants import *
from main.core.theme import *
from main.core.config import *
from main.core.file_utils import *
from main.core.toolchain import *
from main.core.board_catalog import *
from main.core.board_compat import *

# Import UI widgets, dialogs, and Monaco editor bridge
from main.widgets import *
from main.dialogs import *
from main.editor_api import *

# Import all 27 mixin classes composing MCUUploadGUI
from main.mixins.init_startup_mixin import InitStartupMixin
from main.mixins.ui_layout_mixin import UILayoutMixin
from main.mixins.console_serial_mixin import ConsoleSerialMixin
from main.mixins.layout_panes_mixin import LayoutPanesMixin
from main.mixins.async_tasks_mixin import AsyncTasksMixin
from main.mixins.compat_devices_mixin import CompatDevicesMixin
from main.mixins.project_terminal_mixin import ProjectTerminalMixin
from main.mixins.hardware_port_mixin import HardwarePortMixin
from main.mixins.clean_build_mixin import CleanBuildMixin
from main.mixins.build_actions_mixin import BuildActionsMixin
from main.mixins.project_actions_mixin import ProjectActionsMixin
from main.mixins.compile_cache_mixin import CompileCacheMixin
from main.mixins.library_headers_mixin import LibraryHeadersMixin
from main.mixins.build_workspace_mixin import BuildWorkspaceMixin
from main.mixins.soft_reset_template_mixin import SoftResetTemplateMixin
from main.mixins.platformio_ini_mixin import PlatformioIniMixin
from main.mixins.compiler_pipeline_mixin import CompilerPipelineMixin
from main.mixins.upload_pipeline_mixin import UploadPipelineMixin
from main.mixins.monitor_pipeline_mixin import MonitorPipelineMixin
from main.mixins.editor_modes_mixin import EditorModesMixin
from main.mixins.boards_catalog_mixin import BoardsCatalogMixin
from main.mixins.ai_assistant_mixin import AIAssistantMixin
from main.mixins.settings_dialog_mixin import SettingsDialogMixin
from main.mixins.hard_reset_mixin import HardResetMixin
from main.mixins.soft_reset_mixin import SoftResetMixin
from main.mixins.window_lifecycle_mixin import WindowLifecycleMixin
from main.mixins.syntax_checker_mixin import SyntaxCheckerMixin


class MCUUploadGUI(
    InitStartupMixin,
    UILayoutMixin,
    ConsoleSerialMixin,
    LayoutPanesMixin,
    AsyncTasksMixin,
    CompatDevicesMixin,
    ProjectTerminalMixin,
    HardwarePortMixin,
    CleanBuildMixin,
    BuildActionsMixin,
    ProjectActionsMixin,
    CompileCacheMixin,
    LibraryHeadersMixin,
    BuildWorkspaceMixin,
    SoftResetTemplateMixin,
    PlatformioIniMixin,
    CompilerPipelineMixin,
    UploadPipelineMixin,
    MonitorPipelineMixin,
    EditorModesMixin,
    BoardsCatalogMixin,
    AIAssistantMixin,
    SettingsDialogMixin,
    HardResetMixin,
    SoftResetMixin,
    WindowLifecycleMixin,
    SyntaxCheckerMixin,
):
    """
    Main Tkinter GUI application for MCU Flasher by Naph.
    Composed of 27 categorized domain mixins.
    """
    pass


def main():
    import os
    if sys.platform != "win32":
        raise SystemExit("MCU Flasher by Naph requires Windows 10 or newer.")
    _startup_event("main-enter")
    _configure_windows_dpi_awareness()
    # Ensure Bootstrap is ALWAYS run first before the main GUI starts.
    # If not spawned from bootstrap, launch the bootstrap pipeline and exit this process.
    if "--from-bootstrap" not in sys.argv:
        import subprocess
        vbs_launcher = SCRIPT_DIR / "direct" / "runThisOnWindows.vbs"
        if not vbs_launcher.exists():
            vbs_launcher = SCRIPT_DIR / "runThisOnWindows.vbs"
        launched = False
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
                launched = True
                sys.exit(0)
            except Exception:
                launched = False

        if not launched:
            launcher_py = SCRIPT_DIR / "src" / "modules" / "launcher.py"
            bootstrap_py = SCRIPT_DIR / "src" / "modules" / "bootstrap.py"
            target_py = launcher_py if launcher_py.exists() else bootstrap_py
            if target_py.exists():
                try:
                    subprocess.Popen([sys.executable, str(target_py), *sys.argv[1:]], cwd=str(SCRIPT_DIR))
                    sys.exit(0)
                except Exception:
                    pass

    # Keep the app's own cache/runtime/log folders out of the project view.
    # This runs only after bootstrap handoff; user source files and unknown
    # project entries are not touched by the metadata allowlist.
    if "--from-bootstrap" in sys.argv:
        try:
            hide_internal_project_metadata(SCRIPT_DIR)
        except Exception:
            pass

    # A VBS re-run/relaunch must never create a second main GUI accidentally.
    # Additional windows are an explicit opt-in (--new-window), which keeps
    # their per-PID editor/config state isolated from the normal task.
    if not _claim_gui_instance():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                "MCU Flasher is already running. The existing main window will remain active.\n\n"
                "To intentionally run an independent task, launch with --new-window.",
                "MCU Flasher by Naph",
                0x40,
            )
        except Exception:
            pass
        return

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
                        cached_file = script_dir / "src" / "dbs" / "arduino_cli_path.txt"
                        cached_file.parent.mkdir(parents=True, exist_ok=True)
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
                        # Save to arduino_cli_path.txt
                        script_dir = SCRIPT_DIR
                        cached_file = script_dir / "src" / "dbs" / "arduino_cli_path.txt"
                        cached_file.parent.mkdir(parents=True, exist_ok=True)
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

    # Do not import pywebview here. On some machines that native import takes
    # several seconds, and doing it before the Tk thread starts leaves users
    # staring at a frozen/blank handoff. The Monaco placeholder can render
    # immediately; WebView is loaded after the main window is responsive.

    root_ready = threading.Event()
    project_cancelled.clear()
    root_val = None
    app_val = None
    startup_state = {"stage": "creating the Tk root window"}
    startup_failure = {}

    def _run_tk_inner():
        nonlocal root_val, app_val
        root_val = tk.Tk()
        _startup_event("tk-root-created")
        root_val.withdraw()
        root_val.configure(bg="#151922")

        # Set window icon if available
        icon_path = SCRIPT_DIR / "src" / "assets" / "mcu_icon.ico"
        if not icon_path.exists():
            icon_path = SCRIPT_DIR / "src" / "mcu_icon.ico"
        if icon_path.exists():
            try:
                root_val.iconbitmap(default=str(icon_path))
                root_val.iconbitmap(str(icon_path))
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

        work_left, work_top, work_right, work_bottom = _get_monitor_work_area(root_val)
        screen_w = work_right - work_left
        screen_h = work_bottom - work_top
        dpi_scale = _get_widget_dpi_scale(root_val)
        try:
            root_val.tk.call("tk", "scaling", (96.0 * dpi_scale) / 72.0)
        except Exception:
            pass
        logical_screen_w = screen_w / dpi_scale
        logical_screen_h = screen_h / dpi_scale

        # Start maximized or fallback to a suitable default based on display size, centered on the screen
        if logical_screen_w < 1400 or logical_screen_h < 800:
            target_w, target_h = 1000, 600
        else:
            target_w, target_h = 1350, 720
        w = max(320, min(round(target_w * dpi_scale), screen_w - round(48 * dpi_scale)))
        h = max(260, min(round(target_h * dpi_scale), screen_h - round(88 * dpi_scale)))
        x = work_left + max(0, (screen_w - w) // 2)
        y = work_top + max(0, (screen_h - h) // 2)
        x_part = f"+{x}" if x >= 0 else str(x)
        y_part = f"+{y}" if y >= 0 else str(y)
        root_val.geometry(f"{w}x{h}{x_part}{y_part}")

        startup_state["stage"] = "opening the selected project and building the main interface"
        app_val = MCUUploadGUI(root_val)
        _startup_event("gui-constructed")

        # If the user cancelled the Project Selector, __init__ destroyed
        # root and returned early.  Always signal root_ready FIRST so the
        # main thread's root_ready.wait() unblocks cleanly — without this
        # the main thread can hang forever waiting on a set() that never
        # comes, causing the process to appear to error/freeze on cancel.
        cancelled = project_cancelled.is_set()
        if not cancelled:
            try:
                cancelled = not root_val.winfo_exists() or not getattr(app_val, "sketch_dir_path", None)
            except tk.TclError:
                cancelled = True

        if cancelled:
            set_monaco_boot_pending(False)
            project_cancelled.set()
            root_ready.set()   # unblock main-thread wait() before exiting
            return

        # Ensure active project state is synced and notifications loaded
        try:
            if hasattr(app_val, "_sync_project_hardware_state"):
                app_val._sync_project_hardware_state()
            if hasattr(app_val, "_load_persistent_notifications"):
                app_val._load_persistent_notifications()
        except Exception:
            pass

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

        # Startup deliberately does NOT auto-maximize the window anymore --
        # it opens at the geometry set above (sized for the display) and
        # stays there until the user maximizes it themselves. One less
        # window-state transition happening automatically on launch.

        # Intercept Tkinter window closure to exit the entire app
        def on_tk_close():
            try:
                set_monaco_boot_pending(False)
            except Exception:
                pass
            if app_val:
                app_val._on_close()
            else:
                root_val.destroy()
                os._exit(0)
        root_val.protocol("WM_DELETE_WINDOW", on_tk_close)

        startup_state["stage"] = "starting the main Tk event loop"
        root_ready.set()
        _startup_event("shell-input-loop-started")
        root_val.mainloop()

    def run_tk():
        """Run Tk on its dedicated thread and propagate startup failures.

        Exceptions raised while MCUUploadGUI is being constructed used to die
        only on this worker thread.  The main thread remained blocked in
        root_ready.wait(), so the process appeared as a blank window that
        vanished with no crash dialog.  Capture the traceback here, always
        release the wait, and let the main-thread crash guard report it.
        """
        nonlocal root_val, app_val
        try:
            _run_tk_inner()
        except BaseException:
            import traceback

            error_text = traceback.format_exc()
            startup_failure["stage"] = startup_state.get("stage", "starting the main GUI")
            startup_failure["traceback"] = error_text
            try:
                logs_dir = SCRIPT_DIR / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                (logs_dir / "gui_crash.log").write_text(error_text, encoding="utf-8")
            except Exception:
                pass
            try:
                if root_val is not None and root_val.winfo_exists():
                    root_val.destroy()
            except Exception:
                pass
            # Drop the Tcl/Tk objects on the same thread that created them.
            # Letting their final references survive until main-thread process
            # teardown can trigger Tcl_AsyncDelete / wrong-thread cleanup.
            app_val = None
            root_val = None
            root_ready.set()

    tk_thread = threading.Thread(target=run_tk, daemon=True)
    tk_thread.start()

    # Wait for Tkinter to initialize
    root_ready.wait()

    if startup_failure:
        tk_thread.join(timeout=2.0)
        stage = startup_failure.get("stage", "starting the main GUI")
        original_traceback = startup_failure.get("traceback", "Unknown startup failure")
        raise RuntimeError(
            f"The main GUI failed while {stage}.\n\n{original_traceback}"
        )

    # Project selection was cancelled cleanly.  Avoid os._exit(), which skips
    # normal cleanup and can discard buffered diagnostics.
    if project_cancelled.is_set():
        set_monaco_boot_pending(False)
        tk_thread.join(timeout=1.0)
        return

    if requested_mode != "monaco":
        # Default (Tkinter) editor — no separate webview process needed at
        # all. Just block the main thread until the Tk mainloop (running on
        # tk_thread) exits.
        tk_thread.join()
        return

    # ── Monaco mode ─────────────────────────────────────────────────────
    # Import the heavyweight native WebView runtime only after the Tk window
    # and its loading placeholder are visible. Tk runs on its dedicated thread,
    # so this import can no longer freeze the visual transition.
    webview = _load_webview()
    if webview is None:
        requested_mode = "default"
        _RESOLVED_EDITOR_MODE = requested_mode
        # Keep the user's Monaco preference.  The lightweight editor is only
        # a runtime fallback for this launch; silently changing the saved
        # preference made switching editors appear broken on machines that
        # were missing pywebview or WebView2.
        set_monaco_boot_pending(False)

        def _fallback_to_default_editor():
            try:
                app_val._cleanup_active_editor()
                app_val.editor_mode = "default"
                app_val._build_editor_default(app_val.editor_frame)
                app_val._update_editor_info()
                app_val._append(
                    "  ⚠ Monaco is selected, but WebView2/pywebview is unavailable; using the lightweight Default editor for this launch.",
                    "warning",
                )
                if _WEBVIEW_IMPORT_ERROR:
                    app_val._append(f"    Reason: {_WEBVIEW_IMPORT_ERROR}", "warning")
            except Exception as exc:
                try:
                    app_val._set_status(f"Default editor recovery failed: {exc}", Theme.RED)
                except Exception:
                    pass

        try:
            app_val._post_ui(_fallback_to_default_editor)
        except Exception:
            pass
        tk_thread.join()
        return

    # Mark that we're about to attempt Monaco startup. If the process dies
    # anywhere between here and the confirmation callback below, this flag
    # stays set on disk and the *next* launch will detect it and revert to
    # Default automatically instead of crash-looping.
    set_monaco_boot_pending(True)

    # Keep normal Windows scheduling and WebView2's default process policy.
    # Elevating the whole process to HIGH priority and forcing four browser
    # renderers can starve Tk or overwhelm lower-end PCs during the editor's
    # first V8/WebView2 startup — the opposite of a smooth transition.

    def _confirm_monaco_booted():
        set_monaco_boot_pending(False)

    # Tk owns root_val; queue the timer creation onto the Tk thread instead
    # of calling root.after from the WebView/main thread.
    app_val._post_ui(lambda: root_val.after(3500, _confirm_monaco_booted))

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
        min_size=(360, 240),
        hidden=True,
        background_color="#151922",   # matches Theme.BG_DARKEST — no white flash
    )
    app_val.editor_window = editor_window
    
    def _on_editor_page_loaded():
        # Fires on pywebview's own GUI thread — marshal back to the Tk thread.
        try:
            set_monaco_boot_pending(False)
        except Exception:
            pass

        def _page_loaded_on_tk():
            setattr(app_val, "_editor_content_loaded", True)
            app_val._reveal_editor_if_ready()
            app_val._update_editor_info()
            try:
                active_t = get_theme_mode()
                editor_window.evaluate_js(f"if (typeof window.setEditorTheme === 'function') window.setEditorTheme('{active_t}');")
            except Exception:
                pass
            # Defer background syntax parsing by 2 seconds to avoid CPU
            # contention while Monaco paints.
            root_val.after(2000, app_val._start_background_syntax_thread)
            # Sync symbol navigation compiled state after page load.
            root_val.after(1000, lambda: app_val._set_symbol_cache_compiled_state(
                getattr(app_val, "_project_compiled_cache_active", False)
            ))

        app_val._post_ui(_page_loaded_on_tk)
    editor_window.events.loaded += _on_editor_page_loaded

    # Kick off the embed attempt now.
    app_val._post_ui(lambda: root_val.after(50, app_val._try_embed_editor_window))

    def on_closing():
        # This callback is raised by the WebView thread. Queue all Tk reads,
        # timer operations, and reattachment work onto Tk's owning thread.
        def _reattach_on_tk():
            if getattr(app_val, "_editor_embedded", False):
                return
            poll_id = getattr(app_val, "_poll_detached_after_id", None)
            if poll_id is not None:
                try:
                    root_val.after_cancel(poll_id)
                except Exception:
                    pass
                app_val._poll_detached_after_id = None
            app_val._attach_editor()

        app_val._post_ui(_reattach_on_tk)
        return False  # Intercept close and just hide/re-parent

    editor_window.events.closing += on_closing
    webview.start(debug=False)
    # If webview.start() returns normally (window closed cleanly), make sure
    # the sentinel is cleared so a later launch doesn't misread this as a crash.
    set_monaco_boot_pending(False)
    _confirm_monaco_booted()

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

    def _crash_preview(error_text: str, limit: int = 1600) -> str:
        """Return the useful tail of a traceback for the startup dialog.

        Nested startup failures can exceed the dialog limit.  Showing the
        beginning hid the final exception (the actual cause) below the cutoff.
        The end of a Python traceback contains the deepest frame and exception,
        so prefer that while the complete text remains in gui_crash.log.
        """
        clean = str(error_text or "").strip()
        if len(clean) <= limit:
            return clean
        tail = clean[-limit:]
        first_newline = tail.find("\n")
        if first_newline >= 0:
            tail = tail[first_newline + 1:]
        return "… earlier traceback omitted; showing the root-cause end …\n" + tail

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
                f"{_crash_preview(_err)}\n\n"
                f"Full log: {_crash_log}"
            )
            _r.destroy()
        except Exception:
            # Tk itself failed — fall back to a Win32 MessageBox
            try:
                import ctypes as _ct
                _ct.windll.user32.MessageBoxW(
                    0,
                    f"GUI crashed:\n\n{_crash_preview(_err, 1000)}\n\nLog: {_crash_log}",
                    "MCU Flasher by Naph — Crash",
                    0x10,   # MB_ICONERROR
                )
            except Exception:
                pass
        raise SystemExit(1)

