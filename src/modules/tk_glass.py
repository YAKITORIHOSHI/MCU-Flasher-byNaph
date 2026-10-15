"""Static glass cards for the Tk downloader; no blur or animation loop."""
from __future__ import annotations

import tkinter as tk
import sys
import math
import base64
import struct
import zlib
from functools import lru_cache
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

    def _schedule(self, event=None):
        if (event is None or event.widget is self.pane) and self._pending is None:
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
            if vertical:
                # Native SVG actions can be taller than a single text line.
                # Keep one complete reading/control surface in each pane when
                # it fits, before allocating optional scaled padding/chrome.
                reading_left = self.left._reading_height() if isinstance(self.left, GlassCard) else 1
                reading_right = self.right._reading_height() if isinstance(self.right, GlassCard) else 1
                if reading_left + reading_right <= available:
                    left = max(reading_left, min(left, available - reading_right))
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

    def __init__(self, parent, background, *, fill_viewport=False):
        super().__init__(parent, bg=background)
        self._fill_viewport = fill_viewport
        self._fit_after = None
        self.canvas = tk.Canvas(self, bg=background, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.body = tk.Frame(self.canvas, bg=background)
        self._window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.canvas.bind("<Configure>", self._fit_body)
        self.body.bind("<Configure>", self._content_changed)
        self._toplevel = self.winfo_toplevel()
        self._bindings = [(sequence, self._toplevel.bind(sequence, self._wheel, add="+"))
                          for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>")]
        self.bind("<Destroy>", self._detach, add="+")

    def _fit_body(self, _event=None):
        options = {"width": max(1, self.canvas.winfo_width())}
        if self._fill_viewport:
            options["height"] = max(self.body.winfo_reqheight(), self.canvas.winfo_height())
        for key, value in options.items():
            if float(self.canvas.itemcget(self._window, key)) != value:
                self.canvas.itemconfigure(self._window, **{key: value})

    def request_fit(self):
        """Recheck a stretched form after a child's natural height changes."""
        if self._fit_after is None:
            self._fit_after = self.after_idle(self._finish_fit)

    def _finish_fit(self):
        self._fit_after = None
        self._fit_body()

    def _content_changed(self, _event=None):
        if self._fill_viewport:
            self._fit_body()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

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
            if self._fit_after is not None:
                self.after_cancel(self._fit_after)
                self._fit_after = None
            for sequence, callback in self._bindings:
                self._toplevel.unbind(sequence, callback)
            self._bindings.clear()


class ScrollForm(ScrollBody):
    """Scrollable utility form which reveals fields reached with Tab."""

    def __init__(self, parent, background, *, fill_viewport=False):
        super().__init__(parent, background, fill_viewport=fill_viewport)
        self._reveal_after = None
        self._focus_binding = self._toplevel.bind("<FocusIn>", self._focus_in, add="+")
        self.canvas.bind("<Configure>", self._queue_reveal, add="+")
        self.body.bind("<Configure>", self._queue_reveal, add="+")
        self.bind("<Destroy>", self._detach_focus, add="+")

    def _queue_reveal(self, _event=None):
        if self._reveal_after is None:
            self._reveal_after = self.after_idle(self._reveal_current)

    def _reveal_current(self):
        self._reveal_after = None
        self._focus_in()

    def _focus_in(self, event=None):
        try:
            widget = event.widget if event is not None else self.focus_get()
        except (KeyError, tk.TclError):
            return  # A native popup can own focus without a Python wrapper.
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
            if self._reveal_after is not None:
                self.after_cancel(self._reveal_after)
                self._reveal_after = None



def _rgb(color):
    return tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))


def _blend(first, second, amount):
    return tuple(round(a + (b - a) * amount) for a, b in zip(first, second))


