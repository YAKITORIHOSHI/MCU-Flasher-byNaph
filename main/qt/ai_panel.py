#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.ai_panel — AI Assistant Panel for MCU Flasher by Naph.

  • Opening it offers the list of assistants; only a choice starts one, behind a
    loading screen until its interface has painted
  • Persistent background process: once started, toggling only hides or shows the panel
  • Automatic project realignment: session only resets when the active sketch project changes
  • Real interactive xterm.js + ConPTY terminal hosting via Win32 HWND embedding (SetParent)
  • Always attached inside the side panel of the main application window
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

from main.core.ai_agents import DEFAULT_AGENT, agent as agent_spec, last_agent, remember_agent
from main.core.log_colors import glass_inks
from main.core.theme import Theme, get_palette
from main.qt.agent_chooser import AgentChooser
from main.qt.agent_picker import AgentPicker
from main.qt.assistant_loader import (AssistantLoader, restart_controls, set_restart_visible, style_restart_controls,
                                      switch_control)
from main.qt.theme import glass_tokens

_this_file = Path(__file__).resolve()
_project_root = _this_file.parent.parent.parent
# After this long Qt shows the embedded page, whose own cover stays up until OpenCode paints.
_READY_POLLS_BEFORE_PAGE = 120
_READY_POLLS_AFTER_PAGE = 300


from main.qt.icons import IconLabel as QLabel

from main.qt.icons import ActionButton as QPushButton

