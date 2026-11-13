#!/usr/bin/env python3
"""The assistant's terminal stays covered until its real interface has painted.

Uses real offline xterm and both real assistant pages, fed the recorded shape of
OpenCode 1.18's startup through fixture transports, plus the real panel over a real
PTY with a fake local CLI. No OpenCode process, account, network or hardware. The
Windows server and panel logic runs with mocks here: the page is real Chromium,
but this is not native Windows, ConPTY or WebView2 evidence.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QT_PLATFORM_CLAMPS_WINDOWS = not os.environ["QT_QPA_PLATFORM"].startswith("offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from main.qt.garbage_collection import install_gui_garbage_collector

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
COLLECTOR = install_gui_garbage_collector(APP)
AUDIT = ROOT / "temp/audit/assistant-startup"

# What OpenCode writes before its first frame: capability queries, a kitty graphics
# probe whose APC payload looks like text to a byte filter, then blank frames.
QUERIES = ("\x1b[?2031h\x1b]10;?\x07\x1b]11;?\x07\x1b[>0q\x1b[?25l\x1b[s\x1b[6n\x1bP+q4d73\x1b\\"
           "\x1b[?1016$p\x1b[?2027$p\x1b[?2031$p\x1b[?1004$p\x1b[?2004$p\x1b[?2026$p\x1b[?u"
           "\x1b]99;i=opentui-notifications:p=?;\x1b\\\x1b]1337;Capabilities\x1b\\"
           "\x1b[H\x1b]66;w=1; \x1b\\\x1b[6n\x1b[H\x1b]66;s=2; \x1b\\\x1b[6n\x1b[u")
TAKEOVER = "\x1b[s\x1b[?1049h\x1b[>4;1m\x1b[?2027h\x1b[?2004h\x1b[?1000h\x1b[?1002h\x1b[?1003h\x1b[?1006h\x1b[14t"
PROBE = "\x1b]4;0;?\x07\x1b_Gi=31337,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\\x1b[c\x1b[>4;0m\x1b[>5u"
BANNER = ("Microsoft Windows [Version 10.0.26100.2605]\r\n(c) Microsoft Corporation. All rights reserved.\r\n\r\n"
          'C:\\Sketches\\demo>for /L %i in (0,0,1) do ( "opencode" "C:\\Sketches\\demo" & timeout /t 1 /nobreak >nul )\r\n')


def blank_frame(rows=24, cols=80):
    painted = "".join(f"\x1b[{row};1H\x1b[38;2;255;255;255m\x1b[49m{' ' * cols}" for row in range(1, rows + 1))
    return f"\x1b[?2026h\x1b[?25l{painted}\x1b[?2026l"


def interface_frame():
    lines = ("█▀▀█ █▀▀█ █▀▀█ █▀▀▄ █▀▀▀ █▀▀█ █▀▀█ █▀▀█", "┃  Ask anything…", "┃  Build · OpenCode Zen", "tab agents  ctrl+p commands")
    return "\x1b[?2026h\x1b]0;OpenCode\x07" + "".join(
        f"\x1b[{index * 2 + 6};10H\x1b[38;2;255;255;255m\x1b[48;2;10;10;10m{line}" for index, line in enumerate(lines)) + "\x1b[?2026l"


def split_every(text, size=4095):
    return [text[index:index + size] for index in range(0, len(text), size)]


TRANSPORT = r"""
window.fixtureInput = []; window.fixtureReady = []; window.fixtureConsumed = 0;
const RealTerminal = window.Terminal;
window.Terminal = class extends RealTerminal {
  constructor(options) { super(options); window.fixtureTerminal = this; }
};
window.qt = {webChannelTransport:{}};
window.QWebChannel = class {
  constructor(_, complete) {
    complete({objects:{assistantBridge:{
      start: () => {}, resize: () => {}, write: (...args) => window.fixtureInput.push(args.at(-1)),
      consumed: (...args) => { window.fixtureConsumed += args.at(-1); },
      report_interface_ready: (generation, reason) => window.fixtureReady.push([generation, reason]),
      reset_view: {connect: callback => { window.fixtureReset = callback; }},
      output: {connect: callback => { window.fixtureOutput = callback; }}
    }}});
  }
};
window.fixtureSent = [];
window.WebSocket = class {
  static OPEN = 1;
  constructor() { this.readyState = 1; window.fixtureSocket = this; }
  send(value) { window.fixtureSent.push(JSON.parse(value)); }
};
"""

FAKE_CLOCK = r"""
window.makeClock = () => {
  const clock = {time: 1000, timers: [],
    now: () => clock.time,
    setTimeout: (callback, delay) => { const timer = {callback, at: clock.time + delay}; clock.timers.push(timer); return timer; },
    clearTimeout: timer => { clock.timers = clock.timers.filter(item => item !== timer); },
    advance: span => {
      const end = clock.time + span;
      for (;;) {
        const due = clock.timers.filter(item => item.at <= end).sort((a, b) => a.at - b.at)[0];
        if (!due) break;
        clock.timers = clock.timers.filter(item => item !== due);
        clock.time = due.at;
        due.callback();
      }
      clock.time = end;
    }};
  return clock;
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


def windows_template():
    tree = ast.parse((ROOT / "src/modules/dedicated_AI.py").read_text(encoding="utf-8-sig"))
    return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "HTML_CONTENT_TEMPLATE" for target in node.targets))


