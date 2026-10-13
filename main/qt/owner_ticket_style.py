"""Paper surfaces, bold ink and responsive forms for Developer Access."""
from __future__ import annotations

from math import ceil
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QPixmap, QTextDocument, QTextOption
from PySide6.QtWidgets import (
    QBoxLayout, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSizePolicy, QStyle, QStyleOptionComboBox, QTextEdit, QVBoxLayout, QWidget,
)

from main.core.theme import Theme
from main.qt.icons import icon
from main.qt.responsive import ScreenWatcher, fit_dialog
from main.qt.setup_components import VectorGlyph


def portal_colors(mode: str) -> dict[str, str]:
    """Developer Access retains its paper-and-ink identity in every workspace theme."""
    return {
        "BG_DARKEST": "#fffdf5", "BG_DARK": "#ffffff", "BG_MID": "#ffffff",
        "BG_LIGHT": "#ffffff", "BG_HOVER": "#fff5b8", "BORDER": "#141414",
        "TEXT": "#141414", "TEXT_BRIGHT": "#141414", "TEXT_DIM": "#49454b",
        "CYAN": "#135f73", "GREEN": "#126039", "YELLOW": "#735000",
        "RED": "#ab1738", "ORANGE": "#9a3b00", "BLUE": "#15529f",
    }


