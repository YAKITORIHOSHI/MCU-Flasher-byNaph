#!/usr/bin/env python3
"""Real offline xterm pages with fixture transports; no PTY or settings writes."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'windows' if sys.platform == 'win32' else 'offscreen')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication
from main.qt.garbage_collection import install_gui_garbage_collector
from main.qt.theme import get_palette

APP = QApplication.instance() or QApplication([])
COLLECTOR = install_gui_garbage_collector(APP)
RENDER_DIR = None

TRANSPORT = r'''
window.auditMessages = [];
window.auditInput = [];
window.auditConsumed = 0;
window.auditLinks = [];
window.auditCopied = [];
const RealTerminal = window.Terminal;
window.Terminal = class extends RealTerminal {
  constructor(options) { super(options); window.auditTerm = this; }
};
window.WebSocket = class {
  static OPEN = 1;
  constructor() { this.readyState = 1; window.auditSocket = this; }
  send(data) {
    const message = JSON.parse(data); window.auditMessages.push(message);
    if (message.type === 'input') window.auditInput.push(message.data);
  }
};
window.qt = {webChannelTransport: {}};
window.QWebChannel = class {
  constructor(_, complete) {
    complete({objects: {terminalBridge: {
      start: done => done(), resize: () => {},
      consumed: count => { window.auditConsumed += count; },
      write: data => { window.auditInput.push(data); },
      openLink: url => { window.auditLinks.push(url); },
      copyText: text => { window.auditCopied.push(text); },
      setPrimarySelection: () => {},
      primarySelection: done => done('primary fixture')
    }}});
  }
};
'''


class TerminalScrollChecks(unittest.TestCase):
    def setUp(self):
        self.profile = QWebEngineProfile(APP)  # Memory-only cookies/cache/storage.
        self.assertTrue(self.profile.isOffTheRecord())
        self.view = QWebEngineView()
        self.page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self.page)
        self.view.resize(800, 260)
        self.addCleanup(self.dispose)

    def dispose(self):
        self.view.close()
        self.view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.profile.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def wait_for(self, predicate, message, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            APP.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        self.fail(message)

    def js(self, code):
        result = []
        self.page.runJavaScript(code, result.append)
        self.wait_for(lambda: bool(result), 'Renderer JavaScript callback did not finish')
        return result[0]

    def state(self):
        return json.loads(self.js(r'''JSON.stringify((() => {
          const term = window.auditTerm, viewport = term.element.querySelector('.xterm-viewport');
          const style = getComputedStyle(viewport), scrollbar = getComputedStyle(viewport, '::-webkit-scrollbar');
          return {y: term.buffer.active.viewportY, base: term.buffer.active.baseY,
            lines: term.buffer.active.length, first: term.buffer.active.getLine(0).translateToString(true),
            width: viewport.offsetWidth - viewport.clientWidth,
            overflow: style.overflowY, scrollbarWidth: style.scrollbarWidth,
            standardScrollbar: CSS.supports('scrollbar-width', 'none'),
            display: scrollbar.display, cssWidth: scrollbar.width,
            height: viewport.scrollHeight, clientHeight: viewport.clientHeight,
            top: viewport.scrollTop, input: window.auditInput.length};
        })())'''))

    def load_renderer(self, variant):
        if variant == 'windows':
            # Read the actual template without importing config/services.
            tree = ast.parse((ROOT / 'src/modules/project_terminal.py').read_text(encoding='utf-8-sig'))
            html = next(ast.literal_eval(node.value) for node in tree.body
                        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                        and target.id == 'HTML_CONTENT_TEMPLATE' for target in node.targets))
            substitutions = {'__SCROLLBACK__': '2000', '__CURSOR_BLINK__': 'false',
                             '__THEME_BG__': '#10151c', '__THEME_FG__': '#e0e6ed',
                             '__THEME_CURSOR__': '#00d2ff', '__THEME_SELECTION__': '#243040',
                             '__THEME_XTERM__': '{"background":"#10151c","foreground":"#e0e6ed"}'}
            for name, value in substitutions.items():
                html = html.replace(name, value)
            html = html.replace('/assets/xterm/', '../assets/xterm/')
            html = html.replace('/assets/editor/terminal_links.js', 'terminal_links.js')
        else:
            html = (ROOT / 'src/editor/terminal.html').read_text(encoding='utf-8')
        # Assets and app code stay real. Replace only the transport and observe
        # the created terminal without exporting it from production code.
        html = html.replace('<script>', '<script>' + TRANSPORT, 1)
        loaded = []
        self.view.loadFinished.connect(loaded.append)
        self.view.setHtml(html, QUrl.fromLocalFile(str(ROOT / 'src/editor/terminal.html')))
        self.view.show()
        self.wait_for(lambda: bool(loaded), 'Offline terminal page did not load')
        self.assertTrue(loaded[0])
        if variant == 'windows':
            self.assertTrue(self.js("auditSocket.onmessage({data:JSON.stringify({type:'create',shell:'audit'})}); true"))
        self.wait_for(lambda: self.js('!!window.auditTerm && !!auditTerm.element'), 'xterm did not open')
        output = ''.join(f'Scroll fixture row {index:03d}\r\n' for index in range(240))
        self.assertTrue(self.js('window.auditWritten=false; auditTerm.write(' + json.dumps(output)
                               + ',()=>{window.auditWritten=true}); true'))
        self.wait_for(lambda: self.js('window.auditWritten'), 'Fixture output did not parse')
        self.wait_for(lambda: self.state()['height'] > self.state()['clientHeight'], 'Fixture must overflow')
        # Prove the fake input transport is live: xterm must still answer PTY
        # device/cursor queries before history gestures produce zero input.
        self.js(r'auditTerm.write("\x1b[c\x1b[6n"); true')
        self.wait_for(lambda: self.js('auditInput.length') >= 2, 'PTY capability replies did not reach transport')
        replies = self.js('auditInput.join("")')
        self.assertRegex(replies, re.compile(r'\x1b\[\?[0-9;]+c'))
        self.assertRegex(replies, re.compile(r'\x1b\[[0-9]+;[0-9]+R'))
        self.js('window.auditInput = []; true')

    def check_scrolling(self, variant):
        self.load_renderer(variant)
        for mode in ('default', 'light', 'solarized_dark'):
            palette = get_palette(mode)
            theme = {'background': palette['BG_DARKEST'], 'foreground': palette['TEXT']}
            for width in (800, 400, 800):
                with self.subTest(renderer=variant, theme=mode, width=width):
                    self.view.resize(width, 260)
                    if variant == 'windows':
                        self.js('applyTheme(' + json.dumps(theme) + '); fitAll(); true')
                    else:
                        self.js('configureTerminal(' + json.dumps(theme) + ',14,2000); true')
                    QTest.qWait(50)
                    self.js('auditTerm.scrollToBottom(); auditTerm.focus(); true')
                    self.wait_for(lambda: self.state()['y'] == self.state()['base'], 'Scroll to bottom failed')
                    before = self.state()
                    self.assertGreater(before['base'], 100)
                    self.assertEqual(before['input'], 0)
                    self.assertIn(before['overflow'], ('auto', 'scroll'))
                    if before['standardScrollbar']:
                        self.assertEqual(before['scrollbarWidth'], 'none')
                    self.assertEqual(before['display'], 'none')
                    self.assertEqual(before['cssWidth'], '0px')
                    self.assertEqual(before['width'], 0, 'Hidden scrollbar must reserve no output width')
                    self.assertGreater(before['height'], before['clientHeight'])
                    target = self.view.focusProxy()
                    self.assertIsNotNone(target)
                    point = QPointF(target.rect().center())
                    wheel = QWheelEvent(point, QPointF(target.mapToGlobal(point.toPoint())),
                                        QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
                    APP.sendEvent(target, wheel)
                    self.wait_for(lambda: self.state()['y'] < before['y'], 'Wheel must still reveal scrollback')
                    wheel_state = self.state()
                    self.assertLess(wheel_state['top'], before['top'])
                    self.js('auditTerm.scrollToBottom(); auditTerm.focus(); true')
                    self.wait_for(lambda: self.state()['y'] == self.state()['base'], 'Scroll reset failed')
                    QTest.keyClick(target, Qt.Key.Key_PageUp, Qt.KeyboardModifier.ShiftModifier)
                    self.wait_for(lambda: self.state()['y'] < before['y'], 'Shift+PageUp must still reveal scrollback')
                    after = self.state()
                    self.assertEqual(after['lines'], before['lines'])
                    self.assertEqual(after['first'], before['first'])
                    self.assertEqual(after['input'], 0, 'History scrolling must not write to the PTY')
                    self.js('auditTerm.scrollToBottom(); true')
                    if RENDER_DIR is not None and width == 400:
                        QTest.qWait(50)  # Let xterm paint its latest frame.
                        self.assertTrue(self.view.grab().save(str(RENDER_DIR / f'terminal-scroll-{variant}-{mode}.png')))

    def check_links_and_keys(self, variant):
        self.load_renderer(variant)
        self.view.resize(640, 260)
        if variant == 'windows':
            self.js('fitAll(); true')
        else:
            self.js('fitTerminal(); true')
        QTest.qWait(80)
        cols = int(self.js('auditTerm.cols'))
        url = 'https://example.com/' + 'docs/' * ((cols // 5) + 2) + 'end_of_path?x=1'
        self.assertGreater(len(url), cols, 'The fixture URL must wrap')
        text = '\r\nBuild log (see ' + url + '). Then www.example.org/help, ok\r\n'
        self.js('window.auditWritten=false; auditTerm.write(' + json.dumps(text)
                + ',()=>{window.auditWritten=true}); auditTerm.scrollToBottom(); true')
        self.wait_for(lambda: self.js('window.auditWritten'), 'Link fixture output did not parse')
        row = int(self.js("(() => { const b = auditTerm.buffer.active;"
                      " for (let y = b.length - 1; y >= 0; y--) {"
                      "  if (b.getLine(y).translateToString(true).includes('Build log')) return y + 1; }"
                      " return 0; })()"))
        self.assertGreater(row, 0)
        links = json.loads(self.js('JSON.stringify(mcuTerminalLinks.linksOnRow(' + str(row) + '))'))
        self.assertEqual([link['url'] for link in links], [url])
        self.assertEqual(links[0]['range']['start']['y'], row)
        self.assertGreater(links[0]['range']['end']['y'], row, 'A wrapped URL must keep its continuation')
        # The continuation row reports the same complete link, without ").".
        continuation = json.loads(self.js('JSON.stringify(mcuTerminalLinks.linksOnRow(' + str(row + 1) + '))'))
        self.assertIn(url, [link['url'] for link in continuation])
        last = json.loads(self.js('JSON.stringify(mcuTerminalLinks.linksOnRow('
                                  + str(links[0]['range']['end']['y']) + '))'))
        self.assertIn('https://www.example.org/help', [link['url'] for link in last])
        self.assertEqual(self.js("mcuTerminalLinks.trimLink('https://a.example/x_(y)).')"), 'https://a.example/x_(y)')
        # A real click on the link opens it through the host, never in the page.
        point = json.loads(self.js(
            "JSON.stringify((() => {"
            " const cell = auditTerm._core._renderService.dimensions.css.cell;"
            " const screen = auditTerm.element.querySelector('.xterm-screen').getBoundingClientRect();"
            " const start = " + json.dumps(links[0]['range']['start']) + ";"
            " const y = start.y - 1 - auditTerm.buffer.active.viewportY;"
            " return {x: screen.left + (start.x + 12) * cell.width, y: screen.top + (y + 0.5) * cell.height};"
            "})())"))
        target = self.view.focusProxy()
        position = QPoint(round(point['x']), round(point['y']))
        QTest.mouseMove(target, position)
        QTest.qWait(120)
        QTest.mouseMove(target, position + QPoint(1, 0))
        QTest.qWait(120)
        QTest.mouseClick(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, position + QPoint(1, 0))
        if variant == 'windows':
            self.wait_for(lambda: self.js("auditMessages.filter(m => m.type === 'open_link').length") >= 1,
                          'A clicked link did not reach the terminal engine')
            self.assertEqual(self.js("auditMessages.filter(m => m.type === 'open_link')[0].url"), url)
        else:
            self.wait_for(lambda: self.js('auditLinks.length') >= 1, 'A clicked link did not reach the bridge')
            self.assertEqual(self.js('auditLinks[0]'), url)
        # Keyboard: Ctrl+Shift+C copies, Ctrl+C with a selection copies, Ctrl+C
        # alone still interrupts. Plain Ctrl+V reaches vim/nano on Linux only.
        self.js("auditInput = []; auditTerm.focus(); auditTerm.selectAll(); true")
        QTest.keyClick(target, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        if variant != 'windows':
            self.wait_for(lambda: self.js('auditCopied.length') >= 1, 'Ctrl+Shift+C did not copy the selection')
            self.assertIn('Build log', self.js('auditCopied[auditCopied.length - 1]'))
        self.assertTrue(self.js('auditTerm.hasSelection()'), 'Ctrl+Shift+C must keep the selection')
        QTest.keyClick(target, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.wait_for(lambda: not self.js('auditTerm.hasSelection()'), 'Ctrl+C with a selection did not copy it')
        QTest.qWait(50)
        self.assertNotIn('\x03', self.js('auditInput.join("")'), 'Copying also interrupted the program')
        QTest.keyClick(target, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.wait_for(lambda: '\x03' in self.js('auditInput.join("")'), 'Ctrl+C no longer interrupts')
        self.js('auditInput = []; true')
        QTest.keyClick(target, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        QTest.qWait(80)
        if variant == 'windows':
            self.assertNotIn('\x16', self.js('auditInput.join("")'), 'Ctrl+V must paste on Windows')
        else:
            self.assertIn('\x16', self.js('auditInput.join("")'), 'Ctrl+V must stay ^V for vim and nano')

    def test_ubuntu_links_clicks_and_clipboard_keys(self):
        self.check_links_and_keys('ubuntu')

    def test_windows_links_clicks_and_clipboard_keys(self):
        self.check_links_and_keys('windows')

    def test_windows_output_scrollbar_hidden_history_still_scrolls(self):
        self.check_scrolling('windows')

    def test_ubuntu_output_scrollbar_hidden_history_still_scrolls(self):
        self.check_scrolling('ubuntu')

    def check_filled_geometry(self, variant):
        self.load_renderer(variant)
        self.js('window.auditOriginalTerminal=auditTerm; true')
        for zoom, width, height in ((1.75,1000,520),(.75,1200,700),(1,640,360),
                                    (1.25,850,480),(2,900,500),(1,1200,700)):
            with self.subTest(renderer=variant,zoom=zoom,width=width,height=height):
                self.page.setZoomFactor(zoom)
                self.view.resize(width,height)
                self.wait_for(lambda: abs(self.js('innerWidth') - self.view.contentsRect().width()/zoom) <= 1,
                              'Renderer viewport did not match actual host bounds')
                self.wait_for(lambda: self.js(r'''(() => {
                  const host=auditTerm.element.parentElement.getBoundingClientRect(),
                    screen=auditTerm.element.querySelector('.xterm-screen').getBoundingClientRect();
                  return screen.right <= host.right+1 && screen.bottom <= host.bottom+1 &&
                    host.right-screen.right <= screen.width/auditTerm.cols+16 &&
                    host.bottom-screen.bottom <= screen.height/auditTerm.rows+16;
                })()'''), 'The running terminal did not finish fitting its resized native viewport')
                geometry = json.loads(self.js(r'''JSON.stringify((() => {
                  const term=auditTerm, host=term.element.parentElement,
                    screen=term.element.querySelector('.xterm-screen');
                  const rect=element=>{const r=element.getBoundingClientRect();
                    return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,height:r.height};};
                  return {viewport:{width:innerWidth,height:innerHeight},host:rect(host),screen:rect(screen),
                    rows:term.rows,cols:term.cols,same:term===auditOriginalTerminal};
                })())'''))
                viewport, host, screen = geometry['viewport'], geometry['host'], geometry['screen']
                self.assertTrue(geometry['same'],'Display changes must retain the running terminal')
                self.assertAlmostEqual(host['width'],viewport['width'],delta=1)
                self.assertAlmostEqual(host['height'],viewport['height'],delta=1)
                # A terminal grid can leave at most one partial character cell
                # plus its existing reading padding at its right/bottom edge.
                self.assertLessEqual(host['right']-screen['right'],screen['width']/geometry['cols']+16)
                self.assertLessEqual(host['bottom']-screen['bottom'],screen['height']/geometry['rows']+16)
                self.assertGreaterEqual(screen['left'],host['left'])
                self.assertGreaterEqual(screen['top'],host['top'])
                self.assertLessEqual(screen['right'],host['right']+1)
                self.assertLessEqual(screen['bottom'],host['bottom']+1)
                self.assertEqual(self.js('auditInput.length'),0,'Display scaling must not type into the PTY')

    def test_windows_terminal_fills_lower_and_fractional_scale_viewports(self):
        self.check_filled_geometry('windows')

    def test_ubuntu_terminal_fills_lower_and_fractional_scale_viewports(self):
        self.check_filled_geometry('ubuntu')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--render-dir', type=Path)
    args, rest = parser.parse_known_args()
    if args.render_dir is not None:
        RENDER_DIR = args.render_dir.resolve()
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    unittest.main(argv=[sys.argv[0]] + rest, verbosity=2)
