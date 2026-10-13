"""Paint SVG status marks outside copyable log text; no timers or extra blocks."""
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import QWidget
from main.qt.icons import icon


class SvgLogGutter(QWidget):
    def __init__(self, editor, name_for, *, transparent=False):
        super().__init__(editor)
        self.editor, self.name_for = editor, name_for
        # Setup logs expose their owning glass card; an opaque palette Base
        # (black on native Windows) must not cover that material. Ordinary
        # console gutters retain their solid reading surface by default.
        self._transparent = bool(transparent)
        if self._transparent:
            self.setAutoFillBackground(False)
            self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        editor.setViewportMargins(22, 0, 0, 0)
        editor.updateRequest.connect(self._update_request)
        editor.installEventFilter(self)
        self._fit()

    def _fit(self):
        area = self.editor.contentsRect()
        self.setGeometry(QRect(area.left(), area.top(), 22, area.height()))

    def eventFilter(self, watched, event):
        from PySide6.QtCore import QEvent
        if event.type() in (QEvent.Type.Resize, QEvent.Type.FontChange, QEvent.Type.PaletteChange):
            self._fit(); self.update()
        return False

    def _update_request(self, rect, dy):
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if not self._transparent:
            painter.fillRect(self.rect(), self.editor.palette().color(QPalette.ColorRole.Base))
        block = self.editor.firstVisibleBlock()
        y = self.editor.blockBoundingGeometry(block).translated(self.editor.contentOffset()).top()
        budget = 256
        while block.isValid() and y < self.height() and budget:
            height = self.editor.blockBoundingRect(block).height()
            if block.isVisible() and y+height >= 0:
                name, color = self.name_for(block)
                if name:
                    icon(name, color, size=16).paint(painter, 3, round(y+max(0,(self.editor.fontMetrics().height()-16)/2)), 16, 16)
            y += height
            block = block.next(); budget -= 1
        painter.end()
