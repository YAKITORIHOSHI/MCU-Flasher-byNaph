"""Preview what reverting an AI edit leaves before anything is written."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor
import shiboken6
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QRadioButton, QSizePolicy, QSplitter, QVBoxLayout, QWidget)

from main.qt.ai_changes_panel import AIChangesPanel, CodePreview
from main.qt.glass import GlassDialog
from main.qt.icons import ActionButton


class _GuiRelay(QObject):
    """One permanent GUI-thread object that workers hand results to.

    A worker emitting on the dialog itself could race the dialog's deletion on the GUI
    thread (an access violation on Windows). This object is never deleted, and it delivers
    each result on the GUI thread only while the dialog still exists.
    """

    deliver = Signal(object, object)

    def __init__(self):
        super().__init__()
        self.deliver.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    @staticmethod
    def _deliver(callback, result):
        callback(result)


_RELAY: _GuiRelay | None = None


def _gui_relay() -> _GuiRelay:
    global _RELAY
    if _RELAY is None:
        _RELAY = _GuiRelay()  # First made by a dialog, so it lives on the GUI thread.
    return _RELAY


class AIRevertDialog(GlassDialog):
    """Window-modal: choose how far back to go, see it, then apply or cancel."""

    _planned = Signal(int, object)
    _applied = Signal(object)

    def __init__(self, backend, edit_ids, title, parent=None):
        super().__init__(parent)
        self._backend = backend
        self._ids = list(edit_ids)
        self._plans = {}
        self._token = 0
        self._busy = False
        self._relay = _gui_relay()
        self.setWindowTitle("Revert AI edit")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        self._heading = QLabel(title, self)
        self._heading.setWordWrap(True)
        self._heading.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._heading)
        choices = QHBoxLayout()
        choices.setSpacing(14)
        self._only = QRadioButton("Undo only this edit", self)
        self._only.setToolTip("Takes this edit's lines back out and keeps everything written after it")
        self._restore = QRadioButton("Go back to before it", self)
        self._restore.setToolTip("Puts the file back as it was before this edit, replacing later changes")
        self._only.setChecked(True)
        for button in (self._only, self._restore):
            choices.addWidget(button)
            button.toggled.connect(lambda on: on and self._load())
        choices.addStretch(1)
        self._files = QComboBox(self)
        self._files.setAccessibleName("File in this revert")
        self._files.setMinimumWidth(0)
        self._files.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._files.currentIndexChanged.connect(lambda _index: self._show_file())
        choices.addWidget(self._files)
        layout.addLayout(choices)
        self._previews = QSplitter(Qt.Orientation.Horizontal, self)
        self._previews.setChildrenCollapsible(False)
        self._now = self._preview("Now")
        self._then = self._preview("After revert")
        layout.addWidget(self._previews, 1)
        self._note = QLabel("Preparing the preview…", self)
        self._note.setWordWrap(True)
        self._note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._note)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self._cancel = ActionButton("Cancel", self)
        self._cancel.clicked.connect(self.reject)
        self._apply = ActionButton("Revert", self, icon_name="undo")
        self._apply.setDefault(True)
        self._apply.clicked.connect(self._start_apply)
        for button in (self._cancel, self._apply):
            button.setFixedHeight(30)
        actions.addWidget(self._cancel)
        actions.addWidget(self._apply)
        layout.addLayout(actions)
        self._planned.connect(self._finish_plan, Qt.ConnectionType.QueuedConnection)
        self._applied.connect(self._finish_apply, Qt.ConnectionType.QueuedConnection)
        self.apply_theme()
        from main.qt.responsive import fit_dialog, ScreenWatcher
        fit_dialog(self, (900, 560), (360, 300))
        self._screen_watcher = ScreenWatcher(self)
        self._load()

    def _preview(self, title):
        panel = QWidget(self._previews)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 4, 0)
        label = QLabel(title, panel)
        column.addWidget(label)
        view = CodePreview(panel)
        view.setAccessibleName(f"{title}: source preview")
        from main.core.config import get_monitor_font_size
        size = get_monitor_font_size()
        view.setStyleSheet(f"QPlainTextEdit {{ font-family: 'Consolas'; font-size: {size}pt; }}")
        view.setMinimumHeight(view.fontMetrics().lineSpacing() * 4)
        column.addWidget(view, 1)
        self._previews.addWidget(panel)
        return view

    def apply_theme(self, mode=None):
        from main.core.theme import Theme
        from main.qt.theme import get_palette, glass_tokens, liquid_button_sheet
        mode = mode or Theme.active_theme
        palette = get_palette(mode)
        self._heading.setStyleSheet(f"color: {palette['TEXT_BRIGHT']}; font-size: 14px; font-weight: 700;")
        self._note.setStyleSheet(f"color: {palette['TEXT']}; font-size: 11px;")
        glass = glass_tokens(mode)
        choice = (f"QRadioButton {{ color: {palette['TEXT']}; font-size: 12px; spacing: 7px; background: transparent; }}"
                  f"QRadioButton::indicator {{ width: 14px; height: 14px; border-radius: 8px; border: 1px solid {palette['TEXT_DIM']};"
                  f" background: {glass['well']}; }}"
                  f"QRadioButton::indicator:checked {{ border: 1px solid {palette['CYAN']}; background: qradialgradient(cx:.5, cy:.5,"
                  f" radius:.5, fx:.5, fy:.5, stop:0 {palette['CYAN']}, stop:.5 {palette['CYAN']}, stop:.56 transparent, stop:1 transparent); }}"
                  f"QRadioButton:focus {{ color: {palette['TEXT_BRIGHT']}; }}")
        for button in (self._only, self._restore):
            button.setStyleSheet(choice)
        self._cancel.setStyleSheet(liquid_button_sheet(mode, "glass", radius=15))
        self._apply.setStyleSheet(liquid_button_sheet(mode, "accent", radius=15))
        self._colors = []
        for name in ("RED", "GREEN"):
            color = QColor(palette[name])
            color.setAlpha(45)
            self._colors.append(color)
        for view in (self._now, self._then):
            view.apply_theme(palette)

    def _run(self, work, done, *token):
        def arrive(result):
            # On the GUI thread. The dialog may have closed first; a plan writes nothing.
            if shiboken6.isValid(self):
                done.emit(*token, result)

        relay = self._relay

        def task():
            try:
                result = work()
            except Exception as exc:
                result = {"success": False, "error": str(exc) or type(exc).__name__}
            relay.deliver.emit(arrive, result)
        try:
            threading.Thread(target=task, name="MCU_AIRevert", daemon=True).start()
        except RuntimeError as exc:
            done.emit(*token, {"success": False, "error": str(exc)})

    def _load(self):
        restore = self._restore.isChecked()
        self._token += 1
        self._apply.setEnabled(False)
        if restore in self._plans:
            self._finish_plan(self._token, self._plans[restore])
            return
        self._note.setText("Preparing the preview…")
        manager = self._backend.ai_review_manager
        self._run(lambda: manager.plan_ai_revert(self._ids, restore), self._planned, self._token)

    def _finish_plan(self, token, plan):
        if token != self._token:
            return
        if plan.get("success"):
            self._plans[bool(plan.get("restore"))] = plan
        self._plan = plan
        current = self._files.currentText()
        self._files.blockSignals(True)
        self._files.clear()
        self._files.addItems([item["fileName"] for item in plan.get("files", ())])
        self._files.setCurrentIndex(max(0, self._files.findText(current)))
        self._files.blockSignals(False)
        self._files.setVisible(self._files.count() > 1)
        self._apply.setEnabled(bool(plan.get("success") and plan.get("changed")) and not self._busy)
        self._note.setText(self._describe(plan))
        self._show_file()

    @staticmethod
    def _describe(plan):
        if not plan.get("success"):
            return plan.get("error", "This edit cannot be reverted.")
        parts = []
        if plan["blocked"]:
            parts.append("Some lines of this edit were changed again since and stay as they are. "
                         "Choose Go back to before it to replace them too.")
        if not plan["changed"]:
            parts.append("Nothing is left of this edit to undo." if plan["blocked"] else
                         "These files already are as they were before the edit.")
        elif plan["restore"]:
            parts.append("Everything written to these files after the edit is replaced.")
        else:
            parts.append("Later changes are kept.")
        parts.extend(plan["problems"])
        if plan["changed"]:
            parts.append("Undo AI in the editor takes this back.")
        return " ".join(parts)

    def _show_file(self):
        files = getattr(self, "_plan", {}).get("files", [])
        index = self._files.currentIndex()
        item = files[index] if 0 <= index < len(files) else {"current": "", "result": "", "exists": True, "resultExists": True}
        now = item["current"] if item["exists"] else "(This file is not in the sketch now.)"
        then = item["result"] if item["resultExists"] else "(This file is removed: the edit created it.)"
        for view, text in ((self._now, now), (self._then, then)):
            if view.toPlainText() != text:
                view.setPlainText(text)
        removed, added = AIChangesPanel._changed_ranges(now, then)
        AIChangesPanel._highlight(self._now, removed, self._colors[0])
        AIChangesPanel._highlight(self._then, added, self._colors[1])
        for view, ranges in ((self._now, removed), (self._then, added)):
            if ranges:
                cursor = view.textCursor()
                cursor.setPosition(view.document().findBlockByNumber(ranges[0][0]).position())
                view.setTextCursor(cursor)
                view.centerCursor()

    def _start_apply(self):
        plan = getattr(self, "_plan", {})
        if self._busy or not plan.get("success") or not plan.get("changed"):
            return
        self._busy = True
        for control in (self._apply, self._only, self._restore, self._cancel):
            control.setEnabled(False)
        self._note.setText("Reverting…")
        self._run(lambda: self._backend.revert_ai_edits(plan), self._applied)

    def _finish_apply(self, result):
        self._busy = False
        if result.get("success"):
            self.accept()
            return
        for control in (self._only, self._restore, self._cancel):
            control.setEnabled(True)
        self._plans.clear()
        if result.get("conflict"):
            self._load()        # Show what is there now, with the reason.
        self._note.setText(result.get("error", "The edit could not be reverted."))

    def reject(self):
        if not self._busy:      # A write in progress finishes before the dialog closes.
            super().reject()

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
        else:
            super().closeEvent(event)
