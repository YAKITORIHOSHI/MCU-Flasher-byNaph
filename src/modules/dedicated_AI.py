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
import threading
import subprocess
from pathlib import Path
import urllib.request
from http.server import HTTPServer, SimpleHTTPRequestHandler

# Add src/modules to sys.path
_modules_dir = Path(__file__).resolve().parent
if str(_modules_dir) not in sys.path:
    sys.path.insert(0, str(_modules_dir))

# Strict enforcement: NEVER run with system/desktop Python
from private_python_guard import enforce_private_python
enforce_private_python(prefer_pythonw=True)

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

def check_internet_connection(timeout: float = 2.0) -> bool:
    """Robust, fast multi-layer check for active internet connection.

    Uses concurrent probing over port 443 (HTTPS) and HTTP captive-portal probes
    (which respect system/user proxies), with Windows WinINet fallback. Never
    relies on TCP port 53, which is routinely blocked by firewalls and ISPs.
    """
    success = threading.Event()

    def _test_tcp(host: str, port: int) -> None:
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            success.set()
        except Exception:
            pass

    def _test_http(url: str) -> None:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status in (200, 204, 301, 302):
                    success.set()
        except Exception:
            pass

    # Port 443 (HTTPS) Anycast IPs: globally routed, fast, no DNS lookup needed
    targets_tcp = [
        ("1.1.1.1", 443),
        ("8.8.8.8", 443),
        ("1.0.0.1", 443),
        ("9.9.9.9", 443),
    ]
    # HTTP/HTTPS captive-portal probes (urllib respects system proxy and VPN configurations)
    urls = [
        "http://www.msftconnecttest.com/connecttest.txt",
        "http://clients3.google.com/generate_204",
        "https://cloudflare.com",
    ]
    # Hostname probes over 443 in case IP literals are filtered
    host_targets = [
        ("cloudflare.com", 443),
        ("github.com", 443),
    ]

    threads = []
    for host, port in targets_tcp:
        t = threading.Thread(target=_test_tcp, args=(host, port), daemon=True)
        t.start()
        threads.append(t)

    for url in urls:
        t = threading.Thread(target=_test_http, args=(url,), daemon=True)
        t.start()
        threads.append(t)

    for host, port in host_targets:
        t = threading.Thread(target=_test_tcp, args=(host, port), daemon=True)
        t.start()
        threads.append(t)

    success.wait(timeout=timeout)
    if success.is_set():
        return True

    # Secondary check: Windows Network Location Awareness via WinINet
    if sys.platform == "win32":
        try:
            import ctypes.wintypes
            flags = ctypes.wintypes.DWORD()
            if ctypes.windll.wininet.InternetGetConnectedState(ctypes.byref(flags), 0):
                return True
        except Exception:
            pass

    return False


# Ensure workspace 'env' site-packages is on sys.path
SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent
AI_STARTUP_PATCH_VERSION = "v22-readiness-no-fallback"
ENV_DIR = SCRIPT_DIR / "env"
ENV_SITE_PACKAGES = ENV_DIR / "Lib" / "site-packages"
if sys.platform == "win32" and ENV_SITE_PACKAGES.exists() and str(ENV_SITE_PACKAGES) not in sys.path:
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
    websockets = None

try:
    # pyrefly: ignore [missing-import]
    import webview
except ImportError:
    webview = None


def _is_valid_pe_binary(path: Path | str) -> bool:
    """Return True if path is a genuine Windows PE executable (starts with b'MZ' and >= 1KB)."""
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size < 1024:
            return False
        with p.open("rb") as f:
            return f.read(2) == b"MZ"
    except Exception:
        return False


def find_opencode_cli() -> str | None:
    """Find opencode CLI executable on system PATH, active processes, or standard npm locations."""
    # Check active running processes first
    try:
        import psutil
        for proc in psutil.process_iter(['name', 'exe', 'cmdline']):
            try:
                name = (proc.info.get('name') or "").lower()
                exe = proc.info.get('exe') or ""
                if name in ("opencode.exe", "opencode") and exe:
                    p = Path(exe)
                    if p.is_file() and (not exe.lower().endswith(".exe") or _is_valid_pe_binary(p)):
                        return str(p.resolve())
                cmdline = proc.info.get('cmdline') or []
                for arg in cmdline:
                    arg_l = str(arg).lower()
                    if "opencode" in arg_l and (arg_l.endswith(".exe") or arg_l.endswith(".cmd")):
                        p = Path(arg)
                        if p.is_file() and (not arg_l.endswith(".exe") or _is_valid_pe_binary(p)):
                            return str(p.resolve())
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception:
        pass

    import shutil
    for exe_name in ("opencode.cmd", "opencode.exe", "opencode"):
        found = shutil.which(exe_name)
        if found:
            if found.lower().endswith(".exe"):
                if _is_valid_pe_binary(found):
                    return found
            elif Path(found).is_file():
                return found

    if sys.platform == "win32":
        candidates = []
        for env_var in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
            base = os.environ.get(env_var, "")
            if not base:
                continue
            p_base = Path(base)
            if env_var == "APPDATA":
                candidates.extend([
                    p_base / "npm" / "opencode.cmd",
                    p_base / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe",
                    p_base / "npm" / "opencode.exe",
                    p_base / "npm" / "opencode",
                ])
            elif env_var == "LOCALAPPDATA":
                candidates.extend([
                    p_base / "Programs" / "opencode" / "opencode.exe",
                    p_base / "opencode" / "opencode.exe",
                    p_base / "npm" / "opencode.cmd",
                    p_base / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe",
                ])
            elif env_var == "USERPROFILE":
                candidates.extend([
                    p_base / "AppData" / "Roaming" / "npm" / "opencode.cmd",
                    p_base / "AppData" / "Roaming" / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe",
                    p_base / "AppData" / "Roaming" / "npm" / "opencode.exe",
                ])

        candidates.extend([
            Path(r"C:\Program Files\nodejs\opencode.cmd"),
            Path(r"C:\Program Files\nodejs\node_modules\opencode-ai\bin\opencode.exe"),
        ])

        for candidate in candidates:
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() == ".exe":
                if _is_valid_pe_binary(candidate):
                    return str(candidate)
            elif candidate.stat().st_size > 0:
                return str(candidate)

    return None


