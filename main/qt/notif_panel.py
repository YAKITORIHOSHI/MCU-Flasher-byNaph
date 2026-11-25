#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.notif_panel — Notifications history panel for MCU Flasher by Naph.

Displays persisted and live notifications with category filtering,
clipboard copying, and database clearing.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional
from os.path import abspath, normcase
from pathlib import Path
from collections import Counter
import threading

# pyrefly: ignore [missing-import]
from PySide6.QtCore import Slot, QTimer, Signal, Qt
# pyrefly: ignore [missing-import]
from PySide6.QtGui import QFont
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QFrame, QApplication, QSizePolicy
)
from main.qt.card_browser import Card, CardBrowser
from main.qt.icons import ActionButton as QPushButton, icon
from main.qt.log_follow import LogFollow, preserve_log_view
from main.qt.log_buffer import LogBuffer, display_text
from main.core.log_colors import glass_inks
from main.qt.log_colors import themed_log_colors

from src.dbs import dbs_read, dbs_delete


from main.qt.icons import IconLabel as QLabel

VISIBLE_LIMIT = 150
PREVIEW_LINES, PREVIEW_CHARS = 2, 180
DETAIL_CHARS = 1500
CATEGORY_LABELS = {
    "system": "Workspace", "usb": "USB device", "device": "USB device", "serial": "Serial Monitor",
    "board_install": "Board support", "library_install": "Library",
}
CATEGORY_MARKS = {"usb": "usb", "device": "usb", "serial": "serial", "board_install": "devices", "library_install": "package"}
LEVEL_MARKS = {"error": "error", "warning": "warning", "success": "check", "info": "info"}


def _detail_text(details) -> str:
    """Bounded "key: value" lines from a notification's own details."""
    lines = []
    for key, value in details.items() if isinstance(details, dict) else ():
        if key in ("job_id", "coverage_report") or value in (None, "", [], {}):
            continue
        label = str(key).replace("_", " ").strip().capitalize()[:60]
        lines.append(f"{label}: {display_text(value, 400)}".replace("\n", " "))
    return "\n".join(lines)[:DETAIL_CHARS]


