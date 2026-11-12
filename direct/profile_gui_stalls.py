#!/usr/bin/env python3
"""Measure GUI-thread stalls in the real workspace (fixture project, mocked hardware).

Run with the private runtime and no display, e.g.
  env -u DISPLAY -u WAYLAND_DISPLAY QT_QPA_PLATFORM=offscreen .venv-linux/bin/python -B direct/profile_gui_stalls.py
Set STALL_SLOW_IO=0.06 to delay every fsync/replace by 60 ms (an HDD, USB stick or a
sync client's sharing lock). Reports go to temp/audit/gui-stalls/. This is a diagnostic,
not a pass/fail verifier: compare its per-phase totals before and after a change.

A 10 ms QTimer heartbeat runs on the GUI thread; a sampler thread records the
GUI thread's Python stack whenever the heartbeat is late, so every stall comes
with the code that caused it. Persistence, hardware, cloud and compiler
dispatch stay mocked through the Ghost workflow fixture; the AI review manager,
watcher and notification history are real and write only inside the fixture.
"""
from __future__ import annotations

import collections
import contextlib
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "direct"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import verify_ghost_workflow as vgw  # noqa: E402  (applies import-time store guards)

REAL_AI = ("main.core.ai_review.AIReviewManager", "main.core.ai_review.AIEditWatcher",
           "src.dbs.dbs_create.add_notification", "src.dbs.dbs_create.queue_notification")
_real_patch = vgw.patch


def _filtered_patch(target, *args, **kwargs):
    if target in REAL_AI:
        return contextlib.nullcontext()
    return _real_patch(target, *args, **kwargs)


_filtered_patch.object = _real_patch.object
_filtered_patch.dict = _real_patch.dict
vgw.patch = _filtered_patch

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from main.qt.signals import signals  # noqa: E402
from src.dbs import dbs_create  # noqa: E402

SLOW_IO = float(os.environ.get("STALL_SLOW_IO", "0") or 0)


class StallSampler:
    def __init__(self, threshold=0.05):
        self.threshold = threshold
        self.main_id = threading.main_thread().ident
        self.last = time.perf_counter()
        self.phase = "setup"
        self.stalls = []
        self._current = None
        self._stop = False
        self.timer = QTimer()
        self.timer.setInterval(10)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="StallSampler")
        self.thread.start()

    def _tick(self):
        now = time.perf_counter()
        current = self._current
        if current is not None:
            current["duration"] = now - current["start"]
            if current["duration"] >= self.threshold:
                self.stalls.append(current)
            self._current = None
        self.last = now

    def _loop(self):
        while not self._stop:
            time.sleep(0.004)
            now = time.perf_counter()
            if now - self.last < 0.03:
                continue
            frame = sys._current_frames().get(self.main_id)
            if frame is None:
                continue
            stack = tuple(f"{Path(fs.filename).name}:{fs.lineno}:{fs.name}"
                          for fs in traceback.extract_stack(frame)[-28:])
            if self._current is None:
                self._current = {"phase": self.phase, "start": self.last, "samples": collections.Counter()}
            self._current["samples"][stack] += 1

    def stop(self):
        self._stop = True
        self.timer.stop()


HARNESS = ("profile_gui_stalls.py", "case.py", "unittest", "verify_ghost_workflow.py")


def app_frames(stack):
    return [frame for frame in stack if not frame.startswith(HARNESS) and "qWait" not in frame]


def summarize(sampler, out):
    by_phase = collections.defaultdict(list)
    for stall in sampler.stalls:
        by_phase[stall["phase"]].append(stall)
    report = {}
    lines = []
    for phase, stalls in by_phase.items():
        total = sum(s["duration"] for s in stalls)
        worst = max(s["duration"] for s in stalls)
        lines.append(f"\n=== {phase}: {len(stalls)} stalls >= {sampler.threshold*1000:.0f} ms, "
                     f"total {total*1000:.0f} ms, worst {worst*1000:.0f} ms")
        culprits = collections.Counter()
        for stall in stalls:
            for stack, count in stall["samples"].items():
                frames = app_frames(stack)
                key = " <- ".join(reversed(frames[-6:])) if frames else "(harness/Qt event loop)"
                culprits[key] += count * 4
        for key, ms in culprits.most_common(8):
            lines.append(f"  ~{ms:5d} ms  {key}")
        report[phase] = {"count": len(stalls), "total_ms": round(total * 1000),
                         "worst_ms": round(worst * 1000),
                         "culprits": [[k, v] for k, v in culprits.most_common(12)]}
    text = "\n".join(lines)
    print(text)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def slow_io():
    """Optionally emulate a slow disk / sync root by delaying fsync and replace."""
    if not SLOW_IO:
        return contextlib.nullcontext()
    stack = contextlib.ExitStack()
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        time.sleep(SLOW_IO)
        return real_fsync(fd)

    def replace(a, b, *args, **kwargs):
        time.sleep(SLOW_IO)
        return real_replace(a, b, *args, **kwargs)

    stack.enter_context(_real_patch.object(os, "fsync", fsync))
    stack.enter_context(_real_patch.object(os, "replace", replace))
    return stack


