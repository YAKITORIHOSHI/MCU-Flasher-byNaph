"""Record real launch phases, so a slow start on any PC can be diagnosed.

Appends one bounded line per launch to logs/startup-timing.log. Timing can never
affect startup: every failure here is ignored.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_LOG = Path(__file__).resolve().parents[2] / "logs" / "startup-timing.log"
_KEEP_LINES = 40
_started = time.perf_counter()
_marks: dict[str, float] = {}


def mark(name: str) -> None:
    _marks.setdefault(name, round(time.perf_counter() - _started, 3))


def write() -> None:
    """Persist this launch's phases once, off the GUI thread's critical path."""
    try:
        record = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": os.getpid(), "seconds": _marks})
        lines = _LOG.read_text(encoding="utf-8").splitlines()[-(_KEEP_LINES - 1):] if _LOG.is_file() else []
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        _LOG.write_text("\n".join([*lines, record]) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        pass
