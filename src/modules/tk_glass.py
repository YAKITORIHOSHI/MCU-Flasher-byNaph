"""Static glass cards for the Tk downloader; no blur or animation loop."""
from __future__ import annotations

import tkinter as tk
import sys
from tkinter import ttk
from src.modules.ui_palette import mix, readable_foreground
from src.modules.ui_metrics import WorkArea, fit_rect


def ui_scale(widget):
    """Tk distances are native pixels; fonts already follow Tk point scaling."""
    return max(.75, min(3.0, widget.winfo_fpixels("1i") / 96.0))


def native_work_area(widget):
    """Current window's monitor work area in the same native units as Tk."""
    area = WorkArea(widget.winfo_vrootx(), widget.winfo_vrooty(),
                    widget.winfo_vrootwidth(), widget.winfo_vrootheight())
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
            monitor = user32.MonitorFromWindow(widget.winfo_toplevel().winfo_id(), 2)
            if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                rect = info.rcWork
                area = WorkArea(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        except (AttributeError, OSError, ValueError, tk.TclError):
            pass
    return area


def _native_frame(widget):
    """Measure native decoration separately from content pixels."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            handle = int(widget.tk.call("wm", "frame", widget._w), 0)
            rect = wintypes.RECT()
            if user32.GetWindowRect(handle, ctypes.byref(rect)):
                return WorkArea(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top), handle
        except (AttributeError, OSError, ValueError, tk.TclError):
            pass
    return WorkArea(widget.winfo_rootx(), widget.winfo_rooty(), widget.winfo_width(), widget.winfo_height()), None


class DialogFit:
    """Fit complete native frames on open and coalesced window/monitor moves."""

    def __init__(self, dialog, reference=None, preferred=(620, 475), minimum=(320, 240)):
        self.dialog = dialog
        self._pending = None
        self._fitting = False
        self._minimum = minimum
        self._scale = ui_scale(dialog)
        area = native_work_area(reference or dialog)
        width = min(round(preferred[0] * self._scale), max(1, area.width - 32))
        height = min(round(preferred[1] * self._scale), max(1, area.height - 64))
        x, y = area.x + (area.width - width) // 2, area.y + (area.height - height) // 2
        dialog.geometry(f"{width}x{height}{x:+d}{y:+d}")
        dialog.bind("<Configure>", self._schedule, add="+")
        dialog.bind("<Destroy>", self._cancel, add="+")
        dialog.update_idletasks()
        self._fit(area)

    def _schedule(self, event):
        if event.widget is self.dialog and not self._fitting and self._pending is None:
            self._pending = self.dialog.after(60, self._fit)

    def _cancel(self, event):
        if event.widget is self.dialog and self._pending is not None:
            self.dialog.after_cancel(self._pending)
            self._pending = None

    def _fit(self, area=None):
        if self._pending is not None:
            self.dialog.after_cancel(self._pending)
        self._pending = None
        if not self.dialog.winfo_exists():
            return
        self._fitting = True
        try:
            dialog = self.dialog
            area = area or native_work_area(dialog)
            frame, handle = _native_frame(dialog)
            extra_width = max(0, frame.width - dialog.winfo_width())
            extra_height = max(0, frame.height - dialog.winfo_height())
            # X11 fallback reserves conventional native frame clearance.
            if handle is None:
                extra_height = max(extra_height, 40)
            maximum_width = max(1, area.width - extra_width - 16)
            maximum_height = max(1, area.height - extra_height - 16)
            minimum_width = min(round(self._minimum[0] * self._scale), maximum_width)
            minimum_height = min(round(self._minimum[1] * self._scale), maximum_height)
            dialog.minsize(minimum_width, minimum_height)
            width = max(minimum_width, min(dialog.winfo_width(), maximum_width))
            height = max(minimum_height, min(dialog.winfo_height(), maximum_height))
            x, y, _, _ = fit_rect(frame.x, frame.y, width + extra_width, height + extra_height, area, 8)
            if (width, height) != (dialog.winfo_width(), dialog.winfo_height()):
                dialog.geometry(f"{width}x{height}")
                dialog.update_idletasks()
            if handle is not None:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_int, wintypes.UINT]
                # Native positioning handles negative monitor coordinates as
                # absolute coordinates, unlike Tk's screen-edge offsets.
                user32.SetWindowPos(handle, None, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)
            elif (x, y) != (frame.x, frame.y):
                dialog.geometry(f"{width}x{height}{x:+d}{y:+d}")
        finally:
            self._fitting = False


class ResponsivePanes:
    """Reflow list/details once per resize burst and stop on destruction."""

    def __init__(self, pane, left, right):
        self.pane, self.left, self.right = pane, left, right
        self._pending = None
        self._vertical = None
        self._minimum_sizes = None
        pane.bind("<Configure>", self._schedule, add="+")
        pane.bind("<Destroy>", self._cancel, add="+")

    def _schedule(self, event):
        if event.widget is self.pane and self._pending is None:
            self._pending = self.pane.after(60, self._fit)

    def _cancel(self, event):
        if event.widget is self.pane and self._pending is not None:
            self.pane.after_cancel(self._pending)
            self._pending = None

    def _fit(self):
        self._pending = None
        pane = self.pane
        scale = ui_scale(pane)
        vertical = pane.winfo_width() < round(660 * scale)
        changed = vertical != self._vertical
        self._vertical = vertical
        if changed:
            pane.configure(orient=tk.VERTICAL if vertical else tk.HORIZONTAL)
        extent = pane.winfo_height() if vertical else pane.winfo_width()
        available = max(2, extent - pane.winfo_pixels(pane.cget("sashwidth"))
                        - 2 * pane.winfo_pixels(pane.cget("borderwidth")))
        left = round((70 if vertical else 180) * scale)
        right = round((100 if vertical else 300) * scale)
        if left + right > available:
            # Fixed scaled minima can consume every pixel before the details
            # pane is mapped on a short monitor. Both scrollable panes share
            # the actual space; preserve their widgets, fonts and selection.
            left = max(1, min(available - 1, round(available * left / (left + right))))
            right = available - left
        minima = (left, right)
        if minima != self._minimum_sizes:
            pane.paneconfigure(self.left, minsize=left)
            pane.paneconfigure(self.right, minsize=right)
            self._minimum_sizes = minima
        position = round(available * .35) if changed else pane.sash_coord(0)[1 if vertical else 0]
        fitted = max(left, min(position, available - right))
        if changed or fitted != position:
            pane.sash_place(0, 0 if vertical else fitted, fitted if vertical else 0)


class ScrollBody(tk.Frame):
    """Keep installed-item metadata reachable when the detail pane is short."""

    def __init__(self, parent, background):
        super().__init__(parent, bg=background)
        self.canvas = tk.Canvas(self, bg=background, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.body = tk.Frame(self.canvas, bg=background)
        self._window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self._window, width=event.width))
        self.body.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._toplevel = self.winfo_toplevel()
        self._bindings = [(sequence, self._toplevel.bind(sequence, self._wheel, add="+"))
                          for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>")]
        self.bind("<Destroy>", self._detach, add="+")

    def _wheel(self, event):
        widget = event.widget
        if isinstance(widget, (tk.Listbox, tk.Text, tk.Entry, ttk.Combobox)):
            return
        while widget is not None and widget is not self:
            widget = getattr(widget, "master", None)
        if widget is self:
            steps = (-1 if event.num == 4 else 1) if not event.delta else -int(event.delta / 120)
            if event.delta and not steps:
                steps = -1 if event.delta > 0 else 1
            self.canvas.yview_scroll(steps, "units")

    def _detach(self, event):
        if event.widget is self:
            for sequence, callback in self._bindings:
                self._toplevel.unbind(sequence, callback)
            self._bindings.clear()


class ScrollForm(ScrollBody):
    """Scrollable utility form which reveals fields reached with Tab."""

    def __init__(self, parent, background):
        super().__init__(parent, background)
        self._focus_binding = self._toplevel.bind("<FocusIn>", self._focus_in, add="+")
        self.bind("<Destroy>", self._detach_focus, add="+")

    def _focus_in(self, event):
        widget = event.widget
        owner = widget
        while owner is not None and owner is not self.body:
            owner = getattr(owner, "master", None)
        if owner is self.body:
            self.ensure_visible(widget)

    def ensure_visible(self, widget):
        self.update_idletasks()
        top = widget.winfo_rooty() - self.body.winfo_rooty()
        height = widget.winfo_height()
        current = self.canvas.canvasy(0)
        viewport = self.canvas.winfo_height()
        target = current
        if top < current:
            target = top
        elif top + height > current + viewport:
            target = top if height >= viewport else top + height - viewport
        if target != current:
            total = max(1, self.body.winfo_height())
            self.canvas.yview_moveto(max(0, target) / total)

    def _detach_focus(self, event):
        if event.widget is self and self._focus_binding:
            self._toplevel.unbind("<FocusIn>", self._focus_binding)
            self._focus_binding = None


class GlassCard(tk.Canvas):
    """A bordered gradient around an opaque content frame, redrawn on resize."""

    def __init__(self, parent, palette, padding: int = 10, *, expand=False):
        super().__init__(parent, bg=palette.BG_DARKEST, height=1, bd=0, highlightthickness=0)
        self.palette = palette
        self.expand = expand
        self.padding = round(padding * ui_scale(self))
        self._redraw_id = None
        self.body = tk.Frame(self, bg=palette.BG_MID)
        self._body_window = self.create_window(self.padding, self.padding, window=self.body, anchor="nw")
        self.bind("<Configure>", self._schedule)
        self.body.bind("<Configure>", self._schedule)
        self.bind("<Destroy>", self._cancel, add="+")

    def _schedule(self, _event=None):
        if self._redraw_id is None:
            self._redraw_id = self.after(40, self._draw)

    def _cancel(self, event):
        if event.widget is self and self._redraw_id is not None:
            self.after_cancel(self._redraw_id)
            self._redraw_id = None

    def _draw(self):
        self._redraw_id = None
        p = self.palette
        height = self.winfo_height() if self.expand else self.body.winfo_reqheight() + 2 * self.padding
        width = self.winfo_width()
        if not self.expand and self.winfo_pixels(self.cget("height")) != height:
            self.configure(height=height)
        self.itemconfigure(self._body_window, width=max(1, width - 2 * self.padding))
        if self.expand:
            self.itemconfigure(self._body_window, height=max(1, height - 2 * self.padding))
        self.delete("glass")
        radius = min(12, height // 2)
        points = [1 + radius, 1, width - radius - 1, 1, width - 1, 1,
                  width - 1, 1 + radius, width - 1, height - radius - 1,
                  width - 1, height - 1, width - radius - 1, height - 1,
                  radius + 1, height - 1, 1, height - 1, 1, height - radius - 1,
                  1, radius + 1, 1, 1]
        self.create_polygon(points, smooth=True, fill=p.BG_MID, outline=p.BORDER, tags="glass")
        # A small rim suggests frosted glass without obscuring list text.
        self.create_line(radius, 2, width - radius, 2,
                         fill=mix(p.BORDER, p.TEXT_BRIGHT, .28), tags="glass")
        for step in range(6):
            y = 3 + step
            self.create_line(radius, y, width - radius, y,
                             fill=mix(p.BG_MID, p.BG_LIGHT, (6 - step) / 9), tags="glass")
        self.tag_lower("glass", self._body_window)
