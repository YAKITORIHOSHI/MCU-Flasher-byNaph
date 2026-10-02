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
import time
from collections import deque
from datetime import datetime

from PySide6.QtCore import QTimer, Slot, Qt, QEvent
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QPlainTextEdit,
    QPushButton, QCheckBox, QLabel, QLineEdit, QComboBox, QFrame,
    QApplication, QSizePolicy,
)
from main.qt.icons import ActionButton as QPushButton

_TAG_COLORS: dict[str, str] = {
    "info":    "#5ca4f0",
    "success": "#4ec994",
    "warning": "#f1c40f",
    "error":   "#e74c3c",
    "system":  "#56cfbf",
    "dim":     "#6b7280",
    "sent":    "#c678dd",
    "normal":  "#cdd6f4",
}
_DEFAULT_COLOR = "#cdd6f4"
_BG_COLOR      = "#0d1117"
_MONO_FONT     = QFont("Consolas", 11)
from main.core.constants import MAX_BAUD_RATE

_BAUD_RATES = [b for b in ["9600", "19200", "38400", "57600", "74880", "115200",
               "230400", "460800", "512000", "921600"] if int(b) <= MAX_BAUD_RATE]
_LINE_ENDINGS = [("None", "none"), ("\\n", "nl"), ("\\r", "cr"), ("\\r\\n", "both")]