def portal_stylesheet(mode: str) -> str:
    c = portal_colors(mode)
    arrow = (Path(__file__).resolve().parents[2] / "src/assets/icons/developer-chevron.svg").as_posix()
    return f"""
        QDialog, QWidget#developer-portal-surface {{ background: #fffdf5; }}
        QWidget {{ font-family: 'Montserrat', 'Segoe UI', sans-serif; font-size: 12px; color: #141414; }}
        QLabel, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
        QLabel {{ padding: 0; }}
        QLabel[role="heading"], QTextEdit[wrappedLabel="true"][role="heading"] {{ font-size: 25px; font-weight: 900; color: #141414; }}
        QLabel[role="title"], QTextEdit[wrappedLabel="true"][role="title"] {{ font-size: 16px; font-weight: 800; color: #141414; }}
        QLabel[role="label"], QTextEdit[wrappedLabel="true"][role="label"] {{ font-weight: 700; }}
        QLabel[role="muted"], QLabel[role="metadata"], QTextEdit[wrappedLabel="true"][role="muted"], QTextEdit[wrappedLabel="true"][role="metadata"] {{ color: {c['TEXT_DIM']}; }}
        QLabel[role="metadata"], QTextEdit[wrappedLabel="true"][role="metadata"] {{ font-size: 11px; }}
        QLabel[role="value"] {{ font-size: 25px; font-weight: 900; }}
        QLabel[tone="active"], QTextEdit[wrappedLabel="true"][tone="active"], QComboBox[tone="active"] {{ color: {c['CYAN']}; }}
        QLabel[tone="ok"], QTextEdit[wrappedLabel="true"][tone="ok"], QComboBox[tone="ok"] {{ color: {c['GREEN']}; }}
        QLabel[tone="warn"], QTextEdit[wrappedLabel="true"][tone="warn"], QComboBox[tone="warn"] {{ color: {c['YELLOW']}; }}
        QLabel[tone="fail"], QTextEdit[wrappedLabel="true"][tone="fail"], QComboBox[tone="fail"] {{ color: {c['RED']}; }}
        QLabel[tone="high"], QTextEdit[wrappedLabel="true"][tone="high"], QComboBox[tone="high"] {{ color: {c['ORANGE']}; }}
        QLineEdit, QTextEdit, QComboBox {{
            background: #ffffff; color: #141414; border: 2px solid #141414;
            border-radius: 2px; padding: 9px; selection-background-color: #ff91d5; selection-color: #141414;
        }}
        QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{ border-color: #2667c9; }}
        QTextEdit[wrappedLabel="true"], QTextEdit[wrappedLabel="true"]:focus {{
            background: transparent; border: none; border-radius: 0; padding: 0;
        }}
        QLabel[role="feedback"], QTextEdit[wrappedLabel="true"][role="feedback"] {{
            background: #ffe4f4; border: 2px solid #141414; border-radius: 2px; padding: 10px;
        }}
        QComboBox {{ padding-right: 28px; }}
        QComboBox::drop-down {{ border: none; width: 24px; }}
        QComboBox::down-arrow {{ image: url("{arrow}"); width: 10px; height: 10px; }}
        QComboBox QAbstractItemView {{
            background: #ffffff; color: #141414; border: 2px solid #141414;
            selection-background-color: #ffe85b; selection-color: #141414; padding: 4px;
        }}
        QPushButton {{
            background: #ffffff; color: #141414; border: 2px solid #141414;
            border-radius: 2px; padding: 9px 12px; font-weight: 800;
        }}
        QPushButton:hover {{ background: #ffe85b; }}
        QPushButton:pressed {{ background: #80c8ff; }}
        QPushButton:focus {{ border: 2px dashed #2667c9; }}
        QPushButton[role="primary"] {{ background: #ffe85b; }}
        QPushButton[role="primary"]:hover {{ background: #ff91d5; }}
        QPushButton[role="icon"] {{ padding: 5px; }}
        QPushButton[role="category-add"] {{ background: #80c8ff; }}
        QPushButton:disabled {{ background: #e7e3e5; color: #625d63; }}
        QWidget[role="metric"] {{ border: 2px solid #141414; background: #e7f5ff; }}
        QWidget[role="metric"][tone="fail"] {{ background: #ffe4f4; }}
        QWidget[role="metric"][tone="warn"] {{ background: #fff5b8; }}
        QWidget[role="metric"][tone="ok"] {{ background: #e4f5e9; }}
        QWidget[role="metric"] QLabel {{ border: none; background: transparent; }}
        QLabel[role="session"] {{ background: #e4f5e9; border: 2px solid #141414; padding: 5px 8px; font-weight: 700; }}
        QLabel[role="session"][tone="active"] {{ background: #e7f5ff; }}
        QLabel[role="session"][tone="fail"] {{ background: #ffe4f4; }}
        QLabel[role="session"][tone="warn"] {{ background: #fff5b8; }}
        QLabel[role="session"][tone="muted"] {{ background: #e7e3e5; }}
        QTextEdit[wrappedLabel="true"][role="severity"] {{ border: 2px solid #141414; background: #fff5b8; padding: 5px; font-weight: 800; }}
        QTextEdit[wrappedLabel="true"][role="severity"][tone="fail"] {{ background: #ffe4f4; }}
        QTextEdit[wrappedLabel="true"][role="severity"][tone="ok"] {{ background: #e4f5e9; }}
        QScrollBar:vertical {{ background: #fffdf5; width: 10px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: #141414; border-radius: 0; min-height: 28px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        QMenu {{ background: #ffffff; color: #141414; border: 2px solid #141414; padding: 5px; }}
        QMenu::item {{ padding: 8px 16px; }}
        QMenu::item:selected {{ background: #ffe85b; }}
        QWidget#developer-login-page QLabel#developer-connection-note,
        QWidget#developer-login-page QTextEdit#developer-connection-note {{
            background: #80c8ff; color: #141414; border: 2px solid #141414; padding: 10px;
        }}
        QWidget#developer-login-page QLabel[role="feedback"],
        QWidget#developer-login-page QTextEdit[wrappedLabel="true"][role="feedback"] {{
            background: #ff91d5; color: #141414; border: 2px solid #141414; padding: 10px;
        }}
    """


