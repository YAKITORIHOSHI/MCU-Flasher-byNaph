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
from PySide6.QtWidgets import QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QStackedLayout, QSizePolicy

from main.core.theme import Theme, get_palette
from main.qt.assistant_loader import AssistantLoader
from main.qt.log_colors import themed_terminal_colors
from main.qt.icons import icon
from main.qt.posix_terminal_panel import PtySession
from main.platforms.ubuntu_opencode import find_opencode_cli, opencode_argv, OpenCodeRestartPolicy
from main.platforms.ubuntu_assistant import prepare_context
from src.modules.ai_prompt_context import PromptInputTracker
from src.modules.runtime_resources import performance_profile

ROOT = Path(__file__).resolve().parents[2]
ASSISTANT_ASSETS = (
    "src/editor/opencode.html", "src/editor/qwebchannel.js", "src/editor/assistant_readiness.js",
    "src/assets/xterm/xterm.js", "src/assets/xterm/xterm.css",
    "src/assets/xterm/xterm-addon-fit.js",
)
# String sequences come first: a graphics probe's APC payload is control data, not text.
_ANSI_OUTPUT = re.compile(r"\x1b[P_^X].*?(?:\x1b\\|\x07|\Z)|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|\Z)"
                          r"|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]", re.DOTALL)


class OpenCodeSession(QObject):
    """Stable browser bridge around fresh native PTYs; never replay input."""
    output = Signal(int, str)
    reset_view = Signal(int)
    status = Signal(str, str)
    workspace_loading = Signal(int)
    interface_ready = Signal(int, str)

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
        self._output_seen = False
        self._dimensions = (24, 80)
        self._policy = OpenCodeRestartPolicy()
        self._intentional_exit = False
        self._exit_requested_at = 0.0
        self._line_pasted = False
        self._exit_observer = PromptInputTracker(cwd, source="assistant")
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
        self._exit_observer = PromptInputTracker(self.cwd, source="assistant")
        self._diagnostic_tail = ""
        self._output_seen = False
        child = PtySession(self.cwd, opencode_argv(self.executable, self.cwd), self)
        child.assistant_active = True
        child.prompt_source = "assistant"
        self._child = child
        child.resize(*self._dimensions)
        child.output.connect(lambda data: self._forward_output(child, generation, data))
        child.ended.connect(lambda message: self._ended(child, generation, message))
        self.reset_view.emit(generation)
        self.status.emit("loading", "Starting a session…")
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
            if not self._output_seen:
                self._output_seen = True
                self.workspace_loading.emit(generation)
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

    @Slot(int, str)
    def report_interface_ready(self, generation, reason):
        """The page saw OpenCode's interface painted; assistant_readiness.js decides."""
        if not self._closed and generation == self._generation and self._child:
            self.interface_ready.emit(generation, reason)

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
        self._theme = Theme.active_theme
        self._font_size = get_monitor_font_size()
        self._requested = False
        self._closing = False
        self._project_dir = ""
        self._launch_generation = 0
        self._session = self._view = self._channel = None
        self._state = "idle"
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
        self._title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
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
        self._loader = AssistantLoader(self._body)
        self._load_title, self._load_sub = self._loader.title, self._loader.status
        self._retry_button = self._loader.retry_button
        self._loader.show_idle("Open the assistant to start OpenCode in this project.")
        self._loader.retry_requested.connect(self._retry)
        self._loader.reveal_requested.connect(self._show_interface)
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
        self._set_state("loading", "Finding OpenCode for this project…")

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
        session.workspace_loading.connect(lambda epoch: self._workspace_loading(session))
        session.interface_ready.connect(lambda epoch, reason: self._interface_ready(session))
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
        self._set_state(state, text, phase=1)

    def _workspace_loading(self, session):
        if session is self._session and self._state == "loading" and not self._closing:
            self._loader.show_progress("Loading your workspace…", 2)

    def _interface_ready(self, session):
        if session is self._session and self._state == "loading" and not self._closing:
            self._show_interface()

    def _show_interface(self):
        """Lift the cover: the interface painted, or the user asked to see the output."""
        if self._closing or not self._view:
            return
        self._set_state("active", "OpenCode is ready.")
        self._resize_embedded_ai()
        self._focus_view()

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

    def _set_state(self, state, text, phase=0):
        self._state = state
        self._refresh_header(self.width())
        if state == "loading":
            self._loader.show_progress(text, phase)
        elif state == "error":
            self._loader.show_error("OpenCode unavailable", text)
        elif state == "active":
            self._loader.finish()
        else:
            self._loader.show_idle(text)
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
        self._refresh_header(width)
        self._resize_embedded_ai()

    def _refresh_header(self, width):
        title = "OpenCode AI Assistant"
        status = {"idle": "Ready", "loading": "Loading…", "active": "Active", "error": "Unavailable"}[self._state]
        row = self._header.layout()
        margins = row.contentsMargins()
        reserved = margins.left() + margins.right() + 2 * row.spacing() + self._hide_button.width()
        status_width = self._status_badge.fontMetrics().horizontalAdvance(status)
        if width < max(340, reserved + status_width + self._title.fontMetrics().horizontalAdvance(title) + 2):
            title = "OpenCode AI"
        if width < reserved + status_width + self._title.fontMetrics().horizontalAdvance(title) + 2:
            status_text = {"loading": "Wait…", "error": "Retry"}.get(self._state, status)
        else:
            status_text = status
        self._title.setText(title)
        self._status_badge.setText(status_text)
        self._status_badge.setToolTip(status)

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

    def apply_theme(self, mode):
        self._theme = mode
        self._loader.apply_theme(mode)
        self._apply_surfaces()
        self._configure_view()

    @Slot(int)
    def set_font_size(self, size):
        self._font_size = max(6, min(48, int(size)))
        self._configure_view()

    def _discard_view(self):
        self._launch_generation += 1
        self._fit_timer.stop()
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
