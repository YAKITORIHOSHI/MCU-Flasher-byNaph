"""Original circuit-etched vectors, independent of emoji and icon fonts.

The small chamfers, open terminals and routed strokes belong to the MCU
Flasher identity. Keep this geometry app-owned instead of copying icon packs.
"""
from __future__ import annotations

import re
from html import escape
from functools import lru_cache
from PySide6.QtCore import QByteArray, QSize, Qt, QRectF
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QPushButton
from main.core.theme import Theme

PATHS = {
    "brand": '<path d="M7 4h10l3 3v10l-3 3H7l-3-3V7l3-3ZM8 1v3m4-3v3m4-3v3M8 20v3m4-3v3m4-3v3M1 8h3m-3 4h3m-3 4h3m16-8h3m-3 4h3m-3 4h3"/><path d="M13 7l-5 6h4l-1 4 6-7h-4l1-3Z"/>',
    "compile": '<path d="m8 6-5 6 5 6m8-12 5 6-5 6M14 3l-4 18M2 3h3m14 18h3"/>',
    "upload": '<path d="M12 16V3m-4 4 4-4 4 4M4 14v5l2 2h12l2-2v-5M8 18h8"/>',
    "stop": '<path d="M7 4h10l3 3v10l-3 3H7l-3-3V7l3-3ZM9 9h6v6H9Z"/>',
    "clean": '<path d="m14 3-4 10M6 13h8l4 7H3l3-7Zm2 1-2 5m6-5 2 5M17 6h4m-2-2v4"/>',
    "save": '<path d="M4 3h12l5 5v11l-2 2H5l-2-2V4l1-1ZM7 3v6h9V3M7 21v-7h10v7M12 5h1"/>',
    "reload": '<path d="M20 9a8 8 0 1 0-1 8M20 3v6h-6M4 15H1"/>',
    "project": '<path d="M3 7V5h7l3 3h7l1 2v9l-2 2H5l-2-2V7ZM3 11h10m4 0h4"/>',
    "cloud": '<path d="M7 18H5a4 4 0 0 1-1-8 6 6 0 0 1 11-3 5 5 0 0 1 4 10h-2M12 21V12m-3 3 3-3 3 3"/>',
    "search": '<path d="m15 15 6 6m-2-2 2-2M5 4h7l4 4v5l-4 4H7l-4-4V8l2-4Z"/>',
    "settings": '<path d="M3 5h4m4 0h10M3 12h10m4 0h4M3 19h3m4 0h11M7 3h4v4H7ZM13 10h4v4h-4ZM6 17h4v4H6Z"/>',
    "setup": '<path d="M3 5h4m4 0h10M3 12h10m4 0h4M3 19h3m4 0h11M7 3h4v4H7ZM13 10h4v4h-4ZM6 17h4v4H6Z"/>',
    "ai": '<path d="M8 6h8l4 4v7l-3 3H7l-3-3v-7l4-4ZM12 2v4M1 12h3m16 0h3M8 11h1m6 0h1M9 15l3 2 3-2"/>',
    "console": '<path d="M3 7V4h18v16H3v-9m4-3 4 4-4 4m7 0h4"/>',
    "serial": '<path d="M2 12h4l3-8 6 16 3-8h4M2 8v1m20 6v1"/>',
    "devices": '<path d="M8 5h8l3 3v8l-3 3H8l-3-3V8l3-3ZM9 1v4m6-4v4M9 19v4m6-4v4M1 9h4m-4 6h4m14-6h4m-4 6h4M10 10h4v4h-4Z"/>',
    "alerts": '<path d="M5 17h14l-2-3V9l-2-4H9L7 9v5l-2 3Zm5 4h4M12 2v3"/>',
    "copy": '<path d="M9 8h9l3 3v8l-2 2H9l-1-1V9l1-1ZM16 8V3H5L3 5v11h5"/>',
    "clear": '<path d="M3 6h18M9 6V3h6v3M6 6v12l3 3h6l3-3V6M10 10v7m4-7v7"/>',
    "pause": '<path d="M6 4h3v16H6ZM15 4h3v16h-3Z"/>',
    "resume": '<path d="M6 4v16l14-8L6 4ZM3 9v6"/>',
    "modify": '<path d="m5 16 11-12 4 4L9 20H4v-4h1ZM13 7l4 4M3 23h8"/>',
    "download": '<path d="M12 3v13m-4-4 4 4 4-4M4 15v4l2 2h12l2-2v-4M8 19h8"/>',
    "package": '<path d="m12 2 9 5v10l-9 5-9-5V7l9-5ZM3 7l9 5 9-5M12 12v10M8 4l9 5v4"/>',
    "check": '<path d="M4 12v5l3 3h10l3-3V8l-3-4H7L4 8m4 4 3 3 6-7"/>',
    "warning": '<path d="M10 3h4l8 16-2 2H4l-2-2 8-16ZM12 8v6m0 3v.1"/>',
    "details": '<path d="M4 5h16M4 12h16M4 19h10M3 5h.1M17 19h4"/>',
    "time": '<path d="M8 4h8l5 5v7l-5 5H8l-5-5V9l5-5ZM12 8v5h4M9 1h6"/>',
    "close": '<path d="m5 5 14 14M5 19 19 5"/>',
}


def svg_source(name: str, color: str) -> str:
    """Return original, single-ink SVG geometry for export or native rendering."""
    ink = escape(str(color), quote=True)
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{ink}" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round">{PATHS.get(name, PATHS["settings"])}</svg>'


def icon(name: str, color: str | None = None, *, size: int = 20) -> QIcon:
    return QIcon(_render_icon(name, color or Theme.TEXT, max(8, min(128, int(size)))))


@lru_cache(maxsize=192)
def _render_icon(name: str, color: str, size: int = 20) -> QIcon:
    svg = svg_source(name, color)
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return QIcon(pixmap)


class ActionButton(QPushButton):
    """Keep legacy dynamic labels readable on platforms without emoji glyphs."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._icon_name = ""
        self.setIconSize(QSize(16, 16))
        self.setText(text)

    def setObjectName(self, name):
        super().setObjectName(name)
        candidate = name.removeprefix("btn-")
        if candidate in PATHS:
            self._icon_name = candidate
            self.refresh_icon()

    def setText(self, text):
        label = re.sub(r"^[^\w(]+", "", text, flags=re.UNICODE).strip()
        label = label.replace("▾", "...").replace("▸", ">")
        if text == "…":
            label = "..."
        for glyph, candidate in (("⧉", "copy"), ("🗑", "clear"), ("⏸", "pause"), ("▶", "resume"), ("🔍", "search"), ("↺", "reload"), ("✕", "close"), ("✖", "close")):
            if text.startswith(glyph):
                self._icon_name = candidate
                break
        # Icons carry the visual cue; the text remains available to screen readers.
        if not getattr(self, "_icon_name", ""):
            words = label.casefold()
            for word, candidate in (("compile", "compile"), ("upload", "upload"), ("stop", "stop"), ("clean", "clean"), ("save", "save"), ("reload", "reload"), ("download", "download"), ("project", "project"), ("settings", "settings"), ("ai assistant", "ai"), ("modify", "modify")):
                if word in words:
                    self._icon_name = candidate
                    break
            if "🔍" in text:
                self._icon_name = "search"
        if not label and getattr(self, "_icon_name", ""):
            self.setAccessibleName(self._icon_name.capitalize())
        elif not label:
            label = text
        super().setText(label)
        self.refresh_icon()

    def refresh_icon(self):
        if getattr(self, "_icon_name", ""):
            self.setIcon(icon(self._icon_name))
