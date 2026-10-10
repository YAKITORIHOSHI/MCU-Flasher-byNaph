#!/usr/bin/env python3
"""Hardware-free multi-project checks; all writes stay in owned temp fixtures."""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPoint, QRect, QTimer, QStandardPaths
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QMessageBox, QWidget
from main.core import config
from main.core.config_store import ConfigSnapshot
from main.qt.main_window import MCUMainWindow
from main.qt.project_dialog import ProjectDialog, QFileDialog
from main.qt.toolbar import PrimaryToolbar
from main.qt.theme import build_stylesheet, register_fonts
from main import web_bridge
from src.modules.ui_metrics import WorkArea

APP = QApplication.instance() or QApplication([])
RENDER_DIR = None
REAL_INSTANCE_IS_ALIVE = config._instance_is_alive

CHILD = '''
import sys, time, json
from pathlib import Path
from unittest.mock import patch
root, folder, label, operation = sys.argv[1:]
sys.path.insert(0, root)
from main.core import config
folder = Path(folder)
with patch.object(Path, "home", return_value=folder):
    config.LOCAL_GUI_CONFIG = folder / "portable.json"
    config._CONFIG_MEM_SIGNATURE = None
    snapshot = config._load_raw_config()
    (folder / (label + ".ready")).write_text("ready")
    deadline = time.monotonic() + 10
    while not (folder / "go").exists():
        if time.monotonic() > deadline: raise TimeoutError("parent gate")
        time.sleep(.01)
    if operation == "save":
        snapshot.setdefault("instances", {})[config._INSTANCE_ID] = {"label": label}
        snapshot.setdefault("shared", {})[label] = True
        result = config._save_raw_config(snapshot)
    else:
        with patch.object(config, "_instance_is_alive", return_value=True):
            if operation == "project":
                result = config.set_active_sketch_dir(str(folder / "sketch"))
            else:
                result = config.claim_serial_port("COM999")
    (folder / (label + ".result")).write_text(json.dumps(result))
'''


class ProjectChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "temp/audit").mkdir(parents=True, exist_ok=True)
        self.fixture = tempfile.TemporaryDirectory(dir=ROOT / "temp/audit", prefix="projects-")
        self.addCleanup(self.fixture.cleanup)
        self.folder = Path(self.fixture.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(Path, "home", return_value=self.folder))
        self.stack.enter_context(patch.object(config, "LOCAL_GUI_CONFIG", self.folder / "portable.json"))
        self.stack.enter_context(patch.object(config, "_CONFIG_MEM_SIGNATURE", None))
        self.stack.enter_context(patch.object(config, "_CONFIG_MEM_CACHE", {}))
        self.stack.enter_context(patch.object(config, "_CONFIG_MEM_MTIME", 0))
        self.stack.enter_context(patch.object(config, "_INSTANCE_ID", "101"))
        self.stack.enter_context(patch.object(config, "_instance_is_alive", return_value=True))
        (self.folder / "sketch").mkdir()
        (self.folder / "sketch/demo.ino").write_text("void setup() {}\nvoid loop() {}\n")
        self.assertTrue(config._save_raw_config({"instances": {}, "shared": {}}))

    def backend(self):
        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        api.sketch_dir_path = self.folder / "original"
        api.current_board, api.current_port = "Original target", "COM888"
        api.active_file_path = str(api.sketch_dir_path / "unsaved.ino")
        api.modified_files = {api.active_file_path: True}
        api.is_busy, api.active_operation = True, "compile"
        api.emit = Mock()
        return api

    def test_startup_leaves_projects_unbound_and_never_creates_an_example(self):
        example = self.folder / "Documents" / "example"
        for saved_dir in ("", str(self.folder / "sketch"), str(example)):
            with self.subTest(saved_dir=saved_dir), \
                 patch.object(web_bridge, "load_gui_config", return_value={"last_sketch_dir": saved_dir}), \
                 patch.object(web_bridge, "get_reset_on_baud_change", return_value=False), \
                 patch.object(web_bridge, "get_project_remembered_board") as remembered:
                api = web_bridge.MCUWebBackendAPI()
                try:
                    self.assertIsNone(api.sketch_dir_path)
                    self.assertEqual(api.get_project_dir(), "")
                    self.assertEqual(api.get_project_files(), [])
                    self.assertEqual(api.get_initial_state()["project"], {
                        "path": "", "name": "", "files": [], "active_file": ""})
                    self.assertEqual(api.get_default_project_parent(), str(self.folder / "Documents"))
                    self.assertIsNone(api.ai_review_manager.project_dir)
                    self.assertIsNone(api.ai_watcher.project_dir)
                    self.assertFalse(api.ai_watcher._timer.isActive())
                    remembered.assert_not_called()
                    self.assertFalse(example.parent.exists())
                finally:
                    api.ai_watcher.shutdown()
                    api.ai_review_manager.shutdown()
                    api._serial_send_queue.stop()

        # Previously generated examples remain user-owned and selectable;
        # a remembered path must not silently bind or alter them at startup.
        example.mkdir(parents=True)
        source = example / "example.ino"
        source.write_text("// user changes\n", encoding="utf-8")
        with patch.object(web_bridge, "load_gui_config", return_value={"last_sketch_dir": str(example)}), \
             patch.object(web_bridge, "get_reset_on_baud_change", return_value=False):
            api = web_bridge.MCUWebBackendAPI()
            try:
                self.assertIsNone(api.sketch_dir_path)
                self.assertEqual(source.read_text(encoding="utf-8"), "// user changes\n")
                self.assertEqual(list(example.iterdir()), [source])
            finally:
                api.ai_watcher.shutdown()
                api.ai_review_manager.shutdown()
                api._serial_send_queue.stop()

    def test_startup_picker_uses_system_documents_even_when_an_example_exists(self):
        (self.folder / "Documents" / "example").mkdir(parents=True)
        for location, expected in ((str(self.folder / "Redirected Documents"),
                                    str(self.folder / "Redirected Documents")),
                                   ("", str(self.folder / "Documents"))):
            with self.subTest(location=location), \
                 patch.object(QStandardPaths, "writableLocation", return_value=location):
                dialog = ProjectDialog()
                try:
                    self.assertEqual(dialog._start_dir, expected)
                    self.assertEqual(dialog._open_path_edit.text(), expected)
                    self.assertIsNone(dialog.selected_project)
                finally:
                    dialog.close()
                    dialog.deleteLater()
                    APP.processEvents()

    def test_stale_settings_snapshot_preserves_other_window_and_nested_preferences(self):
        first = config._load_raw_config()
        stale = copy.deepcopy(first)
        first["instances"]["202"] = {"active_sketch_dir": "other", "selected_port": "COM2"}
        first["shared"]["theme_mode"] = "light"
        self.assertTrue(config._save_raw_config(first))
        stale["instances"]["101"] = {"active_sketch_dir": "current"}
        stale["shared"]["monitor_font_size"] = 14
        self.assertTrue(config._save_raw_config(stale))
        stale["shared"]["unwritten"] = True
        self.assertNotIn("unwritten", config._load_raw_config()["shared"])
        current = config._load_raw_config()
        self.assertIn("202", current["instances"])
        self.assertEqual(current["shared"], {"theme_mode": "light", "monitor_font_size": 14})
        current["shared"]["unsaved"] = True
        self.assertNotIn("unsaved", config._load_raw_config()["shared"])

    def test_project_and_port_claims_reject_second_owner_and_release_on_close(self):
        self.assertTrue(config.set_active_sketch_dir(str(self.folder / "sketch")))
        self.assertTrue(config.claim_serial_port("COM999"))
        with patch.object(config, "_INSTANCE_ID", "202"):
            self.assertFalse(config.set_active_sketch_dir(str(self.folder / "sketch")))
            self.assertFalse(config.claim_serial_port("COM999 - Fixture device"))
            self.assertTrue(config.set_active_sketch_dir(str(self.folder / "another")))
            self.assertTrue(config.claim_serial_port("COM998"))
        config.clean_instance_config("101")
        with patch.object(config, "_INSTANCE_ID", "202"):
            self.assertTrue(config.set_active_sketch_dir(str(self.folder / "sketch")))
            self.assertTrue(config.claim_serial_port("COM999"))

    def test_settings_and_theme_share_one_transaction_without_overwriting_other_windows(self):
        from main.core.theme import Theme
        self.assertTrue(config._save_raw_config({
            "instances": {"101": {"window_maximized": False}, "202": {"selected_port": "COM2"}},
            "shared": {"monitor_font_size": 14, "theme_mode": "default"},
        }))
        config.load_gui_config()
        api = self.backend()
        original_theme = Theme.active_theme
        self.addCleanup(Theme.apply_theme, original_theme)
        with patch.object(config, "_save_raw_config", wraps=config._save_raw_config) as write:
            result = api.save_settings({"window_maximized": True, "theme_mode": "light"})
        self.assertTrue(result["success"])
        write.assert_called_once()
        current = config._load_raw_config(fresh=True)
        self.assertTrue(current["instances"]["101"]["window_maximized"])
        self.assertEqual(current["instances"]["202"], {"selected_port": "COM2"})
        self.assertEqual(current["shared"]["monitor_font_size"], 14)
        self.assertEqual(current["shared"]["theme_mode"], "light")
        self.assertFalse(current["shared"]["theme_follow_system"])
        self.assertEqual(Theme.active_theme, "light")

    def test_failed_settings_transaction_preserves_theme_and_reports_failure(self):
        from main.core.theme import Theme
        config.load_gui_config()
        api = self.backend()
        original_theme = Theme.active_theme
        before = config._load_raw_config(fresh=True)
        with patch.object(config, "_save_raw_config", return_value=False) as write, \
             patch.object(Theme, "apply_theme") as apply:
            result = api.save_settings({"window_maximized": True, "theme_mode": "light"})
        self.assertFalse(result["success"])
        self.assertIn("could not be written", result["error"])
        write.assert_called_once()
        apply.assert_not_called()
        self.assertEqual(Theme.active_theme, original_theme)
        self.assertEqual(config._load_raw_config(fresh=True), before)
        api.emit.assert_called_once()
        self.assertEqual(api.emit.call_args.args[0], "notification")
        self.assertEqual(api.emit.call_args.args[1]["type"], "error")

    def test_notification_storage_failure_warns_once_without_recursion_and_recovers(self):
        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        bus = Mock()
        api._qt_signals, api._window = bus, None
        notice = {"type": "info", "title": "Original notice", "message": "Still visible"}
        with patch("src.dbs.dbs_create.add_notification", side_effect=[None, None, {"id": "stored"}, None]) as create:
            for _ in range(4):
                api.emit("notification", notice)
        self.assertEqual(create.call_count, 4, "Warnings must never persist recursively")
        delivered = [call.args[0] for call in bus.notification.emit.call_args_list]
        original_notices = [payload for payload in delivered if payload.get("title") == notice["title"]]
        self.assertEqual(len(original_notices), 4)
        self.assertNotIn("id", notice, "Dispatch must not mutate the caller's event")
        self.assertEqual(len({payload["id"] for payload in original_notices}), 4)
        for payload, call in zip(original_notices, create.call_args_list):
            self.assertEqual(payload["id"], call.kwargs["notification_id"])
        warnings = [payload for payload in delivered if payload.get("type") == "warning"]
        self.assertEqual(len(warnings), 2, "Warn once per outage, rearm after a successful write")
        self.assertIn("Activity history", warnings[0]["title"])

    def simultaneous(self, operation):
        processes = []
        try:
            for label in ("one", "two"):
                processes.append(subprocess.Popen(
                    [sys.executable, "-B", "-c", CHILD, str(ROOT), str(self.folder), label, operation],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0))
            deadline = time.monotonic() + 10
            while not all((self.folder / (label + ".ready")).exists() for label in ("one", "two")):
                self.assertLess(time.monotonic(), deadline, "child readiness timed out")
                time.sleep(.01)
            (self.folder / "go").write_text("go")
            for child in processes:
                out, err = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, (out + err).decode(errors="replace"))
            return [json.loads((self.folder / (label + ".result")).read_text()) for label in ("one", "two")]
        finally:
            for child in processes:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    def test_simultaneous_process_writes_retain_both_registrations(self):
        self.assertEqual(self.simultaneous("save"), [True, True])
        data = config._load_raw_config(fresh=True)
        self.assertEqual(len(data["instances"]), 2)
        self.assertTrue(data["shared"]["one"] and data["shared"]["two"])

    def test_first_launch_concurrent_writes_preserve_new_instance_tables(self):
        self.assertTrue(config._save_raw_config({}))
        self.assertEqual(self.simultaneous("save"), [True, True])
        data = config._load_raw_config(fresh=True)
        self.assertEqual(len(data["instances"]), 2)
        self.assertTrue(data["shared"]["one"] and data["shared"]["two"])

    def test_simultaneous_project_claim_has_exactly_one_owner(self):
        self.assertEqual(sorted(self.simultaneous("project")), [False, True])

    def test_simultaneous_port_claim_has_exactly_one_owner(self):
        self.assertEqual(sorted(self.simultaneous("port")), [False, True])

    def test_new_window_open_is_rejected_while_action_running(self):
        api = self.backend()
        before = (api.sketch_dir_path, api.active_file_path, api.current_board, api.current_port, api.modified_files.copy())
        child = Mock(pid=303)
        child.poll.return_value = None
        with patch.object(web_bridge, "find_project_window", return_value=None), \
             patch.object(web_bridge.subprocess, "Popen", return_value=child) as spawn, \
             patch("src.modules.private_python_guard.is_running_private_python", return_value=True), \
             patch.object(QTimer, "singleShot"):
            result = api.open_project_window(str(self.folder / "sketch/demo.ino"))
        self.assertFalse(result["success"])
        self.assertIn("action is in progress", result["error"])
        spawn.assert_not_called()
        self.assertEqual(before, (api.sketch_dir_path, api.active_file_path, api.current_board, api.current_port, api.modified_files))

    def test_project_selector_entrypoints_reject_active_actions(self):
        api = self.backend()
        self.assertFalse(api.open_project(str(self.folder / "sketch"))["success"])
        self.assertFalse(api.open_project_window(str(self.folder / "sketch"))["success"])
        self.assertEqual(api.open_project_picker(), "")
        result = api.create_project(str(self.folder), "blocked", open_in_new_window=True)
        self.assertFalse(result["success"])
        self.assertFalse((self.folder / "blocked").exists())

    def test_existing_project_focuses_without_launch_and_failed_child_is_reported(self):
        api = self.backend()
        api.is_busy, api.active_operation = False, None
        with patch.object(web_bridge, "find_project_window", return_value={"pid": 202, "hwnd": 1}), \
             patch.object(web_bridge, "focus_project_window", return_value=True), \
             patch.object(web_bridge.subprocess, "Popen") as spawn:
            self.assertTrue(api.open_project_window(str(self.folder / "sketch"))["success"])
            spawn.assert_not_called()
        child = Mock()
        child.poll.return_value = 1
        api._project_window_processes = [child]
        api._check_project_window_startup()
        self.assertEqual(api.emit.call_args.args[1]["type"], "error")
        self.assertEqual(api._project_window_processes, [])

    def test_lost_project_claim_returns_before_source_or_metadata_changes(self):
        api = self.backend()
        api.is_busy, api.active_operation = False, None
        api.get_project_files = Mock()
        sketch = self.folder / "sketch/demo.ino"
        before = sketch.read_bytes()
        with patch.object(web_bridge, "find_project_window", side_effect=[None, {"pid": 202}]), \
             patch.object(web_bridge, "set_active_sketch_dir", return_value=False), \
             patch.object(web_bridge, "focus_project_window"), \
             patch.object(web_bridge, "hide_internal_project_metadata") as metadata:
            result = api.open_project(str(sketch.parent))
        self.assertFalse(result["success"])
        self.assertTrue(result["already_open"])
        metadata.assert_not_called()
        api.get_project_files.assert_not_called()
        self.assertEqual(sketch.read_bytes(), before)
        self.assertEqual(api.sketch_dir_path, self.folder / "original")

    def test_invalid_existing_project_never_claims_or_scaffolds(self):
        api = self.backend()
        api.is_busy, api.active_operation = False, None
        api.get_project_files = Mock()
        documents = self.folder / "Documents"
        documents.mkdir()
        document_source = documents / "unrelated.ino"
        document_source.write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
        empty = self.folder / "empty-project"
        empty.mkdir()
        empty_source = empty / "empty.ino"
        empty_source.write_text(" \n\t", encoding="utf-8")
        before = (api.sketch_dir_path, api.active_file_path, api.modified_files.copy())

        with patch.object(web_bridge, "_system_documents_directory", return_value=documents), \
             patch.object(web_bridge, "find_project_window") as find_owner, \
             patch.object(web_bridge, "set_active_sketch_dir") as claim, \
             patch.object(web_bridge, "add_recent_project") as add_recent, \
             patch.object(web_bridge, "hide_internal_project_metadata") as metadata, \
             patch.object(web_bridge.subprocess, "Popen") as spawn:
            documents_result = api.open_project(str(documents))
            current_result = api.open_project(str(empty))
            new_window_result = api.open_project_window(str(empty))

        self.assertFalse(documents_result["success"])
        self.assertIn("inside Documents", documents_result["error"])
        self.assertFalse(current_result["success"])
        self.assertIn("non-empty .ino", current_result["error"])
        self.assertFalse(new_window_result["success"])
        self.assertIn("non-empty .ino", new_window_result["error"])
        self.assertEqual(document_source.read_text(encoding="utf-8"), "void setup() {}\nvoid loop() {}\n")
        self.assertEqual(empty_source.read_text(encoding="utf-8"), " \n\t")
        self.assertEqual(sorted(item.name for item in empty.iterdir()), ["empty.ino"])
        self.assertEqual(before, (api.sketch_dir_path, api.active_file_path, api.modified_files))
        find_owner.assert_not_called()
        claim.assert_not_called()
        add_recent.assert_not_called()
        metadata.assert_not_called()
        api.get_project_files.assert_not_called()
        spawn.assert_not_called()

    def test_picker_rejects_invalid_project_before_window_choice_or_dirty_prompt(self):
        api = self.backend()
        api.is_busy, api.active_operation = False, None
        api.get_recent_projects = Mock(return_value=[])
        invalid = self.folder / "not-a-project"
        invalid.mkdir()
        parent = QWidget()
        dialog = ProjectDialog(api, initial_dir=str(self.folder / "sketch"), parent=parent, open_in_new_window=True)
        try:
            dialog._open_path_edit.setText(str(invalid))
            with patch.object(dialog, "_choose_project_window") as choose_window, \
                 patch.object(dialog, "_prepare_current_window_switch") as dirty_prompt:
                dialog._open_existing()
            choose_window.assert_not_called()
            dirty_prompt.assert_not_called()
            self.assertIn("non-empty .ino", dialog._existing_status.text())
            self.assertIsNone(dialog.selected_project)
        finally:
            dialog.close()
            dialog.deleteLater()
            parent.deleteLater()
            APP.processEvents()

    def test_new_project_creation_and_open_are_blocked_while_busy(self):
        api = self.backend()
        api.open_project_window = Mock(return_value={"success": True})
        with patch.object(web_bridge, "ensure_hidden_read_first_md"):
            result = api.create_project(str(self.folder), "new", include_h=True, open_in_new_window=True)
            self.assertFalse(result["success"])
            self.assertIn("action is in progress", result["error"])
            self.assertFalse((self.folder / "new").exists())
            api.open_project_window.assert_not_called()

            api.is_busy, api.active_operation = False, None
            result = api.create_project(str(self.folder), "new", include_h=True, open_in_new_window=True)
            self.assertTrue(result["success"])
            api.open_project_window.assert_called_once_with(str(self.folder / "new"))
            sketch = self.folder / "new/new.ino"
            sketch.write_text("user edits")
            self.assertFalse(api.create_project(str(self.folder), "new", open_in_new_window=True)["success"])
            self.assertEqual(sketch.read_text(), "user edits")

    def test_reused_process_id_does_not_restore_old_port_or_project_ownership(self):
        self.assertTrue(config._save_raw_config({"instances": {"101": {
            "create_time": 1, "selected_port": "COM999", "active_sketch_dir": "old"}}, "shared": {}}))
        with patch.object(config, "_own_create_time", return_value=100):
            settings = config.load_gui_config()
        self.assertFalse(settings.get("selected_port"))
        self.assertFalse(settings.get("active_sketch_dir"))
        self.assertEqual(settings["create_time"], 100)

    def test_new_window_missing_from_cached_inventory_is_still_considered_alive(self):
        import psutil
        # Use the real predicate rather than this fixture's ownership stub.
        with patch.object(psutil, "Process") as process:
            process.return_value.create_time.return_value = 123
            self.assertTrue(REAL_INSTANCE_IS_ALIVE("202", {"create_time": 123}, {"101": 100}))
            self.assertFalse(REAL_INSTANCE_IS_ALIVE("202", {"create_time": 1}, {"101": 100}))
            process.side_effect = psutil.NoSuchProcess(202)
            self.assertFalse(REAL_INSTANCE_IS_ALIVE("202", {"create_time": 123}, {"101": 100}))

    def test_picker_routes_new_window_choice_and_lists_running_projects_in_all_themes(self):
        api = self.backend()
        api.is_busy, api.active_operation = False, None
        api.get_recent_projects = Mock(return_value=[])
        api.open_project_window = Mock(return_value={"success": True})
        api.open_project = Mock()
        config.set_active_sketch_dir(str(self.folder / "sketch"))
        parent = QWidget()
        dialog = ProjectDialog(api, initial_dir=str(self.folder / "sketch"), parent=parent, open_in_new_window=True)
        try:
            self.assertFalse(dialog._is_busy())
            self.assertEqual(dialog._open_projects_list.count(), 1)
            dialog._tabs.setCurrentIndex(3)
            for theme in ("default", "light", "solarized_dark"):
                APP.setStyleSheet(build_stylesheet(theme))
                dialog._apply_dialog_theme(theme)
                dialog.show()
                APP.processEvents()
                if RENDER_DIR:
                    self.assertTrue(dialog.grab().save(str(RENDER_DIR / ("project-picker-" + theme + ".png"))))
            with patch.object(dialog, "_choose_project_window", return_value=True):
                dialog._open_existing()
            api.open_project_window.assert_called_once()
            api.open_project.assert_not_called()
        finally:
            dialog.close()
            dialog.deleteLater()
            parent.deleteLater()
            APP.processEvents()

    def test_busy_project_picker_and_toolbar_are_blocked(self):
        api = self.backend()
        parent = QMainWindow()
        dialog = ProjectDialog(api, initial_dir=str(self.folder / "sketch"), parent=parent,
                               open_in_new_window=True)
        try:
            self.assertTrue(dialog._is_busy())
            with patch.object(QMessageBox, "warning") as warning:
                self.assertEqual(dialog.exec(), QDialog.DialogCode.Rejected)
            warning.assert_called_once()
            self.assertIsNone(dialog._choose_project_window("blocked"))

            toolbar = PrimaryToolbar(api, parent)
            toolbar.on_operation_phase({"phase": "reset", "is_busy": True,
                                        "can_stop": False, "op": "hard_reset"})
            self.assertFalse(toolbar.btn_project.isEnabled())
            self.assertFalse(toolbar.lbl_sketch_icon.isEnabled())
            self.assertFalse(toolbar.lbl_sketch.isEnabled())
            with patch("main.qt.project_dialog.ProjectDialog") as picker:
                toolbar._on_new_project()
                picker.assert_not_called()
        finally:
            dialog.close()
            dialog.deleteLater()
            parent.deleteLater()
            APP.processEvents()

    def test_ctrl_o_does_not_construct_picker_while_action_running(self):
        window = SimpleNamespace(_is_busy=lambda: True, _backend=self.backend())
        with patch("main.qt.project_dialog.ProjectDialog") as picker:
            MCUMainWindow._shortcut_open_project(window)
        picker.assert_not_called()

    def test_picker_routes_current_window_choice_after_switch_guard(self):
        api = self.backend()
        api.is_busy, api.active_operation = False, None
        api.modified_files.clear()
        api.get_recent_projects = Mock(return_value=[])
        api.open_project_window = Mock(return_value={"success": True})
        api.open_project = Mock(return_value={"success": True})
        parent = QWidget()
        dialog = ProjectDialog(api, initial_dir=str(self.folder / "sketch"), parent=parent, open_in_new_window=True)
        guarded_targets = []
        try:
            dialog._prepare_current_window_switch = lambda target, done, on_wait=None: (guarded_targets.append(target), done(True, "", False))
            with patch.object(dialog, "_choose_project_window", return_value=False):
                dialog._open_existing()
            self.assertEqual(guarded_targets, [str(self.folder / "sketch")])
            api.open_project.assert_called_once_with(str(self.folder / "sketch"))
            api.open_project_window.assert_not_called()
        finally:
            dialog.close()
            dialog.deleteLater()
            parent.deleteLater()
            APP.processEvents()

    def test_sketch_browse_always_starts_in_system_documents_and_cancel_preserves_selection(self):
        api = self.backend()
        api.get_recent_projects = Mock(return_value=[])
        api.open_project = Mock()
        api.open_project_window = Mock()
        before = (api.sketch_dir_path, api.active_file_path, api.modified_files.copy())
        documents = self.folder / "redirected" / "Documents with spaces"
        documents.mkdir(parents=True)
        chosen_folder = self.folder / "chosen"
        chosen_folder.mkdir()
        sketch = chosen_folder / "chosen.ino"
        sketch.write_text("void setup() {}\nvoid loop() {}\n")
        dialog = ProjectDialog(api, initial_dir=str(self.folder / "sketch"), open_in_new_window=True)
        try:
            with patch.object(QStandardPaths, "writableLocation", return_value=str(documents)) as location, \
                 patch.object(QFileDialog, "getOpenFileName", side_effect=[(str(sketch), ""), ("", ""), ("", "")]) as picker:
                dialog._btn_browse.click()
                self.assertEqual(dialog._open_path_edit.text(), str(chosen_folder))
                dialog._btn_browse.click()
                self.assertEqual(dialog._open_path_edit.text(), str(chosen_folder))
                dialog._open_path_edit.setText(str(self.folder / "typed-folder"))
                dialog._btn_browse.click()
                self.assertEqual(dialog._open_path_edit.text(), str(self.folder / "typed-folder"))
            self.assertEqual(picker.call_count, 3)
            for call in picker.call_args_list:
                self.assertEqual(call.args[2], str(documents))
            for call in location.call_args_list:
                self.assertEqual(call.args, (QStandardPaths.StandardLocation.DocumentsLocation,))
            self.assertEqual(location.call_count, 3)
            self.assertIsNone(dialog.selected_project)
            api.open_project.assert_not_called()
            api.open_project_window.assert_not_called()
            self.assertEqual(before, (api.sketch_dir_path, api.active_file_path, api.modified_files))
        finally:
            dialog.close()
            dialog.deleteLater()
            APP.processEvents()

    def test_startup_sketch_browse_uses_documents_and_empty_os_location_has_fallback(self):
        dialog = ProjectDialog(initial_dir=str(self.folder / "sketch"))
        try:
            for location, expected in ((str(self.folder / "Localized Documents"), str(self.folder / "Localized Documents")),
                                       ("", str(self.folder / "Documents"))):
                with self.subTest(location=location), \
                     patch.object(QStandardPaths, "writableLocation", return_value=location), \
                     patch.object(QFileDialog, "getOpenFileName", return_value=("", "")) as picker:
                    dialog._btn_browse.click()
                    self.assertEqual(picker.call_args.args[2], expected)
                    self.assertEqual(dialog._open_path_edit.text(), str(self.folder / "sketch"))
                    self.assertIsNone(dialog.selected_project)
        finally:
            dialog.close()
            dialog.deleteLater()
            APP.processEvents()

    def test_new_project_honors_selected_window(self):
        api = self.backend()
        api.is_busy, api.active_operation = False, None
        api.modified_files.clear()
        api.get_recent_projects = Mock(return_value=[])
        api.create_project = Mock(return_value={"success": True})
        parent = QWidget()
        dialog = ProjectDialog(api, parent=parent, open_in_new_window=True)
        dialog._new_parent_edit.setText(str(self.folder))
        dialog._new_name_edit.setText("new_project")
        try:
            with patch.object(dialog, "_choose_project_window", return_value=True):
                dialog._create_project()
            api.create_project.assert_called_once()
            self.assertTrue(api.create_project.call_args.kwargs["open_in_new_window"])
        finally:
            dialog.close()
            dialog.deleteLater()
            parent.deleteLater()
            APP.processEvents()

    def test_destination_prompt_exposes_current_new_and_cancel_choices(self):
        api = self.backend()
        api.is_busy, api.active_operation = False, None
        api.get_recent_projects = Mock(return_value=[])
        parent = QWidget()
        dialog = ProjectDialog(api, parent=parent, open_in_new_window=True)
        try:
            def choose_current():
                box = next(widget for widget in APP.topLevelWidgets() if isinstance(widget, QMessageBox))
                labels = {button.text() for button in box.buttons()}
                self.assertTrue({"This window", "New window", "Cancel"}.issubset(labels))
                if RENDER_DIR:
                    self.assertTrue(box.grab().save(str(RENDER_DIR / "project-window-choice.png")))
                next(button for button in box.buttons() if button.text() == "This window").click()

            QTimer.singleShot(0, choose_current)
            self.assertFalse(dialog._choose_project_window("demo"))
        finally:
            dialog.close()
            dialog.deleteLater()
            parent.deleteLater()
            APP.processEvents()

    def test_glass_picker_pages_fit_and_preserve_inputs_through_themes_and_resizing(self):
        api = self.backend()
        api.get_recent_projects = Mock(return_value=[str(self.folder / "sketch")])
        api.get_default_project_parent = Mock(return_value=str(self.folder))
        config.set_active_sketch_dir(str(self.folder / "sketch"))
        dialog = ProjectDialog(api, initial_dir=str(self.folder / "sketch"), open_in_new_window=True)
        dialog._new_name_edit.setText("SensorLogger")
        dialog._cb_include_h.setChecked(True)
        dialog._cb_include_cpp.setChecked(True)
        dialog._template_combo.setCurrentIndex(2)
        expected = (dialog._open_path_edit.text(), dialog._new_name_edit.text(),
                    dialog._new_parent_edit.text(), dialog._new_preview_lbl.text(),
                    dialog._template_combo.currentData(), dialog._cb_include_h.isChecked(),
                    dialog._cb_include_cpp.isChecked())
        actions = ((dialog._btn_cancel_existing, dialog._btn_open_existing),
                   (dialog._btn_cancel_new, dialog._btn_create),
                   (dialog._btn_clear_recents, dialog._btn_cancel_recent, dialog._btn_open_recent),
                   (dialog._btn_refresh_projects, dialog._btn_cancel_open, dialog._focus_project_btn))
        pages = ("existing", "new", "recent", "open")
        try:
            with patch("main.qt.responsive.work_area", return_value=WorkArea(0, 0, 1920, 1080)):
                dialog.show()
                APP.processEvents()
                for theme in ("default", "light", "solarized_dark"):
                    APP.setStyleSheet(build_stylesheet(theme))
                    dialog._apply_dialog_theme(theme)
                    self.assertIsNotNone(dialog._header_card._colors)
                    scale = APP.primaryScreen().devicePixelRatio()
                    max_width, max_height = int(1920 / scale) - 32, int(1080 / scale) - 70
                    for size_name, width, height in (("wide", min(1000, max_width), min(650, max_height)),
                                                    ("regular", min(720, max_width), min(560, max_height)),
                                                    ("compact", 400, 360), ("short", 360, 300)):
                        dialog.resize(width, height)
                        for page, buttons in enumerate(actions):
                            dialog._tabs.setCurrentIndex(page)
                            APP.processEvents()
                            self.assertEqual(dialog.width(), width)
                            self.assertEqual(dialog.height(), height)
                            for button in buttons:
                                self.assertTrue(button.isVisible())
                                rect = QRect(button.mapTo(dialog, QPoint()), button.size())
                                self.assertTrue(dialog.rect().contains(rect), f"{size_name} {pages[page]} {rect}")
                                ancestor = button.parentWidget()
                                while ancestor is not None and ancestor is not dialog:
                                    visible = QRect(button.mapTo(ancestor, QPoint()), button.size())
                                    self.assertTrue(ancestor.rect().contains(visible),
                                                    f"{button.text()} clipped by {type(ancestor).__name__}")
                                    ancestor = ancestor.parentWidget()
                                ink_width = button.fontMetrics().horizontalAdvance(button.text())
                                self.assertGreaterEqual(button.width(), ink_width + 20)
                            fields = ((dialog._open_path_edit,) if page == 0 else
                                      (dialog._new_name_edit, dialog._new_parent_edit, dialog._template_combo)
                                      if page == 1 else ())
                            for field in fields:
                                self.assertGreaterEqual(field.height(), field.fontMetrics().height() + 14,
                                                        f'{size_name} {pages[page]} clipped input text')
                            if RENDER_DIR:
                                name = f"project-{theme}-{size_name}-{pages[page]}.png"
                                self.assertTrue(dialog.grab().save(str(RENDER_DIR / name)))
                    current = (dialog._open_path_edit.text(), dialog._new_name_edit.text(),
                               dialog._new_parent_edit.text(), dialog._new_preview_lbl.text(),
                               dialog._template_combo.currentData(), dialog._cb_include_h.isChecked(),
                               dialog._cb_include_cpp.isChecked())
                    self.assertEqual(current, expected)
                dialog._tabs.setCurrentIndex(0)
                dialog._open_path_edit.setFocus()
                dialog._apply_dialog_theme("default")
                APP.processEvents()
                self.assertIs(dialog.focusWidget(), dialog._open_path_edit)
                self.assertEqual(dialog._recent_list.currentRow(), 0)
                dialog.reject()
                dialog.reject()
                self.assertIsNone(dialog.selected_project)
        finally:
            dialog.close()
            dialog.deleteLater()
            APP.processEvents()

    def test_current_window_save_choice_waits_for_save_ack_before_continue(self):
        from types import SimpleNamespace

        api = self.backend()
        api.is_busy, api.active_operation = False, None
        api.get_recent_projects = Mock(return_value=[])
        parent = QWidget()
        parent._active_operation = None
        save_all = Mock(side_effect=lambda callback, failure_callback: callback())
        parent._editor_panel = SimpleNamespace(trigger_save_all=save_all)
        dialog = ProjectDialog(api, parent=parent, open_in_new_window=True)
        outcomes, waiting = [], []
        try:
            def choose_save():
                box = next(widget for widget in APP.topLevelWidgets() if isinstance(widget, QMessageBox))
                labels = {button.text() for button in box.buttons()}
                self.assertIn("Save All", labels)
                self.assertIn("Discard", labels)
                if RENDER_DIR:
                    self.assertTrue(box.grab().save(str(RENDER_DIR / "project-dirty-choice.png")))
                next(button for button in box.buttons() if button.text() == "Save All").click()

            QTimer.singleShot(0, choose_save)
            dialog._prepare_current_window_switch(
                str(self.folder / "sketch"),
                lambda ready, error, cancelled: outcomes.append((ready, error, cancelled)),
                on_wait=lambda: waiting.append(True),
            )
            self.assertEqual(outcomes, [(True, "", False)])
            self.assertEqual(waiting, [True])
            save_all.assert_called_once()
        finally:
            dialog.close()
            dialog.deleteLater()
            parent.deleteLater()
            APP.processEvents()

    def test_glass_project_prompts_cancel_safely_in_all_themes(self):
        api = self.backend()
        api.is_busy, api.active_operation = False, None
        api.get_recent_projects = Mock(return_value=[])
        parent = QWidget()
        parent._active_operation = None
        parent._editor_panel = SimpleNamespace(trigger_save_all=Mock())
        dialog = ProjectDialog(api, parent=parent, open_in_new_window=True)
        before = (api.sketch_dir_path, api.modified_files.copy(), api.current_board, api.current_port)
        try:
            for theme in ('default', 'light', 'solarized_dark'):
                APP.setStyleSheet(build_stylesheet(theme))
                dialog._apply_dialog_theme(theme)

                def cancel_prompt(name):
                    prompt = next(widget for widget in APP.topLevelWidgets()
                                  if isinstance(widget, QMessageBox) and widget.isVisible())
                    self.assertEqual(prompt._glass._colors['BG_DARKEST'], dialog._dialog_palette['BG_DARKEST'])
                    if RENDER_DIR:
                        self.assertTrue(prompt.grab().save(str(RENDER_DIR / f'project-{theme}-{name}.png')))
                    next(button for button in prompt.buttons() if button.text() == 'Cancel').click()

                QTimer.singleShot(0, lambda: cancel_prompt('window-choice'))
                self.assertIsNone(dialog._choose_project_window('SensorLogger'))
                outcomes = []
                QTimer.singleShot(0, lambda: cancel_prompt('unsaved-choice'))
                dialog._prepare_current_window_switch(
                    str(self.folder / 'sketch'),
                    lambda ready, error, cancelled: outcomes.append((ready, error, cancelled)),
                )
                self.assertEqual(outcomes, [(False, '', True)])
                parent._editor_panel.trigger_save_all.assert_not_called()
                self.assertEqual(before, (api.sketch_dir_path, api.modified_files,
                                         api.current_board, api.current_port))
        finally:
            dialog.close()
            dialog.deleteLater()
            parent.deleteLater()
            APP.processEvents()

    def test_picker_foreground_retries_do_not_reopen_cancelled_picker_or_focus_over_prompt(self):
        dialog = ProjectDialog(initial_dir=str(self.folder / 'sketch'))
        try:
            with patch('main.qt.project_dialog.sys.platform', 'win32'), \
                 patch.object(config, 'focus_project_window') as focus:
                dialog.show()
                APP.processEvents()
                self.assertTrue(any(timer.isActive() for timer in dialog._foreground_timers))
                dialog.reject()
                self.assertFalse(any(timer.isActive() for timer in dialog._foreground_timers))
                focus.reset_mock()
                dialog._restore_foreground_focus()
                focus.assert_not_called()
                dialog.show()
                APP.processEvents()
                focus.reset_mock()
                with patch.object(QApplication, 'activeModalWidget', return_value=QWidget()):
                    dialog._restore_foreground_focus()
                focus.assert_not_called()
                with patch.object(QApplication, 'activeModalWidget', return_value=dialog):
                    dialog._restore_foreground_focus()
                focus.assert_called_once()
        finally:
            dialog.close()
            dialog.deleteLater()
            APP.processEvents()

    def test_half_screen_minimum_allows_wider_resize_and_restores_maximization(self):
        class Window(MCUMainWindow):
            def __init__(self):
                QMainWindow.__init__(self)
                self._layout_timer = QTimer(self)

            def showEvent(self, event):
                QMainWindow.showEvent(self, event)

            def closeEvent(self, event):
                QMainWindow.closeEvent(self, event)

            _minimum_width_for_display = staticmethod(MCUMainWindow._minimum_width_for_display)
            _update_minimum_window_size = MCUMainWindow._update_minimum_window_size
            _calculate_optimal_geometry = MCUMainWindow._calculate_optimal_geometry
            _restore_geometry = MCUMainWindow._restore_geometry
            _get_active_screen = lambda self: None
            _apply_responsive_layout = lambda self, width: None
        window = Window()
        try:
            for width, height in ((1920, 1080), (1280, 720), (960, 540), (640, 480)):
                area = WorkArea(-width, 20, width, height)
                with patch("main.qt.main_window.work_area", return_value=area):
                    window._update_minimum_window_size()
                    self.assertEqual(window.minimumWidth(), width // 2)
                    window.resize(width // 4, 400)
                    self.assertEqual(window.width(), width // 2)
                    window.resize(width * 3 // 4, 400)
                    self.assertEqual(window.width(), width * 3 // 4)
                    self.assertLessEqual(window.minimumWidth(), window.maximumWidth())
                    self.assertGreaterEqual(window._calculate_optimal_geometry().width(), width // 2)
            screen = SimpleNamespace(availableGeometry=lambda: QRect(-1920, 20, 1920, 1080))
            settings = {"window_geometry": [-1800, 30, 1800, 900], "window_maximized": False}
            window._backend = SimpleNamespace(get_settings=lambda: settings)
            with patch("main.qt.main_window.work_area", return_value=WorkArea(-1920, 20, 1920, 1080)), \
                 patch("main.qt.responsive.work_area", return_value=WorkArea(-1920, 20, 1920, 1080)), \
                 patch("main.qt.main_window.QGuiApplication.screens", return_value=[screen]):
                window._restore_geometry()
                self.assertEqual(window.minimumWidth(), 960)
                self.assertEqual(window.width(), 1800)
                self.assertFalse(window.isMaximized())
                settings["window_maximized"] = True
                window._restore_geometry()
                APP.processEvents()
                self.assertTrue(window.isMaximized())
                window.showNormal()
                APP.processEvents()
                self.assertFalse(window.isMaximized())
        finally:
            window.close()
            window.deleteLater()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-dir", type=Path)
    args = parser.parse_args()
    RENDER_DIR = args.render_dir
    if RENDER_DIR:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    register_fonts()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ProjectChecks))
    raise SystemExit(0 if result.wasSuccessful() else 1)
