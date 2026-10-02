"""Static glass cards for the Tk downloader; no blur or animation loop."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from src.modules.ui_palette import mix, readable_foreground


def ui_scale(widget):
    """Tk distances are native pixels; fonts already follow Tk point scaling."""
    return max(.75, min(3.0, widget.winfo_fpixels("1i") / 96.0))


class ResponsivePanes:
    """Reflow list/details once per resize burst and stop on destruction."""

    def __init__(self, pane, left, right):
        self.pane, self.left, self.right = pane, left, right
        self._pending = None
        self._vertical = None
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
        if vertical == self._vertical:
            return
        self._vertical = vertical
        pane.configure(orient=tk.VERTICAL if vertical else tk.HORIZONTAL)
        pane.paneconfigure(self.left, minsize=round((70 if vertical else 180) * scale))
        pane.paneconfigure(self.right, minsize=round((100 if vertical else 300) * scale))
        pane.sash_place(0, round(pane.winfo_width() * .35), round(pane.winfo_height() * .35))


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


class GlassCard(tk.Canvas):
    """A bordered gradient around an opaque content frame, redrawn on resize."""

    def __init__(self, parent, palette, padding: int = 10):
        super().__init__(parent, bg=palette.BG_DARKEST, height=1, bd=0, highlightthickness=0)
        self.palette = palette
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
        height = self.body.winfo_reqheight() + 2 * self.padding
        width = self.winfo_width()
        if self.winfo_pixels(self.cget("height")) != height:
            self.configure(height=height)
        self.itemconfigure(self._body_window, width=max(1, width - 2 * self.padding))
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
