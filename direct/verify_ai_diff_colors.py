#!/usr/bin/env python3
"""Render AI review colors in offline Monaco using in-memory source fixtures.

No application backend, project, recovery journal, settings or hardware is used.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from direct import verify_editor_review_ui as fixture
from main.core.ai_review import build_ai_line_diff

CAPTURES = None

COLORS = r"""
(() => {
  const editor=window.editorInstance,model=editor.getModel();
  const decorations=model.getAllDecorations().filter(d=>String(d.options.className||'').startsWith('ai-edit-'));
  const read=selector=>{
    const node=document.querySelector('.monaco-editor '+selector);
    if (!node) return null;
    const css=getComputedStyle(node),r=node.getBoundingClientRect();
    return {text:node.textContent,bg:css.backgroundColor,fg:css.color,
      border:css.borderLeftColor,boundary:css.borderBottomColor,
      animation:css.animationName,rect:{width:r.width,height:r.height},
      marker:getComputedStyle(node,'::before').color};
  };
  const state={};
  for (const kind of ['added','modified','removed']) {
    state[kind]={line:read('.ai-edit-'+kind+'-line'),inline:read('.ai-edit-'+kind+'-inline'),
      gutter:read('.ai-edit-'+kind+'-gutter')};
  }
  state.removed.anchor=read('.ai-edit-removed-anchor-line');
  state.removed.anchorGutter=read('.ai-edit-removed-anchor-gutter');
  const tokens={};
  for (const kind of ['add','remove']) {
    const css=getComputedStyle(document.body);
    tokens[kind]={solid:css.getPropertyValue('--diff-'+kind+'-solid').trim(),
      text:css.getPropertyValue('--diff-'+kind+'-text').trim(),
      rgb:css.getPropertyValue('--diff-'+kind+'-rgb').trim()};
  }
  return {state,tokens,decorations:decorations.map(d=>({range:d.range,className:d.options.className,
    inline:d.options.inlineClassName||'',gutter:d.options.glyphMarginClassName,
    ruler:d.options.overviewRuler?.color,hover:d.options.glyphMarginHoverMessage||null})),
    model:{text:model.getValue(),version:model.getAlternativeVersionId(),undo:model.canUndo(),redo:model.canRedo()},
    tabs:Array.from(document.querySelectorAll('#tab-bar .tab')).map(t=>({path:t._filePath,
      dirty:t.querySelector('.dirty-dot').style.display})),
    calls:window.__reviewFixture.calls.filter(c=>['read_file','save_file','run_action','accept_ai_edit','reject_ai_edit'].includes(c.method))};
})()
"""


def rgb(hex_color):
    values = tuple(int(hex_color[index:index + 2], 16) for index in (1, 3, 5))
    return f"rgb({values[0]}, {values[1]}, {values[2]})"


class AiDiffColorChecks(fixture.EditorReviewChecks):
    def show_diff(self, before, after, *, deleted=False, fallback=False):
        diff = build_ai_line_diff(before, after)
        return self.async_js(r"""
          const f=window.__reviewFixture,model=f.models.get(window.projectPathKey(f.mainPath));
          const before=""" + json.dumps(before) + ";const after=" + json.dumps(after) + r""";
          model.setValue(""" + ("before" if deleted else "after") + r""");
          const snapshot={path:f.mainPath,fileName:'Sketch.ino',revision:'colors',
            beforeExists:true,afterExists:""" + json.dumps(not deleted) + r""",beforeContent:before,content:after,
            pendingCount:1,reviewIndex:1,diff:""" + json.dumps(diff) + r"""};
          f.reviews=[snapshot];
          await window.reloadActiveFileWithDiff(f.mainPath);
          """ + ("window.applyAiDiffHighlights(before,after,model,snapshot);" if fallback else "") + r"""
          editorInstance.revealLineInCenter(2);editorInstance.layout();
          await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
          return true;
        """)

    def colors(self, theme):
        self.js("window.__reviewFixture.theme=" + json.dumps(theme) + ";window.setEditorTheme(window.__reviewFixture.theme);true")
        self.async_js("await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));return true;")
        return self.js(COLORS)

    def assert_green(self, actual, tokens):
        self.assertIsNotNone(actual['line'], actual)
        self.assertIsNotNone(actual['inline'], actual)
        self.assertEqual(actual['line']['animation'], 'none', actual)
        self.assertEqual(actual['line']['border'], rgb(tokens['solid']), actual)
        self.assertEqual(actual['inline']['fg'], rgb(tokens['text']), actual)
        self.assertEqual(actual['inline']['bg'], 'rgba(' + tokens['rgb'] + ', 0.4)', actual)

    def test_replacement_after_is_static_green_with_red_original_marker_in_every_theme(self):
        self.show_diff('// keep\nint oldValue = 1;\n// tail\n', '// keep\nint newValue = 2;\n// tail\n')
        baseline = self.js(COLORS)
        for theme in ('default','light','solarized'):
            with self.subTest(theme=theme):
                observed = self.colors(theme)
                self.assert_green(observed['state']['modified'], observed['tokens']['add'])
                marker = observed['state']['removed']['anchorGutter']
                self.assertIsNotNone(marker, observed)
                self.assertEqual(marker['marker'], rgb(observed['tokens']['remove']['solid']))
                self.assertEqual({d['ruler'].lower() for d in observed['decorations']},
                                 {observed['tokens']['add']['solid'],observed['tokens']['remove']['solid']})
                for key in ('model','tabs','calls'):
                    self.assertEqual(observed[key], baseline[key], key)
                if CAPTURES:
                    self.assertTrue(self.view.grab().save(str(CAPTURES / f'ai-replacement-{theme}.png')))

    def test_added_rows_are_green_and_pure_deletion_keeps_survivor_untinted(self):
        before='// keep\n// removed old line\n// tail\n'
        after='// keep\n// tail\n'
        self.show_diff(before,after)
        for theme in ('default','light','solarized'):
            with self.subTest(theme=theme):
                observed=self.colors(theme)
                self.assertIsNone(observed['state']['modified']['line'])
                self.assertIsNone(observed['state']['removed']['line'])
                self.assertIsNone(observed['state']['removed']['inline'])
                anchor=observed['state']['removed']['anchor']
                self.assertIsNotNone(anchor,observed)
                self.assertEqual(anchor['boundary'],rgb(observed['tokens']['remove']['solid']))
                self.assertEqual(anchor['bg'],'rgba(0, 0, 0, 0)')
        self.show_diff('// keep\n// tail\n','// keep\n// added new line\n// tail\n')
        for theme in ('default','light','solarized'):
            observed=self.colors(theme)
            self.assert_green(observed['state']['added'],observed['tokens']['add'])

    def test_whole_file_removed_text_is_static_red_in_every_theme(self):
        before='// deleted original\nint removedValue = 7;\n// tail\n'
        self.show_diff(before,'',deleted=True)
        for theme in ('default','light','solarized'):
            with self.subTest(theme=theme):
                observed=self.colors(theme)
                removed=observed['state']['removed']
                self.assertIsNotNone(removed['line'],observed)
                self.assertEqual(removed['line']['border'],rgb(observed['tokens']['remove']['solid']))
                self.assertEqual(removed['line']['animation'],'none')
                self.assertEqual(removed['inline']['fg'],rgb(observed['tokens']['remove']['text']))
                self.assertEqual(observed['model']['text'],before)

    def test_original_text_hover_is_code_safe_and_bounded_without_model_writes(self):
        original='<img src=x onerror="window.__originalExecuted=true"> ` ``` <script>bad()</script>'
        before='// keep\n' + '\n'.join(original + ' 😀' * 100 for _ in range(35)) + '\n// tail\n'
        after='// keep\n' + '\n'.join('int replacement' + str(n) + ' = 1;' for n in range(35)) + '\n// tail\n'
        self.show_diff(before,after)
        observed=self.colors('default')
        red=[d for d in observed['decorations'] if d['className']=='ai-edit-removed-anchor-line']
        self.assertEqual(len(red),1)
        hover=red[0]['hover']
        self.assertIn(original,hover['value'])
        self.assertIn('Preview truncated.',hover['value'])
        self.assertLessEqual(len(hover['value'].encode('utf-8')),4400)
        self.assertFalse(hover['isTrusted'])
        self.assertFalse(hover['supportHtml'])
        self.js("editorInstance.setPosition({lineNumber:2,column:1});editorInstance.trigger('fixture','editor.action.showHover',{});true")
        fixture.until(lambda:self.js("Boolean(document.querySelector('.monaco-hover')?.textContent.includes('Original text replaced here'))"))
        rendered=self.js(r"""(() => {const hover=document.querySelector('.monaco-hover');return {
          text:hover.textContent,images:hover.querySelectorAll('img').length,
          scripts:hover.querySelectorAll('script').length,executed:!!window.__originalExecuted};})()""")
        self.assertIn('<img src=x',rendered['text'])
        self.assertEqual((rendered['images'],rendered['scripts'],rendered['executed']),(0,0,False))
        self.assertEqual(self.js(COLORS)['model'],observed['model'])

    def test_fallback_replacement_uses_green_after_and_red_original_marker(self):
        self.show_diff('// keep\nold one\nold two\n// tail\n',
                       '// keep\nnew one\nnew two\n// tail\n',fallback=True)
        for theme in ('default','light','solarized'):
            observed=self.colors(theme)
            self.assert_green(observed['state']['modified'],observed['tokens']['add'])
            marker=observed['state']['removed']['anchorGutter']
            self.assertEqual(marker['marker'],rgb(observed['tokens']['remove']['solid']))
            self.assertTrue(any('old one' in (d['hover'] or {}).get('value','') for d in observed['decorations']))

    def test_exhausted_original_prefix_is_scanned_once_across_failed_preview_hunks(self):
        observed=self.js(r"""(() => {
          const model=editorInstance.getModel(),before='x'.repeat(262144)+'\nold outside budget';
          const changes=Array.from({length:100},(_,index)=>({type:'modified',startLine:1,endLine:1,
            beforeStartLine:index+2,beforeEndLine:index+2}));
          const original=String.prototype.indexOf;let lookups=0;
          try {
            String.prototype.indexOf=function(search,offset) {
              if (this.length===262144 && search==='\n') lookups++;
              return original.call(this,search,offset);
            };
            window.applyStructuredAiDiff({changes,modified:100,firstLine:1},model,
              {path:window.__reviewFixture.helperPath,beforeContent:before,afterExists:true});
            return {lookups,hovers:model.getAllDecorations().filter(d=>d.options.glyphMarginHoverMessage)
              .map(d=>d.options.glyphMarginHoverMessage.value)};
          } finally {String.prototype.indexOf=original;}
        })()""")
        self.assertEqual(observed['lookups'],1,observed)
        self.assertTrue(observed['hovers'])
        self.assertTrue(all(hover=='Original text replaced here' for hover in observed['hovers']))

    def test_theme_hide_and_decoration_refresh_preserve_unsaved_undo_and_review(self):
        self.show_diff('// keep\nint oldValue = 1;\n// tail\n', '// keep\nint newValue = 2;\n// tail\n')
        self.async_js('return await window.navigateToSource({path:window.__reviewFixture.mainPath,line:1,column:1,reveal:"top"});')
        self.js(r"""(() => {const editor=editorInstance;window.__colorModel=editor.getModel();
          editor.pushUndoStop();editor.executeEdits('color-fixture',[
            {range:new monaco.Range(1,1,1,1),text:'// unsaved sentinel\n'}]);editor.pushUndoStop();
          document.querySelector('#tab-bar .tab.active .dirty-dot').style.display='inline';
          editor.setSelection(new monaco.Selection(3,1,3,5));return true;})()""")
        state=r"""(() => ({identity:editorInstance.getModel()===window.__colorModel,
          model:editorInstance.getModel().getValue(),version:editorInstance.getModel().getAlternativeVersionId(),
          undo:editorInstance.getModel().canUndo(),redo:editorInstance.getModel().canRedo(),selection:editorInstance.getSelection(),
          tabs:Array.from(document.querySelectorAll('#tab-bar .tab')).map(t=>({path:t._filePath,
            dirty:t.querySelector('.dirty-dot').style.display})),
          reviews:window.__reviewFixture.reviews.map(r=>({revision:r.revision,diff:r.diff})),
          decorations:editorInstance.getModel().getAllDecorations().filter(d=>String(d.options.className||'').startsWith('ai-edit-'))
            .map(d=>({className:d.options.className,range:d.range}))}))()"""
        baseline=self.js(state)
        self.assertTrue(baseline['undo'])
        for theme in ('light','solarized','default'):
            self.colors(theme)
            self.js('window.setAiBannerHidden(true);window.setAiBannerHidden(false);true')
            self.assertEqual(self.js(state),baseline)
        undone=self.async_js('await window.__colorModel.undo();return window.__colorModel.getValue();')
        self.assertNotIn('unsaved sentinel',undone)
        self.async_js('await window.__colorModel.redo();return true;')
        self.assertEqual(self.js(state)['model'],baseline['model'])
        self.assertEqual(self.js(COLORS)['calls'],[])


def load_tests(loader, tests, pattern):
    # Reuse the renderer setup, not the parent's unrelated navigation suite.
    return unittest.TestSuite(AiDiffColorChecks(name) for name in AiDiffColorChecks.__dict__ if name.startswith('test_'))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--render-dir',type=Path)
    options,args=parser.parse_known_args()
    if options.render_dir:
        CAPTURES=fixture.capture_directory(options.render_dir)
        CAPTURES.mkdir(parents=True,exist_ok=True)
    run=unittest.main(argv=[sys.argv[0],*args],verbosity=2,exit=False)
    fixture.QTimer.singleShot(150,fixture.APP.quit)
    fixture.APP.exec()
    sys.exit(0 if run.result.wasSuccessful() else 1)
