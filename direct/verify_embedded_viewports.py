#!/usr/bin/env python3
"""Real offline Monaco/xterm geometry and theme checks, without sessions or writes.

Run in a fresh process for each QT_SCALE_FACTOR. Page profiles are memory-only;
the fixture supplies transports and models without opening a project or PTY.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from main.core.log_colors import themed_terminal_colors
from main.qt.garbage_collection import install_gui_garbage_collector

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
COLLECTOR = install_gui_garbage_collector(APP)
RENDER_DIR = None
MEASUREMENTS = []

TRANSPORT = r"""
window.fixtureInput = [];
window.fixtureResizes = [];
window.fixtureStarts = 0;
window.fixtureConsumed = 0;
const RealTerminal = window.Terminal;
window.Terminal = class extends RealTerminal {
  constructor(options) { super(options); window.fixtureTerminal = this; }
};
window.qt = {webChannelTransport:{}};
window.QWebChannel = class {
  constructor(_, complete) {
    const bridge = {
      start: done => { window.fixtureStarts++; if (done) done(); },
      resize: (rows, cols) => window.fixtureResizes.push([rows, cols]),
      consumed: (...args) => { window.fixtureConsumed += args.at(-1); },
      write: (...args) => window.fixtureInput.push(args.at(-1)),
      reset_view: {connect: callback => { window.fixtureReset = callback; }},
      output: {connect: callback => { window.fixtureOutput = callback; }}
    };
    complete({objects:{terminalBridge:bridge, assistantBridge:bridge}});
  }
};
window.WebSocket = class {
  static OPEN = 1;
  constructor() { this.readyState=1; window.fixtureSocket=this; }
  send(value) {
    const message=JSON.parse(value);
    if (message.type==='input') window.fixtureInput.push(message.data);
    if (message.type==='resize') window.fixtureResizes.push([message.rows,message.cols]);
    if (message.type==='ack') window.fixtureConsumed+=message.chars;
  }
};
"""


def wait_for(predicate, message, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    raise AssertionError(message)


class EmbeddedViewportChecks(unittest.TestCase):
    def setUp(self):
        self.host = QWidget()
        self.host.resize(960, 540)
        self.view = QWebEngineView(self.host)
        # A desktop WM may maximize a large fixture at high DPR. Keep the child
        # viewport bounded so compact requests still exercise compact rendering.
        self.view.setMaximumSize(960, 540)
        layout = QVBoxLayout(self.host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self.profile = QWebEngineProfile(self.view)
        self.assertTrue(self.profile.isOffTheRecord())
        self.page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self.page)
        self.view.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        self.host.show()
        self.addCleanup(self.dispose)

    def dispose(self):
        self.host.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.profile.deleteLater()
        self.host.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def js(self, code):
        result = []
        self.page.runJavaScript(code, result.append)
        wait_for(lambda: bool(result), "The fixture renderer did not answer")
        return result[0]

    def state(self, code):
        return json.loads(self.js("JSON.stringify((() => {" + code + "})())"))

    def load(self, name):
        loaded = []
        self.view.loadFinished.connect(loaded.append)
        path = ROOT / "src/editor" / (name if name != "windows-terminal" else "terminal.html")
        if name == "index.html":
            self.view.setUrl(QUrl.fromLocalFile(str(path)))
        else:
            if name == "windows-terminal":
                tree = ast.parse((ROOT / "src/modules/project_terminal.py").read_text(encoding="utf-8-sig"))
                html = next(ast.literal_eval(node.value) for node in tree.body
                            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                            and target.id == "HTML_CONTENT_TEMPLATE" for target in node.targets))
                replacements = {"__SCROLLBACK__":"2000", "__CURSOR_BLINK__":"false",
                    "__THEME_BG__":"#101b2a", "__THEME_FG__":"#e3edf6",
                    "__THEME_CURSOR__":"#80d9ce", "__THEME_SELECTION__":"#29475a",
                    "__THEME_XTERM__":json.dumps(themed_terminal_colors("default"))}
                for key, value in replacements.items():
                    html = html.replace(key,value)
                html = html.replace("/assets/xterm/", "../assets/xterm/")
            else:
                html = path.read_text(encoding="utf-8")
            html = html.replace("<script>", "<script>" + TRANSPORT, 1)
            self.view.setHtml(html, QUrl.fromLocalFile(str(path)))
        wait_for(lambda: bool(loaded), "The offline fixture page did not load")
        self.assertTrue(loaded[0])
        self.view.loadFinished.disconnect(loaded.append)
        if name == "windows-terminal":
            self.js("fixtureSocket.onmessage({data:JSON.stringify({type:'create',shell:'fixture'})}); true")

    def fit_host(self, width, height):
        self.view.setMaximumSize(width, height)
        self.host.resize(width, height)
        wait_for(lambda: abs(self.js("innerWidth") - self.view.width()) <= 1
                 and abs(self.js("innerHeight") - self.view.height()) <= 1
                 and self.view.width() <= width and self.view.height() <= height,
                 "CSS viewport must equal Qt logical view size without a second DPI conversion")
        if width <= 420:
            self.assertAlmostEqual(self.view.width(), width, delta=1,
                                   msg="The desktop must exercise the requested compact width")

    def capture(self, name):
        if RENDER_DIR is not None:
            QTest.qWait(50)
            self.assertTrue(self.view.grab().save(str(RENDER_DIR / name)))

    def terminal_checks(self, name, host_id, configure, fit):
        self.load(name)
        wait_for(lambda: self.js("!!window.fixtureTerminal?.element"), "xterm did not open")
        # Real desktop input may arrive while the renderer's own startup code
        # focuses its terminal. It cannot become fixture input or affect output.
        self.js("fixtureTerminal.options.disableStdin=true; true")
        self.js("window.originalTerminal=fixtureTerminal; window.outputReady=false;"
                "fixtureTerminal.write(Array.from({length:150},(_,i)=>`Fixture Unicode λ ${i}\\r\\n`).join(''),"
                "()=>{window.outputReady=true}); true")
        wait_for(lambda: self.js("window.outputReady"), "The fixture output did not parse")
        self.js("fixtureTerminal.select(0,3,12); true")
        baseline = self.state("return {starts:fixtureStarts, first:fixtureTerminal.buffer.active.getLine(0).translateToString(true), selection:fixtureTerminal.getSelection()};")
        self.assertTrue(baseline["selection"])
        for mode in ("default", "light", "solarized_dark", "default"):
            theme = themed_terminal_colors(mode)
            for width, height, hidden in ((960, 540, False), (420, 260, False), (760, 420, True)):
                with self.subTest(page=name, mode=mode, size=(width, height), hidden=hidden):
                    if hidden:
                        self.host.hide()
                    self.host.resize(width, height)
                    if name == "windows-terminal":
                        self.js(f"applyTheme({json.dumps(theme)}); true")
                    else:
                        self.js(f"{configure}({json.dumps(theme)},14,2000); true")
                    if hidden:
                        self.host.show()
                    self.fit_host(width, height)
                    self.js(f"{fit}(); true")
                    QTest.qWait(80)
                    data = self.state("""
                      const term=fixtureTerminal, host=document.getElementById(%s),
                        screen=term.element.querySelector('.xterm-screen'),
                        viewport=term.element.querySelector('.xterm-viewport');
                      const rect=node=>{const r=node.getBoundingClientRect();
                        return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
                      return {dpr:devicePixelRatio,viewport:{width:innerWidth,height:innerHeight},
                        host:rect(host),screen:rect(screen), rows:term.rows,cols:term.cols,
                        scrollbar:viewport.offsetWidth-viewport.clientWidth,
                        backgrounds:[document.documentElement,document.body,host].map(n=>getComputedStyle(n).backgroundColor),
                        expected:(()=>{const n=document.createElement('i');n.style.color=%s;
                          document.body.append(n);const color=getComputedStyle(n).color;n.remove();return color;})(),
                        same:term===originalTerminal,starts:fixtureStarts,input:fixtureInput.length,
                        selection:term.getSelection(),
                        first:term.buffer.active.getLine(0).translateToString(true)};
                    """ % (json.dumps(host_id), json.dumps(theme["background"])))
                    MEASUREMENTS.append({"page": name, "mode": mode, **data})
                    self.assertTrue(data["same"], "Layout/theme changes must retain the terminal instance")
                    self.assertEqual(data["starts"], baseline["starts"], "Display changes must not restart a session")
                    self.assertEqual(data["input"], 0, "Display changes must not type or replay commands")
                    self.assertEqual(data["first"], baseline["first"], "Retained output history must survive display changes")
                    self.assertEqual(data["selection"], baseline["selection"], "The selected output must survive display changes")
                    self.assertAlmostEqual(data["dpr"], self.view.devicePixelRatioF(), delta=.01)
                    for background in data["backgrounds"]:
                        self.assertEqual(background, data["expected"], "All terminal surfaces must follow the active theme")
                    host, screen = data["host"], data["screen"]
                    self.assertAlmostEqual(host["width"], data["viewport"]["width"], delta=1)
                    self.assertAlmostEqual(host["height"], data["viewport"]["height"], delta=1)
                    self.assertLessEqual(host["right"] - screen["right"], screen["width"] / data["cols"] + 16 + data["scrollbar"])
                    self.assertLessEqual(host["bottom"] - screen["bottom"], screen["height"] / data["rows"] + 16)
                    self.assertLessEqual(screen["right"], host["right"] + 1)
                    self.assertLessEqual(screen["bottom"], host["bottom"] + 1)
                    if width == 420:
                        self.capture(f"{Path(name).stem}-{mode}.png")

    def test_project_terminal_retains_geometry_history_and_theme(self):
        self.terminal_checks("terminal.html", "terminal", "configureTerminal", "fitTerminal")

    def test_opencode_retains_geometry_history_and_theme(self):
        self.terminal_checks("opencode.html", "assistant", "configureOpenCode", "fitOpenCode")

    def test_windows_terminal_page_retains_geometry_history_and_theme(self):
        self.terminal_checks("windows-terminal", "terminal-root", "applyTheme", "fitAll")

    def test_monaco_fills_native_viewport_and_preserves_model_undo(self):
        self.load("index.html")
        wait_for(lambda: self.js("!!window.editorInstance && !!window.monaco"), "Monaco did not initialize")
        self.js(r"""(() => {
          const editor=editorInstance;
          window.fixtureModel=monaco.editor.createModel('// untouched\nvoid setup() {}', 'cpp');
          editor.setModel(fixtureModel);
          editor.updateOptions({readOnly:false,minimap:{enabled:false},cursorBlinking:'solid'});
          editor.executeEdits('fixture',[{range:new monaco.Range(1,1,1,1),text:'// unsaved λ\n'}]);
          editor.pushUndoStop();
          editor.setSelection(new monaco.Range(2,4,2,11));
          window.fixtureContent=fixtureModel.getValue();
          window.fixtureVersion=fixtureModel.getAlternativeVersionId();
          const tab=document.createElement('div');tab.className='tab active';
          tab.innerHTML='Fixture.cpp <span class="dirty-dot"></span>';
          document.getElementById('tab-bar').append(tab);window.fixtureTab=tab;
          return true;
        })()""")
        for mode, width, height, hidden in (("default",960,540,False), ("light",420,260,False),
                                           ("solarized_dark",760,420,True), ("default",960,540,False)):
            with self.subTest(mode=mode, size=(width,height), hidden=hidden):
                if hidden:
                    self.host.hide()
                self.host.resize(width,height)
                self.js(f"setEditorTheme({json.dumps(mode)}); forceEditorLayout(); true")
                if hidden:
                    self.host.show()
                self.fit_host(width,height)
                self.js("forceEditorLayout(); true")
                QTest.qWait(80)
                data = self.state("""
                  const host=document.getElementById('editor-container'),
                    r=host.getBoundingClientRect(),layout=editorInstance.getLayoutInfo();
                  return {dpr:devicePixelRatio,viewport:{width:innerWidth,height:innerHeight},
                    host:{x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom},
                    layout:{width:layout.width,height:layout.height},
                    same:editorInstance.getModel()===fixtureModel,content:fixtureModel.getValue(),
                    version:fixtureModel.getAlternativeVersionId(),tab:fixtureTab.isConnected,
                    selection:fixtureModel.getValueInRange(editorInstance.getSelection()),
                    dirty:!!fixtureTab.querySelector('.dirty-dot'),
                    background:getComputedStyle(host).backgroundColor,
                    editorBackground:getComputedStyle(editorInstance.getDomNode()).backgroundColor};
                """)
                self.assertTrue(data["same"])
                self.assertAlmostEqual(data["dpr"], self.view.devicePixelRatioF(), delta=.01)
                self.assertEqual(data["content"], self.js("fixtureContent"))
                self.assertEqual(data["version"], self.js("fixtureVersion"))
                self.assertTrue(data["tab"] and data["dirty"])
                self.assertEqual(data["selection"], "untouch")
                self.assertAlmostEqual(data["host"]["right"], data["viewport"]["width"], delta=1)
                self.assertAlmostEqual(data["host"]["bottom"], data["viewport"]["height"], delta=1)
                self.assertAlmostEqual(data["layout"]["width"], data["host"]["width"], delta=1)
                self.assertAlmostEqual(data["layout"]["height"], data["host"]["height"], delta=1)
                self.assertEqual(data["background"], data["editorBackground"])
                MEASUREMENTS.append({"page":"index.html", "mode":mode, **data})
                if width == 420:
                    self.capture(f"editor-{mode}.png")
        self.js("fixtureModel.undo(); true")
        self.assertEqual(self.js("fixtureModel.getValue()"), "// untouched\nvoid setup() {}")
        self.js("fixtureModel.redo(); true")
        self.assertEqual(self.js("fixtureModel.getValue()"), self.js("fixtureContent"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args, rest = parser.parse_known_args()
    for output in (args.render_dir, args.report):
        if output is not None and ROOT / "temp" not in output.resolve().parents:
            parser.error("Fixture outputs must remain below this checkout's temp directory")
    if args.render_dir:
        RENDER_DIR = args.render_dir.resolve()
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EmbeddedViewportChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"scale":os.environ.get("QT_SCALE_FACTOR","1"),
            "platform":os.environ["QT_QPA_PLATFORM"],"successful":result.wasSuccessful(),
            "measurements":MEASUREMENTS}, indent=2) + "\n", encoding="utf-8")
    sys.exit(not result.wasSuccessful())
