"""App-owned glass and status primitives for setup and utility panels.

These surfaces use static Qt painting. They need neither a compositor blur,
graphics effect, animation timer nor a worker; Qt coalesces ordinary updates.
Palette mappings may contain workspace keys or bootstrap's ``T_`` keys.
"""
from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QVBoxLayout, QWidget

from main.core.theme import Theme
from main.qt.icons import icon


_PALETTE_KEYS = (
    "BG_DARKEST", "BG_DARK", "BG_MID", "BG_LIGHT", "BORDER", "BORDER_LIT",
    "TEXT", "TEXT_DIM", "TEXT_BRIGHT", "CYAN", "GREEN", "YELLOW", "RED",
)
_TONE_KEYS = {"muted": "TEXT_DIM", "active": "CYAN", "ok": "GREEN", "warn": "YELLOW", "fail": "RED"}
_TONE_ICONS = {"muted": "package", "active": "setup", "ok": "check", "warn": "warning", "fail": "warning"}


def _palette(palette: Mapping[str, str] | None) -> dict[str, str]:
    values = palette or {}
    return {key: values.get("T_" + key, values.get(key, getattr(Theme, key))) for key in _PALETTE_KEYS}


def _alpha(color: str, opacity: int) -> QColor:
    value = QColor(color)
    value.setAlpha(opacity)
    return value


