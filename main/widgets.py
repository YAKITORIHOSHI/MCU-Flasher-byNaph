#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import sys
import ctypes
import tkinter as tk
from tkinter import ttk


from main.core.constants import *
from main.core.theme import *
from main.core.config import *
from main.core.file_utils import *
from main.core.toolchain import *
from main.core.board_catalog import *
from main.core.board_compat import *

class _ShellTerminalBuffer:
    """Small ANSI/VT screen model for the embedded Windows PTY.

    PowerShell's PSReadLine redraws the current command after nearly every
    keystroke.  Appending those redraws to a Tk Text widget makes one command
    look duplicated and hides the real command/error flow.  This deliberately
    lightweight screen model handles the control sequences used by cmd.exe,
    Windows PowerShell, and PSReadLine without adding another dependency.
    """

    def __init__(self, columns=120, max_rows=2500):
        self.columns = max(40, int(columns))
        self.max_rows = max(200, int(max_rows))
        self.rows = [[]]
        self.row_styles = [[]]
        self.row = 0
        self.column = 0
        self.saved_position = (0, 0)
        self.escape = ""
        self.style = {
            "foreground": None,
            "background": None,
            "bold": False,
            "dim": False,
            "underline": False,
        }

    def _ensure_row(self, row=None):
        row = self.row if row is None else max(0, int(row))
        while len(self.rows) <= row:
            self.rows.append([])
            self.row_styles.append([])
        return self.rows[row]

    def _trim_scrollback(self):
        if len(self.rows) <= self.max_rows:
            return
        trim = len(self.rows) - self.max_rows
        del self.rows[:trim]
        del self.row_styles[:trim]
        self.row = max(0, self.row - trim)
        saved_row, saved_col = self.saved_position
        self.saved_position = (max(0, saved_row - trim), saved_col)

    def _clear_all(self):
        self.rows = [[]]
        self.row_styles = [[]]
        self.row = 0
        self.column = 0
        self.style = {
            "foreground": None,
            "background": None,
            "bold": False,
            "dim": False,
            "underline": False,
        }

    def _style_name(self):
        """Return a compact Tk tag name for the active ANSI text style."""
        foreground = self.style.get("foreground") or "default"
        flags = []
        if self.style.get("bold"):
            flags.append("bold")
        if self.style.get("dim"):
            flags.append("dim")
        if self.style.get("underline"):
            flags.append("underline")
        suffix = "_".join(flags) if flags else "normal"
        # xterm's background is fixed to the dark terminal surface here; keep
        # foreground and emphasis in the tag so every visible ANSI color maps
        # to a configured Tk style.
        return f"ansi_{foreground}_{suffix}"

    def _erase_line(self, mode):
        line = self._ensure_row()
        styles = self.row_styles[self.row]
        if mode == 1:
            for index in range(min(self.column, len(line))):
                line[index] = " "
                if index < len(styles):
                    styles[index] = self._style_name()
        elif mode == 2:
            line[:] = []
            styles[:] = []
        else:
            del line[self.column:]
            del styles[self.column:]

    def _erase_display(self, mode):
        if mode in (2, 3):
            self._clear_all()
            return
        if mode == 1:
            for index in range(min(self.row, len(self.rows))):
                self.rows[index] = []
                self.row_styles[index] = []
            line = self._ensure_row()
            styles = self.row_styles[self.row]
            for index in range(min(self.column + 1, len(line))):
                line[index] = " "
                if index < len(styles):
                    styles[index] = self._style_name()
            return
        self._erase_line(0)
        for index in range(self.row + 1, len(self.rows)):
            self.rows[index] = []
            self.row_styles[index] = []
        while self.rows and not self.rows[-1]:
            self.rows.pop()
            if self.row_styles:
                self.row_styles.pop()
        if not self.rows:
            self.rows = [[]]
            self.row_styles = [[]]

    @staticmethod
    def _params(raw):
        raw = str(raw or "").lstrip("?>")
        if not raw:
            return []
        values = []
        for value in raw.split(";"):
            value = value.strip()
            if not value:
                values.append(1)
                continue
            try:
                values.append(int(value))
            except ValueError:
                values.append(0)
        return values

    def _csi(self, raw, final):
        params = self._params(raw)
        first = params[0] if params else 1

        if final in ("H", "f"):
            self.row = max(0, (params[0] if len(params) > 0 else 1) - 1)
            self.column = max(0, (params[1] if len(params) > 1 else 1) - 1)
            self._ensure_row()
        elif final == "A":
            self.row = max(0, self.row - first)
        elif final == "B":
            self.row += first
            self._ensure_row()
        elif final == "C":
            self.column += first
        elif final == "D":
            self.column = max(0, self.column - first)
        elif final in ("G", "`"):
            self.column = max(0, first - 1)
        elif final == "d":
            self.row = max(0, first - 1)
            self._ensure_row()
        elif final == "E":
            self.row += first
            self.column = 0
            self._ensure_row()
        elif final == "F":
            self.row = max(0, self.row - first)
            self.column = 0
        elif final == "J":
            self._erase_display(params[0] if params else 0)
        elif final == "K":
            self._erase_line(params[0] if params else 0)
        elif final == "m":
            self._apply_sgr(params)
        elif final == "s":
            self.saved_position = (self.row, self.column)
        elif final == "u":
            self.row, self.column = self.saved_position
            self._ensure_row()
        elif final == "P":
            line = self._ensure_row()
            styles = self.row_styles[self.row]
            count = max(1, first)
            del line[self.column:self.column + count]
            del styles[self.column:self.column + count]
        elif final == "@":
            line = self._ensure_row()
            styles = self.row_styles[self.row]
            count = max(1, first)
            line[self.column:self.column] = [" "] * count
            styles[self.column:self.column] = [self._style_name()] * count
        elif final == "X":
            line = self._ensure_row()
            styles = self.row_styles[self.row]
            count = max(1, first)
            while len(line) < self.column + count:
                line.append(" ")
                styles.append(self._style_name())
            for index in range(self.column, self.column + count):
                line[index] = " "
                styles[index] = self._style_name()
        elif final == "L":
            count = max(1, first)
            self.rows[self.row:self.row] = ([[] for _ in range(count)])
            self.row_styles[self.row:self.row] = ([[] for _ in range(count)])
        elif final == "M":
            count = max(1, first)
            del self.rows[self.row:self.row + count]
            del self.row_styles[self.row:self.row + count]
            if not self.rows:
                self.rows = [[]]
                self.row_styles = [[]]
            self.row = min(self.row, len(self.rows) - 1)
        # Cursor visibility, device status, and mode changes are intentionally
        # ignored; they do not change the visible text model.

    def _apply_sgr(self, params):
        """Track the ANSI SGR subset used by cmd, PowerShell, and OpenCode."""
        if not params:
            params = [0]
        foregrounds = {
            30: "black", 31: "red", 32: "green", 33: "yellow",
            34: "blue", 35: "magenta", 36: "cyan", 37: "white",
            90: "bright_black", 91: "bright_red", 92: "bright_green",
            93: "bright_yellow", 94: "bright_blue", 95: "bright_magenta",
            96: "bright_cyan", 97: "bright_white",
        }
        backgrounds = {
            40: "black", 41: "red", 42: "green", 43: "yellow",
            44: "blue", 45: "magenta", 46: "cyan", 47: "white",
            100: "bright_black", 101: "bright_red", 102: "bright_green",
            103: "bright_yellow", 104: "bright_blue",
            105: "bright_magenta", 106: "bright_cyan", 107: "bright_white",
        }
        for value in params:
            if value == 0:
                self.style = {
                    "foreground": None,
                    "background": None,
                    "bold": False,
                    "dim": False,
                    "underline": False,
                }
            elif value == 1:
                self.style["bold"] = True
                self.style["dim"] = False
            elif value == 2:
                self.style["dim"] = True
            elif value == 4:
                self.style["underline"] = True
            elif value == 22:
                self.style["bold"] = False
                self.style["dim"] = False
            elif value == 24:
                self.style["underline"] = False
            elif value == 39:
                self.style["foreground"] = None
            elif value == 49:
                self.style["background"] = None
            elif value in foregrounds:
                self.style["foreground"] = foregrounds[value]
            elif value in backgrounds:
                self.style["background"] = backgrounds[value]

    def _put(self, char):
        line = self._ensure_row()
        styles = self.row_styles[self.row]
        while len(line) <= self.column:
            line.append(" ")
            styles.append(self._style_name())
        line[self.column] = char
        styles[self.column] = self._style_name()
        self.column += 1
        if self.column >= self.columns:
            self.column = self.columns

    def feed(self, data):
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        for char in str(data or ""):
            if self.escape:
                self.escape += char
                if self.escape.startswith("\x1b]"):
                    if char == "\x07" or self.escape.endswith("\x1b\\"):
                        self.escape = ""
                    continue
                if self.escape == "\x1b[":
                    continue
                if self.escape.startswith("\x1b["):
                    if "@" <= char <= "~":
                        self._csi(self.escape[2:-1], char)
                        self.escape = ""
                    continue
                if len(self.escape) == 2:
                    if char == "7":
                        self.saved_position = (self.row, self.column)
                    elif char == "8":
                        self.row, self.column = self.saved_position
                        self._ensure_row()
                    elif char == "c":
                        self._clear_all()
                    self.escape = ""
                continue

            if char == "\x1b":
                self.escape = char
            elif char in ("\r",):
                self.column = 0
            elif char in ("\n", "\v", "\f"):
                self.row += 1
                self.column = 0
                self._ensure_row()
            elif char == "\b":
                self.column = max(0, self.column - 1)
            elif char == "\t":
                self.column = min(self.columns, ((self.column // 8) + 1) * 8)
                self._ensure_row()
            elif char == "\x07" or ord(char) < 0x20:
                continue
            else:
                self._put(char)
        self._trim_scrollback()

    def render(self):
        last = self._last_visible_row()
        lines = ["".join(row).rstrip() for row in self.rows[:last + 1]]
        return "\n".join(lines)

    def _last_visible_row(self):
        last = max(self.row, 0)
        while last + 1 < len(self.rows) and not self.rows[last + 1]:
            last += 1
        return last

    def render_styled(self):
        """Return visible rows as ``[(text, tag), ...]`` runs for Tk."""
        last = self._last_visible_row()
        rendered = []
        for row, styles in zip(self.rows[:last + 1], self.row_styles[:last + 1]):
            end = len(row)
            while end and row[end - 1] == " ":
                end -= 1
            row = row[:end]
            styles = styles[:end]
            runs = []
            if row:
                start = 0
                current = styles[0] if styles else "ansi_default_normal"
                for index in range(1, len(row)):
                    tag = styles[index] if index < len(styles) else current
                    if tag != current:
                        runs.append(("".join(row[start:index]), current))
                        start = index
                        current = tag
                runs.append(("".join(row[start:]), current))
            rendered.append(runs)
        return rendered

def _get_widget_dpi_scale(widget: tk.Widget) -> float:
    """Return the display scale for a Tk window (1.0 at 96 DPI)."""
    try:
        tk_scale = float(widget.tk.call("tk", "scaling")) / (96.0 / 72.0)
    except Exception:
        tk_scale = 1.0
    if sys.platform == "win32":
        try:
            import ctypes

            get_dpi = getattr(ctypes.windll.user32, "GetDpiForWindow", None)
            if get_dpi is not None:
                get_dpi.argtypes = [ctypes.c_void_p]
                get_dpi.restype = ctypes.c_uint
                dpi = int(get_dpi(ctypes.c_void_p(widget.winfo_id())))
                if dpi > 0:
                    tk_scale = dpi / 96.0
        except Exception:
            pass
    return max(0.75, min(3.0, tk_scale))


def _get_monitor_work_area(widget: tk.Widget) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) for the window's nearest monitor."""
    try:
        fallback = (0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight())
    except Exception:
        fallback = (0, 0, 1920, 1080)
    if sys.platform != "win32":
        return fallback
    try:
        import ctypes
        from ctypes import wintypes

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)
        ]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        monitor = user32.MonitorFromWindow(wintypes.HWND(widget.winfo_id()), 2)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect = info.rcWork
            if rect.right > rect.left and rect.bottom > rect.top:
                return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        pass
    return fallback


