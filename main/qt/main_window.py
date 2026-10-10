#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.main_window — PySide6 QMainWindow for MCU Flasher by Naph.

This is the top-level application window.  It assembles all panels and
toolbars into the final layout and wires the signal bus to each widget.

Layout (top-to-bottom):
  ┌─────────────────────────────────────────────────────────┐
  │  PrimaryToolbar  (action buttons, logo, sketch label)   │
  ├─────────────────────────────────────────────────────────┤
  │  ControlsBar  (board, port, baud, options)              │
  ├─────────────────────────────────────────────────────────┤
  │  ┌──────────────────────┬──────────────────────────┐   │
  │  │  QSplitter (horiz)   │   AI Side Panel           │   │
  │  │  ┌────────────────┐  │   (optional, collapsible) │   │
  │  │  │ Monaco Editor  │  │                           │   │
  │  │  │ (QWebEngineView│  │                           │   │
  │  │  ├────────────────┤  │                           │   │
  │  │  │ QTabWidget     │  │                           │   │
  │  │  │ ·Build Console │  │                           │   │
  │  │  │ ·Serial Monitor│  │                           │   │
  │  │  │ ·Compat Devices│  │                           │   │
  │  │  └────────────────┘  │                           │   │
  │  └──────────────────────┴──────────────────────────┘   │
  ├─────────────────────────────────────────────────────────┤
  │  QStatusBar  (status, progress, telemetry)              │
  └─────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import sys
import os
import time
from pathlib import Path

# pyrefly: ignore [missing-import]
from PySide6.QtCore import Qt, QTimer, Slot, QRect, QEvent
# pyrefly: ignore [missing-import]
from PySide6.QtGui import (
    QKeySequence, QShortcut, QCloseEvent,
    QGuiApplication, QScreen, QCursor, QFont,
    QFontMetrics,
)
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QSplitter,
    QTabWidget, QStatusBar, QLabel, QProgressBar, QApplication,
    QMessageBox,
)

from main.qt.signals import signals as sig_bus
from main.qt.responsive import active_screen, work_area, clamp_window, ScreenWatcher

# ── Project root resolution ───────────────────────────────────────────────────
_this_file = Path(__file__).resolve()
# main/qt/main_window.py → project root is 2 levels up
_project_root = _this_file.parent.parent.parent


