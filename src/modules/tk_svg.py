"""Render the local action SVGs in Tk before Qt or image packages are installed.

The small, reviewed asset subset is stroke-only SVG paths/circles. Rasterization
is bounded and cached; the SVG remains the sole geometry source on both hosts.
"""
from __future__ import annotations

import base64
from functools import lru_cache
import math
import re
import struct
import tkinter as tk
import tkinter.font as tkfont
import xml.etree.ElementTree as ET
import zlib

from src.modules.svg_icons import svg_source, action_name
from src.modules.ui_palette import contrast_ratio, readable_foreground

_TOKEN = re.compile(r'[a-zA-Z]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?')
_COUNTS = {'M': 2, 'L': 2, 'H': 1, 'V': 1, 'C': 6, 'S': 4, 'Q': 4, 'T': 2, 'A': 7}


def _arc(start, args, end):
    rx, ry, rotation, large, sweep = args[:5]
    rx, ry = abs(rx), abs(ry)
    if not rx or not ry or start == end:
        return [end]
    phi = math.radians(rotation % 360)
    co, si = math.cos(phi), math.sin(phi)
    dx, dy = (start[0]-end[0])/2, (start[1]-end[1])/2
    x, y = co*dx+si*dy, -si*dx+co*dy
    factor = x*x/(rx*rx)+y*y/(ry*ry)
    if factor > 1:
        rx *= math.sqrt(factor); ry *= math.sqrt(factor)
    numerator = max(0, rx*rx*ry*ry-rx*rx*y*y-ry*ry*x*x)
    denominator = rx*rx*y*y+ry*ry*x*x
    factor = (-1 if bool(large) == bool(sweep) else 1) * math.sqrt(numerator / denominator) if denominator else 0
    cx, cy = factor*rx*y/ry, -factor*ry*x/rx
    center = (co*cx-si*cy+(start[0]+end[0])/2, si*cx+co*cy+(start[1]+end[1])/2)
    first = math.atan2((y-cy)/ry, (x-cx)/rx)
    last = math.atan2((-y-cy)/ry, (-x-cx)/rx)
    delta = (last-first) % (2*math.pi)
    if not sweep:
        delta -= 2*math.pi
    steps = max(4, math.ceil(abs(delta)*8))
    return [(center[0]+co*rx*math.cos(first+delta*i/steps)-si*ry*math.sin(first+delta*i/steps),
             center[1]+si*rx*math.cos(first+delta*i/steps)+co*ry*math.sin(first+delta*i/steps))
            for i in range(1, steps+1)]


def _path(data):
    tokens = _TOKEN.findall(data)
    if len(tokens) > 2048:
        raise ValueError('SVG path exceeds its token budget')
    index, command, position, start, previous, control = 0, '', (0., 0.), (0., 0.), '', None
    paths, points = [], []
    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]; index += 1
        op = command.upper()
        if op == 'Z':
            points.append(start)
            if points: paths.append(points)
            points, position, command, previous, control = [], start, '', 'Z', None
            continue
        if op not in _COUNTS:
            raise ValueError('Unsupported SVG command')
        count = _COUNTS[op]
        args = list(map(float, tokens[index:index+count])); index += count
        if len(args) != count:
            raise ValueError('Incomplete SVG command')
        relative = command.islower()
        def point(x, y):
            return (x+position[0], y+position[1]) if relative else (x, y)
        endpoint = position
        if op in ('M', 'L', 'T'):
            endpoint = point(*args)
        elif op == 'H':
            endpoint = (args[0]+position[0] if relative else args[0], position[1])
        elif op == 'V':
            endpoint = (position[0], args[0]+position[1] if relative else args[0])
        else:
            endpoint = point(*args[-2:])
        next_control = None
        if op == 'M':
            if points: paths.append(points)
            points, start = [endpoint], endpoint
            command = 'l' if relative else 'L'
        elif op in ('L', 'H', 'V'):
            if not points: points = [position]
            points.append(endpoint)
        elif op == 'A':
            if not points: points = [position]
            points.extend(_arc(position, args, endpoint))
        else:
            if not points: points = [position]
            if op in ('C', 'S'):
                c1 = point(*args[:2]) if op == 'C' else (
                    (2*position[0]-control[0], 2*position[1]-control[1])
                    if previous in ('C', 'S') and control else position)
                c2 = point(*args[-4:-2]); next_control = c2
                for step in range(1, 17):
                    t, u = step/16, 1-step/16
                    points.append(tuple(u**3*position[k]+3*u*u*t*c1[k]+3*u*t*t*c2[k]+t**3*endpoint[k] for k in (0, 1)))
            else:
                c = point(*args[:2]) if op == 'Q' else (
                    (2*position[0]-control[0], 2*position[1]-control[1])
                    if previous in ('Q', 'T') and control else position)
                next_control = c
                for step in range(1, 17):
                    t, u = step/16, 1-step/16
                    points.append(tuple(u*u*position[k]+2*u*t*c[k]+t*t*endpoint[k] for k in (0, 1)))
        position, previous, control = endpoint, op, next_control
    if points: paths.append(points)
    return paths


