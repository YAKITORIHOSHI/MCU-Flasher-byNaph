"""Continue a static glass material behind a native, selectable package list.

The Listbox remains the authoritative model and event target. A child canvas
covers its painted area; it forwards pointer input to the native bindings,
while keyboard input, focus, selection and scrolling stay on the Listbox.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

from src.modules.tk_material import _owner
from src.modules.tk_svg import SvgListbox, photo


class GlassListbox(SvgListbox):
    """An ``SvgListbox`` whose unselected rows share the enclosing glass.

    The normal Listbox API and sibling SVG rail are retained. Material owners
    may call ``refresh_material`` after repainting their shared image. Work is
    coalesced to idle and limited to the 128 visible rows used by SvgListbox.
    """

    def __init__(self, master=None, **options):
        self._material_after = None
        self._material_closed = False
        self._surface_photo = None
        self._rail_photo = None
        self._listvariable_trace = None
        self._last_pointer_press = None
        super().__init__(master, **options)
        self._glass_font = tkfont.Font(root=self, font=self.cget("font"))
        self._surface = tk.Canvas(self, takefocus=0, borderwidth=0,
                                  highlightthickness=0, background=self.cget("background"))
        self._surface.place(x=0, y=0, relwidth=1, relheight=1, bordermode="ignore")
        # A distinct tag keeps painting/cleanup alive when callers replace the
        # ordinary widget bindings for selection or package actions.
        self._material_bindtag = f"GlassListbox:{self}:{id(self)}"
        tags = self.bindtags()
        self.bindtags((tags[0], self._material_bindtag, *tags[1:]))
        self._material_bindings = {
            sequence: self._bind(("bind", self._material_bindtag), sequence, callback, False, True)
            for sequence, callback in (
                ("<Configure>", self._schedule_rail), ("<Map>", self._schedule_rail),
                ("<KeyPress>", self._schedule_rail), ("<FocusIn>", self._schedule_rail),
                ("<FocusOut>", self._schedule_rail), ("<Destroy>", self._close_material))
        }
        # Preserve original coordinates/state/time on the native event target.
        # A second press needs explicit double dispatch: alternating canvas and
        # listbox events otherwise resets Tk's target-specific click history.
        for sequence in ("<ButtonPress-1>", "<ButtonRelease-1>", "<B1-Motion>",
                         "<ButtonPress-2>", "<ButtonRelease-2>", "<B2-Motion>",
                         "<ButtonPress-3>", "<ButtonRelease-3>",
                         "<Enter>", "<Leave>", "<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._surface.bind(sequence, lambda event, seq=sequence: self._forward_pointer(event, seq))
        self._watch_listvariable()
        self._schedule_rail()

    def _schedule_rail(self, _event=None):
        if self._material_closed:
            return
        super()._schedule_rail(_event)
        if hasattr(self, "_surface") and self._material_after is None:
            self._material_after = self.after_idle(self._draw_surface)

    def refresh_material(self):
        self._schedule_rail()

    def _watch_listvariable(self):
        if self._listvariable_trace is not None:
            name, command = self._listvariable_trace
            try:
                self.tk.call("trace", "remove", "variable", name, "write", command)
            except tk.TclError:
                pass
            self.deletecommand(command)
            self._listvariable_trace = None
        name = self.cget("listvariable")
        if name:
            command = self._register(lambda *_args: self._schedule_rail())
            self.tk.call("trace", "add", "variable", name, "write", command)
            self._listvariable_trace = (name, command)

    def configure(self, cnf=None, **options):
        if isinstance(cnf, dict):
            options = {**cnf, **options}
            cnf = None
        result = super().configure(cnf, **options)
        if options and hasattr(self, "_surface"):
            if "listvariable" in options:
                self._watch_listvariable()
            self._schedule_rail()
        return result

    config = configure

    def _forward_pointer(self, event, sequence):
        if self._material_closed:
            return "break"
        x = event.x + self._surface.winfo_x()
        y = event.y + self._surface.winfo_y()
        details = {"x": x, "y": y, "state": event.state, "time": event.time}
        if sequence == "<MouseWheel>":
            details["delta"] = event.delta
        # Motion's state determines its B1 binding. Passing the generic event
        # preserves the native selection/auto-scroll procedures and their grab.
        if sequence in ("<B1-Motion>", "<B2-Motion>"):
            sequence = "<Motion>"
        double = False
        if sequence == "<ButtonPress-1>":
            current = (int(event.time), x, y, int(event.state) & ~256)
            previous = self._last_pointer_press
            double = bool(previous and ((current[0] - previous[0]) & 0xffffffff) <= 500
                          and abs(current[1] - previous[1]) <= 5
                          and abs(current[2] - previous[2]) <= 5
                          and current[3] == previous[3])
            self._last_pointer_press = current
        if double:
            self._dispatch_double_press(details)
        else:
            self.event_generate(sequence, **details)
        self._schedule_rail()
        return "break"

    def _dispatch_double_press(self, details):
        # Tk disallows event_generate('<Double-Button-1>'). Temporarily map the
        # existing double-click binding scripts onto an ordinary native press.
        # This retains %W, button/type/coordinates and 'break' semantics, and
        # avoids replaying the single-click selection or application callback.
        original = self.bindtags()
        temporary = []
        bindings = []
        for index, tag in enumerate(original):
            target = f"{self._material_bindtag}:double:{index}"
            for sequence in self.tk.splitlist(self.tk.call("bind", tag)):
                if "Double-" not in sequence or not sequence.endswith("Button-1>"):
                    continue
                mapped = sequence.replace("Double-", "")
                script = self.tk.call("bind", tag, sequence)
                self.tk.call("bind", target, mapped, script)
                bindings.append((target, mapped))
            temporary.append(target)
        self.bindtags(tuple(temporary))
        try:
            self.event_generate("<ButtonPress-1>", **details)
        finally:
            if self.winfo_exists():
                self.bindtags(original)
            for target, sequence in bindings:
                self.tk.call("bind", target, sequence, "")

    def _paint_background(self, canvas):
        canvas.configure(background=self.cget("background"))
        owner = _owner(canvas)
        image = getattr(owner, "_material_image", None)
        if canvas is self._surface:
            self._surface_photo = image
        else:
            self._rail_photo = image
        if image is not None:
            canvas.create_image(owner.winfo_rootx() - canvas.winfo_rootx(),
                                owner.winfo_rooty() - canvas.winfo_rooty(),
                                anchor="nw", image=image)
        return owner

    def _hex(self, color):
        return "#" + "".join(f"{part // 257:02x}" for part in self.winfo_rgb(color))

    def _row_colors(self, row, owner):
        from src.modules.tk_glass import material_ink

        selected = bool(self.selection_includes(row))
        foreground = self.itemcget(row, "selectforeground" if selected else "foreground")
        foreground = foreground or self.cget("selectforeground" if selected else "foreground")
        if str(self.cget("state")) == "disabled":
            foreground = self.cget("disabledforeground")
        foreground = self._hex(foreground)
        configured_background = self.itemcget(row, "selectbackground" if selected else "background")
        background = configured_background or self.cget("selectbackground" if selected else "background")
        background = self._hex(background)
        paint_background = selected or bool(configured_background)
        bounds = ((background, background) if paint_background
                  else getattr(owner, "_material_bounds", (background, background)))
        return paint_background, background, material_ink(foreground, *bounds)

    def _visible_rows(self):
        if not self.size():
            return
        start = self.nearest(0)
        for row in range(start, min(self.size(), start + 128)):
            box = self.bbox(row)
            if box is None:
                break
            if box[1] > self.winfo_height():
                break
            yield row, box

    def _draw_surface(self):
        self._material_after = None
        if self._material_closed or not self.winfo_exists():
            return
        # Continue the material through the native border as well. Paint the
        # original focus highlight only while this Listbox owns keyboard focus.
        self._surface.place_configure(x=0, y=0, width=0, height=0,
                                      relwidth=1, relheight=1)
        self._surface.delete("all")
        owner = self._paint_background(self._surface)
        actual = self.tk.splitlist(self.tk.call("font", "actual", self.cget("font")))
        self._glass_font.configure(**{key.lstrip("-"): value for key, value in zip(actual[::2], actual[1::2])})
        for row, (x, y, _width, height) in self._visible_rows():
            paint_background, background, foreground = self._row_colors(row, owner)
            if paint_background:
                self._surface.create_rectangle(0, y, self.winfo_width(), y + height,
                                               fill=background, outline="", tags="selection")
            self._surface.create_text(x, y + height / 2, text=self.get(row),
                                      font=self._glass_font, fill=foreground, anchor="w")
        highlight = self.winfo_pixels(self.cget("highlightthickness"))
        if highlight > 0 and str(self.tk.call("focus")) == str(self):
            half = highlight / 2
            self._surface.create_rectangle(half, half, self.winfo_width() - half,
                                           self.winfo_height() - half, width=highlight,
                                           outline=self.cget("highlightcolor"), tags="focus-ring")

    def _draw_rail(self):
        self._rail_after = None
        if self._material_closed or not self.winfo_exists():
            return
        self._rail.delete("all")
        self._rail_images.clear()
        size = max(12, min(32, int(self.tk.call("font", "metrics", self.cget("font"), "-linespace"))))
        self._rail.configure(width=size + 8)
        owner = self._paint_background(self._rail)
        for row, (_x, y, _width, height) in self._visible_rows():
            paint_background, background, foreground = self._row_colors(row, owner)
            if paint_background:
                self._rail.create_rectangle(0, y, size + 8, y + height, fill=background, outline="")
            name = self._icon_for(row) if self._icon_for else "package"
            if name:
                image = photo(self, name, foreground, size)
                self._rail_images.append(image)
                self._rail.create_image((size + 8) / 2, y + height / 2, image=image)

    def insert(self, index, *elements):
        result = super().insert(index, *elements)
        self._schedule_rail()
        return result

    def delete(self, first, last=None):
        result = super().delete(first, last)
        self._schedule_rail()
        return result

    def itemconfigure(self, index, cnf=None, **options):
        result = super().itemconfigure(index, cnf, **options)
        if options or isinstance(cnf, dict):
            self._schedule_rail()
        return result

    itemconfig = itemconfigure

    def activate(self, index):
        result = super().activate(index)
        self._schedule_rail()
        return result

    def selection_set(self, first, last=None):
        result = super().selection_set(first, last)
        self._schedule_rail()
        return result

    select_set = selection_set

    def selection_clear(self, first, last=None):
        result = super().selection_clear(first, last)
        self._schedule_rail()
        return result

    select_clear = selection_clear

    def see(self, index):
        result = super().see(index)
        self._schedule_rail()
        return result

    def xview(self, *args):
        result = super().xview(*args)
        if args:
            self._schedule_rail()
        return result

    def xview_moveto(self, fraction):
        super().xview_moveto(fraction)
        self._schedule_rail()

    def xview_scroll(self, number, what):
        super().xview_scroll(number, what)
        self._schedule_rail()

    def scan_dragto(self, x, y):
        result = super().scan_dragto(x, y)
        self._schedule_rail()
        return result

    def _close_material(self, event):
        if event.widget is not self or self._material_closed:
            return
        self._material_closed = True
        if self._material_after is not None:
            self.after_cancel(self._material_after)
            self._material_after = None
        if self._listvariable_trace is not None:
            name, command = self._listvariable_trace
            try:
                self.tk.call("trace", "remove", "variable", name, "write", command)
            except tk.TclError:
                pass
            self.deletecommand(command)
            self._listvariable_trace = None
        for sequence, command in self._material_bindings.items():
            self.tk.call("bind", self._material_bindtag, sequence, "")
            self.deletecommand(command)
        self._material_bindings.clear()
        self._surface_photo = self._rail_photo = None
