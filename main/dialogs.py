#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import re
import threading
import queue
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont


from main.core.constants import *
from main.core.theme import *
from main.core.config import *
from main.core.file_utils import *
from main.core.toolchain import *
from main.core.board_catalog import *
from main.core.board_compat import *
from main.widgets import *

def _make_dialog_btn(parent, text, command, bg, bg_hover, font, width=None) -> tk.Button:
    """Standalone flat-button factory (selector window runs before
    MCUUploadGUI exists, so it can't reuse the instance-bound helpers)."""
    btn = tk.Button(
        parent, text=text, command=command,
        font=font, fg=Theme.TEXT_BRIGHT, bg=bg,
        activebackground=bg_hover, activeforeground=Theme.TEXT_BRIGHT,
        relief=tk.FLAT, borderwidth=0, padx=14, pady=6, cursor="hand2",
        highlightthickness=1, highlightbackground=Theme.BORDER, highlightcolor=Theme.BORDER_LIT,
    )
    if width:
        btn.configure(width=width)
    btn.bind("<Enter>", lambda e, b=btn, c=bg_hover: b.configure(bg=c))
    btn.bind("<Leave>", lambda e, b=btn, c=bg: b.configure(bg=c))
    return btn


def _make_toolbar_btn(parent, text, command, bg, bg_hover, font) -> tk.Button:
    """Small toolbar button that packs itself (side=LEFT) into *parent*.
    Used for notification/tab toolbars that sit outside MCUUploadGUI's
    instance-method helpers."""
    btn = tk.Button(
        parent, text=text, command=command,
        font=font, fg=Theme.TEXT_BRIGHT, bg=bg,
        activebackground=bg_hover, activeforeground=Theme.TEXT_BRIGHT,
        relief=tk.FLAT, borderwidth=0, padx=8, pady=3, cursor="hand2",
        highlightthickness=1, highlightbackground=Theme.BORDER, highlightcolor=Theme.BORDER_LIT,
    )
    btn.bind("<Enter>", lambda e, b=btn, c=bg_hover: b.configure(bg=c))
    btn.bind("<Leave>", lambda e, b=btn, c=bg: b.configure(bg=c))
    btn.pack(side=tk.LEFT, padx=(4, 0))
    return btn


def _scaffold_new_project(dest_parent: Path, project_name: str,
                           include_h: bool, include_cpp: bool) -> Path:
    """Create <dest_parent>/<project_name>/ with the main .ino and the
    optional .h / .cpp pair, fully wired with #pragma once / #include.
    Returns the new project folder path. Raises on failure (caller catches)."""
    project_dir = dest_parent / project_name
    project_dir.mkdir(parents=True, exist_ok=False)
    ensure_hidden_read_first_md(project_dir)

    ino_includes = ""
    if include_h:
        ino_includes += f'#include "{project_name}.h"\n'

    ino_content = (
        f"{ino_includes}\n"
        f"void setup() {{\n"
        f"  \n"
        f"}}\n\n"
        f"void loop() {{\n"
        f"  \n"
        f"}}\n"
    )
    (project_dir / f"{project_name}.ino").write_text(ino_content, encoding="utf-8")

    if include_h:
        h_content = "#pragma once\n\n"
        (project_dir / f"{project_name}.h").write_text(h_content, encoding="utf-8")

    if include_cpp:
        cpp_includes = f'#include "{project_name}.h"\n\n' if include_h else ""
        (project_dir / f"{project_name}.cpp").write_text(cpp_includes, encoding="utf-8")

    return project_dir


