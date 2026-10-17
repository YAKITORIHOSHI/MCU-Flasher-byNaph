"""Native Tk containers and labels composited over their owner's static glass.

Owners provide a full-sized ``_material_image`` and optional
``_material_bounds = (upper_color, lower_color)``. Containers and labels clip the
shared image at their screen-relative offset without copies or refresh timers.
Small rounded controls can composite a bounded crop through ``material_patch``.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

from src.modules.tk_svg import photo
from src.modules.ui_palette import contrast_ratio, readable_foreground


def _owner(widget):
    parent = widget.master
    while parent is not None:
        if getattr(parent, "_material_image", None) is not None:
            return parent
        parent = parent.master
    return None


def material_location(widget):
    """Return the current material and widget offset, for small native skins."""
    owner = _owner(widget)
    if owner is None:
        return None
    return (owner._material_image, widget.winfo_rootx()-owner.winfo_rootx(),
            widget.winfo_rooty()-owner.winfo_rooty())


def material_patch(widget, width, height, location=None):
    """Copy only a control-sized backdrop; never retain another large surface."""
    location = location or material_location(widget)
    if location is None:
        return None
    if not (0 < width <= 8192 and 0 < height <= 8192 and width*height <= 16_000_000):
        raise ValueError("Native control exceeds the material image budget")
    source, x, y = location
    patch = tk.PhotoImage(master=widget, width=width, height=height)
    patch.put(widget.master.cget("background"), to=(0, 0, width, height))
    left, top = max(0, x), max(0, y)
    right, bottom = min(source.width(), x+width), min(source.height(), y+height)
    if left < right and top < bottom:
        widget.tk.call(patch, "copy", source, "-from", left, top, right, bottom,
                       "-to", left-x, top-y, "-compositingrule", "overlay")
    return patch


def _refresh_children(widget):
    for child in widget.winfo_children():
        if not child.winfo_viewable():
            continue
        refresh = getattr(child, "refresh_material", None)
        if refresh is not None:
            refresh()
        else:
            _refresh_children(child)


class _MaterialCanvas(tk.Canvas):
    def __init__(self, master=None, **options):
        if "background" not in options and "bg" not in options:
            try:
                options["background"] = master.cget("background") if master is not None else "#ffffff"
            except tk.TclError:
                options["background"] = "#ffffff"
        options.setdefault("highlightthickness", 0)
        options.setdefault("borderwidth", 0)
        options.setdefault("width", 1)
        options.setdefault("height", 1)
        self._redraw_id = None
        self._material_photo = None
        self._material_item = None
        self._material_key = None
        self._material_closed = False
        super().__init__(master, **options)
        # Application callers may replace normal widget bindings. Keep our
        # owned geometry/cleanup hooks independent of those bindings.
        self._material_bindtag = f"MaterialCanvas:{self}:{id(self)}"
        self.bindtags((self._material_bindtag, *self.bindtags()))
        self._material_bindings = {
            sequence: self._bind(("bind", self._material_bindtag), sequence, callback, False, True)
            for sequence, callback in (("<Configure>", self._queue_material),
                                       ("<Map>", self._queue_material),
                                       ("<Destroy>", self._close_material))
        }
        self._queue_material()

    def _queue_material(self, *_args):
        # Variable traces supply three arguments; event hooks supply one.
        if not self._material_closed and self._redraw_id is None:
            self._redraw_id = self.after_idle(self.refresh_material)

    def _close_material(self, event):
        if event.widget is not self or self._material_closed:
            return
        self._material_closed = True
        if self._redraw_id is not None:
            self.after_cancel(self._redraw_id)
            self._redraw_id = None
        for sequence, command in self._material_bindings.items():
            self.tk.call("bind", self._material_bindtag, sequence, "")
            self.deletecommand(command)
        self._material_bindings.clear()
        self._material_photo = None

    def _paint_material(self):
        owner = _owner(self)
        image = getattr(owner, "_material_image", None)
        x = self.canvasx(0) + owner.winfo_rootx() - self.winfo_rootx() if image is not None else 0
        y = self.canvasy(0) + owner.winfo_rooty() - self.winfo_rooty() if image is not None else 0
        key = (str(image), x, y, self.winfo_width(), self.winfo_height(),
               getattr(owner, "_material_bounds", None), self.cget("background"))
        if key == self._material_key:
            return False
        self._material_key = key
        self._material_photo = image
        if self._material_photo is None:
            if self._material_item is not None:
                self.delete(self._material_item)
                self._material_item = None
            return True
        if self._material_item is None:
            self._material_item = self.create_image(x, y, anchor="nw", image=self._material_photo)
        else:
            self.coords(self._material_item, x, y)
            self.itemconfigure(self._material_item, image=self._material_photo)
        self.tag_lower(self._material_item)
        return True

    def refresh_material(self):
        if self._material_closed:
            return
        if self._redraw_id is not None:
            self.after_cancel(self._redraw_id)
            self._redraw_id = None
        if self.winfo_exists() and self.winfo_viewable():
            if self._paint_material():
                _refresh_children(self)


class GlassBody(_MaterialCanvas):
    """A pack/grid container, or scroll canvas, that continues its owner's glass.

    Native children keep their normal keyboard and pointer behavior. As with any
    painted scroll canvas, use the content window's bbox for its scrollregion;
    ``bbox('all')`` also includes the background image.
    """

    def xview(self, *args):
        result = super().xview(*args)
        if args:
            self._queue_material()
        return result

    def yview(self, *args):
        result = super().yview(*args)
        if args:
            self._queue_material()
        return result

    def xview_moveto(self, fraction):
        super().xview_moveto(fraction)
        self._queue_material()

    def yview_moveto(self, fraction):
        super().yview_moveto(fraction)
        self._queue_material()

    def xview_scroll(self, number, what):
        super().xview_scroll(number, what)
        self._queue_material()

    def yview_scroll(self, number, what):
        super().yview_scroll(number, what)
        self._queue_material()


class GlassLabel(_MaterialCanvas):
    """Canvas text with Label's common options and the local SVG label contract."""

    _aliases = {"fg": "foreground", "bg": "background"}
    _defaults = {"text": "", "textvariable": "", "font": "TkDefaultFont",
                 "foreground": "#000000", "anchor": "center", "justify": "center",
                 "wraplength": 0, "padx": 1, "pady": 1}

    def __init__(self, master=None, *, icon_name="", icon_only=False, **options):
        self._label_options = dict(self._defaults)
        self._svg_name, self._svg_icon_only = icon_name, icon_only
        self._variable_trace = None
        self._variable_ref = None
        self._text_item = self._icon_item = None
        self._svg_image = None
        self._request_size = None
        self._label_paint_key = None
        for key in tuple(options):
            canonical = self._aliases.get(key, key)
            if canonical in self._label_options:
                self._label_options[canonical] = options.pop(key)
        super().__init__(master, **options)
        self._font = tkfont.Font(root=self, font=self._label_options["font"])
        self._set_variable(self._label_options["textvariable"])
        self.refresh_material()

    def _set_variable(self, variable):
        if self._variable_trace is not None:
            name, command = self._variable_trace
            try:
                self.tk.call("trace", "remove", "variable", name, "write", command)
            except tk.TclError:
                pass
            self.deletecommand(command)
        self._variable_ref = variable if isinstance(variable, tk.Variable) else None
        self._label_options["textvariable"] = str(variable) if variable else ""
        self._variable_trace = None
        if variable:
            name = str(variable)
            if not self.tk.call("info", "exists", name):
                self.setvar(name, self._label_options["text"])
            command = self._register(self._queue_material)
            self.tk.call("trace", "add", "variable", name, "write", command)
            self._variable_trace = (name, command)

    def _text(self):
        name = self._label_options["textvariable"]
        if name:
            try:
                return str(self.getvar(name))
            except tk.TclError:
                pass
        return str(self._label_options["text"])

    def _close_material(self, event):
        if event.widget is self:
            self._set_variable("")
        super()._close_material(event)

    def configure(self, cnf=None, **options):
        if isinstance(cnf, dict):
            options = {**cnf, **options}
            cnf = None
        if cnf is not None:
            key = self._aliases.get(cnf, cnf)
            if key in self._label_options:
                return (key, key, key.title(), self._defaults[key], self.cget(key))
            return super().configure(cnf)
        if not options:
            result = super().configure()
            result.update({key: self.configure(key) for key in self._label_options})
            return result
        for key in tuple(options):
            canonical = self._aliases.get(key, key)
            if canonical not in self._label_options:
                continue
            value = options.pop(key)
            if canonical == "textvariable":
                self._label_options["text"] = self._text()
                self._set_variable(value)
            else:
                self._label_options[canonical] = value
                if canonical == "font":
                    self._font.configure(**tkfont.Font(root=self, font=value).actual())
                    # A named font can change while its option string stays
                    # the same. Its new metrics must invalidate label layout.
                    self._label_paint_key = None
        result = super().configure(**options) if options else None
        self._queue_material()
        return result

    config = configure

    def cget(self, key):
        canonical = self._aliases.get(key, key)
        if canonical in self._label_options:
            return self._text() if canonical == "text" else self._label_options[canonical]
        return super().cget(key)

    __getitem__ = cget

    def set_icon(self, name):
        self._svg_name = name
        self._queue_material()

    _refresh_svg = _MaterialCanvas._queue_material

    def refresh_material(self):
        super().refresh_material()
        if not self.winfo_exists():
            return
        options, text = self._label_options, self._text()
        key = (tuple((name, str(value)) for name, value in options.items()), text,
               self._svg_name, self._svg_icon_only, self.winfo_width(), self.winfo_height(),
               self._material_key)
        if key == self._label_paint_key:
            return
        self._label_paint_key = key
        color = options["foreground"]
        color = "#" + "".join(f"{part // 257:02x}" for part in self.winfo_rgb(color))
        owner = _owner(self)
        bounds = getattr(owner, "_material_bounds", None)
        if bounds:
            from src.modules.tk_glass import material_ink
            color = material_ink(color, *bounds)
        elif contrast_ratio(color, self.cget("background")) < 4.5:
            color = readable_foreground(self.cget("background"))
        wrap = max(0, self.winfo_pixels(options["wraplength"]))
        if self._text_item is None:
            self._text_item = self.create_text(0, 0, anchor="nw")
        self.itemconfigure(self._text_item, text=text, font=self._font, fill=color,
                           justify=options["justify"], width=wrap)
        self.coords(self._text_item, 0, 0)
        bounds = self.bbox(self._text_item) or (0, 0, 0, 0)
        text_width, text_height = bounds[2] - bounds[0], max(self._font.metrics("linespace"), bounds[3] - bounds[1])
        icon_size = max(12, min(32, self._font.metrics("linespace"))) if self._svg_name and (text or self._svg_icon_only) else 0
        gap = 2 if icon_size and text else 0
        width, height = text_width + icon_size + gap, max(text_height, icon_size)
        padx, pady = (max(0, self.winfo_pixels(options[key])) for key in ("padx", "pady"))
        request = (max(1, width + 2 * padx), max(1, height + 2 * pady))
        if request != self._request_size:
            self._request_size = request
            tk.Canvas.configure(self, width=request[0], height=request[1])
        anchor = str(options["anchor"])
        x = padx if "w" in anchor else self.winfo_width() - padx - width if "e" in anchor else (self.winfo_width() - width) / 2
        y = pady if "n" in anchor else self.winfo_height() - pady - height if "s" in anchor else (self.winfo_height() - height) / 2
        self.coords(self._text_item, x + icon_size + gap - bounds[0], y + (height - text_height) / 2 - bounds[1])
        if icon_size:
            self._svg_image = photo(self, self._svg_name, color, icon_size)
            if self._icon_item is None:
                self._icon_item = self.create_image(0, 0, anchor="nw")
            self.coords(self._icon_item, x, y + (height - icon_size) / 2)
            self.itemconfigure(self._icon_item, image=self._svg_image, state="normal")
        elif self._icon_item is not None:
            self.itemconfigure(self._icon_item, state="hidden")
