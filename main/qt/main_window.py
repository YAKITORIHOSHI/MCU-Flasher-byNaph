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
from pathlib import Path

# pyrefly: ignore [missing-import]
from PySide6.QtCore import Qt, QTimer, Slot, QRect, QEvent
# pyrefly: ignore [missing-import]
from PySide6.QtGui import (
    QIcon, QKeySequence, QShortcut, QCloseEvent,
    QGuiApplication, QScreen, QCursor, QFont,
)
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QSplitter,
    QTabWidget, QStatusBar, QLabel, QProgressBar, QApplication,
    QMessageBox,
)

from main.qt.signals import signals as sig_bus

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
        self._detached_window = None
        self._active_operation: str | None = None

        self._setup_window()
        self._build_ui()
        self._connect_signals()
        self._restore_geometry()
        self._on_startup()

        if self._backend and hasattr(self._backend, "start_services"):
            self._backend.start_services()

    # ─────────────────────────────────────────────────────────────────────────
    # Window setup & Screen Adaptation
    # ─────────────────────────────────────────────────────────────────────────

    def _get_active_screen(self) -> QScreen | None:
        """Return the screen where the cursor or window currently resides."""
        try:
            cursor_pos = QCursor.pos()
            screen = QGuiApplication.screenAt(cursor_pos)
            if screen is not None:
                return screen
        except Exception:
            pass
        try:
            return QGuiApplication.primaryScreen()
        except Exception:
            return None

    def _calculate_optimal_geometry(self, screen: QScreen | None = None) -> QRect:
        """Calculate initial window geometry adapted to screen work area dimensions."""
        if screen is None:
            screen = self._get_active_screen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
        avail_w = avail.width()
        avail_h = avail.height()

        # Dynamic target: 90% of available work area, capped at max 1440x920
        target_w = min(1440, max(840, int(avail_w * 0.90)))
        target_h = min(920, max(560, int(avail_h * 0.90)))

        # Clamp strictly to available work area (never overflow)
        target_w = min(target_w, avail_w)
        target_h = min(target_h, avail_h)

        # Center within the available work area (respecting taskbar location)
        x = avail.x() + max(0, (avail_w - target_w) // 2)
        y = avail.y() + max(0, (avail_h - target_h) // 2)
        return QRect(x, y, target_w, target_h)

    @staticmethod
    def _minimum_width_for_display(
        screen_width: int, screen_height: int, display_scale: float = 1.0
    ) -> int:
        """Half-screen normally; compact but on-screen for portrait displays (matches LATEST-WORKING-MCU- FLASHER)."""
        screen_width = max(1, int(screen_width))
        screen_height = max(1, int(screen_height))
        display_scale = max(0.75, min(3.0, float(display_scale or 1.0)))
        safe_margin = min(screen_width // 4, round(24 * display_scale))
        usable_width = max(240, screen_width - safe_margin)
        compact_floor = round(320 * display_scale)
        minimum = max(compact_floor, screen_width // 2)
        if screen_height > screen_width and screen_width < 900:
            minimum = max(minimum, min(round(560 * display_scale), usable_width))
        return min(minimum, usable_width)

    def _update_minimum_window_size(self, screen: QScreen | None = None) -> None:
        """Set window minimum width to half the screen the window is currently on."""
        if screen is None:
            screen = self._get_active_screen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
        sw = avail.width()
        sh = avail.height()
        display_scale = 1.0
        try:
            dpi = screen.logicalDotsPerInch() if screen else 96.0
            display_scale = max(1.0, dpi / 96.0)
        except Exception:
            pass
        new_min_w = self._minimum_width_for_display(sw, sh, display_scale)
        logical_screen_h = sh / display_scale
        logical_min_h = min(460, max(400, logical_screen_h - 120))
        safe_margin_h = min(sh // 4, round(48 * display_scale))
        new_min_h = min(
            round(logical_min_h * display_scale), max(260, sh - safe_margin_h)
        )
        self.setMinimumSize(new_min_w, new_min_h)

    def _setup_window(self) -> None:
        self.setWindowTitle("⚡ MCU Flasher by Naph")

        # Adaptive minimum size: ensures root minsize width is half the current active monitor screen
        screen = self._get_active_screen()
        self._update_minimum_window_size(screen)

        # Apply optimal initial geometry
        initial_geom = self._calculate_optimal_geometry(screen)
        self.setGeometry(initial_geom)

        # Application icon
        icon_path = _project_root / "src" / "assets" / "mcu_icon.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # Hook screen change event to adapt dynamically when moved across monitors
        win_handle = self.windowHandle()
        if win_handle and not getattr(self, "_screen_hooked", False):
            self._screen_hooked = True
            win_handle.screenChanged.connect(self._on_screen_changed)

        self._update_minimum_window_size()
        self._apply_responsive_layout(self.width())

        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = int(self.winId())
                if hwnd:
                    ctypes.windll.user32.ShowWindow(hwnd, 1)  # SW_SHOWNORMAL = 1
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout(event.size().width())
        if hasattr(self, "_terminal_panel") and self._terminal_panel and self._terminal_panel.isVisible():
            self._terminal_panel._resize_embedded_terminal()
            QTimer.singleShot(50, self._terminal_panel._resize_embedded_terminal)
            QTimer.singleShot(150, self._terminal_panel._resize_embedded_terminal)
        if hasattr(self, "_ai_panel") and getattr(self, "_ai_visible", False):
            self._ai_panel._resize_embedded_ai()
            QTimer.singleShot(50, self._ai_panel._resize_embedded_ai)
            QTimer.singleShot(150, self._ai_panel._resize_embedded_ai)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._apply_responsive_layout(self.width())
            if hasattr(self, "_terminal_panel") and self._terminal_panel and self._terminal_panel.isVisible():
                QTimer.singleShot(30, self._terminal_panel._resize_embedded_terminal)
                QTimer.singleShot(100, self._terminal_panel._resize_embedded_terminal)
                QTimer.singleShot(250, self._terminal_panel._resize_embedded_terminal)
            if hasattr(self, "_ai_panel") and getattr(self, "_ai_visible", False):
                QTimer.singleShot(30, self._ai_panel._resize_embedded_ai)
                QTimer.singleShot(100, self._ai_panel._resize_embedded_ai)
                QTimer.singleShot(250, self._ai_panel._resize_embedded_ai)

    def _update_tab_titles_responsive(self, width: int) -> None:
        """Adapt bottom tab titles to avoid squeezing and truncation."""
        if not hasattr(self, "_bottom_tabs"):
            return
        if width >= 1150:
            titles = [
                "⚙ Build Console",
                "📡 Serial Monitor",
                "🔧 Compatible Devices",
                "🔔 Notifications",
                "🔍 Syntax Check",
                "🖥 Terminal",
            ]
        elif width >= 850:
            titles = [
                "⚙ Build",
                "📡 Serial",
                "🔧 Devices",
                "🔔 Alerts",
                "🔍 Syntax",
                "🖥 Term",
            ]
        else:
            titles = [
                "⚙",
                "📡",
                "🔧",
                "🔔",
                "🔍",
                "🖥",
            ]
        for i, title in enumerate(titles):
            if i < self._bottom_tabs.count():
                if self._bottom_tabs.tabText(i) != title:
                    self._bottom_tabs.setTabText(i, title)

    def _apply_responsive_layout(self, w: int) -> None:
        """Propagate responsive width changes to all window components and child panels."""
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
            if not self.isMaximized():
                avail = new_screen.availableGeometry()
                cur = self.geometry()
                new_w = min(cur.width(), avail.width())
                new_h = min(cur.height(), avail.height())
                new_x = max(avail.left(), min(cur.x(), avail.right() - 200))
                new_y = max(avail.top(), min(cur.y(), avail.bottom() - 100))
                if (new_x, new_y, new_w, new_h) != (cur.x(), cur.y(), cur.width(), cur.height()):
                    self.setGeometry(new_x, new_y, new_w, new_h)
            if hasattr(self, "_controls_bar") and hasattr(self._controls_bar, "update_adaptive_sizing"):
                self._controls_bar.update_adaptive_sizing()
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
        from main.qt.terminal_panel import TerminalPanel
        from main.qt.theme import build_stylesheet, register_fonts
        from main.core.config import get_theme_mode
        from main.core.theme import Theme

        # Apply global stylesheet for active theme
        active_theme = get_theme_mode()
        Theme.apply_theme(active_theme)
        register_fonts()
        app = QApplication.instance()
        if app:
            app_font = QFont("Montserrat", 10)
            app_font.setStyleHint(QFont.StyleHint.SansSerif)
            app.setFont(app_font)
            app.setStyleSheet(build_stylesheet(active_theme))

        # ── Primary Toolbar ──────────────────────────────────────────────────
        self._primary_toolbar = PrimaryToolbar(self._backend, self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._primary_toolbar)

        # ── Controls Bar ─────────────────────────────────────────────────────
        self._controls_bar = ControlsBar(self._backend, self)
        # Use a container widget as a second "toolbar row"
        controls_container = QWidget()
        cv = QVBoxLayout(controls_container)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(self._controls_bar)
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
        # pyrefly: ignore [missing-import]
        from PySide6.QtWidgets import QToolBar
        controls_toolbar = QToolBar("Controls", self)
        controls_toolbar.setObjectName("controls-toolbar")
        controls_toolbar.setMovable(False)
        controls_toolbar.setFloatable(False)
        controls_toolbar.addWidget(self._controls_bar)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, controls_toolbar)

        # ── Central widget ────────────────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)

        # ── Horizontal splitter: main area | AI side panel ────────────────────
        self._h_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._h_splitter.setHandleWidth(6)
        self._h_splitter.setChildrenCollapsible(False)
        self._h_splitter.setOpaqueResize(True)
        central_layout.addWidget(self._h_splitter, stretch=1)

        # ── Main vertical splitter: Editor | Bottom tabs ──────────────────────
        self._v_splitter = QSplitter(Qt.Orientation.Vertical)
        self._v_splitter.setHandleWidth(6)
        self._v_splitter.setChildrenCollapsible(False)
        self._v_splitter.setOpaqueResize(True)
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
        self._bottom_tabs.setDocumentMode(True)
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
        self._bottom_tabs.addTab(self._console_container, "⚙ Build Console")

        # ── Serial Monitor tab (placed beside Build Console) ──────────────────
        self._serial_panel = SerialPanel(self._backend)
        self._bottom_tabs.addTab(self._serial_panel, "📡 Serial Monitor")

        # ── Compatible Devices tab ────────────────────────────────────────────
        self._compat_panel = CompatPanel()
        self._compat_panel.connect_signals(sig_bus)
        self._bottom_tabs.addTab(self._compat_panel, "🔧 Compatible Devices")

        # ── Notifications tab ─────────────────────────────────────────────────
        self._notif_panel = NotifPanel(self._backend)
        self._bottom_tabs.addTab(self._notif_panel, "🔔 Notifications")

        # ── Syntax Check tab ──────────────────────────────────────────────────
        self._syntax_panel = SyntaxPanel(self._backend)
        self._bottom_tabs.addTab(self._syntax_panel, "🔍 Syntax Check")

        # ── Terminal tab ──────────────────────────────────────────────────────
        self._terminal_panel = TerminalPanel(self._backend, self)
        self._bottom_tabs.addTab(self._terminal_panel, "🖥 Terminal")

        # Set helpful hover tooltips across all bottom tabs
        self._bottom_tabs.setTabToolTip(0, "Build Console (Ctrl+R to compile, Ctrl+U to upload)")
        self._bottom_tabs.setTabToolTip(1, "Serial Monitor & Real-time MCU Communication")
        self._bottom_tabs.setTabToolTip(2, "Compatible Microcontroller Devices")
        self._bottom_tabs.setTabToolTip(3, "Notifications & Event Log")
        self._bottom_tabs.setTabToolTip(4, "Syntax Diagnostics & Code Analysis")
        self._bottom_tabs.setTabToolTip(5, "Integrated PowerShell / CMD Terminal")

        # Ensure Build Console is always the default active bottom tab on startup
        self._bottom_tabs.setCurrentIndex(0)
        self._bottom_tabs.currentChanged.connect(self._on_bottom_tab_changed)

        def _on_v_splitter_moved(pos: int, index: int) -> None:
            if hasattr(self, "_terminal_panel") and self._terminal_panel and self._terminal_panel.isVisible():
                self._terminal_panel._resize_embedded_terminal()
                QTimer.singleShot(50, self._terminal_panel._resize_embedded_terminal)
                QTimer.singleShot(150, self._terminal_panel._resize_embedded_terminal)

        self._v_splitter.splitterMoved.connect(_on_v_splitter_moved)

        # ── AI side panel (hidden by default) ─────────────────────────────────
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

        # ── Keyboard shortcuts ────────────────────────────────────────────────
        self._setup_shortcuts()

        # ── Connect Global Signals ───────────────────────────────────────────
        from main.qt.signals import signals
        signals.theme_changed.connect(self._on_theme_changed)
        if hasattr(signals, "compat_confirm_requested"):
            signals.compat_confirm_requested.connect(self._on_compat_confirm_requested)

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

    def _on_theme_changed(self, theme_name: str) -> None:
        from main.core.theme import Theme
        from main.qt.theme import build_stylesheet
        Theme.apply_theme(theme_name)
        app = QApplication.instance()
        if app:
            app.setStyleSheet(build_stylesheet(theme_name))
        if hasattr(self, "_editor_panel") and self._editor_panel:
            self._editor_panel.set_theme(theme_name)
        if hasattr(self, "_primary_toolbar") and self._primary_toolbar:
            self._primary_toolbar.apply_theme(theme_name)
        if hasattr(self, "_controls_bar") and self._controls_bar:
            self._controls_bar.apply_theme(theme_name)
        if hasattr(self, "_terminal_panel") and self._terminal_panel:
            self._terminal_panel.apply_theme(theme_name)
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

    def _build_status_bar(self) -> None:
        sb = QStatusBar()
        self.setStatusBar(sb)
        sb.setFixedHeight(24)

        self._status_label = QLabel("Ready")
        self._status_label.setWordWrap(False)
        self._status_label.setFixedHeight(18)
        sb.addWidget(self._status_label, stretch=1)

        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedWidth(180)
        self._progress_bar.setMaximumHeight(12)
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setVisible(False)
        sb.addPermanentWidget(self._progress_bar)

        self._lbl_telemetry = QLabel("CPU: --  RAM: -- GB free")
        self._lbl_telemetry.setStyleSheet("color: #6b7280; font-size: 11px; margin-right: 8px;")
        sb.addPermanentWidget(self._lbl_telemetry)

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._shortcut_compile)
        QShortcut(QKeySequence("Ctrl+U"), self, activated=self._shortcut_upload)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._shortcut_save)
        QShortcut(QKeySequence("Ctrl+Shift+S"), self, activated=self._shortcut_save_all)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self._shortcut_open_project)

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
        sig_bus.project_updated.connect(self._on_project_updated)
        sig_bus.window_closable.connect(self._set_window_closable)

        # Connect child panels
        self._console_container.connect_signals(sig_bus)
        self._serial_panel.connect_signals(sig_bus)
        self._notif_panel.connect_signals(sig_bus)
        self._compat_panel.connect_signals(sig_bus)
        self._syntax_panel.connect_signals(sig_bus)
        self._terminal_panel.connect_signals(sig_bus)
        self._editor_panel.connect_signals(sig_bus)
        self._primary_toolbar.connect_signals(sig_bus)
        self._controls_bar.connect_signals(sig_bus)

        # Toolbar action signals
        sig_bus.file_reload_requested.connect(self._shortcut_reload_file)
        sig_bus.modify_files_requested.connect(self._open_modify_files_dialog)

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
        is_busy: bool = payload.get("is_busy", False)
        phase: str    = payload.get("phase", "idle")
        op: str       = payload.get("op", "")
        if is_busy:
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
            self._status_label.setText(f"⚙ {display_phase}…")
            self._progress_bar.setRange(0, 0)
            self._progress_bar.setTextVisible(False)
            self._progress_bar.setVisible(True)

            is_reset = (
                phase in ("reset", "resetting", "hard_reset", "soft_reset")
                or op in ("reset", "hard_reset", "soft_reset")
            )
            is_build_or_flash = (
                phase in ("compile", "upload", "flash", "flashing")
                or op in ("compile", "upload", "flash")
            )

            # Switch to Build Console when a build/upload or reset starts
            if is_build_or_flash or is_reset:
                if not self._monitors_pane_visible:
                    self.toggle_monitors_pane()
                self._bottom_tabs.setCurrentWidget(self._console_container)

            # Disable Serial Monitor during flash or reset
            if is_reset or phase in ("flash", "flashing", "upload") or op in ("flash", "upload", "reset", "hard_reset", "soft_reset"):
                serial_idx = self._bottom_tabs.indexOf(self._serial_panel)
                if serial_idx >= 0:
                    self._bottom_tabs.setTabEnabled(serial_idx, False)
                if hasattr(self, "_serial_panel") and self._serial_panel:
                    self._serial_panel.setEnabled(False)
        else:
            self._status_label.setText("Ready")
            self._progress_bar.setVisible(False)
            self._progress_bar.setRange(0, 0)

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
            if prev_op in ("upload", "flash", "hard_reset", "soft_reset", "reset") and is_success:
                QTimer.singleShot(500, self._focus_serial_monitor)

    def _focus_serial_monitor(self) -> None:
        """Switch bottom tab to Serial Monitor and set focus (e.g. after upload completes)."""
        if getattr(self, "_active_operation", None) is not None:
            return
        if not self._monitors_pane_visible:
            self.toggle_monitors_pane()
        self._bottom_tabs.setCurrentWidget(self._serial_panel)
        if hasattr(self._serial_panel, "setFocus"):
            self._serial_panel.setFocus()

    @Slot(dict)
    def _on_console_progress(self, payload: dict) -> None:
        action = payload.get("action", "")
        if action.lower() in ("completed", "ready", "idle", "done", "success"):
            if not getattr(self, "_active_operation", None) or getattr(self, "_active_operation", None) in ("clean", "reset", "soft_reset", "hard_reset", "syntax"):
                self._status_label.setText("Ready")
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
            self._status_label.setText(display_action)

    def _trigger_temporary_action(self, action_name: str, duration_ms: int = 700) -> None:
        """Display an indeterminate circulation loading indicator for quick UI actions (e.g. Save, Reload)."""
        if getattr(self, "_active_operation", None):
            return

        display_text = action_name.strip()
        if not (display_text.endswith("…") or display_text.endswith("...")):
            display_text = f"{display_text}…"
        if not display_text.startswith("⚙ "):
            display_text = f"⚙ {display_text}"

        self._status_label.setText(display_text)
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setVisible(True)

        def _finish():
            if getattr(self, "_active_operation", None) is None:
                self._progress_bar.setVisible(False)
                self._progress_bar.setRange(0, 0)
                self._status_label.setText("Ready")

        QTimer.singleShot(duration_ms, _finish)

    @Slot(dict)
    def _on_telemetry(self, payload: dict) -> None:
        cpu = payload.get("cpu_percent", 0)
        ram = payload.get("ram_free_gb", 0)
        self._lbl_telemetry.setText(f"CPU: {cpu:.0f}%  RAM: {ram:.1f} GB free")

    @Slot(dict)
    def _on_notification(self, payload: dict) -> None:
        title = payload.get("title", "")
        msg   = payload.get("message", "")
        ntype = payload.get("type", "info")
        colors = {
            "success": "#4ec994",
            "error":   "#e74c3c",
            "warning": "#f1c40f",
            "info":    "#5ca4f0",
        }
        color = colors.get(ntype, "#cdd6f4")
        self._status_label.setStyleSheet(f"color: {color};")

        # Guard against multi-line text expanding status bar
        if "\n" in msg:
            first_line = msg.split("\n", 1)[0].strip()
            display_text = f"{title}: {first_line}" if title and first_line and not first_line.startswith("•") else (title or first_line)
        else:
            display_text = msg or title

        self._status_label.setText(display_text)
        QTimer.singleShot(5000, lambda: (
            self._status_label.setStyleSheet(""),
            self._status_label.setText("Ready")
        ))

    @Slot(dict)
    def _on_project_updated(self, payload: dict) -> None:
        name = payload.get("name", "")
        if name:
            self.setWindowTitle(f"⚡ MCU Flasher by Naph — {name}")
        path = payload.get("path", "")
        if path and hasattr(self, "_primary_toolbar"):
            self._primary_toolbar.update_sketch_label(path)
        if hasattr(self, "_editor_panel"):
            active = payload.get("active_file", "")
            if active:
                self._editor_panel.open_file(active)
        if hasattr(self, "_ai_panel") and getattr(self._ai_panel, "_is_active", False):
            self._ai_panel.reset_for_project(path)

    # ─────────────────────────────────────────────────────────────────────────
    # Keyboard shortcut handlers
    # ─────────────────────────────────────────────────────────────────────────

    def _shortcut_compile(self) -> None:
        if self._backend and not self._backend.is_busy:
            self._editor_panel.trigger_save_all()
            self._backend.compile_sketch()

    def _shortcut_upload(self) -> None:
        if self._backend and not self._backend.is_busy:
            self._editor_panel.trigger_save_all()
            self._backend.upload_sketch()

    def _shortcut_save(self) -> None:
        self._trigger_temporary_action("Saving", 700)
        self._editor_panel.trigger_save()

    def _shortcut_save_all(self) -> None:
        self._trigger_temporary_action("Saving All", 800)
        self._editor_panel.trigger_save_all()

    def _shortcut_open_project(self) -> None:
        from main.qt.project_dialog import ProjectDialog
        dlg = ProjectDialog(self._backend, parent=self)
        dlg.exec()

    def _open_modify_files_dialog(self) -> None:
        """Open the Modify Project Files dialog."""
        from main.qt.modify_dialog import ModifyFilesDialog
        dlg = ModifyFilesDialog(self._backend, parent=self)
        dlg.exec()

    def _shortcut_reload_file(self) -> None:
        """Reload the active file in the editor from disk."""
        self._trigger_temporary_action("Reloading", 700)
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
                self._controls_bar.set_editor_visible(False)
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
                if hasattr(self._editor_panel, "_view") and self._editor_panel._view:
                    self._editor_panel._view.update()

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
        self._editor_detached = True

        from main.qt.detached_editor import DetachedEditorWindow
        if self._detached_window is None:
            self._detached_window = DetachedEditorWindow(self)
            self._detached_window.closing.connect(self.attach_editor)

        # Center detached window over main window
        geo = self.geometry()
        w, h = 1000, 700
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
        self._on_notification({"type": "info", "message": "✓ Code editor detached to separate window."})

    def attach_editor(self) -> None:
        """Re-embed the detached Monaco editor back into the main window."""
        if not self._editor_detached or self._is_attaching_editor:
            return
        self._is_attaching_editor = True
        try:
            self._editor_detached = False

            # Clear detached window central widget before reparenting
            if self._detached_window is not None:
                self._detached_window.setCentralWidget(QWidget())
                self._detached_window.hide()

            # Place editor back inside _editor_area
            self._editor_area.addWidget(self._editor_panel)
            self._editor_area.setCurrentWidget(self._editor_panel)

            # Ensure editor pane is marked visible
            self._editor_pane_visible = True

            # Synchronize layout back to docked
            self._sync_ai_and_editor_layout()

            # Ensure Chromium paints immediately
            self._editor_panel.show()
            if hasattr(self._editor_panel, "_view") and self._editor_panel._view:
                self._editor_panel._view.update()

            self._on_notification({"type": "info", "message": "✓ Code editor re-attached to main window."})
        finally:
            self._is_attaching_editor = False


    # ─────────────────────────────────────────────────────────────────────────
    # Startup & Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def _on_startup(self) -> None:
        """Post-show initialization: update sketch label and precompiled state."""
        if self._backend:
            path = str(self._backend.sketch_dir_path)
            self._primary_toolbar.update_sketch_label(path)
            name = Path(path).name
            if name:
                self.setWindowTitle(f"⚡ MCU Flasher by Naph — {name}")

            # Notify user if project was precompiled and cached binary is ready
            board = self._backend.current_board
            if board and self._backend.check_can_skip_compile():
                self._backend.emit("console:log", {
                    "text": f"⚡ Project precompiled for {board} — binary cached & ready (upload can skip compile).",
                    "tag": "info",
                    "newline": True,
                })

            # Load active file into Monaco after a short delay to let Qt initialize
            QTimer.singleShot(800, self._load_initial_file)

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

    def closeEvent(self, event: QCloseEvent) -> None:
        if getattr(self._backend, "_framework_download_active", False):
            QMessageBox.warning(
                self,
                "Framework Download in Progress",
                "A critical framework/tool download is currently in progress. "
                "Closing the application now may corrupt your PlatformIO core installation.\n\n"
                "Please wait for the download to finish."
            )
            event.ignore()
            return

        if self._backend and self._backend.is_busy:
            proc = getattr(self._backend, "_active_process", None)
            phase = getattr(self._backend, "_current_op_phase", None)
            kind = getattr(self._backend, "_active_reset_kind", None)
            op = getattr(self._backend, "active_operation", None)

            # Auto-recover: if no process is actually running and not in flash/reset, clear stale busy flag
            if (proc is None or proc.poll() is not None) and op not in ("flash", "reset") and phase not in ("flashing", "writing", "resetting", "erasing"):
                self._backend.is_busy = False
            else:
                # Allow closing if we are currently ONLY in the compilation phase!
                if phase == "compiling" and op != "flash":
                    try:
                        self._backend.stop_operation()
                    except Exception:
                        pass
                    # Proceed to normal exit below
                else:
                    if kind == "hard":
                        msg = ("A Hard Reset (bootloader burn) is in progress.\n\n"
                               "Interrupting this can permanently brick the board. "
                               "Please wait for it to finish.")
                    elif kind == "soft":
                        msg = ("A Soft Reset (flash rewrite) is in progress.\n\n"
                               "Interrupting this can leave the board in a broken state. "
                               "Please wait for it to finish.")
                    else:
                        msg = ("A Flash Upload is currently writing to the MCU memory.\n\n"
                               "Interrupting this direct flash write can leave your board corrupted. "
                               "Please wait for the upload to complete.")
                    QMessageBox.warning(self, "Flash Upload / Reset in Progress", msg)
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

        try:
            from src.modules.crash_detector import mark_session_clean_exit
            mark_session_clean_exit(os.getpid())
        except Exception:
            pass

        event.accept()
