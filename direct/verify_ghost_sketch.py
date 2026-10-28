#!/usr/bin/env python3
"""Ghost Sketch lifecycle checks using owned source trees and mocked persistence."""
from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from contextlib import ExitStack
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.dont_write_bytecode = True


class GhostSketchChecks(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("main.core.ghost_sketch"),
                             "Ghost Sketch lifecycle has not been implemented")
        self.ghost = importlib.import_module("main.core.ghost_sketch")
        (ROOT / "temp/audit").mkdir(parents=True, exist_ok=True)
        self.sandbox = tempfile.TemporaryDirectory(prefix="ghost-sketch-", dir=ROOT / "temp/audit")
        self.addCleanup(self.sandbox.cleanup)
        self.root = Path(self.sandbox.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(self.ghost, "_ghost_store_root", return_value=self.root / "owned"))

    def create(self):
        return self.ghost.create_ghost_project_path()

    def test_new_ghosts_are_independent_and_recognizable_across_store_instances(self):
        first, second = self.create(), self.create()
        self.assertNotEqual(first, second)
        self.assertEqual(sorted(p.name for p in first.iterdir()), ["GhostSketch.ino"])
        self.assertIn("void setup()", (first / "GhostSketch.ino").read_text())
        self.assertTrue(self.ghost.is_ghost_project(first))
        self.assertFalse(self.ghost.is_ghost_project(self.root))
        (first / "GhostSketch.ino").write_text("unsaved first ghost")
        self.assertIn("void setup()", (second / "GhostSketch.ino").read_text())

    def test_permanent_save_copies_only_exact_root_sources_and_retains_original(self):
        source = self.create()
        expected = {"GhostSketch.ino": b"// main\r\n", "notes.txt": b"\xef\xbb\xbfnotes\r\n",
                    "a.cpp": b"// cpp\n", "b.c": b"// c\n", "c.h": b"#pragma once\n",
                    "d.hpp": b"// compatible header\n"}
        for name, content in expected.items():
            (source / name).write_bytes(content)
        (source / "gui_config.json").write_text("private settings")
        (source / "picture.bin").write_bytes(b"unrecognized asset")
        cache = source / ".mcu_flasher_build_cache"
        cache.mkdir()
        (cache / "journal.txt").write_text("private recovery")
        nested = source / "nested"
        nested.mkdir()
        (nested / "hidden.cpp").write_text("nested source")
        target = self.root / "Saved sketch"
        saved = self.ghost.promote_ghost_project(source, target)
        self.assertEqual(saved, target)
        self.assertEqual({p.name: p.read_bytes() for p in saved.iterdir()}, expected)
        self.assertTrue(self.ghost.is_ghost_project(source))
        self.assertFalse(self.ghost.is_ghost_project(saved))
        self.assertEqual((source / "notes.txt").read_bytes(), expected["notes.txt"])
        self.assertEqual((cache / "journal.txt").read_text(), "private recovery")

    def test_existing_empty_destination_is_supported_but_occupied_is_preserved(self):
        source = self.create()
        target = self.root / "empty"
        target.mkdir()
        self.assertEqual(self.ghost.promote_ghost_project(source, target), target)
        with self.assertRaises((FileExistsError, ValueError)):
            self.ghost.promote_ghost_project(source, target)
        self.assertIn("void setup()", (target / "GhostSketch.ino").read_text())

    def test_source_changes_during_staging_refuse_publication(self):
        source = self.create()
        target = self.root / "save"
        copyfile = shutil.copyfile
        def changed_copy(src, dst, *args, **kwargs):
            result = copyfile(src, dst, *args, **kwargs)
            Path(src).write_text("newer external source bytes")
            return result
        with patch.object(self.ghost.shutil, "copyfile", side_effect=changed_copy):
            with self.assertRaises((OSError, ValueError)):
                self.ghost.promote_ghost_project(source, target)
        self.assertFalse(target.exists())
        self.assertEqual((source / "GhostSketch.ino").read_text(), "newer external source bytes")

    def test_symlink_source_and_nested_save_target_are_refused(self):
        source = self.create()
        try:
            (source / "linked.h").symlink_to(source / "GhostSketch.ino")
        except OSError:
            self.skipTest("Host cannot create fixture symlinks")
        with self.assertRaises((OSError, ValueError)):
            self.ghost.promote_ghost_project(source, self.root / "linked-save")
        (source / "linked.h").unlink()
        with self.assertRaises((OSError, ValueError)):
            self.ghost.promote_ghost_project(source, source / "nested-save")

    def backend(self):
        from main import web_bridge
        api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        self.assertTrue(callable(getattr(api, "save_ghost_project", None)), "Backend promotion is missing")
        api._lock = threading.Lock()
        api.sketch_dir_path = None
        api.active_file_path = ""
        api.modified_files = {}
        api.is_busy = False
        api.active_operation = api._current_op_phase = None
        api._operation_worker = None
        api._cloud_project_operation = False
        api.current_board, api.current_port, api.current_baud = "User target", "User port", 57600
        api.ai_watcher = api.ai_review_manager = None
        api._cloud_project_link = None
        self.events = []
        api.emit = lambda name, data: self.events.append((name, data))
        for name in ("refresh_cloud_project_link", "_restore_project_compile_state", "_load_compat_cache",
                     "_sync_project_hardware_state", "update_skip_compile_availability"):
            setattr(api, name, lambda *args, **kwargs: None)
        api._detect_sketch_baud_rate = lambda: None
        self.recents, self.settings = [], []
        self.stack.enter_context(patch.object(web_bridge, "find_project_window", return_value=None))
        self.stack.enter_context(patch.object(web_bridge, "set_active_sketch_dir", return_value=True))
        self.stack.enter_context(patch.object(web_bridge, "add_recent_project", side_effect=self.recents.append))
        self.stack.enter_context(patch.object(web_bridge, "load_gui_config", return_value={}))
        self.stack.enter_context(patch.object(web_bridge, "save_gui_config", side_effect=lambda cfg: self.settings.append(dict(cfg)) or True))
        self.stack.enter_context(patch.object(web_bridge, "hide_internal_project_metadata"))
        return api

    def test_backend_creation_skips_temporary_history_and_normal_save_needs_no_picker(self):
        api = self.backend()
        result = api.create_ghost_project()
        self.assertTrue(result["success"], result)
        self.assertTrue(api.is_ghost_sketch)
        self.assertEqual(self.recents, [])
        self.assertEqual(self.settings, [])
        path = api.active_file_path
        api.mark_modified(path)
        self.assertTrue(api.save_file(path, "void setup() {}\nvoid loop() {}\n")["success"])
        self.assertFalse(api.modified_files[path])
        self.assertTrue(api.is_ghost_sketch, "Compile flush must not permanently save Ghost")
        self.assertEqual(Path(path).read_text(), "void setup() {}\nvoid loop() {}\n")

    def test_backend_promotion_maps_active_files_and_preserves_hardware(self):
        api = self.backend()
        self.assertTrue(api.create_ghost_project()["success"])
        old_path = api.active_file_path
        api.modified_files[old_path] = False
        self.events.clear()
        target = self.root / "permanent"
        result = api.save_ghost_project(str(target))
        self.assertTrue(result["success"], result)
        new_path = str(target / "GhostSketch.ino")
        self.assertEqual(api.active_file_path, new_path)
        self.assertEqual(api.modified_files, {new_path: False})
        self.assertFalse(api.is_ghost_sketch)
        self.assertEqual(result["path_mapping"], {old_path: new_path})
        self.assertEqual((api.current_board, api.current_port, api.current_baud), ("User target", "User port", 57600))
        updates = [data for name, data in self.events if name == "project:updated"]
        self.assertEqual(len(updates), 1)
        self.assertTrue(updates[0]["preserve_buffers"])
        self.assertEqual(updates[0]["operation"], "ghost_saved")

    def test_busy_dirty_cancel_and_registration_failure_preserve_ghost(self):
        api = self.backend()
        self.assertTrue(api.create_ghost_project()["success"])
        original = api.sketch_dir_path
        for busy, operation, phase, cloud, dirty, destination in (
                (True, None, None, False, False, "busy"),
                (False, "upload", None, False, False, "upload"),
                (False, None, "flash", False, False, "phase"),
                (False, None, None, True, False, "cloud"),
                (False, None, None, False, True, "dirty"),
                (False, None, None, False, False, "")):
            api.is_busy, api.active_operation, api._current_op_phase = busy, operation, phase
            api._cloud_project_operation = cloud
            api.modified_files = {api.active_file_path: dirty}
            path = str(self.root / destination) if destination else ""
            self.assertFalse(api.save_ghost_project(path)["success"])
            self.assertEqual(api.sketch_dir_path, original)
        api.modified_files = {}
        from main import web_bridge
        with patch.object(web_bridge, "set_active_sketch_dir", return_value=False):
            failed = api.save_ghost_project(str(self.root / "registration-refused"))
        self.assertFalse(failed["success"])
        self.assertEqual(api.sketch_dir_path, original)
        self.assertTrue(api.is_ghost_sketch)
        self.assertIn("void setup()", Path(api.active_file_path).read_text())

    def test_post_publication_service_failures_still_return_the_model_mapping(self):
        api = self.backend()
        self.assertTrue(api.create_ghost_project()["success"])
        original = api.active_file_path
        def unavailable(*args, **kwargs):
            raise OSError("Fixture service unavailable")
        api.ai_watcher = SimpleNamespace(bind_project=unavailable)
        from main import web_bridge
        with patch.object(web_bridge, "save_gui_config", side_effect=unavailable):
            result = api.save_ghost_project(str(self.root / "saved-with-warning"))
        self.assertTrue(result["success"], result)
        self.assertEqual(result["project"]["path_mapping"][original], api.active_file_path)
        self.assertFalse(api.is_ghost_sketch)
        self.assertIn("warning", result)

    def test_promotion_reserves_backend_actions_until_copy_finishes(self):
        api = self.backend()
        self.assertTrue(api.create_ghost_project()["success"])
        copyfile = shutil.copyfile
        def while_saving(src, dst, *args, **kwargs):
            self.assertTrue(api.is_busy)
            self.assertFalse(api.save_file(api.active_file_path, "late edit")["success"])
            self.assertFalse(api.create_ghost_project()["success"])
            self.assertFalse(api.discard_ghost_project()["success"])
            return copyfile(src, dst, *args, **kwargs)
        with patch.object(self.ghost.shutil, "copyfile", side_effect=while_saving):
            self.assertTrue(api.save_ghost_project(str(self.root / "reserved"))["success"])
        self.assertFalse(api.is_busy)

    def test_discard_only_releases_the_exact_owned_temporary_session(self):
        api = self.backend()
        self.assertTrue(api.create_ghost_project()["success"])
        original = api.sketch_dir_path
        other = self.create()
        result = api.save_ghost_project(str(self.root / "kept-permanent"))
        self.assertTrue(result["success"])
        permanent = api.sketch_dir_path
        self.assertFalse(api.discard_ghost_project(str(permanent))["success"])
        self.assertTrue(permanent.is_dir())
        self.assertTrue(api.discard_ghost_project(str(original))["success"])
        self.assertFalse(original.exists())
        self.assertTrue(other.is_dir())
        self.assertTrue(permanent.is_dir())
        self.assertEqual(api.sketch_dir_path, permanent)

    def test_repeated_ghost_state_reads_never_probe_storage(self):
        api = self.backend()
        self.assertTrue(api.create_ghost_project()["success"])
        from main import web_bridge
        with patch.object(web_bridge, "is_ghost_project", side_effect=AssertionError("Unexpected storage probe")):
            for _ in range(100):
                self.assertTrue(api.is_ghost_sketch)
            api.sketch_dir_path = self.root / "ordinary-project"
            self.assertFalse(api.is_ghost_sketch)

    def test_failed_new_window_creation_releases_only_its_new_ghost(self):
        api = self.backend()
        new_path, other = self.create(), self.create()
        from main import web_bridge
        with patch.object(web_bridge, "create_ghost_project_path", return_value=new_path):
            api.open_project_window = lambda path: {"success": False, "error": "Fixture launch failure"}
            result = api.create_ghost_project(open_in_new_window=True)
        self.assertFalse(result["success"])
        self.assertFalse(new_path.exists())
        self.assertTrue(other.is_dir())

    def test_automatic_release_preserves_ghost_ai_recovery_history(self):
        api = self.backend()
        self.assertTrue(api.create_ghost_project()["success"])
        original = api.sketch_dir_path
        history = original / ".mcu_flasher_build_cache/.mcu_ai_edits/session1/edit1.txt"
        history.parent.mkdir(parents=True)
        history.write_bytes(b"exact fixture recovery history\r\n")
        self.assertTrue(api.save_ghost_project(str(self.root / "saved-with-history"))["success"])
        release = getattr(api, "release_saved_ghost", None)
        self.assertTrue(callable(release), "Automatic release must preserve AI history")
        result = release(str(original))
        self.assertTrue(result["success"], result)
        self.assertTrue(result["retained"])
        self.assertEqual(result["recovery_path"], str(original))
        self.assertEqual(history.read_bytes(), b"exact fixture recovery history\r\n")
        self.assertFalse((api.sketch_dir_path / ".mcu_flasher_build_cache").exists())

    def test_automatic_release_removes_ghost_without_ai_history(self):
        api = self.backend()
        self.assertTrue(api.create_ghost_project()["success"])
        original = api.sketch_dir_path
        self.assertTrue(api.save_ghost_project(str(self.root / "saved-no-history"))["success"])
        release = getattr(api, "release_saved_ghost", None)
        self.assertTrue(callable(release), "Automatic release is missing")
        self.assertTrue(release(str(original))["success"])
        self.assertFalse(original.exists())


if __name__ == "__main__":
    unittest.main()
