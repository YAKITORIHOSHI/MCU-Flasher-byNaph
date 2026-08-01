"""
MCU Flash GUI - Embedded xterm.js Terminal via pywebview (Monaco Engine Architecture)

Uses pywebview (Microsoft Edge WebView2) to render xterm.js in a native Python desktop window,
connected to a ConPTY (pywinpty) backend. This is identical to how Monaco Editor is embedded.
"""

import os
import sys
import json
import time
import socket
import asyncio
import threading
import subprocess
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler

# Ensure workspace 'env' site-packages is on sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_DIR = SCRIPT_DIR / "env"
ENV_SITE_PACKAGES = ENV_DIR / "Lib" / "site-packages"
if ENV_SITE_PACKAGES.exists() and str(ENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(ENV_SITE_PACKAGES))

# Check dependencies
try:
    # pyrefly: ignore [missing-import]
    from winpty import PtyProcess
except ImportError:
    print("[ERROR] pywinpty module not found. Run runThisOnWindows.vbs first.")
    sys.exit(1)

try:
    # pyrefly: ignore [missing-import]
    import websockets
except ImportError:
    print("[NOTICE] Installing missing dependency: websockets...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets"])
    # pyrefly: ignore [missing-import]
    import websockets

try:
    # pyrefly: ignore [missing-import]
    import webview
except ImportError:
    print("[NOTICE] Installing missing dependency: pywebview...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pywebview"])
    # pyrefly: ignore [missing-import]
    import webview


# HTML Page containing xterm.js (Microsoft VS Code Terminal Renderer)
HTML_CONTENT = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>OpenCode AI Terminal</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" />
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-webgl@0.16.0/lib/xterm-addon-webgl.js"></script>
    <style>
        html, body {
            margin: 0;
            padding: 0;
            width: 100%;
            height: 100%;
            background-color: #000000 !important;
            overflow: hidden !important;
        }
        #terminal-container {
            width: 100%;
            height: 100%;
            background-color: #000000 !important;
            overflow: hidden !important;
        }
        .xterm, .xterm-viewport, .xterm-screen {
            background-color: #000000 !important;
            overflow-y: hidden !important;
        }
        ::-webkit-scrollbar {
            display: none !important;
            width: 0px !important;
            height: 0px !important;
        }
    </style>
</head>
<body>
    <div id="terminal-container"></div>
    <script>
        const term = new Terminal({
            cursorBlink: true,
            cursorStyle: 'block',
            fontSize: 14,
            fontFamily: 'Consolas, "Courier New", monospace',
            overviewRulerWidth: 0,
            theme: {
                background: '#000000',
                foreground: '#cccccc',
                cursor: '#ffffff',
                selectionBackground: '#264f78',
                black: '#000000',
                red: '#cd3131',
                green: '#0dbc79',
                yellow: '#e5e510',
                blue: '#2472c8',
                magenta: '#bc3fbc',
                cyan: '#11a8cd',
                white: '#e5e5e5',
                brightBlack: '#666666',
                brightRed: '#f14c4c',
                brightGreen: '#23d18b',
                brightYellow: '#f5f543',
                brightBlue: '#3b8eea',
                brightMagenta: '#d670d6',
                brightCyan: '#29b8db',
                brightWhite: '#e5e5e5'
            }
        });

        const fitAddon = new FitAddon.FitAddon();
        term.loadAddon(fitAddon);

        const container = document.getElementById('terminal-container');
        term.open(container);
        
        function fitTerminal() {
            fitAddon.fit();
            sendResize();
        }

        setTimeout(fitTerminal, 50);
        setTimeout(fitTerminal, 200);

        // Connect WebSocket to Python ConPTY Backend
        const wsPort = parseInt(window.location.port || "80") + 1;
        const wsUrl = `ws://${window.location.hostname}:${wsPort}/ws`;
        const socket = new WebSocket(wsUrl);

        socket.onopen = () => {
            sendResize();
        };

        socket.onmessage = (event) => {
            term.write(event.data);
        };

        socket.onclose = () => {
            term.write('\\r\\n\\x1b[31m[Terminal session closed]\\x1b[0m\\r\\n');
        };

        term.onData((data) => {
            if (socket.readyState === WebSocket.OPEN) {
                socket.send(JSON.stringify({ type: 'input', data: data }));
            }
        });

        function sendResize() {
            if (socket.readyState === WebSocket.OPEN) {
                socket.send(JSON.stringify({
                    type: 'resize',
                    cols: term.cols,
                    rows: term.rows
                }));
            }
        }

        window.addEventListener('resize', () => {
            fitAddon.fit();
            sendResize();
        });
    </script>
</body>
</html>
"""


class TerminalServer:
    def __init__(self, port=8765, target_dir=None, command="opencode"):
        self.port = port
        self.target_dir = target_dir or os.getcwd()
        self.command = command
        self.pty = None
        self.loop = None

    async def ws_handler(self, websocket):
        target_dir = os.path.abspath(self.target_dir)
        self.pty = PtyProcess.spawn("cmd.exe", dimensions=(30, 120))

        # Initial command setup
        if self.command:
            self.pty.write(f'cd /d "{target_dir}" && set PATH=%APPDATA%\\npm;%PATH% && cls && {self.command}\r\n')

        # Thread: PTY -> WebSocket
        def pty_read_loop():
            while True:
                try:
                    data = self.pty.read(4096)
                    if data and websocket:
                        asyncio.run_coroutine_threadsafe(websocket.send(data), self.loop)
                except Exception:
                    break

        threading.Thread(target=pty_read_loop, daemon=True).start()

        # Handle WebSocket -> PTY
        try:
            async for message in websocket:
                msg = json.loads(message)
                if msg.get('type') == 'input':
                    self.pty.write(msg['data'])
                elif msg.get('type') == 'resize':
                    cols = msg.get('cols', 120)
                    rows = msg.get('rows', 30)
                    self.pty.setwinsize(rows, cols)
        except Exception:
            pass
        finally:
            if self.pty:
                try: self.pty.close()
                except Exception: pass

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

        httpd = HTTPServer(('127.0.0.1', self.port), Handler)
        httpd.serve_forever()

    async def start_async(self):
        threading.Thread(target=self.run_http_server, daemon=True).start()
        self.loop = asyncio.get_running_loop()

        ws_port = self.port + 1
        async with websockets.serve(self.ws_handler, '127.0.0.1', ws_port):
            await asyncio.Future()  # Run server indefinitely


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


def main():
    port = find_free_pair(8765)
    server = TerminalServer(port=port, command="opencode")

    # Start Async WebSockets & HTTP Server in background thread
    def run_server_loop():
        asyncio.run(server.start_async())

    threading.Thread(target=run_server_loop, daemon=True).start()
    time.sleep(0.5)

    # Launch native desktop Python window using pywebview (Monaco Editor architecture)
    url = f"http://127.0.0.1:{port}"
    print(f"[INFO] Opening pywebview Python window at {url}...")

    window = webview.create_window(
        title="MCU Flash GUI - OpenCode AI Terminal",
        url=url,
        width=1040,
        height=680,
        resizable=True,
        background_color="#000000"
    )
    webview.start()


if __name__ == "__main__":
    main()