#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.compat_panel — Compatible Devices panel for MCU Flasher by Naph.

One header row (filter, result, Copy) above one card per board family. Cards show
their first boards and open to every match; the text stays selectable and copyable.
"""
from __future__ import annotations

# pyrefly: ignore [missing-import]
from PySide6.QtCore import QSize, Qt, QTimer
# pyrefly: ignore [missing-import]
from PySide6.QtGui import QFont
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QFrame, QSizePolicy,
)
from main.core.log_colors import glass_inks
from main.qt.card_browser import Card, CardBrowser
from main.qt.icons import ActionButton as QPushButton, IconLabel as QLabel
from main.qt.log_colors import themed_log_colors
from main.qt.log_follow import LogFollow, preserve_log_view

EMPTY_STATUS = "Compile to see compatible devices"
FAMILY_ORDER = ("ESP32-S3", "ESP32-C6", "ESP32-C3", "ESP32-S2", "ESP32-C2", "ESP32", "ESP32 (other)",
                "ESP8266", "ESP8266 (other)", "Arduino AVR", "Uno")
COLUMN_WIDTH = 250
PREVIEW_BOARDS = 6
PREVIEW_REASONS = 3


class _ElidedStatus(QLabel):
    """The result text: shortens to its space instead of widening the row."""

    def __init__(self, text: str):
        super().__init__(text)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self._elide()

    def full_text(self) -> str:
        return self._full

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, max(0, self.width() - 4))
        if self.text() != shown:
            self.setText(shown)


class CompatPanel(QWidget):
    """Compatible Devices: a one-line header over expandable board-family cards."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._full_text: list[tuple[str, str]] = []   # [(text, tag), ...] without structured data
        self._data: dict | None = None
        self._opened: dict[str, bool] = {}   # Families the reader opened or closed.
        self._columns = 0
        self._tag_colors: dict[str, str] = {}
        self._render_needed = False
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header: filter, result and Copy on one line ───────────────────────
        header = QWidget()
        header.setObjectName("compat-header")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        header.setFixedHeight(36)
        row = QHBoxLayout(header)
        row.setContentsMargins(10, 4, 10, 4)
        row.setSpacing(8)
        self._header_layout = row
        self._search_icon = QLabel("", icon_name="search")
        row.addWidget(self._search_icon)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter compatible devices…")
        self.search_input.setAccessibleName("Filter compatible devices by name, chip or family")
        self.search_input.setMinimumWidth(70)
        self.search_input.setMaximumWidth(460)
        self.search_input.textChanged.connect(self._apply_filter)
        row.addWidget(self.search_input, stretch=3)
        self.btn_clear_search = QPushButton("Clear", icon_name="close")
        self.btn_clear_search.setToolTip("Clear search filter")
        self.btn_clear_search.clicked.connect(self.search_input.clear)
        self.btn_clear_search.setEnabled(False)
        row.addWidget(self.btn_clear_search)
        self.lbl_status = _ElidedStatus(EMPTY_STATUS)
        self.lbl_status.setProperty("role", "dim")
        row.addWidget(self.lbl_status, stretch=2)
        self.btn_copy = QPushButton("Copy", icon_name="copy")
        self.btn_copy.setToolTip("Copy compatible devices list to clipboard")
        self.btn_copy.clicked.connect(self._copy_output)
        row.addWidget(self.btn_copy)
        for button in (self.btn_clear_search, self.btn_copy):
            button.setFixedHeight(26)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("separator")

        # ── Family cards ──────────────────────────────────────────────────────
        self._output = CardBrowser()
        self._output.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self._output.setMinimumHeight(24)
        self._output.setObjectName("compat-console")
        self._output.setFont(QFont("Montserrat", 10))
        self._output.setAccessibleName("Compatible devices by board family")
        self._output.toggled.connect(self._on_card_toggled)
        self._output.set_before_read(self._render_if_needed)
        self._follow = LogFollow(self._output, enabled=False)
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_if_needed)

        root.addWidget(header)
        root.addWidget(sep)
        root.addWidget(self._output, stretch=1)

        from main.core.config import get_theme_mode
        self.apply_theme(get_theme_mode())

    # ── Layout ────────────────────────────────────────────────────────────────
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._render_if_needed()

    def set_responsive_width(self, width: int) -> None:
        """Shorten the header's labels, and refit the board columns."""
        self._current_responsive_width = width
        wide = width >= 700
        self._is_ultra_compact = not wide
        self.btn_copy.set_icon("copy")
        self.btn_copy.setText("Copy" if wide else "")
        self.btn_clear_search.set_icon("close")
        self.btn_clear_search.setText("Clear" if width >= 850 else "")
        self.lbl_status.setVisible(width >= 420)
        self._search_icon.setVisible(width >= 360)
        if self._fit_columns() != self._columns and self._data:
            self._request_render()

    def _fit_columns(self) -> int:
        return max(1, min(6, (self.width() - 90) // COLUMN_WIDTH))

    # ── Content ───────────────────────────────────────────────────────────────
    def set_content(self, entries: list[tuple[str, str]]) -> None:
        """Show plain (text, tag) lines when there is no structured result."""
        self._data = None
        self._full_text = list(entries)
        self._render()

    def set_data(self, data: dict) -> None:
        """Show a structured result from the compatibility analysis."""
        self._full_text = []
        self._data = data
        self._request_render()

    def _apply_filter(self, _query: str | None = None) -> None:
        self.btn_clear_search.setEnabled(bool(self.search_input.text()))
        self._render()

    def _request_render(self) -> None:
        self._render_needed = True
        if self.isVisible():
            self._render_timer.start(0)

    def _render_if_needed(self) -> None:
        if self._render_needed:
            self._render()

    def _render(self) -> None:
        self._render_needed = False
        self._render_timer.stop()
        self._columns = self._fit_columns()
        self._rebuild()

    @preserve_log_view(rebuild=True)
    def _rebuild(self) -> None:
        query = self.search_input.text().strip().lower()
        cards, message = (self._data_cards(query) if self._data else self._line_cards(query))
        # A filter opens what it found; otherwise the reader's own choices stand.
        self._output.set_expanded(card.key for card in cards
                                  if card.expandable and self._opened.get(card.key, bool(query) or card.key == self._default_open))
        self._output.set_cards(cards, message=message)

    _default_open = ""

    def _line_cards(self, query):
        lines = [(text.strip(), tag) for text, tag in self._full_text
                 if text.strip() and (not query or query in text.lower())]
        if not lines:
            return [], f"No devices match '{query}'" if query and self._full_text else ""
        (title, tag), body = lines[0], lines[1:]
        return [Card("lines", title, accent=tag, mark="devices", body=body)], ""

    def _data_cards(self, query):
        data, state = self._data or {}, (self._data or {}).get("state")
        if state == "empty":
            return [Card("empty", "Compile to see compatible devices", accent="info", mark="compile",
                         body=[("The list is built when this project is compiled. Choose Compile or Upload.", "normal")])], ""
        if state == "analyzing":
            return [Card("analyzing", "Checking this sketch", accent="info", mark="time",
                         body=[("Comparing its headers, APIs, pins and memory with every board.", "normal")])], ""
        if state == "error":
            return [Card("error", "Compatibility could not be checked", accent="error", mark="error",
                         body=[(str(data.get("message") or "Unknown error"), "normal")])], ""
        boards = data.get("boards") or []
        selected = data.get("selected") or ""
        matches = [board for board in boards if not query or query in board["search"]]
        families: dict[str, list] = {}
        for board in matches:
            families.setdefault(board["family"], []).append(board)
        self._default_open = next((f"family:{board['family']}" for board in boards if board["name"] == selected), "")
        cards = [self._summary_card(data, len(boards))] if not query else []
        order = {name: index for index, name in enumerate(FAMILY_ORDER)}
        for family in sorted(families, key=lambda name: (order.get(name, len(order)), name.lower())):
            cards.append(self._family_card(family, families[family], selected))
        if not cards:
            return [], f"No devices match '{query}'"
        return cards, ""

    def _summary_card(self, data, count: int) -> Card:
        total, stale = int(data.get("total") or 0), bool(data.get("stale"))
        reasons = [str(reason) for reason in data.get("reasons") or []]
        accent = "warning" if stale else "success" if count else "error"
        body = []
        if stale:
            body.append(("The sketch changed since this list was made. Compile again to refresh it.", "warning"))
        elif not reasons and count == total and total:
            body.append(("No board-specific code found, so this sketch should build for every board.", "normal"))
        rows = body + [(f"Excluded: {reason}" if index == 0 else reason, "warning") for index, reason in enumerate(reasons)]
        preview = rows[:len(body) + PREVIEW_REASONS]
        if len(rows) > len(preview):
            hidden = len(rows) - len(preview)
            preview = preview + [(f"and {hidden} more reason{'' if hidden == 1 else 's'}", "dim")]
        updated = str(data.get("updated_at") or "")
        return Card(
            "summary", f"{count} of {total} boards can run this sketch", accent=accent,
            mark="check" if accent == "success" else accent,
            meta="   ".join(part for part in (str(data.get("sketch") or ""), f"Checked at {updated}" if updated else "") if part),
            body=rows, preview=preview,
            details=[("How this is checked", "headers, API calls, GPIO range, flash size and PSRAM"),
                     ("Limits", "an estimate, not a guarantee: the board must also expose the pins the sketch uses")])

    def _family_card(self, family: str, boards: list, selected: str) -> Card:
        rows = []
        for board in boards:
            chosen = board["name"] == selected
            row = [(board["name"], "system" if chosen else "normal")]
            if board.get("mcu"):
                row.append((f"  {board['mcu']}", "dim"))
            if chosen:
                row.append(("  selected", "success"))
            rows.append(row)
        shown = [board["name"] for board in boards[:PREVIEW_BOARDS]]
        rest = len(boards) - len(shown)
        preview = [(",  ".join(shown) + (f"  and {rest} more" if rest else ""), "normal")]
        count = f"{len(boards)} board{'' if len(boards) == 1 else 's'}"
        return Card(f"family:{family}", family, accent="system", mark="devices", meta=count,
                    body=rows, preview=preview, columns=min(self._columns, max(1, len(rows))))

    def _on_card_toggled(self, key: str, expanded: bool) -> None:
        self._opened[key] = expanded
        self._render()

    @preserve_log_view(rebuild=True)
    def _recolor_content(self) -> None:
        self._render()

    # ── Status ────────────────────────────────────────────────────────────────
    def set_status(self, text: str) -> None:
        self.lbl_status.set_full_text(text)

    def clear(self) -> None:
        self._full_text.clear()
        self._data = None
        self._opened.clear()
        self.search_input.clear()
        self._render()
        self._follow.reset()
        self.set_status(EMPTY_STATUS)

    def _copy_output(self) -> None:
        self._render_if_needed()
        blocks = []
        for card in self._output.cards():
            rows = ["".join(text for text, _tag in (row if isinstance(row, list) else [row])) for row in card.body]
            blocks.append("\n".join([card.title + (f" ({card.meta})" if card.meta else ""), *rows]))
        QApplication.clipboard().setText("\n\n".join(blocks) or self._output.toPlainText())
        self.btn_copy.set_icon("check")
        self.btn_copy.setText("" if getattr(self, "_is_ultra_compact", False) else "Copied!")

        def _restore_btn():
            self.btn_copy.set_icon("copy")
            self.btn_copy.setText("" if getattr(self, "_is_ultra_compact", False) else "Copy")
        QTimer.singleShot(1500, self, _restore_btn)

    def connect_signals(self, sig_bus, *, connect_theme=True) -> None:
        """Connect to the MCUSignals bus."""
        if connect_theme and hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.apply_theme)
        if hasattr(sig_bus, "compat_devices_updated"):
            sig_bus.compat_devices_updated.connect(self._on_compat_updated)
        if hasattr(sig_bus, "board_selected"):
            sig_bus.board_selected.connect(self._on_board_selected)

    def _on_board_selected(self, payload: dict) -> None:
        """Mark the newly chosen board without waiting for another compile."""
        name = str((payload or {}).get("board_name") or "")
        if self._data and self._data.get("state") == "ready" and self._data.get("selected") != name:
            self._data = dict(self._data, selected=name)
            self._request_render()

    def _on_compat_updated(self, payload: dict) -> None:
        """Handle incoming compatible devices update from backend."""
        if not isinstance(payload, dict):
            return
        data = payload.get("data")
        if isinstance(data, dict):
            self.set_data(data)
        else:
            lines = payload.get("lines", [])
            self.set_content([
                (item[0], item[1]) if isinstance(item, (list, tuple)) and len(item) >= 2 else (str(item), "normal")
                for item in lines])
        if payload.get("status"):
            self.set_status(payload["status"])

    def apply_theme(self, theme_name: str) -> None:
        """Apply active theme palette to compatible devices panel."""
        from main.qt.theme import get_palette, glass_tokens
        pal = get_palette(theme_name)
        glass = glass_tokens(theme_name)
        self._tag_colors = themed_log_colors(theme_name)
        fg = pal.get("TEXT", "#e0e6ed")
        self.lbl_status.setStyleSheet(f"color: {glass_inks(theme_name)['normal']}; font-size: 11px;")
        self._output.setStyleSheet(
            "QTextBrowser#compat-console { "
            f"background: {glass['reading']}; color: {fg}; padding: 2px; font-family: Montserrat; "
            f"selection-background-color: {glass['selection']}; selection-color: {pal['TEXT_BRIGHT']}; "
            f"border: 1px solid {glass['reading_rim']}; border-radius: 10px; }}"
        )
        self._output.set_theme(self._tag_colors, theme_name)
        self._recolor_content()
