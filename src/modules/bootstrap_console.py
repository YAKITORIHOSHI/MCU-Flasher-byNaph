"""Plain-text setup view used only until the private runtime's Qt is verified.

The release ships Qt, so this appears only when those packages are missing or
damaged. The installer pipeline stays in bootstrap.py. Workers enqueue events; only
the creating thread writes here. Once Python dependencies are ready the display
moves to the Qt window without restarting the installer worker.
"""
from __future__ import annotations

from collections import deque
import sys
import threading
import time

from src.modules.bootstrap_presentation import BootstrapPresentation, concise_status

_SC_CLOSE = 0xF060
_MF_BYCOMMAND = 0x0000


def _open_windows_console():
    """Show progress from a windowless Python; setup must survive its window closing."""
    import ctypes
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    if not kernel32.GetConsoleWindow():
        kernel32.FreeConsole()
        if not kernel32.AllocConsole():
            return None
    window = kernel32.GetConsoleWindow()
    if window:
        user32.DeleteMenu(user32.GetSystemMenu(window, False), _SC_CLOSE, _MF_BYCOMMAND)
    kernel32.SetConsoleCtrlHandler(None, True)
    return open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)


class ConsoleBootstrapView:
    """Same dispatcher contract as the Qt dialog, without any window toolkit."""

    MAX_SHOWN_EVENTS = 2500
    MAX_SHOWN_CHARS = 256000
    POLL_SECONDS = .03

    def __init__(self, gui, record_exception, *, dispatcher, stream=None):
        self.gui, self.record_exception, self.signals = gui, record_exception, dispatcher
        self.thread_id = threading.get_ident()
        self.closed = False
        self._presentation = BootstrapPresentation()
        self._shown = deque()
        self._shown_chars = 0
        self._status = ""
        self._progress = 0
        self._owns_console = False
        self._stream = stream if stream is not None else self._open_stream()
        self._set_title()
        dispatcher.attach(self.dispatch)
        self._write("MCU Flasher setup is preparing its components. Its window opens when they are ready.")

    def _open_stream(self):
        if sys.platform != "win32":
            return sys.stdout
        try:
            stream = _open_windows_console()
        except (AttributeError, OSError, ValueError) as error:
            self.record_exception(f"Setup console could not open: {error}")
            return None
        self._owns_console = stream is not None
        return stream

    def _write(self, text):
        if self._stream is None:
            return
        try:
            self._stream.write(text + "\n")
            self._stream.flush()
        except (OSError, ValueError):
            self._stream = None  # Setup continues; the durable log has every line.

    def _set_title(self):
        if sys.platform != "win32":
            return
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(f"MCU Flasher setup — {self._progress}%")
        except (AttributeError, OSError):
            pass

    def dispatch(self, name, args):
        if self.closed:
            return
        try:
            if name == "call":
                args[0](*args[1])
            elif name == "close":
                self._on_close()
            elif name != "hide":
                getattr(self, "_on_" + name)(*args)
        except Exception as error:  # pylint: disable=broad-except
            self.record_exception(f"Bootstrap console callback error: {error}")

    def _on_log(self, text, tag):
        self._shown.append((text, tag))
        self._shown_chars += len(text)
        while self._shown and (len(self._shown) > self.MAX_SHOWN_EVENTS or self._shown_chars > self.MAX_SHOWN_CHARS):
            self._shown_chars -= len(self._shown.popleft()[0])
        for event in self._presentation.summaries(text, tag):
            self._write(("" if event.tag in ("section", "subsection") else "  ") + event.text)

    def _on_status(self, text):
        status = concise_status(text)
        if status != self._status:
            self._status = status
            self._write(f"[{self._progress:3d}%] {status}")

    def _on_progress(self, value):
        self._progress = max(0, min(100, round(value)))
        self._set_title()

    def _on_stop_spinner(self, text, _ok):
        self._on_status(text)

    def _on_update_block(self, _block_type, _table):
        """Live tables appear in the Qt window; status lines report progress here."""

    def _on_commit_block(self):
        """Nothing to commit: live tables are not printed."""

    def _on_clear_block(self):
        """Nothing to clear: live tables are not printed."""

    def snapshot(self):
        """Lines this view already received; queued ones will reach the Qt window once."""
        return list(self._shown)

    def _on_close(self):
        self.signals.close()
        self.retire()

    def retire(self):
        """Close this view while retaining its dispatcher and installer state."""
        if self.closed:
            return
        self.closed = True
        stream, self._stream = self._stream, None
        if self._owns_console:
            try:
                stream.close()
                import ctypes
                ctypes.windll.kernel32.FreeConsole()
            except (AttributeError, OSError, ValueError):
                pass

    def pump(self):
        if not self.closed and threading.get_ident() == self.thread_id:
            self.signals.drain()

    def mainloop(self):
        while not self.closed:
            self.signals.drain()
            if not self.closed:
                time.sleep(self.POLL_SECONDS)


__all__ = ["ConsoleBootstrapView"]
