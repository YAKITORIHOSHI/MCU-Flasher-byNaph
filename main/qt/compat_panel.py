#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.compat_panel — Compatible Devices panel for MCU Flasher by Naph.

Replaces the Tkinter compat_text (tk.Text) widget with a QPlainTextEdit
plus a search/filter bar matching the original Tkinter layout.
"""
from __future__ import annotations

# pyrefly: ignore [missing-import]
from PySide6.QtCore import QTimer
# pyrefly: ignore [missing-import]
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QPushButton, QLabel, QLineEdit, QFrame,
)

_TAG_COLORS: dict[str, str] = {
    "system":  "#56cfbf",
    "success": "#4ec994",
    "error":   "#e74c3c",
    "warning": "#f1c40f",
    "dim":     "#6b7280",
    "normal":  "#cdd6f4",
}
_DEFAULT_COLOR = "#cdd6f4"
_BG_COLOR      = "#0d1117"
_MONO_FONT     = QFont("Consolas", 11)


class CompatPanel(QWidget):
    """
    Compatible Devices panel with header, search bar, and text view.
    Mirrors the Tkinter compat_frame / compat_text structure.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._full_text: list[tuple[str, str]] = []   # [(text, tag), ...]
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ───────────────────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("compat-header")
        h = QHBoxLayout(header)
        h.setContentsMargins(10, 4, 10, 4)
        h.setSpacing(8)
        self._header_layout = h

        self.lbl_status = QLabel("Please compile to see the list of compatible devices")
        self.lbl_status.setProperty("role", "dim")
        h.addWidget(self.lbl_status, stretch=1)

        self.btn_copy = QPushButton("⧉ Copy")
        self.btn_copy.setFixedHeight(26)
        self.btn_copy.setToolTip("Copy compatible devices list to clipboard")
        self.btn_copy.clicked.connect(self._copy_output)
        h.addWidget(self.btn_copy)

        # ── Search bar ────────────────────────────────────────────────────────
        search_bar = QWidget()
        search_bar.setObjectName("compat-search-bar")
        search_bar.setFixedHeight(34)
        sb = QHBoxLayout(search_bar)
        sb.setContentsMargins(10, 2, 10, 2)
        sb.setSpacing(6)
        self._search_layout = sb

        sb.addWidget(QLabel("🔍"))

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter compatible devices…")
        self.search_input.textChanged.connect(self._apply_filter)
        sb.addWidget(self.search_input, stretch=1)

        btn_clear_search = QPushButton("✕ Clear")
        btn_clear_search.setFixedHeight(24)
        btn_clear_search.setToolTip("Clear search filter")
        btn_clear_search.clicked.connect(lambda: self.search_input.clear())
        self.btn_clear_search = btn_clear_search
        sb.addWidget(btn_clear_search)

        # ── Separator ─────────────────────────────────────────────────────────
        sep = QFrame()
        self._sep = sep
        sep.setFrameShape(QFrame.Shape.HLine)

        # ── Output View ───────────────────────────────────────────────────────
        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setFont(_MONO_FONT)
        self._output.setObjectName("compat-console")

        root.addWidget(header)
        root.addWidget(search_bar)
        root.addWidget(sep)
        root.addWidget(self._output, stretch=1)

        from main.core.config import get_theme_mode
        self.apply_theme(get_theme_mode())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())

    def set_responsive_width(self, width: int) -> None:
        """Dynamically adapt header status text and search buttons based on width."""
        self._current_responsive_width = width
        if width >= 1100:
            self._is_ultra_compact = False
            if not self._full_text:
                self.lbl_status.setText("Please compile to see the list of compatible devices")
            self.btn_copy.setText("⧉ Copy")
            self.btn_clear_search.setText("✕ Clear")
        elif width >= 850:
            self._is_ultra_compact = False
            if not self._full_text:
                self.lbl_status.setText("Compile to see compatible devices")
            self.btn_copy.setText("⧉ Copy")
            self.btn_clear_search.setText("✕")
        else:
            self._is_ultra_compact = True
            if not self._full_text:
                self.lbl_status.setText("Compatible devices")
            self.btn_copy.setText("⧉")
            self.btn_clear_search.setText("✕")

    def set_content(self, entries: list[tuple[str, str]]) -> None:
        """Set the full compatible devices content as (text, tag) pairs."""
        self._full_text = entries
        self._apply_filter()

    def _apply_filter(self) -> None:
        query = self.search_input.text().strip().lower()
        self._output.clear()
        cursor = self._output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        matched = 0
        for text, tag in self._full_text:
            if query and query not in text.lower():
                continue
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(_TAG_COLORS.get(tag, _DEFAULT_COLOR)))
            cursor.insertText(text, fmt)
            cursor.insertText("\n")
            matched += 1

        if query and matched == 0:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor("#6b7280"))
            cursor.insertText(f"No devices match '{query}'", fmt)

    def set_status(self, text: str) -> None:
        self.lbl_status.setText(text)

    def clear(self) -> None:
        self._full_text.clear()
        self._output.clear()
        self.lbl_status.setText("Please compile to see the list of compatible devices")
        self.search_input.clear()

    def _copy_output(self) -> None:
        # pyrefly: ignore [missing-import]
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self._output.toPlainText())
        self.btn_copy.setText("✔ Copied!")
        def _restore_btn():
            self.btn_copy.setText("⧉" if getattr(self, "_is_ultra_compact", False) else "⧉ Copy")
        QTimer.singleShot(1500, _restore_btn)

    def connect_signals(self, sig_bus) -> None:
        """Connect to the MCUSignals bus."""
        if hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.apply_theme)
        if hasattr(sig_bus, "compat_devices_updated"):
            sig_bus.compat_devices_updated.connect(self._on_compat_updated)

    def _on_compat_updated(self, payload: dict) -> None:
        """Handle incoming compatible devices update from backend."""
        if not isinstance(payload, dict):
            return
        lines = payload.get("lines", [])
        status = payload.get("status", "")
        entries = [
            (item[0], item[1]) if isinstance(item, (list, tuple)) and len(item) >= 2 else (str(item), "normal")
            for item in lines
        ]
        self.set_content(entries)
        if status:
            self.set_status(status)

    def apply_theme(self, theme_name: str) -> None:
        """Apply active theme palette to compatible devices panel."""
        from main.qt.theme import get_palette
        pal = get_palette(theme_name)
        bg = pal.get("BG_DARKEST", "#0a0e14")
        fg = pal.get("TEXT", "#e0e6ed")
        border = pal.get("BORDER", "#2d3748")
        palette = self._output.palette()
        palette.setColor(palette.ColorRole.Base, QColor(bg))
        palette.setColor(palette.ColorRole.Text, QColor(fg))
        self._output.setPalette(palette)
        if hasattr(self, "_sep") and self._sep:
            self._sep.setStyleSheet(f"color: {border};")
