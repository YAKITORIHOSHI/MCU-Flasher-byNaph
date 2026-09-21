#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.ai_panel — OpenCode AI Assistant Panel for MCU Flasher by Naph.

Faithfully aligned with the original stable release:
  • No offline cover or launch button — automatically loads directly into AI when opened
  • Persistent background process: once started, toggling only hides or shows the panel
  • Automatic project realignment: session only resets when the active sketch project changes
  • Real interactive OpenCode xterm.js + ConPTY terminal hosting via Win32 HWND embedding (SetParent)
  • Pop-out / Detach (↗) to floating window and Reattach (↙) to side panel
"""
from __future__ import annotations

import sys
import json
import time
import ctypes
import subprocess
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QMessageBox, QStackedWidget, QSizePolicy,
)

try:
    import win32gui
    import win32con
except ImportError:
    win32gui = None
    win32con = None

from main.core.config import _load_raw_config, _save_raw_config

_this_file = Path(__file__).resolve()
_project_root = _this_file.parent.parent.parent
_SPIN_CHARS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class _AIEmbedContainer(QWidget):
    """Native window embedding container with automatic geometry synchronization."""

    def __init__(self, panel: "AIPanel", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._panel = panel
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setStyleSheet("background: #0c0d10;")
        self.setMinimumWidth(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self._panel, "_resize_embedded_ai"):
            self._panel._resize_embedded_ai()


class AIPanel(QWidget):
    """
    Side panel for OpenCode AI Assistant.
    Hosts the native pywebview + xterm + ConPTY window inside Qt via Win32 embedding.
    """

    def __init__(self, backend=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._backend = backend
        self._proc: Optional[subprocess.Popen] = None
        self._is_active = False
        self._is_embedded = False
        self._is_ready = False
        self._ai_hwnd: Optional[int] = None
        self._original_ai_style: Optional[int] = None
        self._original_ai_ex_style: Optional[int] = None

        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(90)
        self._spin_timer.timeout.connect(self._tick_spinner)
        self._spin_idx = 0

        self._embed_poll_timer = QTimer(self)
        self._embed_poll_timer.setInterval(60)
        self._embed_poll_timer.timeout.connect(self._poll_for_ai_window)
        self._poll_attempts = 0

        self._ready_poll_timer = QTimer(self)
        self._ready_poll_timer.setInterval(100)
        self._ready_poll_timer.timeout.connect(self._poll_for_ai_readiness)
        self._ready_poll_attempts = 0

        self._build_ui()

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Header Bar ────────────────────────────────────────────────────────
        header = QFrame()
        header.setFixedHeight(38)
        header.setStyleSheet("QFrame { background: #1c2333; border-bottom: 1px solid #2d3748; }")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 4, 12, 4)
        hl.setSpacing(8)

        lbl = QLabel("🤖 OPENCODE AI ASSISTANT")
        self._title_lbl = lbl
        lbl.setStyleSheet("color: #00e5ff; font-weight: 700; font-size: 11px; letter-spacing: 0.5px;")
        hl.addWidget(lbl)

        self._status_badge = QLabel("● Initializing…")
        self._status_badge.setStyleSheet("color: #f1c40f; font-size: 11px; font-weight: 600;")
        hl.addWidget(self._status_badge)

        hl.addStretch()

        self._btn_popout = QPushButton("↗")
        self._btn_popout.setToolTip("Open in external window")
        self._btn_popout.setFixedSize(24, 24)
        self._btn_popout.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_popout.setStyleSheet("""
            QPushButton {
                background: #2d3748; color: #cdd6f4; font-size: 12px; font-weight: 700;
                border-radius: 4px; border: none;
            }
            QPushButton:hover { background: #3a4a60; color: #ffffff; }
        """)
        self._btn_popout.clicked.connect(self._popout_ai)
        hl.addWidget(self._btn_popout)

        self._btn_close = QPushButton("✕")
        self._btn_close.setToolTip("Hide AI Assistant")
        self._btn_close.setFixedSize(24, 24)
        self._btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_close.setStyleSheet("""
            QPushButton {
                background: transparent; color: #94a3b8; font-size: 13px; font-weight: 700;
                border-radius: 4px; border: none;
            }
            QPushButton:hover { background: #2d3748; color: #ffffff; }
        """)
        self._btn_close.clicked.connect(self._on_close_clicked)
        hl.addWidget(self._btn_close)

        main_layout.addWidget(header)

        # ── Stacked View (Loading View & Embedded Terminal Container) ────────
        self._stack = QStackedWidget(self)

        # 1. Loading View (Active while launching, attaching, or realigning)
        self._loader_card = QFrame()
        self._loader_card.setStyleSheet("QFrame { background: #0c0d10; border: none; }")
        lv = QVBoxLayout(self._loader_card)
        lv.setContentsMargins(20, 40, 20, 40)
        lv.setSpacing(14)
        lv.addStretch()

        self._spin_lbl = QLabel("⠋")
        self._spin_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._spin_lbl.setStyleSheet("font-size: 32px; color: #00e5ff;")
        lv.addWidget(self._spin_lbl)

        self._load_title = QLabel("Initializing OpenCode AI Assistant…")
        self._load_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._load_title.setStyleSheet("color: #00e5ff; font-size: 14px; font-weight: 700; font-family: 'Montserrat', 'Segoe UI', sans-serif;")
        lv.addWidget(self._load_title)

        self._load_sub = QLabel("Preparing ConPTY terminal & project environment…")
        self._load_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._load_sub.setStyleSheet("color: #94a3b8; font-size: 11px; font-family: 'Montserrat', 'Segoe UI', sans-serif;")
        self._load_sub.setWordWrap(True)
        lv.addWidget(self._load_sub)
        lv.addStretch()

        self._stack.addWidget(self._loader_card)

        # 2. Native Embedding Container View
        self._embed_container = _AIEmbedContainer(self)
        self._stack.addWidget(self._embed_container)

        self._stack.setCurrentWidget(self._loader_card)
        main_layout.addWidget(self._stack, stretch=1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())
        self._resize_embedded_ai()

    def set_responsive_width(self, width: int) -> None:
        if hasattr(self, "_title_lbl") and self._title_lbl:
            if width >= 340:
                self._title_lbl.setText("🤖 OPENCODE AI ASSISTANT")
            elif width >= 270:
                self._title_lbl.setText("🤖 AI Assistant")
            else:
                self._title_lbl.setText("🤖 AI")

    def _tick_spinner(self) -> None:
        self._spin_idx = (self._spin_idx + 1) % len(_SPIN_CHARS)
        self._spin_lbl.setText(_SPIN_CHARS[self._spin_idx])

    def _on_close_clicked(self) -> None:
        """Hide the AI side panel without stopping the background process."""
        mw = self.window()
        if mw and hasattr(mw, "toggle_ai_panel"):
            mw.toggle_ai_panel(False)

    # ── Helpers ──────────────────────────────────────────────────────────────
    def _get_sketch_dir(self) -> str:
        if self._backend and hasattr(self._backend, "sketch_dir_path") and self._backend.sketch_dir_path:
            p = Path(self._backend.sketch_dir_path)
            if p.exists():
                return str(p)
        return str(_project_root)

    def _confirm_ai_assistant_launch(self) -> bool:
        """Prompt user with beta & copyright disclaimer before first launch."""
        cfg = _load_raw_config()
        if cfg.get("shared", {}).get("opencode_disclaimer_accepted", False):
            return True

        disclaimer_title = "OpenCode AI Assistant (Beta Test)"
        disclaimer_msg = (
            "🤖 OpenCode AI Assistant\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📌 Notice & Disclaimer:\n"
            "• OpenCode AI integration in this project is currently in BETA TESTING.\n"
            "• MCU Flash GUI does not claim any copyright, trademark, or ownership of OpenCode AI. "
            "All rights, trademarks, and intellectual property belong to their respective creators.\n\n"
            "🛡️ System Permission:\n"
            "• Clicking 'Yes' will launch OpenCode in an elevated terminal to assist you with "
            "fixing, explaining, and debugging code.\n\n"
            "Do you want to proceed and launch OpenCode AI as Administrator?"
        )
        ret = QMessageBox.question(
            self, disclaimer_title, disclaimer_msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if ret == QMessageBox.StandardButton.Yes:
            if "shared" not in cfg or not isinstance(cfg["shared"], dict):
                cfg["shared"] = {}
            cfg["shared"]["opencode_disclaimer_accepted"] = True
            _save_raw_config(cfg)
            return True
        return False

    def _check_internet(self) -> bool:
        """Verify active network socket connectivity."""
        try:
            from src.modules.dedicated_AI import check_internet_connection
            return check_internet_connection()
        except Exception:
            import socket
            for host, port in [("1.1.1.1", 53), ("8.8.8.8", 53), ("google.com", 80)]:
                try:
                    s = socket.create_connection((host, port), timeout=2.0)
                    s.close()
                    return True
                except Exception:
                    continue
            return False

    def _sync_project_hardware_state(self, custom_dir: Optional[str] = None) -> None:
        """Write current target board and port metadata to .mcu_flasher_project_hardware.json."""
        try:
            target_path = custom_dir if custom_dir else self._get_sketch_dir()
            sketch_dir = Path(target_path)
            if not sketch_dir.is_dir():
                return
            board = getattr(self._backend, "current_board", "")
            port = getattr(self._backend, "current_port", "")
            baud = getattr(self._backend, "current_baud", 115200)
            binfo = {}
            if self._backend and hasattr(self._backend, "_resolve_board_info"):
                binfo = self._backend._resolve_board_info(board)
            state = {
                "project_dir": str(sketch_dir),
                "board_name": board,
                "platform": binfo.get("platform", "unknown"),
                "board_id": binfo.get("board", "unknown"),
                "com_port": port,
                "baud_rate": baud,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if (sketch_dir / "index_json").is_dir():
                hw_file = sketch_dir / "index_json" / ".mcu_flasher_project_hardware.json"
            elif (_project_root / "index_json").is_dir() and sketch_dir == _project_root:
                hw_file = _project_root / "index_json" / ".mcu_flasher_project_hardware.json"
            else:
                hw_file = sketch_dir / ".mcu_flasher_project_hardware.json"
            hw_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ── Startup & Embedding ──────────────────────────────────────────────────
    def ensure_started(self) -> None:
        """Start the AI process automatically when the panel is shown."""
        if not self._is_active:
            self._start_ai()

    def _start_ai(self) -> None:
        if not self._check_internet():
            QMessageBox.warning(
                self,
                "No Internet Connection",
                "OpenCode AI Assistant requires an active internet connection to communicate with AI services.\n\n"
                "Please check your network connection and try again.",
            )
            self._on_close_clicked()
            return

        if not self._confirm_ai_assistant_launch():
            self._on_close_clicked()
            return

        script_path = _project_root / "src" / "modules" / "dedicated_AI.py"
        if not script_path.exists():
            QMessageBox.critical(self, "AI Assistant Error", f"Missing AI script at {script_path}")
            self._on_close_clicked()
            return

        from src.modules.private_python_guard import get_private_python_exe
        py_exe = get_private_python_exe(prefer_pythonw=True)
        target_dir = str(Path(self._get_sketch_dir()).resolve())

        self._sync_project_hardware_state()

        # Remove any stale ready signal from previous session
        try:
            (Path(target_dir) / ".ai_ready_signal").unlink(missing_ok=True)
        except Exception:
            pass

        # Update UI to loading state
        self._is_ready = False
        self._ready_poll_timer.stop()
        self._ready_poll_attempts = 0
        self._stack.setCurrentWidget(self._loader_card)
        self._status_badge.setText("● Initializing…")
        self._status_badge.setStyleSheet("color: #f1c40f; font-size: 11px; font-weight: 600;")
        self._load_title.setText("Initializing OpenCode AI Assistant…")
        self._load_sub.setText("Preparing ConPTY terminal & project environment…")
        self._spin_timer.start()

        cmd = [str(py_exe), str(script_path), "--launch-ai", target_dir]

        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(_project_root),
                creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
            self._is_active = True
            self._poll_attempts = 0
            self._embed_poll_timer.start()
        except Exception as e:
            self._spin_timer.stop()
            self._status_badge.setText("● Offline")
            self._status_badge.setStyleSheet("color: #e74c3c; font-size: 11px; font-weight: 600;")
            self._load_title.setText("AI Assistant Error")
            self._load_sub.setText(f"Failed to start AI Assistant: {e}")
            QMessageBox.critical(self, "AI Assistant Error", f"Failed to start AI Assistant:\n\n{e}")

    def _poll_for_ai_window(self) -> None:
        """Poll for the hidden pywebview OS window and embed into self._embed_container."""
        self._poll_attempts += 1
        if win32gui is None or win32con is None:
            self._embed_poll_timer.stop()
            self._spin_timer.stop()
            return

        hwnd = ctypes.windll.user32.FindWindowW(None, "MCU Flash GUI - OpenCode AI Assistant")
        if not hwnd or hwnd == 0:
            if self._poll_attempts > 240:  # ~15 seconds timeout
                self._embed_poll_timer.stop()
                self._spin_timer.stop()
                self._status_badge.setText("● Timeout")
                self._status_badge.setStyleSheet("color: #e74c3c; font-size: 11px; font-weight: 600;")
                self._load_title.setText("Connection Timed Out")
                self._load_sub.setText("Could not attach to the OpenCode AI terminal window.")
            return

        # Check if window is responsive
        user32 = ctypes.windll.user32
        if user32.IsHungAppWindow(hwnd):
            return

        self._embed_poll_timer.stop()
        # Embed the window into the container in the background, but keep it hidden
        self._embed_ai_hwnd(hwnd)

        # Keep loading spinner active until OpenCode TUI actually settles
        self._load_title.setText("Starting OpenCode AI Assistant…")
        self._load_sub.setText("Loading AI environment, models, and workspace…")
        self._ready_poll_attempts = 0
        self._ready_poll_timer.start()

    def _embed_ai_hwnd(self, hwnd: int) -> None:
        """Reparent the native pywebview window into the Qt container."""
        if win32gui is None or win32con is None:
            return

        try:
            container_hwnd = int(self._embed_container.winId())

            # Store original styles
            if self._original_ai_style is None:
                self._original_ai_style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                self._original_ai_ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)

            # Strip caption/borders to embed as child control
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
            self._ai_hwnd = hwnd
            self._is_embedded = True

            if not self._is_ready:
                # Keep window hidden until OpenCode TUI is actually rendered and ready
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                self._resize_embedded_ai(show=False)
                self._stack.setCurrentWidget(self._loader_card)
            else:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                self._resize_embedded_ai(show=True)
                self._stack.setCurrentWidget(self._embed_container)
                self._status_badge.setText("● Active")
                self._status_badge.setStyleSheet("color: #4ec994; font-size: 11px; font-weight: 600;")
                self._btn_popout.setText("↗")
                self._btn_popout.setToolTip("Open in external window")
        except Exception as e:
            print(f"[MCU Flasher] Error embedding AI window: {e}")

    def _poll_for_ai_readiness(self) -> None:
        """Poll until OpenCode TUI has settled before revealing the terminal view."""
        self._ready_poll_attempts += 1

        # If the background process died, stop and show error
        if self._proc and self._proc.poll() is not None:
            self._ready_poll_timer.stop()
            self._spin_timer.stop()
            self._status_badge.setText("● Stopped")
            self._status_badge.setStyleSheet("color: #e74c3c; font-size: 11px; font-weight: 600;")
            self._load_title.setText("AI Assistant Exited")
            self._load_sub.setText("The OpenCode process terminated unexpectedly.")
            return

        target_dir = Path(self._get_sketch_dir()).resolve()
        ready_sig = target_dir / ".ai_ready_signal"

        # Ready if .ai_ready_signal exists, or after safety fallback timeout (~12s = 120 attempts @ 100ms)
        is_ready = ready_sig.exists() or (self._ready_poll_attempts >= 120)

        if is_ready:
            self._ready_poll_timer.stop()
            self._spin_timer.stop()
            self._is_ready = True

            if self._ai_hwnd and win32gui and win32con and win32gui.IsWindow(int(self._ai_hwnd)):
                win32gui.ShowWindow(int(self._ai_hwnd), win32con.SW_SHOW)
                self._resize_embedded_ai(show=True)

            self._stack.setCurrentWidget(self._embed_container)
            self._status_badge.setText("● Active")
            self._status_badge.setStyleSheet("color: #4ec994; font-size: 11px; font-weight: 600;")
            self._btn_popout.setText("↗")
            self._btn_popout.setToolTip("Open in external window")

            # Deferred resize passes for smooth layout sync
            QTimer.singleShot(60, lambda: self._resize_embedded_ai(show=True))
            QTimer.singleShot(200, lambda: self._resize_embedded_ai(show=True))

    def _resize_embedded_ai(self, show: Optional[bool] = None) -> None:
        """Resize the embedded Win32 window to match the Qt container."""
        if not self._ai_hwnd or not self._is_embedded or win32gui is None or win32con is None:
            return
        try:
            if not win32gui.IsWindow(int(self._ai_hwnd)):
                self._ai_hwnd = None
                return
            w = self._embed_container.width()
            h = self._embed_container.height()
            if w <= 100 or h <= 80:
                w = max(self._stack.width(), self.width(), 100)
                h = max(self._stack.height(), self.height() - 34, 80)

            should_show = self._is_ready if show is None else show
            flags = win32con.SWP_FRAMECHANGED | win32con.SWP_NOZORDER
            if should_show:
                flags |= win32con.SWP_SHOWWINDOW
            else:
                flags |= win32con.SWP_NOACTIVATE

            hwnd = int(self._ai_hwnd)
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
        except Exception:
            pass

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_embedded_ai()

    # ── Pop-out (Detach & Reattach) ──────────────────────────────────────────
    def _popout_ai(self) -> None:
        if not self._ai_hwnd or win32gui is None or not win32gui.IsWindow(int(self._ai_hwnd)):
            if not self._is_active:
                self._start_ai()
            return

        if self._is_embedded:
            # Detach to desktop
            self._is_embedded = False
            win32gui.SetParent(int(self._ai_hwnd), 0)
            orig_style = self._original_ai_style or (
                win32con.WS_POPUP | win32con.WS_CAPTION | win32con.WS_THICKFRAME |
                win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX | win32con.WS_SYSMENU
            )
            orig_ex = self._original_ai_ex_style or 0
            win32gui.SetWindowLong(int(self._ai_hwnd), win32con.GWL_STYLE, orig_style)
            win32gui.SetWindowLong(int(self._ai_hwnd), win32con.GWL_EXSTYLE, orig_ex)
            win32gui.SetWindowPos(
                int(self._ai_hwnd), 0, 100, 100, 1040, 680,
                win32con.SWP_FRAMECHANGED | win32con.SWP_SHOWWINDOW
            )
            self._load_title.setText("OpenCode AI Assistant (Detached)")
            self._load_sub.setText("The AI terminal is running in a separate window.")
            self._stack.setCurrentWidget(self._loader_card)
            self._btn_popout.setText("↙")
            self._btn_popout.setToolTip("Reattach AI Assistant to side panel")
            self._status_badge.setText("● Detached")
            self._status_badge.setStyleSheet("color: #5ca4f0; font-size: 11px; font-weight: 600;")
        else:
            # Reattach into panel
            self._embed_ai_hwnd(self._ai_hwnd)

    # ── Project Realignment ──────────────────────────────────────────────────
    def reset_for_project(self, new_project_dir: str) -> None:
        """
        Restart OpenCode AI Assistant so the new sketch directory becomes its session root.
        Only called when the user actually switches projects.
        """
        if not self._is_active:
            return

        self._embed_poll_timer.stop()
        self._ready_poll_timer.stop()
        self._is_ready = False
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

        try:
            from src.modules.dedicated_AI import close_active_opencode
            close_active_opencode()
        except Exception:
            pass

        if self._ai_hwnd and win32gui and win32con and win32gui.IsWindow(int(self._ai_hwnd)):
            try:
                win32gui.ShowWindow(int(self._ai_hwnd), win32con.SW_HIDE)
            except Exception:
                pass
        self._ai_hwnd = None
        self._is_embedded = False

        # Show loader view while realigning
        self._stack.setCurrentWidget(self._loader_card)
        self._status_badge.setText("● Realigning…")
        self._status_badge.setStyleSheet("color: #f1c40f; font-size: 11px; font-weight: 600;")
        self._load_title.setText("Realigning AI to New Project…")
        proj_name = Path(new_project_dir).name if new_project_dir else "Project"
        self._load_sub.setText(f"Switching AI workspace to {proj_name}…")
        self._spin_timer.start()

        # Sync hardware state for new project folder
        self._sync_project_hardware_state(custom_dir=new_project_dir)

        # Clean ready signal in new directory
        target_dir = str(Path(new_project_dir if new_project_dir else self._get_sketch_dir()).resolve())
        try:
            (Path(target_dir) / ".ai_ready_signal").unlink(missing_ok=True)
        except Exception:
            pass

        # Launch fresh standalone AI process rooted in new directory
        script_path = _project_root / "src" / "modules" / "dedicated_AI.py"
        if not script_path.exists():
            return

        from src.modules.private_python_guard import get_private_python_exe
        py_exe = get_private_python_exe(prefer_pythonw=True)

        cmd = [str(py_exe), str(script_path), "--launch-ai", target_dir]
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(_project_root),
                creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
            self._poll_attempts = 0
            self._embed_poll_timer.start()
        except Exception as e:
            print(f"[MCU Flasher] Error restarting AI for new project: {e}")

    # ── Stop & Cleanup (Only on application exit) ────────────────────────────
    def _stop_ai(self) -> None:
        """Terminate the AI subprocess on application exit."""
        self._embed_poll_timer.stop()
        self._ready_poll_timer.stop()
        self._spin_timer.stop()
        self._is_ready = False
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

        try:
            from src.modules.dedicated_AI import close_active_opencode
            close_active_opencode()
        except Exception:
            pass

        self._ai_hwnd = None
        self._is_embedded = False
        self._is_active = False

    def closeEvent(self, event) -> None:
        self._stop_ai()
        super().closeEvent(event)
