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

class EditorModesMixin(_Base):
    """Mixin providing EditorModesMixin capabilities for MCUUploadGUI."""
    def _build_editor(self, parent_frame):
        """Dispatch to the active editor implementation based on the
        configured editor mode ('default' Tkinter editor or 'monaco')."""
        mode = getattr(self, "editor_mode", "default")
        if mode == "monaco":
            self._build_editor_monaco(parent_frame)
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
        self._editor_fallback_ready = False

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
            font=tkfont.Font(family="Segoe UI", size=16, weight="bold"),
            fg=Theme.CYAN, bg=Theme.BG_DARKEST
        )
        status_lbl.pack(pady=10)
        self._editor_status_lbl = status_lbl

        desc_lbl = tk.Label(
            placeholder,
            text="Attaching the editor to this window…",
            font=tkfont.Font(family="Segoe UI", size=10),
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
            Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, font=tkfont.Font(family="Segoe UI", size=10, weight="bold")
        )
        # Only packed (shown) if/when embedding fails — see
        # _try_embed_editor_window below.

        # Keep the embedded editor window sized to match this frame.
        parent_frame.bind("<Configure>", self._resize_embedded_editor)

        # Initialize callbacks to evaluate_js on the webview window
        self._load_editor_files = lambda: self.editor_window.evaluate_js(
            "(window.safeLoadProject ? window.safeLoadProject() : window.loadProject())"
        ) if hasattr(self, "editor_window") else None
        self._save_all_editor_files = lambda: self.editor_window.evaluate_js("saveAllFiles()") if hasattr(self, "editor_window") else None
        self._save_current_editor_file = lambda: self.editor_window.evaluate_js("saveActiveFile()") if hasattr(self, "editor_window") else None
        self._reload_current_editor_file = lambda: self.editor_window.evaluate_js("reloadActiveFile()") if hasattr(self, "editor_window") else None

    def _build_editor_default(self, parent_frame):
        """Build the embedded tabbed code-editor container showing all .ino / .cpp / .h
        source files in the current sketch directory.
        """
        self._default_editor_ready = False
        self.editor_content_frame = tk.Frame(parent_frame, bg=Theme.BG_DARKEST)
        self.editor_content_frame.pack(fill=tk.BOTH, expand=True)
        parent_frame = self.editor_content_frame
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
            # Do not scan the entire document on every click/tab switch.  Remove
            # only the previously highlighted line, then tag the new one.
            previous = getattr(text_widget, "_mcu_active_line_range", None)
            if previous:
                try:
                    text_widget.tag_remove("active_line", previous[0], previous[1])
                except Exception:
                    pass
            start = text_widget.index("insert linestart")
            end = text_widget.index("insert lineend + 1c")
            text_widget.tag_add("active_line", start, end)
            text_widget._mcu_active_line_range = (start, end)

        # ── Find & Replace Panel & Logic ──────────────────────────────────
        find_panel = tk.Frame(parent_frame, bg=Theme.BG_MID, pady=6, padx=12)
        find_panel.columnconfigure(1, weight=1)
        find_panel.columnconfigure(3, weight=1)
        
        tk.Label(find_panel, text="Find:", font=self.font_label, fg=Theme.TEXT_DIM, bg=Theme.BG_MID).grid(row=0, column=0, sticky=tk.W, pady=2)
        find_ent = tk.Entry(find_panel, width=25, font=self.font_mono_sm, bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT, insertbackground=Theme.CYAN, borderwidth=0, highlightthickness=1, highlightcolor=Theme.CYAN_DIM, highlightbackground=Theme.BORDER)
        find_ent.grid(row=0, column=1, padx=6, pady=2, sticky=tk.EW)
        find_ent.bind("<Button-1>", lambda e: safe_reclaim_os_focus(find_ent), add="+")

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
        replace_ent.bind("<Button-1>", lambda e: safe_reclaim_os_focus(replace_ent), add="+")

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

            # C++ realtime syntax checker integration
            file_path = None
            for frame, data in tab_data.items():
                if data["text"] == text_widget:
                    file_path = data["path"]
                    break
            if file_path and file_path.suffix in (".ino", ".cpp", ".h"):
                self._run_realtime_syntax_check(text_widget, file_path)
                self._schedule_syntax_ui_update()

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
            cursor_str = f"Ln {ln}, Col {int(col)+1}"
            lbl_cursor.config(text=cursor_str)
            self._update_editor_info(cursor_str)

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
                self._set_symbol_cache_compiled_state(False)
            else:
                self._update_skip_compile_state()

        # ── Save helpers ──────────────────────────────────────────────────
        def _save_tab(frame):
            data = tab_data.get(frame)
            if data is None:
                return
            existing_autosave = _autosave_after_ids.pop(frame, None)
            if existing_autosave:
                try:
                    self.root.after_cancel(existing_autosave)
                except Exception:
                    pass
            content = data["text"].get("1.0", tk.END)
            # Strip the trailing newline Tk always appends
            if content.endswith("\n"):
                content = content[:-1]
            try:
                data["path"].write_text(content, encoding="utf-8")
                if getattr(self, "ai_controller", None):
                    self.ai_controller.note_local_save(data["path"], content)
                data["original"] = content + "\n"   # match Tk's representation
                data["modified"] = False
                nb.tab(frame, text=data["path"].name)
                _set_editor_status(f"✔ Saved — {data['path'].name}")
                # Invalidate compile cache so the GUI knows sources changed
                self._compile_cache_hash = None
                self._update_skip_compile_state()
                self._run_manual_syntax_check()
            except Exception as exc:
                _set_editor_status(f"✖ Save failed: {exc}", color=Theme.RED, ms=5000)

        # ── Auto-save (idle-triggered, configurable via Settings) ──────────
        # Per-tab debounce: every keystroke pushes the save further out until
        # the user has been idle for `self.autosave_delay_ms`. Controlled by
        # self.autosave_enabled / self.autosave_delay_ms, which are loaded at
        # startup and refreshed live from the Settings dialog (see
        # _open_settings / save_settings) without needing a restart.
        _autosave_after_ids = {}

        def _schedule_autosave(frame):
            if not getattr(self, "autosave_enabled", False):
                return
            existing = _autosave_after_ids.pop(frame, None)
            if existing:
                try:
                    self.root.after_cancel(existing)
                except Exception:
                    pass

            def _do_autosave(f=frame):
                _autosave_after_ids.pop(f, None)
                data = tab_data.get(f)
                if data and data.get("modified"):
                    _save_tab(f)
                    _set_editor_status(f"💾 Auto-saved — {data['path'].name}", Theme.CYAN)

            delay_ms = max(200, int(getattr(self, "autosave_delay_ms", 1500)))
            _autosave_after_ids[frame] = self.root.after(delay_ms, _do_autosave)

        def _cancel_autosave(frame):
            existing = _autosave_after_ids.pop(frame, None)
            if existing:
                try:
                    self.root.after_cancel(existing)
                except Exception:
                    pass

        # Exposed so Settings can react instantly to the checkbox being
        # unticked mid-session (cancel any pending autosave timers) and so
        # tab-closing logic elsewhere can cancel a stale timer.
        self._autosave_cancel_all = lambda: [
            _cancel_autosave(f) for f in list(_autosave_after_ids.keys())
        ]

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
            txt.edit_reset()
            data["original"] = txt.get("1.0", tk.END)
            data["modified"] = False
            nb.tab(frame, text=data["path"].name)
            _highlight(txt)
            _sync_linenos(txt, data["lineno_text"])
            _set_editor_status(f"✔ Reloaded — {data['path'].name}")
            self._update_skip_compile_state()

        # ── Periodic Reload timer (re-reads all open tabs from disk) ──────
        # Controlled by self.periodic_reload_enabled / periodic_reload_interval_s
        # which are loaded at startup and refreshed live from the Settings
        # dialog. Only reloads tabs whose on-disk content actually differs
        # from the editor buffer, so the UI cost is near-zero when files
        # haven't changed externally.

        def _reload_default_tabs_if_changed():
            for frame, d in list(tab_data.items()):
                if d.get("modified"):
                    continue  # skip unsaved user edits

                try:
                    disk_content = d["path"].read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue  # file deleted / inaccessible — skip silently

                current_content = d["text"].get("1.0", "end-1c")
                if disk_content == current_content:
                    continue  # no change on disk — skip

                # Save cursor position & scroll to restore after reload
                try:
                    cursor_pos = d["text"].index(tk.INSERT)
                except Exception:
                    cursor_pos = "1.0"
                try:
                    scroll_pos = d["text"].yview()
                except Exception:
                    scroll_pos = None

                d["text"].delete("1.0", tk.END)
                d["text"].insert("1.0", disk_content)
                d["text"].edit_reset()
                d["original"] = d["text"].get("1.0", tk.END)
                d["modified"] = False
                nb.tab(frame, text=d["path"].name)
                _highlight(d["text"])
                _sync_linenos(d["text"], d["lineno_text"])

                # Restore cursor & scroll
                try:
                    d["text"].mark_set(tk.INSERT, cursor_pos)
                    d["text"].see(cursor_pos)
                except Exception:
                    pass
                if scroll_pos:
                    try:
                        d["text"].yview_moveto(scroll_pos[0])
                    except Exception:
                        pass

        self._reload_default_tabs_if_changed = _reload_default_tabs_if_changed

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

        # ── Symbol search & Hover Card helper for Default Editor ──────────
        _DEFAULT_HOVER_STATE = {"win": None, "timer": None, "last_word": ""}

        BUILTIN_ARDUINO_DEFS = {
            'pinMode': {'kind': 'function', 'return_type': 'void', 'params': ['uint8_t pin', 'uint8_t mode'], 'prototype': 'void pinMode(uint8_t pin, uint8_t mode)'},
            'digitalWrite': {'kind': 'function', 'return_type': 'void', 'params': ['uint8_t pin', 'uint8_t val'], 'prototype': 'void digitalWrite(uint8_t pin, uint8_t val)'},
            'digitalRead': {'kind': 'function', 'return_type': 'int', 'params': ['uint8_t pin'], 'prototype': 'int digitalRead(uint8_t pin)'},
            'analogWrite': {'kind': 'function', 'return_type': 'void', 'params': ['uint8_t pin', 'int val'], 'prototype': 'void analogWrite(uint8_t pin, int val)'},
            'analogRead': {'kind': 'function', 'return_type': 'int', 'params': ['uint8_t pin'], 'prototype': 'int analogRead(uint8_t pin)'},
            'delay': {'kind': 'function', 'return_type': 'void', 'params': ['unsigned long ms'], 'prototype': 'void delay(unsigned long ms)'},
            'delayMicroseconds': {'kind': 'function', 'return_type': 'void', 'params': ['unsigned int us'], 'prototype': 'void delayMicroseconds(unsigned int us)'},
            'millis': {'kind': 'function', 'return_type': 'unsigned long', 'params': [], 'prototype': 'unsigned long millis()'},
            'micros': {'kind': 'function', 'return_type': 'unsigned long', 'params': [], 'prototype': 'unsigned long micros()'},
            'attachInterrupt': {'kind': 'function', 'return_type': 'void', 'params': ['uint8_t interrupt', 'void (*userFunc)(void)', 'int mode'], 'prototype': 'void attachInterrupt(uint8_t interrupt, void (*userFunc)(void), int mode)'},
            'detachInterrupt': {'kind': 'function', 'return_type': 'void', 'params': ['uint8_t interrupt'], 'prototype': 'void detachInterrupt(uint8_t interrupt)'},
            'map': {'kind': 'function', 'return_type': 'long', 'params': ['long x', 'long in_min', 'long in_max', 'long out_min', 'long out_max'], 'prototype': 'long map(long x, long in_min, long in_max, long out_min, long out_max)'},
            'constrain': {'kind': 'function', 'return_type': 'long', 'params': ['long x', 'long a', 'long b'], 'prototype': 'long constrain(long x, long a, long b)'}
        }

        def _find_symbol_definition_default(word: str):
            if not word or not word.isidentifier():
                return None
            kwords = {
                'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'default',
                'return', 'break', 'continue', 'struct', 'class', 'enum', 'union',
                'public', 'private', 'protected', 'void', 'int', 'float', 'double',
                'char', 'bool', 'const', 'static', 'unsigned', 'signed', 'true',
                'false', 'null', 'nullptr', 'include', 'define', 'ifdef', 'ifndef',
                'endif'
            }
            if word in kwords:
                return None

            escaped = re.escape(word)
            func_re = re.compile(r'(?:^|\s)([a-zA-Z0-9_:\*&]+(?:\s+[*&]*)?)\s+' + escaped + r'\s*\(([^)]*)\)')
            macro_re = re.compile(r'#\s*define\s+' + escaped + r'(?:\(([^)]*)\))?\s*(.*)')
            var_re = re.compile(r'(?:^|\s)([a-zA-Z0-9_:\*&]+(?:\s+[*&]*)?)\s+' + escaped + r'\s*(=|;|,|\[)')
            type_re = re.compile(r'(class|struct|enum|union)\s+' + escaped)

            fallback_sym = None

            current_tab = None
            try:
                cur = nb.select()
                if cur:
                    current_tab = parent_frame.nametowidget(cur)
            except Exception:
                pass

            ordered_frames = list(tab_data.keys())
            if current_tab in ordered_frames:
                ordered_frames.remove(current_tab)
                ordered_frames.insert(0, current_tab)

            # 1. Search open tabs first
            for frame in ordered_frames:
                d = tab_data[frame]
                text_widget = d["text"]
                content = text_widget.get("1.0", tk.END)
                lines = content.splitlines()
                for line_idx, line in enumerate(lines, start=1):
                    sline = line.strip()
                    if not sline or sline.startswith("//"):
                        continue

                    m_type = type_re.search(sline)
                    if m_type:
                        return {
                            "kind": m_type.group(1).lower(),
                            "name": word,
                            "return_type": "",
                            "params": [],
                            "prototype": m_type.group(0),
                            "frame": frame,
                            "path": d.get("path"),
                            "line_no": line_idx,
                            "col_no": line.find(word)
                        }

                    m_macro = macro_re.search(sline)
                    if m_macro:
                        p_str = m_macro.group(1)
                        p_list = [p.strip() for p in p_str.split(",")] if p_str else []
                        return {
                            "kind": "macro",
                            "name": word,
                            "return_type": "",
                            "params": p_list,
                            "prototype": f"#define {word}{'(' + p_str + ')' if p_str else ''} {m_macro.group(2) or ''}".strip(),
                            "frame": frame,
                            "path": d.get("path"),
                            "line_no": line_idx,
                            "col_no": line.find(word)
                        }

                    m_func = func_re.search(sline)
                    if m_func:
                        ret_t = m_func.group(1).strip()
                        p_str = m_func.group(2).strip()
                        p_list = [p.strip() for p in p_str.split(",")] if p_str else []
                        is_impl = "{" in sline or (line_idx < len(lines) and lines[line_idx].strip().startswith("{"))
                        sym_obj = {
                            "kind": "function",
                            "name": word,
                            "return_type": ret_t,
                            "params": p_list,
                            "prototype": f"{ret_t} {word}({p_str})",
                            "frame": frame,
                            "path": d.get("path"),
                            "line_no": line_idx,
                            "col_no": line.find(word)
                        }
                        if is_impl:
                            return sym_obj
                        elif not fallback_sym:
                            fallback_sym = sym_obj

                    m_var = var_re.search(sline)
                    if m_var and not fallback_sym:
                        ret_t = m_var.group(1).strip()
                        fallback_sym = {
                            "kind": "variable",
                            "name": word,
                            "return_type": ret_t,
                            "params": [],
                            "prototype": f"{ret_t} {word}",
                            "frame": frame,
                            "path": d.get("path"),
                            "line_no": line_idx,
                            "col_no": line.find(word)
                        }

            # 2. Search unopened .ino, .cpp, .c, .h, .hpp files in project directory
            if hasattr(self, "sketch_dir_path") and self.sketch_dir_path and self.sketch_dir_path.exists():
                open_paths = {d["path"] for d in tab_data.values() if d.get("path")}
                for ext in ("*.ino", "*.cpp", "*.c", "*.h", "*.hpp"):
                    for file_path in self.sketch_dir_path.glob(ext):
                        if file_path in open_paths:
                            continue
                        try:
                            file_content = file_path.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            continue

                        lines = file_content.splitlines()
                        for line_idx, line in enumerate(lines, start=1):
                            sline = line.strip()
                            if not sline or sline.startswith("//"):
                                continue

                            m_func = func_re.search(sline)
                            if m_func:
                                ret_t = m_func.group(1).strip()
                                p_str = m_func.group(2).strip()
                                p_list = [p.strip() for p in p_str.split(",")] if p_str else []
                                is_impl = "{" in sline or (line_idx < len(lines) and lines[line_idx].strip().startswith("{"))
                                sym_obj = {
                                    "kind": "function",
                                    "name": word,
                                    "return_type": ret_t,
                                    "params": p_list,
                                    "prototype": f"{ret_t} {word}({p_str})",
                                    "unopened_path": file_path,
                                    "line_no": line_idx,
                                    "col_no": line.find(word)
                                }
                                if is_impl:
                                    return sym_obj
                                elif not fallback_sym:
                                    fallback_sym = sym_obj

                            m_macro = macro_re.search(sline)
                            if m_macro and not fallback_sym:
                                p_str = m_macro.group(1)
                                p_list = [p.strip() for p in p_str.split(",")] if p_str else []
                                fallback_sym = {
                                    "kind": "macro",
                                    "name": word,
                                    "return_type": "",
                                    "params": p_list,
                                    "prototype": f"#define {word}{'(' + p_str + ')' if p_str else ''} {m_macro.group(2) or ''}".strip(),
                                    "unopened_path": file_path,
                                    "line_no": line_idx,
                                    "col_no": line.find(word)
                                }

            if fallback_sym:
                return fallback_sym

            if word in BUILTIN_ARDUINO_DEFS:
                res = dict(BUILTIN_ARDUINO_DEFS[word])
                res["name"] = word
                return res

            return None

        def _hide_default_hover():
            if _DEFAULT_HOVER_STATE["timer"]:
                try:
                    self.root.after_cancel(_DEFAULT_HOVER_STATE["timer"])
                except Exception:
                    pass
                _DEFAULT_HOVER_STATE["timer"] = None
            if _DEFAULT_HOVER_STATE["win"]:
                try:
                    _DEFAULT_HOVER_STATE["win"].destroy()
                except Exception:
                    pass
                _DEFAULT_HOVER_STATE["win"] = None
            _DEFAULT_HOVER_STATE["last_word"] = ""

        def _jump_to_symbol_def(sym_info):
            _hide_default_hover()
            if not sym_info or sym_info.get("not_compiled"):
                return

            frame = sym_info.get("frame")
            if not frame and sym_info.get("unopened_path"):
                u_path = sym_info["unopened_path"]
                _build_tab(u_path)
                for f_key, d_val in tab_data.items():
                    if d_val.get("path") == u_path:
                        frame = f_key
                        break

            if not frame:
                return

            line_no = sym_info.get("line_no", 1)
            col_no = max(0, sym_info.get("col_no", 0))

            nb.select(frame)
            t = tab_data[frame]["text"]
            pos_idx = f"{line_no}.{col_no}"
            t.mark_set(tk.INSERT, pos_idx)
            t.see(f"{line_no}.0")
            t.focus_set()

            t.tag_remove("jump_highlight", "1.0", tk.END)
            t.tag_add("jump_highlight", f"{line_no}.0", f"{line_no}.end")
            self.root.after(1500, lambda: t.tag_remove("jump_highlight", "1.0", tk.END))

        def _show_default_hover(x: int, y: int, x_root: int, y_root: int, t_widget: tk.Text):
            try:
                click_idx = t_widget.index(f"@{x},{y}")
                line_content = t_widget.get(f"{click_idx} linestart", f"{click_idx} lineend")
                col = int(click_idx.split(".")[1])
                if col >= len(line_content):
                    _hide_default_hover()
                    return

                if not (line_content[col].isalnum() or line_content[col] == '_'):
                    _hide_default_hover()
                    return

                start_col = col
                while start_col > 0 and (line_content[start_col - 1].isalnum() or line_content[start_col - 1] == '_'):
                    start_col -= 1
                end_col = col
                while end_col < len(line_content) and (line_content[end_col].isalnum() or line_content[end_col] == '_'):
                    end_col += 1

                word = line_content[start_col:end_col]
                if not word or word == _DEFAULT_HOVER_STATE["last_word"]:
                    return

                sym = _find_symbol_definition_default(word)
                if not sym:
                    _hide_default_hover()
                    return

                _hide_default_hover()
                _DEFAULT_HOVER_STATE["last_word"] = word

                if sym.get("not_compiled"):
                    win = tk.Toplevel(self.root)
                    win.wm_overrideredirect(True)
                    win.attributes("-topmost", True)
                    _DEFAULT_HOVER_STATE["win"] = win

                    frame = tk.Frame(win, bg="#151a23", bd=1, relief=tk.SOLID, highlightbackground="#3d2c18", highlightthickness=1, padx=10, pady=8)
                    frame.pack(fill=tk.BOTH, expand=True)

                    lbl = tk.Label(frame, text="⚙ Project Not Compiled", font=("Consolas", 10, "bold"), fg="#e6a23c", bg="#151a23")
                    lbl.pack(anchor=tk.W)
                    lbl_desc = tk.Label(frame, text="Compile the project (⚙ Compile) to enable definition navigation.", font=("Consolas", 8), fg="#abb2bf", bg="#151a23")
                    lbl_desc.pack(anchor=tk.W, pady=(2, 0))

                    pos_x = x_root + 15
                    pos_y = y_root + 15
                    win.geometry(f"+{pos_x}+{pos_y}")
                    return

                win = tk.Toplevel(self.root)
                win.wm_overrideredirect(True)
                win.attributes("-topmost", True)
                _DEFAULT_HOVER_STATE["win"] = win

                # Prevent window auto-hide when mouse moves into hover card
                win.bind("<Enter>", lambda e: self.root.after_cancel(_DEFAULT_HOVER_STATE["timer"]) if _DEFAULT_HOVER_STATE["timer"] else None)

                frame = tk.Frame(win, bg="#151a23", bd=1, relief=tk.SOLID, highlightbackground="#253244", highlightthickness=1, padx=10, pady=8, cursor="hand2")
                frame.pack(fill=tk.BOTH, expand=True)

                # Clicking anywhere on the hover card jumps to definition!
                frame.bind("<Button-1>", lambda e, s=sym: _jump_to_symbol_def(s))

                h_frame = tk.Frame(frame, bg="#151a23", cursor="hand2")
                h_frame.pack(anchor=tk.W, fill=tk.X)
                h_frame.bind("<Button-1>", lambda e, s=sym: _jump_to_symbol_def(s))

                lbl_kind = tk.Label(h_frame, text=sym["kind"], font=("Consolas", 10, "bold"), fg="#e8edf3", bg="#151a23", cursor="hand2")
                lbl_kind.pack(side=tk.LEFT)
                lbl_kind.bind("<Button-1>", lambda e, s=sym: _jump_to_symbol_def(s))

                lbl_name = tk.Label(h_frame, text=f" {sym['name']}", font=("Consolas", 10, "bold"), fg="#00d2ff", bg="#151a23", cursor="hand2")
                lbl_name.pack(side=tk.LEFT)
                lbl_name.bind("<Button-1>", lambda e, s=sym: _jump_to_symbol_def(s))

                if sym.get("return_type"):
                    lbl_ret = tk.Label(frame, text=f"  → {sym['return_type']}", font=("Consolas", 9), fg="#39c5bb", bg="#151a23", anchor=tk.W, cursor="hand2")
                    lbl_ret.pack(anchor=tk.W, pady=(2, 0))
                    lbl_ret.bind("<Button-1>", lambda e, s=sym: _jump_to_symbol_def(s))

                if sym.get("params"):
                    lbl_p_title = tk.Label(frame, text="Parameters:", font=("Consolas", 9, "bold"), fg="#7f8c8d", bg="#151a23", anchor=tk.W, cursor="hand2")
                    lbl_p_title.pack(anchor=tk.W, pady=(6, 2))
                    lbl_p_title.bind("<Button-1>", lambda e, s=sym: _jump_to_symbol_def(s))

                    for p in sym["params"]:
                        lbl_p = tk.Label(frame, text=f"  {p}", font=("Consolas", 9), fg="#abb2bf", bg="#151a23", anchor=tk.W, cursor="hand2")
                        lbl_p.pack(anchor=tk.W)
                        lbl_p.bind("<Button-1>", lambda e, s=sym: _jump_to_symbol_def(s))

                if sym.get("prototype"):
                    tk.Frame(frame, bg="#242d3d", height=1).pack(fill=tk.X, pady=6)
                    lbl_proto_title = tk.Label(frame, text="Function Prototypes", font=("Consolas", 9, "bold"), fg="#7f8c8d", bg="#151a23", anchor=tk.W, cursor="hand2")
                    lbl_proto_title.pack(anchor=tk.W, pady=(0, 2))
                    lbl_proto_title.bind("<Button-1>", lambda e, s=sym: _jump_to_symbol_def(s))

                    lbl_proto = tk.Label(frame, text=f"👉  {sym['prototype']}", font=("Consolas", 9, "bold"), fg="#39c5bb", bg="#151a23", cursor="hand2", anchor=tk.W)
                    lbl_proto.pack(anchor=tk.W)

                    def _on_hover_in(e):
                        lbl_proto.config(fg="#00ffff", font=("Consolas", 9, "bold", "underline"))
                    def _on_hover_out(e):
                        lbl_proto.config(fg="#39c5bb", font=("Consolas", 9, "bold"))

                    lbl_proto.bind("<Enter>", _on_hover_in)
                    lbl_proto.bind("<Leave>", _on_hover_out)
                    lbl_proto.bind("<Button-1>", lambda e, s=sym: _jump_to_symbol_def(s))

                pos_x = x_root + 15
                pos_y = y_root + 15
                win.geometry(f"+{pos_x}+{pos_y}")
            except Exception:
                pass

        def _clear_ctrl_link(t_widget: tk.Text):
            """Remove the Ctrl+hover underline from the text widget."""
            t_widget.tag_remove("ctrl_link", "1.0", tk.END)
            t_widget.config(cursor="xterm")

        def _on_ctrl_motion_default(event, t_widget: tk.Text):
            """Ctrl+hover: underline the word under cursor if it has a navigable definition."""
            try:
                click_idx = t_widget.index(f"@{event.x},{event.y}")
                line_content = t_widget.get(f"{click_idx} linestart", f"{click_idx} lineend")
                col = int(click_idx.split(".")[1])
                if col >= len(line_content) or not (line_content[col].isalnum() or line_content[col] == '_'):
                    _clear_ctrl_link(t_widget)
                    return

                start_col = col
                while start_col > 0 and (line_content[start_col - 1].isalnum() or line_content[start_col - 1] == '_'):
                    start_col -= 1
                end_col = col
                while end_col < len(line_content) and (line_content[end_col].isalnum() or line_content[end_col] == '_'):
                    end_col += 1

                word = line_content[start_col:end_col]
                row = click_idx.split(".")[0]
                word_start = f"{row}.{start_col}"
                word_end = f"{row}.{end_col}"

                # Check if this word is a keyword (not navigable)
                kwords = {
                    'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'default',
                    'return', 'break', 'continue', 'struct', 'class', 'enum', 'union',
                    'public', 'private', 'protected', 'void', 'int', 'float', 'double',
                    'char', 'bool', 'const', 'static', 'unsigned', 'signed', 'true',
                    'false', 'null', 'nullptr', 'include', 'define', 'ifdef', 'ifndef',
                    'endif'
                }
                if not word or not word.isidentifier() or word in kwords:
                    _clear_ctrl_link(t_widget)
                    return

                # Apply underline tag
                t_widget.tag_remove("ctrl_link", "1.0", tk.END)
                t_widget.tag_add("ctrl_link", word_start, word_end)
                t_widget.config(cursor="hand2")
            except Exception:
                _clear_ctrl_link(t_widget)

        def _on_key_release_ctrl(event, t_widget: tk.Text):
            """When Ctrl is released, clear the underline."""
            _clear_ctrl_link(t_widget)

        def _on_mouse_motion_default(event, t_widget: tk.Text):
            # If Ctrl is held, show underline link instead of hover popup
            if event.state & 0x4:  # Ctrl modifier bitmask
                _hide_default_hover()
                _on_ctrl_motion_default(event, t_widget)
                return
            else:
                _clear_ctrl_link(t_widget)

            x, y = event.x, event.y
            x_root, y_root = event.x_root, event.y_root
            if _DEFAULT_HOVER_STATE["timer"]:
                try:
                    self.root.after_cancel(_DEFAULT_HOVER_STATE["timer"])
                except Exception:
                    pass
            _DEFAULT_HOVER_STATE["timer"] = self.root.after(250, lambda: _show_default_hover(x, y, x_root, y_root, t_widget))

        def _on_ctrl_click_default(event, t_widget: tk.Text):
            try:
                click_idx = t_widget.index(f"@{event.x},{event.y}")
                line_content = t_widget.get(f"{click_idx} linestart", f"{click_idx} lineend")
                col = int(click_idx.split(".")[1])
                if col < len(line_content) and (line_content[col].isalnum() or line_content[col] == '_'):
                    start_col = col
                    while start_col > 0 and (line_content[start_col - 1].isalnum() or line_content[start_col - 1] == '_'):
                        start_col -= 1
                    end_col = col
                    while end_col < len(line_content) and (line_content[end_col].isalnum() or line_content[end_col] == '_'):
                        end_col += 1
                    word = line_content[start_col:end_col]
                    sym = _find_symbol_definition_default(word)
                    if sym:
                        _clear_ctrl_link(t_widget)
                        _jump_to_symbol_def(sym)
                        return "break"
            except Exception:
                pass

        def _build_tab(file_path: Path, init_content=None, orig_content=None, is_modified=False, cursor_pos=None, scroll_pos=None, defer_highlight=False):
            if init_content is not None:
                content = init_content
            else:
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
            txt.bind("<Button-1>", lambda e: safe_reclaim_os_focus(txt), add="+")

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
            vsb.update_idletasks()
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
            lineno_text.bind("<Button-4>", _on_lineno_scroll)   # Scroll up fallback
            lineno_text.bind("<Button-5>", _on_lineno_scroll)   # Scroll down fallback

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
            txt.tag_configure("syntax_error", background="#4a1515", underline=True)
            txt.tag_configure("syntax_warning", background="#4a3b15", underline=True)
            txt.tag_configure("jump_highlight", background="#1c3b57")
            txt.tag_configure("ctrl_link", foreground="#4fc1ff", underline=True)
            txt.tag_raise("syntax_error", "active_line")
            txt.tag_raise("syntax_warning", "active_line")
            txt.tag_raise("search_match", "active_line")
            txt.tag_raise("search_match_active", "search_match")
            txt.tag_raise("jump_highlight", "active_line")
            txt.tag_raise("ctrl_link", "active_line")
            txt.tag_raise("sel", "active_line")

            # Load content
            txt.insert("1.0", content)
            txt.edit_reset()
            original_snapshot = orig_content if orig_content is not None else txt.get("1.0", tk.END)

            tab_data[tab_frame] = {
                "path":       file_path,
                "text":       txt,
                "lineno_text": lineno_text,
                "modified":   is_modified,
                "original":   original_snapshot,
            }

            if is_modified:
                nb.tab(tab_frame, text="* " + file_path.name)

            if cursor_pos:
                try:
                    txt.mark_set(tk.INSERT, cursor_pos)
                except Exception:
                    pass

            if scroll_pos:
                try:
                    txt.yview_moveto(scroll_pos[0])
                except Exception:
                    pass

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
                    _schedule_autosave(f),
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
            txt.bind("<ISO_Left_Tab>",  lambda e, t=txt: _on_shift_tab(e, t))  # Shift-Tab binding fallback
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

            # Mouse hover popover & Ctrl+Click definition jump bindings
            txt.bind("<Motion>",            lambda e, t=txt: _on_mouse_motion_default(e, t))
            txt.bind("<Leave>",             lambda e, t=txt: (_hide_default_hover(), _clear_ctrl_link(t)))
            txt.bind("<Control-Button-1>",  lambda e, t=txt: _on_ctrl_click_default(e, t))
            txt.bind("<F12>",               lambda e, t=txt: _on_ctrl_click_default(e, t))
            txt.bind("<KeyRelease-Control_L>", lambda e, t=txt: _on_key_release_ctrl(e, t))
            txt.bind("<KeyRelease-Control_R>", lambda e, t=txt: _on_key_release_ctrl(e, t))

            # General after-key refresh (highlight, line-nos, dirty flag, cursor)
            txt.bind("<KeyRelease>",        _on_key)
            txt.bind("<ButtonRelease-1>",   _on_click)
            txt.bind("<Control-s>",         lambda e, f=tab_frame: (_save_tab(f), "break")[1])
            txt.bind("<Control-S>",         lambda e, f=tab_frame: (_save_tab(f), "break")[1])
            txt.bind("<Control-slash>",     _toggle_comment)

        def _try_embed_editor_window(self):
            if win32gui is None or win32con is None:
                self._append("  ⚠ pywin32 not available — editor will open in its own window.", "warning")
                self._show_editor_fallback_button()
                return

            if getattr(self, "_editor_embedded", False) or getattr(self, "_embedding_in_progress", False):
                return

            # Ensure main window is viewable (mapped) before embedding
            if not self.root.winfo_exists() or not self.root.winfo_viewable():
                self.root.after(150, self._try_embed_editor_window)
                return

            self._embedding_in_progress = True
            hwnd = self._find_editor_hwnd()
            if not hwnd:
                self._embedding_in_progress = False
                self._editor_reparent_attempts += 1
                if self._editor_reparent_attempts < 120:
                    poll_delay = 50 if self._editor_reparent_attempts < 20 else 100
                    self.root.after(poll_delay, self._try_embed_editor_window)
                else:
                    self._append("  ✖ Could not locate the editor's window to embed it after 8s — "
                                  "falling back to a separate window.", "error")
                    self._show_editor_fallback_button()
                return

            # ⚡ Background Execution Optimization:
            # Reparenting a WebView2 HWND while its V8 engine is mid-initialization causes Win32
            # cross-thread message queue locks, freezing the main GUI window.
            # Wait until pywebview signals that the editor page content has finished loading in the background
            # before attaching native handles.
            if not getattr(self, "_editor_content_loaded", False):
                self._embedding_in_progress = False
                self._editor_reparent_attempts += 1
                if self._editor_reparent_attempts < 120:
                    self.root.after(80, self._try_embed_editor_window)
                else:
                    self._show_editor_fallback_button()
                return

            # Fast, non-blocking check: verify host window is not hung before reparenting
            import ctypes
            try:
                user32 = ctypes.windll.user32
                if user32.IsHungAppWindow(hwnd):
                    self._embedding_in_progress = False
                    self._editor_reparent_attempts += 1
                    if self._editor_reparent_attempts < 80:
                        self.root.after(100, self._try_embed_editor_window)
                    else:
                        self._append("  ✖ Editor window stopped responding during startup — falling back to separate window.", "error")
                        self._show_editor_fallback_button()
                    return
            except Exception:
                pass

            try:
                frame = self._editor_embed_frame
                tk_hwnd = frame.winfo_id()

                # Set WS_CLIPCHILDREN on parent Tk frame to prevent paint overlap
                try:
                    tk_style = win32gui.GetWindowLong(tk_hwnd, win32con.GWL_STYLE)
                    win32gui.SetWindowLong(tk_hwnd, win32con.GWL_STYLE, tk_style | win32con.WS_CLIPCHILDREN)
                except Exception:
                    pass

                # Strip title bar / borders / system menu so the window
                # behaves like a plain child control rather than a floating top-level window.
                if not hasattr(self, "_original_editor_style") or getattr(self, "_original_editor_style", None) is None:
                    self._original_editor_style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                    self._original_editor_ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)

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

                # Re-parent it under the Tkinter frame.
                win32gui.SetParent(hwnd, tk_hwnd)

                w = max(frame.winfo_width(), 50)
                h = max(frame.winfo_height(), 50)
                # Use SWP_ASYNCWINDOWPOS (0x4000) so SetWindowPos executes without blocking calling thread
                win32gui.SetWindowPos(
                    hwnd, 0, 0, 0, w, h,
                    win32con.SWP_FRAMECHANGED | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW | 0x4000
                )

                try:
                    if hasattr(self, "editor_window"):
                        self.editor_window.show()
                except Exception as e:
                    self._append(f"  ⚠ editor_window.show() failed: {e}", "warning")

                self._editor_embedded = True
                self._embedding_in_progress = False
                self._stop_editor_spinner()
                self._editor_embed_frame.config(bg=Theme.BG_DARKEST)
            except Exception as e:
                self._embedding_in_progress = False
                self._append(f"  ✖ Embedding failed: {e}", "error")
                self._show_editor_fallback_button()

        def _reload_files():
            for tab in nb.tabs():
                nb.forget(tab)
            tab_data.clear()

            # Track tabs that need deferred highlighting
            _deferred_highlight_tabs = []

            backup = getattr(self, "_default_editor_state_backup", None)
            if backup:
                self._default_editor_state_backup = None
                for idx, item in enumerate(backup):
                    _build_tab(
                        item["path"],
                        init_content=item["content"],
                        orig_content=item["original"],
                        is_modified=item["modified"],
                        cursor_pos=item["cursor"],
                        scroll_pos=item["scroll"],
                        defer_highlight=(idx > 0),
                    )
                    if idx > 0:
                        # Collect non-first tabs for deferred highlighting
                        tab_id = nb.tabs()[-1]
                        _deferred_highlight_tabs.append(tab_id)
            else:
                sketch_dir = self.sketch_dir_path
                files = get_project_root_source_files(
                    sketch_dir, (".ino", ".cpp", ".c", ".h", ".txt")
                )

                order_file = get_project_build_cache_root(sketch_dir, create=False) / ".mcu_flash_tab_order.json"
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
                            file_map.setdefault(os.path.normcase(rel), f)
                        ordered_files = []
                        ordered_keys = set()
                        for name in saved_order:
                            key = os.path.normcase(str(name))
                            if key in file_map and key not in ordered_keys:
                                ordered_files.append(file_map[key])
                                ordered_keys.add(key)
                                file_map.pop(key, None)
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
                    self._default_editor_ready = True
                    self._mark_startup_ready("Code editor ready")
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
                    _update_cursor_label(data["text"])
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
            self._default_editor_ready = True
            self._mark_startup_ready("Code editor ready")

        self._load_editor_files = _reload_files
        self._save_all_editor_files = _save_all
        self._save_current_editor_file = _save_current
        self._reload_current_editor_file = _reload_files

        # Creating the editor widgets is part of the essential UI shell, but
        # globbing a project, opening every source file, and highlighting it is
        # not. Defer that disk-heavy pass until the first window paint has had
        # time to settle. This also prevents the startup project handler from
        # immediately doing the same load a second time.
        self._editor_files_load_pending = True
        old_load_job = getattr(self, "_default_editor_initial_load_job", None)
        if old_load_job:
            try:
                self.root.after_cancel(old_load_job)
            except Exception:
                pass

        def _load_default_files_after_first_paint():
            self._default_editor_initial_load_job = None
            if getattr(self, "editor_notebook", None) is not nb:
                return
            self._editor_files_load_pending = False
            try:
                _reload_files()
            except Exception as exc:
                # Keep the main window usable if a remote/inaccessible project
                # fails while its tabs are being materialized. The editor shell
                # is still available for choosing another project.
                self._append(f"  ⚠ Editor project load warning: {exc}", "warning")
                self._default_editor_ready = True
                self._mark_startup_ready("Editor opened with a warning")

        self._default_editor_initial_load_job = self.root.after(
            550, _load_default_files_after_first_paint
        )

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
            seen_paths = set()
            for tab_id in nb.tabs():
                widget = nb.nametowidget(tab_id)
                data = tab_data.get(widget)
                if data and "path" in data:
                    try:
                        rel = str(data["path"].relative_to(self.sketch_dir_path))
                    except Exception:
                        rel = str(data["path"])
                    key = os.path.normcase(rel)
                    if key not in seen_paths:
                        seen_paths.add(key)
                        paths.append(rel)
            order_file = get_project_build_cache_root(self.sketch_dir_path) / ".mcu_flash_tab_order.json"
            try:
                import json
                ensure_file_writable(order_file)
                order_file.write_text(json.dumps(paths, indent=2), encoding="utf-8")
                hide_hidden_attribute(order_file)
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

        # Window-level keyboard bindings. These must be bound to the actual
        # toplevel that hosts this editor instance (the main window, or the
        # separate Toplevel when detached) rather than unconditionally to
        # self.root — Tk does not propagate key events between sibling
        # toplevels, so binding to self.root left Ctrl+F / Ctrl+/ / zoom
        # shortcuts dead whenever the Default editor was detached into its
        # own window.
        bind_target = parent_frame.winfo_toplevel()
        bind_target.bind("<Control-f>", _toggle_find_panel)
        bind_target.bind("<Control-F>", _toggle_find_panel)
        bind_target.bind("<Control-slash>", _toggle_comment)
        bind_target.bind("<Control-Key-equal>", _zoom_in)
        bind_target.bind("<Control-Key-plus>", _zoom_in)
        bind_target.bind("<Control-Key-minus>", _zoom_out)

        # Deferred background services are started by _mark_startup_ready.


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
                if any(k in title for k in ("Embedded Code Editor", "Monaco Code Editor", "Monaco Editor")):
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

        # 2. Fallback to exact match or title variants
        for variant in (EDITOR_WINDOW_TITLE, "Monaco Code Editor", "Embedded Code Editor"):
            hwnd = win32gui.FindWindow(None, variant)
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
                    t = win32gui.GetWindowText(h)
                    if any(k in t for k in ("MCU Flasher", "Embedded Code Editor", "Monaco")):
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
            self._mark_startup_ready("Monaco Editor ready")
            # Editor is ready! Trigger deferred background init immediately
            self.root.after(10, self._deferred_background_init)

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

        if getattr(self, "_editor_embedded", False) or getattr(self, "_embedding_in_progress", False):
            return

        # Ensure main window is viewable (mapped) before embedding
        if not self.root.winfo_exists() or not self.root.winfo_viewable():
            self.root.after(150, self._try_embed_editor_window)
            return

        self._embedding_in_progress = True
        hwnd = self._find_editor_hwnd()
        if not hwnd:
            self._embedding_in_progress = False
            self._editor_reparent_attempts += 1
            if self._editor_reparent_attempts < 120:
                poll_delay = 25 if self._editor_reparent_attempts < 20 else 50
                self.root.after(poll_delay, self._try_embed_editor_window)
            else:
                self._append("  ✖ Could not locate the editor's window to embed it after 8s — "
                              "falling back to a separate window.", "error")
                self._show_editor_fallback_button()
            return

        # Ensure the webview host thread is actively pumping messages before attempting
        # reparenting or thread input linking. Premature AttachThreadInput while WebView2 /
        # pywebview is mid-initialization causes Windows to lock message queues, making
        # the app temporarily "Not Responding".
        import ctypes
        from ctypes import wintypes
        try:
            user32 = ctypes.windll.user32
            if user32.IsHungAppWindow(hwnd):
                self._embedding_in_progress = False
                self._editor_reparent_attempts += 1
                if self._editor_reparent_attempts < 80:
                    self.root.after(100, self._try_embed_editor_window)
                else:
                    self._append("  ✖ Editor window stopped responding during startup — falling back to separate window.", "error")
                    self._show_editor_fallback_button()
                return

            res = wintypes.DWORD()
            # SMTO_ABORTIFHUNG = 0x0002 — check if thread responds to message within 50ms
            if not user32.SendMessageTimeoutW(hwnd, 0, 0, 0, 0x0002, 50, ctypes.byref(res)):
                self._embedding_in_progress = False
                self._editor_reparent_attempts += 1
                if self._editor_reparent_attempts < 80:
                    self.root.after(100, self._try_embed_editor_window)
                else:
                    self._append("  ✖ Editor window host thread timed out during embed retry.", "error")
                    self._show_editor_fallback_button()
                return
        except Exception:
            pass

        try:
            frame = self._editor_embed_frame
            frame.update_idletasks()
            tk_hwnd = frame.winfo_id()

            # Set WS_CLIPCHILDREN on parent Tk frame to prevent paint overlap
            try:
                tk_style = win32gui.GetWindowLong(tk_hwnd, win32con.GWL_STYLE)
                win32gui.SetWindowLong(tk_hwnd, win32con.GWL_STYLE, tk_style | win32con.WS_CLIPCHILDREN)
            except Exception:
                pass

            # Strip title bar / borders / system menu so the window
            # behaves like a plain child control rather than a floating top-level window.
            if not hasattr(self, "_original_editor_style") or getattr(self, "_original_editor_style", None) is None:
                self._original_editor_style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                self._original_editor_ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)

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

            # Re-parent it under the Tkinter frame.
            win32gui.SetParent(hwnd, tk_hwnd)

            w = max(frame.winfo_width(), 50)
            h = max(frame.winfo_height(), 50)
            win32gui.SetWindowPos(
                hwnd, 0, 0, 0, w, h,
                win32con.SWP_FRAMECHANGED | win32con.SWP_NOZORDER | win32con.SWP_SHOWWINDOW | 0x4000
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
            self._embedding_in_progress = False
            self._append("  ✓ Code editor embedded into the main window.", "success")
            self._editor_status_lbl.configure(text="📝 Rendering editor…")
            self._editor_desc_lbl.configure(text="Waiting for the editor page to finish loading…")
            self._reveal_editor_if_ready()
        except Exception as e:
            self._embedding_in_progress = False
            self._editor_reparent_attempts += 1
            if self._editor_reparent_attempts < 80:  # ~8 seconds of retrying
                self.root.after(100, self._try_embed_editor_window)
            else:
                self._append(f"  ✖ Failed to embed editor window after retries: {e}", "error")
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
                # Tk's Tcl notifier should start servicing its message queue
                # again immediately after detaching. Queue the notice through
                # the GUI dispatch bridge rather than touching Tk directly
                # from this background thread.
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
                    self._post_ui(_notify)
                except Exception:
                    pass

        threading.Thread(target=_watch, daemon=True).start()

    def _cleanup_active_editor(self):
        """Clean up the active editor by hiding Monaco pywebview windows,
        detaching Windows handles/hooks, canceling pending after jobs,
        and destroying all Tk widgets inside the editor frame."""

        # 0. Close/dispose detached editor window if open
        try:
            self._close_detached_editor_window()
        except Exception:
            pass

        # 1. Detach and hide Monaco pywebview window
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
        for job_attr in (
            "_editor_spinner_job",
            "_editor_resize_job",
            "_default_editor_initial_load_job",
        ):
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
        self._editor_fallback_ready = True
        self._editor_status_lbl.configure(text="📝 Monaco Editor Active")
        self._editor_desc_lbl.configure(text="The editor is running in a separate window.")
        self._editor_fallback_btn.pack(pady=15)
        self._mark_startup_ready("Monaco Editor opened separately")

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
            # Native webview resizing is much more expensive than a Tk
            # geometry update. Wait for a short quiet period so a drag produces
            # one native resize instead of dozens of synchronous reparenting
            # requests on slower PCs.
            self._editor_resize_job = self.root.after(90, _do_resize)
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