class PageFixture(unittest.TestCase):
    def setUp(self):
        self.host = QWidget()
        self.host.resize(800, 480)
        self.view = QWebEngineView(self.host)
        layout = QVBoxLayout(self.host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self.profile = QWebEngineProfile(self.view)
        self.page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self.page)
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

    def load_html(self, html):
        loaded = []
        self.view.loadFinished.connect(loaded.append)
        html = html.replace("<script>", "<script>" + TRANSPORT + FAKE_CLOCK, 1)
        self.view.setHtml(html, QUrl.fromLocalFile(str(ROOT / "src/editor/opencode.html")))
        wait_for(lambda: bool(loaded), "The offline fixture page did not load")
        self.assertTrue(loaded[0])
        wait_for(lambda: self.js("!!window.fixtureTerminal?.element"), "xterm did not open")

    def parsed(self):
        """Wait until xterm has parsed everything written so far."""
        self.js("window.fixtureFlushed = false; fixtureTerminal.write('', () => { window.fixtureFlushed = true; }); true")
        wait_for(lambda: self.js("window.fixtureFlushed"), "xterm did not parse the fixture output")

    def linger(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            APP.processEvents()
            QTest.qWait(10)


class OpenCodePageChecks(PageFixture):
    def setUp(self):
        super().setUp()
        self.load_html((ROOT / "src/editor/opencode.html").read_text(encoding="utf-8"))
        self.js("fixtureReset(1); true")

    def feed(self, *chunks, generation=1):
        for chunk in chunks:
            self.js(f"fixtureOutput({generation}, {json.dumps(chunk)}); true")
        self.parsed()

    def ready(self):
        return self.state("return fixtureReady;")

    def test_real_startup_shape_stays_covered_until_the_interface_paints(self):
        self.feed(QUERIES, TAKEOVER, PROBE, *split_every(blank_frame()))
        self.assertEqual(self.state("return [AssistantReadiness.hasTakenOver(fixtureTerminal),"
                                    "AssistantReadiness.paintedCells(fixtureTerminal)];"), [True, 0],
                         "The APC payload and blank frames must not become screen cells")
        # The old byte gate opened 0.5 seconds after this output went quiet.
        self.linger(1.2)
        self.assertEqual(self.ready(), [])
        self.feed(*split_every(blank_frame()))
        self.linger(.5)
        self.assertEqual(self.ready(), [], "Painting only the background is not an interface")
        self.feed(interface_frame())
        wait_for(lambda: self.ready() == [[1, "interface"]], "The painted interface was not reported")
        self.assertGreater(self.js("fixtureConsumed"), 0)

    def test_sequences_split_across_reads_never_look_like_text(self):
        stream = QUERIES + TAKEOVER + PROBE + blank_frame()
        for size in (1, 2, 3, 5, 7, 11):
            self.js("fixtureTerminal.reset(); true")
            self.feed(*split_every(stream, size))
            self.assertEqual(self.js("AssistantReadiness.paintedCells(fixtureTerminal)"), 0, f"Chunk size {size}")
        self.linger(.5)
        self.assertEqual(self.ready(), [])

    def test_stale_generation_cannot_report_and_a_new_session_rearms(self):
        self.feed(TAKEOVER, interface_frame())
        wait_for(lambda: self.ready() == [[1, "interface"]], "The first session was not reported")
        self.js("fixtureReset(2); true")
        self.assertEqual(self.js("AssistantReadiness.paintedCells(fixtureTerminal)"), 0)
        self.feed(TAKEOVER, interface_frame(), generation=1)
        self.linger(.5)
        self.assertEqual(self.ready(), [[1, "interface"]], "Output from an ended session must be ignored")
        self.feed(TAKEOVER, interface_frame(), generation=2)
        wait_for(lambda: self.ready()[-1] == [2, "interface"], "The fresh session was not reported")

    def decide(self, chunks):
        """Run the decision logic on a second terminal against a controlled clock."""
        self.js("""(() => {
          window.scene = {clock: makeClock(), term: new (Object.getPrototypeOf(fixtureTerminal).constructor)({rows: 24, cols: 80}), reasons: [], busy: 0};
          scene.watch = AssistantReadiness.create(scene.term, reason => scene.reasons.push(reason), scene.clock);
          scene.write = data => { scene.busy += 1; scene.term.write(data, () => { scene.busy -= 1; scene.watch.observe(); }); };
          return true;
        })()""")
        for chunk in chunks:
            self.write_scene(chunk)

    def write_scene(self, chunk):
        self.js(f"scene.write({json.dumps(chunk)}); true")
        wait_for(lambda: self.js("scene.busy") == 0, "The scene terminal did not parse its output")

    def advance(self, span):
        self.js(f"scene.clock.advance({span}); true")
        return self.state("return scene.reasons;")

    def test_plain_text_waits_for_long_silence_so_questions_stay_reachable(self):
        self.decide(["Performing one time database migration, may take a few minutes...\r\n"])
        self.assertEqual(self.advance(7000), [], "Startup text must stay behind the cover")
        self.write_scene("still working\r\n")
        self.assertEqual(self.advance(7000), [], "New output restarts the silence")
        self.assertEqual(self.advance(1500), ["text"], "A question waiting for an answer must become reachable")

    def test_text_then_a_full_screen_interface_takes_the_interface_path(self):
        self.decide(["random startup text\r\n"])
        self.assertEqual(self.advance(1000), [])
        self.write_scene(TAKEOVER)
        self.assertEqual(self.advance(1000), [], "The alternate screen starts blank")
        self.write_scene(interface_frame())
        self.assertEqual(self.advance(400), ["interface"])

    def test_animated_interface_does_not_wait_for_silence_forever(self):
        self.decide([TAKEOVER, interface_frame()])
        reasons = []
        for tick in range(14):
            self.write_scene(f"\x1b[1;1Hspinner{tick}")
            reasons = self.advance(200)
            if reasons:
                break
        self.assertEqual(reasons, ["interface"])
        self.assertGreaterEqual(tick, 4, "It must get a moment to settle before being accepted busy")

    def test_a_short_stray_paint_is_not_an_interface(self):
        self.decide([TAKEOVER, "\x1b[1;1H>"])
        self.assertEqual(self.advance(5000), [])

    def test_conpty_shape_captures_the_mouse_without_an_alternate_screen(self):
        self.decide(["\x1b[?1000h\x1b[?1003h\x1b[?1006h\x1b[2J\x1b[H"])
        self.assertEqual(self.advance(2000), [])
        self.write_scene(interface_frame())
        self.assertEqual(self.state("return [scene.term.buffer.active.type, AssistantReadiness.hasTakenOver(scene.term)];"),
                         ["normal", True])
        self.assertEqual(self.advance(400), ["interface"])

    def test_unicode_paste_and_capability_replies_still_reach_the_session(self):
        self.feed(QUERIES)
        wait_for(lambda: bool(self.state("return fixtureInput;")), "xterm's capability replies must reach the session")
        self.js("fixtureTerminal.paste('Ω😀漢字'); true")
        wait_for(lambda: any("Ω😀漢字" in item for item in self.state("return fixtureInput;")), "Paste must reach the session")


class WindowsPageChecks(PageFixture):
    def load_page(self, html=None):
        html = html if html is not None else windows_template()
        for key, value in (("__THEME_BG__", "#f8f9fa"), ("__THEME_FG__", "#24292f"),
                           ("__THEME_CURSOR__", "#0969da"), ("__THEME_SELECTION__", "#d0d7de")):
            html = html.replace(key, value)
        html = html.replace("/xterm/", "../assets/xterm/").replace("/assistant_readiness.js", "assistant_readiness.js")
        self.load_html(html)
        self.js("fixtureSocket.onopen(); true")

    def feed(self, *chunks):
        for chunk in chunks:
            self.js(f"fixtureSocket.onmessage({{data: {json.dumps(chunk)}}}); true")
        self.parsed()

    def covered(self):
        return self.js("!document.getElementById('startup-cover').hidden")

    def sent(self, kind):
        return [item for item in self.state("return fixtureSent;") if item.get("type") == kind]

    def test_cover_hides_cmd_output_until_the_interface_paints(self):
        self.load_page()
        self.assertTrue(self.covered())
        self.assertEqual(self.sent("client_ready"), [{"type": "client_ready", "readiness": True}])
        # Everything cmd.exe could print, then quiet: still covered.
        self.feed(BANNER)
        self.assertGreater(self.js("AssistantReadiness.paintedCells(fixtureTerminal)"), 0)
        self.linger(1.0)
        self.assertTrue(self.covered())
        self.assertEqual(self.sent("interface_ready"), [])
        # ConPTY's shape: the mouse request is forwarded, the screen clears.
        self.feed("\x1b[?1000h\x1b[?1003h\x1b[?1006h\x1b[2J\x1b[H", blank_frame())
        self.linger(.6)
        self.assertTrue(self.covered())
        self.feed(interface_frame())
        wait_for(lambda: not self.covered(), "The painted interface must lift the page's cover")
        self.assertEqual(self.sent("interface_ready"), [{"type": "interface_ready", "reason": "interface"}])

    def test_exit_covers_the_ending_session_until_the_next_one_paints(self):
        self.load_page()
        self.feed("\x1b[?1049h\x1b[?1003h", interface_frame())
        wait_for(lambda: not self.covered(), "The first session must lift the cover")
        self.feed("\x1b]7337;restart\x07")
        self.assertTrue(self.covered())
        self.assertIn("fresh", self.js("document.getElementById('startup-status').textContent"))
        self.assertEqual(self.js("AssistantReadiness.paintedCells(fixtureTerminal)"), 0)
        self.assertNotIn("7337", self.js("document.body.innerText"), "The control sequence must not be displayed")
        self.feed("\x1b[?1049h\x1b[?1003h", blank_frame())
        self.linger(.6)
        self.assertTrue(self.covered())
        self.feed(interface_frame())
        wait_for(lambda: not self.covered(), "The next session must lift the cover")

    def test_slow_start_offers_the_output_on_request(self):
        html = windows_template()
        self.assertIn("const START_SLOW_MS = 15000;", html)
        self.load_page(html.replace("const START_SLOW_MS = 15000;", "const START_SLOW_MS = 150;"))
        self.assertTrue(self.js("document.getElementById('startup-reveal').hidden"))
        wait_for(lambda: not self.js("document.getElementById('startup-reveal').hidden"), "The escape hatch never appeared")
        self.assertFalse(self.js("document.getElementById('startup-hint').hidden"))
        self.assertTrue(self.covered())
        self.js("document.getElementById('startup-reveal').click(); true")
        self.assertFalse(self.covered())
        self.assertEqual(self.sent("interface_ready"), [{"type": "interface_ready", "reason": "user"}])

    def test_without_the_readiness_script_the_page_falls_back_to_visible(self):
        html = windows_template()
        self.assertIn('<script src="/assistant_readiness.js"></script>', html)
        self.load_page(html.replace('<script src="/assistant_readiness.js"></script>', ""))
        self.assertFalse(self.covered())
        self.assertEqual(self.sent("client_ready"), [{"type": "client_ready", "readiness": False}])

    def test_cover_uses_the_theme_and_fills_the_page(self):
        self.load_page()
        data = self.state("""
          const cover = document.getElementById('startup-cover'), rect = cover.getBoundingClientRect();
          return {background: getComputedStyle(cover).backgroundColor, width: rect.width - innerWidth,
                  height: rect.height - innerHeight, top: document.elementFromPoint(innerWidth / 2, 4).closest('#startup-cover') !== null};
        """)
        self.assertEqual(data["background"], "rgb(248, 249, 250)")
        self.assertEqual((data["width"], data["height"]), (0, 0))
        self.assertTrue(data["top"], "The terminal must not paint above the cover")


class _Socket:
    """A WebSocket whose iteration can observe the marker before the server's cleanup."""

    def __init__(self, marker, *messages):
        self.marker, self.messages = marker, messages
        self.marker_seen = None
        self.sent = []

    def __aiter__(self):
        return self._run()

    async def _run(self):
        for message in self.messages:
            yield json.dumps(message)
        self.marker_seen = self.marker.is_file()

    async def send(self, data):
        self.sent.append(data)


class WindowsServerChecks(unittest.TestCase):
    def setUp(self):
        AUDIT.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=AUDIT)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / ".mcu_flasher_build_cache").mkdir()
        self.marker = self.root / ".mcu_flasher_build_cache/.ai_ready_signal"
        tree = ast.parse((ROOT / "src/modules/dedicated_AI.py").read_text(encoding="utf-8-sig"))
        wanted = [node for node in tree.body if (isinstance(node, ast.ClassDef) and node.name == "TerminalServer")
                  or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_RESTART_SEQUENCE"
                                                           for target in node.targets))]
        self.pty = SimpleNamespace(write=Mock(), close=Mock(), read=Mock(return_value=""))
        self.scope = dict(os=os, sys=sys, json=json, time=time, asyncio=asyncio, Path=Path, threading=threading,
                          PtyProcess=SimpleNamespace(spawn=Mock(return_value=self.pty)))
        exec(compile(ast.Module(body=wanted, type_ignores=[]), "<dedicated-server-fixture>", "exec"), self.scope)
        self.server = self.scope["TerminalServer"](target_dir=str(self.root), command="fixture-opencode")

    def run_socket(self, *messages):
        socket = _Socket(self.marker, *messages)
        started = []

        class Recorder:
            def __init__(self, target=None, **_):
                started.append(target)

            def start(self):
                pass

        with patch.object(threading, "Thread", Recorder), \
             patch("src.modules.ai_prompt_context.PromptInputTracker._publish"), \
             patch("src.modules.ai_prompt_context.assistant_process_active", return_value=False):
            asyncio.run(self.server.ws_handler(socket))
        return socket, started

    def test_supervisor_line_is_not_echoed_between_sessions(self):
        command = self.server._supervised_opencode_command(str(self.root))
        self.assertIn("do @(", command, "cmd.exe would echo the loop body as text before every session")
        self.assertIn("cls", command)

    def test_the_page_decides_and_byte_timing_stays_out_of_the_way(self):
        socket, _started = self.run_socket(dict(type="client_ready", readiness=True),
                                           dict(type="interface_ready", reason="interface"))
        self.assertTrue(socket.marker_seen, "The page's report must publish the marker for the embedding GUI")

    def test_ready_monitor_never_publishes_from_timing_for_a_reporting_page(self):
        socket, started = self.run_socket(dict(type="client_ready", readiness=True))
        self.assertFalse(socket.marker_seen)
        monitor = next(target for target in started if getattr(target, "__name__", "") == "ready_monitor")
        self.server.is_running = True
        # Old rule: 6.5 seconds elapsed plus output seen. Make those facts true.
        original = time.time
        with patch.object(time, "time", lambda: original() + 60), patch.object(time, "sleep"):
            finished = threading.Event()
            worker = threading.Thread(target=lambda: (monitor(), finished.set()), daemon=True)
            worker.start()
            worker.join(2)
        self.assertTrue(finished.is_set(), "The monitor must stop as soon as the page reports readiness")
        self.assertFalse(self.marker.exists())

    def test_exit_uses_the_private_control_only_for_a_reporting_page(self):
        loop_messages = dict(type="input", data="/exit\r")

        def notices(*messages):
            sent = []

            class Loop:
                @staticmethod
                def is_running():
                    return True

            self.server.loop = Loop()
            socket = _Socket(self.marker, *messages)
            original_send = socket.send

            async def recording_send(data):
                sent.append(data)
                await original_send(data)

            socket.send = recording_send

            def run_now(coroutine, _loop):
                asyncio.get_running_loop().create_task(coroutine)

            with patch.object(asyncio, "run_coroutine_threadsafe", run_now), \
                 patch.object(threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None)), \
                 patch("src.modules.ai_prompt_context.PromptInputTracker._publish"), \
                 patch("src.modules.ai_prompt_context.assistant_process_active", return_value=False):
                asyncio.run(self.server.ws_handler(socket))
            self.server.is_running = True
            return sent

        reporting = notices(dict(type="client_ready", readiness=True), loop_messages)
        self.assertEqual(reporting, ["\x1b]7337;restart\x07"])
        fallback = notices(dict(type="client_ready", readiness=False), loop_messages)
        self.assertEqual(len(fallback), 1)
        self.assertIn("Restarting OpenCode", fallback[0], "The text-only fallback renderer still needs words")


