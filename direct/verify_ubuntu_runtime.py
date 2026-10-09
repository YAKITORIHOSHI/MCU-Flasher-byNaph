#!/usr/bin/env python3
"""Isolated native Ubuntu pickers and PTY input checks; no hardware or setup."""
from __future__ import annotations

import ast
import errno
import hashlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog, QWidget
from main.qt.posix_terminal_panel import PtySession
from main.qt.garbage_collection import install_gui_garbage_collector

APP = QApplication.instance() or QApplication([])
COLLECTOR = install_gui_garbage_collector(APP)


class UbuntuRuntimeChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp" / "audit" / "ubuntu-runtime"
        audit.mkdir(parents=True, exist_ok=True)
        self.fixture = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        tracker = patch("src.modules.ai_prompt_context.PromptInputTracker", return_value=Mock())
        tracker.start()
        self.addCleanup(tracker.stop)
        active = patch("src.modules.ai_prompt_context.assistant_process_active", return_value=False)
        active.start()
        self.addCleanup(active.stop)

    def session(self):
        session = PtySession(str(self.root), [])
        session.process = SimpleNamespace(fd=123, pid=0, close=Mock())
        self.addCleanup(session.close)
        return session

    def picker_api(self):
        tree = ast.parse((ROOT / "main" / "web_bridge.py").read_text(encoding="utf-8"))
        cls = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "MCUWebBackendAPI")
        functions = [item for item in cls.body if isinstance(item, ast.FunctionDef)
                     and item.name in {"open_project_picker", "open_file_picker"}]
        scope = {"sys": SimpleNamespace(platform="linux"), "Path": Path,
                 "is_application_codebase_dir": lambda value: Path(value).resolve() == ROOT,
                 "subprocess": SimpleNamespace(run=Mock(side_effect=AssertionError("Linux picker must not run PowerShell")))}
        exec(compile(ast.Module(body=functions, type_ignores=[]), "<native-pickers>", "exec"), scope)
        api = SimpleNamespace(_window=None, sketch_dir_path=self.root, is_busy=False,
                              active_operation=None, _current_op_phase=None)
        return api, scope

    def test_native_project_picker_accepts_owned_directory_and_preserves_busy_guard(self):
        api, scope = self.picker_api()
        api._window = QWidget()
        self.addCleanup(api._window.close)
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(self.root)) as dialog:
            self.assertEqual(scope["open_project_picker"](api), str(self.root))
            self.assertIs(dialog.call_args.args[0], api._window)
            api.is_busy = True
            self.assertEqual(scope["open_project_picker"](api), "")
            self.assertEqual(dialog.call_count, 1)

    def test_native_pickers_handle_cancel_missing_files_and_application_directory(self):
        api, scope = self.picker_api()
        for value in ("", str(self.root / "absent"), str(ROOT)):
            with patch.object(QFileDialog, "getExistingDirectory", return_value=value):
                self.assertEqual(scope["open_project_picker"](api), "")
        source = self.root / "main sketch.ino"
        source.write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
        for value, expected in ((str(source), str(source)), ("", ""),
                                (str(self.root / "absent.ino"), ""), (str(ROOT / "mcu_flash_gui.py"), "")):
            with patch.object(QFileDialog, "getOpenFileName", return_value=(value, "")):
                self.assertEqual(scope["open_file_picker"](api), expected)

    def test_partial_writes_preserve_unicode_capability_replies_and_bracketed_paste_order(self):
        session = self.session()
        transmitted = bytearray()
        calls = [0]

        def write(fd, data):
            self.assertEqual(fd, 123)
            calls[0] += 1
            if calls[0] in (2, 4):
                raise BlockingIOError(errno.EAGAIN, "PTY is full")
            count = min(7, len(data))
            transmitted.extend(data[:count])
            return count

        first = "\x1b[?1;2c\x1b[200~Unicode: Ω😀漢字\n"
        second = "more paste\x1b[201~\r"
        with patch("main.qt.posix_terminal_panel.os.write", side_effect=write):
            session.write(first)
            session.write(second)
            for _ in range(50):
                session._flush_input()
                if not session._input_bytes:
                    break
        self.assertEqual(bytes(transmitted), (first + second).encode("utf-8"))
        self.assertEqual(session._input_bytes, 0)
        self.assertFalse(session._input_chunks)
        self.assertEqual(session._input_offset, 0)

    def test_output_backpressure_does_not_stop_queued_input(self):
        session = self.session()
        with patch("main.qt.posix_terminal_panel.os.write", side_effect=BlockingIOError(errno.EAGAIN, "full")):
            session.write("\x1b[12;34R")
        session._pending_chars = 128_000
        with patch("main.qt.posix_terminal_panel.os.write", side_effect=lambda fd, data: len(data)) as write:
            session._drain()
        write.assert_called_once()
        self.assertEqual(session._input_bytes, 0)

    def test_full_input_queue_reports_rejection_without_overwriting_pending_bytes(self):
        session = self.session()
        output = []
        session.output.connect(output.append)
        with patch("main.qt.posix_terminal_panel.os.write", side_effect=BlockingIOError(errno.EAGAIN, "full")):
            session.write("x" * (4 * 1024 * 1024))
            session.write("not accepted")
            session.write("still not accepted")
        self.assertEqual(session._input_bytes, 4 * 1024 * 1024)
        self.assertEqual(len(session._input_chunks), 1)
        self.assertEqual(len(output), 1)
        self.assertIn("input was not sent", output[0])
        session.close()
        self.assertEqual(session._input_bytes, 0)
        self.assertFalse(session._input_chunks)

    def test_closed_pty_discards_input_without_restart_or_replay(self):
        session = self.session()
        ended = []
        session.ended.connect(ended.append)
        with patch("main.qt.posix_terminal_panel.os.write", side_effect=OSError(errno.EIO, "ended")) as write:
            session.write("some input")
            session.write("later input")
        self.assertEqual(write.call_count, 1)
        self.assertTrue(session._closed)
        self.assertEqual(session._input_bytes, 0)
        self.assertEqual(len(ended), 1)

    def test_real_native_pty_receives_large_paste_exactly(self):
        payload = "\x1b[?1;2c\x1b[200~" + "Ω😀漢字\n" * 20000 + "\x1b[201~\r"
        expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        script = (
            "import hashlib, os, tty; tty.setraw(0); os.write(1, b'READY\\n'); "
            f"remaining = {len(payload.encode('utf-8'))}; digest = hashlib.sha256()\n"
            "while remaining:\n"
            " data = os.read(0, min(4096, remaining)); digest.update(data); remaining -= len(data)\n"
            "os.write(1, ('DIGEST:' + digest.hexdigest() + '\\n').encode())\n"
        )
        session = PtySession(str(self.root), [sys.executable, "-I", "-u", "-c", script])
        self.addCleanup(session.close)
        output = []

        def accept(data):
            output.append(data)
            session.consumed(len(data.encode("utf-16-le")) // 2)

        session.output.connect(accept)
        self.assertTrue(session.start())
        self.assertFalse(os.get_blocking(session.process.fd))

        def wait_for(text):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                APP.processEvents()
                if text in "".join(output):
                    return
                time.sleep(0.002)
            self.fail("PTY did not produce expected output: " + repr("".join(output)[-1000:]))

        wait_for("READY\n")
        session.write(payload)
        wait_for("DIGEST:" + expected)
        self.assertEqual(session._input_bytes, 0)


if __name__ == "__main__":
    if not sys.platform.startswith("linux"):
        raise SystemExit("Use the native Ubuntu private runtime for this verifier.")
    unittest.main(verbosity=2)
