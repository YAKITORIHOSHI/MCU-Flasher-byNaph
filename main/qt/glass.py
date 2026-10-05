"""Static glass workspace and keyboard focus for native tool tabs."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget, QTabBar

from main.core.theme import Theme


class GlassWorkspace(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cached_pixmap = None
        self._cached_palette = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._cached_pixmap = None

    def invalidate_cache(self):
        self._cached_pixmap = None
        self.update()

    def paintEvent(self, event):
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        palette = (Theme.BG_DARKEST, Theme.BG_DARK, Theme.BG_LIGHT, Theme.CYAN, Theme.BLUE)
        if (self._cached_pixmap is None or self._cached_pixmap.size() != self.size()
                or self._cached_palette != palette):
            from PySide6.QtGui import QPixmap
            pix = QPixmap(self.size())
            p = QPainter(pix)
            body = QLinearGradient(0, 0, w * 0.35, h)
            body.setColorAt(0, QColor(Theme.BG_LIGHT))
            body.setColorAt(0.3, QColor(Theme.BG_DARK))
            body.setColorAt(0.75, QColor(Theme.BG_DARKEST))
            body.setColorAt(1, QColor(Theme.BG_DARKEST))
            p.fillRect(self.rect(), body)
            # Directional edge light follows setup's glass surfaces. Nothing
            # samples the desktop or runs a paint timer on constrained hosts.
            for x, y, color, opacity in ((0.1, 0.05, Theme.CYAN, 35), (0.95, 0.9, Theme.BLUE, 18)):
                radius = max(w, h) * 0.7
                gradient = QRadialGradient(w * x, h * y, radius)
                tint = QColor(color)
                tint.setAlpha(opacity)
                gradient.setColorAt(0, tint)
                tint.setAlpha(0)
                gradient.setColorAt(1, tint)
                p.fillRect(self.rect(), gradient)
            reflection = QLinearGradient(0, 0, 0, max(1, h * 0.6))
            tint = QColor("#ffffff")
            tint.setAlpha(42 if QColor(Theme.BG_DARKEST).lightness() > 128 else 22)
            reflection.setColorAt(0, tint)
            tint.setAlpha(0)
            reflection.setColorAt(1, tint)
            band = QPainterPath()
            band.moveTo(0, 0)
            band.lineTo(w * 0.62, 0)
            band.lineTo(w * 0.35, h * 0.65)
            band.lineTo(0, h * 0.4)
            band.closeSubpath()
            p.fillPath(band, reflection)
            p.end()
            self._cached_pixmap = pix
            self._cached_palette = palette
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._cached_pixmap)


class WorkspaceTabBar(QTabBar):
    """Native tab navigation with a focus ring separate from selection."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workspace-tab-bar")
        self.setAccessibleName("Workspace tools")
        self.setExpanding(False)
        self.setDrawBase(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.hasFocus() or self.currentIndex() < 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(Theme.CYAN), 1, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(self.tabRect(self.currentIndex()).adjusted(4, 4, -5, -10), 5, 5)