class WindowsPanelChecks(unittest.TestCase):
    """Qt logic only: the native HWND calls are absent on this host."""

    def setUp(self):
        from main.qt import ai_panel
        self.module = ai_panel
        AUDIT.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=AUDIT)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / ".mcu_flasher_build_cache").mkdir()
        self.marker = self.root / ".mcu_flasher_build_cache/.ai_ready_signal"
        self.panel = ai_panel.AIPanel(SimpleNamespace(sketch_dir_path=str(self.root)))
        self.addCleanup(self.panel.deleteLater)
        self.panel._proc = SimpleNamespace(poll=lambda: None, terminate=lambda: None, wait=lambda timeout=None: 0)
        self.panel._loader_card.show_progress("Loading your workspace…", 2)
        self.panel._ready_poll_timer.start()
        self.addCleanup(self.panel._ready_poll_timer.stop)

    def test_page_is_shown_only_after_a_long_wait_and_active_only_on_the_real_signal(self):
        panel = self.panel
        for _ in range(self.module._READY_POLLS_BEFORE_PAGE - 1):
            panel._poll_for_ai_readiness()
        self.assertIs(panel._stack.currentWidget(), panel._loader_card)
        panel._poll_for_ai_readiness()
        self.assertIs(panel._stack.currentWidget(), panel._embed_container,
                      "After the long wait the page's own cover takes over")
        self.assertEqual(panel._status_badge.text(), "Starting…", "The page's cover is not the interface")
        self.assertTrue(panel._ready_poll_timer.isActive(), "It must keep listening for the real signal")
        self.assertEqual(panel._ready_poll_timer.interval(), 1000)
        self.marker.write_text("{}", encoding="utf-8")
        panel._poll_for_ai_readiness()
        self.assertEqual(panel._status_badge.text(), "Active")
        self.assertFalse(self.marker.exists(), "The signal must never linger in a project")
        self.assertFalse(panel._ready_poll_timer.isActive())

    def test_signal_reveals_immediately_and_the_slow_listener_stops_eventually(self):
        panel = self.panel
        self.marker.write_text("{}", encoding="utf-8")
        panel._poll_for_ai_readiness()
        self.assertIs(panel._stack.currentWidget(), panel._embed_container)
        self.assertEqual(panel._status_badge.text(), "Active")
        panel._is_ready = False
        panel._ready_poll_attempts = 0
        panel._ready_poll_timer.start()
        for _ in range(self.module._READY_POLLS_BEFORE_PAGE + self.module._READY_POLLS_AFTER_PAGE + 1):
            if not panel._ready_poll_timer.isActive():
                break
            panel._poll_for_ai_readiness()
        self.assertFalse(panel._ready_poll_timer.isActive(), "Polling must be bounded")

    def test_process_exit_shows_a_retry_and_retry_starts_fresh(self):
        panel = self.panel
        panel._proc = SimpleNamespace(poll=lambda: 1, terminate=lambda: None, wait=lambda timeout=None: 0)
        panel.show()
        panel._poll_for_ai_readiness()
        self.assertIs(panel._stack.currentWidget(), panel._loader_card)
        self.assertEqual(panel._status_badge.text(), "Stopped")
        self.assertTrue(panel._loader_card.retry_button.isVisibleTo(panel))
        self.assertFalse(panel._ready_poll_timer.isActive())
        with patch.object(panel, "_start_ai") as start, patch("src.modules.dedicated_AI.close_active_opencode"):
            panel._loader_card.retry_button.click()
        start.assert_called_once()
        panel.hide()

    def test_themes_recolor_the_panel_without_hard_coded_dark_surfaces(self):
        from main.core.log_colors import glass_inks
        from main.core.theme import get_palette
        from main.qt.theme import glass_tokens
        for mode in ("default", "light", "solarized_dark"):
            self.panel.apply_theme(mode)
            palette = get_palette(mode)
            self.assertIn("transparent", self.panel._header.styleSheet())
            # A native child window cannot be translucent, so it keeps the theme's surface.
            self.assertIn(palette["BG_DARKEST"], self.panel._embed_container.styleSheet())
            self.assertIn(glass_tokens(mode)["reading"], self.panel._loader_card.styleSheet())
            self.assertIn(glass_inks(mode)["normal"], self.panel._status_badge.styleSheet())

    def test_hidden_project_switch_does_not_launch_a_windows_assistant(self):
        panel = self.panel
        panel.hide()
        panel._is_active = True
        with patch.object(panel, "_stop_ai") as stop, patch.object(self.module.subprocess, "Popen") as spawn:
            panel.reset_for_project(str(self.root / "next project"))
        stop.assert_called_once()
        spawn.assert_not_called()
        self.assertTrue(panel.isHidden())

    def test_windows_window_lookup_uses_the_owned_child_pid(self):
        panel = self.panel
        panel._proc = SimpleNamespace(pid=9876, poll=lambda: None)
        native = SimpleNamespace(FindWindowW=Mock(return_value=0))
        with patch.object(self.module, "win32gui", Mock()), patch.object(self.module, "win32con", Mock()), \
             patch.object(self.module.ctypes, "windll", SimpleNamespace(user32=native), create=True):
            panel._poll_for_ai_window()
        native.FindWindowW.assert_called_once_with(None, "MCU Flash GUI - OpenCode AI Assistant [9876]")


