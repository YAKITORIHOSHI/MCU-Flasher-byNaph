"""
MCU Flash GUI — Dedicated OpenCode AI Assistant Controller (pywebview + xterm.js + pywinpty Engine)

Renders OpenCode AI inside a native desktop Python window using pywebview (Microsoft Edge WebView2)
and xterm.js connected to a ConPTY (pywinpty) backend. Identical to Monaco Editor architecture.
"""

import os
import sys
import json
import time
import socket
import asyncio
import ctypes
from ctypes import wintypes
import threading
import subprocess
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
import tkinter as tk
from tkinter import ttk, messagebox

# Set AppUserModelID so taskbar groups windows under custom app icon
if sys.platform == "win32":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("naph.mcuflasher.gui.v3")
    except Exception:
        pass


def _configure_windows_dpi_awareness():
    """Enable DPI awareness before any window is created.

    Must mirror the main GUI's awareness level: the main app is
    per-monitor-DPI aware, so a virtualized AI subprocess would disagree
    with it on pixel coordinates after the window is reparented into the
    Tk frame (causing a dead strip at the bottom of the embedded view).
    """
    if sys.platform != "win32":
        return
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_configure_windows_dpi_awareness()

# Ensure workspace 'env' site-packages is on sys.path
SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent
AI_STARTUP_PATCH_VERSION = "v22-readiness-no-fallback"
ENV_DIR = SCRIPT_DIR / "env"
ENV_SITE_PACKAGES = ENV_DIR / "Lib" / "site-packages"
if ENV_SITE_PACKAGES.exists() and str(ENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(ENV_SITE_PACKAGES))

try:
    # pyrefly: ignore [missing-import]
    from winpty import PtyProcess
except ImportError:
    PtyProcess = None

try:
    # pyrefly: ignore [missing-import]
    import websockets
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets"])
        # pyrefly: ignore [missing-import]
        import websockets
    except Exception:
        websockets = None

try:
    # pyrefly: ignore [missing-import]
    import webview
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pywebview"])
        # pyrefly: ignore [missing-import]
        import webview
    except Exception:
        webview = None


def find_opencode_cli() -> str | None:
    """Find opencode CLI executable on system PATH or standard npm/installation locations."""
    import shutil
    exe = shutil.which("opencode") or shutil.which("opencode.cmd") or shutil.which("opencode.exe")
    if exe:
        return exe

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            npm_cmd = Path(appdata) / "npm" / "opencode.cmd"
            if npm_cmd.exists():
                return str(npm_cmd)
            npm_exe = Path(appdata) / "npm" / "opencode.exe"
            if npm_exe.exists():
                return str(npm_exe)
            npm_ps1 = Path(appdata) / "npm" / "opencode"
            if npm_ps1.exists():
                return str(npm_ps1)

        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            for candidate in [
                Path(local_app) / "Programs" / "opencode" / "opencode.exe",
                Path(local_app) / "opencode" / "opencode.exe",
            ]:
                if candidate.exists():
                    return str(candidate)

    return None


def is_opencode_installed() -> bool:
    """Return True if opencode CLI is installed on this PC."""
    return find_opencode_cli() is not None


# Global references for active OpenCode AI session tracking
active_ai_proc = None