def center_toplevel(toplevel: tk.Toplevel, parent: tk.Tk | tk.Toplevel, width: int | None = None, height: int | None = None):
    """Center a Toplevel dialog relative to its parent window.
    If width or height is None or 0, dynamically measures the Toplevel's required content size.
    Clamps dimensions to fit within current screen boundaries.
    """
    toplevel.update_idletasks()
    if parent:
        try:
            parent.update_idletasks()
        except Exception:
            pass

    req_w = toplevel.winfo_reqwidth()
    req_h = toplevel.winfo_reqheight()

    dpi_scale = _get_widget_dpi_scale(toplevel)
    w = round(width * dpi_scale) if (width and width > 0) else req_w
    h = round(height * dpi_scale) if (height and height > 0) else req_h

    work_left, work_top, work_right, work_bottom = _get_monitor_work_area(toplevel)
    screen_w = work_right - work_left
    screen_h = work_bottom - work_top
    margin_x = round(40 * dpi_scale)
    margin_y = round(60 * dpi_scale)

    w = min(w, max(240, screen_w - margin_x))
    h = min(h, max(200, screen_h - margin_y))

    if parent and parent.winfo_viewable() and parent.winfo_ismapped() and parent.winfo_width() > 1:
        parent_w = parent.winfo_width()
        parent_h = parent.winfo_height()
        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        x = parent_x + (parent_w - w) // 2
        y = parent_y + (parent_h - h) // 2
    else:
        x = work_left + (screen_w - w) // 2
        y = work_top + (screen_h - h) // 2

    x = min(max(work_left, x), max(work_left, work_right - w))
    y = min(max(work_top, y), max(work_top, work_bottom - h))
    x_part = f"+{x}" if x >= 0 else str(x)
    y_part = f"+{y}" if y >= 0 else str(y)
    toplevel.geometry(f"{w}x{h}{x_part}{y_part}")