def make_project(root: Path) -> Path:
    project = root / "StallSketch"
    project.mkdir()
    (project / "StallSketch.ino").write_text(
        "#include \"lib.h\"\nvoid setup() {\n  Serial.begin(115200);\n}\n\nvoid loop() {\n"
        + "".join(f"  value_{i} = compute({i});\n" for i in range(300)) + "}\n", encoding="utf-8")
    (project / "lib.h").write_text("#pragma once\nint compute(int);\n", encoding="utf-8")
    (project / "lib.cpp").write_text("".join(
        f"int helper_{i}(int x) {{\n  return x * {i} + {i % 7};\n}}\n\n" for i in range(900)), encoding="utf-8")
    (project / "NOTE.txt").write_text("notes\n", encoding="utf-8")
    return project


def seed_history(project: Path):
    """A mature project: 200 settled AI Changes cards and 500 notifications."""
    from main.core.ai_review import AIReviewManager
    manager = AIReviewManager(project)
    target = project / "lib.cpp"
    base = target.read_text(encoding="utf-8")
    for index in range(200):
        after = base.replace("return x *", f"return x /* rev {index} */ *", 40 + index % 30)
        manager.queue_ai_edit_snapshot(str(target), base, after,
                                       prompt_context={"id": f"seed-{index // 4}", "prompt": f"Refactor {index}",
                                                       "source": "cli", "sourceVersion": 1})
        manager.accept_ai_edit(str(target))
    manager.shutdown()
    records = [{"id": f"n{i}", "timestamp": "2026-11-11T00:00:00", "date": "2026-11-11", "time": "00:00:00",
                "category": "build", "level": "info", "title": f"Build {i}",
                "message": "Compiled firmware successfully. " * 6, "details": {}} for i in range(500)]
    database = project / ".mcu_flasher_build_cache" / "dbs_notif.json"
    database.parent.mkdir(exist_ok=True)
    database.write_text(json.dumps(records, indent=2), encoding="utf-8")
    return database


