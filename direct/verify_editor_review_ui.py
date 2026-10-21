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
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, QUrl
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from main.qt.garbage_collection import install_gui_garbage_collector

APP = QApplication.instance() or QApplication([])
COLLECTOR = install_gui_garbage_collector(APP)
APP.setQuitOnLastWindowClosed(False)
CAPTURES = None


def capture_directory(path: Path, checkout: Path = ROOT) -> Path:
    """Confine optional PNG output to the checkout's two capture locations."""
    checkout = checkout.resolve()
    candidate = path.resolve()
    # Keep these roots lexical after resolving the checkout. Resolving an
    # approved root itself would let a symlink redefine it outside the checkout.
    allowed = (checkout / 'temp', checkout / 'audit' / 'migration' / 'captures')
    if not any(candidate == root or root in candidate.parents for root in allowed):
        raise ValueError('Captures must remain inside this checkout\'s temp or audit/migration/captures directory')
    return candidate


class CaptureDirectoryTests(unittest.TestCase):
    def test_capture_paths_are_limited_to_approved_checkout_locations(self):
        for relative in ('temp', 'temp/review', 'audit/migration/captures',
                         'audit/migration/captures/editor-review-2'):
            with self.subTest(allowed=relative):
                candidate = ROOT / relative
                self.assertEqual(capture_directory(candidate), candidate.resolve())
        for relative in ('.', 'direct', 'audit', 'audit/migration/scratch',
                         'temp/../direct', 'audit/migration/captures/../../../direct'):
            with self.subTest(rejected=relative), self.assertRaises(ValueError):
                capture_directory(ROOT / relative)

    def test_capture_paths_cannot_escape_through_symlinks(self):
        fixtures = ROOT / 'audit' / 'migration' / 'scratch'
        fixtures.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='editor-capture-path-', dir=fixtures) as directory:
            fixture = Path(directory)
            checkout = fixture / 'checkout'
            outside = fixture / 'outside'
            checkout.mkdir()
            outside.mkdir()
            try:
                (checkout / 'temp').symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f'Directory symlinks unavailable: {error}')
            with self.assertRaises(ValueError):
                capture_directory(checkout / 'temp' / 'review', checkout)
            (checkout / 'temp').unlink()
            (checkout / 'temp').mkdir()
            (checkout / 'temp' / 'escape').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                capture_directory(checkout / 'temp' / 'escape' / 'review', checkout)
            captures = checkout / 'audit' / 'migration' / 'captures'
            captures.parent.mkdir(parents=True)
            captures.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                capture_directory(captures / 'review', checkout)


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
    dirty.textContent = ''; dirty.style.display = 'none';
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
    realtime_check_syntax: () => 'null',  // Qt queues diagnostics separately from the string reply.
    save_tab_order: () => true,
    set_cursor_position: () => true,
    set_active_file: () => true,
    get_board_info: () => ({connected:false}),
    accept_ai_edit: (path, revision) => {
      const snapshot = state.reviews.find(s => s.revision === revision) || {};
      state.reviews = state.reviews.filter(s => s.revision !== revision);
      state.history = {canUndo:true,canRedo:false,undoDepth:1,redoDepth:0,
        undo:{path,fileName:path.split('/').pop(),action:'accepted',
          promptSource:snapshot.promptSource,promptSourceVersion:snapshot.promptSourceVersion,
          promptSources:snapshot.promptSources,prompt:snapshot.prompt}};
      return {success:true};
    },
    reject_ai_edit: () => ({success:false,error:'Fixture rejects writes'}),
    undo_ai_edit_decision: () => ({success:false,error:'Fixture history is read-only'}),
    redo_ai_edit_decision: () => ({success:false,error:'Fixture history is read-only'}),
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
    source:{text:byId('ai-review-source').innerText,title:byId('ai-review-source').title,
      label:byId('ai-review-source').getAttribute('aria-label')},
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


