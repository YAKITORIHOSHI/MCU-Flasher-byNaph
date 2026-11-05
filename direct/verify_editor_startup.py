#!/usr/bin/env python3
"""Measure offline editor startup against memory-only sources and preferences.

The optional HEAD comparison uses an owned temp copy of the page and bundle.
It never opens project sources, settings, device services or recovery journals.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PySide6.QtCore import QCoreApplication, QEvent, QUrl, QUrlQuery
from PySide6.QtTest import QTest
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication


FIXTURE = r"""
(() => {
  const sample='// isolated source\nvoid step() {}\n';
  const files=Array.from({length:12},(_,n)=>({name:`source${n}.cpp`,path:`/fixture/source${n}.cpp`}));
  const state={lists:0,reads:0,themes:0,fonts:0,layouts:0,updates:0,ready:0,files};
  window.__startupFixture=state;
  let editor;
  Object.defineProperty(window,'editorInstance',{configurable:true,get:()=>editor,set:value=>{
    editor=value;
    state.initialOptions=JSON.parse(JSON.stringify(editor.getRawOptions()));
    state.initialBackground=getComputedStyle(editor.getDomNode()).backgroundColor;
    state.loadingStarted=Boolean(document.getElementById('editor-loading')&&!document.getElementById('editor-loading').hidden);
    const layout=editor.layout.bind(editor),update=editor.updateOptions.bind(editor),setModel=editor.setModel.bind(editor);
    editor.layout=(...args)=>{state.layouts++;return layout(...args);};
    editor.updateOptions=(...args)=>{state.updates++;return update(...args);};
    editor.setModel=(...args)=>{const result=setModel(...args);if(state.reads===12&&!state.ready)state.ready=performance.now();return result;};
  }});
  const fixtureApi={
    get_project_files:async()=>{state.lists++;return files;},
    read_file:async()=>{state.reads++;await new Promise(resolve=>setTimeout(resolve,8));return {content:sample,success:true};},
    get_theme_mode:async()=>{state.themes++;return new URLSearchParams(location.search).get('theme')||'default';},
    get_font_size:async()=>{state.fonts++;return 19;},
    get_recovery_buffers:async()=>({}),recovery_complete:async()=>true,
    set_active_file:async()=>true,mark_modified:async()=>true,
    get_ai_history_state:async()=>({canUndo:false,canRedo:false}),
    get_ai_edit_reviews:async()=>[],get_pending_ai_review:async()=>null,
    realtime_check_syntax:async()=> 'null'
  };
  if(new URLSearchParams(location.search).get('transport')!=='native') window.pywebview={api:fixtureApi};
  let loadReady;
  Object.defineProperty(window,'__mcuProjectLoadReady',{configurable:true,get:()=>loadReady,set:value=>{
    loadReady=value;if(value)state.ready=performance.now();
  }});
  document.addEventListener('DOMContentLoaded',()=>{
    state.documentReady=performance.now();
    if(window.__mcuInitialProjectLoad) window.__mcuInitialProjectLoad.then(()=>{state.ready=performance.now();});
  });
})();
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--native-transport", action="store_true")
    parser.add_argument("--theme", choices=("default", "light", "solarized_dark"), default="default")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--capture", type=Path)
    args = parser.parse_args()
    if args.baseline and args.native_transport:
        parser.error("The immutable HEAD comparison uses its original compatibility transport")
    for output in (args.report, args.capture):
        if output and ROOT / "temp" not in output.resolve().parents:
            parser.error("Outputs must remain below the checkout's temp directory")
    scratch = ROOT / "temp" / "audit" / "editor-startup"
    scratch.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    view = QWebEngineView()
    view.resize(960, 540)
    profile = QWebEngineProfile(view)
    assert profile.isOffTheRecord()
    page = QWebEnginePage(profile, view)
    view.setPage(page)
    backend = None
    renderer = ""
    if args.native_transport:
        from PySide6.QtWebChannel import QWebChannel
        from main.qt.editor_panel import EditorBridgeAPI

        class MemoryBackend:
            sketch_dir_path = Path("/fixture")
            active_file_path = None
            modified_files = {}
            lists = 0
            reads = 0
            threads = set()

            def get_cloud_project_files(self):
                self.lists += 1
                self.threads.add(threading.get_ident())
                time.sleep(.008)
                return {"success": True, "files": [
                    {"name": f"source{index}.cpp", "path": f"/fixture/source{index}.cpp"}
                    for index in range(12)]}

            def get_project_files(self):
                raise AssertionError("Initial source listing must use the strict worker")

            def read_file(self, path):
                self.reads += 1
                self.threads.add(threading.get_ident())
                time.sleep(.008)
                return {"success": True, "content": "// isolated source\nvoid step() {}\n"}

            def set_active_file(self, path):
                self.active_file_path = path

        backend = MemoryBackend()
        bridge = EditorBridgeAPI(backend, view)
        renderer = bridge.begin_renderer_load()
        channel = QWebChannel(page)
        channel.registerObject("editorBridge", bridge)
        page.setWebChannel(channel)
    script = QWebEngineScript()
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setSourceCode(FIXTURE)
    page.scripts().insert(script)
    view.show()

    def until(predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        raise AssertionError("The isolated editor startup did not complete")

    def js(code):
        values = []
        page.runJavaScript("JSON.stringify((()=>{" + code + "})())", values.append)
        until(lambda: bool(values))
        return json.loads(values[0]) if values[0] else None

    with tempfile.TemporaryDirectory(prefix="head-", dir=scratch) as directory:
        source = ROOT / "src" / "editor" / "index.html"
        if args.baseline:
            # Resolve normal assets from the actual offline directory, while
            # keeping both compared app scripts at their immutable HEAD bytes.
            source = Path(directory) / "index.html"
            html = subprocess.check_output(["git", "show", "HEAD:src/editor/index.html"], cwd=ROOT).decode()
            bundle = Path(directory) / "bundle.js"
            bundle.write_bytes(subprocess.check_output(["git", "show", "HEAD:src/editor/bundle.js"], cwd=ROOT))
            html = html.replace('<head>', '<head><base href="' + QUrl.fromLocalFile(str(ROOT / 'src/editor') + '/').toString() + '">', 1)
            html = html.replace('src="bundle.js"', 'src="' + QUrl.fromLocalFile(str(bundle)).toString() + '"')
            source.write_text(html, encoding="utf-8")
        url = QUrl.fromLocalFile(str(source))
        query = QUrlQuery()
        query.addQueryItem("theme", args.theme)
        query.addQueryItem("font", "19")
        query.addQueryItem("constrained", "1")
        query.addQueryItem("renderer", renderer)
        if args.native_transport:
            query.addQueryItem("transport", "native")
        url.setQuery(query)
        view.load(url)
        until(lambda: js("return !!window.editorInstance && document.querySelectorAll('#tab-bar .tab').length===12 && !!document.querySelector('#tab-bar .tab.active')?._filePath;"))
        QTest.qWait(450)
        report = js("""
          const state=window.__startupFixture,editor=editorInstance,host=document.getElementById('editor-container');
          return {...state,files:state.files.length,baseline:""" + json.dumps(args.baseline) + """,
            theme:""" + json.dumps(args.theme) + """,elapsed:performance.now(),
            background:getComputedStyle(host).backgroundColor,
            editorBackground:getComputedStyle(editor.getDomNode()).backgroundColor,
            font:editor.getOption(monaco.editor.EditorOption.fontSize),
            loadingHidden:document.getElementById('editor-loading')?.hidden??true,
            modelCount:monaco.editor.getModels().length,
            automaticLayout:editor.getRawOptions().automaticLayout};
        """)
        if backend:
            report.update(lists=backend.lists, reads=backend.reads, nativeTransport=True,
                          storageOffGui=bool(backend.threads) and threading.get_ident() not in backend.threads)
            assert report["storageOffGui"], report
        if not args.baseline:
            assert report["lists"] == 1, report
            assert report["reads"] == 12, report
            assert report["themes"] == report["fonts"] == 0, report
            assert report["initialOptions"]["fontSize"] == report["font"] == 19, report
            expected_theme = {"default": "mcu-default-dark", "light": "mcu-light",
                              "solarized_dark": "mcu-solarized-dark"}[args.theme]
            assert report["initialOptions"]["theme"] == expected_theme, report
            # Monaco is see-through from its first paint, over the well's smoke.
            assert report["initialBackground"] == report["editorBackground"] == "rgba(0, 0, 0, 0)", report
            assert report["loadingStarted"], report
            assert report["background"] != report["editorBackground"], report
            assert report["loadingHidden"], report
            assert not report["automaticLayout"], report
        if args.capture:
            args.capture.parent.mkdir(parents=True, exist_ok=True)
            assert view.grab().save(str(args.capture))
        report.pop("initialOptions", None)
        print(json.dumps(report, indent=2))
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        view.close()
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        profile.deleteLater()
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    main()
