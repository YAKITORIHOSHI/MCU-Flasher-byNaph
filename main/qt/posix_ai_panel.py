"""Dedicated, workspace-owned AI assistant view for Linux and macOS desktops."""
from __future__ import annotations

import json
from pathlib import Path
import re
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, QSize, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QStackedLayout, QSizePolicy

from main.core.ai_agents import DEFAULT_AGENT, agent as agent_spec, agent_argv, agent_environment, find_agent, last_agent, remember_agent
from main.core.theme import Theme, get_palette
from main.qt.agent_chooser import AgentChooser
from main.qt.agent_picker import AgentPicker
from main.qt.assistant_loader import (AssistantLoader, restart_controls, set_restart_visible, style_restart_controls,
                                      switch_control)
from main.core.log_colors import glass_inks, glass_terminal_colors
from main.qt.glass import PageBackdrop, page_surface
from main.qt.icons import icon
from main.qt.posix_terminal_panel import PtySession
from main.platforms.ubuntu_opencode import find_opencode_cli, opencode_argv, OpenCodeRestartPolicy
from main.platforms.ubuntu_assistant import prepare_context
from src.modules.ai_prompt_context import PromptInputTracker
from src.modules.runtime_resources import performance_profile

ROOT = Path(__file__).resolve().parents[2]
ASSISTANT_ASSETS = (
    "src/editor/opencode.html", "src/editor/qwebchannel.js", "src/editor/assistant_readiness.js", "src/editor/glass_page.js",
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

    def __init__(self, cwd, executable, parent=None, agent_id=DEFAULT_AGENT):
        super().__init__(parent)
        self.cwd, self.executable = cwd, executable
        self.agent = agent_spec(agent_id)
        self._exit_commands = {"/exit"} if self.agent.id == DEFAULT_AGENT else {"/exit", "/quit"}
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
        argv = (opencode_argv(self.executable, self.cwd) if self.agent.id == DEFAULT_AGENT
                else agent_argv(self.agent.id, self.executable, self.cwd))
        child = PtySession(self.cwd, argv, self)
        child.extra_environment = agent_environment(self.agent.id)
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
            self.status.emit("error", self._start_error
                             or f"{self.agent.name} could not start. Check its installation and retry.")
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
            self.status.emit("loading", f"Starting a fresh {self.agent.name} session…")
            self._restart_timer.start()
        else:
            text = f"{self.agent.name} stopped repeatedly. The assistant stays attached; retry after checking its configuration."
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
                        and observer.line.strip().casefold() in self._exit_commands):
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
    """Single protected assistant interface, separate from project terminals."""
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
        self._agent = agent_spec(DEFAULT_AGENT)
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
        self._title = QLabel("AI Assistant", self._header)
        self._title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self._title, 1)
        self._status_badge = QLabel("Ready", self._header)
        row.addWidget(self._status_badge)
        # Restart sits beside the status, set apart by a divider.
        self._restart_divider, self._btn_restart = restart_controls(self._header, self._restart_session)
        row.addSpacing(4)
        row.addWidget(self._restart_divider, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addSpacing(4)
        row.addWidget(self._btn_restart, 0, Qt.AlignmentFlag.AlignVCenter)
        self._btn_switch = switch_control(self._header, self._switch_agent)
        row.addWidget(self._btn_switch, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._header)
        self._body = QWidget(self)
        self._stack = QStackedLayout(self._body)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self._loader = AssistantLoader(self._body)
        self._load_title, self._load_sub = self._loader.title, self._loader.status
        self._retry_button = self._loader.retry_button
        self._loader.show_idle("Open the assistant to start it in this project.")
        self._loader.retry_requested.connect(self._retry)
        self._loader.reveal_requested.connect(self._show_interface)
        self._stack.addWidget(self._loader)
        self._chooser = AgentChooser(self._body)
        self._chooser.setEnabled(False)
        self._picker = AgentPicker(self._chooser, self)
        self._picker.chosen.connect(self.start_agent)
        self._stack.addWidget(self._chooser)
        self._stack.setCurrentWidget(self._loader)
        layout.addWidget(self._body, 1)

    def _cwd(self):
        return str(self._project_dir or getattr(self._backend, "sketch_dir_path", "") or ROOT)

    def ensure_started(self):
        """Revealing the assistant offers the list; only a choice starts anything."""
        if self._requested or self._closing:
            return
        if self._state != "choose":
            self._chooser.select(last_agent())
            self._set_state("choose", "")
        if not self._picker.downloading:
            self._picker.refresh()

    @Slot(str)
    def start_agent(self, agent_id):
        """Start the chosen assistant in the current project."""
        if self._requested or self._closing:
            return
        self._agent = agent_spec(agent_id)
        self._loader.set_agent(self._agent.name)
        remember_agent(self._agent.id)
        self._launch()

    def _switch_agent(self):
        """End this session and offer the list again."""
        if self._closing:
            return
        self._discard_view()
        self._requested = False
        self._state = "idle"
        self.ensure_started()

    def _launch(self):
        if self._requested or self._closing:
            return
        self._requested = True
        self._launch_generation += 1
        generation, project, spec = self._launch_generation, self._cwd(), self._agent
        self._project_dir = project  # Pin pending discovery before the backend can change projects.
        self._set_state("loading", f"Finding {spec.name} for this project…")

        def discover():
            try:
                cwd = str(Path(project).resolve())
                executable = None
                if Path(cwd).is_dir():
                    executable = find_opencode_cli() if spec.id == DEFAULT_AGENT else find_agent(spec.id)
                message = "" if executable else (
                    "OpenCode is unavailable. Close the workspace and run Ubuntu Bootstrap repair: bash direct/ubuntu/run.sh --repair."
                    if spec.id == DEFAULT_AGENT else f"{spec.name} is not here any more. Choose another assistant or download it again.")
                if executable and any(not (ROOT / name).is_file() for name in ASSISTANT_ASSETS):
                    executable = None
                    message = "The assistant view assets are missing. Restore the application's local assistant assets, then retry."
                if executable and not prepare_context(self._backend, cwd,
                        cancelled=lambda: self._closing or generation != self._launch_generation):
                    return
            except Exception as exc:
                cwd, executable, message = project, None, f"{spec.name} discovery failed: {exc}"
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
        view.page().setBackgroundColor(QColor(get_palette(self._theme)["BG_DARKEST"]))
        self._backdrop = PageBackdrop(
            view, lambda values: view.page().runJavaScript(f"window.setGlassBackdrop?.({json.dumps(values)})"))
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        session = OpenCodeSession(cwd, executable, view, self._agent.id)
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
        url = QUrl.fromLocalFile(str(ROOT / "src/editor/opencode.html"))
        if self._agent.inline:
            url.setQuery("inline=1")
        view.setUrl(url)

    def _loaded(self, view, ok):
        if self._closing or view is not self._view:
            return
        if ok:
            self._configure_view()
        else:
            if self._session:
                self._session.close()
            self._set_state("error", "The assistant view could not load. Restore the application's local assistant assets and retry.")

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
        self._set_state("active", f"{self._agent.name} is ready.")
        self._resize_embedded_ai()
        self._focus_view()

    def _renderer_terminated(self, view):
        if view is not self._view or self._closing:
            return
        # A renderer cannot reconstruct a live interactive screen reliably.
        # Keep the protected container; require an explicit fresh-session retry.
        if self._session:
            self._session.close()
        self._set_state("error", f"The {self._agent.name} view stopped. Retry to start a fresh session; submitted commands will not be replayed.")

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
            self._loader.show_error(f"{self._agent.name} unavailable", text)
        elif state == "active":
            self._loader.finish()
        elif state != "choose":
            self._loader.show_idle(text)
        # Covered pages stay in the stack; only the one on top may take keys.
        self._chooser.setEnabled(state == "choose")
        self._stack.setCurrentWidget(self._chooser if state == "choose" else
                                     self._view if state == "active" and self._view else self._loader)
        self._apply_surfaces()

    def _retry(self):
        if self._closing:
            return
        if self._session and not self._session._closed and self._session.retry():
            return
        self._discard_view()
        self._requested = False
        self._launch()

    def _restart_session(self):
        """Start a fresh session of this assistant in the current project; nothing typed earlier is replayed."""
        if not self._is_active:
            return
        self._discard_view()
        self._requested = False
        self._launch()

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
        choosing = self._state in ("idle", "choose") and not self._requested
        title = "AI Assistant" if choosing else f"{self._agent.name} AI Assistant"
        status = {"idle": "Ready", "choose": "Ready", "loading": "Loading…", "active": "Active", "error": "Unavailable"}[self._state]
        row = self._header.layout()
        margins = row.contentsMargins()
        reserved = margins.left() + margins.right() + row.spacing()
        status_width = self._status_badge.fontMetrics().horizontalAdvance(status)
        if width < max(340, reserved + status_width + self._title.fontMetrics().horizontalAdvance(title) + 2):
            title = "AI" if choosing else f"{self._agent.name} AI"
        if width < reserved + status_width + self._title.fontMetrics().horizontalAdvance(title) + 2:
            status_text = {"loading": "Wait…", "error": "Retry"}.get(self._state, status)
        else:
            status_text = status
        if width < 180:
            title = "AI"
        self._title.setText(title)
        self._status_badge.setText(status_text)
        self._status_badge.setToolTip(status)
        set_restart_visible(self._restart_divider, self._btn_restart, self._is_active, width, self._btn_switch)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.set_responsive_width(event.size().width())

    def _configure_view(self):
        if self._view:
            self._view.page().runJavaScript(f"window.setGlassSurface?.({json.dumps(page_surface(self._theme))})")
            self._backdrop.forget()
            self._view.page().runJavaScript(
                f"window.configureOpenCode?.({json.dumps(glass_terminal_colors(self._theme))}, "
                f"{self._font_size}, {performance_profile().terminal_scrollback})")

    def _apply_surfaces(self):
        palette = get_palette(self._theme)
        self._header.setStyleSheet("QFrame#opencode-header { background: transparent; border: none; }")
        inks = glass_inks(self._theme)
        self._title.setStyleSheet(f"color: {inks['system']}; font-size: 11px; font-weight: 700;")
        ink = inks['success' if self._state == "active" else 'error' if self._state == "error" else 'normal']
        self._status_badge.setStyleSheet(f"color: {ink}; font-size: 10px; font-weight: 600;")
        style_restart_controls(self._restart_divider, self._btn_restart, self._theme, self._btn_switch)

    def apply_theme(self, mode):
        self._theme = mode
        self._loader.apply_theme(mode)
        self._chooser.apply_theme(mode)
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
        # A hidden assistant must not start a new CLI merely because a local
        # or cloud project opened. Reveal offers the list for the new project.
        if was_active and not self.isHidden():
            self._launch()
        elif was_active:
            self._state = "idle"
        self._refresh_header(self.width())

    def _stop_ai(self):
        self._closing = True
        self._requested = False
        self._picker.close()
        self._discard_view()
        self._refresh_header(self.width())

    def closeEvent(self, event):
        if self._closing:
            super().closeEvent(event)
        else:
            event.ignore()  # Only the owning workspace ends this container.
