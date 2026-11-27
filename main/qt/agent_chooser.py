"""The AI Assistant's list of coding agents, drawn as one chip with a lead per agent.

Deliberately still, like the assistant's loader: only a choice changes what is lit.
One click on an agent that is here starts it; there is no separate Start button.
Everything is decided by the owner; this widget shows states and reports choices.
"""
from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path
import re
import time

from PySide6.QtCore import QByteArray, QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QAbstractButton, QAbstractSlider, QButtonGroup, QFrame, QHBoxLayout, QLabel,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

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
_HEADER = 44        # The package's top strip: pin 1, its silkscreen and the circled i
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
    """One agent: its socket mark, name and whether it is here. Clicking it is the choice."""

    def __init__(self, spec, designator, parent=None):
        super().__init__(parent)
        self.spec = spec
        self._designator = designator
        self._installed = False
        self._offer = False
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
        self._describe()
        self.update()

    def set_offer(self, offer: bool) -> None:
        """Whether choosing it shows its download, because it is not here yet, instead of starting it."""
        if bool(offer) != self._offer:
            self._offer = bool(offer)
            self._describe()

    def _describe(self) -> None:
        label = " ".join([self.spec.name, *(text for text, _font, _ink in self.suffixes())])
        # The row is the action now that no Start button follows the list.
        self.setAccessibleName(f"{self.spec.name}, not downloaded" if self._offer else f"Start {label}")
        # Its maker and note are on the row and below the list; no hover card repeats them.
        self.setAccessibleDescription(f"{self.spec.maker}. {self.spec.note}")

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
        # A held press already shows as chosen: releasing on the row is what starts it.
        selected, hover = self.isChecked() or self.isDown(), self.underMouse() or self.hasFocus()
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

    # A click is a left press and release on this row (QAbstractButton: dragging off before the
    # release, or the right and middle buttons, click nothing). Space clicks too; so does Enter.

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.isAutoRepeat():
                self.click()
            return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Left, Qt.Key.Key_Down, Qt.Key.Key_Right):
            # Arrows move the choice without making it. (QAbstractButton would click the next row.)
            if not self.parentWidget().step(self, -1 if key in (Qt.Key.Key_Up, Qt.Key.Key_Left) else 1):
                event.ignore()          # At either end the page may scroll instead.
            return
        super().keyPressEvent(event)

    def hitButton(self, pos) -> bool:
        # Just after a choice the pointer may be finishing a double click that moved too far to
        # count as one; that second click presses no row at all.
        return super().hitButton(pos) and time.monotonic() >= self.parentWidget().quiet_until

    def mouseDoubleClickEvent(self, event) -> None:
        # The first click already chose. The second press starts nothing more, not even on a row
        # that the first click's change moved under the pointer.
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
        else:
            event.ignore()

    def focusInEvent(self, event) -> None:
        # Each row is its own action, so each keeps its Tab stop; QAbstractButton's handler would
        # leave a single one for the whole group.
        QWidget.focusInEvent(self, event)

    def contextMenuEvent(self, event) -> None:
        # A right-click (or the Menu key) offers this agent's own actions; it chooses nothing.
        keyboard = event.reason() == event.Reason.Keyboard
        position = self.mapToGlobal(self.rect().center()) if keyboard else event.globalPos()
        self.parentWidget().menu_requested.emit(self, position)
        event.accept()


def disclaimer_text() -> str:
    """Who owns the assistants on the list: their makers, never MCU Flasher. Its purpose in bold."""
    owners = "".join(
        f"<br>&#8226; <b>{spec.name}</b> &#8212; "
        f"{'its open-source authors' if spec.maker.lower() == 'open source' else spec.maker}"
        for spec in AGENTS)
    return ("<p><b>The AI command-line tools on this list were not created by, and do not belong to, "
            "the developer of MCU Flasher.</b></p>"
            "<p>Each one, with its name, logo and software, is owned solely by its rightful owner:"
            + owners + "</p>"
            "<p><b>MCU Flasher only downloads them from their makers when you ask, and starts them for you.</b> "
            "<b>You use each one under its owner's own terms, with your own account.</b></p>")


