#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.notif_panel — Notifications history panel for MCU Flasher by Naph.

Displays persisted and live notifications with category filtering,
clipboard copying, and database clearing.
"""
from __future__ import annotations

from typing import Optional
from html import escape
from os.path import abspath, normcase
from pathlib import Path
from collections import Counter
import threading

# pyrefly: ignore [missing-import]
from PySide6.QtCore import Slot, QTimer, Signal, Qt
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QTextBrowser, QFrame, QApplication, QSizePolicy
)
from main.qt.icons import ActionButton as QPushButton
from main.qt.log_follow import LogFollow, preserve_log_view
from main.qt.log_buffer import LogBuffer, display_text
from main.qt.log_colors import contrast_ratio, themed_log_colors

from src.dbs import dbs_read, dbs_delete


class NotifPanel(QWidget):
    """
    Bottom dock tab displaying project notifications and hardware/build events.
    """

    _refresh_finished = Signal(dict)

    def __init__(self, backend=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._backend = backend
        path = str(getattr(backend, "sketch_dir_path", "") or "")
        self._project_path = normcase(abspath(path)) if path else ""
        self._records = LogBuffer(256_000, 500, lambda record: "".join(record.values()))
        self._pending_live = LogBuffer(256_000, 500, lambda record: "".join(record.values()))
        self._persisted_counts = Counter()
        self._refresh_generation = 0
        self._refresh_running = False
        self._refresh_pending = False
        self._refresh_finished.connect(self._finish_refresh, Qt.ConnectionType.QueuedConnection)
        self._empty_message = "No notifications found for this filter."
        self._clear_error = None
        self._refresh_error = None
        self._build_ui()
        self._follow = LogFollow(self._browser)
        from main.core.config import get_theme_mode
        self.apply_theme(get_theme_mode())
        self._load_notifications()

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
        self._browser.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self._browser.setMinimumHeight(24)
        self._browser.setUndoRedoEnabled(False)
        self._browser.document().setMaximumBlockCount(1000)
        self._browser.setObjectName("notif-browser")
        self._browser.setOpenExternalLinks(False)
        layout.addWidget(self._browser)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._on_tab_revealed()

    def _on_tab_revealed(self) -> None:
        """Discover notifications written by other processes without blocking Qt."""
        self._request_refresh()

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

    def connect_signals(self, sig_bus, *, connect_theme=True) -> None:
        """Connect to global Qt signal bus for live notifications."""
        sig_bus.notification.connect(self._on_live_notification)
        if hasattr(sig_bus, "project_updated"):
            sig_bus.project_updated.connect(self._on_project_updated)
        if connect_theme and hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.apply_theme)

    def apply_theme(self, theme_name: str) -> None:
        """Apply active theme palette to notifications panel."""
        from main.qt.theme import get_palette
        pal = get_palette(theme_name)
        cyan = pal.get("CYAN", "#00d2ff")
        bg = pal.get("BG_DARKEST", "#0a0e14")
        fg = pal.get("TEXT", "#e0e6ed")
        self._palette = pal
        reading_background = min((bg, pal["BG_DARK"], pal["BG_MID"]),
                                 key=lambda background: contrast_ratio(fg, background))
        self._card_colors = themed_log_colors(theme_name, background=reading_background)
        cyan = self._card_colors["system"]
        self._lbl_filter.setStyleSheet(f"color: {self._card_colors['dim']}; font-size: 11px;")
        for button in (self._btn_clear, self._btn_copy):
            button.setStyleSheet(
                f"QPushButton {{ background: {pal['BG_DARK']}; color: {fg}; "
                f"border: 1px solid {pal['BORDER']}; "
                "font-size: 11px; padding: 1px 8px; border-radius: 3px; }"
                f"QPushButton:hover {{ background: {pal['BG_HOVER']}; color: {pal['TEXT_BRIGHT']}; }}"
            )
        if hasattr(self, "_title_lbl") and self._title_lbl:
            self._title_lbl.setStyleSheet(f"color: {cyan}; font-weight: 600; font-size: 11px;")
        if hasattr(self, "_browser") and self._browser:
            self._browser.setStyleSheet(
                f"QTextBrowser {{ background: {bg}; color: {fg}; border: none; font-family: 'Consolas', 'Courier New', monospace; font-size: 12px; padding: 8px; }}"
                f"QTextBrowser {{ selection-background-color: {pal['BG_HOVER']}; selection-color: {pal['TEXT_BRIGHT']}; }}"
            )
            self._render_records()

    @Slot(dict)
    def _on_live_notification(self, payload: dict) -> None:
        """Handle incoming live notification emitted by backend."""
        self._append_notification_card(payload)

    def _on_filter_changed(self, category_label: str) -> None:
        self._clear_error = None
        self._browser.clear()
        self._follow.reset()
        self._render_records()

    @Slot(dict)
    def _on_project_updated(self, payload: dict) -> None:
        path = str(payload.get("path", "") or "")
        path = normcase(abspath(path)) if path else ""
        if path and path != self._project_path:
            self._project_path = path
            self._load_notifications()

    def _load_notifications(self) -> None:
        """Reset a project history and schedule its first bounded read."""
        self._browser.clear()
        self._refresh_generation += 1
        self._records.clear()
        self._pending_live.clear()
        self._persisted_counts.clear()
        self._clear_error = None
        self._refresh_error = None
        self._follow.reset()
        self._empty_message = "No notifications found for this filter."
        self._render_records()
        self._request_refresh()

    def _notification_db_path(self) -> str:
        if self._project_path:
            from main.core.constants import PROJECT_BUILD_CACHE_DIR
            return str(Path(self._project_path) / PROJECT_BUILD_CACHE_DIR / "dbs_notif.json")
        from src.dbs.dbs_create import get_default_db_path
        return get_default_db_path()

    def _request_refresh(self) -> None:
        if self._refresh_running:
            self._refresh_pending = True
            return
        self._refresh_running = True
        generation, project = self._refresh_generation, self._project_path
        database = self._notification_db_path()
        reader = dbs_read.get_notifications

        def read_history():
            result = {"generation": generation, "project": project, "database": database}
            try:
                records = reader(limit=500, db_path=database)
                bounded = LogBuffer(256_000, 500, lambda record: "".join(record.values()))
                # Database queries are newest first; retention is oldest first.
                for record in reversed(records):
                    if isinstance(record, dict):
                        bounded.append(NotifPanel._display_record(record))
                result["records"] = list(bounded)
            except Exception as exc:
                result["error"] = str(exc)
            try:
                self._refresh_finished.emit(result)
            except RuntimeError:
                pass  # A closed panel never receives a worker's widget updates.

        try:
            threading.Thread(target=read_history, name="MCU_NotificationReader", daemon=True).start()
        except (RuntimeError, OSError) as exc:
            self._refresh_running = False
            self._refresh_pending = False
            self._refresh_error = self._display_record({
                "level": "warning", "title": "Notifications could not be refreshed", "message": str(exc),
            })
            self._render_records()

    @staticmethod
    def _record_key(record: dict) -> tuple[str, ...]:
        return tuple(record[key] for key in ("category", "level", "title", "message"))

    @Slot(dict)
    def _finish_refresh(self, result: dict) -> None:
        self._refresh_running = False
        pending, self._refresh_pending = self._refresh_pending, False
        current = (result["generation"] == self._refresh_generation and result["project"] == self._project_path
                   and result["database"] == self._notification_db_path())
        if current and "error" not in result:
            records = result["records"]
            # New persisted occurrences acknowledge live events, including repeated
            # identical messages. Existing historical matches cannot erase a new
            # live warning whose persistence failed.
            current_counts = Counter((record["id"], self._record_key(record)) for record in records)
            available = Counter(current_counts)
            acknowledged = Counter()
            for (_identifier, key), count in (current_counts - self._persisted_counts).items():
                acknowledged[key] += count
            live = list(self._pending_live)
            self._pending_live.clear()
            for record in live:
                key = self._record_key(record)
                signature = (record["id"], key)
                if record["id"] and available[signature]:
                    available[signature] -= 1
                    if acknowledged[key]:
                        acknowledged[key] -= 1
                elif not record["id"] and acknowledged[key]:
                    acknowledged[key] -= 1
                else:
                    self._pending_live.append(record)
            self._persisted_counts = current_counts
            self._refresh_error = None
            self._records.clear()
            for record in records:
                self._records.append(record)
            for record in self._pending_live:
                self._records.append(record)
            self._render_records()
        elif current:
            self._refresh_error = self._display_record({
                "level": "warning", "title": "Notifications could not be refreshed", "message": result["error"],
            })
            self._render_records()
        if pending:
            self._request_refresh()

    @staticmethod
    def _display_record(record: dict) -> dict:
        return {
            "id": display_text(record.get("id", ""), 128),
            "level": display_text(record.get("level", "info"), 64).lower(),
            "category": display_text(record.get("category", "system"), 64).lower(),
            "title": display_text(record.get("title", ""), 1024),
            "message": display_text(record.get("message", "")),
            "date": display_text(record.get("date", ""), 64),
            "time": display_text(record.get("time", ""), 64),
        }

    def _matches_filter(self, record: dict) -> bool:
        label = self._filter_combo.currentText()
        if label == "📦 Boards & Libraries":
            return record["category"] in ("board_install", "library_install")
        if label == "🔌 USB Devices":
            return record["category"] in ("usb", "device")
        if label == "✖ Errors":
            return record["level"].lower() == "error"
        return True

    def _visible_records(self) -> list[dict]:
        return [record for record in self._records if self._matches_filter(record)][-150:]

    @preserve_log_view(rebuild=True)
    def _render_records(self) -> None:
        """Recolor bounded displayed history without reloading the database."""
        records = self._visible_records()
        if self._clear_error is not None:
            records.append(self._clear_error)
        if self._refresh_error is not None:
            records.append(self._refresh_error)
        if records:
            self._browser.setHtml("".join(self._format_record_html(record) for record in records))
        else:
            self._browser.setHtml(
                f"<div style='color: {self._card_colors['dim']}; font-style: italic; padding: 12px;'>"
                f"{escape(self._empty_message)}</div>"
            )

    def _format_record_html(self, r: dict) -> str:
        lvl = str(r.get("level", "info")).lower()
        color = self._card_colors.get(lvl, self._card_colors["info"])
        badge = escape(lvl.upper())
        title = escape(display_text(r.get("title", ""), 1024))
        message = escape(display_text(r.get("message", "")))
        date_str = r.get("date", "")
        time_str = r.get("time", "")
        timestamp = escape(f"{date_str} {time_str}".strip())

        return (
            f"<div style='margin-bottom: 8px; padding: 6px 10px; background: {self._palette['BG_DARK']}; border-left: 3px solid {color}; border-radius: 4px;'>"
            f"  <div style='display: flex; justify-content: space-between; margin-bottom: 2px;'>"
            f"    <span style='font-weight: 700; color: {color};'>[{badge}] {title}</span>"
            f"    <span style='color: {self._card_colors['dim']}; font-size: 10px; float: right;'>{timestamp}</span>"
            f"  </div>"
            f"  <div style='color: {self._card_colors['normal']}; margin-top: 3px; white-space: pre-wrap;'>{message}</div>"
            f"</div>"
        )

    def _append_notification_card(self, payload: dict) -> None:
        """Append one live notification directly to the bottom of the display."""
        title = payload.get("title", "Notification")
        msg = payload.get("history_message", payload.get("message", ""))
        ntype = payload.get("type", "info")
        record = self._display_record({
            "id": payload.get("id", ""),
            "level": ntype,
            "category": payload.get("category", "system"),
            "title": title,
            "message": msg,
            "time": "Just now",
        })
        if record["id"] and self._persisted_counts[(record["id"], self._record_key(record))]:
            return  # This exact event was discovered before its live signal arrived.
        previous_dropped = self._records.dropped
        visible_count = len(self._visible_records())
        self._pending_live.append(record)
        self._records.append(record)
        self._empty_message = "No notifications found for this filter."
        if not self._matches_filter(record):
            if self._records.dropped != previous_dropped:
                self._render_records()
            return
        if (not visible_count or visible_count >= 150 or self._records.dropped != previous_dropped
                or self._clear_error is not None or self._refresh_error is not None):
            self._render_records()
        else:
            self._append_card_html(record)

    @preserve_log_view()
    def _append_card_html(self, record: dict) -> None:
        card_html = self._format_record_html(record)
        self._browser.append(card_html)

    def _clear_notifications(self) -> None:
        """Clear database records and reset UI view."""
        try:
            if not dbs_delete.clear_all_notifications(db_path=self._notification_db_path()):
                raise OSError("The notification database could not be updated. Try again.")
        except Exception as exc:
            self._clear_error = self._display_record({
                "level": "error", "title": "Notifications could not be cleared", "message": str(exc),
            })
            self._render_records()
            return
        self._records.clear()
        self._pending_live.clear()
        self._persisted_counts.clear()
        self._refresh_generation += 1
        self._refresh_pending = False
        self._clear_error = None
        self._refresh_error = None
        self._empty_message = "All notifications cleared."
        self._browser.clear()
        self._follow.reset()
        self._render_records()

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
