#!/usr/bin/env python3
"""Isolated dedicated OpenCode UI/PTY lifecycle; never launch an AI service."""
from __future__ import annotations
import json
import os
import shlex
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget, QPushButton, QTabWidget
from main.qt import posix_ai_panel as ai
from main.qt.garbage_collection import install_gui_garbage_collector

APP = QApplication.instance() or QApplication([])
COLLECTOR = install_gui_garbage_collector(APP)
PANELS = []


class FakeView(QWidget):
    loadFinished = Signal(bool)
    renderProcessTerminated = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page = Mock()
        self.setUrl = Mock()
        self.setContextMenuPolicy = Mock()

    def page(self):
        return self._page


class FakePty(QObject):
    output = Signal(str)
    ended = Signal(str)
    instances = []

    def __init__(self, cwd, argv, parent=None):
        super().__init__(parent)
        self.cwd, self.argv = cwd, argv
        self.start = Mock(return_value=True)
        self.close, self.write, self.resize, self.consumed = Mock(), Mock(), Mock(), Mock()
        self.instances.append(self)


class DedicatedAssistantChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        audit = ROOT / "temp/audit/ubuntu-ai"
        audit.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=audit)))
        FakePty.instances.clear()
        for name, value in (("PtySession", FakePty), ("QWebEngineView", FakeView),
                            ("QWebChannel", lambda *_: SimpleNamespace(registerObject=Mock())),
                            ("prepare_context", Mock(return_value=True)),
                            ("find_opencode_cli", Mock(return_value="/fixture/opencode"))):
            self.stack.enter_context(patch.object(ai, name, value))
        self.stack.enter_context(patch("main.core.config.get_monitor_font_size", return_value=12))

    def panel(self):
        panel = ai.PosixAIPanel(SimpleNamespace(sketch_dir_path=self.root))
        PANELS.append(panel)
        self.addCleanup(lambda: (panel._stop_ai(), panel.close()))
        return panel

    def wait_for(self, condition):
        until = time.monotonic() + 2
        while time.monotonic() < until:
            APP.processEvents()
            if condition():
                return
            time.sleep(0.005)
        self.fail("Fixture condition did not become ready")

    def started_panel(self):
        panel = self.panel()
        panel.show()
        panel.ensure_started()
        self.wait_for(lambda: panel._session is not None)
        self.assertTrue(panel._session.start())
        return panel

    def session(self):
        session = ai.OpenCodeSession(str(self.root), "/fixture/opencode")
        self.addCleanup(session.close)
        self.assertTrue(session.start())
        return session

    def test_dedicated_view_has_hide_only_and_cannot_close_independently(self):
        panel = self.panel()
        panel.show()
        buttons = [button.text() for button in panel.findChildren(QPushButton)]
        for forbidden in ("New Bash", "Clear", "End session"):
            self.assertNotIn(forbidden, buttons)
        self.assertEqual(panel.findChildren(QTabWidget), [])
        self.assertFalse(hasattr(panel, "add_session"))
        self.assertEqual(panel._hide_button.accessibleName(), "Hide AI Assistant")
        self.assertFalse(panel._hide_button.icon().isNull())
        self.assertFalse(panel.close())
        self.assertTrue(panel.isVisible())
        panel._hide_button.click()
        self.assertFalse(panel.isVisible())

    def test_start_is_explicit_and_hide_reveal_preserves_process_view_and_input(self):
        panel = self.panel()
        panel.resize(360, 500)
        panel.apply_theme("light")
        self.assertEqual(FakePty.instances, [])
        panel.show()
        panel.ensure_started()
        self.wait_for(lambda: panel._session is not None)
        session, view = panel._session, panel._view
        session.start()
        child = session._child
        session.write(session._generation, "user input")
        panel.hide()
        panel.show()
        panel.ensure_started()
        self.assertIs(panel._view, view)
        self.assertIs(panel._session, session)
        self.assertEqual(len(FakePty.instances), 1)
        child.close.assert_not_called()
        child.write.assert_called_once_with("user input")

    def test_only_changed_project_realigns_and_stale_discovery_is_discarded(self):
        panel = self.started_panel()
        child = panel._session._child
        view = panel._view
        panel.reset_for_project(str(self.root))
        self.assertIs(panel._view, view)
        child.close.assert_not_called()
        old_generation = panel._launch_generation
        destination = self.root / "next project"
        destination.mkdir()
        panel.reset_for_project(str(destination))
        child.close.assert_called_once()
        panel._discovery_finished(old_generation, "/stale/opencode", json.dumps([str(self.root), ""]))
        self.wait_for(lambda: panel._session is not None)
        self.assertEqual(panel._session.cwd, str(destination))
        self.assertEqual(panel._session.executable, "/fixture/opencode")

    def test_missing_opencode_shows_wrapped_error_without_starting_bash(self):
        ai.find_opencode_cli.return_value = None
        panel = self.panel()
        panel.ensure_started()
        self.wait_for(lambda: panel._state == "error")
        self.assertIsNone(panel._view)
        self.assertEqual(FakePty.instances, [])
        self.assertIn("Ubuntu Bootstrap repair", panel._load_sub.text())
        self.assertIn("bash direct/ubuntu/run.sh --repair", panel._load_sub.text())
        self.assertTrue(panel._load_sub.wordWrap())

    def test_missing_local_assets_fail_before_creating_a_process_or_view(self):
        panel = self.panel()
        with patch.object(ai, "ROOT", self.root):
            panel.ensure_started()
            self.wait_for(lambda: panel._state == "error")
        self.assertIn("assets are missing", panel._load_sub.text())
        self.assertIsNone(panel._view)
        self.assertEqual(FakePty.instances, [])

    def test_failed_context_preparation_cannot_start_or_attach_an_assistant(self):
        ai.prepare_context.side_effect = RuntimeError("Project connection state could not be saved")
        panel = self.panel()
        panel.ensure_started()
        self.wait_for(lambda: panel._state == "error")
        self.assertIn("could not be saved", panel._load_sub.text())
        self.assertIsNone(panel._view)
        self.assertEqual(FakePty.instances, [])

    def test_renderer_failure_keeps_container_and_requires_fresh_retry(self):
        panel = self.started_panel()
        session, view, child = panel._session, panel._view, panel._session._child
        view.renderProcessTerminated.emit(1, 9)
        self.assertEqual(panel._state, "error")
        child.close.assert_called_once()
        self.assertTrue(session._closed)
        self.assertIs(panel._view, view)
        self.assertFalse(panel.close())
        panel._retry()
        self.wait_for(lambda: panel._session is not None)
        self.assertIsNot(panel._view, view)
        self.assertIsNot(panel._session, session)

    def test_project_change_during_discovery_cannot_attach_the_old_project(self):
        panel = self.panel()
        entered, release = threading.Event(), threading.Event()
        original = str(self.root)
        destination = self.root / "new project"
        destination.mkdir()

        def find():
            entered.set()
            release.wait(2)
            return "/fixture/opencode"

        ai.find_opencode_cli.side_effect = find
        panel.ensure_started()
        self.wait_for(entered.is_set)
        panel._backend.sketch_dir_path = str(destination)
        panel.reset_for_project(str(destination))
        release.set()
        self.wait_for(lambda: panel._session is not None)
        self.assertEqual(panel._session.cwd, str(destination))
        self.assertNotEqual(panel._session.cwd, original)

    def test_readiness_requires_real_output_and_theme_font_preserve_session(self):
        panel = self.started_panel()
        session, view = panel._session, panel._view
        panel._spawn_time -= 10
        panel._last_output -= 10
        panel._check_ready()
        self.assertEqual(panel._state, "loading")
        session._child.output.emit("OpenCode interface " * 50)
        panel._last_output -= 1
        panel._check_ready()
        self.assertEqual(panel._state, "loading", "Output must be rendered before reveal")
        session.consumed(session._generation, 500)
        panel._check_ready()
        self.assertEqual(panel._state, "active")
        self.assertIs(panel._stack.currentWidget(), view)
        for theme in ("light", "solarized_dark", "default"):
            panel.apply_theme(theme)
            panel.set_font_size(16)
        self.assertIs(panel._session, session)
        self.assertIs(panel._view, view)
        self.assertEqual(len(FakePty.instances), 1)

    def test_short_rendered_prompt_is_accessible_but_hidden_panel_cannot_take_focus(self):
        panel = self.started_panel()
        panel.hide()
        panel._spawn_time -= 10
        session = panel._session
        session._child.output.emit("Sign in? ")
        session.consumed(session._generation, 9)
        panel._last_output -= 1
        with patch.object(panel._view, "setFocus") as focus:
            panel._check_ready()
            self.assertEqual(panel._state, "active")
            focus.assert_not_called()

    def test_capability_requests_alone_cannot_claim_a_ready_interface(self):
        panel = self.started_panel()
        panel._spawn_time -= 10
        session = panel._session
        session._child.output.emit("\x1b[6n\x1b[c" * 100)
        session.consumed(session._generation, 900)
        panel._last_output -= 1
        panel._check_ready()
        self.assertEqual(panel._state, "loading")

    def test_exit_restarts_a_fresh_pty_with_no_input_replay_and_stale_acks_ignored(self):
        session = self.session()
        first, epoch = session._child, session._generation
        payload = "\x1b[?1;2c\x1b[200~Ω😀漢字\n\x1b[201~\r"
        session.write(epoch, payload)
        first.write.assert_called_once_with(payload)
        session.write(epoch, "/exit\r")
        self.assertTrue(session._intentional_exit)
        first.ended.emit("exited")
        session._restart_timer.stop()
        session._spawn()
        second = session._child
        self.assertIsNot(first, second)
        self.assertEqual(second.argv, ["/fixture/opencode", str(self.root)])
        second.write.assert_not_called()
        session.write(epoch, "stale data")
        session.consumed(epoch, 100)
        second.write.assert_not_called()
        second.consumed.assert_not_called()

    def test_pasted_exit_text_is_not_an_intentional_exit(self):
        session = self.session()
        session.write(session._generation, "\x1b[200~/exit\n\x1b[201~\r")
        self.assertFalse(session._intentional_exit)

    def test_old_unacted_exit_request_does_not_bypass_crash_limit(self):
        session = self.session()
        session.write(session._generation, "/exit\r")
        session._exit_requested_at -= 11
        with patch.object(session._policy, "allow_restart", return_value=False) as policy:
            session._child.ended.emit("crashed later")
        policy.assert_called_once_with(intentional=False)

    def test_unexpected_exits_are_bounded_and_owner_shutdown_cancels_restart(self):
        session = self.session()
        states = []
        session.status.connect(lambda state, message: states.append((state, message)))
        for _ in range(3):
            session._child.output.emit("\x1b[31mConfiguration fixture error\x1b[0m\r\n")
            session._child.ended.emit("crashed")
            if session._restart_timer.isActive():
                session._restart_timer.stop()
                session._spawn()
        self.assertEqual(len(FakePty.instances), 3)
        self.assertIsNone(session._child)
        self.assertEqual(states[-1][0], "error")
        self.assertIn("Configuration fixture error", states[-1][1])
        self.assertNotIn("\x1b", states[-1][1])
        self.assertFalse(session._restart_timer.isActive())
        session.retry()
        current = session._child
        session.close()
        current.close.assert_called_once()
        self.assertFalse(session.start())
        self.assertFalse(session._spawn())

    def test_compact_headers_fit_the_owned_panel_in_each_palette(self):
        panel = self.panel()
        panel.show()
        for width in (240, 340, 500):
            panel.resize(width, 320)
            for theme in ("default", "light", "solarized_dark"):
                panel.apply_theme(theme)
                for state in ("idle", "loading", "active", "error"):
                    panel._set_state(state, "Fixture state")
                    APP.processEvents()
                    self.assertLessEqual(panel.width(), width)
                    for widget in (panel._title, panel._status_badge, panel._hide_button):
                        self.assertTrue(panel._header.rect().contains(widget.geometry()), widget.geometry())