class NeoCard(QFrame):
    """Solid paper, a thick ink border and a hard offset shadow; no glass layers."""

    def __init__(self, parent=None, *, accent=False, fill="#ffffff"):
        super().__init__(parent)
        self._fill = QColor(fill)
        self._accent = accent
        self._cached_surface = None
        self._cache_key = None
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._cached_surface = None

    def paintEvent(self, event):
        if self.width() <= 0 or self.height() <= 0:
            return
        dpr = self.devicePixelRatioF()
        key = (self.size(), dpr)
        if self._cached_surface is None or self._cache_key != key:
            self._cached_surface = QPixmap(ceil(self.width() * dpr), ceil(self.height() * dpr))
            self._cached_surface.setDevicePixelRatio(dpr)
            self._cached_surface.fill(Qt.GlobalColor.transparent)
            painter = QPainter(self._cached_surface)
            bounds = QRectF(self.rect()).adjusted(2, 2, -9, -9)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#141414"))
            painter.drawRect(bounds.translated(7, 7))
            painter.setPen(QPen(QColor("#141414"), 3))
            painter.setBrush(self._fill)
            painter.drawRect(bounds)
            if self._accent:
                painter.fillRect(bounds.adjusted(2, 2, -2, -bounds.height() + 8), QColor("#ffe85b"))
            painter.end()
            self._cache_key = key
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._cached_surface)


class WrappedLabel(QTextEdit):
    """Selectable literal reading text whose minimum width never follows a token.

    QLabel's word wrap makes an uninterrupted word the minimum layout width.
    A plain-text document can fall back to character wrapping without inserting
    characters into copied text. A separate measuring document keeps selection
    and the displayed document untouched while layouts request other widths.
    """

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setProperty("wrappedLabel", True)
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setFrameShape(QTextEdit.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.viewport().setAutoFillBackground(False)
        self.document().setDocumentMargin(0)
        self._measure = QTextDocument(self)
        self._measure.setDocumentMargin(0)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self._measure.setDefaultTextOption(option)
        self._heights = {}
        self._measurement_refresh = QTimer(self)
        self._measurement_refresh.setSingleShot(True)
        self._measurement_refresh.timeout.connect(self._refresh_measurement)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setPlainText(text)
        self.textChanged.connect(self._invalidate_measurement)
        self._refresh_measurement()
        self.installEventFilter(self)

    def text(self):
        return self.toPlainText()

    def setText(self, text):
        self.setPlainText(text)

    def setTextInteractionFlags(self, flags):
        super().setTextInteractionFlags(flags)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus if flags & Qt.TextInteractionFlag.TextSelectableByKeyboard
                            else Qt.FocusPolicy.NoFocus)

    def setReadingHeightLimit(self, height):
        """Keep large literal content reachable without a giant card surface."""
        self.setMaximumHeight(height)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.updateGeometry()

    def _invalidate_measurement(self):
        if not self._measurement_refresh.isActive():
            self._measurement_refresh.start(0)

    def _refresh_measurement(self):
        # Qt can request geometry while delivering its native font/style event.
        # Mutate the separate document only after that event has returned.
        self._heights.clear()
        self._measure.setDefaultFont(self.font())
        self._measure.setPlainText(self.toPlainText())
        self.updateGeometry()

    def eventFilter(self, watched, event):
        if watched is self and event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._invalidate_measurement()
        return False

    def heightForWidth(self, width):
        horizontal = max(0, self.width() - self.viewport().width())
        vertical = max(0, self.height() - self.viewport().height())
        key = max(1, width - horizontal), vertical
        if self._measurement_refresh.isActive():
            # A native style event may request a width we have not seen before.
            # Reuse the last complete height until the queued refresh relayouts.
            previous = self._heights.get(key, max(self._heights.values(),
                                                 default=self.fontMetrics().height() + vertical))
            return min(self.maximumHeight(), previous)
        if key not in self._heights:
            self._measure.setTextWidth(key[0])
            # Keep a bounded cache: a resize must not retain every visited width.
            if len(self._heights) >= 8:
                self._heights.clear()
            self._heights[key] = ceil(self._measure.size().height()) + vertical
        return min(self.maximumHeight(), self._heights[key])

    def minimumSizeHint(self):
        return QSize(1, self.fontMetrics().height())

    def sizeHint(self):
        width = min(520, max(1, self.width()))
        return QSize(width, self.heightForWidth(width))


