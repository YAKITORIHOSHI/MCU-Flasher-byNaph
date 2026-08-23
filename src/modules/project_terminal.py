"""
MCU Flasher Project Terminal

The Project Terminal uses the same native architecture as the OpenCode panel:
pywebview/WebView2 renders xterm.js, while pywinpty owns the real Windows
PowerShell and Command Prompt sessions.  It runs in a small child process so
the Tk event loop and WebView2 message loop never compete with one another.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_SITE_PACKAGES = SCRIPT_DIR / "env" / "Lib" / "site-packages"
XTERM_ASSET_DIR = SCRIPT_DIR / "src" / "assets" / "xterm"
if ENV_SITE_PACKAGES.exists() and str(ENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(ENV_SITE_PACKAGES))

try:
    from winpty import PtyProcess
except Exception:
    PtyProcess = None

try:
    import websockets
except Exception:
    websockets = None

try:
    import webview
except Exception:
    webview = None


WINDOW_TITLE = "MCU Flash GUI - Project Terminal"


# This is deliberately the same local page shape, xterm.js version, theme,
# WebSocket handshake, and fit/resize behavior used by dedicated_AI.py.  The
# only difference is that it keeps one xterm instance per shell so switching
# PowerShell/CMD does not destroy scrollback or the current prompt.
HTML_CONTENT = r"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>MCU Flash GUI - Project Terminal</title>
    <style>
        html, body {
            margin: 0; padding: 0; width: 100%; height: 100%;
            background: #0c0d10; color: #cccccc;
            font-family: Consolas, "Courier New", monospace; font-size: 14px;
            overflow: hidden; box-sizing: border-box;
        }
        #terminal-root, .terminal-host {
            width: 100%; height: 100%; box-sizing: border-box;
            background: #0c0d10;
        }
        .terminal-host { display: none; }
        .terminal-host.active { display: block; }
        .xterm {
            padding: 2px 4px !important;
            width: 100% !important; height: 100% !important;
            box-sizing: border-box;
        }
        .xterm, .xterm-viewport, .xterm-screen {
            background: #0c0d10 !important;
            overflow-y: hidden !important;
            width: 100% !important;
        }
        ::-webkit-scrollbar { display: none !important; width: 0; height: 0; }
    </style>
    <link rel="stylesheet" href="/assets/xterm/xterm.css" />
    <script src="/assets/xterm/xterm.js" onerror="window.xtermErr=true"></script>
    <script src="/assets/xterm/xterm-addon-fit.js" onerror="window.xtermErr=true"></script>
</head>
<body>
    <div id="terminal-root">
        <div id="term-pwsh" class="terminal-host active"></div>
        <div id="term-cmd" class="terminal-host"></div>
    </div>
    <script>
        const shellKinds = ["pwsh", "cmd"];
        const hosts = {
            pwsh: document.getElementById("term-pwsh"),
            cmd: document.getElementById("term-cmd")
        };
        const terminals = {};
        const fitAddons = {};
        let activeShell = "pwsh";
        let socket = null;
        let fallback = false;
        const root = document.getElementById("terminal-root");

        function sanitizeTerminalInput(data) {
            if (!data || typeof data !== "string") return "";
            return data
                .replace(/\x1b\[\?[0-9;]*c/g, "")
                .replace(/\x1b\[>[0-9;]*c/g, "")
                .replace(/\x1b\[\?[0-9;]*\$y/g, "")
                .replace(/\x1b\[[0-9;]*\$y/g, "")
                .replace(/\x1b\]\d+;[^\x1b\x07]*(?:\x1b\\|\x07)?/g, "")
                .replace(/\x1bP>\|[^\x1b\x07]*(?:\x1b\\|\x07)?/g, "")
                .replace(/\x1b\[>[0-9;]*q/g, "");
        }

        function setActiveShell(kind) {
            if (!hosts[kind]) return;
            activeShell = kind;
            shellKinds.forEach(k => hosts[k].classList.toggle("active", k === kind));
            if (fitAddons[kind]) {
                try { fitAddons[kind].fit(); } catch (e) {}
                sendResize();
            }
            if (terminals[kind]) terminals[kind].focus();
        }

        function makeTerminal(kind) {
            if (typeof Terminal === "undefined" || typeof FitAddon === "undefined") {
                return false;
            }
            try {
                const term = new Terminal({
                    cursorBlink: true,
                    cursorStyle: "block",
                    fontSize: 14,
                    fontFamily: 'Consolas, "Courier New", monospace',
                    scrollback: 5000,
                    overviewRulerWidth: 0,
                    theme: {
                        background: "#0c0d10",
                        foreground: "#cccccc",
                        cursor: "#ffffff",
                        selectionBackground: "#264f78"
                    }
                });
                const fit = new FitAddon.FitAddon();
                term.loadAddon(fit);
                term.open(hosts[kind]);
                terminals[kind] = term;
                fitAddons[kind] = fit;
                term.onData(data => {
                    const clean = sanitizeTerminalInput(data);
                    if (clean && socket && socket.readyState === WebSocket.OPEN && activeShell === kind) {
                        socket.send(JSON.stringify({ type: "input", shell: kind, data: clean }));
                    }
                });
                return true;
            } catch (e) {
                console.warn("xterm.js failed to initialize", e);
                return false;
            }
        }

        function enableFallback() {
            // This path is only reached when WebView2 cannot load the same
            // xterm assets as OpenCode. The parent GUI will normally switch to
            // its proven Tk PTY surface before this becomes visible.
            fallback = true;
            root.innerHTML = "<pre style='margin:12px;color:#cccccc'>Project Terminal could not load xterm.js.</pre>";
        }

        function sendResize() {
            const term = terminals[activeShell];
            if (socket && socket.readyState === WebSocket.OPEN && term) {
                socket.send(JSON.stringify({
                    type: "resize", shell: activeShell,
                    cols: term.cols, rows: term.rows
                }));
            }
        }

        function fitAll() {
            shellKinds.forEach(kind => {
                if (fitAddons[kind]) {
                    try { fitAddons[kind].fit(); } catch (e) {}
                }
            });
            sendResize();
        }

        if (window.xtermErr || !makeTerminal("pwsh") || !makeTerminal("cmd")) {
            enableFallback();
        }

        const wsPort = parseInt(window.location.port || "80", 10) + 1;
        const wsUrl = `ws://${window.location.hostname}:${wsPort}/ws`;
        try { socket = new WebSocket(wsUrl); } catch (e) { socket = null; }

        if (socket) {
            socket.onopen = () => {
                fitAll();
                socket.send(JSON.stringify({ type: "client_ready", xterm: !fallback }));
            };
            socket.onmessage = event => {
                let message;
                try { message = JSON.parse(event.data); } catch (e) { message = null; }
                if (!message) {
                    if (terminals[activeShell]) terminals[activeShell].write(event.data);
                    return;
                }
                if (message.type === "activate") {
                    setActiveShell(message.shell);
                    return;
                }
                if (message.type === "reset") {
                    const term = terminals[message.shell];
                    if (term) term.reset();
                    return;
                }
                if (message.type === "snapshot") {
                    const term = terminals[message.shell];
                    if (term) {
                        term.reset();
                        term.write(message.data || "");
                        try { term.scrollToBottom(); } catch (e) {}
                    }
                    return;
                }
                if (message.type === "output") {
                    const term = terminals[message.shell];
                    if (term) term.write(message.data || "");
                    return;
                }
                if (message.type === "status" && message.shell === activeShell && message.data) {
                    // The Tk header owns the status text; this keeps the page
                    // intentionally identical to the OpenCode xterm surface.
                    return;
                }
            };
            socket.onclose = () => {
                const term = terminals[activeShell];
                if (term) term.write("\r\n[Project terminal session closed]\r\n");
            };
        }

        // xterm.js implements the correct console editing semantics (including
        // Backspace, Delete, Ctrl+C, Ctrl+V, arrows, selection, and ANSI redraws).
        // Keep explicit clipboard handling for WebView2 builds where the browser
        // default Ctrl+C/Ctrl+V is intercepted before xterm sees it.
        document.addEventListener("keydown", event => {
            const term = terminals[activeShell];
            if (!term || !event.ctrlKey) return;
            if (event.key.toLowerCase() === "c" && term.hasSelection()) {
                event.preventDefault();
                event.stopImmediatePropagation();
                const value = term.getSelection();
                try {
                    if (term.copySelection) term.copySelection();
                } catch (e) {}
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(value).catch(() => {});
                }
                return;
            }
        }, true);
        document.addEventListener("paste", event => {
            const term = terminals[activeShell];
            if (!term || !event.clipboardData) return;
            const value = event.clipboardData.getData("text")
                .replace(/\r\n/g, "\r").replace(/\n/g, "\r");
            if (socket && socket.readyState === WebSocket.OPEN && value) {
                event.preventDefault();
                event.stopImmediatePropagation();
                socket.send(JSON.stringify({ type: "input", shell: activeShell, data: value }));
            }
        }, true);

        window.addEventListener("resize", fitAll);
        if (typeof ResizeObserver !== "undefined") {
            new ResizeObserver(fitAll).observe(root);
        }
        [50, 150, 350, 700, 1200].forEach(ms => setTimeout(fitAll, ms));
    </script>
</body>
</html>
"""