def native_renderer_check(render_dir=None, workspace=False):
    """Use real WebEngine and a native PTY with a local, unauthenticated fixture."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from main.qt.theme import build_stylesheet, register_fonts

    audit = ROOT / "temp/audit/ubuntu-ai"
    audit.mkdir(parents=True, exist_ok=True)
    register_fonts()
    with tempfile.TemporaryDirectory(prefix="native project ", dir=audit) as directory, ExitStack() as patches:
        project = Path(directory)
        program = project / "fixture.py"
        program.write_text(r'''
import json, os, signal, sys, tty
from pathlib import Path
events = Path(__file__).with_name("events.jsonl")
def record(kind, **values):
    with events.open("a", encoding="utf-8") as log:
        log.write(json.dumps(dict(kind=kind, pid=os.getpid(), **values)) + "\n")
record("start", argv=sys.argv[1:], cwd=os.getcwd())
tty.setraw(0)
def draw(*_):
    rows, cols = os.get_terminal_size(0).lines, os.get_terminal_size(0).columns
    frame = "\x1b[?1049h\x1b[2J\x1b[H\x1b[?2004h\x1b[36mOpenCode renderer fixture\x1b[0m\r\n"
    frame += "Native Ubuntu PTY - isolated verification\r\n\r\n"
    frame += "Project: native project fixture\r\n"
    frame += f"View: {cols} columns x {rows} rows\r\n\r\n"
    frame += "Unicode: Ω / 漢字 / 😀\r\n\r\n"
    frame += "Hide keeps this session alive. /exit starts a fresh session.\r\n"
    frame += "No authenticated assistant or hardware is running.\r\n\r\n> "
    os.write(1, frame.encode("utf-8"))
signal.signal(signal.SIGWINCH, draw)
draw()
os.write(1, b"\x1b[6n\x1b[c")
received = b""
while True:
    data = os.read(0, 4096)
    if not data:
        break
    record("input", hex=data.hex())
    received = (received + data)[-65536:]
    if b"/exit\r" in received:
        record("exit")
        os.write(1, b"\x1b[?1049l")
        break
''', encoding="utf-8")
        executable = project / "opencode-fixture"
        executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(program)) + ' "$@"\n', encoding="utf-8")
        executable.chmod(0o700)
        patches.enter_context(patch.object(ai, "find_opencode_cli", return_value=str(executable)))
        patches.enter_context(patch.object(ai, "prepare_context", return_value=True))
        patches.enter_context(patch("main.core.config.get_monitor_font_size", return_value=14))
        patches.enter_context(patch("src.modules.ai_prompt_context.PromptInputTracker._publish"))
        patches.enter_context(patch("src.modules.ai_prompt_context.assistant_process_active", return_value=False))
        owner = None
        if workspace:
            from main.core import config, file_utils
            from main.qt.main_window import MCUMainWindow
            from src.modules.ui_metrics import WorkArea
            from verify_runtime import PreviewBackend
            backend = PreviewBackend()
            backend.sketch_dir_path = project
            backend.active_file_path = str(project / "preview.ino")
            backend.start_services = Mock()
            backend.compile_sketch = Mock()
            backend.upload_sketch = Mock()
            for name, value in (("_load_raw_config", Mock(return_value={"shared": {}, "instances": {}})),
                                ("_save_raw_config", Mock()), ("save_gui_config", Mock()),
                                ("get_theme_mode", Mock(return_value="default")),
                                ("focus_project_window", Mock(return_value=False))):
                patches.enter_context(patch.object(config, name, value))
            patches.enter_context(patch.object(file_utils, "hide_internal_project_metadata"))
            patches.enter_context(patch("src.modules.ubuntu_download_manager.quit_if_last_workspace", return_value=False))
            for name in ("main.qt.main_window.work_area", "main.qt.responsive.work_area"):
                patches.enter_context(patch(name, return_value=WorkArea(0, 0, 1600, 1000)))
            owner = MCUMainWindow(backend)
            panel = owner._ai_panel
        else:
            panel = ai.PosixAIPanel(SimpleNamespace(sketch_dir_path=str(project)))
        PANELS.append(panel)

        def wait_for(condition, label, seconds=15):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                APP.processEvents()
                if condition():
                    return
                time.sleep(.01)
            raise AssertionError(f"Native fixture timed out: {label}; state={panel._state}, detail={panel._load_sub.text()}")

        def events():
            path = project / "events.jsonl"
            if not path.is_file():
                return []
            lines = path.read_text(encoding="utf-8").splitlines()
            result = []
            for line in lines:
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # An in-progress write is not a delivered event yet.
            return result

        def input_bytes(pid):
            return b"".join(bytes.fromhex(event["hex"]) for event in events() if event["kind"] == "input" and event["pid"] == pid)

        try:
            if owner:
                owner.resize(1400, 900)
                owner.show()
                owner.activateWindow()  # The owned virtual display has no window manager.
                owner.toggle_ai_panel(True)
            else:
                panel.resize(760, 520)
                panel.show()
                panel.ensure_started()
            wait_for(lambda: panel._state == "active", "rendered OpenCode fixture")
            session, view = panel._session, panel._view
            first = session._child
            epoch = session._generation
            initial = next(event for event in events() if event["kind"] == "start")
            # Input is recorded by the actual CLI, now a child of the live
            # supervisor that owns this PTY and its detached tools.
            pid = initial["pid"]
            import psutil
            assert pid in {child.pid for child in psutil.Process(first.process.pid).children(recursive=True)}
            assert initial["argv"] == [str(project)] and initial["cwd"] == str(project)
            wait_for(lambda: b"R" in input_bytes(pid) and b"c" in input_bytes(pid), "renderer capability replies")
            wait_for(lambda: first._pending_chars == 0, "output consumption acknowledgements")
            payload = "\x1b[200~Ω😀漢字\n\x1b[201~\r"
            session.write(epoch, payload)
            wait_for(lambda: payload.encode("utf-8") in input_bytes(pid), "unchanged Unicode bracketed paste")
            assert not panel.close() and panel.isVisible()
            panel._hide_button.click()
            assert not panel.isVisible() and first.process.isalive()
            if owner:
                owner.toggle_ai_panel(True)
            else:
                panel.show()
                panel.ensure_started()
            assert panel._session is session and panel._view is view and session._child is first
            if owner:
                panel._focus_view()
                APP.processEvents()
                assert owner._native_cli_shortcuts and all(not shortcut.isEnabled() for shortcut in owner._native_cli_shortcuts)
                for key in (Qt.Key.Key_R, Qt.Key.Key_U, Qt.Key.Key_S, Qt.Key.Key_O):
                    QTest.keyClick(view.focusProxy() or view, key, Qt.KeyboardModifier.ControlModifier)
                wait_for(lambda: b"\x12\x15\x13\x0f" in input_bytes(pid), "CLI owns application Ctrl shortcuts")
                backend.compile_sketch.assert_not_called()
                backend.upload_sketch.assert_not_called()
                editor = owner._editor_panel
                readiness = {"ready": False, "pending": False}
                def ready_result(value):
                    readiness.update(ready=bool(value), pending=False)
                def editor_ready():
                    if not readiness["pending"]:
                        readiness["pending"] = True
                        editor._view.page().runJavaScript("Boolean(window.editorInstance?.getModel())", ready_result)
                    return readiness["ready"]
                wait_for(editor_ready, "offline editor readiness")
                editor.restore_input_focus()
                wait_for(lambda: editor.has_input_focus(), "editor keyboard focus")
                assert all(shortcut.isEnabled() for shortcut in owner._native_cli_shortcuts)
                owner.detach_editor()
                APP.processEvents()
                owner.attach_editor()
                owner.activateWindow()
                wait_for(lambda: editor.has_input_focus(), "editor focus survives detach/reattach with AI open")
                assert panel._view is view and session._child is first
            if render_dir:
                render_dir = Path(render_dir)
                render_dir.mkdir(parents=True, exist_ok=True)
            for theme, width in (("default", 760), ("light", 340), ("solarized_dark", 500)):
                APP.setStyleSheet(build_stylesheet(theme))
                panel.apply_theme(theme)
                panel.set_font_size(14)
                if owner:
                    owner.resize(1400 if width == 760 else 900, 900)
                else:
                    panel.resize(width, 520)
                deadline = time.monotonic() + .5
                while time.monotonic() < deadline:
                    APP.processEvents()
                    time.sleep(.01)
                assert panel._view is view and session._child is first
                assert panel._header.rect().contains(panel._hide_button.geometry())
                if owner:
                    assert owner._editor_panel.has_input_focus(), "Theme/resize stole editor input focus"
                if render_dir:
                    target = owner or panel
                    assert target.grab().save(str(render_dir / f"{'workspace-' if owner else ''}opencode-{theme}.png"))
            panel._focus_view()
            APP.processEvents()
            QTest.keyClicks(view.focusProxy() or view, "/exit")
            QTest.keyClick(view.focusProxy() or view, Qt.Key.Key_Return)
            wait_for(lambda: session._generation > epoch and panel._state == "active", "fresh session after renderer /exit")
            second = session._child
            assert panel._view is view and second is not first and first.process is None
            starts = [event for event in events() if event["kind"] == "start"]
            assert len(starts) == 2 and starts[-1]["argv"] == [str(project)]
            assert payload.encode("utf-8") not in input_bytes(starts[-1]["pid"])
            assert b"/exit" not in input_bytes(starts[-1]["pid"])
            panel._stop_ai()
            assert second.process is None and not session._restart_timer.isActive()
            print("Native WebEngine/PTY: literal project, renderer readiness, capability replies, Unicode/paste, protected close, hide/reveal, three palettes, fresh /exit and shutdown: OK")
        finally:
            panel._stop_ai()
            panel.close()
            if owner:
                owner.close()
            APP.processEvents()


if __name__ == "__main__":
    if "--native-renderer" in sys.argv or "--workspace-renderer" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--native-renderer", action="store_true")
        parser.add_argument("--workspace-renderer", action="store_true")
        parser.add_argument("--render-dir", type=Path)
        args = parser.parse_args()
        native_renderer_check(args.render_dir, workspace=args.workspace_renderer)
    else:
        unittest.main(verbosity=2)
