#!/usr/bin/env python3
"""Real offline Monaco/Ghost controller smoke; all source IO is fixture-owned.

Mocked boundaries: config/notification/ownership persistence, AI watchers,
hardware discovery/services, compiler dispatch, syntax analysis, cloud/network,
credential access and download-manager exit. No build or upload is executed.
"""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
import argparse
import atexit
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
sys.dont_write_bytecode = True

# Board/config modules consult their stores while importing. Redirect those
# initial probes too, before importing the backend or constructing Qt pages.
CAPTURES = ROOT / "temp/audit/ghost-detached"
CAPTURES.mkdir(parents=True, exist_ok=True)
_IMPORT_FIXTURE = tempfile.TemporaryDirectory(prefix="import-stores-", dir=CAPTURES)
_IMPORT_ROOT = Path(_IMPORT_FIXTURE.name)
_IMPORT_GUARDS = ExitStack()
_IMPORT_GUARDS.enter_context(patch.object(Path, "home", return_value=_IMPORT_ROOT))
_IMPORT_GUARDS.enter_context(patch.dict(os.environ, {
    "XDG_CACHE_HOME": str(_IMPORT_ROOT / "cache"),
    "XDG_CONFIG_HOME": str(_IMPORT_ROOT / "config"),
    "XDG_DATA_HOME": str(_IMPORT_ROOT / "data"),
    "LOCALAPPDATA": str(_IMPORT_ROOT / "local"),
    "APPDATA": str(_IMPORT_ROOT / "roaming"),
    "PLATFORMIO_CORE_DIR": str(_IMPORT_ROOT / "platformio"),
    "MCU_PACKAGE_EVENTS_ROOT": str(_IMPORT_ROOT / "package-events"),
}))
from main.core import config, constants, toolchain
_IMPORT_GUARDS.enter_context(patch.object(config, "LOCAL_GUI_CONFIG", _IMPORT_ROOT / "portable.json"))
_IMPORT_GUARDS.enter_context(patch.object(config, "GUI_CONFIG_FILE", _IMPORT_ROOT / "portable.json"))
with patch.object(constants, "SCRIPT_DIR", _IMPORT_ROOT / "application"), \
     patch.object(toolchain, "_get_safe_platformio_core_dir", return_value=str(_IMPORT_ROOT / "platformio")), \
     patch.object(os.path, "expanduser", side_effect=lambda value:
                  str(_IMPORT_ROOT) + value[1:] if str(value).startswith("~") else value):
    from main.core import board_catalog

def _close_import_stores():
    _IMPORT_GUARDS.close()
    _IMPORT_FIXTURE.cleanup()

atexit.register(_close_import_stores)

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QToolButton
from shiboken6 import isValid
from main import web_bridge
from main.core import file_utils, ghost_sketch
from main.qt.main_window import MCUMainWindow
from main.qt.project_dialog import ProjectDialog
from main.qt.signals import signals
from main.qt.theme import build_stylesheet, register_fonts
from src.modules.ui_metrics import WorkArea
from verify_projects import isolate_layout_cloud

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
register_fonts()
APP.setStyleSheet(build_stylesheet("default"))


