#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.board_dialog — PySide6 Search & Select MCU Board Dialog.

Matches the layout, behavior, and keyboard navigation of the stable reference
implementation (main/dialogs.py: BoardSearchDialog).
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

# pyrefly: ignore [missing-import]
from PySide6.QtCore import Qt, QTimer
# pyrefly: ignore [missing-import]
from PySide6.QtGui import QColor, QFont, QKeyEvent, QKeySequence, QShortcut
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import (
    QDialog,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QFrame,
)

from main.core.board_catalog import SUPPORTED_BOARDS
from main.core.config import load_recent_boards, add_recent_board


class _SearchLineEdit(QLineEdit):
    """QLineEdit with Down Arrow intercept to jump focus into the list widget."""

    def __init__(self, target_list: QListWidget, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._target_list = target_list

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Down:
            if self._target_list.count() > 0:
                self._target_list.setFocus()
                if self._target_list.currentRow() < 0:
                    for i in range(self._target_list.count()):
                        item = self._target_list.item(i)
                        if item and (item.flags() & Qt.ItemFlag.ItemIsSelectable):
                            self._target_list.setCurrentRow(i)
                            break
            return
        super().keyPressEvent(event)


class BoardSearchDialog(QDialog):
    """
    Modal dialog to search & select from all supported MCU boards.
    Replicates BoardSearchDialog from the stable release architecture with
    recent board quick-select support (max 5).
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        current_board: str = "",
        board_list: Optional[Sequence[str]] = None,
        on_select_callback: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("🔍 Search & Select MCU Board")
        self.setModal(True)
        self.resize(580, 500)
        self.setMinimumSize(520, 450)

        self.on_select_callback = on_select_callback
        if board_list is not None:
            self.all_boards = list(board_list)
        else:
            self.all_boards = sorted(list(SUPPORTED_BOARDS.keys()))
        self.current_board = current_board or ""
        self.result_board: Optional[str] = None

        # Load up to 5 valid recent boards with canonical name resolution
        board_canonical_map = {b.lower(): b for b in self.all_boards}
        resolved_recents: list[str] = []
        for b in load_recent_boards():
            canon = board_canonical_map.get(b.lower(), b)
            if canon and canon not in resolved_recents:
                resolved_recents.append(canon)
        self.recent_boards = resolved_recents[:5]

        self._build_ui()
        self._apply_dialog_theme()
        self._apply_filter("")  # also pins recent boards to the top on initial open


        # Connect theme changed signal for live re-theming
        try:
            from main.qt.signals import signals
            signals.theme_changed.connect(self._apply_dialog_theme)
        except Exception:
            pass

        # Pre-select active board if present
        if self.current_board in self.all_boards:
            self._select_item_by_name(self.current_board)

        # Autofocus search entry immediately
        QTimer.singleShot(50, self.search_ent.setFocus)

    def _apply_dialog_theme(self, theme_mode: str | None = None) -> None:
        """Apply active theme palette across all BoardSearchDialog components."""
        if not theme_mode:
            from main.core.config import get_theme_mode
            theme_mode = get_theme_mode()
        from main.qt.theme import get_palette
        pal = get_palette(theme_mode)
        self._pal = pal

        bg_darkest = pal.get("BG_DARKEST", "#0d1117")
        bg_dark    = pal.get("BG_DARK", "#151922")
        bg_mid     = pal.get("BG_MID", "#1c2333")
        bg_hover   = pal.get("BG_HOVER", "#2a3a55")

        text        = pal.get("TEXT", "#e0e6ed")
        text_bright = pal.get("TEXT_BRIGHT", "#ffffff")
        text_dim    = pal.get("TEXT_DIM", "#8fa1b3")
        cyan        = pal.get("CYAN", "#00d2ff")
        border      = pal.get("BORDER", "#2d3748")

        btn_compile   = pal.get("BTN_COMPILE", "#1a5c3a")
        btn_compile_h = pal.get("BTN_COMPILE_H", "#216e46")
        btn_stop      = pal.get("BTN_STOP", "#6e2020")
        btn_stop_h    = pal.get("BTN_STOP_H", "#882828")
        btn_clear     = pal.get("BTN_CLEAR", "#2d3748")
        btn_clear_h   = pal.get("BTN_CLEAR_H", "#3a4a60")

        self.setStyleSheet(f"QDialog {{ background-color: {bg_dark}; color: {text}; }} QLabel {{ color: {text}; font-family: 'Montserrat', 'Segoe UI', sans-serif; }}")
        self.hdr_frame.setStyleSheet(f"background-color: {bg_darkest}; padding: 10px 14px; border-bottom: 1px solid {border};")
        self.lbl_hdr.setStyleSheet(f"color: {cyan}; font-size: 13px; font-weight: bold; background: transparent;")

        self.search_frame.setStyleSheet(f"background-color: {bg_dark}; padding: 8px 12px;")
        self.lbl_search.setStyleSheet(f"color: {text_dim}; font-size: 11px; font-weight: bold;")
        self.search_ent.setStyleSheet(f"""
            QLineEdit {{
                background-color: {bg_darkest};
                color: {text_bright};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 5px 8px;
                font-family: Consolas, monospace;
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border: 1px solid {cyan};
            }}
        """)


        self.listbox.setStyleSheet(f"""
            QListWidget {{
                background-color: {bg_darkest};
                border: 1px solid {border};
                border-radius: 4px;
                color: {text};
                font-family: Consolas, monospace;
                font-size: 12px;
                outline: none;
                padding: 4px;
            }}
            QListWidget::item {{
                padding: 5px 8px;
                border-radius: 3px;
            }}
            QListWidget::item:hover {{
                background-color: {bg_mid};
                color: {cyan};
            }}
            QListWidget::item:selected {{
                background-color: {bg_hover};
                color: {cyan};
                font-weight: bold;
            }}
        """)

        self.btn_frame.setStyleSheet(f"background-color: {bg_dark}; border-top: 1px solid {border};")
        self.lbl_count.setStyleSheet(f"color: {text_dim}; font-size: 11px;")

        self.btn_cancel.setStyleSheet(f"""
            QPushButton:enabled {{
                background-color: {btn_stop};
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 4px;
                font-family: 'Montserrat', 'Segoe UI', sans-serif;
                font-size: 11px;
            }}
            QPushButton:enabled:hover {{ background-color: {btn_stop_h}; border-color: {pal.get('RED', '#e74c3c')}; }}
            QPushButton:enabled:pressed {{ background-color: {btn_stop}; border-color: {pal.get('RED', '#e74c3c')}; }}
            QPushButton:disabled {{ background-color: {bg_mid}; color: {text_dim}; border: 1px solid {border}; }}
        """)

        self.btn_select.setStyleSheet(f"""
            QPushButton:enabled {{
                background-color: {btn_compile};
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 4px;
                font-family: 'Montserrat', 'Segoe UI', sans-serif;
                font-size: 11px;
                font-weight: bold;
            }}
            QPushButton:enabled:hover {{ background-color: {btn_compile_h}; border-color: {pal.get('GREEN', '#4ec994')}; }}
            QPushButton:enabled:pressed {{ background-color: {btn_compile}; border-color: {pal.get('GREEN', '#4ec994')}; }}
            QPushButton:disabled {{ background-color: {bg_mid}; color: {text_dim}; border: 1px solid {border}; }}
        """)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header Banner ─────────────────────────────────────────────────────
        self.hdr_frame = QFrame()
        hdr_layout = QHBoxLayout(self.hdr_frame)
        hdr_layout.setContentsMargins(14, 8, 14, 8)
        self.lbl_hdr = QLabel("🔍 Search MCU Board")
        hdr_layout.addWidget(self.lbl_hdr)
        root.addWidget(self.hdr_frame)

        # ── Search Input Row ──────────────────────────────────────────────────
        self.search_frame = QFrame()
        search_layout = QHBoxLayout(self.search_frame)
        search_layout.setContentsMargins(12, 10, 12, 6)
        search_layout.setSpacing(8)

        self.lbl_search = QLabel("Search:")
        search_layout.addWidget(self.lbl_search)

        # Create listbox early so search field can reference it for Down key
        self.listbox = QListWidget()

        self.search_ent = _SearchLineEdit(self.listbox)
        self.search_ent.setPlaceholderText("Type to filter boards (e.g. ESP32, Uno, Nano)...")
        self.search_ent.setClearButtonEnabled(True)
        self.search_ent.textChanged.connect(self._apply_filter)
        self.search_ent.returnPressed.connect(self._confirm_selection)
        search_layout.addWidget(self.search_ent)
        root.addWidget(self.search_frame)

        # ── Listbox ───────────────────────────────────────────────────────────
        list_container = QWidget()
        list_v = QVBoxLayout(list_container)
        list_v.setContentsMargins(12, 6, 12, 8)
        list_v.addWidget(self.listbox)
        root.addWidget(list_container, stretch=1)

        self.listbox.itemDoubleClicked.connect(lambda item: self._confirm_selection())
        self.listbox.itemSelectionChanged.connect(self._update_select_button_state)

        # ── Action Buttons ────────────────────────────────────────────────────
        self.btn_frame = QFrame()
        btn_layout = QHBoxLayout(self.btn_frame)
        btn_layout.setContentsMargins(12, 10, 12, 12)
        btn_layout.setSpacing(8)

        self.lbl_count = QLabel(f"{len(self.all_boards)} boards available")
        btn_layout.addWidget(self.lbl_count)

        btn_layout.addStretch()

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setFixedSize(85, 30)
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)

        self.btn_select = QPushButton("Select Board")
        self.btn_select.setFixedSize(100, 30)
        self.btn_select.setEnabled(False)
        self.btn_select.setCursor(Qt.CursorShape.ArrowCursor)
        self.btn_select.clicked.connect(self._confirm_selection)
        btn_layout.addWidget(self.btn_select)

        root.addWidget(self.btn_frame)

        # ── Shortcuts ─────────────────────────────────────────────────────────
        QShortcut(QKeySequence("Escape"), self, activated=self.reject)
        QShortcut(QKeySequence("Return"), self, activated=self._confirm_selection)
        QShortcut(QKeySequence("Enter"), self, activated=self._confirm_selection)

    def _populate_list(self, items: list[str], separator_after: int = -1) -> None:
        self.listbox.clear()

        pal = getattr(self, "_pal", {})
        cyan_color = QColor(pal.get("CYAN", "#00d2ff"))
        dim_color = QColor(pal.get("TEXT_DIM", "#8fa1b3"))
        border_color = QColor(pal.get("BORDER", "#3a4a60"))

        hdr_font = QFont("Consolas", 10)
        hdr_font.setBold(True)

        if separator_after > 0:
            rec_hdr = QListWidgetItem("  RECENTLY USED BOARDS")
            rec_hdr.setFlags(Qt.ItemFlag.NoItemFlags)  # not selectable
            rec_hdr.setForeground(cyan_color)
            rec_hdr.setFont(hdr_font)
            self.listbox.addItem(rec_hdr)

        for idx, name in enumerate(items):
            # Insert a visual separator between recent group and full list
            if idx == separator_after:
                sep = QListWidgetItem("  ─────────────────────────────────────────────")
                sep.setFlags(Qt.ItemFlag.NoItemFlags)  # not selectable
                sep.setForeground(border_color)
                self.listbox.addItem(sep)

                all_hdr = QListWidgetItem("  ALL BOARDS")
                all_hdr.setFlags(Qt.ItemFlag.NoItemFlags)  # not selectable
                all_hdr.setForeground(dim_color)
                all_hdr.setFont(hdr_font)
                self.listbox.addItem(all_hdr)

            prefix = "★  " if (name in self.recent_boards) else ""
            item = QListWidgetItem(f"{prefix}{name}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            if name in self.recent_boards:
                item.setForeground(QColor("#56cfbf"))
            self.listbox.addItem(item)

        self.lbl_count.setText(f"{len(items)} of {len(self.all_boards)} boards")
        self._update_select_button_state()

    def _update_select_button_state(self) -> None:
        curr = self.listbox.currentItem()
        is_sel = bool(curr and (curr.flags() & Qt.ItemFlag.ItemIsSelectable))
        self.btn_select.setEnabled(is_sel)
        self.btn_select.setCursor(Qt.CursorShape.PointingHandCursor if is_sel else Qt.CursorShape.ArrowCursor)

    def _select_item_by_name(self, name: str) -> None:
        for idx in range(self.listbox.count()):
            item = self.listbox.item(idx)
            if item and (item.flags() & Qt.ItemFlag.ItemIsSelectable):
                val = item.data(Qt.ItemDataRole.UserRole) or item.text()
                if val == name or val.replace("★", "").strip() == name:
                    self.listbox.setCurrentRow(idx)
                    self.listbox.scrollToItem(item)
                    break

    def _apply_filter(self, query: str) -> None:
        q = (query or "").strip().lower()
        separator_after = -1
        if not q:
            # Pin recent boards to the top, then the rest alphabetically
            recent_set = set(self.recent_boards)
            rest = [b for b in self.all_boards if b not in recent_set]
            if self.recent_boards:
                matches = list(self.recent_boards) + rest
                separator_after = len(self.recent_boards)  # divider after last recent
            else:
                matches = list(self.all_boards)
        else:
            matches = []
            # Prioritize matching recent boards at the top of results
            for b in self.recent_boards:
                if q in b.lower():
                    matches.append(b)
                else:
                    info = SUPPORTED_BOARDS.get(b, {})
                    plat = str(info.get("platform", "") or "").lower()
                    mcu = str(info.get("mcu", "") or "").lower()
                    if q in plat or q in mcu:
                        matches.append(b)

            for b in self.all_boards:
                if b in matches:
                    continue
                if q in b.lower():
                    matches.append(b)
                    continue
                info = SUPPORTED_BOARDS.get(b, {})
                plat = str(info.get("platform", "") or "").lower()
                mcu = str(info.get("mcu", "") or "").lower()
                if q in plat or q in mcu:
                    matches.append(b)

        self._populate_list(matches, separator_after=separator_after)
        if matches:
            for i in range(self.listbox.count()):
                item = self.listbox.item(i)
                if item and (item.flags() & Qt.ItemFlag.ItemIsSelectable):
                    self.listbox.setCurrentRow(i)
                    break

    def _confirm_selection(self) -> None:
        curr = self.listbox.currentItem()
        if curr and (curr.flags() & Qt.ItemFlag.ItemIsSelectable):
            raw_name = curr.data(Qt.ItemDataRole.UserRole)
            if not raw_name:
                raw_name = curr.text().replace("★", "").replace("⚡", "").strip()
            self.result_board = raw_name
            add_recent_board(self.result_board)
            if self.on_select_callback:
                self.on_select_callback(self.result_board)
            self.accept()
        else:
            self.reject()

    def selected_board(self) -> Optional[str]:
        return self.result_board
