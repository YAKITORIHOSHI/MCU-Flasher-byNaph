"""The AI Assistant's list of coding agents, drawn as one chip with a lead per agent.

Deliberately still, like the assistant's loader: only a choice changes what is lit.
Everything is decided by the owner; this widget shows states and reports choices.
"""
from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path
import re

from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
                               QToolTip, QVBoxLayout, QWidget)

from main.core.ai_agents import AGENTS, DEFAULT_AGENT, agent, install_plan, isolation
from main.core.theme import get_palette, glass_material
from main.qt.glass import paint_cover
from main.qt.icons import ActionButton
from main.qt.log_colors import themed_log_colors
from main.qt.theme import glass_tokens, liquid_button_sheet

BUILTIN_TEXT = "(Built-in)"
DOWNLOADED_TEXT = "(Downloaded)"
NOTICE = ("Beta. These tools belong to their makers and use your own account with them. "
          "They run with your normal permissions, never as administrator.")
_GAP = 8            # Between a name and what follows it
_LEAD = 12          # How far a lead stands out from the package
_TRACE = 12         # The routed trace beyond it
_ICONS = Path(__file__).resolve().parents[2] / "src" / "assets" / "icons" / "agents"
_ICON = 20          # Logical size of an agent's own icon inside its tile


@lru_cache(maxsize=64)
def agent_icon(agent_id: str, size: int, ratio: float, ink: str) -> QImage | None:
    """The agent's own mark at device pixels; one-color marks take the theme's ink."""
    try:
        data = (_ICONS / f"{agent_id}.svg").read_bytes()
    except OSError:
        return None
    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        return None
    pixels = max(1, math.ceil(size * ratio))
    image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, pixels, pixels))
    if b"currentColor" in data:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(image.rect(), QColor(ink))
    painter.end()
    image.setDevicePixelRatio(ratio)
    return image


