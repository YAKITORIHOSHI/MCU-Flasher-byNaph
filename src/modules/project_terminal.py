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
import sys
import threading
import time
from collections import deque
from typing import Any
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


def _detect_system_theme() -> str:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        )
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return "light" if val == 1 else "default"
    except Exception:
        return "default"


def _resolve_terminal_theme() -> dict:
    theme = "default"
    for cfg in (SCRIPT_DIR / "src" / "gui_config.json", Path.home() / ".mcu_gui_config.json"):
        try:
            if cfg.exists():
                data = json.loads(cfg.read_text(encoding="utf-8"))
                shared = data.get("shared", {})
                if shared.get("theme_follow_system", False):
                    theme = _detect_system_theme()
                    break
                m = shared.get("theme_mode", "default")
                if m:
                    theme = m
                    break
        except Exception:
            pass

    if theme == "light":
        return {
            "bg": "#f8f9fa",
            "fg": "#24292f",
            "cursor": "#0969da",
            "selection": "#d0d7de"
        }
    elif theme in ("solarized_dark", "solarized", "solarize_dark"):
        return {
            "bg": "#002b36",
            "fg": "#ffffff",
            "cursor": "#39c5bb",
            "selection": "#073642"
        }
    return {
        "bg": "#0c0d10",
        "fg": "#cccccc",
        "cursor": "#ffffff",
        "selection": "#264f78"
    }


HTML_CONTENT_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>MCU Flash GUI - Project Terminal</title>
    <style>
        html, body {
            margin: 0; padding: 0; width: 100%; height: 100%;
            background: __THEME_BG__; color: __THEME_FG__;
            font-family: Consolas, "Courier New", monospace; font-size: 14px;
            overflow: hidden; box-sizing: border-box;
        }
        #terminal-root {
            width: 100%; height: 100%; box-sizing: border-box;
            background: __THEME_BG__; position: relative;
        }
        .terminal-host {
            display: none; width: 100%; height: 100%; box-sizing: border-box;
            background: __THEME_BG__; position: absolute; top: 0; left: 0;
        }
        .terminal-host.active { display: block; }
        .xterm {
            padding: 2px 4px !important;
            height: 100% !important;
            box-sizing: border-box;
        }
        .xterm-viewport {
            background: __THEME_BG__ !important;
            overflow-y: auto !important;
        }
        .xterm-screen {
            background: __THEME_BG__ !important;
        }
        ::-webkit-scrollbar { display: none !important; width: 0; height: 0; }
    </style>
    <link rel="stylesheet" href="/assets/xterm/xterm.css" />
    <script src="/assets/xterm/xterm.js" onerror="window.xtermErr=true"></script>
    <script src="/assets/xterm/xterm-addon-fit.js" onerror="window.xtermErr=true"></script>