@lru_cache(maxsize=64)
def glass_png(width, height, base, lower, rim, surround, radius=12, reflection=True):
    """Precomposed native glass, with antialiased rims and a soft reflection.

    Tk has no child-widget alpha compositing. Paint the material once instead
    of claiming desktop blur. Bounded horizontal samples soften the reflection
    without per-pixel work across large reading panes; no optional packages.
    Only the small control skins are cached (large cards call the uncached body).
    """
    width, height = max(1, int(width)), max(1, int(height))
    if width > 8192 or height > 8192 or width * height > 16_000_000:
        raise ValueError("Glass surface exceeds the native image budget")
    rounded = radius > 0
    radius = min(max(1, radius), width / 2, height / 2)
    top, bottom, edge, outside = map(_rgb, (base, lower, rim, surround))
    rows = bytearray()
    # Smooth coverage at the rounded boundary: each scanline only needs four
    # edge pixels, and broad interior runs are built as repeated RGB bytes.
    for y in range(height):
        center = y + .5
        dy = max(0., radius - min(center, height - center))
        inset = radius - math.sqrt(max(0., radius * radius - dy * dy))
        inner_radius = max(.1, radius - 1)
        inner_dy = max(0., inner_radius - min(center - 1, height - center - 1))
        inner_inset = 1 + inner_radius - math.sqrt(max(0., inner_radius**2 - inner_dy**2))
        vertical = center / max(1, height)
        color = _blend(top, bottom, min(1., vertical * 1.25))
        rows.append(0)
        # A faint broad reflection fades in two axes; no sharp diagonal cuts.
        samples = min(48, max(1, width // 8))
        for step in range(samples):
            x0, x1 = round(width * step / samples), round(width * (step + 1) / samples)
            x = (x0 + x1) / 2
            glow = max(0., 1 - abs(x / width + vertical * .32 - .18) / .65)
            sheen = (.06 if sum(top) < 500 else .035) * glow * max(0., 1 - vertical * 1.2) if reflection else 0.
            fill = _blend(color, (255, 255, 255), sheen)
            if reflection and width * height > 65536:
                # Broad defocused light through the material, rather than
                # black slabs or bands painted around an opaque center.
                left = max(0., 1 - ((x / width - .12) ** 2 / .45 + (vertical - .18) ** 2 / .65)) ** 2
                right = max(0., 1 - ((x / width - .92) ** 2 / .42 + (vertical - .7) ** 2 / .7)) ** 2
                fill = _blend(fill, (142, 219, 219), .18 * left)
                fill = _blend(fill, (145, 160, 224), .14 * right)
            if not rounded or (x0 >= inner_inset + 1 and x1 <= width - inner_inset - 1 and 1 <= y < height - 1):
                rows.extend(bytes(fill) * (x1 - x0))
                continue
            for pixel in range(x0, x1):
                distance = min(pixel + .5, width - pixel - .5)
                coverage = min(1., max(0., distance - inset + .5))
                interior = min(1., max(0., distance - inner_inset + .5))
                interior *= min(1., max(0., center - 1), max(0., height - center - 1))
                boundary = _blend(edge, fill, interior)
                rows.extend(_blend(outside, boundary, coverage))
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 3)) + chunk(b'IEND', b''))


def glass_photo(master, width, height, base, lower, rim, surround, radius=12, reflection=True):
    renderer = glass_png if width * height <= 65536 else glass_png.__wrapped__
    return tk.PhotoImage(master=master, data=base64.b64encode(
        renderer(width, height, base, lower, rim, surround, radius, reflection)), format="png")


from src.modules.tk_svg import SvgButton, photo as svg_photo


