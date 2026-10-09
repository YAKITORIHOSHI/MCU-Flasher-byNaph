"""Static glass, readable inks and responsive forms for the developer portal."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSizePolicy, QStyle, QStyleOptionComboBox, QTextEdit, QVBoxLayout, QWidget,
)

from main.core.log_colors import contrast_ratio
from main.core.theme import Theme, get_palette
from main.qt.glass import GlassWorkspace
from main.qt.icons import icon
from main.qt.responsive import ScreenWatcher, fit_dialog
from main.qt.setup_components import GlassCard, VectorGlyph


def portal_colors(mode: str) -> dict[str, str]:
    """Keep text readable even under the shared card's brightest reflection."""
    colors = dict(get_palette(mode))
    light = QColor(colors["BG_DARKEST"]).lightness() > 128
    base = QColor(colors["BG_LIGHT"])
    alpha = (92 if light else 46) / 255
    reflection = QColor(*[round(c * (1 - alpha) + 255 * alpha)
                          for c in (base.red(), base.green(), base.blue())]).name()
    surfaces = [colors[key] for key in ("BG_DARKEST", "BG_DARK", "BG_MID", "BG_LIGHT", "BG_HOVER")]
    surfaces.append(reflection)
    for key in ("TEXT", "TEXT_DIM", "TEXT_BRIGHT", "CYAN", "GREEN", "YELLOW", "RED", "ORANGE", "BLUE"):
        value = QColor(colors[key])
        for _ in range(36):
            if min(contrast_ratio(value.name(), bg) for bg in surfaces) >= 4.5:
                break
            value = value.darker(110) if light else value.lighter(110)
        else:
            value = QColor("#000000" if light else "#ffffff")
        colors[key] = value.name()
    return colors


