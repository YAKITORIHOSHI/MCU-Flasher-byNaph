"""Short entrance motion for dropdowns and the compact tool tabs.

A dropdown fades in while its contents slide a few pixels from the field that
opened it. The native popup window keeps its final, anchored geometry from the
first frame, so Wayland (where clients cannot move a shown popup) and Windows
(where translucent popups can be repositioned by the platform) both end exactly
where combo_popup.py placed them.

The motion animates one cached image of the popup's resting appearance. The
live list stays in place, invisible behind a fully transparent effect (which
paints nothing at all), so a frame costs a single drawPixmap. Fading the live
list instead re-rendered every row through an off-screen effect on every frame
and stuttered; showing it before the motion began made it blink.

Windows' "Show animations in Windows" switch and MCU_FLASHER_REDUCED_MOTION=1
turn the motion off; everything then appears at once, as before.
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QVariantAnimation, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QWidget

POPUP_DURATION_MS = 170
POPUP_TRAVEL_PX = 8
TAB_DURATION_MS = 190


def motion_enabled() -> bool:
    """Respect an explicit reduced-motion request and Windows' own animation switch."""
    if str(os.environ.get("MCU_FLASHER_REDUCED_MOTION", "")).strip().casefold() in {"1", "true", "yes"}:
        return False
    if sys.platform == "win32" and QApplication.instance() is not None:
        # Qt reads SPI_GETUIEFFECTS ("Show animations in Windows") into UI_General.
        return QApplication.isEffectEnabled(Qt.UIEffect.UI_General)
    return True


class _MotionLayer(QWidget):
    """Paints the popup's cached resting image at the motion's opacity and offset."""

    def __init__(self, popup: QWidget, image):
        super().__init__(popup)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAutoFillBackground(False)
        self._image = image
        self.progress = 0.0
        self.offset = 0
        self.setGeometry(popup.rect())

    def paintEvent(self, event):
        if self.progress <= 0.0:
            return
        painter = QPainter(self)
        painter.setOpacity(min(1.0, self.progress))
        painter.drawPixmap(0, self.offset, self._image)
        painter.end()


class PopupMotion(QObject):
    """Fade a popup's contents in and slide them toward their final place."""

    def __init__(self, popup: QWidget):
        super().__init__(popup)
        self._popup = popup
        self._animation: QVariantAnimation | None = None
        self._layer: _MotionLayer | None = None
        self._concealed: list[QWidget] = []
        self._direction = 1
        self._progress = 1.0

    def running(self) -> bool:
        return self._animation is not None or bool(self._concealed)

    def progress(self) -> float:
        """0 while hidden, 1 at rest; partial values only while animating."""
        return self._progress

    @staticmethod
    def _hide_part(widget: QWidget) -> None:
        effect = QGraphicsOpacityEffect(widget)
        effect.setOpacity(0.0)
        widget.setGraphicsEffect(effect)

    def conceal(self, parts) -> None:
        """Hide the parts before the popup's first frame; start() then reveals them.

        Call synchronously from the popup's Show event, so the contents are never
        drawn once at full strength and then snapped to transparent.
        """
        if not motion_enabled() or self._animation is not None:
            return
        for widget in parts:
            if widget is None or widget in self._concealed:
                continue
            self._hide_part(widget)
            self._concealed.append(widget)
        if self._concealed:
            self._progress = 0.0

    def start(self, parts, *, opens_below: bool = True) -> None:
        if not motion_enabled():
            self.finish()
            return
        if self._animation is not None:
            self.finish()
        self.conceal(parts)
        visible = [widget for widget in self._concealed if widget.isVisibleTo(self._popup)]
        if not visible or self._popup.width() <= 0 or self._popup.height() <= 0:
            self.finish()
            return
        self._direction = -1 if opens_below else 1
        # One image of the resting popup: its parts drawn normally, just once.
        for widget in self._concealed:
            widget.setGraphicsEffect(None)
        try:
            image = self._popup.grab()
        finally:
            for widget in self._concealed:
                self._hide_part(widget)
        layer = _MotionLayer(self._popup, image)
        layer.show()
        layer.raise_()
        self._layer = layer
        animation = QVariantAnimation(self)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setDuration(POPUP_DURATION_MS)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.valueChanged.connect(self._step)
        animation.finished.connect(self.finish)
        self._animation = animation
        self._popup.installEventFilter(self)
        self._step(0.0)
        animation.start()

    def _step(self, value) -> None:
        self._progress = float(value)
        layer = self._layer
        if layer is None:
            return
        layer.progress = self._progress
        layer.offset = round((1.0 - self._progress) * POPUP_TRAVEL_PX * self._direction)
        layer.update()

    def eventFilter(self, watched, event):
        # The cached image matches only this size; a changed popup rests at once.
        if watched is self._popup and event.type() == QEvent.Type.Resize and self._layer is not None:
            if self._layer.size() != self._popup.size():
                self.finish()
        return False

    def finish(self) -> None:
        """End at the exact resting state: the live contents, no effects, no layer."""
        animation, self._animation = self._animation, None
        if animation is not None:
            animation.stop()
            animation.deleteLater()
        try:
            self._popup.removeEventFilter(self)
        except RuntimeError:
            pass
        layer, self._layer = self._layer, None
        if layer is not None:
            try:
                layer.hide()
                layer.deleteLater()
            except RuntimeError:
                pass  # The popup closed and took its layer with it.
        concealed, self._concealed = self._concealed, []
        for widget in concealed:
            try:
                widget.setGraphicsEffect(None)
            except RuntimeError:
                pass  # A popup can close and delete its rows mid-animation.
        self._progress = 1.0