class AgentRow(QAbstractButton):
    """One agent: its socket mark, name and whether it is here."""

    def __init__(self, spec, designator, parent=None):
        super().__init__(parent)
        self.spec = spec
        self._designator = designator
        self._installed = False
        self._inks = {}
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._name_font = QFont(self.font())
        self._name_font.setPixelSize(13)
        self._name_font.setWeight(QFont.Weight.Bold)
        self._suffix_font = QFont(self.font())
        self._suffix_font.setPixelSize(12)
        self._italic_font = QFont(self._suffix_font)
        self._italic_font.setItalic(True)
        self._note_font = QFont(self.font())
        self._note_font.setPixelSize(11)
        self.set_installed(False)

    @property
    def installed(self) -> bool:
        return self._installed

    def set_installed(self, installed: bool) -> None:
        self._installed = bool(installed)
        label = " ".join([self.spec.name, *(text for text, _font, _ink in self.suffixes())])
        self.setAccessibleName(label)
        # Its maker and note are on the row and below the list; no hover card repeats them.
        self.setAccessibleDescription(f"{self.spec.maker}. {self.spec.note}")
        self.update()

    def suffixes(self):
        """(text, font, ink name) after the name."""
        if self.spec.builtin:
            # Setup always provides it, so "(Downloaded)" would say nothing.
            return [(BUILTIN_TEXT, self._suffix_font, "dim")]
        return [(DOWNLOADED_TEXT, self._italic_font, "success")] if self._installed else []

    def set_inks(self, inks: dict) -> None:
        self._inks = inks
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(220, 48)

    def minimumSizeHint(self) -> QSize:
        return QSize(60, 48)

    def lines(self, width: float):
        """What fits: [(text, font, ink)] on the name line, then on the second line."""
        first = [(self.spec.name, self._name_font, "bright")]
        rest = self.suffixes()
        used = QFontMetricsF(self._name_font).horizontalAdvance(self.spec.name)
        second = []
        for text, font, ink in rest:
            # An italic's last letter leans past its advance.
            advance = _GAP + QFontMetricsF(font).horizontalAdvance(text) + (3 if font.italic() else 0)
            if used + advance <= width and not second:
                first.append((text, font, ink))
                used += advance
            else:
                second.append((text, font, ink))
        if not second:
            second = [(self.spec.maker, self._note_font, "normal")]
        return first, second

    def paintEvent(self, event) -> None:
        inks = self._inks
        if not inks:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(self.rect()).adjusted(1, 2, -1, -2)
        path = QPainterPath()
        path.addRoundedRect(body, 13, 13)
        selected, hover = self.isChecked(), self.underMouse() or self.hasFocus()
        sheet = QLinearGradient(body.topLeft(), body.bottomLeft())
        top, bottom = inks["row_selected"] if selected else inks["row_hover"] if hover else inks["row"]
        sheet.setColorAt(0, top)
        sheet.setColorAt(.55, bottom)
        sheet.setColorAt(1, top if selected else bottom)
        painter.fillPath(path, sheet)
        painter.setPen(QPen(inks["accent"] if selected else inks["rim_lit"] if hover else inks["rim"], 1.4 if selected else 1))
        painter.drawPath(path)
        shine = QLinearGradient(body.topLeft(), QPointF(body.left(), body.top() + 12))
        shine.setColorAt(0, inks["spec"])
        shine.setColorAt(1, QColor(0, 0, 0, 0))
        painter.setPen(QPen(shine, 1))
        painter.drawPath(path)
        compact = self.width() < 150
        left = body.left() + 10
        if not compact:
            self._paint_mark(painter, QRectF(left, body.center().y() - 13, 26, 26), selected)
            left += 38
        available = body.right() - 10 - left
        for row, parts in enumerate(self.lines(available)):
            x = left
            baseline = body.top() + (19 if row == 0 else 35)
            for text, font, ink in parts:
                metrics = QFontMetricsF(font)
                text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(0, body.right() - 6 - x))
                painter.setFont(font)
                painter.setPen(inks[ink])
                painter.drawText(QPointF(x, baseline), text)
                x += metrics.horizontalAdvance(text) + _GAP
        painter.end()

    def _paint_mark(self, painter, rect, selected) -> None:
        """The agent's own icon in a small glass tile; a socketed chip if its icon is missing."""
        inks = self._inks
        ink = inks["accent"] if selected else inks["bright"]
        icon = agent_icon(self.spec.id, _ICON, self.devicePixelRatioF(), ink.name(QColor.NameFormat.HexArgb))
        if icon is None:
            self._paint_chip(painter, rect, selected)
            return
        tile = rect.adjusted(-2, -2, 2, 2)
        shape = QPainterPath()
        shape.addRoundedRect(tile, 9, 9)
        fill = QLinearGradient(tile.topLeft(), tile.bottomLeft())
        fill.setColorAt(0, inks["tile_top"])
        fill.setColorAt(1, inks["tile"])
        painter.fillPath(shape, fill)
        painter.setPen(QPen(inks["accent"] if selected else inks["tile_rim"], 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(shape)
        offset = (tile.width() - _ICON) / 2
        painter.drawImage(QPointF(tile.left() + offset, tile.top() + offset), icon)

    def _paint_chip(self, painter, rect, selected) -> None:
        """A small package in its own socket: three pins a side and the board designator."""
        inks = self._inks
        painter.setPen(QPen(inks["accent"] if selected else inks["normal"], 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        body = rect.adjusted(4, 2, -4, -2)
        chip = QPainterPath(QPointF(body.left() + 4, body.top()))
        for point in ((body.right(), body.top()), (body.right(), body.bottom()), (body.left(), body.bottom()),
                      (body.left(), body.top() + 4)):
            chip.lineTo(*point)
        chip.closeSubpath()
        painter.drawPath(chip)
        for index in range(3):
            y = body.top() + body.height() * (index + .5) / 3
            painter.drawLine(QPointF(rect.left(), y), QPointF(body.left(), y))
            painter.drawLine(QPointF(body.right(), y), QPointF(rect.right(), y))
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPixelSize(9)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(body, Qt.AlignmentFlag.AlignCenter, self._designator)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.setChecked(True)
            self.parentWidget().activated.emit(self.spec.id)
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        super().mouseDoubleClickEvent(event)
        self.setChecked(True)
        self.parentWidget().activated.emit(self.spec.id)


def disclaimer_text() -> str:
    """Who owns the assistants on the list: their makers, never MCU Flasher."""
    owners = "".join(
        f"<br>&#8226; <b>{spec.name}</b> &#8212; "
        f"{'its open-source authors' if spec.maker.lower() == 'open source' else spec.maker}"
        for spec in AGENTS)
    return ("<b>Disclaimer</b><br>"
            "The AI command-line tools on this list were not created by, and do not belong to, "
            "the developer of MCU Flasher. Each one, with its name, logo and software, is owned "
            "solely by its rightful owner:" + owners + "<br><br>"
            "MCU Flasher only downloads them from their makers when you ask and starts them for "
            "you. You use each one under its owner's own terms, with your own account.")


class _InfoMark(QAbstractButton):
    """The standard circled "i": hover or click it for the disclaimer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._inks = {}
        self.setFixedSize(20, 20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("About these assistants")
        self.setAccessibleDescription("Disclaimer: these AI tools belong to their makers, not to MCU Flasher.")
        self.setToolTip(disclaimer_text())
        self.clicked.connect(self.show_disclaimer)

    def set_inks(self, inks) -> None:
        self._inks = inks
        self.update()

    def show_disclaimer(self) -> None:
        QToolTip.showText(self.mapToGlobal(self.rect().bottomLeft()), self.toolTip(), self, self.rect(), 15000)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        if not self._inks:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        lit = self.underMouse() or self.isDown()
        ink = self._inks["accent"] if lit else self._inks["dim"]
        center = QPointF(self.width() / 2, self.height() / 2)
        if lit:
            painter.setPen(Qt.PenStyle.NoPen)
            glow = QColor(ink)
            glow.setAlpha(46)
            painter.setBrush(glow)
            painter.drawEllipse(center, 9.5, 9.5)
        painter.setPen(QPen(ink, 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, 7.5, 7.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        painter.drawEllipse(QPointF(center.x(), center.y() - 3.6), 1.15, 1.15)
        painter.drawRoundedRect(QRectF(center.x() - .8, center.y() - 1.4, 1.6, 5.6), .8, .8)
        painter.end()


class _Package(QWidget):
    """The chip the rows sit in, with a lit lead for the chosen one."""

    activated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._inks = {}
        self.rows: list[AgentRow] = []
        self.info = _InfoMark(self)

    def set_inks(self, inks) -> None:
        self._inks = inks
        for row in self.rows:
            row.set_inks(inks)
        self.info.set_inks(inks)
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        body = self.body_rect()
        self.info.move(int(body.right() - 12 - self.info.width()), int(body.top() + 6))
        self.info.raise_()

    def leads(self) -> bool:
        return self.width() >= 230

    def body_rect(self) -> QRectF:
        inset = _LEAD + _TRACE if self.leads() else 2
        return QRectF(self.rect()).adjusted(inset, 1, -inset, -1)

    def paintEvent(self, event) -> None:
        inks = self._inks
        if not inks:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = self.body_rect()
        if self.leads():
            for row in self.rows:
                self._paint_leads(painter, body, row)
        path = QPainterPath()
        path.addRoundedRect(body, 18, 18)
        notch = QPainterPath()
        notch.addEllipse(QPointF(body.center().x(), body.top()), 9, 9)
        path = path.subtracted(notch)
        sheet = QLinearGradient(body.topLeft(), body.bottomLeft())
        sheet.setColorAt(0, inks["package_top"])
        sheet.setColorAt(1, inks["package"])
        painter.fillPath(path, sheet)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(inks["rim_lit"], 1))
        painter.drawPath(path)
        # Pin 1, and the board's own silkscreen.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(inks["accent"])
        painter.drawEllipse(QPointF(body.left() + 16, body.top() + 16), 3, 3)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPixelSize(10)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        painter.setFont(font)
        painter.setPen(inks["dim"])
        if body.width() >= 150:
            painter.drawText(QRectF(body.left() + 26, body.top() + 8, body.width() - 70, 16),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "AI CLI")
        painter.end()

    def _paint_leads(self, painter, body, row) -> None:
        lit = row.isChecked()
        ink = self._inks["accent"] if lit else self._inks["lead"]
        y = row.geometry().center().y() + .5
        for side in (-1, 1):
            edge = body.left() if side < 0 else body.right()
            lead = QRectF(edge - _LEAD if side < 0 else edge, y - 4, _LEAD, 8)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ink)
            painter.drawRoundedRect(lead, 2, 2)
            start = edge + side * _LEAD
            trace = QPainterPath(QPointF(start, y))
            trace.lineTo(start + side * 3, y)
            trace.lineTo(start + side * 6, y - 3)
            trace.lineTo(start + side * (_TRACE - 2.5), y - 3)
            painter.setPen(QPen(ink, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(trace)
            painter.setBrush(ink)
            painter.drawEllipse(QPointF(start + side * (_TRACE - 2.5), y - 3), 1.8, 1.8)


class AgentChooser(QFrame):
    """Choose the assistant to start; download one only when asked to."""

    start_requested = Signal(str)
    download_requested = Signal(str)
    cancel_requested = Signal()
    retry_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("agent-chooser")
        self._busy = ""
        self._checked_files = False
        self._online = True
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setObjectName("agent-chooser-scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumSize(1, 1)
        self._scroll = scroll
        outer.addWidget(scroll)
        page = QWidget(scroll)
        page.setObjectName("agent-chooser-page")
        self._page_layout = QVBoxLayout(page)
        self._page_layout.setSpacing(10)
        self._page_layout.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        host = QWidget(page)
        host.setObjectName("agent-chooser-column")
        host.setMaximumWidth(460)
        row.addWidget(host, 100)
        row.addStretch(1)
        self._page_layout.addLayout(row)
        self._page_layout.addStretch(1)
        scroll.setWidget(page)

        views = QVBoxLayout(host)
        views.setContentsMargins(0, 0, 0, 0)
        views.setSpacing(0)
        # Every assistant works through its maker's servers: offline, the list is replaced by
        # a notice that keeps checking (AgentPicker) and brings the list back by itself.
        self._list_view = QWidget(host)
        self._list_view.setObjectName("agent-chooser-list")
        views.addWidget(self._list_view)
        self._offline_view = QWidget(host)
        self._offline_view.setObjectName("agent-chooser-offline")
        views.addWidget(self._offline_view)
        offline = QVBoxLayout(self._offline_view)
        offline.setContentsMargins(0, 0, 0, 0)
        offline.setSpacing(8)
        self.offline_title = QLabel("No internet connection", self._offline_view)
        self.offline_text = QLabel("The assistants work through their makers' servers, so the list appears "
                                   "once this computer is online. It checks again by itself.", self._offline_view)
        for label in (self.offline_title, self.offline_text):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            label.setMinimumWidth(1)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            offline.addWidget(label)
        retry_row = QHBoxLayout()
        retry_row.addStretch(1)
        self.retry_button = ActionButton("Try again", self._offline_view, icon_name="reload")
        self.retry_button.setFixedHeight(30)
        self.retry_button.setAutoDefault(False)
        self.retry_button.clicked.connect(self._retry)
        retry_row.addWidget(self.retry_button)
        retry_row.addStretch(1)
        offline.addLayout(retry_row)
        self._offline_view.hide()
        column = QVBoxLayout(self._list_view)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self.title = QLabel("Choose an assistant", host)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)
        column.addWidget(self.title)
        self.package = _Package(host)
        rows = QVBoxLayout(self.package)
        rows.setSpacing(4)
        self._rows_layout = rows
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for index, spec in enumerate(AGENTS, 1):
            button = AgentRow(spec, f"U{index}", self.package)
            self._group.addButton(button)
            self.package.rows.append(button)
            rows.addWidget(button)
            button.toggled.connect(lambda *_: self._selection_changed())
        self.package.activated.connect(lambda *_: self._act())
        column.addWidget(self.package)
        self.detail = QLabel("", host)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail.setWordWrap(True)
        self.detail.setMinimumWidth(1)
        self.detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.detail)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch(1)
        self.cancel_button = ActionButton("Cancel", host)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.cancel_button.hide()
        actions.addWidget(self.cancel_button)
        self.primary = ActionButton("Start", host, icon_name="resume")
        self.primary.setAutoDefault(False)
        for button in (self.primary, self.cancel_button):
            button.setFixedHeight(30)
        self.primary.clicked.connect(self._act)
        actions.addWidget(self.primary)
        actions.addStretch(1)
        column.addLayout(actions)
        self.notice = QLabel(NOTICE, host)
        self.notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.notice.setWordWrap(True)
        column.addWidget(self.notice)
        self.select(DEFAULT_AGENT)
        self.apply_theme("default")

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)      # Like the loader, this must never hold a pane open.

    def sizeHint(self) -> QSize:
        return QSize(260, 380)

    # ── State ────────────────────────────────────────────────────────────────

    def row(self, agent_id) -> AgentRow:
        return next(row for row in self.package.rows if row.spec.id == agent(agent_id).id)

    @property
    def selected(self) -> str:
        button = self._group.checkedButton()
        return button.spec.id if button is not None else DEFAULT_AGENT

    def select(self, agent_id) -> None:
        self.row(agent_id).setChecked(True)

    def set_installed(self, found: dict) -> None:
        self._checked_files = True
        for row in self.package.rows:
            row.set_installed(bool(found.get(row.spec.id)))
        self._selection_changed()

    @property
    def busy(self) -> str:
        return self._busy

    @property
    def online(self) -> bool:
        return self._online

    def set_online(self, online: bool) -> None:
        """Show the list only while this computer reaches the internet."""
        online = bool(online) or bool(self._busy)
        self._online = online
        self._list_view.setVisible(online)
        self._offline_view.setVisible(not online)
        if not online:
            self.retry_button.setEnabled(True)
            self.retry_button.setText("Try again")

    def _retry(self) -> None:
        self.retry_button.setEnabled(False)
        self.retry_button.setText("Checking…")
        self.retry_requested.emit()

    def show_progress(self, agent_id, text) -> None:
        self._busy = agent_id
        self._show_detail(text)
        self._refresh_actions()

    def show_message(self, text, *, error=False) -> None:
        self._busy = ""
        self._selection_changed()
        if text:
            self._show_detail(text, error=error)

    def _show_detail(self, text, error=False) -> None:
        # A long address may wrap at its dots and dashes instead of widening the pane.
        self.detail.setText(re.sub(r"(?<=\w)([.\-/@])(?=\w)", "\\1\u200b", text))
        self.detail.setProperty("failed", error)
        self._style_detail()

    def _selection_changed(self) -> None:
        self.package.update()
        if self._busy:
            return
        row = self.row(self.selected)
        plan = install_plan(row.spec.id)
        if row.installed or not self._checked_files:
            self._show_detail(row.spec.note + ".")
        elif plan is not None:
            where = ("into MCU Flasher's own folder, about 3 GB with its own tools" if isolation(row.spec.id)
                     else "for this account only")
            self._show_detail(f"{row.spec.note}. Downloads from {plan.source}, {where}.")
        else:
            self._show_detail("Setup provides OpenCode. Run it with --repair if it is missing.")
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        row = self.row(self.selected)
        downloadable = not row.installed and self._checked_files and install_plan(row.spec.id) is not None
        self.cancel_button.setVisible(bool(self._busy))
        self.primary.setVisible(not self._busy)
        narrow = self.width() < 250
        self.primary.setText(("Download" if narrow else f"Download {row.spec.name}") if downloadable else
                             "Start" if narrow else f"Start {row.spec.name}")
        self.primary.set_icon("download" if downloadable else "resume")
        for button in self.package.rows:
            button.setEnabled(not self._busy)

    def _act(self) -> None:
        if self._busy:
            return
        row = self.row(self.selected)
        if not row.installed and self._checked_files and install_plan(row.spec.id) is not None:
            self.download_requested.emit(row.spec.id)
        else:
            self.start_requested.emit(row.spec.id)

    # ── Native events and theme ──────────────────────────────────────────────

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        wide, tall = event.size().width() >= 300, event.size().height() >= 300
        margin = 16 if wide else 8
        self._page_layout.setContentsMargins(margin, 12 if tall else 6, margin, 12 if tall else 6)
        inset = int(_LEAD + _TRACE + 10) if event.size().width() - 2 * margin >= 230 else 10
        self._rows_layout.setContentsMargins(inset, 30, inset, 12)
        self.notice.setVisible(event.size().height() >= 260)
        self._refresh_actions()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        if not paint_cover(self, painter):
            painter.fillRect(self.rect(), QColor(self._cover_surface))
        painter.end()
        super().paintEvent(event)

    def apply_theme(self, mode: str) -> None:
        self._mode = mode
        palette, glass = get_palette(mode), glass_tokens(mode)
        colors = themed_log_colors(mode)
        light = mode == "light"
        self._cover_surface = palette["BG_DARKEST"]

        def glassy(value, alpha):
            color = QColor(value)
            color.setAlpha(alpha)
            return color
        # The theme's own glass: its tint for the body, its caught light along the top, and on
        # frost a steel edge, since a white edge on white would vanish.
        material = glass_material(mode)
        sheen, edge = material["tint"], material["edge"]
        self._inks = {
            "bright": QColor(palette["TEXT_BRIGHT"]), "normal": QColor(colors["normal"]), "dim": QColor(colors["dim"]),
            "success": QColor(colors["success"]), "accent": QColor(palette["CYAN"]),
            "lead": glassy(palette["TEXT_DIM"], 130), "rim": glassy(edge, 96 if light else 60),
            "rim_lit": glassy(edge, 160 if light else 120), "spec": glassy(material["spec"], 220 if light else 96),
            "tile_top": glassy(sheen, 235) if light else glassy(sheen, 34),
            "tile": glassy(sheen, 150) if light else glassy(sheen, 10),
            "tile_rim": glassy(edge, 110 if light else 70),
            "package_top": glassy(sheen, 120) if light else glassy(sheen, 20),
            "package": glassy(sheen, 60) if light else glassy(palette["BG_DARKEST"], 90),
            "row": (glassy(sheen, 120), glassy(sheen, 64)) if light else (glassy(sheen, 20), glassy(sheen, 6)),
            "row_hover": (glassy(sheen, 190), glassy(sheen, 110)) if light else (glassy(sheen, 34), glassy(sheen, 14)),
            "row_selected": (glassy(palette["CYAN"], 40), glassy(palette["CYAN"], 16)) if light else
                            (glassy(palette["CYAN"], 58), glassy(palette["CYAN"], 22)),
        }
        self.setStyleSheet(
            f"QFrame#agent-chooser {{ background: {glass['reading']}; border: 1px solid {glass['reading_rim']}; border-radius: 10px; }}"
            "QScrollArea#agent-chooser-scroll, QWidget#agent-chooser-page, QWidget#agent-chooser-column, "
            "QWidget#agent-chooser-list, QWidget#agent-chooser-offline { background: transparent; }")
        self._scroll.viewport().setStyleSheet("background: transparent;")
        self.title.setStyleSheet(f"color: {palette['TEXT_BRIGHT']}; font-size: 15px; font-weight: 700; background: transparent;")
        self.notice.setStyleSheet(f"color: {colors['dim']}; font-size: 10px; background: transparent;")
        self.offline_title.setStyleSheet(
            f"color: {palette['TEXT_BRIGHT']}; font-size: 15px; font-weight: 700; background: transparent;")
        self.offline_text.setStyleSheet(f"color: {colors['normal']}; font-size: 11px; background: transparent;")
        self.retry_button.setStyleSheet(liquid_button_sheet(mode, "glass", radius=15))
        self.primary.setStyleSheet(liquid_button_sheet(mode, "accent", radius=15))
        self.cancel_button.setStyleSheet(liquid_button_sheet(mode, "glass", radius=15))
        self.package.set_inks(self._inks)
        self._style_detail()

    def _style_detail(self) -> None:
        colors = themed_log_colors(getattr(self, "_mode", "default"))
        ink = colors["error"] if self.detail.property("failed") else colors["normal"]
        self.detail.setStyleSheet(f"color: {ink}; font-size: 11px; background: transparent;")
