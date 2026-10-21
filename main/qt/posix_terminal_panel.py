"""Native Linux PTY terminal rendered by the existing offline xterm assets."""
from __future__ import annotations

import codecs
import json
import os
import shutil
import sys
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QPushButton, QLabel, QStackedWidget

from main.qt.log_colors import themed_terminal_colors
from main.platforms.ubuntu_pty import PromptObserver, close_pty_tree
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
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._drain)
        self._read_notifier = None
        self._write_notifier = None
        self._notifier_fd = None
        self._notifier_connections = []
        self._fallback_polling = False
        self._draining = False
        self._closed = False
        self._prompt_observer = None
        self.assistant_active = None
        self.prompt_source = "cli"

    @Slot(result=bool)
    def start(self):
        if self.process or self._closed:
            return False
        try:
            # Native descriptor modules are needed only for an explicit PTY
            # start; keep mocked Qt lifecycle checks importable on either host.
            from main.platforms.ubuntu_pty_process import NativePtyProcess
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
            # Partial writes are retained until Qt reports writable readiness.
            os.set_blocking(self.process.fd, False)
            self._install_notifiers()
            return True
        except (ImportError, OSError) as exc:
            # A failure after spawn still owns a live handle/descriptor.
            self.close()
            self.ended.emit(f"Terminal could not start: {exc}. Run the Ubuntu setup command to repair dependencies.")
            return False

    def _install_notifiers(self):
        try:
            self._notifier_fd = self.process.fd
            self._read_notifier = QSocketNotifier(self._notifier_fd, QSocketNotifier.Type.Read, self)
            self._read_notifier.setEnabled(False)
            self._write_notifier = QSocketNotifier(self._notifier_fd, QSocketNotifier.Type.Write, self)
            self._write_notifier.setEnabled(False)
            self._notifier_connections.append(self._read_notifier.activated.connect(self._read_ready))
            self._notifier_connections.append(self._write_notifier.activated.connect(self._write_ready))
            self._sync_notifiers()
        except (OSError, RuntimeError, TypeError) as exc:
            self._remove_notifiers()
            self._fallback_polling = True
            self.timer.setSingleShot(False)
            self._send_output(f"\r\nTerminal readiness is unavailable; using polling fallback: {exc}\r\n")
            if not self._closed:
                self.timer.start()

    def _remove_notifiers(self):
        self._notifier_fd = None
        self._pause_notifiers()
        for connection in self._notifier_connections:
            QObject.disconnect(connection)
        self._notifier_connections.clear()
        for name in ("_read_notifier", "_write_notifier"):
            notifier = getattr(self, name)
            setattr(self, name, None)
            if notifier is not None:
                notifier.deleteLater()

    def _pause_notifiers(self):
        for notifier in (self._read_notifier, self._write_notifier):
            if notifier is not None:
                notifier.setEnabled(False)

    def _sync_notifiers(self):
        if self._closed or self.process is None or self._fallback_polling:
            return
        ready = not self._draining and not self.timer.isActive()
        if self._read_notifier is not None:
            self._read_notifier.setEnabled(ready and self._pending_chars < 128_000)
        if self._write_notifier is not None:
            self._write_notifier.setEnabled(ready and bool(self._input_chunks))

    def _schedule_frame(self):
        if self._read_notifier is not None and not self._closed:
            self._pause_notifiers()
            self.timer.start()

    def _read_ready(self, *_):
        if (self.process is not None and not self._closed and not self._draining
                and not self.timer.isActive() and self.process.fd == self._notifier_fd
                and self._read_notifier is not None and self._read_notifier.isEnabled()):
            self._drain()

    def _write_ready(self, *_):
        if (self.process is not None and not self._closed and not self._draining
                and not self.timer.isActive() and self.process.fd == self._notifier_fd
                and self._write_notifier is not None and self._write_notifier.isEnabled()):
            self._drain()

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
                    self._prompt_observer = PromptObserver(self.cwd, active=self.assistant_active,
                                                           source=self.prompt_source)
                self._prompt_observer.feed(data, self.process.pid)
            except Exception:
                pass
            self._flush_input()

    def _flush_input(self):
        if not self.process or self._closed:
            return False
        wrote = False
        self._pause_notifiers()
        try:
            for _ in range(4 if self._performance.constrained else 8):
                if not self._input_chunks:
                    break
                chunk = self._input_chunks[0]
                written = os.write(self.process.fd, chunk[self._input_offset:self._input_offset + 4096])
                if written <= 0:
                    break
                wrote = True
                self._input_bytes -= written
                self._input_offset += written
                if self._input_offset == len(chunk):
                    self._input_chunks.popleft()
                    self._input_offset = 0
        except (BlockingIOError, InterruptedError):
            # Keep the exact remaining bytes until writable readiness returns.
            pass
        except OSError as exc:
            self._finish(str(exc))
        if self._input_bytes < 2 * 1024 * 1024:
            self._input_overflow_notified = False
        if not self._draining:
            if wrote and self._input_chunks:
                self._schedule_frame()
            self._sync_notifiers()
        return wrote

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
        self._sync_notifiers()

    def _send_output(self, text):
        if text:
            self._pending_chars += len(text.encode("utf-16-le")) // 2
            self._sync_notifiers()
            self.output.emit(text)

    def _drain(self):
        if not self.process or self._closed:
            return
        self._draining = True
        self._pause_notifiers()
        active = False
        chunks = []
        try:
            active = self._flush_input()
            if not self.process or self._closed or self._pending_chars >= 128_000:
                return
            # Bound work per frame so verbose commands cannot starve the GUI.
            for _ in range(4 if self._performance.constrained else 8):
                raw = self.process.read(4096)
                if raw is None:
                    break
                if not raw:
                    raise EOFError
                active = True
                text = self._decoder.decode(raw)
                if text:
                    chunks.append(text)
        except (BlockingIOError, InterruptedError):
            # Nonblocking descriptors can become empty after notification.
            pass
        except (OSError, EOFError):
            tail = self._decoder.decode(b"", final=True)
            if tail:
                chunks.append(tail)
            self._send_output("".join(chunks))
            self._finish("Session ended. Open a new session to continue.")
            return
        finally:
            self._draining = False
            if not self._closed:
                # One cooldown follows active work; an empty pass returns to
                # readiness with no recurring idle wakeups. A paused renderer
                # needs only its consumption acknowledgement, not a timer.
                if active and (self._pending_chars < 128_000 or self._input_chunks):
                    self._schedule_frame()
                self._sync_notifiers()
        self._send_output("".join(chunks))
        if self._pending_chars >= 128_000 and not self._input_chunks and not self._fallback_polling:
            self.timer.stop()
            self._sync_notifiers()

    def _finish(self, message):
        self.close()
        self.ended.emit(message)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.timer.stop()
        self._remove_notifiers()
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
        self._label.setWordWrap(True)
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
        view.loadFinished.connect(lambda ok: self._loaded(view, ok))
        if hasattr(view, "renderProcessTerminated"):
            view.renderProcessTerminated.connect(lambda *_: self._renderer_terminated(view))
        self._sessions[view] = (session, channel)
        self._label.setText("Project terminal")
        self._tabs.setCurrentIndex(self._tabs.addTab(view, label))
        self._update_session_state()
        view.setUrl(QUrl.fromLocalFile(str(ROOT / "src" / "editor" / "terminal.html")))

    def _loaded(self, view, ok):
        entry = self._sessions.get(view)
        if entry is None:
            return
        if ok:
            self._configure(view)
        else:
            entry[0].close()
            self._label.setText("Terminal assets failed to load.")

    def _renderer_terminated(self, view):
        entry = self._sessions.get(view)
        if entry is None:
            return
        # Once xterm is gone there is no reliable interactive state to recover.
        # Stop only this PTY, retain its tab, and never replay commands.
        entry[0].close()
        self._label.setText("Terminal view stopped. Start a new session.")

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
