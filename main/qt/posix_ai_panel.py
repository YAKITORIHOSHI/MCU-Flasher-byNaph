"""Dedicated, workspace-owned OpenCode view for native Ubuntu desktops."""
from __future__ import annotations

import json
from pathlib import Path
import re
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, QSize, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QStackedLayout

from main.core.theme import get_palette
from main.qt.log_colors import themed_terminal_colors
from main.qt.icons import icon
from main.qt.posix_terminal_panel import PtySession
from main.platforms.ubuntu_opencode import find_opencode_cli, opencode_argv, OpenCodeRestartPolicy
from main.platforms.ubuntu_assistant import prepare_context
from src.modules.ai_prompt_context import PromptInputTracker
from src.modules.runtime_resources import performance_profile

ROOT = Path(__file__).resolve().parents[2]
ASSISTANT_ASSETS = (
    "src/editor/opencode.html", "src/editor/qwebchannel.js",
    "src/assets/xterm/xterm.js", "src/assets/xterm/xterm.css",
    "src/assets/xterm/xterm-addon-fit.js",
)
_ANSI_OUTPUT = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


class OpenCodeSession(QObject):
    """Stable browser bridge around fresh native PTYs; never replay input."""
    output = Signal(int, str)
    reset_view = Signal(int)
    status = Signal(str, str)
    rendered = Signal(int, int)

    def __init__(self, cwd, executable, parent=None):
        super().__init__(parent)
        self.cwd, self.executable = cwd, executable
        self._child = None
        self._generation = 0
        self._client_ready = False
        self._closed = False
        self._starting = False
        self._start_error = ""
        self._diagnostic_tail = ""
        self._dimensions = (24, 80)
        self._policy = OpenCodeRestartPolicy()
        self._intentional_exit = False
        self._exit_requested_at = 0.0
        self._line_pasted = False
        self._exit_observer = PromptInputTracker(cwd)
        self._restart_timer = QTimer(self)
        self._restart_timer.setSingleShot(True)
        self._restart_timer.setInterval(800)
        self._restart_timer.timeout.connect(self._spawn)

    @Slot(result=bool)
    def start(self):
        self._client_ready = True
        if self._closed or self._child or self._restart_timer.isActive():
            return False
        return self._spawn()

    def _spawn(self):
        if self._closed or not self._client_ready or self._child:
            return False
        self._generation += 1
        generation = self._generation
        self._intentional_exit = False
        self._exit_requested_at = 0.0
        self._line_pasted = False
        self._exit_observer = PromptInputTracker(self.cwd)
        self._diagnostic_tail = ""
        child = PtySession(self.cwd, opencode_argv(self.executable, self.cwd), self)
        child.assistant_active = True
        self._child = child
        child.resize(*self._dimensions)
        child.output.connect(lambda data: self._forward_output(child, generation, data))
        child.ended.connect(lambda message: self._ended(child, generation, message))
        self.reset_view.emit(generation)
        self.status.emit("loading", "Starting OpenCode…")
        self._starting, self._start_error = True, ""
        try:
            started = child.start()
        except Exception as exc:
            started, self._start_error = False, str(exc)
        finally:
            self._starting = False
        if not started:
            self._child = None
            child.close()
            child.deleteLater()
            self.status.emit("error", self._start_error or "OpenCode could not start. Check its installation and retry.")
        return started

    def _forward_output(self, child, generation, data):
        if not self._closed and child is self._child and generation == self._generation:
            text = _ANSI_OUTPUT.sub("", data)
            self._diagnostic_tail = (self._diagnostic_tail + "".join(c for c in text if c.isprintable() or c in "\r\n"))[-2000:]
            self.output.emit(generation, data)

    def _ended(self, child, generation, message):
        if self._closed or child is not self._child or generation != self._generation:
            return
        if self._starting:
            self._start_error = message
            return
        self._child = None
        child.close()
        child.deleteLater()
        intentional = self._intentional_exit and time.monotonic() - self._exit_requested_at <= 10.0
        self._intentional_exit = False
        if self._policy.allow_restart(intentional=intentional):
            self.status.emit("loading", "Starting a fresh OpenCode session…")
            self._restart_timer.start()
        else:
            text = "OpenCode stopped repeatedly. The assistant stays attached; retry after checking its configuration."
            if self._diagnostic_tail.strip():
                text += "\n\n" + self._diagnostic_tail.strip()
            self.status.emit("error", text)

    @Slot(int, str)
    def write(self, generation, data):
        if self._closed or generation != self._generation or not self._child:
            return
        # Observe only the explicit exit command. Capability replies, editing
        # keys, Unicode and bracketed paste still reach OpenCode unchanged.
        observer = self._exit_observer
        for char in data:
            committed = char in "\r\n" and not observer.escape and not observer.paste
            if committed:
                if (not self._line_pasted and not observer.uncertain
                        and observer.line.strip().casefold() == "/exit"):
                    self._intentional_exit = True
                    self._exit_requested_at = time.monotonic()
                self._line_pasted = False
            observer.feed(char, active=False)
            if observer.paste:
                self._line_pasted = True
            elif char in "\x03\x15" and not observer.escape:
                self._line_pasted = False
        self._child.write(data)

    @Slot(int, int)
    def resize(self, rows, columns):
        self._dimensions = (max(2, min(rows, 500)), max(10, min(columns, 1000)))
        if self._child:
            self._child.resize(*self._dimensions)

    @Slot(int, int)
    def consumed(self, generation, chars):
        if not self._closed and generation == self._generation and self._child:
            self._child.consumed(chars)
            self.rendered.emit(generation, chars)

    def retry(self):
        if not self._closed and not self._child and not self._restart_timer.isActive():
            self._policy.reset()
            return self._spawn()
        return False

    def close(self):
        self._closed = True
        self._restart_timer.stop()
        self._generation += 1
        if self._child:
            child, self._child = self._child, None
            child.close()
            child.deleteLater()


