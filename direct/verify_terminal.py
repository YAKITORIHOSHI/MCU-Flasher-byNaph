#!/usr/bin/env python3
"""Exercise the real Windows PTY, WebSocket and offline xterm together.

Requires Node.js and the app's Windows dependencies. Uses only temp/audit,
local sockets and version commands; never starts an authenticated AI session.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

PROBE = r'''
const received = [];
let pasteReady = false;
const info = {stdinTTY: !!process.stdin.isTTY, stdoutTTY: !!process.stdout.isTTY,
  columns: process.stdout.columns, rows: process.stdout.rows};
if (!info.stdinTTY || !info.stdoutTTY) process.exit(2);
process.stdin.setRawMode(true);
process.stdin.resume();
const timeout = setTimeout(() => {console.log('MCU_PROBE_TIMEOUT'); process.exit(3);}, 12000);
function finish() {
  clearTimeout(timeout);
  const all = received.join('');
  info.deviceReply = /\x1b\[[?>]?[0-9;]*c/.test(all);
  info.cursorReply = /\x1b\[\d+;\d+R/.test(all);
  info.paste = all.includes('\x1b[200~line_one😀\rline_two\x1b[201~');
  info.interrupt = true;
  process.stdin.setRawMode(false);
  // Windows TTY writes are asynchronous; wait before exiting.
  process.stdout.write('\x1b[?2004l\x1b[?1049l\r\nMCU_PROBE_RESULT:' + JSON.stringify(info) + '\r\n', () => process.exit(0));
}
process.on('SIGINT', finish);
process.stdin.on('data', data => {
  received.push(data.toString('utf8'));
  const all = received.join('');
  if (!pasteReady && /\x1b\[[?>]?[0-9;]*c/.test(all) && /\x1b\[\d+;\d+R/.test(all)) {
    pasteReady = true;
    process.stdout.write('\r\nMCU_QUERY_OK\r\n');
  }
  if (all.includes('\x1b[201~')) process.stdout.write('\r\nMCU_PASTE_OK\r\n');
  if (all.includes('\x03')) {
    finish();
  }
});
process.stdout.write('\x1b[?1049h\x1b[?2004h\x1b[c\x1b[6n');
'''


def main() -> int:
    if sys.platform != "win32":
        print("Windows ConPTY verification skipped on this OS. Use verify_runtime.py for native Linux PTY checks.")
        return 0
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Install Node.js to run the interactive CLI protocol probe.")
    from src.modules import project_terminal as terminal
    if terminal.PtyProcess is None or terminal.websockets is None:
        raise RuntimeError("The Windows terminal requires pywinpty and websockets.")
    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView

    scratch = ROOT / "temp/audit/terminal"
    scratch.mkdir(parents=True, exist_ok=True)
    probe = scratch / "coding-probe.cjs"
    probe.write_text(PROBE, encoding="utf-8")
    app = QApplication(["Terminal protocol verification"])
    view = QWebEngineView()
    view.resize(960, 500)
    server = terminal.ProjectTerminalServer(terminal._find_free_pair(), str(scratch), str(scratch))
    loop = asyncio.new_event_loop()
    task_holder = []
    def serve():
        asyncio.set_event_loop(loop)
        task = loop.create_task(server.start_async())
        task_holder.append(task)
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        finally:
            pending = asyncio.all_tasks(loop)
            for item in pending:
                item.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()

    def wait_until(predicate, message, seconds=12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError(message)

    def js(source):
        result = []
        view.page().runJavaScript(source, lambda value: result.append(value))
        wait_until(lambda: bool(result), "JavaScript callback timed out", 3)
        return result[0]

    try:
        wait_until(lambda: server.loop is not None, "Local terminal server did not start")
        view.setUrl(QUrl(f"http://127.0.0.1:{server.port}"))
        view.show()
        wait_until(lambda: bool(server.clients), "Offline terminal did not connect")
        server.control({"action": "new", "shell": "audit", "kind": "pwsh"})
        session = server.sessions["audit"]
        wait_until(lambda: session.ready and session.pty is not None, "PowerShell PTY did not start")
        wait_until(lambda: js("!!terminals.audit && !!terminals.audit.element"), "xterm did not initialize")
        assert js("terminals.audit.options.minimumContrastRatio") >= 4.5, "ANSI terminal text lacks contrast correction"
        command = f"& '{node.replace(chr(39), chr(39)*2)}' '{str(probe).replace(chr(39), chr(39)*2)}'"
        js(f"terminals.audit.paste({json.dumps(command)}); true")
        session.pty.write("\r")
        wait_until(lambda: "MCU_QUERY_OK" in session.history_text(), "CLI did not receive terminal capability/cursor replies")
        # Dispatch the same browser paste event used by a real clipboard paste.
        js("(() => {const data = new DataTransfer(); data.setData('text', 'line_one😀\\nline_two'); document.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true})); return true;})()")
        wait_until(lambda: "MCU_PASTE_OK" in session.history_text(), "Bracketed multiline paste did not reach the CLI")
        js("(() => {const input = terminals.audit.element.querySelector('textarea'); input.dispatchEvent(new KeyboardEvent('keydown', {key: 'c', code: 'KeyC', ctrlKey: true, keyCode: 67, which: 67, bubbles: true, cancelable: true})); return true;})()")
        wait_until(lambda: "MCU_PROBE_RESULT:" in session.history_text(), "Ctrl+C did not reach the CLI")
        match = re.search(r"MCU_PROBE_RESULT:(\{[^\r\n]+\})", session.history_text())
        assert match, "CLI report missing"
        report = json.loads(match.group(1))
        for name in ("stdinTTY", "stdoutTTY", "deviceReply", "cursorReply", "paste", "interrupt"):
            assert report[name], f"CLI protocol check failed: {name}"
        dimensions = json.loads(js("JSON.stringify({cols: terminals.audit.cols, rows: terminals.audit.rows})"))
        assert report["columns"] == dimensions["cols"] and report["rows"] == dimensions["rows"], report
        print("ConPTY + offline xterm: TTY, capability replies, cursor reports, multiline Unicode paste, Ctrl+C, alternate screen and dimensions OK")
        # Only version commands; do not authenticate or run model requests.
        env = terminal._build_terminal_env(str(scratch))
        ansi = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x1b\x07]*(?:\x1b\\|\x07))")
        def visible_history():
            return ansi.sub("", session.history_text())
        for name in ("codex", "claude", "opencode"):
            executable = shutil.which(name + ".cmd", path=env["PATH"]) or shutil.which(name + ".exe", path=env["PATH"])
            if not executable:
                print(f"{name}: not installed; version smoke skipped")
                continue
            with session.lock:
                session.history.clear()
                session.history_chars = 0
            escaped = executable.replace("'", "''")
            marker = f"MCU_{name.upper()}_VERSION_EXIT_"
            session.pty.write(f"& '{escaped}' --version; Write-Output ('{marker}' + $LASTEXITCODE)\r")
            wait_until(lambda: re.search(r"\r?\n" + marker + r"\d+", visible_history()) is not None, f"{name} version command did not finish", 20)
            history = visible_history()
            assert re.search(r"\r?\n" + marker + r"0(?:\r?\n|$)", history), f"{name} failed in the project terminal"
            print(f"{name}: --version succeeded through the project PTY")
        wait_until(lambda: all(sum(values.values()) == 0 for values in server._pending_output.values()), "Renderer did not acknowledge terminal output")
        # Acknowledge parsing and allow the final frame to paint before capture.
        app.processEvents()
        time.sleep(0.05)
        app.processEvents()
        assert view.grab().save(str(scratch / "terminal-protocol.png"))
        burst = scratch / "burst-output.cjs"
        burst.write_text("process.stdout.write(('MCU_BURST_DATA ' + 'x'.repeat(100) + '\\n').repeat(10000), () => process.stdout.write('MCU_BURST_COMPLETE\\n', () => process.exit(0)));", encoding="utf-8")
        escaped = str(burst).replace("'", "''")
        escaped_node = node.replace("'", "''")
        session.pty.write(f"& '{escaped_node}' '{escaped}'\r")
        peak_pending = [0]
        def burst_finished():
            for counts in list(server._pending_output.values()):
                peak_pending[0] = max(peak_pending[0], sum(counts.values()))
            return "MCU_BURST_COMPLETE" in session.history_text()
        wait_until(burst_finished, "Verbose CLI output did not drain", 20)
        assert session.history_chars <= session.history_limit
        assert peak_pending[0] <= server._output_limit + 8192, peak_pending[0]
        wait_until(lambda: all(sum(values.values()) == 0 for values in server._pending_output.values()), "Verbose output was not acknowledged")
        report["burstPeakPendingChars"] = peak_pending[0]
        report["historyLimitChars"] = session.history_limit
        print("Verbose CLI output (>1 MB): bounded pending output and history, renderer consumed all output OK")
        (scratch / "protocol-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 0
    finally:
        for sid, session in list(server.sessions.items()):
            (scratch / f"{sid}-history.log").write_text(session.history_text(), encoding="utf-8")
        view.close()
        view.deleteLater()
        app.processEvents()
        server.stop()
        if task_holder and not loop.is_closed():
            loop.call_soon_threadsafe(task_holder[0].cancel)
        worker.join(5)
        assert not worker.is_alive(), "Local terminal server did not stop"
        assert server.httpd is None or server.httpd.socket.fileno() == -1, "Terminal HTTP listener leaked after shutdown"
        print("Terminal shutdown: server worker stopped and HTTP listener closed OK")


if __name__ == "__main__":
    raise SystemExit(main())