def safe_reclaim_os_focus(widget: tk.Widget):
    """Safely steal Windows OS keyboard focus back from embedded pywebview/Monaco Editor
    to a Tkinter widget after a 10ms delay, avoiding Win32 message pump deadlocks.
    """
    def _do_steal():
        try:
            top = widget.winfo_toplevel()
            top.focus_force()
            if hasattr(widget, "focus_force"):
                widget.focus_force()
            widget.focus_set()
        except Exception:
            pass
    try:
        widget.after(10, _do_steal)
    except Exception:
        pass


def setup_combobox_place_popdown(root: tk.Widget):
    """Override Tcl ::ttk::combobox::PlacePopdown to support opening popdown lists upwards ('above')
    when requested via set_combobox_direction(combo, 'above').
    """
    tcl_override = f"""
    proc ::ttk::combobox::PlacePopdown {{cb popdown}} {{
        set x [winfo rootx $cb]
        set y [winfo rooty $cb]
        set w [winfo width $cb]
        set h [winfo height $cb]
        set style [$cb cget -style]
        if {{ $style eq {{}} }} {{
          set style TCombobox
        }}
        set postoffset [ttk::style lookup $style -postoffset {{}} {{0 0 0 0}}]
        foreach var {{x y w h}} delta $postoffset {{
            incr $var $delta
        }}

        set H [winfo reqheight $popdown]
        if {{[info exists ::combobox_direction($cb)] && $::combobox_direction($cb) eq "above"}} {{
            set Y [expr {{$y - $H}}]
        }} elseif {{$y + $h + $H > [winfo screenheight $popdown]}} {{
            set Y [expr {{$y - $H}}]
        }} else {{
            set Y [expr {{$y + $h}}]
        }}
        wm geometry $popdown ${{w}}x${{H}}+${{x}}+${{Y}}
    }}
    """
    try:
        root.tk.eval(tcl_override)
    except Exception:
        pass