BANNER_GEOMETRY = r"""
(() => {
  const banner = document.getElementById('ai-diff-banner');
  const rect = element => {
    const r = element.getBoundingClientRect();
    return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height};
  };
  const visible = element => getComputedStyle(element).display !== 'none' && element.getBoundingClientRect().width > 0;
  const controls = Array.from(banner.querySelectorAll('button')).filter(visible).map(button => ({
    id:button.id,rect:rect(button),label:button.getAttribute('aria-label'),title:button.title,
    text:button.innerText,disabled:button.disabled,
    hit:document.elementFromPoint((rect(button).left+rect(button).right)/2,
      (rect(button).top+rect(button).bottom)/2)?.closest('button')?.id
  }));
  return {viewport:{width:innerWidth,height:innerHeight},banner:rect(banner),
    editor:rect(document.getElementById('editor-container')),controls,
    content:Array.from(banner.children).filter(visible).map(element => ({id:element.id,rect:rect(element)})),
    title:banner.title,description:banner.getAttribute('aria-description'),
    navLabel:getComputedStyle(document.querySelector('.review-nav-label')).display,
    badgeLabel:getComputedStyle(document.querySelector('.diff-badge-label')).display,
    fileTitle:document.getElementById('ai-review-file').title,
    positionTitle:document.getElementById('ai-review-position').title};
})()
"""


class ReviewPage(QWebEnginePage):
    """Keep asynchronous renderer warnings/errors visible to unittest."""
    def __init__(self, profile, parent):
        super().__init__(profile, parent)
        self.console_issues = []

    def javaScriptConsoleMessage(self, level, message, line, source):
        if level != QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel:
            self.console_issues.append({"message": message, "line": line, "source": source})
        super().javaScriptConsoleMessage(level, message, line, source)


