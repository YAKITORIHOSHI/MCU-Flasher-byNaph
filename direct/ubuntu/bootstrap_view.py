"""The native launcher's side of Ubuntu's Bootstrap window (system Python, no Qt).

A desktop launch has no terminal. Whenever the private runtime's Qt is usable,
setup, repair and source updates appear in the same Bootstrap window as on Windows
(``direct/ubuntu/bootstrap_window.py``). The launcher keeps doing the work and sends
the window one JSON event per line; the window is only a view.

The window outlives the launcher's restart after an update: ``hand_over`` leaves
its pipe open across ``exec`` and names it in ``MCU_FLASHER_BOOTSTRAP_WINDOW``, and
the restarted launcher ``adopt``s it. One window therefore covers the download, a
repair the new source needs and opening the workspace. A first run, before the
private runtime has Qt, still uses a terminal, as Windows uses its text console.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ENVIRONMENT_KEY = "MCU_FLASHER_BOOTSTRAP_WINDOW"
# A window gone within this time never appeared (no display, a broken Qt): that is
# not a person closing it, so an update carries on.
STARTUP_FAILURE_SECONDS = 3.0
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# setup.py announces each stage as "── [3/8] Python dependencies ──".
_STAGE = re.compile(r"^── \[(\d+)/(\d+)\] (.+?) ──$")


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def qt_runtime_present(root: Path) -> bool:
    """Whether the private runtime holds PySide6, without importing it here."""
    python = root / ".venv-linux/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        return False
    return any((root / ".venv-linux/lib").glob("python3*/site-packages/PySide6/QtWidgets.*so"))


class BootstrapWindow:
    def __init__(self, stream, pid: int, *, process: subprocess.Popen | None = None,
                 adopted: bool = False):
        self._stream = stream
        self._pid = pid
        self._process = process
        self._started = time.monotonic() - (STARTUP_FAILURE_SECONDS if adopted else 0.0)
        self._finished = False
        self._exited = False
        self._closed_stream = False

    @classmethod
    def open(cls, root: Path, *, env: dict[str, str], subtitle: str):
        """Start the window for a desktop launch, or return None to use another view."""
        if not _has_display() or not qt_runtime_present(root):
            return None
        script = root / "direct/ubuntu/bootstrap_window.py"
        if not script.is_file():
            return None
        try:
            folder = root / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            # Only the window's own diagnostics: setup output has its own run log.
            errors = open(folder / "ubuntu-bootstrap-window.log", "wb")
        except OSError:
            errors = subprocess.DEVNULL
        environment = dict(env)
        environment.pop(ENVIRONMENT_KEY, None)
        try:
            process = subprocess.Popen(
                [str(root / ".venv-linux/bin/python"), "-B", str(script), "--subtitle", subtitle],
                cwd=root, env=environment, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=errors, text=True, encoding="utf-8", errors="replace", start_new_session=True,
            )
        except OSError:
            return None
        finally:
            if errors is not subprocess.DEVNULL:
                errors.close()
        return cls(process.stdin, process.pid, process=process)

    @classmethod
    def adopt(cls, env: dict[str, str] | None = None):
        """Take over the window a launcher handed over before restarting, if any."""
        value = os.environ.pop(ENVIRONMENT_KEY, None)
        if env is not None:
            env.pop(ENVIRONMENT_KEY, None)
        match = re.fullmatch(r"(\d{1,6}):(\d{1,9})", value or "")
        if match is None:
            return None
        descriptor, pid = int(match.group(1)), int(match.group(2))
        if descriptor <= 2:
            return None
        try:
            os.fstat(descriptor)
            os.set_inheritable(descriptor, False)
            stream = os.fdopen(descriptor, "w", encoding="utf-8", errors="replace")
        except OSError:
            return None
        window = cls(stream, pid, adopted=True)
        return window if window.alive() else None

    def hand_over(self, env: dict[str, str]) -> bool:
        """Keep this window open across exec; the restarted launcher adopts it."""
        if self._finished or not self.alive():
            return False
        try:
            self._stream.flush()
            descriptor = self._stream.fileno()
            os.set_inheritable(descriptor, True)
        except (OSError, ValueError):
            return False
        env[ENVIRONMENT_KEY] = f"{descriptor}:{self._pid}"
        return True

    def take_back(self, env: dict[str, str]) -> None:
        """An exec that failed leaves the window with this launcher."""
        env.pop(ENVIRONMENT_KEY, None)
        try:
            os.set_inheritable(self._stream.fileno(), False)
        except (OSError, ValueError):
            pass

    def alive(self) -> bool:
        if self._exited:
            return False
        if self._process is not None:
            if self._process.poll() is None:
                return True
        else:
            try:
                pid, _status = os.waitpid(self._pid, os.WNOHANG)
                if pid == 0:
                    return True
            except ChildProcessError:
                try:
                    os.kill(self._pid, 0)
                    return True
                except OSError:
                    pass
        self._exited = True
        return False

    def _send(self, event: str, *args) -> None:
        if self._finished or self._closed_stream or not self.alive():
            return
        try:
            self._stream.write(json.dumps({"event": event, "args": list(args)}, ensure_ascii=False) + "\n")
            self._stream.flush()
        except (OSError, ValueError):
            # The window is only a view; setup never depends on it.
            self._closed_stream = True

    def log(self, text: str, tag: str = "normal") -> None:
        self._send("log", str(text)[:8192], tag)

    def section(self, text: str) -> None:
        self._send("log", str(text)[:8192], "section")

    def status(self, text: str) -> None:
        self._send("status", str(text)[:8192])

    def set_progress(self, percent: float) -> None:
        self._send("progress", max(0.0, min(100.0, float(percent))))

    def subtitle(self, text: str) -> None:
        self._send("subtitle", str(text))

    def cancellable(self, value: bool) -> None:
        self._send("cancellable", bool(value))

    # The app_update report/progress interface, shared with the zenity window.
    def message(self, text: str) -> None:
        self.log(text)
        self.status(text)

    def update(self, progress) -> None:
        self.status(progress.describe())
        if progress.percent is not None:
            self.set_progress(progress.percent)

    def cancelled(self) -> bool:
        return (not self._finished and not self.alive()
                and time.monotonic() - self._started >= STARTUP_FAILURE_SECONDS)

    def finish(self, ok: bool = True, text: str = "") -> None:
        """Success closes the window by itself; a failure stays on screen until closed."""
        if self._finished:
            return
        self._send("finish", bool(ok), str(text))
        self._finished = True
        try:
            self._stream.close()
        except (OSError, ValueError):
            pass
        self._closed_stream = True


def classify(line: str) -> str:
    lowered = line.casefold()
    if "bootstrap failed" in lowered or lowered.startswith(("error:", "error ")):
        return "fail"
    if lowered.startswith(("warning:", "warning ")):
        return "warn"
    if "runtime ready" in lowered or "available in all apps" in lowered:
        return "ok"
    return "normal"


def run_logged(command: list[str], *, cwd: Path, env: dict[str, str], window: BootstrapWindow,
               log_path: Path | None = None) -> int:
    """Run a setup child with its output in the window, a run log and this stdout.

    stdin is not a terminal, so administrator prompts use the desktop's own
    authentication dialog (pkexec) instead of a password typed into a terminal.
    """
    log = None
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("ab")
            window.log(f"Detailed run log: {log_path}", "dim")
        except OSError:
            log = None
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError:
        if log is not None:
            log.close()
        raise
    pending = b""
    try:
        while True:
            chunk = process.stdout.read1(65536) if hasattr(process.stdout, "read1") else process.stdout.read(65536)
            if not chunk:
                break
            if log is not None:
                log.write(chunk)
                log.flush()
            try:
                sys.stdout.buffer.write(chunk)
                sys.stdout.flush()
            except (AttributeError, OSError, ValueError):
                pass
            pending += chunk
            *lines, pending = re.split(rb"\r\n|\r|\n", pending)
            for raw in lines:
                _show_line(window, raw)
        if pending:
            _show_line(window, pending)
        return process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        if log is not None:
            log.close()


def _show_line(window: BootstrapWindow, raw: bytes) -> None:
    line = _ANSI.sub("", raw.decode("utf-8", errors="replace")).rstrip()
    if not line.strip():
        return
    stage = _STAGE.match(line.strip())
    if stage:
        index, count, name = int(stage.group(1)), max(1, int(stage.group(2))), stage.group(3)
        window.section(name)
        window.status(f"{name}…")
        window.set_progress(100.0 * (index - 1) / count)
        return
    window.log(line, classify(line))
