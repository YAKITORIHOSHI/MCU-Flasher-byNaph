"""Circular loading spinner component for MCU Flasher.

Provides a smooth, QPainter-based animated circular spinner with anti-aliasing,
theme-matched ink, configurable size and stroke, and resource-conscious pause/resume
behavior (pauses when hidden or disabled to save CPU/battery).
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

__all__ = ["ButtonSpinner", "CircularSpinner"]


class CircularSpinner(QWidget):
    """An elegant circular vector loading spinner with animated arc rotation.

    API:
        start(): Begins rotation and ensures visibility.
        stop(): Pauses rotation and hides the spinner.
        set_busy(busy: bool): Convenience method matching application task patterns.
        is_busy() -> bool: Returns True if currently in busy/spinning mode.
        is_spinning() -> bool: Returns True if the animation timer is actively firing.
        set_color(color: str | QColor): Updates spinner foreground color.
        set_size(size: int): Updates diameter.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        size: int = 24,
        color: str | QColor = "#008184",
        line_width: float = 2.5,
        bg_track_color: str | QColor | None = None,
    ) -> None:
        super().__init__(parent)
        self._size = max(12, int(size))
        self._color = QColor(color)
        self._line_width = max(1.0, float(line_width))

        if bg_track_color is not None:
            self._track_color = QColor(bg_track_color)
        else:
            # Default to subtle opacity of main color for background track
            track = QColor(self._color)
            track.setAlpha(40)
            self._track_color = track

        self._angle = 0.0
        self._step = 8.0  # degrees per tick
        self._arc_length = 100.0  # degrees of drawn arc
        self._busy = False

        self.setFixedSize(self._size, self._size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAccessibleName("Loading indicator")
        self.setAccessibleDescription("Operation in progress")

        self._timer = QTimer(self)
        self._timer.setInterval(20)  # 50 FPS for smooth rotation
        self._timer.timeout.connect(self._rotate)

    def sizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    def minimumSizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    @Slot()
    def start(self) -> None:
        """Start the rotation animation and show the widget."""
        self._busy = True
        self.setVisible(True)
        if self.isVisible() and self.isEnabled() and not self._timer.isActive():
            self._timer.start()
        self.update()

    @Slot()
    def stop(self) -> None:
        """Stop the rotation animation and hide the widget."""
        self._busy = False
        if self._timer.isActive():
            self._timer.stop()
        self.setVisible(False)
        self.update()

    @Slot(bool)
    def set_busy(self, busy: bool) -> None:
        """Set busy state to start or stop the spinner."""
        if busy:
            self.start()
        else:
            self.stop()

    def is_busy(self) -> bool:
        """Return True if spinner is in active busy state."""
        return self._busy

    def is_spinning(self) -> bool:
        """Return True if timer is actively rotating the spinner."""
        return self._busy and self._timer.isActive()

    def set_color(self, color: str | QColor) -> None:
        """Update spinner accent color and recalculate track color."""
        self._color = QColor(color)
        track = QColor(self._color)
        track.setAlpha(40)
        self._track_color = track
        self.update()

    def set_size(self, size: int) -> None:
        """Update widget size."""
        self._size = max(12, int(size))
        self.setFixedSize(self._size, self._size)
        self.update()

    def set_line_width(self, width: float) -> None:
        """Update arc stroke width."""
        self._line_width = max(1.0, float(width))
        self.update()

    def _rotate(self) -> None:
        """Advance the rotation angle by one step."""
        self._angle = (self._angle + self._step) % 360.0
        self.update()

    def hideEvent(self, event) -> None:
        """Pause the animation timer when widget is hidden to save CPU/battery."""
        super().hideEvent(event)
        if self._timer.isActive():
            self._timer.stop()

    def showEvent(self, event) -> None:
        """Resume animation timer when widget becomes visible if busy."""
        super().showEvent(event)
        if self._busy and self.isEnabled() and not self._timer.isActive():
            self._timer.start()

    def changeEvent(self, event) -> None:
        """Pause/resume timer on enabled/disabled changes."""
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            if self.isEnabled() and self._busy and self.isVisible():
                if not self._timer.isActive():
                    self._timer.start()
            else:
                if self._timer.isActive():
                    self._timer.stop()

    def paintEvent(self, event) -> None:
        """Render circular arc with antialiased QPainter."""
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)

            # Inset drawing bounds by half line-width + padding so stroke stays inside widget rect
            padding = 2.0
            half_pen = self._line_width / 2.0
            rect = QRectF(
                half_pen + padding,
                half_pen + padding,
                self.width() - self._line_width - (padding * 2.0),
                self.height() - self._line_width - (padding * 2.0),
            )

            # 1. Background guide track
            if self._track_color.alpha() > 0:
                track_pen = QPen(self._track_color)
                track_pen.setWidthF(self._line_width)
                track_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(track_pen)
                painter.drawEllipse(rect)

            # 2. Rotating foreground arc
            arc_pen = QPen(self._color)
            arc_pen.setWidthF(self._line_width)
            arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(arc_pen)

            start_angle = int(-self._angle * 16)
            span_angle = int(self._arc_length * 16)
            painter.drawArc(rect, start_angle, span_angle)
        finally:
            painter.end()