def _native_shell_executable(kind: str) -> str | None:
    if kind == "cmd":
        root = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
        if root:
            path = Path(root) / "System32" / "cmd.exe"
            if path.exists():
                return str(path)
        return os.environ.get("COMSPEC") or "cmd.exe"
    root = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if root:
        path = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if path.exists():
            return str(path)
    return "powershell.exe"


def _shell_cd_command(kind: str, target: str) -> str:
    if kind == "cmd":
        return f'cd /d "{target}"\r\n'
    escaped = str(target).replace("'", "''")
    return f"Set-Location -LiteralPath '{escaped}'\r\n"


RE_TERMINAL_QUERY_RESPONSE = re.compile(
    r"\x1b(?:\[\?[0-9;]*c|\[>[0-9;]*c|\[\?[0-9;]*\$y|\[[0-9;]*\$y|\]\d+;[^\x1b\x07]*(?:\x1b\\|\x07)?|P>\|[^\x1b\x07]*(?:\x1b\\|\x07)?|\[>[0-9;]*q)"
)


def sanitize_terminal_input(data: str) -> str:
    if not data:
        return ""
    return RE_TERMINAL_QUERY_RESPONSE.sub("", str(data))


def _find_free_pair(start_port: int = 8765) -> int:
    for port in range(start_port, start_port + 200, 2):
        try:
            with socket.socket() as first, socket.socket() as second:
                first.bind(("127.0.0.1", port))
                second.bind(("127.0.0.1", port + 1))
            return port
        except OSError:
            continue
    raise OSError("No free local terminal ports were available")


