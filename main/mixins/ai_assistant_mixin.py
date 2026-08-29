#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import os
import time
import json
import re
import shutil
import tempfile
import subprocess
import threading
import queue
import ctypes
import traceback
import hashlib
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING
from pathlib import Path
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, font as tkfont


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

class AIAssistantMixin(_Base):
    """Mixin providing AIAssistantMixin capabilities for MCUUploadGUI."""
    def _confirm_ai_assistant_launch(self) -> bool:
        """Ask for consent before importing or starting the AI integration."""
        disclaimer_title = "OpenCode AI Assistant (Beta Test)"
        disclaimer_msg = (
            "🤖 OpenCode AI Assistant\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📌 Notice & Disclaimer:\n"
            "• OpenCode AI integration in this project is currently in BETA TESTING.\n"
            "• MCU Flash GUI does not claim any copyright, trademark, or ownership of OpenCode AI. "
            "All rights, trademarks, and intellectual property belong to their respective creators.\n\n"
            "🛡️ System Permission:\n"
            "• Clicking 'Yes' will launch OpenCode in an Elevated (Administrator) Command Prompt "
            "to assist you with fixing, explaining, and debugging code.\n\n"
            "Do you want to proceed and launch OpenCode AI as Administrator?"
        )
        
        return bool(messagebox.askyesno(disclaimer_title, disclaimer_msg, parent=self.root))

    def _launch_opencode_ai_assistant(self):
        """Prompt user with beta & copyright disclaimer before launching elevated OpenCode AI."""
        if not self._is_internet_available():
            messagebox.showwarning(
                "No Internet Connection",
                "OpenCode AI Assistant requires an active internet connection to function.\n\n"
                "Please check your network connection and try again.",
                parent=self.root,
            )
            return
        proceed = self._confirm_ai_assistant_launch()
        if proceed:
            try:
                # pyrefly: ignore [missing-import]
                from dedicated_AI import launch_opencode_elevated_cmd
                launch_opencode_elevated_cmd(str(self.sketch_dir_path))
            except Exception as e:
                # Fallback in case dedicated_AI import fails
                try:
                    import ctypes
                    target_dir = os.path.abspath(str(self.sketch_dir_path))
                    window_title = "MCU Flash GUI - OpenCode AI Assistant (Administrator)"
                    cmd_args = f'/k "cd /d \"{target_dir}\" && title {window_title} && cls && opencode"'
                    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", cmd_args, target_dir, 1)
                    if int(ret) <= 32:
                        subprocess.Popen(f'start cmd /k "cd /d \"{target_dir}\" && title {window_title} && cls && opencode"', shell=True)
                except Exception as err:
                    messagebox.showerror("Launch Error", f"Failed to launch OpenCode AI: {err}", parent=self.root)

    def _dispose_active_ai_assistant(self):
        """Dispose and terminate the active OpenCode AI CMD process window if open."""
        try:
            self._hide_ai_side_panel()
            was_closed = False
            if getattr(self, "ai_controller", None):
                was_closed = self.ai_controller.dispose()
            else:
                try:
                    # pyrefly: ignore [missing-import]
                    from dedicated_AI import close_active_opencode
                    was_closed = close_active_opencode()
                except Exception:
                    pass
            if was_closed and hasattr(self, "_append_notif"):
                self._append_notif("  ℹ Active AI Assistant process closed (project changed).", "info")
        except Exception:
            pass

    def _ensure_ai_controller(self, module=None) -> bool:
        """Initialize the optional AI controller on the Tk thread.

        Importing ``dedicated_AI`` can load optional native packages and may
        perform a one-time package check.  The first-click path prepares that
        module in the background and passes it here after the worker finishes;
        this method itself must remain UI-thread-only because it constructs a
        controller that owns Tk callbacks.
        """
        if getattr(self, "ai_controller", None) is not None:
            return True
        module_was_prepared = module is not None
        module = module or _load_dedicated_ai()
        if module is None:
            return False
        try:
            if not module_was_prepared and not module.is_opencode_installed():
                return False
            controller_class = getattr(module, "AIController", None)
            if controller_class is None:
                return False
            self.ai_controller = controller_class(
                get_sketch_dir_func=lambda: getattr(self, "sketch_dir_path", os.getcwd()),
                root=self.root,
                on_ai_edit_func=self._on_ai_applied_edit,
                on_state_change_func=self._update_ai_button_label,
            )
            if getattr(self, "btn_ai_assistant", None):
                self.ai_controller.add_button(self.btn_ai_assistant)
            return True
        except Exception:
            self.ai_controller = None
            return False

    def _prepare_ai_controller_background(self):
        """Load and validate the optional AI integration away from Tk."""
        module = _load_dedicated_ai()
        if module is None:
            raise RuntimeError("The AI Assistant integration could not be loaded.")
        if not module.is_opencode_installed():
            raise RuntimeError("OpenCode is not installed or is not available on PATH.")
        return module

    def _finish_ai_controller_background(self, module):
        """Attach a worker-prepared AI module without blocking the UI."""
        if not getattr(self, "_ai_side_visible", False):
            return
        if not self._ensure_ai_controller(module):
            self._dismiss_ai_loading_overlay()
            self._hide_ai_side_panel()
            messagebox.showerror(
                "AI Assistant",
                "The AI Assistant could not be initialized.",
                parent=self.root,
            )
            return

        # The first-click consent was already accepted before the worker was
        # scheduled, so this launch must not display the controller's prompt
        # a second time.
        self.ai_controller.disclaimer_accepted = True
        if getattr(self.ai_controller, "is_launching", False) or module.is_opencode_running():
            return
        started = self.ai_controller.toggle_ai()
        if not started:
            self._hide_ai_side_panel()

    def _ai_controller_background_error(self, exc):
        """Report a first-use AI preparation failure on the Tk thread."""
        self._dismiss_ai_loading_overlay()
        if getattr(self, "_ai_side_visible", False):
            self._hide_ai_side_panel()
        messagebox.showerror("AI Assistant", str(exc), parent=self.root)

    def _update_ai_button_label(self):
        """Update top toolbar AI button text to '🤖 Hide AI' when open and '🤖 AI Assistant' when hidden."""
        btn = getattr(self, "btn_ai_assistant", None)
        if not btn:
            return
        if getattr(self, "ai_controller", None) and getattr(self.ai_controller, "is_launching", False):
            return
        is_visible = getattr(self, "_ai_side_visible", False)
        if is_visible:
            btn.config(text="🤖 Hide AI", bg=Theme.BTN_CLEAR, fg=Theme.TEXT_BRIGHT)
        else:
            btn.config(text="🤖 AI Assistant", bg=Theme.BTN_CLEAR, fg=Theme.TEXT_BRIGHT)


    def _toggle_ai_side_panel(self):
        """Toggle right-side OpenCode AI Assistant container panel visibility."""
        is_visible = getattr(self, "_ai_side_visible", False)
        if is_visible:
            self._hide_ai_side_panel()
            return

        if not self._is_internet_available():
            messagebox.showwarning(
                "No Internet Connection",
                "OpenCode AI Assistant requires an active internet connection to function.\n\n"
                "Please check your network connection and try again.",
                parent=self.root,
            )
            return

        # The consent dialog must be the first action on the first click.
        # Do not show the panel, import optional WebView packages, or start a
        # worker until the user has explicitly accepted it.
        if not getattr(self, "ai_controller", None):
            if getattr(self, "_ai_controller_init_pending", False):
                return
            if not self._confirm_ai_assistant_launch():
                return
            self._ai_controller_init_pending = True

        # Show the shell and loader immediately.  The optional AI module can
        # import native WebView/PTY packages (and may perform a package check),
        # so doing that before returning to Tk makes the first click appear
        # frozen on slower or antivirus-scanned machines.
        self._show_ai_side_panel()
        if getattr(self, "ai_controller", None):
            try:
                # pyrefly: ignore [missing-import]
                from dedicated_AI import is_opencode_running
                if not is_opencode_running():
                    started = self.ai_controller.toggle_ai()
                    if not started:
                        self._hide_ai_side_panel()
            except Exception as exc:
                self._ai_controller_background_error(exc)
            return

        def _on_ready(module):
            self._ai_controller_init_pending = False
            self._finish_ai_controller_background(module)

        def _on_error(exc):
            self._ai_controller_init_pending = False
            self._ai_controller_background_error(exc)

        self._run_bg_task(
            self._prepare_ai_controller_background,
            on_success=_on_ready,
            on_error=_on_error,
        )

    def _show_ai_loading_overlay_4s(self):
        """Show a non-blocking loader while the AI WebView is attaching.

        Readiness is accepted from either the cross-process marker or a valid
        embedded HWND. This prevents a fully loaded AI terminal from remaining
        hidden behind the spinner when startup output completes unusually fast.
        """
        self._dismiss_ai_loading_overlay()
        try:
            if hasattr(self, "ai_side_container") and self.ai_side_container:
                bg = getattr(Theme, "BG_DARKEST", "#0c0d10")
                self.ai_loading_overlay = CircularLoadingOverlay(
                    self.ai_side_container,
                    bg_color=bg,
                    spinner_color="#00e5ff",
                    text="Initializing AI Assistant..."
                )
                self.ai_loading_overlay.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)
                self.ai_loading_overlay.lift()

                if hasattr(self, "root") and self.root:
                    self._ai_launch_marker_time = time.time()
                    self._ai_embed_ready_time = 0.0
                    self._ai_overlay_retry_count = 0
                    self._ai_overlay_timer_id = self.root.after(100, self._poll_ai_ready_dismiss)
        except Exception:
            pass

    _show_ai_loading_overlay_3s = _show_ai_loading_overlay_4s

    def _poll_ai_ready_dismiss(self):
        """Dismiss only after OpenCode reports that its TUI is interactive.

        Native embedding or WebSocket connection alone is not readiness: the
        WebView may already be painted while OpenCode is still loading models,
        configuration, or its initial screen. The child process publishes a
        JSON marker only after meaningful PTY output has settled.
        """
        overlay = getattr(self, "ai_loading_overlay", None)
        if not overlay:
            return
        now = time.time()
        launch_time = float(getattr(self, "_ai_launch_marker_time", now) or now)
        elapsed = max(0.0, now - launch_time)
        embedded = False
        marker_ready = False
        marker_reason = ""
        try:
            hwnd = getattr(self, "_ai_hwnd", None)
            embedded = bool(hwnd and getattr(self, "_ai_is_embedded", False))
            if embedded and win32gui is not None:
                embedded = bool(win32gui.IsWindow(hwnd))

            sketch_dir = getattr(self, "sketch_dir_path", None)
            if sketch_dir:
                marker = Path(sketch_dir) / ".ai_ready_signal"
                if marker.exists() and marker.stat().st_mtime >= (launch_time - 2.0):
                    try:
                        marker_data = json.loads(marker.read_text(encoding="utf-8", errors="replace"))
                    except Exception:
                        marker_data = {}
                    marker_reason = str(marker_data.get("reason", ""))
                    marker_ready = marker_reason in {
                        "opencode-ready",
                        "pty-settled",
                    }
        except Exception:
            pass

        # Keep the copy accurate to the actual startup phase.
        try:
            if marker_ready and not embedded:
                overlay.update_message(
                    "Attaching AI Assistant...",
                    "OpenCode is ready. Finishing the embedded terminal window...",
                )
            elif embedded and not marker_ready:
                if elapsed >= 20.0:
                    overlay.update_message(
                        "OpenCode is still starting...",
                        "First launch can take longer. Waiting until the terminal is interactive...",
                    )
                else:
                    overlay.update_message(
                        "Loading OpenCode...",
                        "Terminal attached. Waiting for OpenCode to finish initialization...",
                    )
            else:
                overlay.update_message(
                    "Initializing AI Assistant...",
                    "Starting the OpenCode process and embedded terminal...",
                )
        except Exception:
            pass

        if embedded and marker_ready:
            self._dismiss_ai_loading_overlay()
            return

        self._ai_overlay_retry_count = getattr(self, "_ai_overlay_retry_count", 0) + 1
        if hasattr(self, "root") and self.root:
            self._ai_overlay_timer_id = self.root.after(150, self._poll_ai_ready_dismiss)

    def _dismiss_ai_loading_overlay(self):
        """Remove and clean up circular loading overlay."""
        if hasattr(self, "_ai_overlay_timer_id") and self._ai_overlay_timer_id and hasattr(self, "root") and self.root:
            try:
                self.root.after_cancel(self._ai_overlay_timer_id)
            except Exception:
                pass
            self._ai_overlay_timer_id = None

        if hasattr(self, "ai_loading_overlay") and self.ai_loading_overlay:
            try:
                self.ai_loading_overlay.stop_and_destroy()
            except Exception:
                pass
            self.ai_loading_overlay = None

    def _show_ai_side_panel(self):
        """Show the AI Assistant side panel container (stacked vertically when editor is detached, column on right when attached)."""
        if not hasattr(self, "ai_side_container") or not hasattr(self, "h_split_pane"):
            return

        self._ai_side_visible = True
        self._update_ai_button_label()
        self._sync_ai_and_editor_layout()

        # Display the readiness-driven loading overlay only on first launch.
        # Subsequent hide/unhide toggles keep the already-running terminal visible.
        # Do not import dedicated_AI here: its first import can load native
        # WebView/PTY dependencies and must stay off the Tk thread.
        ai_module = _dedicated_ai_module if _dedicated_ai_module else None
        ai_running = False
        if ai_module is not None:
            try:
                ai_running = bool(ai_module.is_opencode_running())
            except Exception:
                ai_running = False
        if not getattr(self, "_has_shown_ai_first_time_loader", False) or not ai_running:
            self._has_shown_ai_first_time_loader = True
            self._show_ai_loading_overlay_4s()

        # Reveal embedded native Win32 window if previously hidden
        if getattr(self, "_ai_hwnd", None) and win32gui is not None:
            try:
                win32gui.ShowWindow(self._ai_hwnd, win32con.SW_SHOW)
                self._resize_embedded_ai()
            except Exception:
                pass

        # Trigger launch if AI process is not running.  If the optional module
        # is still being prepared, the background completion callback will do
        # this later; no import or package probe belongs on the Tk thread.
        if getattr(self, "ai_controller", None):
            try:
                if not ai_running and not getattr(self.ai_controller, "is_launching", False):
                    started = self.ai_controller.toggle_ai()
                    if not started:
                        self._hide_ai_side_panel()
                        return
            except Exception:
                pass
        elif getattr(self, "_ai_controller_init_pending", False):
            pass
        elif hasattr(self, "_launch_opencode_ai_assistant"):
            self._launch_opencode_ai_assistant()

        # Poll and embed AI pywebview OS window into self.ai_embed_frame
        self._start_ai_embedding_poll()
        self._start_ai_size_watchdog()

    def _hide_ai_side_panel(self):
        """Hide the AI Assistant side panel container."""
        self._dismiss_ai_loading_overlay()

        if hasattr(self, "_ai_embed_poll_job") and self._ai_embed_poll_job:
            try:
                self.root.after_cancel(self._ai_embed_poll_job)
            except Exception:
                pass
            self._ai_embed_poll_job = None

        if hasattr(self, "_ai_size_watchdog_job") and self._ai_size_watchdog_job:
            try:
                self.root.after_cancel(self._ai_size_watchdog_job)
            except Exception:
                pass
            self._ai_size_watchdog_job = None

        # Explicitly hide embedded native Win32 window first so it doesn't linger on screen
        if getattr(self, "_ai_hwnd", None) and win32gui is not None:
            try:
                win32gui.ShowWindow(self._ai_hwnd, win32con.SW_HIDE)
            except Exception:
                pass

        self._ai_side_visible = False
        self._update_ai_button_label()
        self._sync_ai_and_editor_layout()

    def _start_ai_embedding_poll(self):
        self._ai_embed_attempts = 0
        if hasattr(self, "_ai_embed_poll_job") and self._ai_embed_poll_job:
            try:
                self.root.after_cancel(self._ai_embed_poll_job)
            except Exception:
                pass
            self._ai_embed_poll_job = None
        if hasattr(self, "root") and self.root:
            self._ai_embed_poll_job = self.root.after(50, self._try_embed_ai_window)

    def _try_embed_ai_window(self):
        """Find and embed the pywebview-hosted AI window into the right-side container."""
        # The scheduled callback is now running; clear the handle before any
        # possible reschedule so stale Tk after-ids never accumulate.
        self._ai_embed_poll_job = None
        if win32gui is None or win32con is None:
            return

        # Check if already embedded and HWND is still valid
        if getattr(self, "_ai_hwnd", None):
            try:
                if win32gui.IsWindow(self._ai_hwnd):
                    self._resize_embedded_ai()
                    return
            except Exception:
                self._ai_hwnd = None

        import ctypes
        hwnd = ctypes.windll.user32.FindWindowW(None, "MCU Flash GUI - OpenCode AI Assistant")
        if not hwnd or hwnd == 0:
            self._ai_embed_attempts = getattr(self, "_ai_embed_attempts", 0) + 1
            if self._ai_embed_attempts < 200 and getattr(self, "_ai_side_visible", False):
                self._ai_embed_poll_job = self.root.after(75, self._try_embed_ai_window)
            else:
                self._ai_embed_poll_job = None
            return

        try:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            tk_hwnd = self.ai_embed_frame.winfo_id()

            # Set WS_CLIPCHILDREN on parent Tk frame
            try:
                tk_style = win32gui.GetWindowLong(tk_hwnd, win32con.GWL_STYLE)
                win32gui.SetWindowLong(tk_hwnd, win32con.GWL_STYLE, tk_style | win32con.WS_CLIPCHILDREN)
            except Exception:
                pass

            if not hasattr(self, "_original_ai_style") or getattr(self, "_original_ai_style", None) is None:
                self._original_ai_style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                self._original_ai_ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)

            # Strip caption/borders to embed as child control
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            style &= ~(win32con.WS_CAPTION | win32con.WS_THICKFRAME |
                       win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX |
                       win32con.WS_SYSMENU | win32con.WS_POPUP | win32con.WS_BORDER)
            style |= win32con.WS_CHILD
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)

            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex_style &= ~(win32con.WS_EX_DLGMODALFRAME | win32con.WS_EX_APPWINDOW |
                          win32con.WS_EX_WINDOWEDGE | win32con.WS_EX_CLIENTEDGE)
            ex_style |= win32con.WS_EX_TOOLWINDOW
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

            win32gui.SetParent(hwnd, tk_hwnd)
            self._ai_hwnd = hwnd
            self._ai_is_embedded = True
            self._ai_embed_ready_time = time.time()
            self._ai_embed_poll_job = None

            # Reparenting an already-created WebView2 window does not always
            # trigger a native WM_SIZE/paint pass. Force an initial show and a
            # few inexpensive deferred resizes so first launch behaves exactly
            # like the previously-working hide/show cycle.
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                win32gui.RedrawWindow(
                    hwnd, None, None,
                    win32con.RDW_INVALIDATE | win32con.RDW_UPDATENOW |
                    win32con.RDW_ALLCHILDREN,
                )
            except Exception:
                pass
            self._apply_ai_embed_size(force=True)
            for delay in (50, 150, 350):
                try:
                    self.root.after(delay, lambda: self._apply_ai_embed_size(force=True))
                except Exception:
                    pass
            self._update_ai_button_label()
        except Exception as e:
            print(f"[MCU Flasher] Error embedding AI window: {e}")

    def _resize_embedded_ai(self, event=None):
        if not getattr(self, "_ai_hwnd", None) or not getattr(self, "_ai_is_embedded", False):
            return
        if win32gui is None or win32con is None:
            return
        try:
            if not win32gui.IsWindow(self._ai_hwnd):
                self._ai_hwnd = None
                return
            if hasattr(self, "root") and self.root:
                # <Configure> can fire while the paned-window layout is still
                # settling (pane insertion, sash drags, first show), leaving
                # the embedded window a few pixels short of the frame.  Re-read
                # the frame's FINAL size after the idle layout pass and apply.
                self.root.after_idle(self._apply_ai_embed_size)
            else:
                self._apply_ai_embed_size()
        except Exception:
            pass

    def _apply_ai_embed_size(self, force=False):
        if not getattr(self, "_ai_hwnd", None) or not getattr(self, "_ai_is_embedded", False):
            return
        if win32gui is None or win32con is None:
            return
        try:
            if not win32gui.IsWindow(self._ai_hwnd):
                self._ai_hwnd = None
                return
            frame = self.ai_embed_frame
            w = max(frame.winfo_width(), 50)
            h = max(frame.winfo_height(), 50)
            # During first attachment force SetWindowPos even when the outer
            # dimensions happen to match; WebView2 still needs the WM_SIZE paint.
            left, top, right, bottom = win32gui.GetWindowRect(self._ai_hwnd)
            if force or right - left != w or bottom - top != h:
                win32gui.SetWindowPos(
                    self._ai_hwnd, 0, 0, 0, w, h,
                    win32con.SWP_FRAMECHANGED | win32con.SWP_NOZORDER |
                    win32con.SWP_SHOWWINDOW | 0x4000
                )
            self._force_ai_webview_fill(force=force)
        except Exception:
            pass

    def _force_ai_webview_fill(self, force=False):
        """Force the pywebview child control(s) to fill the AI window client area.

        The WebView2 control inside the pywebview WinForms host uses
        Dock=Fill; after reparenting and stripping the caption, WinForms can
        keep a stale layout, leaving a dead strip at the bottom/right of the
        embedded view. Check each child HWND and resize so it fills the full container.
        """
        if not getattr(self, "_ai_hwnd", None) or win32gui is None:
            return
        try:
            if not win32gui.IsWindow(self._ai_hwnd):
                self._ai_hwnd = None
                return
            frame = getattr(self, "ai_embed_frame", None)
            fw = frame.winfo_width() if frame else 0
            fh = frame.winfo_height() if frame else 0
            cr = win32gui.GetClientRect(self._ai_hwnd)
            cw, ch = cr[2], cr[3]
            target_w = max(fw, cw)
            target_h = max(fh, ch)

            child_rects = []

            def _collect(hwnd, lparam):
                try:
                    r = win32gui.GetWindowRect(hwnd)
                    child_rects.append((hwnd, r[2] - r[0], r[3] - r[1]))
                except Exception:
                    pass
                return True

            win32gui.EnumChildWindows(self._ai_hwnd, _collect, None)
            mismatched = [(hwnd,) for hwnd, cwd, chd in child_rects
                          if force or cwd != target_w or chd != target_h]
            for (hwnd,) in mismatched:
                win32gui.SetWindowPos(
                    hwnd, 0, 0, 0, target_w, target_h,
                    win32con.SWP_NOZORDER | win32con.SWP_SHOWWINDOW | 0x4000
                )
        except Exception:
            pass

    def _start_ai_size_watchdog(self):
        """While the AI side panel is visible, verify every 500 ms that the
        embedded window truly fills its container and re-sync if any event
        (missed <Configure>, show/hide toggle, sash drag) left it short."""
        if not hasattr(self, "root") or not self.root:
            return
        def _watch():
            if not getattr(self, "_ai_side_visible", False):
                self._ai_size_watchdog_job = None
                return
            try:
                if getattr(self, "_ai_hwnd", None) and win32gui is not None:
                    if not win32gui.IsWindow(self._ai_hwnd):
                        self._ai_hwnd = None
                    else:
                        frame = self.ai_embed_frame
                        w = max(frame.winfo_width(), 50)
                        h = max(frame.winfo_height(), 50)
                        left, top, right, bottom = win32gui.GetWindowRect(self._ai_hwnd)
                        if right - left != w or bottom - top != h:
                            self._apply_ai_embed_size()
                        else:
                            self._force_ai_webview_fill()
            except Exception:
                pass
            try:
                self._ai_size_watchdog_job = self.root.after(500, _watch)
            except Exception:
                self._ai_size_watchdog_job = None
        self._ai_size_watchdog_job = self.root.after(500, _watch)
    def _on_ai_applied_edit(
        self,
        filepath=None,
        before_content=None,
        after_content=None,
        before_exists=True,
        after_exists=True,
    ):
        """Triggered when OpenCode AI applies an edit to any file in the workspace.
        Invokes the Reload button to automatically update the editor view,
        and posts a notification so the user can see what the AI changed.
        """
        review_notification_state = {"queued": None}

        def _do_reload():
            if getattr(self, "editor_mode", "default") == "monaco" and hasattr(self, "editor_window") and self.editor_window:
                try:
                    import json
                    js_path = str(filepath) if filepath else ""
                    if (filepath and hasattr(self, "editor_api") and self.editor_api
                            and before_content is not None and after_content is not None):
                        queue_result = self.editor_api.queue_ai_edit_snapshot(
                            filepath,
                            before_content,
                            after_content,
                            before_exists,
                            after_exists,
                        )
                        if queue_result == "cancelled":
                            review_notification_state["queued"] = False
                            self.editor_window.evaluate_js(
                                "onAiReviewCancelled("
                                f"{json.dumps(js_path)}, {json.dumps(bool(after_exists))})"
                            )
                            return
                        if not queue_result:
                            review_notification_state["queued"] = False
                            return
                        review_notification_state["queued"] = True
                    self.editor_window.evaluate_js(f"reloadActiveFileWithDiff({json.dumps(js_path)})")
                    return
                except Exception as exc:
                    review_notification_state["queued"] = False
                    print(f"[MCU Flasher] AI review bridge error: {exc}")
                    if hasattr(self, "_append_notif"):
                        self._append_notif(
                            f"  AI review could not open: {exc}",
                            "error",
                            category="system",
                            title="AI review error",
                        )
            if hasattr(self, "btn_reload_file") and self.btn_reload_file:
                try:
                    self.btn_reload_file.invoke()
                    return
                except Exception:
                    pass
            if hasattr(self, "_reload_current_editor_file") and callable(self._reload_current_editor_file):
                self._reload_current_editor_file()

        def _do_notify():
            if review_notification_state["queued"] is False:
                return
            if hasattr(self, "_append_notif"):
                if filepath:
                    try:
                        fname = Path(filepath).name
                    except Exception:
                        fname = str(filepath)
                    self._append_notif(
                        f"  AI edit awaiting review: {fname}",
                        "warning",
                        category="system",
                        title=f"Review AI edit: {fname}",
                    )
                else:
                    self._append_notif(
                        "  AI edit awaiting review.",
                        "warning",
                        category="system",
                        title="Review AI edit",
                    )

        if hasattr(self, "root") and self.root:
            self.root.after(0, _do_reload)
            self.root.after(50, _do_notify)

