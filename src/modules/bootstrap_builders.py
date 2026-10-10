"""Stream bootstrap builder output with bounded memory and durable diagnostics."""
from __future__ import annotations

import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import signal
import subprocess
import sys
import threading
import time

from src.modules.bootstrap_output import known_builder_notice, output_chunks

QUEUE_CHUNKS = 32
READ_CHARS = 4096
TAIL_CHARS = 4000
DISPLAY_CHARS = 3000


class _DisplayRecords:
    """Preserve line/progress boundaries while bounding a long display record."""
    def __init__(self, log):
        self.log = log
        self.pending = []
        self.shortened = False
        self.progress = False

    def feed(self, chunk):
        for char in chunk:
            if char in "\r\n":
                self.flush()
                continue
            if len(self.pending) < DISPLAY_CHARS:
                self.pending.append(char)
            else:
                self.shortened = True
            if char == "%" and not self.shortened:
                text = "".join(self.pending).strip()
                if re.fullmatch(r"(?:Downloading|Unpacking)\s+\d+(?:\.\d+)?%", text, re.I):
                    self.progress = True
                    self.flush(progress=True)
                elif self.progress and re.fullmatch(r"\d+(?:\.\d+)?%", text):
                    self.flush(progress=True)

    def flush(self, *, progress=False):
        text = "".join(self.pending).strip()
        self.pending.clear()
        if text:
            if self.shortened:
                suffix = " … [display shortened; full output in builder log]"
                text = text[:DISPLAY_CHARS - len(suffix)] + suffix
            notice = known_builder_notice(text)
            self.log(notice or text)
            if not progress and not notice:
                self.progress = bool(re.fullmatch(r"(?:Downloading|Unpacking)(?:\.{3})?", text, re.I))
        self.shortened = False


def _terminate_tree(process):
    """Stop the owned builder and descendants without retrying its work."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        # Descendants can retain the pipe after the parent has already exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_builder(command, *, env, label, output_path, log, timeout=1800, heartbeat=20, cancel=None):
    """Run one builder; return elapsed seconds or fail with its full log path."""
    if any(source.get(name) for source in (os.environ, env) for name in
           ("MCU_FLASHER_OFFLINE_RUNTIME", "MCU_FLASHER_WORKSPACE_RUNTIME")):
        raise RuntimeError("Builder preparation belongs to bootstrap, outside the workspace process")
    if timeout <= 0 or heartbeat <= 0:
        raise ValueError("Builder timeout and heartbeat must be positive")
    if cancel is not None and cancel.is_set():
        raise InterruptedError("Builder preparation cancelled before launch")
    output_path = Path(os.path.abspath(output_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pending = Queue(maxsize=QUEUE_CHUNKS)
    stopped = threading.Event()
    display = _DisplayRecords(log)
    tail = ""
    process = None
    reader = None
    started = time.monotonic()
    deadline = started + timeout
    next_heartbeat = started + heartbeat
    stream_done = False
    failure = None
    complete = False

    def enqueue(kind, value=""):
        while not stopped.is_set():
            try:
                pending.put((kind, value), timeout=0.1)
                return
            except Full:
                continue

    def read_output():
        try:
            for chunk in output_chunks(process.stdout):
                if stopped.is_set():
                    return
                for offset in range(0, len(chunk), READ_CHARS):
                    enqueue("data", chunk[offset:offset + READ_CHARS])
        except Exception as exc:
            enqueue("error", str(exc)[:TAIL_CHARS])
        finally:
            enqueue("end")

    def record(output, chunk):
        nonlocal tail
        output.write(chunk)
        output.flush()
        tail = (tail + chunk)[-TAIL_CHARS:]
        display.feed(chunk)

    with output_path.open("w", encoding="utf-8", newline="") as output:
        try:
            kwargs = {"env": env, "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                      "stderr": subprocess.STDOUT, "text": True, "encoding": "utf-8",
                      "errors": "replace"}
            if sys.platform == "win32":
                kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(command, **kwargs)
            reader = threading.Thread(target=read_output, name="bootstrap-builder-output", daemon=True)
            reader.start()
            while True:
                now = time.monotonic()
                if cancel is not None and cancel.is_set():
                    failure = "cancelled after another preparation failed"
                    _terminate_tree(process)
                    break
                if stream_done and process.poll() is not None:
                    break
                if now >= deadline:
                    failure = f"timed out after {timeout:g} seconds"
                    _terminate_tree(process)
                    break
                if now >= next_heartbeat:
                    log(f"Still preparing builder {label} — {now - started:.0f}s elapsed")
                    next_heartbeat = time.monotonic() + heartbeat
                wait = min(0.25, max(0.001, deadline - now), max(0.001, next_heartbeat - now))
                try:
                    kind, chunk = pending.get(timeout=wait)
                except Empty:
                    continue
                if kind == "data":
                    record(output, chunk)
                elif kind == "error":
                    failure = "output reader failed: " + chunk
                    _terminate_tree(process)
                    break
                else:
                    stream_done = True
            # The terminated pipe should close promptly. Drain queued diagnostics
            # before stopping the bounded reader; never wait on a silent builder.
            if failure:
                drain_deadline = time.monotonic() + 2
                while not stream_done and time.monotonic() < drain_deadline:
                    try:
                        kind, chunk = pending.get(timeout=0.1)
                    except Empty:
                        continue
                    if kind == "data":
                        record(output, chunk)
                    elif kind == "end":
                        stream_done = True
            display.flush()
            elapsed = time.monotonic() - started
            returncode = process.poll()
            if failure or returncode:
                detail = f"{failure}; " if failure else ""
                raise RuntimeError(f"Builder preparation failed for {label} ({detail}exit code {returncode}).\n"
                                   f"Full builder output: {output_path}\n{tail}")
            log(f"Builder ready: {label} ({elapsed:.1f}s)")
            complete = True
            return elapsed
        except OSError as exc:
            raise RuntimeError(f"Builder preparation failed for {label}: {exc}.\n"
                               f"Full builder output: {output_path}") from exc
        finally:
            if process is not None and not complete:
                _terminate_tree(process)
            stopped.set()
            if reader is not None:
                reader.join(timeout=2)
            if process is not None and (reader is None or not reader.is_alive()):
                process.stdout.close()
