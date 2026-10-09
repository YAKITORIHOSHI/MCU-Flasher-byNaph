"""Follow live output without interrupting the reader's scroll or selection."""
from contextlib import contextmanager
from functools import wraps

from PySide6.QtCore import QObject, QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QApplication
from shiboken6 import isValid


class LogFollow(QObject):
    """Keep one viewport anchor while output changes a bounded text document.

    Scrollbar values describe document positions, not stable text identities.
    A persistent cursor follows retained text when old history is removed.
    Only output transactions suppress the reader's scrollbar change signals.
    """

    def __init__(self, view, enabled=True, *, resume_on_release=False, hold_to_pause=False):
        super().__init__(view)
        self.view = view
        self.enabled = bool(enabled)
        # Live logs follow new output, pausing during a hold. Idle wheel/key
        # navigation must remain where the reader left it until output arrives.
        # Other logs can retain their independent reading-position policy.
        self.hold_to_pause = bool(hold_to_pause)
        self.resume_on_release = bool(resume_on_release or hold_to_pause)
        self.scrollbar_held = False
        self.user_scrolled_up = False
        self._depth = 0
        self._content_revision = 0
        self._plain = isinstance(view, QPlainTextEdit)
        self._pending_scroll = None
        self._resize_state = None
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.timeout.connect(self._settle_scroll)
        self._resume_timer = QTimer(self)
        self._resume_timer.setSingleShot(True)
        self._resume_timer.timeout.connect(self._finish_release)
        bar = view.verticalScrollBar()
        self._bar = bar
        bar.valueChanged.connect(self._on_value_changed)
        bar.rangeChanged.connect(self._on_range_changed)
        bar.sliderPressed.connect(self._press)
        bar.sliderReleased.connect(self._release)
        view.selectionChanged.connect(self._selection_changed)
        view.document().contentsChange.connect(self._on_content_changed)
        bar.installEventFilter(self)
        view.viewport().installEventFilter(self)
        view.installEventFilter(self)
        # A drag can finish outside the scrollbar or viewport. Observe release
        # at the application boundary so a lost grab cannot leave Auto anchored.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _at_bottom(self):
        bar = self.view.verticalScrollBar()
        return bar.value() == bar.maximum()

    def _on_value_changed(self, _value):
        if not self._depth:
            self._cancel_settle()
            self.user_scrolled_up = not self._at_bottom()

    def _on_content_changed(self, _position, removed, added):
        # QTextDocument.revision() also advances for empty edit blocks. Only
        # real content changes should make an idle output transaction follow.
        if removed or added:
            self._content_revision += 1

    def _press(self):
        self._cancel_settle()
        self._resume_timer.stop()
        self.scrollbar_held = True

    def _selection_changed(self):
        if not self._depth:
            self._cancel_settle()

    def _wrapped(self):
        return self._plain and self.view.lineWrapMode() != QPlainTextEdit.LineWrapMode.NoWrap

    def _cancel_settle(self):
        self._settle_timer.stop()
        self._pending_scroll = None
        self._resize_state = None

    def _on_range_changed(self, _minimum, _maximum):
        # QPlainTextEdit can finish wrapping after the output transaction.
        # Coalesce that layout work without polling or one callback per line.
        if self._depth or not self._wrapped() or self.scrollbar_held or self._bar.isSliderDown():
            return
        if self._pending_scroll is None and self.enabled and not self.user_scrolled_up:
            self._pending_scroll = self._capture(False, follow_output=False)
        if self._pending_scroll is not None:
            self._settle_timer.start(0)

    def _settle_scroll(self):
        state = self._pending_scroll
        self._pending_scroll = None
        if state is None or not isValid(self.view) or self.scrollbar_held or self._bar.isSliderDown():
            return
        self._depth += 1
        try:
            self._restore_scroll(state)
        finally:
            self._depth -= 1

    def _release(self):
        self.scrollbar_held = False
        # The scrollbar applies its final drag/track position after its event
        # filter and can also emit sliderReleased during that handler.
        self._resume_timer.start(0)

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
            state = self._capture(False)
            state["following"] = True
            self._depth += 1
            try:
                self._restore_scroll(state)
                if self._wrapped():
                    self._pending_scroll = state
                    self._settle_timer.start(0)
            finally:
                self._depth -= 1

    def _sync_scroll(self):
        if isValid(self.view) and not self._depth:
            self.user_scrolled_up = not self._at_bottom()

    def eventFilter(self, obj, event):
        # Child scrollbars receive destruction events after their view is gone.
        if not isValid(self.view):
            return False
        kind = event.type()
        if (self.scrollbar_held and kind == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton):
            self._release()
        if self.scrollbar_held and kind in (QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate):
            self._release()
        if obj == self._bar:
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._press()
            elif kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._release()
        elif obj in (self.view, self.view.viewport()) and kind == QEvent.Type.MouseButtonPress:
            if self.hold_to_pause and event.button() == Qt.MouseButton.LeftButton:
                self._press()
            else:
                self._cancel_settle()
        elif obj in (self.view, self.view.viewport()) and kind in (QEvent.Type.Wheel, QEvent.Type.KeyPress):
            # QAbstractScrollArea routes wheel input through its viewport.
            self._cancel_settle()
            self._resume_timer.stop()
            QTimer.singleShot(0, self._sync_scroll)
        return super().eventFilter(obj, event)

    def set_enabled(self, enabled):
        self._cancel_settle()
        self._resume_timer.stop()
        self.enabled = bool(enabled)
        if self.resume_on_release and self.enabled:
            self._resume_scroll()
        else:
            self._sync_scroll()

    def reset(self):
        self._cancel_settle()
        self._resume_timer.stop()
        # Clearing output must not release a scrollbar the reader still holds.
        self.scrollbar_held = self.scrollbar_held or self._bar.isSliderDown()
        self.user_scrolled_up = False

    def _capture(self, rebuild, *, follow_output=True):
        view = self.view
        bar = view.verticalScrollBar()
        wrapped = self._wrapped()
        following = (self.enabled and not self.scrollbar_held and not bar.isSliderDown()
                     and ((self.hold_to_pause and follow_output)
                          or (not self.user_scrolled_up and (wrapped or self._at_bottom()))))
        if self._plain and not wrapped:
            anchor = QTextCursor(view.firstVisibleBlock())
        else:
            anchor = view.cursorForPosition(QPoint(0, 1))
        anchor.setKeepPositionOnInsert(True)
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
            "content_revision": self._content_revision,
            "block": anchor.blockNumber(), "y": view.cursorRect(anchor).top(),
            "horizontal": view.horizontalScrollBar().value(), "selection": endpoints,
            "rebuild": rebuild, "empty": view.document().isEmpty(),
            "selection_points": points,
            "anchor_point": (anchor.blockNumber(), anchor.positionInBlock(), anchor.block().text()) if rebuild else None,
            "row_offset": bar.value() - self._visual_row(anchor) if wrapped else 0,
        }

    @staticmethod
    def _visual_row(cursor):
        block = cursor.block()
        layout = block.layout()
        line = layout.lineForTextPosition(cursor.positionInBlock()) if layout is not None else None
        return max(0, block.firstLineNumber()) + (line.lineNumber() if line is not None and line.isValid() else 0)

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
        if state["anchor_point"] is not None:
            anchor = QTextCursor(view.document())
            anchor.setPosition(self._rebuilt_position(state["anchor_point"]))
            anchor.setKeepPositionOnInsert(True)
            state["anchor"] = anchor
            state["block"] = anchor.blockNumber()
            state["anchor_point"] = None
        self._restore_scroll(state)

    def _restore_scroll(self, state):
        view = self.view
        bar = view.verticalScrollBar()
        if state["following"] and not self.scrollbar_held and not bar.isSliderDown():
            # Moving into a lazily laid-out final block can increase the range.
            # Resolve that bounded final viewport before finishing the follow.
            for _ in range(4):
                bar.setValue(bar.maximum())
                if bar.value() == bar.maximum():
                    break
            self.user_scrolled_up = False
        elif self._wrapped():
            bar.setValue(0 if state["empty"] else self._visual_row(state["anchor"]) + state["row_offset"])
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
    def update(self, *, rebuild=False, resize=False):
        outer = not self._depth
        state = (self._resize_state if resize and self._resize_state is not None
                 else self._capture(rebuild, follow_output=not (resize or rebuild))) if outer else None
        if outer:
            # Keep the original character through repeated width changes;
            # recapturing each newly wrapped row would accumulate drift.
            self._resize_state = state if resize else None
        self._depth += 1
        try:
            yield
        finally:
            try:
                if outer and (resize or rebuild or self._content_revision != state["content_revision"]):
                    self._restore(state)
                    if self._wrapped() and not self.scrollbar_held and not self._bar.isSliderDown():
                        self._pending_scroll = state
                        self._settle_timer.start(0)
                    else:
                        self._cancel_settle()
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
