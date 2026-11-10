#!/usr/bin/env python3
"""Real offline Monaco pairing, preview isolation and acknowledged save races."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'direct'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import verify_embedded_viewports as embedded
from main.qt.editor_panel import EditorBridgeAPI


class EditorFeatures(unittest.TestCase):
    js = embedded.EmbeddedViewportChecks.js
    state = embedded.EmbeddedViewportChecks.state
    load = embedded.EmbeddedViewportChecks.load
    dispose = embedded.EmbeddedViewportChecks.dispose

    def setUp(self):
        directory = ROOT / 'temp/audit/editor-features'
        directory.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.temporary.cleanup)
        self.project = str(Path(self.temporary.name) / 'Sketch Ω')
        self.path = str(Path(self.project) / 'Sketch.cpp')
        embedded.EmbeddedViewportChecks.setUp(self)
        self.load('index.html')
        embedded.wait_for(lambda: self.js('!!window.editorInstance && !!window.__mcuProjectFileStore && !!window.navigateSourceAtPosition'),
                          'The production editor scripts did not initialize')
        self.js(f"window.fixture = {{path:{json.dumps(self.path)}, root:{json.dumps(self.project)}, saves:[], buffers:[], hold:false}}; true")
        self.js(r"""
          fixture.files = {[fixture.path]:'void stepRun() {}\nvoid stepRun(int value) {}\nvoid run() { stepRun(4); }\n'};
          window.pywebview = {api:{
            get_project_files:async()=>Object.keys(fixture.files).map(path=>({path,name:path.split(/[\\/]/).pop()})),
            get_project_dir:async()=>fixture.root,
            read_file:async path=>({success:true,content:fixture.files[path]}),
            save_file:async(path,content)=>{
              fixture.saves.push({path,content});
              if(fixture.hold) return await new Promise(resolve=>fixture.saved=resolve);
              return {success:true};
            },
            set_active_file:async()=>{}, mark_modified:async()=>{},
            update_editor_buffer:async(path,content)=>{fixture.buffers.push({path,content});},
            snapshot_buffer:async()=>{}, on_editor_content_change:async()=>{},
            get_tab_order:async()=>[], save_tab_order:async()=>{},
            get_recovery_buffers:async()=>({}),
            get_renderer_recovery:async()=>({accepted:true,buffers:{}}), complete_renderer_recovery:async()=>true,
            get_ai_edit_reviews:async()=>[], get_ai_history_state:async()=>null
          }};
          fixture.loaded=false;
          window.safeLoadProject().then(value=>{fixture.loaded=value;}); true
        """)
        embedded.wait_for(lambda: self.js('fixture.loaded === true && window.__mcuProjectFileStore.list().length === 1'),
                          'The isolated project did not load through its production file store')
        self.js('fixture.languageReady=false; window.configureCppIndentation().then(()=>fixture.languageReady=true); true')
        embedded.wait_for(lambda: self.js('fixture.languageReady'), 'C++ language configuration did not load')

    def test_native_pairs_wrapping_overtype_deletion_escape_undo_and_multiple_cursors(self):
        for opening, closing in [('(', ')'), ('[', ']'), ('{', '}'), ('\"', '\"'), ("'", "'")]:
            with self.subTest(pair=opening+closing):
                result = self.state(f"""
                  const ed=editorInstance, model=ed.getModel();
                  setEditorAutoPairs(true); model.setValue(''); ed.setPosition({{lineNumber:1,column:1}});
                  ed.trigger('keyboard','type',{{text:{json.dumps(opening)}}});
                  const paired=model.getValue();
                  ed.trigger('keyboard','type',{{text:{json.dumps(closing)}}});
                  const overtyped=model.getValue();
                  model.setValue('word'); ed.setSelection(new monaco.Range(1,1,1,5));
                  ed.trigger('keyboard','type',{{text:{json.dumps(opening)}}});
                  return {{paired,overtyped,wrapped:model.getValue()}};
                """)
                self.assertEqual(result['paired'], opening+closing)
                self.assertEqual(result['overtyped'], opening+closing)
                self.assertEqual(result['wrapped'], opening+'word'+closing)
        result = self.state(r"""
          const ed=editorInstance, model=ed.getModel();
          model.setValue(''); ed.setPosition({lineNumber:1,column:1});
          ed.trigger('keyboard','type',{text:'('}); ed.trigger('keyboard','deleteLeft');
          const deleted=model.getValue();
          model.setValue('a\nb'); ed.setSelections([new monaco.Selection(1,2,1,2),new monaco.Selection(2,2,2,2)]);
          ed.trigger('keyboard','type',{text:'('}); const multiple=model.getValue();
          ed.trigger('keyboard','undo'); const undone=model.getValue();
          setEditorAutoPairs(false); model.setValue(''); ed.setPosition({lineNumber:1,column:1});
          ed.trigger('keyboard','type',{text:'('}); const disabled=model.getValue();
          return {deleted,multiple,undone,disabled};
        """)
        self.assertEqual(result, {'deleted':'', 'multiple':'a()\nb()', 'undone':'a\nb', 'disabled':'('})

    def test_quote_escaping_and_block_indentation_use_the_cpp_language_configuration(self):
        prefix = '"path\\'
        result = self.state(f"""
          const ed=editorInstance, model=ed.getModel(); setEditorAutoPairs(true);
          model.setValue({json.dumps(prefix)}); ed.setPosition({{lineNumber:1,column:model.getLineMaxColumn(1)}});
          ed.trigger('keyboard','type',{{text:'"'}}); const escaped=model.getValue();
          model.setValue(''); ed.setPosition({{lineNumber:1,column:1}});
          ed.trigger('keyboard','type',{{text:'{{'}}); ed.trigger('keyboard','type',{{text:'\\n'}});
          return {{escaped, block:model.getValue(), column:ed.getPosition().column}};
        """)
        self.assertEqual(result['escaped'], prefix + '"')
        self.assertRegex(result['block'], r'^\{\r?\n[ \t]+\r?\n\}$')
        self.assertGreater(result['column'], 1)

    def preview(self):
        path = str(Path(self.temporary.name) / 'library/motor.h')
        content = 'void stepRun();\nvoid stepRun(int value);\n'
        destination = dict(path=path, content=content, name='stepRun', line=1, column=5,
                           end_line=1, end_column=12, signature='stepRun(int value)', read_only=True)
        self.js(f'fixture.definition={json.dumps(destination)}; true')
        self.js("""
          window.editorBridge={navigation_ready:{connect:fn=>fixture.reply=fn},
            request_definition:(token,id,payload,accepted)=>{
              fixture.request=JSON.parse(payload); accepted(true);
              queueMicrotask(()=>fixture.reply(token,id,{success:true,destinations:[fixture.definition]}));
            }};
          fixture.navigated=false;
          editorInstance.setPosition({lineNumber:3,column:16});
          navigateSourceAtPosition(editorInstance.getPosition()).then(value=>fixture.navigated=value); true
        """)
        embedded.wait_for(lambda: self.js('fixture.navigated'), 'The library preview did not open')
        return path, content

    def test_preview_stays_locked_through_unlock_and_save_all_preserves_dirty_root_buffer(self):
        self.js(r"editorInstance.getModel().setValue(editorInstance.getValue()+'// unsaved root\n'); true")
        original = self.js('editorInstance.getValue()')
        path, content = self.preview()
        self.assertEqual(self.js('editorInstance.getValue()'), content)
        self.assertTrue(self.js('editorInstance.getRawOptions().readOnly'))
        self.assertEqual(self.js('editorInstance.getSelection().startLineNumber'), 2)
        self.js('window.unlockEditorNow(); true')
        self.assertTrue(self.js('editorInstance.getRawOptions().readOnly'))
        self.js('fixture.flushed=false; window.saveAllFilesSafe().then(()=>fixture.flushed=true); true')
        embedded.wait_for(lambda: self.js('fixture.flushed'), 'Save All did not acknowledge project sources')
        saves = self.state('return fixture.saves;')
        self.assertEqual(saves, [{'path':self.path, 'content':original}])
        self.js("document.querySelector('[data-preview] button').click(); true")
        self.assertFalse(self.js('editorInstance.getRawOptions().readOnly'))
        self.assertEqual(self.js('editorInstance.getValue()'), original)
        self.assertEqual(self.js('__mcuProjectFileStore.list().length'), 1)
        self.assertNotIn(path, [item['path'] for item in saves])

    def test_newer_typing_during_save_is_not_marked_clean_or_reported_saved(self):
        self.js("fixture.hold=true; editorInstance.getModel().setValue('first buffer'); fixture.outcome=null;"
                "window.saveAllFilesSafe().then(()=>fixture.outcome='saved').catch(()=>fixture.outcome='retained'); true")
        embedded.wait_for(lambda: self.js('!!fixture.saved'), 'The controlled save did not start')
        self.js("editorInstance.getModel().setValue('newer unsaved buffer'); fixture.saved({success:true}); true")
        embedded.wait_for(lambda: self.js("fixture.outcome === 'retained'"), 'The save race was incorrectly acknowledged')
        self.assertEqual(self.js('editorInstance.getValue()'), 'newer unsaved buffer')
        self.assertTrue(self.js('__mcuProjectFileStore.active().isDirty'))
        self.assertTrue(self.js("document.querySelector('#editor-notice button').textContent === 'Close'"))
        self.js("document.querySelector('#editor-notice button').click(); true")
        self.assertFalse(self.js("!!document.getElementById('editor-notice')"))

    def test_preview_history_restores_call_selection_and_project_reload_disposes_preview_models(self):
        self.preview()
        self.js("fixture.back=false; editorInstance.getAction('mcu.navigation.back').run().then(()=>fixture.back=true); true")
        embedded.wait_for(lambda: self.js('fixture.back'), 'Navigation back did not complete')
        self.assertEqual(self.js('editorInstance.getSelection().startLineNumber'), 3)
        self.assertFalse(self.js('editorInstance.getRawOptions().readOnly'))
        self.js("fixture.forward=false; editorInstance.getAction('mcu.navigation.forward').run().then(()=>fixture.forward=true); true")
        embedded.wait_for(lambda: self.js('fixture.forward'), 'Navigation forward did not complete')
        self.assertTrue(self.js('editorInstance.getRawOptions().readOnly'))
        self.js('fixture.previewModel=editorInstance.getModel(); fixture.loaded=false; safeLoadProject().then(()=>fixture.loaded=true); true')
        embedded.wait_for(lambda: self.js('fixture.loaded'), 'Project reload did not finish')
        self.assertTrue(self.js('fixture.previewModel.isDisposed()'))
        self.assertEqual(self.js('__mcuProjectFileStore.list().filter(file=>file.preview).length'), 0)

    def test_navigation_reply_after_source_edit_cannot_move_the_editor(self):
        self.preview()
        self.js("document.querySelector('[data-preview] button').click(); fixture.deferred=null;"
                "editorBridge.request_definition=(token,id,payload,accepted)=>{accepted(true); fixture.deferred={token,id};};"
                "fixture.navigated=null; navigateSourceAtPosition(editorInstance.getPosition()).then(value=>fixture.navigated=value); true")
        embedded.wait_for(lambda: self.js('!!fixture.deferred'), 'The navigation request did not start')
        self.js("editorInstance.getModel().setValue('// newer root buffer');"
                "fixture.reply(fixture.deferred.token,fixture.deferred.id,{success:true,destinations:[fixture.definition]}); true")
        embedded.wait_for(lambda: self.js('fixture.navigated !== null'), 'Stale navigation was not acknowledged')
        self.assertFalse(self.js('fixture.navigated'))
        self.assertEqual(self.js('editorInstance.getValue()'), '// newer root buffer')

    def test_native_storage_save_runs_off_qt_and_keeps_the_event_loop_alive(self):
        from PySide6.QtCore import QTimer
        started, release, threads, results, ticks = threading.Event(), threading.Event(), [], [], []
        main_thread = threading.get_ident()
        def save(path, content):
            threads.append(threading.get_ident()); started.set()
            release.wait(2)
            return {'success':True}
        backend = SimpleNamespace(sketch_dir_path=Path(self.project), save_file=save)
        bridge = EditorBridgeAPI(backend)
        token = bridge.begin_renderer_load()
        bridge.editor_storage_ready.connect(lambda *args: results.append(args))
        timer = QTimer(); timer.setInterval(5); timer.timeout.connect(lambda: ticks.append(True)); timer.start()
        try:
            self.assertTrue(bridge.request_editor_storage(token, 'save-request', 'save_file',
                json.dumps({'path':self.path, 'content':'unsaved text'})))
            embedded.wait_for(lambda: started.is_set() and len(ticks) >= 2, 'Saving blocked the Qt event loop')
            release.set()
            embedded.wait_for(lambda: bool(results), 'The asynchronous save acknowledgement was lost')
            self.assertNotEqual(threads, [main_thread])
            self.assertTrue(results[0][2]['value']['success'])
        finally:
            release.set(); timer.stop(); bridge.deleteLater()


if __name__ == '__main__':
    unittest.main(verbosity=2)