def is_opencode_installed() -> bool:
    """Return True if opencode CLI is installed on this PC."""
    return find_opencode_cli() is not None


# Global references for active OpenCode AI session tracking
active_ai_proc = None


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


def _resolve_ai_theme() -> dict:
    theme = "default"
    repo_root = Path(__file__).resolve().parent.parent
    for cfg in (
        repo_root / "src" / "gui_config.json",
        repo_root.parent / "src" / "gui_config.json",
        Path.home() / ".mcu_gui_config.json"
    ):
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


# HTML Page containing xterm.js with native fallback terminal renderer
HTML_CONTENT_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>OpenCode AI Assistant</title>
    <style>
        html, body {
            margin: 0; padding: 0; width: 100% !important; height: 100% !important;
            background-color: __THEME_BG__ !important; color: __THEME_FG__;
            font-family: 'Consolas', 'Courier New', monospace; font-size: 14px;
            overflow: hidden;
            box-sizing: border-box;
        }
        #terminal-container {
            width: 100% !important; height: 100% !important;
            background-color: __THEME_BG__;
            margin: 0; padding: 0;
            box-sizing: border-box;
        }
        #fallback-container {
            display: none; width: 100% !important; height: 100% !important; box-sizing: border-box;
            padding: 12px; flex-direction: column; background: __THEME_BG__; color: #00ff66;
        }
        #fallback-output {
            flex: 1; overflow-y: auto; white-space: pre-wrap; word-break: break-all;
            background: __THEME_BG__; color: __THEME_FG__; font-family: inherit; font-size: 13px;
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
            background-color: __THEME_BG__ !important;
            overflow-y: hidden !important;
            width: 100% !important;
        }
        ::-webkit-scrollbar {
            display: none !important;
            width: 0px !important;
            height: 0px !important;
        }
        #fallback-output::-webkit-scrollbar {
            display: block !important;
            width: 8px !important;
        }
    </style>
    <link rel="stylesheet" href="/xterm/xterm.css" />
    <script src="/xterm/xterm.js" onerror="window.xtermErr=true"></script>
    <script src="/xterm/xterm-addon-fit.js" onerror="window.xtermErr=true"></script>
