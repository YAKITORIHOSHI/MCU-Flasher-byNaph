"""Window-owned, cached frosted material for every native combo popup."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QFrame, QStyledItemDelegate, QWidget

from main.core.theme import get_palette
from main.qt.glass import _render_scene, _sheet_image, _theme_name
from main.qt.theme import glass_tokens


def prepare_popup(popup):
    """Use per-pixel alpha, including on Windows, before creating the native window.

    A rounded mask cuts integer pixels out of an opaque rectangular window. It
    leaves a dark, jagged fringe at fractional scales; paint the alpha edge instead.
    """
    popup.setWindowFlags(popup.windowFlags() | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
    popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    popup.setAutoFillBackground(False)


class PopupBackdrop(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAutoFillBackground(False)
        self.mode = "default"
        self._key = self._image = None

    def paintEvent(self, event):
        ratio = self.devicePixelRatioF()
        key = (self.width(), self.height(), ratio, self.mode)
        if self._key != key:
            scene = _render_scene(self.size(), ratio, self.mode, veiled=True)
            self._image = QPixmap(scene.size())
            self._image.setDevicePixelRatio(ratio)
            self._image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(self._image)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(QRectF(self.rect()), 9, 9)
            painter.setClipPath(path)
            painter.drawPixmap(0, 0, scene)
            painter.drawImage(0, 0, _sheet_image(self.width(), self.height(), 9, ratio, self.mode == "light"))
            painter.end()
            self._key = key
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._image)
        painter.end()


class _PopupMaterial(QObject):
    def __init__(self, combo):
        super().__init__(combo)
        self.combo = combo
        self.view = combo.view()
        self.popup = self.view.window()
        self._mode = None
        self._fitting = False
        prepare_popup(self.popup)
        self.popup.setObjectName("glass-combo-popup")
        self.popup.setAutoFillBackground(False)
        self.popup.setStyleSheet("QFrame#glass-combo-popup { background: transparent; border: none; }")
        if isinstance(self.popup, QFrame):
            self.popup.setFrameShape(QFrame.Shape.NoFrame)
        self.backdrop = PopupBackdrop(self.popup)
        self.backdrop.lower()
        self.backdrop.show()
        self.view.setItemDelegate(QStyledItemDelegate(self.view))
        self.view.setAutoFillBackground(False)
        self.view.viewport().setAutoFillBackground(False)
        self.popup.installEventFilter(self)
        self._style_rows()

    def _style_rows(self):
        mode = _theme_name()
        if self._mode != mode:
            self._mode = mode
            self.backdrop.mode = mode
            pal, glass = get_palette(mode), glass_tokens(mode)
            self.view.setStyleSheet(f"""
                QAbstractItemView {{ background: transparent; color: {pal['TEXT']};
                    border: none; padding: 5px; outline: none;
                    selection-background-color: {glass['chip']}; selection-color: {glass['chip_ink']}; }}
                QAbstractItemView > QWidget {{ background: transparent; border: none; }}
                QAbstractItemView::item {{ background: transparent; color: {pal['TEXT']};
                    min-height: 22px; padding: 3px 8px; margin: 1px 0; border: none; border-radius: 5px; }}
                QAbstractItemView::item:hover {{ background: {glass['button_hover']}; color: {pal['TEXT_BRIGHT']}; }}
                QAbstractItemView::item:selected {{ background: {glass['chip']}; color: {glass['chip_ink']}; }}
                QAbstractItemView::item:disabled {{ color: {glass['disabled_ink']}; }}
            """)

    def _refresh(self):
        self._style_rows()
        if not self.popup.isVisible():
            return
        if self._fitting:
            return
        self._fitting = True
        try:
            # Leave native popup frame insets inside the work area too.
            area = self.popup.screen().availableGeometry().adjusted(4, 4, -4, -4)
            self.view.setMaximumWidth(max(40, area.width() - 8))
            self.popup.setMaximumWidth(area.width())
            rect = self.popup.geometry()
            rect.setWidth(min(rect.width(), area.width()))
            rect.setHeight(min(rect.height(), area.height()))
            # Native Windows may reposition a translucent private container
            # over the selected row at fractional scales. Anchor the list to
            # its field explicitly, in the same Qt logical coordinate space.
            field = self.combo.mapToGlobal(QPoint(0, 0))
            below = field.y() + self.combo.height()
            above = field.y() - rect.height()
            rect.moveLeft(field.x())
            rect.moveTop(below if below + rect.height() <= area.bottom() + 1 else above)
            rect.moveLeft(max(area.left(), min(rect.left(), area.right() - rect.width() + 1)))
            rect.moveTop(max(area.top(), min(rect.top(), area.bottom() - rect.height() + 1)))
            if rect != self.popup.geometry():
                self.popup.setGeometry(rect)
            self.backdrop.setGeometry(self.popup.rect())
            self.backdrop.lower()
            self.backdrop.update()
        finally:
            self._fitting = False

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Resize, QEvent.Type.DevicePixelRatioChange):
            self._refresh()
            if event.type() == QEvent.Type.Show:
                QTimer.singleShot(0, self, self._refresh)
        elif event.type() == QEvent.Type.StyleChange and self._mode != _theme_name():
            QTimer.singleShot(0, self, self._refresh)
        return False


def install_combo_popup(combo):
    """Called after native style polish, never from a global event filter."""
    if not hasattr(combo, "_mcu_glass_popup"):
        combo._mcu_glass_popup = _PopupMaterial(combo)