@lru_cache(maxsize=192)
def png_data(name, color, size):
    size = max(8, min(64, int(size)))
    root = ET.fromstring(svg_source(name, color))
    paths = []
    for child in root:
        tag = child.tag.rsplit('}', 1)[-1]
        if tag == 'path':
            paths.extend(_path(child.attrib['d']))
        elif tag == 'circle':
            cx, cy, radius = (float(child.attrib[k]) for k in ('cx', 'cy', 'r'))
            paths.append([(cx+radius*math.cos(i*math.pi/24), cy+radius*math.sin(i*math.pi/24)) for i in range(49)])
        else:
            raise ValueError('Unsupported action SVG element')
    scale, width = size*3/24, size*3
    radius = float(root.attrib.get('stroke-width', '1.65')) * scale/2
    mask = bytearray(width*width)
    for points in paths:
        for first, last in zip(points, points[1:]):
            ax, ay = first[0]*scale, first[1]*scale
            bx, by = last[0]*scale, last[1]*scale
            dx, dy = bx-ax, by-ay
            length = dx*dx+dy*dy
            for y in range(max(0, math.floor(min(ay,by)-radius)), min(width, math.ceil(max(ay,by)+radius))):
                for x in range(max(0, math.floor(min(ax,bx)-radius)), min(width, math.ceil(max(ax,bx)+radius))):
                    t = max(0, min(1, ((x+.5-ax)*dx+(y+.5-ay)*dy)/length)) if length else 0
                    if (x+.5-ax-t*dx)**2+(y+.5-ay-t*dy)**2 <= radius*radius:
                        mask[y*width+x] = 1
    rgb = bytes(int(color[i:i+2], 16) for i in (1,3,5))
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            count = sum(mask[(y*3+dy)*width+x*3+dx] for dy in range(3) for dx in range(3))
            rows.extend(rgb); rows.append(round(count*255/9))
    def chunk(kind, data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    return (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',size,size,8,6,0,0,0))+
            chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b''))


def photo(master, name, color, size=16):
    root = master._root()
    cache = getattr(root, '_svg_photos', None)
    if cache is None:
        cache = root._svg_photos = {}
    key = (name, color, size)
    if key not in cache:
        if len(cache) >= 128:
            cache.clear()
        cache[key] = tk.PhotoImage(master=root, data=base64.b64encode(png_data(*key)), format='png')
    return cache[key]


class _SvgWidget:
    def set_icon(self, name):
        self._svg_name = name
        self._refresh_svg()

    def _refresh_svg(self):
        name = getattr(self, '_svg_name', '')
        if not name:
            return
        if isinstance(self, tk.Label) and not self.cget('text') and not getattr(self, '_svg_icon_only', False):
            self._svg_base.configure(self, image='')
            return
        disabled = str(self.cget('state')) == 'disabled' if isinstance(self, tk.Button) else False
        active = getattr(self, '_svg_hovering', False) and not disabled
        color = str(self.cget('disabledforeground' if disabled else 'activeforeground' if active else 'foreground'))
        background = str(self.cget('activebackground' if active else 'background'))
        if not re.fullmatch(r'#[a-fA-F0-9]{6}', color):
            color = '#' + ''.join(f'{v//257:02x}' for v in self.winfo_rgb(color))
        if not re.fullmatch(r'#[a-fA-F0-9]{6}', background):
            background = '#' + ''.join(f'{v//257:02x}' for v in self.winfo_rgb(background))
        if contrast_ratio(color, background) < 3:
            color = readable_foreground(background)
        size = max(12, min(32, int(self.tk.call('font','metrics',self.cget('font'),'-linespace'))))
        self._svg_image = photo(self, name, color, size)
        self._svg_base.configure(self, image=self._svg_image, compound='left')

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}; cnf = None
        if 'text' in kwargs and isinstance(self, tk.Button):
            candidate = action_name(kwargs['text'])
            if candidate:
                self._svg_name = candidate
        result = self._svg_base.configure(self, cnf, **kwargs)
        if cnf is None and kwargs:
            self._refresh_svg()
        return result

    config = configure


