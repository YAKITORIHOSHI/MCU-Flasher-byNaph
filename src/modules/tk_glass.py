"""Static glass cards for the Tk downloader; no blur or animation loop."""
from __future__ import annotations

import tkinter as tk
import sys
import math
from tkinter import font as tkfont
from tkinter import ttk
from src.modules.ui_palette import mix, readable_foreground
from src.modules.ui_metrics import WorkArea, fit_rect


def ui_scale(widget):
    """Tk distances are native pixels; fonts already follow Tk point scaling."""
    return max(.75, min(3.0, widget.winfo_fpixels("1i") / 96.0))


def _desktop_work_area(fallback, values, desktop=None):
    """Validate EWMH rectangles and intersect them with Tk's native bounds."""
    if not values or len(values) % 4:
        return fallback
    if desktop is None:
        if len(values) != 4:
            return fallback
        desktop = 0
    if not isinstance(desktop, int) or desktop < 0 or desktop >= len(values) // 4:
        return fallback
    x, y, width, height = values[desktop * 4:desktop * 4 + 4]
    if width <= 0 or height <= 0:
        return fallback
    # X11 CARDINALs carry signed origins in their low 32 bits.
    x = x - 2**32 if x >= 2**31 else x
    y = y - 2**32 if y >= 2**31 else y
    left, top = max(fallback.x, x), max(fallback.y, y)
    right = min(fallback.x + fallback.width, x + width)
    bottom = min(fallback.y + fallback.height, y + height)
    return WorkArea(left, top, right - left, bottom - top) if right > left and bottom > top else fallback


def _x11_work_area(widget, fallback):
    """Read bounded desktop work-area hints through native X11, without probes."""
    import ctypes
    display = None
    try:
        x11 = ctypes.CDLL("libX11.so.6")
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p)]
        x11.XFree.argtypes = [ctypes.c_void_p]
        display = x11.XOpenDisplay(widget.winfo_screen().encode("utf-8"))
        if not display:
            return fallback
        root = x11.XDefaultRootWindow(display)

        def property_values(name, limit):
            atom = x11.XInternAtom(display, name, 1)
            if not atom:
                return None
            actual, count, remaining = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
            format_bits, data = ctypes.c_int(), ctypes.c_void_p()
            try:
                status = x11.XGetWindowProperty(display, root, atom, 0, limit, 0, 0,
                    ctypes.byref(actual), ctypes.byref(format_bits), ctypes.byref(count),
                    ctypes.byref(remaining), ctypes.byref(data))
                if status or format_bits.value != 32 or remaining.value or count.value > limit or not data.value:
                    return None
                pointer = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))
                return [int(pointer[index]) for index in range(count.value)]
            finally:
                if data.value:
                    x11.XFree(data)

        desktops = property_values(b"_NET_CURRENT_DESKTOP", 1)
        values = property_values(b"_NET_WORKAREA", 64)
        return _desktop_work_area(fallback, values, desktops[0] if desktops else None)
    except (AttributeError, OSError, ValueError, tk.TclError):
        return fallback
    finally:
        if display:
            x11.XCloseDisplay(display)


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
    elif sys.platform.startswith("linux"):
        area = _x11_work_area(widget, area)
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


def _x11_focus_unset(widget):
    """Force restoration only if X11 has no focused client, never another app."""
    import ctypes
    display = None
    try:
        x11 = ctypes.CDLL("libX11.so.6")
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XGetInputFocus.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int)]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        display = x11.XOpenDisplay(widget.winfo_screen().encode("utf-8"))
        if not display:
            return False
        window, revert = ctypes.c_ulong(), ctypes.c_int()
        x11.XGetInputFocus(display, ctypes.byref(window), ctypes.byref(revert))
        return window.value in (0, 1)  # X11 None / PointerRoot, no other client.
    except (AttributeError, OSError, tk.TclError):
        return False
    finally:
        if display:
            x11.XCloseDisplay(display)