# HTML Page containing xterm.js with native fallback terminal renderer
HTML_CONTENT = r"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>OpenCode AI Assistant</title>
    <style>
        html, body {
            margin: 0; padding: 0; width: 100% !important; height: 100% !important;
            background-color: #0c0d10 !important; color: #cccccc;
            font-family: 'Consolas', 'Courier New', monospace; font-size: 14px;
            overflow: hidden;
            box-sizing: border-box;
        }
        #terminal-container {
            width: 100% !important; height: 100% !important;
            background-color: #0c0d10;
            margin: 0; padding: 0;
            box-sizing: border-box;
        }
        #fallback-container {
            display: none; width: 100% !important; height: 100% !important; box-sizing: border-box;
            padding: 12px; flex-direction: column; background: #0c0d10; color: #00ff66;
        }
        #fallback-output {
            flex: 1; overflow-y: auto; white-space: pre-wrap; word-break: break-all;
            background: #0c0d10; color: #cccccc; font-family: inherit; font-size: 13px;
        }
        #fallback-input-row { display: flex; align-items: center; background: #16181f; padding: 6px; border-top: 1px solid #252830; }
        #fallback-prompt { color: #00d2ff; font-weight: bold; margin-right: 8px; }
        #fallback-input {
            flex: 1; background: transparent; border: none; outline: none;
            color: #ffffff; font-family: inherit; font-size: 14px;
        }
        .xterm {
            padding: 2px 4px !important;
            height: 100% !important;
            width: 100% !important;
            box-sizing: border-box;
        }
        .xterm, .xterm-viewport, .xterm-screen {
            background-color: #0c0d10 !important;
            overflow-y: hidden !important;
            width: 100% !important;
        }
        ::-webkit-scrollbar {
            display: none !important;
            width: 0px !important;
            height: 0px !important;
        }
    </style>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" />
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js" onerror="window.xtermErr=true"></script>
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js" onerror="window.xtermErr=true"></script>
</head>
<body>
    <div id="terminal-container"></div>
    <div id="fallback-container">
        <div id="fallback-output">🤖 MCU Flash GUI — OpenCode AI Terminal\n---------------------------------------\nConnecting to PTY Session...\n</div>
        <div id="fallback-input-row">
            <span id="fallback-prompt">&gt;</span>
            <input type="text" id="fallback-input" autofocus placeholder="Type command here and press Enter..." />
        </div>
    </div>
    <script>
        let term = null;
        let useFallback = false;
        const container = document.getElementById('terminal-container');
        const fallbackContainer = document.getElementById('fallback-container');
        const fallbackOutput = document.getElementById('fallback-output');
        const fallbackInput = document.getElementById('fallback-input');

        function enableFallback() {
            useFallback = true;
            container.style.display = 'none';
            fallbackContainer.style.display = 'flex';
            fallbackInput.focus();
        }

        if (window.xtermErr || typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') {
            enableFallback();
        } else {
            try {
                term = new Terminal({
                    cursorBlink: true,
                    cursorStyle: 'block',
                    fontSize: 14,
                    fontFamily: 'Consolas, "Courier New", monospace',
                    overviewRulerWidth: 0,
                    theme: {
                        background: '#0c0d10',
                        foreground: '#cccccc',
                        cursor: '#ffffff',
                        selectionBackground: '#264f78'
                    }
                });
                const fitAddon = new FitAddon.FitAddon();
                term.loadAddon(fitAddon);
                term.open(container);
                window.fitAddon = fitAddon;
                setTimeout(() => { fitAddon.fit(); }, 100);
            } catch (e) {
                console.warn('xterm.js failed to initialize, enabling fallback:', e);
                enableFallback();
            }
        }

        const wsPort = parseInt(window.location.port || "80") + 1;
        const wsUrl = `ws://${window.location.hostname}:${wsPort}/ws`;
        const socket = new WebSocket(wsUrl);

        socket.onopen = () => {
            if (term && window.fitAddon) {
                window.fitAddon.fit();
                sendResize();
            }
            // Tell the Python host that WebView2, xterm, and the WebSocket are
            // alive. This is more reliable than inferring readiness solely from
            // OpenCode's output timing, which may finish before the old watcher
            // arms on fast machines.
            try {
                socket.send(JSON.stringify({ type: 'client_ready' }));
            } catch (e) {}
        };

        function stripAnsi(str) {
            return str.replace(/\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g, '');
        }

        socket.onmessage = (event) => {
            if (term) {
                term.write(event.data);
            } else {
                fallbackOutput.textContent += stripAnsi(event.data);
                fallbackOutput.scrollTop = fallbackOutput.scrollHeight;
            }
        };

        socket.onclose = () => {
            const msg = '\r\n[Terminal session closed]\r\n';
            if (term) term.write(msg);
            else fallbackOutput.textContent += msg;
        };

        if (term) {
            term.onData((data) => {
                if (socket.readyState === WebSocket.OPEN) {
                    socket.send(JSON.stringify({ type: 'input', data: data }));
                }
            });
        }

        fallbackInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                const val = fallbackInput.value + '\r\n';
                if (socket.readyState === WebSocket.OPEN) {
                    socket.send(JSON.stringify({ type: 'input', data: val }));
                }
                fallbackInput.value = '';
            }
        });

        function sendResize() {
            if (term && socket.readyState === WebSocket.OPEN) {
                socket.send(JSON.stringify({
                    type: 'resize',
                    cols: term.cols,
                    rows: term.rows
                }));
            }
        }

        function triggerFit() {
            if (term && window.fitAddon) {
                try {
                    window.fitAddon.fit();
                    sendResize();
                } catch (e) {}
            }
        }

        window.addEventListener('resize', triggerFit);
        document.addEventListener('DOMContentLoaded', triggerFit);

        if (typeof ResizeObserver !== 'undefined' && container) {
            const ro = new ResizeObserver(() => {
                triggerFit();
            });
            ro.observe(container);
        }

        [50, 150, 350, 700, 1200, 2000].forEach(ms => setTimeout(triggerFit, ms));
    </script>