class GlassButton(SvgButton):
    """Native button semantics and SVGs over a rounded, static glass skin."""

    def __init__(self, *args, **kwargs):
        self._glass_colors = {}
        for key in ("background", "bg", "activebackground"):
            if key in kwargs:
                self._glass_colors["background" if key == "bg" else key] = kwargs[key]
        super().__init__(*args, **kwargs)
        tk.Button.configure(self, highlightthickness=0, bd=0, relief="flat")
        self.bind("<FocusIn>", lambda _event: self._refresh_svg(), add="+")
        self.bind("<FocusOut>", lambda _event: self._refresh_svg(), add="+")

    def cget(self, key):
        canonical = "background" if key == "bg" else key
        if canonical in ("background", "activebackground"):
            if canonical not in self._glass_colors:
                self._glass_colors[canonical] = tk.Button.cget(self, canonical)
            return self._glass_colors[canonical]
        return tk.Button.cget(self, key)

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}
            cnf = None
        previous = dict(self._glass_colors)
        for key in ("background", "bg", "activebackground"):
            if key in kwargs:
                self._glass_colors["background" if key == "bg" else key] = kwargs[key]
        try:
            return super().configure(cnf, **kwargs)
        except tk.TclError:
            self._glass_colors = previous
            raise

    config = configure

    def _refresh_svg(self):
        font = tkfont.Font(root=self, font=self.cget("font"))
        text = self.cget("text")
        scale = ui_scale(self)
        size = max(12, min(32, font.metrics("linespace")))
        name = getattr(self, "_svg_name", "")
        disabled = str(self.cget("state")) == "disabled"
        active = getattr(self, "_svg_hovering", False) and not disabled
        base = self.cget("activebackground" if active else "background")
        ink = self.cget("disabledforeground" if disabled else "activeforeground" if active else "foreground")
        outside = self.master.cget("background")
        def hex_color(value):
            return '#' + ''.join(f'{part // 257:02x}' for part in self.winfo_rgb(value))
        base, ink, outside = map(hex_color, (base, ink, outside))
        if disabled:
            base = mix(base, outside, .55)
        # Preserve text contrast against the *brightest* material sample.
        upper = mix(base, "#ffffff", .018 if readable_foreground(base) == "#ffffff" else .10)
        lower = mix(base, "#000000", .035)
        from src.modules.ui_palette import contrast_ratio
        def contrast(value, first, last):
            return min(contrast_ratio(value, mix(first, "#ffffff", .065)), contrast_ratio(value, last))
        if contrast(ink, upper, lower) < 4.5:
            ink = max(("#000000", "#ffffff"), key=lambda value: contrast(value, upper, lower))
            if contrast(ink, upper, lower) < 4.5:
                # Some middle luminance hues cross the black/white threshold.
                # Adjust only the rendered material, retaining the action hue.
                endpoint = "#000000" if ink == "#ffffff" else "#ffffff"
                for step in range(1, 26):
                    first, last = mix(upper, endpoint, step / 100), mix(lower, endpoint, step / 100)
                    if contrast(ink, first, last) >= 4.5:
                        upper, lower = first, last
                        break
            tk.Button.configure(self, **{"disabledforeground" if disabled else "activeforeground" if active else "foreground": ink})
        width = font.measure(text) + round(24 * scale) + (2 * (size + round(5 * scale)) if name else 0)
        height = max(size, font.metrics("linespace")) + round(14 * scale)
        rim = mix(base, "#ffffff", .12 if readable_foreground(base) == "#ffffff" else .24)
        if self.focus_get() is self:
            rim = hex_color(self.cget("highlightcolor"))
        key = (width, height, upper, lower, rim, outside, ink, name)
        if key != getattr(self, "_glass_key", None):
            self._glass_key = key
            self._svg_image = glass_photo(self, width, height, upper, lower, rim, outside, max(4, round(8 * scale)))
            if name:
                icon = svg_photo(self, name, ink, size)
                x = round((width - font.measure(text)) / 2 - size - 5 * scale)
                self.tk.call(self._svg_image, "copy", icon, "-to", x, (height - size) // 2,
                             "-compositingrule", "overlay")
            tk.Button.configure(self, image=self._svg_image, compound="center", padx=0, pady=0,
                                highlightbackground=outside)
        # Classic Tk reserves another pixel around an image even with bd=0.
        # Paint it in the surrounding surface, preserving the action colors
        # separately so a square native frame cannot outline the rounded skin.
        tk.Button.configure(self, background=outside, activebackground=outside)


