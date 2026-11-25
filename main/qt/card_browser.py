"""Expandable glass cards whose text stays one selectable, copyable document.

Every card is a frame in an ordinary text document, so selection, copying, links and
keyboard scrolling keep working and hundreds of cards cost one widget. The rounded
sheet, accent rail, mark and expand arrow are painted around that text.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QPainter, QPainterPath, QPen, QTextBlockFormat, QTextCharFormat,
                           QTextCursor, QTextFrameFormat)
from PySide6.QtWidgets import QTextBrowser

from main.qt.icons import icon

_GAP = 6
_PADDING = 9
_TEXT_LEFT = 32
_ARROW = 22
_RADIUS = 10


@dataclass
class Card:
    """One card. `details` are (label, value) rows; each `body` row is (text, color tag)
    or a list of those, shown on one line."""

    key: str
    title: str
    accent: str = "info"
    mark: str = "info"
    meta: str = ""
    body: list = field(default_factory=list)
    details: list = field(default_factory=list)
    links: list = field(default_factory=list)
    preview: list | None = None   # Body rows while collapsed; None shows `body` always.
    columns: int = 0              # Lay the body out as this many aligned columns.

    @property
    def expandable(self) -> bool:
        return bool(self.details or self.links or (self.preview is not None and self.preview != self.body))


class CardBrowser(QTextBrowser):
    """Paints one glass sheet per top-level frame and toggles a card's details."""

    toggled = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._before_read = None
        self.setUndoRedoEnabled(False)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        super().document().setDocumentMargin(8)
        self.viewport().setMouseTracking(True)
        self._cards: list[Card] = []
        self._expanded: set[str] = set()
        self._colors: dict[str, str] = {}
        self._light = False
        self._accent_ink = "#ffffff"
        self._hover = -1
        self._pressed = None

    # ── Content ───────────────────────────────────────────────────────────────
    def set_before_read(self, callback) -> None:
        """Called before the document is read, so a coalesced update is never missed."""
        self._before_read = callback

    def document(self):
        self._settle()
        return super().document()

    def toPlainText(self) -> str:
        self._settle()
        return super().toPlainText().strip("\n")

    def _settle(self) -> None:
        callback, self._before_read = self._before_read, None
        if callback is not None:
            try:
                callback()
            finally:
                self._before_read = callback

    def set_theme(self, colors: dict, theme_name: str) -> None:
        """`colors` are text inks for a reading surface (themed_log_colors)."""
        self._colors = dict(colors)
        self._light = theme_name == "light"

    def is_expanded(self, key: str) -> bool:
        return key in self._expanded

    def set_expanded(self, keys) -> None:
        self._expanded = set(keys)

    def cards(self) -> list[Card]:
        return list(self._cards)

    def set_cards(self, cards, *, message: str = "", message_tag: str = "dim") -> None:
        """Replace the document; `message` is the plain text of an empty list."""
        document = super().document()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()
        cursor.setBlockFormat(QTextBlockFormat())
        self._cards = list(cards)
        self._expanded &= {card.key for card in self._cards}
        if not self._cards:
            cursor.insertText(message, self._format(message_tag, italic=True))
        else:
            for card in self._cards:
                self._insert(document.rootFrame().lastCursorPosition(), card)
            self._separate(document.rootFrame().firstCursorPosition())
            self._separate(document.rootFrame().lastCursorPosition())
        cursor.endEditBlock()
        self._hover = -1
        self.viewport().update()

    def prepend_card(self, card: Card, *, limit: int = 0) -> None:
        """Add the newest card on top without rebuilding the rest."""
        if not self._cards:
            self.set_cards([card])
            return
        document = super().document()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        self._cards.insert(0, card)
        self._insert(document.rootFrame().firstCursorPosition(), card)
        self._separate(document.rootFrame().firstCursorPosition())
        while limit and len(self._cards) > limit:
            self._remove_last()
        cursor.endEditBlock()
        self.viewport().update()

    def _remove_last(self) -> None:
        card = self._cards.pop()
        self._expanded.discard(card.key)
        frame = super().document().rootFrame().childFrames()[-1]
        cursor = QTextCursor(super().document())
        cursor.setPosition(max(0, frame.firstPosition() - 2))
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self._separate(super().document().rootFrame().lastCursorPosition())

    def _insert(self, at: QTextCursor, card: Card) -> None:
        outline = QTextFrameFormat()
        outline.setBorder(0)
        outline.setMargin(0)
        outline.setPadding(_PADDING)
        self._separate(at)
        at.insertFrame(outline)
        expanded = card.key in self._expanded
        title = QTextBlockFormat()
        title.setLeftMargin(_TEXT_LEFT - _PADDING)
        title.setRightMargin(_ARROW + 6 if card.expandable else 0)
        text = QTextBlockFormat(title)
        text.setTopMargin(2)
        body_format = QTextBlockFormat(text)
        body_format.setRightMargin(4)
        at.setBlockFormat(title)
        at.insertText(card.title, self._format(card.accent, bold=True))
        if card.meta:
            at.insertBlock(title)
            at.insertText(card.meta, self._format("dim", small=True))
        rows = card.body if expanded or card.preview is None else card.preview
        if card.columns > 1 and expanded and rows:
            self._insert_columns(at, rows, card.columns, body_format)
        else:
            for row in rows:
                at.insertBlock(body_format)
                self._insert_row(at, row)
        if expanded:
            for index, (label, value) in enumerate(card.details):
                detail = QTextBlockFormat(body_format)
                detail.setTopMargin(8 if not index else 1)
                at.insertBlock(detail)
                at.insertText(f"{label}:  ", self._format("dim", small=True))
                at.insertText(value, self._format("normal", small=True))
            for label, href in card.links:
                at.insertBlock(body_format)
                link = self._format("system", small=True)
                link.setAnchor(True)
                link.setAnchorHref(href)
                link.setFontUnderline(True)
                at.insertText(label, link)

    def _insert_columns(self, at: QTextCursor, rows, columns: int, block: QTextBlockFormat) -> None:
        """Body rows in reading order down each column, aligned without a visible grid."""
        from PySide6.QtGui import QTextLength, QTextTableFormat
        height = (len(rows) + columns - 1) // columns
        layout = QTextTableFormat()
        layout.setBorder(0)
        layout.setCellPadding(1)
        layout.setCellSpacing(0)
        layout.setTopMargin(4)
        layout.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
        # The indent is a column: a left margin would push a full-width table past the card.
        layout.setColumnWidthConstraints(
            [QTextLength(QTextLength.Type.FixedLength, _TEXT_LEFT - _PADDING)]
            + [QTextLength(QTextLength.Type.PercentageLength, 100 / columns)] * columns)
        at.insertBlock(block)
        table = at.insertTable(height, columns + 1, layout)
        for index, row in enumerate(rows):
            cell = table.cellAt(index % height, index // height + 1)
            self._insert_row(cell.firstCursorPosition(), row, small=True)
        at.setPosition(table.lastPosition() + 1)
        self._separate(at, height=2)

    def _insert_row(self, at: QTextCursor, row, *, small=False) -> None:
        for text, tag in (row if isinstance(row, list) else [row]):
            at.insertText(text, self._format(tag, small=small))

    @staticmethod
    def _separate(cursor: QTextCursor, *, height: int = _GAP) -> None:
        spacing = QTextBlockFormat()
        spacing.setLineHeight(height, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        cursor.setBlockFormat(spacing)

    def _format(self, tag: str, *, bold=False, italic=False, small=False) -> QTextCharFormat:
        result = QTextCharFormat()
        result.setForeground(QColor(self._colors.get(tag, self._colors.get("normal", "#e0e6ed"))))
        if bold:
            result.setFontWeight(QFont.Weight.DemiBold)
        result.setFontItalic(italic)
        if small:
            size = self.font().pointSizeF()
            if size > 0:
                result.setFontPointSize(max(6.0, size - 1))
        return result

    # ── Painting ──────────────────────────────────────────────────────────────
    def _card_rect(self, frame) -> QRectF:
        layout = super().document().documentLayout()
        rect = layout.frameBoundingRect(frame)
        return rect.translated(-self.horizontalScrollBar().value(), -self.verticalScrollBar().value())

    def _frames(self):
        return super().document().rootFrame().childFrames() if self._cards else []

    def _arrow_rect(self, rect: QRectF) -> QRectF:
        return QRectF(rect.right() - _ARROW - 8, rect.top() + 7, _ARROW, _ARROW)

    def paintEvent(self, event) -> None:
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(event.rect())
        # Smoke under dark text surfaces and milk under light ones only ever raise the
        # contrast the inks were chosen for.
        fill = QColor(255, 255, 255, 130) if self._light else QColor(0, 0, 0, 52)
        hover_fill = QColor(255, 255, 255, 178) if self._light else QColor(0, 0, 0, 78)
        rim = QColor(31, 35, 40, 40) if self._light else QColor(255, 255, 255, 34)
        lit = QColor(255, 255, 255, 210) if self._light else QColor(255, 255, 255, 88)
        for index, (frame, card) in enumerate(zip(self._frames(), self._cards)):
            rect = self._card_rect(frame).adjusted(0.5, 0.5, -0.5, -0.5)
            if not rect.intersects(area):
                continue
            path = QPainterPath()
            path.addRoundedRect(rect, _RADIUS, _RADIUS)
            painter.fillPath(path, hover_fill if index == self._hover else fill)
            painter.setPen(QPen(rim, 1))
            painter.drawPath(path)
            accent = QColor(self._colors.get(card.accent, "#8fa1b3"))
            painter.save()
            painter.setClipPath(path)
            painter.fillRect(QRectF(rect.left(), rect.top(), 3, rect.height()), accent)
            painter.setPen(QPen(lit, 1))
            painter.drawLine(QPointF(rect.left() + _RADIUS, rect.top()), QPointF(rect.right() - _RADIUS, rect.top()))
            painter.restore()
            painter.drawPixmap(QPointF(rect.left() + 11, rect.top() + 9),
                               icon(card.mark, accent.name(), size=16).pixmap(16, 16))
            if card.expandable:
                self._paint_arrow(painter, self._arrow_rect(rect), card.key in self._expanded, index == self._hover)
        painter.end()
        super().paintEvent(event)

    def _paint_arrow(self, painter: QPainter, rect: QRectF, expanded: bool, hovered: bool) -> None:
        ink = QColor(self._colors.get("normal" if hovered else "dim", "#8fa1b3"))
        if hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(31, 35, 40, 26) if self._light else QColor(255, 255, 255, 30))
            painter.drawRoundedRect(rect, 7, 7)
        pen = QPen(ink, 1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        center, arm, rise = rect.center(), 4.2, 2.2
        tip = -rise if expanded else rise
        path = QPainterPath(QPointF(center.x() - arm, center.y() - tip))
        path.lineTo(QPointF(center.x(), center.y() + tip))
        path.lineTo(QPointF(center.x() + arm, center.y() - tip))
        painter.drawPath(path)

    # ── Expanding ─────────────────────────────────────────────────────────────
    def _card_at(self, point: QPoint) -> int:
        for index, frame in enumerate(self._frames()):
            if self._card_rect(frame).contains(QPointF(point)):
                return index
        return -1

    def _header_at(self, point: QPoint) -> int:
        """The card whose title row or arrow is under the pointer."""
        index = self._card_at(point)
        if index < 0 or not self._cards[index].expandable or self.anchorAt(point):
            return -1
        rect = self._card_rect(self._frames()[index])
        return index if point.y() <= rect.top() + _PADDING + self.fontMetrics().lineSpacing() + 4 else -1

    def toggle(self, key: str) -> None:
        card = next((card for card in self._cards if card.key == key and card.expandable), None)
        if card is None:
            return
        expanded = key not in self._expanded
        (self._expanded.add if expanded else self._expanded.discard)(key)
        self.toggled.emit(key, expanded)

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        point = event.position().toPoint()
        hover = self._card_at(point)
        if hover != self._hover:
            self._hover = hover
            self.viewport().update()
        if not event.buttons() and self._header_at(point) >= 0:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        if self._hover >= 0:
            self._hover = -1
            self.viewport().update()

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        point = event.position().toPoint()
        self._pressed = (self._header_at(point), point) if event.button() == Qt.MouseButton.LeftButton else None

    def mouseReleaseEvent(self, event) -> None:
        pressed, self._pressed = self._pressed, None
        super().mouseReleaseEvent(event)
        if pressed is None or pressed[0] < 0 or event.button() != Qt.MouseButton.LeftButton:
            return
        point = event.position().toPoint()
        # A drag selects text; only a still click opens or closes a card.
        if (point - pressed[1]).manhattanLength() <= 4 and not self.textCursor().hasSelection() \
                and self._header_at(point) == pressed[0]:
            self.toggle(self._cards[pressed[0]].key)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and not self.textCursor().hasSelection():
            position = self.textCursor().position()
            for frame, card in zip(self._frames(), self._cards):
                if frame.firstPosition() - 1 <= position <= frame.lastPosition() + 1 and card.expandable:
                    self.toggle(card.key)
                    event.accept()
                    return
        super().keyPressEvent(event)