</head>
<body>
    <div id="terminal-root">
        <div id="empty-state" style="display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; color: __THEME_FG__; opacity: 0.5; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 13px; text-align: center; padding: 20px; box-sizing: border-box; user-select: none;">
            <div style="font-size: 15px; font-weight: 600; margin-bottom: 6px; color: __THEME_FG__;">No Terminal Session Open</div>
            <div>Click <b>[▾]</b> in the top toolbar to open a new PowerShell or Command Prompt terminal.</div>
        </div>
    </div>
    <script>
        const hosts = {};
        const terminals = {};
        const fitAddons = {};
        let activeShell = null;
        let socket = null;
        let fallback = false;
        const root = document.getElementById("terminal-root");
        const emptyState = document.getElementById("empty-state");

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

        function getOrCreateHost(id) {
            let host = hosts[id] || document.getElementById("term-" + id);
            if (!host) {
                host = document.createElement("div");
                host.id = "term-" + id;
                host.className = "terminal-host";
                root.appendChild(host);
                hosts[id] = host;
            }
            return host;
        }

        function setActiveShell(kind) {
            activeShell = kind || null;
            if (!activeShell || !hosts[activeShell]) {
                Object.keys(hosts).forEach(k => {
                    if (hosts[k]) hosts[k].classList.remove("active");
                });
                if (emptyState) emptyState.style.display = "flex";
                return;
            }
            if (emptyState) emptyState.style.display = "none";
            Object.keys(hosts).forEach(k => {
                if (hosts[k]) hosts[k].classList.toggle("active", k === activeShell);
            });
            window.requestAnimationFrame(() => {
                const host = hosts[activeShell];
                if (host && host.offsetWidth > 40 && host.offsetHeight > 40) {
                    if (fitAddons[activeShell]) {
                        try { fitAddons[activeShell].fit(); } catch (e) {}
                    }
                    if (terminals[activeShell]) {
                        try {
                            terminals[activeShell].refresh(0, terminals[activeShell].rows - 1);
                            terminals[activeShell].focus();
                        } catch (e) {}
                    }
                    sendResize(activeShell);
                }
            });
        }

        function makeTerminal(kind) {
            if (!kind) return false;
            if (terminals[kind]) return true;
            if (typeof Terminal === "undefined" || typeof FitAddon === "undefined") {
                return false;
            }
            try {
                const host = getOrCreateHost(kind);
                const term = new Terminal({
                    cursorBlink: true,
                    cursorStyle: "block",
                    fontSize: 14,
                    fontFamily: 'Consolas, "Courier New", monospace',
                    scrollback: 5000,
                    overviewRulerWidth: 0,
                    theme: {
                        background: "__THEME_BG__",
                        foreground: "__THEME_FG__",
                        cursor: "__THEME_CURSOR__",
                        selectionBackground: "__THEME_SELECTION__"
                    }
                });
                const fit = new FitAddon.FitAddon();
                term.loadAddon(fit);
                term.open(host);
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
                console.warn("xterm.js failed to initialize for", kind, e);
                return false;
            }
        }

        function removeTerminal(id) {
            if (terminals[id]) {
                try { terminals[id].dispose(); } catch (e) {}
                delete terminals[id];
            }
            if (fitAddons[id]) {
                delete fitAddons[id];
            }
            const host = hosts[id] || document.getElementById("term-" + id);
            if (host) {
                try { host.remove(); } catch (e) {}
                delete hosts[id];
            }
            if (activeShell === id) {
                setActiveShell(null);
            }
        }

        function enableFallback() {
            fallback = true;
            root.innerHTML = "<pre style='margin:12px;color:__THEME_FG__'>Project Terminal could not load xterm.js.</pre>";
        }

        function sendResize(id) {
            const termId = id || activeShell;
            if (!termId) return;
            const term = terminals[termId];
            if (socket && socket.readyState === WebSocket.OPEN && term) {
                if (term.cols >= 10 && term.rows >= 3) {
                    socket.send(JSON.stringify({
                        type: "resize", shell: termId,
                        cols: term.cols, rows: term.rows
                    }));
                }
            }
        }

        function fitAll() {
            if (!activeShell) return;
            const host = hosts[activeShell];
            if (!host || host.offsetWidth <= 50 || host.offsetHeight <= 50) return;
            if (fitAddons[activeShell]) {
                try { fitAddons[activeShell].fit(); } catch (e) {}
            }
            if (terminals[activeShell]) {
                try { terminals[activeShell].refresh(0, terminals[activeShell].rows - 1); } catch (e) {}
            }
            sendResize(activeShell);
        }

        window.addEventListener("resize", () => {
            window.requestAnimationFrame(fitAll);
        });

        if (window.xtermErr) {
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
                    if (activeShell && terminals[activeShell]) terminals[activeShell].write(event.data);
                    return;
                }
                if (message.type === "create") {
                    makeTerminal(message.shell);
                    setActiveShell(message.shell);
                    return;
                }
                if (message.type === "destroy") {
                    removeTerminal(message.shell);
                    return;
                }
                if (message.type === "activate") {
                    if (message.shell) makeTerminal(message.shell);
                    setActiveShell(message.shell);
                    return;
                }
                if (message.type === "reset") {
                    const term = terminals[message.shell];
                    if (term) {
                        try { term.reset(); } catch (e) {}
                    }
                    return;
                }
                if (message.type === "snapshot") {
                    makeTerminal(message.shell);
                    const term = terminals[message.shell];
                    if (term) {
                        term.reset();
                        term.write(message.data || "");
                        try { term.scrollToBottom(); } catch (e) {}
                    }
                    return;
                }
                if (message.type === "output") {
                    makeTerminal(message.shell);
                    const term = terminals[message.shell];
                    if (term) term.write(message.data || "");
                    return;
                }
            };
            socket.onclose = () => {
                const term = terminals[activeShell];
                if (term) term.write("\r\n[Project terminal session closed]\r\n");
            };
        }

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


def get_terminal_html() -> str:
    t = _resolve_terminal_theme()
    return (
        HTML_CONTENT_TEMPLATE
        .replace("__THEME_BG__", t["bg"])
        .replace("__THEME_FG__", t["fg"])
        .replace("__THEME_CURSOR__", t["cursor"])
        .replace("__THEME_SELECTION__", t["selection"])
    )


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


RE_PWSH_PROMPT = re.compile(r"PS [^>\r\n]*>", re.MULTILINE)
RE_CMD_PROMPT = re.compile(r"[A-Za-z]:\\[^>\r\n]*>", re.MULTILINE)


