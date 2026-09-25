#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.terminal_panel — Integrated Project Terminal panel for MCU Flasher by Naph.

Faithfully aligned with the original native architecture:
  • Child process isolation: runs src/modules/project_terminal.py with a local HTTP/WS server
  • Real interactive ConPTY sessions (PowerShell & Command Prompt) via pywinpty + xterm.js
  • Clean Win32 HWND embedding (SetParent) into a native Qt widget container
  • True multi-session tabs: sleek VS Code-style tabs ([ pwsh ✕ ], [ cmd ✕ ], [+])
  • Compact toolbar controls: [•], [⌧ Clear], [🗑 Kill] (red), [⛶ Full], [↗ Pop-out]
  • Active theme synchronization: live updates xterm.js colors when theme changes
  • Seamless resizing: dynamically updates terminal geometry without corrupting DirectComposition
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

try:
    import win32con
    import win32gui
    import win32process
except ImportError:
    win32gui = None
    win32con = None
    win32process = None

_this_file = Path(__file__).resolve()
_project_root = _this_file.parent.parent.parent
_SPIN_CHARS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class _EmbedContainer(QWidget):
    """Native window embedding container with automatic geometry synchronization."""

    def __init__(self, panel: "TerminalPanel", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._panel = panel
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setStyleSheet("background: #0a0e14;")
        self.setMinimumSize(40, 40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        self._panel.focus_terminal()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self._panel.focus_terminal()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self._panel, "_resize_embedded_terminal"):
            self._panel._resize_embedded_terminal()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if hasattr(self._panel, "_resize_embedded_terminal"):
            self._panel._resize_embedded_terminal(show=True)
            QTimer.singleShot(30, lambda: self._panel._resize_embedded_terminal(show=True))
            QTimer.singleShot(100, lambda: self._panel._resize_embedded_terminal(show=True))
            QTimer.singleShot(250, self._panel.focus_terminal)


class TerminalPanel(QWidget):
    """
    Bottom dock tab displaying multi-session tabbed Project Terminals.
    Embeds the native xterm.js + ConPTY pywebview window via Win32 SetParent.
    """

    def __init__(self, backend=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._backend = backend
        self._proc: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._port_file: Optional[Path] = None

        self._is_active = False
        self._is_embedded = False
        self._is_ready = False
        self._term_hwnd: Optional[int] = None
        self._original_style: Optional[int] = None
        self._original_ex_style: Optional[int] = None

        self._sessions_meta: dict[str, dict] = {}
        self._active_session_id: Optional[str] = None
        self._session_counter = 0
        self._pending_controls: list[tuple[str, Optional[str], Optional[dict]]] = []

        self._current_theme = "default"
        self._current_font_size = 14
        self._is_fullscreen = False
        self._saved_splitter_sizes: Optional[list[int]] = None

        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(90)
        self._spin_timer.timeout.connect(self._tick_spinner)
        self._spin_idx = 0

        self._embed_poll_timer = QTimer(self)
        self._embed_poll_timer.setInterval(60)
        self._embed_poll_timer.timeout.connect(self._poll_for_terminal_window)
        self._poll_attempts = 0

        self._ready_poll_timer = QTimer(self)
        self._ready_poll_timer.setInterval(100)
        self._ready_poll_timer.timeout.connect(self._poll_for_terminal_readiness)
        self._ready_attempts = 0

        self._build_ui()
        self._update_buttons_state()
        self._apply_panel_theme(self._current_theme)

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Header Bar (Compact VS Code / RECENT-WORKING Parity) ───────────────
        self._header = QFrame()
        header = self._header
        header.setObjectName("terminal-header")
        header.setFixedHeight(28)
        header.setStyleSheet("QFrame#terminal-header { background: #0b0e14; border-bottom: 1px solid #1a2230; }")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(6, 2, 6, 2)
        hl.setSpacing(4)
        self._header_layout = hl

        # ── Left: Session Tabs ([ pwsh ✕ ] [ cmd ✕ ]) ─────────────────────────
        self._tab_bar = QTabBar()
        self._tab_bar.setTabsClosable(True)
        self._tab_bar.setMovable(True)
        self._tab_bar.setDrawBase(False)
        self._tab_bar.setExpanding(False)
        self._tab_bar.currentChanged.connect(self._on_tab_changed)
        self._tab_bar.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tab_bar.setStyleSheet("""
            QTabBar { background: transparent; border: none; }
            QTabBar::tab {
                background: #111620; color: #8fa1b3; border: 1px solid #1c2636;
                border-radius: 3px; padding: 2px 7px; font-family: Consolas, 'Segoe UI', monospace;
                font-size: 11px; font-weight: 600; margin-right: 4px; min-height: 18px;
            }
            QTabBar::tab:selected {
                background: #1c2636; color: #56cfbf; border: 1px solid #3d5069;
            }
            QTabBar::tab:hover:!selected {
                background: #161e2a; color: #cdd6f4;
            }
            QTabBar::close-button {
                subcontrol-position: right; margin-left: 5px;
            }
            QTabBar::close-button:hover {
                background: #e74c3c; border-radius: 2px;
            }
        """)
        hl.addWidget(self._tab_bar)

        # ── + New Terminal dropdown button right next to tabs ────────────────
        self._btn_add_tab = QPushButton("+ ▾")
        self._btn_add_tab.setObjectName("btn-terminal-add")
        self._btn_add_tab.setFixedSize(32, 20)
        self._btn_add_tab.setToolTip("New Terminal (choose PowerShell or Command Prompt)")
        self._btn_add_tab.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add_tab.setStyleSheet("""
            QPushButton#btn-terminal-add {
                background: #10151c; color: #8fa1b3; border: 1px solid #1c2636;
                border-radius: 3px; font-size: 11px; font-weight: bold; padding: 0 2px;
            }
            QPushButton#btn-terminal-add:hover {
                background: #1c2636; color: #56cfbf; border-color: #56cfbf;
            }
        """)
        self._btn_add_tab.clicked.connect(self._show_add_menu)
        hl.addWidget(self._btn_add_tab)

        hl.addStretch()

        # ── Right Toolbar Controls ([•] [⌧ Clear] [🗑 Kill] [⛶ Full]) ─────────
        # Status dot indicator
        self._status_dot = QLabel("●")
        self._status_dot.setObjectName("terminal-status-dot")
        self._status_dot.setFixedSize(14, 20)
        self._status_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_dot.setToolTip("Project Terminal: Ready")
        self._status_dot.setStyleSheet("""
            QLabel#terminal-status-dot {
                background: transparent; border: none;
                color: #4ec994; font-size: 10px; margin-right: 2px;
            }
        """)
        hl.addWidget(self._status_dot)

        # ⌧ Clear button
        self._btn_clear = QPushButton("⌧ Clear")
        self._btn_clear.setObjectName("btn-terminal-clear")
        self._btn_clear.setToolTip("Clear active terminal display")
        self._btn_clear.setFixedHeight(22)
        self._btn_clear.setEnabled(False)
        self._btn_clear.setCursor(Qt.CursorShape.ArrowCursor)
        self._btn_clear.setStyleSheet("""
            QPushButton#btn-terminal-clear:enabled {
                background: #10151c; color: #cdd6f4; font-size: 11px; font-weight: 600;
                border: 1px solid #1c2636; border-radius: 3px; padding: 2px 8px;
            }
            QPushButton#btn-terminal-clear:enabled:hover { background: #1c2636; color: #ffffff; border-color: #3d5069; }
            QPushButton#btn-terminal-clear:disabled {
                background: #0b0e14; color: #4b5563; font-size: 11px;
                border: 1px solid #151b24; border-radius: 3px; padding: 2px 8px;
            }
        """)
        self._btn_clear.clicked.connect(self._clear_active_session)
        hl.addWidget(self._btn_clear)

        # 🗑 Kill button (prominent red styling just like RECENT-WORKING-MCU-FLASHER)
        self._btn_kill = QPushButton("🗑 Kill")
        self._btn_kill.setObjectName("btn-terminal-kill")
        self._btn_kill.setToolTip("Kill active terminal session")
        self._btn_kill.setFixedHeight(22)
        self._btn_kill.setEnabled(False)
        self._btn_kill.setCursor(Qt.CursorShape.ArrowCursor)
        self._btn_kill.setStyleSheet("""
            QPushButton#btn-terminal-kill:enabled {
                background: #10151c; color: #e74c3c; font-size: 11px; font-weight: 600;
                border: 1px solid #1c2636; border-radius: 3px; padding: 2px 8px;
            }
            QPushButton#btn-terminal-kill:enabled:hover {
                background: #251417; color: #ff6b6b; border-color: #e74c3c;
            }
            QPushButton#btn-terminal-kill:disabled {
                background: #0b0e14; color: #4b5563; font-size: 11px;
                border: 1px solid #151b24; border-radius: 3px; padding: 2px 8px;
            }
        """)
        self._btn_kill.clicked.connect(self._kill_active_session)
        hl.addWidget(self._btn_kill)

        # ⛶ Full / Restore button
        self._btn_fullscreen = QPushButton("⛶ Full")
        self._btn_fullscreen.setObjectName("btn-terminal-fullscreen")
        self._btn_fullscreen.setToolTip("Maximize bottom terminal dock height")
        self._btn_fullscreen.setFixedHeight(22)
        self._btn_fullscreen.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_fullscreen.setStyleSheet("""
            QPushButton#btn-terminal-fullscreen {
                background: #10151c; color: #cdd6f4; font-size: 11px; font-weight: 600;
                border: 1px solid #1c2636; border-radius: 3px; padding: 2px 8px;
            }
            QPushButton#btn-terminal-fullscreen:hover {
                background: #1c2636; color: #00d2ff; border-color: #00d2ff;
            }
        """)
        self._btn_fullscreen.clicked.connect(self.toggle_fullscreen)
        hl.addWidget(self._btn_fullscreen)

        main_layout.addWidget(header)

        # ── Stacked View (Loader, Embedded Container, Empty Placeholder) ─────
        self._stack = QStackedWidget(self)

        # 0. Loading View
        self._loader_card = QFrame()
        self._loader_card.setStyleSheet("QFrame { background: #0a0e14; border: none; }")
        lv = QVBoxLayout(self._loader_card)
        lv.setContentsMargins(20, 20, 20, 20)
        lv.setSpacing(10)
        lv.addStretch()

        self._spin_lbl = QLabel("⠋")
        self._spin_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._spin_lbl.setStyleSheet("font-size: 26px; color: #56cfbf;")
        lv.addWidget(self._spin_lbl)

        self._load_title = QLabel("Initializing Project Terminal…")
        self._load_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._load_title.setStyleSheet("color: #56cfbf; font-size: 13px; font-weight: 700; font-family: 'Segoe UI', Consolas, sans-serif;")
        lv.addWidget(self._load_title)

        self._load_sub = QLabel("Preparing ConPTY terminal engine & environment…")
        self._load_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._load_sub.setStyleSheet("color: #8fa1b3; font-size: 11px; font-family: 'Segoe UI', Consolas, sans-serif;")
        self._load_sub.setWordWrap(True)
        lv.addWidget(self._load_sub)
        lv.addStretch()

        self._stack.addWidget(self._loader_card)

        # 1. Native Embedding Container View
        self._embed_container = _EmbedContainer(self)
        self._stack.addWidget(self._embed_container)

        # 2. Empty state card
        self._empty_card = QFrame()
        self._empty_card.setStyleSheet("QFrame { background: #0a0e14; border: none; }")
        el = QVBoxLayout(self._empty_card)
        el.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.setSpacing(10)

        self._lbl_no_term = QLabel("No active terminal sessions")
        lbl_no_term = self._lbl_no_term
        lbl_no_term.setStyleSheet("color: #64748b; font-size: 13px; font-weight: 600;")
        el.addWidget(lbl_no_term, alignment=Qt.AlignmentFlag.AlignCenter)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._btn_open_pwsh = QPushButton("+ PowerShell (pwsh)")
        self._btn_open_pwsh.setObjectName("btn-open-pwsh")
        self._btn_open_pwsh.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_open_pwsh.setFixedHeight(28)
        self._btn_open_pwsh.setStyleSheet("""
            QPushButton#btn-open-pwsh {
                background: #10151c; color: #56cfbf; font-size: 11px; font-weight: 600;
                border: 1px solid #1c2636; border-radius: 4px; padding: 4px 14px;
            }
            QPushButton#btn-open-pwsh:hover { background: #1c2636; color: #ffffff; border-color: #56cfbf; }
        """)
        self._btn_open_pwsh.clicked.connect(lambda: self.add_session("pwsh"))
        btn_row.addWidget(self._btn_open_pwsh)

        self._btn_open_cmd = QPushButton("+ Command Prompt (cmd)")
        self._btn_open_cmd.setObjectName("btn-open-cmd")
        self._btn_open_cmd.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_open_cmd.setFixedHeight(28)
        self._btn_open_cmd.setStyleSheet("""
            QPushButton#btn-open-cmd {
                background: #10151c; color: #cdd6f4; font-size: 11px; font-weight: 600;
                border: 1px solid #1c2636; border-radius: 4px; padding: 4px 14px;
            }
            QPushButton#btn-open-cmd:hover { background: #1c2636; color: #ffffff; border-color: #3d5069; }
        """)
        self._btn_open_cmd.clicked.connect(lambda: self.add_session("cmd"))
        btn_row.addWidget(self._btn_open_cmd)

        self._btn_new_empty = self._btn_open_pwsh
        el.addLayout(btn_row)

        self._stack.addWidget(self._empty_card)

        self._stack.setCurrentWidget(self._empty_card)
        main_layout.addWidget(self._stack, stretch=1)

    def _show_add_menu(self, pos=None) -> None:
        """Dropdown menu to choose between Command Prompt and PowerShell."""
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background: #10151c; color: #cdd6f4; border: 1px solid #1c2636;
                border-radius: 4px; font-size: 11px; padding: 4px;
                font-family: Consolas, 'Segoe UI', monospace;
            }
            QMenu::item { padding: 6px 18px; border-radius: 3px; }
            QMenu::item:selected { background: #1c2636; color: #00d2ff; }
        """)
        act_c = menu.addAction("Command Prompt (cmd)")
        act_c.triggered.connect(lambda: self.add_session("cmd"))
        act_p = menu.addAction("PowerShell (pwsh)")
        act_p.triggered.connect(lambda: self.add_session("pwsh"))

        btn_rect = self._btn_add_tab.rect()
        popup_pos = self._btn_add_tab.mapToGlobal(btn_rect.bottomLeft())
        menu.exec(popup_pos)

    def toggle_fullscreen(self) -> None:
        """Toggle bottom dock height between normal and maximized (~85% height)."""
        main_win = self.window()
        if not hasattr(main_win, "_v_splitter"):
            return
        splitter = main_win._v_splitter
        total_h = sum(splitter.sizes())
        if total_h <= 100:
            total_h = max(main_win.height(), 600)

        if not getattr(self, "_is_fullscreen", False):
            self._saved_splitter_sizes = splitter.sizes()
            self._is_fullscreen = True
            top_h = max(int(total_h * 0.12), 60)
            bot_h = total_h - top_h
            splitter.setSizes([top_h, bot_h])
            self._btn_fullscreen.setText("⛶ Restore")
            self._btn_fullscreen.setToolTip("Restore terminal to normal height")
        else:
            self._is_fullscreen = False
            saved = getattr(self, "_saved_splitter_sizes", None)
            if saved and len(saved) == 2 and sum(saved) > 100:
                splitter.setSizes(saved)
            else:
                splitter.setSizes([int(total_h * 0.55), int(total_h * 0.45)])
            self._btn_fullscreen.setText("⛶ Full")
            self._btn_fullscreen.setToolTip("Maximize bottom terminal dock height")

        QTimer.singleShot(30, self._resize_embedded_terminal)
        QTimer.singleShot(100, self._resize_embedded_terminal)
        QTimer.singleShot(200, self.focus_terminal)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())
        if hasattr(self, "_resize_embedded_terminal"):
            self._resize_embedded_terminal()

    def set_responsive_width(self, width: int) -> None:
        """Dynamically adapt terminal header buttons based on width."""
        if width < 700:
            self._btn_clear.setText("⌧")
            self._btn_kill.setText("🗑")
            self._btn_fullscreen.setText("⛶")
        else:
            self._btn_clear.setText("⌧ Clear")
            self._btn_kill.setText("🗑 Kill")
            self._btn_fullscreen.setText("⛶ Restore" if getattr(self, "_is_fullscreen", False) else "⛶ Full")

    def _tick_spinner(self) -> None:
        self._spin_idx = (self._spin_idx + 1) % len(_SPIN_CHARS)
        self._spin_lbl.setText(_SPIN_CHARS[self._spin_idx])

    def _get_target_dir(self) -> str:
        if self._backend and hasattr(self._backend, "sketch_dir_path") and self._backend.sketch_dir_path:
            p = Path(self._backend.sketch_dir_path)
            if p.exists():
                return str(p)
        return str(_project_root)

    # ── Startup & Embedding ──────────────────────────────────────────────────
    def ensure_started(self) -> None:
        """Ensure the project terminal child process is running."""
        if not self._is_active:
            self._start_terminal()

    def _start_terminal(self) -> None:
        script_path = _project_root / "src" / "modules" / "project_terminal.py"
        if not script_path.exists():
            QMessageBox.critical(self, "Terminal Error", f"Missing terminal script at {script_path}")
            return

        from src.modules.private_python_guard import get_private_python_exe
        py_exe = get_private_python_exe(prefer_pythonw=True)
        target_dir = str(Path(self._get_target_dir()).resolve())

        try:
            fd, port_file = tempfile.mkstemp(prefix="mcu-terminal-", suffix=".json")
            os.close(fd)
            try:
                os.unlink(port_file)
            except OSError:
                pass
            self._port_file = Path(port_file)
        except Exception:
            self._port_file = _project_root / "temp" / f"terminal_port_{os.getpid()}.json"

        # Show loading view
        self._is_ready = False
        self._poll_attempts = 0
        self._ready_attempts = 0
        self._stack.setCurrentWidget(self._loader_card)
        self._status_dot.setStyleSheet(
            "QLabel#terminal-status-dot { background: transparent; border: none; color: #f1c40f; font-size: 10px; margin-right: 2px; }"
        )
        self._status_dot.setToolTip("Project Terminal: Initializing ConPTY engine…")
        self._load_title.setText("Initializing Project Terminal…")
        self._load_sub.setText("Preparing ConPTY terminal engine & environment…")
        self._spin_timer.start()

        cmd = [
            str(py_exe),
            str(script_path),
            "--launch-terminal", target_dir,
            "--initial-cwd", target_dir,
            "--port-file", str(self._port_file),
        ]

        try:
            creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(_project_root),
                creationflags=creationflags,
            )
            self._is_active = True
            self._poll_attempts = 0
            self._embed_poll_timer.start()
        except Exception as e:
            self._spin_timer.stop()
            self._status_dot.setStyleSheet(
                "QLabel#terminal-status-dot { background: transparent; border: none; color: #e74c3c; font-size: 10px; margin-right: 2px; }"
            )
            self._status_dot.setToolTip("Project Terminal: Startup error")
            self._load_title.setText("Terminal Startup Error")
            self._load_sub.setText(f"Failed to start Project Terminal: {e}")
            QMessageBox.critical(self, "Terminal Error", f"Failed to start Project Terminal:\n\n{e}")

    def _poll_for_terminal_window(self) -> None:
        """Poll for the hidden pywebview OS window and embed into self._embed_container."""
        self._poll_attempts += 1
        if win32gui is None or win32con is None:
            self._embed_poll_timer.stop()
            self._spin_timer.stop()
            return

        if self._port_file and self._port_file.exists():
            try:
                data = json.loads(self._port_file.read_text(encoding="utf-8"))
                if "port" in data:
                    self._port = int(data["port"])
            except Exception:
                pass

        hwnd = ctypes.windll.user32.FindWindowW(None, "MCU Flash GUI - Project Terminal")
        if not hwnd or hwnd == 0:
            if self._poll_attempts > 240:  # ~15 seconds timeout
                self._embed_poll_timer.stop()
                self._spin_timer.stop()
                self._status_dot.setStyleSheet(
                    "QLabel#terminal-status-dot { background: transparent; border: none; color: #e74c3c; font-size: 10px; margin-right: 2px; }"
                )
                self._status_dot.setToolTip("Project Terminal: Connection timed out")
                self._load_title.setText("Connection Timed Out")
                self._load_sub.setText("Could not attach to the Project Terminal window.")
            return

        # Check if window is responsive
        user32 = ctypes.windll.user32
        if user32.IsHungAppWindow(hwnd):
            return

        self._embed_poll_timer.stop()
        self._embed_terminal_hwnd(hwnd)

        self._load_title.setText("Starting Project Terminal…")
        self._load_sub.setText("Loading interactive shell session…")
        self._ready_attempts = 0
        self._ready_poll_timer.start()

    def _embed_terminal_hwnd(self, hwnd: int) -> None:
        """Reparent the native pywebview window into the Qt container."""
        if win32gui is None or win32con is None:
            return

        try:
            container_hwnd = int(self._embed_container.winId())

            # Ensure WS_CLIPCHILDREN is set on container and stack so Qt paint events don't paint over terminal
            try:
                c_style = win32gui.GetWindowLong(container_hwnd, win32con.GWL_STYLE)
                win32gui.SetWindowLong(container_hwnd, win32con.GWL_STYLE, c_style | win32con.WS_CLIPCHILDREN)
                s_hwnd = int(self._stack.winId())
                s_style = win32gui.GetWindowLong(s_hwnd, win32con.GWL_STYLE)
                win32gui.SetWindowLong(s_hwnd, win32con.GWL_STYLE, s_style | win32con.WS_CLIPCHILDREN)
            except Exception:
                pass

            if self._original_style is None:
                self._original_style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                self._original_ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)

            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            style &= ~(win32con.WS_CAPTION | win32con.WS_THICKFRAME |
                       win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX |
                       win32con.WS_SYSMENU | win32con.WS_POPUP | win32con.WS_BORDER)
            style |= win32con.WS_CHILD
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)

            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            ex_style &= ~(win32con.WS_EX_DLGMODALFRAME | win32con.WS_EX_APPWINDOW |
                          win32con.WS_EX_WINDOWEDGE | win32con.WS_EX_CLIENTEDGE)
            ex_style |= win32con.WS_EX_TOOLWINDOW
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

            win32gui.SetParent(hwnd, container_hwnd)
            self._term_hwnd = hwnd
            self._is_embedded = True

            if not self._is_ready:
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                self._resize_embedded_terminal(show=False)
                self._stack.setCurrentWidget(self._loader_card)
            else:
                should_show = self.isVisible()
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW if should_show else win32con.SW_HIDE)
                self._resize_embedded_terminal(show=should_show)
                if len(self._sessions_meta) > 0:
                    self._stack.setCurrentWidget(self._embed_container)
                else:
                    self._stack.setCurrentWidget(self._empty_card)
                self._status_dot.setStyleSheet(
                    "QLabel#terminal-status-dot { background: transparent; border: none; color: #4ec994; font-size: 10px; margin-right: 2px; }"
                )
                self._status_dot.setToolTip("Project Terminal: Ready")
        except Exception as e:
            print(f"[MCU Flasher] Error embedding Project Terminal window: {e}")

    def _poll_for_terminal_readiness(self) -> None:
        """Poll until the terminal engine reports ready before revealing."""
        self._ready_attempts += 1

        if self._proc and self._proc.poll() is not None:
            self._ready_poll_timer.stop()
            self._spin_timer.stop()
            self._status_dot.setStyleSheet(
                "QLabel#terminal-status-dot { background: transparent; border: none; color: #e74c3c; font-size: 10px; margin-right: 2px; }"
            )
            self._status_dot.setToolTip("Project Terminal: Exited")
            self._load_title.setText("Terminal Exited")
            self._load_sub.setText("The terminal subprocess terminated unexpectedly.")
            return

        is_ready = False
        if self._port_file and self._port_file.exists():
            try:
                data = json.loads(self._port_file.read_text(encoding="utf-8"))
                if not self._port and "port" in data:
                    self._port = int(data["port"])
                if data.get("ready") is True:
                    is_ready = True
            except Exception:
                pass

        if is_ready or (self._ready_attempts >= 100):  # ~10s safety fallback
            self._ready_poll_timer.stop()
            self._spin_timer.stop()
            self._is_ready = True

            # Flush queued control actions
            self._flush_pending_controls()

            # Apply active theme to xterm
            self.apply_theme(self._current_theme)

            has_sessions = len(self._sessions_meta) > 0
            if has_sessions:
                self.refresh_terminal()
            else:
                self._stack.setCurrentWidget(self._empty_card)

            self._status_dot.setStyleSheet(
                "QLabel#terminal-status-dot { background: transparent; border: none; color: #4ec994; font-size: 10px; margin-right: 2px; }"
            )
            self._status_dot.setToolTip("Project Terminal: Ready")
            self._update_buttons_state()

    # ── Session Management ───────────────────────────────────────────────────
    def add_session(self, kind: str = "cmd") -> None:
        """Create and append a new terminal session (Command Prompt or PowerShell)."""
        self.ensure_started()
        self._session_counter += 1
        num = self._session_counter
        session_id = f"{kind}_{num}"
        existing_of_kind = [m for m in self._sessions_meta.values() if m.get("kind") == kind]
        title = kind if len(existing_of_kind) == 0 else f"{kind} {len(existing_of_kind) + 1}"

        self._sessions_meta[session_id] = {
            "id": session_id,
            "kind": kind,
            "title": title,
        }

        # Add tab to QTabBar
        self._tab_bar.blockSignals(True)
        idx = self._tab_bar.addTab(title)
        self._tab_bar.setTabData(idx, session_id)
        self._tab_bar.setCurrentIndex(idx)
        self._tab_bar.blockSignals(False)

        self._active_session_id = session_id
        self._send_control("new", session_id, extra={"kind": kind, "title": title})

        if self._is_ready:
            self.refresh_terminal()
        else:
            self._stack.setCurrentWidget(self._loader_card)

        self._status_dot.setStyleSheet(
            "QLabel#terminal-status-dot { background: transparent; border: none; color: #4ec994; font-size: 10px; margin-right: 2px; }"
        )
        self._status_dot.setToolTip(f"Project Terminal: Running ({title})")
        self._update_buttons_state()

    def _on_tab_changed(self, index: int) -> None:
        """Handle user selecting a different session tab."""
        if index < 0 or index >= self._tab_bar.count():
            return
        session_id = self._tab_bar.tabData(index)
        if session_id:
            self._active_session_id = session_id
            self._send_control("select", session_id)
            self._update_buttons_state()
            self.refresh_terminal()
            QTimer.singleShot(50, self.focus_terminal)

    def _on_tab_close_requested(self, index: int) -> None:
        """Handle user clicking close button (✕) on a session tab."""
        if index < 0 or index >= self._tab_bar.count():
            return
        session_id = self._tab_bar.tabData(index)
        self._tab_bar.removeTab(index)

        if session_id in self._sessions_meta:
            del self._sessions_meta[session_id]
            self._send_control("kill", session_id)

        if self._tab_bar.count() == 0:
            self._active_session_id = None
            self._stack.setCurrentWidget(self._empty_card)
            if self._term_hwnd and win32gui and win32con and win32gui.IsWindow(int(self._term_hwnd)):
                try:
                    win32gui.ShowWindow(int(self._term_hwnd), win32con.SW_HIDE)
                except Exception:
                    pass
            self._status_dot.setStyleSheet(
                "QLabel#terminal-status-dot { background: transparent; border: none; color: #64748b; font-size: 10px; margin-right: 2px; }"
            )
            self._status_dot.setToolTip("Project Terminal: No active sessions")
        else:
            curr_idx = self._tab_bar.currentIndex()
            if curr_idx >= 0:
                new_sid = self._tab_bar.tabData(curr_idx)
                self._active_session_id = new_sid
                self._send_control("select", new_sid)

        self._update_buttons_state()

    def _kill_active_session(self) -> None:
        """Kill the currently selected terminal session."""
        curr_idx = self._tab_bar.currentIndex()
        if curr_idx >= 0:
            self._on_tab_close_requested(curr_idx)

    def _clear_active_session(self) -> None:
        if self._active_session_id:
            self._send_control("clear", self._active_session_id)

    def _update_buttons_state(self) -> None:
        has_active = bool(self._tab_bar.count() > 0 and self._active_session_id)
        self._btn_kill.setEnabled(has_active)
        self._btn_kill.setCursor(Qt.CursorShape.PointingHandCursor if has_active else Qt.CursorShape.ArrowCursor)
        self._btn_clear.setEnabled(has_active)
        self._btn_clear.setCursor(Qt.CursorShape.PointingHandCursor if has_active else Qt.CursorShape.ArrowCursor)

    # ── Control Message Protocol ─────────────────────────────────────────────
    def _send_control(self, action: str, shell_id: Optional[str] = None, extra: Optional[dict] = None) -> None:
        """Send a control command to the project terminal HTTP endpoint."""
        port = self._port
        if not port:
            self._pending_controls.append((action, shell_id, extra))
            return

        payload = {"action": action, "shell": shell_id or ""}
        if extra:
            payload.update(extra)
        data = json.dumps(payload).encode("utf-8")
        url = f"http://127.0.0.1:{int(port)}/control"

        def _worker():
            try:
                import urllib.request
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=2.0):
                    pass
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True, name="TerminalSendControl").start()

    def _flush_pending_controls(self) -> None:
        if not self._port or not self._pending_controls:
            return
        actions = list(self._pending_controls)
        self._pending_controls.clear()
        for action, shell_id, extra in actions:
            self._send_control(action, shell_id, extra)

    # ── Geometry & Sizing ────────────────────────────────────────────────────
    def focus_terminal(self) -> None:
        """Focus the embedded native terminal window so keyboard input flows directly to ConPTY."""
        if not self._term_hwnd or win32gui is None or win32con is None:
            return
        if not win32gui.IsWindow(int(self._term_hwnd)):
            return
        try:
            hwnd = int(self._term_hwnd)
            cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            target_tid, _ = win32process.GetWindowThreadProcessId(hwnd) if win32process else (0, 0)
            if target_tid and target_tid != cur_tid:
                ctypes.windll.user32.AttachThreadInput(cur_tid, target_tid, True)
                try:
                    ctypes.windll.user32.SetFocus(hwnd)
                    ctypes.windll.user32.SetActiveWindow(hwnd)
                finally:
                    ctypes.windll.user32.AttachThreadInput(cur_tid, target_tid, False)
            else:
                try:
                    win32gui.SetFocus(hwnd)
                except Exception:
                    pass

            def _find_leaf(c_hwnd, acc):
                acc.append(c_hwnd)
                return True
            leaves = []
            win32gui.EnumChildWindows(hwnd, _find_leaf, leaves)
            for c in reversed(leaves):
                cname = win32gui.GetClassName(c)
                if "Chrome_RenderWidgetHostHWND" in cname or "Chrome_WidgetWin" in cname:
                    c_tid, _ = win32process.GetWindowThreadProcessId(c) if win32process else (0, 0)
                    if c_tid and c_tid != cur_tid:
                        ctypes.windll.user32.AttachThreadInput(cur_tid, c_tid, True)
                        try:
                            ctypes.windll.user32.SetFocus(c)
                            ctypes.windll.user32.SetActiveWindow(c)
                        finally:
                            ctypes.windll.user32.AttachThreadInput(cur_tid, c_tid, False)
                    else:
                        try:
                            win32gui.SetFocus(c)
                        except Exception:
                            pass
                    break
        except Exception:
            pass

    def refresh_terminal(self) -> None:
        """Self-refresh the terminal view, geometry, and xterm layout."""
        if not self._is_active:
            self.ensure_started()
            return
        has_sessions = len(self._sessions_meta) > 0
        if has_sessions:
            self._stack.setCurrentWidget(self._embed_container)
            if self._is_embedded and self._term_hwnd and win32gui and win32gui.IsWindow(int(self._term_hwnd)):
                win32gui.ShowWindow(int(self._term_hwnd), win32con.SW_SHOW)
                self._resize_embedded_terminal(show=True)
                QTimer.singleShot(30, lambda: self._resize_embedded_terminal(show=True))
                QTimer.singleShot(100, lambda: self._resize_embedded_terminal(show=True))
                QTimer.singleShot(250, self.focus_terminal)
        else:
            self._stack.setCurrentWidget(self._empty_card)
            if self._is_embedded and self._term_hwnd and win32gui and win32gui.IsWindow(int(self._term_hwnd)):
                try:
                    win32gui.ShowWindow(int(self._term_hwnd), win32con.SW_HIDE)
                except Exception:
                    pass
        if self._port and self._is_ready:
            self._send_control("fit")

    def _resize_embedded_terminal(self, show: Optional[bool] = None) -> None:
        """
        Resize the embedded Win32 window to match the Qt container.
        Synchronizes both top-level and child HWNDs (DirectComposition / Chromium WebView2).
        """
        if not self._term_hwnd or not self._is_embedded or win32gui is None or win32con is None:
            return
        try:
            if not win32gui.IsWindow(int(self._term_hwnd)):
                self._term_hwnd = None
                return

            w = self._embed_container.width()
            h = self._embed_container.height()
            if w <= 10 or h <= 10:
                w = max(self._stack.width(), self.width(), 100)
                h = max(self._stack.height(), self.height() - 28, 60)

            has_sessions = len(self._sessions_meta) > 0
            should_show = (self.isVisible() and self._is_ready and has_sessions) if show is None else show
            hwnd = int(self._term_hwnd)

            flags = (
                win32con.SWP_FRAMECHANGED
                | win32con.SWP_NOZORDER
                | win32con.SWP_NOACTIVATE
                | 0x4000  # SWP_ASYNCWINDOWPOS
            )
            if should_show:
                flags |= win32con.SWP_SHOWWINDOW
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            else:
                flags |= win32con.SWP_HIDEWINDOW
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)

            win32gui.SetWindowPos(hwnd, 0, 0, 0, w, h, flags)

            def _enum_child(c_hwnd, _):
                try:
                    win32gui.SetWindowPos(c_hwnd, 0, 0, 0, w, h, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
                except Exception:
                    pass
                return True

            try:
                win32gui.EnumChildWindows(hwnd, _enum_child, None)
            except Exception:
                pass

            # Notify the terminal server over IPC to trigger xterm fit
            if self._port and self._is_ready and should_show:
                self._send_control("fit")
        except Exception:
            pass

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if len(self._sessions_meta) == 0:
            self.add_session("cmd")
        else:
            self.ensure_started()
            self.refresh_terminal()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if self._is_embedded and self._term_hwnd and win32gui and win32gui.IsWindow(int(self._term_hwnd)):
            try:
                ctypes.windll.user32.ShowWindowAsync(int(self._term_hwnd), int(win32con.SW_HIDE))
            except Exception:
                try:
                    win32gui.ShowWindow(int(self._term_hwnd), win32con.SW_HIDE)
                except Exception:
                    pass

    def _on_tab_revealed(self) -> None:
        """Called when bottom dock notebook switches onto this tab."""
        if len(self._sessions_meta) == 0:
            self.add_session("cmd")
        else:
            self.ensure_started()
            self.refresh_terminal()
            QTimer.singleShot(100, self.focus_terminal)

    def _on_tab_hidden(self) -> None:
        """Called when bottom dock notebook switches away from this tab."""
        if self._is_embedded and self._term_hwnd and win32gui and win32gui.IsWindow(int(self._term_hwnd)):
            try:
                ctypes.windll.user32.ShowWindowAsync(int(self._term_hwnd), int(win32con.SW_HIDE))
            except Exception:
                try:
                    win32gui.ShowWindow(int(self._term_hwnd), win32con.SW_HIDE)
                except Exception:
                    pass

    # ── Theme & Font Configuration ───────────────────────────────────────────
    def _build_terminal_theme_payload(self, theme_mode: str) -> dict:
        from main.qt.theme import get_palette
        pal = get_palette(theme_mode)
        bg = pal.get("BG_DARKEST", "#0a0e14")
        fg = pal.get("TEXT", "#e0e6ed")
        cyan = pal.get("CYAN", "#00d2ff")
        hover = pal.get("BG_HOVER", "#1c2636")
        return {
            "background": bg,
            "foreground": fg,
            "cursor": cyan,
            "selectionBackground": hover,
            "black": pal.get("TEXT_DIM", "#8fa1b3"),
            "red": pal.get("BTN_STOP", "#f05050"),
            "green": pal.get("BTN_COMPILE", "#5ccc6e"),
            "yellow": "#e8b83a",
            "blue": "#61afef",
            "magenta": "#c678dd",
            "cyan": cyan,
            "white": pal.get("TEXT_BRIGHT", "#ffffff"),
            "brightBlack": pal.get("TEXT_DIM", "#8fa1b3"),
            "brightRed": pal.get("BTN_STOP_H", "#ff6b6b"),
            "brightGreen": pal.get("BTN_COMPILE_H", "#69db7c"),
            "brightYellow": "#ffd43b",
            "brightBlue": "#74c0fc",
            "brightMagenta": "#da77f2",
            "brightCyan": cyan,
            "brightWhite": "#ffffff",
        }

    def _apply_panel_theme(self, theme_mode: str) -> None:
        from main.qt.theme import get_palette
        pal = get_palette(theme_mode)
        bg_mid = pal.get("BG_MID", "#111620")
        bg_dark = pal.get("BG_DARK", "#0e131b")
        bg_darkest = pal.get("BG_DARKEST", "#0a0e14")
        bg_hover = pal.get("BG_HOVER", "#1c2636")
        border = pal.get("BORDER", "#1c2636")
        border_lit = pal.get("BORDER_LIT", "#00d2ff")
        cyan = pal.get("CYAN", "#00d2ff")
        text = pal.get("TEXT", "#e0e6ed")
        text_dim = pal.get("TEXT_DIM", "#8fa1b3")
        text_bright = pal.get("TEXT_BRIGHT", "#ffffff")

        if hasattr(self, "_header") and self._header:
            self._header.setStyleSheet(f"QFrame#terminal-header {{ background: {bg_dark}; border-bottom: 1px solid {border}; }}")

        if hasattr(self, "_tab_bar") and self._tab_bar:
            self._tab_bar.setStyleSheet(f"""
                QTabBar {{ background: transparent; border: none; }}
                QTabBar::tab {{
                    background: {bg_dark}; color: {text_dim}; border: 1px solid {border};
                    border-radius: 3px; padding: 2px 7px; font-family: Consolas, 'Segoe UI', monospace;
                    font-size: 11px; font-weight: 600; margin-right: 4px; min-height: 18px;
                }}
                QTabBar::tab:selected {{
                    background: {bg_hover}; color: {cyan}; border: 1px solid {cyan};
                }}
                QTabBar::tab:hover:!selected {{
                    background: {bg_hover}; color: {text_bright};
                }}
                QTabBar::close-button {{
                    subcontrol-position: right; margin-left: 5px;
                }}
                QTabBar::close-button:hover {{
                    background: #e74c3c; border-radius: 2px;
                }}
            """)

        if hasattr(self, "_btn_add_tab") and self._btn_add_tab:
            self._btn_add_tab.setStyleSheet(f"""
                QPushButton#btn-terminal-add {{
                    background: {bg_dark}; color: {text_dim}; border: 1px solid {border};
                    border-radius: 3px; font-size: 13px; font-weight: bold; padding: 0;
                }}
                QPushButton#btn-terminal-add:hover {{
                    background: {bg_hover}; color: {cyan}; border-color: {cyan};
                }}
            """)

        btn_act_style = f"""
            QPushButton:enabled {{
                background: {bg_dark}; color: {text}; font-size: 11px; font-weight: 600;
                border: 1px solid {border}; border-radius: 3px; padding: 2px 8px;
            }}
            QPushButton:enabled:hover {{ background: {bg_hover}; color: {text_bright}; border-color: {border_lit}; }}
            QPushButton:disabled {{
                background: {bg_darkest}; color: {text_dim}; font-size: 11px;
                border: 1px solid {border}; border-radius: 3px; padding: 2px 8px;
            }}
        """
        if hasattr(self, "_btn_clear") and self._btn_clear:
            self._btn_clear.setStyleSheet(btn_act_style)
        if hasattr(self, "_btn_fullscreen") and self._btn_fullscreen:
            self._btn_fullscreen.setStyleSheet(f"""
                QPushButton#btn-terminal-fullscreen {{
                    background: {bg_dark}; color: {text}; font-size: 11px; font-weight: 600;
                    border: 1px solid {border}; border-radius: 3px; padding: 2px 8px;
                }}
                QPushButton#btn-terminal-fullscreen:hover {{ background: {bg_hover}; color: {cyan}; border-color: {cyan}; }}
            """)

        if hasattr(self, "_loader_card") and self._loader_card:
            self._loader_card.setStyleSheet(f"QFrame {{ background: {bg_darkest}; border: none; }}")
        if hasattr(self, "_empty_card") and self._empty_card:
            self._empty_card.setStyleSheet(f"QFrame {{ background: {bg_darkest}; border: none; }}")
        if hasattr(self, "_embed_container") and self._embed_container:
            self._embed_container.setStyleSheet(f"background: {bg_darkest};")
        if hasattr(self, "_btn_open_pwsh") and self._btn_open_pwsh:
            self._btn_open_pwsh.setStyleSheet(f"""
                QPushButton#btn-open-pwsh {{
                    background: {bg_dark}; color: {cyan}; font-size: 11px; font-weight: 600;
                    border: 1px solid {border}; border-radius: 4px; padding: 4px 14px;
                }}
                QPushButton#btn-open-pwsh:hover {{ background: {bg_hover}; color: {text_bright}; border-color: {cyan}; }}
            """)
        if hasattr(self, "_btn_open_cmd") and self._btn_open_cmd:
            self._btn_open_cmd.setStyleSheet(f"""
                QPushButton#btn-open-cmd {{
                    background: {bg_dark}; color: {text}; font-size: 11px; font-weight: 600;
                    border: 1px solid {border}; border-radius: 4px; padding: 4px 14px;
                }}
                QPushButton#btn-open-cmd:hover {{ background: {bg_hover}; color: {text_bright}; border-color: {border_lit}; }}
            """)

    def apply_theme(self, theme_name: str) -> None:
        """Update xterm.js theme and Qt container styles on theme change."""
        self._current_theme = theme_name
        self._apply_panel_theme(theme_name)
        payload = self._build_terminal_theme_payload(theme_name)
        self._send_control("theme", extra={"theme": payload})

    def set_font_size(self, size: int) -> None:
        try:
            self._current_font_size = int(size)
        except (ValueError, TypeError):
            self._current_font_size = 14

    def connect_signals(self, sig_bus) -> None:
        if hasattr(sig_bus, "font_size_changed"):
            sig_bus.font_size_changed.connect(self.set_font_size)
        if hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.apply_theme)

    # ── Project Realignment ──────────────────────────────────────────────────
    def reset_for_project(self, new_project_dir: str) -> None:
        """Reset terminal state on project change."""
        was_visible = self.isVisible()
        if self._is_active:
            self._stop_shell()
        self._is_active = False
        self._is_embedded = False
        self._is_ready = False
        self._term_hwnd = None
        self._sessions_meta.clear()
        self._session_counter = 0
        self._active_session_id = None
        self._tab_bar.blockSignals(True)
        while self._tab_bar.count() > 0:
            self._tab_bar.removeTab(0)
        self._tab_bar.blockSignals(False)
        self._stack.setCurrentWidget(self._empty_card)
        self._update_buttons_state()
        if was_visible:
            self.add_session("cmd")

    # ── Cleanup & Shutdown ───────────────────────────────────────────────────
    def _stop_shell(self) -> None:
        """Cleanly terminate child process tree on shutdown."""
        self._embed_poll_timer.stop()
        self._ready_poll_timer.stop()
        self._spin_timer.stop()

        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if sys.platform == "win32":
                    subprocess.Popen(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        stdin=subprocess.DEVNULL,
                        creationflags=0x08000000,
                    )
                else:
                    proc.terminate()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        if self._port_file:
            try:
                self._port_file.unlink(missing_ok=True)
            except Exception:
                pass
            self._port_file = None
        self._port = None

    def closeEvent(self, event) -> None:
        self._stop_shell()
        super().closeEvent(event)