def material_ink(color, upper, lower, minimum=4.5):
    """Retain a semantic ink, adjusting it only for the reflective bounds."""
    from src.modules.ui_palette import contrast_ratio
    surfaces = (mix(upper, "#ffffff", .18), lower)
    def readable(value):
        return all(contrast_ratio(value, surface) >= minimum for surface in surfaces)
    if readable(color):
        return color
    choices = []
    for endpoint in ("#000000", "#ffffff"):
        if not readable(endpoint):
            continue
        low, high = 0., 1.
        for _ in range(16):
            middle = (low + high) / 2
            if readable(mix(color, endpoint, middle)):
                high = middle
            else:
                low = middle
        choices.append((high, mix(color, endpoint, high)))
    return min(choices)[1] if choices else readable_foreground(upper)


class EntryWell(tk.Canvas):
    """Rounded field surround; editing, selection and focus remain native Tk."""

    def __init__(self, parent, palette, **options):
        super().__init__(parent, bg=parent.cget("background"), bd=0, highlightthickness=0, width=1, height=1)
        self.palette = palette
        options.update(bg=palette.BG_DARK, fg=palette.TEXT_BRIGHT, insertbackground=palette.CYAN,
                       selectbackground=palette.CYAN_DIM, selectforeground=readable_foreground(palette.CYAN_DIM),
                       bd=0, relief="flat", highlightthickness=0)
        self.entry = tk.Entry(self, **options)
        self._field = self.create_window(0, 0, window=self.entry, anchor="w")
        self._surface_key = None
        self.bind("<Configure>", self._draw, add="+")
        self.entry.bind("<FocusIn>", self._draw, add="+")
        self.entry.bind("<FocusOut>", self._draw, add="+")
        self._draw()

    def _draw(self, _event=None):
        p, scale = self.palette, ui_scale(self)
        height = self.entry.winfo_reqheight() + round(12 * scale)
        if self.winfo_pixels(self.cget("height")) != height:
            self.configure(height=height)
        width, pad = max(1, self.winfo_width()), round(10 * scale)
        self.coords(self._field, pad, height / 2)
        self.itemconfigure(self._field, width=max(1, width - 2 * pad))
        key = (width, height, p.BG_DARK, p.BG_DARK,
               p.CYAN if self.entry.focus_get() is self.entry else mix(p.BORDER, p.BG_DARK, .55),
               self.cget("background"), round(7 * scale), False)
        if key != self._surface_key:
            self._surface_key = key
            self._surface = glass_photo(self, *key)
            self.delete("surface")
            self.create_image(0, 0, image=self._surface, anchor="nw", tags="surface")
            self.tag_lower("surface", self._field)

    def _schedule(self):
        self._draw()


