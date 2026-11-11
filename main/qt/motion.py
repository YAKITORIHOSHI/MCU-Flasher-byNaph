"""Short entrance motion for dropdowns and the compact tool tabs.

A dropdown fades in while its contents slide a few pixels from the field that
opened it. Only child widgets move and fade: the native popup window keeps its
final, anchored geometry from the first frame, so Wayland (where clients cannot
move a shown popup) and Windows (where translucent popups can be repositioned by
the platform) both end exactly where combo_popup.py placed them.

Windows' "Show animations in Windows" switch and MCU_FLASHER_REDUCED_MOTION=1
turn the motion off; everything then appears at once, as before.
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QEasingCurve, QObject, QVariantAnimation, Qt
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


class PopupMotion(QObject):
    """Fade a popup's children in and slide them toward their final place."""

    def __init__(self, popup: QWidget):
        super().__init__(popup)
        self._popup = popup
        self._animation: QVariantAnimation | None = None
        self._parts: list[tuple[QWidget, object]] = []
        self._direction = 1

    def running(self) -> bool:
        return self._animation is not None

    def start(self, parts, *, opens_below: bool = True) -> None:
        self.finish()
        if not motion_enabled():
            return
        self._direction = -1 if opens_below else 1
        self._parts = []
        for widget in parts:
            if widget is None or not widget.isVisibleTo(self._popup):
                continue
            effect = QGraphicsOpacityEffect(widget)
            effect.setOpacity(0.0)
            widget.setGraphicsEffect(effect)
            self._parts.append((widget, widget.pos()))
        if not self._parts:
            return
        animation = QVariantAnimation(self)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setDuration(POPUP_DURATION_MS)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.valueChanged.connect(self._step)
        animation.finished.connect(self.finish)
        self._animation = animation
        self._step(0.0)
        animation.start()

    def _step(self, value) -> None:
        progress = float(value)
        offset = round((1.0 - progress) * POPUP_TRAVEL_PX * self._direction)
        for widget, origin in self._parts:
            effect = widget.graphicsEffect()
            if isinstance(effect, QGraphicsOpacityEffect):
                effect.setOpacity(progress)
            widget.move(origin.x(), origin.y() + offset)

    def finish(self) -> None:
        """End at the exact resting state: full opacity, laid-out positions, no effects."""
        animation, self._animation = self._animation, None
        if animation is not None:
            animation.stop()
            animation.deleteLater()
        parts, self._parts = self._parts, []
        for widget, origin in parts:
            try:
                widget.setGraphicsEffect(None)
                widget.move(origin)
            except RuntimeError:
                pass  # A popup can close and delete its rows mid-animation.
        layout = self._popup.layout()
        if parts and layout is not None:
            layout.invalidate()
            layout.activate()
