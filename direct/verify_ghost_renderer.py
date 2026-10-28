#!/usr/bin/env python3
"""Ghost promotion preserves real offline Monaco models and native save intent.

Sources are in-memory fixtures. No project cache, settings, hardware or cloud
service is read or written by these checks.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent.parent
NATIVE_FIXTURES = ROOT / 'temp/audit/ghost-renderer-fixtures'
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from main.qt.editor_panel import EditorBridgeAPI, MonacoEditorPanel
from direct import verify_editor_review_ui as renderer
from direct.verify_project_file_lifecycle import FIXTURE


class NativeSaveChecks(unittest.TestCase):
    def test_webchannel_actions_observe_workspace_ghost_lock_after_copy_finishes(self):
        for detached in (False, True):
            with self.subTest(detached=detached):
                workspace = SimpleNamespace(_shortcut_save=Mock(), _shortcut_save_all=Mock(),
                    _ghost_save_in_progress=True, _ghost_sketch=SimpleNamespace(needs_recovery=False))
                host = SimpleNamespace(parentWidget=lambda: workspace) if detached else workspace
                panel = QObject()
                panel.window = lambda: host
                panel.request_workspace_save = lambda all_files: MonacoEditorPanel.request_workspace_save(panel, all_files)
                backend = SimpleNamespace(run_action=Mock(return_value={'success':True}), _ghost_project_operation=False)
                bridge = EditorBridgeAPI(backend, panel)
                self.assertFalse(bridge.run_action('compile')['success'])
                self.assertFalse(bridge.run_action('upload')['success'])
                backend.run_action.assert_not_called()
                workspace._ghost_save_in_progress = False
                workspace._ghost_sketch.needs_recovery = True
                self.assertFalse(bridge.run_action('reload')['success'])
                backend.run_action.assert_not_called()
                self.assertTrue(bridge.run_action('save')['success'])
                self.assertTrue(bridge.run_action('save_all')['success'])
                workspace._shortcut_save.assert_called_once_with()
                workspace._shortcut_save_all.assert_called_once_with()
                backend.run_action.assert_not_called()
                self.assertTrue(bridge.run_action('stop')['success'])
                backend.run_action.assert_called_once_with('stop')
                workspace._ghost_sketch.needs_recovery = False
                self.assertTrue(bridge.run_action('compile')['success'])
                self.assertEqual(backend.run_action.call_args.args, ('compile',))

    def test_webchannel_save_intent_reaches_its_workspace_even_when_detached(self):
        workspace = SimpleNamespace(_shortcut_save=Mock(), _shortcut_save_all=Mock())
        detached = SimpleNamespace(parentWidget=lambda: workspace)
        panel = QObject()
        panel.window = lambda: detached
        panel.request_workspace_save = lambda all_files: MonacoEditorPanel.request_workspace_save(panel, all_files)
        bridge = EditorBridgeAPI(None, panel)
        bridge.request_workspace_save(False)
        bridge.request_workspace_save(True)
        workspace._shortcut_save.assert_called_once_with()
        workspace._shortcut_save_all.assert_called_once_with()

    def test_project_path_remap_retains_dirty_recovery_snapshots(self):
        saved = NATIVE_FIXTURES / 'saved/Sketch'
        source = str(NATIVE_FIXTURES / 'ghost/Sketch/Sketch.ino')
        destination = str(saved / 'Sketch.ino')
        unrelated = str(NATIVE_FIXTURES / 'unrelated/a.h')
        backend = SimpleNamespace(sketch_dir_path=saved)
        bridge = EditorBridgeAPI(backend)
        bridge._buffer_snapshots = {source: 'late edit', unrelated: 'other'}
        bridge._recovery_buffers = {source: 'recover me'}
        bridge._recovering_buffers = True
        bridge.remap_project_paths({source: destination})
        self.assertEqual(bridge._buffer_snapshots, {destination: 'late edit', unrelated: 'other'})
        self.assertEqual(bridge.get_recovery_buffers(), {destination: 'recover me'})
        self.assertTrue(bridge._recovering_buffers)

    def test_native_close_callback_waits_for_current_renderer_acknowledgement(self):
        class Panel(QWidget):
            promote_ghost_paths = MonacoEditorPanel.promote_ghost_paths

        panel = Panel()
        self.addCleanup(panel.deleteLater)
        saved, ghost = NATIVE_FIXTURES / 'saved/Sketch', NATIVE_FIXTURES / 'ghost/Sketch'
        panel._backend = SimpleNamespace(sketch_dir_path=saved)
        panel._bridge = EditorBridgeAPI(panel._backend, panel)
        panel._bridge._renderer_token = 'renderer-current'
        page = SimpleNamespace(runJavaScript=Mock())
        panel._view = SimpleNamespace(page=lambda: page)
        payload = {'path': str(saved), 'old_path': str(ghost),
                   'path_mapping': {str(ghost / 'Sketch.ino'): str(saved / 'Sketch.ino')}}
        accepted, failed = Mock(), Mock()
        panel.promote_ghost_paths(payload)
        panel.promote_ghost_paths(payload, accepted, failed)
        token = panel._ghost_promotion_pending['token']
        self.assertEqual(page.runJavaScript.call_count, 1)
        accepted.assert_not_called()
        panel._bridge.finish_ghost_path_promotion(token, 'renderer-current', True)
        accepted.assert_called_once_with()
        failed.assert_not_called()
        repeated = Mock()
        panel.promote_ghost_paths(payload, repeated, failed)
        repeated.assert_called_once_with()
        self.assertEqual(page.runJavaScript.call_count, 1)


class GhostMonacoChecks(unittest.TestCase):
    _dispose_renderer = renderer.EditorReviewChecks._dispose_renderer
    load = renderer.EditorReviewChecks.load
    js = renderer.EditorReviewChecks.js
    async_js = renderer.EditorReviewChecks.async_js

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1000, 620)
        self.view = QWebEngineView(self.host)
        layout = QVBoxLayout(self.host)
        layout.addWidget(self.view)
        self.profile = QWebEngineProfile(self.view)
        self.page = renderer.ReviewPage(self.profile, self.view)
        self.view.setPage(self.page)
        self.addCleanup(self.assertFalse, self.page.console_issues)
        self.addCleanup(self._dispose_renderer)
        self.view.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.host.show()
        self.view.show()
        self.load()
        self.js(FIXTURE)
        self.assertTrue(self.async_js('return await window.safeLoadProject();'))

    def test_promotion_preserves_models_undo_selection_dirty_text_and_tab_order(self):
        result = self.async_js(r"""
          const f=__filesFixture,s=__mcuProjectFileStore,e=editorInstance;
          f.original=s.list();s.activate(f.original[1]);e.pushUndoStop();
          e.executeEdits('fixture',[{range:new monaco.Range(1,1,1,1),text:'// late unsaved Ω😀\n'}]);e.pushUndoStop();
          e.setSelection(new monaco.Selection(2,1,2,6));e.focus();
          document.getElementById('tab-bar').prepend(document.querySelector('#tab-bar .tab.active'));
          const before=f.snapshot(),selection=e.getSelection(),mapping={};
          for(const file of f.files) mapping[file.path]='/saved/Chosen/'+file.name;
          f.root='/saved/Chosen';f.files=f.files.map(file=>({...file,path:mapping[file.path]}));
          invalidateProjectFileOperations(1,f.root);
          const ok=await promoteGhostProject({path:f.root,old_path:'/fixture/Sketch',path_mapping:mapping,
            renderer:'',generation:1,operation:'ghost_saved',preserve_buffers:true});
          const after=f.snapshot();
          return {ok,same:f.original.every(file=>s.list().includes(file)&&!file.model.isDisposed()),
            before,after,selection:e.getSelection(),oldSelection:selection,focus:e.hasTextFocus(),
            remapped:after.files.every(file=>file.path.startsWith(f.root+'/')),
            snapshotCalls:f.calls.filter(call=>call.name==='update_editor_buffer'),orders:f.orders};
        """)
        self.assertTrue(result['ok'])
        self.assertTrue(result['same'])
        self.assertTrue(result['focus'])
        self.assertTrue(result['remapped'])
        self.assertEqual(result['selection'], result['oldSelection'])
        self.assertEqual([f['text'] for f in result['before']['files']],
                         [f['text'] for f in result['after']['files']])
        self.assertEqual([f['dirty'] for f in result['before']['files']],
                         [f['dirty'] for f in result['after']['files']])
        self.assertEqual([f['undo'] for f in result['before']['files']],
                         [f['undo'] for f in result['after']['files']])
        self.assertEqual([p.rsplit('/', 1)[-1] for p in result['before']['tabs']],
                         [p.rsplit('/', 1)[-1] for p in result['after']['tabs']])
        self.assertTrue(result['snapshotCalls'])
        self.assertTrue(all(call['args'][0].startswith('/saved/Chosen/') for call in result['snapshotCalls']))
        self.assertEqual(result['orders'][-1]['paths'], result['after']['tabs'])

    def test_incomplete_or_stale_promotion_never_partially_relocates_tabs(self):
        result = self.async_js(r"""
          const f=__filesFixture,s=__mcuProjectFileStore,before=f.snapshot();
          const mapping=Object.fromEntries(f.files.map(file=>[file.path,'/saved/'+file.name]));
          f.root='/saved';invalidateProjectFileOperations(1,f.root);
          const incomplete=await promoteGhostProject({path:f.root,old_path:'/fixture/Sketch',
            path_mapping:{'/fixture/Sketch/Sketch.ino':'/saved/Sketch.ino'},renderer:'',generation:1});
          const stale=await promoteGhostProject({path:f.root,old_path:'/fixture/Sketch',
            path_mapping:mapping,renderer:'obsolete-renderer',generation:1});
          return {incomplete,stale,before,after:f.snapshot()};
        """)
        self.assertFalse(result['incomplete'])
        self.assertFalse(result['stale'])
        self.assertEqual(result['before'], result['after'])

    def test_ctrl_s_requests_native_save_once_without_flushing_ghost_files(self):
        result = self.js(r"""(() => {
          const f=__filesFixture;f.methods.request_workspace_save=all=>true;
          const event=new KeyboardEvent('keydown',{key:'s',ctrlKey:true,bubbles:true,cancelable:true});
          (document.querySelector('textarea') || document.body).dispatchEvent(event);
          return {prevented:event.defaultPrevented,
            saves:f.calls.filter(call=>call.name==='request_workspace_save'),
            writes:f.calls.filter(call=>call.name==='save_file'||call.name==='save_all_files')};
        })()""")
        self.assertTrue(result['prevented'])
        self.assertEqual(result['saves'], [{'name': 'request_workspace_save', 'args': [False]}])
        self.assertEqual(result['writes'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
