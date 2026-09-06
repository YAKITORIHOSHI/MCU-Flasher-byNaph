#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import time
import subprocess
import threading
import queue
from collections import deque
from typing import TYPE_CHECKING
from pathlib import Path
import tkinter as tk
from tkinter import font as tkfont


from main.core.constants import *
from main.core.theme import *
from main.core.config import *
from main.core.file_utils import *
from main.core.toolchain import *
from main.core.board_catalog import *
from main.core.board_compat import *
from main.widgets import *
from main.dialogs import *
from main.editor_api import *

if TYPE_CHECKING:
    from main.mcu_flash_gui import MCUUploadGUI
    _Base = MCUUploadGUI
else:
    _Base = object

class InitStartupMixin(_Base):
    """Mixin providing InitStartupMixin capabilities for MCUUploadGUI."""
    def __init__(self, root: tk.Tk):
        self.root = root
        # Track visual UI readiness separately from editor/project/background
        # readiness. The visible shell is painted behind the cover while all
        # independent startup work runs, but the cover is not released until
        # both the layout and the application startup contract have settled.
        self._startup_ready = False
        self._startup_ready_reason = ""
        self._startup_ui_ready = False
        self._startup_overlay_released = False
        self._startup_layout_check_job = None
        self._startup_layout_signature = None
        self._startup_layout_stable_passes = 0
        self._startup_editor_ready = False
        self._startup_project_load_started = False
        self._startup_project_load_complete = False
        self._startup_overlay_dismiss_job = None
        self._startup_readiness_poll_job = None
        self._startup_overlay_raise_job = None
        self._startup_overlay_safety_job = None
        self._startup_min_visible_until = 0.0
        self._default_editor_ready = False
        self._editor_fallback_ready = False
        self.editor_mode = globals().get("_RESOLVED_EDITOR_MODE") or get_editor_mode()
        self.editor_detached = False
        self._is_attaching_editor = False       # re-entrancy guard for _attach_editor
        self._poll_detached_after_id = None     # tracks the _poll_detached_window timer
        self.autosave_enabled, self.autosave_delay_ms = get_autosave_settings()
        self.periodic_reload_enabled, self.periodic_reload_interval_s = get_periodic_reload_settings()
        self._periodic_reload_after_id = None
        self._monaco_autosave_worker = MonacoAutosaveWorker(self)
        self._monaco_autosave_worker.update_state()
        self._restart_periodic_reload()
        # The optional AI module imports pywebview/websockets and can be slow on
        # low-end machines. The button is created with the main UI, while the
        # controller itself is loaded only when the user opens AI Assistant.
        self.ai_controller = None
        self.root.title("MCU Flasher by Naph — ESP32 Upload & Monitor")
        self.screen_w, self.screen_h = self._get_current_monitor_dimensions()
        self._display_scale = _get_widget_dpi_scale(self.root)

        # Use half of the active monitor as the normal resize floor. The
        # compact responsive mode keeps both toolbars on one row at that
        # width; portrait displays are clamped safely inside their work area.
        self._min_window_width = self._minimum_width_for_display(
            self.screen_w, self.screen_h, self._display_scale
        )
        logical_screen_h = self.screen_h / self._display_scale
        logical_min_h = min(460, max(400, logical_screen_h - 120))
        safe_margin_h = min(self.screen_h // 4, round(48 * self._display_scale))
        self._min_window_height = min(
            round(logical_min_h * self._display_scale),
            max(260, self.screen_h - safe_margin_h),
        )
        self.root.minsize(self._min_window_width, self._min_window_height)

        if (self.screen_w / self._display_scale < 1400
                or self.screen_h / self._display_scale < 800):
            self._editor_height = 200
            self._editor_minsize = 140
            self._bottom_height = 220
            self._bottom_minsize = 190
        else:
            self._editor_height = 400
            self._editor_minsize = 180
            self._bottom_height = 250
            self._bottom_minsize = 210
        self.root.configure(bg=Theme.BG_DARKEST)

        # ── State ──
        self.serial_conn: serial.Serial | None = None
        self.serial_thread: threading.Thread | None = None
        self.serial_running = False
        self._monitor_should_run = False   # intent flag: True = keep reconnecting
        self._monitor_paused = False       # True = keep reading the port, but hold back display
        # Serial concurrency is generation-based.  Each connection owns its own
        # cancellation Event and local pyserial handle, so a stale reconnect
        # worker can never read from/close a newer connection through self.serial_conn.
        self._serial_state_lock = threading.RLock()
        self._serial_generation = 0
        self._serial_stop_event = threading.Event()
        # Outbound terminal writes are consumed by the serial I/O worker instead
        # of blocking Tk's event loop in Serial.write().
        self._serial_tx_queue: queue.Queue = queue.Queue(maxsize=256)
        self._tk_thread_id = threading.get_ident()
        self.process: subprocess.Popen | None = None
        self._download_managers: list[subprocess.Popen] = []
        self.is_busy = False
        # Compile owns the CPU; cooperative background workers check this
        # event before performing scans, probes, or syntax work.
        self._compile_background_lock = threading.Event()
        self._board_port_confirmed = False  # True only once a known chip signature confirms what's on the port
        # Board recognition is intentionally one-shot for the currently
        # selected port.  Once the user chooses a board, background detection
        # and Compile/Upload must never replace that choice.
        self._auto_detected_port = ""
        self._selected_port_device = ""
        self._board_selection_manually_overridden = False
        self.sketch_dir_path = DEFAULT_SKETCH_DIR
        self._last_known_ports: set = set()  # for USB hotplug detection
        self._auto_start_after_id = None
        self._last_conn_attempt = {"port": "", "baud": 0, "board": "", "time": 0.0}
        # High-baud serial devices can produce thousands of lines per second.
        # The serial reader must never call Tk directly: it only pushes rows into
        # this bounded deque.  A main-thread display pump drains it in small
        # batches, and suspends expensive Text rendering while the Serial Monitor
        # tab is not visible.  This prevents 921600-baud traffic from starving tab
        # changes and other Tk events.
        self._serial_display_queue: deque[tuple[str, str, bool]] = deque(maxlen=12000)
        self._serial_display_lock = threading.Lock()
        self._serial_display_flush_scheduled = False  # legacy state; pump owns scheduling
        self._serial_display_dropped_rows = 0
        self._serial_tab_visible = False
        self._serial_display_pump_after_id = None
        self._serial_last_trim_monotonic = 0.0
        self._serial_last_autoscroll_monotonic = 0.0
        # The monitor worker adapts this coalescing window to the selected baud.
        self._serial_display_flush_delay_ms = 25
        # Local shell tabs use one dedicated PTY thread per shell.  They do not
        # consume the compile/upload executor: a shell can remain open for the
        # entire life of the GUI without delaying a build or upload callback.
        self._shell_state_lock = threading.RLock()
        self._shell_sessions: dict[str, dict] = {}
        self._shell_active_kind = "pwsh"
        # Keep the same launch directory a native console would show before
        # the selected project is opened.  The project command is sent only
        # after that shell has printed its own banner and first prompt.
        try:
            self._shell_initial_cwd = Path.cwd()
        except Exception:
            self._shell_initial_cwd = Path.home()
        self._shell_terminal_tab_id = None
        self._shell_terminal_pump_after_id = None
        self._shell_prewarm_after_id = None
        self._shells_prewarmed = False
        # The visible Project Terminal is hosted by the same isolated
        # pywebview + xterm.js + pywinpty child process used by OpenCode.  Keep
        # the older Tk PTY state available as a runtime fallback for machines
        # where WebView2 or native window embedding is unavailable.
        self._project_terminal_proc = None
        self._project_terminal_port = None
        self._project_terminal_port_file = None
        self._project_terminal_hwnd = None
        self._project_terminal_embedded = False
        self._project_terminal_launching = False
        self._project_terminal_fallback = False
        self._project_terminal_fallback_revealed = False
        self._project_terminal_embed_attempts = 0
        self._project_terminal_status_job = None
        self._project_terminal_pending_action = None
        self._project_terminal_page_ready = False
        self._project_terminal_visibility_after_id = None
        self._project_terminal_visibility_pending = None
        # Cross-thread UI callbacks are queued here and executed only by Tk's
        # main thread.  Background workers must use _post_ui() instead of
        # calling root.after()/widget methods themselves.
        self._ui_dispatch_queue = queue.SimpleQueue()
        self._ui_dispatch_after_id = self.root.after(16, self._drain_ui_dispatch_queue)
        # Upload progress can arrive much faster than Tk can repaint a Text
        # widget. Keep only the newest row and render it once per UI turn.
        self._upload_progress_lock = threading.Lock()
        self._upload_progress_pending = None
        self._upload_progress_flush_scheduled = False
        # Connection progress has the same producer pattern as flash progress:
        # esptool and retry/watchdog callbacks can report faster than Tk can
        # repaint. Keep one latest, monotonic row per operation session.
        self._connecting_progress_lock = threading.Lock()
        self._connecting_progress_pending = None
        self._connecting_progress_flush_scheduled = False
        self._connecting_progress_rendered = None
        self._status_update_lock = threading.Lock()
        self._status_update_pending = None
        self._status_update_flush_scheduled = False
        # Track if we're at the start of a new line (for timestamp prefix)
        self._serial_at_line_start = True
        # Avoid repeating the same warning on automatic reconnects. A changed
        # USB descriptor gets a fresh warning.
        self._warned_unrecognized_port_signatures: set[tuple[str, str]] = set()
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
        # Build-affecting platformio.ini fingerprint for each exact board.
        # This lets A -> B -> A compare against A's configuration even while
        # the single generated root ini still temporarily describes B.
        self._build_config_hash_by_board: dict[str, str] = {}
        # Exact PlatformIO DEBUG/RAM/Flash rows from each successful build.
        # The direct esptool path intentionally skips PlatformIO's upload
        # wrapper, so this cache lets it restore the useful build summary
        # without paying for another SCons/project scan.
        self._build_metadata_by_board: dict[str, dict] = {}
        self._platform_upload_metadata_cache: dict[tuple[str, str, str], dict] = {}
        self._compat_warnings_approved_hash: str | None = None
        self._compat_cache: dict | None = None
        self._compat_analysis_gen: int = 0
        self._stop_requested: bool = False
        self._op_session_id: int = 0
        # Fast esptool uploads remain eligible after ordinary BOOT timing
        # misses. This state is reserved for genuine uploader/tool failures.
        self._fast_upload_failure_count = 0
        self._fast_upload_disabled_reason = None

        # ── Startup project selector ──
        # Prompt for a project (existing folder, or scaffold a new one)
        # before the main window appears.
        self.root.withdraw()  # hide main window until project is chosen
        config = load_gui_config()
        cpu_mode = config.get("shared", {}).get("cpu_multithreading", "HIGH")

        import concurrent.futures
        # The pool is shared by startup scans, project/cache work, and later
        # compile-adjacent tasks.  Let the existing CPU/RAM safety policy choose
        # the number of workers; the old hard cap of three left available cores
        # idle while startup services were being brought online.
        self._bg_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, _resource_safe_worker_count(cpu_mode)),
            thread_name_prefix="MCUBgExecutor"
        )
        
        # Check if project was passed in command line arguments (e.g. after a restart)
        cmd_project = None
        if "--project" in sys.argv:
            try:
                idx = sys.argv.index("--project")
                if idx + 1 < len(sys.argv):
                    candidate = Path(sys.argv[idx + 1])
                    if candidate.exists() and candidate.is_dir():
                        cmd_project = candidate
            except Exception:
                pass

        if cmd_project:
            if _validate_and_scaffold_ino(self.root, cmd_project):
                project_dir = cmd_project
            else:
                cmd_project = None

        if not cmd_project:
            last_dir = config.get("last_sketch_dir") or ""
            if last_dir and not Path(last_dir).exists():
                last_dir = ""
            project_dir = show_project_selector(self.root, initial_dir=last_dir)

        if not project_dir:
            try:
                set_monaco_boot_pending(False)
                project_cancelled.set()
            except Exception:
                pass
            try:
                self.root.destroy()
            except Exception:
                pass
            return

        self.sketch_dir_path = project_dir
        config["last_sketch_dir"] = str(self.sketch_dir_path)
        save_gui_config(config)

        # ── Icon ──
        if sys.platform == "win32":
            try:
                import ctypes
                myappid = 'Naph.MCUFlasher.GUI.V6'
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
            except Exception:
                pass
        try:
            icon_path = SCRIPT_DIR / "src" / "assets" / "mcu_icon.ico"
            if not icon_path.exists():
                icon_path = SCRIPT_DIR / "src" / "mcu_icon.ico"
            if icon_path.exists():
                self.root.iconbitmap(default=str(icon_path))
                self.root.iconbitmap(str(icon_path))
            else:
                self.root.iconbitmap(default="")
        except Exception:
            pass

        # ── Fonts & button scaling ──
        # Continuous scale factor based on screen resolution instead of a
        # hard small/large cutoff, so sizing adapts smoothly across monitors
        # (1.0 == a 1920x1080 reference display). Also trimmed down slightly
        # across the board since the old fixed sizes ran a bit large.
        logical_screen_w = self.screen_w / self._display_scale
        logical_screen_h = self.screen_h / self._display_scale
        self._ui_scale = max(
            0.65,
            min(1.0, min(logical_screen_w / 1920.0, logical_screen_h / 1080.0)),
        )
        self._last_applied_scale = self._ui_scale
        self._scalable_buttons: list[tk.Button] = []

        def _sz(base: float, floor: int) -> int:
            return max(floor, round(base * self._ui_scale))

        _font_family_ui = "Segoe UI"
        _font_family_mono = "Consolas"

        self.font_title    = tkfont.Font(family=_font_family_ui, size=_sz(15, 11), weight="bold")
        self.font_subtitle = tkfont.Font(family=_font_family_ui, size=_sz(9, 8))
        self.font_label    = tkfont.Font(family=_font_family_ui, size=_sz(9, 8))
        self.font_btn      = tkfont.Font(family=_font_family_ui, size=_sz(9, 7), weight="bold")
        self.font_mono     = tkfont.Font(family=_font_family_mono, size=_sz(10, 8))
        self.font_mono_sm  = tkfont.Font(family=_font_family_mono, size=_sz(9, 8))
        self.font_status   = tkfont.Font(family=_font_family_ui, size=_sz(9, 8))
        self.monitor_font_size = get_monitor_font_size()
        self.monitor_font = tkfont.Font(family=_font_family_mono, size=self.monitor_font_size)
        self.monitor_font_bold = tkfont.Font(family=_font_family_mono, size=self.monitor_font_size, weight="bold")
        self.monitor_font_header = tkfont.Font(family=_font_family_mono, size=self.monitor_font_size + 1, weight="bold")
        self.monitor_font_large_bold = tkfont.Font(family=_font_family_mono, size=self.monitor_font_size + 2, weight="bold")
        self.monitor_heading_font = tkfont.Font(family=_font_family_ui, size=self.monitor_font_size, weight="bold")
        self._btn_padx = round(_sz(10, 6) * self._display_scale)
        self._btn_pady = round(_sz(3, 2) * self._display_scale)

        self._build_ui()

        # Cover the first Windows paint with a lightweight, dark loading shell.
        # Tk/PanedWindow/native editor surfaces can otherwise appear white for
        # one or more frames while their child windows are being mapped.
        self._create_startup_overlay()

        # Finish laying out every widget while the root is still hidden.  The
        # old order deiconified the empty Tk root before _build_ui(), which
        # caused the visible white/blank flash reported at startup.  If widget
        # construction then failed, that empty window was the only thing the
        # user ever saw before the Tk thread terminated.
        # Re-tune button padding/font live as the window is resized. Bind before
        # deiconifying so no startup configure/maximize events are dropped.
        self.root.bind("<Configure>", self._on_root_configure)

        self.root.update_idletasks()

        # Reveal only after the complete main interface exists. UI readiness
        # is deliberately separate from project/editor/background readiness:
        # once this layout has painted, the user should see and use it while
        # the remaining work continues asynchronously.
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        _startup_event("shell-visible")
        # One idle callback is too early on Windows: it can run before the
        # first WM_PAINT/configure burst finishes and expose partially packed
        # toolbar/pane geometry.  The readiness checker below requires stable
        # geometry across multiple event-loop turns.
        self._schedule_startup_layout_check(80)
        self._startup_readiness_poll_job = self.root.after(
            100, self._poll_startup_readiness
        )
        # Refresh skip-compile readiness whenever the main window regains focus
        # (e.g. after the embedded editor has auto-saved files on the side),
        # but skip if an active operation (compile/upload/flash/clean) is running.
        def _on_focus_in(_e=None):
            if getattr(self, "is_busy", False):
                return
            self._update_skip_compile_state()

        self.root.bind("<FocusIn>", lambda _e: self.root.after(
            200, _on_focus_in), add="+")

        # ── Cleanup on close ──
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._first_run = False
        self._deferred_bg_done = False
        self._deferred_bg_services_started = False
        self._deferred_bg_settle_until = 0.0
        self._deferred_bg_finish_job = None
        self._startup_terminal_deadline = 0.0
        self._startup_terminal_timed_out = False

        # Do not run the selected-project scan from inside _build_ui().  Wait
        # until construction has fully returned and Tk is processing events.
        # This both fixes the startup-order bug and lets a project-scan failure
        # degrade to a visible warning instead of terminating the whole app.
        # Give Windows one real paint cycle before project loading starts.
        # after_idle can run before the first WM_PAINT, making a synchronous
        # project scan look like a frozen white window even when it later
        # recovers. A short timer keeps startup responsive and visible.
        self.root.after(100, self._run_initial_project_load)

        # Secondary services start as soon as the stable UI pass (or the core
        # editor fallback) is available. The fallback keeps startup moving if
        # an editor handshake is delayed; the cover remains until full ready.
        self.root.after(5000, self._deferred_background_init)

    def _run_initial_project_load(self):
        """Load the selected project after the main interface is complete.

        Project discovery is useful but non-essential to creating the window.
        A corrupt project cache, inaccessible folder, stale device setting, or
        other project-specific exception must therefore never take down the
        entire GUI.  Preserve the complete traceback for diagnosis and keep the
        editor/window alive so the user can correct the project or choose a new
        one.
        """
        if not getattr(self, "_startup_editor_ready", False):
            try:
                self.root.after(100, self._run_initial_project_load)
            except Exception:
                pass
            return
        if getattr(self, "_startup_project_load_started", False):
            return
        self._startup_project_load_started = True
        try:
            self._print_welcome()
            self._startup_project_load_complete = True
            self._maybe_finish_startup_ready()
            return
        except Exception as exc:
            import traceback

            error_text = traceback.format_exc()
            log_path = SCRIPT_DIR / "logs" / "project_startup_error.log"
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(error_text, encoding="utf-8")
            except Exception:
                pass

            # Keep the GUI usable and surface the actual final exception in the
            # Build Console.  Every notification call is best-effort because
            # this handler may itself be responding to a partially initialized
            # project panel.
            try:
                self._append("")
                self._append("=" * 56, "warning")
                self._append("⚠ PROJECT STARTUP SCAN FAILED — GUI REMAINS OPEN", "warning")
                self._append("=" * 56, "warning")
                self._append(f"  {type(exc).__name__}: {exc}", "error")
                self._append(f"  Full diagnostic: {log_path}", "dim")
                self._append("  You may select another project or inspect the log above.", "info")
            except Exception:
                pass
            try:
                self._set_status("Project scan failed — see Build Console", Theme.YELLOW)
            except Exception:
                pass

            # Python clears the exception target after leaving this `except`
            # block. Capture strings now so the delayed warning callback does
            # not fail silently when it tries to access `exc`.
            error_type = type(exc).__name__
            error_message = str(exc)

            def _show_project_error(
                error_type=error_type,
                error_message=error_message,
                diagnostic_path=str(log_path),
            ):
                try:
                    if not self.root.winfo_exists():
                        return
                    from tkinter import messagebox
                    messagebox.showwarning(
                        "MCU Flasher — Project Load Warning",
                        "The main window started, but the selected project's "
                        "startup scan encountered an error.\n\n"
                        f"{error_type}: {error_message}\n\n"
                        f"Full diagnostic: {diagnostic_path}\n\n"
                        "The GUI will remain open so you can select another "
                        "project or inspect the Build Console.",
                        parent=self.root,
                    )
                except Exception:
                    pass

            try:
                self.root.after(100, _show_project_error)
            except Exception:
                pass
            # The main UI is already usable at this point. Keep the warning
            # visible in the console/message box without re-covering the
            # editor or blocking the user from selecting another project.
            self._set_startup_overlay_message(
                "⚠ MCU Flasher by Naph", "Project scan warning — opening editor…"
            )
            self._startup_project_load_complete = True
            self._maybe_finish_startup_ready()

    def _unset_main_topmost(self):
        try:
            if hasattr(self, "root") and self.root:
                self.root.attributes("-topmost", False)
        except Exception:
            pass

    def _create_startup_overlay(self):
        """Create a cheap first-paint cover for the main window.

        Native child surfaces and PanedWindow geometry are not always painted
        in the same Windows frame as the Tk widgets around them. Keeping this
        overlay visible for the first event cycle prevents a distracting white
        flash without adding a splash process or blocking wait.
        """
        try:
            overlay = CircularLoadingOverlay(
                self.root,
                bg_color=Theme.BG_DARKEST,
                spinner_color=Theme.CYAN,
                fg_title=Theme.TEXT_BRIGHT,
                fg_sub=Theme.TEXT_DIM,
                track_color=Theme.BORDER,
                text="⚡ MCU Flasher by Naph",
            )
            overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            overlay.lift()
            overlay.update_message(
                "⚡ MCU Flasher by Naph",
                ("Loading Monaco Editor…" if getattr(self, "editor_mode", "default") == "monaco"
                 else "Loading project files…"),
            )
            self._startup_overlay = overlay
            # Avoid a one-frame flash on very fast launches. This is only a
            # paint guard; the cover is removed only after stable geometry and
            # the full startup contract have both completed.
            self._startup_min_visible_until = time.monotonic() + 0.35
            self._startup_overlay_created_at = time.monotonic()
        except Exception:
            self._startup_overlay = None

    def _raise_startup_overlay(self):
        """Keep the Tk cover above packed widgets during the first paint."""
        overlay = getattr(self, "_startup_overlay", None)
        if overlay is None or getattr(self, "_startup_overlay_released", False):
            self._startup_overlay_raise_job = None
            return
        try:
            if not overlay.winfo_exists():
                self._startup_overlay_raise_job = None
                return
            overlay.lift()
            self._startup_overlay_raise_job = self.root.after(
                50, self._raise_startup_overlay
            )
        except Exception:
            self._startup_overlay_raise_job = None

    def _schedule_startup_layout_check(self, delay_ms=60):
        """Queue a non-blocking check for stable, visible widget geometry."""
        if (getattr(self, "_startup_overlay_released", False)
                or getattr(self, "_startup_ui_ready", False)
                or getattr(self, "_startup_layout_check_job", None)):
            return
        try:
            self._startup_layout_check_job = self.root.after(
                max(0, int(delay_ms)), self._check_startup_layout_ready
            )
        except Exception:
            self._startup_layout_check_job = None

    def _startup_layout_snapshot(self):
        """Return key widget geometry once the visible shell has settled."""
        try:
            if not self.root.winfo_exists() or self.root.winfo_width() <= 1:
                return None

            action_container = (
                self.top_compact_actions
                if self.top_compact_actions.winfo_ismapped()
                else self.actions_frame
            )
            widgets = (
                self.title_frame,
                self.title_row_top,
                self.title_left,
                self.sketch_frame,
                action_container,
                self.ctrl_row_top,
                self.h_split_pane,
                self.main_pane,
                self.bottom_notebook,
            )
            geometry = [
                (
                    widget.winfo_id(),
                    widget.winfo_x(),
                    widget.winfo_y(),
                    widget.winfo_width(),
                    widget.winfo_height(),
                    widget.winfo_reqwidth(),
                    widget.winfo_reqheight(),
                )
                for widget in widgets
                if widget.winfo_exists()
            ]
            if len(geometry) != len(widgets) or any(
                width <= 0 or height <= 0
                for _wid, _x, _y, width, height, _req_w, _req_h in geometry
            ):
                return None
            return (self.root.winfo_width(), self.root.winfo_height(), tuple(geometry))
        except Exception:
            return None

    def _check_startup_layout_ready(self):
        """Release visual readiness only after two stable layout samples."""
        self._startup_layout_check_job = None
        if (getattr(self, "_startup_overlay_released", False)
                or getattr(self, "_startup_ui_ready", False)):
            return

        # Apply the first responsive pass before sampling.  Configure events
        # are debounced during normal use; during startup we can safely apply
        # the pending pass now while the opaque cover is still raised.
        try:
            resize_job = getattr(self, "_resize_after_id", None)
            if resize_job:
                try:
                    self.root.after_cancel(resize_job)
                except Exception:
                    pass
                self._resize_after_id = None
                self._minwidth_after_id = None
            if hasattr(self, "_apply_debounced_resize"):
                self._apply_debounced_resize()
            elif hasattr(self, "_apply_dynamic_button_scale"):
                self._apply_dynamic_button_scale()
            self.root.update_idletasks()
        except Exception:
            pass

        snapshot = self._startup_layout_snapshot()
        if snapshot is not None and snapshot == getattr(
                self, "_startup_layout_signature", None):
            self._startup_layout_stable_passes += 1
        elif snapshot is not None:
            self._startup_layout_signature = snapshot
            self._startup_layout_stable_passes = 1
        else:
            self._startup_layout_signature = None
            self._startup_layout_stable_passes = 0

        # A second sample prevents the first post-deiconify geometry from being
        # mistaken for the finished layout.  Keep checking even after the
        # minimum paint guard if a native child or responsive repack is still
        # changing dimensions.
        if self._startup_layout_stable_passes >= 2:
            self._mark_startup_ui_ready()
            return
        self._schedule_startup_layout_check(60)

    def _mark_startup_ui_ready(self):
        """Record stable visual readiness; dismissal also requires full startup."""
        if getattr(self, "_startup_ui_ready", False):
            return
        self._startup_ui_ready = True
        self._set_startup_overlay_message(
            "⚡ MCU Flasher by Naph", "UI ready — continuing background setup…"
        )
        self._raise_startup_overlay()
        # Start independent startup services as soon as the visible hierarchy
        # is stable. Their heavy work is already routed through worker threads,
        # while their Tk callbacks remain serialized on the UI thread.
        try:
            self.root.after(0, self._deferred_background_init)
        except Exception:
            pass
        self._schedule_startup_overlay_dismiss()

    def _schedule_startup_overlay_dismiss(self):
        """Schedule cover removal after visual and full startup readiness."""
        if (not getattr(self, "_startup_ui_ready", False)
                or not getattr(self, "_startup_ready", False)
                or getattr(self, "_startup_overlay_released", False)
                or getattr(self, "_startup_overlay", None) is None):
            return
        if getattr(self, "_startup_overlay_dismiss_job", None):
            return
        delay_ms = max(
            0, round((getattr(self, "_startup_min_visible_until", 0.0)
                      - time.monotonic()) * 1000)
        )
        try:
            self._startup_overlay_dismiss_job = self.root.after(
                delay_ms, self._dismiss_startup_overlay
            )
        except Exception:
            self._dismiss_startup_overlay()

    def _set_startup_overlay_message(self, title=None, subtitle=None):
        overlay = getattr(self, "_startup_overlay", None)
        if overlay is None:
            return
        try:
            overlay.update_message(title, subtitle)
        except Exception:
            pass

    def _poll_startup_readiness(self):
        """Keep startup feedback current without blocking Tk's event loop."""
        # This callback used to share _startup_overlay_dismiss_job with the
        # real dismissal callback. That left the poll's own timer ID set while
        # the callback was running, so a ready application could refuse to
        # schedule dismissal and then stop polling forever.
        self._startup_readiness_poll_job = None
        if getattr(self, "_startup_overlay_released", False):
            return
        if getattr(self, "_startup_overlay", None) is None:
            return

        if getattr(self, "_startup_ready", False) and not getattr(
                self, "_startup_ui_ready", False):
            self._schedule_startup_layout_check(0)
            return

        # The visible UI is complete. The cover still waits for the full
        # application readiness state before it is removed.
        if getattr(self, "_startup_ui_ready", False):
            self._raise_startup_overlay()
            self._schedule_startup_overlay_dismiss()
            return

        # Safety fallback: if the layout checker never receives usable geometry
        # (e.g. a native child/window-manager edge case), allow the degraded
        # editor path to begin after 8s. The cover still waits for the full
        # startup contract rather than disappearing on this fallback alone.
        _STARTUP_SAFETY_TIMEOUT = 8.0
        created_at = getattr(self, "_startup_overlay_created_at", 0.0)
        if created_at and (time.monotonic() - created_at) >= _STARTUP_SAFETY_TIMEOUT:
            self._mark_startup_ui_ready()
            self._mark_startup_ready("Application ready")
            self._deferred_background_init()
            return

        if getattr(self, "_startup_editor_ready", False):
            self._set_startup_overlay_message(
                "⚡ MCU Flasher by Naph", "Finishing application initialization…"
            )
        elif getattr(self, "editor_mode", "default") == "monaco":
            editor_ready = (
                getattr(self, "_editor_embedded", False)
                and getattr(self, "_editor_content_loaded", False)
            )
            fallback_ready = getattr(self, "_editor_fallback_ready", False)
            if editor_ready or fallback_ready:
                self._mark_startup_ready("Application ready")
                return
            else:
                self._set_startup_overlay_message(
                    "⚡ MCU Flasher by Naph", "Loading Monaco Editor…"
                )
        elif getattr(self, "_default_editor_ready", False):
            self._mark_startup_ready("Code editor ready")
            return
        else:
            self._set_startup_overlay_message(
                "⚡ MCU Flasher by Naph", "Loading project files…"
            )

        try:
            self._startup_readiness_poll_job = self.root.after(
                100, self._poll_startup_readiness
            )
        except Exception:
            self._startup_readiness_poll_job = None

    def _mark_startup_ready(self, reason=""):
        """Mark the editor/core phase ready and start background work."""
        if getattr(self, "_startup_editor_ready", False):
            return
        self._startup_editor_ready = True
        self._startup_ready_reason = str(reason or "Core UI ready")
        _startup_event("core-ui-ready")
        self._set_startup_overlay_message(
            "⚡ MCU Flasher by Naph", "Finishing application initialization…"
        )

        # Start deferred services immediately once the core editor has reported
        # ready. The visual layout checker also requests this path, so the
        # guard in _deferred_background_init coalesces both triggers.
        try:
            self.root.after(0, self._deferred_background_init)
        except Exception:
            pass
        try:
            self.root.after(0, self._run_initial_project_load)
        except Exception:
            pass

    def _maybe_finish_startup_ready(self):
        """Record completion of non-visual startup services."""
        if getattr(self, "_startup_ready", False):
            return
        if not getattr(self, "_startup_editor_ready", False):
            return
        if not getattr(self, "_startup_project_load_complete", False):
            return
        if not getattr(self, "_deferred_bg_done", False):
            return

        self._startup_ready = True
        self._startup_ready_reason = "Application fully initialized"
        _startup_event("application-ready")
        self._schedule_shell_prewarm()
        if getattr(self, "_startup_ui_ready", False):
            self._schedule_startup_overlay_dismiss()
        else:
            self._schedule_startup_layout_check(0)

    def _dismiss_startup_overlay(self):
        """Remove the startup cover after stable layout and full readiness."""
        overlay = getattr(self, "_startup_overlay", None)
        if overlay is None:
            return
        self._startup_overlay_released = True
        self._startup_overlay = None
        for attr in (
            "_startup_overlay_dismiss_job",
            "_startup_readiness_poll_job",
            "_startup_overlay_raise_job",
            "_startup_layout_check_job",
            "_startup_overlay_safety_job",
        ):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)
        try:
            overlay.stop_and_destroy()
        except Exception:
            try:
                overlay.destroy()
            except Exception:
                pass

        # Ensure a full layout synchronization and paint pass once the cover is gone.
        # If the user resized or maximized the window while the overlay was covering it,
        # cached keys and debounced jobs may leave stale widget geometry or unpainted areas.
        try:
            self._last_responsive_layout_key = None
            self._last_cfg_w = None
            self._last_cfg_h = None
            if hasattr(self, "_apply_dynamic_button_scale"):
                self._apply_dynamic_button_scale()
            self.root.update_idletasks()
        except Exception:
            pass

        # Windows DWM: force a complete non-client and client area redraw of the window
        # hierarchy to clean away any stale GDI bitmaps or offset artifacts from resizing
        # during startup.
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = self.root.winfo_id()
                if hwnd:
                    # RDW_INVALIDATE (0x1) | RDW_ERASE (0x4) | RDW_ALLCHILDREN (0x80) | RDW_UPDATENOW (0x100) = 0x0185
                    ctypes.windll.user32.RedrawWindow(hwnd, None, None, 0x0185)
            except Exception:
                pass

        if getattr(self, "editor_mode", "default") == "monaco" and hasattr(self, "_resize_embedded_editor"):
            try:
                self._resize_embedded_editor()
            except Exception:
                pass

    def _deferred_background_init(self):
        """Start independent secondary services after the UI layout is stable.

        The order below preserves the serial-monitor-before-esptool contract,
        but each service immediately starts its own worker/thread.  The shared
        pool is sized by the CPU/RAM policy, so independent startup work can
        overlap without moving Tk calls off the Tk thread."""
        if (getattr(self, "_deferred_bg_done", False)
                or getattr(self, "_deferred_bg_started", False)):
            return
        self._deferred_bg_started = True

        # Priority order (most user-visible benefit first):
        #   1. Board catalog refresh      — comboboxes show correct data
        #   2. Serial monitor             — must open the port BEFORE any
        #     esptool probe runs, otherwise the probe eats the MCU boot
        #     banner (ESP-ROM/rst/load/entry). Documented ordering contract.
        #   3. Port scan                  — populates the port dropdown
        #   4. Hardware action buttons    — depends on board+port state
        #   5. Hotplug monitor thread     — keeps 3/4 fresh
        #   6. Background syntax checker  — pure comfort, lowest urgency
        #   7. Embedded terminal          — heaviest (PTY/webview spawn);
        #     deliberately last so it cannot compete with the above.
        self._deferred_bg_queue: deque = deque([
            ("board-catalog", self._dbi_board_catalog),
            ("serial-monitor", self._dbi_serial_monitor),
            ("port-scan", self._dbi_port_scan),
            ("hardware-buttons", self._dbi_hardware_buttons),
            ("hotplug-monitor", self._dbi_hotplug_monitor),
            ("syntax-checker", self._dbi_syntax_checker),
            ("terminal", self._dbi_terminal),
        ])
        # Keep a real event-loop turn between services for Tk callbacks, but
        # remove the old 150 ms artificial delay that left cores idle during
        # startup. Heavy work is already asynchronous inside each service.
        self._deferred_bg_step_gap_ms = 0
        self._advance_deferred_background_init()

    _start_deferred_background_init = _deferred_background_init

    def _dbi_board_catalog(self):
        # Dynamic board matching is intentionally deferred until the main
        # window has painted. A cached catalog is already available when
        # possible; this refresh keeps it correct without blocking launch.
        self._reload_supported_boards()
        self._apply_board_monitor_baud()
        self._apply_board_upload_speed()
        self._on_board_changed()

    def _dbi_serial_monitor(self):
        # Signals the esptool probe to bail out immediately once running.
        self._monitor_should_run = True
        self._schedule_auto_start_monitor(0)

    def _dbi_port_scan(self):
        # Async since the port-scan rework: kicks a worker thread off the Tk
        # thread and applies results back when ready.
        self._refresh_ports()

    def _dbi_hardware_buttons(self):
        self._update_hardware_action_buttons()

    def _dbi_hotplug_monitor(self):
        self._start_port_polling()

    def _dbi_syntax_checker(self):
        self._start_background_syntax_thread()

    def _dbi_terminal(self):
        # Native terminal webview and shell are created lazily when the
        # Terminal tab is opened, keeping startup lightweight and fast.
        pass

    def _advance_deferred_background_init(self):
        """Run the next queued service, then yield the event loop before the
        following one. Drains into the shared settle/completion tracking."""
        queue = getattr(self, "_deferred_bg_queue", None)
        if getattr(self, "_deferred_bg_done", False):
            return
        try:
            if queue:
                name, service = queue.popleft()
                try:
                    service()
                except Exception as e:
                    print(f"[MCU Flasher] Deferred service '{name}' failed: {e}")
                if queue:
                    try:
                        self.root.after(
                            getattr(self, "_deferred_bg_step_gap_ms", 150),
                            self._advance_deferred_background_init,
                        )
                        return
                    except Exception:
                        pass  # root gone — fall through to drain silently
            # Queue empty (or event loop unavailable): services have been
            # STARTED. Starting worker threads is not the same as being
            # ready — keep the startup cover for one settling pass so their
            # first UI callbacks, port scan, monitor startup, and syntax
            # initialization can complete before readiness is reported.
            self._deferred_bg_services_started = True
            self._deferred_bg_settle_until = time.monotonic() + 1.5
            self._schedule_deferred_background_completion_check()
        except Exception as e:
            print(f"[MCU Flasher] Error in deferred background init: {e}")
            self._deferred_bg_services_started = True
            self._deferred_bg_settle_until = time.monotonic() + 1.5
            self._schedule_deferred_background_completion_check()

    def _schedule_deferred_background_completion_check(self):
        if getattr(self, "_deferred_bg_done", False):
            return
        try:
            self._deferred_bg_finish_job = self.root.after(
                100, self._finish_deferred_background_init
            )
        except Exception:
            self._deferred_bg_finish_job = None

    def _finish_deferred_background_init(self):
        """Release startup only after the initial background pass has settled."""
        self._deferred_bg_finish_job = None
        if getattr(self, "_deferred_bg_done", False):
            return
        if not getattr(self, "_deferred_bg_services_started", False):
            self._schedule_deferred_background_completion_check()
            return
        if time.monotonic() < getattr(self, "_deferred_bg_settle_until", 0.0):
            self._schedule_deferred_background_completion_check()
            return
        self._deferred_bg_done = True
        try:
            self._set_status(
                "Application ready — terminal and background services initialized",
                Theme.CYAN,
            )
        except Exception:
            pass
        _startup_event("optional-services-settled")
        self._maybe_finish_startup_ready()

    def _startup_terminal_ready(self):
        """Return true only after the terminal has a usable native/fallback shell."""
        if getattr(self, "_project_terminal_fallback", False):
            with self._shell_state_lock:
                session = self._shell_sessions.get(self._shell_active_kind)
                if not session:
                    return False
                if session.get("ready"):
                    return True
                # A deliberate compatibility error is a completed attempt;
                # do not leave the entire application covered forever when a
                # machine has no PTY support.
                return bool(
                    not session.get("running")
                    and "[MCU Flasher]" in str(session.get("output", ""))
                )
        return bool(
            getattr(self, "_project_terminal_embedded", False)
            and getattr(self, "_project_terminal_page_ready", False)
        )

    def _get_sketch_display_name(self) -> str:
        if hasattr(self, "sketch_dir_path") and self.sketch_dir_path:
            return self.sketch_dir_path.name
        return ""

    def _update_sketch_marquee(self):
        """Perform a smooth sliding/marquee text animation step for the project name label."""
        if getattr(self, "_sketch_marquee_after_id", None) is not None:
            try:
                self.root.after_cancel(self._sketch_marquee_after_id)
            except Exception:
                pass
            self._sketch_marquee_after_id = None

        if not hasattr(self, "lbl_sketch") or not self.lbl_sketch:
            return

        folder_name = self.sketch_dir_path.name if hasattr(self, "sketch_dir_path") and self.sketch_dir_path else ""
        if not folder_name:
            return

        width = 0
        try:
            width = self.root.winfo_width()
        except Exception:
            pass

        # Determine visible character limit based on window width
        if width and width < 950:
            limit = 14
        elif width and width < 1200:
            limit = 18
        elif width and width < 1400:
            limit = 28
        else:
            limit = 45

        if len(folder_name) <= limit:
            # Full project name fits inside container, no sliding needed
            try:
                self.lbl_sketch.configure(text=folder_name)
            except Exception:
                pass
            self._sketch_marquee_idx = 0
            self._sketch_marquee_dir = 1
            self._sketch_marquee_after_id = self.root.after(1000, self._update_sketch_marquee)
            return

        # Perform ping-pong marquee animation with pauses at start and end
        max_idx = len(folder_name) - limit
        idx = getattr(self, "_sketch_marquee_idx", 0)
        direction = getattr(self, "_sketch_marquee_dir", 1)

        display_str = folder_name[idx:idx + limit]
        try:
            self.lbl_sketch.configure(text=display_str)
        except Exception:
            pass

        delay = 350
        if idx == 0 and direction == 1:
            delay = 1800  # Pause at start so user can read beginning easily
        elif idx >= max_idx and direction == 1:
            delay = 1800  # Pause at end
            direction = -1
        elif idx <= 0 and direction == -1:
            delay = 1800  # Pause at start before bouncing right again
            direction = 1

        next_idx = idx + direction
        if next_idx > max_idx:
            next_idx = max_idx
            direction = -1
        elif next_idx < 0:
            next_idx = 0
            direction = 1

        self._sketch_marquee_idx = next_idx
        self._sketch_marquee_dir = direction

        self._sketch_marquee_after_id = self.root.after(delay, self._update_sketch_marquee)
