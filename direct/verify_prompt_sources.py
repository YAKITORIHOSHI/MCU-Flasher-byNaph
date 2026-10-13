"""Isolated prompt-source producers; mocked PTYs and no assistant requests."""
from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from main.platforms.ubuntu_pty import PromptObserver
from main.qt.posix_terminal_panel import PtySession
from main.qt import posix_ai_panel
from src.modules.ai_prompt_context import PromptInputTracker
from src.modules import project_terminal

APP = QApplication.instance() or QApplication([])
INPUT = "\x1b[?1;2c\x1b[12;34R\x1b[200~Update Ω😀漢字\nthen header\x1b[201~\r"
PROMPT = "Update Ω😀漢字\nthen header"


class FixtureSocket:
    def __init__(self, *messages):
        self.messages, self.sent = messages, []

    async def __aiter__(self):
        for message in self.messages:
            yield json.dumps(message, ensure_ascii=False)

    async def send(self, data):
        self.sent.append(data)


class FixtureChild(QObject):
    output = Signal(str)
    ended = Signal(str)

    def __init__(self, cwd, argv, parent=None):
        super().__init__(parent)
        self.cwd, self.argv = cwd, argv
        self.write, self.close, self.resize, self.consumed = Mock(), Mock(), Mock(), Mock()

    def start(self):
        self.started_source = self.prompt_source
        return True


class PromptSourceChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/prompt-sources"
        audit.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / ".mcu_flasher_build_cache"
        self.cache.mkdir()

    def payload(self):
        return json.loads((self.cache / "ai_prompt.json").read_text(encoding="utf-8"))

    def test_default_cli_and_explicit_assistant_publish_versioned_literal_context(self):
        for source in ("cli", "assistant"):
            tracker = PromptInputTracker(self.root) if source == "cli" else PromptInputTracker(self.root, source=source)
            tracker.feed(INPUT[:8])
            tracker.feed(INPUT[8:])
            payload = self.payload()
            self.assertEqual(payload["source"], source)
            self.assertEqual(payload["sourceVersion"], 1)
            self.assertEqual(payload["prompt"], PROMPT)
            self.assertTrue(payload["id"])
            previous = (self.cache / "ai_prompt.json").read_bytes()
            tracker.feed("\x1b[?1;2c\x1b[12;34Ryes\r/exit\r")
            tracker.feed("ordinary shell command\r", active=False)
            self.assertEqual((self.cache / "ai_prompt.json").read_bytes(), previous)

    def test_windows_embedded_opencode_uses_assistant_without_changing_pty_input(self):
        # Extract the real server class, excluding native window/launcher setup.
        tree = ast.parse((ROOT / "src/modules/dedicated_AI.py").read_text(encoding="utf-8-sig"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TerminalServer")
        pty = SimpleNamespace(write=Mock(), close=Mock())
        scope = dict(os=os, json=json, time=time, asyncio=asyncio, Path=Path, threading=threading,
                     PtyProcess=SimpleNamespace(spawn=Mock(return_value=pty)))
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "<dedicated-server-fixture>", "exec"), scope)
        server = scope["TerminalServer"](target_dir=str(self.root), command="fixture-opencode")
        socket = FixtureSocket(dict(type="input", data=INPUT))
        with patch.object(threading, "Thread"):
            asyncio.run(server.ws_handler(socket))
        self.assertEqual(pty.write.call_args_list[-1].args, (INPUT,))
        self.assertEqual(pty.write.call_count, 2)  # Supervisor startup and the one original input.
        self.assertEqual(self.payload()["source"], "assistant")
        self.assertEqual(self.payload()["sourceVersion"], 1)
        self.assertEqual(self.payload()["prompt"], PROMPT)

    def terminal(self):
        server = project_terminal.ProjectTerminalServer(8765, str(self.root), str(self.root))
        shell = project_terminal.ShellSession(server, "fixture", "pwsh")
        shell.running, shell.pty = True, SimpleNamespace(pid=17, write=Mock(), close=Mock())
        server.sessions[shell.session_id] = shell
        server.active_shell = shell.session_id
        return server, shell

    def send_terminal(self, server, data):
        socket = FixtureSocket(dict(type="input", shell="fixture", data=data))
        with patch("src.modules.ai_prompt_context.assistant_process_active", return_value=True):
            asyncio.run(server.websocket_handler(socket))

    def test_windows_project_terminal_cli_restart_discards_partial_old_prompt(self):
        server, shell = self.terminal()
        self.send_terminal(server, "Old unfinished prompt")
        old_pty = shell.pty
        with patch.object(project_terminal.threading, "Thread"):
            self.assertTrue(server.control(dict(action="restart", shell="fixture"))["success"])
        old_pty.close.assert_called_once_with(force=True)
        self.assertFalse(shell._assistant_input_active)
        shell.pty = SimpleNamespace(pid=18, write=Mock(), close=Mock())
        self.send_terminal(server, INPUT)
        shell.pty.write.assert_called_once_with(INPUT)
        self.assertEqual(self.payload()["source"], "cli")
        self.assertEqual(self.payload()["sourceVersion"], 1)
        self.assertEqual(self.payload()["prompt"], PROMPT)
        server._stop_session("fixture")

    def test_terminal_clear_preserves_partial_prompt_and_sends_no_command(self):
        server, shell = self.terminal()
        self.send_terminal(server, "Update the ")
        self.assertTrue(server.control(dict(action="clear", shell="fixture"))["success"])
        self.send_terminal(server, "sensor\r")
        self.assertEqual([call.args[0] for call in shell.pty.write.call_args_list], ["Update the ", "sensor\r"])
        self.assertEqual(self.payload()["source"], "cli")
        self.assertEqual(self.payload()["prompt"], "Update the sensor")
        server._stop_session("fixture")

    def test_ubuntu_observer_retains_source_after_overflow_reset_and_closes(self):
        for source in ("cli", "assistant"):
            observer = PromptObserver(self.root, active=True, source=source)
            self.addCleanup(observer.close)
            observer._tracker.feed("Old unfinished prompt")
            observer.feed("x" * (1024 * 1024 + 1), 17)
            finished = threading.Event()
            original = PromptInputTracker._publish

            def publish(tracker, prompt):
                original(tracker, prompt)
                finished.set()

            with patch.object(PromptInputTracker, "_publish", publish), \
                    patch("src.modules.ai_prompt_context.assistant_process_active", side_effect=AssertionError("Explicit active context needs no scan")):
                observer.feed(INPUT, 17)
                self.assertTrue(finished.wait(2), "Observation did not publish after overflow")
                payload = self.payload()
                self.assertEqual(payload["source"], source)
                self.assertEqual(payload["sourceVersion"], 1)
                self.assertEqual(payload["prompt"], PROMPT)
                observer.close()
                observer._worker.join(2)
                self.assertFalse(observer._worker.is_alive())
                before = (self.cache / "ai_prompt.json").read_bytes()
                observer.feed("Do not replay after close\r", 17)
                self.assertEqual((self.cache / "ai_prompt.json").read_bytes(), before)

    def test_ubuntu_session_forwards_source_and_preserves_partial_unicode_bytes(self):
        for source in ("cli", "assistant"):
            session = PtySession(str(self.root), [])
            self.assertEqual(session.prompt_source, "cli")
            session.prompt_source, session.assistant_active = source, True
            session.process = SimpleNamespace(fd=123, pid=17, close=Mock())
            transmitted, calls = bytearray(), []
            observer = Mock()

            def write(fd, data):
                calls.append(fd)
                if len(calls) == 2:
                    raise BlockingIOError("Input backpressure")
                count = min(7, len(data))
                transmitted.extend(data[:count])
                return count

            with patch("main.qt.posix_terminal_panel.PromptObserver", return_value=observer) as create, \
                    patch("main.qt.posix_terminal_panel.os.write", side_effect=write), \
                    patch("main.qt.posix_terminal_panel.close_pty_tree"):
                session.write(INPUT)
                for _ in range(50):
                    session._flush_input()
                    if not session._input_bytes:
                        break
                self.assertEqual(bytes(transmitted), INPUT.encode("utf-8"))
                create.assert_called_once_with(str(self.root), active=True, source=source)
                observer.feed.assert_called_once_with(INPUT, 17)
                session.close()
                observer.close.assert_called_once()

    def test_ubuntu_dedicated_restart_sets_assistant_before_first_input(self):
        with patch.object(posix_ai_panel, "PtySession", FixtureChild):
            session = posix_ai_panel.OpenCodeSession(str(self.root), "/fixture/opencode")
            self.addCleanup(session.close)
            self.assertTrue(session.start())
            generation, child = session._generation, session._child
            self.assertEqual(child.started_source, "assistant")
            session.write(generation, INPUT)
            child.write.assert_called_once_with(INPUT)
            session.write(generation, "/exit\r")
            child.ended.emit("Fixture OpenCode exited")
            session._restart_timer.stop()
            self.assertTrue(session._spawn())
            replacement = session._child
            self.assertEqual(replacement.started_source, "assistant")
            replacement.write.assert_not_called()
            session.write(generation, "Stale generation must not replay\r")
            replacement.write.assert_not_called()
            session.write(session._generation, INPUT)
            replacement.write.assert_called_once_with(INPUT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