class GhostWorkflowChecks(unittest.TestCase):
    def setUp(self):
        CAPTURES.mkdir(parents=True, exist_ok=True)
        self.fixture = tempfile.TemporaryDirectory(prefix="workflow-", dir=CAPTURES)
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        isolate_layout_cloud(self, self.stack)
        self.windows = []
        self.addCleanup(self.dispose)
        self.async_errors = []
        self.stack.enter_context(patch.object(sys, "excepthook", side_effect=lambda kind, value, tb:
                                              self.async_errors.append(f"{kind.__name__}: {value}")))
        self.stack.enter_context(patch.object(ghost_sketch, "_ghost_store_root", return_value=self.root / "owned"))
        for module in (config, web_bridge):
            for name, value in (("load_gui_config", {}), ("save_gui_config", True),
                                ("find_project_window", None), ("set_active_sketch_dir", True),
                                ("focus_project_window", False)):
                if hasattr(module, name):
                    self.stack.enter_context(patch.object(module, name, side_effect=lambda *a, _value=value, **k:
                                                          dict(_value) if isinstance(_value, dict) else _value))
        for name, value in (("_load_raw_config", {"shared": {}, "instances": {}}),
                            ("_save_raw_config", True), ("get_theme_mode", "default"),
                            ("get_autosave_settings", (False, 1000))):
            self.stack.enter_context(patch.object(config, name, return_value=value))
        self.stack.enter_context(patch.object(config, "clean_instance_config"))
        self.stack.enter_context(patch.object(web_bridge, "add_recent_project"))
        self.stack.enter_context(patch.object(web_bridge, "load_recent_projects", return_value=[]))
        self.stack.enter_context(patch.object(config, "load_recent_projects", return_value=[]))
        self.stack.enter_context(patch.object(file_utils, "hide_internal_project_metadata"))
        self.stack.enter_context(patch.object(web_bridge, "hide_internal_project_metadata"))
        self.stack.enter_context(patch("src.dbs.dbs_create.add_notification", return_value={"id": "fixture"}))
        self.stack.enter_context(patch("src.modules.ubuntu_download_manager.quit_if_last_workspace"))
        self.stack.enter_context(patch("src.syntax_checker.analyze_cpp_syntax", return_value=[]))
        for target in ("main.qt.responsive.work_area", "main.qt.main_window.work_area"):
            self.stack.enter_context(patch(target, return_value=WorkArea(0, 0, 1440, 960)))
        for target in ("main.core.ai_review.AIReviewManager", "main.core.ai_review.AIEditWatcher"):
            self.stack.enter_context(patch(target, side_effect=lambda *a, **k: Mock(
                get_ai_edit_reviews=Mock(return_value=[]), get_ai_changes=Mock(return_value=[]),
                user_file_operation=lambda *a, **k: nullcontext())))
        for name in ("_restore_project_compile_state", "_load_compat_cache", "_sync_project_hardware_state",
                     "refresh_cloud_project_link", "update_skip_compile_availability"):
            self.stack.enter_context(patch.object(web_bridge.MCUWebBackendAPI, name))
        self.stack.enter_context(patch.object(web_bridge.MCUWebBackendAPI, "_detect_sketch_baud_rate", return_value=None))
        self.hardware = self.stack.enter_context(patch.object(web_bridge.serial, "Serial", side_effect=AssertionError("Live serial access")))
        self.stack.enter_context(patch.object(QMessageBox, "warning", side_effect=self.unexpected_warning))
        self.questions = self.stack.enter_context(patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard))

    def unexpected_warning(self, *args):
        self.fail("Unexpected workspace warning: " + str(args[1:]))

    def pump(self):
        APP.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def until(self, condition, label, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pump()
            if condition():
                return
            QTest.qWait(15)
        self.fail("Timed out: " + label)

    def js(self, page, script, seconds=5):
        answers = []
        page.runJavaScript(script, answers.append)
        self.until(lambda: bool(answers), "JavaScript result", seconds)
        return answers[0]

    def backend(self, project=None):
        if self.windows and not any(isValid(window) for window in self.windows):
            # Shipping workspaces live in separate processes. Sequential fixture
            # windows share this signal bus, including plain toolbar lambdas.
            try:
                signals.project_updated.disconnect()
            except RuntimeError:
                pass
        api = web_bridge.MCUWebBackendAPI()
        api._qt_signals = signals
        api.start_services = Mock()
        api.stop_services = Mock()
        api.save_settings = Mock(return_value={"success": True})
        api._scan_ports = Mock(side_effect=AssertionError("Live hardware discovery"))
        api.save_tab_order = lambda paths: setattr(api, "fixture_tab_order", list(paths))
        api.fixture_tab_order = []
        if project is None:
            result = api.create_ghost_project()
        else:
            result = api.open_project(str(project))
        self.assertTrue(result.get("success"), result)
        return api

    def workspace(self, api):
        window = MCUMainWindow(api)
        self.windows.append(window)
        window.resize(1300, 860)
        window.show()
        page = window._editor_panel._view.page()
        self.until(lambda: self.js(page,
            "Boolean(window.editorInstance && window.__mcuProjectFileStore?.list().length "
            "&& window.pywebview?.api && document.querySelector('#tab-bar .tab')?._filePath)"),
            "offline Monaco project load")
        return window, page

    def dispose(self):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Discard):
            for window in self.windows:
                if isValid(window):
                    window._ghost_save_in_progress = False
                    window._backend._ghost_project_operation = False
                    window._backend._ghost_project_path = None
                    window.close()
            for _ in range(20):
                self.pump()
                QTest.qWait(10)
            if self.windows:
                try:
                    signals.project_updated.disconnect()
                except RuntimeError:
                    pass
        self.hardware.assert_not_called()
        self.assertEqual(self.async_errors, [], "Qt callback exceptions occurred during the smoke")

    def essentials_visible(self, window):
        toolbar = window._primary_toolbar
        for button in (toolbar.btn_compile, toolbar.btn_upload, toolbar.btn_project,
                       toolbar.btn_cloud, toolbar.btn_download):
            if not button.isVisibleTo(toolbar.window()):
                return False
            child = button
            while child is not toolbar:
                parent = child.parentWidget()
                if parent is None or not parent.rect().contains(QRect(child.mapTo(parent, QPoint()), child.size())):
                    return False
                child = parent
        return not any(button.isVisible() for button in toolbar.findChildren(QToolButton, "qt_toolbar_ext_button"))

    def test_real_monaco_detached_ctrl_s_keeps_models_undo_order_and_selection(self):
        selector = ProjectDialog()
        selector.show()
        self.pump()
        self.assertTrue(selector._btn_ghost_sketch.isVisible())
        self.assertTrue(selector.grab().save(str(CAPTURES / "project-selector.png")))
        QTest.mouseClick(selector._btn_ghost_sketch, Qt.MouseButton.LeftButton)
        project = selector.selected_project
        self.assertIsNotNone(project)
        selector.deleteLater()
        api = self.backend(project)
        self.assertTrue(api.is_ghost_sketch)
        self.assertTrue(api.add_project_file("notes.txt")["success"])
        window, page = self.workspace(api)
        self.until(lambda: self.js(page, "window.__mcuProjectFileStore?.list().length") == 2, "two loaded sources")
        source = str(project / "GhostSketch.ino")
        note = str(project / "notes.txt")
        source_text = "// Ghost Unicode Ω\nvoid setup() {}\nvoid loop() {}\n"
        note_text = "Temporary notes Ω\nsecond line\n"
        script = """(() => {
          const store = window.__mcuProjectFileStore;
          const files = store.list();
          const values = %s;
          window.__ghostFixture = {files:[...files], models:files.map(f=>f.model)};
          for (const file of files) {
            file.model.pushStackElement();
            file.model.pushEditOperations([], [{range:file.model.getFullModelRange(),text:values[file.path]}],()=>null);
            file.model.pushStackElement();
          }
          window.activateProjectFile(%s);
          window.editorInstance.setSelection(new monaco.Selection(1,2,1,7));
          const bar = document.getElementById('tab-bar');
          bar.appendChild(bar.firstElementChild);
          window.__ghostFixture.order = [...bar.querySelectorAll('.tab')].map(t=>t._filePath.split(/[\\\\/]/).pop());
          window.__ghostFixture.selection = window.editorInstance.getSelection();
          return files.every(file=>file.isDirty);
        })()""" % (json.dumps({source: source_text, note: note_text}), json.dumps(source))
        self.assertTrue(self.js(page, script))
        self.until(lambda: api.modified_files.get(source) and api.modified_files.get(note), "dirty buffer acknowledgements")
        build_snapshots = []
        api.current_board = "Fixture target"
        api.compile_sketch = Mock(side_effect=lambda: build_snapshots.append(
            {(project / name).name: (project / name).read_text(encoding='utf-8') for name in ("GhostSketch.ino", "notes.txt")}))
        with patch.object(QFileDialog, "getExistingDirectory", side_effect=AssertionError("Build opened save picker")) as picker:
            window._primary_toolbar._do_compile()
            self.until(lambda: len(build_snapshots) == 1, "acknowledged build flush")
            picker.assert_not_called()
        self.assertEqual(build_snapshots, [{"GhostSketch.ino": source_text, "notes.txt": note_text}])
        self.assertTrue(api.is_ghost_sketch)
        self.assertFalse(any(api.modified_files.values()))
        window.detach_editor()
        detached = window._detached_window
        self.until(lambda: detached.isVisible() and window._editor_detached, "detached editor")
        self.assertIs(window._primary_toolbar.window(), detached)
        self.until(lambda: self.essentials_visible(window), "detached native toolbar action visibility", 5)
        self.assertTrue(detached.grab().save(str(CAPTURES / "detached-before-save.png")))
        note_text += "More before permanent save\n"
        self.assertTrue(self.js(page, """(() => {
          const file=window.__mcuProjectFileStore.list().find(f=>f.path===%s);
          file.model.pushEditOperations([], [{range:file.model.getFullModelRange(),text:%s}],()=>null);
          return file.isDirty;
        })()""" % (json.dumps(note), json.dumps(note_text))))
        self.until(lambda: api.modified_files.get(note), "detached edit acknowledgement")
        target = self.root / "SavedGhost"
        target.mkdir()
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(target)) as picker:
            detached.activateWindow()
            window._editor_panel._view.setFocus()
            self.js(page, "window.editorInstance.focus(); true")
            receiver = window._editor_panel._view.focusProxy() or window._editor_panel._view
            QTest.keyClick(receiver, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
            self.until(lambda: picker.call_count == 1, "detached Ctrl+S workspace routing", 5)
            self.until(lambda: not window._ghost_sketch.is_saving, "source publication and model remap")
        self.assertFalse(api.is_ghost_sketch)
        self.assertEqual(api.sketch_dir_path, target)
        self.assertEqual((target / "GhostSketch.ino").read_text(encoding='utf-8'), source_text)
        self.assertEqual((target / "notes.txt").read_text(encoding='utf-8'), note_text)
        state = self.js(page, """(() => {
          const prior=window.__ghostFixture, files=window.__mcuProjectFileStore.list();
          return JSON.stringify({same:files.every((f,i)=>f===prior.files[i]&&f.model===prior.models[i]),
            order:[...document.querySelectorAll('#tab-bar .tab')].map(t=>t._filePath.split(/[\\\\/]/).pop()),
            previousOrder:prior.order, selection:window.editorInstance.getSelection(), previousSelection:prior.selection,
            paths:files.map(f=>f.path), dirty:files.map(f=>f.isDirty),
            undo:files.every(f=>f.model.canUndo())});
        })()""")
        state = json.loads(state)
        self.assertTrue(state["same"], state)
        self.assertEqual(state["order"], state["previousOrder"])
        self.assertEqual(state["selection"], state["previousSelection"])
        self.assertTrue(state["undo"])
        self.assertFalse(any(state["dirty"]))
        self.assertEqual(set(state["paths"]), {str(target / "GhostSketch.ino"), str(target / "notes.txt")})
        self.assertTrue(all(str(target) in path for path in api.fixture_tab_order))
        self.until(lambda: not project.exists(), "release of acknowledged temporary project")
        self.assertTrue(detached.grab().save(str(CAPTURES / "detached-after-save.png")))
        window.close()
        self.until(lambda: not isValid(window), "saved detached workspace destruction")

    def test_clean_ghost_close_cancel_discard_and_save_wait_for_real_controller(self):
        api = self.backend()
        project = api.sketch_dir_path
        window, _page = self.workspace(api)
        self.assertFalse(any(api.modified_files.values()))
        self.questions.return_value = QMessageBox.StandardButton.Cancel
        self.assertFalse(window.close())
        self.assertTrue(window.isVisible())
        self.assertTrue(project.exists())
        self.questions.return_value = QMessageBox.StandardButton.Discard
        window.close()
        self.until(lambda: not isValid(window) and not project.exists(), "accepted clean-Ghost discard")
        api = self.backend()
        project = api.sketch_dir_path
        window, page = self.workspace(api)
        text = "// saved through close prompt\nvoid setup() {}\nvoid loop() {}\n"
        self.assertTrue(self.js(page, "window.editorInstance.setValue(" + json.dumps(text) + "); true"))
        self.until(lambda: any(api.modified_files.values()), "close-save dirty acknowledgement")
        target = self.root / "SavedOnClose"
        target.mkdir()
        self.questions.return_value = QMessageBox.StandardButton.Save
        with patch.object(QFileDialog, "getExistingDirectory", return_value=str(target)):
            self.assertFalse(window.close(), "Async Save must initially veto close")
            self.until(lambda: not isValid(window), "Save-and-close model acknowledgement")
        self.assertEqual((target / "GhostSketch.ino").read_text(encoding='utf-8'), text)
        self.assertFalse(api.is_ghost_sketch)
        self.until(lambda: not project.exists(), "Save-and-close temporary release")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--render-dir", type=Path)
    options, remaining = parser.parse_known_args()
    if options.render_dir is not None:
        capture_path = options.render_dir.resolve()
        if not capture_path.is_relative_to((ROOT / "temp").resolve()):
            parser.error("Evidence must stay inside this checkout's temp directory")
        CAPTURES = capture_path
    unittest.main(argv=[sys.argv[0], *remaining])