class SvgButton(_SvgWidget, tk.Button):
    _svg_base = tk.Button

    def __init__(self, master=None, *, icon_name='', **kwargs):
        self._svg_name = icon_name or action_name(kwargs.get('text',''))
        tk.Button.__init__(self, master, **kwargs)
        self.bind('<Enter>', lambda e: self._svg_hover(True), add='+')
        self.bind('<Leave>', lambda e: self._svg_hover(False), add='+')
        self._refresh_svg()

    def _svg_hover(self, active):
        self._svg_hovering = active
        self._refresh_svg()


class SvgLabel(_SvgWidget, tk.Label):
    _svg_base = tk.Label

    def __init__(self, master=None, *, icon_name='', icon_only=False, **kwargs):
        self._svg_icon_only = icon_only
        self._svg_name = icon_name
        tk.Label.__init__(self, master, **kwargs)
        self._refresh_svg()


class SvgListbox(tk.Listbox):
    """Keep native list selection/scrolling and paint an SVG in a sibling rail."""

    def __init__(self, master=None, *, icon_for=None, **kwargs):
        self._icon_for = icon_for
        self._rail_after = None
        self._external_scroll = kwargs.pop('yscrollcommand', None)
        super().__init__(master, **kwargs)
        self._rail = tk.Canvas(master, width=24, highlightthickness=0, bd=0, bg=self.cget('background'))
        self._rail.pack(side='left', fill='y')
        self._rail_images = []
        super().configure(yscrollcommand=self._scroll_changed)
        self._rail_bindtag = 'SvgRail'+str(id(self))
        tags = self.bindtags()
        self.bindtags((tags[0], self._rail_bindtag, *tags[1:]))
        for event in ('<Configure>', '<<ListboxSelect>>', '<KeyRelease>', '<ButtonRelease-1>'):
            self.bind_class(self._rail_bindtag, event, self._schedule_rail)
        self.bind('<Destroy>', self._close_rail, add='+')
        self._rail.bind('<Button-1>', self._rail_click)
        self._rail.bind('<MouseWheel>', lambda e: self.event_generate('<MouseWheel>', delta=e.delta))
        self._rail.bind('<Button-4>', lambda e: self.yview_scroll(-1, 'units'))
        self._rail.bind('<Button-5>', lambda e: self.yview_scroll(1, 'units'))

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}; cnf = None
        if 'yscrollcommand' in kwargs:
            self._external_scroll = kwargs.pop('yscrollcommand')
        result = super().configure(cnf, **kwargs)
        if hasattr(self, '_rail') and kwargs:
            self._schedule_rail()
        return result

    config = configure

    def _scroll_changed(self, first, last):
        if callable(self._external_scroll):
            self._external_scroll(first, last)
        elif self._external_scroll:
            self.tk.call(self._external_scroll, first, last)
        self._schedule_rail()

    def _schedule_rail(self, _event=None):
        if self._rail_after is None and hasattr(self, '_rail'):
            self._rail_after = self.after_idle(self._draw_rail)

    def _draw_rail(self):
        self._rail_after = None
        self._rail.delete('all'); self._rail_images.clear()
        self._rail.configure(bg=self.cget('background'))
        selected = set(self.curselection())
        size = max(12, min(32, int(self.tk.call('font','metrics',self.cget('font'),'-linespace'))))
        self._rail.configure(width=size+8)
        start = self.nearest(0)
        for row in range(start, min(self.size(), start+128)):
            box = self.bbox(row)
            if not box:
                break
            _, y, _, height = box
            if y > self.winfo_height():
                break
            name = self._icon_for(row) if self._icon_for else 'package'
            if not name:
                continue
            bg = self.cget('selectbackground' if row in selected else 'background')
            fg = self.cget('selectforeground' if row in selected else 'foreground')
            bg = '#'+''.join(f'{v//257:02x}' for v in self.winfo_rgb(bg))
            fg = '#'+''.join(f'{v//257:02x}' for v in self.winfo_rgb(fg))
            if contrast_ratio(fg, bg) < 3: fg = readable_foreground(bg)
            if row in selected:
                self._rail.create_rectangle(0,y,size+8,y+height,fill=bg,outline='')
            image = photo(self, name, fg, size)
            self._rail_images.append(image)
            self._rail.create_image((size+8)/2,y+height/2,image=image)

    def _rail_click(self, event):
        if not self.size(): return
        self.focus_set()
        self.event_generate('<Button-1>', x=4, y=event.y, state=event.state)
        self.event_generate('<ButtonRelease-1>', x=4, y=event.y, state=event.state)
        self._schedule_rail()

    def _close_rail(self, event):
        if event.widget is self:
            if self._rail_after is not None:
                self.after_cancel(self._rail_after)
                self._rail_after = None
            for sequence in ('<Configure>', '<<ListboxSelect>>', '<KeyRelease>', '<ButtonRelease-1>'):
                self.unbind_class(self._rail_bindtag, sequence)
            self._rail.destroy()


