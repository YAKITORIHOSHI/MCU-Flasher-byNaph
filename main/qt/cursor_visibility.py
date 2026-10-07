"""Event-driven pointer recovery, confined to one workspace and its dialogs."""
from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtWidgets import QApplication, QWidget, QLineEdit, QTextEdit, QPlainTextEdit


class WorkspacePointerGuard(QObject):
    """Do not let an embedded renderer leave the workspace pointer blank.

    Keep normal I-beam/link/resize/busy cursors. Never touch Windows pointer
    preferences or the application's override-cursor stack, and never poll.
    """

    def __init__(self, owner: QWidget):
        super().__init__(owner)
        self._owner = owner
        self._restoring = False
        app = QApplication.instance()
        if app:
            app.installEventFilter(self)

    def eventFilter(self, watched, event):
        if self._restoring or event.type() not in (
            QEvent.Type.CursorChange, QEvent.Type.Enter, QEvent.Type.MouseMove,
        ):
            return False
        if not isinstance(watched, QWidget):
            return False
        if not self.owns(watched):
            return False
        if watched.cursor().shape() != Qt.CursorShape.BlankCursor:
            return False
        text_pointer = isinstance(watched, (QLineEdit, QTextEdit, QPlainTextEdit))
        ancestor = watched
        while ancestor is not None and not text_pointer:
            text_pointer = bool(ancestor.property("mcuTextPointer"))
            ancestor = ancestor.parentWidget()
        self._restoring = True
        try:
            watched.setCursor(Qt.CursorShape.IBeamCursor if text_pointer else Qt.CursorShape.ArrowCursor)
        finally:
            self._restoring = False
        return False

    def owns(self, widget: QWidget) -> bool:
        # QWidget.isAncestorOf deliberately stops at window boundaries. Follow
        # explicit ownership instead, including the detached editor and dialogs.
        while widget is not None:
            if widget is self._owner:
                return True
            widget = widget.parentWidget()
        return False
