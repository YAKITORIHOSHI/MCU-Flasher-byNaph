#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.console_panel — Build Console panel for MCU Flasher by Naph.

Replaces the Tkinter ``tk.Text`` build console widget.
Displays PlatformIO compiler output with ANSI-style color tags rendered
as QTextCharFormat.  Autoscroll, copy, and clear controls are built in.
"""
from __future__ import annotations

from collections import deque

# pyrefly: ignore [missing-import]
from PySide6.QtCore import QTimer, Slot, Qt
# pyrefly: ignore [missing-import]
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor, QFont
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QPushButton, QCheckBox, QLabel, QFrame,
)

# Tag → QColor mapping (mirrors the Tkinter tag_configure calls in ui_layout_mixin)
_TAG_COLORS: dict[str, str] = {
    "info":          "#5ca4f0",   # BLUE
    "success":       "#4ec994",   # GREEN
    "warning":       "#f1c40f",   # YELLOW
    "error":         "#e74c3c",   # RED
    "system":        "#56cfbf",   # CYAN
    "dim":           "#6b7280",   # TEXT_DIM
    "magenta":       "#c678dd",   # MAGENTA
    "connecting_magenta": "#c678dd",
    "orange":        "#e67e22",   # ORANGE
    "bold":          "#cdd6f4",   # TEXT (bold)
    "port_highlight":"#ff3fa4",
    "header":        "#56cfbf",   # CYAN header
    "purple":        "#9b59b6",
    "purple_dim":    "#7d3c98",
    "purple_header": "#b07cc6",
    "purple_info":   "#c678dd",
    "purple_value":  "#ffffff",
    "success_bold_lg": "#4ec994",
    "magenta_bold_lg": "#c678dd",
    "sent":          "#c678dd",   # MAGENTA
    "severe_alert":  "#FF3355",
    "timestamp":     "#6b7280",   # TEXT_DIM
    "normal":        "#cdd6f4",   # TEXT
}
_DEFAULT_COLOR = "#cdd6f4"
_BG_COLOR      = "#0d1117"
_MONO_FONT     = QFont("Consolas", 11)

import re
import time

_ANSI_REGEX = re.compile(r"\x1b\[([0-9;]*)m")
_ANSI_CODE_MAP: dict[int, str] = {
    30: "#000000",
    31: "#e74c3c",  # Red
    32: "#4ec994",  # Green
    33: "#f1c40f",  # Yellow
    34: "#5ca4f0",  # Blue
    35: "#c678dd",  # Magenta
    36: "#56cfbf",  # Cyan
    37: "#cdd6f4",  # White
    90: "#6b7280",  # Bright Black (Gray)
    91: "#ff6b6b",  # Bright Red
    92: "#51cf66",  # Bright Green
    93: "#fcc419",  # Bright Yellow
    94: "#74c0fc",  # Bright Blue
    95: "#e599f7",  # Bright Magenta
    96: "#63e6be",  # Bright Cyan
    97: "#ffffff",  # Bright White
}


class AnsiColorParser:
    """Parses standard ANSI escape codes and appends formatted text to QPlainTextEdit."""

    def __init__(self, default_color: str = _DEFAULT_COLOR):
        self.default_color = default_color
        self._current_color = default_color
        self._is_bold = False

    def append_ansi_text(self, editor: QPlainTextEdit, cursor: QTextCursor, text: str) -> None:
        last_end = 0
        for match in _ANSI_REGEX.finditer(text):
            chunk = text[last_end:match.start()]
            if chunk:
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(self._current_color))
                if self._is_bold:
                    fmt.setFontWeight(QFont.Weight.Bold)
                cursor.insertText(chunk, fmt)

            codes = match.group(1).split(";") if match.group(1) else ["0"]
            for c in codes:
                val = int(c) if c.isdigit() else 0
                if val == 0:
                    self._current_color = self.default_color
                    self._is_bold = False
                elif val == 1:
                    self._is_bold = True
                elif val == 22:
                    self._is_bold = False
                elif val in _ANSI_CODE_MAP:
                    self._current_color = _ANSI_CODE_MAP[val]
                elif val == 39:
                    self._current_color = self.default_color

            last_end = match.end()

        remaining = text[last_end:]
        if remaining:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(self._current_color))
            if self._is_bold:
                fmt.setFontWeight(QFont.Weight.Bold)
            cursor.insertText(remaining, fmt)


class ConsolePanelHeader(QWidget):
    """Header bar for the Build Console tab."""

    def __init__(self, console: "ConsolePanel", parent: QWidget | None = None, backend=None):
        super().__init__(parent)
        self._console = console
        self._backend = backend
        self.setObjectName("console-header")
        self.setFixedHeight(36)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(8)
        self._header_layout = layout

        # Title
        title = QLabel("⚙ BUILD CONSOLE")
        title.setProperty("role", "dim")
        self._title_lbl = title
        layout.addWidget(title)
        layout.addStretch()

        from main.core.config import load_gui_config, save_gui_config
        cfg = load_gui_config()
        init_ts = bool(cfg.get("timestamp_enabled", False))
        self._console.set_timestamp_enabled(init_ts)

        # Auto-clear on action checkbox
        init_clear = bool(cfg.get("clear_console_on_action", True))
        self.cb_auto_clear = QCheckBox("Clear on Action")
        self.cb_auto_clear.setChecked(init_clear)
        self.cb_auto_clear.setToolTip("Clear build console before each compile/upload/clean/reset action")
        self.cb_auto_clear.stateChanged.connect(self._on_auto_clear_changed)
        layout.addWidget(self.cb_auto_clear)
        if self._backend is not None:
            self._backend.clear_console_on_action = init_clear

        # Auto-clear serial on action checkbox
        init_clear_serial = bool(cfg.get("clear_serial_on_action", False))
        self.cb_auto_clear_serial = QCheckBox("Clear Serial on Action")
        self.cb_auto_clear_serial.setChecked(init_clear_serial)
        self.cb_auto_clear_serial.setToolTip("Clear serial monitor before each compile/upload/reset action")
        self.cb_auto_clear_serial.stateChanged.connect(self._on_auto_clear_serial_changed)
        layout.addWidget(self.cb_auto_clear_serial)
        if self._backend is not None:
            self._backend.clear_serial_on_action = init_clear_serial

        # Autoscroll
        self.cb_autoscroll = QCheckBox("Auto-scroll")
        self.cb_autoscroll.setChecked(True)
        self.cb_autoscroll.setToolTip("Auto-scroll console output to bottom")
        layout.addWidget(self.cb_autoscroll)

        # Copy button
        btn_copy = QPushButton("⧉ Copy")
        btn_copy.setObjectName("btn-copy-console")
        btn_copy.setFixedHeight(26)
        btn_copy.setToolTip("Copy build console output to clipboard")
        btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_copy.clicked.connect(self._copy_console)
        self._btn_copy = btn_copy
        layout.addWidget(btn_copy)

        # Clear button
        btn_clear = QPushButton("🗑 Clear")
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
        if width >= 1100:
            self._is_ultra_compact = False
            self._title_lbl.setText("⚙ BUILD CONSOLE")
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
            self._title_lbl.setText("⚙ Build Console")
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
            self._title_lbl.setText("⚙")
            self.cb_auto_clear.setText("Clr Act")
            self.cb_auto_clear_serial.setText("Clr Ser")
            self.cb_autoscroll.setText("Auto")
            self._btn_copy.setText("⧉")
            self._btn_clear.setText("🗑")
            if hasattr(self, "_header_layout"):
                self._header_layout.setSpacing(4)
                self._header_layout.setContentsMargins(4, 4, 4, 4)

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

    def sync_clear_serial_on_action(self, checked: bool) -> None:
        if self.cb_auto_clear_serial.isChecked() != checked:
            self.cb_auto_clear_serial.blockSignals(True)
            self.cb_auto_clear_serial.setChecked(checked)
            self.cb_auto_clear_serial.blockSignals(False)

    def _copy_console(self) -> None:
        include_ts = self._console._timestamp_enabled if hasattr(self._console, "_timestamp_enabled") else False
        text = self._console.get_content_for_clipboard(include_timestamp=include_ts)
        # pyrefly: ignore [missing-import]
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        self._btn_copy.setText("✔ Copied!")
        def _restore_btn_copy():
            self._btn_copy.setText("⧉" if getattr(self, "_is_ultra_compact", False) else "⧉ Copy")
        QTimer.singleShot(1500, _restore_btn_copy)


def _insert_with_bar_styling(cursor: QTextCursor, text: str, default_fmt: QTextCharFormat) -> None:
    if "▰" not in text and "▱" not in text:
        cursor.insertText(text, default_fmt)
        return
    bar_fmt = QTextCharFormat(default_fmt)
    bar_fmt.setFontFamilies(["Segoe UI Symbol", "Segoe UI Variable Static Display", "Consolas", "monospace"])
    bar_fmt.setFontPointSize(12.0)
    bar_fmt.setFontWeight(QFont.Weight.Bold)
    for part in re.split(r"([▰▱]+)", text):
        if not part:
            continue
        if part[0] in ("▰", "▱"):
            cursor.insertText(part, bar_fmt)
        else:
            cursor.insertText(part, default_fmt)


class ConsolePanel(QPlainTextEdit):
    """
    Read-only build console widget.

    Appends log lines as ``{text, tag, newline}`` dicts from the
    ``signals.console_log`` signal.  Color is applied via QTextCharFormat.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(8000)  # prevent runaway memory growth
        from main.core.config import get_monitor_font_size, load_gui_config
        init_font_size = get_monitor_font_size()
        self.set_font_size(init_font_size)
        self.setObjectName("build-console")

        from main.core.config import get_theme_mode
        self.apply_theme(get_theme_mode())

        # Autoscroll flag — controlled by the header checkbox
        self._autoscroll = True

        cfg = load_gui_config()
        self._timestamp_enabled: bool = bool(cfg.get("timestamp_enabled", False))
        self._entries: list[dict] = []

        # In-RAM batch queue and 25ms (~40 FPS) flush timer to eliminate UI stutter during parallel compiles
        self._queue: deque[tuple[str, str, bool, str | None, str]] = deque()
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(25)
        self._flush_timer.timeout.connect(self._flush_queue)
        self._flush_timer.start()

    def set_font_size(self, size: int) -> None:
        """Update font size ensuring strict monospace metrics across the widget and QTextDocument."""
        try:
            sz = int(size)
        except (ValueError, TypeError):
            sz = 11
        font = QFont("Consolas", sz)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        self.setFont(font)
        self.document().setDefaultFont(font)

    def apply_theme(self, theme_name: str) -> None:
        """Apply active theme palette to build console base colors and ANSI default."""
        from main.qt.theme import get_palette
        pal = get_palette(theme_name)
        bg = pal.get("BG_DARKEST", "#0a0e14")
        fg = pal.get("TEXT", "#e0e6ed")
        palette = self.palette()
        palette.setColor(palette.ColorRole.Base, QColor(bg))
        palette.setColor(palette.ColorRole.Text, QColor(fg))
        self.setPalette(palette)
        if hasattr(self, "_ansi_parser") and self._ansi_parser:
            self._ansi_parser.default_color = fg
            self._ansi_parser._current_color = fg

    def set_autoscroll(self, enabled: bool) -> None:
        self._autoscroll = enabled

    def set_timestamp_enabled(self, enabled: bool) -> None:
        """Dynamically toggle timestamps across the entire console."""
        if self._timestamp_enabled == enabled:
            return
        self._timestamp_enabled = enabled
        self._rebuild_document()

    def get_content_for_clipboard(self, include_timestamp: bool | None = None) -> str:
        """Return console text formatted for clipboard copying.

        If include_timestamp is False, any leading timestamps ([HH:MM:SS]) are
        strictly stripped from all lines, guaranteeing clean code/log output.
        """
        if include_timestamp is None:
            include_timestamp = self._timestamp_enabled

        if include_timestamp:
            return self.toPlainText()

        raw_text = self.toPlainText()
        clean_lines = [
            re.sub(r"^\[\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\]\s*", "", line)
            for line in raw_text.splitlines()
        ]
        return "\n".join(clean_lines)

    def copy(self) -> None:
        """Custom clipboard copy respecting timestamp toggle."""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return
        selected_text = cursor.selectedText().replace("\u2029", "\n")
        if not self._timestamp_enabled:
            selected_text = "\n".join(
                re.sub(r"^\[\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\]\s*", "", line)
                for line in selected_text.splitlines()
            )
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(selected_text)

    def _rebuild_document(self) -> None:
        """Re-render the entire console document with/without timestamps."""
        sb = self.verticalScrollBar()
        saved_val = sb.value()
        saved_max = sb.maximum()
        was_at_bottom = (saved_val >= saved_max - 15)

        cursor = self.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()

        first = True
        ts_fmt = QTextCharFormat()
        ts_fmt.setForeground(QColor(_TAG_COLORS.get("timestamp", "#6b7280")))

        for entry in self._entries:
            text = entry.get("text", "")
            tag = entry.get("tag", "normal")
            newline = entry.get("newline", True)
            ts = entry.get("ts", "")

            color_hex = _TAG_COLORS.get(tag, _DEFAULT_COLOR)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color_hex))
            if tag in ("bold", "header", "severe_alert", "success_bold_lg", "magenta_bold_lg", "purple_header"):
                fmt.setFontWeight(QFont.Weight.Bold)

            if newline and (self.document().characterCount() > 1 or not first):
                cursor.insertText("\n", QTextCharFormat())

            if self._timestamp_enabled and ts:
                cursor.insertText(f"{ts} ", ts_fmt)

            cursor.insertText(text, fmt)
            first = False

        cursor.endEditBlock()

        if self._autoscroll or was_at_bottom:
            self.setTextCursor(cursor)
            sb.setValue(sb.maximum())
        else:
            sb.setValue(saved_val)

    @Slot(dict)
    def append_log(self, payload: dict) -> None:
        """Enqueue one log entry from the console:log signal payload for buffered RAM flush."""
        text: str  = payload.get("text", "")
        tag: str   = payload.get("tag", "normal")
        newline: bool = payload.get("newline", True)
        replace_pattern: str | None = payload.get("replace_pattern")
        ts: str = payload.get("timestamp") or time.strftime("[%H:%M:%S]")
        m = re.match(r"^(\[\d+:\d+:\d+\])\s*(.*)$", text)
        if m:
            ts = m.group(1)
            text = m.group(2)
        self._queue.append((text, tag, newline, replace_pattern, ts))

    def _flush_queue(self) -> None:
        if not self._queue:
            return

        # Drain up to 800 items per tick
        items: list[tuple[str, str, bool, str | None, str]] = []
        count = 0
        while self._queue and count < 800:
            items.append(self._queue.popleft())
            count += 1

        if not items:
            return

        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        first = True
        ts_fmt = QTextCharFormat()
        ts_fmt.setForeground(QColor(_TAG_COLORS.get("timestamp", "#6b7280")))

        for text, tag, newline, replace_pattern, ts in items:
            color_hex = _TAG_COLORS.get(tag, _DEFAULT_COLOR)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color_hex))
            if tag in ("bold", "header", "severe_alert", "success_bold_lg", "magenta_bold_lg", "purple_header"):
                fmt.setFontWeight(QFont.Weight.Bold)

            replaced = False
            if replace_pattern:
                pat = re.compile(replace_pattern, re.IGNORECASE)
                doc = self.document()
                block = doc.lastBlock()
                scan_limit = 250
                while block.isValid() and scan_limit > 0:
                    b_text = block.text()
                    clean_b_text = re.sub(r"^\[\d+:\d+:\d+\]\s*", "", b_text)
                    if pat.search(b_text) or pat.search(clean_b_text):
                        cur = QTextCursor(block)
                        cur.movePosition(QTextCursor.MoveOperation.StartOfBlock)
                        cur.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)

                        # Update matching entry in self._entries
                        for entry in reversed(self._entries):
                            if pat.search(entry["text"]):
                                entry["text"] = text
                                entry["tag"] = tag
                                entry["ts"] = ts
                                break

                        cur.beginEditBlock()
                        if self._timestamp_enabled and ts:
                            cur.insertText(f"{ts} ", ts_fmt)
                        _insert_with_bar_styling(cur, text, fmt)
                        cur.endEditBlock()
                        replaced = True
                        break
                    block = block.previous()
                    scan_limit -= 1

            if not replaced:
                self._entries.append({
                    "text": text,
                    "tag": tag,
                    "newline": newline,
                    "ts": ts,
                })
                if len(self._entries) > 8000:
                    self._entries.pop(0)

                cursor.movePosition(QTextCursor.MoveOperation.End)
                if newline and (self.document().characterCount() > 1 or not first):
                    cursor.insertText("\n", QTextCharFormat())
                if self._timestamp_enabled and ts:
                    cursor.insertText(f"{ts} ", ts_fmt)
                _insert_with_bar_styling(cursor, text, fmt)
                first = False

        if self._autoscroll:
            self.setTextCursor(cursor)
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    @Slot(dict)
    def update_progress(self, payload: dict) -> None:
        """Handle console:progress signals (reserved for status bar integration)."""
        pass  # Handled by the main window's status bar / progress widget

    @Slot()
    def clear(self) -> None:
        """Clear all console content and in-RAM queue."""
        self._entries.clear()
        self._queue.clear()
        super().clear()


class ConsolePanelContainer(QWidget):
    """Container combining header + ConsolePanel into one tab widget."""

    def __init__(self, backend=None, parent: QWidget | None = None):
        super().__init__(parent)
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

    def connect_signals(self, sig_bus) -> None:
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
        if hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.apply_theme)

    def apply_theme(self, theme_name: str) -> None:
        self.console.apply_theme(theme_name)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())

    def set_responsive_width(self, width: int) -> None:
        if hasattr(self, "header") and self.header:
            self.header.set_responsive_width(width)