class _InfoMark(QAbstractButton):
    """The standard circled "i": a click opens the disclaimer over the list."""

    SIZE = 24

    def __init__(self, parent=None):
        super().__init__(parent)
        self._inks = {}
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("About these assistants")
        self.setAccessibleDescription("Disclaimer: these AI tools belong to their makers, not to MCU Flasher.")

    def set_inks(self, inks) -> None:
        self._inks = inks
        self.update()

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
            painter.drawEllipse(center, 11.5, 11.5)
        painter.setPen(QPen(ink, 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, 8.5, 8.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        painter.drawEllipse(QPointF(center.x(), center.y() - 4.1), 1.3, 1.3)
        painter.drawRoundedRect(QRectF(center.x() - .9, center.y() - 1.6, 1.8, 6.4), .9, .9)
        painter.end()


class _CloseMark(QAbstractButton):
    """A round close mark that turns into a red disc under the pointer, like the notices' own."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ink = QColor("#808080")
        self.setFixedSize(26, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setAccessibleName("Close the disclaimer")

    def set_ink(self, ink) -> None:
        self._ink = QColor(ink)
        self.update()

    def keyPressEvent(self, event) -> None:
        # QAbstractButton would move focus with the arrows; the disclaimer scrolls with them instead.
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right):
            event.ignore()
            return
        super().keyPressEvent(event)

    def event(self, event):
        if event.type() in (QEvent.Type.HoverEnter, QEvent.Type.HoverLeave):
            self.update()
        return super().event(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        disc = QRectF(3, 3, 20, 20)
        if self.underMouse() or self.isDown():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(196, 43, 50, 240) if self.isDown() else QColor(229, 72, 77, 232))
            painter.drawEllipse(disc)
            ink = QColor("#ffffff")
        else:
            ink = self._ink
            if self.hasFocus():
                # Keyboard focus: a ring, so the mark is not mistaken for an alarm.
                painter.setPen(QPen(ink, 1.2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(disc.adjusted(.5, .5, -.5, -.5))
        pen = QPen(ink, 1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        center, arm = disc.center(), 4.0
        painter.drawLine(QPointF(center.x() - arm, center.y() - arm), QPointF(center.x() + arm, center.y() + arm))
        painter.drawLine(QPointF(center.x() + arm, center.y() - arm), QPointF(center.x() - arm, center.y() + arm))
        painter.end()


class _Disclaimer(QWidget):
    """The disclaimer, over the whole list: who owns each assistant, under a red heading.

    A veil covers the list while it is open, so nothing under it is chosen by mistake. Its close
    mark, Escape or a click on the veil puts it away; focus then returns where it was.
    """

    closed = Signal()
    MARGIN, WIDEST = 12, 420

    def __init__(self, parent):
        super().__init__(parent)
        self._veil = QColor(0, 0, 0, 90)
        self._surface = QColor("#ffffff")
        self._rim = QColor("#c0c8d0")
        self._shadow = QColor(0, 0, 0, 60)
        self._return_focus = None
        self.sheet = QWidget(self)
        self.sheet.setObjectName("agent-disclaimer")
        self.sheet.setAccessibleName("Disclaimer")
        layout = QVBoxLayout(self.sheet)
        layout.setContentsMargins(16, 10, 10, 14)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.setSpacing(6)
        self.heading = QLabel("Disclaimer", self.sheet)
        self.heading.setObjectName("agent-disclaimer-heading")
        header.addWidget(self.heading, 1, Qt.AlignmentFlag.AlignVCenter)
        self.close_mark = _CloseMark(self.sheet)
        self.close_mark.clicked.connect(self.dismiss)
        header.addWidget(self.close_mark, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        self.scroll = QScrollArea(self.sheet)
        self.scroll.setObjectName("agent-disclaimer-scroll")
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text = QLabel(disclaimer_text())
        self.text.setObjectName("agent-disclaimer-text")
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text.setContentsMargins(0, 0, 6, 0)
        self.scroll.setWidget(self.text)
        self.scroll.viewport().setAutoFillBackground(False)
        layout.addWidget(self.scroll, 1)
        self.hide()

    def set_inks(self, *, heading, text, bright, surface, rim, veil, shadow) -> None:
        self._surface, self._rim, self._veil, self._shadow = QColor(surface), QColor(rim), QColor(veil), QColor(shadow)
        self.heading.setStyleSheet(f"color: {heading}; font-size: 15px; font-weight: 800; background: transparent;")
        self.text.setStyleSheet(f"color: {text}; font-size: 12px; background: transparent;")
        # Rich text takes its bold lines' ink from the palette, not from the style sheet.
        self.text.setText(disclaimer_text().replace("<b>", f'<b style="color: {bright};">'))
        self.scroll.setStyleSheet("QScrollArea#agent-disclaimer-scroll, QScrollArea#agent-disclaimer-scroll > QWidget "
                                  "{ background: transparent; }")
        self.close_mark.set_ink(text)
        self.update()

    def open(self) -> None:
        from PySide6.QtWidgets import QApplication
        self._return_focus = QApplication.focusWidget()
        self.fit()
        self.show()
        self.raise_()
        self.close_mark.setFocus(Qt.FocusReason.OtherFocusReason)

    def dismiss(self) -> None:
        if not self.isVisible():
            return
        self.hide()
        widget, self._return_focus = self._return_focus, None
        try:
            if widget is not None and widget.isVisible():
                widget.setFocus(Qt.FocusReason.OtherFocusReason)
        except RuntimeError:
            pass            # It was deleted meanwhile.
        self.closed.emit()

    def fit(self) -> None:
        """Cover the list; the sheet as wide as fits (up to WIDEST) and as tall as its text."""
        self.setGeometry(self.parentWidget().rect())
        area = self.rect().adjusted(self.MARGIN, self.MARGIN, -self.MARGIN, -self.MARGIN)
        width = max(120, min(area.width(), self.WIDEST))
        margins = self.sheet.layout().contentsMargins()
        inner = width - margins.left() - margins.right()
        for label in (self.heading, self.text):
            label.ensurePolished()      # Its style sheet's font size, before it is measured
        wanted = (margins.top() + max(self.heading.sizeHint().height(), self.close_mark.height())
                  + self.sheet.layout().spacing()
                  # Measured as if its scroll bar were showing, so the bar is not needed after all.
                  + self.text.heightForWidth(inner - self.scroll.verticalScrollBar().sizeHint().width())
                  + margins.bottom() + 6)
        height = max(90, min(area.height(), wanted))
        self.sheet.setGeometry(area.left() + (area.width() - width) // 2, area.top() + (area.height() - height) // 2,
                               width, height)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.dismiss()
            event.accept()
            return
        # The arrows and page keys scroll the text, wherever focus is on the sheet.
        steps = {Qt.Key.Key_Up: QAbstractSlider.SliderAction.SliderSingleStepSub,
                 Qt.Key.Key_Down: QAbstractSlider.SliderAction.SliderSingleStepAdd,
                 Qt.Key.Key_PageUp: QAbstractSlider.SliderAction.SliderPageStepSub,
                 Qt.Key.Key_PageDown: QAbstractSlider.SliderAction.SliderPageStepAdd,
                 Qt.Key.Key_Home: QAbstractSlider.SliderAction.SliderToMinimum,
                 Qt.Key.Key_End: QAbstractSlider.SliderAction.SliderToMaximum}
        if key in steps:
            self.scroll.verticalScrollBar().triggerAction(steps[key])
            event.accept()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            event.accept()          # Nothing to move sideways, and nothing under the veil to reach
            return
        super().keyPressEvent(event)

    def focusNextPrevChild(self, next) -> bool:
        # Tab stays on the open sheet, so nothing under the veil can be reached and chosen.
        if self.isVisible():
            self.close_mark.setFocus(Qt.FocusReason.TabFocusReason)
            return True
        return super().focusNextPrevChild(next)

    def mousePressEvent(self, event) -> None:
        # The veil takes every click; one outside the sheet puts the disclaimer away.
        if not self.sheet.geometry().contains(event.position().toPoint()):
            self.dismiss()
        event.accept()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), self._veil)
        sheet = QRectF(self.sheet.geometry())
        painter.setPen(Qt.PenStyle.NoPen)
        for spread, alpha in ((10, .18), (6, .3), (3, .5)):
            shadow = QColor(self._shadow)
            shadow.setAlphaF(shadow.alphaF() * alpha)
            painter.setBrush(shadow)
            painter.drawRoundedRect(sheet.adjusted(-spread, -spread + 3, spread, spread + 3), 16 + spread, 16 + spread)
        painter.setBrush(self._surface)
        painter.setPen(QPen(self._rim, 1))
        painter.drawRoundedRect(sheet.adjusted(.5, .5, -.5, -.5), 16, 16)
        painter.end()


class _Package(QWidget):
    """The chip the rows sit in, with a lit lead for the chosen one."""

    stepped = Signal(object)
    menu_requested = Signal(object, object)     # (row, global position)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._inks = {}
        self.rows: list[AgentRow] = []
        self.quiet_until = 0.0      # Until then a click on a row is the rest of the last one
        self.info = _InfoMark(self)

    def step(self, row, step) -> bool:
        """Light and focus the next row up or down; nothing starts. False past either end."""
        index = self.rows.index(row) + step
        if not 0 <= index < len(self.rows):
            return False
        target = self.rows[index]
        target.setChecked(True)
        target.setFocus(Qt.FocusReason.TabFocusReason if step > 0 else Qt.FocusReason.BacktabFocusReason)
        self.stepped.emit(target)
        return True

    def set_inks(self, inks) -> None:
        self._inks = inks
        for row in self.rows:
            row.set_inks(inks)
        self.info.set_inks(inks)
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        body = self.body_rect()
        # Centred in the top strip, clear of the rim and of the first row on every side.
        self.info.move(round(body.right() - 14 - self.info.width()),
                       round(body.top() + (_HEADER - body.top() - self.info.height()) / 2))
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
        middle = (body.top() + _HEADER) / 2       # The top strip's centre line
        painter.drawEllipse(QPointF(body.left() + 16, middle), 3, 3)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPixelSize(10)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        painter.setFont(font)
        painter.setPen(inks["dim"])
        if body.width() >= 150:
            painter.drawText(QRectF(body.left() + 26, middle - 8, body.width() - 78, 16),
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
    """One click starts an assistant that is here. One that is not here yet first shows where it
    comes from and how much it brings, and downloads only from its own Download button."""

    start_requested = Signal(str)
    download_requested = Signal(str)
    uninstall_requested = Signal(str)
    cancel_requested = Signal()
    retry_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("agent-chooser")
        self._busy = ""
        self._checked_files = False
        self._online = True
        self._pending = ""          # Chosen before the files were checked; acted on once they are
        self._cancellable = True    # Whether the work under way offers Cancel (an uninstall does not)
        self._asking = False        # The owner is still answering a request
        self._message = None        # (text, error) from the owner, shown instead of the description
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
        self._group.buttonClicked.connect(self._choose)
        self.package.stepped.connect(lambda row: self._scroll.ensureWidgetVisible(row, 0, 8))
        self.package.menu_requested.connect(self._show_row_menu)
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
        # Only an agent that is not here yet has a button: its download, after its disclosure.
        self.download_button = ActionButton("Download", host, icon_name="download")
        self.download_button.setAutoDefault(False)
        self.download_button.hide()
        for button in (self.download_button, self.cancel_button):
            button.setFixedHeight(30)
            button.installEventFilter(self)
        self.download_button.clicked.connect(self._download)
        actions.addWidget(self.download_button)
        actions.addStretch(1)
        column.addLayout(actions)
        self.notice = QLabel(NOTICE, host)
        self.notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.notice.setWordWrap(True)
        column.addWidget(self.notice)
        # Over everything else: the disclaimer the circled i opens.
        self.disclaimer = _Disclaimer(self)
        self.package.info.clicked.connect(self.show_disclaimer)
        self.select(DEFAULT_AGENT)
        self.apply_theme("default")

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)      # Like the loader, this must never hold a pane open.

    def show_disclaimer(self) -> None:
        self.disclaimer.open()

    def hideEvent(self, event) -> None:
        # Leaving the list puts the disclaimer away; the circled i opens it again.
        self.disclaimer.dismiss()
        super().hideEvent(event)

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
        """Light this row; it starts nothing. Owners call it whenever they offer the list again,
        so what the list said last gives way to this row's own description."""
        self._pending, self._message, self.package.quiet_until = "", None, 0.0
        row = self.row(agent_id)
        if row.isChecked():
            self._refresh()
        else:
            row.setChecked(True)

    def set_installed(self, found: dict) -> None:
        self._checked_files = True
        for row in self.package.rows:
            row.set_installed(bool(found.get(row.spec.id)))
        self._refresh()
        # A choice made before the files were checked waited for them.
        pending, self._pending = self._pending, ""
        if pending and not self._busy and self.isEnabled() and self.isVisible():
            self._proceed(self.row(pending))

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

    def show_progress(self, agent_id, text, *, cancellable=True) -> None:
        self._busy = agent_id
        self._cancellable = bool(cancellable)
        self._pending, self._message = "", None
        self._show_detail(text)
        self._refresh_actions()

    def show_message(self, text, *, error=False) -> None:
        """It stays until another row is chosen or the list is offered again; the owner's next
        look at the files keeps it."""
        self._busy = ""
        self._cancellable = True
        self._message = (text, error) if text else None
        self._refresh()

    def _show_detail(self, text, error=False) -> None:
        # A long address may wrap at its dots and dashes instead of widening the pane.
        self.detail.setText(re.sub(r"(?<=\w)([.\-/@])(?=\w)", "\\1\u200b", text))
        self.detail.setProperty("failed", error)
        self._style_detail()

    def _selection_changed(self) -> None:
        self._message = None            # What the list said was about the last choice.
        self._refresh()

    def _refresh(self) -> None:
        self.package.update()
        if self._pending and self._pending != self.selected:
            self._pending = ""          # Moved on before the files were checked.
        if self._busy:
            return
        text, error = self._message or (self._description(self.row(self.selected)), False)
        self._show_detail(text, error=error)
        self._refresh_actions()

    def _description(self, row) -> str:
        """Its note; for one not here yet, also where it would come from and where it would go."""
        if row.installed or not self._checked_files:
            return row.spec.note + "."
        plan = install_plan(row.spec.id)
        if plan is not None:
            where = ("into MCU Flasher's own folder, about 3 GB with its own tools" if isolation(row.spec.id)
                     else "for this account only")
            return f"{row.spec.note}. Downloads from {plan.source}, {where}."
        return "Setup provides OpenCode. Run it with --repair if it is missing."

    def _downloadable(self, row) -> bool:
        """Known not to be here and its maker distributes it: choosing it offers the download."""
        return self._checked_files and not row.installed and install_plan(row.spec.id) is not None

    def _refresh_actions(self) -> None:
        row = self.row(self.selected)
        offer = not self._busy and self._downloadable(row)
        self.cancel_button.setVisible(bool(self._busy) and self._cancellable)
        self.download_button.setVisible(offer)
        if offer:
            name = f"Download {row.spec.name}"
            self.download_button.setText("Download" if self.width() < 250 else name)
            self.download_button.setAccessibleName(name)
        for button in self.package.rows:
            button.setEnabled(not self._busy)
            button.set_offer(self._downloadable(button))

    def _choose(self, row) -> None:
        """A click on the row, or Enter or Space on it: start that agent at once. One not here yet
        only shows its disclosure and Download button; nothing is ever downloaded from a row."""
        if self._busy:
            return
        # For a double click's length, a further click on any row is the rest of this one.
        self.package.quiet_until = time.monotonic() + QGuiApplication.styleHints().mouseDoubleClickInterval() / 1000
        if not self._checked_files and not row.spec.builtin:
            self._pending = row.spec.id     # Whether it is here is not known yet.
            return
        self._proceed(row)

    def _proceed(self, row) -> None:
        self._pending = ""
        if not self._downloadable(row):
            self._request(self.start_requested, row.spec.id)

    def row_menu(self, row):
        """A row's right-click menu, or None; separate so checks can choose from it."""
        if not self._checked_files or not (row.installed or row.spec.builtin):
            return None
        from main.qt.combo_popup import GlassMenu
        menu = GlassMenu(self)
        if row.spec.builtin:
            # Setup provides it and the app needs it: say so instead of offering nothing.
            menu.addAction(f"{row.spec.name} comes with MCU Flasher").setEnabled(False)
            return menu
        action = menu.addAction(f"Uninstall {row.spec.name}…")
        action.setEnabled(not self._busy)
        agent_id = row.spec.id
        action.triggered.connect(lambda _checked=False: self._request(self.uninstall_requested, agent_id))
        return menu

    def _show_row_menu(self, row, position) -> None:
        menu = self.row_menu(row)
        if menu is not None:
            menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            menu.popup(position)

    def _download(self) -> None:
        row = self.row(self.selected)
        if not self._busy and self._downloadable(row):
            self._request(self.download_requested, row.spec.id)

    def _request(self, signal, agent_id) -> None:
        if self._asking:
            return          # The owner is still answering the last request.
        self._asking = True
        try:
            signal.emit(agent_id)
        finally:
            self._asking = False

    def eventFilter(self, watched, event) -> bool:
        # Download gives way to Cancel in the same place: the second press of a double click on
        # one must never press the other.
        if event.type() == QEvent.Type.MouseButtonDblClick and (
                watched is self.download_button or watched is self.cancel_button):
            return True
        return super().eventFilter(watched, event)

    # ── Native events and theme ──────────────────────────────────────────────

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        wide, tall = event.size().width() >= 300, event.size().height() >= 300
        margin = 16 if wide else 8
        self._page_layout.setContentsMargins(margin, 12 if tall else 6, margin, 12 if tall else 6)
        inset = int(_LEAD + _TRACE + 10) if event.size().width() - 2 * margin >= 230 else 10
        self._rows_layout.setContentsMargins(inset, _HEADER, inset, 12)
        self.disclaimer.fit()
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
        self.download_button.setStyleSheet(liquid_button_sheet(mode, "accent", radius=15))
        self.cancel_button.setStyleSheet(liquid_button_sheet(mode, "glass", radius=15))
        self.package.set_inks(self._inks)
        # The disclaimer: a near-solid sheet of the theme's surface over a veil, so the list
        # under it never shows through its text; its heading in the theme's readable red.
        surface = QColor(palette["BG_MID"])
        surface.setAlpha(250)
        self.disclaimer.set_inks(heading=colors["error"], text=colors["normal"], bright=palette["TEXT_BRIGHT"],
                                 surface=surface, rim=glassy(edge, 170 if light else 120),
                                 veil=glassy(palette["BG_DARKEST"], 70 if light else 140),
                                 shadow=glassy("#000000", 70 if light else 140))
        self._style_detail()

    def _style_detail(self) -> None:
        colors = themed_log_colors(getattr(self, "_mode", "default"))
        ink = colors["error"] if self.detail.property("failed") else colors["normal"]
        self.detail.setStyleSheet(f"color: {ink}; font-size: 11px; background: transparent;")
