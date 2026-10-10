#!/usr/bin/env python3
"""Isolated Find All, Modify sizing and real offline Monaco Enter checks."""
from __future__ import annotations

import argparse
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
from PySide6.QtCore import QUrl, Qt, QTimer, QPoint, QRect
from PySide6.QtWidgets import QApplication, QScrollArea
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtTest import QTest
from main.qt.theme import register_fonts
from main.qt import project_search
from main.qt.project_search import ProjectSearchDialog, search_project
from main.qt.modify_dialog import ModifyFilesDialog

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
register_fonts()
CAPTURES = None


def until(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Timed out waiting for isolated fixture")


class SearchChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "temp/audit").mkdir(parents=True, exist_ok=True)
        self.fixture = tempfile.TemporaryDirectory(prefix="project-search-", dir=ROOT / "temp/audit")
        self.addCleanup(self.fixture.cleanup)
        self.folder = Path(self.fixture.name)
        self.project = self.folder / "MotorSketch"
        self.project.mkdir()
        self.main = self.project / "MotorSketch.ino"
        self.main.write_text("void setup() {}\nMotor motor motorized\n😀motor\n", encoding="utf-8")
        (self.project / "MotorSketch.h").write_text("#pragma once\nMotor\n", encoding="utf-8")
        self.theme = patch("main.core.config.get_theme_mode", return_value="default")
        self.theme.start()
        self.addCleanup(self.theme.stop)

    def test_case_whole_word_literal_locations_and_dirty_buffers(self):
        matches = search_project(self.project, "motor")["matches"]
        self.assertEqual(len(matches), 5)
        self.assertEqual(len(search_project(self.project, "motor", case_sensitive=True)["matches"]), 3)
        self.assertEqual(len(search_project(self.project, "motor", whole_word=True)["matches"]), 4)
        self.assertEqual(len(search_project(self.project, "setup()", case_sensitive=True)["matches"]), 1)
        self.assertEqual([(m["line"], m["column"]) for m in matches if m["line"] == 3], [(3, 3)])
        result = search_project(self.project, "unsaved", buffers={str(self.main): "\nunsaved\n"})
        self.assertEqual(result["matches"][0]["line"], 2)
        self.assertTrue(result["matches"][0]["unsaved"])
        self.assertFalse(search_project(self.project, "motor", buffers={str(self.main): ""}, case_sensitive=True)["matches"])
        self.main.unlink()
        self.assertEqual(len(search_project(self.project, "lost", buffers={str(self.main): "lost"})["matches"]), 1)

    def test_root_boundary_hidden_nested_and_external_buffers(self):
        cache = self.project / ".mcu_flasher_build_cache"
        cache.mkdir()
        (cache / "hidden.ino").write_text("needle")
        nested = self.project / "nested"
        nested.mkdir()
        (nested / "nested.cpp").write_text("needle")
        outside = self.folder / "outside.cpp"
        outside.write_text("needle")
        (self.project / ".hidden.h").write_text("needle")
        (self.project / "asset.json").write_text("needle")
        self.assertFalse(search_project(self.project, "needle", buffers={str(outside): "needle"})["matches"])
        try:
            (self.project / "escape.cpp").symlink_to(outside)
        except OSError:
            pass  # Windows accounts can lack symlink privilege.
        else:
            self.assertFalse(search_project(self.project, "needle", buffers={str(self.project / "escape.cpp"): "needle"})["matches"])

    def test_limits_cancellation_and_unreadable_sources(self):
        (self.project / "many.cpp").write_text("needle\n" * 20)
        with patch.object(project_search, "MAX_MATCHES", 5):
            result = search_project(self.project, "needle")
        self.assertEqual(len(result["matches"]), 5)
        self.assertTrue(result["limited"])
        with patch.object(project_search, "MAX_FILE_BYTES", 8):
            result = search_project(self.project, "needle")
        self.assertGreater(result["skipped"], 0)
        cancel = threading.Event()
        cancel.set()
        self.assertTrue(search_project(self.project, "needle", cancel=cancel)["cancelled"])
        self.assertIn("error", search_project(self.folder / "missing", "needle"))

    def test_search_snapshot_keeps_last_keystroke_and_newer_edit_or_save(self):
        from main.qt.editor_panel import MonacoEditorPanel
        path = str(self.main)
        original = {"path": path, "content": "latest renderer", "buffer_present": True,
                    "buffer_baseline": "old bridge"}
        panel = SimpleNamespace(_bridge=SimpleNamespace(_buffer_snapshots={path: "old bridge"}),
                                _search_active_snapshot=dict(original))
        self.assertEqual(MonacoEditorPanel._search_buffers(panel)[path], "latest renderer")
        self.assertEqual(MonacoEditorPanel._search_buffers(panel)[path], "latest renderer")
        self.assertEqual(panel._bridge._buffer_snapshots[path], "old bridge")
        panel._bridge._buffer_snapshots[path] = "newer bridge"
        panel._search_active_snapshot = dict(original)
        self.assertEqual(MonacoEditorPanel._search_buffers(panel)[path], "newer bridge")
        panel._bridge._buffer_snapshots.clear()  # A manual save cleared dirty state.
        panel._search_active_snapshot = dict(original)
        self.assertNotIn(path, MonacoEditorPanel._search_buffers(panel))

    def test_dialog_latest_query_navigation_and_closed_results(self):
        navigate = Mock()
        dialog = ProjectSearchDialog(self.project, lambda: {str(self.main): "unsaved target"}, navigate)
        self.addCleanup(dialog.deleteLater)
        gate, entered = threading.Event(), threading.Event()
        actual = project_search.search_project

        def slow(*args, **kwargs):
            if args[1] == "old":
                entered.set()
                gate.wait(2)
            return actual(*args, **kwargs)

        with patch.object(project_search, "search_project", side_effect=slow):
            dialog.open_search("old")
            dialog._search_now()
            until(entered.is_set)
            dialog.query.setText("target")
            dialog._search_now()
            gate.set()
            until(lambda: not dialog._running and dialog.results.topLevelItemCount() == 1)
            item = dialog.results.topLevelItem(0)
            self.assertEqual(item.data(0, Qt.ItemDataRole.UserRole)["preview"], "unsaved target")
            dialog._activate_result(item)
            navigate.assert_called_once_with(str(self.main), 1, 9, 15)
            self.assertTrue(dialog.isVisible())
            dialog.open_search()
            dialog.close()
            until(lambda: not dialog._running)
            self.assertTrue(dialog._closed)

    def test_buffer_snapshot_failure_reports_and_allows_retry(self):
        buffers = Mock(side_effect=[RuntimeError("Editor snapshot unavailable"), {}])
        dialog = ProjectSearchDialog(self.project, buffers, Mock())
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        dialog.open_search("motor")
        dialog._search_now()
        self.assertFalse(dialog._running)
        self.assertIn("Editor snapshot unavailable", dialog.status.text())
        dialog._search_now()
        until(lambda: not dialog._running and dialog.results.topLevelItemCount() == 5)
        self.assertIn("5 matches", dialog.status.text())

    def test_automatic_search_and_empty_results_do_not_stay_searching(self):
        dialog = ProjectSearchDialog(self.project, dict, Mock())
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        dialog.open_search()
        QTest.keyClicks(dialog.query, "112500")
        until(lambda: dialog.status.text().startswith("0 matches"))
        self.assertFalse(dialog._running)
        self.assertFalse(dialog.next_button.isEnabled())
        self.assertEqual(dialog.position.text(), "0 of 0")
        QTest.keyClick(dialog.query, Qt.Key.Key_Return)
        until(lambda: not dialog._running and not dialog._timer.isActive())
        self.assertTrue(dialog.isVisible())
        dialog.query.setText("motor")
        until(lambda: dialog.results.topLevelItemCount() == 5)
        self.assertEqual(dialog.position.text(), "1 of 5")

    def test_navigation_cycles_four_results_with_buttons_and_keyboard(self):
        navigate = Mock()
        dialog = ProjectSearchDialog(self.project, lambda: {str(self.main): "😀hit hit\nhit\nhit"}, navigate)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        dialog.open_search("hit")
        until(lambda: dialog.results.topLevelItemCount() == 4)
        self.assertEqual(dialog.position.text(), "1 of 4")
        self.assertTrue(dialog.next_button.isEnabled())
        dialog.next_button.click()
        self.assertEqual(dialog.position.text(), "2 of 4")
        navigate.assert_called_with(str(self.main), 1, 7, 10)
        dialog.previous_button.click()
        self.assertEqual(dialog.position.text(), "1 of 4")
        navigate.assert_called_with(str(self.main), 1, 3, 6)
        dialog.previous_button.click()
        self.assertEqual(dialog.position.text(), "4 of 4")
        navigate.assert_called_with(str(self.main), 3, 1, 4)
        dialog.next_button.click()
        self.assertEqual(dialog.position.text(), "1 of 4")
        dialog.query.setFocus()
        QTest.keyClick(dialog.query, Qt.Key.Key_F3)
        self.assertEqual(dialog.position.text(), "2 of 4")
        QTest.keyClick(dialog.query, Qt.Key.Key_F3, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(dialog.position.text(), "1 of 4")
        QTest.keyClick(dialog.query, Qt.Key.Key_Return)
        self.assertEqual(dialog.position.text(), "2 of 4")
        self.assertTrue(dialog.isVisible())
        dialog.query.clear()
        self.assertEqual(dialog.position.text(), "0 of 0")
        self.assertFalse(dialog.previous_button.isEnabled())
        calls = navigate.call_count
        dialog._step_result(1)
        self.assertEqual(navigate.call_count, calls)

    def test_reopening_the_same_query_starts_a_fresh_search(self):
        dialog = ProjectSearchDialog(self.project, dict, Mock())
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        actual = project_search.search_project
        calls = []
        def slow(*args, **kwargs):
            calls.append(args[1])
            if len(calls) == 1:
                entered.set()
                gate.wait(2)
            return actual(*args, **kwargs)
        with patch.object(project_search, "search_project", side_effect=slow):
            dialog.open_search("motor")
            until(entered.is_set)
            dialog.close()
            dialog.open_search("motor")
            until(lambda: dialog._pending)
            gate.set()
            until(lambda: not dialog._running and dialog.results.topLevelItemCount() == 5)
        self.assertEqual(calls, ["motor", "motor"])
        self.assertEqual(dialog.position.text(), "1 of 5")

    def test_stalled_storage_reports_waiting_and_keeps_one_worker_until_retry(self):
        dialog = ProjectSearchDialog(self.project, dict, Mock())
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        actual = project_search.search_project
        calls = []
        def slow(*args, **kwargs):
            calls.append(args[1])
            if len(calls) == 1:
                entered.set()
                gate.wait(2)
            return actual(*args, **kwargs)
        with patch.object(project_search, "search_project", side_effect=slow):
            dialog.open_search("old")
            until(entered.is_set)
            dialog._search_waiting()
            self.assertIn("storage is not responding", dialog.status.text())
            self.assertTrue(dialog._cancel.is_set())
            dialog.query.setText("motor")
            dialog.search_button.click()
            self.assertEqual(calls, ["old"])
            self.assertTrue(dialog._pending)
            self.assertIn("previous search", dialog.status.text())
            gate.set()
            until(lambda: not dialog._running and dialog.results.topLevelItemCount() == 5)
        self.assertEqual(calls, ["old", "motor"])

    def test_modify_default_primary_sketch_name_and_short_dialogs_all_themes(self):
        backend = SimpleNamespace(
            sketch_dir_path=self.project, active_file_path=str(self.project / "helper.cpp"),
            get_project_files=lambda: [{"name": "helper.ino"}, {"name": "MotorSketch.ino"}, {"name": "MotorSketch.h"}],
            is_busy=False, active_operation=None,
        )
        dialog = ModifyFilesDialog(backend)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog._add_name_edit.text(), "MotorSketch")
        self.assertGreaterEqual(dialog.height(), 400)
        self.assertEqual(len(dialog.findChildren(QScrollArea)), 3)
        for theme in ("default", "light", "solarized"):
            dialog._apply_dialog_theme(theme)
            for size in ((520, 460), (350, 280)):
                dialog.resize(*size)
                dialog.show()
                APP.processEvents()
                for tab, button in enumerate((dialog._btn_add, dialog._btn_rename, dialog._btn_delete)):
                    dialog._tabs.setCurrentIndex(tab)
                    APP.processEvents()
                    point = button.mapTo(dialog, button.rect().center())
                    self.assertTrue(dialog.rect().contains(point), (theme, size, tab))
                    self.assertTrue(dialog.rect().contains(QRect(button.mapTo(dialog, QPoint()), button.size())), (theme, size, tab))
                    self.assertTrue(button.isVisible())
                if CAPTURES:
                    dialog.grab().save(str(CAPTURES / f"modify-{theme}-{size[0]}.png"))
        dialog.close()
        search = ProjectSearchDialog(self.project, lambda: {str(self.main): "motor\n" * 2000}, Mock())
        self.addCleanup(search.deleteLater)
        for theme in ("default", "light", "solarized"):
            search._apply_theme(theme)
            search.open_search("motor")
            until(lambda: not search._running and not search._timer.isActive())
            self.assertEqual(search.results.topLevelItemCount(), 2000)
            search.results.setCurrentItem(search.results.topLevelItem(1999))
            for width, height in ((680, 440), (350, 280)):
                search.resize(width, height)
                APP.processEvents()
                self.assertEqual(search.width(), width)
                for widget in (search.query, search.match_case, search.whole_word, search.search_button,
                               search.previous_button, search.next_button, search.position, search.close_button):
                    self.assertTrue(search.rect().contains(QRect(widget.mapTo(search, QPoint()), widget.size())),
                                    (theme, width, type(widget).__name__))
                if CAPTURES:
                    search.grab().save(str(CAPTURES / f"search-{theme}-{width}.png"))
        search.close()

    def test_real_monaco_brace_enter_preserves_indent_and_undo(self):
        view = QWebEngineView()
        self.addCleanup(view.deleteLater)
        view.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        view.resize(800, 500)
        loaded = []
        view.loadFinished.connect(loaded.append)
        view.load(QUrl.fromLocalFile(str(ROOT / "src/editor/index.html")))
        until(lambda: loaded, seconds=15)
        self.assertTrue(loaded[-1])
        ready = []

        def poll_ready():
            if not ready:
                view.page().runJavaScript("Boolean(window.editorInstance && window.configureCppIndentation)", lambda value: ready.append(value) if value else None)
            return bool(ready)

        until(poll_ready, seconds=15)
        view.page().runJavaScript("window.configureCppIndentation().then(ready => window.__fixtureIndentReady = ready)")
        configured = []
        until(lambda: (view.page().runJavaScript("Boolean(window.__fixtureIndentReady)", lambda value: configured.append(value) if value else None), bool(configured))[1])
        output = []
        view.page().runJavaScript(r'''(() => {
            const editor = window.editorInstance;
            const checks = [];
            for (const [text, column, expected] of [
                ['void setup() {}', 15, 'void setup() {\n  \n}'],
                ['  if (true) {', 14, '  if (true) {\n    '],
                ['void setup() {', 15, 'void setup() {\n  ']
            ]) {
                const model = monaco.editor.createModel(text, 'cpp');
                model.setEOL(monaco.editor.EndOfLineSequence.LF);
                model.updateOptions({tabSize: 2, insertSpaces: true});
                editor.setModel(model);
                editor.setPosition({lineNumber: 1, column});
                editor.trigger('keyboard', 'type', {text: '\n'});
                checks.push({text: model.getValue(), expected, column: editor.getPosition().column});
                editor.trigger('fixture', 'undo', null);
                checks[checks.length - 1].undo = model.getValue();
                checks[checks.length - 1].original = text;
            }
            const tabs = monaco.editor.createModel('void setup() {}', 'cpp');
            tabs.setEOL(monaco.editor.EndOfLineSequence.LF);
            tabs.updateOptions({tabSize: 4, insertSpaces: false});
            editor.setModel(tabs);
            editor.setPosition({lineNumber: 1, column: 15});
            editor.trigger('keyboard', 'type', {text: '\n'});
            return JSON.stringify({checks, tabs: tabs.getValue(), autoIndent: editor.getRawOptions().autoIndent});
        })()''', output.append)
        until(lambda: output)
        result = json.loads(output[0])
        for check in result["checks"]:
            self.assertEqual(check["text"], check["expected"], result)
            self.assertEqual(check["undo"], check["original"])
        self.assertEqual(result["tabs"], "void setup() {\n\t\n}")
        # Exercise the real renderer snapshot → native Find All route without
        # constructing a live backend or saving the fixture's dirty source.
        from main.qt.editor_panel import MonacoEditorPanel
        path = json.dumps(str(self.main))
        prepared = []
        view.page().runJavaScript(
            "(() => { try {"
            "document.querySelectorAll('#tab-bar .tab.active').forEach(tab => tab.classList.remove('active'));"
            "const tab = document.createElement('div'); tab.className = 'tab active';"
            f"tab._filePath = {path}; document.getElementById('tab-bar').appendChild(tab);"
            "window.editorInstance.getModel().setValue('😀sentinel sentinel');"
            " return JSON.stringify({content: window.editorInstance.getValue(),"
            " path: document.querySelector('#tab-bar .tab.active')?._filePath});"
            " } catch(error) { return JSON.stringify({error:String(error)}); } })()", prepared.append
        )
        until(lambda: prepared)
        self.assertEqual(json.loads(prepared[0]).get("content"), "😀sentinel sentinel", prepared)
        panel = SimpleNamespace(
            _backend=SimpleNamespace(sketch_dir_path=self.project), _view=view,
            _bridge=SimpleNamespace(_buffer_snapshots={str(self.main): "old bridge"}),
            window=lambda: view, goto_location=Mock(),
        )
        panel._search_buffers = lambda: MonacoEditorPanel._search_buffers(panel)
        panel._navigate_source = lambda location: MonacoEditorPanel._navigate_source(panel, location)
        panel.goto_location = lambda *args: MonacoEditorPanel.goto_location(panel, *args)
        view.show()
        MonacoEditorPanel.show_project_search(panel)
        until(lambda: hasattr(panel, "_search_dialog"))
        dialog = panel._search_dialog
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(panel._search_active_snapshot.get("content"), "😀sentinel sentinel", panel._search_active_snapshot)
        dialog.query.setText("sentinel")
        until(lambda: not dialog._running and dialog.results.topLevelItemCount() == 2)
        self.assertEqual(dialog.position.text(), "1 of 2")
        self.assertTrue(dialog.results.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole)["unsaved"])
        dialog.next_button.click()
        location = []
        def positioned():
            if not location:
                view.page().runJavaScript(
                    "(() => { const selection = window.editorInstance.getSelection();"
                    " if (selection.startColumn !== 12 || selection.endColumn !== 20) return null;"
                    " return JSON.stringify({selection, text: window.editorInstance.getValue()}); })()",
                    lambda value: location.append(json.loads(value)) if value else None)
            return bool(location)
        until(positioned)
        self.assertEqual(location[0]["text"], "😀sentinel sentinel")
        self.assertEqual(dialog.position.text(), "2 of 2")
        self.assertTrue(dialog.isVisible())
        # A second query must retain the unacknowledged renderer snapshot.
        dialog.query.setText("😀sentinel")
        until(lambda: not dialog._running and dialog.results.topLevelItemCount() == 1)
        self.assertEqual(panel._bridge._buffer_snapshots[str(self.main)], "old bridge")
        self.assertNotIn("sentinel", self.main.read_text(encoding="utf-8"))
        dialog.close()
        view.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-dir", type=Path)
    options, args = parser.parse_known_args()
    if options.render_dir:
        CAPTURES = options.render_dir.resolve()
        CAPTURES.mkdir(parents=True, exist_ok=True)
    run = unittest.main(argv=[sys.argv[0], *args], verbosity=2, exit=False)
    # Flush owned widgets' DeferredDelete events while Qt is still alive; the
    # WebEngine profile must outlive its page and local renderer shutdown.
    QTimer.singleShot(150, APP.quit)
    APP.exec()
    sys.exit(0 if run.result.wasSuccessful() else 1)
