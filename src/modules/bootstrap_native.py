"""Standard-library setup window for a private Python without Qt packages.

The installer pipeline stays in bootstrap.py. Workers enqueue events; only the
creating thread calls Tk. After Python dependencies are ready, its display can
move to the original Qt window without restarting the installer worker.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

from src.modules.bootstrap_dispatch import BootstrapDispatcher
from src.modules.bootstrap_presentation import BootstrapPresentation, concise_status
from src.modules.tk_glass import ui_scale
from src.modules.ui_metrics import WorkArea, fit_rect
from src.modules.ui_palette import mix


class _GlassCard(tk.Canvas):
    """Rounded static glass rim around an opaque, readable content surface."""

    def __init__(self, parent, palette, *, stretch=False, padding=14):
        super().__init__(parent, bg=palette["T_BG_DARKEST"], bd=0,
                         highlightthickness=0, height=1)
        self.palette = palette
        self.padding = round(padding * ui_scale(self))
        self.stretch = stretch
        self._pending = None
        self.body = tk.Frame(self, bg=palette["T_BG_MID"], bd=0)
        self._body_item = self.create_window(self.padding, self.padding, window=self.body, anchor="nw")
        self.bind("<Configure>", self._schedule)
        self.body.bind("<Configure>", self._schedule)
        self.bind("<Destroy>", self._cancel, add="+")

    def _schedule(self, _event=None):
        if self._pending is None:
            self._pending = self.after(35, self._draw)

    def _cancel(self, event):
        if event.widget is self and self._pending is not None:
            self.after_cancel(self._pending)
            self._pending = None

    def set_padding(self, padding):
        padding = max(0, int(padding))
        if padding != self.padding:
            self.padding = padding
            self.coords(self._body_item, padding, padding)
            self._schedule()

    def _draw(self):
        self._pending = None
        p, inset = self.palette, self.padding
        width = self.winfo_width()
        height = self.winfo_height() if self.stretch else self.body.winfo_reqheight() + inset * 2
        if not self.stretch and self.winfo_pixels(self.cget("height")) != height:
            self.configure(height=height)
        self.itemconfigure(self._body_item, width=max(1, width - inset * 2),
                           height=max(1, height - inset * 2) if self.stretch else 0)
        self.delete("glass")
        scale = ui_scale(self)
        radius = min(round(16 * scale), max(1, height // 2))

        def rounded(left, top, right, bottom, **options):
            self.create_polygon(left + radius, top, right - radius, top, right, top,
                                right, top + radius, right, bottom - radius, right, bottom,
                                right - radius, bottom, left + radius, bottom, left, bottom,
                                left, bottom - radius, left, top + radius, left, top,
                                smooth=True, splinesteps=24, tags="glass", **options)

        depth = max(2, round(3 * scale))
        rounded(depth, depth + 1, width - 1, height - 1,
                fill=mix(p["T_BG_DARKEST"], "#000000", .20), outline="")
        rounded(1, 1, width - depth, height - depth,
                fill=p["T_BG_MID"], outline=mix(p["T_BORDER"], p["T_CYAN"], .15))
        rim = mix(p["T_BORDER"], "#ffffff", .55)
        self.create_line(radius, 2, width - radius - depth, 2,
                         fill=rim, width=max(1, scale), tags="glass")
        # Reflections live in the clear perimeter; the journal and controls
        # retain their opaque reading surfaces. Nothing samples the desktop.
        band = max(3, inset - round(3 * scale))
        for step in range(band):
            strength = .28 * (1 - step / band)
            y = 3 + step
            self.create_line(radius + 2, y, width - radius - depth, y,
                             fill=mix(p["T_BG_MID"], "#ffffff", strength), tags="glass")
        for step in range(max(2, round(4 * scale))):
            self.create_line(radius + round(8 * scale), 4 + step,
                             max(radius + 1, width * .56), band + 1 - step,
                             fill=mix(p["T_BG_MID"], "#ffffff", .15 - step * .018),
                             width=max(1, scale), tags="glass")
        self.create_line(3, radius, 3, height - radius - depth,
                         fill=mix(p["T_BORDER"], "#ffffff", .32), tags="glass")
        self.create_line(4, radius + round(6 * scale), 4, max(radius + 1, height * .64),
                         fill=mix(p["T_BG_MID"], "#ffffff", .16), tags="glass")
        self.create_line(width - depth - 1, radius, width - depth - 1, height - radius - depth,
                         fill=mix(p["T_BG_MID"], p["T_BG_DARKEST"], .65),
                         width=max(1, scale), tags="glass")
        self.create_line(radius, height - depth - 1, width - radius - depth, height - depth - 1,
                         fill=mix(p["T_BG_MID"], p["T_BG_DARKEST"], .72),
                         width=max(1, 2 * scale), tags="glass")
        self.tag_lower("glass", self._body_item)


class _CircuitMark(tk.Canvas):
    """The setup's etched-circuit mark, drawn without a font or image asset."""

    def __init__(self, parent, palette):
        scale = ui_scale(parent)
        super().__init__(parent, width=round(48 * scale), height=round(48 * scale),
                         bg=palette["T_BG_MID"], bd=0, highlightthickness=0)
        line = mix(palette["T_CYAN"], palette["T_TEXT_DIM"], .20)
        def path(points):
            self.create_line(*(round((8 + value * 4 / 3) * scale, 2) for value in points),
                             fill=line, width=max(1, 1.65 * scale), capstyle="round", joinstyle="round")
        path((7, 4, 17, 4, 20, 7, 20, 17, 17, 20, 7, 20, 4, 17, 4, 7, 7, 4))
        path((13, 7, 8, 13, 12, 13, 11, 17, 17, 10, 13, 10, 14, 7, 13, 7))
        for points in ((8, 1, 8, 4), (12, 1, 12, 4), (16, 1, 16, 4),
                       (8, 20, 8, 23), (12, 20, 12, 23), (16, 20, 16, 23),
                       (1, 8, 4, 8), (1, 12, 4, 12), (1, 16, 4, 16),
                       (20, 8, 23, 8), (20, 12, 23, 12), (20, 16, 23, 16)):
            path(points)