class ProjectSelectorDialog:
    """Modal startup window: pick an existing project folder, or build a
    new one from scratch. Call `.run()`; returns a Path or None (cancelled)."""

    def __init__(self, root: tk.Tk, initial_dir: str = ""):
        self.root = root
        self.result: Path | None = None
        self.initial_dir = initial_dir
        self.frame_existing: tk.Frame | None = None
        self.frame_new: tk.Frame | None = None

        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.configure(bg=Theme.BG_DARKEST)
        self.win.title("MCU Flasher by Naph — Select Project")
        # Enable resizability so user and system can adjust dialog dimensions dynamically
        self.win.resizable(True, True)

        # Query the active work area and use logical dimensions so the same
        # layout is selected at 100%, 150%, and 200% display scaling.
        work_left, work_top, work_right, work_bottom = _get_monitor_work_area(self.win)
        screen_w = work_right - work_left
        screen_h = work_bottom - work_top
        display_scale = _get_widget_dpi_scale(self.win)
        logical_w = screen_w / display_scale
        logical_h = screen_h / display_scale

        is_small_screen = (logical_w < 1400 or logical_h < 800)

        if is_small_screen:
            width = min(660, max(520, int(logical_w * 0.52)))
            height = min(560, max(420, int(logical_h * 0.75)))
            self.win.minsize(round(480 * display_scale), round(400 * display_scale))
            title_size, sub_size, label_size = 13, 8, 9
            top_pad_y, bot_pad_y = (10, 0), (2, 8)
        else:
            width = min(700, max(600, int(logical_w * 0.45)))
            height = min(660, max(520, int(logical_h * 0.65)))
            self.win.minsize(round(520 * display_scale), round(460 * display_scale))
            title_size, sub_size, label_size = 15, 9, 10
            top_pad_y, bot_pad_y = (18, 0), (2, 14)

        width = min(round(width * display_scale), screen_w - round(32 * display_scale))
        height = min(round(height * display_scale), screen_h - round(48 * display_scale))
        x = work_left + max(0, (screen_w - width) // 2)
        y = work_top + max(0, (screen_h - height) // 2)
        x_part = f"+{x}" if x >= 0 else str(x)
        y_part = f"+{y}" if y >= 0 else str(y)
        self.win.geometry(f"{width}x{height}{x_part}{y_part}")
        self.win.configure(bg=Theme.BG_DARKEST)
        self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)

        f_title = tkfont.Font(family="Segoe UI", size=title_size, weight="bold")
        f_sub = tkfont.Font(family="Segoe UI", size=sub_size)
        f_label = tkfont.Font(family="Segoe UI", size=label_size)
        f_btn = tkfont.Font(family="Segoe UI", size=label_size, weight="bold")
        f_mono = tkfont.Font(family="Consolas", size=max(8, label_size - 1))
        self._fonts = (f_title, f_sub, f_label, f_btn, f_mono)

        tk.Label(self.win, text="⚡ MCU Flasher by Naph", font=f_title,
                 fg=Theme.CYAN, bg=Theme.BG_DARKEST).pack(pady=top_pad_y)
        tk.Label(self.win, text="Open an existing project, or create a new one",
                 font=f_sub, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(pady=bot_pad_y)

        # ── Mode tabs (Existing / New) ───────────────────────────────
        tab_bar = tk.Frame(self.win, bg=Theme.BG_DARKEST)
        tab_bar.pack(fill=tk.X, padx=24)

        self.btn_tab_existing = _make_dialog_btn(
            tab_bar, "📂 Existing Project", lambda: self._switch_tab("existing"),
            Theme.BTN_FULL, Theme.BTN_FULL_H, f_btn)
        self.btn_tab_existing.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))

        self.btn_tab_new = _make_dialog_btn(
            tab_bar, "✨ New Project", lambda: self._switch_tab("new"),
            Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, f_btn)
        self.btn_tab_new.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(4, 0))

        tk.Frame(self.win, bg=Theme.BORDER, height=2).pack(fill=tk.X, padx=24, pady=(12, 0))

        # ── Body container (swapped per tab) ─────────────────────────
        self.body = tk.Frame(self.win, bg=Theme.BG_DARKEST)
        self.body.pack(fill=tk.BOTH, expand=True, padx=24, pady=16)

        try:
            self._build_existing_tab()
        except Exception as e:
            print(f"[WARN] Error building existing project tab: {e}")

        try:
            self._build_new_tab()
        except Exception as e:
            print(f"[WARN] Error building new project tab: {e}")

        try:
            self._switch_tab("existing")
        except Exception:
            pass

        self.win.bind("<Escape>", lambda e: self._on_cancel())

        # Safely present window, bring to front, focus, and apply modal grab AFTER deiconify
        try:
            self.win.update_idletasks()
            x = max(0, (screen_w - width) // 2)
            y = max(0, (screen_h - height) // 2)
            self.win.geometry(f"{width}x{height}+{x}+{y}")
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
            self.win.attributes("-topmost", True)
            self.win.after(500, self._unset_selector_topmost)
            self.win.grab_set()
        except Exception:
            try:
                self.win.deiconify()
                self.win.lift()
                self.win.focus_force()
                self.win.grab_set()
            except Exception:
                pass

    def _unset_selector_topmost(self):
        try:
            if hasattr(self, "win") and self.win:
                self.win.attributes("-topmost", False)
        except Exception:
            pass

    # ── Tab switching ────────────────────────────────────────────────────
    def _switch_tab(self, which: str):
        self.mode = which
        if which == "existing":
            if hasattr(self, "btn_tab_existing") and self.btn_tab_existing:
                self.btn_tab_existing.configure(bg=Theme.BTN_FULL)
            if hasattr(self, "btn_tab_new") and self.btn_tab_new:
                self.btn_tab_new.configure(bg=Theme.BTN_CLEAR)
            if getattr(self, "frame_new", None):
                self.frame_new.pack_forget()
            if getattr(self, "frame_existing", None):
                self.frame_existing.pack(fill=tk.BOTH, expand=True)
        else:
            if hasattr(self, "btn_tab_new") and self.btn_tab_new:
                self.btn_tab_new.configure(bg=Theme.BTN_FULL)
            if hasattr(self, "btn_tab_existing") and self.btn_tab_existing:
                self.btn_tab_existing.configure(bg=Theme.BTN_CLEAR)
            if getattr(self, "frame_existing", None):
                self.frame_existing.pack_forget()
            if getattr(self, "frame_new", None):
                self.frame_new.pack(fill=tk.BOTH, expand=True)

    def _get_project_files(self, folder_path: Path | str) -> list[str]:
        """Scan only the selected project folder for user-owned source files.

        ``src`` is a reserved build snapshot managed by MCU Flasher. It must
        never make a folder look like a project or appear in the selector's
        preview, because those copies are not the files the user edits.
        """
        valid_exts = {".cpp", ".ino", ".h", ".hpp", ".txt"}
        found_files: list[tuple[int, str]] = []
        try:
            p = Path(folder_path)
            if not p.exists() or not p.is_dir():
                return []

            for item in p.iterdir():
                if item.is_file():
                    ext = item.suffix.lower()
                    if ext in valid_exts:
                        prio = 0 if ext == ".ino" else (1 if ext in (".h", ".hpp") else (2 if ext == ".cpp" else 3))
                        found_files.append((prio, item.name))
        except Exception:
            pass
        
        found_files.sort(key=lambda x: (x[0], x[1].lower()))
        return [name for _, name in found_files]

    # ── Existing-project tab ────────────────────────────────────────────
    def _build_existing_tab(self):
        f_title, f_sub, f_label, f_btn, f_mono = self._fonts
        self.frame_existing = tk.Frame(self.body, bg=Theme.BG_DARKEST)

        # Action Buttons row packed at the bottom FIRST so it is always visible and never cut off
        btn_row = tk.Frame(self.frame_existing, bg=Theme.BG_DARKEST)
        btn_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        _make_dialog_btn(btn_row, "Cancel", self._on_cancel,
                          Theme.BTN_STOP, Theme.BTN_STOP_H, f_btn).pack(side=tk.RIGHT, padx=(8, 0))
        _make_dialog_btn(btn_row, "Open Project ▶", self._on_open_existing,
                          Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, f_btn).pack(side=tk.RIGHT)

        tk.Label(self.frame_existing,
                 text="Pick a folder that already contains your sketch\n"
                      "(.ino / .cpp / .h / .txt files and, optionally, platformio.ini).",
                 font=f_label, fg=Theme.TEXT, bg=Theme.BG_DARKEST,
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(8, 12))

        self.existing_path_var = tk.StringVar(value=self.initial_dir)
        path_row = tk.Frame(self.frame_existing, bg=Theme.BG_DARKEST)
        path_row.pack(fill=tk.X, pady=(0, 8))

        entry = tk.Entry(path_row, textvariable=self.existing_path_var,
                          font=f_mono, bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
                          insertbackground=Theme.CYAN, borderwidth=0,
                          highlightthickness=1, highlightcolor=Theme.CYAN_DIM,
                          highlightbackground=Theme.BORDER)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), ipady=4)

        browse_btn = _make_dialog_btn(path_row, "Browse…", self._browse_existing,
                                       Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, f_label)
        browse_btn.pack(side=tk.LEFT)

        # Live Preview of files inside selected project folder
        preview_frame = tk.Frame(self.frame_existing, bg=Theme.BG_DARK, padx=10, pady=6,
                                 highlightthickness=1, highlightbackground=Theme.BORDER)
        preview_frame.pack(fill=tk.X, pady=(0, 8))

        tk.Label(preview_frame, text="Folder Contents (.cpp / .ino / .h / .txt):",
                 font=f_sub, fg=Theme.CYAN, bg=Theme.BG_DARK, anchor=tk.W).pack(anchor=tk.W)

        self.existing_contents_lbl = tk.Label(
            preview_frame, text="Select a folder to view files...", font=f_mono,
            fg=Theme.TEXT_BRIGHT, bg=Theme.BG_DARK, anchor=tk.W, justify=tk.LEFT, wraplength=480
        )
        self.existing_contents_lbl.pack(anchor=tk.W, pady=(2, 0))

        preview_frame.bind(
            "<Configure>",
            lambda e: self.existing_contents_lbl.configure(wraplength=max(180, e.width - 24))
        )

        def _update_existing_contents(*args):
            raw = self.existing_path_var.get().strip()
            if not raw:
                self.existing_contents_lbl.config(text="No folder selected", fg=Theme.TEXT_DIM)
                return
            p = Path(raw)
            if not p.exists() or not p.is_dir():
                self.existing_contents_lbl.config(text="⚠️ Folder does not exist", fg=Theme.RED)
                return
            files = self._get_project_files(p)
            if files:
                file_str = "  •  ".join(files[:10])
                if len(files) > 10:
                    file_str += f"   (+{len(files) - 10} more)"
                self.existing_contents_lbl.config(text=f"📄 {file_str}", fg=Theme.GREEN)
            else:
                self.existing_contents_lbl.config(text="⚠️ No .cpp, .ino, .h, or .txt files found in this folder", fg=Theme.YELLOW)

        self.existing_path_var.trace_add("write", _update_existing_contents)
        _update_existing_contents()

        # Recent Projects list — show a placeholder and scan on a background
        # thread so the dialog window is responsive immediately.
        self._recent_placeholder = tk.Label(
            self.frame_existing,
            text="Loading recent projects…",
            font=f_sub, fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST,
            anchor=tk.W,
        )
        self._recent_placeholder.pack(anchor=tk.W, pady=(8, 4))

        # Tk widgets belong to the thread that created the dialog.  The worker
        # only computes recent-project data; the Tk-owned poller below performs
        # the final render so startup handoff cannot emit a cross-thread Tcl
        # exception while the selector is opening.
        self._recent_projects_queue = queue.Queue(maxsize=1)
        import threading
        threading.Thread(target=self._populate_recent_projects, daemon=True).start()
        self.win.after(40, self._poll_recent_projects_result)

    def _populate_recent_projects(self):
        """Background-thread worker: scan recent projects and post the
        results back to the main thread for rendering."""
        recent_list = load_recent_projects()
        initial_dir = self.initial_dir
        if initial_dir:
            try:
                curr_resolved = str(Path(initial_dir).resolve())
                recent_list = [p for p in recent_list if str(Path(p).resolve()) != curr_resolved]
            except Exception:
                recent_list = [p for p in recent_list if p != initial_dir]

        f_title, f_sub, f_label, f_btn, f_mono = self._fonts

        # Pre-compute all the data we need off the main thread
        entries = []
        for path in recent_list:
            try:
                p_path = Path(path)
                owner_pid = folder_lock_owner(p_path) if p_path.exists() else None
                is_locked = owner_pid is not None
                files = self._get_project_files(p_path) if p_path.exists() else []
                if files:
                    files_str = "📄 " + "  •  ".join(files[:6]) + (f" (+{len(files)-6} more)" if len(files) > 6 else "")
                    files_color = Theme.CYAN_DIM
                else:
                    files_str = "📄 (No .cpp / .ino / .h / .txt files found)"
                    files_color = Theme.TEXT_DIM

                if is_locked:
                    btn_text = f" 🔒 {p_path.name}  —  {path}   (in use — PID {owner_pid})"
                    fg_color = Theme.RED
                    bg_idle = Theme.BG_DARK
                    bg_hover = Theme.BG_DARK
                    cursor = "no" if sys.platform == "win32" else "X_cursor"
                else:
                    btn_text = f" 📁 {p_path.name}  —  {path}"
                    fg_color = Theme.TEXT_BRIGHT
                    bg_idle = Theme.BG_DARK
                    bg_hover = Theme.BG_HOVER
                    cursor = "hand2"

                entries.append({
                    "path": path,
                    "btn_text": btn_text,
                    "files_str": files_str,
                    "files_color": files_color,
                    "fg_color": fg_color,
                    "bg_idle": bg_idle,
                    "bg_hover": bg_hover,
                    "cursor": cursor,
                })
            except Exception as ex:
                print(f"[WARN] Error scanning recent path '{path}': {ex}")

        try:
            self._recent_projects_queue.put_nowait(entries)
        except Exception:
            pass

    def _poll_recent_projects_result(self):
        """Render recent-project results from the dialog's Tk thread."""
        try:
            entries = self._recent_projects_queue.get_nowait()
        except queue.Empty:
            try:
                if self.win.winfo_exists():
                    self.win.after(40, self._poll_recent_projects_result)
            except Exception:
                pass
            return
        except Exception:
            return
        try:
            if self.win.winfo_exists():
                self._render_recent_projects(entries)
        except Exception:
            pass

    def _render_recent_projects(self, entries: list[dict]):
        """Replace the placeholder with the actual recent project list."""
        if hasattr(self, "_recent_placeholder") and self._recent_placeholder:
            self._recent_placeholder.destroy()
            self._recent_placeholder = None

        f_title, f_sub, f_label, f_btn, f_mono = self._fonts

        if entries:
            tk.Label(
                self.frame_existing, text="Recent Projects (double-click to open):", font=f_label,
                fg=Theme.CYAN, bg=Theme.BG_DARKEST, anchor=tk.W
            ).pack(anchor=tk.W, pady=(8, 4))

            # Scrollable container for the recent project entries
            scroll_outer = tk.Frame(self.frame_existing, bg=Theme.BG_DARKEST)
            scroll_outer.pack(fill=tk.BOTH, expand=True)

            canvas = tk.Canvas(
                scroll_outer, bg=Theme.BG_DARKEST, highlightthickness=0, bd=0
            )
            scrollbar = ttk.Scrollbar(
                scroll_outer, orient=tk.VERTICAL, command=canvas.yview,
                style="Vertical.TScrollbar"
            )

            def _auto_set(lo, hi):
                if float(lo) <= 0.0 and float(hi) >= 1.0:
                    scrollbar.pack_forget()
                else:
                    if not scrollbar.winfo_ismapped():
                        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
                scrollbar.set(lo, hi)

            canvas.configure(yscrollcommand=_auto_set)

            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            recent_frame = tk.Frame(canvas, bg=Theme.BG_DARKEST)
            canvas_window = canvas.create_window((0, 0), window=recent_frame, anchor=tk.NW)

            def _on_recent_frame_configure(event):
                canvas.configure(scrollregion=canvas.bbox("all"))

            def _on_canvas_configure(event):
                canvas.itemconfig(canvas_window, width=event.width)

            recent_frame.bind("<Configure>", _on_recent_frame_configure)
            canvas.bind("<Configure>", _on_canvas_configure)

            def _on_mousewheel(event):
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

            canvas.bind("<MouseWheel>", _on_mousewheel)

            for entry in entries:
                try:
                    path = entry["path"]
                    p_path = Path(path)

                    card = tk.Frame(recent_frame, bg=entry["bg_idle"], padx=8, pady=5, cursor=entry["cursor"])
                    card.pack(fill=tk.X, pady=3)

                    lbl_title = tk.Label(card, text=entry["btn_text"], font=f_mono, fg=entry["fg_color"], bg=entry["bg_idle"], anchor=tk.W, cursor=entry["cursor"])
                    lbl_title.pack(fill=tk.X)

                    lbl_files = tk.Label(card, text=f"    {entry['files_str']}", font=f_sub, fg=entry["files_color"], bg=entry["bg_idle"], anchor=tk.W, cursor=entry["cursor"])
                    lbl_files.pack(fill=tk.X, pady=(2, 0))

                    card.bind("<MouseWheel>", _on_mousewheel)
                    lbl_title.bind("<MouseWheel>", _on_mousewheel)
                    lbl_files.bind("<MouseWheel>", _on_mousewheel)

                    def _make_handlers(c=card, lt=lbl_title, lf=lbl_files, p=path, bg_h=entry["bg_hover"], bg_i=entry["bg_idle"]):
                        def on_enter(e):
                            c.configure(bg=bg_h)
                            lt.configure(bg=bg_h)
                            lf.configure(bg=bg_h)

                        def on_leave(e):
                            c.configure(bg=bg_i)
                            lt.configure(bg=bg_i)
                            lf.configure(bg=bg_i)

                        for widget in (c, lt, lf):
                            widget.bind("<Enter>", on_enter)
                            widget.bind("<Leave>", on_leave)
                            widget.bind("<Button-1>", lambda e, p=p: self.existing_path_var.set(p))
                            widget.bind("<Double-Button-1>", lambda e, p=p: (self.existing_path_var.set(p), self._on_open_existing()))
                    _make_handlers()
                except Exception as ex:
                    print(f"[WARN] Error rendering recent path '{entry['path']}': {ex}")
        else:
            # No recent projects — add a spacer to keep layout clean
            tk.Frame(self.frame_existing, bg=Theme.BG_DARKEST).pack(fill=tk.BOTH, expand=True)

    def _browse_existing(self):
        from tkinter import filedialog
        init_dir = self.existing_path_var.get().strip() or str(Path.home())
        if init_dir:
            try:
                p_init = Path(init_dir)
                if p_init.is_file():
                    init_dir = str(p_init.parent)
            except Exception:
                pass

        selected = filedialog.askopenfilename(
            initialdir=init_dir,
            title="Select Existing Sketch / Project File (.ino, .cpp, .h, .txt)",
            parent=self.win,
            filetypes=[
                ("Project Files & Sketches (*.ino, *.cpp, *.h, *.txt)", "*.ino;*.cpp;*.h;*.hpp;*.txt;platformio.ini"),
                ("Arduino Sketches (*.ino)", "*.ino"),
                ("C/C++ Source & Headers (*.cpp, *.h)", "*.cpp;*.h;*.hpp"),
                ("Text Files (*.txt)", "*.txt"),
                ("All Files (*.*)", "*.*")
            ]
        )
        if selected:
            p = Path(selected)
            folder = p.parent if p.is_file() else p
            self.existing_path_var.set(str(folder))

    def _on_open_existing(self):
        import tkinter.messagebox as mb
        raw = self.existing_path_var.get().strip()
        if not raw:
            mb.showwarning("No Folder Selected", "Please choose a project folder first.", parent=self.win)
            return
        p = Path(raw)
        if not p.exists() or not p.is_dir():
            mb.showerror("Invalid Folder", f"This folder does not exist:\n{p}", parent=self.win)
            return
        owner_pid = folder_lock_owner(p)
        if owner_pid is not None:
            mb.showerror(
                "Project In Use",
                f"This project folder is already open in another MCU Flasher window "
                f"(PID {owner_pid}):\n{p}\n\n"
                "Close that window first, or choose a different project.",
                parent=self.win,
            )
            return

        if not _validate_and_scaffold_ino(self.win, p):
            return

        self.result = p
        self.win.destroy()

    # ── New-project tab ─────────────────────────────────────────────────
    def _build_new_tab(self):
        f_title, f_sub, f_label, f_btn, f_mono = self._fonts
        self.frame_new = tk.Frame(self.body, bg=Theme.BG_DARKEST)

        # Action Buttons row packed at the bottom FIRST so it is always visible and never cut off
        btn_row = tk.Frame(self.frame_new, bg=Theme.BG_DARKEST)
        btn_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        _make_dialog_btn(btn_row, "Cancel", self._on_cancel,
                          Theme.BTN_STOP, Theme.BTN_STOP_H, f_btn).pack(side=tk.RIGHT, padx=(8, 0))
        _make_dialog_btn(btn_row, "Create Project ▶", self._on_create_new,
                          Theme.BTN_COMPILE, Theme.BTN_COMPILE_H, f_btn).pack(side=tk.RIGHT)

        tk.Label(self.frame_new, text="Files to include", font=f_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(anchor=tk.W)

        files_row = tk.Frame(self.frame_new, bg=Theme.BG_DARKEST)
        files_row.pack(fill=tk.X, pady=(6, 16))

        # .ino — always included, shown as a disabled/checked indicator
        ino_chip = tk.Frame(files_row, bg=Theme.BG_LIGHT, padx=10, pady=6)
        ino_chip.pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(ino_chip, text="✔ <name>.ino", font=f_label,
                 fg=Theme.GREEN, bg=Theme.BG_LIGHT).pack()
        tk.Label(files_row, text="(always created)", font=f_sub,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(side=tk.LEFT)

        check_row = tk.Frame(self.frame_new, bg=Theme.BG_DARKEST)
        check_row.pack(fill=tk.X, pady=(0, 4))

        self.var_include_h = tk.BooleanVar(value=False)
        self.var_include_cpp = tk.BooleanVar(value=False)

        def _styled_check(parent, text, var):
            cb = tk.Checkbutton(
                parent, text=text, variable=var,
                font=f_label, fg=Theme.TEXT_BRIGHT, bg=Theme.BG_DARKEST,
                activebackground=Theme.BG_DARKEST, activeforeground=Theme.CYAN,
                selectcolor=Theme.BG_LIGHT, borderwidth=0, highlightthickness=0,
                cursor="hand2", anchor=tk.W,
            )
            return cb

        cb_h = _styled_check(check_row, "  Include <name>.h    →  #pragma once", self.var_include_h)
        cb_h.pack(anchor=tk.W, pady=2)
        cb_cpp = _styled_check(check_row, "  Include <name>.cpp  →  #include \"<name>.h\" (if .h included)", self.var_include_cpp)
        cb_cpp.pack(anchor=tk.W, pady=2)

        tk.Frame(self.frame_new, bg=Theme.BORDER, height=1).pack(fill=tk.X, pady=14)

        # ── Project name ──
        tk.Label(self.frame_new, text="Project name", font=f_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(anchor=tk.W)
        self.new_name_var = tk.StringVar(value="")
        name_entry = tk.Entry(self.frame_new, textvariable=self.new_name_var,
                               font=f_mono, bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
                               insertbackground=Theme.CYAN, borderwidth=0,
                               highlightthickness=1, highlightcolor=Theme.CYAN_DIM,
                               highlightbackground=Theme.BORDER)
        name_entry.pack(fill=tk.X, pady=(6, 14), ipady=4)

        # ── Destination folder ──
        tk.Label(self.frame_new, text="Create inside", font=f_label,
                 fg=Theme.TEXT_DIM, bg=Theme.BG_DARKEST).pack(anchor=tk.W)
        dest_row = tk.Frame(self.frame_new, bg=Theme.BG_DARKEST)
        dest_row.pack(fill=tk.X, pady=(6, 4))

        default_dest = str(Path.home())
        if self.initial_dir:
            try:
                p = Path(self.initial_dir)
                if p.exists():
                    default_dest = str(p.parent)
            except Exception:
                pass
        self.new_dest_var = tk.StringVar(value=default_dest)
        dest_entry = tk.Entry(dest_row, textvariable=self.new_dest_var,
                               font=f_mono, bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
                               insertbackground=Theme.CYAN, borderwidth=0,
                               highlightthickness=1, highlightcolor=Theme.CYAN_DIM,
                               highlightbackground=Theme.BORDER)
        dest_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), ipady=4)
        _make_dialog_btn(dest_row, "Browse…", self._browse_dest,
                          Theme.BTN_CLEAR, Theme.BTN_CLEAR_H, f_label).pack(side=tk.LEFT)

        # Live preview of final path
        self.new_preview_var = tk.StringVar(value="")
        self.new_preview_lbl = tk.Label(
            self.frame_new, textvariable=self.new_preview_var, font=f_sub,
            fg=Theme.CYAN_DIM, bg=Theme.BG_DARKEST, anchor=tk.W,
            justify=tk.LEFT, wraplength=480
        )
        self.new_preview_lbl.pack(anchor=tk.W, pady=(4, 0))

        self.frame_new.bind(
            "<Configure>",
            lambda e: self.new_preview_lbl.configure(wraplength=max(180, e.width - 32))
        )

        self.new_name_var.trace_add("write", lambda *a: self._update_new_preview())
        self.new_dest_var.trace_add("write", lambda *a: self._update_new_preview())
        self._update_new_preview()
        tk.Frame(self.frame_new, bg=Theme.BG_DARKEST).pack(fill=tk.BOTH, expand=True)

    def _update_new_preview(self):
        name = self.new_name_var.get().strip()
        dest = self.new_dest_var.get().strip()
        if name and dest:
            self.new_preview_var.set(f"Will create:  {Path(dest) / name}")
        else:
            self.new_preview_var.set("")

    def _browse_dest(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory(
            initialdir=self.new_dest_var.get() or str(Path.home()),
            title="Select Destination Folder",
            parent=self.win,
        )
        if folder:
            self.new_dest_var.set(folder)

    def _on_create_new(self):
        import tkinter.messagebox as mb
        name = self.new_name_var.get().strip()
        dest_raw = self.new_dest_var.get().strip()

        if not name:
            mb.showwarning("Project Name Required", "Please enter a project name.", parent=self.win)
            return
        if not _VALID_NAME_RE.match(name):
            mb.showerror(
                "Invalid Project Name",
                "Project name must start with a letter or underscore, and contain "
                "only letters, numbers, and underscores (no spaces or symbols).\n\n"
                "This name is reused for the .ino / .h / .cpp filenames.",
                parent=self.win,
            )
            return
        if not dest_raw:
            mb.showwarning("Destination Required", "Please choose a destination folder.", parent=self.win)
            return

        dest_parent = Path(dest_raw)
        if not dest_parent.exists() or not dest_parent.is_dir():
            mb.showerror("Invalid Destination", f"This folder does not exist:\n{dest_parent}", parent=self.win)
            return

        target = dest_parent / name
        if target.exists():
            mb.showerror(
                "Folder Already Exists",
                f"A folder named '{name}' already exists at:\n{dest_parent}\n\n"
                "Choose a different name or destination.",
                parent=self.win,
            )
            return

        try:
            project_dir = _scaffold_new_project(
                dest_parent, name,
                include_h=self.var_include_h.get(),
                include_cpp=self.var_include_cpp.get(),
            )
        except Exception as e:
            mb.showerror("Could Not Create Project", f"Failed to create project:\n{e}", parent=self.win)
            return

        self.result = project_dir
        self.win.destroy()

    def _on_cancel(self):
        self.result = None
        self.win.destroy()

    def run(self) -> Path | None:
        self.win.wait_window()
        return self.result
def _validate_and_scaffold_ino(parent_win, folder_path: Path) -> bool:
    """Validate that the project folder contains at least one .ino file.
    If an .ino file has a case mismatch with the folder name (e.g. Testsketch.ino
    vs TestSketch), prompt the user to rename it before proceeding. If denied,
    show an error dialog and block opening.
    If no .ino files exist, prompt the user to create a default .ino aligned to the folder name.
    Returns True if valid/created/renamed, False if user cancelled/denied or error occurred.
    """
    from tkinter import messagebox

    # 1. Check for case mismatch between .ino files and the folder name
    try:
        folder_name = folder_path.name
        folder_name_lower = folder_name.lower()
        root_files = [f for f in folder_path.iterdir() if f.is_file()]
        mismatched_ino = [
            f for f in root_files
            if f.suffix.lower() == ".ino"
            and f.stem.lower() == folder_name_lower
            and f.stem != folder_name
        ]
        if mismatched_ino:
            bad_file = mismatched_ino[0]
            confirm = messagebox.askyesno(
                "Sketch Case Mismatch Detected",
                f"The sketch filename does not match the folder name's exact casing:\n\n"
                f"  • Folder Name:  {folder_name}\n"
                f"  • Sketch File:  {bad_file.name}\n\n"
                f"Arduino standards require the main sketch filename to match the folder name ({folder_name}.ino).\n\n"
                f"Would you like to rename '{bad_file.name}' to '{folder_name}.ino' to proceed?",
                parent=parent_win,
            )
            if confirm:
                align_sketch_filename_case(folder_path)
            else:
                messagebox.showerror(
                    "Case Mismatch — Cannot Proceed",
                    f"Cannot open project due to a filename case mismatch:\n\n"
                    f"  • Folder Name:  {folder_name}\n"
                    f"  • Sketch File:  {bad_file.name}\n\n"
                    f"Please rename the sketch file to match the folder name exactly ({folder_name}.ino) before opening.",
                    parent=parent_win,
                )
                return False
    except Exception as ex:
        print(f"[WARN] Error checking sketch case match: {ex}")

    try:
        ino_files = list(folder_path.glob("*.ino"))
    except Exception:
        ino_files = []
        
    if ino_files:
        return True

    from tkinter import messagebox
    res = messagebox.askyesno(
        "Create Arduino Sketch?",
        f"The selected folder does not contain any Arduino sketch (.ino) files.\n\n"
        f"Would you like to make this a project directory and create a default .ino file aligned to the current folder?",
        parent=parent_win
    )
    if res:
        try:
            ensure_hidden_read_first_md(folder_path)
            sketch_name = folder_path.name
            safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', sketch_name)
            if not safe_name or safe_name[0].isdigit():
                safe_name = "_" + safe_name if safe_name else "sketch"
            ino_path = folder_path / f"{safe_name}.ino"
            
            default_content = (
                "void setup() {\n"
                "  // put your setup code here, to run once:\n\n"
                "}\n\n"
                "void loop() {\n"
                "  // put your main code here, to run repeatedly:\n\n"
                "}\n"
            )
            ino_path.write_text(default_content, encoding="utf-8")
            return True
        except Exception as e:
            messagebox.showerror(
                "Error Creating File",
                f"Failed to create .ino file:\n{e}",
                parent=parent_win
            )
            return False
    return False


def show_project_selector(root: tk.Tk, initial_dir: str = "") -> Path | None:
    """Show the startup project selector and return the chosen/created
    project folder, or None if the user cancelled."""
    dlg = ProjectSelectorDialog(root, initial_dir)
    return dlg.run()

class BoardSearchDialog(tk.Toplevel):
    """
    Clean modal dialog to search & select from all supported MCU boards.
    """
    def __init__(self, parent, current_board, board_list, on_select_callback):
        super().__init__(parent)
        self.title("🔍 Search & Select MCU Board")
        self.configure(bg=Theme.BG_MID)
        self.resizable(False, False)

        self.on_select_callback = on_select_callback
        self.all_boards = list(board_list)
        self.result = None

        center_toplevel(self, width=460, height=400, parent=parent)

        # Header
        hdr_frame = tk.Frame(self, bg=Theme.BG_DARK, pady=8, padx=12)
        hdr_frame.pack(fill=tk.X)
        tk.Label(
            hdr_frame, text="🔍 Search MCU Board",
            font=("Segoe UI", 11, "bold"), fg=Theme.CYAN, bg=Theme.BG_DARK
        ).pack(side=tk.LEFT)

        # Search Entry
        search_frame = tk.Frame(self, bg=Theme.BG_MID, pady=8, padx=12)
        search_frame.pack(fill=tk.X)

        tk.Label(
            search_frame, text="Search:", font=("Segoe UI", 9, "bold"),
            fg=Theme.TEXT_DIM, bg=Theme.BG_MID
        ).pack(side=tk.LEFT, padx=(0, 6))

        self.search_var = tk.StringVar()
        self.search_ent = tk.Entry(
            search_frame, textvariable=self.search_var,
            font=("Consolas", 10), bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
            insertbackground=Theme.CYAN, borderwidth=0, highlightthickness=1,
            highlightcolor=Theme.CYAN_DIM, highlightbackground=Theme.BORDER
        )
        self.search_ent.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)
        self.search_ent.bind("<KeyRelease>", self._apply_filter)
        self.search_ent.bind("<Down>", self._focus_listbox)
        self.search_ent.bind("<Return>", self._on_return)
        self.search_ent.bind("<Escape>", lambda e: self.destroy())

        # Listbox Frame
        list_frame = tk.Frame(self, bg=Theme.BG_LIGHT, bd=1, relief=tk.SOLID)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))

        self.scrollbar = ttk.Scrollbar(
            list_frame, orient=tk.VERTICAL,
            style="Vertical.TScrollbar"
        )
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.listbox = tk.Listbox(
            list_frame, bg=Theme.BG_LIGHT, fg=Theme.TEXT_BRIGHT,
            selectbackground=Theme.BG_HOVER, selectforeground=Theme.CYAN,
            highlightthickness=0, bd=0, activestyle="none",
            font=("Consolas", 9), yscrollcommand=self.scrollbar.set,
            justify="left", exportselection=False
        )
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.config(command=self.listbox.yview)

        self.listbox.bind("<Double-Button-1>", self._on_double_click)
        self.listbox.bind("<Return>", self._on_return)
        self.listbox.bind("<Escape>", lambda e: self.destroy())

        # Action Buttons
        btn_frame = tk.Frame(self, bg=Theme.BG_MID, pady=8, padx=12)
        btn_frame.pack(fill=tk.X)

        self.btn_select = tk.Button(
            btn_frame, text="Select Board", font=("Segoe UI", 9, "bold"),
            bg=Theme.BTN_MONITOR, fg="#ffffff", activebackground=Theme.BTN_MONITOR_H,
            activeforeground="#ffffff", bd=0, padx=12, pady=4, cursor="hand2", command=self._confirm_selection
        )
        self.btn_select.pack(side=tk.RIGHT, padx=(6, 0))

        btn_cancel = tk.Button(
            btn_frame, text="Cancel", font=("Segoe UI", 9),
            bg=Theme.BTN_STOP, fg="#ffffff", activebackground=Theme.BTN_STOP_H,
            activeforeground="#ffffff",
            bd=0, padx=12, pady=4, cursor="hand2", command=self.destroy
        )
        btn_cancel.pack(side=tk.RIGHT)

        # Populate initial list
        self._populate_list(self.all_boards)
        if current_board in self.all_boards:
            idx = self.all_boards.index(current_board)
            self.listbox.selection_set(idx)
            self.listbox.see(idx)

        # Grab modal focus
        self.transient(parent)
        self.grab_set()
        self.lift()
        self.focus_force()
        self.search_ent.focus_set()
        self.after(50, lambda: (self.lift(), self.focus_force(), self.search_ent.focus_force()))

    def _populate_list(self, items):
        self.listbox.delete(0, tk.END)
        if items:
            self.listbox.insert(tk.END, *items)

    def _apply_filter(self, event=None):
        if event and event.keysym in ("Up", "Down", "Return", "Escape"):
            return
        query = self.search_var.get().strip().lower()
        if not query:
            matches = list(self.all_boards)
        else:
            matches = [b for b in self.all_boards if query in b.lower()]
        self._populate_list(matches)
        if matches:
            self.listbox.selection_set(0)

    def _focus_listbox(self, event=None):
        self.listbox.focus_set()
        if self.listbox.size() > 0 and not self.listbox.curselection():
            self.listbox.selection_set(0)
        return "break"

    def _on_double_click(self, event=None):
        self._confirm_selection()

    def _on_return(self, event=None):
        self._confirm_selection()

    def _confirm_selection(self):
        sel = self.listbox.curselection()
        if sel:
            board = self.listbox.get(sel[0])
            self.result = board
            if self.on_select_callback:
                self.on_select_callback(board)
        self.destroy()


__all__ = [
    "BoardSearchDialog",
    "ProjectSelectorDialog",
    "_make_dialog_btn",
    "_make_toolbar_btn",
    "_scaffold_new_project",
    "_validate_and_scaffold_ino",
    "show_project_selector"
]
