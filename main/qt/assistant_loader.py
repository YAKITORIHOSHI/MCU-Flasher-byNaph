"""Loading screen for the embedded OpenCode assistant.

Says what is happening in plain words and keeps the terminal's startup output out
of sight. Deliberately still: the trace advances when real work advances, and
nothing moves while the user waits.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from main.core.theme import get_palette
from main.qt.glass import paint_cover
from main.qt.icons import icon
from main.qt.theme import glass_tokens

SLOW_AFTER_SECONDS = 15
_STEPS = 3


class _PhaseTrace(QWidget):
    """Pads on one routed trace (three by default); each lights when that step really starts."""

    def __init__(self, parent=None, steps: int = _STEPS):
        super().__init__(parent)
        self._steps = max(2, int(steps))
        self._phase = 0
        self._inks = (QColor("#56cfbf"), QColor("#2a5f58"), QColor("#101b2a"))
        self.setFixedSize(132, 16)
        self.setAccessibleName("Startup progress")
        self.set_phase(0)

    def set_phase(self, phase: int) -> None:
        self._phase = max(0, min(self._steps - 1, int(phase)))
        self.setAccessibleDescription(f"Step {self._phase + 1} of {self._steps}")
        self.update()

    def set_inks(self, lit: str, unlit: str, surface: str) -> None:
        self._inks = (QColor(lit), QColor(unlit), QColor(surface))
        self.update()

    def paintEvent(self, event) -> None:
        lit, unlit, surface = self._inks
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        middle = self.height() / 2
        spacing = (self.width() - 12) / (self._steps - 1)
        pads = [6 + index * spacing for index in range(self._steps)]
        for index, (start, end) in enumerate(zip(pads, pads[1:])):
            # The chamfered jog follows the routed strokes of the app's icon family.
            centre = (start + end) / 2
            path = QPainterPath(QPointF(start, middle))
            for x, y in ((centre - 6, middle), (centre - 2, middle - 4), (centre + 2, middle - 4),
                         (centre + 6, middle), (end, middle)):
                path.lineTo(x, y)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(lit if index < self._phase else unlit, 1.6, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)
        for index, x in enumerate(pads):
            started = index <= self._phase
            painter.setPen(QPen(lit if started else unlit, 1.6))
            painter.setBrush(lit if started else surface)
            painter.drawEllipse(QPointF(x, middle), 3.5, 3.5)


class AssistantLoader(QFrame):
    """Covers the assistant's terminal until its interface has really painted."""

    retry_requested = Signal()
    reveal_requested = Signal()

    def __init__(self, parent=None, *, can_reveal: bool = True):
        super().__init__(parent)
        self.setObjectName("assistant-loader")
        self._can_reveal = can_reveal
        self._loading = False
        self._status_text = ""
        self._started_at = 0.0
        self._error = False
        self._clock = QTimer(self)
        self._clock.setSingleShot(True)
        self._clock.timeout.connect(self._tick)
        self._build_ui()
        self.apply_theme("default")

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # Long diagnostics and very short panes scroll rather than clip.
        scroll = QScrollArea(self)
        scroll.setObjectName("assistant-loader-scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumSize(1, 1)
        self._scroll = scroll
        outer.addWidget(scroll)

        page = QWidget(scroll)
        page.setObjectName("assistant-loader-page")
        self._page_layout = QVBoxLayout(page)
        self._page_layout.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        column_host = QWidget(page)
        column_host.setObjectName("assistant-loader-column")
        column_host.setMaximumWidth(460)
        row.addWidget(column_host, 100)
        row.addStretch(1)
        self._page_layout.addLayout(row)
        self._page_layout.addStretch(1)
        scroll.setWidget(page)

        column = QVBoxLayout(column_host)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self._mark = QLabel(column_host)
        self._mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self._mark)
        self.title = QLabel("OpenCode AI Assistant", column_host)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)
        column.addWidget(self.title)
        self._trace = _PhaseTrace(column_host)
        column.addWidget(self._trace, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.status = QLabel("", column_host)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.status)
        self._hint = QLabel(
            "OpenCode is taking longer than usual. Its first run in a project can take a minute.",
            column_host)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setWordWrap(True)
        self._hint.hide()
        column.addWidget(self._hint)
        self.reveal_button = QPushButton("Show startup output", column_host)
        self.reveal_button.clicked.connect(self.reveal_requested)
        self.reveal_button.hide()
        column.addWidget(self.reveal_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.retry_button = QPushButton("Retry OpenCode", column_host)
        self.retry_button.clicked.connect(self.retry_requested)
        self.retry_button.hide()
        column.addWidget(self.retry_button, alignment=Qt.AlignmentFlag.AlignHCenter)

    def minimumSizeHint(self) -> QSize:
        # The cover must never hold a pane or window open; its content scrolls.
        return QSize(0, 0)

    def sizeHint(self) -> QSize:
        return QSize(240, 160)

    # ── States ───────────────────────────────────────────────────────────────

    def show_idle(self, message: str) -> None:
        self._show("OpenCode AI Assistant", message, loading=False, error=False)

    def show_progress(self, status: str, phase: int = 0, title: str = "Starting OpenCode") -> None:
        if not self._loading:
            self._started_at = time.monotonic()
        self._trace.set_phase(phase)
        self._show(title, status, loading=True, error=False)

    def show_error(self, title: str, message: str) -> None:
        self._show(title, message, loading=False, error=True)

    def finish(self) -> None:
        """The interface is on screen: stop the clock, leaving the cover hidden behind it."""
        self._show(self.title.text(), self._status_text, loading=False, error=False)

    def _show(self, title: str, status: str, *, loading: bool, error: bool) -> None:
        self._loading, self._error = loading, error
        self._status_text = status
        self.title.setText(title)
        self.retry_button.setVisible(error)
        self._trace.setVisible(loading and self.height() >= 110)
        self._tick()

    def _tick(self) -> None:
        elapsed = time.monotonic() - self._started_at if self._loading else 0
        slow = self._loading and elapsed >= SLOW_AFTER_SECONDS
        self.status.setText(self._status_text)
        self._hint.setVisible(slow)
        self.reveal_button.setVisible(slow and self._can_reveal)
        if self._loading and self.isVisible() and not slow:
            self._clock.start(max(1, int((SLOW_AFTER_SECONDS - elapsed) * 1000)))
        else:
            self._clock.stop()

    # ── Native events ────────────────────────────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._loading:
            self._tick()

    def hideEvent(self, event) -> None:
        self._clock.stop()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        height = event.size().height()
        width = event.size().width()
        self._trace.setFixedWidth(min(132, max(48, width - 32)))
        self.reveal_button.setText("Show output" if width < 200 else "Show startup output")
        self.retry_button.setText("Retry" if width < 200 else "Retry OpenCode")
        self.reveal_button.setToolTip("Show startup output")
        self.retry_button.setToolTip("Retry OpenCode")
        # The mark and the trace yield before any words do.
        self._mark.setVisible(height >= 190)
        self._trace.setVisible(self._loading and height >= 110)
        margin = 16 if height >= 150 else 6
        self._page_layout.setContentsMargins(16 if event.size().width() >= 300 else 8, margin,
                                             16 if event.size().width() >= 300 else 8, margin)

    # ── Theme ────────────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:
        # This covers a web view, so the window's glass cannot show through by itself.
        painter = QPainter(self)
        if not paint_cover(self, painter):
            painter.fillRect(self.rect(), QColor(self._cover_surface))
        painter.end()
        super().paintEvent(event)

    def apply_theme(self, mode: str) -> None:
        palette = get_palette(mode)
        glass = glass_tokens(mode)
        self._cover_surface = palette["BG_DARKEST"]
        self.setStyleSheet(
            f"QFrame#assistant-loader {{ background: {glass['reading']}; border: 1px solid {glass['reading_rim']}; border-radius: 10px; }}"
            "QScrollArea#assistant-loader-scroll, QWidget#assistant-loader-page,"
            "QWidget#assistant-loader-column { background: transparent; }")
        self._scroll.viewport().setStyleSheet("background: transparent;")
        self.title.setStyleSheet(f"color: {palette['TEXT']}; font-size: 15px; font-weight: 700; background: transparent;")
        self.status.setStyleSheet(f"color: {palette['TEXT_DIM']}; font-size: 12px; background: transparent;")
        self._hint.setStyleSheet(f"color: {palette['TEXT_DIM']}; font-size: 11px; background: transparent;")
        self._mark.setPixmap(icon("ai", palette["CYAN"], size=36).pixmap(36, 36))
        self._trace.set_inks(palette["CYAN"], palette["BORDER"], palette["BG_DARKEST"])