class LoaderChecks(unittest.TestCase):
    def loader(self, **options):
        from main.qt.assistant_loader import AssistantLoader
        widget = AssistantLoader(**options)
        self.addCleanup(widget.deleteLater)
        self.addCleanup(widget.close)
        return widget

    def pump(self):
        for _ in range(6):
            APP.processEvents()
            time.sleep(.005)

    def test_text_reads_at_least_4_5_to_1_in_every_palette(self):
        from main.core.log_colors import contrast_ratio
        from main.core.theme import get_palette
        for mode in ("default", "light", "solarized_dark"):
            palette = get_palette(mode)
            for ink in ("TEXT", "TEXT_DIM"):
                self.assertGreaterEqual(contrast_ratio(palette[ink], palette["BG_DARKEST"]), 4.5, (mode, ink))

    def test_nothing_animates_and_the_clock_stops_outside_loading(self):
        widget = self.loader()
        widget.resize(400, 300)
        widget.show()
        self.pump()
        self.assertFalse(widget._clock.isActive(), "An idle screen needs no timer")
        widget.show_progress("Loading your workspace…", 2)
        self.assertTrue(widget._clock.isSingleShot(), "Slow-start help needs only one wakeup")
        self.assertTrue(widget._clock.isActive())
        widget.hide()
        self.assertFalse(widget._clock.isActive(), "A hidden cover must not wake the application")
        widget.show()
        self.assertTrue(widget._clock.isActive())
        widget.finish()
        self.assertFalse(widget._clock.isActive())

    def test_status_never_shows_elapsed_time_and_slow_help_still_works(self):
        widget = self.loader()
        widget.resize(400, 360)
        widget.show()
        widget.show_progress("Loading your workspace…", 2)
        self.pump()
        self.assertEqual(widget.status.text(), "Loading your workspace…")
        self.assertFalse(widget.reveal_button.isVisible())
        widget._started_at -= 7
        widget._tick()
        self.assertEqual(widget.status.text(), "Loading your workspace…")
        self.assertFalse(widget.reveal_button.isVisible())
        widget._started_at -= 10
        widget._tick()
        self.pump()
        self.assertTrue(widget.reveal_button.isVisible())
        self.assertTrue(widget._hint.isVisible())
        self.assertEqual(widget.status.text(), "Loading your workspace…")
        self.assertFalse(widget._clock.isActive(), "Visible help needs no repeating timer")
        pressed = []
        widget.reveal_requested.connect(lambda: pressed.append(True))
        widget.reveal_button.click()
        self.assertEqual(pressed, [True])
        widget.show_progress("Starting a fresh OpenCode session…", 1)
        self.assertEqual(widget.status.text(), "Starting a fresh OpenCode session…")
        self.assertTrue(widget._hint.isVisible(), "Slow-start help survives a phase change")
        widget.finish()
        widget.show_progress("Starting a fresh OpenCode session…", 1)
        self.assertEqual(widget.status.text(), "Starting a fresh OpenCode session…", "A new start begins a new clock")
        self.assertFalse(widget._hint.isVisible())

    def test_windows_panel_has_no_reveal_before_there_is_anything_to_show(self):
        widget = self.loader(can_reveal=False)
        widget.resize(400, 360)
        widget.show()
        widget.show_progress("Starting a session…", 1)
        widget._started_at -= 30
        widget._tick()
        self.pump()
        self.assertFalse(widget.reveal_button.isVisible())

    def test_every_size_keeps_the_words_and_long_errors_scroll(self):
        widget = self.loader()
        widget.show()
        available = widget.screen().availableGeometry().size()
        for width, height in ((620, 560), (300, 360), (300, 150), (240, 90), (240, 47), (2560, 1400)):
            # A real desktop keeps a top-level window on its monitor; a CI screen can be small.
            if QT_PLATFORM_CLAMPS_WINDOWS and (width > available.width() or height > available.height()):
                continue
            widget.resize(width, height)
            widget.show_progress("Loading your workspace…", 1)
            self.pump()
            with self.subTest(size=(width, height)):
                self.assertEqual(widget.minimumSizeHint().height(), 0, "The cover must never hold a pane open")
                self.assertEqual(widget.size().toTuple(), (width, height))
                self.assertLessEqual(widget.title.fontMetrics().horizontalAdvance(widget.title.text()), max(widget.title.width(), 200))
                self.assertEqual(widget._mark.isVisibleTo(widget), height >= 190)
                self.assertEqual(widget._trace.isVisibleTo(widget), height >= 110)
                if height >= 90:
                    viewport = widget._scroll.viewport().rect()
                    for label in (widget.title, widget.status):
                        top_left = label.mapTo(widget._scroll.viewport(), label.rect().topLeft())
                        self.assertGreaterEqual(top_left.y(), 0, "Words must not start above the visible area")
                        self.assertLessEqual(top_left.y() + label.height(), max(viewport.height(), widget._scroll.widget().height()))
        widget.resize(300, 200)
        widget.show_error("OpenCode unavailable", "Configuration fixture error with a long diagnostic. " * 40)
        self.pump()
        self.assertGreater(widget._scroll.verticalScrollBar().maximum(), 0, "Long diagnostics must scroll")
        self.assertTrue(widget.retry_button.isVisibleTo(widget))
        self.assertFalse(widget._scroll.horizontalScrollBar().isVisible())

    def test_phase_trace_paints_the_real_step_in_the_theme_ink(self):
        from main.core.theme import get_palette
        from PySide6.QtGui import QColor
        widget = self.loader()
        widget.resize(400, 300)
        widget.show()
        for mode in ("default", "light", "solarized_dark"):
            widget.apply_theme(mode)
            palette = get_palette(mode)
            for phase, lit_pads in ((0, 1), (1, 2), (2, 3)):
                widget.show_progress("Fixture", phase)
                self.pump()
                image = widget._trace.grab().toImage()
                ratio = image.devicePixelRatio()
                pads = [6, 66, 126]
                centers = [image.pixelColor(round(x * ratio), round(8 * ratio)) for x in pads]
                lit = sum(1 for color in centers if color == QColor(palette["CYAN"]))
                self.assertEqual(lit, lit_pads, (mode, phase))
                self.assertEqual(widget._trace.accessibleDescription(), f"Step {phase + 1} of 3")


