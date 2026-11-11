"""Glass: translucent sheets over one out-of-focus scene, and native tool tabs.

Behind every window sits a defocused photograph of a circuit board, lit in the active theme's
own colors (direct/render_glass_scenes.py paints it; src/assets/glass/ holds the result). Every
surface is translucent over that scene, reading surfaces included: a sheet shows a further
blurred copy of what is behind it under a light tint, a thin rim and a soft shadow.

All of it is one cached image per window, with no graphics effect, blur pass or timer, so a
repaint costs a single blit. Web pages cannot show the window behind them, so they paint the
same two images at the same position instead (page_backdrop keeps them lined up).
"""
from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path
import random

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QDialog, QStackedWidget, QTabBar, QTabWidget, QWidget

from main.core.theme import Theme

_ASSETS = Path(__file__).resolve().parents[2] / "src" / "assets" / "glass"
# Both images of every theme have this shape, so one fit describes them all.
_SCENE_ASPECT = 640 / 400
_RESIZE_SETTLE_MS = 120
# How far a pane's shadow reaches past its edge, in logical pixels.
PANE_SHADOW = 8
# White added to a dark theme's sheet at its brightest corner, of 255. main/core/theme.py's
# GLASS_WORST depends on these, and direct/verify_glass.py checks the two against each other.
SHEET_TINT_TOP, SHEET_REFLECTION = 16, 6
# Height of the soft shading behind the menu and status text, which sit straight on the scene.
_EDGE_SHADE = 34


def pane_radius(window_height: int) -> int:
    """Generous corners, tightening only where a short window needs every pixel."""
    return 6 if window_height < 350 else 8 if window_height < 500 else 12 if window_height < 800 else 16


def pane_gutters(width: int, height: int) -> tuple[int, int, int]:
    """Clear space around panes: (window side, half the gap between rows, splitter handle)."""
    if height < 500:
        return 2, 0 if height < 350 else 1, 4
    side = 4 if width < 640 else 6 if height < 800 else 8
    return (side, 2, 6) if height < 800 else (side, 3, 6)


def bar_pane(bar: QWidget, window: QWidget) -> QRect:
    """The sheet behind a toolbar: lined up with the panes below it, inside the bar's own height."""
    side, edge, _handle = pane_gutters(window.width(), window.height())
    rect = bar.geometry()
    return QRect(side, rect.top() + edge, window.width() - 2 * side, rect.height() - 2 * edge)


def _theme_name() -> str:
    name = (Theme.active_theme or "default").lower().replace("-", "_")
    return "solarized_dark" if name in ("solarized", "solarize_dark") else name if name in Theme.PALETTES else "default"


def theme_is_light(name: str | None = None) -> bool:
    return QColor(Theme.PALETTES[name or _theme_name()]["BG_DARKEST"]).lightness() > 128


def _fallback_source(name: str) -> QImage:
    """A missing asset must not stop the application: the theme's own surface, plainly."""
    image = QImage(16, 10, QImage.Format.Format_RGB32)
    image.fill(QColor(Theme.PALETTES[name]["BG_DARK"]))
    return image


@lru_cache(maxsize=2 * len(Theme.PALETTES))
def _source(name: str, frosted: bool) -> QImage:
    path = _ASSETS / f"{name}{'-frosted' if frosted else ''}.png"
    image = QImage(str(path))
    if image.isNull():
        return _fallback_source(name)
    return image.convertToFormat(QImage.Format.Format_RGB32)


def scene_fit(width: float, height: float) -> tuple[float, float, float, float]:
    """Where the scene lands in a window: it fills the window and is centered, never stretched.

    Returns (scene width, scene height, left, top) in the window's logical pixels.
    """
    width, height = max(1.0, width), max(1.0, height)
    if width / height > _SCENE_ASPECT:
        scene_width, scene_height = width, width / _SCENE_ASPECT
    else:
        scene_width, scene_height = height * _SCENE_ASPECT, height
    return scene_width, scene_height, (width - scene_width) / 2, (height - scene_height) / 2