def set_combobox_direction(combo: ttk.Combobox, direction: str = "above"):
    """Specify popdown list orientation for a ttk.Combobox ('above' or 'below')."""
    try:
        combo.tk.call("set", f"::combobox_direction({combo})", direction)
    except Exception:
        pass

class ToolTip:
    """Creates a custom floating tooltip when hovering over a widget."""
    def __init__(self, widget, text_func):
        self.widget = widget
        self.text_func = text_func
        self.tip_window = None
        self.widget.bind("<Enter>", self.show_tip)
        self.widget.bind("<Leave>", self.hide_tip)

    def show_tip(self, event=None):
        if self.tip_window or not self.text_func():
            return
        
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        
        # Styled tooltip matching the theme palette
        label = tk.Label(
            tw, text=self.text_func(), justify=tk.LEFT,
            background=Theme.BG_LIGHT, foreground=Theme.TEXT_BRIGHT,
            relief=tk.SOLID, borderwidth=1,
            highlightbackground=Theme.BORDER,
            padx=8, pady=4,
            font=("Segoe UI", 9)
        )
        label.pack(ipadx=1)

        # Force geometry calculation to get accurate window width
        tw.update_idletasks()
        
        # Calculate screen/widget-relative tooltip position
        widget_x = self.widget.winfo_rootx()
        widget_w = self.widget.winfo_width()
        widget_h = self.widget.winfo_height()
        tip_w = tw.winfo_width()
        
        x = widget_x + 10
        y = self.widget.winfo_rooty() + widget_h + 5
        
        # Check if tooltip extends beyond screen width
        try:
            screen_w = self.widget.winfo_screenwidth()
            if x + tip_w > screen_w:
                x = widget_x + widget_w - tip_w
        except Exception:
            pass
            
        if x < 0:
            x = 10
            
        tw.wm_geometry(f"+{x}+{y}")

    def hide_tip(self, event=None):
        tw = self.tip_window
        self.tip_window = None
        if tw:
            tw.destroy()