class GlassCard(QFrame):
    """An opaque reading surface beneath static reflections and a beveled rim."""

    def __init__(self, parent=None, palette=None, radius: int = 12, accent: bool = False):
        super().__init__(parent)
        self._colors = _palette(palette)
        self._radius = max(0, int(radius))
        self._accent = bool(accent)
        self._cached_surface: QPixmap | None = None
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_palette(self, palette=None):
        colors = _palette(palette)
        if colors != self._colors:
            self._colors = colors
            self.invalidate_cache()

    def invalidate_cache(self):
        self._cached_surface = None
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._cached_surface = None

    def paintEvent(self, event):
        if self.width() <= 0 or self.height() <= 0:
            return
        if self._cached_surface is None or self._cached_surface.size() != self.size():
            self._cached_surface = self._make_surface()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._cached_surface)

    def _make_surface(self) -> QPixmap:
        surface = QPixmap(self.size())
        surface.fill(Qt.GlobalColor.transparent)
        painter = QPainter(surface)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = min(self._radius, bounds.width() / 2, bounds.height() / 2)
        path = QPainterPath()
        path.addRoundedRect(bounds, radius, radius)
        colors = self._colors
        light = QColor(colors["BG_DARKEST"]).lightness() > 128
        body = QLinearGradient(0, 0, self.width() * 0.25, self.height())
        body.setColorAt(0, QColor(colors["BG_LIGHT"]))
        body.setColorAt(0.22, QColor(colors["BG_MID"]))
        body.setColorAt(0.72, QColor(colors["BG_DARK"]))
        body.setColorAt(1, QColor(colors["BG_DARKEST"]))
        painter.fillPath(path, body)

        # These directional bands are baked into one cached surface. They
        # suggest thick glass without sampling the desktop or blurring text.
        # Specular light remains white in the light palette too; ink colors
        # are never used as reflection colors.
        painter.save()
        painter.setClipPath(path)
        reflection = QLinearGradient(0, 0, 0, max(1, self.height() * 0.8))
        reflection.setColorAt(0, _alpha("#ffffff", 92 if light else 46))
        reflection.setColorAt(0.38, _alpha("#ffffff", 34 if light else 17))
        reflection.setColorAt(1, _alpha("#ffffff", 0))
        band = QPainterPath()
        band.moveTo(0, 0)
        band.lineTo(self.width() * 0.62, 0)
        band.lineTo(self.width() * 0.36, self.height() * 0.8)
        band.lineTo(0, self.height() * 0.52)
        band.closeSubpath()
        painter.fillPath(band, reflection)
        streak = QPainterPath()
        streak.moveTo(self.width() * 0.64, 0)
        streak.lineTo(self.width() * 0.68, 0)
        streak.lineTo(self.width() * 0.43, self.height() * 0.8)
        streak.lineTo(self.width() * 0.40, self.height() * 0.8)
        streak.closeSubpath()
        sheen = QLinearGradient(0, 0, 0, max(1, self.height() * 0.8))
        sheen.setColorAt(0, _alpha("#ffffff", 55 if light else 23))
        sheen.setColorAt(1, _alpha("#ffffff", 0))
        painter.fillPath(streak, sheen)
        painter.restore()

        # An inset bevel contributes actual edge depth at small card heights.
        # The bottom and right edge darken while the upper edge catches light.
        bevel = QLinearGradient(0, 0, self.width() * 0.25, self.height())
        bevel.setColorAt(0, _alpha("#ffffff", 190 if light else 115))
        bevel.setColorAt(0.35, _alpha("#ffffff", 8))
        bevel.setColorAt(0.75, _alpha("#000000", 15 if light else 40))
        bevel.setColorAt(1, _alpha("#000000", 45 if light else 105))
        painter.setPen(QPen(bevel, 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(bounds.adjusted(1.5, 1.5, -1.5, -1.5), max(0, radius - 1), max(0, radius - 1))
        painter.setPen(QPen(QColor(colors["BORDER"]), 1))
        painter.drawPath(path)

        painter.save()
        painter.setClipPath(path)
        rim = QLinearGradient(0, 0, self.width(), 0)
        rim.setColorAt(0, _alpha(colors["BORDER_LIT"], 235 if self._accent else 150))
        rim.setColorAt(0.6, _alpha("#ffffff", 170 if light else 130))
        rim.setColorAt(1, _alpha(colors["BORDER_LIT"], 75))
        painter.setPen(QPen(rim, 1))
        painter.drawLine(bounds.topLeft(), bounds.topRight())
        if self._accent and self.width() > 70:
            # An interrupted etched trace replaces decorative glow/shadows.
            trace = QPainterPath()
            trace.moveTo(self.width() - 45, 1.5)
            trace.lineTo(self.width() - 27, 1.5)
            trace.lineTo(self.width() - 17, 11.5)
            trace.lineTo(self.width() - 8, 11.5)
            painter.setPen(QPen(_alpha(colors["CYAN"], 125), 1))
            painter.drawPath(trace)
        painter.restore()
        painter.end()
        return surface


class VectorGlyph(QLabel):
    """A fixed logical-size glyph from the app's original vector family."""

    def __init__(self, name: str, parent=None, color: str | None = None, size: int = 20):
        super().__init__(parent)
        self._logical_size = max(8, min(128, int(size)))
        self._name = name
        self._color = color or Theme.TEXT
        self.setFixedSize(self._logical_size, self._logical_size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: transparent; border: none; padding: 0;")
        self.set_icon(name, self._color)

    def set_icon(self, name: str, color: str | None = None):
        self._name = name
        if color is not None:
            self._color = color
        self.setPixmap(icon(name, self._color, size=self._logical_size).pixmap(QSize(self._logical_size, self._logical_size)))
        self.setAccessibleName(name.replace("_", " ").capitalize())


class StatusChip(QWidget):
    """A compact icon and text status; meaning always remains in readable text."""

    def __init__(self, text: str = "", parent=None, palette=None, tone: str = "muted", icon_name: str | None = None):
        super().__init__(parent)
        self._colors = _palette(palette)
        self._tone = tone if tone in _TONE_KEYS else "muted"
        self._icon_name = icon_name or _TONE_ICONS[self._tone]
        row = QHBoxLayout(self)
        row.setContentsMargins(9, 5, 9, 5)
        row.setSpacing(6)
        self.glyph = VectorGlyph(self._icon_name, self, size=15)
        self.label = QLabel(self)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        self.label.setFont(font)
        row.addWidget(self.glyph)
        row.addWidget(self.label)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setText(text)
        self._refresh()

    def text(self) -> str:
        return self.label.text()

    def setText(self, text: str):
        self.label.setText(text)
        self.setAccessibleName(text)
        self.updateGeometry()

    def set_tone(self, tone: str, icon_name: str | None = None):
        self._tone = tone if tone in _TONE_KEYS else "muted"
        self._icon_name = icon_name or _TONE_ICONS[self._tone]
        self._refresh()

    def set_palette(self, palette=None):
        self._colors = _palette(palette)
        self._refresh()

    def _refresh(self):
        ink = self._colors[_TONE_KEYS[self._tone]]
        self.glyph.set_icon(self._icon_name, ink)
        self.label.setStyleSheet(f"background: transparent; border: none; padding: 0; color: {self._colors['TEXT']};")
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = self._colors[_TONE_KEYS[self._tone]]
        painter.setBrush(_alpha(ink, 18))
        painter.setPen(QPen(_alpha(ink, 100), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)


class _ContinuousProgressBar(QProgressBar):
    """Native progress semantics with one continuous, style-independent fill."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._track_color = QColor(Theme.BG_DARKEST)
        self._border_color = QColor(Theme.BORDER)
        self._fill_color = QColor(Theme.CYAN)

    def set_colors(self, track: str, border: str, fill: str):
        self._track_color = QColor(track)
        self._border_color = QColor(border)
        self._fill_color = QColor(fill)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if bounds.width() <= 0 or bounds.height() <= 0:
            return
        radius = min(2, bounds.width() / 2, bounds.height() / 2)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._track_color)
        painter.drawRoundedRect(bounds, radius, radius)

        total = self.maximum() - self.minimum()
        fraction = max(0.0, min(1.0, (self.value() - self.minimum()) / total)) if total > 0 else 0.0
        inner = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if fraction > 0 and inner.width() > 0 and inner.height() > 0:
            fill = QRectF(inner)
            if self.orientation() == Qt.Orientation.Horizontal:
                fill.setWidth(inner.width() * fraction)
                inverted = self.invertedAppearance() != (self.layoutDirection() == Qt.LayoutDirection.RightToLeft)
                if inverted:
                    fill.moveRight(inner.right())
            else:
                fill.setHeight(inner.height() * fraction)
                if not self.invertedAppearance():
                    fill.moveBottom(inner.bottom())
            painter.setBrush(self._fill_color)
            painter.drawRoundedRect(fill, min(radius, fill.width() / 2, fill.height() / 2), min(radius, fill.width() / 2, fill.height() / 2))

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(self._border_color, 1))
        painter.drawRoundedRect(bounds, radius, radius)


class SetupProgressRow(QWidget):
    """A status row with real determinate progress, or a dash for unknown totals."""

    def __init__(self, parent=None, palette=None):
        super().__init__(parent)
        self._colors = _palette(palette)
        self._tone = "active"
        self._icon_name = "setup"
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        status = QHBoxLayout()
        status.setContentsMargins(0, 0, 0, 0)
        status.setSpacing(8)
        self.glyph = VectorGlyph("setup", self, size=18)
        self.status_lbl = QLabel(self)
        self.status_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setMinimumWidth(0)
        self.status_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.pct_lbl = QLabel("0%", self)
        self.pct_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.pct_lbl.setMinimumWidth(self.pct_lbl.fontMetrics().horizontalAdvance("100%") + 2)
        status.addWidget(self.glyph)
        status.addWidget(self.status_lbl, 1)
        status.addWidget(self.pct_lbl)
        layout.addLayout(status)
        self.progress_bar = _ContinuousProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(5)
        self.progress_bar.setAccessibleName("Setup progress")
        layout.addWidget(self.progress_bar)
        self._refresh()

    def set_status(self, text: str, tone: str = "active", icon_name: str | None = None):
        self._tone = tone if tone in _TONE_KEYS else "active"
        self._icon_name = icon_name or _TONE_ICONS[self._tone]
        self.status_lbl.setText(text)
        self.status_lbl.setAccessibleName(text)
        self._refresh()

    def set_progress(self, value: float | int | None):
        if value is None:
            # Leave operation state in the status text. The dash denotes only
            # an unknown percentage, including a failed or active download.
            self.pct_lbl.setText("—")
            self.progress_bar.setValue(0)
            return
        progress = max(0, min(100, int(value)))
        self.progress_bar.setValue(progress)
        self.pct_lbl.setText(f"{progress}%")

    def set_palette(self, palette=None):
        self._colors = _palette(palette)
        self._refresh()

    def _refresh(self):
        colors = self._colors
        ink = colors[_TONE_KEYS[self._tone]]
        self.glyph.set_icon(self._icon_name, ink)
        self.status_lbl.setStyleSheet(f"background: transparent; border: none; padding: 0; color: {colors['TEXT_BRIGHT']}; font-weight: 600;")
        self.status_lbl.ensurePolished()
        self.status_lbl.setMinimumHeight(self.status_lbl.fontMetrics().height())
        self.pct_lbl.setStyleSheet(f"background: transparent; border: none; padding: 0; color: {colors['TEXT_DIM']};")
        self.progress_bar.set_colors(colors["BG_DARKEST"], colors["BORDER"], ink)
        self.setMinimumHeight(self.layout().minimumSize().height())


__all__ = ["GlassCard", "VectorGlyph", "StatusChip", "SetupProgressRow"]
