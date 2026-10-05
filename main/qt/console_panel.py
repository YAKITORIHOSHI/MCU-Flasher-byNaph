#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.console_panel — Build Console panel for MCU Flasher by Naph.

Presents bounded build events as compact Activity or retained Details.
Semantic colors, autoscroll, full retained-log copy and clear are built in.
"""
from __future__ import annotations

import re
import time
from itertools import islice

# pyrefly: ignore [missing-import]
from PySide6.QtCore import QTimer, Signal, Slot, Qt
# pyrefly: ignore [missing-import]
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor, QFont, QTextBlockUserData
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QPushButton, QCheckBox, QLabel, QFrame, QSizePolicy, QMenu,
)
from main.qt.icons import ActionButton as QPushButton
from main.qt.log_follow import LogFollow, preserve_log_view


class ConsolePanelHeader(QWidget):
    """Header bar for the Build Console tab."""

    def __init__(self, console: "ConsolePanel", parent: QWidget | None = None, backend=None):
        super().__init__(parent)
        self._console = console
        self._backend = backend
        self.setObjectName("console-header")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(36)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(8)
        self._header_layout = layout

        # Title
        title = QLabel("Build activity", self)
        title.setProperty("role", "dim")
        self._title_lbl = title
        layout.addWidget(title)
        self._btn_details = QPushButton("Details", self)
        self._btn_details.setCheckable(True)
        self._btn_details.setFixedHeight(26)
        self._btn_details.setToolTip("Show retained build messages, including individual compilation units")
        self._btn_details.toggled.connect(console.set_details_visible)
        console.details_changed.connect(self._sync_details)
        layout.addWidget(self._btn_details)
        layout.addStretch()

        from main.core.config import load_gui_config, save_gui_config
        cfg = load_gui_config()
        init_ts = bool(cfg.get("timestamp_enabled", False))
        self._console.set_timestamp_enabled(init_ts)

        # Auto-clear on action checkbox
        init_clear = bool(cfg.get("clear_console_on_action", True))
        self.cb_auto_clear = QCheckBox("Clear on Action", self)
        self.cb_auto_clear.setChecked(init_clear)
        self.cb_auto_clear.setToolTip("Clear build console before each compile/upload/clean/reset action")
        self.cb_auto_clear.stateChanged.connect(self._on_auto_clear_changed)
        layout.addWidget(self.cb_auto_clear)
        if self._backend is not None:
            self._backend.clear_console_on_action = init_clear

        # Auto-clear serial on action checkbox
        init_clear_serial = bool(cfg.get("clear_serial_on_action", False))
        self.cb_auto_clear_serial = QCheckBox("Clear Serial on Action", self)
        self.cb_auto_clear_serial.setChecked(init_clear_serial)
        self.cb_auto_clear_serial.setToolTip("Clear serial monitor before each compile/upload/reset action")
        self.cb_auto_clear_serial.stateChanged.connect(self._on_auto_clear_serial_changed)
        layout.addWidget(self.cb_auto_clear_serial)
        if self._backend is not None:
            self._backend.clear_serial_on_action = init_clear_serial

        # Autoscroll
        self.cb_autoscroll = QCheckBox("Auto-scroll", self)
        self.cb_autoscroll.setChecked(True)
        self.cb_autoscroll.setToolTip("Auto-scroll console output to bottom")
        layout.addWidget(self.cb_autoscroll)

        self._btn_options = QPushButton("Options", self)
        self._btn_options.setFixedHeight(26)
        self._btn_options.setToolTip("Build and serial clear-on-action preferences")
        menu = QMenu(self._btn_options)
        self._clear_actions = []
        for checkbox, label in ((self.cb_auto_clear, "Clear build console on action"),
                                (self.cb_auto_clear_serial, "Clear serial monitor on action")):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(checkbox.isChecked())
            action.toggled.connect(checkbox.setChecked)
            checkbox.toggled.connect(action.setChecked)
            self._clear_actions.append(action)
        self._btn_options.setMenu(menu)
        layout.addWidget(self._btn_options)
        self._btn_options.hide()

        # Copy button
        btn_copy = QPushButton("⧉ Copy", self)
        btn_copy.setObjectName("btn-copy-console")
        btn_copy.setFixedHeight(26)
        btn_copy.setToolTip("Copy all retained build messages, including Details and hidden warnings")
        btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_copy.clicked.connect(self._copy_console)
        self._btn_copy = btn_copy
        layout.addWidget(btn_copy)

        # Clear button
        btn_clear = QPushButton("🗑 Clear", self)
        btn_clear.setObjectName("btn-clear-console")
        btn_clear.setFixedHeight(26)
        btn_clear.setToolTip("Clear build console output buffer")
        btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear.clicked.connect(console.clear)
        self._btn_clear = btn_clear
        layout.addWidget(btn_clear)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())

    def set_responsive_width(self, width: int) -> None:
        """Dynamically adapt header checkboxes, labels, and buttons based on width."""
        self._current_responsive_width = width
        compact_options = width < 850
        self.cb_auto_clear.setVisible(not compact_options)
        self.cb_auto_clear_serial.setVisible(not compact_options)
        self._btn_options.setVisible(compact_options)
        self._title_lbl.setVisible(width >= 400)
        self._btn_details.setText("Details")
        if width >= 1100:
            self._is_ultra_compact = False
            self._title_lbl.setText("Build details" if self._console._details_visible else "Build activity")
            self.cb_auto_clear.setText("Clear on Action")
            self.cb_auto_clear_serial.setText("Clear Serial on Action")
            self.cb_autoscroll.setText("Auto-scroll")
            self._btn_copy.setText("⧉ Copy")
            self._btn_clear.setText("🗑 Clear")
            if hasattr(self, "_header_layout"):
                self._header_layout.setSpacing(8)
                self._header_layout.setContentsMargins(10, 4, 10, 4)
        elif width >= 850:
            self._is_ultra_compact = False
            self._title_lbl.setText("Build details" if self._console._details_visible else "Build activity")
            self.cb_auto_clear.setText("Clr Action")
            self.cb_auto_clear_serial.setText("Clr Serial")
            self.cb_autoscroll.setText("Auto")
            self._btn_copy.setText("⧉ Copy")
            self._btn_clear.setText("🗑 Clear")
            if hasattr(self, "_header_layout"):
                self._header_layout.setSpacing(5)
                self._header_layout.setContentsMargins(6, 4, 6, 4)
        else:
            self._is_ultra_compact = True
            self._title_lbl.setText("Build")
            self.cb_auto_clear.setText("Clr Act")
            self.cb_auto_clear_serial.setText("Clr Ser")
            self.cb_autoscroll.setText("Auto")
            self._btn_copy.setText("⧉")
            self._btn_clear.setText("🗑")
            if hasattr(self, "_header_layout"):
                self._header_layout.setSpacing(4)
                self._header_layout.setContentsMargins(4, 4, 4, 4)

    def _sync_details(self, checked: bool) -> None:
        self._btn_details.setChecked(checked)
        self.set_responsive_width(self.width())

    def _on_timestamp_changed(self, state: int) -> None:
        is_checked = bool(state)
        self._console.set_timestamp_enabled(is_checked)
        from main.core.config import load_gui_config, save_gui_config
        cfg = load_gui_config()
        cfg["timestamp_enabled"] = is_checked
        save_gui_config(cfg)
        if self._backend is not None:
            self._backend.timestamp_enabled = is_checked
        from main.qt.signals import signals
        if hasattr(signals, "timestamp_toggled"):
            signals.timestamp_toggled.emit(is_checked)

    def sync_timestamp(self, checked: bool) -> None:
        if hasattr(self, "cb_timestamp") and self.cb_timestamp.isChecked() != checked:
            self.cb_timestamp.blockSignals(True)
            self.cb_timestamp.setChecked(checked)
            self.cb_timestamp.blockSignals(False)
        self._console.set_timestamp_enabled(checked)

    def _on_auto_clear_changed(self, state: int) -> None:
        is_checked = self.cb_auto_clear.isChecked()
        from main.core.config import load_gui_config, save_gui_config
        cfg = load_gui_config()
        cfg["clear_console_on_action"] = is_checked
        save_gui_config(cfg)
        if self._backend is not None:
            self._backend.clear_console_on_action = is_checked
        from main.qt.signals import signals
        if hasattr(signals, "clear_on_action_changed"):
            signals.clear_on_action_changed.emit(is_checked)

    def _on_auto_clear_serial_changed(self, state: int) -> None:
        is_checked = self.cb_auto_clear_serial.isChecked()
        from main.core.config import load_gui_config, save_gui_config
        cfg = load_gui_config()
        cfg["clear_serial_on_action"] = is_checked
        save_gui_config(cfg)
        if self._backend is not None:
            self._backend.clear_serial_on_action = is_checked
        from main.qt.signals import signals
        if hasattr(signals, "clear_serial_on_action_changed"):
            signals.clear_serial_on_action_changed.emit(is_checked)

    def sync_clear_on_action(self, checked: bool) -> None:
        if self.cb_auto_clear.isChecked() != checked:
            self.cb_auto_clear.blockSignals(True)
            self.cb_auto_clear.setChecked(checked)
            self.cb_auto_clear.blockSignals(False)
        self._clear_actions[0].setChecked(checked)

    def sync_clear_serial_on_action(self, checked: bool) -> None:
        if self.cb_auto_clear_serial.isChecked() != checked:
            self.cb_auto_clear_serial.blockSignals(True)
            self.cb_auto_clear_serial.setChecked(checked)
            self.cb_auto_clear_serial.blockSignals(False)
        self._clear_actions[1].setChecked(checked)

    def _copy_console(self) -> None:
        include_ts = self._console._timestamp_enabled if hasattr(self._console, "_timestamp_enabled") else False
        text = self._console.get_content_for_clipboard(include_timestamp=include_ts)
        # pyrefly: ignore [missing-import]
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        self._btn_copy.setText("✔" if getattr(self, "_is_ultra_compact", False) else "✔ Copied!")
        def _restore_btn_copy():
            self._btn_copy.setText("⧉" if getattr(self, "_is_ultra_compact", False) else "⧉ Copy")
        QTimer.singleShot(1500, _restore_btn_copy)


def _insert_with_bar_styling(cursor: QTextCursor, text: str, default_fmt: QTextCharFormat) -> None:
    if "▰" not in text and "▱" not in text:
        cursor.insertText(text, default_fmt)
        return
    bar_fmt = QTextCharFormat(default_fmt)
    bar_fmt.setFontFamilies(["Segoe UI Symbol", "Segoe UI Variable Static Display", "Consolas", "monospace"])
    bar_fmt.setFontWeight(QFont.Weight.Bold)
    for part in re.split(r"([▰▱]+)", text):
        if not part:
            continue
        if part[0] in ("▰", "▱"):
            cursor.insertText(part, bar_fmt)
        else:
            cursor.insertText(part, default_fmt)


class ConsolePanel(QPlainTextEdit):
    """Bounded build event history with compact Activity and retained Details."""

    details_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.setMinimumHeight(24)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setObjectName("build-console")
        from src.modules.runtime_resources import performance_profile
        from main.qt.log_buffer import LogBuffer, DiagnosticLogBuffer
        from main.core.build_output import BuildOutputPresenter
        from main.core.console_text import ConsoleTextCleaner
        from main.core.config import get_monitor_font_size, load_gui_config, get_hide_build_console_warnings, get_theme_mode
        profile = performance_profile()
        self._history_limit = 512_000 if profile.constrained else 2_000_000
        self.setMaximumBlockCount(profile.terminal_scrollback)
        self._entries = LogBuffer(self._history_limit, profile.terminal_scrollback, lambda entry: entry["text"])
        self._activity_entries = LogBuffer(self._history_limit, profile.terminal_scrollback, lambda entry: entry["text"])
        self._queue = DiagnosticLogBuffer(256_000 if profile.constrained else 1_000_000, 2000, lambda item: item[0])
        self._presenter = BuildOutputPresenter()
        self._cleaner = ConsoleTextCleaner()
        self._details_visible = False
        self._entry_number = 0
        self._formats = {}
        self._patterns = {}
        self._progress_blocks = {}
        self._autoscroll = True
        self._follow = LogFollow(self)
        self._timestamp_enabled = bool(load_gui_config().get("timestamp_enabled", False))
        self._hide_warnings = bool(get_hide_build_console_warnings())
        self.set_font_size(get_monitor_font_size())
        self.apply_theme(get_theme_mode())
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(profile.terminal_interval_ms)
        self._flush_timer.timeout.connect(self._flush_queue)

    def set_hide_warnings(self, enabled: bool) -> None:
        if self._hide_warnings != bool(enabled):
            self._hide_warnings = bool(enabled)
            self._rebuild_document()

    def set_details_visible(self, enabled: bool) -> None:
        if self._details_visible != bool(enabled):
            self._details_visible = bool(enabled)
            self._rebuild_document()
            self.details_changed.emit(self._details_visible)

    def set_font_size(self, size: int) -> None:
        try:
            size = int(size)
        except (ValueError, TypeError):
            size = 11
        font = QFont("Consolas", size)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        self.setFont(font)
        # The workspace's universal px rule otherwise overrides saved content
        # fonts when Qt polishes the widget. Points are logical Qt units.
        self.setStyleSheet(f"QPlainTextEdit#build-console {{ font-family: Consolas; font-size: {size}pt; }}")
        self.document().setDefaultFont(font)
        self._formats.clear()
        if hasattr(self, "_tag_colors"):
            self._rebuild_document()

    def apply_theme(self, theme_name: str) -> None:
        from main.qt.theme import get_palette
        from main.qt.log_colors import themed_log_colors
        pal = get_palette(theme_name)
        self._tag_colors = themed_log_colors(theme_name)
        self._theme_name = theme_name
        self._formats.clear()
        palette = self.palette()
        palette.setColor(palette.ColorRole.Base, QColor(pal.get("BG_DARKEST", "#0a0e14")))
        palette.setColor(palette.ColorRole.Text, QColor(pal.get("TEXT", "#e0e6ed")))
        self.setPalette(palette)
        self._rebuild_document()

    def set_autoscroll(self, enabled: bool) -> None:
        self._autoscroll = enabled
        self._follow.set_enabled(enabled)

    def set_timestamp_enabled(self, enabled: bool) -> None:
        if self._timestamp_enabled != bool(enabled):
            self._timestamp_enabled = bool(enabled)
            self._rebuild_document()

    def get_content_for_clipboard(self, include_timestamp: bool | None = None) -> str:
        """Copy retained events regardless of Activity/Details or warning filter."""
        while self._queue:
            self._flush_queue()
        if include_timestamp is None:
            include_timestamp = self._timestamp_enabled
        parts = []
        if self._entries.dropped:
            parts.append(f"[Retained history: {self._entries.dropped} older entries expired]\n")
        for entry in self._entries:
            if entry["newline"] and parts:
                parts.append("\n")
            if include_timestamp and entry["ts"]:
                parts.append(entry["ts"] + " ")
            parts.append(entry["text"])
        return "".join(parts)

    def copy(self) -> None:
        cursor = self.textCursor()
        if cursor.hasSelection():
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(cursor.selectedText().replace("\u2029", "\n"))

    def _format(self, tag):
        tag = tag if tag in self._tag_colors else "normal"
        if tag not in self._formats:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(self._tag_colors.get(tag, self._tag_colors["normal"])))
            if tag in ("bold", "header", "severe_alert", "success_bold_lg", "magenta_bold_lg", "purple_header"):
                fmt.setFontWeight(QFont.Weight.Bold)
            if tag in ("header", "purple_header") and not self._details_visible:
                fmt.setFontPointSize(self.font().pointSizeF() + 1)
            self._formats[tag] = fmt
        return self._formats[tag]

    def _render_entry(self, cursor, entry, replace=False):
        if self._hide_warnings and entry["tag"] == "warning":
            return
        block = self._progress_blocks.get(entry["id"]) if replace else None
        if replace and block is None:
            # The small cache may expire before the bounded lookup horizon.
            # Match a verified progress identity, never diagnostic/source text.
            candidate = self.document().lastBlock()
            for _ in range(250):
                if not candidate.isValid():
                    break
                data = candidate.userData()
                if data is not None and data.entry_id == entry["id"]:
                    block = candidate
                    break
                candidate = candidate.previous()
        if block is not None and block.isValid() and block.userData() is not None and block.userData().entry_id == entry["id"]:
            cursor = QTextCursor(block)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.End)
            if entry["newline"] and self.document().characterCount() > 1:
                # Space phase headings, keeping compiler source/caret rows intact.
                section = not self._details_visible and entry["tag"] in ("header", "purple_header")
                extra = section and bool(cursor.block().text())
                cursor.insertText("\n\n" if extra else "\n", QTextCharFormat())
        if self._timestamp_enabled and entry["ts"]:
            cursor.insertText(entry["ts"] + " ", self._format("timestamp"))
        _insert_with_bar_styling(cursor, entry["text"], self._format(entry["tag"]))
        if entry.get("replace_key") or entry.get("replace_pattern"):
            if "\n" not in entry["text"] and entry["newline"]:
                data = _ProgressBlockData(entry["id"])
                cursor.block().setUserData(data)
                self._progress_blocks[entry["id"]] = cursor.block()
                if len(self._progress_blocks) > 128:
                    self._progress_blocks.pop(next(iter(self._progress_blocks)))

    @preserve_log_view(rebuild=True)
    def _rebuild_document(self) -> None:
        self._progress_blocks.clear()
        self._formats.clear()
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()
        entries = self._entries if self._details_visible else self._activity_entries
        for entry in entries:
            self._render_entry(cursor, entry)
        cursor.endEditBlock()
        from main.qt.log_buffer import trim_document
        trim_document(self, self._history_limit)
        # Qt may defer its scrollbar range after replacing the whole document.
        # Refresh it synchronously before LogFollow restores the reading anchor.
        layout = self.document().documentLayout()
        layout.documentSizeChanged.emit(layout.documentSize())

    def _store_entry(self, history, record):
        """Update one bounded progress record without recounting history."""
        from main.qt.log_buffer import DIAGNOSTIC_TAGS
        match = None
        key = record.get("replace_key")
        pattern = record.get("replace_pattern")
        if key:
            last = next(reversed(history), None)
            if last and last.get("replace_key") == key:
                match = last
        elif pattern and record["newline"] and record["tag"] not in DIAGNOSTIC_TAGS and "\n" not in record["text"]:
            if pattern not in self._patterns:
                try:
                    self._patterns[pattern] = re.compile(pattern, re.IGNORECASE)
                except (re.error, TypeError):
                    self._patterns[pattern] = None
                if len(self._patterns) > 128:
                    self._patterns.pop(next(iter(self._patterns)))
            compiled = self._patterns[pattern]
            if compiled is not None:
                for candidate in islice(reversed(history), 250):
                    if candidate["tag"] in DIAGNOSTIC_TAGS or not candidate["newline"] or not candidate.get("replace_pattern"):
                        break
                    if candidate.get("replace_pattern") == pattern and "\n" not in candidate["text"] and compiled.search(candidate["text"]):
                        match = candidate
                        break
        if match is not None:
            old_length = len(match["text"])
            entry_id = match["id"]
            match.update(record, id=entry_id)
            history.adjust_size(match, old_length)
            return match, True
        self._entry_number += 1
        record = dict(record, id=self._entry_number)
        history.append(record)
        return record, False

    @Slot(dict)
    def append_log(self, payload: dict) -> None:
        from main.qt.log_buffer import display_text
        original = display_text(payload.get("text", ""))
        text = self._cleaner.clean(original)
        if original and not text:
            return
        ts = payload.get("timestamp") or payload.get("ts") or time.strftime("[%H:%M:%S]")
        stamp = re.match(r"^(\[\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\])\s?([\s\S]*)$", text)
        if stamp:
            ts, text = stamp.groups()
        pattern = payload.get("replace_pattern")
        self._queue.append((text, payload.get("tag", "normal"), payload.get("newline", True), pattern if isinstance(pattern, str) else None, ts))
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    @preserve_log_view()
    def _flush_queue(self) -> None:
        from main.qt.log_buffer import coalesce_progress, trim_document
        items = coalesce_progress(self._queue.drain())
        notice = self._queue.take_notice()
        if notice:
            items.insert(0, (notice + "\n", "system", True, None, ""))
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        pending_render = []
        for text, tag, newline, pattern, ts in items:
            record = dict(text=text, tag=tag, newline=newline, replace_pattern=pattern, ts=ts)
            raw, raw_replaced = self._store_entry(self._entries, record)
            activity = self._presenter.present(record)
            rendered = None
            if activity is not None:
                activity, activity_replaced = self._store_entry(self._activity_entries, activity)
                if not self._details_visible:
                    rendered = activity, activity_replaced
            if self._details_visible:
                rendered = raw, raw_replaced
            if rendered:
                # Retain every unit in Details; paint the latest Activity row
                # once per batch instead of reshaping it for every source unit.
                if pending_render and pending_render[-1][0]["id"] == rendered[0]["id"]:
                    pending_render[-1] = rendered
                else:
                    pending_render.append(rendered)
        for rendered in pending_render:
            self._render_entry(cursor, *rendered)
        cursor.endEditBlock()
        trim_document(self, self._history_limit)
        if not self._queue:
            self._flush_timer.stop()

    @Slot(dict)
    def update_progress(self, payload: dict) -> None:
        pass  # Main window owns the operation status/progress indicator.

    @Slot()
    def clear(self) -> None:
        self._entries.clear()
        self._activity_entries.clear()
        self._queue.clear()
        self._presenter.reset()
        self._cleaner.reset()
        self._progress_blocks.clear()
        self._patterns.clear()
        self._entry_number = 0
        self._flush_timer.stop()
        super().clear()
        self._follow.reset()


class _ProgressBlockData(QTextBlockUserData):
    def __init__(self, entry_id):
        super().__init__()
        self.entry_id = entry_id


class ConsolePanelContainer(QWidget):
    """Container combining header + ConsolePanel into one tab widget."""

    def __init__(self, backend=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.console = ConsolePanel()
        self.header  = ConsolePanelHeader(self.console, parent=self, backend=backend)

        # Wire autoscroll checkbox
        self.header.cb_autoscroll.stateChanged.connect(
            lambda s: self.console.set_autoscroll(bool(s))
        )

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("separator")

        layout.addWidget(self.header)
        layout.addWidget(sep)
        layout.addWidget(self.console)

    def connect_signals(self, sig_bus, *, connect_theme: bool = True) -> None:
        """Connect to the MCUSignals bus."""
        sig_bus.console_log.connect(self.console.append_log)
        sig_bus.console_progress.connect(self.console.update_progress)
        sig_bus.console_clear.connect(self.console.clear)
        if hasattr(sig_bus, "clear_on_action_changed"):
            sig_bus.clear_on_action_changed.connect(self.header.sync_clear_on_action)
        if hasattr(sig_bus, "clear_serial_on_action_changed"):
            sig_bus.clear_serial_on_action_changed.connect(self.header.sync_clear_serial_on_action)
        if hasattr(sig_bus, "timestamp_toggled"):
            sig_bus.timestamp_toggled.connect(self.header.sync_timestamp)
        if hasattr(sig_bus, "font_size_changed"):
            sig_bus.font_size_changed.connect(self.console.set_font_size)
        if connect_theme and hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.apply_theme)
        if hasattr(sig_bus, "hide_warnings_changed"):
            sig_bus.hide_warnings_changed.connect(self.console.set_hide_warnings)

    def apply_theme(self, theme_name: str) -> None:
        self.console.apply_theme(theme_name)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())

    def set_responsive_width(self, width: int) -> None:
        if hasattr(self, "header") and self.header:
            self.header.set_responsive_width(width)
