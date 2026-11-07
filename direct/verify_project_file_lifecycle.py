#!/usr/bin/env python3
"""Isolated Modify events and real offline Monaco model-preservation checks.

Only disposable root sources under temp/ and in-memory renderer sources change.
Persistence, hardware, PTYs, accounts and metadata generation are mocked.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PySide6.QtCore import QTimer, QUrlQuery
from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from main import web_bridge
from main.qt.editor_panel import MonacoEditorPanel
from direct import verify_editor_review_ui as renderer
APP, ReviewPage, until = renderer.APP, renderer.ReviewPage, renderer.until


class BackendFileChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "temp/audit").mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(dir=ROOT / "temp/audit", prefix="project-files-")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        (self.root / "Sketch.ino").write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
        (self.root / "other.cpp").write_text("// saved helper\n", encoding="utf-8")
        self.api = web_bridge.MCUWebBackendAPI.__new__(web_bridge.MCUWebBackendAPI)
        self.api.sketch_dir_path = self.root
        self.api.is_busy = False
        self.api.active_operation = None
        self.api.active_file_path = str(self.root / "Sketch.ino")
        self.api.modified_files = {self.api.active_file_path: True}
        self.api.emit = Mock()
        self.api.update_skip_compile_availability = Mock()
        self.api._sync_project_hardware_state = Mock()
        self.addCleanup(web_bridge._sketch_ram_cache.invalidate)

    def test_modify_emits_only_file_events_and_refreshes_current_guidance(self):
        active = self.api.active_file_path
        dirty = dict(self.api.modified_files)
        self.assertTrue(self.api.add_project_file("new.h")["success"])
        self.assertTrue(self.api.rename_project_file("new.h", "renamed.h")["success"])
        self.assertTrue(self.api.delete_project_file("renamed.h")["success"])
        calls = self.api.emit.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["project:files"] * 3)
        self.assertEqual([call.args[1]["operation"] for call in calls], ["add", "rename", "delete"])
        self.assertEqual([call.args[1]["revision"] for call in calls], [1, 2, 3])
        self.assertEqual(calls[0].args[1]["content"], "#pragma once\n")
        self.assertEqual(self.api.modified_files, dirty)
        self.assertEqual(self.api.active_file_path, active)
        self.assertEqual(self.api._sync_project_hardware_state.call_count, 3)
        self.assertEqual(self.api.update_skip_compile_availability.call_count, 3)
        sources = sorted(p.name for p in self.root.iterdir() if p.is_file() and p.name != ".hidden")
        self.assertEqual(sources, ["Sketch.ino", "other.cpp"])
        # Delete never erases: the file waits in the project's hidden bak folder.
        self.assertEqual((self.root / "bak" / "renamed.h").read_text(encoding="utf-8"), "#pragma once\n")
        self.assertEqual(calls[2].args[1]["file"], str(self.root / "renamed.h"))

    def test_deleted_files_collect_in_bak_under_numbered_names(self):
        from main.core import project_bak
        self.api.modified_files = {}
        for content in ("// first\n", "// second\n", "// third\n"):
            (self.root / "helper.cpp").write_text(content, encoding="utf-8")
            result = self.api.delete_project_file("helper.cpp")
            self.assertTrue(result["success"], result)
        bak = self.root / "bak"
        self.assertEqual(sorted(p.name for p in bak.iterdir()), ["helper (1).cpp", "helper (2).cpp", "helper.cpp"])
        self.assertEqual((bak / "helper.cpp").read_text(encoding="utf-8"), "// first\n")
        self.assertEqual((bak / "helper (2).cpp").read_text(encoding="utf-8"), "// third\n")
        # bak is not part of the sketch: no tab, build input or source count sees it.
        from main.core.file_utils import get_sketch_files_fast
        self.assertEqual(sorted(p.name for p in get_sketch_files_fast(self.root)), ["Sketch.ino", "other.cpp"])
        self.assertEqual(sorted(f["name"] for f in self.api.get_project_files()), ["Sketch.ino", "other.cpp"])
        if sys.platform.startswith("linux"):
            # Hidden from Linux file managers by name, keeping anything already listed there.
            self.assertEqual((self.root / ".hidden").read_text(encoding="utf-8"), "bak\n")
            (self.root / ".hidden").write_text("notes", encoding="utf-8")
            project_bak.ensure_bak_directory(self.root)
            self.assertEqual((self.root / ".hidden").read_text(encoding="utf-8"), "notes\nbak\n")
            project_bak.ensure_bak_directory(self.root)
            self.assertEqual((self.root / ".hidden").read_text(encoding="utf-8").count("bak"), 1)
        self.assertEqual(project_bak._numbered("Makefile", 3), "Makefile (3)")
        # The last source of a project is never moved.
        self.assertTrue(self.api.delete_project_file("other.cpp")["success"])
        last = self.api.delete_project_file("Sketch.ino")
        self.assertFalse(last["success"])
        self.assertIn("only source", last["error"])
        self.assertTrue((self.root / "Sketch.ino").is_file())

    def test_bak_never_adopts_a_file_or_link_and_refuses_files_outside_the_root(self):
        from main.core import project_bak
        self.api.modified_files = {}
        (self.root / "bak").write_text("not a folder", encoding="utf-8")
        result = self.api.delete_project_file("other.cpp")
        self.assertFalse(result["success"])
        self.assertIn("not a folder", result["error"])
        self.assertTrue((self.root / "other.cpp").is_file(), "a refused delete leaves the file in place")
        (self.root / "bak").unlink()
        if hasattr(os, "symlink") and sys.platform != "win32":
            elsewhere = self.root / "elsewhere"
            elsewhere.mkdir()
            (self.root / "bak").symlink_to(elsewhere, target_is_directory=True)
            self.assertFalse(self.api.delete_project_file("other.cpp")["success"])
            self.assertEqual(list(elsewhere.iterdir()), [])
            (self.root / "bak").unlink()
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "x.cpp").write_text("", encoding="utf-8")
        with self.assertRaises(project_bak.ProjectBakError):
            project_bak.move_to_bak(self.root, nested / "x.cpp")
        self.assertTrue((nested / "x.cpp").exists())

    def test_new_file_from_the_tab_bar_asks_the_editor_to_open_it(self):
        result = self.api.add_project_file("sensors.cpp", activate=True)
        self.assertTrue(result["success"], result)
        payload = self.api.emit.call_args.args[1]
        self.assertEqual(payload["operation"], "add")
        self.assertTrue(payload["activate"])
        self.assertTrue(self.api.add_project_file("plain.h")["success"])
        self.assertNotIn("activate", self.api.emit.call_args.args[1])

    def test_source_revision_enters_the_existing_serialized_metadata_payload(self):
        self.api.current_board = self.api.current_port = ""
        self.api.current_baud = 115200
        self.api._queue_hardware_state_write = Mock()
        self.api._project_files_revision = 7
        web_bridge.MCUWebBackendAPI._sync_project_hardware_state(self.api)
        args = self.api._queue_hardware_state_write.call_args.args
        self.assertEqual(args[1][-1], 7)
        state = json.loads(args[2])
        self.assertEqual(state["source_revision"], 7)
        self.assertEqual(state["hardware"]["mcu_connected"], False)
        self.assertEqual(state["hardware"]["board_selected"], False)

    def test_new_renderer_resets_file_event_generation_before_loading(self):
        load = Mock()
        host = SimpleNamespace(_project_files_generation=7, _editor_html=ROOT / "src/editor/index.html",
            _bridge=SimpleNamespace(begin_renderer_load=lambda: "replacement-renderer"),
            _performance=SimpleNamespace(constrained=False),
            _view=SimpleNamespace(load=load))
        backdrop = dict(width=1, height=1, x=0, y=0, radius=16)
        with patch("main.core.config.get_theme_mode", return_value="default"), \
             patch("main.core.config.get_editor_font_size", return_value=14), \
             patch("main.qt.editor_panel.page_backdrop_for", return_value=backdrop):
            MonacoEditorPanel._load_editor(host)
        self.assertEqual(host._project_files_generation, 0)
        query = QUrlQuery(load.call_args.args[0])
        self.assertEqual(query.queryItemValue("renderer"), "replacement-renderer")
        self.assertEqual(query.queryItemValue("theme"), "default")
        self.assertEqual(query.queryItemValue("font"), "14")
        self.assertEqual(query.queryItemValue("constrained"), "0")

    def test_rename_and_delete_refuse_the_affected_dirty_source(self):
        before = (self.root / "Sketch.ino").read_bytes()
        for operation in (lambda: self.api.rename_project_file("Sketch.ino", "renamed.ino"),
                          lambda: self.api.delete_project_file("Sketch.ino")):
            result = operation()
            self.assertFalse(result["success"])
            self.assertIn("unsaved changes", result["error"])
        self.assertEqual((self.root / "Sketch.ino").read_bytes(), before)
        self.assertFalse((self.root / "renamed.ino").exists())
        self.api.emit.assert_not_called()
        self.api._sync_project_hardware_state.assert_not_called()


class WorkspaceFileActionChecks(unittest.TestCase):
    """The + chip's dialog and a tab's menu, with the backend mocked."""

    def test_new_file_dialog_always_asks_for_an_extension(self):
        from main.qt.new_file_dialog import NewFileDialog
        backend = Mock()
        backend.add_project_file.return_value = {"success": True, "path": "/fixture/sensors.cpp"}
        dialog = NewFileDialog(backend)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual([dialog.extension_combo.itemText(i) for i in range(dialog.extension_combo.count())],
                         [".h", ".cpp", ".ino", ".hpp", ".c", ".txt"])
        dialog.name_edit.setText("a/b")
        dialog._create()
        backend.add_project_file.assert_not_called()
        self.assertFalse(dialog.status.isHidden())
        dialog.name_edit.setText("motor.hpp")
        self.assertEqual(dialog.extension_combo.currentText(), ".hpp")
        self.assertFalse(dialog.extension_combo.isEnabled(), "a typed extension decides; the selector follows")
        dialog.name_edit.setText("sensors")
        self.assertTrue(dialog.extension_combo.isEnabled())
        dialog.extension_combo.setCurrentText(".cpp")
        dialog._create()
        backend.add_project_file.assert_called_once_with("sensors.cpp", activate=True)
        self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
        refused = Mock()
        refused.add_project_file.return_value = {"success": False, "error": "File 'x.h' already exists."}
        again = NewFileDialog(refused)
        self.addCleanup(again.deleteLater)
        again.name_edit.setText("x")
        again._create()
        self.assertIn("already exists", again.status.text())
        self.assertNotEqual(again.result(), again.DialogCode.Accepted)

    def host(self, busy=False):
        from main.qt.main_window import MCUMainWindow
        parent = QWidget()
        self.addCleanup(parent.deleteLater)
        backend = Mock()
        backend.delete_project_file.return_value = {"success": True, "bak": "/fixture/bak/helper.cpp"}
        backend.rename_project_file.return_value = {"success": True}
        host = SimpleNamespace(_backend=backend, _is_busy=lambda: busy, _file_action_parent=lambda: parent,
                               _on_notification=Mock(), _rename_project_file=Mock(), _delete_project_file=Mock(),
                               _request_new_project_file=Mock())
        host._refuse_file_change_while_busy = lambda: MCUMainWindow._refuse_file_change_while_busy(host)
        return MCUMainWindow, host

    def test_tab_menu_offers_rename_delete_and_new_file(self):
        # QMenu.exec is a C++ overload a class patch cannot intercept; the window's own
        # seam chooses instead, so no fixture ever opens a real modal menu.
        window, host = self.host()
        seen = []
        for wanted in ("Rename…", "Delete", "New file…"):
            def choose(menu, _position, wanted=wanted):
                seen.append([(action.text(), action.isEnabled()) for action in menu.actions() if action.text()])
                return next(action for action in menu.actions() if action.text() == wanted)
            host._exec_file_menu = choose
            window._show_project_file_menu(host, "/fixture/Sketch/helper.cpp")
        host._rename_project_file.assert_called_once_with("helper.cpp")
        host._delete_project_file.assert_called_once_with("helper.cpp")
        host._request_new_project_file.assert_called_once_with()
        self.assertEqual(seen[0], [("Rename…", True), ("Delete", True), ("New file…", True)])
        _window, busy_host = self.host(busy=True)
        busy_host._exec_file_menu = lambda menu, _position: seen.append(
            [a.isEnabled() for a in menu.actions() if a.text()])
        window._show_project_file_menu(busy_host, "/fixture/Sketch/helper.cpp")
        self.assertEqual(seen[-1], [False, False, False], "no file changes while an action runs")

    def test_delete_confirms_with_the_bak_folder_and_cancel_keeps_the_file(self):
        from PySide6.QtWidgets import QMessageBox
        window, host = self.host()
        texts = []

        def answer(choice):
            def clicked(box):
                texts.append(box.informativeText())
                for button in box.buttons():
                    if box.buttonRole(button) == choice:
                        return button
            return clicked
        with patch.object(QMessageBox, "exec", lambda box: 0), \
             patch.object(QMessageBox, "clickedButton", answer(QMessageBox.ButtonRole.RejectRole)):
            window._delete_project_file(host, "helper.cpp")
        host._backend.delete_project_file.assert_not_called()
        with patch.object(QMessageBox, "exec", lambda box: 0), \
             patch.object(QMessageBox, "clickedButton", answer(QMessageBox.ButtonRole.DestructiveRole)):
            window._delete_project_file(host, "helper.cpp")
        host._backend.delete_project_file.assert_called_once_with("helper.cpp")
        self.assertIn("bak", texts[0])
        self.assertIn("bak", host._on_notification.call_args.args[0]["message"])

    def test_busy_workspace_refuses_new_files(self):
        from PySide6.QtWidgets import QMessageBox
        window, host = self.host(busy=True)
        with patch.object(QMessageBox, "warning") as warning, \
             patch("main.qt.new_file_dialog.NewFileDialog") as dialog:
            window._request_new_project_file(host)
        warning.assert_called_once()
        dialog.assert_not_called()


FIXTURE = r"""
(() => {
  const f = {root:'/fixture/Sketch', files:[
    {name:'Sketch.ino', path:'/fixture/Sketch/Sketch.ino'},
    {name:'helper.cpp', path:'/fixture/Sketch/helper.cpp'},
    {name:'remove.h', path:'/fixture/Sketch/remove.h'}
  ], texts:{}, calls:[], orders:[]};
  for (const file of f.files) f.texts[file.path] = '// ' + file.name + '\n// saved text\n';
  window.__filesFixture = f;
  const methods = {
    get_project_dir:() => f.root,
    get_project_files:() => f.files,
    read_file:path => ({success:true,content:f.texts[path] ?? ''}),
    get_theme_mode:() => 'default', get_font_size:() => 14,
    get_recovery_buffers:() => ({}), recovery_complete:() => true,
    get_ai_edit_reviews:() => [], get_ai_history_state:() => ({canUndo:false,canRedo:false}),
    mark_modified:() => true, update_editor_buffer:() => true,
    snapshot_buffer:() => true, on_editor_content_change:() => true,
    realtime_check_syntax:() => 'null',
    set_active_file:() => true, set_cursor_position:() => true,
    save_project_tab_order:(renderer,root,paths) => {f.orders.push({root,paths});return root===f.root;},
    save_tab_order:() => {throw new Error('Incremental refresh requires project-bound persistence');},
    request_new_file:() => ({success:true}),
    request_file_menu:() => ({success:true}),
    save_file:() => {throw new Error('The renderer fixture cannot write sources');},
    run_action:() => {throw new Error('The renderer fixture cannot start a hardware action');}
  };
  f.methods = methods;
  window.pywebview = {api:new Proxy(methods, {get(target,name) {
    if (typeof target[name] !== 'function') return undefined;
    return (...args) => {
      f.calls.push({name,args});
      try {return Promise.resolve(target[name](...args));}
      catch(error) {return Promise.reject(error);}
    };
  }})};
  window.invalidateProjectFileOperations(0,f.root);
  f.change = (operation,file,extra={}) => ({path:f.root,operation,file,renderer:'',generation:0,...extra});
  // Ordinary mapping now reuses the loaded store. Delayed backend-response
  // scenarios explicitly exercise the fallback used before store paths exist.
  f.syncFromBackend = () => {
    const store=window.__mcuProjectFileStore, list=store.list;
    try {store.list=()=>[];return window.syncTabPaths();}
    finally {store.list=list;}
  };
  f.snapshot = () => ({
    active:window.__mcuProjectFileStore.active()?.path,
    files:window.__mcuProjectFileStore.list().map(file => ({path:file.path,dirty:file.isDirty,
      text:file.model.getValue(),version:file.model.getAlternativeVersionId(),undo:file.model.canUndo()})),
    tabs:Array.from(document.querySelectorAll('#tab-bar .tab'),tab=>tab._filePath)
  });
  return true;
})()
"""


class MonacoFileChecks(unittest.TestCase):
    _dispose_renderer = renderer.EditorReviewChecks._dispose_renderer
    load = renderer.EditorReviewChecks.load
    js = renderer.EditorReviewChecks.js
    async_js = renderer.EditorReviewChecks.async_js

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1000, 620)
        self.view = QWebEngineView(self.host)
        self.host_layout = QVBoxLayout(self.host)
        self.host_layout.setContentsMargins(0, 0, 0, 0)
        self.host_layout.addWidget(self.view)
        self.profile = QWebEngineProfile(self.view)
        self.page = ReviewPage(self.profile, self.view)
        self.view.setPage(self.page)
        self.addCleanup(self.assertFalse, self.page.console_issues)
        self.addCleanup(self._dispose_renderer)
        self.view.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.view.resize(1000, 620)
        self.host.show()
        self.view.show()
        self.load()
        self.js(FIXTURE)
        self.assertTrue(self.async_js("return await window.safeLoadProject();"))

    def test_descriptor_card_opens_on_ctrl_hover_or_a_three_second_rest_never_ctrl_shift(self):
        # Monaco-shaped events through the editor's own move handler: synthetic DOM pointer
        # events are not reliably forwarded by Monaco, and its forwarding is not under test.
        result = self.async_js(r"""
          const e=editorInstance, m=e.getModel();
          m.setValue('int counter = 0;\nvoid setup() { counter++; }\nvoid loop() {}\n');
          e.layout();
          const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
          for (let wait = 0; wait < 200 && typeof window.__mcuDescriptorMove !== 'function'; wait++) await sleep(25);
          await sleep(100);
          const position = new monaco.Position(2, 18);
          const at = e.getScrolledVisiblePosition(position);
          const box = e.getDomNode().getBoundingClientRect();
          const clientX = box.left + at.left + 2, clientY = box.top + at.top + 4;
          const card = document.getElementById('monaco-custom-hover-card');
          const shown = () => card.style.display === 'block' && card.style.visibility !== 'hidden';
          const move = (flags, where = position) => window.__mcuDescriptorMove({
            event: {ctrlKey:false, metaKey:false, shiftKey:false, altKey:false, buttons:0, clientX, clientY, ...flags},
            target: {position: where, element: null}});
          const release = () => window.dispatchEvent(new KeyboardEvent('keyup', {key:'Control', bubbles:true}));
          move({ctrlKey:true, shiftKey:true}); await sleep(500);
          const ctrlShift = shown();
          move({ctrlKey:true}); await sleep(600);
          const ctrl = shown();
          release(); await sleep(300);
          const releasedHides = !shown();
          move({}); await sleep(1200);
          move({});  // Resting: the same symbol does not restart the wait.
          const tooEarly = shown();
          await sleep(window.__mcuHoverDwellMs - 1200 + 600);
          const rested = shown();
          window.dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowRight', shiftKey:true, ctrlKey:true, bubbles:true}));
          await sleep(300);
          const selectionHides = !shown();
          return {ctrlShift, ctrl, releasedHides, tooEarly, rested, selectionHides, dwell: window.__mcuHoverDwellMs};
        """)
        self.assertEqual(result, {"ctrlShift": False, "ctrl": True, "releasedHides": True, "tooEarly": False,
                                  "rested": True, "selectionHides": True, "dwell": 3000})

    def test_descriptor_card_never_rests_open_while_selecting_or_over_keywords(self):
        result = self.async_js(r"""
          const e=editorInstance, m=e.getModel();
          m.setValue('int counter = 0;\nvoid setup() { counter++; }\n');
          const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
          for (let wait = 0; wait < 200 && typeof window.__mcuDescriptorMove !== 'function'; wait++) await sleep(25);
          const card = document.getElementById('monaco-custom-hover-card');
          const shown = () => card.style.display === 'block' && card.style.visibility !== 'hidden';
          const move = (flags, where) => window.__mcuDescriptorMove({
            event: {ctrlKey:false, metaKey:false, shiftKey:false, altKey:false, buttons:0, clientX:40, clientY:40, ...flags},
            target: {position: where, element: null}});
          move({buttons:1}, new monaco.Position(2, 18));
          move({shiftKey:true}, new monaco.Position(2, 19));
          await sleep(window.__mcuHoverDwellMs + 300);
          const selecting = shown();
          move({}, new monaco.Position(2, 2)); await sleep(window.__mcuHoverDwellMs + 300);
          const keyword = shown();
          return {selecting, keyword};
        """)
        self.assertEqual(result, {"selecting": False, "keyword": False})

    def test_plus_chip_and_tab_menu_ask_the_workspace_and_new_files_open(self):
        result = self.async_js(r"""
          const f=__filesFixture, s=__mcuProjectFileStore;
          const plus=document.getElementById('tab-add');
          const tabBar=document.getElementById('tab-bar');
          const placedAfterTabs=plus && plus.previousElementSibling===tabBar;
          plus.click();
          const helper=document.querySelectorAll('#tab-bar .tab')[1];
          const event=new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:5,clientY:5});
          helper.querySelector('span').dispatchEvent(event);
          await new Promise(resolve=>setTimeout(resolve,0));
          const calls=f.calls.filter(call=>call.name==='request_new_file'||call.name==='request_file_menu');
          const activeAfterMenu=s.active()?.path;
          f.files.push({name:'opened.cpp',path:f.root+'/opened.cpp'});
          const added=await reconcileProjectFiles(f.change('add',f.root+'/opened.cpp',
            {revision:1,content:'// new\n',activate:true}));
          const noDetachedModify=!document.querySelector('#detached-action-bar [data-action="modify"]');
          return {placedAfterTabs, defaultPrevented:event.defaultPrevented,
                  calls:calls.map(call=>[call.name,...call.args]), activeAfterMenu,
                  added, active:s.active()?.path, label:plus.getAttribute('aria-label'), noDetachedModify};
        """)
        self.assertEqual(result, {
            "placedAfterTabs": True, "defaultPrevented": True,
            "calls": [["request_new_file"], ["request_file_menu", "/fixture/Sketch/helper.cpp"]],
            "activeAfterMenu": "/fixture/Sketch/helper.cpp", "added": True,
            "active": "/fixture/Sketch/opened.cpp", "label": "New file", "noDetachedModify": True})

    def test_tab_mapping_reuses_loaded_sources_without_another_backend_listing(self):
        result = self.async_js(r"""
          const f=__filesFixture;
          const before=f.calls.filter(call=>call.name==='get_project_files').length;
          const files=await syncTabPaths();
          return {extraListings:f.calls.filter(call=>call.name==='get_project_files').length-before,
            paths:files.map(file=>file.path),
            tabs:Array.from(document.querySelectorAll('#tab-bar .tab'),tab=>tab._filePath)};
        """)
        expected = ["/fixture/Sketch/Sketch.ino", "/fixture/Sketch/helper.cpp", "/fixture/Sketch/remove.h"]
        self.assertEqual(result, {"extraListings": 0, "paths": expected, "tabs": expected})

    def test_add_rename_delete_preserve_unrelated_models_undo_dirty_tabs_and_order(self):
        self.js(r"""(() => {
          const f=__filesFixture,s=__mcuProjectFileStore,e=editorInstance;
          f.original=s.list();
          s.activate(f.original[1]);e.pushUndoStop();
          e.executeEdits('fixture',[{range:new monaco.Range(1,1,1,1),text:'// helper unsaved 😀\n'}]);e.pushUndoStop();
          s.activate(f.original[0]);e.pushUndoStop();
          e.executeEdits('fixture',[{range:new monaco.Range(1,1,1,1),text:'// main unsaved Ω\n'}]);e.pushUndoStop();
          e.setSelection(new monaco.Selection(2,1,2,7));e.focus();
          document.getElementById('tab-bar').appendChild(document.querySelector('#tab-bar .tab.active'));
          f.before=f.snapshot();f.selection=e.getSelection();
          f.files.push({name:'fresh.c',path:f.root+'/fresh.c'});f.texts[f.root+'/fresh.c']='// fresh C\n';
          return true;
        })()""")
        self.assertTrue(self.async_js(r"""
          const f=__filesFixture;
          return await reconcileProjectFiles(f.change('add',f.root+'/fresh.c',{revision:1,content:'// fresh C\n'}));
        """))
        result = self.js(r"""(() => {
          const f=__filesFixture,s=__mcuProjectFileStore,e=editorInstance;
          return {before:f.before,after:f.snapshot(),same:f.original.every(file=>s.list().includes(file)&&!file.model.isDisposed()),
            selection:e.getSelection(),oldSelection:f.selection,focus:e.hasTextFocus(),orders:f.orders};
        })()""")
        self.assertTrue(result["same"])
        self.assertTrue(result["focus"])
        self.assertEqual(result["selection"], result["oldSelection"])
        self.assertEqual(result["before"]["files"], result["after"]["files"][:3])
        self.assertEqual(result["before"]["tabs"], result["after"]["tabs"][:3])
        self.assertEqual(result["before"]["active"], result["after"]["active"])
        self.assertTrue(result["after"]["files"][0]["undo"])
        self.assertTrue(result["after"]["files"][1]["undo"])
        self.assertEqual(self.async_js(r"""
          const f=__filesFixture,old=f.root+'/remove.h',renamed=f.root+'/renamed.h';
          f.files.find(file=>file.path===old).path=renamed;
          f.files.find(file=>file.path===renamed).name='renamed.h';
          const ok=await reconcileProjectFiles(f.change('rename',renamed,{old_file:old,revision:2}));
          const renamedTab=document.querySelectorAll('#tab-bar .tab')[1];
          const same=__mcuProjectFileStore.list().includes(f.original[2])&&f.original[2].path===renamed;
          const fresh=__mcuProjectFileStore.list().find(file=>file.path===f.root+'/fresh.c');
          f.files=f.files.filter(file=>file.path!==fresh.path);
          const deleted=await reconcileProjectFiles(f.change('delete',fresh.path,{revision:3}));
          return {ok,deleted,same,renamedAtSameIndex:renamedTab._filePath===renamed,
            disposed:fresh.model.isDisposed(),dirtyRetained:JSON.stringify(f.before.files.slice(0,2))===JSON.stringify(f.snapshot().files.slice(0,2))};
        """), {"ok": True, "deleted": True, "same": True, "renamedAtSameIndex": True,
                "disposed": True, "dirtyRetained": True})
        self.assertTrue(self.async_js(r"""
          const f=__filesFixture,s=__mcuProjectFileStore,removed=f.original[2];
          s.activate(removed);f.files=f.files.filter(file=>file.path!==removed.path);
          const ok=await reconcileProjectFiles(f.change('delete',removed.path,{revision:4}));
          return ok&&removed.model.isDisposed()&&s.active()===f.original[0]&&
            f.original.slice(0,2).every(file=>!file.model.isDisposed()&&file.model.canUndo());
        """))

    def test_rapid_add_rename_and_delete_use_known_bytes_and_drop_duplicate_events(self):
        result = self.async_js(r"""
          const f=__filesFixture,a=f.root+'/A.h',b=f.root+'/B.h',c=f.root+'/C.h';
          f.files.push({name:'B.h',path:b});f.texts[b]='#pragma once\n';
          const before=f.calls.filter(call=>call.name==='read_file').length;
          const renamed=await Promise.all([
            reconcileProjectFiles(f.change('add',a,{revision:1,content:'#pragma once\n'})),
            reconcileProjectFiles(f.change('rename',b,{revision:2,old_file:a}))]);
          const deleted=await Promise.all([
            reconcileProjectFiles(f.change('add',c,{revision:3,content:'#pragma once\n'})),
            reconcileProjectFiles(f.change('delete',c,{revision:4}))]);
          const duplicates=await reconcileProjectFiles(f.change('add',c,{revision:3,content:'must not return'}));
          return {renamed,deleted,duplicates,reads:f.calls.filter(call=>call.name==='read_file').length-before,
            paths:__mcuProjectFileStore.list().map(file=>file.path)};
        """)
        self.assertEqual(result["renamed"], [True, True])
        self.assertEqual(result["deleted"], [True, True])
        self.assertTrue(result["duplicates"])
        self.assertEqual(result["reads"], 0)
        self.assertEqual(result["paths"][-1], "/fixture/Sketch/B.h")
        self.assertNotIn("/fixture/Sketch/C.h", result["paths"])

    def test_pending_old_project_and_renderer_events_cannot_mutate_the_new_project(self):
        self.js(r"""(() => {
          const f=__filesFixture;
          f.methods.get_project_dir=()=>new Promise(resolve=>{f.rootReply=resolve;});
          f.pending=reconcileProjectFiles(f.change('add',f.root+'/stale.h',{revision:1,content:'stale'}));
          f.done=false;f.pending.then(value=>{f.done=value;});return true;
        })()""")
        until(lambda: self.js("Boolean(__filesFixture.rootReply)"))
        result = self.async_js(r"""
          const f=__filesFixture,old=f.root;
          f.root='/fixture/Next';f.files=[{name:'Next.ino',path:f.root+'/Next.ino'}];f.texts[f.files[0].path]='// next';
          invalidateProjectFileOperations(1,f.root);await safeLoadProject();
          f.rootReply(old);await f.pending;
          f.methods.get_project_dir=()=>f.root;
          const staleRenderer=await reconcileProjectFiles({path:f.root,operation:'add',file:f.root+'/bad.h',
            content:'bad',renderer:'outgoing-renderer',generation:1,revision:2});
          return {staleRenderer,paths:__mcuProjectFileStore.list().map(file=>file.path),orders:f.orders};
        """)
        self.assertTrue(result["staleRenderer"])
        self.assertEqual(result["paths"], ["/fixture/Next/Next.ino"])
        self.assertEqual(result["orders"], [])

    def test_outgoing_renderer_cannot_invalidate_the_replacement_generation(self):
        self.assertEqual(self.js(r"""(() => {
          const rejected=invalidateProjectFileOperations(9,'/fixture/Other','outgoing-renderer');
          return {rejected,generation:window.__mcuProjectFilesGeneration};
        })()"""), {"rejected": False, "generation": 0})

    def test_final_rename_already_loaded_retains_one_model_for_the_destination(self):
        result = self.async_js(r"""
          const f=__filesFixture,a=f.root+'/A.h',b=f.root+'/B.h';
          f.files.push({name:'B.h',path:b});f.texts[b]='// actual final source\n';
          await safeLoadProject();
          const loaded=__mcuProjectFileStore.list().find(file=>file.path===b);
          const results=await Promise.all([
            reconcileProjectFiles(f.change('add',a,{revision:1,content:'// actual final source\n'})),
            reconcileProjectFiles(f.change('rename',b,{revision:2,old_file:a}))]);
          const destinations=__mcuProjectFileStore.list().filter(file=>file.path===b);
          return {results,same:destinations[0]===loaded,count:destinations.length,
            text:loaded.model.getValue(),tabs:Array.from(document.querySelectorAll('#tab-bar .tab'),tab=>tab._filePath)};
        """)
        self.assertEqual(result["results"], [True, True])
        self.assertTrue(result["same"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["text"], "// actual final source\n")
        self.assertNotIn("/fixture/Sketch/A.h", result["tabs"])

    def test_incremental_event_waits_for_the_current_full_project_load(self):
        self.js(r"""(() => {
          const f=__filesFixture;
          f.root='/fixture/Next';f.files=[{name:'Next.ino',path:f.root+'/Next.ino'}];
          f.texts[f.files[0].path]='// next project\n';
          invalidateProjectFileOperations(1,f.root);
          const read=f.methods.read_file;
          f.methods.read_file=path=>path===f.root+'/Next.ino'
            ? new Promise(resolve=>{f.loadReply=()=>resolve(read(path));}) : read(path);
          f.loading=safeLoadProject();
          f.event=reconcileProjectFiles({path:f.root,operation:'add',file:f.root+'/added.h',content:'#pragma once\n',
            renderer:'',generation:1,revision:1});
          f.eventFinished=false;f.event.then(()=>{f.eventFinished=true;});return true;
        })()""")
        until(lambda: self.js("Boolean(__filesFixture.loadReply)"))
        self.assertFalse(self.js("__filesFixture.eventFinished"))
        result = self.async_js(r"""
          const f=__filesFixture;f.loadReply();await f.loading;const ok=await f.event;
          return {ok,paths:__mcuProjectFileStore.list().map(file=>file.path),
            texts:__mcuProjectFileStore.list().map(file=>file.model.getValue())};
        """)
        self.assertTrue(result["ok"])
        self.assertEqual(result["paths"], ["/fixture/Next/Next.ino", "/fixture/Next/added.h"])
        self.assertEqual(result["texts"], ["// next project\n", "#pragma once\n"])

    def test_tab_mapping_reply_from_an_older_generation_cannot_remap_new_tabs(self):
        self.js(r"""(() => {
          const f=__filesFixture,list=f.methods.get_project_files;
          let first=true;
          f.methods.get_project_files=()=>{
            if (!first) return list();
            first=false;return new Promise(resolve=>{f.listReply=resolve;});
          };
          f.mapping=f.syncFromBackend();return true;
        })()""")
        until(lambda: self.js("Boolean(__filesFixture.listReply)"))
        result = self.async_js(r"""
          const f=__filesFixture,oldFiles=f.files;
          f.root='/fixture/Next';f.files=[{name:'Sketch.ino',path:f.root+'/Sketch.ino'}];
          f.texts[f.files[0].path]='// new project with the same filename\n';
          invalidateProjectFileOperations(1,f.root);await safeLoadProject();
          f.listReply(oldFiles);await f.mapping;
          return {list:projectFileList.map(file=>file.path),
            tabs:Array.from(document.querySelectorAll('#tab-bar .tab'),tab=>tab._filePath)};
        """)
        self.assertEqual(result["list"], ["/fixture/Next/Sketch.ino"])
        self.assertEqual(result["tabs"], ["/fixture/Next/Sketch.ino"])

    def test_same_generation_listing_cannot_restore_renamed_or_deleted_file_metadata(self):
        for operation in ("rename", "delete"):
            with self.subTest(operation=operation):
                self.js(r"""(() => {
                  const f=__filesFixture,list=f.methods.get_project_files;
                  f.staleFiles=f.files.map(file=>({...file}));
                  let first=true;
                  f.listReply=null;f.saveReply=null;f.eventFinished=false;
                  f.methods.get_project_files=()=>{
                    if (!first) return list();
                    first=false;return new Promise(resolve=>{f.listReply=resolve;});
                  };
                  f.methods.save_project_tab_order=()=>new Promise(resolve=>{f.saveReply=resolve;});
                  f.mapping=f.syncFromBackend();return true;
                })()""")
                until(lambda: self.js("Boolean(__filesFixture.listReply)"))
                self.js(r"""(() => {
                  const f=__filesFixture,operation=OPERATION,old=f.root+'/remove.h',renamed=f.root+'/renamed.h';
                  const change=operation==='rename'
                    ? f.change('rename',renamed,{old_file:old,revision:1})
                    : f.change('delete',renamed,{revision:2});
                  f.files=operation==='rename'
                    ? f.files.map(file=>file.path===old?{name:'renamed.h',path:renamed}:file)
                    : f.files.filter(file=>file.path!==renamed);
                  f.event=reconcileProjectFiles(change);
                  f.event.then(()=>{f.eventFinished=true;});return true;
                })()""".replace("OPERATION", json.dumps(operation)))
                until(lambda: self.js("Boolean(__filesFixture.saveReply)"))
                # An old listing may finish while tab-order persistence is
                # awaiting its native reply. It must not overwrite the fresh
                # store metadata even before reconciliation finishes.
                waiting = self.async_js(r"""
                  const f=__filesFixture;
                  f.listReply(f.staleFiles);await f.mapping;
                  return {finished:f.eventFinished,list:projectFileList.map(file=>file.path),
                    tabs:Array.from(document.querySelectorAll('#tab-bar .tab'),tab=>tab._filePath)};
                """)
                expected = ["/fixture/Sketch/Sketch.ino", "/fixture/Sketch/helper.cpp"]
                if operation == "rename":
                    expected.append("/fixture/Sketch/renamed.h")
                self.assertFalse(waiting["finished"])
                self.assertEqual(waiting["list"], expected)
                self.assertEqual(waiting["tabs"], expected)
                finished = self.async_js(r"""
                  const f=__filesFixture;f.saveReply(true);const ok=await f.event;
                  return {ok,list:projectFileList.map(file=>file.path),
                    tabs:Array.from(document.querySelectorAll('#tab-bar .tab'),tab=>tab._filePath)};
                """)
                self.assertTrue(finished["ok"])
                self.assertEqual(finished["list"], expected)
                self.assertEqual(finished["tabs"], expected)


if __name__ == "__main__":
    run = unittest.main(verbosity=2, exit=False)
    QTimer.singleShot(150, APP.quit)
    APP.exec()
    raise SystemExit(0 if run.result.wasSuccessful() else 1)