class CircularLoadingOverlay(tk.Frame):
    """
    Sleek, modern loading overlay with an animated circular spinner.
    Adapts seamlessly to Light, Dark, and Solarized Dark themes.
    """
    def __init__(
        self,
        parent,
        bg_color=None,
        spinner_color=None,
        fg_title=None,
        fg_sub=None,
        track_color=None,
        text="⚡ MCU Flasher by Naph",
    ):
        bg_color = bg_color or Theme.BG_DARKEST
        spinner_color = spinner_color or Theme.CYAN
        fg_title = fg_title or Theme.TEXT_BRIGHT
        fg_sub = fg_sub or Theme.TEXT_DIM
        track_color = track_color or Theme.BORDER

        super().__init__(parent, bg=bg_color)
        self.bg_color = bg_color
        self.spinner_color = spinner_color
        self.track_color = track_color
        self.angle = 0
        self.is_animating = True
        self._after_id = None

        self.center_frame = tk.Frame(self, bg=bg_color)
        self.center_frame.place(relx=0.5, rely=0.5, anchor="center")

        self.size = 64
        self.canvas = tk.Canvas(
            self.center_frame,
            width=self.size,
            height=self.size,
            bg=bg_color,
            highlightthickness=0
        )
        self.canvas.pack(pady=(0, 16))

        self.title_label = tk.Label(
            self.center_frame,
            text=text,
            font=("Segoe UI", 13, "bold"),
            fg=fg_title,
            bg=bg_color
        )
        self.title_label.pack(pady=(0, 6))

        self.sub_label = tk.Label(
            self.center_frame,
            text="Preparing workspace & loading editor...",
            font=("Segoe UI", 9),
            fg=fg_sub,
            bg=bg_color
        )
        self.sub_label.pack()

        self._draw_spinner()

    def _draw_spinner(self):
        if not self.is_animating:
            return
        try:
            self.canvas.delete("all")
            pad = 6
            r = self.size - pad
            self.canvas.create_oval(
                pad, pad, r, r,
                outline=self.track_color,
                width=4
            )
            self.canvas.create_arc(
                pad, pad, r, r,
                start=self.angle,
                extent=100,
                outline=self.spinner_color,
                style="arc",
                width=4
            )
            self.angle = (self.angle + 12) % 360
            self._after_id = self.after(30, self._draw_spinner)
        except Exception:
            pass

    def update_message(self, title=None, subtitle=None):
        """Update loader copy without recreating the overlay or spinner."""
        try:
            if title is not None and self.title_label.winfo_exists():
                self.title_label.configure(text=str(title))
            if subtitle is not None and self.sub_label.winfo_exists():
                self.sub_label.configure(text=str(subtitle))
        except Exception:
            pass

    def stop_and_destroy(self):
        self.is_animating = False
        if self._after_id:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        try:
            self.place_forget()
            self.destroy()
        except Exception:
            pass

def _configure_windows_dpi_awareness() -> None:
    """Enable DPI handling before the first Tk window is created."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # PER_MONITOR_AWARE_V2 on current Windows 10/11 builds.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


__all__ = [
    "CircularLoadingOverlay",
    "ToolTip",
    "_ShellTerminalBuffer",
    "_configure_windows_dpi_awareness",
    "_get_monitor_work_area",
    "_get_widget_dpi_scale",
    "center_toplevel",
    "safe_reclaim_os_focus",
    "set_combobox_direction",
    "setup_combobox_place_popdown"
]