</body>
</html>
"""


class TerminalServer:
    def __init__(self, port=8765, target_dir=None, command=None):
        self.port = port
        self.target_dir = target_dir or os.getcwd()
        self.command = command or find_opencode_cli() or "opencode"
        self.pty = None
        self.loop = None
        self.is_running = True
        self._pty_lock = threading.RLock()
        self._restart_count = 0

    def _supervised_opencode_command(self, target_dir):
        """Run OpenCode under a persistent CMD supervisor.

        OpenCode's ``/exit`` exits the child CLI, not the container.  The
        infinite CMD loop immediately launches a fresh session in the same
        PTY after that command or any unexpected child termination.
        """
        cmd_to_run = self.command or find_opencode_cli() or "opencode"
        if " " in cmd_to_run or os.path.exists(cmd_to_run):
            invocation = f'"{cmd_to_run}"'
        else:
            invocation = cmd_to_run
        return (
            f'cd /d "{target_dir}" && '
            f'set PATH=%APPDATA%\\npm;%PATH% && cls && '
            f'for /L %i in (0,0,1) do ( {invocation} & '
            f'timeout /t 1 /nobreak >nul )\r\n'
        )

    async def ws_handler(self, websocket):
        target_dir = os.path.abspath(self.target_dir)
        if PtyProcess:
            try:
                self.pty = PtyProcess.spawn("cmd.exe", dimensions=(30, 120))
                self.pty.write(self._supervised_opencode_command(target_dir))
            except Exception as e:
                print(f"[ERROR] Failed to spawn PTY process: {e}")

        # Thread: PTY -> WebSocket
        ready_marker_written = [False]
        ready_marker_lock = threading.Lock()
        spawn_ts = [time.time()]
        last_data_ts = [time.time()]
        output_seen = [False]
        output_bytes = [0]
        output_chunks = [0]
        webview_connected = [False]
        input_line = [""]
        input_escape = [False]

        def input_requests_exit(raw_data):
            """Detect a complete user-entered /exit line without swallowing it."""
            requested = False
            for char in str(raw_data or ""):
                if input_escape[0]:
                    # Ignore cursor/function-key escape sequences while
                    # reconstructing the current command line.
                    if char.isalpha() or char == "~":
                        input_escape[0] = False
                    continue
                if char == "\x1b":
                    input_escape[0] = True
                    continue
                if char in ("\r", "\n"):
                    if input_line[0].strip().casefold() == "/exit":
                        requested = True
                    input_line[0] = ""
                    continue
                if char in ("\x08", "\x7f"):
                    input_line[0] = input_line[0][:-1]
                    continue
                if char == "\x03":
                    input_line[0] = ""
                    continue
                if char.isprintable():
                    input_line[0] = (input_line[0] + char)[-512:]
            return requested

        def notify_container_restart():
            notice = (
                "\r\n[MCU Flasher] /exit detected. Restarting OpenCode "
                "inside the protected AI container…\r\n"
            )
            if self.loop and self.loop.is_running():
                try:
                    asyncio.run_coroutine_threadsafe(websocket.send(notice), self.loop)
                except Exception:
                    pass

        def write_ready_marker(reason="ready"):
            """Publish readiness exactly once for the embedding GUI.

            The marker means the OpenCode TUI itself has settled and should be
            interactive. A connected WebView/WebSocket is intentionally not enough:
            the browser surface can exist several seconds before OpenCode is ready.
            """
            with ready_marker_lock:
                if ready_marker_written[0]:
                    return
                ready_marker_written[0] = True
                try:
                    sig = Path(target_dir) / ".ai_ready_signal"
                    sig.write_text(
                        json.dumps({"time": time.time(), "reason": reason}),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

        def pty_read_loop():
            edit_kw = ["Applied edit", "Applied patch", "Updating file", "Writing to", "Wrote ", "Created ", "Edited ", "[edit]", "[write]", "[create]", "File saved"]
            while self.is_running and self.pty:
                try:
                    data = self.pty.read(4096)
                    if data:
                        output_seen[0] = True
                        output_chunks[0] += 1
                        output_bytes[0] += len(data.encode("utf-8", errors="replace"))
                        last_data_ts[0] = time.time()
                        try:
                            if any(kw in data for kw in edit_kw):
                                sig = Path(target_dir) / ".ai_edit_signal"
                                sig.write_text(str(time.time()), encoding="utf-8")
                                if sys.platform == "win32":
                                    try:
                                        import ctypes
                                        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(sig))
                                        if attrs != -1 and not (attrs & 0x02):
                                            ctypes.windll.kernel32.SetFileAttributesW(str(sig), attrs | 0x02)
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                        if websocket and self.loop and self.loop.is_running():
                            asyncio.run_coroutine_threadsafe(websocket.send(data), self.loop)
                except Exception as err:
                    print(f"[ERROR] pty_read_loop error: {err}")
                    time.sleep(0.05)

        def ready_monitor():
            """Publish readiness only when the OpenCode TUI has settled.

            WebView2 and the websocket often connect before OpenCode has loaded
            its configuration and rendered the interactive prompt. Require a
            connected client, meaningful PTY output, a minimum startup interval,
            and a quiet period. There is no timeout-based ready state: a cold
            first run remains loading until this actual readiness condition is met.
            """
            while self.is_running and not ready_marker_written[0]:
                now = time.time()
                elapsed = now - spawn_ts[0]
                quiet_for = now - last_data_ts[0]
                meaningful_output = (
                    output_seen[0]
                    and (output_bytes[0] >= 512 or output_chunks[0] >= 3)
                )
                if (
                    webview_connected[0]
                    and meaningful_output
                    and elapsed >= 4.0
                    and quiet_for >= 1.75
                ):
                    write_ready_marker("opencode-ready")
                    return
                time.sleep(0.15)

        if self.pty:
            threading.Thread(target=pty_read_loop, daemon=True).start()
            threading.Thread(target=ready_monitor, daemon=True).start()

        # Handle WebSocket -> PTY
        try:
            async for message in websocket:
                msg = json.loads(message)
                if msg.get('type') == 'client_ready':
                    # The browser surface is alive, but OpenCode may still be
                    # loading. The readiness monitor will publish the marker only
                    # after PTY output settles.
                    webview_connected[0] = True
                elif self.pty and msg.get('type') == 'input':
                    input_data = msg.get('data', '')
                    if input_requests_exit(input_data):
                        # Let OpenCode receive /exit normally.  The CMD
                        # supervisor above observes its termination and starts
                        # the next session in the same container.
                        notify_container_restart()
                    with self._pty_lock:
                        current_pty = self.pty
                    if current_pty:
                        current_pty.write(input_data)
                elif self.pty and msg.get('type') == 'resize':
                    cols = msg.get('cols', 120)
                    rows = msg.get('rows', 30)
                    with self._pty_lock:
                        current_pty = self.pty
                    if current_pty:
                        current_pty.setwinsize(rows, cols)
        except Exception:
            pass
        finally:
            self.stop()

    def run_http_server(self):
        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/' or self.path == '/index.html':
                    self.send_response(200)
                    self.send_header('Content-type', 'text/html')
                    self.end_headers()
                    self.wfile.write(HTML_CONTENT.encode('utf-8'))
                else:
                    self.send_error(404)

            def log_message(self, format, *args):
                pass

        try:
            httpd = HTTPServer(('127.0.0.1', self.port), Handler)
            httpd.serve_forever()
        except Exception:
            pass

    async def start_async(self):
        threading.Thread(target=self.run_http_server, daemon=True).start()
        self.loop = asyncio.get_running_loop()

        ws_port = self.port + 1
        if websockets:
            async with websockets.serve(self.ws_handler, '127.0.0.1', ws_port):
                await asyncio.Future()  # Run server indefinitely

    def stop(self):
        self.is_running = False
        with self._pty_lock:
            current_pty = self.pty
            self.pty = None
        if current_pty:
            try:
                current_pty.close()
            except Exception:
                pass


def find_free_pair(start_port=8765):
    for p in range(start_port, start_port + 100, 2):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s1, \
                 socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
                s1.bind(('127.0.0.1', p))
                s2.bind(('127.0.0.1', p + 1))
                return p
        except Exception:
            pass
    return start_port


def apply_window_icon(window_title):
    """Applies src/mcu_icon.ico to the pywebview window using Win32 WM_SETICON and SetClassLongPtrW."""
    if sys.platform != "win32":
        return
    try:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("naph.mcuflasher.gui.v3")
        except Exception:
            pass

        icon_path = SCRIPT_DIR / "src" / "assets" / "mcu_icon.ico"
        if not icon_path.exists():
            icon_path = SCRIPT_DIR / "src" / "mcu_icon.ico"
        if not icon_path.exists():
            icon_path = SCRIPT_DIR.parent / "src" / "assets" / "mcu_icon.ico"
        if not icon_path.exists():
            icon_path = SCRIPT_DIR.parent / "src" / "mcu_icon.ico"
        if not icon_path.exists():
            return

        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        GCLP_HICON = -14
        GCLP_HICONSM = -34

        user32 = ctypes.windll.user32
        hIcon = user32.LoadImageW(
            None,
            str(icon_path),
            IMAGE_ICON,
            0, 0,
            LR_LOADFROMFILE
        )
        if not hIcon:
            return

        def _set_icon():
            hwnd = None
            for _ in range(60):
                time.sleep(0.1)
                hwnd = user32.FindWindowW(None, window_title)
                if not hwnd:
                    def _enum_win_cb(h, l):
                        nonlocal hwnd
                        if user32.IsWindowVisible(h):
                            buf = ctypes.create_unicode_buffer(512)
                            user32.GetWindowTextW(h, buf, 512)
                            if window_title in buf.value or "OpenCode AI" in buf.value:
                                hwnd = h
                                return False
                        return True

                    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                    user32.EnumWindows(WNDENUMPROC(_enum_win_cb), 0)

                if hwnd:
                    break

            if hwnd:
                try:
                    set_class_ptr = getattr(user32, "SetClassLongPtrW", getattr(user32, "SetClassLongA", None))
                    if set_class_ptr:
                        set_class_ptr(hwnd, GCLP_HICON, hIcon)
                        set_class_ptr(hwnd, GCLP_HICONSM, hIcon)
                except Exception:
                    pass

                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hIcon)
                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hIcon)

                try:
                    GA_ROOT = 2
                    root_hwnd = user32.GetAncestor(hwnd, GA_ROOT)
                    if root_hwnd and root_hwnd != hwnd:
                        user32.SendMessageW(root_hwnd, WM_SETICON, ICON_SMALL, hIcon)
                        user32.SendMessageW(root_hwnd, WM_SETICON, ICON_BIG, hIcon)
                except Exception:
                    pass

        threading.Thread(target=_set_icon, daemon=True).start()
    except Exception:
        pass


def _pre_hide_console_for_conpty():
    """Pre-allocate a hidden console before pywebview + pywinpty ConPTY start.

    On Windows 11, spawning a pseudo console (pywinpty ConPTY) from a GUI
    process that already runs a message pump (pywebview/WebView2) makes the
    conhost flash a visible ConsoleWindowClass window for a few milliseconds
    while the console session initializes.  Pre-creating the console hidden
    via AllocConsole() + ShowWindow(SW_HIDE) lets ConPTY reuse that console,
    eliminating the flash.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        if not user32.GetConsoleWindow():
            kernel32.AllocConsole()
        hwnd = user32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


