#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.syntax_panel — Real-time and manual C/C++ syntax diagnostic panel.

Displays compiler and static analyzer diagnostics in a structured table
with file, line number, severity badges, and double-click navigation.
Includes severity filtering, live search, error/warning count badges,
and periodic background syntax verification.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, List, Dict, Any
import threading

# pyrefly: ignore [missing-import]
from PySide6.QtCore import Qt, Slot, QTimer
# pyrefly: ignore [missing-import]
from PySide6.QtGui import QColor, QBrush
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QFrame, QAbstractItemView,
    QComboBox, QLineEdit
)

from src import syntax_checker
from main.core.file_utils import get_sketch_files_fast
from main.qt.signals import signals as sig_bus


class SyntaxPanel(QWidget):
    """
    Bottom dock tab displaying syntax check diagnostics with jump-to-line support,
    severity filtering, live search, and error count badges.
    """

    def __init__(self, backend=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._backend = backend
        self._all_diagnostics: List[Dict[str, Any]] = []
        self._last_mtimes: Dict[str, tuple[int, int]] = {}
        self._is_checking = False

        self._build_ui()
        try:
            from main.core.config import get_monitor_font_size
            self.set_font_size(get_monitor_font_size())
        except Exception:
            pass

        from main.core.config import get_theme_mode
        self.apply_theme(get_theme_mode())

        # Background periodic syntax checking timer (every 4 seconds when idle)
        self._bg_timer = QTimer(self)
        self._bg_timer.setInterval(4000)
        self._bg_timer.timeout.connect(self._on_bg_timer_tick)
        self._bg_timer.start()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header Bar ────────────────────────────────────────────────────────
        header = QFrame()
        header.setObjectName("syntax-header")
        header.setFixedHeight(36)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 2, 10, 2)
        hl.setSpacing(8)
        self._header_layout = hl

        lbl = QLabel("🔍 Syntax Diagnostics")
        self._title_lbl = lbl
        hl.addWidget(lbl)

        # Count badges
        self._badge_errors = QLabel("✖ 0")
        self._badge_errors.setStyleSheet(
            "background: #3d1c24; color: #f7768e; border: 1px solid #7a2d3c; border-radius: 9px; padding: 1px 7px; font-size: 10px; font-weight: 700;"
        )
        hl.addWidget(self._badge_errors)

        self._badge_warnings = QLabel("⚠ 0")
        self._badge_warnings.setStyleSheet(
            "background: #3d321c; color: #e0af68; border: 1px solid #7a632d; border-radius: 9px; padding: 1px 7px; font-size: 10px; font-weight: 700;"
        )
        hl.addWidget(self._badge_warnings)

        # Filter dropdown
        lbl_filter = QLabel("Show:")
        self._lbl_filter = lbl_filter
        lbl_filter.setStyleSheet("color: #94a3b8; font-size: 11px; margin-left: 4px;")
        hl.addWidget(lbl_filter)

        self._filter_combo = QComboBox()
        self._filter_combo.addItems(["All Issues", "✖ Errors Only", "⚠ Warnings Only"])
        self._filter_combo.setFixedWidth(125)
        self._filter_combo.setStyleSheet("QComboBox { font-size: 11px; padding: 2px 6px; }")
        self._filter_combo.currentTextChanged.connect(self._apply_filters)
        hl.addWidget(self._filter_combo)

        # Search filter input
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Filter by file or text…")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.setFixedWidth(160)
        self._search_input.setFixedHeight(22)
        self._search_input.setStyleSheet(
            "QLineEdit { background: #151922; color: #cdd6f4; border: 1px solid #2d3748; border-radius: 3px; padding: 1px 6px; font-size: 11px; }"
            "QLineEdit:focus { border: 1px solid #56cfbf; }"
        )
        self._search_input.textChanged.connect(self._apply_filters)
        hl.addWidget(self._search_input)

        self._lbl_status = QLabel("Ready")
        self._lbl_status.setStyleSheet("color: #94a3b8; font-size: 11px; font-family: monospace;")
        hl.addWidget(self._lbl_status)

        hl.addStretch()

        btn_run = QPushButton("▶ Run Check")
        btn_run.setFixedHeight(22)
        btn_run.setToolTip("Run real-time syntax and diagnostics analysis")
        btn_run.setStyleSheet(
            "QPushButton { font-size: 11px; padding: 1px 10px; background: #24283b; color: #7aa2f7; border: 1px solid #3b4261; border-radius: 3px; font-weight: 600; }"
            "QPushButton:hover { background: #2f3549; color: #bb9af7; }"
        )
        btn_run.clicked.connect(self._run_manual_check)
        self._btn_run = btn_run
        hl.addWidget(btn_run)

        btn_clear = QPushButton("Clear")
        btn_clear.setFixedHeight(22)
        btn_clear.setToolTip("Clear diagnostics list")
        btn_clear.setStyleSheet(
            "QPushButton { font-size: 11px; padding: 1px 8px; background: #2d3748; color: #cdd6f4; border-radius: 3px; }"
            "QPushButton:hover { background: #3b4261; }"
        )
        btn_clear.clicked.connect(self.clear)
        self._btn_clear = btn_clear
        hl.addWidget(btn_clear)

        layout.addWidget(header)

        # ── Results Table ─────────────────────────────────────────────────────
        self._table = QTableWidget(0, 4)
        self._table.setObjectName("syntax-table")
        self._table.setHorizontalHeaderLabels(["File", "Line", "Severity", "Description"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(0, 160)
        self._table.setColumnWidth(1, 65)
        self._table.setColumnWidth(2, 95)

        self._table.itemDoubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self._table)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())

    def set_responsive_width(self, width: int) -> None:
        """Dynamically adapt diagnostics header based on available width."""
        self._current_responsive_width = width
        if width >= 1100:
            self._is_ultra_compact = False
            self._title_lbl.setText("🔍 Syntax Diagnostics")
            self._lbl_filter.setVisible(True)
            self._filter_combo.setFixedWidth(125)
            self._search_input.setFixedWidth(160)
            self._search_input.setPlaceholderText("Filter by file or text…")
            self._btn_run.setText("▶ Run Check")
            self._btn_clear.setText("Clear")
            if hasattr(self, "_header_layout"):
                self._header_layout.setSpacing(8)
        elif width >= 850:
            self._is_ultra_compact = False
            self._title_lbl.setText("🔍 Syntax")
            self._lbl_filter.setVisible(False)
            self._filter_combo.setFixedWidth(115)
            self._search_input.setFixedWidth(120)
            self._search_input.setPlaceholderText("Filter…")
            self._btn_run.setText("▶ Check")
            self._btn_clear.setText("Clear")
            if hasattr(self, "_header_layout"):
                self._header_layout.setSpacing(5)
        else:
            self._is_ultra_compact = True
            self._title_lbl.setText("🔍")
            self._lbl_filter.setVisible(False)
            self._filter_combo.setFixedWidth(90)
            self._search_input.setFixedWidth(80)
            self._search_input.setPlaceholderText("Filter…")
            self._btn_run.setText("▶")
            self._btn_clear.setText("🗑")
            if hasattr(self, "_header_layout"):
                self._header_layout.setSpacing(4)

    def set_font_size(self, size: int) -> None:
        """Update syntax diagnostic table font size."""
        try:
            sz = int(size)
        except (ValueError, TypeError):
            sz = 12
        font = self._table.font()
        font.setPointSize(sz)
        self._table.setFont(font)

    def connect_signals(self, sig_bus) -> None:
        """Connect to global signal bus for real-time diagnostic updates."""
        sig_bus.syntax_errors.connect(self._on_syntax_diagnostics)
        if hasattr(sig_bus, "font_size_changed"):
            sig_bus.font_size_changed.connect(self.set_font_size)
        if hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.apply_theme)

    def apply_theme(self, theme_name: str) -> None:
        """Update syntax diagnostic panel colors to match active theme."""
        from main.qt.theme import get_palette
        pal = get_palette(theme_name)
        cyan = pal.get("CYAN", "#00d2ff")
        bg_darkest = pal.get("BG_DARKEST", "#0a0e14")
        bg_dark = pal.get("BG_DARK", "#10151c")
        bg_mid = pal.get("BG_MID", "#161d27")
        bg_hover = pal.get("BG_HOVER", "#243040")
        border = pal.get("BORDER", "#2a3545")
        text = pal.get("TEXT", "#e0e6ed")
        text_bright = pal.get("TEXT_BRIGHT", "#ffffff")

        if hasattr(self, "_title_lbl") and self._title_lbl:
            self._title_lbl.setStyleSheet(f"color: {cyan}; font-weight: 700; font-size: 11px;")
        if hasattr(self, "_search_input") and self._search_input:
            self._search_input.setStyleSheet(
                f"QLineEdit {{ background: {bg_dark}; color: {text}; border: 1px solid {border}; border-radius: 3px; padding: 1px 6px; font-size: 11px; }}"
                f"QLineEdit:focus {{ border: 1px solid {cyan}; }}"
            )
        if hasattr(self, "_table") and self._table:
            self._table.setStyleSheet(
                f"QTableWidget {{ background: {bg_darkest}; alternate-background-color: {bg_dark}; color: {text}; border: none; font-size: 12px; }}"
                f"QTableWidget::item {{ padding: 4px 8px; }}"
                f"QTableWidget::item:selected {{ background: {bg_hover}; color: {text_bright}; }}"
                f"QHeaderView::section {{ background: {bg_mid}; color: {cyan}; font-weight: 600; font-size: 11px; padding: 4px 8px; border: none; border-bottom: 1px solid {border}; }}"
            )

    @Slot(list)
    def _on_syntax_diagnostics(self, diagnostics: List[Dict[str, Any]]) -> None:
        """Update table with incoming syntax diagnostics."""
        self.set_diagnostics(diagnostics)

    def set_diagnostics(self, diagnostics: List[Dict[str, Any]]) -> None:
        """Store diagnostics, update badges, and render with active filters."""
        self._all_diagnostics = list(diagnostics or [])
        err_count = sum(1 for d in self._all_diagnostics if "err" in str(d.get("severity", "")).lower())
        warn_count = sum(1 for d in self._all_diagnostics if "warn" in str(d.get("severity", "")).lower())

        self._badge_errors.setText(f"✖ {err_count}")
        self._badge_warnings.setText(f"⚠ {warn_count}")

        if not self._all_diagnostics:
            self._lbl_status.setText("Clean ✔")
            self._lbl_status.setStyleSheet("color: #9ece6a; font-size: 11px; font-family: monospace;")
        else:
            self._lbl_status.setText(f"{err_count} err, {warn_count} warn")
            self._lbl_status.setStyleSheet("color: #f7768e; font-size: 11px; font-family: monospace;")

        self._apply_filters()

    def _apply_filters(self) -> None:
        """Filter self._all_diagnostics according to severity and search query."""
        filter_mode = self._filter_combo.currentText()
        query = self._search_input.text().strip().lower()

        filtered: List[Dict[str, Any]] = []
        for diag in self._all_diagnostics:
            sev = str(diag.get("severity", "error")).lower()
            if "Error" in filter_mode and "err" not in sev:
                continue
            if "Warning" in filter_mode and "warn" not in sev:
                continue

            fpath = str(diag.get("file", ""))
            fname = Path(fpath).name.lower()
            msg = str(diag.get("message", diag.get("desc", ""))).lower()

            if query and query not in fname and query not in msg and query not in fpath.lower():
                continue

            filtered.append(diag)

        self._render_rows(filtered)

    def _render_rows(self, diagnostics: List[Dict[str, Any]]) -> None:
        """Populate the table widget with the provided diagnostic items."""
        self._table.setRowCount(0)

        for diag in diagnostics:
            row = self._table.rowCount()
            self._table.insertRow(row)

            fpath = str(diag.get("file", ""))
            fname = Path(fpath).name if fpath else "sketch"
            line = diag.get("line", 1)
            sev = str(diag.get("severity", "error")).lower()
            msg = str(diag.get("message", diag.get("desc", "")))

            if "err" in sev:
                sev_text = "✖ Error"
                sev_color = "#f7768e"
            elif "warn" in sev:
                sev_text = "⚠ Warning"
                sev_color = "#e0af68"
            else:
                sev_text = "ℹ Note"
                sev_color = "#7dcfff"

            item_file = QTableWidgetItem(fname)
            item_file.setData(Qt.ItemDataRole.UserRole, fpath)
            item_line = QTableWidgetItem(str(line))
            item_line.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_sev = QTableWidgetItem(sev_text)
            item_sev.setForeground(QBrush(QColor(sev_color)))
            item_desc = QTableWidgetItem(msg)

            self._table.setItem(row, 0, item_file)
            self._table.setItem(row, 1, item_line)
            self._table.setItem(row, 2, item_sev)
            self._table.setItem(row, 3, item_desc)

    def _on_row_double_clicked(self, item: QTableWidgetItem) -> None:
        """Navigate editor to target file and line on row double-click."""
        row = item.row()
        file_item = self._table.item(row, 0)
        line_item = self._table.item(row, 1)
        if not file_item or not line_item:
            return

        file_path = file_item.data(Qt.ItemDataRole.UserRole) or file_item.text()
        try:
            line_no = int(line_item.text())
        except ValueError:
            line_no = 1

        try:
            sig_bus.editor_goto_line.emit(str(file_path), line_no)
        except Exception:
            pass

    def _get_project_dir(self) -> Optional[Path]:
        if self._backend and hasattr(self._backend, "get_project_dir"):
            p = Path(self._backend.get_project_dir())
            if p.exists():
                return p
        if self._backend and hasattr(self._backend, "sketch_dir_path") and self._backend.sketch_dir_path:
            p = Path(self._backend.sketch_dir_path)
            if p.exists():
                return p
        return None

    def _on_bg_timer_tick(self) -> None:
        """Periodic background check: only runs if files changed since last scan."""
        if self._is_checking:
            return

        # Skip if backend is compiling or uploading
        if self._backend and getattr(self._backend, "is_busy", False):
            return

        proj_dir = self._get_project_dir()
        if not proj_dir:
            return

        current_mtimes: Dict[str, tuple[int, int]] = {}
        files = []
        for ext in ("*.ino", "*.cpp", "*.c", "*.h", "*.hpp"):
            for f in proj_dir.glob(ext):
                try:
                    st = f.stat()
                    current_mtimes[str(f)] = (st.st_mtime_ns, st.st_size)
                    files.append(f)
                except Exception:
                    pass

        if not files:
            return

        if self._last_mtimes and self._last_mtimes == current_mtimes:
            return  # Zero file changes, skip!

        self._last_mtimes = current_mtimes
        self._execute_analysis(files)

    def _run_manual_check(self) -> None:
        """Execute parallel syntax checking across all sketch files in project."""
        proj_dir = self._get_project_dir()
        if not proj_dir:
            return
        files = get_sketch_files_fast(proj_dir)
        self._execute_analysis(files, is_manual=True)

    def _execute_analysis(self, files: list, is_manual: bool = False) -> None:
        if not files or self._is_checking:
            return
        self._is_checking = True
        self._lbl_status.setText("Checking…")
        self._lbl_status.setStyleSheet("color: #7dcfff; font-size: 11px; font-family: monospace;")
        if is_manual:
            sig_bus.console_progress.emit({"action": "Checking Syntax"})

        def _worker():
            try:
                # analyze_files_parallel returns list[dict]
                diagnostics = syntax_checker.analyze_files_parallel(files)
            except Exception:
                diagnostics = []

            def _done():
                self._is_checking = False
                self.set_diagnostics(diagnostics)
                # Broadcast to editor markers
                sig_bus.syntax_errors.emit(diagnostics)
                if is_manual:
                    sig_bus.console_progress.emit({"action": "Completed"})

            QTimer.singleShot(0, _done)

        threading.Thread(target=_worker, name="MCU_SyntaxWorker", daemon=True).start()

    def clear(self) -> None:
        """Clear all entries in the syntax table."""
        self._all_diagnostics.clear()
        self._table.setRowCount(0)
        self._badge_errors.setText("✖ 0")
        self._badge_warnings.setText("⚠ 0")
        self._lbl_status.setText("Ready")
        self._lbl_status.setStyleSheet("color: #94a3b8; font-size: 11px; font-family: monospace;")
        sig_bus.syntax_errors.emit([])
