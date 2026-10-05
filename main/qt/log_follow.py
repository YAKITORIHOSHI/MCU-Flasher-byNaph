"""Follow live output without interrupting the reader's scroll or selection."""
from contextlib import contextmanager
from functools import wraps

from PySide6.QtCore import QObject, QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit
from shiboken6 import isValid


class LogFollow(QObject):
    """Keep one viewport anchor while output changes a bounded text document.

    Scrollbar values describe document positions, not stable text identities.
    A persistent cursor follows retained text when old history is removed.
    Only output transactions suppress the reader's scrollbar change signals.
    """

    def __init__(self, view, enabled=True, *, resume_on_release=False):
        super().__init__(view)
        self.view = view
        self.enabled = bool(enabled)
        # Setup can explicitly resume following on release; workspace logs
        # keep their reading position until the reader returns to the bottom.
        self.resume_on_release = bool(resume_on_release)
        self.scrollbar_held = False
        self.user_scrolled_up = False
        self._depth = 0
        self._plain = isinstance(view, QPlainTextEdit)
        bar = view.verticalScrollBar()
        self._bar = bar
        bar.valueChanged.connect(self._on_value_changed)
        bar.sliderPressed.connect(self._press)
        bar.sliderReleased.connect(self._release)
        bar.installEventFilter(self)
        view.viewport().installEventFilter(self)
        view.installEventFilter(self)

    def _at_bottom(self):
        bar = self.view.verticalScrollBar()
        return bar.value() == bar.maximum()

    def _on_value_changed(self, _value):
        if not self._depth:
            self.user_scrolled_up = not self._at_bottom()

    def _press(self):
        self.scrollbar_held = True

    def _release(self):
        self.scrollbar_held = False
        # The scrollbar applies its final drag/track position after its event
        # filter and can also emit sliderReleased during that handler.
        QTimer.singleShot(0, self._finish_release)

    def _finish_release(self):
        if not isValid(self.view) or self.scrollbar_held or self._bar.isSliderDown():
            return
        if self.resume_on_release and self.enabled:
            self._resume_scroll()
        else:
            self._sync_scroll()

    def _resume_scroll(self):
        if (isValid(self.view) and self.enabled and not self._depth
                and not self.scrollbar_held and not self._bar.isSliderDown()):
            self.user_scrolled_up = False
            self._bar.setValue(self._bar.maximum())

    def _sync_scroll(self):
        if isValid(self.view) and not self._depth:
            self.user_scrolled_up = not self._at_bottom()

    def eventFilter(self, obj, event):
        # Child scrollbars receive destruction events after their view is gone.
        if not isValid(self.view):
            return False
        kind = event.type()
        if obj == self._bar:
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._press()
            elif kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._release()
        elif kind in (QEvent.Type.Wheel, QEvent.Type.KeyPress):
            # QAbstractScrollArea routes wheel input through its viewport.
            QTimer.singleShot(0, self._sync_scroll)
        return super().eventFilter(obj, event)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        if self.resume_on_release and self.enabled:
            self._resume_scroll()
        else:
            self._sync_scroll()

    def reset(self):
        self.scrollbar_held = False
        self.user_scrolled_up = False

    def _capture(self, rebuild):
        view = self.view
        bar = view.verticalScrollBar()
        following = (self.enabled and not self.scrollbar_held and not bar.isSliderDown()
                     and not self.user_scrolled_up and self._at_bottom())
        if self._plain:
            anchor = QTextCursor(view.firstVisibleBlock())
        else:
            anchor = view.cursorForPosition(QPoint(0, 0))
        selection = QTextCursor(view.textCursor())
        endpoints = []
        points = []
        for position in (selection.anchor(), selection.position()):
            point = QTextCursor(view.document())
            point.setPosition(position)
            point.setKeepPositionOnInsert(True)
            endpoints.append(point)
            if rebuild:
                points.append((point.blockNumber(), point.positionInBlock(), point.block().text()))
        return {
            "following": following, "anchor": anchor,
            "block": anchor.blockNumber(), "y": view.cursorRect(anchor).top(),
            "horizontal": view.horizontalScrollBar().value(), "selection": endpoints,
            "rebuild": rebuild, "empty": view.document().isEmpty(),
            "selection_points": points,
        }

    def _rebuilt_position(self, point):
        number, column, old_text = point
        block = self.view.document().findBlockByNumber(number)
        if not block.isValid():
            return max(0, self.view.document().characterCount() - 1)
        new_text = block.text()
        # Timestamp changes add or remove a prefix, leaving the selected text
        # in the same block. QTextCursor columns are UTF-16 positions.
        if old_text and (new_text.endswith(old_text) or old_text.endswith(new_text)):
            column += (len(new_text.encode("utf-16-le")) - len(old_text.encode("utf-16-le"))) // 2
        return block.position() + max(0, min(column, block.length() - 1))

    def _restore(self, state):
        view = self.view
        bar = view.verticalScrollBar()
        # Appending output must not move the active caret or erase a selection.
        if state["rebuild"]:
            selection = QTextCursor(view.document())
            selection.setPosition(self._rebuilt_position(state["selection_points"][0]))
            selection.setPosition(self._rebuilt_position(state["selection_points"][1]), QTextCursor.MoveMode.KeepAnchor)
        else:
            selection = QTextCursor(view.document())
            selection.setPosition(state["selection"][0].position())
            selection.setPosition(state["selection"][1].position(), QTextCursor.MoveMode.KeepAnchor)
        if view.textCursor() != selection:
            view.setTextCursor(selection)
        if state["following"] and not self.scrollbar_held and not bar.isSliderDown():
            bar.setValue(bar.maximum())
        elif self._plain:
            block = state["block"] if state["rebuild"] else state["anchor"].blockNumber()
            bar.setValue(0 if state["empty"] else max(0, block))
        elif state["empty"]:
            bar.setValue(0)
        else:
            delta = view.cursorRect(state["anchor"]).top() - state["y"]
            bar.setValue(bar.value() + delta)
        view.horizontalScrollBar().setValue(state["horizontal"])

    @contextmanager
    def update(self, *, rebuild=False):
        outer = not self._depth
        state = self._capture(rebuild) if outer else None
        self._depth += 1
        try:
            yield
        finally:
            try:
                if outer:
                    self._restore(state)
            finally:
                self._depth -= 1


def preserve_log_view(*, rebuild=False):
    """Wrap a widget's output mutation in its shared following transaction."""
    def decorate(method):
        @wraps(method)
        def render(self, *args, **kwargs):
            with self._follow.update(rebuild=rebuild):
                return method(self, *args, **kwargs)
        return render
    return decorate


__all__ = ["LogFollow", "preserve_log_view"]