def re_prompt(kind: str, text: str) -> bool:
    if not text:
        return False
    if kind == "pwsh":
        return bool(RE_PWSH_PROMPT.search(text)) or ("> " in text or text.rstrip().endswith(">"))
    return bool(RE_CMD_PROMPT.search(text)) or ("> " in text or text.rstrip().endswith(">"))


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
    pty: Any | None
    thread: Any | None

    def __init__(self, server: "ProjectTerminalServer", session_id: str, kind: str, title: str = ""):
        self.server = server
        self.session_id = session_id
        self.kind = kind
        self.title = title or kind
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
        self.sessions = {}
        self.active_shell = None
        self.session_counter = 0
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
        sid = session.session_id

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
            self.broadcast({"type": "output", "shell": sid, "data": message})
            with session.lock:
                session.running = False
            self.broadcast({"type": "status", "shell": sid, "running": False, "ready": False})
            return

        argv = [executable, "-NoLogo", "-NoProfile", "-NoExit"] if kind == "pwsh" else [executable, "/D"]
        pty = None
        ready_candidate = None
        try:
            pty = PtyProcess.spawn(argv, cwd=self.target_dir, dimensions=(30, 120))
            with session.lock:
                session.pty = pty
            probe = ""
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
                    self.broadcast({"type": "output", "shell": sid, "data": text})
                    probe = (probe + text)[-8000:]
                    if not session.ready and (
                        re_prompt(kind, probe)
                        or (ready_candidate and time.monotonic() - ready_candidate >= 0.75)
                    ):
                        with session.lock:
                            session.ready = True
                        if self.active_shell == sid:
                            self._write_port_file(ready=True)
                        self.broadcast({"type": "status", "shell": sid, "running": True, "ready": True})
        except Exception as exc:
            message = f"\r\n[MCU Flasher] Could not start {kind}: {exc}\r\n"
            session.append_history(message)
            self.broadcast({"type": "output", "shell": sid, "data": message})
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
                self.broadcast({"type": "status", "shell": sid, "running": False, "ready": False})

    def control(self, message: dict) -> dict:
        action = str(message.get("action", "")).lower()
        shell_id = str(message.get("shell", message.get("session_id", self.active_shell)))
        kind = str(message.get("kind", "pwsh")).lower()
        title = str(message.get("title", ""))

        if action == "new":
            self.session_counter += 1
            new_id = shell_id if shell_id and shell_id not in self.sessions else f"{kind}_{self.session_counter}"
            new_title = title or kind
            session = ShellSession(self, new_id, kind, new_title)
            self.sessions[new_id] = session
            self.active_shell = new_id
            self.broadcast({"type": "create", "shell": new_id, "kind": kind})
            self._start_session(new_id)
            self.broadcast({"type": "activate", "shell": new_id})
            return {"success": True, "shell": new_id, "title": new_title}

        if action == "kill":
            if shell_id in self.sessions:
                self._stop_session(shell_id)
                del self.sessions[shell_id]
                self.broadcast({"type": "destroy", "shell": shell_id})
                if self.active_shell == shell_id:
                    if self.sessions:
                        next_id = next(iter(self.sessions.keys()))
                        self.active_shell = next_id
                        self.broadcast({"type": "activate", "shell": next_id})
                    else:
                        self.active_shell = None
                        self.broadcast({"type": "activate", "shell": None})
                return {"success": True, "active": self.active_shell}
            return {"success": False, "error": "Session not found"}

        if action == "clear":
            if shell_id in self.sessions:
                session = self.sessions[shell_id]
                with session.lock:
                    pty = session.pty if session.running else None
                if pty:
                    try:
                        pty.write("Clear-Host\r\n" if session.kind == "pwsh" else "cls\r\n")
                    except Exception:
                        pass
                self.broadcast({"type": "reset", "shell": shell_id})
                return {"success": True}
            return {"success": False, "error": "Session not found"}

        if action == "select":
            if shell_id in self.sessions:
                self.active_shell = shell_id
                self._start_session(shell_id)
                with self.sessions[shell_id].lock:
                    already_ready = self.sessions[shell_id].ready
                self._write_port_file(ready=already_ready)
                self.broadcast({"type": "activate", "shell": shell_id})
                return {"success": True, "shell": shell_id}
            if not shell_id or shell_id == "None":
                self.active_shell = None
                self.broadcast({"type": "activate", "shell": None})
                return {"success": True, "shell": None}
            return {"success": False, "error": "Unknown shell"}

        if action == "restart":
            if shell_id in self.sessions:
                self.active_shell = shell_id
                self._write_port_file(ready=False)
                self.broadcast({"type": "reset", "shell": shell_id})
                self._stop_session(shell_id)
                self._start_session(shell_id)
                self.broadcast({"type": "activate", "shell": shell_id})
                return {"success": True, "shell": shell_id}
            return {"success": False, "error": "Unknown shell"}

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
                        already_ready = False
                        if self.active_shell and self.active_shell in self.sessions:
                            with self.sessions[self.active_shell].lock:
                                already_ready = self.sessions[self.active_shell].ready
                        self._write_port_file(xterm=True, ready=already_ready if self.active_shell else True)
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
                    body = get_terminal_html().encode("utf-8")
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

            def log_message(self, format: str, *args) -> None:
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