class BrowserHeader(tk.Canvas):
    """Paint the title directly on glass so opaque labels cannot hide it."""

    def __init__(self, parent, palette, *, title="Libraries & boards", subtitle="MCU Flasher by Naph · Arduino packages"):
        super().__init__(parent, bg=getattr(palette, "WINDOW_BG", palette.BG_DARKEST), height=1, bd=0, highlightthickness=0)
        self.palette = palette
        self.title, self.subtitle = title, subtitle
        self._redraw_id = None
        self._title_font = tkfont.Font(root=self, family="Montserrat", size=14, weight="bold")
        self._subtitle_font = tkfont.Font(root=self, family="Montserrat", size=8)
        self.actions = tk.Canvas(self, bg=palette.BG_MID, width=1, height=1, bd=0, highlightthickness=0)
        self._actions_window = self.create_window(0, 0, window=self.actions, anchor="ne")
        self.bind("<Configure>", self._schedule, add="+")
        self.actions.bind("<Configure>", self._schedule, add="+")
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
        p, scale = self.palette, ui_scale(self)
        width, pad = self.winfo_width(), round(10 * scale)
        mark_size, gap = round(32 * scale), round(10 * scale)
        title_height = self._title_font.metrics("linespace") + self._subtitle_font.metrics("linespace") + round(4 * scale)
        title_width = mark_size + gap + max(self._title_font.measure(self.title),
                                           self._subtitle_font.measure(self.subtitle))
        action_width, action_height = self.actions.winfo_reqwidth(), self.actions.winfo_reqheight()
        stacked = width < title_width + action_width + 3 * pad
        if stacked:
            # Secondary chrome yields before a saved package-content font.
            title_height = self._title_font.metrics("linespace")
        height = 2 * pad + max(mark_size, title_height, action_height)
        if stacked:
            height = 2 * pad + max(mark_size, title_height) + action_height + round(6 * scale)
        if self.winfo_pixels(self.cget("height")) != height:
            self.configure(height=height)
        if width < 2:
            return
        self.delete("material")
        top = mix(p.BG_MID, "#ffffff", .045)
        key = (width, height, top, p.BG_DARK, mix(p.BORDER, p.CYAN, .22), self.cget("background"), round(14 * scale))
        if key != getattr(self, "_surface_key", None):
            self._surface_key = key
            self._surface = glass_photo(self, *key)
        self.create_image(0, 0, image=self._surface, anchor="nw", tags="material")
        self._brand = svg_photo(self, "brand", material_ink(p.CYAN, top, p.BG_DARK, 3), mark_size)
        self.create_image(pad, pad + max(mark_size, title_height) / 2, image=self._brand,
                          anchor="w", tags="material")
        title_x = pad + mark_size + gap
        self.create_text(title_x, pad, text=self.title, anchor="nw",
                         font=self._title_font, fill=p.TEXT_BRIGHT, tags="material")
        if not stacked:
            self.create_text(title_x, pad + self._title_font.metrics("linespace") + round(4 * scale),
                             text=self.subtitle, anchor="nw",
                             font=self._subtitle_font, fill=material_ink(p.TEXT_DIM, top, p.BG_DARK), tags=("material", "subtitle"))
        action_y = height - pad - action_height if stacked else (height - action_height) / 2
        self.coords(self._actions_window, width - pad, action_y)
        self.tag_lower("material", self._actions_window)
        # Native button focus and commands stay native. Blend their surrounding
        # pixels with the material immediately behind the action row.
        background = mix(top, p.BG_DARK, min(1., (action_y + action_height / 2) / height * 1.25))
        if self.actions.cget("background") != background:
            self.actions.configure(bg=background)
        self._actions_surface = tk.PhotoImage(master=self, width=action_width, height=action_height)
        self.tk.call(self._actions_surface, "copy", self._surface,
                     "-from", max(0, width - pad - action_width), round(action_y), width - pad, round(action_y) + action_height)
        self.actions.delete("material")
        self.actions.create_image(0, 0, image=self._actions_surface, anchor="nw", tags="material")
        for button in self.actions.winfo_children():
            if isinstance(button, GlassButton):
                button._refresh_svg()


def configure_glass_tabs(style, root, palette):
    """Nine-sliced rounded tab surfaces with native notebook navigation."""
    p, scale = palette, ui_scale(root)
    size, corner = max(24, round(36 * scale)), max(6, round(10 * scale))
    key = (p.active_theme, size)
    photos = getattr(root, "_glass_tab_photos", None)
    if photos is None:
        photos = root._glass_tab_photos = {}
    name = "BrowserGlass." + p.active_theme + str(size) + ".tab"
    if key not in photos:
        photos[key] = tuple(glass_photo(root, size, size, upper, lower, rim, getattr(p, "WINDOW_BG", p.BG_DARKEST), corner, False)
                            for upper, lower, rim in (
                                (getattr(p, "WINDOW_BG", p.BG_DARKEST), getattr(p, "WINDOW_BG", p.BG_DARKEST),
                                 getattr(p, "WINDOW_BG", p.BG_DARKEST)),
                                (mix(p.BG_MID, p.CYAN, .06), p.BG_MID, mix(p.CYAN, p.BG_MID, .70)),
                                (p.BG_HOVER, p.BG_MID, mix(p.BORDER_LIT, p.BG_MID, .6))))
        normal, selected, active = photos[key]
        style.element_create(name, "image", normal, ("selected", selected), ("active", active),
                             border=(corner, corner, corner, corner), sticky="nsew")
    style.layout("TNotebook.Tab", [(name, {"sticky": "nswe", "children": [
        ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
            ("Notebook.focus", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""})]})]})]})])


