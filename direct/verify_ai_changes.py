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
from PySide6.QtGui import QMouseEvent, QTextCursor
from PySide6.QtWidgets import QApplication
from main.core.ai_review import AIReviewManager, AIEditWatcher, DISMISSED_NOTE, flush_review_journals
from main.qt.ai_changes_panel import AIChangesPanel, CodePreview
from main.qt.console_panel import ConsolePanel
from src.modules.ai_prompt_context import PromptInputTracker

APP = QApplication.instance() or QApplication([])
from main.qt.garbage_collection import install_gui_garbage_collector
COLLECTOR = install_gui_garbage_collector(APP)
from main.qt.theme import register_fonts
register_fonts()


class AIChangesChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "temp/audit").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "temp/audit")
        self.addCleanup(self.temp.cleanup)
        # Journals are written in the background; let them land before removal.
        self.addCleanup(flush_review_journals)
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

    def test_manual_backend_templates_and_new_project_header_are_minimal(self):
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
        self.assertEqual((self.root / "Fresh/Fresh.cpp").read_text(), '#include "Fresh.h"\n')
        self.assertEqual((self.root / "Fresh/Fresh.h").read_text(), "#pragma once\n")
        self.assertEqual((self.root / "Fresh/Fresh.ino").read_text(),
                         '#include "Fresh.h"\n\nvoid setup() {\n\n}\n\nvoid loop() {\n\n}\n')
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
        self.assertEqual(payload["sourceVersion"], 1)
        self.assertEqual(payload["prompt"], "Update Ω and header")
        self.assertTrue(payload["id"])

    def test_source_groups_have_exact_labels_and_keep_known_prompt_tooltip(self):
        known = {"id": "cli-1", "prompt": "Fix the sensor setup", "source": "cli", "sourceVersion": 1}
        self.queue("CLI.ino", prompt=known)
        self.queue("Assistant.cpp", prompt={"id": "assistant-1", "prompt": "Assistant changes header",
                                           "source": "assistant", "sourceVersion": 1})
        self.queue("Legacy.cpp", prompt={"id": "legacy", "prompt": "Assistant prompt (title unavailable)"})
        self.queue("Unknown.h")
        with patch("main.core.config.get_monitor_font_size", return_value=11):
            panel = AIChangesPanel(SimpleNamespace(ai_review_manager=self.manager))
        groups = [panel._tree.topLevelItem(i) for i in range(panel._tree.topLevelItemCount())]
        self.assertEqual([item.text(0) for item in groups].count("CLI"), 1)
        self.assertEqual([item.text(0) for item in groups].count("Assistant"), 1)
        self.assertEqual([item.text(0) for item in groups].count("Unknown source"), 2)
        self.assertTrue(any("Fix the sensor setup" in item.toolTip(0) for item in groups))
        records = self.manager.get_ai_changes()
        self.assertEqual(next(item for item in records if item["groupId"] == "cli-1")["prompt"], known["prompt"])
        output = ROOT / "temp/audit/ai-source-checks"
        output.mkdir(parents=True, exist_ok=True)
        panel.resize(820, 350)
        panel.show()
        APP.processEvents()
        self.assertTrue(panel.grab().save(str(output / "ai-tab-source-labels.png")))
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_source_survives_pending_reload_decision_and_undo_redo(self):
        for source in ("cli", "assistant"):
            context = {"id": source, "source": source, "sourceVersion": 1, "prompt": "Update Ω"}
            path = self.queue(source + ".cpp", prompt=context)
            review = self.manager.consume_ai_edit_snapshot(str(path))
            self.assertEqual(review["promptSource"], source)
            self.assertEqual(review["promptSourceVersion"], 1)
            self.assertEqual(self.manager.get_ai_edit_reviews()[-1]["prompt"], context["prompt"])
            reopened = AIReviewManager()
            reopened.project_dir = self.root
            reopened._ai_review_state_path = self.manager._ai_review_state_path
            reopened._load_pending_ai_edits_locked()
            self.assertEqual(reopened.consume_ai_edit_snapshot(str(path))["promptSource"], source)
            self.assertEqual(reopened.get_ai_changes()[-1]["promptSource"], source)
            self.assertTrue(self.manager.accept_ai_edit(str(path), review["revision"])["success"])
            history = self.manager.get_ai_history_state()["undo"]
            self.assertEqual(history["promptSource"], source)
            self.assertEqual(history["promptSourceVersion"], 1)
            self.assertEqual(history["prompt"], context["prompt"])
            self.assertTrue(self.manager.undo_ai_edit_decision()["success"])
            self.assertEqual(self.manager.get_ai_history_state()["redo"]["promptSource"], source)
            self.assertTrue(self.manager.redo_ai_edit_decision()["success"])

    def test_combined_pending_file_retains_both_channel_sources(self):
        path = self.queue("Combined.cpp", prompt={"id": "cli", "prompt": "Terminal edit", "source": "cli", "sourceVersion": 1})
        path.write_text("newer\n", encoding="utf-8")  # the watcher queues what it read from disk
        self.manager.queue_ai_edit_snapshot(path, "new\n", "newer\n",
            prompt_context={"id": "assistant", "prompt": "Assistant edit", "source": "assistant", "sourceVersion": 1})
        review = self.manager.consume_ai_edit_snapshot(str(path))
        self.assertEqual(review["promptSource"], "mixed")
        self.assertEqual(review["promptSources"], ["assistant", "cli"])
        self.assertEqual(review["prompt"], "Terminal edit\n\nAssistant edit")
        reopened = AIReviewManager()
        reopened.project_dir = self.root
        reopened._ai_review_state_path = self.manager._ai_review_state_path
        reopened._load_pending_ai_edits_locked()
        self.assertEqual(reopened.consume_ai_edit_snapshot(str(path))["prompt"], review["prompt"])
        self.assertEqual([row["promptSource"] for row in self.manager.get_ai_changes()], ["cli", "mixed"])
        self.assertTrue(self.manager.accept_ai_edit(str(path), review["revision"])["success"])
        self.assertEqual(self.manager.get_ai_history_state()["undo"]["promptSource"], "mixed")
        self.assertEqual(self.manager.get_ai_history_state()["undo"]["prompt"], review["prompt"])

    def test_unobserved_context_keeps_known_channels_without_claiming_attribution(self):
        from main.core.ai_review import _merge_prompt_context
        context = _merge_prompt_context(None, {"id": "new", "prompt": "Observed assistant prompt",
                                             "source": "assistant", "sourceVersion": 1})
        path = self.queue("Unknown.cpp", prompt=context)
        review = self.manager.consume_ai_edit_snapshot(str(path))
        self.assertEqual(review["promptSource"], "external")
        self.assertEqual(review["promptSources"], ["assistant", "external"])
        self.assertEqual(review["prompt"], "Observed assistant prompt")
        known = self.queue("KnownThenUnknown.cpp", prompt={"id": "known", "prompt": "Original CLI prompt",
                                                         "source": "cli", "sourceVersion": 1})
        self.manager.queue_ai_edit_snapshot(known, "new\n", "unobserved update\n", prompt_context=None)
        updated = self.manager.consume_ai_edit_snapshot(str(known))
        self.assertEqual(updated["promptSource"], "external")
        self.assertEqual(updated["promptSources"], ["cli", "external"])
        self.assertEqual(updated["prompt"], "Original CLI prompt")

    def test_legacy_metadata_cannot_claim_a_known_source(self):
        for context in ({"id": "old-cli", "source": "cli"},
                        {"id": "old-assistant", "source": "assistant"},
                        {"id": "fallback", "prompt": "Assistant prompt (title unavailable)"},
                        {"id": "bad-version", "source": "cli", "sourceVersion": True}):
            path = self.queue(context["id"] + ".cpp", prompt=context)
            self.assertEqual(self.manager.consume_ai_edit_snapshot(str(path))["promptSource"], "external")
        self.assertEqual(AIChangesPanel._prompt_title({"promptSource": "cli"}), "Unknown source")
        self.assertEqual(AIChangesPanel._prompt_title({"promptSource": "assistant", "promptSourceVersion": True}), "Unknown source")

    def test_watcher_keeps_first_change_context_when_another_prompt_arrives_during_settle(self):
        path = self.root / "Sensor.cpp"
        path.write_text("original", encoding="utf-8")
        watcher = self.watcher()
        self.scan(watcher)
        (self.root / ".mcu_flasher_build_cache").mkdir(exist_ok=True)
        PromptInputTracker(self.root, source="cli").feed("Edit sensor from terminal\r")
        path.write_text("CLI edit", encoding="utf-8")
        self.scan(watcher)
        PromptInputTracker(self.root, source="assistant").feed("Another prompt from assistant\r")
        for item in watcher._pending_settle.values():
            item["last_change_time"] -= 1
        self.scan(watcher)
        self.assertEqual(self.manager.consume_ai_edit_snapshot(str(path))["promptSource"], "cli")

    def test_watcher_records_both_sources_if_file_changes_again_while_settling(self):
        path = self.root / "Sensor.cpp"
        path.write_text("original", encoding="utf-8")
        watcher = self.watcher()
        self.scan(watcher)
        (self.root / ".mcu_flasher_build_cache").mkdir(exist_ok=True)
        PromptInputTracker(self.root, source="cli").feed("Edit sensor from terminal\r")
        path.write_text("CLI edit", encoding="utf-8")
        self.scan(watcher)
        PromptInputTracker(self.root, source="assistant").feed("Update sensor from assistant\r")
        path.write_text("Assistant edit", encoding="utf-8")
        self.scan(watcher)
        for item in watcher._pending_settle.values():
            item["last_change_time"] -= 1
        self.scan(watcher)
        self.assertEqual(self.manager.consume_ai_edit_snapshot(str(path))["promptSource"], "mixed")
        self.assertEqual(self.manager.consume_ai_edit_snapshot(str(path))["prompt"],
                         "Edit sensor from terminal\n\nUpdate sensor from assistant")

    def test_new_watcher_does_not_attribute_edits_to_a_prior_session_prompt(self):
        path = self.root / "Sensor.cpp"
        path.write_text("original", encoding="utf-8")
        (self.root / ".mcu_flasher_build_cache").mkdir(exist_ok=True)
        PromptInputTracker(self.root, source="assistant").feed("A prior session prompt\r")
        watcher = self.watcher()
        self.scan(watcher)
        path.write_text("unobserved edit", encoding="utf-8")
        self.scan(watcher)
        for item in watcher._pending_settle.values():
            item["last_change_time"] -= 1
        self.scan(watcher)
        self.assertEqual(self.manager.consume_ai_edit_snapshot(str(path))["promptSource"], "external")

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
                self.assertFalse(hasattr(preview, "_number_background"), "The gutter is part of the same glass")
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
        prompt = {"id": "three-files", "prompt": "Update controller and header", "source": "cli", "sourceVersion": 1}
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
                    self.assertEqual(group.text(0), "CLI")
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

    def reopen(self):
        reopened = AIReviewManager()
        reopened.project_dir = self.root
        reopened._ai_review_state_path = self.manager._ai_review_state_path
        reopened._load_pending_ai_edits_locked()
        return reopened

    def test_reviews_of_files_changed_while_closed_are_dismissed_without_touching_them(self):
        kept = self.queue("Kept.ino", before="old\n", after="ai\n",
                          prompt={"id": "p", "prompt": "Edit both", "source": "cli", "sourceVersion": 1})
        changed = self.queue("Changed.cpp", before="old\n", after="ai\n",
                             prompt={"id": "p", "prompt": "Edit both", "source": "cli", "sourceVersion": 1})
        deleted = self.queue("Deleted.h", before="old\n", after="ai\n")
        self.manager.shutdown()
        # MCU Flasher is closed: another tool edits one file and removes another.
        changed.write_text("edited while closed\n", encoding="utf-8")
        deleted.unlink()
        reopened = self.reopen()
        self.assertEqual([Path(item["path"]).name for item in reopened.get_ai_edit_reviews()], ["Kept.ino"])
        self.assertTrue(reopened.has_pending_ai_edit(str(kept)))
        self.assertFalse(reopened.has_pending_ai_edit(str(changed)))
        self.assertEqual(changed.read_text(encoding="utf-8"), "edited while closed\n")
        self.assertFalse(deleted.exists())
        self.assertEqual(sorted(item["fileName"] for item in reopened.take_dismissed_reviews()),
                         ["Changed.cpp", "Deleted.h"])
        statuses = {Path(item["path"]).name: item for item in reopened.get_ai_changes()}
        self.assertEqual(statuses["Kept.ino"]["status"], "pending")
        for name in ("Changed.cpp", "Deleted.h"):
            self.assertEqual(statuses[name]["status"], "dismissed")
            self.assertEqual(statuses[name]["statusNote"], DISMISSED_NOTE)
        # The dismissal is durable: a later launch neither restores nor re-reports it.
        reopened.shutdown()
        again = self.reopen()
        self.assertEqual(len(again.get_ai_edit_reviews()), 1)
        self.assertEqual(again.take_dismissed_reviews(), [])
        self.assertEqual(again.reject_ai_edit(str(changed))["success"], False)
        self.assertEqual(changed.read_text(encoding="utf-8"), "edited while closed\n")

    def test_dismissal_found_at_construction_is_still_reported_after_the_project_rebinds(self):
        project = self.root / "Sketch"
        project.mkdir()
        first = AIReviewManager(project)
        path = project / "Sketch.ino"
        path.write_text("ai\n", encoding="utf-8")
        self.assertTrue(first.queue_ai_edit_snapshot(path, "old\n", "ai\n"))
        first.shutdown()
        path.write_text("edited while closed\n", encoding="utf-8")
        backend_manager = AIReviewManager(project)  # the backend binds at construction
        self.addCleanup(backend_manager.shutdown)
        backend_manager.bind_project(project)       # open_project binds the same sketch again
        self.assertEqual([item["fileName"] for item in backend_manager.take_dismissed_reviews()], ["Sketch.ino"])
        self.assertEqual(backend_manager.get_ai_edit_reviews(), [])
        self.assertEqual(path.read_text(encoding="utf-8"), "edited while closed\n")

    def test_rebinding_the_open_project_keeps_reviews_observed_this_session(self):
        manager = AIReviewManager(self.root)
        self.addCleanup(manager.shutdown)
        path = self.root / "Live.ino"
        path.write_text("ai\n", encoding="utf-8")
        self.assertTrue(manager.queue_ai_edit_snapshot(path, "old\n", "ai\n"))
        # A newer edit the watcher has not settled yet must not dismiss the review.
        path.write_text("ai, then more\n", encoding="utf-8")
        manager.bind_project(self.root)
        self.assertTrue(manager.has_pending_ai_edit(str(path)))
        self.assertEqual(manager.take_dismissed_reviews(), [])

    def test_gui_reads_never_wait_for_the_review_lock(self):
        path = self.queue("Busy.ino")
        held, release = threading.Event(), threading.Event()

        def hold():
            with self.manager._pending_ai_lock:
                held.set()
                release.wait(5)

        worker = threading.Thread(target=hold)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        results = []
        reader = threading.Thread(target=lambda: results.append((
            len(self.manager.get_ai_edit_reviews()), self.manager.has_pending_ai_edit(str(path)),
            self.manager.has_any_pending_ai_edits(), len(self.manager.get_ai_changes()),
            bool(self.manager.consume_ai_edit_snapshot(str(path))))))
        reader.start()
        reader.join(1.0)
        self.assertFalse(reader.is_alive(), "A GUI read waited for the review lock")
        self.assertEqual(results, [(1, True, True, 1, True)])

    def test_journal_writes_happen_off_the_calling_thread(self):
        from main.core import ai_review
        writer_threads = []
        original = self.manager._write_journal_atomic

        def slow(path, data):
            writer_threads.append(threading.current_thread().name)
            import time
            time.sleep(0.2)
            return original(path, data)

        with patch.object(self.manager, "_write_journal_atomic", side_effect=slow):
            import time
            started = time.monotonic()
            self.queue("Slow.ino")
            self.assertLess(time.monotonic() - started, 0.15, "Queueing a review waited for storage")
            self.assertTrue(ai_review.flush_review_journals(5))
        self.assertTrue(writer_threads)
        self.assertTrue(all(name == "MCU_AIReviewJournal" for name in writer_threads))
        self.assertEqual(len(self.reopen().get_ai_edit_reviews()), 1)

    def test_history_panel_marks_large_appends_as_one_range(self):
        # Below the 60,000-character preview bound, so neither side is truncated.
        base = "".join(f"int h{i} = x * {i};\n" for i in range(2400))
        self.queue("Large.cpp", before=base, after=base + "// appended\n")
        old_ranges, new_ranges = AIChangesPanel._changed_ranges(base, base + "// appended\n")
        self.assertEqual((old_ranges, new_ranges), ([], [(2400, 2401)]))
        with patch("main.core.config.get_monitor_font_size", return_value=11):
            panel = AIChangesPanel(SimpleNamespace(ai_review_manager=self.manager))
        try:
            self.assertEqual(len(panel._after.extraSelections()), 1)
            self.assertEqual(len(panel._before.extraSelections()), 0)
            selections = panel._after.extraSelections()
            self.assertEqual(QTextCursor(selections[0].cursor).block().text(), "// appended")
            # A multi-line change is one full-width selection spanning its lines.
            changed = base.replace("x * 5;", "x * 50;").replace("x * 6;", "x * 60;")
            self.assertEqual(AIChangesPanel._changed_ranges(base, changed), ([(5, 7)], [(5, 7)]))
            panel._highlight(panel._after, [(5, 7)], panel._added)
            selections = panel._after.extraSelections()
            self.assertEqual(len(selections), 1)
            selection = QTextCursor(selections[0].cursor)
            document = panel._after.document()
            self.assertEqual((document.findBlock(selection.selectionStart()).blockNumber(),
                              document.findBlock(selection.selectionEnd()).blockNumber()), (5, 7))
        finally:
            panel.close()
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_hidden_history_panel_defers_refreshes_until_shown(self):
        self.queue("Shown.ino")
        with patch("main.core.config.get_monitor_font_size", return_value=11):
            panel = AIChangesPanel(SimpleNamespace(ai_review_manager=self.manager))
        try:
            with patch.object(self.manager, "get_ai_changes", wraps=self.manager.get_ai_changes) as reads:
                for _ in range(5):
                    panel._request_refresh()
                APP.processEvents()
                self.assertEqual(reads.call_count, 0, "A hidden panel rebuilt its history")
                self.assertTrue(panel._stale)
                panel.resize(700, 320)
                panel.show()
                deadline = __import__("time").monotonic() + 2
                while panel._stale and __import__("time").monotonic() < deadline:
                    APP.processEvents()
                self.assertFalse(panel._stale)
                self.assertEqual(reads.call_count, 1)
                for _ in range(5):
                    panel._request_refresh()
                deadline = __import__("time").monotonic() + 2
                while panel._refresh_timer.isActive() and __import__("time").monotonic() < deadline:
                    APP.processEvents()
                self.assertEqual(reads.call_count, 2, "Visible refresh bursts are coalesced")
        finally:
            panel.close()
            panel.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main(verbosity=2)
