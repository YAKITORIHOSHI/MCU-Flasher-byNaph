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

    def visible_marks(self):
        """Paint one mark at each block's first laid-out text line."""
        block = self.editor.firstVisibleBlock()
        budget = 256
        while block.isValid() and budget:
            bounds = self.editor.blockBoundingGeometry(block).translated(self.editor.contentOffset())
            if bounds.top() >= self.height():
                break
            if block.isVisible() and bounds.bottom() >= 0:
                name, color = self.name_for(block)
                layout = block.layout()
                if name and layout.lineCount():
                    line = layout.lineAt(0)
                    # Heading/result fonts differ. The widget's default font
                    # metrics cannot locate a rich paragraph's actual line.
                    top = bounds.top() + line.y() + (line.height() - 16) / 2
                    rect = QRect(3, round(top), 16, 16)
                    if rect.intersects(self.rect()):
                        yield block, rect, name, color
            block = block.next(); budget -= 1

    def paintEvent(self, event):
        painter = QPainter(self)
        if not self._transparent:
            painter.fillRect(self.rect(), self.editor.palette().color(QPalette.ColorRole.Base))
        for _, rect, name, color in self.visible_marks():
            icon(name, color, size=16).paint(painter, rect)
        painter.end()