class _LinearProgress(tk.Canvas):
    """A thin native-pixel progress track compatible with configure(value=)."""

    def __init__(self, parent, palette):
        self.palette, self.value = palette, 0
        super().__init__(parent, bg=palette["T_BG_DARKEST"], height=max(3, round(6 * ui_scale(parent))),
                         bd=0, highlightthickness=0)
        self.bind("<Configure>", self._draw)

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}
            cnf = None
        changed = "value" in kwargs
        if changed:
            self.value = max(0, min(100, kwargs.pop("value")))
        result = super().configure(cnf, **kwargs)
        if changed:
            self._draw()
        return result

    config = configure

    def cget(self, key):
        return self.value if key == "value" else super().cget(key)

    def _draw(self, _event=None):
        self.delete("progress")
        width, height = self.winfo_width(), self.winfo_height()
        if self.value:
            self.create_rectangle(0, 0, width * self.value / 100, height,
                                  fill=self.palette["T_CYAN"], outline="", tags="progress")


class _PackageProgress(tk.Canvas):
    """Compact package rows with event-driven progress, never a redraw loop."""

    def __init__(self, parent, palette, font_name):
        super().__init__(parent, bg=palette["T_BG_MID"], bd=0, highlightthickness=0, height=1)
        self.palette, self.font_name = palette, font_name
        self._name_font = tkfont.Font(self, family=font_name, size=9, weight="bold")
        self._state_font = tkfont.Font(self, family=font_name, size=8)
        self.rows = ()
        self._height_budget = None
        self._pending = None
        self.bind("<Configure>", self._schedule)
        parent.bind("<Configure>", self._schedule, add="+")
        self.bind("<Destroy>", self._cancel, add="+")

    def set_rows(self, rows):
        self.rows = tuple(rows)
        self._schedule()

    def set_height_budget(self, height):
        height = max(0, int(height))
        if height != self._height_budget:
            self._height_budget = height
            self._schedule()

    def _schedule(self, _event=None):
        if self._pending is None:
            self._pending = self.after_idle(self._draw)

    def _cancel(self, event):
        if event.widget is self and self._pending is not None:
            self.after_cancel(self._pending)
            self._pending = None

    def _draw(self):
        self._pending = None
        p, scale = self.palette, ui_scale(self)
        width = max(1, self.winfo_width())
        self.delete("all")
        if not self.rows:
            self.configure(height=1)
            return
        # Keep the active entries in view; the raw details retain every row.
        compact = self.master.winfo_height() < round(260 * scale)
        row_height = round((32 if compact else 42) * scale)
        name_font, state_font = self._name_font, self._state_font
        active = [row for row in self.rows if row.tone != "ok"]
        completed = sum(row.tone == "ok" for row in self.rows)
        if not active:
            height = round(26 * scale)
            if self._height_budget is not None and self._height_budget < height:
                self.configure(height=1)
                return
            self.configure(height=height)
            label = f"{completed} package{'s' if completed != 1 else ''} complete" if completed else "Package checks complete"
            self.create_text(round(13 * scale), round(7 * scale), text=label,
                             font=state_font, fill=p["T_TEXT_DIM"], anchor="nw")
            return
        budget = 1 if compact else max(1, min(4, int(self.master.winfo_height() * .40 / row_height)))
        if self._height_budget is not None:
            note_height = round((18 if compact else 22) * scale)
            budget = min(budget, max(0, (self._height_budget - note_height) // row_height))
            if not budget:
                if self._height_budget >= note_height:
                    self.configure(height=note_height)
                    self.create_text(round(13 * scale), 0,
                                     text=f"{len(active)} active packages in technical details",
                                     font=state_font, fill=p["T_TEXT_DIM"], anchor="nw")
                else:
                    self.configure(height=1)
                return
        ordered = sorted(active, key=lambda row: {"dim": 0, "normal": 1, "warn": 2, "fail": 3}.get(row.tone, 1))
        rows = ordered[-budget:]
        remaining = len(active) - len(rows)
        note = []
        if completed:
            note.append(f"{completed} complete")
        if remaining:
            note.append(f"{remaining} more in technical details")
        self.configure(height=len(rows) * row_height + round(((18 if compact else 22) if note else 8) * scale))
        colors = {"ok": p["T_GREEN"], "warn": p["T_YELLOW"], "fail": p["T_RED"], "dim": p["T_TEXT_DIM"]}
        for index, row in enumerate(rows):
            y = index * row_height + round(9 * scale)
            color = colors.get(row.tone, p["T_CYAN"])
            suffix = f"  {row.percent}%" if row.percent is not None else ""
            state = row.status
            while state and state_font.measure(state + "…" + suffix) > width * .44:
                state = state[:-1]
            label = state + ("…" if state != row.status else "") + suffix
            budget = max(round(80 * scale), width - min(state_font.measure(label), width // 2) - round(34 * scale))
            name = row.name
            while name and name_font.measure(name + "…") > budget:
                name = name[:-1]
            if name != row.name:
                name += "…"
            self.create_oval(1, y + round(4 * scale), round(5 * scale), y + round(8 * scale), fill=color, outline="")
            self.create_text(round(13 * scale), y, text=name, font=name_font,
                             fill=p["T_TEXT_BRIGHT"], anchor="nw")
            self.create_text(width - 1, y + round(scale), text=label, font=state_font, fill=color, anchor="ne")
            bar_y = y + round(23 * scale)
            self.create_rectangle(round(13 * scale), bar_y, width - 1, bar_y + max(2, round(3 * scale)),
                                  fill=p["T_BG_DARKEST"], outline="")
            if row.percent is not None:
                end = round(13 * scale) + (width - round(13 * scale) - 1) * max(0, min(100, row.percent)) / 100
                self.create_rectangle(round(13 * scale), bar_y, end, bar_y + max(2, round(3 * scale)),
                                      fill=color, outline="")
        if note:
            self.create_text(round(13 * scale), len(rows) * row_height + round(3 * scale),
                             text=" · ".join(note), font=state_font, fill=p["T_TEXT_DIM"], anchor="nw")


class NativeSignals(BootstrapDispatcher):
    """Compatibility facade for an independently created native setup view."""
    def __init__(self, window):
        self.window = window
        super().__init__(window.thread_id, window.dispatch)


def _monitor_area(root):
    """Own-window monitor geometry in Tk native pixels, with a portable fallback."""
    area = WorkArea(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
    screen = area
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class MonitorInfo(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.MonitorFromWindow.restype = wintypes.HANDLE
            user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            monitor = user32.MonitorFromWindow(root.winfo_id(), 2)
            if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                rect, full = info.rcWork, info.rcMonitor
                area = WorkArea(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
                screen = WorkArea(full.left, full.top, full.right - full.left, full.bottom - full.top)
        except (AttributeError, OSError, ValueError):
            pass
    return area, screen


class NativeBootstrapWindow:
    """Same setup header, reading surface and footer before PySide6 exists."""
    DISPLAY_CHARS = 256000
    DISPLAY_LINES = 2500
    SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, gui, palette, load_config, save_config, record_exception, *, dispatcher=None):
        self.gui, self.palette = gui, palette
        self.load_config, self.save_config = load_config, save_config
        self.record_exception = record_exception
        self.thread_id = threading.get_ident()
        self.closed = False
        self.held = False
        self.user_scrolled_up = False
        self._step_failed = False
        self._live_type = None
        self._presentation = BootstrapPresentation()
        self._summary_step_failed = False
        self._details_expanded = False
        self.summary_user_scrolled_up = False
        self._timers = set()
        self._spin_index = 0
        self._density = None
        self._layout_pending = False
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("MCU Flasher by Naph — Setup")
        self.root.configure(bg=palette["T_BG_DARKEST"])
        self._window_icon = None
        try:
            icon_path = Path(__file__).resolve().parents[1] / "assets" / "icons" / "app_chip.png"
            self._window_icon = tk.PhotoImage(master=self.root, file=str(icon_path))
            self.root.iconphoto(True, self._window_icon)
        except (tk.TclError, OSError):
            pass
        self.root.protocol("WM_DELETE_WINDOW", self._request_close)
        self.signals = NativeSignals(self) if dispatcher is None else dispatcher
        self.signals.attach(self.dispatch)
        self._build()
        self.root.bind("<Configure>", self._schedule_layout, add="+")
        self.root.update_idletasks()
        area, screen = _monitor_area(self.root)
        scale = ui_scale(self.root)
        floor_w, floor_h = round(520 * scale), round(420 * scale)
        width = max(floor_w, round(screen.width * .7))
        height = max(floor_h, round(screen.height * .7))
        content = WorkArea(area.x, area.y, max(1, area.width - round(24 * scale)),
                           max(1, area.height - round(56 * scale)))
        x, y, width, height = fit_rect(area.x + (area.width - width) // 2,
                                     area.y + (area.height - height) // 2,
                                     width, height, content)
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        self.root.minsize(min(floor_w, content.width), min(floor_h, content.height))
        self.root.resizable(True, True)
        self.root.deiconify()
        self.root.update_idletasks()
        self._fit_density()
        for surface in self._cards:
            if surface._pending is not None:
                surface.after_cancel(surface._pending)
            surface._draw()
        self.root.update_idletasks()
        self._later(30, self._drain)
        self._later(1000, self._tick_clock)

    def _build(self):
        p = self.palette
        scale = ui_scale(self.root)
        pad = round(14 * scale)
        self._outer_pad = pad
        font_name = "Montserrat" if "Montserrat" in tkfont.families(self.root) else "Segoe UI"
        mono = "Consolas" if sys.platform == "win32" else "DejaVu Sans Mono"
        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)
        self._cards = []

        def card(row, *, stretch=False):
            surface = _GlassCard(self.root, p, stretch=stretch)
            surface.grid(row=row, column=0, sticky="nsew", padx=pad,
                         pady=(pad if row == 0 else 0, pad))
            self._cards.append(surface)
            return surface.body

        header = card(0)
        self._header = header
        header.columnconfigure(1, weight=1)
        self._chip_mark = _CircuitMark(header, p)
        self._chip_size = 48
        self._chip_mark.grid(row=0, column=0, rowspan=2, padx=(0, pad))
        tk.Label(header, text="MCU Flasher by Naph", bg=p["T_BG_MID"], fg=p["T_TEXT_BRIGHT"],
                 font=(font_name, 15, "bold"), anchor="w").grid(row=0, column=1, sticky="w")
        subtitle = tk.Label(header, text="Runtime setup · Prepare dependencies and board tools",
                            bg=p["T_BG_MID"], fg=p["T_TEXT_DIM"], font=(font_name, 9), anchor="w", justify="left")
        subtitle.grid(row=1, column=1, sticky="ew", pady=(round(4 * scale), 0))
        self._subtitle = subtitle
        self.clock = tk.Label(header, text="00:00", bg=p["T_BG_DARKEST"], fg=p["T_CYAN"],
                              font=(mono, 10, "bold"), padx=pad, pady=round(8 * scale))
        self.clock.grid(row=0, column=2, rowspan=2, padx=(pad, 0))
        header.bind("<Configure>", self._wrap_header, add="+")

        log_card = card(1, stretch=True)
        self._log_card = log_card
        log_card.rowconfigure(2, weight=1)
        log_card.columnconfigure(0, weight=1)
        activity_heading = tk.Frame(log_card, bg=p["T_BG_MID"])
        self._activity_heading = activity_heading
        activity_heading.grid(row=0, column=0, sticky="ew", pady=(0, round(8 * scale)))
        activity_heading.columnconfigure(0, weight=1)
        tk.Label(activity_heading, text="Setup activity", bg=p["T_BG_MID"], fg=p["T_TEXT_BRIGHT"],
                 font=(font_name, 10, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        self.details_button = tk.Button(activity_heading, text="Show technical details", command=self._toggle_details,
                                        bg=p["T_BG_MID"], fg=p["T_TEXT_DIM"], activebackground=p["T_BG_LIGHT"],
                                        activeforeground=p["T_TEXT_BRIGHT"], font=(font_name, 8), bd=0,
                                        highlightthickness=1, highlightbackground=p["T_BORDER"],
                                        highlightcolor=p["T_CYAN"], padx=round(8 * scale), pady=round(4 * scale),
                                        cursor="hand2", relief="flat")
        self.details_button.grid(row=0, column=1, sticky="e")
        self.package_panel = _PackageProgress(log_card, p, font_name)
        self.package_panel.grid(row=1, column=0, sticky="ew", pady=(0, round(6 * scale)))
        deck = tk.Frame(log_card, bg=p["T_BG_DARKEST"])
        self._reading_deck = deck
        deck.grid(row=2, column=0, sticky="nsew")
        deck.rowconfigure(0, weight=1)
        deck.columnconfigure(0, weight=1)
        # Both surfaces keep their native geometry. The disclosure raises the
        # existing raw view, retaining its exact text, marks and reading state.
        self._raw_view = tk.Frame(deck, bg=p["T_BG_DARKEST"])
        self._raw_view.grid(row=0, column=0, sticky="nsew")
        self._raw_view.rowconfigure(0, weight=1)
        self._raw_view.columnconfigure(0, weight=1)
        self.log_edit = tk.Text(self._raw_view, wrap="none", state="disabled", height=1, width=1,
                                bg=p["T_BG_DARKEST"], fg=p["T_TEXT"], insertbackground=p["T_TEXT"],
                                selectbackground=p["T_BG_LIGHT"], selectforeground=p["T_TEXT_BRIGHT"],
                                font=(mono, 9), bd=0, highlightthickness=0, padx=pad, pady=round(9 * scale), takefocus=0)
        self.log_edit.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self._raw_view, orient="vertical", command=self._scroll,
                                       style="Bootstrap.Vertical.TScrollbar")
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, round(5 * scale)), pady=round(8 * scale))
        horizontal = ttk.Scrollbar(self._raw_view, orient="horizontal", command=self.log_edit.xview,
                                   style="Bootstrap.Horizontal.TScrollbar")
        horizontal.grid(row=1, column=0, sticky="ew", padx=pad, pady=(0, round(5 * scale)))
        self._raw_horizontal = horizontal
        self.log_edit.configure(yscrollcommand=self.scrollbar.set, xscrollcommand=horizontal.set)
        self.scrollbar.bind("<ButtonPress-1>", self._press, add="+")
        self.scrollbar.bind("<ButtonRelease-1>", self._release, add="+")
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<KeyPress>"):
            self.log_edit.bind(sequence, lambda _event: self.root.after_idle(self._sync_scroll), add="+")
        self.log_edit.bind("<Configure>", lambda _event: self.root.after_idle(self._fit_reading), add="+")
        colors = {"normal": "T_TEXT", "dim": "T_TEXT_DIM", "section": "T_CYAN",
                  "subsection": "T_CYAN", "ok": "T_GREEN", "warn": "T_YELLOW",
                  "fail": "T_RED", "update": "T_MAGENTA", "pip_row": "T_CYAN"}
        for tag, color in colors.items():
            self.log_edit.tag_configure(tag, foreground=p[color],
                                       font=(mono, 9, "bold" if tag in ("section", "subsection", "ok", "fail") else "normal"))
        self.log_edit.tag_configure("failed_step", foreground=p["T_RED"], font=(mono, 9, "bold"))
        self.log_edit.tag_raise("failed_step")
        self.log_edit.mark_set("step_start", "1.0")
        self.log_edit.mark_gravity("step_start", "left")

        self._summary_view = tk.Frame(deck, bg=p["T_BG_DARKEST"])
        self._summary_view.grid(row=0, column=0, sticky="nsew")
        self._summary_view.rowconfigure(0, weight=1)
        self._summary_view.columnconfigure(0, weight=1)
        self.summary_edit = tk.Text(self._summary_view, wrap="word", state="disabled", height=1, width=1,
                                    bg=p["T_BG_DARKEST"], fg=p["T_TEXT"], insertbackground=p["T_TEXT"],
                                    selectbackground=p["T_BG_LIGHT"], selectforeground=p["T_TEXT_BRIGHT"],
                                    font=(font_name, 9), bd=0, highlightthickness=0, padx=pad,
                                    pady=round(10 * scale), spacing1=round(3 * scale), spacing3=round(4 * scale))
        self.summary_edit.grid(row=0, column=0, sticky="nsew")
        self.summary_scrollbar = ttk.Scrollbar(self._summary_view, orient="vertical", command=self._summary_scroll,
                                               style="Bootstrap.Vertical.TScrollbar")
        self.summary_scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, round(5 * scale)), pady=round(8 * scale))
        self.summary_edit.configure(yscrollcommand=self.summary_scrollbar.set)
        self.summary_scrollbar.bind("<ButtonPress-1>", self._press, add="+")
        self.summary_scrollbar.bind("<ButtonRelease-1>", self._release, add="+")
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<KeyPress>"):
            self.summary_edit.bind(sequence, lambda _event: self.root.after_idle(self._sync_summary_scroll), add="+")
        self.summary_edit.bind("<Configure>", lambda _event: self.root.after_idle(self._fit_reading), add="+")
        for tag, color in colors.items():
            self.summary_edit.tag_configure(tag, foreground=p[color],
                                             font=(font_name, 11 if tag == "section" else 10 if tag == "subsection" else 9,
                                                   "bold" if tag in ("section", "subsection", "ok", "fail") else "normal"))
        self.summary_edit.tag_configure("failed_step", foreground=p["T_RED"])
        self.summary_edit.tag_raise("failed_step")
        self.summary_edit.mark_set("summary_step_start", "1.0")
        self.summary_edit.mark_gravity("summary_step_start", "left")
        self._summary_view.tkraise()

        footer = card(2)
        self._footer = footer
        footer.columnconfigure(1, weight=1)
        self.spinner = tk.Label(footer, text="●", bg=p["T_BG_MID"], fg=p["T_CYAN"], font=(mono, 9))
        self.spinner.grid(row=0, column=0, padx=(0, round(8 * scale)), pady=(0, round(8 * scale)))
        self.status = tk.Label(footer, text=self.gui._status_text, bg=p["T_BG_MID"], fg=p["T_TEXT_BRIGHT"],
                               font=(font_name, 9, "bold"), anchor="w", justify="left")
        self.status.grid(row=0, column=1, sticky="ew", pady=(0, round(8 * scale)))
        self.percent = tk.Label(footer, text="0%", bg=p["T_BG_MID"], fg=p["T_CYAN"], font=(font_name, 9))
        self.percent.grid(row=0, column=2, padx=(pad, 0), pady=(0, round(8 * scale)))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        for name in ("Bootstrap.Vertical.TScrollbar", "Bootstrap.Horizontal.TScrollbar"):
            style.configure(name, background=p["T_BG_LIGHT"], troughcolor=p["T_BG_DARKEST"],
                            bordercolor=p["T_BORDER"], arrowcolor=p["T_TEXT_DIM"],
                            lightcolor=p["T_BG_LIGHT"], darkcolor=p["T_BG_LIGHT"])
            style.map(name, background=[("active", p["T_BG_HOVER"])])
        self.progress = _LinearProgress(footer, p)
        self.progress.grid(row=1, column=0, columnspan=3, sticky="ew")
        options = tk.Frame(footer, bg=p["T_BG_MID"])
        self._options = options
        options.grid(row=2, column=0, columnspan=3, sticky="w", pady=(round(8 * scale), 0))
        self.skip_var = tk.BooleanVar(self.root, self.gui._skip_updates)
        self.auto_var = tk.BooleanVar(self.root, True)
        for label, variable, command in (("Skip Updates", self.skip_var, self._skip),
                                          ("Auto-Scroll", self.auto_var, self._auto)):
            tk.Checkbutton(options, text=label, variable=variable, command=command,
                           bg=p["T_BG_MID"], fg=p["T_TEXT"], activebackground=p["T_BG_MID"],
                           activeforeground=p["T_TEXT_BRIGHT"], selectcolor=p["T_BG_DARKEST"],
                           font=(font_name, 9), cursor="hand2", bd=0).pack(side="left", padx=(0, round(10 * scale)))
        footer.bind("<Configure>", self._wrap_footer, add="+")
        log_card.bind("<Configure>", self._fit_package_budget, add="+")
        self._on_status(self.gui._status_text)

    def _schedule_layout(self, event):
        if event.widget is self.root and not self._layout_pending:
            self._layout_pending = True
            self._later(35, self._fit_density)

    def _wrap_header(self, event=None):
        width = event.width if event is not None else self._header.winfo_width()
        scale = ui_scale(self.root)
        self._subtitle.configure(wraplength=max(80, width - self.clock.winfo_reqwidth()
                                                - round(self._chip_size * scale) - self._outer_pad * 3))

    def _wrap_footer(self, event=None):
        width = event.width if event is not None else self._footer.winfo_width()
        scale = ui_scale(self.root)
        wrap = max(80, width - self.percent.winfo_reqwidth() - self.spinner.winfo_reqwidth()
                   - self._outer_pad - round(8 * scale))
        self.status.configure(wraplength=wrap)
        # Very short windows keep a concise two-line caption. The original
        # pipeline status remains available to promotion and raw diagnostics.
        limit = 100 if self._density == "tight" else 180
        self.status.configure(text=concise_status(getattr(self, "_status_detail", self.gui._status_text), limit))

    def _fit_density(self):
        self._layout_pending = False
        if self.closed:
            return
        scale = ui_scale(self.root)
        height = self.root.winfo_height() / scale
        density = "tight" if height < 340 else "compact" if height < 520 else "normal"
        if density != self._density:
            self._density = density
            tight, compact = density == "tight", density != "normal"
            self._outer_pad = round((6 if tight else 8 if compact else 14) * scale)
            inset = round((6 if tight else 10 if compact else 14) * scale)
            gap = round((4 if compact else 8) * scale)
            for row, surface in enumerate(self._cards):
                surface.set_padding(inset)
                surface.grid_configure(padx=self._outer_pad,
                                       pady=(self._outer_pad if row == 0 else 0, self._outer_pad))
            chip_size = 32 if tight else 48
            if chip_size != self._chip_size:
                ratio = chip_size / self._chip_size
                self._chip_mark.scale("all", 0, 0, ratio, ratio)
                self._chip_mark.configure(width=round(chip_size * scale), height=round(chip_size * scale))
                self._chip_size = chip_size
            self._chip_mark.grid_configure(padx=(0, self._outer_pad))
            if tight:
                self._subtitle.grid_remove()
            else:
                self._subtitle.grid()
            self.clock.configure(padx=self._outer_pad, pady=round((4 if compact else 8) * scale))
            self.clock.grid_configure(padx=(self._outer_pad, 0))
            self.details_button.configure(pady=round((2 if compact else 4) * scale))
            self._activity_heading.grid_configure(pady=(0, gap))
            self.package_panel.grid_configure(pady=(0, round((2 if compact else 6) * scale)))
            self.spinner.grid_configure(pady=(0, gap))
            self.status.grid_configure(pady=(0, gap))
            self.percent.grid_configure(padx=(self._outer_pad, 0), pady=(0, gap))
            self._options.grid_configure(pady=(gap, 0))
            for text in (self.log_edit, self.summary_edit):
                text.configure(padx=self._outer_pad, pady=round((4 if compact else 9 if text is self.log_edit else 10) * scale))
            self.summary_edit.configure(spacing1=round((1 if compact else 3) * scale),
                                        spacing3=round((1 if compact else 4) * scale))
            self._raw_horizontal.grid_configure(padx=self._outer_pad,
                                                 pady=(0, round((2 if compact else 5) * scale)))
            for scrollbar in (self.scrollbar, self.summary_scrollbar):
                scrollbar.grid_configure(pady=round((4 if compact else 8) * scale))
        self._wrap_header()
        self._wrap_footer()
        self._fit_package_budget()

    def _fit_package_budget(self, _event=None):
        if self.closed or not hasattr(self, "_raw_horizontal"):
            return
        scale = ui_scale(self.root)
        def reserve(text):
            font = tkfont.Font(root=self.root, font=text.cget("font"))
            spacing = text.winfo_pixels(text.cget("spacing1")) + text.winfo_pixels(text.cget("spacing3"))
            lines = 2 * (font.metrics("linespace") + spacing) + text.winfo_pixels(text.cget("pady")) * 2
            return max(lines, round(40 * scale))
        raw = reserve(self.log_edit) + self._raw_horizontal.winfo_reqheight()
        raw += round((2 if self._density != "normal" else 5) * scale)
        required = max(reserve(self.summary_edit), raw, round(40 * scale))
        self._log_card.rowconfigure(2, minsize=required)
        heading_gap = round((4 if self._density != "normal" else 8) * scale)
        package_gap = round((2 if self._density != "normal" else 6) * scale)
        available = self._log_card.winfo_height() - self._activity_heading.winfo_reqheight()
        self.package_panel.set_height_budget(available - heading_gap - package_gap - required)

    def _later(self, milliseconds, callback):
        if self.closed:
            return
        holder = []
        def run():
            self._timers.discard(holder[0])
            if not self.closed:
                callback()
        holder.append(self.root.after(milliseconds, run))
        self._timers.add(holder[0])

    def _drain(self):
        self.signals.drain()
        self._later(30, self._drain)

    def dispatch(self, name, args):
        if self.closed:
            return
        try:
            if name == "call":
                args[0](*args[1])
            elif name == "hide":
                self.root.withdraw()
            elif name == "close":
                self._on_close()
            else:
                getattr(self, "_on_" + name)(*args)
        except Exception as error:
            self.record_exception(f"Bootstrap native callback error: {error}")

    def _tick_clock(self):
        seconds = max(0, int(time.time() - self.gui._start_time))
        hours, rest = divmod(seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        self.clock.configure(text=(f"{hours:02d}:" if hours else "") + f"{minutes:02d}:{seconds:02d}")
        self._later(1000, self._tick_clock)

    def _tick_spinner(self):
        # A static circuit status light avoids a perpetual cold-start animation.
        if self.gui._spinning:
            self.spinner.configure(text="●", fg=self.palette["T_CYAN"])

    def _toggle_details(self):
        self._set_details_expanded(not self._details_expanded)

    def _set_details_expanded(self, expanded):
        self._details_expanded = bool(expanded)
        view = self._raw_view if self._details_expanded else self._summary_view
        view.tkraise()
        self.log_edit.configure(takefocus=int(self._details_expanded))
        self.summary_edit.configure(takefocus=int(not self._details_expanded))
        self.details_button.configure(text="Hide technical details" if self._details_expanded else "Show technical details")

    def _press(self, _event=None):
        self.held = True

    def _release(self, _event=None):
        self.held = False
        self.root.after_idle(self._resume)

    def _scroll(self, *args):
        self.log_edit.yview(*args)
        self._sync_scroll()

    def _sync_scroll(self):
        if not self.closed:
            self.user_scrolled_up = self.log_edit.yview()[1] < .999

    def _summary_scroll(self, *args):
        self.summary_edit.yview(*args)
        self._sync_summary_scroll()

    def _sync_summary_scroll(self):
        if not self.closed:
            self.summary_user_scrolled_up = self.summary_edit.yview()[1] < .999

    def _fit_reading(self):
        # Package rows may change the viewport height after a log mutation or
        # scrollbar release. Keep only views already following at the bottom.
        if not self.closed and self.auto_var.get() and not self.held:
            if not self.user_scrolled_up:
                self.log_edit.see("end")
            if not self.summary_user_scrolled_up:
                self.summary_edit.see("end")

    def _resume(self):
        if not self.closed and self.auto_var.get() and not self.held:
            self.user_scrolled_up = False
            self.log_edit.see("end")
            self.summary_user_scrolled_up = False
            self.summary_edit.see("end")

    def _auto(self):
        if self.auto_var.get():
            self._resume()
        else:
            self._sync_scroll()
            self._sync_summary_scroll()

    def _append_summaries(self, events):
        if not events:
            return
        text = self.summary_edit
        empty = text.compare("end-1c", "==", "1.0")
        following = (self.auto_var.get() and not self.held and not self.summary_user_scrolled_up
                     and (empty or text.yview()[1] >= .999))
        text.mark_set("view_anchor", text.index("@0,0"))
        text.mark_gravity("view_anchor", "left" if empty else "right")
        text.configure(state="normal")
        for event in events:
            if event.tag == "section":
                if not text.compare("end-1c", "==", "1.0") and text.get("end-2c linestart", "end-2c lineend"):
                    text.insert("end-1c", "\n", "dim")
                text.mark_set("summary_step_start", "end-1c")
                self._summary_step_failed = False
            if event.tag == "fail":
                self._summary_step_failed = True
                text.tag_add("failed_step", "summary_step_start", "end-1c")
            tags = (event.tag, "failed_step") if self._summary_step_failed else event.tag
            prefix = "" if event.tag in ("section", "subsection") else "  "
            text.insert("end-1c", prefix + event.text + "\n", tags)
        count = text.count("1.0", "end-1c", "chars")
        excess = (count[0] if count else 0) - 128000
        if excess > 0:
            text.delete("1.0", f"1.0+{excess}c")
        lines = int(text.index("end-1c").split(".")[0])
        if lines > 700:
            text.delete("1.0", f"{lines - 700 + 1}.0")
        text.configure(state="disabled")
        if following and not self.held:
            text.see("end")
        else:
            text.yview("view_anchor")

    def _skip(self):
        previous = self.gui._skip_updates
        try:
            config = self.load_config()
            config["skip_updates"] = self.skip_var.get()
            if not self.save_config(config):
                raise OSError("Configuration is not writable")
            self.gui._skip_updates = self.skip_var.get()
        except (OSError, TypeError, ValueError) as error:
            self.skip_var.set(previous)
            self._on_log(f"Skip Updates was not saved: {error}. Check folder permissions and try again.", "warn")

    @contextmanager
    def _update(self):
        text = self.log_edit
        empty = text.compare("end-1c", "==", "1.0")
        following = (self.auto_var.get() and not self.held and not self.user_scrolled_up
                     and (empty or text.yview()[1] >= .999))
        text.mark_set("view_anchor", text.index("@0,0"))
        text.mark_gravity("view_anchor", "left" if empty else "right")
        horizontal = text.xview()[0]
        text.configure(state="normal")
        try:
            yield
        finally:
            count = text.count("1.0", "end-1c", "chars")
            excess = (count[0] if count else 0) - self.DISPLAY_CHARS
            if excess > 0:
                text.delete("1.0", f"1.0+{excess}c")
            lines = int(text.index("end-1c").split(".")[0])
            if lines > self.DISPLAY_LINES:
                text.delete("1.0", f"{lines - self.DISPLAY_LINES + 1}.0")
            text.configure(state="disabled")
            if following and not self.held:
                text.see("end")
            else:
                text.yview("view_anchor")
            text.xview_moveto(horizontal)

    def _on_log(self, message, tag):
        text = self.log_edit
        with self._update():
            position = text.index("live_start") if self._live_type else text.index("end-1c")
            if tag == "section":
                text.mark_set("step_start", position)
                self._step_failed = False
            if tag == "fail":
                self._step_failed = True
            text.insert(position, message[:8192] + "\n", tag)
            if self._step_failed:
                text.tag_add("failed_step", "step_start", "end-1c")
        if tag == "section":
            self.package_panel.set_rows(self._presentation.clear_block())
            self._summary_step_failed = False
            self.summary_edit.mark_set("summary_step_start", "end-1c")
        self._append_summaries(self._presentation.summaries(message, tag))

    def _on_update_block(self, block_type, table):
        if not table.strip():
            return
        text = self.log_edit
        with self._update():
            if self._live_type and self._live_type != block_type:
                self._live_type = None
            if self._live_type:
                position = text.index("live_start")
                text.delete("live_start", "live_end")
            else:
                position = text.index("end-1c")
            text.insert(position, table[:65536], "failed_step" if self._step_failed else "pip_row")
            text.mark_set("live_start", position)
            text.mark_gravity("live_start", "right")
            text.mark_set("live_end", f"{position}+{len(table[:65536])}c")
            self._live_type = block_type
        self.package_panel.set_rows(self._presentation.update_block(block_type, table))

    def _on_commit_block(self):
        with self._update():
            self._live_type = None
            self.log_edit.insert("end-1c", "\n")
        self.package_panel.set_rows(self._presentation.commit_block())

    def _on_clear_block(self):
        with self._update():
            if self._live_type:
                self.log_edit.delete("live_start", "live_end")
            self._live_type = None
        self.package_panel.set_rows(self._presentation.clear_block())

    def _on_status(self, text):
        self._status_detail = text
        if hasattr(self, "_outer_pad"):
            self._wrap_footer()
        else:
            self.status.configure(text=concise_status(text))

    def _on_progress(self, value):
        value = max(0, min(100, round(value)))
        self.progress.configure(value=value)
        self.percent.configure(text=f"{value}%")

    def _on_stop_spinner(self, text, ok):
        self.spinner.configure(text="✔" if ok else "✖", fg=self.palette["T_GREEN" if ok else "T_RED"])
        self._on_status(text)

    def _request_close(self):
        if self.gui._closed:
            self._on_close()
        else:
            messagebox.showwarning("Setup in Progress", "The setup process is running. Please wait for it to complete.", parent=self.root)

    def snapshot(self):
        """Capture tagged display and reading position without reading a log file."""
        text = self.log_edit
        raw = text.get("1.0", "end-1c")
        tags, runs = [], []
        for kind, value, _index in text.dump("1.0", "end-1c", text=True, tag=True):
            if kind == "tagon" and value not in tags:
                tags.append(value)
            elif kind == "tagoff" and value in tags:
                tags.remove(value)
            elif kind == "text":
                runs.append({"text": value, "tags": list(tags)})

        def offset(index):
            return len(text.get("1.0", index))

        selected = text.tag_ranges("sel")
        first, last = text.xview()
        horizontal = first / max(.000001, 1 - (last - first))
        def reading_snapshot(widget, scrolled):
            content = widget.get("1.0", "end-1c")
            current_tags, styled = [], []
            for kind, value, _index in widget.dump("1.0", "end-1c", text=True, tag=True):
                if kind == "tagon" and value not in current_tags:
                    current_tags.append(value)
                elif kind == "tagoff" and value in current_tags:
                    current_tags.remove(value)
                elif kind == "text":
                    styled.append({"text": value, "tags": list(current_tags)})
            ranges = widget.tag_ranges("sel")
            begin, finish = widget.xview()
            return {"text": content, "runs": styled,
                    "selection": [len(widget.get("1.0", index)) for index in ranges] if ranges else [],
                    "top_offset": len(widget.get("1.0", widget.index("@0,0"))),
                    "horizontal_fraction": max(0.0, min(1.0, begin / max(.000001, 1 - (finish - begin)))),
                    "auto_scroll": self.auto_var.get(), "user_scrolled_up": scrolled, "held": self.held,
                    "step_start": len(widget.get("1.0", "summary_step_start")),
                    "step_failed": self._summary_step_failed}

        return {"text": raw, "runs": runs, "step_start": offset("step_start"),
                "step_failed": self._step_failed, "live_type": self._live_type,
                "live_start": offset("live_start") if self._live_type else 0,
                "live_end": offset("live_end") if self._live_type else 0,
                "selection": [offset(selected[0]), offset(selected[1])] if selected else [],
                "auto_scroll": self.auto_var.get(), "user_scrolled_up": self.user_scrolled_up,
                "held": self.held, "top_offset": offset(text.index("@0,0")),
                "horizontal_fraction": max(0.0, min(1.0, horizontal)),
                "summary": reading_snapshot(self.summary_edit, self.summary_user_scrolled_up),
                "presentation": self._presentation.snapshot(), "details_expanded": self._details_expanded}

    def _on_close(self):
        self.signals.close()
        self.retire()

    def retire(self):
        """Close this Tk view while retaining its dispatcher and installer state."""
        if not self.closed:
            self.closed = True
            for timer in self._timers:
                self.root.after_cancel(timer)
            self._timers.clear()
            self.root.quit()
            self.root.destroy()

    def pump(self):
        if not self.closed and threading.get_ident() == self.thread_id:
            self.root.update()

    def mainloop(self):
        if not self.closed:
            self.root.mainloop()


__all__ = ["NativeBootstrapWindow", "NativeSignals"]