class TkLogGutter(tk.Canvas):
    """Status rail for native setup text; selected/copied text remains literal."""

    def __init__(self, editor):
        self.editor = editor
        self._size = max(12, min(32, int(editor.tk.call('font','metrics',editor.cget('font'),'-linespace'))))
        super().__init__(editor, width=self._size+6, bd=0, highlightthickness=0, bg=editor.cget('background'))
        self._pending = None
        self._images = []
        self.place(x=0,y=0,width=self._size+6,relheight=1)
        self.bind('<MouseWheel>', lambda e: editor.event_generate('<MouseWheel>', delta=e.delta))
        self.bind('<Button-4>', lambda e: editor.yview_scroll(-1,'units'))
        self.bind('<Button-5>', lambda e: editor.yview_scroll(1,'units'))
        for tag in editor.tag_names():
            if tag != 'sel': editor.tag_configure(tag,lmargin1=self._size+8,lmargin2=self._size+8)
        self._scroll = editor.cget('yscrollcommand')
        editor.configure(yscrollcommand=self._scrolled)
        editor.bind('<Configure>',self._schedule,add='+')
        editor.bind('<Destroy>',self._close,add='+')

    def _scrolled(self, first, last):
        if self._scroll: self.tk.call(self._scroll,first,last)
        self._schedule()

    def _schedule(self, _event=None):
        if self._pending is None:
            self._pending = self.after_idle(self._draw)

    def _draw(self):
        from src.modules.svg_icons import status_name
        self._pending = None
        self.delete('all'); self._images.clear()
        line = int(self.editor.index('@0,0').split('.')[0])
        last = int(self.editor.index('@0,%d'%self.editor.winfo_height()).split('.')[0])
        for number in range(line,min(last+1,line+128)):
            index = f'{number}.0'
            box = self.editor.dlineinfo(index)
            if not box: continue
            _,y,_,height,_ = box
            tags = self.editor.tag_names(index)
            tag = next((tag for tag in reversed(tags) if status_name(tag)),'')
            name = status_name(tag)
            if not name: continue
            color = self.editor.tag_cget(tag,'foreground') or self.editor.cget('foreground')
            color = '#'+''.join(f'{v//257:02x}' for v in self.winfo_rgb(color))
            image = photo(self,name,color,self._size)
            self._images.append(image)
            self.create_image((self._size+6)/2,y+min(height,self.editor.tk.call('font','metrics',self.editor.cget('font'),'-linespace'))/2,image=image)

    def _close(self, event):
        if event.widget is self.editor and self._pending is not None:
            self.after_cancel(self._pending); self._pending = None