class PosixAIPanel(QWidget):
    """Single protected OpenCode interface, separate from project terminals."""
    _discovered = Signal(int, str, str)

    def __init__(self, backend=None, parent=None):
        super().__init__(parent)
        from main.core.config import get_monitor_font_size
        self._backend = backend
        self._theme = "default"
        self._font_size = get_monitor_font_size()
        self._requested = False
        self._closing = False
        self._project_dir = ""
        self._launch_generation = 0
        self._session = self._view = self._channel = None
        self._state = "idle"
        self._output_bytes = 0
        self._visible_output = self._rendered_chars = 0
        self._last_output = 0.0
        self._spawn_time = 0.0
        self._ready_timer = QTimer(self)
        self._ready_timer.setInterval(150)
        self._ready_timer.timeout.connect(self._check_ready)
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(60)
        self._fit_timer.timeout.connect(self._fit_view)
        self._discovered.connect(self._discovery_finished, Qt.ConnectionType.QueuedConnection)
        self._build_ui()
        # Window-owned theme propagation remains the only theme subscription.
        from main.qt.signals import signals
        signals.font_size_changed.connect(self.set_font_size)
        self.apply_theme(self._theme)

    @property
    def _is_active(self):
        return self._requested and not self._closing

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._header = QFrame(self)
        self._header.setObjectName("opencode-header")
        self._header.setFixedHeight(38)
        row = QHBoxLayout(self._header)
        row.setContentsMargins(10, 4, 10, 4)
        row.setSpacing(6)
        self._title = QLabel("OpenCode AI Assistant", self._header)
        row.addWidget(self._title, 1)
        self._status_badge = QLabel("Ready", self._header)
        row.addWidget(self._status_badge)
        self._hide_button = QPushButton(self._header)
        self._hide_button.setFixedSize(24, 24)
        self._hide_button.setStyleSheet("padding: 0px;")
        self._hide_button.setIconSize(QSize(12, 12))
        self._hide_button.setAccessibleName("Hide AI Assistant")
        self._hide_button.setToolTip("Hide AI Assistant; keep OpenCode running")
        self._hide_button.clicked.connect(self._hide_panel)
        row.addWidget(self._hide_button)
        layout.addWidget(self._header)
        self._body = QWidget(self)
        self._stack = QStackedLayout(self._body)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self._loader = QFrame(self._body)
        loader_layout = QVBoxLayout(self._loader)
        loader_layout.setContentsMargins(16, 16, 16, 16)
        loader_layout.addStretch()
        self._load_title = QLabel("OpenCode AI Assistant", self._loader)
        self._load_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._load_title.setWordWrap(True)
        loader_layout.addWidget(self._load_title)
        self._load_sub = QLabel("Open the assistant to start OpenCode in this project.", self._loader)
        self._load_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._load_sub.setWordWrap(True)
        loader_layout.addWidget(self._load_sub)
        self._retry_button = QPushButton("Retry OpenCode", self._loader)
        self._retry_button.clicked.connect(self._retry)
        self._retry_button.hide()
        loader_layout.addWidget(self._retry_button, alignment=Qt.AlignmentFlag.AlignCenter)
        loader_layout.addStretch()
        self._stack.addWidget(self._loader)
        layout.addWidget(self._body, 1)

    def _cwd(self):
        return str(self._project_dir or getattr(self._backend, "sketch_dir_path", "") or ROOT)

    def ensure_started(self):
        if self._requested or self._closing:
            return
        self._requested = True
        self._launch_generation += 1
        generation, project = self._launch_generation, self._cwd()
        self._project_dir = project  # Pin pending discovery before the backend can change projects.
        self._set_state("loading", "Locating OpenCode for this project…")

        def discover():
            try:
                cwd = str(Path(project).resolve())
                executable = find_opencode_cli() if Path(cwd).is_dir() else None
                message = "" if executable else "OpenCode is unavailable. Close the workspace and run Ubuntu Bootstrap repair: bash direct/ubuntu/run.sh --repair."
                if executable and any(not (ROOT / name).is_file() for name in ASSISTANT_ASSETS):
                    executable = None
                    message = "The OpenCode view assets are missing. Restore the application's local assistant assets, then retry."
                if executable and not prepare_context(self._backend, cwd,
                        cancelled=lambda: self._closing or generation != self._launch_generation):
                    return
            except Exception as exc:
                cwd, executable, message = project, None, f"OpenCode discovery failed: {exc}"
            try:
                self._discovered.emit(generation, executable or "", json.dumps([cwd, message]))
            except RuntimeError:
                pass  # Parent window was destroyed while discovery completed.

        threading.Thread(target=discover, name="ubuntu-opencode-discovery", daemon=True).start()

    @Slot(int, str, str)
    def _discovery_finished(self, generation, executable, details):
        if self._closing or generation != self._launch_generation:
            return
        cwd, message = json.loads(details)
        if not executable:
            self._set_state("error", message)
            return
        self._project_dir = cwd
        view = QWebEngineView(self._body)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        session = OpenCodeSession(cwd, executable, view)
        channel = QWebChannel(view.page())
        channel.registerObject("assistantBridge", session)
        view.page().setWebChannel(channel)
        self._view, self._session, self._channel = view, session, channel
        session.status.connect(lambda state, text: self._session_status(session, state, text))
        session.output.connect(lambda epoch, data: self._observed_output(session, data))
        session.rendered.connect(lambda epoch, chars: self._observed_render(session, chars))
        view.loadFinished.connect(lambda ok: self._loaded(view, ok))
        if hasattr(view, "renderProcessTerminated"):
            view.renderProcessTerminated.connect(lambda *_: self._renderer_terminated(view))
        self._stack.addWidget(view)
        self._stack.setCurrentWidget(self._loader)
        view.setUrl(QUrl.fromLocalFile(str(ROOT / "src/editor/opencode.html")))

    def _loaded(self, view, ok):
        if self._closing or view is not self._view:
            return
        if ok:
            self._configure_view()
        else:
            if self._session:
                self._session.close()
            self._set_state("error", "The OpenCode view could not load. Restore the application's local assistant assets and retry.")

    def _session_status(self, session, state, text):
        if session is not self._session or self._closing:
            return
        if state == "loading":
            self._output_bytes = 0
            self._visible_output = self._rendered_chars = 0
            self._spawn_time = self._last_output = time.monotonic()
            self._ready_timer.start()
        self._set_state(state, text)

    def _renderer_terminated(self, view):
        if view is not self._view or self._closing:
            return
        # A renderer cannot reconstruct a live interactive screen reliably.
        # Keep the protected container; require an explicit fresh-session retry.
        if self._session:
            self._session.close()
        self._set_state("error", "The OpenCode view stopped. Retry to start a fresh session; submitted commands will not be replayed.")

    def has_input_focus(self):
        from PySide6.QtWidgets import QApplication
        widget = QApplication.focusWidget()
        return bool(widget and self.isVisible() and (widget is self or self.isAncestorOf(widget)))

    def _observed_output(self, session, data):
        if session is self._session and not self._closing:
            self._output_bytes = min(65536, self._output_bytes + len(data.encode("utf-8")))
            text = _ANSI_OUTPUT.sub("", data)
            self._visible_output = min(65536, self._visible_output + sum(c.isprintable() and not c.isspace() for c in text))
            self._last_output = time.monotonic()

    def _observed_render(self, session, chars):
        if session is self._session and not self._closing:
            self._rendered_chars = min(65536, self._rendered_chars + max(0, chars))

    def _check_ready(self):
        now = time.monotonic()
        if self._state != "loading" or not self._session or not self._session._child:
            self._ready_timer.stop()
            return
        # Short startup questions must remain accessible too. Reveal only
        # settled visible output acknowledged by the renderer, never a timeout.
        elapsed = now - self._spawn_time
        if (self._visible_output and self._rendered_chars and elapsed >= 1.0
                and (now - self._last_output >= 0.5 or elapsed >= 6.5)):
            self._ready_timer.stop()
            self._set_state("active", "OpenCode is ready.")
            self._resize_embedded_ai()
            self._focus_view()

    def _set_state(self, state, text):
        self._state = state
        self._status_badge.setText({"idle": "Ready", "loading": "Loading…", "active": "Active", "error": "Unavailable"}[state])
        self._load_title.setText("OpenCode AI Assistant" if state != "error" else "OpenCode unavailable")
        self._load_sub.setText(text)
        self._retry_button.setVisible(state == "error")
        if state != "loading":
            self._ready_timer.stop()
        self._stack.setCurrentWidget(self._view if state == "active" and self._view else self._loader)
        self._apply_surfaces()

    def _retry(self):
        if self._closing:
            return
        if self._session and not self._session._closed and self._session.retry():
            return
        self._discard_view()
        self._requested = False
        self.ensure_started()

    def _hide_panel(self):
        owner = self.window()
        if owner is not self and hasattr(owner, "toggle_ai_panel"):
            owner.toggle_ai_panel(False)
        else:
            self.hide()

    def _fit_view(self):
        if self._view and self.isVisible() and not self._closing:
            self._view.page().runJavaScript("window.fitOpenCode?.()")

    def _focus_view(self):
        if self._state == "active" and self._view and self.isVisible() and not self._closing:
            self._view.setFocus()
            self._view.page().runJavaScript("window.focusOpenCode?.()")

    def showEvent(self, event):
        super().showEvent(event)
        self._resize_embedded_ai()
        QTimer.singleShot(0, self._focus_view)

    def _resize_embedded_ai(self):
        if self._view and self.isVisible() and not self._closing:
            self._fit_timer.start()

    def set_responsive_width(self, width):
        self._title.setText("OpenCode AI Assistant" if width >= 340 else "OpenCode AI")
        self._resize_embedded_ai()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())

    def _configure_view(self):
        if self._view:
            self._view.page().runJavaScript(
                f"window.configureOpenCode?.({json.dumps(themed_terminal_colors(self._theme))}, "
                f"{self._font_size}, {performance_profile().terminal_scrollback})")

    def _apply_surfaces(self):
        palette = get_palette(self._theme)
        self._header.setStyleSheet(f"QFrame#opencode-header {{ background: {palette['BG_DARK']}; border-bottom: 1px solid {palette['BORDER']}; }}")
        self._title.setStyleSheet(f"color: {palette['CYAN']}; font-size: 11px; font-weight: 700;")
        self._hide_button.setIcon(icon("close", palette['TEXT_DIM'], size=12))
        ink = palette['GREEN' if self._state == "active" else 'RED' if self._state == "error" else 'TEXT_DIM']
        self._status_badge.setStyleSheet(f"color: {ink}; font-size: 10px; font-weight: 600;")
        self._loader.setStyleSheet(f"QFrame {{ background: {palette['BG_DARKEST']}; border: none; }}")
        self._load_title.setStyleSheet(f"color: {palette['TEXT']}; font-size: 14px; font-weight: 700;")
        self._load_sub.setStyleSheet(f"color: {palette['TEXT_DIM']}; font-size: 11px;")

    def apply_theme(self, mode):
        self._theme = mode
        self._apply_surfaces()
        self._configure_view()

    @Slot(int)
    def set_font_size(self, size):
        self._font_size = max(6, min(48, int(size)))
        self._configure_view()

    def _discard_view(self):
        self._launch_generation += 1
        self._fit_timer.stop()
        self._ready_timer.stop()
        if self._session:
            self._session.close()
        self._session = self._channel = None
        if self._view:
            view, self._view = self._view, None
            self._stack.removeWidget(view)
            view.deleteLater()

    def reset_for_project(self, path):
        if Path(path).absolute() == Path(self._cwd()).absolute():
            return
        was_active = self._is_active
        self._discard_view()
        self._project_dir = str(path)
        self._requested = False
        if was_active:
            self.ensure_started()

    def _stop_ai(self):
        self._closing = True
        self._requested = False
        self._discard_view()

    def closeEvent(self, event):
        if self._closing:
            super().closeEvent(event)
        else:
            event.ignore()  # Only the owning workspace ends this container.