def portal_stylesheet(mode: str) -> str:
    c = portal_colors(mode)
    accent = get_palette(mode)["CYAN"]
    primary_ink = max(("#101b2a", "#ffffff"), key=lambda ink: contrast_ratio(ink, accent))
    arrow = (Path(__file__).resolve().parents[2] / "src/assets/icons/chevron-down.svg").as_posix()
    return f"""
        QDialog {{ background: {c['BG_DARKEST']}; }}
        QWidget {{ font-family: 'Montserrat', 'Segoe UI', sans-serif; font-size: 12px; color: {c['TEXT']}; }}
        QLabel, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
        QLabel {{ padding: 0; }}
        QLabel[role="heading"] {{ font-size: 24px; font-weight: 700; color: {c['TEXT_BRIGHT']}; }}
        QLabel[role="title"] {{ font-size: 16px; font-weight: 700; color: {c['TEXT_BRIGHT']}; }}
        QLabel[role="label"] {{ font-weight: 600; }}
        QLabel[role="muted"], QLabel[role="metadata"] {{ color: {c['TEXT_DIM']}; }}
        QLabel[role="metadata"] {{ font-size: 11px; }}
        QLabel[role="value"] {{ font-size: 23px; font-weight: 700; }}
        QLabel[tone="active"] {{ color: {c['CYAN']}; }}
        QLabel[tone="ok"] {{ color: {c['GREEN']}; }}
        QLabel[tone="warn"] {{ color: {c['YELLOW']}; }}
        QLabel[tone="fail"] {{ color: {c['RED']}; }}
        QLabel[tone="high"] {{ color: {c['ORANGE']}; }}
        QLabel[role="feedback"] {{ background: {c['BG_DARKEST']}; border-radius: 8px; padding: 10px; }}
        QLineEdit, QTextEdit, QComboBox {{
            background: {c['BG_DARKEST']}; color: {c['TEXT']};
            border: 1px solid {c['BORDER']}; border-radius: 8px; padding: 9px 11px;
            selection-background-color: {c['BG_HOVER']}; selection-color: {c['TEXT_BRIGHT']};
        }}
        QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{ border: 1px solid {accent}; }}
        QComboBox {{ padding-right: 28px; }}
        QComboBox::drop-down {{ border: none; width: 24px; }}
        QComboBox::down-arrow {{ image: url("{arrow}"); width: 10px; height: 10px; }}
        QComboBox QAbstractItemView {{
            background: {c['BG_DARK']}; color: {c['TEXT']}; border: 1px solid {c['BORDER']};
            selection-background-color: {c['BG_HOVER']}; selection-color: {c['TEXT_BRIGHT']}; padding: 4px;
        }}
        QComboBox[tone="active"] {{ color: {c['CYAN']}; }}
        QComboBox[tone="ok"] {{ color: {c['GREEN']}; }}
        QComboBox[tone="warn"] {{ color: {c['YELLOW']}; }}
        QComboBox[tone="fail"] {{ color: {c['RED']}; }}
        QComboBox[tone="high"] {{ color: {c['ORANGE']}; }}
        QPushButton {{
            background: {c['BG_MID']}; border: 1px solid {c['BORDER']}; border-radius: 8px;
            padding: 9px 13px; font-weight: 600; color: {c['TEXT']};
        }}
        QPushButton:hover {{ background: {c['BG_HOVER']}; border-color: {accent}; }}
        QPushButton:focus {{ border: 2px solid {accent}; padding: 8px 12px; }}
        QPushButton[role="primary"] {{ background: {accent}; color: {primary_ink}; border-color: {accent}; }}
        QPushButton[role="primary"]:hover {{ background: {QColor(accent).lighter(108).name()}; }}
        QPushButton[role="quiet"] {{ background: transparent; }}
        QPushButton[role="icon"] {{ background: transparent; border-color: transparent; padding: 6px; }}
        QPushButton[role="icon"]:hover, QPushButton[role="icon"]:focus {{ background: {c['BG_HOVER']}; border-color: {accent}; }}
        QPushButton:disabled {{ color: {c['TEXT_DIM']}; background: {c['BG_DARK']}; border-color: {c['BORDER']}; }}
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
        QScrollBar::handle:vertical {{ background: {c['BORDER']}; border-radius: 3px; min-height: 28px; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        QMenu {{ background: {c['BG_DARK']}; border: 1px solid {c['BORDER']}; padding: 5px; }}
        QMenu::item {{ padding: 8px 16px; }}
        QMenu::item:selected {{ background: {c['BG_HOVER']}; }}
    """


def label(text: str, parent: QWidget, role: str = "", *, wrap: bool = False) -> QLabel:
    result = QLabel(text, parent)
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(wrap)
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
        size.setWidth(max(size.width(), width))
        return size

    def sizeHint(self):
        return super().sizeHint().expandedTo(self.minimumSizeHint())


def combo(items, parent, current="") -> QComboBox:
    result = PortalComboBox(parent)
    result.addItems(items)
    if current and current not in items:
        result.addItem(current)
    if current:
        result.setCurrentText(current)
    result.setMinimumWidth(0)
    result.setMinimumContentsLength(3)
    result.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    result.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    result.setCursor(Qt.CursorShape.PointingHandCursor)
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
        columns = min(self._max_columns, max(1, (self.width() + 12) // (self._cell_width + 12)))
        if columns == self._columns:
            return
        while self.grid.count():
            self.grid.takeAt(0)
        for column in range(self._max_columns):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)
        for index, widget in enumerate(self._widgets):
            self.grid.addWidget(widget, index // columns, index % columns)
        self._columns = columns

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
    for card in root.findChildren(GlassCard):
        card.set_palette(get_palette(mode))
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
            if action.property("role") == "primary":
                accent = get_palette(mode)["CYAN"]
                ink = max(("#101b2a", "#ffffff"), key=lambda value: contrast_ratio(value, accent))
            action.setIcon(icon(vector, ink))


class PortalDialog(QDialog):
    """A cached workspace backdrop with shared glass form styling."""

    def __init__(self, parent=None, *, preferred=(880, 740)):
        super().__init__(parent)
        self._theme_name = Theme.active_theme
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.backdrop = GlassWorkspace(self)
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
        self.backdrop.invalidate_cache()
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
