"""Bounded passive prompt observation and identity-checked POSIX PTY teardown."""
from collections import deque
import threading


class PromptObserver:
    """Observe input on a worker without delaying or changing native PTY bytes."""
    def __init__(self, project, *, active=None):
        from src.modules.ai_prompt_context import PromptInputTracker
        self._tracker = PromptInputTracker(project)
        self._active = active
        self._known_active = False
        self._condition = threading.Condition()
        self._pending = deque()
        self._bytes = 0
        self._closed = False
        self._worker = None
        self._reset = False

    def feed(self, data, pid):
        with self._condition:
            if self._closed:
                return
            size = len(data.encode("utf-8"))
            if self._bytes + size > 1024 * 1024:
                self._pending.clear()
                self._bytes = 0
                self._reset = True  # Drop observation only, never terminal input.
                return
            self._pending.append((data, pid, size))
            self._bytes += size
            if self._worker is None:
                worker = threading.Thread(target=self._run, name="MCU_PtyPromptContext", daemon=True)
                worker.start()
                self._worker = worker
            self._condition.notify()

    def _run(self):
        from src.modules.ai_prompt_context import assistant_process_active, PromptInputTracker
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending)
                if self._closed:
                    return
                data, pid, size = self._pending.popleft()
                self._bytes -= size
                if self._reset:
                    self._tracker = PromptInputTracker(self._tracker.project)
                    self._reset = False
            try:
                active = self._active
                if active is None:
                    if "\r" in data or "\n" in data:
                        self._known_active = assistant_process_active(pid)
                    active = self._known_active
                if not self._closed:
                    self._tracker.feed(data, active=active)
            except Exception:
                pass  # Context failure must never consume or replay a command.

    def close(self):
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._bytes = 0
            self._condition.notify_all()


def close_pty_tree(process):
    """Close an owned PTY and its checked descendants, outside the GUI thread."""
    children = []
    try:
        import psutil
        # A live owned PTY handle has not reaped its original child yet, so
        # that child's PID cannot be reused while identities are captured.
        if process.isalive():
            leader = psutil.Process(process.pid)
            children = leader.children(recursive=True)
            for child in reversed(children):
                try:
                    child.terminate()  # psutil checks the process creation identity.
                except psutil.Error:
                    pass
            _, alive = psutil.wait_procs(children, timeout=.3)
            for child in alive:
                try:
                    child.kill()
                except psutil.Error:
                    pass
    except Exception:
        pass
    finally:
        try:
            process.close(force=True)
        except (OSError, EOFError):
            pass
