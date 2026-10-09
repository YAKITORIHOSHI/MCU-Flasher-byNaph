"""Native OpenCode discovery and restart limits, without launching a process."""
from __future__ import annotations

import os
import json
import shutil
import time
from collections import deque
from pathlib import Path


_WINDOWS_SUFFIXES = frozenset({".exe", ".cmd", ".bat", ".ps1"})
_USER_BIN_DIRS = (".opencode/bin", ".local/bin", ".npm-global/bin", ".bun/bin")
ROOT = Path(__file__).resolve().parents[2]
MANAGED_OWNER = "mcu-flasher-ubuntu-opencode"


def managed_opencode_cli() -> str | None:
    """Locate a certified app-owned installation; Bootstrap verifies its hash."""
    directory = ROOT / ".ubuntu-tools" / "opencode"
    certificate = directory / "installation.json"
    try:
        if directory.parent.is_symlink() or directory.is_symlink() or certificate.is_symlink():
            return None
        if certificate.stat().st_size > 8192:
            return None
        state = json.loads(certificate.read_text(encoding="utf-8"))
        if state.get("owner") != MANAGED_OWNER or state.get("schema") != 1:
            return None
        executable = directory / "opencode"
        if executable.is_symlink():
            return None
        stat = executable.stat()
        if stat.st_size != state.get("size") or stat.st_mtime_ns != state.get("mtime_ns"):
            return None
        return _native_executable(executable)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _native_executable(candidate) -> str | None:
    """Accept native executable files while retaining their invocation spelling."""
    if not candidate:
        return None
    try:
        path = Path(os.path.abspath(os.fspath(candidate)))
        if path.suffix.casefold() in _WINDOWS_SUFFIXES:
            return None
        target = path.resolve(strict=True)
        if target.suffix.casefold() in _WINDOWS_SUFFIXES:
            return None
        if not target.is_file() or not os.access(path, os.X_OK):
            return None
        # A copied Windows binary may have been renamed to "opencode".
        with path.open("rb") as stream:
            if stream.read(2) == b"MZ":
                return None
        return str(path)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def find_opencode_cli() -> str | None:
    """Find installed OpenCode without shell startup, process scans or downloads."""
    managed = managed_opencode_cli()
    if managed:
        return managed
    try:
        found = _native_executable(shutil.which("opencode"))
    except (OSError, ValueError):
        found = None
    if found:
        return found
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        return None
    for directory in _USER_BIN_DIRS:
        found = _native_executable(home / directory / "opencode")
        if found:
            return found
    return None


def opencode_argv(executable, project) -> list[str]:
    """Keep the executable and exact project path as literal, separate arguments."""
    executable = os.fspath(executable)
    project = os.fspath(project)
    if not executable or not project:
        raise ValueError("OpenCode requires an executable and project directory.")
    return [executable, os.path.abspath(project)]


class OpenCodeRestartPolicy:
    """Permit fresh /exit sessions while bounding unexpected child restarts."""

    def __init__(self, max_restarts: int = 2, window_seconds: float = 60.0):
        self.max_restarts = int(max_restarts)
        self.window_seconds = float(window_seconds)
        if self.max_restarts < 0 or self.window_seconds <= 0:
            raise ValueError("Restart limits must be nonnegative with a positive window.")
        self._unexpected: deque[float] = deque(maxlen=self.max_restarts)

    def allow_restart(self, *, intentional: bool = False, now: float | None = None) -> bool:
        """Authorize only a new empty session; never retain or replay CLI input."""
        if intentional:
            return True
        moment = time.monotonic() if now is None else float(now)
        while self._unexpected and moment - self._unexpected[0] >= self.window_seconds:
            self._unexpected.popleft()
        if len(self._unexpected) >= self.max_restarts:
            return False
        self._unexpected.append(moment)
        return True

    def reset(self) -> None:
        """Start a new lifecycle after an explicitly changed project."""
        self._unexpected.clear()
