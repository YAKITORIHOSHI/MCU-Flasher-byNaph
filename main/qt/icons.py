"""Small vector icons that render without depending on emoji fonts."""
from __future__ import annotations

import re
from functools import lru_cache
from PySide6.QtCore import QByteArray, QSize, Qt, QRectF
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QPushButton
from main.core.theme import Theme

PATHS = {
    "compile": '<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-2-15-4 18"/>',
    "upload": '<path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/>',
    "stop": '<rect x="5" y="5" width="14" height="14" rx="3"/>',
    "clean": '<path d="m14 3-4 10m-5 0h10l4 8H2l3-8Zm4 1-2 6m5-6 2 6"/>',
    "save": '<path d="M4 3h13l4 4v14H3V3h1Zm3 0v7h10V3M7 21v-7h10v7"/>',
    "reload": '<path d="M20 7a9 9 0 1 0 1 7M20 2v6h-6"/>',
    "project": '<path d="M3 6h7l2 3h9v12H3V6Z"/>',
    "search": '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
    "settings": '<path d="M4 5h16M4 12h16M4 19h16"/><circle cx="9" cy="5" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="8" cy="19" r="2"/>',
    "ai": '<rect x="5" y="6" width="14" height="14" rx="4"/><path d="M12 2v4M2 11h3m14 0h3M9 15h6M9 10v1m6-1v1"/>',
    "console": '<path d="m4 6 6 6-6 6m9 0h7"/>',
    "serial": '<path d="M2 12h3l3-8 6 16 3-8h5"/>',
    "devices": '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4"/>',
    "alerts": '<path d="M5 17h14l-2-3V9a5 5 0 0 0-10 0v5l-2 3Zm5 4h4"/>',
    "copy": '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    "clear": '<path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7"/>',
    "pause": '<path d="M8 4v16M16 4v16"/>',
    "resume": '<path d="m6 3 15 9-15 9V3Z"/>',
    "modify": '<path d="m4 17 12-12 4 4L8 21H4v-4ZM14 7l4 4"/>',
    "download": '<path d="M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4"/>',
    "close": '<path d="m5 5 14 14M5 19 19 5"/>',
}


def icon(name: str, color: str | None = None) -> QIcon:
    return QIcon(_render_icon(name, color or Theme.TEXT))


@lru_cache(maxsize=128)
def _render_icon(name: str, color: str) -> QIcon:
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{PATHS.get(name, PATHS["settings"])}</svg>'
    pixmap = QPixmap(40, 40)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, 20, 20))
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