class ShellSession:
    def __init__(self, server: "ProjectTerminalServer", kind: str):
        self.server = server
        self.kind = kind
        self.target = ""
        self.pty = None
        self.thread = None
        self.running = False
        self.ready = False
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.generation = 0
        self.history = deque(maxlen=3000)

    def append_history(self, data: str) -> None:
        with self.lock:
            self.history.append(str(data or ""))

    def history_text(self) -> str:
        with self.lock:
            # Keep switching lightweight and below the WebSocket frame limit.
            # xterm restores the selected screen from this bounded snapshot,
            # then continues receiving live PTY output normally.
            return "".join(self.history)[-500_000:]


class ProjectTerminalServer:
    def __init__(self, port: int, target_dir: str, initial_cwd: str, port_file: str | None = None):
        self.port = int(port)
        self.target_dir = os.path.abspath(target_dir)
        self.initial_cwd = os.path.abspath(initial_cwd) if initial_cwd else os.getcwd()
        if not os.path.isdir(self.initial_cwd):
            self.initial_cwd = os.path.expanduser("~")
        self.port_file = Path(port_file) if port_file else None
        self.loop = None
        self.running = True
        self.httpd = None
        self.http_error = None
        self.http_ready = threading.Event()
        self.sessions = {kind: ShellSession(self, kind) for kind in ("pwsh", "cmd")}
        self.active_shell = "pwsh"
        self.clients = set()
        self.clients_lock = threading.RLock()

    def _write_port_file(self, xterm: bool | None = None, ready: bool | None = None) -> None:
        if not self.port_file:
            return
        try:
            self.port_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {"port": self.port, "pid": os.getpid()}
            if xterm is not None:
                payload["xterm"] = bool(xterm)
            if ready is not None:
                payload["ready"] = bool(ready)
            temporary = self.port_file.with_suffix(self.port_file.suffix + ".tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(self.port_file)
        except Exception:
            pass

    async def _send(self, websocket, payload: dict) -> None:
        try:
            await websocket.send(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    def broadcast(self, payload: dict) -> None:
        if not self.loop or not self.loop.is_running():
            return
        with self.clients_lock:
            clients = list(self.clients)
        for websocket in clients:
            try:
                asyncio.run_coroutine_threadsafe(self._send(websocket, payload), self.loop)
            except Exception:
                pass

    def _start_session(self, kind: str) -> bool:
        session = self.sessions.get(kind)
        if not session:
            return False
        with session.lock:
            if session.running:
                return True
            session.generation += 1
            generation = session.generation
            session.running = True
            session.ready = False
            session.target = self.target_dir
            session.stop_event.clear()
            session.history.clear()
        session.thread = threading.Thread(
            target=self._shell_worker,
            args=(session, generation),
            name=f"MCUProjectTerminal-{kind}",
            daemon=True,
        )
        session.thread.start()
        self.broadcast({"type": "status", "shell": kind, "running": True, "ready": False})
        return True

    def _stop_session(self, kind: str) -> None:
        session = self.sessions.get(kind)
        if not session:
            return
        with session.lock:
            session.generation += 1
            session.running = False
            session.stop_event.set()
            pty = session.pty
            session.pty = None
        if pty:
            try:
                pty.close(force=True)
            except Exception:
                try:
                    pty.close()
                except Exception:
                    pass

    def _shell_worker(self, session: ShellSession, generation: int) -> None:
        kind = session.kind

        def is_current() -> bool:
            with session.lock:
                return (
                    session.generation == generation
                    and session.running
                    and not session.stop_event.is_set()
                )

        executable = _native_shell_executable(kind)
        if not executable or PtyProcess is None:
            message = "\r\n[MCU Flasher] Native PTY support is unavailable.\r\n"
            session.append_history(message)
            self.broadcast({"type": "output", "shell": kind, "data": message})
            with session.lock:
                session.running = False
            self.broadcast({"type": "status", "shell": kind, "running": False, "ready": False})
            return

        argv = [executable, "-NoProfile", "-NoExit"] if kind == "pwsh" else [executable, "/D"]
        pty = None
        ready_candidate = None
        try:
            # Start in the project folder immediately. Waiting for a first
            # prompt, then issuing cd + cls added a complete extra command
            # round-trip to each cold shell start.
            pty = PtyProcess.spawn(argv, cwd=self.target_dir, dimensions=(30, 120))
            with session.lock:
                session.pty = pty
            cd_pending = False
            probe = ""
            deadline = time.monotonic() + 30.0
            while is_current():
                try:
                    data = pty.read(4096)
                except Exception:
                    break
                if data:
                    if not is_current():
                        break
                    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
                    session.append_history(text)
                    self.broadcast({"type": "output", "shell": kind, "data": text})
                    probe = (probe + text)[-8000:]
                    if cd_pending:
                        prompt_seen = bool(re_prompt(kind, probe))
                        if prompt_seen or time.monotonic() >= deadline:
                            try:
                                pty.write(_shell_cd_command(kind, self.target_dir) + "cls\r\n")
                            except Exception:
                                pass
                            cd_pending = False
                            ready_candidate = time.monotonic()
                    elif not session.ready and (
                        re_prompt(kind, probe)
                        or (ready_candidate and time.monotonic() - ready_candidate >= 0.75)
                    ):
                        with session.lock:
                            session.ready = True
                        if self.active_shell == kind:
                            self._write_port_file(ready=True)
                        self.broadcast({"type": "status", "shell": kind, "running": True, "ready": True})
                elif cd_pending and time.monotonic() >= deadline:
                    try:
                        pty.write(_shell_cd_command(kind, self.target_dir) + "cls\r\n")
                    except Exception:
                        pass
                    cd_pending = False
        except Exception as exc:
            message = f"\r\n[MCU Flasher] Could not start {kind}: {exc}\r\n"
            session.append_history(message)
            self.broadcast({"type": "output", "shell": kind, "data": message})
        finally:
            current = False
            with session.lock:
                current = session.generation == generation
                if current:
                    session.running = False
                    session.pty = None
            try:
                if pty:
                    pty.close(force=True)
            except Exception:
                pass
            if current:
                self.broadcast({"type": "status", "shell": kind, "running": False, "ready": False})

    def control(self, message: dict) -> dict:
        action = str(message.get("action", "")).lower()
        kind = str(message.get("shell", self.active_shell)).lower()
        if kind not in self.sessions:
            return {"success": False, "error": "Unknown shell"}
        if action == "select":
            self.active_shell = kind
            self._start_session(kind)
            with self.sessions[kind].lock:
                already_ready = self.sessions[kind].ready
            self._write_port_file(ready=already_ready)
            self.broadcast({"type": "activate", "shell": kind})
            return {"success": True, "shell": kind}
        if action == "restart":
            self.active_shell = kind
            self._write_port_file(ready=False)
            self.broadcast({"type": "reset", "shell": kind})
            self._stop_session(kind)
            self._start_session(kind)
            self.broadcast({"type": "activate", "shell": kind})
            return {"success": True, "shell": kind}
        return {"success": False, "error": "Unknown action"}

    async def websocket_handler(self, websocket):
        with self.clients_lock:
            self.clients.add(websocket)
        try:
            await self._send(websocket, {"type": "activate", "shell": self.active_shell})
            for kind, session in self.sessions.items():
                history = session.history_text()
                if history:
                    await self._send(websocket, {"type": "output", "shell": kind, "data": history})
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except Exception:
                    continue
                message_type = message.get("type")
                if message_type == "client_ready":
                    if message.get("xterm") is False:
                        self._write_port_file(xterm=False)
                    else:
                        with self.sessions[self.active_shell].lock:
                            already_ready = self.sessions[self.active_shell].ready
                        self._write_port_file(xterm=True, ready=already_ready)
                    # Warm both terminal choices while the native terminal is
                    # already being created. This prevents shell switching
                    # from being the event that finally starts the PTYs.
                    for kind in ("pwsh", "cmd"):
                        self._start_session(kind)
                    continue
                if message_type == "input":
                    kind = str(message.get("shell", self.active_shell))
                    session = self.sessions.get(kind)
                    if not session:
                        continue
                    clean_data = sanitize_terminal_input(message.get("data", ""))
                    if not clean_data:
                        continue
                    with session.lock:
                        pty = session.pty if session.running else None
                    if pty:
                        try:
                            pty.write(clean_data)
                        except Exception:
                            pass
                    continue
                if message_type == "resize":
                    try:
                        cols = max(20, int(message.get("cols", 120)))
                        rows = max(5, int(message.get("rows", 30)))
                    except Exception:
                        cols, rows = 120, 30
                    for session in self.sessions.values():
                        with session.lock:
                            pty = session.pty if session.running else None
                        if pty:
                            try:
                                pty.setwinsize(rows, cols)
                            except Exception:
                                pass
        except Exception:
            pass
        finally:
            with self.clients_lock:
                self.clients.discard(websocket)

    def run_http_server(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _write_json(self, payload: dict, status: int = 200):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path in ("/", "/index.html"):
                    body = HTML_CONTENT.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                assets = {
                    "/assets/xterm/xterm.css": (XTERM_ASSET_DIR / "xterm.css", "text/css; charset=utf-8"),
                    "/assets/xterm/xterm.js": (XTERM_ASSET_DIR / "xterm.js", "application/javascript; charset=utf-8"),
                    "/assets/xterm/xterm-addon-fit.js": (XTERM_ASSET_DIR / "xterm-addon-fit.js", "application/javascript; charset=utf-8"),
                }
                asset = assets.get(path)
                if asset and asset[0].is_file():
                    body = asset[0].read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", asset[1])
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/status":
                    self._write_json(owner.status())
                    return
                self._write_json({"success": False, "error": "Not found"}, 404)

            def do_POST(self):
                if urlsplit(self.path).path != "/control":
                    self._write_json({"success": False, "error": "Not found"}, 404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    self._write_json(owner.control(payload))
                except Exception as exc:
                    self._write_json({"success": False, "error": str(exc)}, 400)

            def log_message(self, _format, *_args):
                return

        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
            self.http_ready.set()
            self.httpd.serve_forever(poll_interval=0.2)
        except Exception as exc:
            self.http_error = str(exc)
            self.http_ready.set()

    def status(self) -> dict:
        shells = {}
        for kind, session in self.sessions.items():
            with session.lock:
                shells[kind] = {"running": session.running, "ready": session.ready}
        return {"active": self.active_shell, "shells": shells}

    async def start_async(self) -> None:
        threading.Thread(target=self.run_http_server, name="MCUTerminalHTTP", daemon=True).start()
        self.http_ready.wait(timeout=5.0)
        if self.httpd is None:
            raise RuntimeError(self.http_error or "local terminal HTTP server failed")
        if websockets is None:
            raise RuntimeError("websockets is unavailable")
        self.loop = asyncio.get_running_loop()
        async with websockets.serve(self.websocket_handler, "127.0.0.1", self.port + 1, max_size=2**22):
            self._write_port_file()
            await asyncio.Future()

    def stop(self) -> None:
        self.running = False
        for kind in self.sessions:
            self._stop_session(kind)
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass

def re_prompt(kind: str, text: str) -> bool:
    if kind == "cmd":
        return bool(re.search(r"(?m)^[A-Za-z]:[^\r\n]*>\s*$", text))
    return bool(re.search(r"(?m)^PS [^\r\n]*>\s*$", text))


def _hide_console_for_conpty() -> None:
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        if not user32.GetConsoleWindow():
            kernel32.AllocConsole()
        hwnd = user32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def _apply_window_icon() -> None:
    # The main AI child uses the same icon helper. Keep this best-effort so
    # icon setup can never delay or break terminal startup.
    if sys.platform != "win32":
        return
    def _worker():
        try:
            icon_path = SCRIPT_DIR / "src" / "assets" / "mcu_icon.ico"
            if not icon_path.exists():
                icon_path = SCRIPT_DIR / "src" / "mcu_icon.ico"
            if not icon_path.exists():
                return
            user32 = ctypes.windll.user32
            hwnd = 0
            for _ in range(80):
                hwnd = user32.FindWindowW(None, WINDOW_TITLE)
                if hwnd:
                    break
                time.sleep(0.05)
            if not hwnd:
                return
            icon = user32.LoadImageW(None, str(icon_path), 1, 0, 0, 0x10)
            if icon:
                user32.SendMessageW(hwnd, 0x0080, 0, icon)
                user32.SendMessageW(hwnd, 0x0080, 1, icon)
        except Exception:
            pass

    threading.Thread(target=_worker, name="MCUTerminalIcon", daemon=True).start()


def run_standalone_project_terminal(target_directory: str, initial_cwd: str, port_file: str | None = None) -> None:
    _hide_console_for_conpty()
    target = os.path.abspath(target_directory or os.getcwd())
    port = _find_free_pair()
    server = ProjectTerminalServer(port, target, initial_cwd, port_file)

    def run_server():
        try:
            asyncio.run(server.start_async())
        except Exception as exc:
            if server.port_file:
                try:
                    server.port_file.write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
                except Exception:
                    pass

    threading.Thread(target=run_server, name="MCUTerminalServer", daemon=True).start()
    if not server.http_ready.wait(timeout=5.0):
        return
    for _ in range(100):
        if server.port_file and server.port_file.exists():
            break
        time.sleep(0.05)

    if webview is None:
        server.stop()
        return

    _apply_window_icon()
    try:
        webview.create_window(
            title=WINDOW_TITLE,
            url=f"http://127.0.0.1:{port}",
            width=900,
            height=520,
            min_size=(320, 180),
            hidden=True,
            focus=False,
            background_color="#0c0d10",
        )
        webview.start(debug=False)
    finally:
        server.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-terminal", dest="target_directory")
    parser.add_argument("--initial-cwd", default=os.getcwd())
    parser.add_argument("--port-file")
    args = parser.parse_args()
    if args.target_directory:
        run_standalone_project_terminal(args.target_directory, args.initial_cwd, args.port_file)


if __name__ == "__main__":
    main()