@lru_cache(maxsize=1)
def _grain() -> QPixmap:
    """A level or two of fine noise, which stops the dark scene banding into contour lines."""
    size = 128
    noise = random.Random(0x6c61).randbytes(size * size)
    pixels = bytearray(size * size * 4)
    for index, value in enumerate(noise):
        # Premultiplied, byte order BGRA on every supported CPU: light or dark specks at low alpha.
        alpha = (value & 3) + 1
        level = alpha if value & 8 else 0
        pixels[index * 4:index * 4 + 4] = bytes((level, level, level, alpha))
    # QImage does not own this memory, so its copy must be taken while the bytes are alive.
    data = bytes(pixels)
    return QPixmap.fromImage(QImage(data, size, size, size * 4, QImage.Format.Format_ARGB32_Premultiplied).copy())


def _render_layer(size: QSize, ratio: float, name: str, frosted: bool = False) -> QPixmap:
    """The scene at device pixels, filling this window as scene_fit says."""
    # Rounded up: at a fractional scale a pixmap one pixel short leaves a line along the edge.
    device = QSize(max(1, math.ceil(size.width() * ratio)), max(1, math.ceil(size.height() * ratio)))
    scene_width, scene_height, left, top = scene_fit(size.width(), size.height())
    source = _source(name, frosted)
    scaled = source.scaled(max(device.width(), math.ceil(scene_width * ratio)),
                           max(device.height(), math.ceil(scene_height * ratio)),
                           Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    pixmap = QPixmap.fromImage(scaled.copy(round(-left * ratio), round(-top * ratio), device.width(), device.height()))
    painter = QPainter(pixmap)
    # Device pixels: one speck of grain is one physical pixel at every display scale.
    painter.drawTiledPixmap(pixmap.rect(), _grain())
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def _shade_window_edges(painter: QPainter, size: QSize, name: str) -> None:
    """The menu and status text sit straight on the scene, so its top and bottom edges stay calm."""
    shade = QColor(Theme.PALETTES[name]["BG_DARKEST"])
    reach = min(_EDGE_SHADE, size.height() / 4)
    for start, end in ((0.0, reach), (float(size.height()), size.height() - reach)):
        gradient = QLinearGradient(0, start, 0, end)
        shade.setAlpha(205)
        gradient.setColorAt(0, shade)
        gradient.setColorAt(.6, shade)
        shade.setAlpha(0)
        gradient.setColorAt(1, shade)
        painter.fillRect(QRectF(0, min(start, end), size.width(), reach), gradient)


def _veil_color(name: str) -> QColor:
    """Smoke over a whole window, enough that any ink readable in a reading well reads here too."""
    if theme_is_light(name):
        return QColor(255, 255, 255, 190)
    color = QColor(Theme.PALETTES[name]["BG_DARKEST"])
    color.setAlpha(150)
    return color


def _render_scene(size: QSize, ratio: float, name: str, veiled: bool = False) -> QPixmap:
    """What a window shows where no sheet covers it, or all of it for a veiled window."""
    pixmap = _render_layer(size, ratio, name, frosted=veiled)
    painter = QPainter(pixmap)
    if veiled:
        painter.fillRect(QRectF(0, 0, size.width() + 1, size.height() + 1), _veil_color(name))
    else:
        _shade_window_edges(painter, size, name)
    painter.end()
    return pixmap


def _pane_path(width: float, height: float, radius: float, ratio: float, margin: float = 0.0) -> QPainterPath:
    # On a half device pixel, so the rim is one crisp pixel at any display scale.
    inset = .5 / ratio
    path = QPainterPath()
    bounds = QRectF(margin + inset, margin + inset, width - 2 * inset, height - 2 * inset)
    radius = min(radius, bounds.width() / 2, bounds.height() / 2)
    path.addRoundedRect(bounds, radius, radius)
    return path


@lru_cache(maxsize=32)
def _shadow_image(width: int, height: int, radius: float, ratio: float, light: bool) -> QImage:
    """A pane's soft shadow, PANE_SHADOW logical pixels larger on every side and hollow inside."""
    margin = PANE_SHADOW
    image = QImage(math.ceil((width + 2 * margin) * ratio), math.ceil((height + 2 * margin) * ratio),
                   QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(ratio)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    outside = QPainterPath()
    outside.addRect(QRectF(0, 0, width + 2 * margin, height + 2 * margin))
    # The shadow stops at the pane's edge, or it would darken what shows through the pane.
    painter.setClipPath(outside.subtracted(_pane_path(width, height, radius, ratio, margin)))
    painter.setPen(Qt.PenStyle.NoPen)
    radius = min(radius, width / 2, height / 2)
    for spread in range(margin, 0, -1):
        share = (1 - spread / margin) ** 2
        painter.setBrush(QColor(0, 0, 0, round((6 if light else 20) * share) + 1))
        painter.drawRoundedRect(QRectF(margin - spread, margin - spread + 2, width + 2 * spread, height + 2 * spread),
                                radius + spread, radius + spread)
    painter.end()
    return image


@lru_cache(maxsize=32)
def _sheet_image(width: int, height: int, radius: float, ratio: float, light: bool) -> QImage:
    """One sheet of glass without what is behind it: its tint, reflection, bevel and rim."""
    image = QImage(math.ceil(width * ratio), math.ceil(height * ratio), QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(ratio)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = _pane_path(width, height, radius, ratio)
    bounds = path.boundingRect()
    radius = min(radius, bounds.width() / 2, bounds.height() / 2)

    # The tint: strongest at the top, where light enters the sheet. Enough to lift the sheet
    # off the scene and little enough that the scene stays what you see.
    body = QLinearGradient(0, bounds.top(), 0, bounds.bottom())
    body.setColorAt(0, QColor(255, 255, 255, 132 if light else SHEET_TINT_TOP))
    body.setColorAt(1, QColor(255, 255, 255, 92 if light else 10))
    painter.fillPath(path, body)
    painter.save()
    painter.setClipPath(path)
    # A broad reflection with no edge of its own: it fades out long before the far corner.
    reflection = QLinearGradient(bounds.topLeft(), bounds.topLeft() + QPointF(width * .5, height * .9))
    reflection.setColorAt(0, QColor(255, 255, 255, 54 if light else SHEET_REFLECTION))
    reflection.setColorAt(1, QColor(255, 255, 255, 0))
    painter.fillPath(path, reflection)
    # The bevel: a highlight just inside the top edge, gone a little way down the sheet.
    bevel = QLinearGradient(0, bounds.top(), 0, bounds.top() + min(16.0, height * .5))
    bevel.setColorAt(0, QColor(255, 255, 255, 110 if light else 46))
    bevel.setColorAt(1, QColor(255, 255, 255, 0))
    inner = QPen(bevel, 1)
    inner.setCosmetic(True)
    painter.setPen(inner)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    highlight = 1 / ratio
    painter.drawRoundedRect(bounds.adjusted(highlight, highlight, -highlight, -highlight),
                            max(0, radius - highlight), max(0, radius - highlight))
    painter.restore()

    # One device pixel wide: bright where light falls, fading along the sheet's far side.
    rim = QLinearGradient(0, bounds.top(), 0, bounds.bottom())
    rim.setColorAt(0, QColor(255, 255, 255, 240) if light else QColor(255, 255, 255, 96))
    rim.setColorAt(.5, QColor(255, 255, 255, 170) if light else QColor(255, 255, 255, 58))
    rim.setColorAt(1, QColor(31, 35, 40, 44) if light else QColor(255, 255, 255, 34))
    pen = QPen(rim, 1)
    pen.setCosmetic(True)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.end()
    return image


def pane_image(width: int, height: int, radius: float, ratio: float, light: bool) -> QImage:
    """A sheet with its shadow for a surface that paints itself, PANE_SHADOW pixels larger each way."""
    margin = PANE_SHADOW
    image = _shadow_image(width, height, radius, ratio, light).copy()
    image.setDevicePixelRatio(ratio)
    painter = QPainter(image)
    painter.drawImage(margin, margin, _sheet_image(width, height, radius, ratio, light))
    painter.end()
    return image


class GlassScene:
    """One window's backdrop. A live resize stretches what it has until the burst settles."""

    def __init__(self, owner: QWidget, veiled: bool = False):
        self._owner = owner
        self._veiled = veiled
        self._base: QPixmap | None = None
        self._frost: QPixmap | None = None
        self._composite: QPixmap | None = None
        self._scene_key = None
        self._composite_key = None
        self._settled = False
        self._settle = QTimer(owner)
        self._settle.setSingleShot(True)
        self._settle.setInterval(_RESIZE_SETTLE_MS)
        self._settle.timeout.connect(self._rebuild)

    def invalidate(self) -> None:
        self._settle.stop()
        self._base = self._frost = self._composite = None
        self._owner.update()

    def _rebuild(self) -> None:
        self._settled = True
        self._owner.update()

    def _paint_panes(self, painter: QPainter, panes, ratio: float, name: str, target: QRect) -> None:
        """Each sheet: the frosted scene clipped to its shape, then its own tint and rim."""
        light = theme_is_light(name)
        margin = PANE_SHADOW
        # Every shadow first: one pane's shadow must never fall across its neighbor.
        for rect, radius in panes:
            painter.drawImage(rect.x() - margin, rect.y() - margin,
                              _shadow_image(rect.width(), rect.height(), radius, ratio, light))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for rect, radius in panes:
            painter.save()
            painter.setClipPath(_pane_path(rect.width(), rect.height(), radius, ratio).translated(rect.topLeft()),
                                Qt.ClipOperation.IntersectClip)
            painter.drawPixmap(target, self._frost)
            painter.restore()
            painter.drawImage(rect.topLeft(), _sheet_image(rect.width(), rect.height(), radius, ratio, light))

    def paint(self, painter: QPainter, panes=()) -> None:
        owner = self._owner
        ratio = owner.devicePixelRatioF()
        name = _theme_name()
        scene_key = (owner.width(), owner.height(), ratio, name)
        panes = tuple((rect, radius) for rect, radius in panes if rect.width() > 2 and rect.height() > 2)
        composite_key = (scene_key, tuple((rect.getRect(), radius) for rect, radius in panes))
        if self._composite is not None and composite_key == self._composite_key:
            painter.drawPixmap(0, 0, self._composite)
            return
        # A new theme or monitor is not a burst, a first paint has nothing to reuse, and when
        # only the panes moved the scene is still good, so there is nothing to wait for.
        fresh = self._base is None or scene_key[2:] != self._scene_key[2:]
        if fresh or self._settled or scene_key == self._scene_key:
            self._settled = False
            self._settle.stop()
            if scene_key != self._scene_key or self._base is None:
                self._base = _render_scene(owner.size(), ratio, name, self._veiled)
                self._frost = None if self._veiled else _render_layer(owner.size(), ratio, name, frosted=True)
                self._scene_key = scene_key
            composite = QPixmap(self._base)
            layer = QPainter(composite)
            if panes:
                self._paint_panes(layer, panes, ratio, name, owner.rect())
            layer.end()
            self._composite = composite
            self._composite_key = composite_key
            painter.drawPixmap(0, 0, self._composite)
            return
        # Mid-resize: what the window already has, stretched, under sheets at their new places.
        self._settle.start()
        painter.drawPixmap(owner.rect(), self._base)
        if panes and self._frost is not None:
            self._paint_panes(painter, panes, ratio, name, owner.rect())


def paint_scene(widget: QWidget, panes=(), *, veiled: bool = False) -> None:
    """From a paintEvent: the backdrop, then a sheet of glass for each (rect, radius).

    A veiled window is one sheet from edge to edge, for content with no panes of its own.
    """
    scene = getattr(widget, "_glass_scene", None)
    if scene is None:
        scene = widget._glass_scene = GlassScene(widget, veiled)
    painter = QPainter(widget)
    scene.paint(painter, panes)
    painter.end()


def paint_cover(widget: QWidget, painter: QPainter) -> bool:
    """For a widget covering a web view: what its window shows there, which the view would hide."""
    window = widget.window()
    scene = getattr(window, "_glass_scene", None)
    composite = scene._composite if scene is not None else None
    if composite is None:
        return False
    ratio = composite.devicePixelRatio()
    origin = widget.mapTo(window, QPoint())
    painter.drawPixmap(QRectF(widget.rect()), composite,
                       QRectF(origin.x() * ratio, origin.y() * ratio, widget.width() * ratio, widget.height() * ratio))
    return True


def invalidate_scene(widget: QWidget) -> None:
    scene = getattr(widget, "_glass_scene", None)
    if scene is not None:
        scene.invalidate()


def page_surface(theme_name: str) -> dict[str, str]:
    """What a terminal page paints under its own text, in CSS terms."""
    from main.core.theme import _palette_key
    from main.qt.theme import css_color, glass_tokens
    name = _palette_key(theme_name)
    tokens = glass_tokens(name)
    return {"frost": f"../assets/glass/{name}-frosted.png", "fallback": Theme.PALETTES[name]["BG_DARK"],
            "sheet": "rgba(255, 255, 255, 0.4)" if theme_is_light(name) else "rgba(255, 255, 255, 0.05)",
            "reading": css_color(tokens["reading"]), "readingRim": css_color(tokens["reading_rim"])}


def page_backdrop_for(view: QWidget) -> dict[str, float]:
    """Numbers that line a web page's copy of the scene up with its window's.

    ``hostWidth``/``hostHeight`` are the view's own logical size. At a fractional display
    scale Chromium rounds the page's surface up to whole device pixels, so its viewport
    comes out a fraction to a whole CSS pixel larger than the widget showing it (125%:
    every size; 150%: most). A sheet filling that viewport loses its right and bottom
    rim past the widget's edge, so the page sizes its sheet to these numbers instead.
    """
    window = view.window()
    origin = view.mapTo(window, QPoint())
    width, height, left, top = scene_fit(window.width(), window.height())
    return {"width": round(width, 2), "height": round(height, 2),
            "x": round(left - origin.x(), 2), "y": round(top - origin.y(), 2),
            "radius": pane_radius(window.height()),
            "hostWidth": view.width(), "hostHeight": view.height()}


class PageBackdrop(QObject):
    """Keeps one web view's copy of the scene aligned while the view or anything above it moves."""

    _WATCHED = (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.ParentChange)

    def __init__(self, view: QWidget, apply):
        super().__init__(view)
        self._view = view
        self._apply = apply
        self._last = None
        self._watched: list[QWidget] = []
        self._pending = False
        self._watch()
        self.refresh()

    def _watch(self) -> None:
        chain = []
        widget = self._view
        while widget is not None:
            chain.append(widget)
            widget = None if widget.isWindow() else widget.parentWidget()
        if chain == self._watched:
            return
        for widget in self._watched:
            widget.removeEventFilter(self)
        for widget in chain:
            widget.installEventFilter(self)
        self._watched = chain

    def eventFilter(self, watched, event) -> bool:
        if event.type() in self._WATCHED and not self._pending:
            self._pending = True
            QTimer.singleShot(0, self.refresh)
        return False

    def refresh(self) -> None:
        self._pending = False
        self._watch()
        values = page_backdrop_for(self._view)
        if values != self._last:
            self._last = values
            self._apply(values)

    def forget(self) -> None:
        """The page was replaced, so whatever it was told is gone with it."""
        self._last = None
        self.refresh()


class GlassWorkspace(QWidget):
    """A plain container that shows the backdrop, for windows whose content sits inside it."""

    def invalidate_cache(self):
        invalidate_scene(self)

    def paintEvent(self, event):
        paint_scene(self, veiled=True)


class GlassDialog(QDialog):
    """A dialog that is one sheet of smoked glass over the theme's scene."""

    def paintEvent(self, event):
        paint_scene(self, veiled=True)


class _RepaintsWindow:
    """The window paints the sheet under this widget, so it has to follow the widget around."""

    def moveEvent(self, event):
        super().moveEvent(event)
        self.window().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.window().update()

    def showEvent(self, event):
        super().showEvent(event)
        self.window().update()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.window().update()


class PaneContainer(_RepaintsWindow, QWidget):
    pass


class PaneStack(_RepaintsWindow, QStackedWidget):
    pass


class PaneTabs(_RepaintsWindow, QTabWidget):
    pass


def pane_for(widget: QWidget, window: QWidget, grow: int = 0) -> QRect:
    """The widget's area in its window, optionally enlarged so a rim shows around it."""
    return QRect(widget.mapTo(window, widget.rect().topLeft()), widget.size()).adjusted(-grow, -grow, grow, grow)


def _qcolor(value: str) -> QColor:
    """A QColor from a stylesheet color: #rrggbb or rgba() with 0-255 alpha."""
    value = str(value).strip()
    if value.startswith("rgba("):
        red, green, blue, alpha = (int(float(part)) for part in value[5:-1].split(","))
        return QColor(red, green, blue, alpha)
    return QColor(value)


def _gradient_stops(value: str) -> tuple[str, str]:
    """The two stops of a theme's vertical qlineargradient token."""
    import re
    stops = re.findall(r"stop:\s*[01]\s+(rgba\([^)]*\)|#[0-9a-fA-F]{6})", str(value))
    return (stops[0], stops[-1]) if stops else (value, value)


class WorkspaceTabBar(QTabBar):
    """Tool tabs switch by mouse without taking focus from the editor or terminal.

    In compact (icon-only) panes the selected tool slides open to show its name
    beside its icon while the others stay icons. The owner enables that only when
    the widest name fits the pane, so the strip never needs to scroll.
    """

    LABEL_GAP = 6
    LABEL_TAIL = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workspace-tab-bar")
        self.setAccessibleName("Workspace tools")
        self.setExpanding(False)
        self.setDrawBase(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._labels: list[str] | None = None
        self._reveal: dict[int, float] = {}
        self._reveal_animation = None
        self.currentChanged.connect(self._slide_to_current)

    # ── Compact labels ──────────────────────────────────────────────────────
    def _label_font(self):
        from PySide6.QtGui import QFont
        font = QFont(self.font())
        font.setPixelSize(12)  # QTabBar::tab in the workspace stylesheet.
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def label_extent(self, text: str) -> int:
        """Width a revealed name adds to an icon tab, from the same font it is painted in."""
        from PySide6.QtGui import QFontMetrics
        return QFontMetrics(self._label_font()).horizontalAdvance(text) + self.LABEL_GAP + self.LABEL_TAIL + 1

    def compact_labels(self) -> list[str] | None:
        return list(self._labels) if self._labels is not None else None

    def set_compact_labels(self, labels) -> None:
        """Names to reveal for the selected icon tab, or None for plain icon/text tabs."""
        labels = list(labels) if labels else None
        if labels == self._labels:
            return
        self._labels = labels
        self._stop_reveal()
        current = self.currentIndex()
        self._reveal = {current: 1.0} if labels is not None and current >= 0 else {}
        self._relayout()

    def _stop_reveal(self) -> None:
        animation, self._reveal_animation = self._reveal_animation, None
        if animation is not None:
            animation.stop()
            animation.deleteLater()

    def _slide_to_current(self, index: int) -> None:
        if self._labels is None:
            return
        from main.qt.motion import TAB_DURATION_MS, motion_enabled
        self._stop_reveal()
        targets = {index: 1.0} if index >= 0 else {}
        if not motion_enabled():
            self._reveal = targets
            self._relayout()
            return
        start = dict(self._reveal)
        keys = set(start) | set(targets)
        from PySide6.QtCore import QEasingCurve, QVariantAnimation
        animation = QVariantAnimation(self)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setDuration(TAB_DURATION_MS)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        def step(value, keys=keys, start=start, targets=targets):
            progress = float(value)
            self._reveal = {key: start.get(key, 0.0) + (targets.get(key, 0.0) - start.get(key, 0.0)) * progress
                            for key in keys}
            self._relayout()

        def finished(targets=targets):
            self._reveal = dict(targets)
            self._reveal_animation = None
            self._relayout()

        animation.valueChanged.connect(step)
        animation.finished.connect(finished)
        self._reveal_animation = animation
        animation.start()

    def revealing(self) -> bool:
        return self._reveal_animation is not None

    def _relayout(self) -> None:
        # setIconSize marks Qt's tab layout dirty, so every tab rect follows tabSizeHint.
        self.setIconSize(self.iconSize())
        self.update()

    def _extra(self, index: int) -> int:
        if self._labels is None or not 0 <= index < len(self._labels):
            return 0
        progress = self._reveal.get(index, 0.0)
        return round(progress * self.label_extent(self._labels[index])) if progress > 0 else 0

    def _paint_chip(self, painter, rect, selected, hovered, pal, glass) -> None:
        from PySide6.QtGui import QBrush
        compact = bool(self.parent() is not None and self.parent().property("compactPane"))
        top, right, bottom = (3, 3, 2) if compact else (5, 4, 4)
        box = QRectF(rect.adjusted(0, top, -right, -bottom)).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if selected:
            fill = QBrush(_qcolor(glass["chip"]))
            rim = _qcolor(glass["chip"])
        else:
            upper, lower = ((_gradient_stops(glass["button_hover"])) if hovered
                            else (glass["glass_highlight"], glass["glass"]))
            gradient = QLinearGradient(box.topLeft(), box.bottomLeft())
            gradient.setColorAt(0, _qcolor(upper))
            gradient.setColorAt(1, _qcolor(lower))
            fill = QBrush(gradient)
            rim = _qcolor(pal.get("BORDER_LIT", Theme.CYAN) if hovered else glass["rim"])
        painter.setPen(QPen(rim, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(box, 11, 11)
        painter.restore()

    def tabSizeHint(self, index):
        hint = super().tabSizeHint(index)
        extra = self._extra(index)
        if extra:
            hint.setWidth(hint.width() + extra)
        return hint

    def paintEvent(self, event):
        if self._labels is None:
            super().paintEvent(event)
            return
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QStyle, QStyleOptionTab, QStylePainter
        painter = QStylePainter(self)
        font = self._label_font()
        from main.qt.theme import get_palette, glass_tokens
        mode = _theme_name()
        pal, glass = get_palette(mode), glass_tokens(mode)
        current, hovered = self.currentIndex(), self.tabAt(self.mapFromGlobal(self.cursor().pos()))
        icon_size = self.iconSize()
        for index in range(self.count()):
            option = QStyleOptionTab()
            self.initStyleOption(option, index)
            # A copy: option.icon is the option's own member, cleared on the next line.
            icon = QIcon(option.icon)
            option.icon = QIcon()
            option.text = ""
            rect = option.rect
            selected = index == current
            if self._extra(index):
                # The stylesheet fixes an icon tab's box at 18px, so a widening chip is
                # painted from the same tokens: radius, margins, fill and rim.
                self._paint_chip(painter, rect, selected, index == hovered, pal, glass)
            else:
                painter.drawControl(QStyle.ControlElement.CE_TabBarTabShape, option)
            base = rect.width() - self._extra(index)
            # The icon keeps its centred place in the collapsed chip, so it never shifts.
            x = rect.left() + (base - icon_size.width()) // 2
            y = rect.top() + (rect.height() - icon_size.height()) // 2
            state = QIcon.State.On if selected else QIcon.State.Off
            mode_icon = QIcon.Mode.Normal if self.isTabEnabled(index) else QIcon.Mode.Disabled
            icon.paint(painter, QRect(x, y, icon_size.width(), icon_size.height()),
                       Qt.AlignmentFlag.AlignCenter, mode_icon, state)
            progress = self._reveal.get(index, 0.0)
            if progress <= 0.01 or index >= len(self._labels):
                continue
            ink = QColor(glass["chip_ink"] if selected else
                         pal.get("TEXT_BRIGHT", Theme.TEXT) if index == hovered else pal.get("TEXT", Theme.TEXT))
            ink.setAlphaF(min(1.0, progress * 1.4))
            left = x + icon_size.width() + self.LABEL_GAP
            clip = QRect(left, rect.top(), max(0, rect.right() - left - 2), rect.height())
            painter.save()
            painter.setClipRect(clip)
            painter.setFont(font)
            painter.setPen(ink)
            painter.drawText(QRect(left, rect.top(), self.label_extent(self._labels[index]), rect.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self._labels[index])
            painter.restore()
        painter.end()