def label(text: str, parent: QWidget, role: str = "", *, wrap: bool = False) -> QLabel | WrappedLabel:
    result = WrappedLabel(text, parent) if wrap else QLabel(text, parent)
    if not wrap:
        result.setTextFormat(Qt.TextFormat.PlainText)
    result.setProperty("role", role)
    result.setMinimumWidth(0)
    return result


def retone(widget: QWidget, tone: str) -> None:
    widget.setProperty("tone", tone)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def button(text: str, parent: QWidget, callback=None, *, role: str = "", vector: str = "") -> QPushButton:
    result = QPushButton(text, parent)
    result.setProperty("role", role)
    result.setProperty("vector", vector)
    result.setAutoDefault(False)
    result.setDefault(False)
    result.setMinimumHeight(36)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    if vector:
        result.setIcon(icon(vector))
        result.setIconSize(QSize(16, 16))
    if callback is not None:
        result.clicked.connect(callback)
    return result


class PortalComboBox(QComboBox):
    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        # Measure native/styled chrome after the effective font is applied;
        # Qt's minimum-content policy alone clips statuses by a few pixels.
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        edit_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option,
            QStyle.SubControl.SC_ComboBoxEditField, self)
        chrome = max(64, self.rect().width() - edit_rect.width())
        width = max((self.fontMetrics().horizontalAdvance(self.itemText(index))
                     for index in range(self.count())), default=0) + chrome + 8
        # Unknown server-supplied classifications must not enlarge a form.
        # Native combo text can elide; the full current value stays in its tooltip.
        size.setWidth(min(self.maximumWidth(), max(120, min(width, 220))))
        return size

    def sizeHint(self):
        size = super().sizeHint().expandedTo(self.minimumSizeHint())
        size.setWidth(min(size.width(), self.maximumWidth(), 220))
        return size


def combo(items, parent, current="") -> QComboBox:
    result = PortalComboBox(parent)
    result.addItems(items)
    if current and current not in items:
        result.addItem(current)
    if current:
        result.setCurrentText(current)
    for index in range(result.count()):
        result.setItemData(index, result.itemText(index), Qt.ItemDataRole.ToolTipRole)
    result.view().setTextElideMode(Qt.TextElideMode.ElideRight)
    result.setMinimumWidth(0)
    result.setMinimumContentsLength(3)
    result.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    result.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
    result.setToolTip(result.currentText())
    result.currentTextChanged.connect(result.setToolTip)
    return result


def field(caption: str, control: QWidget, parent: QWidget) -> QWidget:
    box = QWidget(parent)
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(7)
    caption_label = label(caption, box, "label")
    caption_label.setBuddy(control)
    control.setAccessibleName(caption)
    control.setParent(box)
    layout.addWidget(caption_label)
    layout.addWidget(control)
    return box