class EditorReviewChecks(unittest.TestCase):
    def setUp(self):
        # Match the application's embedded editor. A WM can maximize/clamp a
        # top-level WebEngine window at 200%, preventing the compact viewport
        # requests below from ever reaching its real render-widget geometry.
        self.host = QWidget()
        self.host.resize(1000, 620)
        self.view = QWebEngineView(self.host)
        self.host_layout = QVBoxLayout(self.host)
        self.host_layout.setContentsMargins(0, 0, 0, 0)
        self.host_layout.addWidget(self.view)
        self.profile = QWebEngineProfile(self.view)  # Off the record: no saved browser/session data.
        self.page = ReviewPage(self.profile, self.view)
        self.view.setPage(self.page)
        # Cleanup assertions run after disposal, so delayed console messages
        # cannot silently turn a failing renderer check into a passing test.
        self.addCleanup(self.assertFalse, self.page.console_issues)
        self.addCleanup(self._dispose_renderer)
        self.view.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.view.setMaximumSize(1000, 620)
        self.host.show()
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
        self.host.close()
        self.host.deleteLater()
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

    def resize_viewport(self, width, height):
        """Wait for CSS to reflect the client size actually granted by Qt."""
        # The layout caps this logical viewport to the visible host contents.
        # It also responds if the WM subsequently changes the host's size.
        self.view.setMaximumSize(width, height)
        self.host_layout.activate()
        observed = {'requested': {'width': width, 'height': height}}

        def ready():
            contents = self.view.contentsRect()
            host = self.host.contentsRect()
            zoom = self.page.zoomFactor()
            observed.update(qtViewport={'width': contents.width(), 'height': contents.height()},
                            hostViewport={'width': host.width(), 'height': host.height()}, zoom=zoom)
            observed['cssViewport'] = self.js('({width:innerWidth,height:innerHeight})')
            return (host.contains(self.view.geometry()) and
                    all(observed['qtViewport'][axis] == min(observed['requested'][axis], observed['hostViewport'][axis])
                        and abs(observed['cssViewport'][axis] - observed['qtViewport'][axis]/zoom) <= 1
                        for axis in ('width', 'height')))

        try:
            until(ready)
        except AssertionError as error:
            self.fail('WebEngine viewport did not settle: ' + json.dumps(observed) + ': ' + str(error))
        return observed

    def review(self, revision='review-1'):
        return self.async_js(r"""
          const f = window.__reviewFixture, model = f.models.get(window.projectPathKey(f.mainPath));
          const snapshot = {path:f.mainPath,fileName:'Sketch.ino',revision:""" + json.dumps(revision) + r""",
            promptSource:'assistant',promptSourceVersion:1,promptSources:['assistant'],prompt:'Fixture Assistant prompt',
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

    def test_editor_fills_viewport_through_lower_and_fractional_scaling(self):
        self.view.setFocus()
        self.js(r"""(() => {
          const editor=window.editorInstance;
          window.__layoutModel=editor.getModel();
          editor.executeEdits('fixture',[{range:new monaco.Range(1,1,1,1),text:'// unsaved 😀\n'}]);
          document.querySelector('#tab-bar .tab.active .dirty-dot').style.display='inline';
          editor.setSelection(new monaco.Selection(2,1,2,7));editor.focus();return true;
        })()""")
        state = r"""(() => ({model:editorInstance.getModel()===window.__layoutModel,
          text:editorInstance.getModel().getValue(),version:editorInstance.getModel().getAlternativeVersionId(),
          selection:editorInstance.getSelection(),tabs:Array.from(document.querySelectorAll('#tab-bar .tab'))
            .map(t=>({path:t._filePath,dirty:t.querySelector('.dirty-dot').style.display}))}))()"""
        baseline = self.js(state)
        for zoom, width, height in ((1.75,1000,620),(.75,1200,700),(1,640,360),
                                    (1.25,850,480),(2,900,500),(1,1200,700)):
            with self.subTest(zoom=zoom,width=width,height=height):
                self.page.setZoomFactor(zoom)
                self.resize_viewport(width, height)
                self.async_js('await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));return true;')
                geometry = self.js(r"""(() => {
                  const rect=id=>{const r=document.getElementById(id).getBoundingClientRect();
                    return {x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom};};
                  return {width:innerWidth,height:innerHeight,app:rect('app-container'),
                    host:rect('editor-container'),anchor:rect('monaco-anchor'),
                    monaco:editorInstance.getLayoutInfo(),focus:editorInstance.hasTextFocus()};
                })()""")
                self.assertAlmostEqual(geometry['app']['width'],geometry['width'],delta=1)
                self.assertAlmostEqual(geometry['app']['bottom'],geometry['height'],delta=1)
                self.assertAlmostEqual(geometry['host']['bottom'],geometry['height'],delta=1)
                for axis in ('width','height'):
                    self.assertAlmostEqual(geometry['anchor'][axis],geometry['host'][axis],delta=1)
                    self.assertAlmostEqual(geometry['monaco'][axis],geometry['host'][axis],delta=1)
                self.assertTrue(geometry['focus'],'Resizing must preserve Monaco keyboard focus')
                self.assertEqual(self.js(state),baseline,'Sizing must retain model, dirty state, tab order and selection')
                if CAPTURES and zoom == .75:
                    self.assertTrue(self.view.grab().save(str(CAPTURES / 'editor-lower-scale-filled.png')))

    def test_realtime_syntax_reply_uses_queued_diagnostic_contract(self):
        self.assertEqual(self.async_js(r"""
          const f = window.__reviewFixture;
          return await window.pywebview.api.realtime_check_syntax(f.helperPath, 'void helper() {}');
        """), 'null')
        self.js(r"""(() => {
          const f = window.__reviewFixture;
          f.calls.length = 0;
          window.editorInstance.getModel().applyEdits([
            {range:new monaco.Range(1,1,1,1),text:'// delayed syntax fixture\n'}
          ]);
          return true;
        })()""")
        until(lambda: self.js("window.__reviewFixture.calls.some(c => c.method === 'realtime_check_syntax')"))
        self.async_js('await new Promise(resolve => setTimeout(resolve,100));return true;')
        self.assertFalse(self.page.console_issues)

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
        self.assertEqual(len(before['decorations']), 4, before)
        self.js("(() => {const close=document.getElementById('ai-history-close');close.focus();close.click();return true;})()")
        hidden = self.snapshot()
        self.assertEqual(hidden['banner'], 'none')
        self.assertEqual(hidden['toggle']['expanded'], 'false')
        self.assertFalse(hidden['toggle']['disabled'])
        self.assertTrue(hidden['toggle']['label'])
        focus = self.js("({inside:document.getElementById('ai-diff-banner').contains(document.activeElement),editor:window.editorInstance.hasTextFocus(),toggle:document.activeElement===document.getElementById('ai-banner-toggle')})")
        self.assertFalse(focus['inside'], focus)
        self.assertTrue(focus['editor'] or focus['toggle'], focus)
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

        # Focus fallback must not edit buffers or consume their real undo stack.
        self.review('focus-fallbacks')
        # Explicit navigation retires the review's delayed first-change reveal;
        # its 90 ms callback must not race the independent focus checks below.
        self.assertTrue(self.async_js("return await window.navigateToSource({path:window.__reviewFixture.mainPath,line:5,column:1,reveal:'top'});"))
        original = self.js("window.editorInstance.getModel().getValue()")
        self.js(r"""(() => {
          const editor=window.editorInstance;
          window.__focusModel=editor.getModel();
          editor.pushUndoStop();
          editor.executeEdits('focus-fixture',[{range:new monaco.Range(1,1,1,1),text:'// focus sentinel\n'}]);
          editor.pushUndoStop();return true;
        })()""")
        buffer_state = r"""(() => ({identity:editorInstance.getModel()===window.__focusModel,
          models:Array.from(window.__reviewFixture.models.values()).map(model=>({
            uri:model.uri.toString(),content:model.getValue(),version:model.getAlternativeVersionId(),
            undo:model.canUndo(),redo:model.canRedo()})),selection:editorInstance.getSelection()}))()"""
        buffers = self.js(buffer_state)
        self.assertTrue(buffers['identity'])
        self.assertTrue(self.js('window.__focusModel.canUndo()'))
        fallback = self.js(r"""(() => {
          const editor=window.editorInstance,focus=editor.focus;
          try {
            editor.focus=()=>{};
            document.getElementById('ai-history-close').focus();
            window.setAiBannerHidden(true);
            const toggle=document.getElementById('ai-banner-toggle');
            return {toggle:document.activeElement===toggle,visible:getComputedStyle(toggle).display,
              inside:document.getElementById('ai-diff-banner').contains(document.activeElement)};
          } finally {editor.focus=focus;}
        })()""")
        self.assertTrue(fallback['toggle'], fallback)
        self.assertNotEqual(fallback['visible'], 'none', fallback)
        self.assertFalse(fallback['inside'], fallback)
        self.assertEqual(self.js(buffer_state), buffers)
        self.js('window.setAiBannerHidden(false);true')
        external = self.js(r"""(() => {
          const toolbar=document.getElementById('detached-action-bar'),display=toolbar.style.display;
          const button=toolbar.querySelector('button.compile');
          try {
            toolbar.style.display='flex';button.focus();
            const before=document.activeElement===button;
            window.setAiBannerHidden(true);
            return {before,after:document.activeElement===button};
          } finally {button.blur();toolbar.style.display=display;}
        })()""")
        self.assertTrue(external['before'], external)
        self.assertTrue(external['after'], external)
        self.assertEqual(self.js(buffer_state), buffers)
        self.js('window.setAiBannerHidden(false);true')
        empty = self.js(r"""(() => {
          const editor=window.editorInstance,focus=editor.focus;
          try {
            editor.focus=()=>{};
            document.getElementById('ai-history-close').focus();
            window.__reviewFixture.reviews=[];
            window.__reviewFixture.history={canUndo:false,canRedo:false};
            window.resetAiReviewForProject();window.setAiBannerHidden(true);
            return {inside:document.getElementById('ai-diff-banner').contains(document.activeElement),
              toggle:getComputedStyle(document.getElementById('ai-banner-toggle')).display};
          } finally {editor.focus=focus;}
        })()""")
        self.assertFalse(empty['inside'], empty)
        self.assertEqual(empty['toggle'], 'none', empty)
        self.assertEqual(self.js(buffer_state), buffers)
        undone = self.async_js('await window.__focusModel.undo();return window.__focusModel.getValue();')
        self.assertEqual(undone, original)
        self.async_js('await window.__focusModel.redo();return true;')
        self.assertEqual(self.js(buffer_state)['models'], buffers['models'])

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

    def test_review_and_history_fit_one_row_at_narrow_short_and_scaled_sizes(self):
        self.review('responsive-review')
        self.async_js(r"""
          const f=window.__reviewFixture;
          const main=f.reviews[0];
          main.fileName='Extremely_long_source_filename_with_unicode_😀_and_details.ino';
          main.pendingCount=2;main.reviewIndex=1;
          main.diff.added=1234567;main.diff.modified=4567890;main.diff.removed=7890123;
          const helper=f.models.get(window.projectPathKey(f.helperPath));
          f.reviews.push({...main,path:f.helperPath,fileName:'helper.cpp',revision:'responsive-helper',
            reviewIndex:2,content:helper.getValue(),beforeContent:helper.getValue(),diff:{}});
          await window.reloadActiveFileWithDiff(f.mainPath);
          document.querySelectorAll('#tab-bar .tab .dirty-dot').forEach(dot => dot.style.display='inline');
          return true;
        """)
        baseline = self.js(r"""(() => {
          const f=window.__reviewFixture;
          return {models:Array.from(f.models.values()).map(m => ({uri:m.uri.toString(),content:m.getValue(),
            version:m.getAlternativeVersionId()})),tabs:Array.from(document.querySelectorAll('#tab-bar .tab'))
              .map(t => ({path:t._filePath,dirty:t.querySelector('.dirty-dot').style.display}))};
        })()""")
        sizes = (
            ('wide',1000,620,1), ('short',1000,300,1), ('compact',620,240,1),
            ('middle',520,240,1), ('badge-limit',480,220,1),
            ('narrow',460,180,1), ('small',360,150,1), ('minimum',280,120,1),
            ('micro',250,150,1),
            ('scale125',800,400,1.25), ('scale150',720,330,1.5),
            ('scale175',700,300,1.75), ('scale200',640,260,2),
        )
        for mode in ('review','history'):
            if mode == 'history':
                self.js(r"""(() => {
                  const f=window.__reviewFixture;f.reviews=[];
                  f.history={canUndo:true,canRedo:true,undoDepth:123,redoDepth:45,
                    undo:{path:f.mainPath,fileName:'Extremely_long_history_filename_😀.ino',action:'accepted',
                      promptSource:'assistant',promptSourceVersion:1,promptSources:['assistant'],prompt:'Fixture Assistant history prompt'},
                    redo:{path:f.helperPath,fileName:'helper.cpp',action:'rejected'}};
                  window.resetAiReviewForProject();return true;
                })()""")
                until(lambda: 'History' in self.snapshot()['title'])
            expected_ids = {'ai-history-undo','ai-history-redo','ai-history-close'} if mode == 'history' else {
                'ai-review-previous','ai-review-next','ai-review-reject','ai-review-accept','ai-history-close'}
            for theme in ('default','light','solarized'):
                self.js('window.__reviewFixture.theme=' + json.dumps(theme) + ';window.setEditorTheme(window.__reviewFixture.theme);true')
                for label,width,height,zoom in sizes:
                    with self.subTest(mode=mode,theme=theme,size=label):
                        self.page.setZoomFactor(zoom)
                        self.resize_viewport(width, height)
                        # Native windows may clamp a top-level WebEngine view
                        # to the monitor. Compare CSS with the actual reading
                        # viewport, while retaining the complete control bounds
                        # and one-row assertions below.
                        self.async_js('await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));return true;')
                        geometry = self.js(BANNER_GEOMETRY)
                        bar, editor = geometry['banner'], geometry['editor']
                        self.assertLessEqual(bar['height'], 42, geometry)
                        self.assertGreaterEqual(bar['left'], editor['left'], geometry)
                        self.assertLessEqual(bar['right'], editor['right'] + .5, geometry)
                        self.assertGreaterEqual(bar['top'], editor['top'], geometry)
                        self.assertLessEqual(bar['bottom'], editor['bottom'], geometry)
                        self.assertEqual({c['id'] for c in geometry['controls']}, expected_ids, geometry)
                        for control in geometry['controls']:
                            r = control['rect']
                            self.assertTrue(control['label'], control)
                            self.assertTrue(control['title'], control)
                            self.assertEqual(control['hit'], control['id'], control)
                            self.assertGreaterEqual(r['width'], 28, control)
                            self.assertGreaterEqual(r['height'], 28, control)
                            self.assertGreaterEqual(r['left'], bar['left'], geometry)
                            self.assertLessEqual(r['right'], bar['right'], geometry)
                            self.assertLess(abs((r['top'] + r['bottom'])/2 - (bar['top'] + bar['bottom'])/2), 1, geometry)
                        for item in geometry['content']:
                            self.assertGreaterEqual(item['rect']['left'], bar['left'], geometry)
                            self.assertLessEqual(item['rect']['right'], bar['right'] + .5, geometry)
                        self.assertIn('C:/fixture/Sketch/Sketch.ino', geometry['title'])
                        self.assertEqual(geometry['description'], geometry['title'])
                        self.assertIn('Assistant source', geometry['title'])
                        if geometry['viewport']['width'] <= 900 or geometry['viewport']['height'] <= 420:
                            self.assertEqual(geometry['navLabel'], 'none', geometry)
                            self.assertEqual(geometry['badgeLabel'], 'none', geometry)
                            self.assertLessEqual(bar['height'], 38, geometry)
                        if mode == 'review':
                            self.assertIn('+1234567 added', geometry['title'])
                            self.assertIn('1 of 2', geometry['title'])
                            self.assertEqual(next(c['text'] for c in geometry['controls'] if c['id']=='ai-review-accept'), 'Accept')
                            self.assertEqual(next(c['text'] for c in geometry['controls'] if c['id']=='ai-review-reject'), 'Reject')
                        else:
                            self.assertIn('123 undo / 45 redo', geometry['title'])
                            self.assertIn('Last AI edit accepted', geometry['title'])
                        if CAPTURES and label in ('wide','short','small','scale200'):
                            # The DOM can report its new viewport before the
                            # offscreen WebEngine surface paints that frame.
                            self.async_js('await new Promise(resolve => setTimeout(resolve,120));return true;')
                            self.view.grab().save(str(CAPTURES / f'{mode}-{theme}-{label}.png'))
                self.js("document.getElementById('ai-history-close').focus();true")
                self.assertEqual(self.js('document.activeElement.id'), 'ai-history-close')
            unchanged = self.js(r"""(() => {
              const f=window.__reviewFixture;
              return {models:Array.from(f.models.values()).map(m => ({uri:m.uri.toString(),content:m.getValue(),
                version:m.getAlternativeVersionId()})),tabs:Array.from(document.querySelectorAll('#tab-bar .tab'))
                  .map(t => ({path:t._filePath,dirty:t.querySelector('.dirty-dot').style.display}))};
            })()""")
            self.assertEqual(unchanged, baseline)
            self.assertEqual(self.snapshot()['calls'], [])
        self.page.setZoomFactor(1)

    def test_compact_controls_keep_navigation_decisions_and_history_actions(self):
        self.review('compact-main')
        self.async_js(r"""
          const f=window.__reviewFixture,main=f.reviews[0];
          main.pendingCount=2;main.reviewIndex=1;
          const helper=f.models.get(window.projectPathKey(f.helperPath));
          f.reviews.push({...main,path:f.helperPath,fileName:'helper.cpp',revision:'compact-helper',
            reviewIndex:2,content:helper.getValue(),beforeContent:helper.getValue(),diff:{}});
          await window.reloadActiveFileWithDiff(f.mainPath);return true;
        """)
        viewport = self.resize_viewport(320, 140)
        self.assertLessEqual(viewport['cssViewport']['width'], 320, viewport)
        self.async_js('await window.reloadActiveFileWithDiff(window.__reviewFixture.helperPath);return true;')
        self.assertTrue(self.snapshot()['path'].endswith('Sketch.ino'))
        self.assertIn('Another AI-edited file is queued', self.js("document.getElementById('ai-diff-banner').title"))
        self.js("document.getElementById('ai-review-next').click();true")
        until(lambda: self.snapshot()['path'].endswith('helper.cpp'))
        until(lambda: not self.snapshot()['accept']['disabled'])
        self.js("document.getElementById('ai-review-previous').click();true")
        until(lambda: self.snapshot()['path'].endswith('Sketch.ino'))
        until(lambda: not self.snapshot()['accept']['disabled'])
        before = self.snapshot()
        self.js("document.getElementById('ai-review-reject').click();true")
        until(lambda: 'Fixture rejects writes' in self.js("document.getElementById('ai-diff-banner').title"))
        rejected = self.snapshot()
        notice = self.js("(() => {const node=document.getElementById('ai-review-message');return {label:node.getAttribute('aria-label'),icon:getComputedStyle(node.querySelector('.review-icon')).display,text:getComputedStyle(node.querySelector('.review-message-text')).display};})()")
        self.assertEqual(notice['label'], 'Fixture rejects writes')
        self.assertEqual(notice['icon'], 'block')
        self.assertEqual(notice['text'], 'none')
        for key in ('uri','content','version','readOnly','decorations','pending'):
            self.assertEqual(rejected[key], before[key], key)
        self.assertEqual([call['method'] for call in rejected['calls']], ['reject_ai_edit'])
        self.assertFalse(rejected['accept']['disabled'])
        self.assertFalse(rejected['reject']['disabled'])
        self.js(r"""(() => {
          const f=window.__reviewFixture;f.reviews=[];
          f.history={canUndo:true,canRedo:true,undoDepth:1,redoDepth:1,
            undo:{path:f.mainPath,fileName:'Sketch.ino',action:'accepted'},
            redo:{path:f.helperPath,fileName:'helper.cpp',action:'rejected'}};
          window.resetAiReviewForProject();return true;
        })()""")
        until(lambda: 'History' in self.snapshot()['title'])
        for direction in ('undo','redo'):
            self.js("document.getElementById('ai-history-" + direction + "').click();true")
            until(lambda: self.js("window.__reviewFixture.calls.filter(c => c.method === '" + direction + "_ai_edit_decision').length") == 1)
            until(lambda: not self.snapshot()[direction]['disabled'])
        after = self.snapshot()
        self.assertEqual(after['content'], before['content'])
        self.assertEqual(after['version'], before['version'])
        self.assertFalse(any(call['method'] in ('read_file','save_file','run_action') for call in after['calls']))

    def test_review_and_history_source_labels_preserve_explicit_and_unknown_provenance(self):
        self.review('source-provenance')
        before = self.snapshot()
        legacy = 'Assistant prompt (title unavailable)'
        scenarios = (
            ('cli',1,'Run the command-line edit\nKeep the literal prompt.','CLI','CLI',['cli']),
            ('assistant',1,'Update from the built-in Assistant','Assistant','Asst.',['assistant']),
            ('assistant',1,legacy,'Assistant','Asst.',['assistant']),
            ('external',1,legacy,'Unknown','?',['external']),
            ('external',1,'An external editor changed this source','Unknown','?',['external']),
            ('mixed',1,'A Terminal and Assistant contributed','Mixed','Mixed',['cli','assistant']),
            ('unrecognized',1,'No known source metadata','Unknown','?',None),
            ('cli',None,legacy,'Unknown','?',None),
            ('assistant',None,legacy,'Unknown','?',None),
            ('cli',0,'Old channel metadata','Unknown','?',None),
            ('cli','1','Unverified marker type','Unknown','?',None),
            (None,None,legacy,'Unknown','?',None),
            (None,None,'Assistant-like text alone cannot identify a source','Unknown','?',None),
        )
        for source,version,prompt,label,compact,sources in scenarios:
            with self.subTest(source=source,prompt=prompt):
                self.async_js(r"""
                  const f=window.__reviewFixture,review=f.reviews[0];
                  const source=""" + json.dumps(source) + r""";
                  if (source===null) delete review.promptSource;else review.promptSource=source;
                  const version=""" + json.dumps(version) + r""";
                  if (version===null) delete review.promptSourceVersion;else review.promptSourceVersion=version;
                  const sources=""" + json.dumps(sources) + r""";
                  if (sources===null) delete review.promptSources;else review.promptSources=sources;
                  review.prompt=""" + json.dumps(prompt) + r""";
                  await window.reloadActiveFileWithDiff(f.mainPath);return true;
                """)
                title = label + ' source' + (' (CLI + Assistant)' if label == 'Mixed' else '') + '\n' + prompt
                for width,height,expected in ((1000,620,label),(280,140,compact)):
                    viewport = self.resize_viewport(width, height)
                    # A native top-level window can be clamped by its monitor.
                    # Keep both sides of the actual source-label CSS breakpoint
                    # covered instead of waiting for an ungranted requested size.
                    if width > 320:
                        self.assertGreater(viewport['cssViewport']['width'], 320, viewport)
                    else:
                        self.assertLessEqual(viewport['cssViewport']['width'], 320, viewport)
                    state = self.snapshot()
                    self.assertEqual(state['source']['text'], expected)
                    self.assertEqual(state['source']['title'], title)
                    self.assertEqual(state['source']['label'], state['source']['title'])
                    geometry = self.js(BANNER_GEOMETRY)
                    self.assertIn(title, geometry['title'])
                    self.assertLessEqual(geometry['banner']['height'],42)
                    self.assertEqual({control['id'] for control in geometry['controls']},
                        {'ai-review-accept','ai-review-reject','ai-history-close'})
                    for control in geometry['controls']:
                        self.assertEqual(control['hit'],control['id'])
                        self.assertLessEqual(control['rect']['right'],geometry['banner']['right'])
                self.js('window.setAiBannerHidden(true);true')
                self.assertIn(label + ' Review',self.snapshot()['toggle']['text'])
                self.js('window.setAiBannerHidden(false);true')
                after = self.snapshot()
                for key in ('uri','content','version','readOnly','decorations','pending','calls'):
                    self.assertEqual(after[key],before[key],key)
        for active_source,version,prompt,label,sources in (
                ('assistant',1,'Assistant history prompt','Assistant',['assistant']),
                ('cli',1,'CLI history prompt','CLI',['cli']),
                ('mixed',1,'Combined history prompt','Mixed',['cli','assistant']),
                ('external',1,legacy,'Unknown',['external']),('cli',None,legacy,'Unknown',None)):
            self.js(r"""(() => {
              const f=window.__reviewFixture;f.reviews=[];
              f.history={canUndo:true,canRedo:true,undoDepth:1,redoDepth:1,
                undo:{path:f.mainPath,fileName:'Sketch.ino',action:'accepted',
                  promptSource:""" + json.dumps(active_source) + ',promptSourceVersion:' + json.dumps(version) +
                  ',promptSources:' + json.dumps(sources) + ',prompt:' + json.dumps(prompt) + r"""},
                redo:{path:f.helperPath,fileName:'helper.cpp',action:'rejected',
                  promptSource:'cli',promptSourceVersion:1,promptSources:['cli'],prompt:'Separate CLI redo prompt'}};
              window.resetAiReviewForProject();return true;
            })()""")
            title = label + ' source' + (' (CLI + Assistant)' if label == 'Mixed' else '') + '\n' + prompt
            until(lambda: self.snapshot()['source']['title'] == title)
            state = self.snapshot()
            self.assertEqual(state['source']['title'],title)
            labels = self.js("['ai-history-undo','ai-history-redo'].map(id => document.getElementById(id).getAttribute('aria-label'))")
            self.assertIn('Undo ' + label + ' edit',labels[0])
            self.assertIn('Redo CLI edit',labels[1])
            self.js('window.setAiBannerHidden(true);true')
            self.assertIn(label + ' History',self.snapshot()['toggle']['text'])
            self.js('window.setAiBannerHidden(false);true')
        self.js("(() => {const f=window.__reviewFixture;f.history.canUndo=false;f.history.undo=null;window.resetAiReviewForProject();return true;})()")
        until(lambda: self.snapshot()['source']['title'] == 'CLI source\nSeparate CLI redo prompt')
        self.assertEqual(self.snapshot()['content'],before['content'])
        self.assertEqual(self.snapshot()['version'],before['version'])
        self.assertEqual(self.snapshot()['calls'],[])

    def test_removed_anchor_and_existing_rulers_follow_each_theme(self):
        self.review()
        themes = (
            ('default','#f05050',{'#5ccc6e','#f05050'}),
            ('light','#cf222e',{'#1a7f37','#cf222e'}),
            ('solarized','#dc322f',{'#859900','#dc322f'}),
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
            self.assertEqual(result['decorationCount'], 4, result)
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
        self.assertEqual(len(result['original']), 4, result)
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
        try:
            CAPTURES = capture_directory(options.render_dir)
        except ValueError as error:
            parser.error(str(error))
        CAPTURES.mkdir(parents=True, exist_ok=True)
    run = unittest.main(argv=[sys.argv[0], *args], verbosity=2, exit=False)
    QTimer.singleShot(150, APP.quit)
    APP.exec()
    sys.exit(0 if run.result.wasSuccessful() else 1)