class GlassEmptyState(tk.Canvas):
    """A quiet reflective empty pane, retaining the native package layout."""

    def __init__(self, parent, palette, *, installed=False):
        super().__init__(parent, bg=palette.BG_DARKEST, bd=0, highlightthickness=0)
        self.palette, self.installed = palette, installed
        self._redraw_id = None
        self.bind("<Configure>", self._schedule, add="+")
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
        width, height = self.winfo_width(), self.winfo_height()
        if min(width, height) < 2:
            return
        p, scale = self.palette, ui_scale(self)
        top = mix(p.BG_MID, "#ffffff", .045)
        key = (width, height, top, p.BG_DARK, top, p.BG_DARKEST, 0)
        if key != getattr(self, "_surface_key", None):
            self._surface_key = key
            self._surface = glass_photo(self, *key)
        self.delete("all")
        self.create_image(0, 0, image=self._surface, anchor="nw")
        center = height // 2
        compact = height < round(145 * scale)
        if not compact:
            self._mark = svg_photo(self, "package" if self.installed else "book", p.TEXT_DIM, round(36 * scale))
            self.create_image(width // 2, center - round(37 * scale), image=self._mark)
        self.create_text(width // 2, center + (0 if compact else round(6 * scale)),
                         text="Select an installed package" if self.installed else "Select a library or board",
                         fill=p.TEXT_BRIGHT, font=("Montserrat", 12), width=max(1, width - round(24 * scale)))
        if not compact:
            self.create_text(width // 2, center + round(34 * scale),
                             text="Its releases and details will appear here.", fill=p.TEXT_DIM,
                             font=("Montserrat", 9), width=max(1, width - round(24 * scale)))


class GlassCard(tk.Canvas):
    """Static shaded glass chrome around an opaque native reading surface."""

    def __init__(self, parent, palette, padding: int = 10, *, expand=False, heading=""):
        try:
            surround = parent.cget("background")
        except tk.TclError:
            surround = getattr(palette, "WINDOW_BG", palette.BG_DARKEST)
        super().__init__(parent, bg=surround, height=1, bd=0, highlightthickness=0)
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
                if isinstance(widget, GlassButton):
                    # SVG artwork can exceed the typographic line height. The
                    # native request includes both the image and hit-area
                    # padding and must fit wholly inside the reading viewport.
                    line_height = max(line_height, widget.winfo_reqheight())
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
        requested = self.body.winfo_reqheight() + 2 * self.padding
        height = self.winfo_height() if self.expand else max(requested, self.winfo_height())
        width = self.winfo_width()
        if not self.expand and self.winfo_pixels(self.cget("height")) != requested:
            self.configure(height=requested)
            self.event_generate("<<GlassRequestChanged>>", when="tail")
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
        # The reading body stays opaque; the enclosing material uses a soft,
        # continuous reflection and a single smooth rim, without stacked bevels.
        top = mix(p.BG_MID, p.BG_LIGHT, .18)
        bottom = mix(p.BG_MID, p.BG_DARKEST, .12)
        rim = mix(p.BORDER, p.BG_MID, .70)
        key = (width, height, top, bottom, rim, self.cget("background"), round(14 * ui_scale(self)))
        if key != getattr(self, "_surface_key", None):
            self._surface_key = key
            self._surface = glass_photo(self, *key)
        self.create_image(0, 0, image=self._surface, anchor="nw", tags="glass")
        if heading_height:
            self.create_text(self.padding + 2, self.padding + heading_height // 2,
                             text=self.heading, anchor="w", font=self._heading_font,
                             fill=p.TEXT_BRIGHT, tags="glass")
        self.tag_lower("glass", self._body_window)
