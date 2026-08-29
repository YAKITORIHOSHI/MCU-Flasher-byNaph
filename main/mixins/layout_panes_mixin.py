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

class LayoutPanesMixin(_Base):
    """Mixin providing LayoutPanesMixin capabilities for MCUUploadGUI."""
    def _update_editor_info(self, cursor_pos: str = None):
        """Update editor statistics without scanning project files on Tk."""
        label = getattr(self, "editor_info_label", None)
        if label is None:
            return
        try:
            if not label.winfo_exists():
                return
        except Exception:
            return

        mode = getattr(self, "editor_mode", "default")
        project_dir = Path(getattr(self, "sketch_dir_path", ""))
        active_path = None
        tabs_count = 0
        active_cursor = cursor_pos
        active_lines_from_ui = None

        # Capture cheap widget facts on Tk, then leave all disk work to a worker.
        if mode == "default":
            try:
                notebook = getattr(self, "editor_notebook", None)
                if notebook is not None and notebook.winfo_exists():
                    tabs_count = int(notebook.index("end"))
                    active_tab = notebook.select()
                    data = getattr(self, "editor_tab_data", {}).get(active_tab)
                    if data:
                        text_widget = data["text"]
                        if active_cursor is None:
                            ln, col = text_widget.index(tk.INSERT).split(".")
                            active_cursor = f"Ln {ln}, Col {int(col) + 1}"
                        active_lines_from_ui = int(text_widget.index("end-1c").split(".")[0])
            except Exception:
                pass
        elif mode == "monaco":
            try:
                api = getattr(self, "editor_api", None)
                active_path = getattr(api, "active_file_path", None) if api else None
            except Exception:
                active_path = None

        generation = getattr(self, "_editor_info_generation", 0) + 1
        self._editor_info_generation = generation

        def _collect():
            files_count = 0
            try:
                if project_dir and project_dir.exists():
                    files = get_project_root_source_files(
                        project_dir, (".ino", ".cpp", ".h", ".c", ".txt")
                    )
                    files_count = len(files)
            except Exception:
                pass

            active_info = ""
            if mode == "default":
                if active_lines_from_ui is not None:
                    active_info = f" | {active_cursor} ({active_lines_from_ui} lines)"
                text = f"Editor: Default | Tabs: {tabs_count}{active_info}"
            elif mode == "monaco":
                if active_path:
                    try:
                        path = Path(active_path)
                        if path.exists():
                            line_count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
                            active_info = f" | {path.name} ({line_count} lines)"
                    except Exception:
                        pass
                text = f"Editor: Monaco | Files: {files_count}{active_info}"
            else:
                text = ""
            return generation, text

        def _apply(result):
            result_generation, text = result
            if result_generation != getattr(self, "_editor_info_generation", result_generation):
                return
            try:
                if self.editor_info_label.winfo_exists():
                    self.editor_info_label.configure(text=text)
            except Exception:
                pass

        self._run_bg_task(_collect, on_success=_apply)

    def _set_serial_status(self, connected):
        if connected is True or connected == "connected":
            state = "connected"
        elif str(connected).lower().startswith("reconnect"):
            state = "reconnecting"
        else:
            state = "disconnected"
        self._serial_status_state = state

        def _do(expected_state=state):
            # Render the newest state only; an old queued callback must not
            # overwrite a newer connection transition.
            st = getattr(self, "_serial_status_state", expected_state)
            if st == "connected":
                self.serial_status.configure(text="● Connected", fg=Theme.GREEN)
            elif st == "reconnecting":
                self.serial_status.configure(text="● Reconnecting...", fg=Theme.YELLOW)
            else:
                self.serial_status.configure(text="● Disconnected", fg=Theme.RED)
        self._post_ui(_do)

    def _set_buttons_busy(self, busy: bool):
        """Disable/enable action buttons during operations (legacy helper).
        Delegates to _set_buttons_state with operation='any'."""
        self._set_buttons_state(busy, operation="any")

    def _set_buttons_state(self, busy: bool, operation: str = "any"):
        previous_operation = getattr(self, "_active_operation", None)
        entering_busy = bool(busy and previous_operation is None)
        self._active_operation = operation if busy else None
        def _do():
            if busy:
                
                self.btn_compile.configure(state=tk.DISABLED)
                self.btn_upload.configure(state=tk.DISABLED)
                self.btn_new_project.configure(state=tk.DISABLED)
                self.btn_settings.configure(state=tk.DISABLED)
                # If compact OPTIONS was already open, discard the stale popup.
                # Reopening it while busy rebuilds the menu from the real button
                # states, so Settings appears disabled there as well.
                self._close_options_dropdown()
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
                if hasattr(self, "serial_baud_combo"):
                    if operation in ("upload", "flash", "reset"):
                        self.serial_baud_combo.configure(state="disabled")
                    else:
                        self.serial_baud_combo.configure(state="readonly")
                self.upload_speed_combo.configure(state="disabled")
                self.btn_clean.configure(state=tk.DISABLED)

                # STOP: enabled during compile and upload phase 1 (building only,
                # safe to cancel). DISABLED during flash/reset (direct flash write
                # — brick risk) and generic fallback.
                if operation in ("compile", "upload"):
                    self.btn_stop.configure(state=tk.NORMAL)
                    if hasattr(self, "bottom_notebook"):
                        self.bottom_notebook.tab(self._serial_monitor_tab_index(), state="normal")
                        if entering_busy:
                            self.bottom_notebook.select(0)  # Switch once at operation start
                elif operation in ("flash", "reset"):
                    self.btn_stop.configure(state=tk.DISABLED)
                    if hasattr(self, "bottom_notebook"):
                        self.bottom_notebook.tab(self._serial_monitor_tab_index(), state="disabled")
                        if entering_busy:
                            self.bottom_notebook.select(0)
                        self._set_window_closable(False)
                else:
                    self.btn_stop.configure(state=tk.DISABLED)

                self.lbl_sketch.configure(cursor="arrow")

                # Visual label so the user knows which op is active
                is_compact = getattr(self, "_action_compact_mode", False)
                if operation == "compile":
                    self.btn_compile.configure(text="Compiling..." if is_compact else "⚙ Compiling...")
                    self.btn_upload.configure(text="Upload" if is_compact else "⚡ Upload")
                elif operation in ("upload", "flash"):
                    self.btn_compile.configure(text="Compile" if is_compact else "⚙ Compile")
                    self.btn_upload.configure(text="Uploading..." if is_compact else "⡿ Uploading...")
                elif operation == "reset":
                    self.btn_compile.configure(text="Compile" if is_compact else "⚙ Compile")
                    self.btn_upload.configure(text="Resetting..." if is_compact else "⡿ Resetting...")
                elif operation == "clean":
                    self.btn_compile.configure(text="Compile" if is_compact else "⚙ Compile")
                    self.btn_upload.configure(text="Upload" if is_compact else "⚡ Upload")
                    self.btn_clean.configure(text="Cleaning..." if is_compact else "🧹 Cleaning...")
            else:
                self._framework_download_active = False
                is_compact = getattr(self, "_action_compact_mode", False)
                self.btn_compile.configure(state=tk.NORMAL, text="Compile" if is_compact else "⚙ Compile")
                self.btn_upload.configure(state=tk.NORMAL, text="Upload" if is_compact else "⚡ Upload")
                self.btn_new_project.configure(state=tk.NORMAL)
                self.btn_settings.configure(state=tk.NORMAL)
                self.btn_stop.configure(state=tk.DISABLED, text="Stop" if is_compact else "■ Stop")
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
                    current_tab = self.bottom_notebook.select()
                    serial_index = self._serial_monitor_tab_index()
                    was_locked = self.bottom_notebook.tab(serial_index, "state") == "disabled"
                    self.bottom_notebook.tab(serial_index, state="normal")
                    target_tab = self._focus_tab_on_unlock
                    self._focus_tab_on_unlock = None  # one-shot, always consume
                    if target_tab is not None:
                        self.bottom_notebook.select(target_tab)
                    elif was_locked:
                        # Keep a user-selected Compatible Devices/Notifications tab.
                        # Only fall back when the selected tab itself was the one locked.
                        try:
                            if current_tab and self.bottom_notebook.index(current_tab) == serial_index:
                                self.bottom_notebook.select(0)
                        except Exception:
                            self.bottom_notebook.select(0)
                    if self._compatible_devices_is_selected():
                        self.root.after_idle(self._repair_compatible_devices_interaction)
                
                # Re-enable board/ports/baud selection
                self.board_combo.configure(state="readonly")
                self.port_combo.configure(state="readonly")
                if hasattr(self, "serial_baud_combo"):
                    self.serial_baud_combo.configure(state="readonly")
                
                # If board is AVR, keep upload speed combo disabled, else readonly
                board_name = self.board_var.get()
                board_info = SUPPORTED_BOARDS.get(board_name, {})
                is_avr = (board_info.get("platform", "") == "atmelavr")

                if is_avr:
                    self.upload_speed_combo.configure(state="disabled")
                else:
                    self.upload_speed_combo.configure(state="readonly")

                # Re-enable Clean only when there is actually something to
                # clean. Unconditionally forcing NORMAL here used to re-arm
                # the button right after a successful Clean, letting users
                # "clean" repeatedly with nothing left to remove.
                self._update_clean_button_state()
                
                self.lbl_sketch.configure(cursor="hand2")
                
                # Always safe to restore closability once we're back to idle
                self._set_window_closable(True)

                # The block above unconditionally re-enabled Compile/Upload;
                # re-apply the board-selected (and, for Upload, hardware-
                # recognized & port-present) gating now that is_busy is back to False.
                self._update_hardware_action_buttons()
                self._refresh_ports(called_from_hotplug=True)

            self._sync_detached_compact_actions()
                
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

    def _toggle_editor_detachment(self):
        if getattr(self, "editor_detached", False):
            self._attach_editor()
        else:
            self._detach_editor()

    def _sync_ai_and_editor_layout(self):
        """Synchronize the main window layout based on whether the code editor is detached
        and whether the OpenCode AI Assistant side panel is visible.

        Requirements:
        - When editor is detached:
          1. Hide editor frame on the main window.
          2. If AI Assistant is visible, configure h_split_pane to orient=VERTICAL,
             stacking OpenCode AI Assistant (top) and Build Console (bottom) in two rows.
        - When editor is attached:
          1. Preserve the user's editor visibility choice.
          2. Configure h_split_pane to orient=HORIZONTAL, placing OpenCode AI Assistant
             in a right-side column alongside main_pane.
        """
        detached = getattr(self, "editor_detached", False)
        ai_visible = getattr(self, "_ai_side_visible", False)

        if not hasattr(self, "h_split_pane") or not hasattr(self, "main_pane") or not hasattr(self, "ai_side_container"):
            return

        if detached:
            # 1. Hide editor frame from main_pane when editor is detached
            if hasattr(self, "editor_frame"):
                try:
                    if self.editor_frame in self.main_pane.panes() or str(self.editor_frame) in [str(p) for p in self.main_pane.panes()]:
                        self.main_pane.forget(self.editor_frame)
                except Exception:
                    pass
            self.editor_pane_visible = False
            if hasattr(self, "btn_toggle_editor") and self.btn_toggle_editor and self.btn_toggle_editor.winfo_exists():
                self.btn_toggle_editor.configure(text="🗖 Show Editor")

            # 2. When detached and AI Assistant is visible, stack OpenCode and Build Console in two VERTICAL rows
            if ai_visible:
                try:
                    self.h_split_pane.configure(orient=tk.VERTICAL)
                    for p in list(self.h_split_pane.panes()):
                        self.h_split_pane.forget(p)

                    root_h = max(400, self.root.winfo_height()) if hasattr(self, "root") else 600
                    calc_h = max(180, int(root_h * 0.45))
                    self.h_split_pane.add(self.ai_side_container, height=calc_h)
                    self.h_split_pane.add(self.main_pane, stretch="always")
                except Exception:
                    pass
            else:
                # AI is hidden: ensure main_pane fills h_split_pane
                try:
                    for p in list(self.h_split_pane.panes()):
                        if str(p) != str(self.main_pane) and p != self.main_pane:
                            self.h_split_pane.forget(p)
                    if self.main_pane not in self.h_split_pane.panes() and str(self.main_pane) not in [str(p) for p in self.h_split_pane.panes()]:
                        self.h_split_pane.add(self.main_pane, stretch="always")
                except Exception:
                    pass
        else:
            # Editor is ATTACHED
            editor_should_be_visible = getattr(self, "editor_pane_visible", True)
            # 1. Configure h_split_pane orientation back to HORIZONTAL
            try:
                self.h_split_pane.configure(orient=tk.HORIZONTAL)
                for p in list(self.h_split_pane.panes()):
                    self.h_split_pane.forget(p)

                self.h_split_pane.add(self.main_pane, stretch="always")
            except Exception:
                pass

            # 2. Restore or keep the editor frame according to the user's
            # current Hide/Show Editor choice.  Previously this branch always
            # re-added the editor, so opening AI after hiding it left an empty
            # editor-sized region above the build console.
            if hasattr(self, "editor_frame") and editor_should_be_visible:
                try:
                    main_panes = [str(p) for p in self.main_pane.panes()]
                    if str(self.editor_frame) not in main_panes and self.editor_frame not in self.main_pane.panes():
                        if hasattr(self, "bottom_frame") and (str(self.bottom_frame) in main_panes or self.bottom_frame in self.main_pane.panes()):
                            self.main_pane.add(self.editor_frame, before=self.bottom_frame,
                                                minsize=getattr(self, "_editor_minsize", 100),
                                                height=getattr(self, "_editor_height", 400))
                        else:
                            self.main_pane.add(self.editor_frame,
                                                minsize=getattr(self, "_editor_minsize", 100),
                                                height=getattr(self, "_editor_height", 400))
                except Exception:
                    pass
            elif hasattr(self, "editor_frame") and not editor_should_be_visible:
                try:
                    if self.editor_frame in self.main_pane.panes() or str(self.editor_frame) in [str(p) for p in self.main_pane.panes()]:
                        self.main_pane.forget(self.editor_frame)
                except Exception:
                    pass
            self.editor_pane_visible = editor_should_be_visible
            if hasattr(self, "btn_toggle_editor") and self.btn_toggle_editor and self.btn_toggle_editor.winfo_exists():
                self.btn_toggle_editor.configure(
                    text="🗖 Hide Editor" if editor_should_be_visible else "🗖 Show Editor"
                )

            # 3. If AI Assistant is visible, place it on the right column of h_split_pane
            if ai_visible:
                try:
                    h_panes = [str(p) for p in self.h_split_pane.panes()]
                    if str(self.ai_side_container) not in h_panes and self.ai_side_container not in self.h_split_pane.panes():
                        root_w = max(480, self.root.winfo_width()) if hasattr(self, "root") else 800
                        calc_w = max(220, int(root_w * 0.44))
                        calc_w = min(calc_w, max(220, root_w - 300))
                        self.h_split_pane.add(self.ai_side_container, width=calc_w, stretch="always")
                except Exception:
                    pass

        # Trigger AI WebView resize update after layout change
        if ai_visible and hasattr(self, "_resize_embedded_ai"):
            try:
                self._resize_embedded_ai()
            except Exception:
                pass

    def _detach_editor(self):
        mode = getattr(self, "editor_mode", "default")
        if mode == "monaco":
            if not sys.platform == "win32" or win32gui is None:
                return
            hwnd = getattr(self, "_editor_hwnd", None)
            if not hwnd:
                self._append("  ⚠ Code editor is not loaded yet.", "warning")
                return
            
            # Save original styles before stripping, if not done already
            if not hasattr(self, "_original_editor_style") or getattr(self, "_original_editor_style", None) is None:
                try:
                    self._original_editor_style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                    self._original_editor_ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                except Exception:
                    pass

            # Save timestamp for grace period in _poll_detached_window
            self._detach_timestamp = time.time()

            # 1. Reparent to desktop (0)
            try:
                win32gui.SetParent(hwnd, 0)
            except Exception:
                pass
            
            # 2. Restore styles
            if getattr(self, "_original_editor_style", None) is not None:
                try:
                    win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, self._original_editor_style)
                    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, self._original_editor_ex_style)
                except Exception:
                    pass
            
            # 3. Ensure webview control visibility
            try:
                if hasattr(self, "editor_window") and self.editor_window:
                    self.editor_window.show()
            except Exception:
                pass

            # 4. Set position and size to make it visible (centered over main window)
            try:
                parent_x = self.root.winfo_x()
                parent_y = self.root.winfo_y()
                parent_w = self.root.winfo_width()
                parent_h = self.root.winfo_height()
                w, h = 1000, 700
                x = parent_x + (parent_w - w) // 2
                y = parent_y + (parent_h - h) // 2
                win32gui.SetWindowPos(
                    hwnd, 0, max(0, x), max(0, y), w, h,
                    win32con.SWP_FRAMECHANGED | win32con.SWP_SHOWWINDOW
                )
            except Exception:
                pass
                
            self._editor_embedded = False
            self.editor_detached = True
            self._update_detach_button_style()
            
            # 6. Show placeholder in main GUI editor frame
            self._show_detached_placeholder()
            
            # 7. Start polling to re-attach if closed
            self._poll_detached_window()
            
        else: # default editor
            if not hasattr(self, "editor_content_frame") or not self.editor_content_frame:
                return
            
            # Backup default editor state
            self._backup_default_editor_state()
            
            # Destroy old editor widgets in main window
            try:
                self.editor_content_frame.destroy()
            except Exception:
                pass
            self.editor_content_frame = None
            
            # Show the "Editor Detached" placeholder in the MAIN window now,
            # while editor_content_frame is still None. This must happen
            # BEFORE _build_editor_default() is called below, because that
            # call reassigns self.editor_content_frame to point at the new
            # Toplevel's own content frame (it's shared code for both the
            # main window and the popped-out window). If we called
            # _show_detached_placeholder() afterwards instead, it would
            # pack_forget() the freshly-built Toplevel content rather than
            # the old main-window content — leaving the detached window
            # blank while looking fine when re-attached.
            self.editor_detached = True
            self._update_detach_button_style()
            
            # Ensure the editor pane is visible so the placeholder appears
            if not self.editor_pane_visible:
                self.main_pane.add(self.editor_frame, minsize=self._editor_minsize, height=self._editor_height)
                self.editor_pane_visible = True
                self.btn_toggle_editor.configure(text="🗖 Hide Editor")
            
            self._show_detached_placeholder()
            
            # 1. Create Toplevel — independent standalone window with maximize/restore
            self.default_editor_toplevel = tk.Toplevel(self.root)
            self.default_editor_toplevel.title("MCU Flasher — Code Editor")
            self.default_editor_toplevel.configure(bg=Theme.BG_DARKEST)
            self.default_editor_toplevel.resizable(True, True)
            self.default_editor_toplevel.minsize(500, 350)

            # Set window icon if available
            try:
                icon_path = SCRIPT_DIR / "src" / "assets" / "mcu_icon.ico"
                if not icon_path.exists():
                    icon_path = SCRIPT_DIR / "src" / "mcu_icon.ico"
                if icon_path.exists():
                    self.default_editor_toplevel.iconbitmap(str(icon_path))
            except Exception:
                pass

            # Center the toplevel
            center_toplevel(self.default_editor_toplevel, self.root, 1000, 700)

            # Rebuild default editor notebook inside the new Toplevel
            self._build_detached_editor_toolbar(self.default_editor_toplevel)
            self._build_editor_default(self.default_editor_toplevel)

            # Map the window — no deiconify/lift/focus_force needed for a
            # normal Toplevel; those are for popup dialogs that start withdrawn.
            self.default_editor_toplevel.update()

            # Bind close event to re-attach
            self.default_editor_toplevel.protocol("WM_DELETE_WINDOW", self._attach_editor)

        self.root.after_idle(self._apply_dynamic_button_scale)
        self._sync_ai_and_editor_layout()
        self._append_notif("  ✓ Code editor detached to separate window.", "success")

    def _attach_editor(self):
        """Re-embed the detached editor into the main window.

        Guarded against re-entrancy: both the pywebview ``on_closing``
        event (fires on the WinForms thread) and the Tk-side
        ``_poll_detached_window`` timer can trigger this near-simultaneously.
        Only the first caller proceeds; subsequent calls bail out.
        """
        if getattr(self, "_is_attaching_editor", False):
            return
        self._is_attaching_editor = True
        # Cancel any pending poll timer so it cannot fire a duplicate
        # _attach_editor() call while we are mid-attach.
        poll_id = getattr(self, "_poll_detached_after_id", None)
        if poll_id is not None:
            try:
                self.root.after_cancel(poll_id)
            except Exception:
                pass
            self._poll_detached_after_id = None
        try:
            self._attach_editor_impl()
        finally:
            self._is_attaching_editor = False

    def _attach_editor_impl(self):
        """Internal attach logic — always called via _attach_editor()."""
        mode = getattr(self, "editor_mode", "default")
        if mode == "monaco":
            if not sys.platform == "win32" or win32gui is None:
                return
            hwnd = getattr(self, "_editor_hwnd", None)
            if not hwnd:
                return
                
            # Hide placeholder
            if hasattr(self, "_editor_placeholder"):
                self._editor_placeholder.place_forget()

            # 0. Hide the detached window and reclaim OS focus BEFORE
            #    reparenting.  This prevents Windows from dispatching
            #    synchronous cross-thread WM_ACTIVATE / WM_WINDOWPOSCHANGED
            #    messages during SetParent(), which is the primary deadlock
            #    trigger when the AI Assistant WebView is also active.
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            except Exception:
                pass
            try:
                self.root.focus_force()
            except Exception:
                pass

            # 1. Reparent back to editor frame
            try:
                frame = self._editor_embed_frame
                frame.update_idletasks()
                frame.update()
                tk_hwnd = frame.winfo_id()
                
                # Set WS_CLIPCHILDREN on parent Tk frame to isolate child rendering
                tk_style = win32gui.GetWindowLong(tk_hwnd, win32con.GWL_STYLE)
                win32gui.SetWindowLong(tk_hwnd, win32con.GWL_STYLE, tk_style | win32con.WS_CLIPCHILDREN)

                # Strip styles again to embed
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
                
                # Resize — flush pending Tk geometry before measuring
                frame.update_idletasks()
                w = max(frame.winfo_width(), 50)
                h = max(frame.winfo_height(), 50)
                win32gui.SetWindowPos(
                    hwnd, 0, 0, 0, w, h,
                    win32con.SWP_FRAMECHANGED | win32con.SWP_NOZORDER | win32con.SWP_SHOWWINDOW | 0x4000
                )
            except Exception:
                pass
                
            self._editor_embedded = True
            self.editor_detached = False
            self._update_detach_button_style()

            # Let Tk's layout settle, then sync the editor size one more time
            self.root.after(200, lambda: self._resize_embedded_editor())
            
        else: # default editor
            # Backup default editor state from Toplevel window
            self._backup_default_editor_state()
            
            if hasattr(self, "default_editor_toplevel") and self.default_editor_toplevel:
                try:
                    self.default_editor_toplevel.destroy()
                except Exception:
                    pass
                self.default_editor_toplevel = None
                
            if hasattr(self, "editor_content_frame") and self.editor_content_frame:
                try:
                    self.editor_content_frame.destroy()
                except Exception:
                    pass
                self.editor_content_frame = None
                
            if hasattr(self, "_default_placeholder_frame"):
                try:
                    self._default_placeholder_frame.destroy()
                except Exception:
                    pass
                self._default_placeholder_frame = None

            # Ensure the editor pane is visible in the main window so the
            # rebuilt editor container is actually shown. _detach_editor
            # already guarantees this on the way out, but _attach_editor can
            # fire from a Toplevel close callback after the pane was hidden
            # mid-session — without this check the editor rebuilds into a
            # frame that is no longer mapped inside main_pane and silently
            # vanishes.
            if not self.editor_pane_visible:
                self.main_pane.add(self.editor_frame, minsize=self._editor_minsize, height=self._editor_height)
                self.editor_pane_visible = True
                self.btn_toggle_editor.configure(text="🗖 Hide Editor")

            # Rebuild default editor inside the main window editor_frame
            self._build_editor_default(self.editor_frame)
            self.editor_detached = False
            self._update_detach_button_style()

            # When both panes were hidden at the time of re-attach, toggle
            # buttons may be stale. Update them now so the user can operate
            # the editor they just recovered.
            self._update_pane_toggle_buttons()
            
        self.root.after_idle(self._apply_dynamic_button_scale)
        # Defer layout sync so Tk geometry and the Win32 reparent fully
        # settle before the PanedWindow orientation / AI container are
        # reshuffled — avoids DWM paint deadlocks with two WebView2 HWNDs.
        self.root.after(100, self._sync_ai_and_editor_layout)
        self._append_notif("  ✓ Code editor re-attached to the main window.", "success")

    def _close_detached_editor_window(self):
        """If the code editor is currently detached in a separate window,
        dispose/close that window cleanly before main GUI shutdown."""
        if not getattr(self, "editor_detached", False):
            return

        mode = getattr(self, "editor_mode", "default")
        if mode == "monaco":
            # Destroy pywebview window asynchronously
            if hasattr(self, "editor_window") and self.editor_window:
                try:
                    self.editor_window.destroy()
                except Exception:
                    pass
            # Post WM_CLOSE asynchronously (NEVER use SendMessage which deadlocks the Tk thread)
            hwnd = getattr(self, "_editor_hwnd", None)
            if hwnd and win32gui is not None:
                try:
                    if win32con is not None:
                        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                except Exception:
                    pass
        else:
            if hasattr(self, "default_editor_toplevel") and self.default_editor_toplevel:
                try:
                    self.default_editor_toplevel.destroy()
                except Exception:
                    pass
                self.default_editor_toplevel = None

        self.editor_detached = False
        self._editor_embedded = False

    def _build_detached_editor_toolbar(self, parent):
        """Add action controls to a detached Default editor window.

        Tk does not allow existing widgets to be re-parented into a sibling
        Toplevel.  These buttons call the same actions as their main-window
        counterparts, so the detached editor remains fully usable.
        """
        bar = tk.Frame(parent, bg=Theme.BG_DARK, padx=8, pady=6)
        bar.pack(fill=tk.X)
        tk.Label(bar, text="ACTIONS", font=self.font_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_DARK).pack(side=tk.LEFT, padx=(0, 6))
        self._detached_buttons_dict = {}
        actions = [
            ("compile", "Compile", self._do_compile, Theme.BTN_COMPILE, Theme.BTN_COMPILE_H),
            ("upload", "Upload", self._do_upload, Theme.BTN_FULL, Theme.BTN_FULL_H),
            ("stop", "Stop", self._do_stop, Theme.BTN_STOP, Theme.BTN_STOP_H),
            ("clean", "Clean", self._do_clean, Theme.BTN_CLEAR, Theme.BTN_CLEAR_H),
            ("save", "Save", self._trigger_save, Theme.BTN_COMPILE, Theme.BTN_COMPILE_H),
            ("save_all", "Save All", self._trigger_save_all, Theme.BTN_FULL, Theme.BTN_FULL_H),
            ("reload", "Reload", self._reload_current_editor_file, Theme.BTN_CLEAR, Theme.BTN_CLEAR_H),
            ("modify", "Modify", self._open_modify_files_dialog, Theme.BTN_MONITOR, Theme.BTN_MONITOR_H),
        ]
        for index, (key, label, command, bg, hover) in enumerate(actions):
            if index == 4:
                tk.Frame(bar, bg=Theme.BORDER, width=2, height=22).pack(
                    side=tk.LEFT, padx=7
                )
            btn = self._make_btn(bar, label, command, bg, hover, font=self.font_label)
            btn.pack(side=tk.LEFT, padx=2)
            self._detached_buttons_dict[key] = btn
        self._detached_editor_toolbar = bar
        self._sync_detached_compact_actions()

    def _sync_detached_compact_actions(self, width=None):
        """Put actions in the detached editor whenever it is detached and sync button states."""
        if width is None:
            try:
                width = self.root.winfo_width()
            except Exception:
                width = 0
        active = bool(getattr(self, "editor_detached", False))

        # Default editor: its detached window is Tk, so show/hide its local
        # toolbar directly.
        toolbar = getattr(self, "_detached_editor_toolbar", None)
        try:
            if toolbar and toolbar.winfo_exists():
                if active and not toolbar.winfo_ismapped():
                    toolbar.pack(fill=tk.X, before=getattr(self, "editor_content_frame", None))
                elif not active and toolbar.winfo_ismapped():
                    toolbar.pack_forget()
        except Exception:
            pass

        busy = getattr(self, "is_busy", False)
        operation = getattr(self, "_active_operation", None)

        stop_enabled = bool(busy and operation in ("compile", "upload"))
        actions_enabled = not busy

        # Update Default Editor detached toolbar buttons if present
        detached_btns = getattr(self, "_detached_buttons_dict", {})
        if detached_btns:
            for act, btn in detached_btns.items():
                try:
                    if not btn or not btn.winfo_exists():
                        continue
                    if act == "stop":
                        btn.configure(state=tk.NORMAL if stop_enabled else tk.DISABLED)
                    elif act in ("compile", "upload", "clean"):
                        btn.configure(state=tk.NORMAL if actions_enabled else tk.DISABLED)
                except Exception:
                    pass

        # Monaco is a native WebView window. Its page owns an equivalent bar
        # and calls EditorApi.run_action(), keeping the controls in the actual
        # detached editor window rather than in a separate helper window.
        if getattr(self, "editor_mode", "default") == "monaco":
            try:
                if hasattr(self, "editor_window") and self.editor_window:
                    self.editor_window.evaluate_js(
                        "window.setDetachedActionBar && window.setDetachedActionBar(" +
                        ("true" if active else "false") + ")"
                    )
                    states_json = json.dumps({
                        "compile": actions_enabled,
                        "upload": actions_enabled,
                        "stop": stop_enabled,
                        "clean": actions_enabled,
                        "save": actions_enabled,
                        "save_all": actions_enabled,
                        "reload": actions_enabled,
                        "modify": actions_enabled,
                    })
                    self.editor_window.evaluate_js(
                        f"window.setDetachedButtonStates && window.setDetachedButtonStates({states_json});"
                    )
            except Exception:
                pass

    def _show_detached_placeholder(self):
        # Clear/hide any existing widgets in editor_frame except the placeholder
        if hasattr(self, "editor_content_frame") and self.editor_content_frame:
            self.editor_content_frame.pack_forget()
            
        # If we are in Monaco mode, we have self._editor_placeholder
        # Let's show it with the detach/attach label (no redundant button!)
        if hasattr(self, "_editor_placeholder"):
            self._editor_placeholder.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
            self._editor_status_lbl.configure(text="📝 Editor Detached")
            self._editor_desc_lbl.configure(text="The code editor is running in a separate window.")
            self._editor_fallback_btn.pack_forget()
            self._stop_editor_spinner()
            if hasattr(self, "_editor_spinner_canvas"):
                self._editor_spinner_canvas.pack_forget()
        else:
            # For default editor, let's create a temporary placeholder frame if not exists
            if not hasattr(self, "_default_placeholder_frame"):
                placeholder = tk.Frame(self.editor_frame, bg=Theme.BG_DARKEST)
                self._default_placeholder_frame = placeholder
                
                status_lbl = tk.Label(
                    placeholder,
                    text="📝 Editor Detached",
                    font=tkfont.Font(family="Segoe UI", size=16, weight="bold"),
                    fg=Theme.CYAN, bg=Theme.BG_DARKEST
                )
                status_lbl.pack(pady=10)
                
                desc_lbl = tk.Label(
                    placeholder,
                    text="The code editor is running in a separate window.",
                    font=tkfont.Font(family="Segoe UI", size=10),
                    fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST
                )
                desc_lbl.pack(pady=5)
                
            self._default_placeholder_frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

    def _poll_detached_window(self):
        if not getattr(self, "editor_detached", False) or not self._editor_hwnd:
            self._poll_detached_after_id = None
            return
        # Grace period: allow 1.5s after detaching before checking visibility
        detach_time = getattr(self, "_detach_timestamp", 0)
        if time.time() - detach_time < 1.5:
            self._poll_detached_after_id = self.root.after(300, self._poll_detached_window)
            return
        try:
            import win32gui
            if not win32gui.IsWindowVisible(self._editor_hwnd):
                # The user hid/closed the detached window — re-attach it!
                self._poll_detached_after_id = None
                self._attach_editor()
                return
        except Exception:
            pass
        self._poll_detached_after_id = self.root.after(500, self._poll_detached_window)

    def _update_detach_button_style(self):
        if not hasattr(self, "btn_detach_editor") or not self.btn_detach_editor.winfo_exists():
            return
            
        detached = getattr(self, "editor_detached", False)
        if detached:
            # DETACHED Mode -> orange background, white text
            bg_color = "#e67e22"        # Orange
            bg_hover = "#d35400"        # Darker Orange
            text = "🗗 Attach Editor"
        else:
            # ATTACHED Mode -> green background, white text
            bg_color = "#2d7d46"        # Green
            bg_hover = "#38a058"        # Darker Green
            text = "🗗 Detach Editor"
            
        # Configure button
        self.btn_detach_editor.configure(
            image="",
            text=text,
            bg=bg_color,
            fg="#ffffff",
            activebackground=bg_hover,
            activeforeground="#ffffff",
            compound=tk.NONE
        )
        
        # Re-bind hover events for solid colors
        self.btn_detach_editor.bind("<Enter>", lambda e, c=bg_hover: self.btn_detach_editor.configure(bg=c))
        self.btn_detach_editor.bind("<Leave>", lambda e, c=bg_color: self.btn_detach_editor.configure(bg=c))

    def _print_welcome(self):
        self._append("=" * 56, "header")
        self._append("⚡ MCU Flasher by Naph — ESP32 Upload & Monitor (PIO)", "header")
        self._append("=" * 56, "header")
        self._append("")
        # Kick off a project-folder scan immediately so the user sees
        # detected libs, ini status, and source files right on startup.
        self._on_folder_changed()

