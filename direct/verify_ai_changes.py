"""Isolated AI history, manual file operations, prompt grouping and follow checks."""
from __future__ import annotations

import os
import sys
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication
from main.core.ai_review import AIReviewManager, AIEditWatcher
from main.qt.ai_changes_panel import AIChangesPanel, CodePreview
from main.qt.console_panel import ConsolePanel
from src.modules.ai_prompt_context import PromptInputTracker

APP = QApplication.instance() or QApplication([])
from main.qt.theme import register_fonts
register_fonts()


class AIChangesChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "temp/audit").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "temp/audit")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = AIReviewManager()
        self.manager.project_dir = self.root
        self.manager._ai_review_state_path = self.root / "fixture-journal.json"

    def queue(self, name, before="old\n", after="new\n", prompt=None):
        path = self.root / name
        path.write_text(after, encoding="utf-8")
        self.manager.queue_ai_edit_snapshot(path, before, after, prompt_context=prompt)
        return path

    def watcher(self):
        with patch("main.core.ai_review.threading.Thread"):
            watcher = AIEditWatcher(self.root, self.manager)
        watcher._scan_running = False
        self.addCleanup(watcher.shutdown)
        return watcher

    def scan(self, watcher):
        with watcher._lock:
            request = (watcher._generation, self.root, watcher._baseline_ready,
                dict(watcher._baseline_contents), dict(watcher._baseline_mtimes),
                {key: dict(value) for key, value in watcher._pending_settle.items()},
                dict(watcher._save_versions), True)
        return watcher._scan_project(request)

    def test_prompt_groups_per_file_decisions_persist_and_history_clear_preserves_review(self):
        first = {"id": "prompt-1", "prompt": "Update the sensor and header"}
        a = self.queue("Sensor.ino", prompt=first)
        b = self.queue("Sensor.h", prompt=first)
        reviews = self.manager.get_ai_edit_reviews()
        self.assertEqual([item["reviewIndex"] for item in reviews], [1, 2])
        self.assertEqual([item["pendingCount"] for item in reviews], [2, 2])
        self.assertTrue(self.manager.accept_ai_edit(str(a), reviews[0]["revision"])["success"])
        self.assertTrue(self.manager.has_pending_ai_edit(str(b)))
        self.queue("Sensor.cpp", prompt={"id": "prompt-2", "prompt": "Add the implementation"})
        records = self.manager.get_ai_changes()
        self.assertEqual([item["groupId"] for item in records], ["prompt-1", "prompt-1", "prompt-2"])
        self.assertEqual(records[0]["status"], "accepted")
        self.assertTrue(all(item["timestamp"] for item in records))
        reopened = AIReviewManager()
        reopened.project_dir = self.root
        reopened._ai_review_state_path = self.manager._ai_review_state_path
        reopened._load_pending_ai_edits_locked()
        self.assertEqual(reopened.get_ai_changes(), records)
        self.assertTrue(self.manager.delete_ai_changes(records[0]["id"])["success"])
        self.assertEqual(len(self.manager.get_ai_changes()), 2)
        self.assertTrue(self.manager.delete_ai_changes()["success"])
        self.assertEqual(len(self.manager.get_ai_edit_reviews()), 2)

    def test_failed_history_clear_retains_both_replicas(self):
        self.queue("Sensor.h")
        original = self.manager._write_journal_atomic
        fail = [True]
        def write(path, data):
            if path == self.manager._ai_review_state_path and fail[0]:
                fail[0] = False
                raise OSError("fixture denied replace")
            return original(path, data)
        with patch.object(self.manager, "_write_journal_atomic", side_effect=write):
            result = self.manager.delete_ai_changes()
        self.assertFalse(result["success"])
        self.assertEqual(len(self.manager.get_ai_changes()), 1)
        reopened = AIReviewManager()
        reopened.project_dir = self.root
        reopened._ai_review_state_path = self.manager._ai_review_state_path
        reopened._load_pending_ai_edits_locked()
        self.assertEqual(len(reopened.get_ai_changes()), 1)

    def test_manual_add_rename_delete_and_failed_operation_never_propose_ai_review(self):
        watcher = self.watcher()
        self.scan(watcher)
        added = self.root / "manual.h"
        renamed = self.root / "manual.cpp"
        with watcher.user_file_operation(added):
            added.write_text("#pragma once\n", encoding="utf-8")
            self.scan(watcher)
        self.scan(watcher)
        with watcher.user_file_operation(added, renamed):
            added.rename(renamed)
            self.scan(watcher)
        with watcher.user_file_operation(renamed):
            renamed.unlink()
            self.scan(watcher)
        with self.assertRaises(OSError), watcher.user_file_operation(added):
            raise OSError("fixture write failure")
        self.scan(watcher)
        self.assertFalse(self.manager.has_any_pending_ai_edits())
        self.assertFalse(watcher._user_operations)

    def test_manual_backend_templates_and_new_project_are_empty(self):
        from main import web_bridge
        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        api.sketch_dir_path = self.root
        api.is_busy = False
        api.ai_watcher = self.watcher()
        api.emit = Mock()
        api.update_skip_compile_availability = Mock()
        api.open_project = Mock(return_value={"success": True})
        self.scan(api.ai_watcher)
        # App-codebase and metadata calls are explicitly mocked for this fixture.
        with patch.object(web_bridge, "is_application_codebase_dir", return_value=False), \
             patch.object(web_bridge, "ensure_hidden_read_first_md"):
            for name, expected in (("manual.cpp", ""), ("manual.h", "#pragma once\n"),
                                   ("manual.ino", "void setup() {\n\n}\n\nvoid loop() {\n\n}\n")):
                self.assertTrue(api.add_project_file(name)["success"])
                self.assertEqual((self.root / name).read_text(), expected)
            self.assertTrue(api.create_project(parent_dir=str(self.root), name="Fresh", include_h=True,
                                               include_cpp=True)["success"])
        self.assertEqual((self.root / "Fresh/Fresh.cpp").read_text(), "")
        self.assertEqual((self.root / "Fresh/Fresh.h").read_text(), "#pragma once\n")
        self.assertEqual((self.root / "Fresh/Fresh.ino").read_text(),
                         "void setup() {\n\n}\n\nvoid loop() {\n\n}\n")
        self.scan(api.ai_watcher)
        self.assertFalse(self.manager.has_any_pending_ai_edits())

    def test_external_file_deletion_is_reviewed_and_reject_restores(self):
        path = self.root / "external.txt"
        path.write_text("previous note", encoding="utf-8")
        watcher = self.watcher()
        self.scan(watcher)
        path.unlink()
        self.scan(watcher)
        for item in watcher._pending_settle.values():
            item["last_change_time"] -= 1
        self.scan(watcher)
        review = self.manager.consume_ai_edit_snapshot(str(path))
        self.assertFalse(review["afterExists"])
        self.assertTrue(self.manager.reject_ai_edit(str(path), review["revision"])["success"])
        self.assertEqual(path.read_text(), "previous note")

    def test_prompt_tracker_preserves_multiline_unicode_and_ignores_capability_replies(self):
        tracker = PromptInputTracker(self.root)
        observed = []
        tracker._publish = observed.append
        tracker.feed("\x1b[1;2R")
        tracker.feed("\x1b[200~Update Ω\nthen header\x1b[201~\r")
        self.assertEqual(observed, ["Update Ω\nthen header"])
        tracker.feed("yes\r/exit\r")
        self.assertEqual(len(observed), 1)
        tracker.feed("ordinary shell command\r", active=False)
        self.assertEqual(len(observed), 1)
        tracker.feed("Modify header\x1b[D\r")
        self.assertEqual(observed[-1], "Assistant prompt (title unavailable)")

    def test_cli_prompt_context_identifies_source_without_rewriting_prompt(self):
        import json
        cache = self.root / ".mcu_flasher_build_cache"
        cache.mkdir(exist_ok=True)
        tracker = PromptInputTracker(self.root)
        tracker.feed("Update Ω and header\r")
        payload = json.loads((cache / "ai_prompt.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["source"], "cli")
        self.assertEqual(payload["prompt"], "Update Ω and header")
        self.assertTrue(payload["id"])

    def test_cli_groups_have_exact_label_and_keep_known_prompt_tooltip(self):
        known = {"id": "cli-1", "prompt": "Fix the sensor setup", "source": "cli"}
        self.queue("CLI.ino", prompt=known)
        self.queue("Legacy.cpp", prompt={"id": "legacy", "prompt": "Assistant prompt (title unavailable)"})
        self.queue("Unknown.h")
        with patch("main.core.config.get_monitor_font_size", return_value=11):
            panel = AIChangesPanel(SimpleNamespace(ai_review_manager=self.manager))
        groups = [panel._tree.topLevelItem(i) for i in range(panel._tree.topLevelItemCount())]
        self.assertEqual([item.text(0) for item in groups].count("Assistant Prompt (CLI)"), 2)
        self.assertIn("External assistant changes (prompt unavailable)", [item.text(0) for item in groups])
        self.assertIn("Fix the sensor setup", [item.toolTip(0) for item in groups])
        records = self.manager.get_ai_changes()
        self.assertEqual(next(item for item in records if item["groupId"] == "cli-1")["prompt"], known["prompt"])
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_code_preview_numbers_follow_scroll_wrap_and_font_metrics(self):
        from PySide6.QtGui import QFont, QTextCursor
        preview = CodePreview()
        def cleanup():
            preview.close()
            preview.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(cleanup)
        preview.setFont(QFont("Consolas", 11))
        preview.resize(480, 160)
        preview.setPlainText("\n".join(f"line {number}: " + "x" * 150 for number in range(1, 121)))
        preview.show()
        APP.processEvents()
        width = preview._number_area.width()
        self.assertGreaterEqual(preview.viewport().x(), width)
        self.assertEqual(next(preview._number_rows())[0], 1)
        preview.verticalScrollBar().setValue(57)
        preview.horizontalScrollBar().setValue(40)
        APP.processEvents()
        number, top, _ = next(preview._number_rows())
        self.assertEqual(number, preview.firstVisibleBlock().blockNumber() + 1)
        self.assertGreater(number, 1)
        cursor = QTextCursor(preview.document().findBlockByNumber(number - 1))
        self.assertAlmostEqual(top, preview.cursorRect(cursor).top(), delta=1)
        self.assertEqual(preview._number_area.x(), preview.contentsRect().x())
        self.assertEqual(preview._number_area.width(), width)
        preview.setFont(QFont("Consolas", 17))
        APP.processEvents()
        self.assertEqual(preview._number_area.width(), preview._number_width())
        self.assertGreater(preview._number_area.width(), width)
        preview.setFont(QFont("Consolas", 11))
        preview.setLineWrapMode(CodePreview.LineWrapMode.WidgetWidth)
        preview.setPlainText("wrapped " + "x" * 150 + "\nsecond source line\nthird source line")
        preview.resize(240, 300)
        APP.processEvents()
        rows = list(preview._number_rows())
        self.assertEqual([row[0] for row in rows], [1, 2, 3])
        second = QTextCursor(preview.document().findBlockByNumber(1))
        self.assertAlmostEqual(rows[1][1], preview.cursorRect(second).top(), delta=1)
        self.assertGreater(rows[1][1] - rows[0][1], preview.fontMetrics().height())
        self.assertTrue(preview.isReadOnly())

    def test_auto_follow_resumes_after_viewport_hold_and_outside_release(self):
        with patch("main.core.config.get_monitor_font_size", return_value=11), \
             patch("main.core.config.get_hide_build_console_warnings", return_value=False):
            console = ConsolePanel()
        console.resize(720, 160)
        console.show()
        APP.processEvents()
        for row in range(120):
            console.append_log({"text": f"Output {row}"})
        console._flush_queue()
        APP.processEvents()
        console._follow._press()
        console.verticalScrollBar().setValue(15)
        console.append_log({"text": "during hold"})
        console._flush_queue()
        self.assertLess(console.verticalScrollBar().value(), console.verticalScrollBar().maximum())
        release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(1, 1), QPointF(1, 1),
                              Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        console._follow.eventFilter(APP, release)
        APP.processEvents()
        self.assertEqual(console.verticalScrollBar().value(), console.verticalScrollBar().maximum())
        console.set_autoscroll(False)
        console.verticalScrollBar().setValue(10)
        console._follow._press()
        console._follow.eventFilter(APP, release)
        APP.processEvents()
        self.assertEqual(console.verticalScrollBar().value(), 10)
        console.close()
        console.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_history_panel_themes_diff_and_compact_actions(self):
        self.queue("Sensor.h", before="#pragma once\nold();\n", after="#pragma once\nnew();\n")
        with patch("main.core.config.get_monitor_font_size", return_value=11):
            panel = AIChangesPanel(SimpleNamespace(ai_review_manager=self.manager))
        output = ROOT / "temp/audit/ai-changes"
        output.mkdir(parents=True, exist_ok=True)
        for mode in ("default", "light", "solarized_dark"):
            from main.qt.theme import build_stylesheet
            APP.setStyleSheet(build_stylesheet(mode))
            panel.apply_theme(mode)
            panel.resize(500, 360)
            panel.show()
            APP.processEvents()
            self.assertEqual(len(panel._before.extraSelections()), 1)
            self.assertEqual(len(panel._after.extraSelections()), 1)
            for preview in (panel._before, panel._after):
                from main.qt.theme import get_palette
                pal = get_palette(mode)
                self.assertEqual(preview._number_foreground.name(), pal["TEXT_DIM"].lower())
                self.assertEqual(preview._number_background.name(), pal["BG_DARK"].lower())
                self.assertGreater(preview.viewport().x(), 0)
                self.assertTrue(preview._number_area.isVisible())
                self.assertEqual([row[0] for row in preview._number_rows()], [1, 2, 3])
            self.assertEqual(panel._splitter.orientation(), Qt.Orientation.Vertical)
            for button in (panel._review, panel._delete, panel._clear):
                self.assertTrue(button.isVisible())
                self.assertTrue(panel.rect().contains(button.mapTo(panel, button.rect().bottomRight())))
            self.assertTrue(panel.grab().save(str(output / f"{mode}.png")))
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_filenames_and_timestamps_remain_visible_in_shared_history_pane(self):
        prompt = {"id": "three-files", "prompt": "Update controller and header", "source": "cli"}
        paths = [self.queue(name, prompt=prompt) for name in ("Header.h", "Controller.ino", "loop.cpp")]
        review = self.manager.get_ai_edit_reviews()[0]
        self.assertTrue(self.manager.accept_ai_edit(str(paths[0]), review["revision"])["success"])
        with patch("main.core.config.get_monitor_font_size", return_value=11):
            panel = AIChangesPanel(SimpleNamespace(ai_review_manager=self.manager))
        output = ROOT / "temp/audit/ai-changes"
        output.mkdir(parents=True, exist_ok=True)
        requested = []
        from main.qt.signals import signals
        signals.ai_review_requested.connect(requested.append)
        try:
            for mode in ("default", "light", "solarized_dark"):
                from main.qt.theme import build_stylesheet
                APP.setStyleSheet(build_stylesheet(mode))
                panel.apply_theme(mode)
                for width in (1440, 920, 500):
                    panel.resize(width, 380)
                    panel.show()
                    APP.processEvents()
                    self.assertEqual(panel._tree.columnCount(), 2)
                    group = panel._tree.topLevelItem(0)
                    self.assertEqual(group.text(0), "Assistant Prompt (CLI)")
                    self.assertEqual(group.childCount(), 3)
                    for index in range(group.childCount()):
                        card = group.child(index)
                        record = panel._records[card.data(0, Qt.ItemDataRole.UserRole)]
                        name, timestamp = card.text(0).splitlines()
                        self.assertEqual(name, Path(record["path"]).name)
                        self.assertIn(":", timestamp)
                        self.assertIn(record["path"], card.toolTip(0))
                        self.assertIn(timestamp, card.toolTip(0))
                        self.assertEqual(card.text(1), record["status"].capitalize())
                        self.assertGreaterEqual(panel._tree.columnWidth(0) - panel._tree.indentation() * 2,
                                                panel._tree.fontMetrics().horizontalAdvance(timestamp))
                        self.assertGreaterEqual(panel._tree.visualItemRect(card).height(),
                                                panel._tree.fontMetrics().height() * 2)
                    first_column_space = panel._tree.columnWidth(0) - panel._tree.indentation() * 2
                    self.assertGreater(first_column_space, panel._tree.fontMetrics().horizontalAdvance("Header.h"))
                    self.assertTrue(panel.grab().save(str(output / f"{mode}-files-{width}.png")))
            card = group.child(0)
            panel._tree.setCurrentItem(card)
            panel._review_selected()
            self.assertEqual(requested, [panel._records[card.data(0, Qt.ItemDataRole.UserRole)]["path"]])
        finally:
            signals.ai_review_requested.disconnect(requested.append)
            panel.close()
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main(verbosity=2)
