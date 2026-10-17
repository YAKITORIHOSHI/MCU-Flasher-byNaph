#!/usr/bin/env python3
"""Cloud source enumeration in temp fixtures; no GUI, settings or hardware writes."""
from __future__ import annotations

import ast
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.core import file_utils
from main.core.constants import is_application_codebase_dir
from PySide6.QtCore import QCoreApplication, QObject
from main.qt.editor_panel import EditorBridgeAPI, MonacoEditorPanel


def isolated_backend_class():
    """Run the actual source methods without initializing services or persistence."""
    tree = ast.parse((ROOT / "main/web_bridge.py").read_text(encoding="utf-8"))
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                    and node.name == "MCUWebBackendAPI")
    methods = [node for node in original.body if isinstance(node, ast.FunctionDef)
               and node.name in {"get_project_files", "get_cloud_project_files"}]
    assert len(methods) == 2
    selected = ast.ClassDef(name="IsolatedBackend", bases=[], keywords=[],
                            body=methods, decorator_list=[])
    scope = {"Any": Any, "get_sketch_files_fast": file_utils.get_sketch_files_fast,
             "is_application_codebase_dir": is_application_codebase_dir}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[selected], type_ignores=[])),
                 "isolated-cloud-editor-backend", "exec"), scope)
    return scope["IsolatedBackend"]


Backend = isolated_backend_class()
APP = QCoreApplication.instance() or QCoreApplication(["Cloud manifest verification"])


class CloudManifestChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/cloud-editor-manifest"
        audit.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.backend = Backend()
        self.backend.sketch_dir_path = self.project
        self.backend.modified_files = {}

    def create(self, name):
        target = self.project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture source\n", encoding="utf-8")
        return target

    def wait(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(0.005)
        self.assertTrue(predicate(), "Queued Cloud manifest completion timed out")

    def test_empty_directory_is_successful_but_missing_root_is_not(self):
        self.assertEqual(self.backend.get_cloud_project_files(), {"success": True, "files": []})
        self.backend.sketch_dir_path = self.project / "missing"
        self.assertEqual(self.backend.get_cloud_project_files(), {"success": False, "files": []})
        self.assertEqual(self.backend.get_project_files(), [])

    def test_denied_root_does_not_certify_existing_sources_as_empty(self):
        source = self.create("sketch.ino")
        original = source.read_bytes()
        with patch.object(file_utils.os, "scandir", side_effect=PermissionError("fixture denied")):
            self.assertEqual(self.backend.get_project_files(), [])
            self.assertEqual(self.backend.get_cloud_project_files(), {"success": False, "files": []})
        self.assertEqual(source.read_bytes(), original)

    def test_denied_nested_directory_rejects_partial_enumeration(self):
        self.create("sketch.ino")
        self.create("notes/header.h")
        scan = file_utils.os.scandir

        def denied(directory):
            if Path(directory) == self.project / "notes":
                raise PermissionError("fixture nested denied")
            return scan(directory)

        with patch.object(file_utils.os, "scandir", side_effect=denied):
            self.assertEqual([row["name"] for row in self.backend.get_project_files()], ["sketch.ino"])
            self.assertEqual(self.backend.get_cloud_project_files(), {"success": False, "files": []})

    def test_supported_sources_and_existing_pruning_remain_compatible(self):
        case_note = "notes_lower.h" if sys.platform == "win32" else "notes.h"
        included = [self.create(name) for name in ("main.ino", "logic.cpp", "driver.c", "Notes.h", case_note, "config.hpp", "NOTE.txt", "notes/extra.txt")]
        for name in (".hidden.txt", "src/internal.cpp", "build/internal.h", "assets/image.png"):
            self.create(name)
        manifest = self.backend.get_cloud_project_files()
        self.assertTrue(manifest["success"])
        expected = {str(path) for path in included}
        self.assertEqual({row["path"] for row in manifest["files"]}, expected)
        self.assertEqual({row["path"] for row in self.backend.get_project_files()}, expected)
        self.assertEqual(manifest["files"][0]["name"], "main.ino")

    def test_manifest_runs_off_owner_thread_and_completion_is_queued(self):
        self.create("sketch.ino")
        bridge = EditorBridgeAPI(self.backend)
        self.addCleanup(bridge.deleteLater)
        owner = threading.get_ident()
        scan_threads, ready_threads, replies = [], [], []
        original = self.backend.get_cloud_project_files

        def scan():
            scan_threads.append(threading.get_ident())
            return original()

        def ready(token, manifest):
            ready_threads.append(threading.get_ident())
            replies.append((token, manifest))

        self.backend.get_cloud_project_files = scan
        bridge.cloud_project_files_ready.connect(ready)
        self.assertTrue(bridge.request_cloud_project_files("fixture request"))
        self.wait(lambda: bool(replies))
        self.assertTrue(replies[0][1]["success"])
        self.assertEqual(replies[0][0], "fixture request")
        self.assertNotEqual(scan_threads, [owner])
        self.assertEqual(ready_threads, [owner])

    def test_only_one_scan_runs_and_project_change_rejects_its_result(self):
        bridge = EditorBridgeAPI(self.backend)
        self.addCleanup(bridge.deleteLater)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        replies = []

        def scan():
            entered.set()
            if not release.wait(5):
                return {"success": False, "files": []}
            return {"success": True, "files": []}

        self.backend.get_cloud_project_files = scan
        bridge.cloud_project_files_ready.connect(lambda token, manifest: replies.append((token, manifest)))
        self.assertTrue(bridge.request_cloud_project_files("original request"))
        self.assertTrue(entered.wait(5))
        self.assertFalse(bridge.request_cloud_project_files("overlapping request"))
        self.backend.sketch_dir_path = self.project / "replacement"
        release.set()
        self.wait(lambda: bool(replies))
        self.assertEqual(replies, [("original request", {"success": False, "files": []})])

    def host(self):
        host = QObject()
        self.addCleanup(host.deleteLater)
        bridge = EditorBridgeAPI(self.backend, host)
        bridge.begin_renderer_load()
        host._bridge = bridge
        host._backend = self.backend
        scripts = []
        host._view = type("View", (), {"page": lambda _: type("Page", (), {
            "runJavaScript": lambda _, script: scripts.append(script)})()})()
        return host, bridge, scripts

    def test_renderer_change_during_scan_rejects_before_any_model_reload(self):
        host, bridge, scripts = self.host()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def scan():
            entered.set()
            release.wait(5)
            return {"success": True, "files": []}

        self.backend.get_cloud_project_files = scan
        self.backend.modified_files = {"fixture.ino": True}
        replies = []
        MonacoEditorPanel.reload_cloud_snapshot(host, callback=lambda: replies.append(True),
                                               failure_callback=lambda: replies.append(False))
        self.assertTrue(entered.wait(5))
        bridge.begin_renderer_load()
        release.set()
        self.wait(lambda: bool(replies))
        self.assertEqual(replies, [False])
        self.assertEqual(len(scripts), 1)
        self.assertIn("invalidateProjectFileOperations", scripts[0])
        self.assertFalse(any("window.reloadCloudSnapshot(" in script for script in scripts))
        self.assertEqual(self.backend.modified_files, {"fixture.ino": True})

    def test_cancelled_worker_and_stale_replies_cannot_finish_newer_reload(self):
        host, bridge, scripts = self.host()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def scan():
            entered.set()
            release.wait(5)
            return {"success": True, "files": []}

        self.backend.get_cloud_project_files = scan
        self.backend.modified_files = {"fixture.ino": True}
        replies = []
        reload = lambda: MonacoEditorPanel.reload_cloud_snapshot(host,
            callback=lambda: replies.append(True), failure_callback=lambda: replies.append(False))
        reload()
        old_token = host._cloud_reload_pending
        self.assertTrue(entered.wait(5))
        bridge.finish_cloud_reload(old_token, False)  # Same cancellation path as the timeout.
        release.set()
        self.wait(lambda: not bridge._cloud_manifest_running)
        self.assertEqual(replies, [False])
        self.assertEqual(len(scripts), 1)
        self.assertIn("invalidateProjectFileOperations", scripts[0])
        self.assertFalse(any("window.reloadCloudSnapshot(" in script for script in scripts))
        reload()
        new_token = host._cloud_reload_pending
        bridge.cloud_project_files_ready.emit(old_token, {"success": True, "files": []})
        bridge.finish_cloud_reload(old_token, True)
        self.assertEqual(host._cloud_reload_pending, new_token)
        self.assertEqual(replies, [False])
        self.wait(lambda: any("window.reloadCloudSnapshot(" in script for script in scripts))
        bridge.finish_cloud_reload(new_token, True)
        self.assertEqual(replies, [False, True])
        self.assertEqual(self.backend.modified_files, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
