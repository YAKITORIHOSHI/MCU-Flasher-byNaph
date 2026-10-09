"""Native Linux PTY terminal rendered by the existing offline xterm assets."""
from __future__ import annotations

import codecs
import json
import os
import select
import shutil
import sys
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QPushButton, QLabel, QStackedWidget

from main.qt.log_colors import themed_terminal_colors
from main.platforms.ubuntu_pty import PromptObserver, close_pty_tree
from main.platforms.ubuntu_pty_process import NativePtyProcess
from src.modules.runtime_resources import performance_profile

ROOT = Path(__file__).resolve().parents[2]


class PtySession(QObject):
    output = Signal(str)
    ended = Signal(str)

    def __init__(self, cwd, argv, parent=None):
        super().__init__(parent)
        self.cwd, self.argv = cwd, argv
        self.process = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._performance = performance_profile()
        self._pending_chars = 0
        self._input_chunks = deque()
        self._input_offset = 0
        self._input_bytes = 0
        self._input_overflow_notified = False
        self._dimensions = (24, 80)
        self.timer = QTimer(self)
        self.timer.setInterval(self._performance.terminal_interval_ms)
        self.timer.timeout.connect(self._drain)
        self._closed = False
        self._prompt_observer = None
        self.assistant_active = None

    @Slot(result=bool)
    def start(self):
        if self.process or self._closed:
            return False
        try:
            env = os.environ.copy()
            env.pop("PYTHONHOME", None)
            env.pop("PYTHONPATH", None)
            env.pop("MCU_FLASHER_OFFLINE_RUNTIME", None)
            env.pop("MCU_FLASHER_WORKSPACE_RUNTIME", None)
            env.pop("PIP_NO_INDEX", None)
            if "NO_COLOR" in env:
                env.pop("FORCE_COLOR", None)
            env.update(TERM="xterm-256color", COLORTERM="truecolor", TERM_PROGRAM="MCUFlasher")
            # The private supervisor adopts detached tool descendants when a
            # shell or assistant exits, so they cannot outlive this session.
            supervisor = ROOT / "main/platforms/ubuntu_pty_supervisor.py"
            if not supervisor.is_file():
                raise OSError("The Ubuntu terminal supervisor is missing")
            command = [sys.executable, "-I", "-B", str(supervisor), "--", *self.argv]
            self.process = NativePtyProcess.spawn(command, cwd=self.cwd, env=env, dimensions=self._dimensions)
            # A full PTY input buffer must never block Qt during a large paste.
            # Partial writes are retained and drained by the session timer.
            os.set_blocking(self.process.fd, False)
            self.timer.start()
            return True
        except (ImportError, OSError) as exc:
            self.ended.emit(f"Terminal could not start: {exc}. Run the Ubuntu setup command to repair dependencies.")
            return False

    @Slot(str)
    def write(self, data):
        if self.process and not self._closed:
            encoded = data.encode("utf-8")
            if self._input_bytes + len(encoded) > 4 * 1024 * 1024:
                if not self._input_overflow_notified:
                    self._input_overflow_notified = True
                    self._send_output("\r\nTerminal input is busy; this input was not sent. Wait for the command to read its input before pasting again.\r\n")
                return
            if not encoded:
                return
            self._input_chunks.append(encoded)
            self._input_bytes += len(encoded)
            try:
                if self._prompt_observer is None:
                    self._prompt_observer = PromptObserver(self.cwd, active=self.assistant_active)
                self._prompt_observer.feed(data, self.process.pid)
            except Exception:
                pass
            self._flush_input()

    def _flush_input(self):
        if not self.process or self._closed:
            return
        try:
            for _ in range(4 if self._performance.constrained else 8):
                if not self._input_chunks:
                    break
                chunk = self._input_chunks[0]
                written = os.write(self.process.fd, chunk[self._input_offset:self._input_offset + 4096])
                if written <= 0:
                    break
                self._input_bytes -= written
                self._input_offset += written
                if self._input_offset == len(chunk):
                    self._input_chunks.popleft()
                    self._input_offset = 0
        except (BlockingIOError, InterruptedError):
            # Keep the exact remaining bytes for the next timer tick.
            pass
        except OSError as exc:
            self._finish(str(exc))
        if self._input_bytes < 2 * 1024 * 1024:
            self._input_overflow_notified = False

    @Slot(int, int)
    def resize(self, rows, columns):
        dimensions = (max(2, min(rows, 500)), max(10, min(columns, 1000)))
        if self._dimensions == dimensions:
            return
        self._dimensions = dimensions
        if self.process and not self._closed:
            try:
                self.process.setwinsize(*dimensions)
            except OSError:
                self._finish("Terminal session ended.")

    @Slot(int)
    def consumed(self, chars):
        self._pending_chars = max(0, self._pending_chars - max(0, chars))

    def _send_output(self, text):
        if text:
            self._pending_chars += len(text.encode("utf-16-le")) // 2
            self.output.emit(text)

    def _drain(self):
        self._flush_input()
        if not self.process or self._closed or self._pending_chars >= 128_000:
            return
        chunks = []
        try:
            # Bound work per frame so verbose commands cannot starve the GUI.
            for _ in range(4 if self._performance.constrained else 8):
                if not select.select([self.process.fd], [], [], 0)[0]:
                    break
                raw = self.process.read(4096)
                if raw is None:
                    break
                if not raw:
                    raise EOFError
                text = self._decoder.decode(raw)
                if text:
                    chunks.append(text)
        except (BlockingIOError, InterruptedError):
            # Readiness can change after select; this is temporary backpressure.
            pass
        except (OSError, EOFError):
            tail = self._decoder.decode(b"", final=True)
            if tail:
                chunks.append(tail)
            self._send_output("".join(chunks))
            self._finish("Session ended. Open a new session to continue.")
            return
        self._send_output("".join(chunks))

    def _finish(self, message):
        self.close()
        self.ended.emit(message)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.timer.stop()
        self._input_chunks.clear()
        self._input_bytes = self._input_offset = 0
        if self._prompt_observer:
            self._prompt_observer.close()
        if self.process:
            process, self.process = self.process, None
            import threading
            # Python waits for this bounded cleanup on exit; a daemon could
            # disappear before killing an assistant's still-running tools.
            threading.Thread(target=close_pty_tree, args=(process,), name="MCU_ClosePtyTree", daemon=False).start()


