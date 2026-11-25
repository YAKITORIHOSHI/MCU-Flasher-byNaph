"""Bounded per-project AI history, with prompt groups and two source previews."""
from __future__ import annotations

import difflib
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor, QFont, QPainter
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTreeWidget,
    QTreeWidgetItem, QSplitter, QPlainTextEdit, QMessageBox, QTextEdit, QHeaderView,
    QSizePolicy)

from main.qt.icons import ActionButton
from main.qt.signals import signals


class _LineNumberArea(QWidget):
    def __init__(self, preview):
        super().__init__(preview)
        self._preview = preview
        self.setAccessibleName("Source line numbers")

    def paintEvent(self, event):
        self._preview.paint_line_numbers(event)


class CodePreview(QPlainTextEdit):
    """Read-only source preview with a gutter tied to document block geometry."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._number_foreground = QColor("#adc0d3")
        self._number_border = QColor("#425e79")
        self._number_area = _LineNumberArea(self)
        self.blockCountChanged.connect(self._update_number_width)
        self.updateRequest.connect(self._update_number_area)
        self._update_number_width()

    def _number_width(self):
        digits = len(str(max(1, self.blockCount())))
        return 14 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_number_width(self, *_):
        width = self._number_width()
        self.setViewportMargins(width, 0, 0, 0)
        rect = self.contentsRect()
        self._number_area.setGeometry(rect.left(), rect.top(), width, rect.height())
        self._number_area.update()

    def _update_number_area(self, rect, dy):
        if dy:
            self._number_area.scroll(0, dy)
        else:
            self._number_area.update(0, rect.y(), self._number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_number_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_number_width()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "_number_area"):
            self._update_number_width()

    def _number_rows(self):
        block = self.firstVisibleBlock()
        while block.isValid():
            top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
            bottom = top + self.blockBoundingRect(block).height()
            if top > self.viewport().height():
                break
            if block.isVisible() and bottom >= 0:
                yield block.blockNumber() + 1, top, bottom
            block = block.next()

    def paint_line_numbers(self, event):
        painter = QPainter(self._number_area)
        painter.setPen(self._number_border)
        painter.drawLine(self._number_area.width() - 1, event.rect().top(),
                         self._number_area.width() - 1, event.rect().bottom())
        painter.setFont(self.font())
        painter.setPen(self._number_foreground)
        height = self.fontMetrics().height()
        for number, top, _ in self._number_rows():
            painter.drawText(QRectF(0, top, self._number_area.width() - 7, height),
                             Qt.AlignmentFlag.AlignRight, str(number))

    def apply_theme(self, palette):
        self._number_foreground = QColor(palette["TEXT_DIM"])
        rim = QColor(palette["TEXT"])
        rim.setAlpha(46)
        self._number_border = rim
        self._number_area.update()


_EDIT, _SESSION = Qt.ItemDataRole.UserRole, Qt.ItemDataRole.UserRole + 1


class AIChangesPanel(QWidget):
    _deleted = Signal(object)
    _sessions_read = Signal(int, object)
    _edit_read = Signal(str, object)

    def __init__(self, backend=None, parent=None):
        super().__init__(parent)
        self._backend = backend
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.setObjectName("ai-changes-panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._records = {}
        self._records_signature = None
        self._preview_key = None
        self._stale = False
        self._busy = False
        self._closed = False
        self._archive = None            # Saved sessions, once "Earlier sessions" was opened
        self._archive_edits = {}        # Their full texts, the last few read
        self._archive_token = 0
        # History changes arrive in bursts while an assistant edits several
        # files. Coalesce them, and refresh a hidden panel only when it shows.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(120)
        self._refresh_timer.timeout.connect(self.refresh)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        header = QHBoxLayout()
        self._status = QLabel("No AI changes yet", self)
        self._status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header.addWidget(self._status, 1)
        self._review = ActionButton("Review file", self)
        self._review.setToolTip("Open this file's pending Accept / Reject review")
        self._review.clicked.connect(self._review_selected)
        header.addWidget(self._review)
        self._revert = ActionButton("Revert edit", self, icon_name="undo")
        self._revert.setToolTip("Preview and undo this AI edit, or every edit of the selected session")
        self._revert.clicked.connect(self._revert_selected)
        header.addWidget(self._revert)
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
        # A tall, stacked history must be allowed to receive a shorter resize
        # before its child reading minima switch back to a single row. Native
        # Windows otherwise pins the outer window to the previous stack height.
        splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self._splitter = splitter
        self._tree = QTreeWidget(splitter)
        self._tree.setHeaderLabels(["Prompt / file", "Decision"])
        self._tree.headerItem().setToolTip(0, "Edited filename and local edit time; hover for the full path")
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.setMinimumWidth(0)
        self._tree.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        # Keep file identity visible even when the history shares a narrow pane
        # with previews. A separate timestamp column can consume the whole pane.
        self._tree.setUniformRowHeights(False)
        self._tree_indent = self._tree.indentation()
        self._tree.currentItemChanged.connect(self._select)
        self._tree.itemExpanded.connect(self._open_archive)
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
        signals.ai_changes_updated.connect(self._request_refresh)
        signals.ai_review_resolved.connect(self._request_refresh)
        signals.project_updated.connect(self._request_refresh)
        self._deleted.connect(self._finish_delete, Qt.ConnectionType.QueuedConnection)
        self._sessions_read.connect(self._show_archive, Qt.ConnectionType.QueuedConnection)
        self._edit_read.connect(self._show_archived_edit, Qt.ConnectionType.QueuedConnection)
        self.apply_theme()
        self.refresh()

    def _preview(self, title, layout, parent):
        panel = QWidget(parent)
        column = QVBoxLayout(panel)
        column.setContentsMargins(4, 0, 0, 0)
        column.addWidget(QLabel(title, panel))
        text = CodePreview(panel)
        text.setAccessibleName(f"{title} code preview")
        from main.core.config import get_monitor_font_size
        size = get_monitor_font_size()
        text.setFont(QFont("Consolas", size))
        text.setStyleSheet(f"QPlainTextEdit {{ font-family: 'Consolas'; font-size: {size}pt; }}")
        column.addWidget(text, 1)
        layout.addWidget(panel, 1)
        return text

    @staticmethod
    def _prompt_title(record):
        # Earlier trackers tagged both channels as CLI; titles cannot recover
        # that provenance. New records explicitly version their channel tag.
        source = (record.get("promptSource") if type(record.get("promptSourceVersion")) is int
                  and record["promptSourceVersion"] == 1 else "external")
        return {"cli": "CLI", "assistant": "Assistant", "mixed": "Mixed sources"}.get(source, "Unknown source")

    def _request_refresh(self, *_):
        if not self.isVisible():
            self._stale = True
            return
        self._refresh_timer.start()

    def refresh_if_stale(self):
        if self._stale:
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        if self._stale:
            self._refresh_timer.start(0)

    def refresh(self, *_):
        self._refresh_timer.stop()
        self._stale = False
        manager = getattr(self._backend, "ai_review_manager", None)
        records = manager.get_ai_changes() if manager else []
        project = str(getattr(manager, "project_dir", "") or "")
        if project != getattr(self, "_project", None):
            # Saved sessions belong to one sketch; read another sketch's on demand.
            self._project, self._archive, self._archive_edits = project, None, {}
            self._archive_token += 1
        signature = tuple((item.get("id"), item.get("updatedAt"), item.get("status"), item.get("statusNote"),
                           item.get("groupId"), item.get("prompt")) for item in records)
        signature = (project, signature)
        if signature == self._records_signature and self._tree.topLevelItemCount() == len(
                {item.get("groupId", "") for item in records}) + bool(self._archive_item()):
            # Same cards: keep the tree, its scroll position and selection.
            self._records = {item["id"]: item for item in records}
            self._select(self._tree.currentItem())
            return
        self._records_signature = signature
        selected = self._selected_id()
        archive_open = bool(self._archive_item() and self._archive_item().isExpanded())
        self._tree.blockSignals(True)
        self._tree.clear()
        self._records = {item["id"]: item for item in records}
        groups = {}
        target = None
        for record in reversed(list(self._records.values())):
            group_id = record.get("groupId", "")
            if group_id not in groups:
                group = QTreeWidgetItem([self._prompt_title(record), ""])
                group.setToolTip(0, f"Source: {self._prompt_title(record)}\n{record.get('prompt', '')}")
                self._tree.addTopLevelItem(group)
                group.setFirstColumnSpanned(True)
                group.setExpanded(True)
                groups[group_id] = group
            try:
                edited = datetime.fromisoformat(record["timestamp"]).astimezone().strftime("%b %d, %Y %H:%M:%S")
            except (ValueError, KeyError):
                edited = str(record.get("timestamp", ""))
            decision = record.get("status", "pending").capitalize()
            card = QTreeWidgetItem([f"{Path(record['path']).name}\n{edited}", decision])
            card.setData(0, _EDIT, record["id"])
            tooltip = f"{record['path']}\nEdited: {edited}\nDecision: {decision}\nSource: {self._prompt_title(record)}"
            if record.get("statusNote"):
                tooltip += f"\n{record['statusNote']}"
            card.setToolTip(0, tooltip)
            card.setToolTip(1, tooltip)
            groups[group_id].addChild(card)
            groups[group_id].setData(0, _SESSION, (groups[group_id].data(0, _SESSION) or []) + [record["id"]])
            if record["id"] == selected or target is None:
                target = card
        if callable(getattr(manager, "list_ai_sessions", None)):
            self._add_archive_item(archive_open)
        self._tree.blockSignals(False)
        self._tree.setCurrentItem(target)
        self._select(target)
        count = len(self._records)
        self._status.setText(f"{count} change{'s' if count != 1 else ''}" if count else "No changes yet")
        self._status.setToolTip(self._status.text())
        self._clear.setEnabled(bool(self._records) and not self._busy)

    def _selected_id(self):
        item = self._tree.currentItem()
        return item.data(0, _EDIT) or "" if item else ""

    # ── Saved sessions ─────────────────────────────────────────────────────

    def _archive_item(self):
        last = self._tree.topLevelItem(self._tree.topLevelItemCount() - 1)
        return last if last is not None and last.data(0, _SESSION) == "archive" else None

    def _add_archive_item(self, expanded=False):
        item = QTreeWidgetItem(["Earlier sessions", ""])
        item.setToolTip(0, "AI edits kept by earlier sessions of this sketch, stored hidden inside it")
        item.setData(0, _SESSION, "archive")
        item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        self._tree.addTopLevelItem(item)
        item.setFirstColumnSpanned(True)
        if self._archive is not None:
            self._fill_archive(item)
            item.setExpanded(expanded)

    def _open_archive(self, item):
        if item is not self._archive_item() or self._archive is not None:
            return
        manager = getattr(self._backend, "ai_review_manager", None)
        self._archive_token += 1
        token = self._archive_token
        item.takeChildren()
        item.addChild(QTreeWidgetItem(["Reading saved sessions…", ""]))

        def read():
            try:
                sessions = manager.list_ai_sessions()
            except Exception as exc:
                sessions = exc
            try:
                self._sessions_read.emit(token, sessions)
            except RuntimeError:
                pass
        threading.Thread(target=read, daemon=True, name="MCU_AISessions").start()

    def _show_archive(self, token, sessions):
        item = self._archive_item()
        if token != self._archive_token or item is None:
            return
        if isinstance(sessions, Exception):
            item.takeChildren()
            item.addChild(QTreeWidgetItem([f"Saved sessions could not be read: {sessions}", ""]))
            return
        self._archive = sessions
        self._fill_archive(item)
        item.setExpanded(True)

    def _fill_archive(self, item):
        item.takeChildren()
        if not self._archive:
            item.addChild(QTreeWidgetItem(["No earlier AI sessions were kept for this sketch", ""]))
        for session in self._archive or ():
            year, month, day = session["date"]
            try:
                date = datetime(year, month, day).strftime("%b %d, %Y")
            except ValueError:
                date = session["id"].split("/")[0]
            count = len(session["edits"])
            node = QTreeWidgetItem([f"{date} · Session {session['session']} ({count} edit{'s' if count != 1 else ''})", ""])
            node.setData(0, _SESSION, [edit["id"] for edit in session["edits"] if edit.get("available")])
            item.addChild(node)
            node.setFirstColumnSpanned(True)
            for edit in session["edits"]:
                try:
                    edited = datetime.fromisoformat(edit["timestamp"]).astimezone().strftime("%b %d, %Y %H:%M:%S")
                except (ValueError, KeyError):
                    edited = str(edit.get("timestamp", ""))
                decision = str(edit.get("status") or "pending").replace("-applied", "").capitalize()
                child = QTreeWidgetItem([f"{Path(edit['path']).name}\n{edited}", decision])
                child.setData(0, _EDIT, edit["id"])
                tooltip = f"{edit['path']}\nEdited: {edited}\nDecision: {decision}"
                if not edit.get("available"):
                    tooltip += "\nThis file is not a source of this sketch."
                for column in (0, 1):
                    child.setToolTip(column, tooltip)
                node.addChild(child)

    def _read_archived_edit(self, edit_id):
        manager = getattr(self._backend, "ai_review_manager", None)

        def read():
            try:
                record = manager.get_ai_edit(edit_id)
            except Exception as exc:
                record = {"error": str(exc)}
            try:
                self._edit_read.emit(edit_id, record)
            except RuntimeError:
                pass
        threading.Thread(target=read, daemon=True, name="MCU_AISessionEdit").start()

    def _show_archived_edit(self, edit_id, record):
        while len(self._archive_edits) >= 8:
            self._archive_edits.pop(next(iter(self._archive_edits)))
        self._archive_edits[edit_id] = record
        if self._selected_id() == edit_id:
            self._select(self._tree.currentItem())

    def _revert_targets(self, item=None):
        """(ids newest first, title) for the selected edit or session."""
        item = item or self._tree.currentItem()
        if item is None:
            return [], ""
        edit_id = item.data(0, _EDIT)
        if edit_id:
            name = item.text(0).split("\n")[0]
            return [edit_id], f"Revert this AI edit in {name}"
        ids = item.data(0, _SESSION)
        if not isinstance(ids, list) or not ids:
            return [], ""
        return ids, f"Revert {len(ids)} AI edit{'s' if len(ids) != 1 else ''} from {item.text(0)}"

    def _revert_selected(self):
        ids, title = self._revert_targets()
        if not ids or not callable(getattr(self._backend, "revert_ai_edits", None)):
            return
        from main.qt.ai_revert_dialog import AIRevertDialog
        dialog = AIRevertDialog(self._backend, ids, title, self.window())
        self._revert_dialog = dialog
        dialog.destroyed.connect(lambda *_: setattr(self, "_revert_dialog", None))
        dialog.finished.connect(self._request_refresh)
        dialog.open()

    def _select(self, item=None, *_):
        edit_id = str(item.data(0, _EDIT) or "") if item else ""
        record = self._records.get(edit_id, {})
        if edit_id.startswith("archive:"):
            record = self._archive_edits.get(edit_id)
            if record is None:
                self._read_archived_edit(edit_id)
                record = {}
            elif record.get("error"):
                self._status.setText(record["error"])
        ids, _title = self._revert_targets(item) if item else ([], "")
        self._revert.setEnabled(bool(ids) and not self._busy and callable(getattr(self._backend, "revert_ai_edits", None)))
        compact = getattr(self, "_compact", False)
        session = item is not None and not edit_id and bool(ids)
        self._revert.setText("Revert" if compact else "Revert session" if session else "Revert edit")
        before, after = str(record.get("beforeContent", "")), str(record.get("content", ""))
        self._delete.setEnabled(edit_id in self._records and not self._busy)
        manager = getattr(self._backend, "ai_review_manager", None)
        self._review.setEnabled(bool(edit_id in self._records and manager and manager.has_pending_ai_edit(record["path"])))
        added, removed = getattr(self, "_added", None), getattr(self, "_removed", None)
        key = (record.get("id"), record.get("updatedAt"), len(before), len(after), hash(before), hash(after),
               added.name(QColor.NameFormat.HexArgb) if added is not None else "",
               removed.name(QColor.NameFormat.HexArgb) if removed is not None else "")
        if key == self._preview_key:
            return
        self._preview_key = key
        if self._before.toPlainText() != before:
            self._before.setPlainText(before)
        if self._after.toPlainText() != after:
            self._after.setPlainText(after)
        old_ranges, new_ranges = self._changed_ranges(before, after)
        self._highlight(self._before, old_ranges, removed)
        self._highlight(self._after, new_ranges, added)
        tip = "Preview truncated; use Review file for the complete edit." if record.get("previewTruncated") else ""
        self._before.setToolTip(tip)
        self._after.setToolTip(tip)

    @staticmethod
    def _changed_ranges(before, after):
        """Changed line ranges [(start, end)) on each side.

        The common head and tail are trimmed first, so an appended line in a
        3,000-line file marks one line instead of the whole file, and the
        matcher only ever sees the bounded changed middle.
        """
        old_lines, new_lines = before.splitlines(), after.splitlines()
        prefix, limit = 0, min(len(old_lines), len(new_lines))
        while prefix < limit and old_lines[prefix] == new_lines[prefix]:
            prefix += 1
        old_end, new_end = len(old_lines), len(new_lines)
        while old_end > prefix and new_end > prefix and old_lines[old_end - 1] == new_lines[new_end - 1]:
            old_end -= 1
            new_end -= 1
        middle_old, middle_new = old_lines[prefix:old_end], new_lines[prefix:new_end]
        if len(middle_old) + len(middle_new) <= 2000:
            opcodes = difflib.SequenceMatcher(None, middle_old, middle_new, autojunk=False).get_opcodes()
        else:
            opcodes = [("replace", 0, len(middle_old), 0, len(middle_new))]
        old_ranges, new_ranges = [], []
        for kind, i1, i2, j1, j2 in opcodes:
            if kind == "equal":
                continue
            if i2 > i1:
                old_ranges.append((prefix + i1, prefix + i2))
            if j2 > j1:
                new_ranges.append((prefix + j1, prefix + j2))
        return old_ranges, new_ranges

    @staticmethod
    def _highlight(view, ranges, color):
        """One full-width selection per changed range, not one per line."""
        selections = []
        if color is None:
            view.setExtraSelections(selections)
            return
        document = view.document()
        final_position = max(0, document.characterCount() - 1)

        def add(cursor):
            selected = QTextEdit.ExtraSelection()
            selected.cursor = cursor
            selected.format.setBackground(QColor(color))
            selected.format.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
            selections.append(selected)

        for start, end in ranges[:500]:
            first = document.findBlockByNumber(start)
            if not first.isValid():
                continue
            last = document.findBlockByNumber(end - 1)
            if not last.isValid():
                last = document.lastBlock()
            cursor = QTextCursor(first)
            if last.blockNumber() > first.blockNumber():
                # Through the last line's newline, so Qt paints that line full width too.
                cursor.setPosition(min(last.position() + last.length(), final_position),
                                   QTextCursor.MoveMode.KeepAnchor)
                add(cursor)
                if last == document.lastBlock():
                    add(QTextCursor(last))  # the final line has no newline to select
            else:
                add(cursor)  # a collapsed cursor marks its whole line
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
        self.setStyleSheet(f"#ai-changes-panel {{ background: transparent; color: {pal['TEXT']}; }}")
        for preview in (self._before, self._after):
            preview.apply_theme(pal)
        self._added = pal.get("GREEN", "#2e9d61")
        self._removed = pal.get("RED", "#c74b4b")
        # Muted fills retain the theme's readable text foreground.
        for name in ("_added", "_removed"):
            color = QColor(getattr(self, name))
            color.setAlpha(45)
            setattr(self, name, color)
        self._select(self._tree.currentItem())
        self._adapt_layout(self.width(), self.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._adapt_layout(event.size().width(), event.size().height())

    def _adapt_layout(self, width, height):
        compact = width < 650
        self._compact = compact
        metrics = self._tree.fontMetrics()
        self._tree.setMinimumWidth(0 if compact else (
            metrics.horizontalAdvance("Sep 30, 2000 23:59:59") + metrics.horizontalAdvance("Accepted")
            + self._tree.indentation() * 2 + 32))
        row_minimum = self._tree.header().sizeHint().height() + metrics.height() * 2 + self._tree.frameWidth() * 2
        stacked = compact and height >= 240
        short_history = compact and not stacked
        self._tree.setIndentation(12 if short_history else self._tree_indent)
        self._tree.setMinimumHeight(min(
            self._tree.header().height() + metrics.height() * 7 + self._tree.frameWidth() * 2,
            max(row_minimum, height // 3)) if stacked else row_minimum)
        for button, full, short in ((self._review, "Review file", "Review"),
                                    (self._delete, "Delete card", "Delete"),
                                    (self._clear, "Delete all", "Clear")):
            button.setText(short if compact else full)
        session = bool(self._tree.currentItem()) and not self._selected_id()
        self._revert.setText("Revert" if compact else "Revert session" if session else "Revert edit")
        self._splitter.setOrientation(Qt.Orientation.Vertical if stacked else Qt.Orientation.Horizontal)
        if getattr(self, "_short_history", None) != short_history:
            self._short_history = short_history
            self._splitter.setStretchFactor(0, 1)
            self._splitter.setStretchFactor(1, 1 if short_history else 3)
            if short_history:
                self._splitter.setSizes([width // 2, width // 2])
        for preview in (self._before, self._after):
            preview.setMinimumHeight(preview.fontMetrics().lineSpacing()
                + round(preview.document().documentMargin() * 2)
                + preview.frameWidth() * 2
                + preview.horizontalScrollBar().sizeHint().height())

    def set_font_size(self, size):
        for preview in (self._before, self._after):
            font = preview.font()
            font.setPointSize(int(size))
            preview.setFont(font)
            preview.setStyleSheet(f"QPlainTextEdit {{ font-family: 'Consolas'; font-size: {int(size)}pt; }}")
        self._adapt_layout(self.width(), self.height())
