"""Original Qt setup view, imported lazily after the target Qt install is ready."""
from __future__ import annotations
import re
import sys
import time
from pathlib import Path
from typing import Optional
from PySide6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QProgressBar, QPlainTextEdit, QCheckBox, QFrame, QSizePolicy, QPushButton, QStackedWidget, QWidget)
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QIcon, QTextCursor, QTextCharFormat, QColor, QFont
from main.qt.log_follow import LogFollow, preserve_log_view
from main.qt.icons import icon
from main.qt.setup_components import GlassCard, VectorGlyph, StatusChip, SetupProgressRow
from src.modules.bootstrap_presentation import BootstrapPresentation, concise_status

class BootstrapDialog(QDialog):
    SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, gui, context):
        super().__init__()
        self._gui = gui
        self._context = context
        self._allow_close = False
        self._live_block_start: Optional[int] = None
        self._live_block_len: int = 0
        self._live_block_type: Optional[str] = None
        self._presentation = BootstrapPresentation()
        self.details_expanded = False
        self.setWindowTitle("MCU Flasher by Naph — Setup")
        self.setObjectName("BootstrapDialog")
        try:
            from main.qt.theme import register_fonts
            register_fonts()
        except ImportError:
            pass  # Standalone setup can still use the system UI font.
        self.setFont(QFont("Montserrat", 10))

        # Match the reference setup's 70% full-screen sizing in Qt's
        # logical coordinates, then fit the native frame to the work area.
        from main.qt.responsive import active_screen, fit_dialog, ScreenWatcher, work_area
        area = work_area(self)
        screen = active_screen(self)
        screen_width = screen.geometry().width() if screen else area.width
        screen_height = screen.geometry().height() if screen else area.height
        preferred = (max(520, int(screen_width * 0.70)),
                     max(420, int(screen_height * 0.70)))
        fit_dialog(self, preferred, (340, 300))
        self._screen_watcher = ScreenWatcher(self)
        self.setSizeGripEnabled(True)
        from src.modules.runtime_resources import performance_profile
        self._constrained = performance_profile().constrained
        self._display_char_limit = 256000 if self._constrained else 1000000

        self.setWindowIcon(icon("brand"))

        # Initial 1-second topmost elevation
        try:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            QTimer.singleShot(1000, self._unset_topmost)
        except Exception:
            pass

        # Checkbox checkmark icons (dynamic multi-user discovery)
        icons_dir = self._context.root / "src" / "assets" / "icons"
        if not (icons_dir / "checkbox_checked.svg").exists():
            icons_dir = Path(__file__).resolve().parent.parent / "assets" / "icons"
        icon_checked = (icons_dir / "checkbox_checked.svg").as_posix()
        icon_checked_dim = (icons_dir / "checkbox_checked_disabled.svg").as_posix()

        pal, mode = self._context.resolve_theme()
        self._theme_pal = pal
        self._theme_mode = mode

        bg_darkest  = pal["T_BG_DARKEST"]
        bg_dark     = pal["T_BG_DARK"]
        bg_mid      = pal["T_BG_MID"]
        bg_light    = pal["T_BG_LIGHT"]
        bg_hover    = pal["T_BG_HOVER"]
        border      = pal["T_BORDER"]
        border_lit  = pal.get("T_BORDER_LIT", pal["T_CYAN"])
        text        = pal["T_TEXT"]
        text_dim    = pal["T_TEXT_DIM"]
        text_bright = pal["T_TEXT_BRIGHT"]
        cyan        = pal["T_CYAN"]
        green       = pal["T_GREEN"]
        yellow      = pal["T_YELLOW"]
        red         = pal["T_RED"]
        magenta     = pal["T_MAGENTA"]

        base_style = f"""
            QDialog#BootstrapDialog {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {bg_mid}, stop:1 {bg_darkest});
                color: {text};
                font-family: 'Montserrat', 'Segoe UI', system-ui, sans-serif;
            }}
            QFrame#setupHeader, QFrame#setupFooter {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {bg_light}, stop:1 {bg_dark});
                border: 1px solid {border};
                border-top: 1px solid {border_lit};
                border-radius: 12px;
            }}
            QLabel {{ background: transparent; border: none; color: {text}; }}
            QLabel#titleLabel {{
                color: {text_bright};
                font-family: 'Montserrat', 'Segoe UI', sans-serif;
                font-size: 17px;
                font-weight: bold;
                letter-spacing: 0.5px;
            }}
            QLabel#subLabel {{
                color: {text_dim};
                font-family: 'Montserrat', 'Segoe UI', sans-serif;
                font-size: 11px;
            }}
            QLabel#timerLabel {{
                color: {cyan};
                font-family: 'Consolas', monospace;
                font-size: 12px;
                font-weight: bold;
                background: {bg_darkest};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 8px 12px;
                qproperty-alignment: AlignCenter;
            }}
            QLabel#spinLabel {{
                color: {cyan};
                font-size: 13px;
                font-weight: bold;
            }}
            QLabel#statusLabel {{
                color: {text_bright};
                font-family: 'Montserrat', 'Segoe UI', sans-serif;
                font-size: 12px;
                font-weight: 600;
            }}
            QLabel#pctLabel {{
                color: {cyan};
                font-size: 12px;
                font-weight: bold;
                font-family: 'Consolas', monospace;
            }}
            QProgressBar {{
                background-color: {bg_mid};
                border: 1px solid {border};
                border-radius: 2px;
                height: 6px;
                text-align: right;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {cyan}, stop:1 {border_lit});
                border-radius: 2px;
            }}
            QPlainTextEdit#logEdit {{
                background-color: {bg_darkest};
                border: 1px solid {border};
                border-radius: 12px;
                color: {text};
                font-family: 'Consolas', 'Cascadia Code', monospace;
                font-size: 12px;
                padding: 12px;
            }}
            /* ── High-Visibility Modern Pill ScrollBars ────────── */
            QScrollBar:vertical {{
                background: {bg_darkest};
                width: 13px;
                margin: 0px;
                border: none;
                border-left: 1px solid {border};
            }}
            QScrollBar::handle:vertical {{
                background: {border};
                min-height: 26px;
                border-radius: 4px;
                margin: 2px 2px 2px 2px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {cyan};
            }}
            QScrollBar::handle:vertical:pressed {{
                background: {border_lit};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
                background: none;
                border: none;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
            QScrollBar:horizontal {{
                background: {bg_darkest};
                height: 13px;
                margin: 0px;
                border: none;
                border-top: 1px solid {border};
            }}
            QScrollBar::handle:horizontal {{
                background: {border};
                min-width: 26px;
                border-radius: 4px;
                margin: 2px 2px 2px 2px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: {cyan};
            }}
            QScrollBar::handle:horizontal:pressed {{
                background: {border_lit};
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0px;
                background: none;
                border: none;
            }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                background: none;
            }}
            /* ── High-Visibility CheckBoxes with Vector Checkmark ─ */
            QCheckBox {{
                background: transparent;
                color: {text};
                font-family: 'Montserrat', 'Segoe UI', sans-serif;
                font-size: 11px;
                font-weight: 500;
                spacing: 8px;
            }}
            QCheckBox:hover {{
                color: {text_bright};
            }}
            QCheckBox:focus {{
                color: {cyan};
            }}
            QCheckBox::indicator {{
                width: 15px;
                height: 15px;
                border-radius: 4px;
                border: 1px solid {border};
                background-color: {bg_darkest};
            }}
            QCheckBox::indicator:hover {{
                border-color: {cyan};
                background-color: {bg_hover};
            }}
            QCheckBox::indicator:pressed {{
                background-color: {bg_mid};
                border-color: {border_lit};
            }}
            QCheckBox::indicator:checked {{
                background-color: {cyan};
                border: 1px solid {border_lit};
                image: url("ICON_CHECKED");
            }}
            QCheckBox::indicator:checked:hover {{
                background-color: {border_lit};
                border-color: {cyan};
                image: url("ICON_CHECKED");
            }}
            QCheckBox::indicator:checked:pressed {{
                background-color: {cyan};
                border-color: {border_lit};
                image: url("ICON_CHECKED");
            }}
            QCheckBox::indicator:disabled {{
                border-color: {border};
                background-color: {bg_dark};
            }}
            QCheckBox::indicator:checked:disabled {{
                background-color: {bg_mid};
                border-color: {border};
                image: url("ICON_CHECKED_DIM");
            }}
            QPushButton {{
                background-color: {bg_mid};
                color: {text_bright};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 4px 10px;
                font-family: 'Montserrat', 'Segoe UI', sans-serif;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background-color: {bg_hover};
                border: 1px solid {border_lit};
                color: {text_bright};
            }}
        """
        self.setStyleSheet(
            base_style.replace("ICON_CHECKED_DIM", icon_checked_dim).replace("ICON_CHECKED", icon_checked)
        )

        # Cached painted cards provide the same quiet depth as the workspace.
        self.setStyleSheet(self.styleSheet() + f"""
            QFrame#setupHeader, QFrame#setupFooter, QFrame#setupActivity {{
                background: transparent; border: none; border-radius: 12px;
            }}
            QPlainTextEdit#summaryEdit, QPlainTextEdit#logEdit {{
                background: transparent; border: none; padding: 2px 0px;
                color: {text}; selection-background-color: {bg_hover};
            }}
            QPlainTextEdit#summaryEdit {{ font-family: 'Montserrat', 'Segoe UI'; font-size: 12px; }}
            QLabel#activityLabel {{ color: {text_bright}; font-size: 12px; font-weight: 600; }}
            QPushButton#detailsButton {{ background: transparent; border-radius: 7px; padding: 5px 9px; font-size: 11px; }}
            QPushButton#detailsButton:checked {{ background: {bg_mid}; border-color: {border_lit}; }}
            QPushButton#detailsButton:focus {{ border: 1px solid {cyan}; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 10)
        layout.setSpacing(10)

        header = GlassCard(self, pal, accent=True)
        header.setObjectName("setupHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(12)
        header_layout.addWidget(VectorGlyph("brand", header, cyan, 32))
        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        self.title_lbl = QLabel("MCU Flasher by Naph", header)
        self.title_lbl.setObjectName("titleLabel")
        self.title_lbl.setWordWrap(True)
        self.sub_lbl = QLabel("Runtime setup · Prepare your workspace", header)
        self.sub_lbl.setWordWrap(True)
        self.sub_lbl.setObjectName("subLabel")
        title_col.addWidget(self.title_lbl)
        title_col.addWidget(self.sub_lbl)
        header_layout.addLayout(title_col, 1)
        self.timer_chip = StatusChip("00:00", header, pal, icon_name="time")
        self.timer_lbl = self.timer_chip.label
        header_layout.addWidget(self.timer_chip)
        layout.addWidget(header)

        activity = GlassCard(self, pal)
        activity.setObjectName("setupActivity")
        activity_layout = QVBoxLayout(activity)
        activity_layout.setContentsMargins(16, 12, 16, 12)
        activity_layout.setSpacing(10)
        activity_header = QHBoxLayout()
        self.activity_lbl = QLabel("Setup activity", activity)
        self.activity_lbl.setObjectName("activityLabel")
        activity_header.addWidget(self.activity_lbl, 1)
        self.details_btn = QPushButton("Technical details", activity)
        self.details_btn.setObjectName("detailsButton")
        self.details_btn.setIcon(icon("details", text_dim))
        self.details_btn.setCheckable(True)
        self.details_btn.setToolTip("Show retained installer output. The detailed run log contains the full output.")
        self.details_btn.toggled.connect(self._set_details_expanded)
        activity_header.addWidget(self.details_btn)
        activity_layout.addLayout(activity_header)

        self.package_panel = QWidget(activity)
        packages = QVBoxLayout(self.package_panel)
        packages.setContentsMargins(0, 0, 0, 0)
        packages.setSpacing(8)
        self.package_rows = [SetupProgressRow(self.package_panel, pal) for _ in range(3)]
        for row in self.package_rows:
            packages.addWidget(row)
            row.hide()
        self.package_note = QLabel(self.package_panel)
        self.package_note.setStyleSheet(f"color: {text_dim}; font-size: 11px;")
        self.package_note.setWordWrap(True)
        packages.addWidget(self.package_note)
        self.package_panel.hide()
        activity_layout.addWidget(self.package_panel)

        self.log_stack = QStackedWidget(activity)
        self.summary_edit = QPlainTextEdit(self.log_stack)
        self.summary_edit.setObjectName("summaryEdit")
        self.summary_edit.setReadOnly(True)
        self.summary_edit.setMaximumBlockCount(700)
        self.summary_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log_edit = QPlainTextEdit(self.log_stack)
        self.log_edit.setObjectName("logEdit")
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(1000 if self._constrained else 4000)
        self.log_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_stack.addWidget(self.summary_edit)
        self.log_stack.addWidget(self.log_edit)
        activity_layout.addWidget(self.log_stack, 1)
        layout.addWidget(activity, 1)
        self._follow = LogFollow(self.log_edit, resume_on_release=True)
        self._summary_follow = LogFollow(self.summary_edit, resume_on_release=True)
        self._step_start_cursor = QTextCursor(self.log_edit.document())
        self._step_start_cursor.setKeepPositionOnInsert(True)
        self._summary_step_start = QTextCursor(self.summary_edit.document())
        self._summary_step_start.setKeepPositionOnInsert(True)
        self._step_failed = False
        self._summary_failed = False

        footer = GlassCard(self, pal, accent=True)
        footer.setObjectName("setupFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(16, 12, 16, 12)
        footer_layout.setSpacing(10)
        self.overall_row = SetupProgressRow(footer, pal)
        self.status_lbl = self.overall_row.status_lbl
        self.status_lbl.setObjectName("statusLabel")
        self.spin_lbl = self.overall_row.glyph
        self.pct_lbl = self.overall_row.pct_lbl
        self.prog_bar = self.overall_row.progress_bar
        self._on_status(gui._status_text)
        footer_layout.addWidget(self.overall_row)
        options_row = QHBoxLayout()
        options_row.setSpacing(12)
        self.skip_cb = QCheckBox("Skip Updates", footer)
        self.skip_cb.setChecked(bool(gui._skip_updates))
        self.skip_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.skip_cb.setToolTip("Skip optional online updates. Missing or broken required dependencies are still repaired.")
        self.skip_cb.toggled.connect(self._on_skip_toggled)
        options_row.addWidget(self.skip_cb)
        self.auto_scroll_cb = QCheckBox("Auto-Scroll", footer)
        self.auto_scroll_cb.setChecked(True)
        self.auto_scroll_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_scroll_cb.setToolTip("Follow output; pause while holding the scrollbar and resume when released")
        self.auto_scroll_cb.toggled.connect(self._on_autoscroll_toggled)
        options_row.addWidget(self.auto_scroll_cb)
        options_row.addStretch()
        footer_layout.addLayout(options_row)
        layout.addWidget(footer)
        # Compatibility handle only: a static vector replaces the decorative spinner.
        self._spinner_timer = QTimer(self)
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)
        self._layout_density = None
        self._fit_density()

    def bind_dispatcher(self):
        self._gui._signals.attach(self.dispatch)
        self._event_timer = QTimer(self)
        self._event_timer.timeout.connect(self._gui._signals.drain)
        self._event_timer.start(30)

    def _set_details_expanded(self, expanded):
        self.details_expanded = bool(expanded)
        self.details_btn.blockSignals(True)
        self.details_btn.setChecked(self.details_expanded)
        self.details_btn.blockSignals(False)
        with self._follow.update(), self._summary_follow.update():
            self.log_stack.setCurrentWidget(self.log_edit if self.details_expanded else self.summary_edit)
            self.log_stack.layout().activate()
        self.activity_lbl.setText("Installer output" if self.details_expanded else "Setup activity")
        self._render_packages()

    def _render_packages(self):
        follow = getattr(self, "_summary_follow", None)
        if follow is None:
            self._render_package_rows()
        else:
            with follow.update():
                self._render_package_rows()
                self.package_panel.parentWidget().layout().activate()

    def _render_package_rows(self):
        rows = self._presentation.rows
        active = [row for row in rows if row.tone != "ok"]
        active.sort(key=lambda row: {"fail": 0, "warn": 1, "normal": 2, "dim": 3}.get(row.tone, 2))
        count = 0 if self.height() < 320 else 1 if self.height() < 450 else 3
        visible = active[:count]
        for widget, row in zip(self.package_rows, visible):
            tone = {"normal": "active", "dim": "muted"}.get(row.tone, row.tone)
            widget.set_status(f"{row.name} · {row.status}", tone, "package" if tone == "active" else None)
            widget.set_progress(row.percent)
            widget.setToolTip(row.detail)
            widget.show()
        for widget in self.package_rows[len(visible):]:
            widget.hide()
        complete = len(rows) - len(active)
        if rows and not active:
            note = f"{complete} package{'s' if complete != 1 else ''} complete"
        elif len(active) > len(visible):
            note = (f"{len(active)} packages queued or in progress · Technical details" if not visible else
                    f"{complete} complete · {len(active) - len(visible)} more queued or in progress")
        elif complete:
            note = f"{complete} package{'s' if complete != 1 else ''} complete"
        else:
            note = ""
        self.package_note.setText(note)
        self.package_note.setVisible(bool(note))
        self.package_panel.setVisible(bool(rows) and not self.details_expanded)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "package_panel"):
            self._fit_density()
            self._render_packages()

    def _fit_density(self):
        # Qt already uses logical pixels. Spend less height on card chrome on
        # short work areas, without reducing the chosen display or text scale.
        compact = self.height() < 380
        if getattr(self, "_layout_density", None) == compact:
            return
        self._layout_density = compact
        self.layout().setContentsMargins(*(8, 6, 8, 6) if compact else (14, 14, 14, 10))
        self.layout().setSpacing(6 if compact else 10)
        for name in ("setupHeader", "setupActivity", "setupFooter"):
            card = self.findChild(QFrame, name)
            if card is None:
                continue
            normal = (16, 14, 16, 14) if name == "setupHeader" else (16, 12, 16, 12)
            card.layout().setContentsMargins(*(10, 6, 10, 6) if compact else normal)
            card.layout().setSpacing(6 if compact else 12 if name == "setupHeader" else 10)

    def _summary_format(self, tag, failed=False):
        palette = self._theme_pal
        keys = {"ok": "T_GREEN", "warn": "T_YELLOW", "fail": "T_RED",
                "failed_step": "T_RED", "section": "T_CYAN", "subsection": "T_TEXT_BRIGHT", "dim": "T_TEXT_DIM",
                "update": "T_MAGENTA", "normal": "T_TEXT"}
        fmt = QTextCharFormat()
        fmt.setFontFamilies(["Montserrat", "Segoe UI", "sans-serif"])
        fmt.setFontPointSize(11 if tag == "section" else 10 if tag == "subsection" else 9)
        fmt.setFontWeight(QFont.Weight.Bold if tag in ("section", "subsection") else
                          QFont.Weight.DemiBold if tag in ("ok", "fail", "failed_step") or failed else QFont.Weight.Normal)
        fmt.setForeground(QColor(palette["T_RED"] if failed else palette[keys.get(tag, "T_TEXT")]))
        return fmt

    def _append_summaries(self, events):
        palette = self._theme_pal
        with self._summary_follow.update():
            doc = self.summary_edit.document()
            for event in events:
                cursor = QTextCursor(doc)
                cursor.movePosition(QTextCursor.MoveOperation.End)
                if event.tag == "section":
                    if not doc.isEmpty() and cursor.block().previous().text():
                        cursor.insertText("\n", self._summary_format("dim"))
                    self._summary_failed = False
                    self._summary_step_start = QTextCursor(cursor)
                    self._summary_step_start.setKeepPositionOnInsert(True)
                elif event.tag == "fail":
                    self._summary_failed = True
                    recolor = QTextCursor(self._summary_step_start)
                    recolor.setPosition(doc.characterCount() - 1, QTextCursor.MoveMode.KeepAnchor)
                    failed = QTextCharFormat()
                    failed.setForeground(QColor(palette["T_RED"]))
                    failed.setFontWeight(QFont.Weight.DemiBold)
                    recolor.mergeCharFormat(failed)
                prefix = "" if event.tag in ("section", "subsection") else "  "
                cursor.insertText(prefix + event.text + "\n", self._summary_format(event.tag, self._summary_failed))
            excess = doc.characterCount() - BootstrapPresentation.MAX_CHARS - 1
            if excess > 0:
                cursor = QTextCursor(doc)
                cursor.setPosition(0)
                cursor.setPosition(excess, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()

    def _restore_summary(self, snapshot):
        self._presentation.restore_snapshot(snapshot.get("presentation", {}))
        state = snapshot.get("summary")
        if not state:
            self._append_summaries(self._presentation.events)
            self._set_details_expanded(snapshot.get("details_expanded", False))
            return
        doc = self.summary_edit.document()
        raw = state.get("text", "")
        keys = {"ok": "T_GREEN", "warn": "T_YELLOW", "fail": "T_RED", "failed_step": "T_RED",
                "section": "T_CYAN", "subsection": "T_TEXT_BRIGHT", "dim": "T_TEXT_DIM", "update": "T_MAGENTA"}
        cursor = QTextCursor(doc)
        for run in state.get("runs", ({"text": raw, "tags": []},)):
            tags = run.get("tags", [])
            tag = next((value for value in reversed(tags) if value in keys and value != "failed_step"), "normal")
            fmt = self._summary_format(tag, "failed_step" in tags)
            cursor.insertText(run["text"], fmt)
        removed = max(0, len(raw.encode("utf-16-le")) // 2 - doc.characterCount() + 1)
        def position(offset):
            return max(0, min(len(raw[:offset].encode("utf-16-le")) // 2 - removed, doc.characterCount() - 1))
        selection = state.get("selection")
        if selection:
            cursor = QTextCursor(doc)
            cursor.setPosition(position(selection[0]))
            cursor.setPosition(position(selection[1]), QTextCursor.MoveMode.KeepAnchor)
            self.summary_edit.setTextCursor(cursor)
        # The presenter supplies dedupe state; the display preserves the reader.
        section = next((event.text for event in reversed(self._presentation.events) if event.tag == "section"), None)
        start = raw.rfind(section) if section else 0
        self._summary_step_start = QTextCursor(doc)
        self._summary_step_start.setPosition(position(state.get("step_start", max(0, start))))
        self._summary_step_start.setKeepPositionOnInsert(True)
        current_stage = []
        for event in reversed(self._presentation.events):
            if event.tag == "section":
                break
            current_stage.append(event)
        self._summary_failed = bool(state.get("step_failed", any(event.tag == "fail" for event in current_stage)))
        enabled = snapshot.get("auto_scroll", True)
        self._summary_follow.enabled = enabled
        reading = state.get("user_scrolled_up", False)
        self._summary_follow.user_scrolled_up = reading
        self._set_details_expanded(snapshot.get("details_expanded", False))
        top = doc.findBlock(position(state.get("top_offset", 0))).blockNumber()
        def restore_view():
            self._summary_follow._depth += 1
            try:
                bar = self.summary_edit.verticalScrollBar()
                bar.setValue(max(0, top) if reading or not enabled else bar.maximum())
                horizontal = self.summary_edit.horizontalScrollBar()
                horizontal.setValue(round(horizontal.maximum() * state.get("horizontal_fraction", 0)))
            finally:
                self._summary_follow._depth -= 1
            self._summary_follow.user_scrolled_up = reading
        restore_view()
        QTimer.singleShot(0, restore_view)

    def dispatch(self, name, args):
        try:
            if name == "call":
                self._on_call(*args)
            elif name == "hide":
                self.hide()
            elif name == "close":
                self._on_close()
            else:
                getattr(self, "_on_" + name)(*args)
        except Exception as error:
            self._context.record_exception(f"Bootstrap Qt display event error: {error}")

    def restore_snapshot(self, snapshot):
        """Restore the actual bounded Tk display, including committed tables."""
        raw = snapshot["text"]
        doc = self.log_edit.document()
        maximum_blocks = self.log_edit.maximumBlockCount()
        self.log_edit.setMaximumBlockCount(0)
        cursor = QTextCursor(doc)
        palette = self._theme_pal
        colors = {"ok": "T_GREEN", "warn": "T_YELLOW", "fail": "T_RED",
                  "failed_step": "T_RED", "section": "T_CYAN", "subsection": "T_CYAN",
                  "dim": "T_TEXT_DIM", "update": "T_MAGENTA", "normal": "T_TEXT",
                  "pip_row": "T_CYAN"}
        for run in snapshot["runs"]:
            tags = run["tags"]
            tag = "failed_step" if "failed_step" in tags else next((value for value in reversed(tags) if value in colors), "normal")
            fmt = QTextCharFormat()
            color = palette[colors[tag]]
            fmt.setForeground(QColor(color))
            fmt.setFontFamilies(["Consolas", "Cascadia Code", "Courier New", "monospace"])
            if tag in ("section", "subsection", "ok", "fail", "failed_step"):
                fmt.setFontWeight(QFont.Weight.Bold)
            self._insert_with_bar_styling(cursor, run["text"], fmt, color)
        self.log_edit.setMaximumBlockCount(maximum_blocks)
        removed = max(0, len(raw.encode("utf-16-le")) // 2 - doc.characterCount() + 1)

        def position(offset):
            value = len(raw[:offset].encode("utf-16-le")) // 2 - removed
            return max(0, min(value, doc.characterCount() - 1))

        self._step_start_cursor = QTextCursor(doc)
        self._step_start_cursor.setPosition(position(snapshot["step_start"]))
        self._step_start_cursor.setKeepPositionOnInsert(True)
        self._step_failed = snapshot["step_failed"]
        if snapshot["live_type"]:
            self._live_block_type = snapshot["live_type"]
            self._live_start_cursor = QTextCursor(doc)
            self._live_start_cursor.setPosition(position(snapshot["live_start"]))
            self._live_end_cursor = QTextCursor(doc)
            self._live_end_cursor.setPosition(position(snapshot["live_end"]))
            self._sync_live_block()
        selection = snapshot["selection"]
        if selection:
            cursor = QTextCursor(doc)
            cursor.setPosition(position(selection[0]))
            cursor.setPosition(position(selection[1]), QTextCursor.MoveMode.KeepAnchor)
            self.log_edit.setTextCursor(cursor)
        enabled = snapshot["auto_scroll"]
        self.auto_scroll_cb.blockSignals(True)
        self.auto_scroll_cb.setChecked(enabled)
        self.auto_scroll_cb.blockSignals(False)
        self._follow.enabled = enabled
        self._follow.user_scrolled_up = snapshot["user_scrolled_up"]
        top_block = doc.findBlock(position(snapshot["top_offset"])).blockNumber()
        horizontal = snapshot["horizontal_fraction"]

        def restore_view():
            vertical = self.log_edit.verticalScrollBar()
            # Qt also consumes valueChanged to move the text viewport.
            # Suppress only follow-state tracking, never that native signal.
            self._follow._depth += 1
            try:
                if enabled and not snapshot["user_scrolled_up"]:
                    vertical.setValue(vertical.maximum())
                else:
                    vertical.setValue(max(0, top_block))
            finally:
                self._follow._depth -= 1
            self._follow.user_scrolled_up = snapshot["user_scrolled_up"]
            bar = self.log_edit.horizontalScrollBar()
            bar.setValue(round(bar.maximum() * horizontal))
        restore_view()
        QTimer.singleShot(0, restore_view)
        self._on_status(self._gui._status_text)
        self._on_progress(self._gui._overall_progress)
        self._tick_clock()
        self._restore_summary(snapshot)

    def _unset_topmost(self):
        try:
            visible = self.isVisible() and not getattr(self._gui, "_closed", False)
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
            if visible:
                self.show()
        except Exception:
            pass

    def _tick_clock(self):
        elapsed = int(time.time() - self._gui._start_time)
        m, s = divmod(elapsed, 60)
        h, m = divmod(m, 60)
        if h > 0:
            t_str = f"{h:02d}:{m:02d}:{s:02d}"
        else:
            t_str = f"{m:02d}:{s:02d}"
        self.timer_lbl.setText(t_str)
        self.timer_lbl.adjustSize()

    def _tick_spinner(self):
        pass  # Status is a static vector; actual progress drives its state.

    def _on_skip_toggled(self, checked: bool):
        previous = self._gui._skip_updates
        self._gui._skip_updates = checked
        try:
            c = self._context.load_config()
            c["skip_updates"] = checked
            if not self._context.save_config(c):
                raise OSError("Configuration is not writable")
        except (OSError, TypeError, ValueError) as error:
            self._gui._skip_updates = previous
            self.skip_cb.blockSignals(True)
            self.skip_cb.setChecked(previous)
            self.skip_cb.blockSignals(False)
            self._on_log(f"Skip Updates was not saved: {error}. Check folder permissions and try again.", "warn")

    def _on_autoscroll_toggled(self, checked: bool):
        self._follow.set_enabled(checked)
        self._summary_follow.set_enabled(checked)

    def _insert_with_bar_styling(self, cursor: QTextCursor, text: str, default_fmt: QTextCharFormat, color: Optional[str] = None):
        """Insert text with enlarged, bold glyph formatting for any progress bar characters."""
        if "▰" not in text and "▱" not in text:
            cursor.insertText(text, default_fmt)
            return
        bar_fmt = QTextCharFormat()
        bar_fmt.setForeground(QColor(color or self._theme_pal["T_CYAN"]))
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

    @Slot(str, str)
    @preserve_log_view()
    def _on_log(self, text: str, tag: str):
        self._append_summaries(self._presentation.summaries(text, tag))
        if tag == "section":
            self._presentation.clear_block()
            self._render_packages()
        self._sync_live_block()
        if len(text) > 8192:
            text = text[:8192] + " … [display shortened; full output retained in the setup log]"
        pal = getattr(self, "_theme_pal", self._context.palette)
        green    = pal.get("T_GREEN", "#10b981")
        yellow   = pal.get("T_YELLOW", "#f59e0b")
        red      = pal.get("T_RED", "#ef4444")
        cyan     = pal.get("T_CYAN", "#00e5ff")
        text_dim = pal.get("T_TEXT_DIM", "#64748b")
        magenta  = pal.get("T_MAGENTA", "#c084fc")
        text_c   = pal.get("T_TEXT", "#cbd5e1")
        color_map = {
            "ok": green,
            "warn": yellow,
            "fail": red,
            "section": cyan,
            "subsection": cyan,
            "dim": text_dim,
            "update": magenta,
            "normal": text_c,
            "pip_row": cyan,
        }
        doc = self.log_edit.document()

        if tag == "section":
            start_pos = (self._live_block_start if self._live_block_start is not None and self._live_block_len
                         else doc.characterCount() - 1)
            self._step_start_cursor = QTextCursor(doc)
            self._step_start_cursor.setPosition(max(0, min(start_pos, doc.characterCount() - 1)))
            self._step_start_cursor.setKeepPositionOnInsert(True)
            self._step_failed = False

        if tag == "fail":
            self._step_failed = True
            # Recolour the whole output of this failed step in bold red
            cursor_recolor = QTextCursor(doc)
            start_p = max(0, min(self._step_start_cursor.position(), doc.characterCount() - 1))
            cursor_recolor.setPosition(start_p)
            cursor_recolor.setPosition(doc.characterCount() - 1, QTextCursor.MoveMode.KeepAnchor)
            fail_fmt = QTextCharFormat()
            fail_fmt.setForeground(QColor(red))
            fail_fmt.setFontWeight(QFont.Weight.Bold)
            cursor_recolor.mergeCharFormat(fail_fmt)

        # If this step already failed, display remaining output of the step in red
        if getattr(self, "_step_failed", False):
            color = red
        else:
            color = color_map.get(tag, text_c)

        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if tag in ("section", "subsection", "ok", "fail") or getattr(self, "_step_failed", False):
            fmt.setFontWeight(QFont.Weight.Bold)
        fmt.setFontFamilies(["Consolas", "Cascadia Code", "Courier New", "monospace"])

        msg = text + "\n"
        if (
            self._live_block_start is not None
            and self._live_block_len > 0
            and self._live_block_start < doc.characterCount()
        ):
            cursor = QTextCursor(doc)
            pos = min(self._live_block_start, max(0, doc.characterCount() - 1))
            cursor.setPosition(pos)
            self._insert_with_bar_styling(cursor, msg, fmt, color)
        else:
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self._insert_with_bar_styling(cursor, msg, fmt, color)

        self._sync_live_block()

        self._trim_display()

    @Slot(str, str)
    @preserve_log_view()
    def _on_update_block(self, block_type: str, table_text: str):
        self._presentation.update_block(block_type, table_text)
        self._render_packages()
        self._sync_live_block()
        table_text = table_text[:65536]
        if not table_text.strip():
            return
        doc = self.log_edit.document()

        if self._live_block_type is not None and self._live_block_type != block_type:
            self._live_block_start = None
            self._live_block_len = 0
        self._live_block_type = block_type

        fmt = QTextCharFormat()
        color = self._theme_pal["T_RED" if self._step_failed else "T_CYAN"]
        fmt.setForeground(QColor(color))
        if self._step_failed:
            fmt.setFontWeight(QFont.Weight.Bold)
        fmt.setFontFamilies(["Consolas", "Cascadia Code", "Courier New", "monospace"])

        cursor = QTextCursor(doc)
        if (
            self._live_block_start is not None
            and self._live_block_len > 0
            and self._live_block_start < doc.characterCount()
        ):
            cursor.setPosition(self._live_block_start)
            end_pos = min(self._live_block_start + self._live_block_len, doc.characterCount() - 1)
            cursor.setPosition(end_pos, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        else:
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self._live_block_start = cursor.position()

        start_pos = cursor.position()
        self._insert_with_bar_styling(cursor, table_text, fmt, color)
        # Persistent cursors follow insertion and removal of old log lines.
        # Integer offsets alone become stale when maximumBlockCount trims.
        self._live_end_cursor = QTextCursor(cursor)
        self._live_start_cursor = QTextCursor(doc)
        self._live_start_cursor.setPosition(max(0, cursor.position() - len(table_text.encode("utf-16-le")) // 2))
        self._sync_live_block()

        self._trim_display()

    def _sync_live_block(self):
        start = getattr(self, "_live_start_cursor", None)
        end = getattr(self, "_live_end_cursor", None)
        if start is not None and end is not None:
            self._live_block_start = start.position()
            self._live_block_len = max(0, end.position() - start.position())

    def _trim_display(self):
        doc = self.log_edit.document()
        excess = doc.characterCount() - self._display_char_limit - 1
        if excess > 0:
            cursor = QTextCursor(doc)
            cursor.setPosition(0)
            cursor.setPosition(excess, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            self._sync_live_block()

    def _forget_live_block(self):
        self._live_start_cursor = self._live_end_cursor = None
        self._live_block_start = None
        self._live_block_len = 0
        self._live_block_type = None

    @Slot()
    @preserve_log_view()
    def _on_commit_block(self):
        self._presentation.commit_block()
        self._render_packages()
        self._forget_live_block()
        doc = self.log_edit.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("\n")

    @Slot()
    @preserve_log_view()
    def _on_clear_block(self):
        self._presentation.clear_block()
        self._render_packages()
        self._sync_live_block()
        if self._live_block_start is not None and self._live_block_len > 0:
            doc = self.log_edit.document()
            if self._live_block_start < doc.characterCount():
                cursor = QTextCursor(doc)
                cursor.setPosition(self._live_block_start)
                end_pos = min(self._live_block_start + self._live_block_len, doc.characterCount() - 1)
                cursor.setPosition(end_pos, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
        self._forget_live_block()

    @Slot(str)
    def _on_status(self, text: str):
        self.overall_row.set_status(concise_status(text), "active")
        self.status_lbl.setToolTip(text)

    @Slot(float)
    def _on_progress(self, val: float):
        v = max(0, min(100, int(round(val))))
        self.overall_row.set_progress(v)

    @Slot(str, bool)
    def _on_stop_spinner(self, done_text: str, ok: bool):
        self._spinner_timer.stop()
        self.overall_row.set_status(concise_status(done_text), "ok" if ok else "fail")
        self.status_lbl.setToolTip(done_text)

    @Slot(object, tuple)
    def _on_call(self, func, args):
        try:
            func(*args)
        except Exception as e:
            self._context.record_exception(f"Bootstrap callback error: {e}")

    def closeEvent(self, event):
        if getattr(self, "_allow_close", False) or getattr(self._gui, "_closed", False):
            event.accept()
        else:
            event.ignore()
            if sys.platform == "win32":
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(
                        int(self.winId()),
                        "The setup process is running and cannot be closed.\n\n"
                        "Please wait for it to complete.",
                        "Setup in Progress",
                        0x30,  # MB_ICONWARNING
                    )
                except Exception:
                    pass

    @Slot()
    def _on_close(self):
        self._allow_close = True
        self._spinner_timer.stop()
        self._clock_timer.stop()
        if hasattr(self, "_event_timer"):
            self._event_timer.stop()
        self._gui._signals.close()
        self.close()
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(50, app.quit)