class NotifPanel(QWidget):
    """
    Bottom dock tab displaying project notifications and hardware/build events.

    Newest first, one expandable card each: the arrow (or the title, or Enter) shows
    the full message, when it happened and what it came with.
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
        self._render_needed = False
        self._card_colors = {}
        self._build_ui()
        # Newest is on top, so nothing follows the bottom; the anchor keeps a reader
        # scrolled into older cards where they are while new ones arrive above.
        self._follow = LogFollow(self._browser, enabled=False)
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_if_needed)
        self._browser.set_before_read(self._render_if_needed)
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

        lbl = QLabel("Notifications", icon_name="alerts")
        self._title_lbl = lbl
        hl.addWidget(lbl)

        lbl_filter = QLabel("Filter:")
        self._lbl_filter = lbl_filter
        hl.addWidget(lbl_filter)

        self._filter_combo = QComboBox()
        self._filter_combo.addItems(["All", "Boards & Libraries", "USB Devices", "Errors", "Warnings"])
        self._filter_combo.setFixedWidth(160)
        self._filter_combo.setStyleSheet("QComboBox { font-size: 11px; padding: 2px 6px; }")
        self._filter_combo.currentTextChanged.connect(self._on_filter_changed)
        hl.addWidget(self._filter_combo)

        # How many problems are in the list, without opening a single card.
        self._error_count = QLabel("", icon_name="error")
        self._warning_count = QLabel("", icon_name="warning")
        for label in (self._error_count, self._warning_count):
            label.hide()
            hl.addWidget(label)
        hl.addStretch()

        self._btn_expand = QPushButton("Expand all", icon_name="details")
        self._btn_expand.setToolTip("Show every card's details")
        self._btn_expand.clicked.connect(self._toggle_all)
        self._btn_copy = QPushButton("Copy", icon_name="copy")
        self._btn_copy.setToolTip("Copy the listed notifications with their details")
        self._btn_copy.clicked.connect(self._copy_notifications)
        self._btn_clear = QPushButton("Clear", icon_name="clear")
        self._btn_clear.setToolTip("Clear this sketch's notifications")
        self._btn_clear.clicked.connect(self._clear_notifications)
        for button in (self._btn_expand, self._btn_copy, self._btn_clear):
            button.setFixedHeight(24)
            hl.addWidget(button)
        layout.addWidget(header)

        # ── Notification View ─────────────────────────────────────────────────
        self._browser = CardBrowser()
        self._browser.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self._browser.setMinimumHeight(24)
        self._browser.setObjectName("notif-browser")
        self._browser.setFont(QFont("Montserrat", 10))
        self._browser.setAccessibleName("Notifications, newest first")
        self._browser.anchorClicked.connect(self._on_report_link)
        self._browser.toggled.connect(self._on_card_toggled)
        layout.addWidget(self._browser)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._render_if_needed()
        self._on_tab_revealed()

    def _on_tab_revealed(self) -> None:
        """Discover notifications written by other processes without blocking Qt."""
        self._request_refresh()

    def set_responsive_width(self, width: int) -> None:
        """Dynamically adapt notifications header based on available width."""
        self._current_responsive_width = width
        wide, medium = width >= 1100, width >= 850
        self._is_ultra_compact = not medium
        self._title_lbl.set_icon("alerts")
        self._title_lbl.setText("Notifications" if wide else "Alerts" if medium else "")
        self._lbl_filter.setVisible(wide)
        self._filter_combo.setFixedWidth(160 if wide else 130 if medium else 95)
        for button, name, text in ((self._btn_expand, "details", self._expand_label()),
                                   (self._btn_copy, "copy", "Copy"), (self._btn_clear, "clear", "Clear")):
            button.set_icon(name)
            button.setText(text if medium else "")
            button.setAccessibleName(text)
        self._header_layout.setSpacing(8 if wide else 5 if medium else 4)
        self._show_counts()

    def connect_signals(self, sig_bus, *, connect_theme=True) -> None:
        """Connect to global Qt signal bus for live notifications."""
        sig_bus.notification.connect(self._on_live_notification)
        if hasattr(sig_bus, "project_updated"):
            sig_bus.project_updated.connect(self._on_project_updated)
        if connect_theme and hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.apply_theme)

    def apply_theme(self, theme_name: str) -> None:
        """Apply active theme palette to notifications panel."""
        from main.qt.theme import get_palette, glass_tokens
        pal = get_palette(theme_name)
        glass = glass_tokens(theme_name)
        fg = pal.get("TEXT", "#e0e6ed")
        self._palette = pal
        for index, name in enumerate(("filter", "package", "usb", "error", "warning")):
            self._filter_combo.setItemIcon(index, icon(name, pal["TEXT"]))
        self._card_colors = themed_log_colors(theme_name)
        # The header sits straight on the sheet, the cards in the smoked well below it.
        header = glass_inks(theme_name)
        self._header_inks = header
        self._lbl_filter.setStyleSheet(f"color: {header['normal']}; font-size: 11px;")
        for button in (self._btn_expand, self._btn_clear, self._btn_copy):
            button.setStyleSheet("QPushButton { font-size: 11px; padding: 1px 8px; }")
        self._title_lbl.setStyleSheet(f"color: {header['system']}; font-weight: 600; font-size: 11px;")
        self._error_count.setStyleSheet(f"color: {header['error']}; font-size: 11px; font-weight: 600;")
        self._warning_count.setStyleSheet(f"color: {header['warning']}; font-size: 11px; font-weight: 600;")
        self._browser.setStyleSheet(
            f"QTextBrowser {{ background: {glass['reading']}; color: {fg}; border: 1px solid {glass['reading_rim']}; border-radius: 10px; padding: 2px; font-family: Montserrat; }}"
            f"QTextBrowser {{ selection-background-color: {glass['selection']}; selection-color: {pal['TEXT_BRIGHT']}; }}"
        )
        self._browser.set_theme(self._card_colors, theme_name)
        self._request_render()

    @Slot(dict)
    def _on_live_notification(self, payload: dict) -> None:
        """Handle incoming live notification emitted by backend."""
        self._append_notification_card(payload)

    def _on_filter_changed(self, category_label: str) -> None:
        self._clear_error = None
        self._follow.reset()
        self._render_records()
        self._browser.verticalScrollBar().setValue(0)

    @Slot(dict)
    def _on_project_updated(self, payload: dict) -> None:
        path = str(payload.get("path", "") or "")
        path = normcase(abspath(path)) if path else ""
        if path and path != self._project_path:
            self._project_path = path
            self._load_notifications()

    def _load_notifications(self) -> None:
        """Reset a project history and schedule its first bounded read."""
        self._browser.set_expanded(())
        self._refresh_generation += 1
        self._records.clear()
        self._pending_live.clear()
        self._persisted_counts.clear()
        self._clear_error = None
        self._refresh_error = None
        self._follow.reset()
        self._empty_message = "No notifications found for this filter."
        self._request_render()
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
            self._request_render()

    @staticmethod
    def _record_key(record: dict) -> tuple[str, ...]:
        return tuple(record[key] for key in ("category", "level", "title", "message")) + (record.get("job_id", ""),)

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
            self._request_render()
        elif current:
            self._refresh_error = self._display_record({
                "level": "warning", "title": "Notifications could not be refreshed", "message": result["error"],
            })
            self._request_render()
        if pending:
            self._request_refresh()

    @staticmethod
    def _display_record(record: dict) -> dict:
        details = record.get("details")
        details = details if isinstance(details, dict) else {}
        return {
            "id": display_text(record.get("id", ""), 128),
            "level": display_text(record.get("level", "info"), 64).lower(),
            "category": display_text(record.get("category", "system"), 64).lower(),
            "title": display_text(record.get("title", ""), 1024),
            "message": display_text(record.get("message", "")),
            "date": display_text(record.get("date", ""), 64),
            "time": display_text(record.get("time", ""), 64),
            # Keep display-buffer values as strings. The report viewer uses
            # the job identity and reconstructs its app-owned path in a worker.
            "job_id": display_text(record.get("job_id") or details.get("job_id", ""), 80),
            "coverage_report": display_text(record.get("coverage_report") or details.get("coverage_report", ""), 4096),
            # The display keeps short readable lines, never the original details object.
            "detail_text": _detail_text(details),
        }

    def _matches_filter(self, record: dict) -> bool:
        label = self._filter_combo.currentText()
        if label == "Boards & Libraries":
            return record["category"] in ("board_install", "library_install")
        if label == "USB Devices":
            return record["category"] in ("usb", "device")
        if label == "Errors":
            return record["level"] == "error"
        if label == "Warnings":
            return record["level"] == "warning"
        return True

    def _visible_records(self) -> list[dict]:
        """Matching records in the order they happened; the newest is last."""
        return [record for record in self._records if self._matches_filter(record)][-VISIBLE_LIMIT:]

    def _request_render(self) -> None:
        """Coalesce rebuilds, and do none of them while the tab is hidden."""
        self._render_needed = True
        if self.isVisible():
            self._render_timer.start(0)

    def _render_if_needed(self) -> None:
        if self._render_needed:
            self._render_records()

    def _render_records(self) -> None:
        self._render_needed = False
        self._render_timer.stop()
        bar = self._browser.verticalScrollBar()
        at_top = bar.value() <= 2 and not bar.isSliderDown()
        self._rebuild()
        if at_top:
            bar.setValue(0)
        self._show_counts()

    @preserve_log_view(rebuild=True)
    def _rebuild(self) -> None:
        """Recolor bounded displayed history without reloading the database."""
        records = self._visible_records()
        notices = [record for record in (self._clear_error, self._refresh_error) if record is not None]
        cards = self._cards_for(records + notices)
        self._browser.set_cards(cards, message=self._empty_message)

    def _cards_for(self, records) -> list[Card]:
        """Cards for records in the order they happened, newest first."""
        seen, cards = Counter(), []
        for record in records:
            # Repeated identical events stay separate cards with their own state.
            key = record["id"] or "\x1f".join(record.values())
            cards.append(self._card_for(record, f"{key}#{seen[key]}"))
            seen[key] += 1
        cards.reverse()
        return cards

    @staticmethod
    def _when(record) -> str:
        stamp, day = record.get("time", ""), record.get("date", "")
        try:
            value = date.fromisoformat(day)
        except ValueError:
            return f"{day} {stamp}".strip()
        today = date.today()
        if value == today:
            return f"Today {stamp}".strip()
        if value == today - timedelta(days=1):
            return f"Yesterday {stamp}".strip()
        return f"{value:%b} {value.day}{'' if value.year == today.year else f', {value.year}'}  {stamp}".strip()

    def _card_for(self, record: dict, key: str = "") -> Card:
        from main.qt.package_coverage import JOB_ID
        level = record["level"] if record["level"] in LEVEL_MARKS else "info"
        category = record["category"]
        label = CATEGORY_LABELS.get(category, category.replace("_", " ").capitalize() or "Workspace")
        when = self._when(record)
        message = [(line, "normal") for line in record["message"].splitlines() or [""]]
        preview = []
        for line, tag in message[:PREVIEW_LINES]:
            preview.append((line if len(line) <= PREVIEW_CHARS else line[:PREVIEW_CHARS].rstrip() + "…", tag))
        if len(message) > PREVIEW_LINES:
            last, tag = preview[-1]
            preview[-1] = (last if last.endswith("…") else last + " …", tag)
        details = [("Level", level.capitalize()), ("From", label)]
        if when:
            details.append(("When", f"{record['date']} {record['time']}".strip()))
        for line in filter(None, record["detail_text"].splitlines()):
            name, _, value = line.partition(": ")
            details.append((name, value) if value else ("Detail", name))
        job_id, links = record["job_id"], []
        if record["coverage_report"] and JOB_ID.fullmatch(job_id):
            links.append(("View all board results", f"mcu-coverage:{job_id}"))
        return Card(
            key=key or record["id"], title=record["title"] or "Notification", accent=level,
            mark=LEVEL_MARKS[level] if level in ("error", "warning") else CATEGORY_MARKS.get(category, LEVEL_MARKS[level]),
            meta="   ".join(part for part in (label, when) if part),
            body=message, preview=preview, details=details, links=links)

    def _show_counts(self) -> None:
        records = list(self._records)
        for label, level, noun in ((self._error_count, "error", "error"), (self._warning_count, "warning", "warning")):
            count = sum(record["level"] == level for record in records)
            label.setText(f"{count} {noun}{'' if count == 1 else 's'}" if getattr(self, "_current_responsive_width", 1200) >= 1000 else str(count))
            label.setToolTip(f"{count} {noun}{'' if count == 1 else 's'} in this list")
            label.setVisible(bool(count) and getattr(self, "_current_responsive_width", 1200) >= 460)
        self._btn_expand.setEnabled(bool(self._browser.cards()))

    # ── Expanding ─────────────────────────────────────────────────────────────
    def _all_expanded(self) -> bool:
        cards = self._browser.cards()
        return bool(cards) and all(self._browser.is_expanded(card.key) for card in cards)

    def _expand_label(self) -> str:
        return "Collapse all" if self._all_expanded() else "Expand all"

    def _toggle_all(self) -> None:
        self._render_if_needed()
        keys = () if self._all_expanded() else [card.key for card in self._browser.cards()]
        self._browser.set_expanded(keys)
        self._on_card_toggled()

    def _on_card_toggled(self, *_args) -> None:
        self._render_records()
        label = self._expand_label()
        self._btn_expand.setAccessibleName(label)
        if not getattr(self, "_is_ultra_compact", False):
            self._btn_expand.set_icon("details")
            self._btn_expand.setText(label)

    def _on_report_link(self, url) -> None:
        from main.qt.package_coverage import JOB_ID, open_package_coverage
        job_id = url.path()
        if url.scheme() == "mcu-coverage" and not url.query() and not url.fragment() and JOB_ID.fullmatch(job_id):
            open_package_coverage(self.window(), job_id)

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
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": datetime.now().strftime("%H:%M:%S"),
            "details": payload.get("details", {}),
            "job_id": payload.get("job_id", ""),
        })
        if record["id"] and self._persisted_counts[(record["id"], self._record_key(record))]:
            return  # This exact event was discovered before its live signal arrived.
        previous_dropped = self._records.dropped
        visible_count = len(self._visible_records())
        self._pending_live.append(record)
        self._records.append(record)
        self._empty_message = "No notifications found for this filter."
        if (not self.isVisible() or self._render_needed or not visible_count
                or self._records.dropped != previous_dropped
                or self._clear_error is not None or self._refresh_error is not None):
            self._request_render()
            return
        if self._matches_filter(record):
            self._prepend(record)
        self._show_counts()

    def _prepend(self, record: dict) -> None:
        """Add the newest card above the rest without moving what is being read."""
        card = self._cards_for(self._visible_records())[0]
        bar = self._browser.verticalScrollBar()
        at_top = bar.value() <= 2 and not bar.isSliderDown()
        self._insert_newest(card)
        if at_top:
            bar.setValue(0)

    @preserve_log_view()
    def _insert_newest(self, card: Card) -> None:
        self._browser.prepend_card(card, limit=VISIBLE_LIMIT)

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
        self._browser.set_expanded(())
        self._follow.reset()
        self._render_records()

    def _copy_notifications(self) -> None:
        """Copy the listed notifications, newest first, with their details."""
        self._render_if_needed()
        blocks = []
        for card in self._browser.cards():
            lines = [card.title, *(text for text, _tag in card.body)]
            lines += [f"{label}: {value}" for label, value in card.details]
            blocks.append("\n".join(line for line in lines if line))
        cb = QApplication.clipboard()
        if cb:
            cb.setText("\n\n".join(blocks) or self._empty_message)
            self._btn_copy.set_icon("check")
            self._btn_copy.setText("" if getattr(self, "_is_ultra_compact", False) else "Copied")
            def _restore_btn():
                self._btn_copy.set_icon("copy")
                self._btn_copy.setText("" if getattr(self, "_is_ultra_compact", False) else "Copy")
            QTimer.singleShot(1500, self, _restore_btn)
