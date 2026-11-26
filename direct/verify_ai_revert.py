#!/usr/bin/env python3
"""Verify reverting one AI edit or a whole session, with owned sketches and journals."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from main.core import ai_revert, file_utils
from main.core.ai_review import AIEditWatcher, AIReviewManager, flush_review_journals
from main.qt.ai_changes_panel import AIChangesPanel
from main.qt.ai_revert_dialog import AIRevertDialog
from main.qt.signals import signals

APP = QApplication.instance() or QApplication([])
from main.qt.theme import register_fonts
register_fonts()

OLD = "int led = 13;\n\nvoid setup() {\n  pinMode(led, OUTPUT);\n}\n\nvoid loop() {\n  delay(1000);\n}\n"
NEW = OLD.replace("int led = 13;", "int led = 2;\nint button = 4;").replace("delay(1000)", "delay(250)")


def delete_on_gui_thread(obj) -> None:
    """Destroy a Qt object here, never in whichever thread next runs Python's collector.

    A watcher owns a QFileSystemWatcher (socket notifiers). Left to garbage collection it can
    be destroyed from a revert or writer thread, which Qt reports and then crashes on.
    """
    try:
        obj.deleteLater()
    except RuntimeError:
        return  # Already destroyed.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def until(condition, message, seconds=8):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        APP.processEvents()
        time.sleep(0.005)


class UndoTextChecks(unittest.TestCase):
    def test_whole_edit_comes_out_and_later_changes_elsewhere_stay(self):
        self.assertEqual(ai_revert.undo_change(NEW, OLD, NEW)[0], OLD)
        later = "// header\n" + NEW.replace("pinMode(led, OUTPUT);", "pinMode(led, OUTPUT);\n  Serial.begin(9600);")
        text, undone, blocked = ai_revert.undo_change(later, OLD, NEW)
        self.assertEqual(text, "// header\n" + OLD.replace("pinMode(led, OUTPUT);", "pinMode(led, OUTPUT);\n  Serial.begin(9600);"))
        self.assertEqual((undone, blocked), (2, 0))

    def test_lines_edited_again_are_left_alone_and_reported(self):
        text, undone, blocked = ai_revert.undo_change(NEW.replace("int led = 2;", "int led = 7;"), OLD, NEW)
        self.assertIn("int led = 7;", text)
        self.assertIn("delay(1000)", text)
        self.assertEqual((undone, blocked), (1, 1))

    def test_removed_lines_return_between_their_neighbours_only(self):
        self.assertEqual(ai_revert.undo_change("a\nc\n", "a\nb\nc\n", "a\nc\n"), ("a\nb\nc\n", 1, 0))
        self.assertEqual(ai_revert.undo_change("a\nx\nc\n", "a\nb\nc\n", "a\nc\n"), ("a\nx\nc\n", 0, 1))
        self.assertEqual(ai_revert.undo_change("", "a\n", ""), ("a\n", 1, 0))

    def test_already_undone_changes_and_line_endings(self):
        self.assertEqual(ai_revert.undo_change(OLD, OLD, NEW), (OLD, 2, 0))
        windows = NEW.replace("\n", "\r\n")
        self.assertEqual(ai_revert.undo_change(windows, OLD, NEW)[0], OLD.replace("\n", "\r\n"))
        self.assertEqual(ai_revert.undo_change(NEW, OLD.replace("\n", "\r\n"), NEW.replace("\n", "\r\n"))[0], OLD)

    def test_very_large_rewrites_stay_bounded(self):
        before, after = "".join(f"a{i}\n" for i in range(9000)), "".join(f"b{i}\n" for i in range(9000))
        started = time.monotonic()
        self.assertEqual(ai_revert.undo_change(after, before, after), (before, 1, 0))
        self.assertEqual(ai_revert.undo_change(after + "x\n", before, after), (after + "x\n", 0, 1))
        self.assertLess(time.monotonic() - started, 5)


class Fixture(unittest.TestCase):
    def setUp(self):
        (ROOT / "temp/audit").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "temp/audit")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(flush_review_journals)
        self.root = Path(self.temp.name).resolve() / "Blink"
        self.root.mkdir()
        self.manager = self.open()

    def open(self):
        manager = AIReviewManager(self.root)
        self.addCleanup(manager.shutdown)
        return manager

    def edit(self, name="Blink.ino", before=OLD, after=NEW, prompt="p", *, accept=True, manager=None, **exists):
        manager = manager or self.manager
        path = self.root / name
        if exists.get("after_exists", True):
            path.write_text(after, encoding="utf-8", newline="")
        manager.queue_ai_edit_snapshot(path, before, after, prompt_context={"id": prompt, "prompt": prompt}, **exists)
        if accept:
            self.assertTrue(manager.accept_ai_edit(str(path))["success"])
        return path, manager.get_ai_changes()[-1]["id"]

    def plan(self, *ids, restore=False, manager=None):
        plan = (manager or self.manager).plan_ai_revert(ids, restore)
        self.assertTrue(plan["success"], plan)
        return plan


class ManagerChecks(Fixture):
    def test_one_edit_is_undone_recorded_and_can_be_taken_back(self):
        path, card = self.edit()
        later = NEW.replace("void loop() {", "void loop() {\n  blink();")
        path.write_text(later, encoding="utf-8")
        plan = self.plan(card)
        self.assertEqual(path.read_text(encoding="utf-8"), later, "A preview writes nothing")
        result = self.manager.apply_ai_revert(plan)
        self.assertTrue(result["success"], result)
        expected = OLD.replace("void loop() {", "void loop() {\n  blink();")
        self.assertEqual(path.read_text(encoding="utf-8"), expected)
        item = self.manager.get_ai_changes()[-1]
        self.assertEqual(item["status"], "reverted")
        self.assertIn("later changes were kept", item["statusNote"])
        history = self.manager.get_ai_history_state()
        self.assertEqual(history["undo"]["action"], "reverted")
        self.assertTrue(self.manager.undo_ai_edit_decision()["success"])
        self.assertEqual(path.read_text(encoding="utf-8"), later)
        self.assertTrue(self.manager.redo_ai_edit_decision()["success"])
        self.assertEqual(path.read_text(encoding="utf-8"), expected)

    def test_going_back_before_an_edit_replaces_later_changes(self):
        path, card = self.edit()
        path.write_text(NEW + "// mine\n", encoding="utf-8")
        plan = self.plan(card, restore=True)
        self.assertEqual(plan["files"][0]["result"], OLD)
        self.assertTrue(self.manager.apply_ai_revert(plan)["success"])
        self.assertEqual(path.read_text(encoding="utf-8"), OLD)

    def test_created_and_deleted_files(self):
        path, card = self.edit("Added.h", "", "#pragma once\n", "create", before_exists=False)
        self.assertTrue(self.manager.apply_ai_revert(self.plan(card))["success"])
        self.assertFalse(path.exists())
        undone = self.manager.undo_ai_edit_decision()
        self.assertTrue(undone["workspaceShapeChanged"])
        self.assertEqual(path.read_text(encoding="utf-8"), "#pragma once\n")
        path.write_text("#pragma once\nint mine;\n", encoding="utf-8")
        self.assertEqual((self.plan(card)["changed"], self.plan(card)["blocked"]), (0, 1), "Own work is never deleted")
        gone = self.root / "Gone.cpp"
        gone.write_text("int x;\n", encoding="utf-8")
        gone.unlink()
        _, card = self.edit("Gone.cpp", "int x;\n", "", "delete", after_exists=False)
        self.assertTrue(self.manager.apply_ai_revert(self.plan(card))["success"])
        self.assertEqual(gone.read_text(encoding="utf-8"), "int x;\n")

    def test_pending_review_newer_change_and_failed_write_leave_files_alone(self):
        path, card = self.edit(accept=False)
        refused = self.manager.plan_ai_revert([card])
        self.assertFalse(refused["success"])
        self.assertIn("pending review", refused["error"])
        self.assertTrue(self.manager.accept_ai_edit(str(path))["success"])
        plan = self.plan(card)
        path.write_text(NEW + "// newer\n", encoding="utf-8")
        result = self.manager.apply_ai_revert(plan)
        self.assertTrue(result["conflict"])
        self.assertEqual(path.read_text(encoding="utf-8"), NEW + "// newer\n")
        other, other_card = self.edit("Other.cpp", "int a;\n", "int b;\n", "p2")
        plan = self.plan(other_card, card)
        real = self.manager._write_text_atomic
        with patch.object(self.manager, "_write_text_atomic", side_effect=lambda target, text: (
                real(target, text) if Path(target) == other else (_ for _ in ()).throw(OSError("locked")))):
            result = self.manager.apply_ai_revert(plan)
        self.assertIn("locked", result["error"])
        self.assertEqual(other.read_text(encoding="utf-8"), "int b;\n", "A failed session revert is rolled back")
        self.assertEqual({item["status"] for item in self.manager.get_ai_changes()}, {"accepted"})

    def test_a_session_is_reverted_newest_first_across_files(self):
        path, first = self.edit(prompt="one")
        newest = NEW.replace("delay(250)", "delay(50)")
        _, second = self.edit(before=NEW, after=newest, prompt="two")
        other, third = self.edit("Other.cpp", "int a;\n", "int b;\n", "two")
        plan = self.plan(third, second, first)
        self.assertEqual({item["fileName"]: item["result"] for item in plan["files"]}, {"Blink.ino": OLD, "Other.cpp": "int a;\n"})
        self.assertTrue(self.manager.apply_ai_revert(plan)["success"])
        self.assertEqual((path.read_text(encoding="utf-8"), other.read_text(encoding="utf-8")), (OLD, "int a;\n"))

    def test_saved_sessions_survive_restart_and_moving_the_sketch(self):
        path, _card = self.edit()
        saved = self.manager._ai_backup_store.session_dir
        self.manager.shutdown()
        before = {item.name: item.read_bytes() for item in saved.iterdir()}
        moved = self.root.with_name("Moved Blink")
        self.root.rename(moved)
        self.root = moved
        manager = self.open()
        sessions = manager.list_ai_sessions()
        self.assertEqual([len(session["edits"]) for session in sessions], [1])
        edit = sessions[0]["edits"][0]
        self.assertTrue(edit["available"])
        self.assertEqual(Path(edit["path"]), moved / "Blink.ino")
        plan = self.plan(edit["id"], manager=manager)
        self.assertTrue(manager.apply_ai_revert(plan)["success"])
        self.assertEqual((moved / "Blink.ino").read_text(encoding="utf-8"), OLD)
        folder = moved / saved.relative_to(path.parent)
        self.assertEqual({item.name: item.read_bytes() for item in folder.iterdir()}, before, "Saved sessions are read only")
        manager.delete_ai_changes()
        self.assertEqual(len(manager.list_ai_sessions()), 1, "Clearing cards keeps saved sessions")

    def test_large_cards_use_the_complete_saved_text(self):
        before = "".join(f"int value{i} = {i};\n" for i in range(9000))
        after = before.replace("int value8999 = 8999;", "int value8999 = 1;")
        path, card = self.edit(before=before, after=after)
        self.assertTrue(self.manager.get_ai_changes()[-1]["previewTruncated"])
        self.assertEqual(self.plan(card)["files"][0]["result"], before)
        self.manager.shutdown()
        manager = self.open()
        self.assertEqual(self.plan(card, manager=manager)["files"][0]["result"], before)

    def test_archive_ids_and_records_cannot_leave_the_sketch(self):
        root = self.manager._history_root()
        for edit_id in ("archive:../x/session1/edit1.txt", "archive:11-25-26/session1/../../x.txt", "archive:11-25-26/session1/session.txt",
                        "archive:/etc/passwd", "archive:"):
            with self.assertRaises(ValueError):
                ai_revert.read_edit(root, edit_id)
        project = self.root
        for record, expected in (
            ({"path": str(project / "a.ino"), "project": str(project)}, project / "a.ino"),
            ({"path": r"D:\Old\Blink\sub\a.cpp", "project": r"D:\Old\Blink"}, project / "sub/a.cpp"),
            ({"path": r"D:\Old\Blink\..\x.ino", "project": r"D:\Old\Blink"}, None),
            ({"path": "/etc/passwd", "project": ""}, None), ({"path": "", "project": ""}, None)):
            self.assertEqual(ai_revert.project_path(record, project), expected, record)
        for target in (project / ".mcu_flasher_build_cache/x.ino", project / "notes.md", project.parent / "x.ino", None):
            self.assertFalse(self.manager._revert_target_allowed(target) if target is None or project in target.parents else False)
        linked = root / "1-1-26/session1"
        linked.mkdir(parents=True)
        outside = self.root / "secret.txt"
        outside.write_text("x", encoding="utf-8")
        try:
            (linked / "edit1.txt").symlink_to(outside)
        except OSError:
            return
        with self.assertRaises(ValueError):
            ai_revert.read_edit(root, "archive:1-1-26/session1/edit1.txt")
        self.assertFalse([item for item in self.manager.list_ai_sessions() if item["id"] == "1-1-26/session1"])

    def test_record_text_round_trips_exactly(self):
        store = self.manager._ai_backup_store
        before = "line\r\n<<< BEFORE CONTENT >>>\n\n"
        record = {"editNumber": 1, "reviewId": "r", "path": str(self.root / "a.ino"), "project": str(self.root),
                  "beforeContent": before, "afterContent": "", "beforeExists": True, "afterExists": False, "createdAt": "2026-11-25T10:00:00+08:00"}
        parsed = ai_revert.parse_edit_record(store._format_record(record))
        self.assertEqual((parsed["beforeContent"], parsed["content"], parsed["afterExists"]), (before, "", False))
        with self.assertRaises(ValueError):
            ai_revert.parse_edit_record("Review-ID: r\n")


class BridgeChecks(Fixture):
    def backend(self, **state):
        from main.web_bridge import MCUWebBackendAPI
        with patch("main.core.ai_review.threading.Thread"):
            watcher = AIEditWatcher(self.root, self.manager)
        watcher._scan_running = False
        self.addCleanup(delete_on_gui_thread, watcher)  # Runs after shutdown (cleanups are LIFO).
        self.addCleanup(watcher.shutdown)
        backend = SimpleNamespace(ai_review_manager=self.manager, ai_watcher=watcher, is_busy=False, modified_files={},
                                  emit=Mock(), _get_qt_signals=lambda: signals, **state)
        backend.revert_ai_edits = lambda plan: MCUWebBackendAPI.revert_ai_edits(backend, plan)
        return backend

    def test_busy_and_unsaved_files_refuse_before_any_write(self):
        path, card = self.edit()
        plan = self.plan(card)
        backend = self.backend()
        backend.is_busy = True
        self.assertIn("finish", backend.revert_ai_edits(plan)["error"])
        backend.is_busy = False
        backend.modified_files = {str(path): True}
        self.assertIn("unsaved", backend.revert_ai_edits(plan)["error"])
        self.assertEqual(path.read_text(encoding="utf-8"), NEW)
        backend.emit.assert_not_called()

    def test_a_revert_is_shown_and_never_proposed_as_a_new_ai_edit(self):
        path, card = self.edit()
        backend = self.backend()
        watcher, shown = backend.ai_watcher, []

        def scan():
            with watcher._lock:
                request = (watcher._generation, self.root, watcher._baseline_ready, dict(watcher._baseline_contents),
                           dict(watcher._baseline_mtimes), {key: dict(value) for key, value in watcher._pending_settle.items()},
                           dict(watcher._save_versions), True)
            watcher._finish_scan(watcher._scan_project(request))
        with patch("main.core.ai_review.threading.Thread"):
            scan()
            signals.ai_edits_reverted.connect(shown.append)
            self.addCleanup(signals.ai_edits_reverted.disconnect, shown.append)
            self.assertTrue(backend.revert_ai_edits(self.plan(card))["success"])
            for _ in range(3):
                scan()
        self.assertEqual(path.read_text(encoding="utf-8"), OLD)
        self.assertFalse(self.manager.has_any_pending_ai_edits())
        self.assertEqual(shown, [[{"path": str(path), "fileName": "Blink.ino", "exists": True, "shapeChanged": False}]])
        self.assertIn("Undo AI", backend.emit.call_args.args[1]["message"])


class PanelChecks(BridgeChecks):
    def panel(self, backend):
        panel = AIChangesPanel(backend)
        panel.resize(1000, 420)
        panel.show()
        self.addCleanup(QCoreApplication.sendPostedEvents, None, QEvent.Type.DeferredDelete)
        self.addCleanup(delete_on_gui_thread, panel)
        self.addCleanup(panel.close)
        return panel

    def test_edit_session_and_saved_session_can_each_be_chosen(self):
        self.edit(prompt="one")
        self.manager.shutdown()
        self.manager = self.open()
        self.edit("Other.cpp", "int a;\n", "int b;\n", "two")
        panel = self.panel(self.backend())
        self.assertEqual(panel._revert.text(), "Revert edit")
        self.assertTrue(panel._revert.isEnabled())
        self.assertEqual(panel._revert_targets()[1], "Revert this AI edit in Other.cpp")
        group = panel._tree.topLevelItem(0)
        self.assertFalse(group.flags() & Qt.ItemFlag.ItemIsSelectable, "A group row is never the selection")
        shown, chosen = panel._after.toPlainText(), panel._selected_id()
        panel._tree.setCurrentItem(group)
        self.assertEqual((panel._revert.text(), panel._after.toPlainText(), panel._selected_id()),
                         ("Revert edit", shown, chosen), "Moving onto a group keeps the file shown")
        rect = panel._tree.visualItemRect(group)
        QTest.mouseClick(panel._tree.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertFalse(group.isExpanded(), "One click closes a group")
        QTest.mouseClick(panel._tree.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        self.assertTrue(group.isExpanded(), "and the next opens it")
        menu = panel._session_menu(group)
        self.addCleanup(delete_on_gui_thread, menu)
        self.assertEqual([action.text() for action in menu.actions()], ["Revert session (1 edit)…"])
        opened = []
        with patch.object(panel, "_open_revert", side_effect=lambda ids, title: opened.append((ids, title))):
            menu.actions()[0].trigger()
        self.assertEqual(opened, [(group.data(0, Qt.ItemDataRole.UserRole + 1), panel._revert_targets(group)[1])])
        archive = panel._archive_item()
        self.assertIsNone(panel._session_menu(archive), "Earlier sessions has nothing to revert itself")
        archive.setExpanded(True)
        until(lambda: panel._archive is not None, "saved sessions")
        session = archive.child(0)
        self.assertIn("(1 edit)", session.text(0))
        self.assertFalse(session.flags() & Qt.ItemFlag.ItemIsSelectable)
        session_menu = panel._session_menu(session)
        self.addCleanup(delete_on_gui_thread, session_menu)
        self.assertEqual([action.text() for action in session_menu.actions()], ["Revert session (1 edit)…"])
        panel._tree.setCurrentItem(session.child(0))
        until(lambda: panel._before.toPlainText() == OLD, "saved edit preview")
        self.assertEqual(panel._after.toPlainText(), NEW)
        panel._request_refresh()
        panel.refresh()
        self.assertTrue(panel._archive_item().isExpanded())
        panel.resize(420, 320)
        APP.processEvents()
        self.assertEqual(panel._revert.text(), "Revert")

    def test_dialog_previews_both_ways_and_applies_one(self):
        path, card = self.edit()
        path.write_text(NEW.replace("int led = 2;", "int led = 7;"), encoding="utf-8")
        backend = self.backend()
        dialog = AIRevertDialog(backend, [card], "Revert this AI edit in Blink.ino")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.addCleanup(delete_on_gui_thread, dialog)
        dialog.show()
        until(dialog._apply.isEnabled, "preview")
        self.assertIn("int led = 7;", dialog._then.toPlainText())
        self.assertIn("stay as they are", dialog._note.text())
        dialog._restore.setChecked(True)
        until(lambda: dialog._then.toPlainText() == OLD, "restore preview")
        self.assertIn("replaced", dialog._note.text())
        dialog._apply.click()
        until(lambda: not dialog.isVisible(), "applied")
        self.assertEqual(path.read_text(encoding="utf-8"), OLD)

    def test_a_dialog_deleted_while_its_preview_runs_is_never_called_back(self):
        import threading
        path, card = self.edit()
        backend = self.backend()
        release, started = threading.Event(), threading.Event()
        original = backend.ai_review_manager.plan_ai_revert

        def slow_plan(*args, **kwargs):
            started.set()
            release.wait(5)
            return original(*args, **kwargs)
        with patch.object(backend.ai_review_manager, "plan_ai_revert", side_effect=slow_plan):
            dialog = AIRevertDialog(backend, [card], "Revert")
            dialog.show()
            until(started.is_set, "preview started")
            finished = []
            dialog._planned.connect(lambda *args: finished.append(args))
            delete_on_gui_thread(dialog)
            release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                APP.processEvents()
                time.sleep(0.005)
        self.assertEqual(finished, [], "a deleted dialog receives nothing")
        self.assertEqual(path.read_text(encoding="utf-8"), NEW, "a preview never writes")

    def test_dialog_reports_a_newer_change_and_shows_it(self):
        path, card = self.edit()
        dialog = AIRevertDialog(self.backend(), [card], "Revert")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.addCleanup(delete_on_gui_thread, dialog)
        dialog.show()
        until(dialog._apply.isEnabled, "preview")
        path.write_text(NEW + "// newer\n", encoding="utf-8")
        dialog._apply.click()
        until(lambda: "// newer" in dialog._now.toPlainText(), "the newer text")
        self.assertTrue(dialog.isVisible())
        self.assertEqual(path.read_text(encoding="utf-8"), NEW + "// newer\n")
        dialog.close()


class HiddenStorageChecks(Fixture):
    def test_every_history_folder_and_file_is_hidden_on_windows(self):
        self.edit()
        self.manager.shutdown()
        flush_review_journals()
        hidden = []
        with patch.object(file_utils, "hide_generated_directory", side_effect=lambda path: hidden.append(Path(path))), \
                patch.object(file_utils.sys, "platform", "win32"):
            file_utils.hide_ai_history(self.manager._history_root())
        cache = self.root / ".mcu_flasher_build_cache"
        session = self.manager._ai_backup_store.session_dir
        self.assertEqual(set(hidden), {cache, cache / ".mcu_ai_edits", cache / ".mcu_ai_edits/.state", session.parent, session})
        written = []
        with patch.object(file_utils, "hide_hidden_attribute", side_effect=lambda path: written.append(Path(path).name)):
            self.manager._ai_backup_store._write_text_atomic(session / "edit9.txt", "x")
        self.assertEqual(written, ["edit9.txt"])
        from main.core import ai_review
        with patch.object(ai_review, "hide_hidden_attribute", side_effect=lambda path: written.append(Path(path).name)):
            AIReviewManager._write_journal_atomic(cache / ".mcu_ai_edits/.state/pending_reviews.json", {})
        self.assertEqual(written[-1], "pending_reviews.json")

    def test_history_stays_inside_the_sketch_in_hidden_folders_on_every_host(self):
        self.edit()
        session = self.manager._ai_backup_store.session_dir
        parts = session.relative_to(self.root).parts
        self.assertEqual(parts[:2], (".mcu_flasher_build_cache", ".mcu_ai_edits"))
        visible = [item.name for item in self.root.iterdir() if not item.name.startswith(".")]
        self.assertEqual(visible, ["Blink.ino"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
