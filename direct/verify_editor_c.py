#!/usr/bin/env python3
"""Real offline C editor checks with synthetic models and a mocked bridge."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'direct'))
from verify_editor_review_ui import EditorReviewChecks, until
from PySide6.QtWebEngineCore import QWebEngineScript


C_STARTUP_RACE = r"""
(() => {
  const state={cppReleased:false,cReleased:false,cppWaiters:[],cWaiters:[],model:null,
    completionRegistrations:0,provider:null};
  window.__cStartupRace=state;
  let api;
  Object.defineProperty(window,'monaco',{configurable:true,get:()=>api,set:value=>{
    api=value;
    if(!api?.languages) return;
    const register=api.languages.registerCompletionItemProvider;
    if(!register.__startupFixtureWrapped){
      api.languages.registerCompletionItemProvider=function(language,provider){
        if(language==='c'){state.completionRegistrations++;state.provider=provider;}
        return register.apply(this,arguments);
      };
      api.languages.registerCompletionItemProvider.__startupFixtureWrapped=true;
    }
    const gate=entry=>{
      const id=entry.id;
      if(id!=='cpp' || entry.loader.__startupFixtureWrapped) return;
      const loader=entry.loader;
      entry.loader=()=>loader().then(loaded=>new Promise(resolve=>{
        if(state[id+'Released']) resolve(loaded);
        else state[id+'Waiters'].push(()=>resolve(loaded));
      }));
      entry.loader.__startupFixtureWrapped=true;
    };
    const registerLanguage=api.languages.register;
    if(!registerLanguage.__startupFixtureWrapped){
      api.languages.register=function(entry){gate(entry);return registerLanguage.apply(this,arguments);};
      api.languages.register.__startupFixtureWrapped=true;
    }
    const registerFactory=api.languages.registerTokensProviderFactory;
    if(!registerFactory.__startupFixtureWrapped){
      api.languages.registerTokensProviderFactory=function(language,factory){
        if(language==='c'){
          const original=factory;
          factory={create:async()=>{
            const definition=await original.create();
            return await new Promise(resolve=>{
              if(state.cReleased) resolve(definition);
              else state.cWaiters.push(()=>resolve(definition));
            });
          }};
        }
        return registerFactory.call(this,language,factory);
      };
      api.languages.registerTokensProviderFactory.__startupFixtureWrapped=true;
    }
    api.languages.getLanguages().filter(language=>['cpp','c'].includes(language.id)).forEach(gate);
  }});
  document.addEventListener('DOMContentLoaded',()=>{
    state.model=monaco.editor.createModel('_Generic class namespace','c');
    monaco.editor.tokenize(state.model.getValue(),'c');
  },{once:true});
  state.release=id=>{
    state[id+'Released']=true;state[id+'Waiters'].splice(0).forEach(resolve=>resolve());
  };
})()
"""


class EditorCChecks(unittest.TestCase):
    # Share the isolated renderer fixture without rerunning its review suite.
    load = EditorReviewChecks.load
    js = EditorReviewChecks.js
    async_js = EditorReviewChecks.async_js
    _dispose_renderer = EditorReviewChecks._dispose_renderer

    def setUp(self):
        EditorReviewChecks.setUp(self)
        self.assertTrue(self.async_js('return await window.editorCFamilyReady;'))
        self.js(r"""(() => {
          const f=window.__reviewFixture;
          const file={name:'driver.c',path:'C:/fixture/Sketch/driver.c'};
          const content='#include <stdint.h>\nstruct Reading { int value; };\nint read_value(void) {\n  return 1;\n}\n';
          const model=monaco.editor.createModel(content,'cpp',monaco.Uri.file(file.path));
          model.setEOL(monaco.editor.EndOfLineSequence.LF);
          f.files.push(file);f.models.set(window.projectPathKey(file.path),model);f.cPath=file.path;
          const tab=document.createElement('div');tab.className='tab';tab._filePath=file.path;
          tab.setAttribute('data-file-path',file.path);
          const label=document.createElement('span');label.textContent=file.name;
          const dirty=document.createElement('span');dirty.className='dirty-dot';dirty.style.display='none';
          tab.append(label,dirty);tab.addEventListener('click',()=>{
            document.querySelectorAll('#tab-bar .tab.active').forEach(t=>t.classList.remove('active'));
            tab.classList.add('active');window.editorInstance.setModel(model);
          });document.getElementById('tab-bar').append(tab);tab.click();
          window.syncModelLanguageForActiveTab();return true;
        })()""")

    def test_c_selection_preserves_dirty_model_version_range_and_tab_order(self):
        result=self.js(r"""(() => {
          const e=window.editorInstance,f=window.__reviewFixture,m=e.getModel();
          m.applyEdits([{range:new monaco.Range(4,10,4,11),text:'42'}]);
          e.setSelection(new monaco.Range(4,3,4,12));
          const expected={value:m.getValue(),version:m.getAlternativeVersionId(),selection:e.getSelection(),
            tabs:Array.from(document.querySelectorAll('#tab-bar .tab')).map(t=>t._filePath)};
          monaco.editor.setModelLanguage(m,'cpp');window.syncModelLanguageForActiveTab();
          return {expected,actual:{value:m.getValue(),version:m.getAlternativeVersionId(),selection:e.getSelection(),
            tabs:Array.from(document.querySelectorAll('#tab-bar .tab')).map(t=>t._filePath)},
            language:m.getLanguageId(),same:m===f.models.get(window.projectPathKey(f.cPath))};
        })()""")
        self.assertTrue(result['same'])
        self.assertEqual(result['language'],'c')
        self.assertEqual(result['actual'],result['expected'])
        result=self.js(r"""(() => {
          const f=window.__reviewFixture,e=window.editorInstance;
          window.activateProjectFile(f.mainPath);window.syncModelLanguageForActiveTab();const ino=e.getModel().getLanguageId();
          window.activateProjectFile(f.helperPath);window.syncModelLanguageForActiveTab();const cpp=e.getModel().getLanguageId();
          window.activateProjectFile(f.cPath);window.syncModelLanguageForActiveTab();
          return {ino,cpp,c:e.getModel().getLanguageId()};
        })()""")
        self.assertEqual(result,{'ino':'cpp','cpp':'cpp','c':'c'})

    def test_real_c_tokens_do_not_treat_cpp_words_as_keywords(self):
        result=self.js(r"""(() => {
          const sample='restrict _Generic _Atomic struct class namespace template nullptr\n0x1.fp+3 3. 0.5f\n/* class */\n"namespace"';
          const tokens=monaco.editor.tokenize(sample,'c');
          const words=sample.split('\n')[0].split(' ');let offset=0;const kinds={};
          for(const word of words){const token=tokens[0].filter(t=>t.offset<=offset).pop();kinds[word]=token?.type;offset+=word.length+1;}
          return {kinds,tokens};
        })()""")
        for word in ['restrict','_Generic','_Atomic','struct','nullptr']:
            self.assertTrue(result['kinds'][word].startswith('keyword.'),result)
        for word in ['class','namespace','template']:
            self.assertEqual(result['kinds'][word],'identifier.c',result)
        self.assertTrue(any(t['type']=='number.float.c' for t in result['tokens'][1]),result)
        self.assertTrue(all(t['type'].startswith('comment') for t in result['tokens'][2]),result)
        self.assertTrue(all(t['type'].startswith('string') for t in result['tokens'][3]),result)

    def test_c_suggest_widget_offers_c_keywords_and_standard_headers(self):
        self.js(r"""(() => {const e=window.editorInstance;e.getModel().setValue('_Gen');e.setPosition({lineNumber:1,column:5});e.focus();e.trigger('fixture','editor.action.triggerSuggest',{});return true;})()""")
        rows=[]
        until(lambda: (rows.append(self.js("Array.from(document.querySelectorAll('.suggest-widget .monaco-list-row')).map(row=>row.textContent)")), any('_Generic' in row for row in rows[-1]))[1])
        self.assertFalse(any('namespace' in row or 'class' in row for row in rows[-1]),rows[-1])
        self.js(r"""(() => {const e=window.editorInstance;e.trigger('fixture','hideSuggestWidget',{});e.getModel().setValue('#include <std');e.setPosition({lineNumber:1,column:14});e.trigger('fixture','editor.action.triggerSuggest',{});return true;})()""")
        until(lambda: (rows.append(self.js("Array.from(document.querySelectorAll('.suggest-widget .monaco-list-row')).map(row=>row.textContent)")), any('stdint.h' in row for row in rows[-1]))[1])

    def test_c_brace_enter_indents_and_keeps_syntax_markers(self):
        result=self.js(r"""(() => {
          const e=window.editorInstance,m=e.getModel();m.setValue('void step(void) {}');
          m.updateOptions({tabSize:4,insertSpaces:true});e.setPosition({lineNumber:1,column:18});
          monaco.editor.setModelMarkers(m,'fixture-c',[{severity:monaco.MarkerSeverity.Warning,
            startLineNumber:1,startColumn:6,endLineNumber:1,endColumn:10,message:'Synthetic warning'}]);
          e.trigger('keyboard','type',{text:'\n'});
          return {value:m.getValue(),eol:m.getEOL(),language:m.getLanguageId(),warnings:monaco.editor.getModelMarkers({owner:'fixture-c'}).length};
        })()""")
        self.assertEqual(result['language'],'c')
        self.assertEqual(result['value'],result['eol'].join(['void step(void) {','    ','}']))
        self.assertEqual(result['warnings'],1)


class EditorCStartupChecks(unittest.TestCase):
    js = EditorReviewChecks.js
    async_js = EditorReviewChecks.async_js
    _dispose_renderer = EditorReviewChecks._dispose_renderer

    def load(self, reload=False):
        script=QWebEngineScript()
        script.setName('Isolated first C activation race')
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setSourceCode(C_STARTUP_RACE)
        self.page.scripts().insert(script)
        EditorReviewChecks.load(self,reload=reload)

    def setUp(self):
        EditorReviewChecks.setUp(self)

    def test_first_c_model_and_late_lazy_registration_keep_c_tokens(self):
        self.assertTrue(self.js('Boolean(window.__cStartupRace.model)'))
        until(lambda:self.js('window.__cStartupRace.cppWaiters.length>0'))
        self.js("window.__cStartupRace.release('cpp');true")
        self.assertTrue(self.async_js('return await window.editorCFamilyReady;'))
        until(lambda:self.js('window.__cStartupRace.cWaiters.length>0'))
        before=self.js("monaco.editor.tokenize('_Generic class namespace','c')[0]")
        self.assertEqual(before[0]['type'],'keyword.-Generic.c',before)
        self.js("window.__cStartupRace.release('c');true")
        self.async_js('await new Promise(resolve=>setTimeout(resolve,100));return true;')
        after=self.js("monaco.editor.tokenize('_Generic class namespace','c')[0]")
        self.assertEqual(after[0]['type'],'keyword.-Generic.c',after)
        self.assertEqual(after[2]['type'],'identifier.c',after)
        cpp=self.js("monaco.editor.tokenize('class _Generic','cpp')[0]")
        self.assertEqual(cpp[0]['type'],'keyword.class.cpp',cpp)
        self.assertEqual(cpp[2]['type'],'identifier.cpp',cpp)
        self.assertEqual(self.js('window.__cStartupRace.completionRegistrations'),1)

    def test_all_c_project_loads_before_configuration_without_losing_dirty_content(self):
        self.assertTrue(self.async_js(r"""
          const f=window.__reviewFixture;
          f.files.splice(0,f.files.length,{name:'driver.c',path:'C:/fixture/Sketch/driver.c'},
            {name:'sensor.c',path:'C:/fixture/Sketch/sensor.c'});
          window.pywebview.api.read_file=path=>({success:true,content:'int class = 1;\nint read_value(void) { return class; }\n'});
          await window.safeLoadProject();await window.syncTabPaths();
          window.syncModelLanguageForActiveTab();
          const e=window.editorInstance,m=e.getModel();
          m.applyEdits([{range:new monaco.Range(1,13,1,14),text:'42'}]);
          window.__startupModel=m;
          window.__startupDirty={content:m.getValue(),version:m.getAlternativeVersionId(),
            order:Array.from(document.querySelectorAll('#tab-bar .tab')).map(t=>t._filePath)};
          return m.getLanguageId()==='c';
        """))
        self.js("window.__cStartupRace.release('cpp');true")
        self.assertTrue(self.async_js('return await window.editorCFamilyReady;'))
        until(lambda:self.js('window.__cStartupRace.cWaiters.length>0'))
        self.js("window.__cStartupRace.release('c');true")
        self.async_js('await new Promise(resolve=>setTimeout(resolve,100));return true;')
        result=self.js(r"""(() => {
          const e=window.editorInstance,m=e.getModel();
          const actual={content:m.getValue(),version:m.getAlternativeVersionId(),
            order:Array.from(document.querySelectorAll('#tab-bar .tab')).map(t=>t._filePath)};
          return {expected:window.__startupDirty,actual,same:m===window.__startupModel,
            language:m.getLanguageId(),tokens:monaco.editor.tokenize('class _Generic','c')[0],
            completions:window.__cStartupRace.completionRegistrations};
        })()""")
        self.assertTrue(result['same'],result)
        self.assertEqual(result['actual'],result['expected'])
        self.assertEqual(result['language'],'c')
        self.assertEqual(result['tokens'][0]['type'],'identifier.c')
        self.assertEqual(result['tokens'][2]['type'],'keyword.-Generic.c')
        self.assertEqual(result['completions'],1)


if __name__=='__main__':
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case)
        for case in (EditorCChecks,EditorCStartupChecks))
    outcome=unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if outcome.wasSuccessful() else 1)
