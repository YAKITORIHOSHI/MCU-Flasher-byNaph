#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.serial_panel — Serial Monitor panel for MCU Flasher by Naph.

Replaces the Tkinter serial console (tk.Text + input Entry).
Features:
  - Read-only QPlainTextEdit for serial output with color tags
  - QLineEdit send bar with line-ending selector
  - Baud rate selector
  - Clear, Copy, Pause, Reset MCU buttons
  - Auto-scroll checkbox
  - Connection status indicator
"""
from __future__ import annotations

import re
from datetime import datetime
from itertools import chain
from collections import deque

from PySide6.QtCore import QTimer, Slot, Signal, Qt
from PySide6.QtGui import QColor, QFont, QKeySequence, QTextCharFormat, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QPlainTextEdit,
    QPushButton, QCheckBox, QLabel, QLineEdit, QComboBox, QFrame,
    QApplication, QSizePolicy, QToolButton, QMenu,
)
from main.qt.icons import ActionButton as QPushButton
from main.qt.log_follow import LogFollow, preserve_log_view

from main.core.constants import MAX_BAUD_RATE

_BAUD_RATES = [b for b in ["9600", "19200", "38400", "57600", "74880", "115200",
               "230400", "460800", "512000", "921600"] if int(b) <= MAX_BAUD_RATE]
_LINE_ENDINGS = [("None", "none"), ("\\n", "nl"), ("\\r", "cr"), ("\\r\\n", "both")]

_SERIAL_CONTROLS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")


def _visible_serial_controls(text: str) -> str:
    """Keep clipboard-hostile controls visible without discarding device data."""
    def escape(match):
        value = ord(match.group())
        return f"\\x{value:02x}" if value < 256 else f"\\u{value:04x}"
    return _SERIAL_CONTROLS_RE.sub(escape, text.replace("\r\n", "\n"))


class _SerialAnsiParser:
    """Bounded incremental terminal controls for an append-only serial log.

    A random/incomplete escape must not swallow unlimited subsequent output.
    Cursor-home does not erase history; only an explicit erase-screen does.
    """
    def __init__(self):
        self._sequence_id = 0
        self.reset()

    def reset(self):
        self._state = "text"
        self._pending = ""

    def finish(self):
        text = _visible_serial_controls(self._pending)
        self.reset()
        return text

    def feed(self, text):
        if self._state == "text" and "\x1b" not in text:
            return [(_visible_serial_controls(text), False)]
        events, pieces = [], []

        def emit(clear=False):
            if pieces:
                events.append((_visible_serial_controls("".join(pieces)), False))
                pieces.clear()
            if clear:
                events.append(("", True))

        for char in text:
            if self._state == "text":
                if char == "\x1b":
                    self._sequence_id += 1
                    self._pending = char
                    self._state = "escape"
                else:
                    pieces.append(char)
                continue
            self._pending += char
            if len(self._pending) > 256:
                pieces.append(self.finish())
                continue
            if self._state == "escape":
                self._state = {"[": "csi", "]": "string", "P": "string",
                               "^": "string", "_": "string"}.get(char, "text")
                if self._state == "text":
                    pieces.append(self.finish())
            elif self._state == "csi":
                if "@" <= char <= "~":
                    clear = char == "J" and self._pending[2:-1] in ("2", "3")
                    self.reset()
                    if clear:
                        emit(True)
                elif not (" " <= char <= "?"):
                    pieces.append(self.finish())
            elif self._state == "string":
                if char == "\x07":
                    self.reset()
                elif char == "\x1b":
                    self._state = "string_end"
            elif self._state == "string_end":
                if char == "\\":
                    self.reset()
                else:
                    self._state = "string"
        emit()
        return events


class SerialOutputView(QPlainTextEdit):
    """Read-only high-performance serial monitor output view.

    Coalesces output with bounded pending/history buffers, small render batches
    and long-line limits. Sustained display overload produces a visible notice.
    """
    copy_started = Signal()
    line_wrap_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("serial-console")
        self.setReadOnly(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.setUndoRedoEnabled(False)
        from src.modules.runtime_resources import performance_profile
        from main.qt.log_buffer import LogBuffer
        profile = performance_profile()
        self._history_limit = 512_000 if profile.constrained else 2_000_000
        self.setMaximumBlockCount(profile.terminal_scrollback)
        from main.core.config import get_monitor_font_size
        init_font_size = get_monitor_font_size()
        self.set_font_size(init_font_size)
        from main.core.config import get_theme_mode
        self.apply_theme(get_theme_mode())
        self._autoscroll = True
        self._follow = LogFollow(self, hold_to_pause=True)

        self._paused = False
        self._paused_dirty = False
        self._ansi_clear_enabled = True
        self._ansi_parser = _SerialAnsiParser()
        self._render_line_start = True
        self._history_line_start = True
        self._stream_generation = None
        self._ansi_pending_tag = "normal"
        self._ansi_pending_stamp = ""
        from main.core.config import load_gui_config
        cfg = load_gui_config()
        self._line_wrap_enabled = bool(cfg.get("serial_line_wrap", True))
        self._apply_line_wrap()
        self._timestamp_enabled = bool(cfg.get("timestamp_enabled", False))
        self._entries = LogBuffer(self._history_limit, profile.terminal_scrollback, lambda item: item[0])
        self._queue = LogBuffer(256_000 if profile.constrained else 1_000_000, 2000, lambda item: item[0])
        self._last_autoscroll = 0.0

        # Batch drain timer (drains queue at ~30ms / ~33 FPS)
        self._flush_timer = QTimer(self)
        from src.modules.runtime_resources import performance_profile
        self._flush_timer.setInterval(40 if performance_profile().constrained else 30)
        self._flush_timer.timeout.connect(self._flush_queue)
        # Demand-driven: timer starts when logs arrive and stops when queue is drained

    def resizeEvent(self, event) -> None:
        if hasattr(self, "_follow"):
            with self._follow.update(resize=True):
                super().resizeEvent(event)
        else:
            super().resizeEvent(event)

    def set_font_size(self, size: int) -> None:
        """Update font size ensuring strict monospace metrics across the widget and QTextDocument."""
        try:
            sz = max(6, min(48, int(size)))
        except (ValueError, TypeError):
            sz = 12
        self._font_size = sz
        font = QFont("Consolas", sz)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        self.setFont(font)
        self.document().setDefaultFont(font)
        if hasattr(self, "_theme_name"):
            self.apply_theme(self._theme_name)

    def apply_theme(self, theme_name: str) -> None:
        """Apply active theme palette to serial output view base colors."""
        from main.qt.theme import get_palette
        from main.qt.log_colors import themed_log_colors
        pal = get_palette(theme_name)
        bg = pal.get("BG_DARKEST", "#0a0e14")
        fg = pal.get("TEXT", "#e0e6ed")
        self._tag_colors = themed_log_colors(theme_name)
        self._theme_name = theme_name
        hover = pal.get("BG_HOVER", bg)
        bright = pal.get("TEXT_BRIGHT", fg)
        self.setStyleSheet(
            "QPlainTextEdit#serial-console { "
            f"background-color: {bg}; color: {fg}; "
            f"selection-background-color: {hover}; selection-color: {bright}; "
            f"font-family: Consolas; font-size: {self._font_size}pt; "
            "border: none; }"
        )
        palette = self.palette()
        palette.setColor(palette.ColorRole.Base, QColor(bg))
        palette.setColor(palette.ColorRole.Text, QColor(fg))
        self.setPalette(palette)
        if hasattr(self, "_entries"):
            self._rebuild_document()

    def set_autoscroll(self, enabled: bool) -> None:
        self._autoscroll = enabled
        self._follow.set_enabled(enabled)

    def _apply_line_wrap(self) -> None:
        option = self.document().defaultTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
                           if self._line_wrap_enabled else QTextOption.WrapMode.NoWrap)
        self.document().setDefaultTextOption(option)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth
                             if self._line_wrap_enabled else QPlainTextEdit.LineWrapMode.NoWrap)
        status = "Long serial lines wrap to fit." if self._line_wrap_enabled else "Serial line wrapping is off."
        self.setToolTip(f"{status} Right-click for Wrap lines and Copy options.")
        self.setAccessibleDescription("Read-only serial output. Wrap lines changes display only and preserves copied line breaks.")

    @preserve_log_view()
    def set_line_wrap_enabled(self, enabled: bool) -> None:
        """Reflow the visible document without changing retained device lines."""
        enabled = bool(enabled)
        if self._line_wrap_enabled == enabled:
            return
        self._line_wrap_enabled = enabled
        self._apply_line_wrap()
        self.line_wrap_changed.emit(enabled)

    def request_line_wrap(self, enabled: bool) -> bool:
        """Apply an explicit display choice only after it is safely saved."""
        enabled = bool(enabled)
        if enabled == self._line_wrap_enabled:
            return True
        from main.qt.preferences import save_display_preference
        if not save_display_preference("serial_line_wrap", enabled, "Serial line wrapping"):
            return False
        self.set_line_wrap_enabled(enabled)
        return True

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        if not self._paused and self._paused_dirty:
            self._rebuild_document()
            self._paused_dirty = False

    def set_ansi_clear_enabled(self, enabled: bool) -> None:
        self._ansi_clear_enabled = enabled

    def set_timestamp_enabled(self, enabled: bool) -> None:
        if self._timestamp_enabled == enabled:
            return
        self._timestamp_enabled = enabled
        self._rebuild_document()

    def get_content_for_clipboard(self, include_timestamp: bool | None = None) -> str:
        """Copy retained serial data, including output awaiting a display batch.

        Qt shortens very long visual lines to protect responsiveness. Copy uses
        the bounded journal, so it still includes retained data on those lines.
        Device-authored timestamp-like text is never removed.
        """
        if include_timestamp is None:
            include_timestamp = self._timestamp_enabled
        while self._queue:
            self._flush_queue()
        # An unfinished escape is still received data. Snapshot it visibly for
        # Copy without resetting the active parser or corrupting its next chunk.
        pending = _visible_serial_controls(self._ansi_parser._pending)
        tail = [(pending, self._ansi_pending_tag, False, self._ansi_pending_stamp)] if pending else []
        chunks, _ = self._formatted_chunks(chain(self._entries, tail), bool(include_timestamp))
        return "".join(text for text, _tag in chunks)

    def copy(self) -> None:
        """Copy exactly the selected visible text, without reinterpretation."""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return
        selected_text = cursor.selectedText().replace("\u2029", "\n")
        self.copy_started.emit()
        QApplication.clipboard().setText(selected_text)

    def keyPressEvent(self, event) -> None:
        # QPlainTextEdit.copy is a non-virtual C++ slot. Native shortcuts must
        # enter our handler to cancel an older pending header-copy retry.
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
            event.accept()
            return
        super().keyPressEvent(event)

    def _show_context_menu(self, position) -> None:
        menu = QMenu(self)
        copy_action = menu.addAction(self.tr("&Copy"))
        copy_action.setObjectName("serial-copy-selection")
        copy_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))
        copy_action.setEnabled(self.textCursor().hasSelection())
        copy_action.triggered.connect(self.copy)
        menu.addSeparator()
        select_all = menu.addAction(self.tr("Select &All"))
        select_all.setObjectName("serial-select-all")
        select_all.setShortcut(QKeySequence(QKeySequence.StandardKey.SelectAll))
        select_all.setEnabled(not self.document().isEmpty())
        select_all.triggered.connect(self.selectAll)
        menu.addSeparator()
        wrap_action = menu.addAction(self.tr("Wrap lines"))
        wrap_action.setObjectName("serial-wrap-lines")
        wrap_action.setCheckable(True)
        wrap_action.setChecked(self._line_wrap_enabled)
        wrap_action.setToolTip("Fit long lines to the panel width; copied output keeps its original line breaks")

        def toggle_wrap(enabled):
            self.request_line_wrap(enabled)
            previous = wrap_action.blockSignals(True)
            wrap_action.setChecked(self._line_wrap_enabled)
            wrap_action.blockSignals(previous)

        wrap_action.toggled.connect(toggle_wrap)
        try:
            menu.exec(self.viewport().mapToGlobal(position))
        finally:
            menu.deleteLater()

    @staticmethod
    def _formatted_chunks(entries, timestamps, line_start=True):
        """Format only application prefixes, once per logical device line."""
        groups = []

        def add(text, tag):
            if not text:
                return
            if groups and groups[-1][1] == tag:
                groups[-1][0].append(text)
            else:
                groups.append(([text], tag))

        for text, tag, newline, stamp in entries:
            payload = text + ("\n" if newline else "")
            parts = payload.split("\n")
            for index, part in enumerate(parts):
                if part:
                    if timestamps and line_start:
                        add(f"[{stamp}] ", "timestamp")
                    add(part, tag or "normal")
                    line_start = False
                if index < len(parts) - 1:
                    add("\n", tag or "normal")
                    line_start = True
        return [("".join(parts), tag) for parts, tag in groups], line_start

    def _insert_chunks(self, cursor, chunks):
        for text, tag in chunks:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(self._tag_colors.get(tag, self._tag_colors["normal"])))
            cursor.insertText(text, fmt)

    @staticmethod
    def _visual_entries(entries, timestamps=False):
        """Bound paragraphs before Qt shapes them; retain the journal intact.

        Match ordinary display trimming: the latest 8191 UTF-16 units plus its
        marker (QTextBlock's 8192-unit tail also counts the block terminator).
        A deque and small per-event encodes avoid shaping or encoding the full
        multi-megabyte line. Trimming never splits a Unicode surrogate pair.
        """
        line, size, total, truncated = deque(), 0, 0, False
        first_stamp = ""
        prefix_units = 0

        def flush():
            if truncated:
                yield ("[Long line display truncated] ", "warning", False, first_stamp)
            for text, tag, newline, stamp, _units in line:
                yield text, tag, newline, stamp

        for text, tag, newline, stamp in entries:
            parts = (text + ("\n" if newline else "")).split("\n")
            for index, part in enumerate(parts):
                if part:
                    if not line:
                        first_stamp = stamp
                        prefix_units = len(f"[{stamp}] ") if timestamps else 0
                    units = len(part.encode("utf-16-le")) // 2
                    line.append((part, tag, False, stamp, units))
                    size += units
                    total += units
                    truncated = truncated or total + prefix_units > 16383
                    limit = 8191 if truncated else 16383 - prefix_units
                    excess = size - limit
                    while excess > 0:
                        old, old_tag, _, old_stamp, old_units = line.popleft()
                        cut = min(excess, old_units)
                        remaining = ""
                        if cut < old_units:
                            encoded = old.encode("utf-16-le")
                            offset = cut * 2
                            next_unit = int.from_bytes(encoded[offset:offset + 2], "little")
                            if 0xdc00 <= next_unit <= 0xdfff:
                                cut += 1
                            remaining = encoded[cut * 2:].decode("utf-16-le")
                        size -= cut
                        excess -= cut
                        truncated = True
                        if remaining:
                            line.appendleft((remaining, old_tag, False, old_stamp, old_units - cut))
                if index < len(parts) - 1:
                    yield from flush()
                    yield ("\n", tag, False, stamp)
                    line.clear()
                    size, total, truncated = 0, 0, False
        yield from flush()

    @preserve_log_view(rebuild=True)
    def _rebuild_document(self) -> None:
        if self._paused:
            self._paused_dirty = True
            return
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()

        chunks, self._render_line_start = self._formatted_chunks(
            self._visual_entries(self._entries, self._timestamp_enabled), self._timestamp_enabled)
        self._insert_chunks(cursor, chunks)

        cursor.endEditBlock()
        from main.qt.log_buffer import trim_document
        trim_document(self, self._history_limit)

    @Slot(dict)
    def append_log(self, payload: dict) -> None:
        tag: str  = payload.get("tag", "normal")
        lines = payload.get("lines")
        from main.qt.log_buffer import display_text
        stamp = str(payload.get("timestamp", "")).strip("[]")
        if len(stamp) > 32 or not re.fullmatch(r"[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?", stamp):
            stamp = datetime.now().strftime("%H:%M:%S")
        stream = bool(payload.get("stream", True))
        generation = payload.get("generation")
        stream_end = bool(payload.get("stream_end", False))
        if isinstance(lines, list):
            for line in lines:
                self._queue.append((display_text(line), tag, True, stamp, stream, generation, stream_end))
        else:
            self._queue.append((display_text(payload.get("text", "")), tag,
                                bool(payload.get("newline", True)), stamp, stream, generation, stream_end))
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    @preserve_log_view()
    def _flush_queue(self) -> None:
        if not self._queue:
            if self._flush_timer.isActive():
                self._flush_timer.stop()
            return

        items = self._queue.drain(max_items=128, max_chars=32768)
        notice = self._queue.take_notice()
        if notice:
            # A missing chunk can split a terminal control; do not allow its
            # remainder to swallow the subsequent retained output.
            self._ansi_parser.reset()
            items.insert(0, ("\n" + notice, "warning", True,
                             datetime.now().strftime("%H:%M:%S"), False, None, False))

        if not items:
            return

        additions = []
        cleared = False
        for text, tag, newline, stamp, stream, generation, stream_end in items:
            events = []
            sequence = self._ansi_parser._sequence_id

            def finish_pending():
                pending = self._ansi_parser.finish()
                if pending:
                    events.append((pending, False, self._ansi_pending_tag, self._ansi_pending_stamp))

            if generation is not None and generation != self._stream_generation:
                finish_pending()
                self._stream_generation = generation
            if not stream:
                finish_pending()
                line_open = not self._history_line_start or bool(events)
                prefix = "\n" if line_open and (text or newline) and not text.startswith("\n") else ""
                events.append((prefix + _visible_serial_controls(text) + ("\n" if newline else ""), False, tag, stamp))
            else:
                events.extend((content, erased, tag, stamp) for content, erased in
                              self._ansi_parser.feed(text + ("\n" if newline else "")))
            if self._ansi_parser._pending and self._ansi_parser._sequence_id != sequence:
                self._ansi_pending_tag = tag
                self._ansi_pending_stamp = stamp
            if stream_end:
                finish_pending()
            for clean_text, erase_screen, event_tag, event_stamp in events:
                if erase_screen and self._ansi_clear_enabled:
                    additions.clear()
                    self._entries.clear()
                    self._render_line_start = True
                    self._history_line_start = True
                    cleared = True
                if clean_text:
                    entry = (clean_text, event_tag, False, event_stamp)
                    self._entries.append(entry)
                    additions.append(entry)
                    self._history_line_start = clean_text.endswith("\n")

        if self._paused:
            self._paused_dirty = self._paused_dirty or bool(additions) or cleared
            if not self._queue:
                self._flush_timer.stop()
            return

        if cleared:
            super().clear()
        chunks, self._render_line_start = self._formatted_chunks(
            additions, self._timestamp_enabled, self._render_line_start)

        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        try:
            self._insert_chunks(cursor, chunks)
        finally:
            cursor.endEditBlock()
        from main.qt.log_buffer import trim_document
        trim_document(self, self._history_limit)
        if not self._queue and self._flush_timer.isActive():
            self._flush_timer.stop()

    @Slot()
    def clear(self) -> None:
        self._entries.clear()
        self._queue.clear()
        self._ansi_parser.reset()
        self._render_line_start = True
        self._history_line_start = True
        self._stream_generation = None
        self._paused_dirty = False
        if self._flush_timer.isActive():
            self._flush_timer.stop()
        super().clear()
        self._follow.reset()


class SerialPanel(QWidget):
    """
    Full serial monitor panel: header controls + output view + send bar.
    Mirrors the Tkinter serial_monitor_frame structure exactly.
    """

    def __init__(self, backend, parent: QWidget | None = None):
        super().__init__(parent)
        self._backend = backend
        self._paused = False
        self._setup_ui()
        from main.core.config import get_theme_mode
        self.apply_theme(get_theme_mode())

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ───────────────────────────────────────────────────────────
        header = QWidget()
        self._header = header
        header.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        header.setObjectName("serial-header")
        h = QGridLayout(header)
        h.setContentsMargins(10, 4, 10, 4)
        h.setSpacing(8)
        self._header_layout = h

        title = QLabel("Serial monitor")
        self._title_lbl = title
        title.setStyleSheet("color: #00d2ff; font-weight: bold; font-size: 12px;")
        h.addWidget(title, 0, 0)

        # Reset MCU button
        self.btn_reset = QPushButton("↺ Reset")
        self.btn_reset.setFixedHeight(26)
        self.btn_reset.setToolTip("Hardware reboot microcontroller via DTR/RTS reset pulse")
        self.btn_reset.clicked.connect(self._on_reset)
        h.addWidget(self.btn_reset, 0, 1)

        # Pause button
        self.btn_pause = QPushButton("⏸ Pause")
        self.btn_pause.setFixedHeight(26)
        self.btn_pause.setCheckable(True)
        self.btn_pause.setToolTip("Pause / resume serial stream display")
        self.btn_pause.clicked.connect(self._on_pause_toggle)
        h.addWidget(self.btn_pause, 0, 2)

        h.setColumnStretch(3, 1)

        from main.core.config import load_gui_config
        cfg = load_gui_config()

        # Autoscroll
        self.cb_autoscroll = QCheckBox("Auto-scroll")
        self.cb_autoscroll.setChecked(bool(cfg.get("serial_autoscroll", True)))
        self.cb_autoscroll.setToolTip("Auto-scroll output to newest received line")
        self.cb_autoscroll.stateChanged.connect(self._on_autoscroll_changed)
        h.addWidget(self.cb_autoscroll, 0, 3)

        # Clear on Action checkbox
        init_clear_serial = bool(cfg.get("clear_serial_on_action", False))
        self.cb_auto_clear = QCheckBox("Clear on Action")
        self.cb_auto_clear.setChecked(init_clear_serial)
        self.cb_auto_clear.setToolTip("Clear serial monitor before each compile/upload/reset action")
        self.cb_auto_clear.stateChanged.connect(self._on_auto_clear_changed)
        h.addWidget(self.cb_auto_clear, 0, 4)

        # ANSI clear-screen checkbox
        self.cb_ansi_clear = QCheckBox("Clear-screen")
        self.cb_ansi_clear.setChecked(bool(cfg.get("serial_ansi_clear", True)))
        self.cb_ansi_clear.setToolTip("Honour ANSI terminal clear-screen sequences from MCU output")
        self.cb_ansi_clear.stateChanged.connect(self._on_ansi_clear_changed)
        h.addWidget(self.cb_ansi_clear, 0, 5)

        # Separator
        div = QFrame()
        self._div_sep = div
        div.setFrameShape(QFrame.Shape.VLine)
        div.setStyleSheet("color: #2a5f58;")
        h.addWidget(div, 0, 6)

        # Connection status label
        initial_connected = False
        if self._backend and getattr(self._backend, "serial_running", False):
            initial_connected = True

        self.lbl_status = QLabel("● Connected" if initial_connected else "● Disconnected")
        self._connection_text = self.lbl_status.text()
        self._connection_color_tag = "success" if initial_connected else "error"
        h.addWidget(self.lbl_status, 0, 7)

        div2 = QFrame()
        self._div2_sep = div2
        div2.setFrameShape(QFrame.Shape.VLine)
        div2.setStyleSheet("color: #2a5f58;")
        h.addWidget(div2, 0, 8)

        # Baud rate container (label + combo)
        self.baud_container = QWidget()
        self.baud_container.setObjectName("serial-baud-container")
        self.baud_container.setStyleSheet("background: transparent;")
        b_layout = QHBoxLayout(self.baud_container)
        b_layout.setContentsMargins(0, 0, 0, 0)
        b_layout.setSpacing(6)

        self._lbl_baud = QLabel("Baud rate")
        self._lbl_baud.setStyleSheet("font-size: 11px; font-weight: 600; background: transparent;")
        b_layout.addWidget(self._lbl_baud)

        self.baud_combo = QComboBox()
        self.baud_combo.addItems(_BAUD_RATES)
        self.baud_combo.setCurrentText("115200")
        self.baud_combo.setFixedWidth(self._get_adaptive_baud_width())
        self.baud_combo.setToolTip("Serial monitor baud rate")
        self.baud_combo.currentTextChanged.connect(self._on_baud_changed)
        b_layout.addWidget(self.baud_combo)

        self._baud_container = self.baud_container
        h.addWidget(self.baud_container, 0, 9)

        # Copy button
        self.btn_copy = QPushButton("⧉ Copy")
        self.btn_copy.setFixedHeight(26)
        self.btn_copy.setToolTip("Copy serial monitor output to clipboard")
        self.btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy.clicked.connect(self._copy_output)
        h.addWidget(self.btn_copy, 0, 10)

        # Clear button
        self.btn_clear = QPushButton("🗑 Clear")
        self.btn_clear.setFixedHeight(26)
        self.btn_clear.setToolTip("Clear serial monitor output buffer")
        self.btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear.clicked.connect(self._output_clear)
        h.addWidget(self.btn_clear, 0, 11)

        self._header_stacked = None
        self._header_mode = None
        self._header_row_layouts = (QHBoxLayout(), QHBoxLayout())
        self._display_options = QToolButton(header)
        self._display_options.setObjectName("serial-display-options")
        self._display_options.setText("Options")
        self._display_options.setAccessibleName("Serial display options")
        self._display_options.setToolTip("Serial display options, including Wrap lines; also available by right-clicking output")
        self._display_options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self._display_options)
        self._display_option_actions = []
        self._wrap_action = menu.addAction("Wrap lines")
        self._wrap_action.setObjectName("serial-wrap-lines")
        self._wrap_action.setCheckable(True)
        self._wrap_action.setChecked(bool(cfg.get("serial_line_wrap", True)))
        self._wrap_action.setToolTip("Fit long lines to the panel width; copied output keeps its original line breaks")
        self._wrap_action.toggled.connect(self._on_line_wrap_changed)
        for label, checkbox in (("Auto-scroll", self.cb_autoscroll), ("Clear on Action", self.cb_auto_clear), ("Clear-screen", self.cb_ansi_clear)):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(checkbox.isChecked())
            action.toggled.connect(checkbox.setChecked)
            self._display_option_actions.append((action, checkbox))
        menu.aboutToShow.connect(self._sync_display_options)
        menu.addSeparator()
        menu.addAction("Copy output", self._copy_output)
        menu.addAction("Clear output", self._output_clear)
        self._display_options.setMenu(menu)
        self._display_options.hide()
        # ── Separator ────────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #2a5f58;")

        # ── Output View ───────────────────────────────────────────────────────
        self._output = SerialOutputView()
        self._output.copy_started.connect(self._cancel_copy_output)
        self._output.line_wrap_changed.connect(self._sync_display_options)
        self._output.set_autoscroll(self.cb_autoscroll.isChecked())
        self._output.set_ansi_clear_enabled(self.cb_ansi_clear.isChecked())
        output_policy = self._output.sizePolicy()
        output_policy.setVerticalPolicy(QSizePolicy.Policy.Ignored)
        self._output.setSizePolicy(output_policy)
        self._output.setMinimumHeight(24)

        # ── Send Bar ─────────────────────────────────────────────────────────
        send_bar = QWidget()
        send_bar.setFixedHeight(40)
        send_bar.setObjectName("serial-send-bar")
        sb = QHBoxLayout(send_bar)
        sb.setContentsMargins(10, 4, 10, 4)
        sb.setSpacing(8)

        self._send_layout = sb
        self._send_bar = send_bar
        self._send_label = QLabel("Send >")
        sb.addWidget(self._send_label)

        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Type command and press Enter…")
        self.input_field.returnPressed.connect(self._send_serial)
        self.input_field.setEnabled(initial_connected)
        self.input_field.setMinimumWidth(0)
        self.input_field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        sb.addWidget(self.input_field, stretch=1)

        # Line ending selector
        self.line_ending_combo = QComboBox()
        for label, _ in _LINE_ENDINGS:
            self.line_ending_combo.addItem(label)
        self.line_ending_combo.setCurrentIndex(3)  # \r\n default
        self.line_ending_combo.setFixedWidth(70)
        sb.addWidget(self.line_ending_combo)

        self.btn_send = QPushButton("Send")
        self.btn_send.setObjectName("btn-serial-send")
        self.btn_send.setFixedHeight(30)
        self.btn_send.clicked.connect(self._send_serial)
        self.btn_send.setEnabled(initial_connected)
        self.btn_send.setCursor(Qt.CursorShape.PointingHandCursor if initial_connected else Qt.CursorShape.ArrowCursor)
        sb.addWidget(self.btn_send)

        # Initial gating for header buttons
        self.btn_reset.setEnabled(initial_connected)
        self.btn_reset.setCursor(Qt.CursorShape.PointingHandCursor if initial_connected else Qt.CursorShape.ArrowCursor)
        self.btn_pause.setEnabled(initial_connected)
        self.btn_pause.setCursor(Qt.CursorShape.PointingHandCursor if initial_connected else Qt.CursorShape.ArrowCursor)

        # Assemble layout (top-to-bottom)
        root.addWidget(header)
        root.addWidget(sep)
        root.addWidget(self._output, stretch=1)
        root.addWidget(send_bar)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())

    def _get_adaptive_baud_width(self, width: int | None = None) -> int:
        """Reserve text and arrow space without multiplying Qt's logical metrics."""
        try:
            fm = self.baud_combo.fontMetrics()
            text_w = max(fm.horizontalAdvance(b) for b in _BAUD_RATES)
        except Exception:
            text_w = 48

        return max(86, text_w + 44)

    def update_adaptive_sizing(self) -> None:
        """Refresh adaptive sizing when screen resolution or DPI scaling changes."""
        w = getattr(self, "_current_responsive_width", self.width())
        self.set_responsive_width(w)

    def set_responsive_width(self, width: int) -> None:
        """Dynamically adapt header controls, checkboxes, and labels based on width."""
        self._current_responsive_width = width
        baud_w = self._get_adaptive_baud_width(width)
        if width >= 1100:
            self._is_ultra_compact = False
            self._title_lbl.setText("Serial monitor")
            self.btn_reset.setText("↺ Reset")
            self.btn_pause.setText("▶ Resume" if self._paused else "⏸ Pause")
            self.cb_autoscroll.setText("Auto-scroll")
            self.cb_auto_clear.setText("Clear on Action")
            self.cb_ansi_clear.setText("Clear-screen")
            if hasattr(self, "_lbl_baud"):
                self._lbl_baud.setVisible(True)
                self._lbl_baud.setText("Baud rate")
            self.baud_combo.setFixedWidth(baud_w)
            self.btn_copy.setText("⧉ Copy")
            self.btn_clear.setText("🗑 Clear")
            if hasattr(self, "_header_layout"):
                self._header_layout.setSpacing(8)
                self._header_layout.setContentsMargins(10, 4, 10, 4)
        elif width >= 850:
            self._is_ultra_compact = False
            self._title_lbl.setText("Serial")
            self.btn_reset.setText("↺ Reset")
            self.btn_pause.setText("▶ Resume" if self._paused else "⏸ Pause")
            self.cb_autoscroll.setText("Auto")
            self.cb_auto_clear.setText("Clr Action")
            self.cb_ansi_clear.setText("ANSI")
            if hasattr(self, "_lbl_baud"):
                self._lbl_baud.setVisible(True)
                self._lbl_baud.setText("BAUD")
            self.baud_combo.setFixedWidth(baud_w)
            self.btn_copy.setText("⧉ Copy")
            self.btn_clear.setText("🗑 Clear")
            if hasattr(self, "_header_layout"):
                self._header_layout.setSpacing(5)
                self._header_layout.setContentsMargins(6, 4, 6, 4)
        else:
            self._is_ultra_compact = True
            self._title_lbl.setText("Serial")
            self.btn_reset.setText("↺")
            self.btn_pause.setText("▶" if self._paused else "⏸")
            self.cb_autoscroll.setText("Auto")
            self.cb_auto_clear.setText("Clr Act")
            self.cb_ansi_clear.setText("ANSI")
            if hasattr(self, "_lbl_baud"):
                self._lbl_baud.setVisible(False)
            self.baud_combo.setFixedWidth(baud_w)
            self.btn_copy.setText("⧉")
            self.btn_clear.setText("🗑")
            if hasattr(self, "_header_layout"):
                self._header_layout.setSpacing(4)
                self._header_layout.setContentsMargins(4, 4, 4, 4)

        self._reflow_header(width)
        compact_send = width < 420
        self._send_label.setVisible(not compact_send)
        self._send_layout.setContentsMargins(4 if compact_send else 10, 4, 4 if compact_send else 10, 4)
        self._send_layout.setSpacing(4 if compact_send else 8)
        short_panel = self.height() < 140
        self.btn_send.setFixedHeight(26 if short_panel else 30)
        self._send_bar.setFixedHeight(32 if short_panel else 40)
        self._send_layout.setContentsMargins(4 if compact_send else 10, 2 if short_panel else 4, 4 if compact_send else 10, 2 if short_panel else 4)

    def _sync_display_options(self) -> None:
        previous = self._wrap_action.blockSignals(True)
        self._wrap_action.setChecked(self._output._line_wrap_enabled)
        self._wrap_action.blockSignals(previous)
        for action, checkbox in self._display_option_actions:
            action.blockSignals(True)
            action.setChecked(checkbox.isChecked())
            action.blockSignals(False)

    def _reflow_header(self, width: int) -> None:
        h = self._header_layout
        first = (self._title_lbl, self.btn_reset, self.btn_pause, self.lbl_status, self.btn_copy, self.btn_clear)
        second = (self.cb_autoscroll, self.cb_auto_clear, self.cb_ansi_clear, self.baud_container)
        regular = (self._title_lbl, self.btn_reset, self.btn_pause, self.cb_autoscroll, self.cb_auto_clear,
                   self.cb_ansi_clear, self._div_sep, self.lbl_status, self._div2_sep,
                   self.baud_container, self.btn_copy, self.btn_clear)
        self.lbl_status.setText(self._connection_text)
        required = sum(w.minimumSizeHint().width() for w in regular) + 60
        row_required = max(sum(widget.minimumSizeHint().width() for widget in row) + h.spacing() * (len(row) - 1) for row in (first, second))
        mode = "single" if width >= required else "stacked" if width >= row_required + 16 else "micro"
        if self.height() < 140:
            mode = "short"
            h.setContentsMargins(4, 2, 4, 2)
        compact_options = mode in ("micro", "short")
        self.lbl_status.setText("●" if compact_options else self._connection_text)
        self.lbl_status.setToolTip(self._connection_text)
        self.lbl_status.setAccessibleName(self._connection_text)
        if mode != self._header_mode:
            for widget in (*regular, self._display_options):
                h.removeWidget(widget)
                for row in self._header_row_layouts:
                    row.removeWidget(widget)
            for row in self._header_row_layouts:
                h.removeItem(row)
            for col in range(len(regular)):
                h.setColumnStretch(col, 0)
            self._div_sep.setVisible(mode == "single")
            self._div2_sep.setVisible(mode == "single")
            self._display_options.setVisible(compact_options)
            self._title_lbl.setVisible(mode != "short")
            self.btn_copy.setVisible(mode != "short")
            self.btn_clear.setVisible(mode != "short")
            for checkbox in (self.cb_autoscroll, self.cb_auto_clear, self.cb_ansi_clear):
                checkbox.setVisible(not compact_options)
            if mode == "short":
                for col, widget in enumerate((self.btn_reset, self.btn_pause, self.lbl_status, self.baud_container, self._display_options)):
                    h.addWidget(widget, 0, col)
                h.setColumnStretch(3, 1)
            elif mode != "single":
                top, bottom = self._header_row_layouts
                for row in (top, bottom):
                    row.setContentsMargins(0, 0, 0, 0)
                    row.setSpacing(h.spacing())
                for widget in first:
                    top.addWidget(widget, 1 if widget is self.lbl_status else 0)
                for widget in ((self.baud_container, self._display_options) if mode == "micro" else second):
                    bottom.addWidget(widget, 1 if widget is self.baud_container else 0)
                h.addLayout(top, 0, 0, 1, len(regular))
                h.addLayout(bottom, 1, 0, 1, len(regular))
            else:
                for col, widget in enumerate(regular):
                    h.addWidget(widget, 0, col)
                h.setColumnStretch(3, 1)
            self._header_mode = mode
            self._header_stacked = mode not in ("single", "short")
        self._header.setFixedHeight(max(30 if mode == "short" else 36, h.sizeHint().height()))

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_reset(self) -> None:
        if self._backend:
            self.btn_reset.setEnabled(False)
            self.btn_reset.setCursor(Qt.CursorShape.ArrowCursor)
            def _restore_reset():
                if "Connected" in self._connection_text:
                    self.btn_reset.setEnabled(True)
                    self.btn_reset.setCursor(Qt.CursorShape.PointingHandCursor)
            QTimer.singleShot(1200, _restore_reset)
            self._backend.reset_mcu()

    def _on_pause_toggle(self, checked: bool) -> None:
        self._paused = checked
        self._output.set_paused(checked)
        if getattr(self, "_is_ultra_compact", False):
            self.btn_pause.setText("▶" if checked else "⏸")
        else:
            self.btn_pause.setText("▶ Resume" if checked else "⏸ Pause")

    def _on_baud_changed(self, baud_str: str) -> None:
        try:
            baud = int(baud_str)
            if self._backend:
                self._backend.set_baud_rate(baud)
        except (ValueError, Exception):
            pass

    def _copy_output(self) -> None:
        flush_serial = getattr(self._backend, "flush_serial_output", None)
        if callable(flush_serial):
            flush_serial()
        sig_bus = getattr(self, "_sig_bus", None)
        if sig_bus is not None and hasattr(sig_bus, "flush_pending_logs"):
            sig_bus.flush_pending_logs("serial")
        include_ts = self._output._timestamp_enabled if hasattr(self._output, "_timestamp_enabled") else False
        text = self._output.get_content_for_clipboard(include_timestamp=include_ts)
        self._ensure_copy_timers()
        self._cancel_copy_output()
        self._copy_snapshot = text
        self._copy_attempts = 0
        self._copy_active = True
        self.btn_copy.setText("⧉" if getattr(self, "_is_ultra_compact", False) else "Copying…")
        self.btn_copy.setToolTip("Copying retained serial output to the clipboard")
        self._attempt_copy_output(self._copy_generation)

    def _ensure_copy_timers(self) -> None:
        if hasattr(self, "_copy_retry_timer"):
            return
        self._copy_generation = 0
        self._copy_active = False
        self._copy_snapshot = ""
        self._copy_retry_timer = QTimer(self)
        self._copy_retry_timer.setSingleShot(True)
        self._copy_retry_timer.setInterval(30)
        self._copy_retry_timer.timeout.connect(self._retry_copy_output)
        self._copy_feedback_timer = QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(1500)
        self._copy_feedback_timer.timeout.connect(self._restore_copy_button)

    def _restore_copy_button(self) -> None:
        self.btn_copy.setText("⧉" if getattr(self, "_is_ultra_compact", False) else "⧉ Copy")
        self.btn_copy.setToolTip("Copy serial monitor output to clipboard")

    def _cancel_copy_output(self) -> None:
        """A newer header or selection copy owns the clipboard from now on."""
        if not hasattr(self, "_copy_retry_timer"):
            return
        self._copy_generation += 1
        self._copy_active = False
        self._copy_snapshot = ""
        self._copy_retry_timer.stop()
        self._copy_feedback_timer.stop()
        self._restore_copy_button()

    def _retry_copy_output(self) -> None:
        self._attempt_copy_output(self._copy_retry_generation)

    def _attempt_copy_output(self, generation: int) -> None:
        if not self._copy_active or generation != self._copy_generation:
            return
        self._copy_attempts += 1
        try:
            clipboard = QApplication.clipboard()
            clipboard.setText(self._copy_snapshot)
            accepted = clipboard.text() == self._copy_snapshot
        except RuntimeError:
            accepted = False
        if accepted:
            self._copy_active = False
            self._copy_snapshot = ""
            self._copy_retry_timer.stop()
            self.btn_copy.setText("✔" if getattr(self, "_is_ultra_compact", False) else "✔ Copied!")
            self.btn_copy.setToolTip("Retained serial output copied to clipboard")
            self._copy_feedback_timer.start()
        elif self._copy_attempts < 3:
            self._copy_retry_generation = generation
            self._copy_retry_timer.start()
        else:
            self._copy_active = False
            self._copy_snapshot = ""
            self._copy_retry_timer.stop()
            self.btn_copy.setText("⚠" if getattr(self, "_is_ultra_compact", False) else "Copy failed")
            self.btn_copy.setToolTip("The system clipboard did not accept the text. Try Copy again.")

    def _output_clear(self) -> None:
        clear_serial = getattr(self._backend, "clear_serial_output", None)
        if callable(clear_serial) and clear_serial() is True:
            return
        sig_bus = getattr(self, "_sig_bus", None)
        if sig_bus is not None and hasattr(sig_bus, "clear_log_queue"):
            sig_bus.clear_log_queue("serial")
        self._output.clear()

    def _on_timestamp_changed(self, state: int) -> None:
        is_checked = bool(state)
        from main.qt.preferences import save_display_preference
        if not save_display_preference("timestamp_enabled", is_checked, "Timestamps"):
            self.sync_timestamp(self._output._timestamp_enabled)
            return
        self._output.set_timestamp_enabled(is_checked)
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
        self._output.set_timestamp_enabled(checked)

    def _send_serial(self) -> None:
        text = self.input_field.text()
        if not text or not self._backend:
            return
        idx = self.line_ending_combo.currentIndex()
        ending_key = _LINE_ENDINGS[idx][1] if idx < len(_LINE_ENDINGS) else "both"
        self._backend.serial_send(text, ending_key)
        self.input_field.clear()

    def _on_auto_clear_changed(self, state: int) -> None:
        is_checked = self.cb_auto_clear.isChecked()
        from main.qt.preferences import save_display_preference, restore_checkbox
        if not save_display_preference("clear_serial_on_action", is_checked, "Clear serial monitor on action"):
            restore_checkbox(self.cb_auto_clear, not is_checked)
            return
        if self._backend is not None:
            self._backend.clear_serial_on_action = is_checked
        from main.qt.signals import signals
        if hasattr(signals, "clear_serial_on_action_changed"):
            signals.clear_serial_on_action_changed.emit(is_checked)

    def _on_autoscroll_changed(self, state: int) -> None:
        enabled = bool(state)
        from main.qt.preferences import save_display_preference, restore_checkbox
        if not save_display_preference("serial_autoscroll", enabled, "Serial auto-scroll"):
            restore_checkbox(self.cb_autoscroll, self._output._autoscroll)
            return
        self._output.set_autoscroll(enabled)

    def _on_line_wrap_changed(self, enabled: bool) -> None:
        self._output.request_line_wrap(enabled)
        self._sync_display_options()

    def _on_ansi_clear_changed(self, state: int) -> None:
        enabled = bool(state)
        from main.qt.preferences import save_display_preference, restore_checkbox
        if not save_display_preference("serial_ansi_clear", enabled, "Serial clear-screen"):
            restore_checkbox(self.cb_ansi_clear, self._output._ansi_clear_enabled)
            return
        self._output.set_ansi_clear_enabled(enabled)

    def sync_clear_serial_on_action(self, checked: bool) -> None:
        if self.cb_auto_clear.isChecked() != checked:
            self.cb_auto_clear.blockSignals(True)
            self.cb_auto_clear.setChecked(checked)
            self.cb_auto_clear.blockSignals(False)

    @Slot(dict)
    def update_status(self, payload: dict) -> None:
        """Handle serial:status signal."""
        generation = payload.get("generation")
        if generation is not None:
            previous = getattr(self, "_status_generation", None)
            if previous is not None and generation < previous:
                return
            self._status_generation = generation
        connected: bool = payload.get("connected", False)
        state: str = payload.get("state", "connected" if connected else "disconnected")
        is_conn = bool(connected or state == "connected")
        if is_conn:
            self.lbl_status.setText("● Connected")
            self._connection_color_tag = "success"
        elif state == "reconnecting":
            self.lbl_status.setText("● Reconnecting...")
            self._connection_color_tag = "warning"
        else:
            self.lbl_status.setText("● Disconnected")
            self._connection_color_tag = "error"
        self._apply_connection_color()
        self._connection_text = self.lbl_status.text()
        self._reflow_header(getattr(self, "_current_responsive_width", self.width()))

        self.btn_reset.setEnabled(is_conn)
        self.btn_reset.setCursor(Qt.CursorShape.PointingHandCursor if is_conn else Qt.CursorShape.ArrowCursor)

        self.btn_pause.setEnabled(is_conn)
        self.btn_pause.setCursor(Qt.CursorShape.PointingHandCursor if is_conn else Qt.CursorShape.ArrowCursor)
        if not is_conn and self._paused:
            self._paused = False
            self.btn_pause.setChecked(False)
            self.btn_pause.setText("⏸ Pause")
            self._output.set_paused(False)

        if hasattr(self, "btn_send") and self.btn_send:
            self.btn_send.setEnabled(is_conn)
            self.btn_send.setCursor(Qt.CursorShape.PointingHandCursor if is_conn else Qt.CursorShape.ArrowCursor)

        if hasattr(self, "input_field") and self.input_field:
            self.input_field.setEnabled(is_conn)

    def connect_signals(self, sig_bus, *, connect_theme: bool = True) -> None:
        """Connect to the MCUSignals bus."""
        self._sig_bus = sig_bus
        sig_bus.serial_log.connect(self._output.append_log)
        sig_bus.serial_status.connect(self.update_status)
        sig_bus.serial_clear.connect(self._output.clear)
        if hasattr(sig_bus, "clear_serial_on_action_changed"):
            sig_bus.clear_serial_on_action_changed.connect(self.sync_clear_serial_on_action)
        if hasattr(sig_bus, "timestamp_toggled"):
            sig_bus.timestamp_toggled.connect(self.sync_timestamp)
        if hasattr(sig_bus, "font_size_changed"):
            sig_bus.font_size_changed.connect(self._output.set_font_size)
        if connect_theme and hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.apply_theme)
        if self._backend and hasattr(self._backend, "timestamp_enabled"):
            self._output.set_timestamp_enabled(bool(self._backend.timestamp_enabled))

    def apply_theme(self, theme_name: str) -> None:
        """Update serial monitor child widgets to match active theme."""
        self._output.apply_theme(theme_name)
        self._theme_name = theme_name
        from main.qt.theme import get_palette
        pal = get_palette(theme_name)
        cyan = pal.get("CYAN", "#00d2ff")
        border = pal.get("BORDER", "#2d3748")
        if hasattr(self, "_title_lbl") and self._title_lbl:
            self._title_lbl.setStyleSheet(f"color: {cyan}; font-weight: bold; font-size: 12px;")
        self._apply_connection_color()
        if hasattr(self, "_div_sep") and self._div_sep:
            self._div_sep.setStyleSheet(f"color: {border};")

    def _apply_connection_color(self) -> None:
        from main.qt.log_colors import themed_log_colors
        colors = themed_log_colors(getattr(self, "_theme_name", "default"))
        color = colors.get(getattr(self, "_connection_color_tag", "error"), colors["normal"])
        self.lbl_status.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: 600;")