class _AIEmbedContainer(QWidget):
    """Native window embedding container with automatic geometry synchronization."""

    def __init__(self, panel: "AIPanel", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._panel = panel
        self.setProperty("mcuForeignInput", True)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setMinimumWidth(80)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        from main.qt.responsive import EmbeddedGeometryWatcher
        self._geometry_watcher = EmbeddedGeometryWatcher(self, panel._resize_embedded_ai)


class AIPanel(QWidget):
    """
    Side panel for the AI Assistant.
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
        self._agent = agent_spec(DEFAULT_AGENT)
        self._badge = ("info", "Ready", "TEXT_DIM")

        self._embed_poll_timer = QTimer(self)
        self._embed_poll_timer.setInterval(60)
        self._embed_poll_timer.timeout.connect(self._poll_for_ai_window)
        self._poll_attempts = 0

        self._ready_poll_timer = QTimer(self)
        self._ready_poll_timer.setInterval(100)
        self._ready_poll_timer.timeout.connect(self._poll_for_ai_readiness)
        self._ready_poll_attempts = 0

        self._build_ui()
        self.apply_theme(Theme.active_theme)

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Header Bar ────────────────────────────────────────────────────────
        header = QFrame(self)
        header.setObjectName("opencode-header")
        header.setFixedHeight(38)
        self._header = header
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 4, 12, 4)
        hl.setSpacing(8)

        lbl = QLabel("AI ASSISTANT", icon_name="ai")
        self._title_lbl = lbl
        hl.addWidget(lbl)

        self._status_badge = QLabel("Ready", icon_name="info")
        hl.addWidget(self._status_badge)
        # Restart sits beside the status, set apart by a divider.
        self._restart_divider, self._btn_restart = restart_controls(header, self._restart_session)
        hl.addSpacing(4)
        hl.addWidget(self._restart_divider, 0, Qt.AlignmentFlag.AlignVCenter)
        hl.addSpacing(4)
        hl.addWidget(self._btn_restart, 0, Qt.AlignmentFlag.AlignVCenter)
        self._btn_switch = switch_control(header, self._switch_agent)
        hl.addWidget(self._btn_switch, 0, Qt.AlignmentFlag.AlignVCenter)

        hl.addStretch()

        main_layout.addWidget(header)

        # ── Stacked View (Loading View & Embedded Terminal Container) ────────
        self._stack = QStackedWidget(self)

        # 1. Loading View (Active while launching, attaching, or realigning)
        # The page owns "Show startup output": before it appears there is nothing to show.
        self._loader_card = AssistantLoader(can_reveal=False)
        self._load_title, self._load_sub = self._loader_card.title, self._loader_card.status
        self._loader_card.show_idle("Open the assistant to start it in this project.")
        self._loader_card.retry_requested.connect(self._retry)
        self._stack.addWidget(self._loader_card)

        # 2. Native Embedding Container View
        self._embed_container = _AIEmbedContainer(self)
        self._stack.addWidget(self._embed_container)

        # 3. The list of assistants, shown whenever none is running
        self._chooser = AgentChooser(self)
        self._picker = AgentPicker(self._chooser, self)
        self._picker.chosen.connect(self.start_agent)
        self._stack.addWidget(self._chooser)

        self._stack.setCurrentWidget(self._loader_card)
        main_layout.addWidget(self._stack, stretch=1)

    def apply_theme(self, mode: str) -> None:
        self._theme = mode
        palette = get_palette(mode)
        self._header.setStyleSheet("QFrame#opencode-header { background: transparent; border: none; }")
        self._title_lbl.setStyleSheet(f"color: {glass_inks(mode)['system']}; font-size: 11px; font-weight: 700;")
        self._embed_container.setStyleSheet(f"background: {palette['BG_DARKEST']};")
        self._loader_card.apply_theme(mode)
        self._chooser.apply_theme(mode)
        style_restart_controls(self._restart_divider, self._btn_restart, mode, self._btn_switch)
        self._set_badge(*self._badge)

    def _set_badge(self, icon_name: str, text: str, ink: str) -> None:
        """Show the header status in a palette ink that follows theme changes."""
        self._badge = (icon_name, text, ink)
        self._status_badge.set_icon(icon_name)
        self._status_badge.setText(text)
        semantic = {"GREEN": "success", "RED": "error", "YELLOW": "warning", "CYAN": "system"}.get(ink, "normal")
        self._status_badge.setStyleSheet(
            f"color: {glass_inks(self._theme)[semantic]}; font-size: 11px; font-weight: 600;")
        self._update_restart()

    def _update_restart(self) -> None:
        if hasattr(self, "_btn_restart"):
            set_restart_visible(self._restart_divider, self._btn_restart, bool(self._is_active), self.width(),
                                self._btn_switch)

    def _restart_session(self) -> None:
        """Start a fresh session in the current project; nothing typed earlier is replayed."""
        if not self._is_active or self.isHidden():
            return
        self.reset_for_project(self._get_sketch_dir())
        self._set_badge("reload", "Restarting…", "TEXT_DIM")
        self._loader_card.show_progress(f"Restarting {self._agent.name}…", 0, title=f"Opening {self._agent.name}")

    def _switch_agent(self) -> None:
        """End this session and offer the list again."""
        self._stop_ai()
        self.ensure_started()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())
        self._resize_embedded_ai()

    def set_responsive_width(self, width: int) -> None:
        self._status_badge.setVisible(width >= 180)
        self._update_restart()
        if hasattr(self, "_title_lbl") and self._title_lbl:
            name = self._agent.name.upper() if self._is_active else ""
            self._title_lbl.set_icon("ai")
            if width >= 400:
                self._title_lbl.setText(f"{name} AI ASSISTANT".strip())
            elif width >= 290:
                self._title_lbl.setText(f"{self._agent.name} AI" if name else "AI Assistant")
            else:
                self._title_lbl.setText("AI")

    def _retry(self) -> None:
        """Start a fresh session after a failure; nothing typed earlier is replayed."""
        self._stop_ai()
        self._start_ai()

    def _on_close_clicked(self) -> None:
        """Handle the panel button to hide the AI Assistant side panel."""
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

    def _check_internet(self) -> bool:
        """Verify active network socket connectivity."""
        try:
            from src.modules.dedicated_AI import check_internet_connection
            return check_internet_connection()
        except Exception:
            try:
                from src.modules.downloader import check_internet_connection as dl_check
                return dl_check()
            except Exception:
                pass
            import socket
            for host, port in [("1.1.1.1", 443), ("8.8.8.8", 443), ("1.0.0.1", 443)]:
                try:
                    s = socket.create_connection((host, port), timeout=1.5)
                    s.close()
                    return True
                except Exception:
                    continue
            return False

    def _sync_project_hardware_state(self, custom_dir: Optional[str] = None) -> None:
        """Sync active target hardware state to .mcu_flasher_build_cache/project_state.json and purge root files."""
        try:
            target_path = custom_dir if custom_dir else self._get_sketch_dir()
            sketch_dir = Path(target_path)
            if not sketch_dir.is_dir():
                return
            # Remove any misplaced hardware state files from root or index_json
            try:
                (sketch_dir / ".mcu_flasher_project_hardware.json").unlink(missing_ok=True)
                (sketch_dir / "index_json" / ".mcu_flasher_project_hardware.json").unlink(missing_ok=True)
                (_project_root / "index_json" / ".mcu_flasher_project_hardware.json").unlink(missing_ok=True)
            except Exception:
                pass
            if self._backend and hasattr(self._backend, "_sync_project_hardware_state"):
                self._backend._sync_project_hardware_state(target_path)
        except Exception:
            pass

    # ── Startup & Embedding ──────────────────────────────────────────────────
    def ensure_started(self) -> None:
        """Opening the panel offers the list of assistants; only a choice starts one."""
        if self._is_active:
            return
        if self._stack.currentWidget() is not self._chooser:
            self._chooser.select(last_agent())
            self._stack.setCurrentWidget(self._chooser)
            self._set_badge("info", "Ready", "TEXT_DIM")
        self.set_responsive_width(self.width())
        if not self._picker.downloading:
            self._picker.refresh()

    def start_agent(self, agent_id: str) -> None:
        """Start the chosen assistant in the current project."""
        if self._is_active:
            return
        self._agent = agent_spec(agent_id)
        self._loader_card.set_agent(self._agent.name)
        remember_agent(self._agent.id)
        self._start_ai()

    def _launch_command(self, target_dir: str) -> list[str]:
        from src.modules.private_python_guard import get_private_python_exe
        return [str(get_private_python_exe(prefer_pythonw=True)), str(_project_root / "src/modules/dedicated_AI.py"),
                "--launch-ai", target_dir, "--agent", self._agent.id]

    def _start_ai(self) -> None:
        if not self._check_internet():
            reply = QMessageBox.question(
                self,
                "Internet Connection Warning",
                "Could not verify an active internet connection.\n\n"
                f"{self._agent.name} typically requires internet access to communicate with cloud AI services.\n\n"
                "Would you like to try launching anyway (for local models, custom proxies, or offline use)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self.ensure_started()       # Back to the list; another choice may work offline.
                return

        script_path = _project_root / "src" / "modules" / "dedicated_AI.py"
        if not script_path.exists():
            QMessageBox.critical(self, "AI Assistant Error", f"Missing AI script at {script_path}")
            self._on_close_clicked()
            return

        target_dir = str(Path(self._get_sketch_dir()).resolve())

        self._sync_project_hardware_state()

        # Remove any stale ready signal from previous session
        try:
            (Path(target_dir) / ".ai_ready_signal").unlink(missing_ok=True)
            (Path(target_dir) / ".mcu_flasher_build_cache" / ".ai_ready_signal").unlink(missing_ok=True)
        except Exception:
            pass

        # Update UI to loading state
        self._is_ready = False
        self._ready_poll_timer.stop()
        self._ready_poll_attempts = 0
        self._ready_poll_timer.setInterval(100)
        self._stack.setCurrentWidget(self._loader_card)
        self._set_badge("time", "Starting…", "TEXT_DIM")
        self._loader_card.show_progress("Preparing the terminal…", 0)

        try:
            cmd = self._launch_command(target_dir)
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(_project_root),
                creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
            self._is_active = True
            self.set_responsive_width(self.width())
            self._update_restart()
            self._poll_attempts = 0
            self._loader_card.show_progress("Starting a session…", 1)
            self._embed_poll_timer.start()
        except Exception as e:
            self._set_badge("disconnected", "Offline", "RED")
            self._loader_card.show_error(f"{self._agent.name} could not start", f"The assistant process failed to start: {e}")
            QMessageBox.critical(self, "AI Assistant Error", f"Failed to start AI Assistant:\n\n{e}")

    def _poll_for_ai_window(self) -> None:
        """Poll for the hidden pywebview OS window and embed into self._embed_container."""
        self._poll_attempts += 1
        if win32gui is None or win32con is None:
            self._embed_poll_timer.stop()
            return

        if not self._proc or self._proc.poll() is not None:
            return
        hwnd = ctypes.windll.user32.FindWindowW(None, f"MCU Flash GUI - OpenCode AI Assistant [{self._proc.pid}]")
        if not hwnd or hwnd == 0:
            if self._poll_attempts > 240:  # ~15 seconds timeout
                self._embed_poll_timer.stop()
                self._set_badge("warning", "Timeout", "RED")
                self._loader_card.show_error(f"{self._agent.name} did not open",
                                             "The assistant's window did not appear in time. Retry to start a fresh session.")
            return

        # Check if window is responsive
        user32 = ctypes.windll.user32
        if user32.IsHungAppWindow(hwnd):
            return

        self._embed_poll_timer.stop()
        # Embed the window into the container in the background, but keep it hidden
        self._embed_ai_hwnd(hwnd)

        # Keep the loading screen until the assistant's interface has actually painted.
        self._loader_card.show_progress("Loading your workspace…", 2)
        self._ready_poll_attempts = 0
        self._ready_poll_timer.setInterval(100)
        self._ready_poll_timer.start()

    def _embed_ai_hwnd(self, hwnd: int) -> None:
        """Reparent the native pywebview window into the Qt container."""
        if win32gui is None or win32con is None:
            return

        try:
            container_hwnd = int(self._embed_container.winId())

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
                # Keep the window hidden until the assistant is actually rendered and ready
                win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
                self._resize_embedded_ai(show=False)
                self._stack.setCurrentWidget(self._loader_card)
            else:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWNA)
                self._resize_embedded_ai(show=True)
                self._stack.setCurrentWidget(self._embed_container)
                self._loader_card.finish()
                self._set_badge("connected", "Active", "GREEN")
        except Exception as e:
            print(f"[MCU Flasher] Error embedding AI window: {e}")

    def _poll_for_ai_readiness(self) -> None:
        """Reveal the embedded view once the assistant's interface has painted."""
        self._ready_poll_attempts += 1

        # If the background process died, stop and show error
        if self._proc and self._proc.poll() is not None:
            self._ready_poll_timer.stop()
            self._is_ready = False
            self._stack.setCurrentWidget(self._loader_card)
            self._set_badge("stop", "Stopped", "RED")
            self._loader_card.show_error(f"{self._agent.name} stopped",
                                         "The assistant process ended unexpectedly. Retry to start a fresh session.")
            return

        target_dir = Path(self._get_sketch_dir()).resolve()
        cache_sig = target_dir / ".mcu_flasher_build_cache" / ".ai_ready_signal"
        root_sig = target_dir / ".ai_ready_signal"

        if cache_sig.exists() or root_sig.exists():
            # Immediately unlink the ready signal so it never lingers or appears in Explorer
            try:
                cache_sig.unlink(missing_ok=True)
                root_sig.unlink(missing_ok=True)
            except Exception:
                pass
            self._ready_poll_timer.stop()
            self._set_badge("connected", "Active", "GREEN")
            self._reveal_embedded_view()
        elif not self._is_ready and self._ready_poll_attempts >= _READY_POLLS_BEFORE_PAGE:
            # A slow start: the page's own cover keeps its startup output out of
            # sight and offers it on request. Keep listening for the signal.
            self._reveal_embedded_view()
            self._ready_poll_attempts = 0
            self._ready_poll_timer.setInterval(1000)
        elif self._is_ready and self._ready_poll_attempts >= _READY_POLLS_AFTER_PAGE:
            self._ready_poll_timer.stop()

    def _reveal_embedded_view(self) -> None:
        self._is_ready = True
        self._loader_card.finish()
        if self._ai_hwnd and win32gui and win32con and win32gui.IsWindow(int(self._ai_hwnd)):
            win32gui.ShowWindow(int(self._ai_hwnd), win32con.SW_SHOWNA)
            self._resize_embedded_ai(show=True)
        self._stack.setCurrentWidget(self._embed_container)

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
            from main.qt.responsive import native_client_size
            w, h = native_client_size(self._embed_container, win32gui)

            should_show = self.isVisible() and self._is_ready and show is not False
            flags = win32con.SWP_FRAMECHANGED | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
            if should_show:
                flags |= win32con.SWP_SHOWWINDOW
            else:
                flags |= win32con.SWP_HIDEWINDOW

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



    # ── Project Realignment ──────────────────────────────────────────────────
    def reset_for_project(self, new_project_dir: str) -> None:
        """
        Restart the assistant so the new sketch directory becomes its session root.
        Only called when the user actually switches projects.
        """
        if not self._is_active:
            return
        if self.isHidden():
            self._stop_ai()
            return  # Only the next explicit reveal may start the new project.

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

        # Hide and discard any existing AI window
        if self._ai_hwnd and win32gui and win32con:
            try:
                if win32gui.IsWindow(int(self._ai_hwnd)):
                    win32gui.ShowWindow(int(self._ai_hwnd), win32con.SW_HIDE)
            except Exception:
                pass
        self._ai_hwnd = None
        self._is_embedded = False

        # Show loader while the new session spins up
        self._stack.setCurrentWidget(self._loader_card)
        self._set_badge("reload", "Switching…", "TEXT_DIM")
        proj_name = Path(new_project_dir).name if new_project_dir else "Project"
        self._loader_card.show_progress(f"Switching to {proj_name}…", 0, title=f"Opening {self._agent.name}")

        # Sync hardware state for new project folder
        self._sync_project_hardware_state(custom_dir=new_project_dir)

        # Clean ready signal in new directory
        target_dir = str(Path(new_project_dir if new_project_dir else self._get_sketch_dir()).resolve())
        try:
            (Path(target_dir) / ".ai_ready_signal").unlink(missing_ok=True)
            (Path(target_dir) / ".mcu_flasher_build_cache" / ".ai_ready_signal").unlink(missing_ok=True)
        except Exception:
            pass

        # Launch fresh standalone AI process rooted in new directory
        script_path = _project_root / "src" / "modules" / "dedicated_AI.py"
        if not script_path.exists():
            return

        try:
            cmd = self._launch_command(target_dir)
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

        target_dir = Path(self._get_sketch_dir()).resolve()
        try:
            (target_dir / ".ai_ready_signal").unlink(missing_ok=True)
            (target_dir / ".mcu_flasher_build_cache" / ".ai_ready_signal").unlink(missing_ok=True)
        except Exception:
            pass

        try:
            from src.modules.dedicated_AI import close_active_opencode
            close_active_opencode()
        except Exception:
            pass

        self._ai_hwnd = None
        self._is_embedded = False
        self._is_active = False
        self._update_restart()

    def closeEvent(self, event) -> None:
        self._picker.close()
        self._stop_ai()
        super().closeEvent(event)