</head>
<body>
    <div id="terminal-container"></div>
    <div id="fallback-container">
        <div id="fallback-output">MCU Flash GUI — OpenCode AI Terminal\n---------------------------------------\nConnecting to PTY Session...\n</div>
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
        let fallbackPointerHeld = false;
        fallbackOutput.addEventListener('pointerdown', () => { fallbackPointerHeld = true; });
        const releaseFallbackPointer = () => { fallbackPointerHeld = false; };
        window.addEventListener('pointerup', releaseFallbackPointer, true);
        window.addEventListener('pointercancel', releaseFallbackPointer, true);
        window.addEventListener('blur', releaseFallbackPointer);

        function appendFallbackOutput(data) {
            const top = fallbackOutput.scrollTop;
            const following = !fallbackPointerHeld &&
                fallbackOutput.scrollHeight - fallbackOutput.clientHeight - top <= 1;
            // New text nodes retain any selection in older output.
            fallbackOutput.appendChild(document.createTextNode(stripAnsi(data)));
            fallbackOutput.scrollTop = following ? fallbackOutput.scrollHeight : top;
        }

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
                    minimumContrastRatio: 4.5,
                    overviewRulerWidth: 0,
                    theme: {
                        background: '__THEME_BG__',
                        foreground: '__THEME_FG__',
                        cursor: '__THEME_CURSOR__',
                        selectionBackground: '__THEME_SELECTION__'
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
                appendFallbackOutput(event.data);
            }
        };

        socket.onclose = () => {
            const msg = '\r\n[Terminal session closed]\r\n';
            if (term) term.write(msg);
            else appendFallbackOutput(msg);
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


def get_ai_html() -> str:
    t = _resolve_ai_theme()
    return (
        HTML_CONTENT_TEMPLATE
        .replace("__THEME_BG__", t["bg"])
        .replace("__THEME_FG__", t["fg"])
        .replace("__THEME_CURSOR__", t["cursor"])
        .replace("__THEME_SELECTION__", t["selection"])
    )


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
            f'for /L %i in (0,0,1) do ( {invocation} "{target_dir}" & '
            f'timeout /t 1 /nobreak >nul )\r\n'
        )

    async def ws_handler(self, websocket):
        target_dir = os.path.abspath(self.target_dir)
        if PtyProcess:
            try:
                self.pty = PtyProcess.spawn("cmd.exe", cwd=target_dir, dimensions=(30, 120))
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
        from src.modules.ai_prompt_context import PromptInputTracker
        prompt_tracker = PromptInputTracker(target_dir, source="assistant")

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
                    cache_dir = Path(target_dir) / ".mcu_flasher_build_cache"
                    if cache_dir.is_dir():
                        sig = cache_dir / ".ai_ready_signal"
                    else:
                        sig = Path(target_dir) / ".ai_ready_signal"
                    sig.write_text(
                        json.dumps({"time": time.time(), "reason": reason}),
                        encoding="utf-8",
                    )
                    try:
                        from main.core.file_utils import hide_hidden_attribute
                        hide_hidden_attribute(sig)
                    except Exception:
                        pass
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
                    and (
                        (elapsed >= 3.0 and quiet_for >= 1.0)
                        or (elapsed >= 6.5)
                    )
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
                    try:
                        prompt_tracker.feed(input_data)
                    except Exception:
                        pass
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
                    self.wfile.write(get_ai_html().encode('utf-8'))
                elif self.path in ('/xterm/xterm.css', '/xterm/xterm.js', '/xterm/xterm-addon-fit.js'):
                    asset = SCRIPT_DIR / 'src/assets/xterm' / self.path.rsplit('/', 1)[1]
                    try:
                        data = asset.read_bytes()
                    except OSError:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header('Content-type', 'text/css' if asset.suffix == '.css' else 'application/javascript')
                    self.end_headers()
                    self.wfile.write(data)
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
        try:
            (Path(self.target_dir) / ".ai_ready_signal").unlink(missing_ok=True)
            (Path(self.target_dir) / ".mcu_flasher_build_cache" / ".ai_ready_signal").unlink(missing_ok=True)
        except Exception:
            pass


def find_free_pair(start_port=8765):
    for p in range(start_port, start_port + 200, 2):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s1, \
                 socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
                s1.bind(('127.0.0.1', p))
                s2.bind(('127.0.0.1', p + 1))
                return p
        except Exception:
            pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]
    except Exception:
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
    # When spawned by the main GUI via --launch-ai, the GUI handles pre-flight
    # connection checks and user confirmation. Child process must not display
    # an unparented blocking MessageBox or abort embedding.
    is_gui_child = any(arg == "--launch-ai" for arg in sys.argv)
    if not is_gui_child and not check_internet_connection():
        msg = (
            "Could not verify an active internet connection.\n\n"
            "OpenCode AI Assistant typically requires an active internet connection to communicate with cloud AI services.\n\n"
            "Would you like to continue launching anyway?"
        )
        if sys.platform == "win32":
            try:
                ret = ctypes.windll.user32.MessageBoxW(
                    0, msg, "Internet Connection Warning", 0x24  # MB_ICONQUESTION | MB_YESNO
                )
                if ret != 6:  # IDYES == 6
                    return
            except Exception:
                pass
        else:
            return

    _pre_hide_console_for_conpty()
    target_dir = os.path.abspath(target_directory) if target_directory else os.getcwd()
    # Remove a stale marker from an earlier process. The new process publishes
    # a fresh marker after WebView/PTY readiness, preventing old timestamps from
    # racing the main GUI's loading overlay.
    try:
        (Path(target_dir) / ".ai_ready_signal").unlink(missing_ok=True)
        (Path(target_dir) / ".mcu_flasher_build_cache" / ".ai_ready_signal").unlink(missing_ok=True)
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
        from private_python_guard import get_private_python_exe
        py_exe = str(get_private_python_exe(prefer_pythonw=True))
    except Exception:
        py_exe = sys.executable

    try:
        active_ai_proc = subprocess.Popen(
            [py_exe, script_path, "--launch-ai", target_directory],
            cwd=target_directory,
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
            # Do not synchronously wait for a large WebView2/OpenCode process
            # tree while the Tk event loop is switching projects.  taskkill is
            # forceful; it can finish tearing down the descendants in parallel
            # while the replacement process starts in the new project.
            subprocess.Popen(
                ["taskkill", "/F", "/T", "/PID", str(active_ai_proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
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


if __name__ == "__main__":
    # The workspace launches "--launch-ai <folder>"; a bare run opens the current folder.
    arguments = sys.argv[2:] if sys.argv[1:2] == ["--launch-ai"] else sys.argv[1:]
    run_standalone_ai(arguments[0] if arguments else os.getcwd())
