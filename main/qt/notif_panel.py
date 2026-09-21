#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.notif_panel — Notifications history panel for MCU Flasher by Naph.

Displays persisted and live notifications with category filtering,
clipboard copying, and database clearing.
"""
from __future__ import annotations

from typing import Optional

# pyrefly: ignore [missing-import]
from PySide6.QtCore import Slot, QTimer
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QTextBrowser, QFrame, QApplication
)

from src.dbs import dbs_read, dbs_delete


class NotifPanel(QWidget):
    """
    Bottom dock tab displaying project notifications and hardware/build events.
    """

    def __init__(self, backend=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._backend = backend
        self._build_ui()
        from main.core.config import get_theme_mode
        self.apply_theme(get_theme_mode())
        self._load_notifications("All")

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header Bar ────────────────────────────────────────────────────────
        header = QFrame()
        header.setObjectName("notif-header")
        header.setFixedHeight(34)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 2, 10, 2)
        hl.setSpacing(8)
        self._header_layout = hl

        lbl = QLabel("🔔 Notifications")
        self._title_lbl = lbl
        hl.addWidget(lbl)

        lbl_filter = QLabel("Filter:")
        self._lbl_filter = lbl_filter
        lbl_filter.setStyleSheet("color: #94a3b8; font-size: 11px;")
        hl.addWidget(lbl_filter)

        self._filter_combo = QComboBox()
        self._filter_combo.addItems(["All", "📦 Boards & Libraries", "🔌 USB Devices", "✖ Errors"])
        self._filter_combo.setFixedWidth(160)
        self._filter_combo.setStyleSheet("QComboBox { font-size: 11px; padding: 2px 6px; }")
        self._filter_combo.currentTextChanged.connect(self._on_filter_changed)
        hl.addWidget(self._filter_combo)

        btn_clear = QPushButton("Clear")
        btn_clear.setFixedHeight(22)
        btn_clear.setToolTip("Clear all notifications from database")
        btn_clear.setStyleSheet(
            "QPushButton { font-size: 11px; padding: 1px 8px; background: #2d3748; color: #cdd6f4; border-radius: 3px; }"
            "QPushButton:hover { background: #3b4261; }"
        )
        btn_clear.clicked.connect(self._clear_notifications)
        self._btn_clear = btn_clear
        hl.addWidget(btn_clear)

        self._btn_copy = QPushButton("Copy")
        self._btn_copy.setFixedHeight(22)
        self._btn_copy.setToolTip("Copy notifications log to clipboard")
        self._btn_copy.setStyleSheet(
            "QPushButton { font-size: 11px; padding: 1px 8px; background: #2d3748; color: #cdd6f4; border-radius: 3px; }"
            "QPushButton:hover { background: #3b4261; }"
        )
        self._btn_copy.clicked.connect(self._copy_notifications)
        hl.addWidget(self._btn_copy)

        hl.addStretch()
        layout.addWidget(header)

        # ── Notification View ─────────────────────────────────────────────────
        self._browser = QTextBrowser()
        self._browser.setObjectName("notif-browser")
        self._browser.setOpenExternalLinks(False)
        layout.addWidget(self._browser)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())

    def set_responsive_width(self, width: int) -> None:
        """Dynamically adapt notifications header based on available width."""
        self._current_responsive_width = width
        if width >= 1100:
            self._is_ultra_compact = False
            self._title_lbl.setText("🔔 Notifications")
            self._lbl_filter.setVisible(True)
            self._filter_combo.setFixedWidth(160)
            self._btn_clear.setText("Clear")
            self._btn_copy.setText("Copy")
            if hasattr(self, "_header_layout"):
                self._header_layout.setSpacing(8)
        elif width >= 850:
            self._is_ultra_compact = False
            self._title_lbl.setText("🔔 Alerts")
            self._lbl_filter.setVisible(False)
            self._filter_combo.setFixedWidth(130)
            self._btn_clear.setText("Clear")
            self._btn_copy.setText("Copy")
            if hasattr(self, "_header_layout"):
                self._header_layout.setSpacing(5)
        else:
            self._is_ultra_compact = True
            self._title_lbl.setText("🔔")
            self._lbl_filter.setVisible(False)
            self._filter_combo.setFixedWidth(95)
            self._btn_clear.setText("🗑")
            self._btn_copy.setText("⧉")
            if hasattr(self, "_header_layout"):
                self._header_layout.setSpacing(4)

    def connect_signals(self, sig_bus) -> None:
        """Connect to global Qt signal bus for live notifications."""
        sig_bus.notification.connect(self._on_live_notification)
        if hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.apply_theme)

    def apply_theme(self, theme_name: str) -> None:
        """Apply active theme palette to notifications panel."""
        from main.qt.theme import get_palette
        pal = get_palette(theme_name)
        cyan = pal.get("CYAN", "#00d2ff")
        bg = pal.get("BG_DARKEST", "#0a0e14")
        fg = pal.get("TEXT", "#e0e6ed")
        if hasattr(self, "_title_lbl") and self._title_lbl:
            self._title_lbl.setStyleSheet(f"color: {cyan}; font-weight: 600; font-size: 11px;")
        if hasattr(self, "_browser") and self._browser:
            self._browser.setStyleSheet(
                f"QTextBrowser {{ background: {bg}; color: {fg}; border: none; font-family: 'Consolas', 'Courier New', monospace; font-size: 12px; padding: 8px; }}"
            )

    @Slot(dict)
    def _on_live_notification(self, payload: dict) -> None:
        """Handle incoming live notification emitted by backend."""
        self._append_notification_card(payload)

    def _on_filter_changed(self, category_label: str) -> None:
        self._load_notifications(category_label)

    def _load_notifications(self, category_label: str) -> None:
        """Fetch matching notifications from database and render them."""
        self._browser.clear()
        category_map = {
            "All": None,
            "📦 Boards & Libraries": "board_install",
            "🔌 USB Devices": "usb",
            "✖ Errors": "error",
        }
        target_cat = category_map.get(category_label)
        try:
            records = dbs_read.get_notifications(
                category=target_cat if target_cat != "error" else None,
                level="error" if target_cat == "error" else None,
                limit=150,
            )
        except Exception:
            records = []

        if not records:
            self._browser.setHtml(
                "<div style='color: #64748b; font-style: italic; padding: 12px;'>No notifications found for this filter.</div>"
            )
            return

        html_blocks = []
        for r in records:
            html_blocks.append(self._format_record_html(r))

        self._browser.setHtml("".join(html_blocks))

    def _format_record_html(self, r: dict) -> str:
        lvl = str(r.get("level", "info")).lower()
        color_map = {
            "error": "#f7768e",
            "warning": "#e0af68",
            "success": "#9ece6a",
            "info": "#7dcfff",
        }
        color = color_map.get(lvl, "#7aa2f7")
        badge = lvl.upper()
        title = r.get("title", "")
        message = r.get("message", "")
        date_str = r.get("date", "")
        time_str = r.get("time", "")
        timestamp = f"{date_str} {time_str}".strip()

        return (
            f"<div style='margin-bottom: 8px; padding: 6px 10px; background: #1a1e2a; border-left: 3px solid {color}; border-radius: 4px;'>"
            f"  <div style='display: flex; justify-content: space-between; margin-bottom: 2px;'>"
            f"    <span style='font-weight: 700; color: {color};'>[{badge}] {title}</span>"
            f"    <span style='color: #64748b; font-size: 10px; float: right;'>{timestamp}</span>"
            f"  </div>"
            f"  <div style='color: #c0caf5; margin-top: 3px; white-space: pre-wrap;'>{message}</div>"
            f"</div>"
        )

    def _append_notification_card(self, payload: dict) -> None:
        """Append one live notification directly to the bottom of the display."""
        title = payload.get("title", "Notice")
        msg = payload.get("message", "")
        ntype = payload.get("type", "info")
        record = {
            "level": ntype,
            "title": title,
            "message": msg,
            "time": "Just now",
        }
        card_html = self._format_record_html(record)
        self._browser.append(card_html)

    def _clear_notifications(self) -> None:
        """Clear database records and reset UI view."""
        try:
            dbs_delete.clear_all_notifications()
        except Exception:
            pass
        self._browser.setHtml(
            "<div style='color: #64748b; font-style: italic; padding: 12px;'>🗑 All notifications cleared.</div>"
        )

    def _copy_notifications(self) -> None:
        """Copy text of all displayed notifications to system clipboard."""
        text = self._browser.toPlainText()
        cb = QApplication.clipboard()
        if cb:
            cb.setText(text)
            self._btn_copy.setText("✔ Copied")
            def _restore_btn():
                self._btn_copy.setText("⧉" if getattr(self, "_is_ultra_compact", False) else "Copy")
            QTimer.singleShot(1500, _restore_btn)
