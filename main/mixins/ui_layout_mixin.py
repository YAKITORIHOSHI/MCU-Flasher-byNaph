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
from main.dialogs import _make_toolbar_btn
from main.editor_api import *

if TYPE_CHECKING:
    from main.mcu_flash_gui import MCUUploadGUI
    _Base = MCUUploadGUI
else:
    _Base = object

class UILayoutMixin(_Base):
    """Mixin providing UILayoutMixin capabilities for MCUUploadGUI."""
    def _build_ui(self):
        # Use the 'clam' ttk theme for the whole window right from the start.
        # Native Windows themes can render
        # Notebook tabs, Comboboxes, and Scrollbars using the OS's own visual
        # styling engine and silently ignore ttk style.configure() background/
        # foreground overrides. 'clam' draws everything with ttk's own
        # generic engine, which is the only way our dark colorway (Theme.*)
        # actually reaches these widgets. This must happen before any ttk
        # widget in this window is created or styled.
        ttk.Style().theme_use("clam")

        # ── Title Bar ──
        logical_screen_w = self.screen_w / self._display_scale
        logical_screen_h = self.screen_h / self._display_scale
        is_compact_display = logical_screen_w < 1400 or logical_screen_h < 800
        title_pady = round((6 if is_compact_display else 10) * self._display_scale)
        title_padx = round((10 if is_compact_display else 16) * self._display_scale)
        title_frame = tk.Frame(self.root, bg=Theme.BG_DARK, pady=title_pady, padx=title_padx)
        self.title_frame = title_frame
        title_frame.pack(fill=tk.X)

        self.title_row_top = tk.Frame(title_frame, bg=Theme.BG_DARK)
        self.title_row_top.pack(fill=tk.X)

        self.title_row_bottom = tk.Frame(title_frame, bg=Theme.BG_DARK)

        self.title_left = tk.Frame(self.title_row_top, bg=Theme.BG_DARK)
        self.title_left.pack(side=tk.LEFT)

        # Logo text
        self.lbl_app_title = tk.Label(
            self.title_left, text="⚡ MCU Flasher by Naph",
            font=self.font_title, fg=Theme.CYAN, bg=Theme.BG_DARK,
        )
        self.lbl_app_title.pack(side=tk.LEFT)

        self.title_subtitle_label = None

        # Sketch path on the right (packed first to lock it to the far right)
        self.sketch_frame = tk.Frame(self.title_row_top, bg=Theme.BG_DARK)
        self.sketch_frame.pack(side=tk.RIGHT)

        self.lbl_sketch_icon = tk.Label(
            self.sketch_frame, text="📁 ",
            font=self.font_mono_sm, fg=Theme.TEXT_DIM, bg=Theme.BG_DARK,
            cursor="hand2"
        )
        self.lbl_sketch_icon.pack(side=tk.LEFT)
        self.lbl_sketch_icon.bind("<Button-1>", lambda e: self._open_sketch_in_explorer())
        self.lbl_sketch_icon.bind("<Button-3>", lambda e: self._select_sketch_folder())

        self.lbl_sketch = tk.Label(
            self.sketch_frame, text=self._get_sketch_display_name(),
            font=self.font_mono_sm, fg=Theme.TEXT_DIM, bg=Theme.BG_DARK,
            cursor="hand2"
        )
        self.lbl_sketch.pack(side=tk.LEFT)
        self.lbl_sketch.bind("<Button-1>", lambda e: self._open_sketch_in_explorer())
        self.lbl_sketch.bind("<Button-3>", lambda e: self._select_sketch_folder())
        ToolTip(self.lbl_sketch, lambda: f"{self.sketch_dir_path}  •  Left-click: open in Explorer  •  Right-click: change project folder")
        ToolTip(self.lbl_sketch_icon, lambda: f"{self.sketch_dir_path}  •  Left-click: open in Explorer  •  Right-click: change project folder")

        self._update_sketch_marquee()

        self.btn_new_project = self._make_icon_btn(
            self.sketch_frame, "📁", self._new_project,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, width=3
        )
        self.btn_new_project.pack(side=tk.LEFT, padx=(4, 0))

        download_btn_text = "⬇ Download" if is_compact_display else "⬇ Download Boards/Libraries"
        self.btn_download_mgr = self._make_btn(
            self.sketch_frame, download_btn_text, self._open_download_manager,
            Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, font=self.font_label
        )
        self.btn_download_mgr.pack(side=tk.LEFT, padx=(8, 0))

        # Centered action buttons container with indicator
        self.actions_frame = tk.Frame(self.root, bg=Theme.BG_DARK)
        self.actions_frame.pack(in_=self.title_row_top, side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.top_compact_actions = tk.Frame(self.root, bg=Theme.BG_DARK)
        self.top_compact_inner = tk.Frame(self.top_compact_actions, bg=Theme.BG_DARK)
        self.top_compact_inner.pack(expand=True)
        # Compact mode keeps the two most common actions visible, and puts
        # every other action behind the same kind of lightweight popup used
        # by OPTIONS.  This prevents functionality disappearing on narrow
        # windows while keeping the title bar useful.
        self._actions_dropdown_btn = self._make_btn(
            self.root, "Actions ▾", self._toggle_actions_dropdown,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_btn
        )
        self._actions_dropdown_win = None

        self.inner_actions = tk.Frame(self.actions_frame, bg=Theme.BG_DARK)
        self.inner_actions.pack(expand=True)

        self.lbl_actions_title = tk.Label(
            self.root, text="ACTIONS", font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARK
        )
        self.lbl_actions_title.pack(in_=self.inner_actions, side=tk.LEFT, padx=(0, 8))

        self.btn_compile = self._make_btn(self.root, "⚙ Compile", self._do_compile,
                                           Theme.BTN_COMPILE, Theme.BTN_COMPILE_H)
        self.btn_compile.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)

        self.btn_upload = self._make_btn(self.root, "⚡ Upload", self._do_upload,
                                          Theme.BTN_FULL, Theme.BTN_FULL_H)
        self.btn_upload.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)

        self.btn_stop = self._make_btn(self.root, "■ Stop", self._do_stop,
                                        Theme.BTN_STOP, Theme.BTN_STOP_H)
        self.btn_stop.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)
        self.btn_stop.configure(state=tk.DISABLED)

        self.btn_clean = self._make_btn(self.root, "🧹 Clean", self._do_clean,
                                         Theme.BTN_CLEAR, Theme.BTN_CLEAR_H)
        self.btn_clean.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)

        # Divider frame
        self.title_divider = tk.Frame(self.root, bg=Theme.BORDER, width=2, height=22)
        self.title_divider.pack(in_=self.inner_actions, side=tk.LEFT, padx=8)

        self.btn_save = self._make_btn(
            self.root, "💾 Save", self._trigger_save,
            Theme.BTN_COMPILE, Theme.BTN_COMPILE_H
        )
        self.btn_save.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)

        self.btn_save_all = self._make_btn(
            self.root, "💾 Save All", self._trigger_save_all,
            Theme.BTN_FULL, Theme.BTN_FULL_H
        )
        self.btn_save_all.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)

        self.btn_reload_file = self._make_btn(
            self.root, "↺ Reload", lambda: self._reload_current_editor_file(),
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H
        )
        self.btn_reload_file.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)

        self.btn_modify_files = self._make_btn(
            self.root, "🛠 Modify", self._open_modify_files_dialog,
            Theme.BTN_MONITOR, Theme.BTN_MONITOR_H
        )
        self.btn_modify_files.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)

        # ── Separator ──
        tk.Frame(self.root, bg=Theme.CYAN_DIM, height=2).pack(fill=tk.X)

        # ── Controls Bar ──
        ctrl_frame = tk.Frame(
            self.root, bg=Theme.BG_MID,
            pady=round(8 * self._display_scale),
        )
        self.ctrl_frame = ctrl_frame
        ctrl_frame.pack(fill=tk.X, padx=0)

        self.ctrl_row_top = tk.Frame(ctrl_frame, bg=Theme.BG_MID)
        self.ctrl_row_top.pack(fill=tk.X)

        self.ctrl_row_bottom = tk.Frame(ctrl_frame, bg=Theme.BG_MID)

        # Board selection
        self.board_group = tk.Frame(self.ctrl_row_top, bg=Theme.BG_MID)
        self.board_group.pack(side=tk.LEFT, padx=(round(12 * self._display_scale), 8))

        tk.Label(self.board_group, text="BOARD", font=self.font_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_MID).pack(anchor=tk.W)

        board_row = tk.Frame(self.board_group, bg=Theme.BG_MID)
        board_row.pack(fill=tk.X)

        self._build_board_dropdown(board_row)

        # Port selection
        self.port_group = tk.Frame(self.ctrl_row_top, bg=Theme.BG_MID)
        self.port_group.pack(side=tk.LEFT, padx=(0, 8))

        tk.Label(self.port_group, text="PORT", font=self.font_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_MID).pack(anchor=tk.W)

        port_row = tk.Frame(self.port_group, bg=Theme.BG_MID)
        port_row.pack()

        self.port_var = tk.StringVar()
        # Make the width of the port combobox dynamic to prevent overflow on smaller screens (like 1366x768)
        port_width = 30 if is_compact_display else 45
        self.port_combo = ttk.Combobox(
            port_row, textvariable=self.port_var, width=port_width,
            font=self.font_mono_sm, state="readonly", justify="left",
            postcommand=self._refresh_ports,
        )
        self.port_combo.pack(side=tk.LEFT, padx=(0, 4))
        self.port_combo.bind("<<ComboboxSelected>>", lambda e: self._on_port_changed())
        self.port_combo.bind("<Button-1>", lambda e: safe_reclaim_os_focus(self.port_combo), add="+")

        self._marquee_dir = 1
        self._marquee_pause = 0
        self._board_marquee_dir = 1
        self._board_marquee_pause = 0
        self._start_marquee()

        # Baud rate now lives in the Serial Monitor tab header only.
        # Keep baud_var for internal use (auto_start_monitor, resume_monitor).
        self.baud_var = tk.StringVar(value=str(DEFAULT_BAUD))

        # Upload speed (used for flashing; independent of serial monitor baud)
        self.upload_spd_group = tk.Frame(self.ctrl_row_top, bg=Theme.BG_MID)
        self.upload_spd_group.pack(side=tk.LEFT, padx=(0, 8))
        self._upload_spd_group = self.upload_spd_group  # saved for show/hide

        lbl_upload_spd = tk.Label(self.upload_spd_group, text="UPLOAD SPD", font=self.font_label,
                                  fg=Theme.TEXT_DIM, bg=Theme.BG_MID)
        lbl_upload_spd.pack(anchor=tk.W)
        self._lbl_upload_spd = lbl_upload_spd  # referenced by _apply_responsive_layout

        self.upload_speed_var = tk.StringVar(value=str(DEFAULT_UPLOAD_SPEED))
        self.upload_speed_combo = ttk.Combobox(
            self.upload_spd_group, textvariable=self.upload_speed_var, width=10,
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
            platform = board_info.get("platform", "")
            if platform == "atmelavr":
                return "115200 is the only stable upload speed supported by AVR boards."
            elif platform in ("espressif32", "espressif8266"):
                return "460800 is the recommended stable upload speed for ESP32/ESP8266."
            return "460800 is the recommended stable upload speed."

        ToolTip(lbl_upload_spd, _get_upload_speed_tip)
        ToolTip(self.upload_speed_combo, _get_upload_speed_tip)

        # Action buttons moved to title bar

        # Right side: clear + autoscroll
        self.right_group = tk.Frame(self.root, bg=Theme.BG_MID)
        self.right_group.pack(in_=self.ctrl_row_top, side=tk.RIGHT, padx=(0, round(16 * self._display_scale)))
        self._right_ctrl_group = self.right_group  # saved for upload spd re-packing

        self.lbl_options_title = tk.Label(self.right_group, text="OPTIONS", font=self.font_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_MID)
        self.lbl_options_title.pack(pady=(0, 2))

        self.opt_row = tk.Frame(self.right_group, bg=Theme.BG_MID)
        self.opt_row.pack()

        self.opt_checkboxes_frame = tk.Frame(self.opt_row, bg=Theme.BG_MID)
        self.opt_checkboxes_frame.pack(side=tk.LEFT, padx=(0, 16))

        self.opt_buttons_frame = tk.Frame(self.opt_row, bg=Theme.BG_MID)
        self.opt_buttons_frame.pack(side=tk.LEFT)

        self.console_autoscroll_var = tk.BooleanVar(value=True)
        self.serial_autoscroll_var = tk.BooleanVar(value=True)

        self.timestamp_var = tk.BooleanVar(value=False)
        self.cb_timestamp = tk.Checkbutton(
            self.opt_checkboxes_frame, text="Time Stamp", variable=self.timestamp_var,
            command=self._toggle_timestamps,
            font=self.font_label, fg=Theme.TEXT, bg=Theme.BG_MID,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_MID,
            activeforeground=Theme.TEXT,
        )
        self.cb_timestamp.pack(side=tk.LEFT, padx=(0, 8))

        self.skip_compile_var = tk.BooleanVar(value=False)
        self.cb_skip_compile = tk.Checkbutton(
            self.opt_checkboxes_frame, text="Skip Compile", variable=self.skip_compile_var,
            font=self.font_label, fg=Theme.TEXT, bg=Theme.BG_MID,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_MID,
            activeforeground=Theme.TEXT,
        )
        self.cb_skip_compile.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_detach_editor = self._make_btn(
            self.opt_buttons_frame, "🗗 Detach Editor", self._toggle_editor_detachment,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_btn
        )
        if sys.platform == "win32" and win32gui is not None:
            self.btn_detach_editor.pack(side=tk.LEFT, padx=(0, 8))
            self._update_detach_button_style()

        self.btn_toggle_editor = self._make_btn(
            self.opt_buttons_frame, "🗖 Hide Editor", self._toggle_editor_pane,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_btn
        )
        self.btn_toggle_editor.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_toggle_monitors = self._make_btn(
            self.opt_buttons_frame, "🗖 Hide Monitors", self._toggle_monitors_pane,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_btn
        )
        self.btn_toggle_monitors.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_settings = self._make_btn(
            self.root, "⚙ Settings", self._open_settings,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_btn
        )
        self.btn_settings.pack(in_=self.opt_buttons_frame, side=tk.LEFT, padx=(0, 8))

        self.btn_ai_assistant = self._make_btn(
            self.root, "🤖 AI Assistant", self._toggle_ai_side_panel,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_btn
        )
        self.btn_ai_assistant.pack(in_=self.opt_buttons_frame, side=tk.LEFT, padx=(0, 8))
        if self.ai_controller:
            self.ai_controller.add_button(self.btn_ai_assistant)

        # Dropdown trigger for compact mode (shows option buttons in a popup)
        self._opt_dropdown_btn = self._make_btn(
            self.root, "⚙ ▾", self._toggle_options_dropdown,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_btn
        )
        self._opt_dropdown_win = None

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
                            # Keep the selected and unselected tabs on the same
                            # geometry.  The previous larger padding made the
                            # notebook look oversized, while ttk's selected
                            # element made the active tab appear to shrink.
                            padding=[12, 5],
                            font=("Segoe UI", 9, "bold"))
            style.map("Bottom.TNotebook.Tab",
                      background=[("selected", Theme.BG_HOVER), ("active", Theme.BG_LIGHT)],
                      foreground=[("selected", Theme.CYAN), ("active", Theme.TEXT)])
        except Exception:
            pass

        try:
            raw_data = _load_raw_config()
            graphics_accel = raw_data.get("shared", {}).get("graphics_acceleration", "ON") != "OFF"
        except Exception:
            graphics_accel = True

        # ── HORIZONTAL SPLIT PANE: Left (Editor + Monitors) | Right (AI Assistant) ──
        self.h_split_pane = tk.PanedWindow(
            self.root, orient=tk.HORIZONTAL,
            bg=Theme.BORDER, sashwidth=4, sashrelief=tk.FLAT,
            borderwidth=0,
            opaqueresize=graphics_accel,
        )
        self.h_split_pane.pack(fill=tk.BOTH, expand=True)

        # ── LEFT: Main Vertical Pane (Editor top, Monitors bottom) ──
        self.main_pane = tk.PanedWindow(
            self.h_split_pane, orient=tk.VERTICAL,
            bg=Theme.BORDER, sashwidth=3, sashrelief=tk.FLAT,
            borderwidth=0,
            opaqueresize=graphics_accel,
        )
        self.h_split_pane.add(self.main_pane, stretch="always")

        # ── RIGHT SIDEBAR: OpenCode AI Assistant Side Container ──
        # Use the exact embedded terminal background (#0c0d10) so any sliver
        # around the reparented AI window blends seamlessly.
        ai_embed_bg = "#0c0d10"
        self.ai_side_container = tk.Frame(self.h_split_pane, bg=ai_embed_bg)

        # AI Side Header Bar
        self.ai_side_header = tk.Frame(self.ai_side_container, bg=Theme.BG_MID, height=32, padx=8, pady=4)
        self.ai_side_header.pack(fill=tk.X)

        self.lbl_ai_side_title = tk.Label(
            self.ai_side_header,
            text="🤖 OPENCODE AI ASSISTANT",
            font=tkfont.Font(family="Montserrat", size=9, weight="bold"),
            fg=Theme.CYAN, bg=Theme.BG_MID
        )
        self.lbl_ai_side_title.pack(side=tk.LEFT, padx=(2, 0))
        # Embed frame for pywebview AI window
        self.ai_embed_frame = tk.Frame(self.ai_side_container, bg=ai_embed_bg)
        self.ai_embed_frame.pack(fill=tk.BOTH, expand=True)

        self.ai_embed_frame.bind("<Configure>", self._resize_embedded_ai)
        self._ai_side_visible = False
        self._ai_is_embedded = False
        self._ai_hwnd = None

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
        self._build_console_frame = build_console_frame

        # Build Console Header
        console_header = tk.Frame(build_console_frame, bg=Theme.BG_MID, pady=6, padx=10)
        console_header.pack(fill=tk.X)
        self._console_header = console_header

        self.lbl_build_console_title = tk.Label(
            console_header, text="⚙ BUILD CONSOLE",
            font=self.monitor_heading_font,
            fg=Theme.CYAN, bg=Theme.BG_MID,
        )
        self.lbl_build_console_title.pack(side=tk.LEFT)

        btn_clear_console = self._make_btn(
            console_header, "🗑 Clear", self._clear_console,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_mono_sm
        )
        btn_clear_console.pack(side=tk.RIGHT)
        self.btn_clear_console_header = btn_clear_console

        def _copy_console():
            text = self.console.get("1.0", tk.END).strip()
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            if hasattr(self, "btn_copy_console_header") and self.btn_copy_console_header:
                self.btn_copy_console_header.config(text="✔ Copied!")
                self.root.after(1500, lambda: self.btn_copy_console_header.config(text="⧉ Copy") if hasattr(self, "btn_copy_console_header") and self.btn_copy_console_header else None)

        self.btn_copy_console_header = self._make_btn(
            console_header, "⧉ Copy", _copy_console,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_mono_sm
        )
        self.btn_copy_console_header.pack(side=tk.RIGHT, padx=(0, 6))

        initial_clear_val = get_clear_serial_on_upload()
        self.clear_serial_on_upload_var = tk.BooleanVar(value=initial_clear_val)
        self.cb_clear_serial_on_upload = tk.Checkbutton(
            console_header, text="Auto-clear Serial Monitor on Action",
            variable=self.clear_serial_on_upload_var,
            command=lambda: set_clear_serial_on_upload(self.clear_serial_on_upload_var.get()),
            font=self.font_mono_sm, fg=Theme.TEXT, bg=Theme.BG_MID,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_MID,
            activeforeground=Theme.TEXT,
        )
        self.cb_clear_serial_on_upload.pack(side=tk.RIGHT, padx=(0, 10))

        initial_clear_build_val = get_clear_build_console_on_action()
        self.clear_build_console_on_action_var = tk.BooleanVar(value=initial_clear_build_val)
        self.cb_clear_build_console_on_action = tk.Checkbutton(
            console_header, text="Clear Screen on Action",
            variable=self.clear_build_console_on_action_var,
            command=lambda: set_clear_build_console_on_action(self.clear_build_console_on_action_var.get()),
            font=self.font_mono_sm, fg=Theme.TEXT, bg=Theme.BG_MID,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_MID,
            activeforeground=Theme.TEXT,
        )
        self.cb_clear_build_console_on_action.pack(side=tk.RIGHT, padx=(0, 10))
        self.cb_console_autoscroll = tk.Checkbutton(
            console_header, text="Auto-scroll",
            variable=self.console_autoscroll_var,
            font=self.font_mono_sm, fg=Theme.TEXT, bg=Theme.BG_MID,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_MID,
            activeforeground=Theme.TEXT,
        )
        self.cb_console_autoscroll.pack(side=tk.RIGHT, padx=(0, 10))

        tk.Frame(build_console_frame, bg=Theme.BORDER, height=1).pack(fill=tk.X)

        self.console = tk.Text(
            build_console_frame,
            bg=Theme.BG_DARKEST,
            fg=Theme.TEXT,
            font=self.monitor_font,
            insertbackground=Theme.CYAN,
            selectbackground=Theme.BG_HOVER,
            selectforeground=Theme.TEXT_BRIGHT,
            inactiveselectbackground=Theme.BG_HOVER,
            exportselection=False,
            borderwidth=0,
            highlightthickness=0,
            padx=12,
            pady=8,
            wrap=tk.WORD,
            state=tk.DISABLED,
            cursor="xterm",
        )

        scrollbar = ttk.Scrollbar(
            build_console_frame, orient=tk.VERTICAL, command=self.console.yview
        )
        self.console.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._setup_selectable_read_only_text(self.console, clear_callback=self._clear_console)

        # Console text tags for coloring
        for widget in [self.console]:  # tag setup helper
            widget.tag_configure("info",    foreground=Theme.BLUE)
            widget.tag_configure("success", foreground=Theme.GREEN)
            widget.tag_configure("success_bold_lg", foreground=Theme.GREEN, font=self.monitor_font_large_bold)
            widget.tag_configure("warning", foreground=Theme.YELLOW)
            widget.tag_configure("error",   foreground=Theme.RED)
            widget.tag_configure("system",  foreground=Theme.CYAN)
            widget.tag_configure("dim",     foreground=Theme.TEXT_DIM)
            widget.tag_configure("magenta", foreground=Theme.MAGENTA)
            widget.tag_configure("magenta_bold_lg", foreground=Theme.MAGENTA, font=self.monitor_font_large_bold)
            widget.tag_configure("orange",  foreground=Theme.ORANGE)
            widget.tag_configure("bold",    font=self.monitor_font_bold)
            widget.tag_configure("port_highlight", foreground="#ff3fa4",
                                 font=self.monitor_font_bold)
            widget.tag_configure("header",  foreground=Theme.CYAN,
                                 font=self.monitor_font_header)
            widget.tag_configure("purple",  foreground=Theme.PURPLE)
            widget.tag_configure("purple_dim", foreground=Theme.PURPLE_DIM)
            widget.tag_configure("purple_header", foreground=Theme.PURPLE,
                                 font=self.monitor_font_bold)
            widget.tag_configure("purple_info", foreground=Theme.PURPLE_DIM)
            widget.tag_configure("purple_value", foreground=Theme.TEXT_BRIGHT)
            widget.tag_configure("sent",    foreground=Theme.MAGENTA)
            widget.tag_configure("severe_alert", foreground="#FF3355", font=self.monitor_font_bold)
            widget.tag_configure("timestamp", foreground=Theme.TEXT_DIM, elide=True)

        self.bottom_notebook.add(build_console_frame, text="  ⚙ Build Console  ")

        # ── TAB 2: Compatible Devices ──
        compat_frame = tk.Frame(self.bottom_notebook, bg=Theme.BG_DARKEST)
        self._compat_frame = compat_frame
        self._compatible_devices_tab_index_cache = None

        compat_header = tk.Frame(compat_frame, bg=Theme.BG_MID, pady=6, padx=10)
        self._compat_header = compat_header
        compat_header.pack(fill=tk.X)

        self.lbl_compat_title = tk.Label(
            compat_header, text="🔧 COMPATIBLE DEVICES",
            font=self.monitor_heading_font,
            fg=Theme.CYAN, bg=Theme.BG_MID,
        )
        self.lbl_compat_title.pack(side=tk.LEFT)

        self.lbl_compat_status = tk.Label(
            compat_header, text="Please compile to see the list of compatible devices",
            font=self.font_mono_sm,
            fg=Theme.TEXT_DIM, bg=Theme.BG_MID,
        )
        self.lbl_compat_status.pack(side=tk.LEFT, padx=(12, 0))

        def _copy_compat():
            text = self.compat_text.get("1.0", tk.END).strip()
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            if hasattr(self, "btn_compat_copy") and self.btn_compat_copy:
                self.btn_compat_copy.config(text="✔ Copied!")
                self.root.after(1500, lambda: self.btn_compat_copy.config(text="⧉ Copy") if hasattr(self, "btn_compat_copy") and self.btn_compat_copy else None)

        self.btn_compat_copy = self._make_btn(
            compat_header, "⧉ Copy", _copy_compat,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_mono_sm
        )
        self.btn_compat_copy.pack(side=tk.RIGHT)

        # ── Compatible Devices: search / filter bar ──
        compat_search_row = tk.Frame(compat_frame, bg=Theme.BG_MID, pady=4, padx=10)
        compat_search_row.pack(fill=tk.X)
        self._compat_search_row = compat_search_row

        self.lbl_compat_search_icon = tk.Label(
            compat_search_row, text="🔍", bg=Theme.BG_MID, fg=Theme.TEXT_DIM,
            font=self.font_mono_sm,
        )
        self.lbl_compat_search_icon.pack(side=tk.LEFT)

        self.compat_search_var = tk.StringVar()
        self.compat_search_var.trace_add("write", lambda *a: self._apply_compat_filter())
        self._compat_full_state = None
        self.compat_search_entry = tk.Entry(
            compat_search_row, textvariable=self.compat_search_var,
            bg=Theme.BG_DARKEST, fg=Theme.TEXT, insertbackground=Theme.TEXT,
            relief=tk.FLAT, font=self.font_mono_sm,
            highlightthickness=1, highlightbackground=Theme.BORDER,
            highlightcolor=Theme.CYAN,
        )
        self.compat_search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6), ipady=2)
        self.compat_search_entry.configure(takefocus=True, state=tk.NORMAL)
        self.compat_search_entry.bind(
            "<Button-1>",
            lambda _e: self._focus_compatible_search(select_all=False, select_tab=False),
            add="+",
        )
        self.compat_search_entry.bind(
            "<Control-f>",
            lambda _e: self._focus_compatible_search(select_all=True, select_tab=False),
        )
        self.compat_search_entry.bind(
            "<Control-F>",
            lambda _e: self._focus_compatible_search(select_all=True, select_tab=False),
        )
        self.compat_search_entry.bind(
            "<Escape>",
            lambda _e: (self.compat_search_var.set(""), "break")[1],
        )

        def _clear_compat_search():
            self.compat_search_var.set("")
            self.compat_search_entry.focus_set()

        btn_compat_search_clear = self._make_btn(
            compat_search_row, "✕ Clear", _clear_compat_search,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_mono_sm
        )
        btn_compat_search_clear.pack(side=tk.RIGHT)
        self.btn_compat_search_clear = btn_compat_search_clear

        # Clicking the title/search-row background should always reclaim keyboard
        # focus from the embedded Monaco/OpenCode native windows.
        for _compat_focus_widget in (
            compat_header,
            self.lbl_compat_title,
            self.lbl_compat_status,
            compat_search_row,
            self.lbl_compat_search_icon,
        ):
            _compat_focus_widget.bind(
                "<Button-1>",
                lambda _e: self._focus_compatible_search(select_all=False),
                add="+",
            )

        tk.Frame(compat_frame, bg=Theme.BORDER, height=1).pack(fill=tk.X)

        self.compat_text = tk.Text(
            compat_frame,
            bg=Theme.BG_DARKEST,
            fg=Theme.TEXT,
            insertbackground=Theme.TEXT,
            relief=tk.FLAT,
            wrap="word",
            font=self.font_mono,
            padx=10, pady=8,
            spacing1=1, spacing3=1,
        )
        compat_scroll = ttk.Scrollbar(compat_frame, orient=tk.VERTICAL, command=self.compat_text.yview)
        self.compat_text.configure(yscrollcommand=compat_scroll.set)
        compat_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.compat_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._setup_selectable_read_only_text(self.compat_text)
        self.compat_text.bind(
            "<Control-f>",
            lambda _e: self._focus_compatible_search(select_all=True),
        )
        self.compat_text.bind(
            "<Control-F>",
            lambda _e: self._focus_compatible_search(select_all=True),
        )

        for widget in [self.compat_text]:
            widget.tag_configure("system",  foreground=Theme.CYAN)
            widget.tag_configure("success", foreground=Theme.GREEN)
            widget.tag_configure("error",   foreground=Theme.RED)
            widget.tag_configure("warning", foreground=Theme.YELLOW)
            widget.tag_configure("dim",     foreground=Theme.TEXT_DIM)
            widget.tag_configure("normal",  foreground=Theme.TEXT)

        self.bottom_notebook.add(compat_frame, text="  🔧 Compatible Devices  ")

        # ── TAB 2: Serial Monitor Panel ──
        serial_monitor_frame = tk.Frame(self.bottom_notebook, bg=Theme.BG_DARKEST)
        self._serial_monitor_frame_container = serial_monitor_frame

        # Serial monitor header
        serial_header = tk.Frame(serial_monitor_frame, bg=Theme.BG_MID, pady=6, padx=10)
        serial_header.pack(fill=tk.X)
        self._serial_header = serial_header

        self.lbl_serial_monitor_title = tk.Label(
            serial_header, text="📡 SERIAL MONITOR",
            font=self.monitor_heading_font,
            fg=Theme.CYAN, bg=Theme.BG_MID,
        )
        self.lbl_serial_monitor_title.pack(side=tk.LEFT)

        btn_reset_mcu = self._make_btn(
            serial_header, "↺ Reset", self._reset_mcu_from_monitor,
            "#8B5E3C", "#A0724F", font=self.font_mono_sm
        )
        btn_reset_mcu.pack(side=tk.LEFT, padx=(10, 0))
        self.btn_reset_mcu = btn_reset_mcu

        btn_pause_serial = self._make_btn(
            serial_header, "⏸ Pause", self._toggle_serial_pause,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_mono_sm
        )
        btn_pause_serial.pack(side=tk.LEFT, padx=(6, 0))
        self.btn_pause_serial = btn_pause_serial

        # Baud rate selector in Serial Monitor tab (right side)
        self.serial_baud_group = tk.Frame(serial_header, bg=Theme.BG_MID)
        self.serial_baud_group.pack(side=tk.RIGHT, padx=(6, 0))

        self.lbl_serial_baud = tk.Label(
            self.serial_baud_group, text="BAUD RATE", font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_MID
        )
        self.lbl_serial_baud.pack(side=tk.LEFT)

        self.serial_baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
        self.serial_baud_combo = ttk.Combobox(
            self.serial_baud_group, textvariable=self.serial_baud_var, width=10,
            font=self.font_mono_sm, state="readonly",
            values=["9600", "19200", "38400", "57600", "115200", "230400", "460800", "512000", "921600"],
        )
        self.serial_baud_combo.pack(side=tk.LEFT, padx=(4, 0))
        self.serial_baud_combo.bind("<<ComboboxSelected>>", lambda e: self._on_serial_baud_changed())

        # Small divider between BAUD RATE and status indicator
        tk.Frame(serial_header, bg=Theme.CYAN_DIM, width=1, height=20).pack(
            side=tk.RIGHT, padx=(8, 8), fill=tk.Y)

        self.serial_status = tk.Label(
            serial_header, text="● Disconnected", font=self.font_status,
            fg=Theme.RED, bg=Theme.BG_MID, anchor=tk.E,
        )
        self.serial_status.pack(side=tk.RIGHT, padx=(0, 10))

        btn_clear_serial = self._make_btn(
            serial_header, "🗑 Clear", self._clear_serial_console,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_mono_sm
        )
        btn_clear_serial.pack(side=tk.RIGHT, padx=(0, 10))
        self.btn_clear_serial_header = btn_clear_serial

        def _copy_serial():
            text = self.serial_console.get("1.0", tk.END).strip()
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            if hasattr(self, "btn_copy_serial_header") and self.btn_copy_serial_header:
                self.btn_copy_serial_header.config(text="✔ Copied!")
                self.root.after(1500, lambda: self.btn_copy_serial_header.config(text="⧉ Copy") if hasattr(self, "btn_copy_serial_header") and self.btn_copy_serial_header else None)

        self.btn_copy_serial_header = self._make_btn(
            serial_header, "⧉ Copy", _copy_serial,
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, font=self.font_mono_sm
        )
        self.btn_copy_serial_header.pack(side=tk.RIGHT, padx=(0, 6))

        self.ansi_clear_var = tk.BooleanVar(value=True)
        self.cb_ansi_clear = tk.Checkbutton(
            serial_header, text="Clear-screen", variable=self.ansi_clear_var,
            font=self.font_mono_sm, fg=Theme.TEXT, bg=Theme.BG_MID,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_MID,
            activeforeground=Theme.TEXT,
        )
        self.cb_ansi_clear.pack(side=tk.RIGHT, padx=(0, 10))

        self.cb_serial_autoscroll = tk.Checkbutton(
            serial_header, text="Auto-scroll", variable=self.serial_autoscroll_var,
            font=self.font_mono_sm, fg=Theme.TEXT, bg=Theme.BG_MID,
            selectcolor=Theme.BG_DARK, activebackground=Theme.BG_MID,
            activeforeground=Theme.TEXT,
        )
        self.cb_serial_autoscroll.pack(side=tk.RIGHT, padx=(0, 10))

        tk.Frame(serial_monitor_frame, bg=Theme.CYAN_DIM, height=1).pack(fill=tk.X)

        # Serial input bar (packed at BOTTOM first so it's always visible and never clipped)
        input_frame = tk.Frame(serial_monitor_frame, bg=Theme.BG_DARK, pady=6, padx=10)
        self._serial_input_frame = input_frame
        input_frame.pack(fill=tk.X, side=tk.BOTTOM)

        tk.Frame(serial_monitor_frame, bg=Theme.BORDER, height=1).pack(fill=tk.X, side=tk.BOTTOM)

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
        self.serial_input.bind("<Button-1>", lambda e: safe_reclaim_os_focus(self.serial_input), add="+")

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

        # Serial output text container (expands to fill space between top header and bottom input bar)
        serial_console_frame = tk.Frame(serial_monitor_frame, bg=Theme.BG_DARKEST)
        serial_console_frame.pack(fill=tk.BOTH, expand=True)

        self.serial_console = tk.Text(
            serial_console_frame,
            bg=Theme.BG_DARKEST,
            fg=Theme.TEXT,
            font=self.monitor_font,
            insertbackground=Theme.CYAN,
            selectbackground=Theme.BG_HOVER,
            selectforeground=Theme.TEXT_BRIGHT,
            inactiveselectbackground=Theme.BG_HOVER,
            exportselection=False,
            borderwidth=0,
            highlightthickness=0,
            padx=10,
            pady=6,
            wrap=tk.WORD,
            state=tk.DISABLED,
            cursor="xterm",
        )

        serial_scrollbar = ttk.Scrollbar(
            serial_console_frame, orient=tk.VERTICAL, command=self.serial_console.yview
        )
        self.serial_console.configure(yscrollcommand=serial_scrollbar.set)

        serial_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.serial_console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._setup_selectable_read_only_text(self.serial_console, clear_callback=self._clear_serial_console, target_input_widget=self.serial_input)

        # Serial console text tags
        for widget in [self.serial_console]:
            widget.tag_configure("info",    foreground=Theme.BLUE)
            widget.tag_configure("success", foreground=Theme.GREEN)
            widget.tag_configure("success_bold_lg", foreground=Theme.GREEN, font=self.monitor_font_large_bold)
            widget.tag_configure("warning", foreground=Theme.YELLOW)
            widget.tag_configure("error",   foreground=Theme.RED)
            widget.tag_configure("system",  foreground=Theme.CYAN)
            widget.tag_configure("dim",     foreground=Theme.TEXT_DIM)
            widget.tag_configure("magenta", foreground=Theme.MAGENTA)
            widget.tag_configure("magenta_bold_lg", foreground=Theme.MAGENTA, font=self.monitor_font_large_bold)
            widget.tag_configure("orange",  foreground=Theme.ORANGE)
            widget.tag_configure("purple_header", foreground=Theme.PURPLE,
                                 font=self.monitor_font_bold)
            widget.tag_configure("purple_info", foreground=Theme.PURPLE_DIM)
            widget.tag_configure("purple_value", foreground=Theme.TEXT_BRIGHT)
            widget.tag_configure("sent",    foreground=Theme.MAGENTA)
            widget.tag_configure("timestamp", foreground=Theme.TEXT_DIM, elide=True)

        self.bottom_notebook.add(serial_monitor_frame, text="  📡 Serial Monitor  ")
        self._serial_monitor_frame = serial_monitor_frame
        self._serial_monitor_tab_index_cache = len(self.bottom_notebook.tabs()) - 1

        # ── TAB 3: Notifications ──
        notif_frame = tk.Frame(self.bottom_notebook, bg=Theme.BG_DARKEST)
        self._notif_frame = notif_frame

        notif_header = tk.Frame(notif_frame, bg=Theme.BG_MID, pady=6, padx=10)
        notif_header.pack(fill=tk.X)
        self._notif_header = notif_header

        tk.Label(
            notif_header, text="🔔 NOTIFICATIONS",
            font=self.monitor_heading_font,
            fg=Theme.ORANGE, bg=Theme.BG_MID,
        ).pack(side=tk.LEFT)

        notif_toolbar = tk.Frame(notif_header, bg=Theme.BG_MID)
        self._notif_toolbar = notif_toolbar
        notif_toolbar.pack(side=tk.RIGHT)

        tk.Label(notif_toolbar, text="Filter:", font=self.font_btn, fg=Theme.TEXT_DIM, bg=Theme.BG_MID).pack(side=tk.LEFT, padx=(0, 4))
        self._notif_filter_var = tk.StringVar(value="All")
        notif_combo = ttk.Combobox(
            notif_toolbar,
            textvariable=self._notif_filter_var,
            values=["All", "📦 Boards & Libraries", "🔌 USB Devices", "✖ Errors"],
            state="readonly",
            width=18,
            font=self.font_mono_sm
        )
        notif_combo.pack(side=tk.LEFT, padx=(0, 10))
        notif_combo.bind("<<ComboboxSelected>>", lambda e: self._load_persistent_notifications(self._notif_filter_var.get()))

        def _clear_notifications():
            try:
                if dbs_delete is not None and hasattr(dbs_delete, "clear_all_notifications"):
                    dbs_delete.clear_all_notifications()
            except Exception as e:
                print(f"[MCU Flasher] Error clearing database notifications: {e}")
            if hasattr(self, "notif_console") and self.notif_console:
                self.notif_console.configure(state=tk.NORMAL)
                self.notif_console.delete("1.0", tk.END)
                self.notif_console.insert(tk.END, "  🗑 All notifications cleared.\n", "dim")
                self.notif_console.configure(state=tk.DISABLED)

        def _copy_notifications():
            try:
                text = self.notif_console.get("1.0", tk.END).rstrip()
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                if hasattr(self, "btn_copy_notif") and self.btn_copy_notif:
                    self.btn_copy_notif.config(text="✔ Copied!", bg=Theme.GREEN)
                    self.root.after(1500, lambda: self.btn_copy_notif.config(text="📋 Copy", bg=Theme.BTN_DIM) if hasattr(self, "btn_copy_notif") and self.btn_copy_notif else None)
            except Exception:
                pass

        self.btn_copy_notif = _make_toolbar_btn(notif_toolbar, "📋 Copy", _copy_notifications, Theme.BTN_DIM,
                          Theme.BTN_DIM_H, self.font_btn)
        self.btn_clear_notif = _make_toolbar_btn(notif_toolbar, "🗑 Clear All", _clear_notifications, Theme.BTN_DANGER,
                          Theme.BTN_DANGER_H, self.font_btn)

        notif_text_frame = tk.Frame(notif_frame, bg=Theme.BG_DARKEST)
        self._notif_text_frame = notif_text_frame
        notif_text_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.notif_console = tk.Text(
            notif_text_frame,
            bg=Theme.BG_DARK,
            fg=Theme.TEXT,
            insertbackground=Theme.TEXT,
            selectbackground=Theme.BG_HOVER,
            selectforeground=Theme.TEXT_BRIGHT,
            inactiveselectbackground=Theme.BG_HOVER,
            exportselection=False,
            font=self.monitor_font,
            wrap=tk.WORD,
            state=tk.DISABLED,
            padx=6, pady=6,
            relief=tk.FLAT,
            highlightthickness=0,
            cursor="xterm",
        )
        notif_scroll = ttk.Scrollbar(
            notif_text_frame, orient=tk.VERTICAL, command=self.notif_console.yview,
        )
        self.notif_console.configure(yscrollcommand=notif_scroll.set)
        self.notif_console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        notif_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._setup_selectable_read_only_text(self.notif_console, clear_callback=_clear_notifications)

        for tag_name, color in [
            ("timestamp", Theme.TEXT_DIM),
            ("dim",       Theme.TEXT_DIM),
            ("info",      Theme.CYAN),
            ("success",   Theme.GREEN),
            ("warning",   Theme.YELLOW),
            ("error",     Theme.RED),
            ("system",    Theme.PURPLE),
            ("header",    Theme.ORANGE),
        ]:
            self.notif_console.tag_configure(tag_name, foreground=color)

        self.bottom_notebook.add(notif_frame, text="  🔔 Notifications  ")
        self._load_persistent_notifications()

        # ── TAB 4: Syntax Check Panel ──
        syntax_check_frame = tk.Frame(self.bottom_notebook, bg=Theme.BG_DARKEST)
        self._syntax_check_frame = syntax_check_frame

        # Syntax header
        syntax_header = tk.Frame(syntax_check_frame, bg=Theme.BG_MID, pady=6, padx=10)
        syntax_header.pack(fill=tk.X)
        self._syntax_header = syntax_header

        self.lbl_syntax_status = tk.Label(
            syntax_header, text="🔍 SYNTAX CHECK",
            font=self.monitor_heading_font,
            fg=Theme.CYAN, bg=Theme.BG_MID,
        )
        self.lbl_syntax_status.pack(side=tk.LEFT)

        lbl_syntax_beta_notice = tk.Label(
            syntax_header, text=" (Beta: Checker is under progress; some warnings or errors may be approximate)",
            font=tkfont.Font(family="Montserrat", size=8),
            fg=Theme.YELLOW, bg=Theme.BG_MID,
        )
        lbl_syntax_beta_notice.pack(side=tk.LEFT, padx=(6, 0))

        # Treeview to display results
        tree_frame = tk.Frame(syntax_check_frame, bg=Theme.BG_DARKEST)
        self._syntax_tree_frame = tree_frame
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Style treeview to match the theme
        try:
            style = ttk.Style()
            style.configure(
                "Syntax.Treeview",
                background=Theme.BG_DARK,
                foreground=Theme.TEXT,
                fieldbackground=Theme.BG_DARK,
                rowheight=26,
                font=self.monitor_font
            )
            style.configure(
                "Syntax.Treeview.Heading",
                font=("Segoe UI", 9, "bold"),
                background=Theme.BG_MID,
                foreground=Theme.TEXT_BRIGHT,
                padding=[8, 4]
            )
        except Exception:
            pass

        self.syntax_tree = ttk.Treeview(
            tree_frame, columns=("file", "line", "severity", "desc"), show="headings",
            style="Syntax.Treeview"
        )
        self.syntax_tree.heading("file", text="  File  ")
        self.syntax_tree.heading("line", text="  Line  ")
        self.syntax_tree.heading("severity", text="  Severity  ")
        self.syntax_tree.heading("desc", text="  Description  ")

        self.syntax_tree.column("file", width=160, minwidth=100, stretch=True, anchor=tk.W)
        self.syntax_tree.column("line", width=70, minwidth=60, stretch=False, anchor=tk.CENTER)
        self.syntax_tree.column("severity", width=110, minwidth=90, stretch=False, anchor=tk.CENTER)
        self.syntax_tree.column("desc", width=400, minwidth=150, stretch=True, anchor=tk.W)

        scrollbar_y = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.syntax_tree.yview)
        self.syntax_tree.configure(yscrollcommand=scrollbar_y.set)

        scrollbar_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.syntax_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Color tags
        self.syntax_tree.tag_configure("error", foreground=Theme.RED)
        self.syntax_tree.tag_configure("warning", foreground=Theme.ORANGE)

        self.syntax_tree.bind("<Double-Button-1>", self._on_syntax_tree_double_click)

        self.bottom_notebook.add(syntax_check_frame, text="  🔍 Syntax Check  ")

        # ── TAB 5: Project Terminal ──
        # Keep the terminal lazy: creating the tab is cheap, and the first PTY
        # is only started when the user opens this tab.  This avoids adding a
        # shell startup cost to the normal editor/compile path.
        terminal_frame = tk.Frame(self.bottom_notebook, bg=Theme.BG_DARKEST)
        self._shell_terminal_frame = terminal_frame

        terminal_body = tk.Frame(terminal_frame, bg=Theme.BG_DARKEST)
        terminal_body.pack(fill=tk.BOTH, expand=True)
        self._terminal_body = terminal_body

        shell_switcher = tk.Frame(
            terminal_body, bg=Theme.BG_DARKEST, width=116,
            highlightthickness=1, highlightbackground=Theme.BORDER,
        )
        shell_switcher.pack(side=tk.RIGHT, fill=tk.Y)
        shell_switcher.pack_propagate(False)
        self._shell_switcher = shell_switcher
        tk.Label(
            shell_switcher, text="SHELLS", font=self.font_label,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST,
        ).pack(fill=tk.X, padx=8, pady=(10, 6), anchor=tk.W)
        self._shell_switch_buttons = {}
        for shell_kind, shell_label in (("pwsh", "▣ pwsh"), ("cmd", "▣ cmd")):
            shell_btn = tk.Button(
                shell_switcher, text=shell_label, anchor=tk.W,
                font=self.font_mono_sm, fg=Theme.TEXT, bg=Theme.BG_DARK,
                activeforeground=Theme.TEXT_BRIGHT, activebackground=Theme.BG_HOVER,
                relief=tk.FLAT, borderwidth=0, padx=9, pady=7, cursor="hand2",
                takefocus=True,
            )
            shell_btn.bind(
                "<ButtonRelease-1>",
                lambda event, kind=shell_kind: self._shell_switch_button_click(event, kind),
            )
            shell_btn.bind(
                "<Return>",
                lambda _event, kind=shell_kind: self._shell_select(kind),
            )
            shell_btn.bind(
                "<space>",
                lambda _event, kind=shell_kind: self._shell_select(kind),
            )
            shell_btn.pack(fill=tk.X, padx=5, pady=2)
            self._shell_switch_buttons[shell_kind] = shell_btn

        shell_left = tk.Frame(terminal_body, bg=Theme.BG_DARKEST)
        shell_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._shell_left = shell_left

        # Native terminal surface. The child WebView2 window is reparented
        # into this frame after it creates its own HWND. The small placeholder
        # remains visible until the real xterm page has attached.
        self._shell_terminal_embed_frame = tk.Frame(shell_left, bg="#0c0d10")
        self._shell_terminal_embed_frame.pack(fill=tk.BOTH, expand=True)
        self._shell_terminal_embed_frame.bind(
            "<Configure>", self._resize_project_terminal
        )
        self._shell_terminal_placeholder = tk.Label(
            self._shell_terminal_embed_frame,
            text="Loading native Project Terminal…",
            font=self.font_mono_sm,
            fg=Theme.TEXT_DIM,
            bg="#0c0d10",
        )
        self._shell_terminal_placeholder.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        # Existing Tk PTY surface is retained only as a compatibility fallback.
        # It is not mapped during the normal path, so it cannot intercept
        # Backspace/Ctrl+C/Ctrl+V before xterm.js receives them.
        self._shell_terminal_fallback_frame = tk.Frame(shell_left, bg=Theme.BG_DARKEST)
        shell_scroll = ttk.Scrollbar(self._shell_terminal_fallback_frame, orient=tk.VERTICAL)
        self.shell_console = tk.Text(
            self._shell_terminal_fallback_frame, bg="#0c0d10", fg="#cccccc",
            insertbackground="#ffffff", selectbackground="#264f78",
            selectforeground="#ffffff", font=self.font_mono,
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
            padx=12, pady=8, wrap=tk.NONE, state=tk.NORMAL,
            spacing1=0, spacing2=0, spacing3=0,
            cursor="xterm", undo=False,
        )
        shell_scroll.configure(command=self.shell_console.yview)
        self.shell_console.configure(yscrollcommand=shell_scroll.set)
        shell_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.shell_console.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._shell_terminal_fallback_frame.pack_forget()
        self._configure_shell_ansi_tags()
        self._configure_shell_selection_highlight()
        # Keep the output selectable, but make this a real PTY terminal: key
        # presses are forwarded to the selected shell instead of being inserted
        # into the Text widget.
        self._setup_selectable_read_only_text(
            self.shell_console,
        )
        # Keep native shell semantics for Ctrl+C when there is no selection,
        # while allowing the familiar terminal copy behavior when text is
        # selected.  The generic read-only viewer bindings would otherwise
        # consume these keys before they reached the PTY.
        for _sequence in (
            "<Control-c>", "<Control-C>", "<Control-a>", "<Control-A>",
            "<Control-v>", "<Control-V>", "<Control-Shift-c>",
            "<Control-Shift-C>", "<Control-Insert>",
        ):
            self.shell_console.unbind(_sequence)
        # Replace the widget's generic key binding instead of appending to it.
        # Appending allowed Tk's Text class binding to insert the character
        # after it had already been sent to the PTY, producing doubled input.
        self.shell_console.bind("<KeyPress>", self._shell_console_key)
        self.shell_console.bind("<Control-c>", self._shell_console_copy_or_interrupt)
        self.shell_console.bind("<Control-C>", self._shell_console_copy_or_interrupt)
        self.shell_console.bind("<Control-v>", self._shell_console_paste)
        self.shell_console.bind("<Control-V>", self._shell_console_paste)
        self.shell_console.bind("<Shift-Insert>", self._shell_console_paste)
        # Keep editing keys entirely in the PTY.  These explicit bindings
        # prevent Tk's Text class from applying its own deletion behavior.
        self.shell_console.bind("<BackSpace>", self._shell_console_backspace)
        self.shell_console.bind("<Delete>", self._shell_console_delete)
        self.shell_console.bind("<Control-Shift-c>", self._shell_console_copy)
        self.shell_console.bind("<Control-Shift-C>", self._shell_console_copy)
        self.shell_console.bind("<Control-Insert>", self._shell_console_copy)

        self.bottom_notebook.add(terminal_frame, text="  ⌘ Terminal  ")
        self._shell_terminal_tab_id = terminal_frame
        self._shell_refresh_switcher()

        # ── Tab order: keep Build Console and Serial Monitor side by side ──
        # Tabs are added in construction order (Build Console, Compatible
        # Devices, Serial Monitor, …), which placed the Compatible Devices
        # tab between the console and the monitor.  Move the Serial Monitor
        # tab to position 1 so the two most-used tabs sit adjacent, and
        # invalidate the pre-cached serial-monitor index so lookups re-derive
        # the new position.
        try:
            self.bottom_notebook.insert(1, serial_monitor_frame)
        except Exception:
            pass
        self._serial_monitor_tab_index_cache = None
        self._compatible_devices_tab_index_cache = None
        self.bottom_notebook.bind(
            "<<NotebookTabChanged>>", self._on_bottom_notebook_tab_changed, add="+"
        )
        self.bottom_notebook.bind(
            "<ButtonRelease-1>", self._on_bottom_notebook_click_release, add="+"
        )
        # Serial rendering is driven by one Tk-owned pump.  It keeps receiving
        # bytes in the background at all times, but avoids repainting the hidden
        # terminal while the user is working in another tab.
        self._serial_display_pump_after_id = self.root.after(25, self._serial_display_pump)
        self._shell_terminal_pump_after_id = self.root.after(40, self._shell_output_pump)

        self.main_pane.add(bottom_frame, minsize=self._bottom_minsize, height=self._bottom_height)

        # ── Editor / Monitors show-hide state ──
        self.editor_pane_visible = True
        self.monitors_pane_visible = True
        self._update_pane_toggle_buttons()

        # ── Status Bar ──
        tk.Frame(self.root, bg=Theme.BORDER, height=1).pack(fill=tk.X)

        status_frame = tk.Frame(self.root, bg=Theme.BG_DARK, pady=4, padx=12)
        status_frame.pack(fill=tk.X)
        self._status_frame = status_frame

        self.status_label = tk.Label(
            status_frame, text="Ready", font=self.font_status,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARK, anchor=tk.W,
        )
        self.status_label.pack(side=tk.LEFT)

        self.editor_info_label = tk.Label(
            status_frame, text="", font=self.font_status,
            fg=Theme.TEXT_DIM, bg=Theme.BG_DARK, anchor=tk.E,
        )
        self.editor_info_label.pack(side=tk.RIGHT)

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

        # Style the dropdown popup listbox for ttk.Combobox (the "clam" theme
        # uses a plain Tk Listbox for its popdown, so we set global Listbox
        # defaults AND add Tcl-level hooks to re-style it each time it opens.)
        self.root.option_add("*TCombobox*Listbox.background", Theme.BG_LIGHT)
        self.root.option_add("*TCombobox*Listbox.foreground", Theme.TEXT_BRIGHT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", Theme.BG_HOVER)
        self.root.option_add("*TCombobox*Listbox.selectForeground", Theme.CYAN)
        self.root.option_add("*TCombobox*Listbox.relief", "flat")
        self.root.option_add("*TCombobox*Listbox.borderWidth", "1")
        self.root.option_add("*TCombobox*Listbox.highlightBackground", Theme.BORDER)
        self.root.option_add("*TCombobox*Listbox.justify", "left")
        self.root.option_add("*Combobox*Listbox.justify", "left")

        # Also set global Listbox defaults so other custom popups
        # (BoardSearchDialog, etc.) inherit the same dark look.
        self.root.option_add("*Listbox.background", Theme.BG_LIGHT)
        self.root.option_add("*Listbox.foreground", Theme.TEXT_BRIGHT)
        self.root.option_add("*Listbox.selectBackground", Theme.BG_HOVER)
        self.root.option_add("*Listbox.selectForeground", Theme.CYAN)
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

        # The welcome/project scan is scheduled after MCUUploadGUI.__init__
        # completes.  Calling it here used to run _on_folder_changed() while
        # the interface was still being constructed, so any project-specific
        # failure aborted the entire GUI before the main event loop started.
        self._update_editor_info()

    def _setup_selectable_read_only_text(self, text_widget: tk.Text, clear_callback=None, target_input_widget: tk.Widget = None):
        """Configure a read-only console Text widget so mouse text selection, highlighting,
        Ctrl+C/Ctrl+A keyboard shortcuts, and right-click context menu ALWAYS work reliably,
        even when state='disabled' and regardless of focus changes.
        If target_input_widget is provided:
          - A simple click on text_widget redirects focus to target_input_widget.
          - Hold-click, click-and-drag selection, or double-click preserves selection on text_widget.
        """
        text_widget.configure(
            exportselection=False,
            inactiveselectbackground=Theme.BG_HOVER,
            cursor="xterm",
        )

        press_state = {"time": 0.0, "x": 0, "y": 0, "timer": None}

        def _on_click(event):
            text_widget.focus_set()
            if press_state["timer"] is not None:
                try:
                    text_widget.after_cancel(press_state["timer"])
                except Exception:
                    pass
                press_state["timer"] = None
            press_state["time"] = time.time()
            press_state["x"] = event.x
            press_state["y"] = event.y

        def _on_release(event):
            if not target_input_widget:
                return

            dt = time.time() - press_state["time"]
            dx = abs(event.x - press_state["x"])
            dy = abs(event.y - press_state["y"])

            # If user dragged mouse (dx > 4 or dy > 4), or held click down (> 0.25s),
            # treat it as text selection / hold-click on console print. Focus stays on text_widget.
            if dx > 4 or dy > 4 or dt > 0.25:
                return

            # For quick single click, schedule focus redirect after a short delay (180ms)
            # so double-click word selection or drag-selection is not interrupted.
            def _do_focus_redirect():
                press_state["timer"] = None
                try:
                    # If text was selected (e.g. double-click or drag), don't redirect focus
                    if bool(text_widget.tag_ranges("sel")):
                        return
                    safe_reclaim_os_focus(target_input_widget)
                except Exception:
                    pass

            if press_state["timer"] is not None:
                try:
                    text_widget.after_cancel(press_state["timer"])
                except Exception:
                    pass

            press_state["timer"] = text_widget.after(180, _do_focus_redirect)

        def _on_copy(event=None):
            try:
                selection_tag = "shell_selection" if text_widget.tag_ranges("shell_selection") else "sel"
                ranges = text_widget.tag_ranges(selection_tag)
                if not ranges:
                    return "break"
                sel = text_widget.get(ranges[0], ranges[1])
                if sel:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(sel)
            except tk.TclError:
                pass
            return "break"

        def _on_select_all(event=None):
            text_widget.tag_add("sel", "1.0", tk.END)
            text_widget.focus_set()
            return "break"

        # Explicit keyboard focus on click
        text_widget.bind("<Button-1>", _on_click, add="+")
        if target_input_widget:
            text_widget.bind("<ButtonRelease-1>", _on_release, add="+")

        # Explicit copy & select-all bindings for disabled widgets
        text_widget.bind("<Control-c>", _on_copy)
        text_widget.bind("<Control-C>", _on_copy)
        text_widget.bind("<Control-a>", _on_select_all)
        text_widget.bind("<Control-A>", _on_select_all)
        text_widget.bind("<Command-c>", _on_copy)
        text_widget.bind("<Command-C>", _on_copy)
        text_widget.bind("<Command-a>", _on_select_all)
        text_widget.bind("<Command-A>", _on_select_all)

        # Context menu (Right-click)
        menu = tk.Menu(
            text_widget, tearoff=0, bg=Theme.BG_DARK, fg=Theme.TEXT_BRIGHT,
            activebackground=Theme.CYAN, activeforeground=Theme.BG_DARKEST,
            bd=1, relief=tk.SOLID
        )
        menu.add_command(label="📋 Copy", command=_on_copy, accelerator="Ctrl+C")
        menu.add_command(label="Select All", command=_on_select_all, accelerator="Ctrl+A")
        if clear_callback:
            menu.add_separator()
            menu.add_command(label="🗑 Clear", command=clear_callback)

        def _show_context_menu(event):
            text_widget.focus_set()
            try:
                has_sel = bool(
                    text_widget.tag_ranges("sel")
                    or text_widget.tag_ranges("shell_selection")
                )
                menu.entryconfig(0, state=tk.NORMAL if has_sel else tk.DISABLED)
            except Exception:
                pass
            menu.tk_popup(event.x_root, event.y_root)

        text_widget.bind("<Button-3>", _show_context_menu)
        text_widget.bind("<Button-2>", _show_context_menu)

    def _make_btn(self, parent, text, command, bg, bg_hover, width=None, font=None) -> tk.Button:
        """Create a styled flat button."""
        btn_font = font if font is not None else self.font_btn
        btn = tk.Button(
            parent, text=text, command=command,
            font=btn_font, fg=Theme.TEXT_BRIGHT, bg=bg,
            activebackground=bg_hover, activeforeground=Theme.TEXT_BRIGHT,
            relief=tk.FLAT, borderwidth=0, padx=self._btn_padx, pady=self._btn_pady, cursor="hand2",
            highlightthickness=1, highlightbackground=Theme.BORDER, highlightcolor=Theme.BORDER_LIT,
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
            font=self.font_btn, fg=Theme.TEXT_BRIGHT, bg=bg,
            activebackground=bg_hover, activeforeground=Theme.TEXT_BRIGHT,
            relief=tk.FLAT, borderwidth=0, width=width, cursor="hand2",
            highlightthickness=1, highlightbackground=Theme.BORDER, highlightcolor=Theme.BORDER_LIT,
        )
        btn.bind("<Enter>", lambda e, b=btn, c=bg_hover: b.configure(bg=c))
        btn.bind("<Leave>", lambda e, b=btn, c=bg: b.configure(bg=c))
        return btn

    def _restyle_btn(self, btn, bg, bg_hover, fg=None):
        if not btn:
            return
        btn_fg = fg if fg is not None else Theme.TEXT_BRIGHT
        try:
            btn.configure(
                bg=bg, activebackground=bg_hover, fg=btn_fg, activeforeground=btn_fg,
                highlightbackground=Theme.BORDER, highlightcolor=Theme.BORDER_LIT,
            )
            btn.bind("<Enter>", lambda e, b=btn, c=bg_hover: b.configure(bg=c))
            btn.bind("<Leave>", lambda e, b=btn, c=bg: b.configure(bg=c))
        except Exception:
            pass

    def _apply_theme_to_ui(self, mode_name: str = None):
        """Live-update all Tkinter UI widgets, ttk styles, and embedded Monaco Editor to the active theme."""
        active_mode = Theme.apply_theme(mode_name or get_theme_mode())

        try:
            self.root.configure(bg=Theme.BG_DARKEST)
        except Exception:
            pass

        for f in (
            getattr(self, "title_frame", None),
            getattr(self, "title_row_top", None),
            getattr(self, "title_row_bottom", None),
            getattr(self, "title_left", None),
            getattr(self, "sketch_frame", None),
            getattr(self, "actions_frame", None),
            getattr(self, "inner_actions", None),
            getattr(self, "top_compact_actions", None),
            getattr(self, "top_compact_inner", None),
        ):
            if f:
                try:
                    f.configure(bg=Theme.BG_DARK)
                except Exception:
                    pass

        if hasattr(self, "lbl_app_title") and self.lbl_app_title:
            try:
                self.lbl_app_title.configure(fg=Theme.CYAN, bg=Theme.BG_DARK)
            except Exception:
                pass
        if hasattr(self, "lbl_sketch") and self.lbl_sketch:
            try:
                self.lbl_sketch.configure(fg=Theme.TEXT_DIM, bg=Theme.BG_DARK)
            except Exception:
                pass
        if hasattr(self, "lbl_sketch_icon") and self.lbl_sketch_icon:
            try:
                self.lbl_sketch_icon.configure(fg=Theme.TEXT_DIM, bg=Theme.BG_DARK)
            except Exception:
                pass
        if hasattr(self, "lbl_actions_title") and self.lbl_actions_title:
            try:
                self.lbl_actions_title.configure(fg=Theme.TEXT_DIM, bg=Theme.BG_DARK)
            except Exception:
                pass
        if hasattr(self, "title_divider") and self.title_divider:
            try:
                self.title_divider.configure(bg=Theme.BORDER)
            except Exception:
                pass

        # Action buttons with colored backgrounds get pure white text
        self._restyle_btn(getattr(self, "btn_compile", None), Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, fg="#ffffff")
        self._restyle_btn(getattr(self, "btn_upload", None), Theme.BTN_FULL, Theme.BTN_FULL_H, fg="#ffffff")
        self._restyle_btn(getattr(self, "btn_stop", None), Theme.BTN_STOP, Theme.BTN_STOP_H, fg="#ffffff")
        self._restyle_btn(getattr(self, "btn_save", None), Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, fg="#ffffff")
        self._restyle_btn(getattr(self, "btn_save_all", None), Theme.BTN_FULL, Theme.BTN_FULL_H, fg="#ffffff")
        self._restyle_btn(getattr(self, "btn_modify_files", None), Theme.BTN_MONITOR, Theme.BTN_MONITOR_H, fg="#ffffff")
        self._restyle_btn(getattr(self, "btn_download_mgr", None), Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, fg="#ffffff")
        self._restyle_btn(getattr(self, "btn_detach_editor", None), Theme.BTN_COMPILE if not getattr(self, "editor_detached", False) else Theme.BTN_UPLOAD, Theme.BTN_COMPILE_H, fg="#ffffff")
        self._restyle_btn(getattr(self, "btn_reset_mcu", None), "#8B5E3C", "#A0724F", fg="#ffffff")
        self._restyle_btn(getattr(self, "btn_search_board", None), Theme.BTN_MONITOR, Theme.BTN_MONITOR_H, fg="#ffffff")
        self._restyle_btn(getattr(self, "btn_send", None), Theme.BTN_MONITOR, Theme.BTN_MONITOR_H, fg="#ffffff")

        # Neutral buttons get Theme.TEXT_BRIGHT (dark in light mode, bright in dark mode)
        self._restyle_btn(getattr(self, "btn_clean", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "btn_reload_file", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "btn_new_project", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "_actions_dropdown_btn", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "btn_toggle_editor", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "btn_toggle_monitors", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "btn_settings", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "btn_ai_assistant", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "_opt_dropdown_btn", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "btn_pause_serial", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "btn_clear_console_header", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "btn_copy_console_header", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "btn_clear_serial_header", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "btn_copy_serial_header", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)
        self._restyle_btn(getattr(self, "btn_compat_copy", None), Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, fg=Theme.TEXT_BRIGHT)

        if hasattr(self, "_update_detach_button_style"):
            try:
                self._update_detach_button_style()
            except Exception:
                pass

        if hasattr(self, "top_separator") and self.top_separator:
            try:
                self.top_separator.configure(bg=Theme.BORDER)
            except Exception:
                pass

        if hasattr(self, "board_entry") and self.board_entry:
            try:
                self.board_entry.configure(
                    bg=Theme.BG_LIGHT,
                    fg=Theme.TEXT_BRIGHT,
                    readonlybackground=Theme.BG_LIGHT,
                    disabledbackground=Theme.BG_LIGHT,
                    disabledforeground=Theme.TEXT_DIM,
                    highlightbackground=Theme.BORDER,
                    highlightcolor=Theme.BORDER_LIT,
                )
            except Exception:
                pass

        for f in (
            getattr(self, "ctrl_frame", None),
            getattr(self, "ctrl_row_top", None),
            getattr(self, "ctrl_row_bottom", None),
            getattr(self, "board_group", None),
            getattr(self, "port_group", None),
            getattr(self, "baud_group", None),
            getattr(self, "upload_spd_group", None),
            getattr(self, "right_group", None),
            getattr(self, "opt_row", None),
            getattr(self, "opt_checkboxes_frame", None),
            getattr(self, "opt_buttons_frame", None),
            getattr(self, "opt_actions_frame", None),
            getattr(self, "opt_actions_inner", None),
        ):
            if f:
                try:
                    f.configure(bg=Theme.BG_MID)
                except Exception:
                    pass

        for lbl in (
            getattr(self, "lbl_board", None),
            getattr(self, "lbl_port", None),
            getattr(self, "lbl_baud", None),
            getattr(self, "lbl_upload_spd", None),
            getattr(self, "lbl_options_title", None),
            getattr(self, "_lbl_upload_spd", None),
        ):
            if lbl:
                try:
                    lbl.configure(fg=Theme.TEXT_DIM, bg=Theme.BG_MID)
                except Exception:
                    pass

        for cb in (
            getattr(self, "auto_scroll_cb", None),
            getattr(self, "cb_clear_serial", None),
            getattr(self, "cb_clear_build_console_on_action", None),
            getattr(self, "cb_console_autoscroll", None),
            getattr(self, "cb_timestamp", None),
            getattr(self, "cb_skip_compile", None),
        ):
            if cb:
                try:
                    cb.configure(
                        fg=Theme.TEXT_BRIGHT, bg=Theme.BG_MID,
                        selectcolor=Theme.BG_LIGHT, activebackground=Theme.BG_MID,
                        activeforeground=Theme.TEXT_BRIGHT
                    )
                except Exception:
                    pass

        for p in (
            getattr(self, "main_pane", None),
            getattr(self, "h_split_pane", None),
        ):
            if p:
                try:
                    p.configure(bg=Theme.BORDER)
                except Exception:
                    pass

        for p in (
            getattr(self, "editor_frame", None),
            getattr(self, "_editor_embed_frame", None),
            getattr(self, "_editor_placeholder", None),
            getattr(self, "bottom_frame", None),
            getattr(self, "bottom_notebook", None),
        ):
            if p:
                try:
                    p.configure(bg=Theme.BG_DARKEST)
                except Exception:
                    pass

        if hasattr(self, "_editor_spinner_canvas") and self._editor_spinner_canvas:
            try:
                self._editor_spinner_canvas.configure(bg=Theme.BG_DARKEST)
            except Exception:
                pass
        if hasattr(self, "_editor_status_lbl") and self._editor_status_lbl:
            try:
                self._editor_status_lbl.configure(fg=Theme.TEXT_BRIGHT, bg=Theme.BG_DARKEST)
            except Exception:
                pass
        if hasattr(self, "_editor_desc_lbl") and self._editor_desc_lbl:
            try:
                self._editor_desc_lbl.configure(fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST)
            except Exception:
                pass

        for console in (getattr(self, "console", None), getattr(self, "serial_console", None)):
            if console:
                try:
                    console.configure(
                        bg=Theme.BG_DARKEST,
                        fg=Theme.TEXT,
                        insertbackground=Theme.CYAN,
                        selectbackground=Theme.BG_HOVER,
                        selectforeground=Theme.TEXT_BRIGHT,
                        inactiveselectbackground=Theme.BG_HOVER,
                    )
                    console.tag_configure("info", foreground=Theme.BLUE)
                    console.tag_configure("success", foreground=Theme.GREEN)
                    console.tag_configure("success_bold_lg", foreground=Theme.GREEN)
                    console.tag_configure("warning", foreground=Theme.YELLOW)
                    console.tag_configure("error", foreground=Theme.RED)
                    console.tag_configure("system", foreground=Theme.CYAN)
                    console.tag_configure("dim", foreground=Theme.TEXT_DIM)
                    console.tag_configure("magenta", foreground=Theme.MAGENTA)
                    console.tag_configure("magenta_bold_lg", foreground=Theme.MAGENTA)
                    console.tag_configure("orange", foreground=Theme.ORANGE)
                    console.tag_configure("header", foreground=Theme.CYAN)
                    console.tag_configure("purple", foreground=Theme.PURPLE)
                    console.tag_configure("purple_dim", foreground=Theme.PURPLE_DIM)
                    console.tag_configure("purple_header", foreground=Theme.PURPLE)
                    console.tag_configure("purple_info", foreground=Theme.PURPLE_DIM)
                    console.tag_configure("purple_value", foreground=Theme.TEXT_BRIGHT)
                    console.tag_configure("sent", foreground=Theme.MAGENTA)
                    console.tag_configure("timestamp", foreground=Theme.TEXT_DIM)
                except Exception:
                    pass

        # ── Tab container frames (previously local vars, now stored as self._xxx) ──
        for f in (
            getattr(self, "_build_console_frame", None),
            getattr(self, "_serial_monitor_frame_container", None),
            getattr(self, "_notif_frame", None),
            getattr(self, "_notif_text_frame", None),
            getattr(self, "_syntax_check_frame", None),
            getattr(self, "_syntax_tree_frame", None),
            getattr(self, "_terminal_body", None),
            getattr(self, "_shell_terminal_embed_frame", None),
            getattr(self, "_shell_terminal_fallback_frame", None),
            getattr(self, "_shell_left", None),
        ):
            if f:
                try:
                    f.configure(bg=Theme.BG_DARKEST)
                except Exception:
                    pass

        # Tab headers / toolbars — BG_MID background
        for f in (
            getattr(self, "_console_header", None),
            getattr(self, "_serial_header", None),
            getattr(self, "_notif_header", None),
            getattr(self, "_notif_toolbar", None),
            getattr(self, "_syntax_header", None),
            getattr(self, "_compat_header", None),
            getattr(self, "_compat_search_row", None),
        ):
            if f:
                try:
                    f.configure(bg=Theme.BG_MID)
                except Exception:
                    pass

        # Serial input bar — BG_DARK background
        if getattr(self, "_serial_input_frame", None):
            try:
                self._serial_input_frame.configure(bg=Theme.BG_DARK)
            except Exception:
                pass

        # Status bar frame
        if getattr(self, "_status_frame", None):
            try:
                self._status_frame.configure(bg=Theme.BG_DARK)
            except Exception:
                pass

        # Status labels (bg=BG_DARK to match their parent frame)
        for lbl in (
            getattr(self, "status_label", None),
            getattr(self, "editor_info_label", None),
        ):
            if lbl:
                try:
                    lbl.configure(fg=Theme.TEXT_DIM, bg=Theme.BG_DARK)
                except Exception:
                    pass

        # Shell switcher panel
        if getattr(self, "_shell_switcher", None):
            try:
                self._shell_switcher.configure(
                    bg=Theme.BG_DARKEST, highlightbackground=Theme.BORDER
                )
                for child in self._shell_switcher.winfo_children():
                    try:
                        if isinstance(child, tk.Label):
                            child.configure(fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST)
                        elif isinstance(child, tk.Button):
                            child.configure(
                                fg=Theme.TEXT, bg=Theme.BG_DARK,
                                activeforeground=Theme.TEXT_BRIGHT,
                                activebackground=Theme.BG_HOVER,
                            )
                    except Exception:
                        pass
            except Exception:
                pass

        # Labels inside the tab headers that use BG_MID bg
        for lbl in (
            getattr(self, "lbl_build_console_title", None),
            getattr(self, "lbl_serial_monitor_title", None),
            getattr(self, "lbl_syntax_status", None),
            getattr(self, "lbl_compat_title", None),
        ):
            if lbl:
                try:
                    lbl.configure(fg=Theme.CYAN, bg=Theme.BG_MID)
                except Exception:
                    pass

        for lbl in (
            getattr(self, "lbl_compat_status", None),
            getattr(self, "lbl_serial_baud", None),
        ):
            if lbl:
                try:
                    lbl.configure(fg=Theme.TEXT_DIM, bg=Theme.BG_MID)
                except Exception:
                    pass

        for lbl in (
            getattr(self, "serial_status", None),
        ):
            if lbl:
                try:
                    lbl.configure(bg=Theme.BG_MID)
                except Exception:
                    pass

        # Checkbuttons inside tab headers (cb_ansi_clear, cb_serial_autoscroll)
        for cb in (
            getattr(self, "cb_ansi_clear", None),
            getattr(self, "cb_serial_autoscroll", None),
            getattr(self, "cb_clear_serial_on_upload", None),
            getattr(self, "cb_clear_build_console_on_action", None),
            getattr(self, "cb_console_autoscroll", None),
        ):
            if cb:
                try:
                    cb.configure(
                        fg=Theme.TEXT, bg=Theme.BG_MID,
                        selectcolor=Theme.BG_DARK,
                        activebackground=Theme.BG_MID,
                        activeforeground=Theme.TEXT,
                    )
                except Exception:
                    pass

        # Buttons inside tab headers
        for btn in (
            getattr(self, "btn_clear_console_header", None),
            getattr(self, "btn_copy_console_header", None),
            getattr(self, "btn_clear_serial_header", None),
            getattr(self, "btn_copy_serial_header", None),
            getattr(self, "btn_compat_copy", None),
            getattr(self, "btn_pause_serial", None),
        ):
            if btn:
                self._restyle_btn(btn, Theme.BTN_CLEAR, Theme.BTN_CLEAR_H)

        # Serial input field
        if getattr(self, "serial_input", None):
            try:
                self.serial_input.configure(
                    bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
                    insertbackground=Theme.CYAN,
                    selectbackground=Theme.CYAN_DIM,
                    highlightcolor=Theme.CYAN_DIM,
                    highlightbackground=Theme.BORDER,
                )
            except Exception:
                pass

        # Notif console
        if getattr(self, "notif_console", None):
            try:
                self.notif_console.configure(
                    bg=Theme.BG_DARK, fg=Theme.TEXT,
                    insertbackground=Theme.TEXT,
                    selectbackground=Theme.BG_HOVER,
                    selectforeground=Theme.TEXT_BRIGHT,
                )
                for tag_name, color in [
                    ("timestamp", Theme.TEXT_DIM), ("dim", Theme.TEXT_DIM),
                    ("info", Theme.CYAN), ("success", Theme.GREEN),
                    ("warning", Theme.YELLOW), ("error", Theme.RED),
                    ("system", Theme.PURPLE), ("header", Theme.ORANGE),
                ]:
                    self.notif_console.tag_configure(tag_name, foreground=color)
            except Exception:
                pass

        # Syntax treeview style
        try:
            style = ttk.Style()
            style.configure(
                "Syntax.Treeview",
                background=Theme.BG_DARK, foreground=Theme.TEXT,
                fieldbackground=Theme.BG_DARK,
            )
            style.configure(
                "Syntax.Treeview.Heading",
                background=Theme.BG_MID, foreground=Theme.TEXT_BRIGHT,
            )
        except Exception:
            pass
        if getattr(self, "syntax_tree", None):
            try:
                self.syntax_tree.tag_configure("error", foreground=Theme.RED)
                self.syntax_tree.tag_configure("warning", foreground=Theme.ORANGE)
            except Exception:
                pass


        for s in (
            getattr(self, "status_bar", None),
            getattr(self, "lbl_status", None),
            getattr(self, "lbl_ports_status", None),
            getattr(self, "lbl_editor_status", None),
            getattr(self, "lbl_chip_info", None),
        ):
            if s:
                try:
                    s.configure(bg=Theme.BG_DARKEST)
                except Exception:
                    pass

        if hasattr(self, "editor_text") and self.editor_text:
            try:
                self.editor_text.configure(
                    bg=Theme.BG_DARKEST,
                    fg=Theme.TEXT_BRIGHT,
                    insertbackground=Theme.CYAN,
                    selectbackground=Theme.BG_HOVER,
                    selectforeground=Theme.TEXT_BRIGHT,
                )
            except Exception:
                pass
        if hasattr(self, "editor_lineno") and self.editor_lineno:
            try:
                self.editor_lineno.configure(bg=Theme.BG_DARK, fg=Theme.TEXT_DIM)
            except Exception:
                pass
        if hasattr(self, "editor_tab_frame") and self.editor_tab_frame:
            try:
                self.editor_tab_frame.configure(bg=Theme.BG_DARK)
            except Exception:
                pass

        try:
            style = ttk.Style()
            style.configure(
                "TCombobox",
                fieldbackground=Theme.BG_LIGHT,
                background=Theme.BG_HOVER,
                foreground=Theme.TEXT_BRIGHT,
                selectbackground=Theme.CYAN_DIM,
                selectforeground=Theme.TEXT_BRIGHT,
                bordercolor=Theme.BORDER,
                arrowcolor=Theme.TEXT_BRIGHT,
            )
            style.map(
                "TCombobox",
                fieldbackground=[("readonly", Theme.BG_LIGHT), ("focus", Theme.BG_LIGHT)],
                foreground=[("readonly", Theme.TEXT_BRIGHT), ("focus", Theme.TEXT_BRIGHT)],
                selectbackground=[("readonly", Theme.CYAN_DIM)],
                selectforeground=[("readonly", Theme.TEXT_BRIGHT)],
                bordercolor=[("focus", Theme.BORDER_LIT), ("hover", Theme.BORDER_LIT)],
            )
            style.configure(
                "Vertical.TScrollbar",
                background=Theme.BG_MID,
                troughcolor=Theme.BG_DARKEST,
                bordercolor=Theme.BORDER,
                arrowcolor=Theme.TEXT_BRIGHT,
            )
            style.configure(
                "Horizontal.TScrollbar",
                background=Theme.BG_MID,
                troughcolor=Theme.BG_DARKEST,
                bordercolor=Theme.BORDER,
                arrowcolor=Theme.TEXT_BRIGHT,
            )
            style.configure(
                "TNotebook",
                background=Theme.BG_DARKEST,
                bordercolor=Theme.BORDER,
                darkcolor=Theme.BG_DARKEST,
                lightcolor=Theme.BG_DARKEST,
            )
            style.configure(
                "TNotebook.Tab",
                background=Theme.BG_DARK,
                foreground=Theme.TEXT_DIM,
                bordercolor=Theme.BORDER,
                lightcolor=Theme.BG_DARK,
                darkcolor=Theme.BG_DARK,
                padding=[12, 4],
            )
            style.map(
                "TNotebook.Tab",
                background=[("selected", Theme.BG_DARKEST), ("active", Theme.BG_HOVER)],
                foreground=[("selected", Theme.CYAN), ("active", Theme.TEXT_BRIGHT)],
                bordercolor=[("selected", Theme.BORDER_LIT), ("active", Theme.BORDER_LIT)],
            )
            style.configure(
                "Bottom.TNotebook",
                background=Theme.BG_DARK,
                borderwidth=0,
                tabmargins=[2, 4, 0, 0],
            )
            style.configure(
                "Bottom.TNotebook.Tab",
                background=Theme.BG_MID,
                foreground=Theme.TEXT_DIM,
                bordercolor=Theme.BORDER,
                lightcolor=Theme.BG_MID,
                darkcolor=Theme.BG_MID,
                padding=[12, 5],
                font=("Segoe UI", 9, "bold"),
            )
            style.map(
                "Bottom.TNotebook.Tab",
                background=[("selected", Theme.BG_HOVER), ("active", Theme.BG_LIGHT)],
                foreground=[("selected", Theme.CYAN), ("active", Theme.TEXT_BRIGHT)],
                bordercolor=[("selected", Theme.BORDER_LIT), ("active", Theme.BORDER_LIT)],
            )
        except Exception:
            pass

        if getattr(self, "editor_mode", "default") == "monaco" and hasattr(self, "editor_window") and self.editor_window:
            try:
                self.editor_window.evaluate_js(
                    f"if (typeof window.setEditorTheme === 'function') window.setEditorTheme('{active_mode}');"
                )
            except Exception as e:
                print(f"[MCU Flasher] Error applying theme to Monaco: {e}")

        try:
            self.root.option_add("*TCombobox*Listbox.background", Theme.BG_LIGHT)
            self.root.option_add("*TCombobox*Listbox.foreground", Theme.TEXT_BRIGHT)
            self.root.option_add("*TCombobox*Listbox.selectBackground", Theme.BG_HOVER)
            self.root.option_add("*TCombobox*Listbox.selectForeground", Theme.CYAN)
            self.root.option_add("*Listbox.background", Theme.BG_LIGHT)
            self.root.option_add("*Listbox.foreground", Theme.TEXT_BRIGHT)
            self.root.option_add("*Listbox.selectBackground", Theme.BG_HOVER)
            self.root.option_add("*Listbox.selectForeground", Theme.CYAN)
        except Exception:
            pass

    def _get_current_monitor_dimensions(self):
        """Return the work-area dimensions of the monitor containing the app."""
        left, top, right, bottom = _get_monitor_work_area(self.root)
        return max(1, right - left), max(1, bottom - top)

    def _get_window_dpi_scale(self) -> float:
        return _get_widget_dpi_scale(self.root)

    @staticmethod
    def _minimum_width_for_display(
        screen_width: int, screen_height: int, display_scale: float = 1.0
    ) -> int:
        """Half-screen normally; compact but on-screen for portrait displays."""
        screen_width = max(1, int(screen_width))
        screen_height = max(1, int(screen_height))
        display_scale = max(0.75, min(3.0, float(display_scale or 1.0)))
        safe_margin = min(screen_width // 4, round(24 * display_scale))
        usable_width = max(240, screen_width - safe_margin)
        compact_floor = round(320 * display_scale)
        minimum = max(compact_floor, screen_width // 2)
        if screen_height > screen_width and screen_width < 900:
            minimum = max(minimum, min(round(560 * display_scale), usable_width))
        return min(minimum, usable_width)

    def _on_root_configure(self, event):
        """Debounced handler for live window-resize rescaling of buttons."""
        if event.widget is not self.root:
            return
        for attr in ("_resize_after_id", "_minwidth_after_id"):
            aid = getattr(self, attr, None)
            if aid:
                try:
                    self.root.after_cancel(aid)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._resize_after_id = self.root.after(150, self._apply_dynamic_button_scale)
        self._minwidth_after_id = self.root.after(150, self._update_min_window_width)

    def _update_min_window_width(self):
        """Set root minsize width to half the screen the window is currently on.
        Called on every Configure event (move/resize), debounced 150 ms.
        This keeps the window usable when dragged between monitors of different
        resolutions (e.g. 1920 px ↔ 1366 px)."""
        self._minwidth_after_id = None
        sw, sh = self._get_current_monitor_dimensions()
        display_scale = self._get_window_dpi_scale()
        new_min_w = self._minimum_width_for_display(sw, sh, display_scale)
        logical_screen_h = sh / display_scale
        logical_min_h = min(460, max(400, logical_screen_h - 120))
        safe_margin_h = min(sh // 4, round(48 * display_scale))
        new_min_h = min(
            round(logical_min_h * display_scale), max(260, sh - safe_margin_h)
        )
        min_key = (new_min_w, new_min_h, round(display_scale, 3))
        if min_key == getattr(self, "_last_minimum_geometry", None):
            return
        self._last_minimum_geometry = min_key
        self._last_minwidth = new_min_w
        self._min_window_height = new_min_h
        self._display_scale = display_scale
        self.screen_w, self.screen_h = sw, sh
        self.root.minsize(new_min_w, new_min_h)

    def _apply_dynamic_button_scale(self):
        """Shrink (or restore) button font/padding based on the current
        window width, on top of the static screen-resolution scale computed
        at startup. Lets buttons keep auto-resizing as the user resizes the
        window, not just once when the app launches."""
        self._resize_after_id = None
        try:
            width = self.root.winfo_width()
            height = self.root.winfo_height()
        except Exception:
            return
        if width <= 1:
            return
        self._apply_responsive_layout(width, height)

        # How much the current window width has shrunk relative to a
        # reasonable reference width for this screen.
        display_scale = self._get_window_dpi_scale()
        self._display_scale = display_scale
        logical_width = width / display_scale
        logical_screen_w = self.screen_w / display_scale
        logical_screen_h = self.screen_h / display_scale
        self._ui_scale = max(
            0.65,
            min(1.0, min(logical_screen_w / 1920.0, logical_screen_h / 1080.0)),
        )
        ref_width = max(900, min(logical_screen_w, 1920))
        dyn_factor = max(0.75, min(1.0, logical_width / ref_width))
        scale = max(0.6, min(1.0, self._ui_scale * dyn_factor))

        scale_key = (round(scale, 3), round(display_scale, 3))
        if scale_key == getattr(self, "_last_applied_layout_scale", None):
            return  # avoid churn for tiny resize deltas
        self._last_applied_layout_scale = scale_key
        self._last_applied_scale = scale

        new_btn_size = max(7, round(9 * scale))
        self.font_btn.configure(size=new_btn_size)

        try:
            self.font_title.configure(size=max(11, round(15 * scale)))
            self.font_subtitle.configure(size=max(8, round(9 * scale)))
            self.font_label.configure(size=max(8, round(9 * scale)))
            self.font_mono.configure(size=max(8, round(10 * scale)))
            self.font_mono_sm.configure(size=max(8, round(9 * scale)))
            self.font_status.configure(size=max(8, round(9 * scale)))
            if getattr(self, "_shell_bold_font", None):
                self._shell_bold_font.configure(
                    size=max(8, round(10 * scale))
                )
        except Exception:
            pass

        self._btn_padx = max(
            round(6 * display_scale), round(10 * scale * display_scale)
        )
        self._btn_pady = max(
            round(2 * display_scale), round(3 * scale * display_scale)
        )
        for btn in self._scalable_buttons:
            try:
                btn.configure(padx=self._btn_padx, pady=self._btn_pady)
            except Exception:
                pass

    def _apply_responsive_layout(self, width: int, height: int):
        """Keep the packed controls and vertical panes usable as the window
        changes size.  Widget widths are expressed in characters, so adapting
        them here is more reliable across DPI scales than fixed pixels."""
        actual_width = width
        display_scale = self._get_window_dpi_scale()
        self._display_scale = display_scale
        width = max(1, round(width / display_scale))
        height = max(1, round(height / display_scale))

        # Tk emits Configure events continuously while a window is dragged.
        # Repacking the whole toolbar and recreating ttk styles for every one
        # of those events is expensive on low-end machines and can starve
        # paint/input long enough to look like a frozen application.  The
        # responsive layout only changes at these actual UI breakpoints; font
        # scaling and the lightweight AI pane width update still happen below.
        width_breakpoints = (520, 650, 680, 700, 760, 820, 850, 900,
                             950, 1000, 1150, 1200, 1400, 1450)
        width_bucket = tuple(int(width >= point) for point in width_breakpoints)
        height_bucket = (int(height >= 560), int(height >= 720))
        layout_key = (
            width_bucket,
            height_bucket,
            round(display_scale, 3),
            bool(getattr(self, "editor_detached", False)),
            bool(getattr(self, "_ai_side_visible", False)),
            bool(getattr(self, "editor_pane_visible", True)),
            bool(getattr(self, "monitors_pane_visible", True)),
        )
        if layout_key == getattr(self, "_last_responsive_layout_key", None):
            if getattr(self, "_ai_side_visible", False):
                try:
                    ai_min = round(220 * display_scale)
                    main_min = round(300 * display_scale)
                    ai_width = max(
                        ai_min,
                        min(int(actual_width * 0.44), max(ai_min, actual_width - main_min)),
                    )
                    self.h_split_pane.paneconfigure(self.ai_side_container, width=ai_width)
                except Exception:
                    pass
            return
        self._last_responsive_layout_key = layout_key

        # Below this width the title bar must give priority to the one-row
        # action toolbar. Compact labels reclaim space without hiding actions.
        compact = width < 1200
        if width < 650:
            port_chars = 8
            board_chars = 12
            baud_chars = 7
            upload_spd_chars = 6
        elif width < 760:
            port_chars = 11
            board_chars = 16
            baud_chars = 8
            upload_spd_chars = 7
        elif width < 850:
            port_chars = 14
            board_chars = 20
            baud_chars = 8
            upload_spd_chars = 8
        elif width < 1150:
            port_chars = 20
            board_chars = 26
            baud_chars = 10
            upload_spd_chars = 10
        elif width < 1450:
            port_chars = 26
            board_chars = 34
            baud_chars = 10
            upload_spd_chars = 10
        else:
            port_chars = 34
            board_chars = 42
            baud_chars = 10
            upload_spd_chars = 10

        try:
            self.port_combo.configure(width=port_chars)
            self.board_combo.entry.configure(width=board_chars)
            if hasattr(self, "serial_baud_combo"):
                self.serial_baud_combo.configure(width=baud_chars)
            self.upload_speed_combo.configure(width=upload_spd_chars)
        except Exception:
            pass

        # Keep the controls-bar background edge-to-edge (padx=0) so no black empty gaps appear
        # on either end, while configuring inner left/right inset gutters.
        try:
            self.ctrl_row_top.master.pack_configure(padx=0)
            left_pad = round((12 if width < 700 else (16 if width < 1400 else 20)) * display_scale)
            right_pad = round((12 if width < 700 else (16 if width < 1400 else 20)) * display_scale)
            inner_gap = round((4 if width < 700 else 8) * display_scale)

            self.board_group.pack_configure(padx=(left_pad, inner_gap))
            self.port_group.pack_configure(padx=(0, inner_gap))
            self.upload_spd_group.pack_configure(padx=(0, inner_gap))
            if hasattr(self, "right_group") and self.right_group:
                self.right_group.pack_configure(padx=(0, right_pad))
        except Exception:
            pass

        # On very narrow windows the UPLOAD SPD label is the single
        # biggest fixed-width item in the bar (10 characters where BOARD/
        # PORT/BAUD are 4-5); abbreviate it once real estate gets tight
        # rather than letting it force everything else back into overflow.
        try:
            lbl_upload_spd_text = "SPD" if width < 700 else "UPLOAD SPD"
            self._lbl_upload_spd.configure(text=lbl_upload_spd_text)
        except Exception:
            pass

        # Project names used to push the title-bar actions out of view after
        # shrinking a window on a large monitor.
        try:
            self._update_sketch_marquee()
        except Exception:
            pass

        try:
            if width < 1000:
                self.lbl_sketch.pack_forget()
                self.lbl_app_title.configure(
                    text="⚡ MCU" if width < 520 else (
                        "⚡ MCU Flasher" if width < 680 else "⚡ MCU Flasher by Naph"
                    )
                )
                self.btn_download_mgr.configure(text="↓", width=3)
            else:
                if not self.lbl_sketch.winfo_ismapped():
                    self.lbl_sketch.pack(side=tk.LEFT, after=self.lbl_sketch_icon)
                self.lbl_app_title.configure(text="⚡ MCU Flasher by Naph")
                self.btn_download_mgr.configure(width=0)
        except Exception:
            pass

        # Dynamic reflow of Title Bar elements based on width
        try:
            if width < 1150:
                # Compact half-screen mode. Actions remain in the title row;
                # only their contents collapse into Compile/Upload/Actions.
                # 1. Unpack from their wide-mode container frames
                self.actions_frame.pack_forget()
                self.btn_compile.pack_forget()
                self.btn_upload.pack_forget()
                self.btn_settings.pack_forget()
                
                # 2. Pack Compile, Upload on top_compact_inner (same row on top)
                detached_compact = bool(getattr(self, "editor_detached", False))
                self.btn_compile.pack_forget()
                self.btn_upload.pack_forget()
                self._actions_dropdown_btn.pack_forget()
                if detached_compact:
                    # The detached editor owns the complete action toolbar in
                    # this exact mode; leave no duplicate actions in the GUI.
                    self.top_compact_actions.pack_forget()
                    self._close_actions_dropdown()
                else:
                    self.btn_compile.pack(in_=self.top_compact_inner, side=tk.LEFT, padx=3)
                    self.btn_upload.pack(in_=self.top_compact_inner, side=tk.LEFT, padx=3)
                    self._actions_dropdown_btn.pack(in_=self.top_compact_inner, side=tk.LEFT, padx=(6, 3))
                    compact_action_width = 12 if width >= 900 else (9 if width >= 650 else 7)
                    self.btn_compile.configure(width=compact_action_width)
                    self.btn_upload.configure(width=compact_action_width)
                    self._actions_dropdown_btn.configure(width=compact_action_width)
                    if self.title_row_bottom.winfo_ismapped():
                        self.title_row_bottom.pack_forget()
                    self.top_compact_actions.pack(
                        in_=self.title_row_top, side=tk.LEFT, fill=tk.BOTH,
                        expand=True
                    )

                # 3. Clean up inner_actions (unpacking actions label and Compile/Upload buttons so they don't appear in the bottom actions row)
                self.lbl_actions_title.pack_forget()
                
                # Settings button stays in options row
                self.btn_settings.pack(in_=self.opt_buttons_frame, side=tk.LEFT, padx=(0, 8))
                self.btn_settings.configure(font=self.font_btn, width=0)
            else:
                # Wide Title Bar mode: Put action buttons in the same row as logo and sketch path, settings in options bar
                if self.title_row_bottom.winfo_ismapped():
                    self.title_row_bottom.pack_forget()
                if self.top_compact_actions.winfo_ismapped():
                    self.top_compact_actions.pack_forget()
                
                self.btn_compile.pack_forget()
                self.btn_upload.pack_forget()
                self._actions_dropdown_btn.pack_forget()
                self._close_actions_dropdown()
                self.btn_settings.pack_forget()
                
                # Unpack everything in inner_actions so we can pack them in the correct original order
                self.lbl_actions_title.pack_forget()
                self.btn_stop.pack_forget()
                self.btn_clean.pack_forget()
                self.title_divider.pack_forget()
                self.btn_save.pack_forget()
                self.btn_save_all.pack_forget()
                self.btn_reload_file.pack_forget()
                self.btn_modify_files.pack_forget()
                
                # Repack in inner_actions (correct wide-mode order)
                self.lbl_actions_title.pack(in_=self.inner_actions, side=tk.LEFT, padx=(0, 8))
                self.btn_compile.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)
                self.btn_upload.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)
                self.btn_compile.configure(width=0)
                self.btn_upload.configure(width=0)
                self._actions_dropdown_btn.configure(width=0)
                self.btn_stop.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)
                self.btn_clean.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)
                self.title_divider.pack(in_=self.inner_actions, side=tk.LEFT, padx=8)
                self.btn_save.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)
                self.btn_save_all.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)
                self.btn_reload_file.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)
                self.btn_modify_files.pack(in_=self.inner_actions, side=tk.LEFT, padx=3)

                if getattr(self, "editor_detached", False):
                    # The detached editor is the sole home for actions.
                    self.actions_frame.pack_forget()
                else:
                    self.actions_frame.pack(in_=self.title_row_top, side=tk.LEFT, fill=tk.BOTH, expand=True)
                
                # Repack btn_settings back to options frame and set standard font
                self.btn_settings.pack(in_=self.opt_buttons_frame, side=tk.LEFT, padx=(0, 8))
                self.btn_settings.configure(font=self.font_btn, width=0)
        except Exception:
            pass

        self._sync_detached_compact_actions(width)

        # Dynamic reflow of Controls Bar elements based on width
        try:
            # Strictly one row: Board, Port, Upload Speed, and Options never
            # move vertically. Narrow layouts shrink/collapse their contents.
            self.right_group.pack_forget()
            if self.ctrl_row_bottom.winfo_ismapped():
                self.ctrl_row_bottom.pack_forget()
            self.right_group.pack(in_=self.ctrl_row_top, side=tk.RIGHT, padx=(0, right_pad))

            # OPTIONS title centered above, content in one row below
            self.lbl_options_title.pack_forget()
            self.opt_row.pack_forget()
            self.lbl_options_title.pack(side=tk.TOP, pady=(0, 2))
            self.opt_row.pack(side=tk.TOP)

            # Checkboxes always visible
            self.opt_checkboxes_frame.pack_forget()
            self.opt_checkboxes_frame.pack(side=tk.LEFT, padx=(0, 8))

            # On 1366x768-class displays the full names crowd the controls
            # bar. Keep the concise names there (and on manually narrowed
            # windows), while larger displays retain the descriptive labels.
            self.cb_timestamp.pack_forget()
            self.cb_skip_compile.pack_forget()
            use_short_labels = width < 820
            if use_short_labels:
                self.cb_timestamp.configure(text="TS")
                self.cb_skip_compile.configure(text="Skip")
            else:
                self.cb_timestamp.configure(text="Time Stamp")
                self.cb_skip_compile.configure(text="Skip Compile")
            self.cb_timestamp.pack(side=tk.LEFT, padx=(0, 8))
            self.cb_skip_compile.pack(side=tk.LEFT, padx=(0, 8))

            compact_buttons = width < 1200
            if compact_buttons:
                # Compact buttons: collapse option buttons into a dropdown trigger
                self.opt_buttons_frame.pack_forget()
                self._opt_dropdown_btn.pack_forget()
                self._opt_dropdown_btn.pack(in_=self.opt_row, side=tk.LEFT, padx=(4, 0))
            else:
                # Wide buttons: show all option buttons inline
                self._opt_dropdown_btn.pack_forget()
                self._close_options_dropdown()
                self.opt_buttons_frame.pack_forget()
                self.opt_buttons_frame.pack(side=tk.LEFT)
        except Exception:
            pass

        # Dynamic adjustments of Build Console and Serial Monitor toolbars based on width/height
        try:
            if width < 700:
                self.lbl_build_console_title.pack_forget()
                self.cb_clear_serial_on_upload.configure(text="Clear serial")
                self.cb_clear_build_console_on_action.configure(text="Clear build")
                self.cb_console_autoscroll.configure(text="Scroll")
                self.btn_copy_console_header.configure(text="Copy")
                self.btn_clear_console_header.configure(text="Clear")
            else:
                if not self.lbl_build_console_title.winfo_ismapped():
                    self.lbl_build_console_title.pack(side=tk.LEFT)
                self.cb_clear_serial_on_upload.configure(text="Auto-clear Serial Monitor on Action")
                self.cb_clear_build_console_on_action.configure(text="Clear Screen on Action")
                self.cb_console_autoscroll.configure(text="Auto-scroll")

            if width < 950:
                self.lbl_serial_monitor_title.pack_forget()
            else:
                if not self.lbl_serial_monitor_title.winfo_ismapped():
                    self.lbl_serial_monitor_title.pack_forget()
                    self.btn_reset_mcu.pack_forget()
                    self.btn_pause_serial.pack_forget()
                    self.lbl_serial_monitor_title.pack(side=tk.LEFT)
                    self.btn_reset_mcu.pack(side=tk.LEFT, padx=(10, 0))
                    self.btn_pause_serial.pack(side=tk.LEFT, padx=(6, 0))

            serial_tight = width < 820
            self.lbl_serial_baud.configure(text="" if serial_tight else "BAUD RATE")
            st = getattr(self, "_serial_status_state", "disconnected")
            if st == "connected":
                status_text = "● Connected"
            elif st == "reconnecting":
                status_text = "● Reconnecting..."
            else:
                status_text = "● Disconnected"

            self.serial_status.configure(
                text="●" if serial_tight else status_text
            )
            self.btn_reset_mcu.configure(text="Reset" if serial_tight else "↻ Reset")
            self.btn_pause_serial.configure(
                text="Resume" if self._monitor_paused else ("Pause" if serial_tight else "⏸ Pause")
            )
            self.btn_copy_serial_header.configure(text="Copy")
            self.btn_clear_serial_header.configure(text="Clear")
            if serial_tight:
                self.cb_serial_autoscroll.pack_forget()
                self.cb_ansi_clear.pack_forget()
            else:
                if not self.cb_ansi_clear.winfo_ismapped():
                    self.cb_ansi_clear.pack(side=tk.RIGHT, padx=(0, 10))
                if not self.cb_serial_autoscroll.winfo_ismapped():
                    self.cb_serial_autoscroll.pack(side=tk.RIGHT, padx=(0, 10))

            style = ttk.Style()
            style.configure(
                "Bottom.TNotebook.Tab",
                # Use one stable padding value for every state so switching
                # tabs cannot change their apparent size.  Keep the compact
                # layout on narrow windows, but reduce the desktop maximum a
                # little without making the labels cramped.
                padding=[8 if width < 700 else 12, 4 if height < 600 else 5],
                font=("Segoe UI", 8 if width < 700 else 9, "bold"),
            )

            if width < 700:
                self.editor_info_label.pack_forget()
            elif not self.editor_info_label.winfo_ismapped():
                self.editor_info_label.pack(side=tk.RIGHT)
        except Exception:
            pass

        # Keep every action in one row, but shorten surrounding title-bar
        # content and the action captions before there is any clipping.
        try:
            if self.title_subtitle_label is not None:
                if compact:
                    self.title_subtitle_label.pack_forget()
                elif not self.title_subtitle_label.winfo_ismapped():
                    self.title_subtitle_label.pack(side=tk.LEFT, pady=(4, 0))

            if getattr(self, "_action_compact_mode", None) != compact:
                self._action_compact_mode = compact
                if compact:
                    labels = {
                        "btn_compile": "Compile", "btn_upload": "Upload",
                        "btn_stop": "Stop", "btn_clean": "Clean",
                        "btn_save": "Save", "btn_save_all": "Save+",
                        "btn_reload_file": "Reload", "btn_modify_files": "Modify",
                    }
                    self.btn_download_mgr.configure(text="↓" if width < 1000 else "Download")
                else:
                    labels = {
                        "btn_compile": "Compile", "btn_upload": "Upload",
                        "btn_stop": "Stop", "btn_clean": "Clean",
                        "btn_save": "Save", "btn_save_all": "Save All",
                        "btn_reload_file": "Reload", "btn_modify_files": "Modify",
                    }
                    self.btn_download_mgr.configure(text="Download Boards/Libraries")
                for attr, text in labels.items():
                    getattr(self, attr).configure(text=text)
        except Exception:
            pass

        # A monitor needs space for its tab header, toolbar, text area, and
        # serial-send row.  Keep both panes above those practical floors.
        if height < 560:
            editor_min, monitor_min = 72, 105
        elif height < 720:
            editor_min, monitor_min = 100, 145
        else:
            editor_min, monitor_min = self._editor_minsize, self._bottom_minsize
        try:
            if getattr(self, "editor_pane_visible", True):
                self.main_pane.paneconfigure(
                    self.editor_frame, minsize=round(editor_min * display_scale)
                )
            if getattr(self, "monitors_pane_visible", True):
                self.main_pane.paneconfigure(
                    self.bottom_frame, minsize=round(monitor_min * display_scale)
                )
            if getattr(self, "_ai_side_visible", False):
                ai_min = round(220 * display_scale)
                main_min = round(300 * display_scale)
                ai_width = max(
                    ai_min,
                    min(int(actual_width * 0.44), max(ai_min, actual_width - main_min)),
                )
                self.h_split_pane.paneconfigure(self.ai_side_container, width=ai_width)
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────
    # OPTIONS DROPDOWN (compact mode)
    # ──────────────────────────────────────────────────────────
    def _toggle_options_dropdown(self):
        """Toggle a popup with option buttons for compact mode."""
        if self._opt_dropdown_win is not None:
            self._close_options_dropdown()
            return

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.configure(bg=Theme.BORDER)
        win.attributes("-topmost", True)

        inner = tk.Frame(win, bg=Theme.BG_MID, padx=8, pady=6)
        inner.pack(padx=1, pady=1)

        # Build buttons with current labels AND current enabled/disabled state
        # from the real buttons.  The compact popup is only a second view of the
        # same controls; it must never bypass the state enforced on the wide UI.
        # Each entry is: (label, action, normal_bg, hover_bg, source_widget).
        entries = []
        if sys.platform == "win32" and win32gui is not None:
            detached = getattr(self, "editor_detached", False)
            if detached:
                det_bg, det_bgh = "#e67e22", "#d35400"
            else:
                det_bg, det_bgh = "#2d7d46", "#38a058"
            entries.append((self.btn_detach_editor.cget("text"),
                            self._toggle_editor_detachment, det_bg, det_bgh,
                            self.btn_detach_editor))
        entries.append((self.btn_toggle_editor.cget("text"), self._toggle_editor_pane,
                        Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, self.btn_toggle_editor))
        entries.append((self.btn_toggle_monitors.cget("text"),
                        self._toggle_monitors_pane,
                        Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, self.btn_toggle_monitors))
        entries.append(("⚙ Settings", self._open_settings,
                        Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, self.btn_settings))
        
        if self.ai_controller:
            ai_label = "🤖 AI Assistant"
            ai_bg, ai_bgh = Theme.BTN_CLEAR, Theme.BTN_CLEAR_H
            ai_action = self._toggle_ai_side_panel
            if getattr(self, "_ai_side_visible", False):
                ai_label = "🤖 Hide AI"
            entries.append((ai_label, ai_action, ai_bg, ai_bgh,
                            getattr(self, "btn_ai_assistant", None)))

        for label, action, bg_c, bg_h, source_widget in entries:
            def _source_is_disabled(widget=source_widget):
                try:
                    return widget is not None and str(widget.cget("state")) == str(tk.DISABLED)
                except Exception:
                    return False

            def make_cb(a=action, source=source_widget):
                def cb():
                    self._close_options_dropdown()
                    # Re-check at click time as well.  An operation can begin
                    # after the popup was opened but before the user clicks.
                    try:
                        if source is not None and str(source.cget("state")) == str(tk.DISABLED):
                            return
                    except Exception:
                        pass
                    a()
                return cb

            disabled = _source_is_disabled()
            btn = tk.Button(
                inner, text=label, command=make_cb(),
                font=self.font_btn, fg=Theme.TEXT_BRIGHT, bg=bg_c,
                activebackground=bg_h, activeforeground=Theme.TEXT_BRIGHT,
                disabledforeground=Theme.TEXT_DIM,
                relief=tk.FLAT, borderwidth=0, padx=self._btn_padx, pady=self._btn_pady,
                cursor="arrow" if disabled else "hand2",
                anchor=tk.CENTER,
                state=tk.DISABLED if disabled else tk.NORMAL,
            )
            btn.pack(fill=tk.X, pady=2)
            if not disabled:
                btn.bind("<Enter>", lambda e, b=btn, c=bg_h: b.configure(bg=c))
                btn.bind("<Leave>", lambda e, b=btn, c=bg_c: b.configure(bg=c))

        # Position below the dropdown trigger button
        self.root.update_idletasks()
        bx = self._opt_dropdown_btn.winfo_rootx()
        by = (self._opt_dropdown_btn.winfo_rooty()
              + self._opt_dropdown_btn.winfo_height() + 2)
        win.update_idletasks()
        pw = win.winfo_reqwidth()
        
        # Keep dropdown inside the main window boundaries on the right
        rx = self.root.winfo_rootx() + self.root.winfo_width()
        if bx + pw > rx:
            bx = max(self.root.winfo_rootx(), rx - pw - 10) # 10px padding from right window edge
            
        win.geometry(f"+{bx}+{by}")

        self._opt_dropdown_win = win

        # Close when focus leaves the popup
        win.bind("<FocusOut>",
                 lambda e: self.root.after(120, self._maybe_close_dropdown))
        win.focus_set()

    def _maybe_close_dropdown(self):
        """Close dropdown if focus moved outside it."""
        if self._opt_dropdown_win is None:
            return
        try:
            fw = self.root.focus_get()
            if fw is None or not str(fw).startswith(
                    str(self._opt_dropdown_win)):
                self._close_options_dropdown()
        except Exception:
            self._close_options_dropdown()

    def _close_options_dropdown(self):
        """Destroy the options dropdown popup."""
        if self._opt_dropdown_win is not None:
            try:
                self._opt_dropdown_win.destroy()
            except Exception:
                pass
            self._opt_dropdown_win = None

    # ACTIONS DROPDOWN (compact mode)
    def _toggle_actions_dropdown(self):
        """Show the non-primary actions that are collapsed in compact mode."""
        if self._actions_dropdown_win is not None:
            self._close_actions_dropdown()
            return

        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.configure(bg=Theme.BORDER)
        win.attributes("-topmost", True)
        inner = tk.Frame(win, bg=Theme.BG_MID, padx=8, pady=6)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        entries = [
            (self.btn_stop, Theme.BTN_STOP, Theme.BTN_STOP_H),
            (self.btn_clean, Theme.BTN_CLEAR, Theme.BTN_CLEAR_H),
            (self.btn_save, Theme.BTN_COMPILE, Theme.BTN_COMPILE_H),
            (self.btn_save_all, Theme.BTN_FULL, Theme.BTN_FULL_H),
            (self.btn_reload_file, Theme.BTN_CLEAR, Theme.BTN_CLEAR_H),
            (self.btn_modify_files, Theme.BTN_MONITOR, Theme.BTN_MONITOR_H),
        ]
        for source, bg_c, bg_h in entries:
            def invoke(button=source):
                self._close_actions_dropdown()
                if str(button.cget("state")) != str(tk.DISABLED):
                    button.invoke()
            btn = tk.Button(inner, text=source.cget("text"), command=invoke,
                            font=self.font_btn, fg=Theme.TEXT_BRIGHT, bg=bg_c,
                            activebackground=bg_h, activeforeground=Theme.TEXT_BRIGHT,
                            relief=tk.FLAT, borderwidth=0, padx=self._btn_padx, pady=self._btn_pady,
                            cursor="hand2", anchor=tk.CENTER)
            if str(source.cget("state")) == str(tk.DISABLED):
                btn.configure(state=tk.DISABLED)
            btn.pack(fill=tk.X, pady=2)
            btn.bind("<Enter>", lambda e, b=btn, c=bg_h: b.configure(bg=c))
            btn.bind("<Leave>", lambda e, b=btn, c=bg_c: b.configure(bg=c))

        self.root.update_idletasks()
        x = self._actions_dropdown_btn.winfo_rootx()
        y = self._actions_dropdown_btn.winfo_rooty() + self._actions_dropdown_btn.winfo_height() + 2
        btn_w = self._actions_dropdown_btn.winfo_width()

        win.update_idletasks()
        win_h = win.winfo_reqheight()

        # Keep dropdown inside the main window boundaries on the right
        rx = self.root.winfo_rootx() + self.root.winfo_width()
        if x + btn_w > rx:
            x = max(self.root.winfo_rootx(), rx - btn_w - 10)

        win.geometry(f"{btn_w}x{win_h}+{max(self.root.winfo_rootx(), x)}+{y}")
        self._actions_dropdown_win = win
        win.bind("<FocusOut>", lambda e: self.root.after(120, self._maybe_close_actions_dropdown))
        win.focus_set()

    def _maybe_close_actions_dropdown(self):
        if self._actions_dropdown_win is None:
            return
        try:
            focus = self.root.focus_get()
            if focus is None or not str(focus).startswith(str(self._actions_dropdown_win)):
                self._close_actions_dropdown()
        except Exception:
            self._close_actions_dropdown()

    def _close_actions_dropdown(self):
        if self._actions_dropdown_win is not None:
            try:
                self._actions_dropdown_win.destroy()
            except Exception:
                pass
            self._actions_dropdown_win = None