class PosixTerminalPanel(QWidget):
    @property
    def _is_active(self):
        return bool(self._sessions)

    def __init__(self, backend=None, parent=None):
        super().__init__(parent)
        self._backend = backend
        self._theme = "default"
        from main.core.config import get_monitor_font_size
        self._font_size = get_monitor_font_size()
        self._sessions = {}
        self._project_dir = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        self._label = QLabel("Project terminal", self)
        header.addWidget(self._label, 1)
        self._session_actions = []
        for label, handler in (("New Bash", self.add_session), ("Clear", self._clear), ("End session", self._end_current)):
            button = QPushButton(label, self)
            button.clicked.connect(handler)
            header.addWidget(button)
            if label != "New Bash":
                self._session_actions.append(button)
                button.setEnabled(False)
        layout.addLayout(header)
        self._stack = QStackedWidget(self)
        self._empty = QLabel("No terminal sessions\nChoose New Bash to start a shell in this project.", self._stack)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._stack.addWidget(self._empty)
        self._tabs = QTabWidget(self._stack)
        self._tabs.setTabsClosable(True)
        self._tabs.tabCloseRequested.connect(self._close_tab)
        self._tabs.currentChanged.connect(self._update_session_state)
        self._stack.addWidget(self._tabs)
        layout.addWidget(self._stack, 1)
        self._label.setToolTip("Commands run in the selected sketch directory. Sessions never restart or replay commands automatically.")

    def _update_session_state(self, index=None):
        has_session = bool(self._sessions)
        self._stack.setCurrentWidget(self._tabs if has_session else self._empty)
        for button in self._session_actions:
            button.setEnabled(has_session)

    def _cwd(self):
        project = self._project_dir or str(getattr(self._backend, "sketch_dir_path", "") or ROOT)
        return str(Path(project).resolve())

    def add_session(self, checked=False, *, argv=None, label="Bash"):
        shell = shutil.which("bash") or shutil.which("sh")
        if argv is None:
            if not shell:
                self._label.setText("Install Bash to open a terminal.")
                return
            argv = [shell, "-i"]
        view = QWebEngineView(self)
        channel = QWebChannel(view.page())
        session = PtySession(self._cwd(), argv, view)
        channel.registerObject("terminalBridge", session)
        view.page().setWebChannel(channel)
        session.output.connect(lambda data: view.page().runJavaScript(f"window.writeOutput?.({json.dumps(data)})"))
        session.ended.connect(lambda data: view.page().runJavaScript(f"window.sessionEnded?.({json.dumps(data)})"))
        view.loadFinished.connect(lambda ok: self._configure(view) if ok else self._label.setText("Terminal assets failed to load."))
        self._sessions[view] = (session, channel)
        self._tabs.setCurrentIndex(self._tabs.addTab(view, label))
        self._update_session_state()
        view.setUrl(QUrl.fromLocalFile(str(ROOT / "src" / "editor" / "terminal.html")))

    def _configure(self, view):
        theme = themed_terminal_colors(self._theme)
        scrollback = performance_profile().terminal_scrollback
        view.page().runJavaScript(f"window.configureTerminal?.({json.dumps(theme)}, {self._font_size}, {scrollback})")

    def _clear(self):
        view = self._tabs.currentWidget()
        if view:
            view.page().runJavaScript("window.clearTerminal?.()")

    def _end_current(self):
        if self._tabs.currentIndex() >= 0:
            self._close_tab(self._tabs.currentIndex())

    def _close_tab(self, index):
        view = self._tabs.widget(index)
        if view is None:
            return
        if view in self._sessions:
            self._sessions.pop(view)[0].close()
        self._tabs.removeTab(index)
        self._update_session_state()
        view.deleteLater()

    def _on_tab_revealed(self):
        self._resize_embedded_terminal()

    def _on_tab_hidden(self):
        pass

    def _resize_embedded_terminal(self):
        view = self._tabs.currentWidget()
        if view:
            view.page().runJavaScript("window.fitTerminal?.()")

    def set_responsive_width(self, width):
        self._resize_embedded_terminal()

    def has_input_focus(self):
        from PySide6.QtWidgets import QApplication
        widget = QApplication.focusWidget()
        return bool(widget and self.isVisible() and (widget is self or self.isAncestorOf(widget)))

    def set_font_size(self, size):
        self._font_size = max(6, min(48, int(size)))
        for view in self._sessions:
            self._configure(view)

    def apply_theme(self, mode):
        self._theme = mode
        for view in self._sessions:
            self._configure(view)

    def connect_signals(self, bus, *, connect_theme=True):
        if connect_theme:
            bus.theme_changed.connect(self.apply_theme)
        if hasattr(bus, "font_size_changed"):
            bus.font_size_changed.connect(self.set_font_size)

    def reset_for_project(self, path):
        self._stop_shell()
        self._project_dir = path
        if self.isVisible():
            self._on_tab_revealed()

    def _stop_shell(self):
        while self._tabs.count():
            self._close_tab(0)

    def closeEvent(self, event):
        self._stop_shell()
        super().closeEvent(event)


def __getattr__(name):
    # Keep the older import available without a circular eager dependency.
    if name == "PosixAIPanel":
        from main.qt.posix_ai_panel import PosixAIPanel
        return PosixAIPanel
    raise AttributeError(name)