class NativeResizeFocus:
    """Retain a downloader control across Linux's native resize focus reset."""

    def __init__(self, root):
        self.root = root
        self._focused = None
        self._pending = None
        root.bind("<FocusIn>", self._remember, add="+")
        root.bind("<Configure>", self._schedule, add="+")
        root.bind("<Destroy>", self._cancel, add="+")

    def _remember(self, event):
        if event.widget is not self.root:
            self._focused = self.root.focus_get()

    def _schedule(self, event):
        if event.widget is self.root and self._pending is None:
            self._pending = self.root.after(60, self._restore)

    def _cancel(self, event):
        if event.widget is self.root and self._pending is not None:
            self.root.after_cancel(self._pending)
            self._pending = None

    def _restore(self):
        self._pending = None
        focused = self._focused
        try:
            if (focused is not None and focused.winfo_exists() and focused.winfo_viewable()
                    and self.root.focus_get() is None and _x11_focus_unset(self.root)):
                focused.focus_force()
        except tk.TclError:
            pass


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


class FieldRow:
    """Keep a native entry usable by moving it below its label and action."""

    def __init__(self, parent, label, field, action, minimum=150):
        self.parent, self.label, self.field, self.action = parent, label, field, action
        self.minimum = minimum
        self._pending = None
        self._stacked = None
        for widget in (label, field, action):
            widget.pack_forget()
        action.bind("<Configure>", self._schedule, add="+")
        parent.bind("<Configure>", self._schedule, add="+")
        parent.bind("<Destroy>", self._cancel, add="+")
        self._schedule()

    def _schedule(self, event=None):
        if (event is None or event.widget in (self.parent, self.action)) and self._pending is None:
            self._pending = self.parent.after(40, self._fit)

    def _cancel(self, event):
        if event.widget is self.parent and self._pending is not None:
            self.parent.after_cancel(self._pending)
            self._pending = None

    def _fit(self):
        self._pending = None
        gap = round(8 * ui_scale(self.parent))
        required = self.label.winfo_reqwidth() + self.action.winfo_reqwidth() + round(self.minimum * ui_scale(self.parent)) + 2 * gap
        stacked = self.parent.winfo_width() < required
        if stacked == self._stacked:
            return
        self._stacked = stacked
        self.parent.grid_columnconfigure(0, weight=1 if stacked else 0)
        self.parent.grid_columnconfigure(1, weight=0 if stacked else 1)
        self.parent.grid_columnconfigure(2, weight=0)
        self.label.grid(row=0, column=0, sticky="w")
        self.action.grid(row=0, column=1 if stacked else 2, sticky="e")
        self.field.grid(row=1 if stacked else 0, column=0 if stacked else 1,
                        columnspan=2 if stacked else 1, sticky="ew",
                        padx=0 if stacked else (gap, gap), pady=(gap, 0) if stacked else 0)


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
    """Static shaded glass chrome around an opaque native reading surface."""

    def __init__(self, parent, palette, padding: int = 10, *, expand=False, heading=""):
        super().__init__(parent, bg=palette.BG_DARKEST, height=1, bd=0, highlightthickness=0)
        self.palette = palette
        self.expand = expand
        self.padding = round(padding * ui_scale(self))
        self.heading = heading
        self._heading_font = tkfont.Font(root=self, family="Montserrat", size=9, weight="bold") if heading else None
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

    def _reading_height(self):
        """Reserve one native text line before spending short-pane space on rims."""
        required = 1
        for child in self.body.winfo_children():
            if not child.winfo_manager():
                continue
            line_height = 1
            pending = [child]
            while pending:
                widget = pending.pop()
                pending.extend(item for item in widget.winfo_children() if item.winfo_manager())
                try:
                    content_font = widget.cget("font")
                except tk.TclError:
                    continue
                if content_font:
                    height = int(widget.tk.call("font", "metrics", content_font, "-linespace"))
                    for name in ("pady", "borderwidth", "highlightthickness"):
                        try:
                            height += 2 * widget.winfo_pixels(widget.cget(name))
                        except tk.TclError:
                            pass
                    line_height = max(line_height, height)
            if child.winfo_manager() == "pack":
                padding = child.pack_info().get("pady", 0)
                if isinstance(padding, (tuple, list)):
                    line_height += sum(child.winfo_pixels(value) for value in padding)
                else:
                    line_height += 2 * child.winfo_pixels(padding)
            required = max(required, line_height)
        return required

    def _draw(self):
        self._redraw_id = None
        p = self.palette
        height = self.winfo_height() if self.expand else self.body.winfo_reqheight() + 2 * self.padding
        width = self.winfo_width()
        if not self.expand and self.winfo_pixels(self.cget("height")) != height:
            self.configure(height=height)
        heading_height = self._heading_font.metrics("linespace") + round(10 * ui_scale(self)) if self.heading else 0
        reading_height = self._reading_height() if self.expand else self.body.winfo_reqheight()
        # A short pane spends its pixels on reading before panel headings.
        if self.expand and height < reading_height + heading_height + 2 * self.padding:
            heading_height = 0
        self.itemconfigure(self._body_window, width=max(1, width - 2 * self.padding))
        if self.expand:
            vertical_padding = min(self.padding, max(0, (height - reading_height - heading_height) // 2))
            self.coords(self._body_window, self.padding, vertical_padding + heading_height)
            self.itemconfigure(self._body_window, height=max(1, height - 2 * vertical_padding - heading_height))
        self.delete("glass")
        if width < 4 or height < 4:
            return
        radius = min(round(12 * ui_scale(self)), height // 2, width // 2)
        points = []
        for x, y, angle in ((1 + radius, 1 + radius, 180),
                            (width - 1 - radius, 1 + radius, 270),
                            (width - 1 - radius, height - 1 - radius, 0),
                            (1 + radius, height - 1 - radius, 90)):
            for step in range(9):
                theta = math.radians(angle + step * 90 / 8)
                points.extend((x + radius * math.cos(theta), y + radius * math.sin(theta)))
        light = readable_foreground(p.BG_DARKEST) == "#000000"
        top = mix(p.BG_LIGHT, "#ffffff", .30 if light else .10)
        bottom = mix(p.BG_MID, p.BG_DARKEST, .30)
        rim = mix(p.BORDER, p.BG_MID, .30)
        self.create_polygon(points, smooth=True, fill=bottom, outline="", tags="glass")
        # Bounded bands add depth down the whole rim. The upper-left diagonal
        # reflection follows the workspace without sampling or blurring it.
        bands = min(80, max(1, height - 4))
        for step in range(bands):
            y0 = 2 + (height - 4) * step / bands
            y1 = 2 + (height - 4) * (step + 1) / bands
            center = (y0 + y1) / 2
            edge = max(0, radius - min(center - 1, height - 1 - center))
            inset = radius - math.sqrt(max(0, radius * radius - edge * edge))
            color = mix(top, bottom, min(1, center / max(1, height * .72)))
            self.create_rectangle(1 + inset, y0, width - 1 - inset, y1,
                                  fill=color, outline="", tags="glass")
            reflection_end = width * (.60 - .26 * center / max(1, height))
            if reflection_end > 1 + inset:
                reflection = mix(color, "#ffffff", (.13 if light else .055) * max(0, 1 - center / max(1, height * .6)))
                self.create_rectangle(1 + inset, y0, reflection_end, y1,
                                      fill=reflection, outline="", tags="glass")
        self.create_polygon(points, smooth=True, fill="", outline=rim, tags="glass")
        self.create_line(radius + 1, 2, width - radius - 1, 2,
                         fill=mix(rim, "#ffffff", .45 if light else .32), tags="glass")
        self.create_line(radius + 1, height - 2, width - radius - 1, height - 2,
                         fill=mix(bottom, p.BG_DARKEST, .55), tags="glass")
        if heading_height:
            self.create_text(self.padding + 2, self.padding + heading_height // 2,
                             text=self.heading, anchor="w", font=self._heading_font,
                             fill=p.TEXT_BRIGHT, tags="glass")
        self.tag_lower("glass", self._body_window)
