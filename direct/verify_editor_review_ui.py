#!/usr/bin/env python3
"""Real offline Monaco navigation, review visibility and semantic diff checks.

Every source model, tab, backend method and review is an in-memory fixture. The
renderer cannot access remote URLs and this verifier never opens a live project,
hardware connection, configuration store or AI recovery journal.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
CAPTURES = None


def until(predicate, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Timed out waiting for the isolated Monaco fixture")


FIXTURE = r"""
(() => {
  const editor = window.editorInstance;
  const files = [
    {name:'helper.cpp', path:'C:/fixture/Sketch/helper.cpp'},
    {name:'Sketch.ino', path:'C:/fixture/Sketch/Sketch.ino'}
  ];
  const models = new Map();
  const calls = [];
  const state = {files, models, calls, theme:'default', reviews:[], history:{canUndo:false,canRedo:false},
    mainPath:files[1].path, helperPath:files[0].path};
  window.__reviewFixture = state;
  document.querySelectorAll('#tab-bar .tab').forEach(tab => tab.remove());
  for (const file of files) {
    const content = file.name === 'Sketch.ino'
      ? Array.from({length:120}, (_, n) => n === 74 ? '// 😀target diagnostic' : `// fixture line ${n+1}`).join('\n')
      : '// helper dirty sentinel\nvoid helper() {}';
    const model = monaco.editor.createModel(content, 'cpp', monaco.Uri.file(file.path));
    model.setEOL(monaco.editor.EndOfLineSequence.LF);
    models.set(window.projectPathKey(file.path), model);
    const tab = document.createElement('div');
    tab.className = 'tab'; tab._filePath = file.path;
    tab.setAttribute('data-file-path', file.path);
    const label = document.createElement('span'); label.textContent = file.name;
    const dirty = document.createElement('span'); dirty.className = 'dirty-dot';
    dirty.textContent = '●'; dirty.style.display = 'none';
    tab.append(label, dirty);
    tab.addEventListener('click', () => {
      document.querySelectorAll('#tab-bar .tab.active').forEach(t => t.classList.remove('active'));
      tab.classList.add('active'); editor.setModel(model);
    });
    document.getElementById('tab-bar').appendChild(tab);
  }
  const methods = {
    get_project_files: () => files,
    get_theme_mode: () => state.theme,
    get_font_size: () => 14,
    get_recovery_buffers: () => ({}),
    recovery_complete: () => true,
    get_ai_edit_reviews: () => state.reviews,
    consume_ai_edit_snapshot: path => state.reviews.find(s => window.projectPathKey(s.path) === window.projectPathKey(path)) || {},
    get_ai_history_state: () => state.history,
    mark_modified: () => true,
    snapshot_buffer: () => true,
    on_editor_content_change: () => true,
    realtime_check_syntax: () => ({success:true, errors:[], warnings:[]}),
    save_tab_order: () => true,
    set_cursor_position: () => true,
    get_board_info: () => ({connected:false}),
    accept_ai_edit: (path, revision) => {
      state.reviews = state.reviews.filter(s => s.revision !== revision);
      state.history = {canUndo:true,canRedo:false,undoDepth:1,redoDepth:0,
        undo:{path,fileName:path.split('/').pop(),action:'accepted'}};
      return {success:true};
    },
    reject_ai_edit: () => ({success:false,error:'Fixture rejects writes'}),
    read_file: () => {throw new Error('Navigation must preserve its already loaded model');},
    save_file: () => {throw new Error('No fixture may write source files');},
    run_action: () => {throw new Error('No hardware action is permitted in this fixture');}
  };
  window.pywebview = {api:new Proxy(methods, {get(target, method) {
    if (typeof target[method] !== 'function') return undefined;
    return (...args) => {
      calls.push({method,args});
      try {return Promise.resolve(target[method](...args));}
      catch(error) {return Promise.reject(error);}
    };
  }})};
  document.querySelector('#tab-bar .tab').click();
  editor.updateOptions({readOnly:false, smoothScrolling:false, minimap:{enabled:false}});
  editor.layout();
  return true;
})()
"""


SNAPSHOT = r"""
(() => {
  const editor = window.editorInstance, model = editor.getModel();
  const byId = id => document.getElementById(id);
  const ai = model.getAllDecorations().filter(d =>
    [d.options.className,d.options.inlineClassName,d.options.glyphMarginClassName,
      d.options.linesDecorationsClassName].some(v => String(v || '').includes('ai-edit-')));
  const banner = byId('ai-diff-banner'), toggle = byId('ai-banner-toggle');
  return {
    uri:model.uri.toString(), content:model.getValue(), version:model.getAlternativeVersionId(),
    path:document.querySelector('#tab-bar .tab.active')?._filePath,
    readOnly:editor.getRawOptions().readOnly,
    glyphMargin:editor.getOption(monaco.editor.EditorOption.glyphMargin),
    banner:getComputedStyle(banner).display,
    toggle:toggle ? {display:getComputedStyle(toggle).display, disabled:toggle.disabled,
      expanded:toggle.getAttribute('aria-expanded'), text:toggle.textContent,
      label:toggle.getAttribute('aria-label')} : null,
    title:byId('ai-review-title').textContent,
    file:byId('ai-review-file').textContent,
    accept:{disabled:byId('ai-review-accept').disabled,display:byId('ai-review-accept').style.display},
    reject:{disabled:byId('ai-review-reject').disabled,display:byId('ai-review-reject').style.display},
    close:{disabled:byId('ai-history-close').disabled,display:byId('ai-history-close').style.display,
      label:byId('ai-history-close').getAttribute('aria-label'),text:byId('ai-history-close').textContent},
    undo:{disabled:byId('ai-history-undo').disabled,display:byId('ai-history-undo').style.display},
    redo:{disabled:byId('ai-history-redo').disabled,display:byId('ai-history-redo').style.display},
    decorations:ai.map(d => ({id:d.id,range:d.range,className:d.options.className,
      inline:d.options.inlineClassName,gutter:d.options.glyphMarginClassName,
      edge:d.options.linesDecorationsClassName,ruler:d.options.overviewRuler?.color})),
    pending:window.__reviewFixture.reviews.map(r => r.revision),
    calls:window.__reviewFixture.calls.filter(c => ['read_file','save_file','run_action','accept_ai_edit','reject_ai_edit'].includes(c.method))
  };
})()
"""


class EditorReviewChecks(unittest.TestCase):
    def setUp(self):
        self.view = QWebEngineView()
        self.profile = QWebEngineProfile(self.view)  # Off the record: no saved browser/session data.
        self.page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self.page)
        self.addCleanup(self._dispose_renderer)
        self.view.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.view.resize(1000, 620)
        self.view.show()
        self.load()
        self.js(FIXTURE)
        self.js("sessionStorage.removeItem('mcu_ai_banner_hidden'); window.setAiBannerHidden(false); true")

    def _dispose_renderer(self):
        self.view.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.profile.deleteLater()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()

    def load(self, reload=False):
        loaded = []
        self.view.loadFinished.connect(loaded.append)
        if reload:
            self.view.reload()
        else:
            self.view.load(QUrl.fromLocalFile(str(ROOT / 'src/editor/index.html')))
        until(lambda: loaded, seconds=20)
        self.view.loadFinished.disconnect(loaded.append)
        self.assertTrue(loaded[-1])
        until(lambda: self.js("Boolean(window.editorInstance && window.navigateToSource && window.setAiBannerHidden && window.setEditorTheme)"), seconds=20)

    def js(self, code):
        output = []
        self.page.runJavaScript("(() => {try {return JSON.stringify(eval(" + json.dumps(code) +
            "));} catch(error) {return JSON.stringify({__fixtureJavaScriptError:String(error)});}})()", output.append)
        until(lambda: bool(output))
        if output[0] is None:
            raise AssertionError('JavaScript did not return a fixture result: ' + code[:160])
        result = json.loads(output[0])
        if isinstance(result, dict) and '__fixtureJavaScriptError' in result:
            raise AssertionError(result['__fixtureJavaScriptError'] + ': ' + code[:160])
        return result

    def async_js(self, code):
        self.js("(() => {window.__fixtureResult=null; Promise.resolve().then(async () => {" + code +
            "}).then(value => {window.__fixtureResult={value:value ?? null};}).catch(error => {window.__fixtureResult={error:String(error)};});return true;})()")
        result = []
        until(lambda: (result.append(self.js("window.__fixtureResult")), bool(result[-1]))[1])
        self.assertNotIn('error', result[-1], result[-1])
        return result[-1]['value']

    def snapshot(self):
        return self.js(SNAPSHOT)

    def review(self, revision='review-1'):
        return self.async_js(r"""
          const f = window.__reviewFixture, model = f.models.get(window.projectPathKey(f.mainPath));
          const snapshot = {path:f.mainPath,fileName:'Sketch.ino',revision:""" + json.dumps(revision) + r""",
            beforeExists:true,afterExists:true,content:model.getValue(),beforeContent:'// original',
            pendingCount:1,reviewIndex:1,diff:{added:1,modified:1,removed:2,firstLine:75,changes:[
              {type:'added',startLine:73,endLine:73},
              {type:'modified',startLine:74,endLine:74},
              {type:'removed',startLine:75,endLine:75,anchorOnly:true,removedCount:2}
            ]}};
          f.reviews = [snapshot];
          await window.reloadActiveFileWithDiff(f.mainPath);
          return true;
        """)

    def test_diagnostics_activate_exact_model_preserve_dirty_buffers_and_reveal_top(self):
        result = self.async_js(r"""
          const f = window.__reviewFixture, editor = window.editorInstance;
          const helper = f.models.get(window.projectPathKey(f.helperPath));
          const main = f.models.get(window.projectPathKey(f.mainPath));
          helper.setValue('// unsaved helper sentinel\nvoid helper() {}');
          main.applyEdits([{range:new monaco.Range(1,1,1,1),text:'// unsaved main sentinel '}]);
          const before = {helper:helper.getValue(),main:main.getValue(),
            helperVersion:helper.getAlternativeVersionId(),mainVersion:main.getAlternativeVersionId()};
          document.querySelectorAll('#tab-bar .tab .dirty-dot').forEach(dot => dot.style.display = 'inline');
          const ok = await window.navigateToSource({path:f.mainPath.toUpperCase(),line:75,column:5,
            endLine:75,endColumn:11,columnEncoding:'codepoint',reveal:'top'});
          await new Promise(resolve => setTimeout(resolve,100));
          return {ok,identity:editor.getModel()===main,position:editor.getPosition(),selection:editor.getSelection(),
            y:editor.getTopForLineNumber(75)-editor.getScrollTop(),before,
            after:{helper:helper.getValue(),main:main.getValue(),helperVersion:helper.getAlternativeVersionId(),
              mainVersion:main.getAlternativeVersionId()},
            dirty:Array.from(document.querySelectorAll('#tab-bar .tab .dirty-dot')).map(d => d.style.display),
            calls:f.calls.filter(c => ['read_file','save_file','run_action'].includes(c.method))};
        """)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['identity'], result)
        self.assertEqual(result['before'], result['after'])
        self.assertEqual(result['selection']['startColumn'], 6, result)  # One astral symbol = two UTF-16 units.
        self.assertEqual(result['selection']['endColumn'], 12, result)
        self.assertGreaterEqual(result['y'], 0, result)
        self.assertLessEqual(result['y'], 6, result)  # A small reading inset still anchors at the top.
        self.assertTrue(all(value != 'none' for value in result['dirty']))
        self.assertEqual(result['calls'], [])
        markers = self.js(r"""(() => {
          const f=window.__reviewFixture;
          window.setEditorMarkers(JSON.stringify([
            {file:f.mainPath,line:75,col:5,endLine:75,endCol:11,severity:'error',message:'fixture target'},
            {file:f.helperPath,line:1,col:1,severity:'warning',message:'belongs to another file'}
          ]));
          return monaco.editor.getModelMarkers({resource:window.editorInstance.getModel().uri})
            .filter(marker => marker.message.startsWith('fixture') || marker.message.startsWith('belongs'));
        })()""")
        self.assertEqual(len(markers), 1, markers)
        self.assertEqual(markers[0]['startLineNumber'], 75, markers)
        self.assertEqual(markers[0]['startColumn'], 6, markers)
        self.assertEqual(markers[0]['endColumn'], 12, markers)
        final = self.async_js(r"""
          const f=window.__reviewFixture, editor=window.editorInstance;
          const ok=await window.navigateToSource({path:f.mainPath,line:120,column:1,reveal:'top'});
          await new Promise(resolve => setTimeout(resolve,100));
          return {ok,position:editor.getPosition(),y:editor.getTopForLineNumber(120)-editor.getScrollTop()};
        """)
        self.assertEqual(final['position']['lineNumber'], 120, final)
        self.assertGreaterEqual(final['y'], 0, final)
        self.assertLessEqual(final['y'], 6, final)

    def test_review_hide_and_reopen_preserve_pending_state_decorations_and_editor(self):
        self.review()
        before = self.snapshot()
        self.assertEqual(before['banner'], 'flex')
        self.assertFalse(before['close']['disabled'])
        self.assertNotEqual(before['close']['display'], 'none')
        self.assertEqual(len(before['decorations']), 3, before)
        self.js("document.getElementById('ai-history-close').click(); true")
        hidden = self.snapshot()
        self.assertEqual(hidden['banner'], 'none')
        self.assertEqual(hidden['toggle']['expanded'], 'false')
        self.assertFalse(hidden['toggle']['disabled'])
        self.assertTrue(hidden['toggle']['label'])
        for key in ('uri','content','version','readOnly','glyphMargin','decorations','pending','accept','reject','calls'):
            self.assertEqual(hidden[key], before[key], key)
        self.js("document.getElementById('ai-banner-toggle').click(); true")
        reopened = self.snapshot()
        self.assertEqual(reopened['banner'], 'flex')
        self.assertEqual(reopened['toggle']['expanded'], 'true')
        self.assertFalse(reopened['accept']['disabled'])
        self.assertFalse(reopened['reject']['disabled'])
        for key in ('uri','content','version','readOnly','glyphMargin','decorations','pending','calls'):
            self.assertEqual(reopened[key], before[key], key)
        self.js("window.editorInstance.updateOptions({readOnly:true}); window.setAiBannerHidden(true); true")
        self.assertTrue(self.snapshot()['readOnly'])
        self.js("window.toggleAiBanner(); true")
        self.assertTrue(self.snapshot()['readOnly'])
        self.js("window.editorInstance.updateOptions({readOnly:false}); true")
        self.js("window.setAiBannerHidden(true); true")
        self.review('review-2')
        # A fresh pending edit must reveal its decision controls even when the
        # user previously hid the history/review bar.
        self.assertEqual(self.snapshot()['banner'], 'flex')
        self.assertEqual(self.snapshot()['toggle']['expanded'], 'true')
        self.assertEqual(self.js("sessionStorage.getItem('mcu_ai_banner_hidden')"), '0')
        self.js("window.setEditorTheme('light'); true")
        self.assertEqual(self.snapshot()['banner'], 'flex')
        self.js("document.getElementById('ai-review-accept').click(); true")
        until(lambda: not self.snapshot()['pending'])
        self.assertEqual(len(self.snapshot()['calls']), 1)
        self.assertEqual(self.snapshot()['calls'][0]['method'], 'accept_ai_edit')

    def test_history_hide_remains_hidden_on_new_state_and_reload(self):
        self.js("(() => {const f=window.__reviewFixture; f.history={canUndo:true,canRedo:false,undoDepth:1,redoDepth:0,undo:{path:f.mainPath,fileName:'Sketch.ino',action:'accepted'}};window.resetAiReviewForProject();return true;})()")
        until(lambda: 'History' in self.snapshot()['title'])
        before = self.snapshot()
        self.assertEqual(before['banner'], 'flex')
        self.assertFalse(before['undo']['disabled'])
        self.assertTrue(before['redo']['disabled'])
        self.js("document.getElementById('ai-history-close').click(); true")
        self.assertEqual(self.snapshot()['banner'], 'none')
        self.js("(() => {const f=window.__reviewFixture; f.history.undoDepth=2;f.history.undo.action='rejected';window.resetAiReviewForProject();return true;})()")
        until(lambda: 'History' in self.snapshot()['title'])
        self.assertEqual(self.snapshot()['banner'], 'none')
        self.assertTrue(self.js("sessionStorage.getItem('mcu_ai_banner_hidden') !== null"))
        self.load(reload=True)
        self.js(FIXTURE)
        self.review('after-reload')
        self.assertEqual(self.snapshot()['banner'], 'flex')
        self.assertEqual(self.snapshot()['toggle']['expanded'], 'true')

    def test_removed_anchor_and_existing_rulers_follow_each_theme(self):
        self.review()
        themes = (
            ('default','#f05050',{'#5ccc6e','#61afef','#f05050'}),
            ('light','#cf222e',{'#1a7f37','#0969da','#cf222e'}),
            ('solarized','#dc322f',{'#859900','#268bd2','#dc322f'}),
        )
        for theme, expected, ruler_colors in themes:
            self.js('window.__reviewFixture.theme=' + json.dumps(theme) + '; window.setEditorTheme(window.__reviewFixture.theme); true')
            result = self.js(r"""(() => {
              const editor=window.editorInstance,model=editor.getModel();
              const decorations=model.getAllDecorations().filter(d => String(d.options.glyphMarginClassName||'').startsWith('ai-edit-'));
              const removed=decorations.find(d => d.options.glyphMarginClassName.includes('removed'));
              const surface=document.querySelector('.monaco-editor');
              const gutter=document.createElement('span');gutter.className=removed.options.glyphMarginClassName;
              surface.appendChild(gutter);
              const gutterColor=getComputedStyle(gutter,'::before').color;gutter.remove();
              const line=document.createElement('span');line.className=removed.options.className||'';
              surface.appendChild(line);const lineStyle=getComputedStyle(line);
              const result={solid:getComputedStyle(document.body).getPropertyValue('--diff-remove-solid').trim(),
                gutterColor,ruler:removed.options.overviewRuler.color,className:removed.options.className,
                inline:removed.options.inlineClassName||'',bg:lineStyle.backgroundColor,
                boundary:lineStyle.borderBottomColor,boundaryWidth:lineStyle.borderBottomWidth,
                decorationCount:decorations.length,rulers:decorations.map(d => d.options.overviewRuler.color)};
              line.remove();return result;
            })()""")
            self.assertEqual(result['solid'], expected, result)
            self.assertEqual(result['ruler'].lower(), expected, result)
            rgb = tuple(int(expected[i:i+2], 16) for i in (1,3,5))
            self.assertEqual(result['gutterColor'], f'rgb({rgb[0]}, {rgb[1]}, {rgb[2]})', result)
            self.assertEqual(result['boundary'], result['gutterColor'], result)
            self.assertEqual(result['boundaryWidth'], '2px', result)
            self.assertIn(result['bg'], ('rgba(0, 0, 0, 0)','transparent'), result)
            self.assertFalse(result['inline'], result)
            self.assertEqual(result['decorationCount'], 3, result)
            self.assertEqual({color.lower() for color in result['rulers']}, ruler_colors, result)
            if CAPTURES:
                self.async_js('await new Promise(resolve => setTimeout(resolve,80)); return true;')
                self.view.grab().save(str(CAPTURES / f'review-{theme}.png'))
        whole = self.async_js(r"""
          const f=window.__reviewFixture,model=f.models.get(window.projectPathKey(f.mainPath));
          const before='// removed whole file\nvoid deleted() {}\n// original final line';
          model.setValue(before);
          const snapshot={path:f.mainPath,fileName:'Sketch.ino',revision:'deleted-whole',beforeExists:true,
            afterExists:false,content:'',beforeContent:before,diff:{removed:3,firstLine:1,
            changes:[{type:'removed',startLine:1,endLine:3}]}};
          f.reviews=[snapshot];await window.reloadActiveFileWithDiff(f.mainPath);
          const removed=model.getAllDecorations().find(d => String(d.options.className||'').includes('removed-line'));
          return {content:model.getValue(),before,range:removed?.range,inline:removed?.options.inlineClassName,
            ruler:removed?.options.overviewRuler.color};
        """)
        self.assertEqual(whole['content'], whole['before'])
        self.assertEqual(whole['range']['startLineNumber'], 1, whole)
        self.assertEqual(whole['range']['endLineNumber'], 3, whole)
        self.assertEqual(whole['inline'], 'ai-edit-removed-inline')
        self.assertEqual(whole['ruler'], '#dc322f')

    def test_theme_recolor_keeps_ranges_shifted_by_unsaved_edits(self):
        self.review()
        result = self.js(r"""(() => {
          const editor=window.editorInstance,model=editor.getModel();
          const ranges=() => model.getAllDecorations()
            .filter(d => String(d.options.glyphMarginClassName||'').startsWith('ai-edit-'))
            .map(d => ({kind:d.options.glyphMarginClassName,range:d.range}))
            .sort((a,b) => a.kind.localeCompare(b.kind));
          const original=ranges();
          model.applyEdits([{range:new monaco.Range(1,1,1,1),text:'// unsaved inserted first line\n// unsaved inserted second line\n'}]);
          const shifted=ranges(),content=model.getValue(),version=model.getAlternativeVersionId();
          window.__reviewFixture.theme='light';window.setEditorTheme('light');
          return {original,shifted,recolored:ranges(),content,after:model.getValue(),
            version,afterVersion:model.getAlternativeVersionId()};
        })()""")
        self.assertEqual(len(result['original']), 3, result)
        for original, shifted in zip(result['original'], result['shifted']):
            self.assertEqual(shifted['kind'], original['kind'])
            self.assertEqual(shifted['range']['startLineNumber'], original['range']['startLineNumber'] + 2)
            self.assertEqual(shifted['range']['endLineNumber'], original['range']['endLineNumber'] + 2)
        self.assertEqual(result['recolored'], result['shifted'], result)
        self.assertEqual(result['after'], result['content'])
        self.assertEqual(result['afterVersion'], result['version'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--render-dir', type=Path)
    options, args = parser.parse_known_args()
    if options.render_dir:
        CAPTURES = options.render_dir.resolve()
        audit = (ROOT / 'temp').resolve()
        if CAPTURES != audit and audit not in CAPTURES.parents:
            parser.error('Captures must remain inside this checkout\'s temp directory')
        CAPTURES.mkdir(parents=True, exist_ok=True)
    run = unittest.main(argv=[sys.argv[0], *args], verbosity=2, exit=False)
    QTimer.singleShot(150, APP.quit)
    APP.exec()
    sys.exit(0 if run.result.wasSuccessful() else 1)