@unittest.skipIf(sys.platform == "win32", "Windows uses its own embedded assistant process")
class RealPanelOverRealPty(unittest.TestCase):
    """Real PosixAIPanel, real xterm, real PTY; only the CLI is a local fixture."""

    PROGRAM = r'''
import os, sys, time, tty
tty.setraw(0)
def out(text):
    os.write(1, text.encode("utf-8"))
if "--noisy" in sys.argv:
    out("Performing one time database migration, may take a few minutes...\r\n")
    out("sqlite-migration:0\r\n")
    time.sleep(1.2)
out(QUERIES)
out(TAKEOVER)
out(PROBE)
out(BLANK)
time.sleep(float(os.environ["FIXTURE_BLANK_SECONDS"]))
out(BLANK)
out(INTERFACE)
while True:
    if not os.read(0, 4096):
        break
'''

    def setUp(self):
        from main.qt import posix_ai_panel as ai
        self.ai = ai
        AUDIT.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix="pty project ", dir=AUDIT)
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        constants = "".join(f"{name} = {value!r}\n" for name, value in (
            ("QUERIES", QUERIES), ("TAKEOVER", TAKEOVER), ("PROBE", PROBE),
            ("BLANK", blank_frame()), ("INTERFACE", interface_frame())))
        program = self.project / "fixture.py"
        program.write_text(constants + self.PROGRAM, encoding="utf-8")
        self.executable = self.project / "opencode-fixture"

    def run_panel(self, *arguments, blank_seconds=1.6):
        self.executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -I "
                                   + shlex.quote(str(self.project / "fixture.py")) + " "
                                   + " ".join(arguments) + "\n", encoding="utf-8")
        self.executable.chmod(0o700)
        with patch.object(self.ai, "find_opencode_cli", return_value=str(self.executable)), \
             patch.object(self.ai, "prepare_context", return_value=True), \
             patch("main.core.config.get_monitor_font_size", return_value=13), \
             patch("src.modules.ai_prompt_context.PromptInputTracker._publish"), \
             patch("src.modules.ai_prompt_context.assistant_process_active", return_value=False), \
             patch.dict(os.environ, {"FIXTURE_BLANK_SECONDS": str(blank_seconds)}):
            panel = self.ai.PosixAIPanel(SimpleNamespace(sketch_dir_path=str(self.project)))
            panel.resize(760, 520)
            panel.show()
            panel.ensure_started()
            timeline = []
            try:
                deadline = time.monotonic() + 25
                started = time.monotonic()
                while panel._state != "active" and time.monotonic() < deadline:
                    APP.processEvents()
                    if not timeline or timeline[-1][1] != panel._load_sub.text():
                        timeline.append((round(time.monotonic() - started, 2), panel._load_sub.text(),
                                         panel._stack.currentWidget() is panel._loader))
                    time.sleep(.005)
                self.assertEqual(panel._state, "active", timeline)
                self.assertIs(panel._stack.currentWidget(), panel._view)
                result = []
                panel._view.page().runJavaScript(
                    "document.querySelector('.xterm-rows').innerText", result.append)
                wait_for(lambda: bool(result), "The renderer did not answer")
                return timeline, result[0], time.monotonic() - started
            finally:
                panel._stop_ai()
                panel.close()
                panel.deleteLater()
                APP.processEvents()

    def test_quiet_blank_startup_stays_covered_until_the_real_frame(self):
        timeline, screen, elapsed = self.run_panel(blank_seconds=1.6)
        self.assertTrue(all(covered for _at, _status, covered in timeline), timeline)
        self.assertEqual([status for _at, status, _covered in timeline if status].count("Loading your workspace…"), 1)
        self.assertIn("Ask anything", screen, "The cover must lift only with the interface on screen")
        self.assertGreater(elapsed, 1.6, "The blank period must not reveal the terminal")

    def test_text_printed_before_the_interface_is_never_shown(self):
        timeline, screen, elapsed = self.run_panel("--noisy", blank_seconds=.8)
        self.assertTrue(all(covered for _at, _status, covered in timeline), timeline)
        self.assertGreater(elapsed, 2.0)
        self.assertIn("Ask anything", screen)
        self.assertNotIn("migration", screen, "The interface's screen replaces the startup text")


class DiagnosticFilterChecks(unittest.TestCase):
    def test_graphics_probe_payload_is_control_data_not_text(self):
        from main.qt.posix_ai_panel import _ANSI_OUTPUT
        self.assertEqual(_ANSI_OUTPUT.sub("", PROBE), "")
        self.assertEqual(_ANSI_OUTPUT.sub("", "\x1bP+q4d73\x1b\\Error: real text\x1b]0;title\x07 stays"), "Error: real text stays")
        self.assertEqual(_ANSI_OUTPUT.sub("", "\x1b[31mConfiguration\x1b[0m error"), "Configuration error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