def run_standalone_ai(target_directory=None):
    """Entry point when executed as an independent AI terminal process."""
    _pre_hide_console_for_conpty()
    target_dir = os.path.abspath(target_directory) if target_directory else os.getcwd()
    # Remove a stale marker from an earlier process. The new process publishes
    # a fresh marker after WebView/PTY readiness, preventing old timestamps from
    # racing the main GUI's loading overlay.
    try:
        (Path(target_dir) / ".ai_ready_signal").unlink(missing_ok=True)
    except Exception:
        pass
    port = find_free_pair(8765)
    opencode_exe = find_opencode_cli() or "opencode"
    server = TerminalServer(port=port, target_dir=target_dir, command=opencode_exe)

    def run_server_loop():
        try:
            asyncio.run(server.start_async())
        except Exception:
            pass

    threading.Thread(target=run_server_loop, daemon=True).start()
    time.sleep(0.4)

    url = f"http://127.0.0.1:{port}"
    window_title = "MCU Flash GUI - OpenCode AI Assistant"
    print(f"[INFO] Launching OpenCode AI Terminal at {url}...")

    apply_window_icon(window_title)

    if webview:
        window = webview.create_window(
            title=window_title,
            url=url,
            width=1040,
            height=680,
            resizable=True,
            # The main GUI embeds this native window after WebView2 creates
            # it.  Keep it hidden during that short handoff so it cannot flash
            # as a separate window on the desktop.
            hidden=True,
            focus=False,
            background_color="#0c0d10"
        )
        try:
            webview.start()
        finally:
            server.stop()
            os._exit(0)
    else:
        import webbrowser
        webbrowser.open(url)


def launch_opencode_elevated_cmd(target_directory=None):
    """
    Launches OpenCode AI Terminal in an independent pywebview process.
    Prevents Win32 message loop deadlocks with Tkinter mainloop.
    """
    global active_ai_proc

    close_active_opencode()

    if not target_directory:
        target_directory = os.getcwd()

    target_directory = os.path.abspath(str(target_directory))
    script_path = str(Path(__file__).resolve())
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    try:
        active_ai_proc = subprocess.Popen(
            [sys.executable, script_path, "--launch-ai", target_directory],
            creationflags=flags
        )
        print(f"[INFO] OpenCode AI process started (PID: {active_ai_proc.pid})")
        return active_ai_proc
    except Exception as e:
        print(f"[ERROR] Failed to launch OpenCode AI process: {e}")
        return None


