"""Theme-aware Qt rendering of the app-owned SVG action assets."""
from __future__ import annotations

from functools import lru_cache
from PySide6.QtCore import QByteArray, QEvent, QSize, Qt, QRectF
from PySide6.QtGui import QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLabel, QPushButton, QWidget
from main.core.theme import Theme
from src.modules.svg_icons import NAMES, svg_source, action_name
from src.modules.ui_palette import contrast_ratio, readable_foreground

# Retain the public membership contract used by existing action owners.
PATHS = NAMES


def icon(name: str, color: str | None = None, *, size: int = 20) -> QIcon:
    return QIcon(_render_icon(name, color or Theme.TEXT, max(8, min(128, int(size)))))


@lru_cache(maxsize=192)
def _render_icon(name, color, size):
    result = QIcon()
    renderer = QSvgRenderer(QByteArray(svg_source(name, color).encode()))
    for ratio in (1, 2):
        pixmap = QPixmap(size * ratio, size * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        result.addPixmap(pixmap)
    return result


def _ink(widget, role, background, group=QPalette.ColorGroup.Active):
    palette = widget.palette()
    color = palette.color(group, role).name()
    surface = palette.color(group, background).name()
    return color if contrast_ratio(color, surface) >= 3 else readable_foreground(surface)


class ActionButton(QPushButton):
    """Text and SVG are independent; compact controls keep accessible names."""

    def __init__(self, text="", parent=None, *, icon_name=""):
        super().__init__(parent)
        self._icon_name = icon_name
        self._explicit_icon = bool(icon_name)
        self._action_name = str(text)
        self._refreshing_icon = False
        self.setIconSize(QSize(16, 16))
        self.setText(text)

    def set_icon(self, name):
        if name not in NAMES:
            raise ValueError(f"Unknown action icon: {name}")
        self._icon_name = name
        self._explicit_icon = True
        self.refresh_icon()

    def setObjectName(self, name):
        super().setObjectName(name)
        candidate = name.removeprefix("btn-")
        if candidate in NAMES:
            self.set_icon(candidate)

    def setText(self, text):
        label = str(text).strip()
        candidate = action_name(label)
        if candidate and not getattr(self, "_explicit_icon", False):
            self._icon_name = candidate
        if label:
            self._action_name = label
        super().setText(label)
        self.setAccessibleName(getattr(self, "_action_name", "") or
                               getattr(self, "_icon_name", "").replace("_", " ").capitalize())
        self.refresh_icon()

    def refresh_icon(self):
        name = getattr(self, "_icon_name", "")
        if not name or getattr(self, "_refreshing_icon", False):
            return
        self._refreshing_icon = True
        try:
            result = QIcon()
            for mode, group in ((QIcon.Mode.Normal, QPalette.ColorGroup.Active),
                                (QIcon.Mode.Active, QPalette.ColorGroup.Active),
                                (QIcon.Mode.Disabled, QPalette.ColorGroup.Disabled),
                                (QIcon.Mode.Selected, QPalette.ColorGroup.Active)):
                color = _ink(self, QPalette.ColorRole.ButtonText, QPalette.ColorRole.Button, group)
                rendered = icon(name, color, size=self.iconSize().width())
                for ratio in (1, 2):
                    result.addPixmap(rendered.pixmap(self.iconSize(), ratio), mode, QIcon.State.Off)
                    result.addPixmap(rendered.pixmap(self.iconSize(), ratio), mode, QIcon.State.On)
            self.setIcon(result)
        finally:
            self._refreshing_icon = False

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange,
                            QEvent.Type.ApplicationPaletteChange, QEvent.Type.EnabledChange):
            self.refresh_icon()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_icon()


class IconLabel(QLabel):
    """A painted SVG beside literal text, with no glyph in selected/copied text."""

    def __init__(self, text="", parent=None, *, icon_name=""):
        if isinstance(text, QWidget):
            parent, text = text, ""
        super().__init__(str(text), parent)
        self._icon_name = ""
        if icon_name:
            self.set_icon(icon_name)

    def set_icon(self, name):
        if name and name not in NAMES:
            raise ValueError(f"Unknown label icon: {name}")
        self._icon_name = name
        if name and not self.text():
            self.setAccessibleName(name.replace("_", " ").capitalize())
        self.setIndent(22 if name else 0)
        self.setMinimumWidth(22 if name else 0)
        self.updateGeometry()
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        name = getattr(self, "_icon_name", "")
        if name:
            painter = QPainter(self)
            group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
            color = _ink(self, QPalette.ColorRole.WindowText, QPalette.ColorRole.Window, group)
            x = max(2, (self.width() - 16) // 2) if not self.text() and self.alignment() & Qt.AlignmentFlag.AlignHCenter else 2
            icon(name, color, size=16).paint(painter, x, max(0, (self.height() - 16) // 2), 16, 16)
            painter.end()