class MCUMainWindow(QMainWindow):
    """
    Main application window for MCU Flasher by Naph (PySide6).

    Assembles all Qt panels, connects signals, and manages the window lifecycle.
    """

    def __init__(self, backend=None):
        super().__init__()
        self._backend = backend
        self._ai_visible = False
        self._editor_pane_visible = True
        self._monitors_pane_visible = True
        self._editor_detached = False
        self._is_attaching_editor = False
        self._editor_pane_visible_before_detach = True
        self._detached_window = None
        self._active_operation: str | None = None
        self._operation_generation = 0
        self._startup_complete = False
        self._startup_scheduled = False
        self._pending_catalog = None
        self._pending_operation_close = None
        self._operation_close_timer = QTimer(self)
        self._operation_close_timer.setInterval(150)
        self._operation_close_timer.timeout.connect(self._finish_operation_close)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(60)
        self._layout_timer.timeout.connect(self._settle_embedded_layout)

        self._setup_window()
        self._build_ui()
        self._connect_signals()
        from main.qt.cursor_visibility import WorkspacePointerGuard
        self._pointer_guard = WorkspacePointerGuard(self)
        self._restore_geometry()
        self._screen_watcher = ScreenWatcher(self, self._on_screen_changed)

    # ─────────────────────────────────────────────────────────────────────────
    # Window setup & Screen Adaptation
    # ─────────────────────────────────────────────────────────────────────────

    def _get_active_screen(self) -> QScreen | None:
        """Use the window's monitor, falling back to the cursor before show."""
        return active_screen(self)

    def _calculate_optimal_geometry(self, screen: QScreen | None = None) -> QRect:
        """Calculate initial window geometry adapted to screen work area dimensions."""
        if screen is None:
            screen = self._get_active_screen()
        area = work_area(self, screen)
        avail = QRect(area.x, area.y, area.width, area.height)
        avail_w = avail.width()
        avail_h = avail.height()

        from src.modules.ui_metrics import WorkArea, preferred_size
        target_w, target_h = preferred_size(
            WorkArea(avail.x(), avail.y(), avail_w, avail_h), 1440, 920, ratio=.90)
        target_w = max(target_w, self._minimum_width_for_display(avail_w, avail_h))

        # Center within the available work area (respecting taskbar location)
        x = avail.x() + max(0, (avail_w - target_w) // 2)
        y = avail.y() + max(0, (avail_h - target_h) // 2)
        return QRect(x, y, target_w, target_h)

    @staticmethod
    def _minimum_width_for_display(
        screen_width: int, screen_height: int, display_scale: float = 1.0
    ) -> int:
        """Use logical work-area width; Qt already applies the OS display scale."""
        screen_width = max(1, int(screen_width))
        screen_height = max(1, int(screen_height))
        return max(1, screen_width // 2)

    def _update_minimum_window_size(self, screen: QScreen | None = None) -> None:
        """Fit minimum dimensions to the current logical work area."""
        if screen is None:
            screen = self._get_active_screen()
        area = work_area(self, screen)
        avail = QRect(area.x, area.y, area.width, area.height)
        sw = avail.width()
        sh = avail.height()
        new_min_w = self._minimum_width_for_display(sw, sh)
        new_min_h = min(380, max(1, sh - 48))
        self.setMaximumWidth(16777215)
        self.setMinimumSize(new_min_w, new_min_h)

    def _setup_window(self) -> None:
        self.setWindowTitle("MCU Flasher by Naph")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)

        # Logical work-area bounds permit compact and portrait desktops.
        screen = self._get_active_screen()
        self._update_minimum_window_size(screen)

        # Apply optimal initial geometry
        initial_geom = self._calculate_optimal_geometry(screen)
        self.setGeometry(initial_geom)

        self._refresh_window_icon()

    def _refresh_window_icon(self) -> None:
        """Use the same app-owned circuit mark as setup and workspace actions."""
        from main.core.theme import Theme
        from main.qt.icons import icon
        self.setWindowIcon(icon("brand", Theme.CYAN, size=32))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._startup_scheduled:
            self._startup_scheduled = True
            QTimer.singleShot(100, self._on_startup)
        self._update_minimum_window_size()
        self._apply_responsive_layout(self.width())

        self.raise_()
        self.activateWindow()
        if sys.platform == "win32":
            try:
                from main.core.config import focus_project_window
                hwnd = int(self.winId())
                if hwnd:
                    focus_project_window(hwnd)
                    QTimer.singleShot(50, lambda: focus_project_window(hwnd))
                    QTimer.singleShot(200, lambda: focus_project_window(hwnd))
            except Exception:
                pass

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_controls_bar"):
            self._controls_bar.set_responsive_height(event.size().height())
        tight = event.size().height() < 350
        if tight != getattr(self, "_short_screen", None):
            self._short_screen = tight
            for name in ("primary-toolbar", "controls-toolbar"):
                toolbar = self.findChild(QWidget, name)
                if toolbar is not None:
                    if not hasattr(toolbar, "_full_height_margins"):
                        toolbar._full_height_margins = toolbar.contentsMargins()
                    margins = toolbar._full_height_margins
                    toolbar.setProperty("shortScreen", tight)
                    toolbar.style().unpolish(toolbar)
                    toolbar.style().polish(toolbar)
                    toolbar.setContentsMargins(margins.left(), 0 if tight else margins.top(),
                                               margins.right(), 0 if tight else margins.bottom())
                    toolbar_layout = toolbar.layout()
                    if toolbar_layout is not None:
                        if not hasattr(toolbar, "_full_height_layout_margins"):
                            toolbar._full_height_layout_margins = toolbar_layout.contentsMargins()
                        margins = toolbar._full_height_layout_margins
                        toolbar_layout.setContentsMargins(margins.left(), 0 if tight else margins.top(),
                                                          margins.right(), 0 if tight else margins.bottom())
                    toolbar.updateGeometry()
        self._apply_responsive_layout(event.size().width())
        self._layout_timer.start()

    def _settle_embedded_layout(self) -> None:
        """Fit embedded views once after a burst of resize/state events."""
        # Initial/deferred stylesheet polish can reset QToolBarLayout margins
        # after the resize handler. Apply the height budget to polished bars.
        for name in ("primary-toolbar", "controls-toolbar"):
            toolbar = self.findChild(QWidget, name)
            if toolbar is not None and hasattr(toolbar, "_full_height_layout_margins"):
                toolbar.ensurePolished()
                margins = toolbar._full_height_layout_margins
                if self._short_screen:
                    toolbar.layout().setContentsMargins(0, 0, 0, 0)
                else:
                    toolbar.layout().setContentsMargins(margins)
                toolbar.layout().invalidate()
                toolbar.layout().activate()
        self._fit_panes_for_height()
        if hasattr(self, "_editor_panel") and self._editor_panel and self._editor_panel.isVisible():
            if hasattr(self._editor_panel, "force_layout"):
                self._editor_panel.force_layout()
        if hasattr(self, "_terminal_panel") and self._terminal_panel and self._terminal_panel.isVisible():
            self._terminal_panel._resize_embedded_terminal()
        if hasattr(self, "_ai_panel") and getattr(self, "_ai_visible", False):
            self._ai_panel._resize_embedded_ai()

    def _fit_panes_for_height(self) -> None:
        if not hasattr(self, "_bottom_tabs") or self._editor_detached:
            return
        if not self._editor_pane_visible or not self._monitors_pane_visible:
            return
        available = self._v_splitter.height()
        if available <= 0:
            return
        panel = self._bottom_tabs.currentWidget()
        output = (getattr(self, "_console_panel", None) if panel is getattr(self, "_console_container", None) else
                  getattr(getattr(self, "_serial_panel", None), "_output", None)
                  if panel is getattr(self, "_serial_panel", None) else None)
        if output is not None:
            metrics = QFontMetrics(output.document().defaultFont())
            chrome = max(0, output.height() - output.viewport().height())
            reading_pad = int(output.document().documentMargin())
            output.setMinimumHeight(max(24, metrics.lineSpacing() + reading_pad + chrome + 1))
        # Reserve tab navigation, fixed header/send rows, and one output line.
        required = self._bottom_tabs.tabBar().sizeHint().height()
        required += max(24, panel.minimumSizeHint().height() if panel else 24)
        gap = self._v_splitter.handleWidth()
        monitor_min = min(required, max(0, available - gap - 38))
        editor_min = min(150, max(0, available - monitor_min - gap))
        if self._editor_area.minimumHeight() != editor_min:
            self._editor_area.setMinimumHeight(editor_min)
        if self._bottom_tabs.minimumHeight() != monitor_min:
            self._bottom_tabs.setMinimumHeight(monitor_min)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._apply_responsive_layout(self.width())
            self._layout_timer.start()

    def _update_tab_titles_responsive(self, width: int) -> None:
        """Adapt bottom tab titles to avoid squeezing and truncation."""
        if not hasattr(self, "_bottom_tabs"):
            return
        if width >= 1150:
            titles = [
                "Build Console",
                "Serial Monitor",
                "Compatible Devices",
                "Notifications",
                "Syntax Check",
                "Terminal",
                "AI Changes",
            ]
        elif width >= 850:
            titles = [
                "Build",
                "Serial",
                "Devices",
                "Alerts",
                "Syntax",
                "Terminal",
                "AI Changes",
            ]
        else:
            titles = [
                "Build",
                "Serial",
                "Devices",
                "Alerts",
                "Syntax",
                "Terminal",
                "AI Changes",
            ]
        if not getattr(self, "_tab_icons_initialized", False):
            from main.qt.icons import icon
            names = ("console", "serial", "devices", "alerts", "search", "console", "edit")
            for i, name in enumerate(names):
                if i < self._bottom_tabs.count():
                    self._bottom_tabs.setTabIcon(i, icon(name))
            self._tab_icons_initialized = True
        for i, title in enumerate(titles):
            if i < self._bottom_tabs.count():
                if self._bottom_tabs.tabText(i) != title:
                    self._bottom_tabs.setTabText(i, title)

    def _apply_responsive_layout(self, w: int) -> None:
        """Propagate responsive width changes to all window components and child panels."""
        if getattr(self, "_last_responsive_width", None) == w:
            return
        self._last_responsive_width = w
        if hasattr(self, "_primary_toolbar") and self._primary_toolbar:
            self._primary_toolbar.set_responsive_width(w)
        if hasattr(self, "_controls_bar") and self._controls_bar:
            self._controls_bar.set_responsive_width(w)
        self._update_tab_titles_responsive(w)

        # Panel content width (accounting for open AI side panel or splitters)
        panel_w = w
        if hasattr(self, "_bottom_tabs") and self._bottom_tabs and self._bottom_tabs.width() > 100:
            panel_w = self._bottom_tabs.width()

        if hasattr(self, "_console_container") and hasattr(self._console_container, "set_responsive_width"):
            self._console_container.set_responsive_width(panel_w)
        if hasattr(self, "_serial_panel") and hasattr(self._serial_panel, "set_responsive_width"):
            self._serial_panel.set_responsive_width(panel_w)
        if hasattr(self, "_compat_panel") and hasattr(self._compat_panel, "set_responsive_width"):
            self._compat_panel.set_responsive_width(panel_w)
        if hasattr(self, "_notif_panel") and hasattr(self._notif_panel, "set_responsive_width"):
            self._notif_panel.set_responsive_width(panel_w)
        if hasattr(self, "_syntax_panel") and hasattr(self._syntax_panel, "set_responsive_width"):
            self._syntax_panel.set_responsive_width(panel_w)
        if hasattr(self, "_terminal_panel") and hasattr(self._terminal_panel, "set_responsive_width"):
            self._terminal_panel.set_responsive_width(panel_w)

        if hasattr(self, "_lbl_telemetry"):
            if w < 750:
                self._lbl_telemetry.setVisible(False)
            else:
                self._lbl_telemetry.setVisible(True)

    def _on_screen_changed(self, new_screen: QScreen | None) -> None:
        """Adapt geometry when window moves to a monitor with different dimensions or scale."""
        if not new_screen:
            return
        try:
            self._update_minimum_window_size(new_screen)
            clamp_window(self, new_screen)
            if hasattr(self, "_controls_bar") and hasattr(self._controls_bar, "update_adaptive_sizing"):
                self._controls_bar.update_adaptive_sizing()
            if hasattr(self, "_serial_panel") and hasattr(self._serial_panel, "update_adaptive_sizing"):
                self._serial_panel.update_adaptive_sizing()
            self._apply_responsive_layout(self.width())
            if hasattr(self, "_ai_panel") and self._ai_panel:
                QTimer.singleShot(100, self._ai_panel._resize_embedded_ai)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # UI construction
    # ─────────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        from main.qt.toolbar import PrimaryToolbar, ControlsBar
        from main.qt.editor_panel import MonacoEditorPanel
        from main.qt.console_panel import ConsolePanelContainer
        from main.qt.serial_panel import SerialPanel
        from main.qt.compat_panel import CompatPanel
        from main.qt.notif_panel import NotifPanel
        from main.qt.syntax_panel import SyntaxPanel
        if sys.platform.startswith("linux"):
            from main.qt.posix_terminal_panel import PosixTerminalPanel as TerminalPanel
        else:
            from main.qt.terminal_panel import TerminalPanel
        from main.qt.glass import GlassWorkspace, WorkspaceTabBar
        from main.qt.theme import build_stylesheet, register_fonts
        from main.core.config import get_theme_mode
        from main.core.theme import Theme

        # Apply global stylesheet for active theme
        active_theme = get_theme_mode()
        Theme.apply_theme(active_theme)
        register_fonts()
        app = QApplication.instance()
        if app and app.property("mcuAppliedTheme") != active_theme:
            app_font = QFont("Montserrat", 10)
            app_font.setStyleHint(QFont.StyleHint.SansSerif)
            app.setFont(app_font)
            app.setStyleSheet(build_stylesheet(active_theme))
            app.setProperty("mcuAppliedTheme", active_theme)

        # ── Primary Toolbar ──────────────────────────────────────────────────
        self._primary_toolbar = PrimaryToolbar(self._backend, self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._primary_toolbar)

        # ── Controls Bar ─────────────────────────────────────────────────────
        self._controls_bar = ControlsBar(self._backend, self)
        # The native toolbar owns the controls; no temporary top-level container.
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
        # pyrefly: ignore [missing-import]
        from PySide6.QtWidgets import QToolBar
        controls_toolbar = QToolBar("Controls", self)
        controls_toolbar.setObjectName("controls-toolbar")
        controls_toolbar.setMovable(False)
        controls_toolbar.setFloatable(False)
        controls_toolbar.addWidget(self._controls_bar)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, controls_toolbar)
        self._controls_toolbar = controls_toolbar

        # Toolbar visibility can also be changed from Qt's native toolbar
        # context menu. Keep a persistent recovery route when every toolbar is
        # hidden, because that native menu otherwise has no visible anchor.
        self._setup_view_menu()

        # ── Central widget ────────────────────────────────────────────────────
        central = GlassWorkspace()
        self.setCentralWidget(central)
        central_layout = QVBoxLayout(central)
        # The editor and every tool tab share the complete workspace bounds.
        # Reading/control padding belongs inside the panels, not around them.
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)

        # ── Horizontal splitter: main area | AI side panel ────────────────────
        from main.core.config import _load_raw_config
        from src.modules.runtime_resources import performance_profile
        constrained = performance_profile().constrained
        try:
            _cfg = _load_raw_config()
            g_accel = _cfg.get("shared", {}).get("graphics_acceleration", "OFF" if constrained else "ON") == "ON"
        except Exception:
            g_accel = not constrained

        self._h_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._h_splitter.setHandleWidth(8)
        self._h_splitter.setChildrenCollapsible(False)
        self._h_splitter.setOpaqueResize(g_accel)
        central_layout.addWidget(self._h_splitter, stretch=1)

        # ── Main vertical splitter: Editor | Bottom tabs ──────────────────────
        self._v_splitter = QSplitter(Qt.Orientation.Vertical)
        self._v_splitter.setHandleWidth(8)
        self._v_splitter.setChildrenCollapsible(False)
        self._v_splitter.setOpaqueResize(g_accel)
        self._h_splitter.addWidget(self._v_splitter)

        # ── Top Editor Area (Host for Docked Editor or Detached Placeholder) ──
        # pyrefly: ignore [missing-import]
        from PySide6.QtWidgets import QStackedWidget
        self._editor_area = QStackedWidget(self)
        self._editor_area.setObjectName("editor-area-stacked")
        self._editor_area.setMinimumHeight(150)

        # Page 0: Monaco Editor Panel
        self._editor_panel = MonacoEditorPanel(self._backend, _project_root, self._editor_area)
        self._editor_panel.setObjectName("monaco-editor-panel")
        self._editor_area.addWidget(self._editor_panel)

        # Page 1: Detached Placeholder
        from main.qt.detached_editor import EditorDetachedPlaceholder
        self._placeholder_widget = EditorDetachedPlaceholder(self._editor_area)
        self._placeholder_widget.attach_requested.connect(self.attach_editor)
        self._editor_area.addWidget(self._placeholder_widget)

        self._editor_area.setCurrentWidget(self._editor_panel)
        self._v_splitter.addWidget(self._editor_area)

        # ── Bottom tab widget ─────────────────────────────────────────────────
        self._bottom_tabs = QTabWidget()
        self._bottom_tabs.setObjectName("workspace-tabs")
        self._bottom_tabs.setTabBar(WorkspaceTabBar(self._bottom_tabs))
        self._bottom_tabs.setDocumentMode(True)
        self._bottom_tabs.tabBar().setDrawBase(False)
        self._bottom_tabs.setTabPosition(QTabWidget.TabPosition.North)
        self._bottom_tabs.setUsesScrollButtons(True)
        self._v_splitter.addWidget(self._bottom_tabs)

        # Proportional splitter ratio: editor gets ~60%, bottom ~40%
        self._v_splitter.setStretchFactor(0, 3)
        self._v_splitter.setStretchFactor(1, 2)
        h_initial = max(600, self.height())
        self._v_splitter.setSizes([int(h_initial * 0.55), int(h_initial * 0.35)])

        # ── Build Console tab ─────────────────────────────────────────────────
        self._console_container = ConsolePanelContainer(backend=self._backend)
        self._console_panel = self._console_container.console
        self._bottom_tabs.addTab(self._console_container, "Build Console")

        # ── Serial Monitor tab (placed beside Build Console) ──────────────────
        self._serial_panel = SerialPanel(self._backend)
        self._bottom_tabs.addTab(self._serial_panel, "Serial Monitor")

        # ── Compatible Devices tab ────────────────────────────────────────────
        self._compat_panel = CompatPanel()
        self._bottom_tabs.addTab(self._compat_panel, "Compatible Devices")

        # ── Notifications tab ─────────────────────────────────────────────────
        self._notif_panel = NotifPanel(self._backend)
        self._bottom_tabs.addTab(self._notif_panel, "Notifications")

        # ── Syntax Check tab ──────────────────────────────────────────────────
        self._syntax_panel = SyntaxPanel(self._backend)
        self._syntax_panel.set_buffer_provider(
            lambda: dict(self._editor_panel._bridge._buffer_snapshots),
            lambda: self._editor_panel._bridge._syntax_generation)
        self._bottom_tabs.addTab(self._syntax_panel, "Syntax Check")

        # ── Terminal tab ──────────────────────────────────────────────────────
        self._terminal_panel = TerminalPanel(self._backend, self)
        self._bottom_tabs.addTab(self._terminal_panel, "Terminal")

        from main.qt.ai_changes_panel import AIChangesPanel
        self._ai_changes_panel = AIChangesPanel(self._backend, self)
        self._bottom_tabs.addTab(self._ai_changes_panel, "AI Changes")

        # Set helpful hover tooltips across all bottom tabs
        self._bottom_tabs.setTabToolTip(0, "Build Console (Ctrl+R to compile, Ctrl+U to upload)")
        self._bottom_tabs.setTabToolTip(1, "Serial Monitor & Real-time MCU Communication")
        self._bottom_tabs.setTabToolTip(2, "Compatible Microcontroller Devices")
        self._bottom_tabs.setTabToolTip(3, "Notifications & Event Log")
        self._bottom_tabs.setTabToolTip(4, "Syntax Diagnostics & Code Analysis")
        self._bottom_tabs.setTabToolTip(5, "Integrated Bash terminal" if sys.platform.startswith("linux") else "Integrated PowerShell / CMD terminal")
        self._bottom_tabs.setTabToolTip(6, "AI file changes grouped by prompt, with timestamps and before/after previews")

        # Ensure Build Console is always the default active bottom tab on startup
        self._bottom_tabs.setCurrentIndex(0)
        self._bottom_tabs.currentChanged.connect(self._on_bottom_tab_changed)

        # Pin tab-bar cursor to Arrow — prevents IBeam bleed-through from child widgets
        # (QWebEngineView, QPlainTextEdit) propagating their text-edit cursor upward.
        self._bottom_tabs.tabBar().setCursor(Qt.CursorShape.ArrowCursor)

        def _on_v_splitter_moved(pos: int, index: int) -> None:
            if hasattr(self, "_terminal_panel") and self._terminal_panel and self._terminal_panel.isVisible():
                self._terminal_panel._resize_embedded_terminal()
                QTimer.singleShot(50, self._terminal_panel._resize_embedded_terminal)
                QTimer.singleShot(150, self._terminal_panel._resize_embedded_terminal)

        self._v_splitter.splitterMoved.connect(_on_v_splitter_moved)

        # ── AI side panel (hidden by default) ─────────────────────────────────
        if sys.platform.startswith("linux"):
            from main.qt.posix_ai_panel import PosixAIPanel as AIPanel
        else:
            from main.qt.ai_panel import AIPanel
        self._ai_panel = AIPanel(self._backend, self)
        self._ai_panel.setMinimumWidth(240)
        # No setMaximumWidth — user can freely resize via splitter handle
        self._ai_panel.setVisible(False)
        self._h_splitter.addWidget(self._ai_panel)
        self._h_splitter.setStretchFactor(0, 3)
        self._h_splitter.setStretchFactor(1, 1)
        self._h_splitter.splitterMoved.connect(self._on_h_splitter_moved)

        # ── Status bar ────────────────────────────────────────────────────────
        self._build_status_bar()
        from main.qt.package_progress import PackageProgressCard
        self._package_card = PackageProgressCard(self)
        self._package_card.details_requested.connect(self._show_package_details)

        # ── Keyboard shortcuts ────────────────────────────────────────────────
        self._setup_shortcuts()

        # ── Connect Global Signals ───────────────────────────────────────────
        from main.qt.signals import signals
        if hasattr(signals, "compat_confirm_requested"):
            signals.compat_confirm_requested.connect(self._on_compat_confirm_requested)

    def _show_package_details(self):
        self._bottom_tabs.setCurrentWidget(self._notif_panel)
        job = self._package_card.current_job()
        if job.get("details", {}).get("coverage_report"):
            from main.qt.package_coverage import open_package_coverage
            open_package_coverage(self, job["job_id"])

    def _on_compat_confirm_requested(self, payload: dict) -> None:
        title = payload.get("title", "Compatibility Warning")
        message = payload.get("message", "")
        callback = payload.get("callback")
        ret = QMessageBox.question(
            self,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if callable(callback):
            callback(ret == QMessageBox.StandardButton.Yes)

    def _on_bottom_tab_changed(self, index: int) -> None:
        widget = self._bottom_tabs.widget(index)
        if hasattr(self, "_terminal_panel") and self._terminal_panel:
            if widget is self._terminal_panel:
                self._terminal_panel._on_tab_revealed()
            else:
                self._terminal_panel._on_tab_hidden()
        self._fit_panes_for_height()
        if widget is getattr(self, "_ai_changes_panel", None):
            self._ai_changes_panel.refresh()
        self._layout_timer.start()

    def _on_theme_changed(self, theme_name: str) -> None:
        focused = QApplication.focusWidget()
        preserve_focus = bool(focused and self._pointer_guard.owns(focused))
        from main.core.theme import Theme
        from main.qt.theme import build_stylesheet
        Theme.apply_theme(theme_name)
        if hasattr(self, "_package_card"):
            self._package_card.apply_theme(theme_name)
        self._refresh_window_icon()
        self._tab_icons_initialized = False
        central = self.centralWidget()
        if hasattr(central, "invalidate_cache"):
            central.invalidate_cache()
        self._update_tab_titles_responsive(self.width())
        app = QApplication.instance()
        if app:
            app.setStyleSheet(build_stylesheet(theme_name))
            app.setProperty("mcuAppliedTheme", Theme.active_theme)
        if getattr(self, "_notification_type", None):
            self._apply_notification_color(self._notification_type)
        self._apply_telemetry_color(theme_name)
        from main.qt.icons import ActionButton
        for button in self.findChildren(ActionButton):
            button.refresh_icon()
        if hasattr(self, "_editor_panel") and self._editor_panel:
            self._editor_panel.set_theme(theme_name)
        if hasattr(self, "_primary_toolbar") and self._primary_toolbar:
            self._primary_toolbar.apply_theme(theme_name)
        if hasattr(self, "_controls_bar") and self._controls_bar:
            self._controls_bar.apply_theme(theme_name)
        if hasattr(self, "_terminal_panel") and self._terminal_panel:
            self._terminal_panel.apply_theme(theme_name)
        if hasattr(self, "_ai_panel") and hasattr(self._ai_panel, "apply_theme"):
            self._ai_panel.apply_theme(theme_name)
        if hasattr(self, "_console_container") and hasattr(self._console_container, "apply_theme"):
            self._console_container.apply_theme(theme_name)
        if hasattr(self, "_serial_panel") and hasattr(self._serial_panel, "apply_theme"):
            self._serial_panel.apply_theme(theme_name)
        if hasattr(self, "_syntax_panel") and hasattr(self._syntax_panel, "apply_theme"):
            self._syntax_panel.apply_theme(theme_name)
        if hasattr(self, "_compat_panel") and hasattr(self._compat_panel, "apply_theme"):
            self._compat_panel.apply_theme(theme_name)
        if hasattr(self, "_notif_panel") and hasattr(self._notif_panel, "apply_theme"):
            self._notif_panel.apply_theme(theme_name)
        if hasattr(self, "_ai_changes_panel"):
            self._ai_changes_panel.apply_theme(theme_name)
        self._apply_responsive_layout(self.width())
        self._layout_timer.start()
        if preserve_focus and focused.isVisible() and focused.isEnabled():
            focused.setFocus(Qt.FocusReason.OtherFocusReason)

    def _setup_view_menu(self) -> None:
        """Keep toolbar visibility controls available when the bars are hidden."""
        menu_bar = self.menuBar()
        menu_bar.setNativeMenuBar(False)
        menu_bar.setObjectName("workspace-menu-bar")

        self._view_menu = menu_bar.addMenu("&View")
        self._toolbar_menu = self._view_menu.addMenu("&Toolbars")
        toolbars = (self._primary_toolbar, self._controls_toolbar)
        self._toolbar_visibility_actions = tuple(
            toolbar.toggleViewAction() for toolbar in toolbars
        )
        for action in self._toolbar_visibility_actions:
            self._toolbar_menu.addAction(action)

        self._view_menu.addSeparator()
        self._restore_toolbars_action = self._view_menu.addAction("Show All Toolbars")
        self._restore_toolbars_action.setStatusTip(
            "Show the Primary Actions and Controls toolbars"
        )
        self._restore_toolbars_action.triggered.connect(self._restore_toolbars)

    def _restore_toolbars(self) -> None:
        """Show both top toolbars from the persistent View menu."""
        self._primary_toolbar.setVisible(True)
        self._controls_toolbar.setVisible(True)

    def _build_status_bar(self) -> None:
        sb = QStatusBar()
        sb.setSizeGripEnabled(False)
        self.setStatusBar(sb)
        sb.setFixedHeight(26)

        self._status_label = QLabel("Ready")
        self._status_label.setWordWrap(False)
        self._status_label.setFixedHeight(18)
        sb.addWidget(self._status_label, stretch=1)
        self._notification_type = None
        self._status_base_text = "Ready"
        self._notification_timer = QTimer(self)
        self._notification_timer.setSingleShot(True)
        self._notification_timer.setInterval(5000)
        self._notification_timer.timeout.connect(self._clear_notification_status)
        self._temporary_status_timer = QTimer(self)
        self._temporary_status_timer.setSingleShot(True)
        self._temporary_status_timer.timeout.connect(self._finish_temporary_action)

        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedWidth(180)
        self._progress_bar.setMaximumHeight(12)
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setVisible(False)
        sb.addPermanentWidget(self._progress_bar)

        self._lbl_telemetry = QLabel("CPU: --  RAM: -- GB free")
        from main.core.theme import Theme
        self._apply_telemetry_color(Theme.active_theme)
        sb.addPermanentWidget(self._lbl_telemetry)

    def _apply_telemetry_color(self, theme_name: str) -> None:
        from main.qt.log_colors import themed_log_colors
        from main.qt.theme import get_palette
        color = themed_log_colors(theme_name, background=get_palette(theme_name)["BG_DARK"])["dim"]
        self._lbl_telemetry.setStyleSheet(f"color: {color}; font-size: 11px; margin-right: 8px;")

    def _set_status_text(self, text: str, notification_type: str | None = None) -> None:
        """Replace status text and invalidate older transient callbacks."""
        self._notification_timer.stop()
        self._temporary_status_timer.stop()
        self._notification_type = notification_type
        if notification_type:
            self._apply_notification_color(notification_type)
        else:
            self._status_base_text = text
            self._status_label.setStyleSheet("")
        self._status_label.setText(text)

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._shortcut_compile)
        QShortcut(QKeySequence("Ctrl+U"), self, activated=self._shortcut_upload)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._shortcut_save)
        QShortcut(QKeySequence("Ctrl+Shift+S"), self, activated=self._shortcut_save_all)
        self._find_all_shortcut = QShortcut(QKeySequence("Ctrl+Shift+F"), self,
                                          activated=self._editor_panel.show_project_search)
        self._project_shortcut = QShortcut(QKeySequence("Ctrl+O"), self, activated=self._shortcut_open_project)
        if sys.platform.startswith("linux"):
            # Native coding CLIs own these keys while their Qt view is focused,
            # just as Windows' embedded foreign assistant receives them.
            self._native_cli_shortcuts = self.findChildren(QShortcut)
            QApplication.instance().focusChanged.connect(self._update_native_cli_shortcuts)

    def _update_native_cli_shortcuts(self, *_args):
        focused = any(getattr(panel, "has_input_focus", lambda: False)()
                      for panel in (self._ai_panel, self._terminal_panel))
        for shortcut in self._native_cli_shortcuts:
            shortcut.setEnabled(not focused)

    # ─────────────────────────────────────────────────────────────────────────
    # Signal connections
    # ─────────────────────────────────────────────────────────────────────────

    def _connect_signals(self) -> None:
        """Wire all Qt signal bus connections for this window and child panels."""
        # Connect status bar
        sig_bus.operation_phase.connect(self._on_operation_phase)
        sig_bus.console_progress.connect(self._on_console_progress)
        sig_bus.telemetry.connect(self._on_telemetry)
        sig_bus.notification.connect(self._on_notification)
        sig_bus.package_progress.connect(self._package_card.update_job)
        sig_bus.project_updated.connect(self._on_project_updated)
        sig_bus.window_closable.connect(self._set_window_closable)
        sig_bus.board_catalog_updated.connect(self._on_catalog_updated)

        # Connect child panels
        # The workspace owns one theme propagation pass. Standalone panels may
        # still subscribe directly when they are not hosted in this window.
        self._console_container.connect_signals(sig_bus, connect_theme=False)
        self._serial_panel.connect_signals(sig_bus, connect_theme=False)
        self._notif_panel.connect_signals(sig_bus, connect_theme=False)
        self._compat_panel.connect_signals(sig_bus, connect_theme=False)
        self._syntax_panel.connect_signals(sig_bus, connect_theme=False)
        self._terminal_panel.connect_signals(sig_bus, connect_theme=False)
        self._editor_panel.connect_signals(sig_bus, connect_theme=False)
        self._primary_toolbar.connect_signals(sig_bus, connect_theme=False)
        self._controls_bar.connect_signals(sig_bus, connect_theme=False)

        # Toolbar action signals
        sig_bus.file_reload_requested.connect(self._shortcut_reload_file)
        sig_bus.modify_files_requested.connect(self._open_modify_files_dialog)

        # Settings and theme signals
        sig_bus.theme_changed.connect(self._on_theme_changed)
        sig_bus.font_size_changed.connect(self._on_content_font_changed)
        sig_bus.font_size_changed.connect(self._ai_changes_panel.set_font_size)
        if hasattr(sig_bus, "graphics_accel_changed"):
            sig_bus.graphics_accel_changed.connect(self._on_graphics_accel_changed)

    @Slot(int)
    def _on_content_font_changed(self, _size: int) -> None:
        self._layout_timer.start()

    @Slot(dict)
    def _on_catalog_updated(self, data):
        # Incremental previews belong to the picker, never to target controls.
        if data.get("partial"):
            return
        if "error" in data:
            self._set_status_text(f"Board catalog unavailable: {data['error']}")
            return
        self._pending_catalog = data
        self._apply_pending_catalog()

    def _apply_pending_catalog(self):
        # Keep only the latest authoritative result during an operation. Its
        # completion signal applies it; no per-result recursive polling timers.
        if not self._pending_catalog or (self._backend and self._backend.is_busy):
            return
        data, self._pending_catalog = self._pending_catalog, None
        from main.core.board_catalog import SUPPORTED_BOARDS
        SUPPORTED_BOARDS.replace(data.get("boards", {}))
        self._primary_toolbar._update_action_button_states()
        if self._backend and self._backend.current_board:
            self._controls_bar._update_hardware_defaults_for_board(self._backend.current_board, update_monitor=False)
        if data.get("warning"):
            self._set_status_text("Showing locally prepared boards. Use Boards & Libraries Manager for new packs.")
            self._status_label.setToolTip(data["warning"])

    @Slot(bool)
    def _on_graphics_accel_changed(self, enabled: bool) -> None:
        """Update live splitter resize behavior dynamically."""
        if hasattr(self, "_h_splitter") and self._h_splitter:
            self._h_splitter.setOpaqueResize(enabled)
        if hasattr(self, "_v_splitter") and self._v_splitter:
            self._v_splitter.setOpaqueResize(enabled)

    # ─────────────────────────────────────────────────────────────────────────
    # Slots
    # ─────────────────────────────────────────────────────────────────────────

    def nativeEvent(self, event_type, message):
        """Handle Windows OS-level hardware messages (e.g. WM_DEVICECHANGE for USB hotplug)."""
        if sys.platform == "win32" and event_type == b"windows_generic_MSG":
            try:
                import ctypes
                from ctypes import wintypes
                ptr = int(message)
                if ptr:
                    msg = wintypes.MSG.from_address(ptr)
                    WM_DEVICECHANGE = 0x0219
                    if msg.message == WM_DEVICECHANGE:
                        if hasattr(self, "_backend") and self._backend:
                            QTimer.singleShot(250, self._backend.refresh_ports)
            except Exception:
                pass
        return super().nativeEvent(event_type, message)

    @Slot(bool)
    def _set_window_closable(self, closable: bool) -> None:
        """Grey out (or restore) the window's native [X] close button and
        Alt+F4 at the OS level on Windows. Matches LATEST-WORKING-MCU- FLASHER."""
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            if not hwnd:
                return
            import ctypes
            from ctypes import wintypes
            MF_BYCOMMAND = 0x00000000
            MF_GRAYED    = 0x00000001
            MF_ENABLED   = 0x00000000
            SC_CLOSE     = 0xF060
            user32 = ctypes.windll.user32
            hmenu = user32.GetSystemMenu(wintypes.HWND(hwnd), False)
            if hmenu:
                flag = MF_ENABLED if closable else MF_GRAYED
                user32.EnableMenuItem(hmenu, SC_CLOSE, MF_BYCOMMAND | flag)
        except Exception:
            pass

    @Slot(dict)
    def _on_operation_phase(self, payload: dict) -> None:
        if hasattr(self, "_controls_bar") and self._controls_bar:
            self._controls_bar.on_operation_phase(payload)
        is_busy: bool = payload.get("is_busy", False)
        phase: str    = payload.get("phase", "idle")
        op: str       = payload.get("op", "")
        if is_busy:
            if hasattr(self, "_project_shortcut"):
                self._project_shortcut.setEnabled(False)
            self._operation_generation += 1
            self._active_operation = op or phase
            phase_map = {
                "compile": "Compiling",
                "upload": "Uploading",
                "flash": "Flashing Firmware",
                "flashing": "Flashing Firmware",
                "clean": "Cleaning Cache",
                "reset": "Resetting Board",
                "hard_reset": "Hard Resetting",
                "soft_reset": "Soft Resetting",
                "syntax": "Checking Syntax",
                "toolchain": "Preparing Toolchain",
                "save": "Saving",
                "save_all": "Saving All",
                "reload": "Reloading",
            }
            effective_key = (op or phase).lower()
            display_phase = phase_map.get(effective_key, phase_map.get(phase.lower(), phase.replace("_", " ").title()))
            self._set_status_text(f"⚙ {display_phase}…")
            self._progress_bar.setRange(0, 0)
            self._progress_bar.setTextVisible(False)
            self._progress_bar.setVisible(True)

            is_reset = (
                phase in ("reset", "resetting", "hard_reset", "soft_reset")
                or op in ("reset", "hard_reset", "soft_reset")
            )
            is_compile_phase = (
                phase in ("compile", "compiling")
                or (op == "compile" and phase not in ("flash", "flashing", "upload"))
            )
            is_upload_or_flash = (
                (phase in ("flash", "flashing", "upload") or (op in ("upload", "flash") and phase != "compile"))
                and not is_compile_phase
            )

            # Switch to Build Console when a build/upload or reset starts
            if is_compile_phase or is_upload_or_flash or is_reset:
                if not self._monitors_pane_visible:
                    self.toggle_monitors_pane()
                if not getattr(self, "_active_operation_started", False) or is_upload_or_flash or is_reset:
                    self._bottom_tabs.setCurrentWidget(self._console_container)
                    self._active_operation_started = True

            # Phase-aware Serial Monitor tab accessibility:
            # During compile or compile-phase, the Serial Monitor tab MUST remain active and accessible.
            # It should ONLY be disabled during actual uploading/flashing write operations or hardware resets.
            serial_idx = self._bottom_tabs.indexOf(self._serial_panel)
            if is_upload_or_flash or is_reset:
                if self._bottom_tabs.currentWidget() == self._serial_panel:
                    self._bottom_tabs.setCurrentWidget(self._console_container)
                if serial_idx >= 0:
                    self._bottom_tabs.setTabEnabled(serial_idx, False)
                if hasattr(self, "_serial_panel") and self._serial_panel:
                    self._serial_panel.setEnabled(False)
            elif is_compile_phase:
                if serial_idx >= 0:
                    self._bottom_tabs.setTabEnabled(serial_idx, True)
                if hasattr(self, "_serial_panel") and self._serial_panel:
                    self._serial_panel.setEnabled(True)
        else:
            if hasattr(self, "_project_shortcut"):
                self._project_shortcut.setEnabled(True)
            self._apply_pending_catalog()
            self._set_status_text("Ready")
            self._progress_bar.setVisible(False)
            self._progress_bar.setRange(0, 0)
            self._active_operation_started = False

            # Re-enable Serial Monitor tab and panel
            serial_idx = self._bottom_tabs.indexOf(self._serial_panel)
            if serial_idx >= 0:
                self._bottom_tabs.setTabEnabled(serial_idx, True)
            if hasattr(self, "_serial_panel") and self._serial_panel:
                self._serial_panel.setEnabled(True)

            # If an upload, flash, or hard/soft reset just finished, switch focused tab to Serial Monitor after a 500ms delay
            prev_op = getattr(self, "_active_operation", None) or payload.get("op")
            is_success = payload.get("success", True)
            self._active_operation = None
            generation = self._operation_generation
            target = self._post_upload_target()
            if prev_op in ("upload", "flash", "hard_reset", "soft_reset", "reset") and is_success:
                QTimer.singleShot(500, self, lambda: self._finish_upload_ui(generation, target))

            # After an upload/flash or soft reset, once the Serial Monitor tab is focused, issue
            # a silent DTR pulse to reboot the MCU so its boot logs and sketch
            # output appear immediately — no manual Reset button press needed.
            if prev_op in ("upload", "flash") and is_success:
                QTimer.singleShot(700, self, lambda: self._finish_upload_ui(generation, target, reset=True))

    def _post_upload_target(self):
        if not self._backend:
            return None
        return (self._backend.current_board, self._backend.current_port,
                str(self._backend.sketch_dir_path))

    def _finish_upload_ui(self, generation, target, reset=False):
        # A previous successful upload must not refocus or reset a new target,
        # even if a second operation has already started AND finished meanwhile.
        if (generation != self._operation_generation or target != self._post_upload_target()
                or self._active_operation is not None
                or (self._backend and self._backend.is_busy)):
            return
        if reset:
            self._post_upload_dtr_pulse()
        else:
            self._focus_serial_monitor()

    def _focus_serial_monitor(self) -> None:
        """Switch bottom tab to Serial Monitor and set focus (e.g. after upload completes)."""
        if getattr(self, "_active_operation", None) is not None:
            return
        if not self._monitors_pane_visible:
            self.toggle_monitors_pane()
        self._bottom_tabs.setCurrentWidget(self._serial_panel)
        if hasattr(self._serial_panel, "setFocus"):
            self._serial_panel.setFocus()

    def _post_upload_dtr_pulse(self) -> None:
        """Issue a silent DTR reset pulse after an upload so the MCU starts immediately.

        Only fires when:
        - No new operation has started since the upload completed (rapid-action guard).
        - The Serial Monitor tab is currently the visible bottom tab.
        - The backend serial connection is open.
        """
        if getattr(self, "_active_operation", None) is not None:
            return  # A new operation started during the 700ms delay — abort.
        if self._bottom_tabs.currentWidget() is not self._serial_panel:
            return  # User navigated away — skip the pulse.
        if self._backend:
            self._backend.pulse_dtr_reset()

    @Slot(dict)
    def _on_console_progress(self, payload: dict) -> None:
        action = payload.get("action", "")
        if action.lower() in ("failed", "error", "cancelled"):
            self._set_status_text(action)
            self._progress_bar.setVisible(False)
            return
        if action.lower() in ("completed", "ready", "idle", "done", "success"):
            if not getattr(self, "_active_operation", None) or getattr(self, "_active_operation", None) in ("clean", "reset", "soft_reset", "hard_reset", "syntax"):
                self._set_status_text("Ready")
                self._progress_bar.setVisible(False)
                self._progress_bar.setRange(0, 0)
            return

        # Keep smooth circulation loading without displaying percentages
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setVisible(True)
        if action:
            low = action.lower()
            if low in ("flashing", "uploading"):
                display_action = "Uploading"
            elif low in ("compiling", "building"):
                display_action = "Compiling"
            elif low in ("cleaning", "cleaning build cache"):
                display_action = "Cleaning Cache"
            else:
                display_action = action

            if not (display_action.endswith("…") or display_action.endswith("...")):
                display_action = f"{display_action}…"
            if not display_action.startswith("⚙ "):
                display_action = f"⚙ {display_action}"
            self._set_status_text(display_action)

    def _trigger_temporary_action(self, action_name: str, duration_ms: int = 700) -> None:
        """Display an indeterminate circulation loading indicator for quick UI actions (e.g. Save, Reload)."""
        if getattr(self, "_active_operation", None):
            return

        display_text = action_name.strip()
        if not (display_text.endswith("…") or display_text.endswith("...")):
            display_text = f"{display_text}…"
        if not display_text.startswith("⚙ "):
            display_text = f"⚙ {display_text}"

        self._set_status_text(display_text)
        self._status_base_text = "Ready"
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setVisible(True)

        self._temporary_status_timer.start(duration_ms)

    def _finish_temporary_action(self) -> None:
        if self._active_operation is None:
            self._progress_bar.setVisible(False)
            self._progress_bar.setRange(0, 0)
            self._set_status_text("Ready")

    @Slot(dict)
    def _on_telemetry(self, payload: dict) -> None:
        cpu = payload.get("cpu_percent", 0)
        ram = payload.get("ram_free_gb", 0)
        self._lbl_telemetry.setText(f"CPU: {cpu:.0f}%  RAM: {ram:.1f} GB free")

    @Slot(dict)
    def _on_notification(self, payload: dict) -> None:
        title = payload.get("title", "")
        msg   = payload.get("message", "")
        ntype = payload.get("type") or "info"

        # Guard against multi-line text expanding status bar
        if "\n" in msg:
            first_line = msg.split("\n", 1)[0].strip()
            display_text = f"{title}: {first_line}" if title and first_line and not first_line.startswith("•") else (title or first_line)
        else:
            display_text = msg or title

        self._set_status_text(display_text, ntype)
        if not self._active_operation:
            self._progress_bar.setVisible(False)
        self._notification_timer.start()

    def _apply_notification_color(self, notification_type: str) -> None:
        from main.core.theme import Theme
        from main.qt.log_colors import themed_log_colors
        from main.qt.theme import get_palette
        tag = notification_type if notification_type in ("success", "error", "warning") else "info"
        color = themed_log_colors(Theme.active_theme, background=get_palette(Theme.active_theme)["BG_DARK"])[tag]
        self._status_label.setStyleSheet(f"color: {color};")

    def _clear_notification_status(self) -> None:
        if self._notification_type is not None:
            self._set_status_text(self._status_base_text)

    @Slot(dict)
    def _on_project_updated(self, payload: dict) -> None:
        name = payload.get("name", "")
        if name:
            self.setWindowTitle(f"MCU Flasher by Naph — {name}")
        path = payload.get("path", "")
        if path and hasattr(self, "_primary_toolbar"):
            self._primary_toolbar.update_sketch_label(path)
        if hasattr(self, "_editor_panel"):
            active = payload.get("active_file", "")
            if active:
                self._editor_panel.open_file(active)
        if hasattr(self, "_ai_panel") and getattr(self._ai_panel, "_is_active", False):
            self._ai_panel.reset_for_project(path)
        if hasattr(self, "_terminal_panel") and getattr(self._terminal_panel, "_is_active", False):
            self._terminal_panel.reset_for_project(path)

    # ─────────────────────────────────────────────────────────────────────────
    # Keyboard shortcut handlers
    # ─────────────────────────────────────────────────────────────────────────

    def _shortcut_compile(self) -> None:
        self._primary_toolbar._do_compile()

    def _shortcut_upload(self) -> None:
        self._primary_toolbar._do_upload()

    def _shortcut_save(self) -> None:
        self._trigger_temporary_action("Saving", 700)
        self._editor_panel.trigger_save()

    def _shortcut_save_all(self) -> None:
        self._trigger_temporary_action("Saving All", 800)
        self._editor_panel.trigger_save_all()

    def restart_for_offline_mode(self, previous_mode: bool) -> None:
        """Save acknowledged editor bytes before handing restart to Bootstrap."""
        from src.modules.offline_mode import transition_blocker
        import subprocess

        def failed(message="The sketch could not be saved. The workspace remains open."):
            self.setEnabled(True)
            from main.core.config import _load_raw_config, _save_raw_config
            data = _load_raw_config(fresh=True)
            data.setdefault("shared", {})["offline_enabled"] = bool(previous_mode)
            data["shared"]["offline_preparation_pending"] = False
            if _save_raw_config(data) is False:
                message += " The previous mode could not be restored; run Bootstrap on the next launch."
            QMessageBox.warning(self, "Restart cancelled", message)

        def saved():
            blocker = transition_blocker(self._backend)
            if blocker or self._is_busy():
                failed(blocker or "An operation started before restart. Wait for it to finish.")
                return
            environment = os.environ.copy()
            for key in ("PYTHONHOME", "PYTHONPATH", "MCU_FLASHER_WORKSPACE_RUNTIME",
                        "MCU_FLASHER_OFFLINE_RUNTIME", "MCU_FLASHER_APP_ROOT", "PIP_NO_INDEX"):
                environment.pop(key, None)
            command = [sys.executable, "-B", str(_project_root / "direct/restart_workspace.py"),
                       "--parent", str(os.getpid()), "--project", str(getattr(self._backend, "sketch_dir_path", "") or "")]
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
            try:
                child = subprocess.Popen(command, cwd=_project_root, env=environment,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, **options)
            except OSError as exc:
                failed(f"Bootstrap restart could not start: {exc}")
                return
            def close_after_handoff():
                if child.poll() is not None:
                    failed("The restart helper stopped before the workspace closed.")
                elif transition_blocker(self._backend) or self._is_busy():
                    child.terminate()
                    failed("An operation or another project window opened before restart.")
                else:
                    if not self.close():
                        child.terminate()
                        failed("The workspace could not close safely. Wait for the current action to finish.")
            QTimer.singleShot(250, close_after_handoff)

        blocker = transition_blocker(self._backend)
        if blocker or self._is_busy():
            failed(blocker or "Wait for the current operation to finish before restarting.")
            return
        self.setEnabled(False)
        try:
            self._editor_panel.trigger_save_all(callback=saved, failure_callback=failed)
        except (OSError, RuntimeError) as exc:
            failed(f"The editor could not save before restart: {exc}")

    def _is_busy(self) -> bool:
        if self._backend and (self._backend.is_busy
                              or getattr(self._backend, "active_operation", None) is not None
                              or getattr(self._backend, "_current_op_phase", None) is not None):
            return True
        if getattr(self, "_active_operation", None) is not None:
            return True
        return False

    def _shortcut_open_project(self) -> None:
        if self._is_busy():
            return
        from main.qt.project_dialog import ProjectDialog
        dlg = ProjectDialog(self._backend, parent=self, open_in_new_window=True)
        dlg.exec()
        dlg.deleteLater()

    def _open_modify_files_dialog(self) -> None:
        """Open the Modify Project Files dialog."""
        if self._is_busy():
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self,
                "Action in Progress",
                "Modifying project files is not allowed while an action is in progress.\n\n"
                "Please wait for the current action to finish or stop it first.",
            )
            return
        from main.qt.modify_dialog import ModifyFilesDialog
        dlg = ModifyFilesDialog(self._backend, parent=self)
        dlg.exec()

    def _shortcut_reload_file(self) -> None:
        """Reload the active file in the editor from disk."""
        self._on_console_progress({"action": "Reloading"})
        self._editor_panel.trigger_reload()

    # ─────────────────────────────────────────────────────────────────────────
    # AI panel toggle
    # ─────────────────────────────────────────────────────────────────────────

    def _on_h_splitter_moved(self, pos: int, index: int) -> None:
        if hasattr(self, "_ai_panel") and getattr(self, "_ai_visible", False):
            self._ai_panel._resize_embedded_ai()
        if hasattr(self, "_terminal_panel") and self._terminal_panel and self._terminal_panel.isVisible():
            self._terminal_panel._resize_embedded_terminal()
        self._apply_responsive_layout(self.width())

    def _sync_ai_and_editor_layout(self) -> None:
        """Synchronize the main window layout based on whether the code editor is detached
        and whether the OpenCode AI Assistant side panel is visible.

        Faithfully aligned with the original stable release:
        - When editor is DETACHED:
          1. Hide editor area on the main window (_editor_area.setVisible(False)), so no
             space is wasted on placeholders.
          2. If AI Assistant is visible:
             Configure _h_splitter to orient=VERTICAL, stacking OpenCode AI Assistant (top)
             and TABS / Monitors (bottom) in two rows so they take 100% of the window.
          3. If AI Assistant is NOT visible:
             _bottom_tabs takes 100% of the main window space.
        - When editor is ATTACHED (docked):
          1. Preserve the user's editor visibility choice.
          2. Configure _h_splitter to orient=HORIZONTAL, placing OpenCode AI Assistant
             in a right-side column alongside the main vertical pane.
        """
        detached = getattr(self, "_editor_detached", False)
        ai_visible = getattr(self, "_ai_visible", False)

        if detached:
            # 1. Editor is detached to separate window:
            # Hide editor area in main window so it consumes zero space
            self._editor_area.setMinimumHeight(0)
            self._editor_area.setVisible(False)

            # Ensure monitors / bottom tabs are visible
            self._monitors_pane_visible = True
            self._bottom_tabs.setVisible(True)

            if ai_visible:
                # Stack AI Assistant (top) and TABS (bottom) in two rows
                self._h_splitter.setOrientation(Qt.Orientation.Vertical)
                self._h_splitter.insertWidget(0, self._ai_panel)
                self._h_splitter.insertWidget(1, self._v_splitter)
                self._ai_panel.setVisible(True)

                self._h_splitter.setStretchFactor(0, 1)
                self._h_splitter.setStretchFactor(1, 1)

                h_total = max(500, self.centralWidget().height())
                ai_h = max(200, int(h_total * 0.52))
                tabs_h = max(180, h_total - ai_h)
                self._h_splitter.setSizes([ai_h, tabs_h])
                self._v_splitter.setSizes([0, tabs_h])

                if hasattr(self, "_ai_panel"):
                    QTimer.singleShot(80, self._ai_panel._resize_embedded_ai)
            else:
                # AI is hidden: ensure _bottom_tabs fills the entire window
                self._ai_panel.setVisible(False)
                self._h_splitter.setOrientation(Qt.Orientation.Horizontal)
                self._h_splitter.insertWidget(0, self._v_splitter)
                self._h_splitter.insertWidget(1, self._ai_panel)

                self._h_splitter.setStretchFactor(0, 1)
                self._h_splitter.setStretchFactor(1, 0)
                self._v_splitter.setSizes([0, 1000])
                self._h_splitter.setSizes([1000, 0])

            if hasattr(self, "_controls_bar"):
                self._controls_bar.set_editor_detached(True)
                # Editor is in its own window — hide/show toggle has no meaning.
                self._controls_bar.set_editor_visible(False)
                self._controls_bar.btn_toggle_editor.setEnabled(False)
                self._controls_bar.set_monitors_visible(True)
                self._controls_bar.set_ai_panel_visible(ai_visible)

        else:
            # Editor is ATTACHED (docked):
            self._editor_area.setMinimumHeight(150)
            self._h_splitter.setOrientation(Qt.Orientation.Horizontal)
            self._h_splitter.insertWidget(0, self._v_splitter)
            self._h_splitter.insertWidget(1, self._ai_panel)

            editor_visible = getattr(self, "_editor_pane_visible", True)
            monitors_visible = getattr(self, "_monitors_pane_visible", True)

            self._editor_area.setVisible(editor_visible)
            self._bottom_tabs.setVisible(monitors_visible)

            if editor_visible:
                self._editor_panel.setVisible(True)
                self._editor_panel.show()
                if hasattr(self._editor_panel, "force_layout"):
                    self._editor_panel.force_layout()

            v_total = max(500, self.centralWidget().height())
            if editor_visible and monitors_visible:
                self._v_splitter.setSizes([int(v_total * 0.58), int(v_total * 0.42)])
            elif editor_visible:
                self._v_splitter.setSizes([v_total, 0])
            else:
                self._v_splitter.setSizes([0, v_total])

            if ai_visible:
                self._ai_panel.setVisible(True)
                self._h_splitter.setStretchFactor(0, 3)
                self._h_splitter.setStretchFactor(1, 1)
                w_total = max(800, self.centralWidget().width())
                self._h_splitter.setSizes([int(w_total * 0.70), int(w_total * 0.30)])
                if hasattr(self, "_ai_panel"):
                    QTimer.singleShot(80, self._ai_panel._resize_embedded_ai)
            else:
                self._ai_panel.setVisible(False)
                self._h_splitter.setStretchFactor(0, 1)
                self._h_splitter.setStretchFactor(1, 0)
                self._h_splitter.setSizes([1000, 0])

            if hasattr(self, "_controls_bar"):
                self._controls_bar.set_editor_detached(False)
                # Re-enable Hide/Show Editor toggle when back in docked mode.
                self._controls_bar.btn_toggle_editor.setEnabled(True)
                self._controls_bar.set_editor_visible(editor_visible)
                self._controls_bar.set_monitors_visible(monitors_visible)
                self._controls_bar.set_ai_panel_visible(ai_visible)

        # Ensure terminal embedded child is notified if visible
        if hasattr(self, "_terminal_panel") and self._terminal_panel and self._terminal_panel.isVisible():
            QTimer.singleShot(30, self._terminal_panel._resize_embedded_terminal)
            QTimer.singleShot(100, self._terminal_panel._resize_embedded_terminal)
            QTimer.singleShot(250, self._terminal_panel._resize_embedded_terminal)

    def toggle_ai_panel(self, visible: bool) -> None:
        self._ai_visible = visible
        if hasattr(self, "_ai_panel") and visible:
            self._ai_panel.ensure_started()
        self._sync_ai_and_editor_layout()

    # ── Pane and Editor Layout / Detachment handlers ─────────────────────────

    def reveal_editor_for_navigation(self) -> None:
        """Expose the existing embedded editor for an explicit source jump."""
        if not self._editor_detached and not self._editor_pane_visible:
            self._editor_pane_visible = True
            self._sync_ai_and_editor_layout()

    def toggle_editor_pane(self) -> None:
        """Show/hide the embedded code editor pane.
        If the editor is currently detached, clicking this re-attaches it to the main window."""
        if self._editor_detached:
            self.attach_editor()
            return

        if self._editor_pane_visible:
            if not self._monitors_pane_visible:
                self._monitors_pane_visible = True
            self._editor_pane_visible = False
        else:
            self._editor_pane_visible = True

        self._sync_ai_and_editor_layout()

    def toggle_monitors_pane(self) -> None:
        """Show/hide the Monitors pane (Build Console / Serial Monitor tabs)."""
        if self._editor_detached and not self._ai_visible:
            return

        if self._monitors_pane_visible:
            if not self._editor_pane_visible and not self._editor_detached:
                self._editor_pane_visible = True
            self._monitors_pane_visible = False
        else:
            self._monitors_pane_visible = True

        self._sync_ai_and_editor_layout()

    def toggle_editor_detachment(self) -> None:
        """Toggle between docked and detached floating code editor."""
        if self._editor_detached:
            self.attach_editor()
        else:
            self.detach_editor()

    def detach_editor(self) -> None:
        """Pop the Monaco editor out into an independent floating window."""
        if self._editor_detached:
            return
        self._editor_had_focus_before_detach = self._editor_panel.has_input_focus()
        self._editor_detached = True
        # Remember the user's editor visibility choice so re-attaching restores
        # it exactly (hidden stays hidden, shown stays shown).
        self._editor_pane_visible_before_detach = self._editor_pane_visible
        # Mark the embedded pane as "not visible" so the toolbar shows
        # "Show Editor" and so that reattaching restores the correct state.
        self._editor_pane_visible = False

        from main.qt.detached_editor import DetachedEditorWindow
        if self._detached_window is None:
            self._detached_window = DetachedEditorWindow(self)
            self._detached_window.closing.connect(self.attach_editor)

        # Center detached window over main window, clamped to the screen work
        # area so it never overflows small displays or hidden taskbars
        geo = self.geometry()
        w, h = 1000, 700
        screen = self._get_active_screen()
        avail = screen.availableGeometry() if screen else None
        if avail:
            w = min(w, avail.width())
            h = min(h, avail.height())
            x = max(avail.left(), min(geo.x() + (geo.width() - w) // 2, avail.right() - w))
            y = max(avail.top(), min(geo.y() + (geo.height() - h) // 2, avail.bottom() - h))
        else:
            x = max(0, geo.x() + (geo.width() - w) // 2)
            y = max(0, geo.y() + (geo.height() - h) // 2)
        self._detached_window.setGeometry(x, y, w, h)

        # Reparent editor to detached window and ALWAYS ensure it is visible
        self._editor_panel.setParent(self._detached_window)
        self._detached_window.setCentralWidget(self._editor_panel)
        self._editor_panel.setVisible(True)
        self._detached_window.show()
        self._detached_window.raise_()
        self._detached_window.activateWindow()

        # Synchronize layout in the main window
        self._sync_ai_and_editor_layout()
        if self._editor_had_focus_before_detach:
            self._editor_panel.restore_input_focus()
        self._on_notification({"type": "info", "message": "✓ Code editor detached to separate window."})

    def attach_editor(self) -> None:
        """Re-embed the detached Monaco editor back into the main window."""
        if not self._editor_detached or self._is_attaching_editor:
            return
        self._is_attaching_editor = True
        try:
            restore_editor_focus = (self._editor_panel.has_input_focus()
                                    or getattr(self, "_editor_had_focus_before_detach", False))
            self._editor_detached = False

            # Take (NOT delete) the editor panel out of the detached window
            # before reparenting.  setCentralWidget(QWidget()) would queue a
            # deleteLater() on the editor panel and destroy it — the editor
            # would be gone for good instead of re-attaching.
            if self._detached_window is not None:
                if self._detached_window.centralWidget() is self._editor_panel:
                    self._detached_window.takeCentralWidget()
                self._detached_window.hide()

            # Place editor back inside _editor_area
            self._editor_area.addWidget(self._editor_panel)
            self._editor_area.setCurrentWidget(self._editor_panel)

            # Restore the editor pane visibility the user had before detaching:
            # if the editor was hidden when popped out, it stays hidden after
            # re-attach so the docked container doesn't eat space.
            self._editor_pane_visible = getattr(
                self, "_editor_pane_visible_before_detach", True
            )

            # Synchronize layout back to docked
            self._sync_ai_and_editor_layout()

            # The retained WebEngine page only needs one coalesced geometry pass.
            self._editor_panel.show()
            if hasattr(self._editor_panel, "force_layout"):
                self._editor_panel.force_layout()

            if restore_editor_focus and self._editor_pane_visible:
                self.activateWindow()
                self._editor_panel.restore_input_focus()

            if self._editor_pane_visible:
                self._on_notification({"type": "info", "message": "✓ Code editor re-attached to main window."})
            else:
                self._on_notification({"type": "info", "message": "✓ Code editor re-attached (pane hidden — use Show Editor)."})
        finally:
            self._is_attaching_editor = False


    # ─────────────────────────────────────────────────────────────────────────
    # Startup & Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def _on_startup(self) -> None:
        """Post-show initialization: update sketch label and precompiled state."""
        if self._startup_complete:
            return
        self._startup_complete = True
        if self._backend:
            path = str(self._backend.sketch_dir_path)
            self._primary_toolbar.update_sketch_label(path)
            name = Path(path).name
            if name:
                self.setWindowTitle(f"MCU Flasher by Naph — {name}")

            self._backend.update_skip_compile_availability()
            if hasattr(self._backend, "start_services"):
                self._backend.start_services()

            # Enforce project file hygiene and initial hardware state sync in background worker
            # to prevent mechanical HDD seek stalls from blocking the GUI thread during window show
            def _bg_startup_hygiene():
                try:
                    from main.core.file_utils import hide_internal_project_metadata, SCRIPT_DIR
                    hide_internal_project_metadata(SCRIPT_DIR)
                    if self._backend and self._backend.sketch_dir_path:
                        hide_internal_project_metadata(self._backend.sketch_dir_path)
                    if hasattr(self._backend, "_sync_project_hardware_state"):
                        self._backend._sync_project_hardware_state()
                except Exception:
                    pass

            import threading
            threading.Thread(target=_bg_startup_hygiene, name="MCU_StartupHygiene", daemon=True).start()

            # Load active file into Monaco immediately without sluggish delay
            self._load_initial_file()

    def _load_initial_file(self) -> None:
        if self._backend and self._backend.active_file_path:
            self._editor_panel.open_file(self._backend.active_file_path)

    def _restore_geometry(self) -> None:
        """Restore window size/position from saved config with multi-monitor validation."""
        if not self._backend:
            return
        try:
            cfg = self._backend.get_settings()
            geom = cfg.get("window_geometry")
            was_maximized = bool(cfg.get("window_maximized", False))

            if geom and len(geom) == 4:
                x, y, w, h = int(geom[0]), int(geom[1]), int(geom[2]), int(geom[3])
                saved_rect = QRect(x, y, w, h)

                # Validate that saved_rect visibly intersects any currently connected display
                matching_screen = None
                screens = QGuiApplication.screens()
                for scr in screens:
                    intersect = scr.availableGeometry().intersected(saved_rect)
                    # Minimum 150x100px visible on the monitor
                    if intersect.width() >= 150 and intersect.height() >= 100:
                        matching_screen = scr
                        break

                if matching_screen:
                    self._update_minimum_window_size(matching_screen)
                    avail = matching_screen.availableGeometry()
                    # Clamp dimensions to the available work area
                    w = min(w, avail.width())
                    h = min(h, avail.height())
                    # Ensure window titlebar and top-left are on-screen
                    x = max(avail.left(), min(x, avail.right() - 200))
                    y = max(avail.top(), min(y, avail.bottom() - 100))
                    self.setGeometry(x, y, w, h)
                else:
                    # Monitor disconnected or coordinates out-of-bounds; center on active screen
                    optimal = self._calculate_optimal_geometry()
                    self.setGeometry(optimal)

            clamp_window(self)
            if was_maximized:
                self.showMaximized()
        except Exception:
            pass

    def _save_geometry(self) -> None:
        """Persist window geometry and maximized state to config on close."""
        if not self._backend:
            return
        try:
            cfg = self._backend.get_settings()
            is_max = self.isMaximized()
            cfg["window_maximized"] = is_max
            # If maximized, save normalGeometry so unmaximizing later restores cleanly
            if is_max:
                g = self.normalGeometry()
            else:
                g = self.geometry()
            cfg["window_geometry"] = [g.x(), g.y(), g.width(), g.height()]
            self._backend.save_settings(cfg)
        except Exception:
            pass

    def _operation_children_alive(self) -> bool:
        """A finished process can still have a resolving/cleanup worker."""
        backend = self._backend
        if backend is None:
            return False
        worker = getattr(backend, "_operation_worker", None)
        process = getattr(backend, "_active_process", None)
        try:
            return bool((worker is not None and worker.is_alive()) or
                        (process is not None and process.poll() is None))
        except Exception:
            return True

    def _defer_operation_close(self) -> None:
        """Wait on Qt's event loop after requesting safe cancellation."""
        if self._pending_operation_close is not None:
            return
        backend = self._backend
        self._pending_operation_close = (
            getattr(backend, "_op_session_id", 0),
            getattr(backend, "_operation_worker", None), time.monotonic() + 20.0,
        )
        try:
            backend.stop_operation()
        except Exception as exc:
            self._pending_operation_close = None
            backend.emit("console:log", {"text": f"Cannot close while the operation is running: {exc}",
                                         "tag": "error", "newline": True})
            return
        self._operation_close_timer.start()

    def _finish_operation_close(self) -> None:
        pending = self._pending_operation_close
        if pending is None:
            self._operation_close_timer.stop()
            return
        backend = self._backend
        session, worker, deadline = pending
        phase = getattr(backend, "_current_op_phase", None)
        changed = (getattr(backend, "_op_session_id", 0) != session or
                   getattr(backend, "_operation_worker", None) is not worker)
        unsafe = (phase in {"flashing", "writing", "erasing", "resetting", "cleaning"} or
                  getattr(backend, "_framework_download_active", False))
        if changed or unsafe or time.monotonic() >= deadline:
            self._operation_close_timer.stop()
            self._pending_operation_close = None
            text = ("Close cancelled because the active operation changed." if changed or unsafe else
                    "The operation is still stopping. The workspace remains open; close it again after the worker exits.")
            backend.emit("console:log", {"text": text, "tag": "warning", "newline": True})
            return
        if self._operation_children_alive():
            return
        self._operation_close_timer.stop()
        self._pending_operation_close = None
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        # ── Read current backend operation state ─────────────────────────
        phase = getattr(self._backend, "_current_op_phase", None) if self._backend else None
        op    = getattr(self._backend, "active_operation",   None) if self._backend else None
        kind  = getattr(self._backend, "_active_reset_kind", None) if self._backend else None

        # Destructive phases that must NEVER be interrupted.
        # "compiling" alone (pure compile, not the compile step of an upload)
        # is NOT in this set — it's safe to cancel.
        DESTRUCTIVE_PHASES = {"flashing", "writing", "erasing", "resetting", "cleaning"}
        DESTRUCTIVE_OPS    = {"flash", "reset", "clean"}

        # ── Framework / toolchain downloads (flagged by bootstrap module) ─
        if getattr(self._backend, "_framework_download_active", False):
            QMessageBox.warning(
                self,
                "Cannot Close — Download In Progress",
                "A critical toolchain / framework download is currently running.\n\n"
                "Closing the app now may corrupt the PlatformIO core installation.\n"
                "Please wait for the download to complete before exiting.",
            )
            event.ignore()
            return

        if self._backend and (self._backend.is_busy or self._operation_children_alive()):
            proc = getattr(self._backend, "_active_process", None)

            # ── Safe stale-busy auto-recovery ────────────────────────────
            # Exclude op == "upload": the upload flow transitions internally from
            # compile → flash; the compile process may have exited while flash
            # hasn't launched yet, so we must NOT clear busy in that window.
            stale = (
                (proc is None or proc.poll() is not None)
                and not self._operation_children_alive()
                and op not in DESTRUCTIVE_OPS
                and op != "upload"
                and phase not in DESTRUCTIVE_PHASES
            )
            if stale:
                self._backend.is_busy = False
                # Fall through to normal clean exit below.

            else:
                # Safe cancellation must finish before child-panel teardown.
                if ((op == "compile" and phase in {None, "resolving", "compiling"}) or
                        (op == "upload" and phase in {"resolving", "compiling", "connecting"}) or
                        (op is None and phase is None and proc is None)):
                    self._defer_operation_close()
                    event.ignore()
                    return

                else:
                    # ── Destructive operation — block the close ──────────
                    if op == "upload" and phase == "compiling":
                        msg = (
                            "Upload In Progress — Compiling Firmware\n\n"
                            "The application is compiling firmware and is about to write it to "
                            "your MCU.\nInterrupting this process now may corrupt the board "
                            "firmware.\n\n"
                            "Please wait for the upload to complete."
                        )
                    elif phase in ("flashing", "writing"):
                        msg = (
                            "Firmware is actively being written to the MCU memory.\n\n"
                            "Closing the app mid-write can leave your board in a corrupted, "
                            "unbootable state.\n\n"
                            "Please wait for the upload to finish."
                        )
                    elif phase == "erasing":
                        msg = (
                            "The MCU flash memory is being erased.\n\n"
                            "Interrupting an erase operation can brick the board.\n\n"
                            "Please wait for the erase to finish."
                        )
                    elif phase == "resetting":
                        if kind == "hard":
                            msg = (
                                "A hardware reset / bootloader sequence is in progress.\n\n"
                                "Interrupting this can leave the board stuck in bootloader mode "
                                "and may require a manual power cycle.\n\n"
                                "Please wait for the reset to complete."
                            )
                        else:
                            msg = (
                                "A firmware rewrite / soft reset is in progress.\n\n"
                                "Interrupting this can leave the board in a broken state.\n\n"
                                "Please wait for the reset to complete."
                            )
                    elif phase == "cleaning":
                        msg = (
                            "The build cache is being cleaned.\n\n"
                            "Interrupting a cache clean mid-operation may corrupt cached build "
                            "objects, forcing a full rebuild on next compile.\n\n"
                            "Please wait for the clean to finish."
                        )
                    else:
                        msg = (
                            "A critical operation is currently running.\n\n"
                            "Closing the app now may leave your MCU or build environment in "
                            "an inconsistent state.\n\n"
                            "Please wait for the operation to complete."
                        )

                    QMessageBox.warning(self, "Cannot Close — Operation In Progress", msg)
                    event.ignore()
                    return

        # Instantly hide UI window so the user experiences zero visual latency on exit
        try:
            self.hide()
            if sys.platform == "win32":
                try:
                    import ctypes
                    hwnd = int(self.winId())
                    if hwnd:
                        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE = 0
                except Exception:
                    pass
        except Exception:
            pass

        self._save_geometry()
        # Clean up child panels
        if hasattr(self, "_terminal_panel"):
            try:
                self._terminal_panel._stop_shell()
            except Exception:
                pass
        if hasattr(self, "_ai_panel"):
            try:
                self._ai_panel._stop_ai()
            except Exception:
                pass
        if getattr(self, "_detached_window", None) is not None and self._detached_window.isVisible():
            try:
                # App is shutting down — drop the floating window without
                # re-attaching (closeEvent would otherwise emit closing → attach).
                try:
                    self._detached_window.closing.disconnect(self.attach_editor)
                except (TypeError, RuntimeError):
                    pass
                self._detached_window.hide()
                self._detached_window.close()
            except Exception:
                pass
        # Stop all background workers cleanly (signals Events, joins threads)
        if self._backend:
            try:
                self._backend.stop_services()
            except Exception:
                pass

        # Signal any sleeping Download Manager process to terminate if this is the last window
        try:
            app = QApplication.instance()
            top_levels = [w for w in app.topLevelWidgets() if isinstance(w, MCUMainWindow) and w is not self] if app else []
            if not top_levels:
                if sys.platform.startswith("linux"):
                    from main.core.config import clean_instance_config
                    from src.modules.ubuntu_download_manager import quit_if_last_workspace
                    clean_instance_config()
                    quit_if_last_workspace(_project_root, os.getpid())
                else:
                    exit_trigger = Path(_project_root) / "index_json" / ".dm_force_exit"
                    exit_trigger.parent.mkdir(parents=True, exist_ok=True)
                    exit_trigger.write_text("exit", encoding="utf-8")
        except Exception:
            pass

        event.accept()