def _paint_arc(painter: QPainter, rect: QRectF, angle: float, color: QColor, line_width: float) -> None:
    """Draw the spinner's faint track and rotating arc inside ``rect``."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    inset = line_width / 2.0 + 1.0
    rect = rect.adjusted(inset, inset, -inset, -inset)
    track = QColor(color)
    track.setAlpha(48)
    pen = QPen(track)
    pen.setWidthF(line_width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawEllipse(rect)
    pen.setColor(color)
    painter.setPen(pen)
    painter.drawArc(rect, int(-angle * 16), int(100 * 16))


class ButtonSpinner(QObject):
    """Spin in place of a button's own icon while it works.

    Each frame is a plain pixmap icon of the button's own icon size, so the
    label and every neighbouring control stay exactly where they were.
    Matches the parts of CircularSpinner's API that task dialogs use.
    """

    def __init__(self, button, color: str | QColor = "#141414", line_width: float = 2.0) -> None:
        super().__init__(button)
        self._button = button
        self.color = QColor(color)
        self.line_width = float(line_width)
        self.angle = 0.0
        self._busy = False
        self._saved_icon = QIcon()
        self._frame_key = None
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._advance)

    def set_busy(self, busy: bool) -> None:
        busy = bool(busy)
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self._saved_icon = self._button.icon()
            self.angle = 0.0
            self._show_frame()
            self._timer.start()
        else:
            self._timer.stop()
            self._frame_key = None
            self._button.setIcon(self._saved_icon)

    def start(self) -> None:
        self.set_busy(True)

    def stop(self) -> None:
        self.set_busy(False)

    def is_busy(self) -> bool:
        return self._busy

    def is_spinning(self) -> bool:
        return self._busy and self._timer.isActive()

    def set_color(self, color: str | QColor) -> None:
        self.color = QColor(color)
        if self._busy:
            self._show_frame()

    def _show_frame(self) -> None:
        size = self._button.iconSize()
        if size.isEmpty():
            size = QSize(16, 16)
        ratio = max(1.0, float(self._button.devicePixelRatioF()))
        image = QPixmap(max(1, round(size.width() * ratio)), max(1, round(size.height() * ratio)))
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        _paint_arc(painter, QRectF(0, 0, size.width(), size.height()), self.angle, self.color, self.line_width)
        painter.end()
        icon = QIcon(image)
        # A busy button is disabled; keep the spinner's own ink rather than a greyed copy.
        icon.addPixmap(image, QIcon.Mode.Disabled)
        self._frame_key = icon.cacheKey()
        self._button.setIcon(icon)

    def _advance(self) -> None:
        if self._button.icon().cacheKey() != self._frame_key:
            # A theme refresh replaced the icon; restore that one afterwards.
            self._saved_icon = self._button.icon()
        self.angle = (self.angle + 12.0) % 360.0
        self._show_frame()