def close_active_opencode():
    """Terminates the active OpenCode AI pywebview subprocess cleanly."""
    global active_ai_proc
    closed = False

    if active_ai_proc and active_ai_proc.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(active_ai_proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            closed = True
        except Exception:
            try:
                active_ai_proc.kill()
                closed = True
            except Exception:
                pass

    active_ai_proc = None
    return closed


def is_opencode_running(hProcess=None):
    """Checks if the OpenCode AI terminal process is active."""
    global active_ai_proc
    if active_ai_proc and active_ai_proc.poll() is None:
        return True
    return False


# ==============================================================================
# Dedicated AI Controller Helper Class (Used for Button State & Animation)
# ==============================================================================
class AIController:
    def __init__(self, button_widgets=None, get_sketch_dir_func=None, root=None, on_ai_edit_func=None, on_state_change_func=None):
        self.buttons = button_widgets or []
        self.get_sketch_dir = get_sketch_dir_func or os.getcwd
        self.root = root
        self.on_ai_edit_func = on_ai_edit_func
        self.on_state_change_func = on_state_change_func
        self.is_launching = False
        self.running_handle = None
        self.anim_job = None
        self.monitor_job = None
        self.anim_index = 0
        self.anim_frames = ["⏳ Starting AI", "⚡ Starting AI.", "⏳ Starting AI..", "⚡ Starting AI..."]
        self._file_mtimes = {}
        self._file_contents = {}
        self._last_signal_mtime = 0
        self._pending_edits = {}
        self._watch_lock = threading.RLock()
        self._last_content_verification = 0.0
        self._monitoring_initialized = False
        if is_opencode_running():
            try:
                self._start_monitoring()
            except Exception:
                pass

    def add_button(self, btn):
        if btn not in self.buttons:
            self.buttons.append(btn)

    def trigger_ai_edit_reload(
        self,
        filepath=None,
        before_content=None,
        after_content=None,
        before_exists=True,
        after_exists=True,
    ):
        """Trigger the on_ai_edit_func reload callback if defined."""
        if self.on_ai_edit_func and callable(self.on_ai_edit_func):
            def _invoke():
                try:
                    import inspect
                    parameters = list(inspect.signature(self.on_ai_edit_func).parameters.values())
                    accepts_varargs = any(
                        parameter.kind is inspect.Parameter.VAR_POSITIONAL
                        for parameter in parameters
                    )
                    positional_count = sum(
                        parameter.kind in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        )
                        for parameter in parameters
                    )
                except (TypeError, ValueError):
                    accepts_varargs = True
                    positional_count = 5
                if accepts_varargs or positional_count >= 5:
                    self.on_ai_edit_func(
                        filepath,
                        before_content,
                        after_content,
                        before_exists,
                        after_exists,
                    )
                elif positional_count >= 3:
                    # Compatibility with the previous three-argument
                    # callback contract.
                    self.on_ai_edit_func(filepath, before_content, after_content)
                else:
                    self.on_ai_edit_func(filepath)
            if self.root:
                try:
                    self.root.after(0, _invoke)
                except Exception:
                    _invoke()
            else:
                _invoke()

    @staticmethod
    def _read_project_text(filepath):
        try:
            # newline="" keeps CRLF/LF exactly as OpenCode wrote it so Reject
            # can restore the original editor text without line-ending churn.
            with open(filepath, "r", encoding="utf-8", errors="replace", newline="") as stream:
                return stream.read()
        except Exception:
            return ""

    @staticmethod
    def _path_key(filepath):
        try:
            return os.path.normcase(str(Path(filepath).resolve(strict=False)))
        except (OSError, ValueError):
            return os.path.normcase(os.path.abspath(str(filepath)))

    def note_local_save(self, filepath, content=None):
        """Advance the watcher baseline for an editor-originated save.

        Without this, any Monaco autosave while OpenCode was open looked like
        an AI edit and produced a false diff notification.
        """
        if not filepath:
            return
        fp = self._path_key(filepath)
        with self._watch_lock:
            if os.path.isfile(fp):
                try:
                    self._file_mtimes[fp] = os.path.getmtime(fp)
                except Exception:
                    pass
                self._file_contents[fp] = (
                    content if content is not None else self._read_project_text(fp)
                )
            else:
                self._file_mtimes.pop(fp, None)
                self._file_contents.pop(fp, None)
            self._pending_edits.pop(fp, None)

    def reset_monitoring_state(self):
        """Drop watcher baselines when the GUI binds a different project."""
        with self._watch_lock:
            self._file_mtimes = {}
            self._file_contents = {}
            self._pending_edits = {}
            self._monitoring_initialized = False
        self._rebaseline_project_files()

    def relaunch_for_project(self, sketch_dir):
        """Restart the running OpenCode AI process so its session points at a
        new sketch directory instead of the one it was launched with.

        Returns True when a running AI process was killed and relaunched,
        False when no AI process was running (watcher is rebaselined only).
        """
        was_running = is_opencode_running()
        if not was_running:
            # Nothing to redirect; just rebaseline the watcher for the new project.
            self.reset_monitoring_state()
            return False

        # Cancel the pending monitor tick so stale baselines are not reused.
        if self.monitor_job and self.root:
            try:
                self.root.after_cancel(self.monitor_job)
            except Exception:
                pass
            self.monitor_job = None

        # launch_opencode_elevated_cmd() closes the old process and spawns a
        # fresh one whose PTY starts inside the new sketch directory.
        try:
            launch_opencode_elevated_cmd(str(sketch_dir))
        except Exception:
            try:
                close_active_opencode()
            except Exception:
                pass
            self.reset_monitoring_state()
            return False

        # Rebaseline the watcher against the new project and re-arm monitoring
        # for the freshly launched process.
        self.reset_monitoring_state()
        try:
            self._start_monitoring()
        except Exception:
            pass
        return True

    def _rebaseline_project_files(self):
        """Re-read the current project's sketch files as fresh watcher
        baselines right after a project switch.

        Without this, the next monitor tick sees every pre-existing file of
        the new project with an empty baseline, waits out the 400 ms settle
        window, and queues an AI Review for EACH file showing the ENTIRE file
        as newly added (the whole project's line count)."""
        try:
            sketch_dir = self.get_sketch_dir()
            if callable(sketch_dir):
                sketch_dir = sketch_dir()
            with self._watch_lock:
                scanned = self._scan_project_files(sketch_dir)
                self._file_mtimes = {
                    self._path_key(fp): mtime
                    for fp, mtime in scanned.items()
                }
                self._file_contents = {
                    self._path_key(fp): self._read_project_text(fp)
                    for fp in scanned
                    if self._is_valid_user_sketch_file(fp)
                }
                self._pending_edits = {}
        except Exception:
            pass

    def collect_unreported_edits(self):
        """Synchronously detect edits not yet emitted by the debounce loop.

        Compile/Upload calls this immediately before its approval gate so an
        OpenCode write cannot slip through during the normal 400 ms settle
        window.
        """
        if not self._monitoring_initialized:
            return []
        ai_is_running = is_opencode_running()
        sketch_dir = self.get_sketch_dir()
        if callable(sketch_dir):
            sketch_dir = sketch_dir()
        current_mtimes = {
            self._path_key(path): mtime
            for path, mtime in self._scan_project_files(sketch_dir).items()
        }
        detected = []
        with self._watch_lock:
            candidate_paths = set(self._file_contents) | set(current_mtimes)
            for path in candidate_paths:
                if not self._is_valid_user_sketch_file(path):
                    continue
                existing = self._pending_edits.get(path)
                before_exists = (
                    existing.get("before_exists", True)
                    if isinstance(existing, dict) else path in self._file_contents
                )
                before = (
                    existing.get("before", "")
                    if isinstance(existing, dict) else self._file_contents.get(path, "")
                )
                after_exists = path in current_mtimes and os.path.isfile(path)
                after = self._read_project_text(path) if after_exists else ""
                if before_exists != after_exists or before != after:
                    detected.append((path, before, after, before_exists, after_exists))
                if after_exists:
                    self._file_contents[path] = after
                    self._file_mtimes[path] = current_mtimes[path]
                else:
                    self._file_contents.pop(path, None)
                    self._file_mtimes.pop(path, None)
                self._pending_edits.pop(path, None)
            if not ai_is_running:
                # One final synchronous scan closes the debounce gap after the
                # CLI exits; later non-AI compiles do not keep rescanning.
                self._monitoring_initialized = False
        return detected

    def _scan_project_files(self, target_dir):
        """Scan project files for modification times."""
        mtimes = {}
        if not target_dir or not os.path.exists(target_dir):
            return mtimes
        try:
            ignore_dirs = {
                ".git", ".vscode", "env", "node_modules", "__pycache__",
                ".platformio", "build", ".pio", "src",
                ".mcu_flasher_build_cache",
            }
            for root_path, dirs, files in os.walk(target_dir):
                dirs[:] = [
                    directory for directory in dirs
                    if directory.lower() not in ignore_dirs and not directory.startswith(".")
                ]
                for file in files:
                    if (file.startswith(".")
                            or file.lower().endswith(".ino.cpp")
                            or file.endswith((".pyc", ".tmp", ".log", ".bak", ".swp"))):
                        continue
                    fp = os.path.join(root_path, file)
                    try:
                        mtimes[fp] = os.path.getmtime(fp)
                    except Exception:
                        pass
        except Exception:
            pass
        return mtimes

    def dispose(self):
        """Dispose/terminate active AI process and reset UI states."""
        self.is_launching = False
        if self.anim_job and self.root:
            try:
                self.root.after_cancel(self.anim_job)
            except Exception:
                pass
            self.anim_job = None
        if self.monitor_job and self.root:
            try:
                self.root.after_cancel(self.monitor_job)
            except Exception:
                pass
            self.monitor_job = None
        was_closed = close_active_opencode()
        self._set_idle_state()
        return was_closed

    def toggle_ai(self):
        """Ensure OpenCode AI process is running (prompts disclaimer prompt ONLY ONCE)."""
        if self.is_launching or is_opencode_running():
            return True

        if not getattr(self, "disclaimer_accepted", False):
            disclaimer_title = "OpenCode AI Assistant (Beta Test)"
            disclaimer_msg = (
                "🤖 OpenCode AI Assistant\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "📌 Notice & Disclaimer:\n"
                "• OpenCode AI integration in this project is currently in BETA TESTING.\n"
                "• MCU Flash GUI does not claim any copyright, trademark, or ownership of OpenCode AI. "
                "All rights, trademarks, and intellectual property belong to their respective creators.\n\n"
                "🛡️ System Permission:\n"
                "• Clicking 'Yes' will launch OpenCode in a native Python desktop window "
                "to assist you with fixing, explaining, and debugging code.\n\n"
                "Do you want to proceed and launch OpenCode AI Assistant?"
            )

            proceed = messagebox.askyesno(disclaimer_title, disclaimer_msg, parent=self.root)
            if not proceed:
                return False
            self.disclaimer_accepted = True

        self._start_launching_animation()
        threading.Thread(target=self._async_launch, daemon=True).start()
        return True

    def _start_launching_animation(self):
        self.is_launching = True
        self.anim_index = 0
        self._animate_step()

    def _animate_step(self):
        if not self.is_launching:
            return

        frame = self.anim_frames[self.anim_index % len(self.anim_frames)]
        self.anim_index += 1

        for b in self.buttons:
            try:
                b.configure(text=frame, state=tk.DISABLED)
            except Exception:
                pass

        if self.root:
            self.anim_job = self.root.after(300, self._animate_step)

    def _async_launch(self):
        sketch_dir = self.get_sketch_dir()
        if callable(sketch_dir):
            sketch_dir = sketch_dir()

        hProcess = launch_opencode_elevated_cmd(str(sketch_dir))

        if self.root:
            self.root.after(0, lambda: self._on_launch_complete(hProcess))

    def _on_launch_complete(self, hProcess):
        self.is_launching = False
        if self.anim_job and self.root:
            self.root.after_cancel(self.anim_job)
            self.anim_job = None

        if is_opencode_running():
            self.running_handle = hProcess
            self._set_running_state()
            self._start_monitoring()
        else:
            self._set_idle_state()

    def _set_running_state(self):
        for b in self.buttons:
            try:
                b.configure(state=tk.NORMAL)
            except Exception:
                pass
        if self.on_state_change_func and callable(self.on_state_change_func):
            try:
                self.on_state_change_func()
            except Exception:
                pass
        else:
            for b in self.buttons:
                try:
                    b.configure(text="🤖 Hide AI")
                except Exception:
                    pass

    def _set_idle_state(self):
        self.running_handle = None
        for b in self.buttons:
            try:
                b.configure(
                    text="🤖 AI Assistant", 
                    state=tk.NORMAL,
                    bg="#252526", 
                    activebackground="#3e3e42",
                    fg="#ffffff"
                )
            except Exception:
                pass
        if self.on_state_change_func and callable(self.on_state_change_func):
            try:
                self.on_state_change_func()
            except Exception:
                pass

    VALID_EDIT_EXTENSIONS = (".ino", ".cpp", ".c", ".h", ".hpp", ".txt")
    IGNORED_SYSTEM_FILENAMES = {
        "agents.md", "opencode.md", "read-first.md", ".read-first.md",
        "skill.md", ".skill.md", ".opencodeignore", ".ignore",
        "platformio.ini", ".mcu_gui_cache.json", ".mcu_gui_compat_cache.json",
        ".mcu_flash_syntax_errors.json",
        ".mcu_flash_tab_order.json", ".ai_edit_signal", ".ai_ready_signal"
    }

    def _is_valid_user_sketch_file(self, filepath):
        if not filepath:
            return False
        p = Path(filepath)
        name_lower = p.name.lower()
        if name_lower in self.IGNORED_SYSTEM_FILENAMES:
            return False
        if p.suffix.lower() in self.VALID_EDIT_EXTENSIONS:
            return True
        return False

    def _start_monitoring(self):
        sketch_dir = self.get_sketch_dir()
        if callable(sketch_dir):
            sketch_dir = sketch_dir()
        with self._watch_lock:
            self._file_mtimes = self._scan_project_files(sketch_dir)
            self._file_contents = {
                self._path_key(fp): self._read_project_text(fp)
                for fp in self._file_mtimes
                if self._is_valid_user_sketch_file(fp)
            }
            self._file_mtimes = {
                self._path_key(fp): mtime
                for fp, mtime in self._file_mtimes.items()
            }
            self._pending_edits = {}
            self._monitoring_initialized = True
        self._monitor_step()

    def _monitor_step(self):
        if is_opencode_running():
            sketch_dir = self.get_sketch_dir()
            if callable(sketch_dir):
                sketch_dir = sketch_dir()

            current_mtimes = self._scan_project_files(sketch_dir)
            now = time.time()

            self._watch_lock.acquire()
            try:
                if not hasattr(self, "_pending_edits"):
                    self._pending_edits = {}

                current_mtimes = {
                    self._path_key(fp): mtime
                    for fp, mtime in current_mtimes.items()
                }
                if now - self._last_content_verification >= 1.0:
                    self._last_content_verification = now
                    for verified_fp in current_mtimes:
                        if not self._is_valid_user_sketch_file(verified_fp):
                            continue
                        verified_text = self._read_project_text(verified_fp)
                        baseline_text = self._file_contents.get(verified_fp, "")
                        if verified_text == baseline_text:
                            continue
                        existing = self._pending_edits.get(verified_fp)
                        self._pending_edits[verified_fp] = {
                            "changed_at": now,
                            "before": (
                                existing.get("before", "")
                                if isinstance(existing, dict) else baseline_text
                            ),
                            "before_exists": (
                                existing.get("before_exists", True)
                                if isinstance(existing, dict) else verified_fp in self._file_contents
                            ),
                        }
                for removed_fp in set(self._file_mtimes) - set(current_mtimes):
                    if self._is_valid_user_sketch_file(removed_fp):
                        existing = self._pending_edits.get(removed_fp)
                        self._pending_edits[removed_fp] = {
                            "changed_at": now,
                            "before": (
                                existing.get("before", "")
                                if isinstance(existing, dict)
                                else self._file_contents.get(removed_fp, "")
                            ),
                            "before_exists": (
                                existing.get("before_exists", True)
                                if isinstance(existing, dict) else True
                            ),
                        }
                    self._file_mtimes.pop(removed_fp, None)
                for fp, mtime in current_mtimes.items():
                    if not self._is_valid_user_sketch_file(fp):
                        continue
                    old_mtime = self._file_mtimes.get(fp, 0)
                    if old_mtime == 0:
                        self._file_mtimes[fp] = mtime
                        existing = self._pending_edits.get(fp)
                        if not isinstance(existing, dict):
                            self._file_contents[fp] = ""
                        self._pending_edits[fp] = {
                            "changed_at": now,
                            "before": existing.get("before", "") if isinstance(existing, dict) else "",
                            "before_exists": (
                                existing.get("before_exists", False)
                                if isinstance(existing, dict) else False
                            ),
                        }
                    elif abs(mtime - old_mtime) > 0.001:
                        self._file_mtimes[fp] = mtime
                        pending = self._pending_edits.get(fp)
                        before = pending.get("before", "") if isinstance(pending, dict) else self._file_contents.get(fp, "")
                        before_exists = (
                            pending.get("before_exists", True)
                            if isinstance(pending, dict) else True
                        )
                        self._pending_edits[fp] = {
                            "changed_at": now,
                            "before": before,
                            "before_exists": before_exists,
                        }

                signal_file = Path(sketch_dir) / ".ai_edit_signal"
                if signal_file.exists():
                    try:
                        sig_mtime = signal_file.stat().st_mtime
                        if sig_mtime > getattr(self, "_last_signal_mtime", 0):
                            self._last_signal_mtime = sig_mtime
                            # The signal is only a wake-up hint. Selecting the
                            # newest file here used to invent a no-op AI edit after
                            # compiles and could clear a real Monaco highlight.
                            # Verify bytes/text against the watcher baseline so a
                            # same-timestamp replacement is still detected without
                            # ever manufacturing a review.
                            for candidate_fp in current_mtimes:
                                if not self._is_valid_user_sketch_file(candidate_fp):
                                    continue
                                current_text = self._read_project_text(candidate_fp)
                                baseline_text = self._file_contents.get(candidate_fp, "")
                                if current_text == baseline_text:
                                    continue
                                existing = self._pending_edits.get(candidate_fp)
                                before = (
                                    existing.get("before", "")
                                    if isinstance(existing, dict) else baseline_text
                                )
                                before_exists = (
                                    existing.get("before_exists", True)
                                    if isinstance(existing, dict) else candidate_fp in self._file_contents
                                )
                                self._pending_edits[candidate_fp] = {
                                    "changed_at": now,
                                    "before": before,
                                    "before_exists": before_exists,
                                }
                            try:
                                signal_file.unlink(missing_ok=True)
                            except Exception:
                                pass
                    except Exception:
                        pass

                # Trigger edit reload once file mtime has stabilized for >= 400ms (completed edit)
                to_trigger = []
                for fp, pending in list(self._pending_edits.items()):
                    changed_at = pending.get("changed_at", now) if isinstance(pending, dict) else pending
                    if now - changed_at >= 0.4:
                        before = pending.get("before", "") if isinstance(pending, dict) else self._file_contents.get(fp, "")
                        before_exists = pending.get("before_exists", True) if isinstance(pending, dict) else True
                        after_exists = os.path.isfile(fp)
                        after = self._read_project_text(fp) if after_exists else ""
                        if before_exists != after_exists or before != after:
                            to_trigger.append((fp, before, after, before_exists, after_exists))
                        elif not after_exists:
                            self._file_contents.pop(fp, None)
                        del self._pending_edits[fp]

                for fp, before, after, before_exists, after_exists in to_trigger:
                    if after_exists:
                        self._file_contents[fp] = after
                    else:
                        self._file_contents.pop(fp, None)
                    self.trigger_ai_edit_reload(
                        fp, before, after, before_exists, after_exists
                    )

            finally:
                self._watch_lock.release()
            if self.root:
                self.monitor_job = self.root.after(300, self._monitor_step)
        else:
            for edit in self.collect_unreported_edits():
                self.trigger_ai_edit_reload(*edit)
            self._set_idle_state()


class DedicatedAIApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("MCU Flash GUI - OpenCode AI Controller Test")
        self.geometry("540x340")
        self.configure(bg="#1e1e1e")
        self.resizable(False, False)

        header_frame = tk.Frame(self, bg="#252526", height=60)
        header_frame.pack(fill=tk.X, side=tk.TOP)
        header_frame.pack_propagate(False)

        lbl_header = tk.Label(
            header_frame,
            text="🛡️ OpenCode AI Assistant Controller",
            font=("Segoe UI", 12, "bold"),
            fg="#007acc",
            bg="#252526"
        )
        lbl_header.pack(side=tk.LEFT, padx=20, pady=15)

        body_frame = tk.Frame(self, bg="#1e1e1e")
        body_frame.pack(fill=tk.BOTH, expand=True, padx=25, pady=15)

        info_text = (
            "• pywebview + xterm.js + pywinpty native terminal.\n"
            "• Animated button state during launching.\n"
            "• Dynamic toggle: '🤖 AI Assistant' -> '⏳ Launching...' -> '🔴 Close AI'.\n"
            "• Automatic detection when the AI window is closed."
        )

        lbl_info = tk.Label(
            body_frame,
            text=info_text,
            font=("Segoe UI", 10),
            fg="#cccccc",
            bg="#1e1e1e",
            justify=tk.LEFT
        )
        lbl_info.pack(anchor="w", pady=(0, 20))

        self.btn_ai = tk.Button(
            body_frame,
            text="🤖 AI Assistant",
            bg="#252526",
            fg="#ffffff",
            font=("Segoe UI", 11, "bold"),
            relief=tk.FLAT,
            padx=15,
            pady=10,
            cursor="hand2"
        )
        self.btn_ai.pack(fill=tk.X)

        self.controller = AIController(
            button_widgets=[self.btn_ai],
            get_sketch_dir_func=os.getcwd,
            root=self
        )
        self.btn_ai.configure(command=self.controller.toggle_ai)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--launch-ai":
        target_dir = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()
        run_standalone_ai(target_dir)
    else:
        app = DedicatedAIApp()
        app.mainloop()
