"""Window-owned, cached frosted material for every native combo popup."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QMenu, QStyledItemDelegate, QWidget

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


def frosted_pixmap(width: int, height: int, ratio: float, mode: str, *, paper: bool = False,
                   radius: float = 9) -> QPixmap:
    """The cached-material image every glass popup paints: scene, frost, tint and rim."""
    image = QPixmap(round(width * ratio), round(height * ratio))
    image.setDevicePixelRatio(ratio)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if paper:
        painter.fillRect(0, 0, width, height, QColor("#ffffff"))
    else:
        from PySide6.QtCore import QSize
        scene = _render_scene(QSize(width, height), ratio, mode, veiled=True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, scene)
        painter.drawImage(0, 0, _sheet_image(width, height, radius, ratio, mode == "light"))
    painter.end()
    return image


class PopupBackdrop(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAutoFillBackground(False)
        self.mode = "default"
        self.paper = False
        self._key = self._image = None

    def paintEvent(self, event):
        ratio = self.devicePixelRatioF()
        key = (self.width(), self.height(), ratio, self.mode, self.paper)
        if self._key != key:
            self._image = frosted_pixmap(self.width(), self.height(), ratio, self.mode, paper=self.paper)
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
        self.backdrop.paper = combo.property("mcuPopupStyle") == "paper"
        self.backdrop.lower()
        self.backdrop.show()
        self.view.setItemDelegate(QStyledItemDelegate(self.view))
        self.view.setAutoFillBackground(False)
        self.view.viewport().setAutoFillBackground(False)
        self.popup.installEventFilter(self)
        self._style_rows()
        from main.qt.motion import PopupMotion
        self.motion = PopupMotion(self.popup)
        self._opens_below = True

    def _style_rows(self):
        mode = _theme_name()
        if self._mode != mode:
            self._mode = mode
            self.backdrop.mode = mode
            if self.backdrop.paper:
                self.view.setStyleSheet("""
                    QAbstractItemView { background: #ffffff; color: #141414;
                        border: 2px solid #141414; padding: 4px; outline: none;
                        selection-background-color: #ffe85b; selection-color: #141414; }
                    QAbstractItemView > QWidget { background: #ffffff; border: none; }
                    QAbstractItemView::item { background: #ffffff; color: #141414;
                        min-height: 22px; padding: 3px 8px; margin: 1px 0; border: none; border-radius: 0; }
                    QAbstractItemView::item:hover { background: #fff5b8; }
                    QAbstractItemView::item:selected { background: #ffe85b; color: #141414; }
                    QAbstractItemView::item:disabled { color: #625d63; }
                """)
                return
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
            below_space = max(0, area.bottom() + 1 - below)
            above_space = max(0, field.y() - area.top())
            use_below = (rect.height() <= below_space or
                         (rect.height() > above_space and below_space >= above_space))
            # When neither side fits, shorten the native scrolling list instead
            # of clamping its full height across the field it belongs to.
            rect.setHeight(max(1, min(rect.height(), below_space if use_below else above_space)))
            rect.moveLeft(field.x())
            rect.moveTop(below if use_below else field.y() - rect.height())
            self._opens_below = use_below
            rect.moveLeft(max(area.left(), min(rect.left(), area.right() - rect.width() + 1)))
            rect.moveTop(max(area.top(), min(rect.top(), area.bottom() - rect.height() + 1)))
            if rect != self.popup.geometry():
                self.popup.setGeometry(rect)
            self.backdrop.setGeometry(self.popup.rect())
            self.backdrop.lower()
            self.backdrop.update()
        finally:
            self._fitting = False

    def _settle_and_reveal(self):
        self._refresh()
        if self.popup.isVisible():
            # The list fades in and slides from its field once its final geometry is set.
            self.motion.start((self.backdrop, self.view), opens_below=self._opens_below)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Resize, QEvent.Type.DevicePixelRatioChange):
            self._refresh()
            if event.type() == QEvent.Type.Show:
                QTimer.singleShot(0, self, self._settle_and_reveal)
        elif event.type() == QEvent.Type.Hide:
            self.motion.finish()
        elif event.type() == QEvent.Type.StyleChange and self._mode != _theme_name():
            QTimer.singleShot(0, self, self._refresh)
        return False


def install_combo_popup(combo):
    """Called after native style polish, never from a global event filter."""
    if not hasattr(combo, "_mcu_glass_popup"):
        combo._mcu_glass_popup = _PopupMaterial(combo)


class GlassMenu(QMenu):
    """A context or options menu on the same cached frosted material as dropdowns.

    QMenu paints its rows itself, so the material is painted first in its own
    paintEvent (a backdrop child would cover the rows). Rows are rounded chips in
    the theme's inks; the menu fades in where the platform supports window opacity.
    """

    def __init__(self, *args):
        super().__init__(*args)
        prepare_popup(self)
        # Qt's own menu scroll/fade effects grab an opaque copy of the window; with a
        # translucent menu they flash black corners on Windows. This menu fades itself.
        QApplication.setEffectEnabled(Qt.UIEffect.UI_AnimateMenu, False)
        QApplication.setEffectEnabled(Qt.UIEffect.UI_FadeMenu, False)
        self._mode = None
        self._material_key = self._material = None
        self._fade = None
        self._style_rows()

    def addMenu(self, *args):
        """Submenus share the material; a plain QMenu would open opaque and square."""
        if args and isinstance(args[0], QMenu):
            return super().addMenu(*args)
        menu = GlassMenu(*(args[1:] if len(args) > 1 else args), self) if args else GlassMenu(self)
        if len(args) == 2:
            menu.setIcon(args[0])
        super().addMenu(menu)
        return menu

    def _style_rows(self):
        mode = _theme_name()
        if self._mode == mode:
            return
        self._mode = mode
        pal, glass = get_palette(mode), glass_tokens(mode)
        self.setStyleSheet(f"""
            QMenu {{ background: transparent; border: none; padding: 6px; color: {pal['TEXT']};
                font-size: 12px; }}
            QMenu::item {{ background: transparent; color: {pal['TEXT']}; padding: 5px 24px 5px 12px;
                margin: 1px 0; border-radius: 6px; }}
            QMenu::item:selected {{ background: {glass['chip']}; color: {glass['chip_ink']}; }}
            QMenu::item:disabled {{ color: {glass['disabled_ink']}; }}
            QMenu::separator {{ height: 1px; background: {glass['rim']}; margin: 4px 10px; }}
            QMenu::indicator {{ width: 14px; height: 14px; left: 4px; }}
        """)

    def showEvent(self, event):
        self._style_rows()
        super().showEvent(event)
        from main.qt.motion import motion_enabled
        from PySide6.QtGui import QGuiApplication
        # Window opacity exists on Windows, X11 compositors and macOS; elsewhere
        # (Wayland, offscreen) Qt warns on every frame, so the menu simply appears.
        if motion_enabled() and QGuiApplication.platformName() in ("windows", "xcb", "cocoa"):
            from PySide6.QtCore import QEasingCurve, QVariantAnimation
            self.setWindowOpacity(0.0)
            fade = QVariantAnimation(self)
            fade.setStartValue(0.0)
            fade.setEndValue(1.0)
            fade.setDuration(140)
            fade.setEasingCurve(QEasingCurve.Type.OutCubic)
            fade.valueChanged.connect(lambda value: self.setWindowOpacity(float(value)))
            fade.finished.connect(lambda: self.setWindowOpacity(1.0))
            self._fade = fade
            fade.start()

    def paintEvent(self, event):
        ratio = self.devicePixelRatioF()
        key = (self.width(), self.height(), ratio, self._mode)
        if self._material_key != key:
            self._material = frosted_pixmap(self.width(), self.height(), ratio, self._mode or "default")
            self._material_key = key
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._material)
        painter.end()
        super().paintEvent(event)
