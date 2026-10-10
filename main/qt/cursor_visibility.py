"""Event-driven pointer recovery, confined to one workspace and its dialogs."""
import sys

from PySide6.QtCore import QObject, QEvent, Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget, QLineEdit, QTextEdit, QPlainTextEdit
from shiboken6 import isValid


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
        if sys.platform.startswith("linux"):
            # Global Python filters wrap Qt's private internal objects before
            # delivery. WebEngine can recursively reenter that conversion on
            # Linux, so only watch the workspace's constructed QWidget tree.
            self._watched_widgets = {}
            self._scan_timer = QTimer(self)
            self._scan_timer.setSingleShot(True)
            self._scan_timer.timeout.connect(self._scan_owned_widgets)
            self._scan_owned_widgets()
        elif app:
            app.installEventFilter(self)

    def _scan_owned_widgets(self):
        if not isValid(self._owner):
            return
        widgets = [self._owner, *self._owner.findChildren(QWidget)]
        owned = {id(widget) for widget in widgets}
        for key, widget in tuple(self._watched_widgets.items()):
            if not isValid(widget) or key not in owned:
                if isValid(widget):
                    widget.removeEventFilter(self)
                self._watched_widgets.pop(key, None)
        for widget in widgets:
            key = id(widget)
            if key not in self._watched_widgets:
                self._watched_widgets[key] = widget
                widget.installEventFilter(self)
                self._restore_pointer(widget)

    def _restore_pointer(self, watched):
        if not self.owns(watched) or watched.cursor().shape() != Qt.CursorShape.BlankCursor:
            return
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

    def eventFilter(self, watched, event):
        if sys.platform.startswith("linux"):
            kind = event.type()
            if kind in (QEvent.Type.ChildAdded, QEvent.Type.ChildRemoved,
                        QEvent.Type.ChildPolished, QEvent.Type.ParentChange):
                # Defer the scan until widget construction has returned; never
                # inspect event.child() while Qt may still be polishing it.
                self._scan_timer.start(0)
            if (not self._restoring and isValid(watched) and isinstance(watched, QWidget)
                    and kind in (QEvent.Type.CursorChange, QEvent.Type.Enter, QEvent.Type.MouseMove)):
                self._restore_pointer(watched)
            return False
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
