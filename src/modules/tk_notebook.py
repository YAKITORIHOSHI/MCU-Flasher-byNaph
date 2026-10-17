"""A material-painted strip over an otherwise native ttk Notebook.

Tk themes fill a notebook's unused tab strip with a flat color. A child canvas
continues the owning static material there, while the real Notebook still owns
page geometry, tab state, pointer activation, focus and keyboard traversal.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont, ttk

from src.modules.tk_material import _refresh_children, material_location


class GlassNotebook(ttk.Notebook):
    """Native tabs with rounded state rims over a shared, aligned material."""

    def __init__(self, master, palette, **options):
        self.palette = palette
        self._paint_id = None
        self._closed = False
        self._hover = None
        self._geometry_key = None
        self._tab_boxes = []
        self._skin_photos = []
        self._client_key = None
        super().__init__(master, **options)
        self._style = ttk.Style(self)
        self._style_name = f"Material{id(self)}.TNotebook"
        self._client_image = tk.PhotoImage(master=self, width=1, height=1)
        element = f"Material{id(self)}.client"
        self._style.element_create(element, "image", self._client_image,
                                   width=0, height=0, border=0, sticky="nsew")
        self._style.layout(self._style_name, [(element, {"sticky": "nsew"})])
        self._style.configure(self._style_name, borderwidth=0, padding=0)
        # An image element tiles to fill the client. Keep the native client
        # until the first full-sized crop is ready; tiling the 1px placeholder
        # across a new page can monopolize Tcl's first event-loop dispatch.
        self._style_ready = False
        self._strip = tk.Canvas(self, borderwidth=0, highlightthickness=0,
                                takefocus=0, background=palette.WINDOW_BG)
        self._font = tkfont.Font(root=self, font="TkDefaultFont")
        # Keep these hooks independent from callers' ordinary widget bindings.
        self._paint_tag = f"GlassNotebook:{id(self)}"
        self.bindtags((self._paint_tag, *self.bindtags()))
        self._paint_bindings = {
            sequence: self._bind(("bind", self._paint_tag), sequence, callback, False, True)
            for sequence, callback in (("<Configure>", self._queue), ("<Map>", self._queue),
                                       ("<<NotebookTabChanged>>", self._queue),
                                       ("<<ThemeChanged>>", self._queue),
                                       ("<FocusIn>", self._queue), ("<FocusOut>", self._queue),
                                       ("<Destroy>", self._close))
        }
        for sequence in ("<Motion>", "<Enter>", "<Leave>", "<ButtonPress>",
                         "<ButtonRelease>", "<MouseWheel>"):
            self._strip.bind(sequence, self._relay)
        self._queue()

    def _queue(self, *_args):
        if not self._closed and self._paint_id is None:
            self._paint_id = self.after_idle(self.refresh_material)

    def _relay(self, event):
        kind = str(event.type)
        if kind in ("4", "ButtonPress", "5", "ButtonRelease"):
            prefix = "ButtonPress" if kind in ("4", "ButtonPress") else "ButtonRelease"
            self.event_generate(f"<{prefix}-{event.num}>", x=event.x, y=event.y, state=event.state)
        elif kind in ("38", "MouseWheel"):
            self.event_generate("<MouseWheel>", x=event.x, y=event.y,
                                delta=event.delta, state=event.state)
        else:
            names = {"6": "Motion", "7": "Enter", "8": "Leave"}
            name = names.get(kind, kind)
            self.event_generate(f"<{name}>", x=event.x, y=event.y, state=event.state)
        hovered = None
        if kind not in ("8", "Leave"):
            hovered = self._index_at(event.x, event.y)
        if hovered != self._hover:
            self._hover = hovered
            self._queue()

    def _index_at(self, x, y):
        try:
            return self.index(f"@{x},{y}")
        except tk.TclError:
            return None

    def _boxes(self, width, height):
        # Ask the native geometry engine once after a size/text/style change.
        # These hit boxes also account for compressed tabs at narrow widths.
        result = []
        y, x, gap = max(0, height // 2), 0, 0
        while x < width and (not result or gap < 16):
            index = self._index_at(x, y)
            if index is None:
                gap += 1
                x += 1
                continue
            gap, left = 0, x
            while x < width and self._index_at(x, y) == index:
                x += 1
            center, top, bottom = (left+x)//2, y, y+1
            while top > 0 and self._index_at(center, top-1) == index:
                top -= 1
            while bottom < height and self._index_at(center, bottom) == index:
                bottom += 1
            result.append((index, left, top, x, bottom))
        return result

    def refresh_material(self):
        if self._closed:
            return
        if self._paint_id is not None:
            self.after_cancel(self._paint_id)
            self._paint_id = None
        # Native geometry can settle while the toplevel is withdrawn. Do not
        # place/raise the overlay or repaint its pages until it is visible;
        # <Map> requests the current material when the window is shown.
        if not self.winfo_viewable():
            return
        width, full_height = self.winfo_width(), self.winfo_height()
        selected = self.select()
        if width < 2 or full_height < 2 or not selected:
            self._strip.place_forget()
            return
        page = self.nametowidget(selected)
        strip_height = min(full_height, max(1, page.winfo_y()))
        if strip_height < 2:
            return
        self._strip.place(x=0, y=0, width=width, height=strip_height)
        self.tk.call("raise", self._strip)
        location = material_location(self)
        self._strip.delete("all")
        self._material_photo = location[0] if location else None
        if location:
            source, x, y = location
            self._strip.create_image(-x, -y, image=source, anchor="nw")
            # Only the current client crop is retained, and its image element
            # requests zero size so native page minima cannot grow with it.
            client_key = (str(source), x, y, width, full_height, strip_height, self.palette.WINDOW_BG)
            if client_key != self._client_key:
                self._client_key = client_key
                self._client_image.configure(width=width, height=max(1, full_height-strip_height))
                self._client_image.put(self.palette.WINDOW_BG, to=(0, 0, width, max(1, full_height-strip_height)))
                left, top = max(0, x), max(0, y+strip_height)
                right, bottom = min(source.width(), x+width), min(source.height(), y+full_height)
                if left < right and top < bottom:
                    self.tk.call(self._client_image, "copy", source, "-from", left, top, right, bottom,
                                 "-to", left-x, top-y-strip_height, "-compositingrule", "overlay")
            if not self._style_ready:
                self._style_ready = True
                self.configure(style=self._style_name)
        tabs = tuple((tab, tuple(sorted(self.tab(tab).items()))) for tab in self.tabs())
        font = self._style.lookup("TNotebook.Tab", "font") or "TkDefaultFont"
        key = (width, strip_height, tabs, selected, str(font))
        if key != self._geometry_key:
            self._geometry_key = key
            self._tab_boxes = self._boxes(width, strip_height)
        self._font.configure(**tkfont.Font(root=self, font=font).actual())
        self._skin_photos = []
        from src.modules.tk_glass import glass_photo, material_ink, ui_scale
        from src.modules.ui_palette import mix
        p = self.palette
        scale = ui_scale(self)
        current = self.index(selected)
        for index, left, top, right, bottom in self._tab_boxes:
            values = self.tab(index)
            disabled = values["state"] == "disabled"
            active = index == self._hover and not disabled
            chosen = index == current
            # Match the monitor tabs: every tab has a quiet rounded rim, with
            # a brighter hover rim and a cyan selected rim/bottom accent.
            upper = mix(p.BG_MID, p.CYAN, .06) if chosen else p.BG_HOVER if active else p.BG_MID
            lower = p.BG_MID if chosen or active else p.BG_DARK
            rim = p.CYAN if chosen else p.BORDER_LIT if active else p.BORDER
            skin_right = max(left+1, right-round(4*scale))
            skin_bottom = max(top+1, bottom-round(4*scale))
            image = glass_photo(self, skin_right-left, skin_bottom-top, upper, lower,
                                rim, p.WINDOW_BG, round(9*scale), False, transparent=True)
            self._skin_photos.append(image)
            self._strip.create_image(left, top, image=image, anchor="nw", tags=("tab-skin", f"tab-{index}"))
            if chosen:
                inset = min(round(9*scale), (skin_right-left)/2)
                self._strip.create_line(left+inset, skin_bottom-1, skin_right-inset,
                                        skin_bottom-1, fill=p.CYAN, width=max(2, round(2*scale)))
            color = p.TEXT_DIM if disabled or not (chosen or active) else p.TEXT_BRIGHT
            bounds = (upper, lower)
            color = material_ink(color, *bounds)
            text = self._strip.create_text((left+skin_right)/2, (top+skin_bottom)/2,
                                          text=values["text"], font=self._font,
                                          fill=color)
            label = str(values["text"])
            underline = int(values["underline"])
            if 0 <= underline < len(label):
                start = (left+skin_right-self._font.measure(label))/2 + self._font.measure(label[:underline])
                line_y = (top+skin_bottom+self._font.metrics("ascent")-self._font.metrics("descent"))/2+1
                self._strip.create_line(start, line_y, start+self._font.measure(label[underline]),
                                        line_y, fill=color)
            if chosen and self.focus_get() is self:
                bounds = self._strip.bbox(text)
                if bounds:
                    x1, y1, x2, y2 = bounds
                    self._strip.create_rectangle(x1-2, y1-2, x2+2, y2+2,
                                                 outline=p.CYAN, dash=(1, 2))
        for child in self.winfo_children():
            if child is not self._strip:
                refresh = getattr(child, "refresh_material", None)
                refresh() if refresh is not None else _refresh_children(child)

    def add(self, child, **options):
        result = super().add(child, **options)
        self._queue()
        return result

    def insert(self, pos, child, **options):
        result = super().insert(pos, child, **options)
        self._queue()
        return result

    def forget(self, tab_id):
        result = super().forget(tab_id)
        self._queue()
        return result

    def hide(self, tab_id):
        result = super().hide(tab_id)
        self._queue()
        return result

    def tab(self, tab_id, option=None, **options):
        result = super().tab(tab_id, option, **options)
        if options:
            self._queue()
        return result

    def _close(self, event):
        if event.widget is not self or self._closed:
            return
        self._closed = True
        if self._paint_id is not None:
            self.after_cancel(self._paint_id)
            self._paint_id = None
        for sequence, command in self._paint_bindings.items():
            self.tk.call("bind", self._paint_tag, sequence, "")
            self.deletecommand(command)
        self._paint_bindings.clear()
        self._skin_photos.clear()
        self._material_photo = None
        self._client_image = None