_ANSI_CLEAR_RE = re.compile(r"\x1b\[2J|\x1b\[H|\x1b\[1;1H")
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class SerialOutputView(QPlainTextEdit):
    """Read-only high-performance serial monitor output view.

    Coalesces output with bounded pending/history buffers, small render batches
    and long-line limits. Sustained display overload produces a visible notice.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
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
        self._scrollbar_held = False
        self._user_scrolled_up = False

        sb = self.verticalScrollBar()
        sb.sliderPressed.connect(self._on_slider_pressed)
        sb.sliderReleased.connect(self._on_slider_released)
        sb.actionTriggered.connect(self._on_action_triggered)
        sb.installEventFilter(self)

        self._paused = False
        self._ansi_clear_enabled = True
        from main.core.config import load_gui_config
        self._timestamp_enabled = bool(load_gui_config().get("timestamp_enabled", False))
        self._entries = LogBuffer(self._history_limit, profile.terminal_scrollback, lambda item: item[0])
        self._queue = LogBuffer(256_000 if profile.constrained else 1_000_000, 2000, lambda item: item[0])
        self._last_autoscroll = 0.0

        # Batch drain timer (drains queue at ~30ms / ~33 FPS)
        self._flush_timer = QTimer(self)
        from src.modules.runtime_resources import performance_profile
        self._flush_timer.setInterval(40 if performance_profile().constrained else 30)
        self._flush_timer.timeout.connect(self._flush_queue)
        # Demand-driven: timer starts when logs arrive and stops when queue is drained

    def _on_slider_pressed(self) -> None:
        self._scrollbar_held = True

    def _on_slider_released(self) -> None:
        self._scrollbar_held = False
        self._update_user_scrolled_state()

    def _on_action_triggered(self, _action: int) -> None:
        self._update_user_scrolled_state()

    def _update_user_scrolled_state(self) -> None:
        sb = self.verticalScrollBar()
        if sb.maximum() - sb.value() <= 3:
            self._user_scrolled_up = False
        else:
            self._user_scrolled_up = True

    def _is_scroll_held(self) -> bool:
        sb = self.verticalScrollBar()
        if sb.isSliderDown():
            return True
        if getattr(self, "_scrollbar_held", False):
            return True
        return False

    def eventFilter(self, obj, event):
        if obj == self.verticalScrollBar():
            etype = event.type()
            if etype == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._scrollbar_held = True
            elif etype == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._scrollbar_held = False
                self._update_user_scrolled_state()
        return super().eventFilter(obj, event)

    def wheelEvent(self, event):
        super().wheelEvent(event)
        self._update_user_scrolled_state()

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
        """Apply active theme palette to serial output view base colors."""
        from main.qt.theme import get_palette
        pal = get_palette(theme_name)
        bg = pal.get("BG_DARKEST", "#0a0e14")
        fg = pal.get("TEXT", "#e0e6ed")
        palette = self.palette()
        palette.setColor(palette.ColorRole.Base, QColor(bg))
        palette.setColor(palette.ColorRole.Text, QColor(fg))
        self.setPalette(palette)

    def set_autoscroll(self, enabled: bool) -> None:
        self._autoscroll = enabled
        if enabled:
            self._user_scrolled_up = False
            sb = self.verticalScrollBar()
            sb.setValue(sb.maximum())

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    def set_ansi_clear_enabled(self, enabled: bool) -> None:
        self._ansi_clear_enabled = enabled

    def set_timestamp_enabled(self, enabled: bool) -> None:
        if self._timestamp_enabled == enabled:
            return
        self._timestamp_enabled = enabled
        self._rebuild_document()

    def get_content_for_clipboard(self, include_timestamp: bool | None = None) -> str:
        """Return serial monitor text formatted for clipboard copying.

        If include_timestamp is False, any timestamps ([HH:MM:SS]) are strictly
        stripped from all lines, guaranteeing clean output when the toggle is off.
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
        sb = self.verticalScrollBar()
        saved_val = sb.value()
        saved_max = sb.maximum()
        was_at_bottom = (saved_val >= saved_max - 15)

        cursor = self.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()

        coalesced_chunks: list[tuple[str, str]] = []
        curr_pieces: list[str] = []
        curr_tag: str = ""

        def _flush_chunk():
            if curr_pieces:
                coalesced_chunks.append(("".join(curr_pieces), curr_tag))
                curr_pieces.clear()

        for clean_text, tag, is_newline, batch_ts in self._entries:
            if not clean_text and not is_newline:
                continue
            line_tag = tag or "normal"
            if self._timestamp_enabled and clean_text:
                if curr_tag != "timestamp":
                    _flush_chunk()
                    curr_tag = "timestamp"
                curr_pieces.append(f"[{batch_ts}] ")

            payload = clean_text + ("\n" if is_newline else "")
            if payload:
                if line_tag != curr_tag:
                    _flush_chunk()
                    curr_tag = line_tag
                curr_pieces.append(payload)

        _flush_chunk()

        for text_chunk, tag in coalesced_chunks:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(_TAG_COLORS.get(tag, _DEFAULT_COLOR)))
            cursor.insertText(text_chunk, fmt)

        cursor.endEditBlock()
        from main.qt.log_buffer import trim_document
        trim_document(self, self._history_limit)

        if (self._autoscroll and not self._is_scroll_held() and not self._user_scrolled_up) or was_at_bottom:
            self.setTextCursor(cursor)
            sb.setValue(sb.maximum())
        else:
            sb.setValue(saved_val)

    @Slot(dict)
    def append_log(self, payload: dict) -> None:
        if self._paused:
            return
        tag: str  = payload.get("tag", "normal")
        lines = payload.get("lines")
        from main.qt.log_buffer import display_text
        if lines and isinstance(lines, list):
            for l in lines:
                self._queue.append((display_text(l), tag, True))
            if not self._flush_timer.isActive():
                self._flush_timer.start()
            return

        text: str = display_text(payload.get("text", ""))
        newline: bool = payload.get("newline", True)
        self._queue.append((text, tag, newline))
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_queue(self) -> None:
        if not self._queue:
            if self._flush_timer.isActive():
                self._flush_timer.stop()
            return

        items = self._queue.drain(max_items=128, max_chars=32768)
        notice = self._queue.take_notice()
        if notice:
            items.insert(0, (notice, "warning", True))

        if not items:
            return

        # Coalesce adjacent lines with identical tags into chunks
        # This turns hundreds of individual Qt text insertions into 1-3 bulk calls
        batch_ts = datetime.now().strftime("%H:%M:%S")
        coalesced_chunks: list[tuple[str, str]] = []
        curr_pieces: list[str] = []
        curr_tag: str = ""

        def _flush_chunk():
            if curr_pieces:
                coalesced_chunks.append(("".join(curr_pieces), curr_tag))
                curr_pieces.clear()

        for clean_text, tag, is_newline in items:
            if "\x1b" in clean_text:
                if self._ansi_clear_enabled and _ANSI_CLEAR_RE.search(clean_text):
                    curr_pieces.clear()
                    coalesced_chunks.clear()
                    self._entries.clear()
                    super().clear()
                    clean_text = _ANSI_CLEAR_RE.sub("", clean_text)
                clean_text = _ANSI_CSI_RE.sub("", clean_text)

            if not clean_text and not is_newline:
                continue

            line_tag = tag or "normal"
            if self._timestamp_enabled and clean_text:
                if curr_tag != "timestamp":
                    _flush_chunk()
                    curr_tag = "timestamp"
                curr_pieces.append(f"[{batch_ts}] ")

            payload = clean_text + ("\n" if is_newline else "")
            if payload:
                if line_tag != curr_tag:
                    _flush_chunk()
                    curr_tag = line_tag
                curr_pieces.append(payload)

            self._entries.append((clean_text, tag, is_newline, batch_ts))

        _flush_chunk()

        if not coalesced_chunks:
            return

        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        try:
            for text_chunk, tag in coalesced_chunks:
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(_TAG_COLORS.get(tag, _DEFAULT_COLOR)))
                cursor.insertText(text_chunk, fmt)
        finally:
            cursor.endEditBlock()
        from main.qt.log_buffer import trim_document
        trim_document(self, self._history_limit)
        if self._autoscroll and not self._is_scroll_held() and not self._user_scrolled_up:
            self.setTextCursor(cursor)
            now = time.monotonic()
            if now - self._last_autoscroll >= 0.06:
                self._last_autoscroll = now
                self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

        if not self._queue and self._flush_timer.isActive():
            self._flush_timer.stop()

    @Slot()
    def clear(self) -> None:
        self._entries.clear()
        self._queue.clear()
        self._user_scrolled_up = False
        self._scrollbar_held = False
        if self._flush_timer.isActive():
            self._flush_timer.stop()
        super().clear()


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

        # Autoscroll
        self.cb_autoscroll = QCheckBox("Auto-scroll")
        self.cb_autoscroll.setChecked(True)
        self.cb_autoscroll.setToolTip("Auto-scroll output to newest received line")
        self.cb_autoscroll.stateChanged.connect(
            lambda s: self._output.set_autoscroll(bool(s))
        )
        h.addWidget(self.cb_autoscroll, 0, 3)

        # Clear on Action checkbox
        from main.core.config import load_gui_config
        cfg = load_gui_config()
        init_clear_serial = bool(cfg.get("clear_serial_on_action", False))
        self.cb_auto_clear = QCheckBox("Clear on Action")
        self.cb_auto_clear.setChecked(init_clear_serial)
        self.cb_auto_clear.setToolTip("Clear serial monitor before each compile/upload/reset action")
        self.cb_auto_clear.stateChanged.connect(self._on_auto_clear_changed)
        h.addWidget(self.cb_auto_clear, 0, 4)

        # ANSI clear-screen checkbox
        self.cb_ansi_clear = QCheckBox("Clear-screen")
        self.cb_ansi_clear.setChecked(True)
        self.cb_ansi_clear.setToolTip("Honour ANSI terminal clear-screen sequences from MCU output")
        self.cb_ansi_clear.stateChanged.connect(
            lambda s: self._output.set_ansi_clear_enabled(bool(s))
        )
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
        self.lbl_status.setStyleSheet(
            "color: #4ec994; font-size: 12px; font-weight: 600;"
            if initial_connected else
            "color: #e74c3c; font-size: 12px; font-weight: 600;"
        )
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
        # ── Separator ────────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #2a5f58;")

        # ── Output View ───────────────────────────────────────────────────────
        self._output = SerialOutputView()
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

        sb.addWidget(QLabel("Send >"))

        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Type command and press Enter…")
        self.input_field.returnPressed.connect(self._send_serial)
        self.input_field.setEnabled(initial_connected)
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

    def _reflow_header(self, width: int) -> None:
        h = self._header_layout
        first = (self._title_lbl, self.btn_reset, self.btn_pause, self.lbl_status, self.btn_copy, self.btn_clear)
        second = (self.cb_autoscroll, self.cb_auto_clear, self.cb_ansi_clear, self.baud_container)
        regular = (self._title_lbl, self.btn_reset, self.btn_pause, self.cb_autoscroll, self.cb_auto_clear,
                   self.cb_ansi_clear, self._div_sep, self.lbl_status, self._div2_sep,
                   self.baud_container, self.btn_copy, self.btn_clear)
        required = sum(w.minimumSizeHint().width() for w in regular) + 60
        stacked = width < required
        if stacked != self._header_stacked:
            for widget in regular:
                h.removeWidget(widget)
            for col in range(len(regular)):
                h.setColumnStretch(col, 0)
            self._div_sep.setVisible(not stacked)
            self._div2_sep.setVisible(not stacked)
            if stacked:
                for col, widget in enumerate(first):
                    h.addWidget(widget, 0, col)
                for col, widget in enumerate(second):
                    h.addWidget(widget, 1, col, 1, 3 if col == 3 else 1)
                h.setColumnStretch(3, 1)
            else:
                for col, widget in enumerate(regular):
                    h.addWidget(widget, 0, col)
                h.setColumnStretch(3, 1)
            self._header_stacked = stacked
        self._header.setFixedHeight(max(36, h.sizeHint().height()))

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_reset(self) -> None:
        if self._backend:
            self.btn_reset.setEnabled(False)
            self.btn_reset.setCursor(Qt.CursorShape.ArrowCursor)
            def _restore_reset():
                if "Connected" in self.lbl_status.text():
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
        include_ts = self._output._timestamp_enabled if hasattr(self._output, "_timestamp_enabled") else False
        text = self._output.get_content_for_clipboard(include_timestamp=include_ts)
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        self.btn_copy.setText("✔ Copied!")
        def _restore_copy_btn():
            self.btn_copy.setText("⧉" if getattr(self, "_is_ultra_compact", False) else "⧉ Copy")
        QTimer.singleShot(1500, _restore_copy_btn)

    def _output_clear(self) -> None:
        self._output._entries.clear()
        self._output.clear()

    def _on_timestamp_changed(self, state: int) -> None:
        is_checked = bool(state)
        self._output.set_timestamp_enabled(is_checked)
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
        from main.core.config import load_gui_config, save_gui_config
        cfg = load_gui_config()
        cfg["clear_serial_on_action"] = is_checked
        save_gui_config(cfg)
        if self._backend is not None:
            self._backend.clear_serial_on_action = is_checked
        from main.qt.signals import signals
        if hasattr(signals, "clear_serial_on_action_changed"):
            signals.clear_serial_on_action_changed.emit(is_checked)

    def sync_clear_serial_on_action(self, checked: bool) -> None:
        if self.cb_auto_clear.isChecked() != checked:
            self.cb_auto_clear.blockSignals(True)
            self.cb_auto_clear.setChecked(checked)
            self.cb_auto_clear.blockSignals(False)

    @Slot(dict)
    def update_status(self, payload: dict) -> None:
        """Handle serial:status signal."""
        connected: bool = payload.get("connected", False)
        state: str = payload.get("state", "connected" if connected else "disconnected")
        is_conn = bool(connected or state == "connected")
        if is_conn:
            self.lbl_status.setText("● Connected")
            self.lbl_status.setStyleSheet("color: #4ec994; font-size: 12px; font-weight: 600;")
        elif state == "reconnecting":
            self.lbl_status.setText("● Reconnecting...")
            self.lbl_status.setStyleSheet("color: #f1c40f; font-size: 12px; font-weight: 600;")
        else:
            self.lbl_status.setText("● Disconnected")
            self.lbl_status.setStyleSheet("color: #e74c3c; font-size: 12px; font-weight: 600;")

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

    def connect_signals(self, sig_bus) -> None:
        """Connect to the MCUSignals bus."""
        sig_bus.serial_log.connect(self._output.append_log)
        sig_bus.serial_status.connect(self.update_status)
        sig_bus.serial_clear.connect(self._output.clear)
        if hasattr(sig_bus, "clear_serial_on_action_changed"):
            sig_bus.clear_serial_on_action_changed.connect(self.sync_clear_serial_on_action)
        if hasattr(sig_bus, "timestamp_toggled"):
            sig_bus.timestamp_toggled.connect(self.sync_timestamp)
        if hasattr(sig_bus, "font_size_changed"):
            sig_bus.font_size_changed.connect(self._output.set_font_size)
        if hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.apply_theme)
        if self._backend and hasattr(self._backend, "timestamp_enabled"):
            self._output.set_timestamp_enabled(bool(self._backend.timestamp_enabled))

    def apply_theme(self, theme_name: str) -> None:
        """Update serial monitor child widgets to match active theme."""
        self._output.apply_theme(theme_name)
        from main.qt.theme import get_palette
        pal = get_palette(theme_name)
        cyan = pal.get("CYAN", "#00d2ff")
        border = pal.get("BORDER", "#2d3748")
        if hasattr(self, "_title_lbl") and self._title_lbl:
            self._title_lbl.setStyleSheet(f"color: {cyan}; font-weight: bold; font-size: 12px;")
        if hasattr(self, "_div_sep") and self._div_sep:
            self._div_sep.setStyleSheet(f"color: {border};")
