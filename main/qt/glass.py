"""Static glass workspace and keyboard focus for native tool tabs."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget, QTabBar

from main.core.theme import Theme


class GlassWorkspace(QWidget):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(Theme.BG_DARKEST))
        # Static pools of light give the translucent panel rims depth at no
        # ongoing animation cost. Qt draws these identically on Windows/Linux.
        for x, y, color in ((0.1, 0.05, Theme.CYAN), (0.95, 0.9, Theme.BLUE)):
            radius = max(self.width(), self.height()) * 0.7
            gradient = QRadialGradient(self.width() * x, self.height() * y, radius)
            tint = QColor(color)
            tint.setAlpha(32)
            gradient.setColorAt(0, tint)
            tint.setAlpha(0)
            gradient.setColorAt(1, tint)
            painter.fillRect(self.rect(), gradient)


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
