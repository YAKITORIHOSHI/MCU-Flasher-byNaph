"""Bounded per-project AI history, with prompt groups and two source previews."""
from __future__ import annotations

import difflib
import threading
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor, QFont
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTreeWidget,
    QTreeWidgetItem, QSplitter, QPlainTextEdit, QMessageBox, QTextEdit, QHeaderView)

from main.qt.icons import ActionButton
from main.qt.signals import signals


class AIChangesPanel(QWidget):
    _deleted = Signal(object)

    def __init__(self, backend=None, parent=None):
        super().__init__(parent)
        self._backend = backend
        self.setObjectName("ai-changes-panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._records = {}
        self._busy = False
        self._closed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        header = QHBoxLayout()
        self._status = QLabel("No AI changes yet", self)
        header.addWidget(self._status, 1)
        self._review = ActionButton("Review file", self)
        self._review.setToolTip("Open this file's pending Accept / Reject review")
        self._review.clicked.connect(self._review_selected)
        header.addWidget(self._review)
        self._delete = ActionButton("Delete card", self)
        self._delete.setToolTip("Remove this history card; retain pending reviews and recovery copies")
        self._delete.clicked.connect(lambda: self._remove(False))
        header.addWidget(self._delete)
        self._clear = ActionButton("Delete all", self)
        self._clear.setToolTip("Remove all history cards; retain pending reviews and recovery copies")
        self._clear.clicked.connect(lambda: self._remove(True))
        header.addWidget(self._clear)
        layout.addLayout(header)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter = splitter
        self._tree = QTreeWidget(splitter)
        self._tree.setHeaderLabels(["Prompt / file", "Edited", "Decision"])
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.setMinimumWidth(0)
        self._tree.setUniformRowHeights(True)
        self._tree.currentItemChanged.connect(self._select)
        self._tree.itemDoubleClicked.connect(lambda *_: self._review_selected())
        previews = QWidget(splitter)
        side = QHBoxLayout(previews)
        side.setContentsMargins(0, 0, 0, 0)
        self._before = self._preview("Before", side, previews)
        self._after = self._preview("AI edit", side, previews)
        splitter.addWidget(self._tree)
        splitter.addWidget(previews)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)
        signals.ai_changes_updated.connect(self.refresh)
        signals.ai_review_resolved.connect(self.refresh)
        signals.project_updated.connect(self.refresh)
        self._deleted.connect(self._finish_delete, Qt.ConnectionType.QueuedConnection)
        self.apply_theme()
        self.refresh()

    def _preview(self, title, layout, parent):
        panel = QWidget(parent)
        column = QVBoxLayout(panel)
        column.setContentsMargins(4, 0, 0, 0)
        column.addWidget(QLabel(title, panel))
        text = QPlainTextEdit(panel)
        text.setReadOnly(True)
        text.setMinimumSize(0, 0)
        text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        from main.core.config import get_monitor_font_size
        size = get_monitor_font_size()
        text.setFont(QFont("Consolas", size))
        column.addWidget(text, 1)
        layout.addWidget(panel, 1)
        return text

    def refresh(self, *_):
        manager = getattr(self._backend, "ai_review_manager", None)
        selected = self._selected_id()
        self._tree.blockSignals(True)
        self._tree.clear()
        self._records = {item["id"]: item for item in manager.get_ai_changes()} if manager else {}
        groups = {}
        target = None
        for record in reversed(list(self._records.values())):
            group_id = record.get("groupId", "")
            if group_id not in groups:
                group = QTreeWidgetItem([record.get("prompt", "Assistant changes"), "", ""])
                group.setToolTip(0, record.get("prompt", ""))
                self._tree.addTopLevelItem(group)
                group.setFirstColumnSpanned(True)
                group.setExpanded(True)
                groups[group_id] = group
            try:
                edited = datetime.fromisoformat(record["timestamp"]).astimezone().strftime("%b %d, %Y %H:%M:%S")
            except (ValueError, KeyError):
                edited = str(record.get("timestamp", ""))
            from pathlib import Path
            card = QTreeWidgetItem([Path(record["path"]).name, edited, record.get("status", "pending").capitalize()])
            card.setData(0, Qt.ItemDataRole.UserRole, record["id"])
            card.setToolTip(0, record["path"])
            groups[group_id].addChild(card)
            if record["id"] == selected or target is None:
                target = card
        self._tree.blockSignals(False)
        self._tree.setCurrentItem(target)
        self._select(target)
        count = len(self._records)
        self._status.setText(f"{count} change{'s' if count != 1 else ''}" if count else "No changes yet")
        self._clear.setEnabled(bool(self._records) and not self._busy)

    def _selected_id(self):
        item = self._tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else ""

    def _select(self, item=None, *_):
        record = self._records.get(item.data(0, Qt.ItemDataRole.UserRole), {}) if item else {}
        before, after = str(record.get("beforeContent", "")), str(record.get("content", ""))
        self._before.setPlainText(before)
        self._after.setPlainText(after)
        self._delete.setEnabled(bool(record) and not self._busy)
        manager = getattr(self._backend, "ai_review_manager", None)
        self._review.setEnabled(bool(record and manager and manager.has_pending_ai_edit(record["path"])))
        old_rows, new_rows = [], []
        old_lines, new_lines = before.splitlines(), after.splitlines()
        if len(old_lines) + len(new_lines) <= 2000:
            opcodes = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
        else:
            opcodes = [("replace", 0, len(old_lines), 0, len(new_lines))]
        for kind, i1, i2, j1, j2 in opcodes:
            if kind != "equal":
                old_rows.extend(range(i1, i2))
                new_rows.extend(range(j1, j2))
        self._highlight(self._before, old_rows, self._removed)
        self._highlight(self._after, new_rows, self._added)
        tip = "Preview truncated; use Review file for the complete edit." if record.get("previewTruncated") else ""
        self._before.setToolTip(tip)
        self._after.setToolTip(tip)

    @staticmethod
    def _highlight(view, rows, color):
        selections = []
        for row in rows[:4000]:
            block = view.document().findBlockByNumber(row)
            if not block.isValid():
                continue
            selected = QTextEdit.ExtraSelection()
            selected.cursor = QTextCursor(block)
            selected.format.setBackground(QColor(color))
            selected.format.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
            selections.append(selected)
        view.setExtraSelections(selections)

    def _review_selected(self):
        record = self._records.get(self._selected_id())
        if record:
            signals.ai_review_requested.emit(record["path"])

    def _remove(self, all_cards):
        manager = getattr(self._backend, "ai_review_manager", None)
        change_id = "" if all_cards else self._selected_id()
        if self._busy or not manager or (not all_cards and not change_id):
            return
        if all_cards and QMessageBox.question(self, "Delete AI change history", "Delete all history cards? Pending file reviews and recovery copies are preserved.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self._busy = True
        self._delete.setEnabled(False)
        self._clear.setEnabled(False)
        def remove():
            try:
                result = manager.delete_ai_changes(change_id)
            except Exception as exc:
                result = {"success": False, "error": str(exc)}
            try:
                self._deleted.emit(result)
            except RuntimeError:
                pass
        try:
            threading.Thread(target=remove, daemon=True, name="MCU_AIHistoryDelete").start()
        except Exception as exc:
            self._finish_delete({"success": False, "error": str(exc)})

    def _finish_delete(self, result):
        self._busy = False
        self.refresh()
        if not result.get("success"):
            QMessageBox.warning(self, "History could not be deleted", result.get("error", "Write failed."))

    def apply_theme(self, mode=None):
        from main.core.theme import Theme
        from main.qt.theme import get_palette
        pal = get_palette(mode or Theme.active_theme)
        self.setStyleSheet(f"#ai-changes-panel {{ background: {pal['BG_DARK']}; color: {pal['TEXT']}; }}")
        self._added = pal.get("GREEN", "#2e9d61")
        self._removed = pal.get("RED", "#c74b4b")
        # Muted fills retain the theme's readable text foreground.
        for name in ("_added", "_removed"):
            color = QColor(getattr(self, name))
            color.setAlpha(45)
            setattr(self, name, color)
        self._select(self._tree.currentItem())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = event.size().width() < 650
        for button, full, short in ((self._review, "Review file", "Review"),
                                    (self._delete, "Delete card", "Delete"),
                                    (self._clear, "Delete all", "Clear")):
            button.setText(short if compact else full)
        self._splitter.setOrientation(Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal)

    def set_font_size(self, size):
        for preview in (self._before, self._after):
            font = preview.font()
            font.setPointSize(int(size))
            preview.setFont(font)