class Profile(vgw.GhostWorkflowChecks):
    def test_profile(self):
        project = make_project(self.root)
        database = seed_history(project)
        self.stack.enter_context(_real_patch.object(dbs_create, "_CURRENT_DB_PATH", str(database)))
        self.stack.enter_context(_real_patch.object(dbs_create, "_FALLBACK_DB_PATH", str(database)))
        sampler = StallSampler()
        self.addCleanup(sampler.stop)
        self.stack.enter_context(slow_io())

        def phase(name, seconds=0.0):
            sampler.phase = name
            if seconds:
                QTest.qWait(int(seconds * 1000))

        phase("open-project")
        api = self.backend(project)
        phase("workspace-startup")
        window, page = self.workspace(api)
        phase("idle-after-startup", 2.0)

        stop = threading.Event()

        def agent(files, rounds, delay):
            for round_index in range(rounds):
                if stop.is_set():
                    return
                for name in files:
                    path = project / name
                    text = path.read_text(encoding="utf-8")
                    path.write_text(text + f"// agent edit {round_index}\n", encoding="utf-8")
                time.sleep(delay)

        phase("external-ai-edits")
        worker = threading.Thread(target=agent, args=(["lib.cpp", "StallSketch.ino", "lib.h"], 6, 0.8))
        worker.start()
        while worker.is_alive():
            QTest.qWait(50)
        phase("external-ai-edits", 2.0)

        phase("ai-changes-tab")
        tabs = window._bottom_tabs
        tabs.setCurrentWidget(window._ai_changes_panel)
        QTest.qWait(500)
        worker = threading.Thread(target=agent, args=(["lib.cpp"], 4, 0.6))
        worker.start()
        while worker.is_alive():
            QTest.qWait(50)
        phase("ai-changes-tab", 1.5)

        phase("accept-reviews")
        bridge = window._editor_panel._bridge if hasattr(window._editor_panel, "_bridge") else None
        manager = api.ai_review_manager
        for review in list(manager.get_ai_edit_reviews()):
            if bridge is not None:
                bridge.accept_ai_edit(review["path"], review["revision"])
            else:
                manager.accept_ai_edit(review["path"], review["revision"])
            QTest.qWait(150)
        phase("accept-reviews", 1.0)

        phase("tool-tab-cycle")
        for _ in range(2):
            for index in range(tabs.count()):
                tabs.setCurrentIndex(index)
                QTest.qWait(120)
        tabs.setCurrentIndex(0)
        phase("tool-tab-cycle", 0.5)

        phase("notifications-burst")
        for index in range(40):
            api.emit("notification", {"title": "Port", "message": f"Device event {index}", "type": "info"})
            QTest.qWait(25)
        phase("notifications-burst", 1.0)

        phase("console-flood")

        def flood(event, count):
            for index in range(count):
                api.emit(event, {"text": f"Compiling .pio/build/mcu_env/src/file_{index}.cpp.o "
                                          "-DARDUINO=10812 -DESP32 -Os -ffunction-sections", "tag": "info",
                                 "newline": True})
                if index % 200 == 0:
                    time.sleep(0.005)

        worker = threading.Thread(target=flood, args=("console:log", 15000))
        worker.start()
        while worker.is_alive():
            QTest.qWait(50)
        phase("console-flood", 1.5)

        phase("serial-flood")
        tabs.setCurrentWidget(window._serial_panel)
        worker = threading.Thread(target=flood, args=("serial:log", 15000))
        worker.start()
        while worker.is_alive():
            QTest.qWait(50)
        phase("serial-flood", 1.5)

        phase("compile-action")
        from main.core.board_catalog import SUPPORTED_BOARDS
        catalog = {f"Fixture board {i}": {"platform": "espressif32", "board": f"fixture{i}", "framework": "arduino",
                                         "frameworks": ["arduino"], "pio_resolved": True, "mcu": "esp32"}
                   for i in range(3000)}
        SUPPORTED_BOARDS.replace(catalog)
        api.current_board = "Fixture board 1"
        signals.board_selected.emit({"board_name": api.current_board})
        QTest.qWait(300)

        def fake_compile():
            api.emit("operation:phase", {"phase": "compiling", "is_busy": True, "can_stop": True, "op": "compile"})
            for index in range(4000):
                api.emit("console:log", {"text": f"Compiling .pio/build/mcu_env/lib/file_{index}.cpp.o", "tag": "info",
                                         "newline": True})
                if index % 100 == 0:
                    api.emit("console:progress", {"key": "build", "text": f"Building {index // 40}%"})
                    time.sleep(0.01)
            api.emit("boards:updated", {"boards": dict(catalog)})
            api.emit("notification", {"title": "Compile complete", "message": "Firmware built.", "type": "success"})
            api._release_requested_operation()

        api._compile_requested_worker = fake_compile
        window._primary_toolbar._update_action_button_states()
        started = []
        original = fake_compile

        def tracked():
            started.append(True)
            original()

        api._compile_requested_worker = tracked
        for review in list(api.ai_review_manager.get_ai_edit_reviews()):
            api.ai_review_manager.accept_ai_edit(review["path"], review["revision"])
        QTest.qWait(200)
        window._primary_toolbar._do_compile()
        self.until(lambda: bool(started), "compile started", 15)
        self.until(lambda: not api.is_busy and not api._operation_worker_alive(), "compile finished", 60)
        phase("compile-action", 1.5)

        phase("theme-changes")
        for theme in ("dark", "solarized", "default"):
            signals.theme_changed.emit(theme)
            QTest.qWait(700)

        phase("save-all")
        done = []
        window._editor_panel.trigger_save_all(callback=lambda *a: done.append(True))
        self.until(lambda: bool(done), "save acknowledgement")
        phase("final-idle", 1.5)
        stop.set()
        sampler.stop()
        out = vgw.CAPTURES.parent / "gui-stalls" / ("report-slow-io.json" if SLOW_IO else "report.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        summarize(sampler, out)


if __name__ == "__main__":
    import unittest
    unittest.main(argv=[sys.argv[0], "Profile.test_profile", "-v"])