class ResponsiveGrid(QWidget):
    """Reflow the same widgets, retaining focus, selection and edited values."""

    def __init__(self, widgets, parent=None, *, columns=3, cell_width=190):
        super().__init__(parent)
        self._widgets = widgets
        self._max_columns = columns
        self._cell_width = cell_width
        self._columns = 0
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(12)
        self._reflow()

    def _reflow(self):
        cell_width = max(self._cell_width, max((widget.minimumSizeHint().width()
                                               for widget in self._widgets), default=0))
        columns = min(self._max_columns, max(1, (self.width() + 12) // (cell_width + 12)))
        if columns == self._columns:
            return
        while self.grid.count():
            self.grid.takeAt(0)
        for column in range(self._max_columns):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)
        for index, widget in enumerate(self._widgets):
            self.grid.addWidget(widget, index // columns, index % columns)
        self._columns = columns
        self.updateGeometry()

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        # A currently wide grid must still be allowed to shrink into one column;
        # otherwise its old columns can keep a scroll body wider than its viewport.
        size.setWidth(max((widget.minimumSizeHint().width() for widget in self._widgets), default=0))
        return size

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()


class ResponsiveRow(QWidget):
    """Stack a compact row without replacing its controls or focus scope."""

    def __init__(self, widgets, parent=None, *, compact_width=440):
        super().__init__(parent)
        self._widgets = widgets
        self._compact_width = compact_width
        self.row = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(10)
        for widget, stretch in widgets:
            self.row.addWidget(widget, stretch)
        self._reflow()

    def _reflow(self):
        compact = self.width() < self._compact_width
        direction = QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight
        if self.row.direction() != direction:
            self.row.setDirection(direction)
            self.updateGeometry()
        for index, (_, stretch) in enumerate(self._widgets):
            self.row.setStretch(index, 0 if compact else stretch)

    def minimumSizeHint(self):
        size = super().minimumSizeHint()
        size.setWidth(max((widget.minimumSizeHint().width() for widget, _ in self._widgets), default=0))
        return size

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()


def scroll_body(parent: QWidget) -> tuple[QScrollArea, QWidget, QVBoxLayout]:
    scroll = QScrollArea(parent)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    body = QWidget(scroll)
    layout = QVBoxLayout(body)
    layout.setContentsMargins(2, 2, 12, 12)
    layout.setSpacing(16)
    scroll.setWidget(body)
    return scroll, body, layout


def refresh_portal_children(root: QWidget, mode: str):
    c = portal_colors(mode)
    for glyph in root.findChildren(VectorGlyph):
        glyph.set_icon(glyph._name, c["CYAN"])
    for control in root.findChildren(QLineEdit) + root.findChildren(QTextEdit):
        palette = control.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["TEXT_DIM"]))
        control.setPalette(palette)
    for action in root.findChildren(QPushButton):
        vector = action.property("vector")
        if vector:
            ink = c["TEXT"]
            action.setIcon(icon(vector, ink))


class PortalDialog(QDialog):
    """Developer dialogs share a solid paper surface and neobrutalist controls."""

    def __init__(self, parent=None, *, preferred=(880, 740)):
        super().__init__(parent)
        self._theme_name = Theme.active_theme
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.backdrop = QWidget(self)
        self.backdrop.setObjectName("developer-portal-surface")
        self.backdrop.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer.addWidget(self.backdrop)
        self.content = QVBoxLayout(self.backdrop)
        self.content.setContentsMargins(24, 20, 24, 20)
        self.content.setSpacing(18)
        fit_dialog(self, preferred=preferred, minimum=(360, 300))
        self._screen_watcher = ScreenWatcher(self)

    def apply_theme(self, mode: str):
        self._theme_name = mode
        self.setStyleSheet(portal_stylesheet(mode))
        refresh_portal_children(self, mode)
        self.backdrop.update()
        for dialog in self.findChildren(PortalDialog):
            if dialog.parentWidget() is self:
                dialog.apply_theme(mode)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "content"):
            margin = 14 if self.width() < 560 else 24
            self.content.setContentsMargins(margin, 16, margin, 16)


def heading(parent: QWidget, title: str, subtitle: str = "") -> QWidget:
    widget = QWidget(parent)
    row = QHBoxLayout(widget)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(14)
    row.addWidget(VectorGlyph("brand", widget, size=38))
    text = QVBoxLayout()
    text.setSpacing(5)
    text.addWidget(label(title, widget, "heading", wrap=True))
    if subtitle:
        text.addWidget(label(subtitle, widget, "muted", wrap=True))
    row.addLayout(text, 1)
    return widget
