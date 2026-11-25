"""Download one coding agent for this account, on request and without an administrator.

Makers' installers are fetched over HTTPS into a private folder and run as files, so
a transfer that breaks off never runs half of one; they verify their own payloads.
npm agents use npm 10+ from the system, or a private Node.js checked against the
publisher's checksums. Detection afterwards is by files, in main/core/ai_agents.py.
"""
from __future__ import annotations

from collections import deque
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

from main.core import ai_agents
from src.modules.windows_components import NODE_DIST, _fetch_text, _unpack, node_lts_release, published_sha256

MINIMUM_NODE = 20
MAX_SCRIPT = 2 * 1024 * 1024
MAX_ARCHIVE = 256 * 1024 * 1024
INSTALL_SECONDS = 1800
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


class AgentSetupError(RuntimeError):
    """A download that did not finish, with words for the user."""


class Cancelled(AgentSetupError):
    pass


def _check(cancelled):
    if cancelled():
        raise Cancelled("The download was cancelled.")


def clean_line(text: str) -> str:
    """One progress line as text: no colours, control characters or carriage-return drafts."""
    text = _ANSI.sub("", text).rstrip("\r\n").rsplit("\r", 1)[-1]
    return "".join(char for char in text if char.isprintable()).strip()[:300]


def fetch_script(url: str, *, cancelled=lambda: False, opener=None) -> bytes:
    """A complete installer script over HTTPS, or an error; never a web page."""
    import urllib.request
    if not url.startswith("https://"):
        raise AgentSetupError("Installers are downloaded only over HTTPS.")
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(url, headers={"User-Agent": "mcu-flasher-setup/1.0"})
    try:
        with opener(request, timeout=30) as response:
            if not str(response.geturl()).startswith("https://"):
                raise AgentSetupError("The installer was redirected away from HTTPS.")
            data = response.read(MAX_SCRIPT + 1)
    except AgentSetupError:
        raise
    except OSError as exc:
        raise AgentSetupError(f"The installer could not be downloaded: {exc}") from exc
    _check(cancelled)
    if len(data) > MAX_SCRIPT or len(data) < 64:
        raise AgentSetupError("The downloaded installer has an unexpected size.")
    start = data.removeprefix(b"\xef\xbb\xbf").lstrip()[:512].lower()
    if start.startswith((b"<", b"{")) or b"<html" in start:
        raise AgentSetupError("The site answered with a page instead of its installer. Try again later.")
    if url.endswith(".sh") and not start.startswith(b"#!"):
        raise AgentSetupError("The downloaded file is not an installer script.")
    return data


def _end_tree(process):
    """End the installer and everything it started; its handle still owns the process ID."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        from src.modules.opencode_npm import _end_process_tree
        _end_process_tree(process)
    else:
        import signal
        for sign, patience in ((signal.SIGTERM, 3), (signal.SIGKILL, 3)):
            try:
                os.killpg(process.pid, sign)
                process.wait(patience)
                break
            except subprocess.TimeoutExpired:
                continue
            except (OSError, ProcessLookupError):
                break
    try:
        process.wait(5)
    except subprocess.TimeoutExpired:
        pass


def run_streamed(command, *, env, cwd, on_line, cancelled, timeout=INSTALL_SECONDS) -> tuple[int, list[str]]:
    """Run one tool, pass on its lines as they come, and end its whole tree when stopped."""
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
    try:
        process = subprocess.Popen(list(command), cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, **options)
    except OSError as exc:
        raise AgentSetupError(f"{Path(command[0]).name} could not start: {exc}") from exc
    lines, tail = queue.Queue(maxsize=2000), deque(maxlen=12)

    def read():
        for line in process.stdout:
            try:
                lines.put(line, timeout=30)
            except queue.Full:
                return
        lines.put(None)
    threading.Thread(target=read, name="MCU_AgentSetupOutput", daemon=True).start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            _check(cancelled)
            if time.monotonic() > deadline:
                raise AgentSetupError(f"The installer did not finish within {timeout // 60} minutes.")
            try:
                line = lines.get(timeout=.2)
            except queue.Empty:
                if process.poll() is not None and lines.empty():
                    break
                continue
            if line is None:
                break
            text = clean_line(line)
            if text:
                tail.append(text)
                on_line(text)
        # Background helpers may keep the pipe open after the installer itself ended.
        while process.poll() is None:
            _check(cancelled)
            if time.monotonic() > deadline:
                raise AgentSetupError(f"The installer did not finish within {timeout // 60} minutes.")
            time.sleep(.1)
        return process.returncode, list(tail)
    finally:
        _end_tree(process)


def _environment(agent_id, extra=()):
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONHOME", "PYTHONPATH", "PIP_NO_INDEX", "MCU_FLASHER_OFFLINE_RUNTIME",
                          "MCU_FLASHER_WORKSPACE_RUNTIME") and key.lower() != "npm_config_ignore_scripts"}
    env.update(ai_agents.agent_environment(agent_id, env))
    env.update(extra)
    env.setdefault("NO_COLOR", "1")
    return env


def _node_major(node: str) -> int:
    try:
        result = subprocess.run([node, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        match = re.match(r"v(\d+)\.", result.stdout.strip())
        return int(match[1]) if match else 0
    except (OSError, subprocess.SubprocessError):
        return 0


def system_npm() -> list[str] | None:
    """An npm whose Node.js is new enough, as a command, or None."""
    names = (["npm.cmd"], ["node.exe"]) if sys.platform == "win32" else (["npm"], ["node"])
    folders = [None, *(ai_agents._windows_directories() if sys.platform == "win32" else ai_agents._posix_directories())]
    for folder in folders:
        path = None if folder is None else str(folder)
        npm, node = (next((found for name in group if (found := shutil.which(name, path=path))), None) for group in names)
        if npm and node and _node_major(node) >= MINIMUM_NODE:
            return [npm]
    return None


def _download_file(url, target, digest, *, on_line, cancelled):
    import hashlib
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": "mcu-flasher-setup/1.0"})
    sha, received, announced = hashlib.sha256(), 0, 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, Path(target).open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            while block := response.read(256 * 1024):
                _check(cancelled)
                received += len(block)
                if received > MAX_ARCHIVE:
                    raise AgentSetupError("The download is larger than expected.")
                sha.update(block)
                output.write(block)
                percent = received * 100 // total if total else 0
                if percent >= announced + 10:
                    announced = percent
                    on_line(f"Downloading Node.js… {percent}%")
    except AgentSetupError:
        raise
    except OSError as exc:
        raise AgentSetupError(f"Node.js could not be downloaded: {exc}") from exc
    if sha.hexdigest() != digest:
        raise AgentSetupError("The downloaded Node.js does not match its publisher's checksum.")


def node_archive_name(version: str) -> str:
    import platform
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if sys.platform == "win32":
        return f"node-{version}-win-{arch}.zip"
    return f"node-{version}-{'darwin' if sys.platform == 'darwin' else 'linux'}-{arch}.tar.{'gz' if sys.platform == 'darwin' else 'xz'}"


def ensure_private_node(*, on_line, cancelled, fetch=_fetch_text, download=_download_file) -> Path:
    """The folder holding the app's own node and npm, downloaded once and verified."""
    present = ai_agents.node_directory()
    if present is not None:
        return present
    on_line("Preparing Node.js for this agent…")
    try:
        version = node_lts_release(fetch)
        name = node_archive_name(version)
        digest = published_sha256(fetch(f"{NODE_DIST}/{version}/SHASUMS256.txt"), name)
    except (OSError, ValueError) as exc:
        raise AgentSetupError(f"The Node.js release could not be read: {exc}") from exc
    if not digest:
        raise AgentSetupError(f"Node.js publishes no checksum for {name}.")
    root = ai_agents.agents_root() / "node"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".download-", dir=root) as staging:
        archive = Path(staging) / name
        download(f"{NODE_DIST}/{version}/{name}", archive, digest, on_line=on_line, cancelled=cancelled)
        content = Path(staging) / "content"
        content.mkdir()
        _check(cancelled)
        on_line("Unpacking Node.js…")
        if name.endswith(".zip"):
            _unpack(archive, content)
        else:
            with tarfile.open(archive) as bundle:
                bundle.extractall(content, filter="data")
        folder = content / re.sub(r"\.(zip|tar\.[gx]z)$", "", name)
        node = folder / ("node.exe" if sys.platform == "win32" else "bin/node")
        if not node.is_file():
            raise AgentSetupError("The Node.js archive holds no node program.")
        target = root / version
        if not target.exists():
            os.replace(folder, target)
    return root / version if sys.platform == "win32" else root / version / "bin"


def _npm_command(*, on_line, cancelled) -> tuple[list[str], dict]:
    found = system_npm()
    if found:
        return found, {}
    folder = ensure_private_node(on_line=on_line, cancelled=cancelled)
    path = {"PATH": os.pathsep.join([str(folder), os.environ.get("PATH") or os.defpath])}
    if sys.platform == "win32":
        return [str(folder / "npm.cmd")], path
    return [str(folder / "node"), str(folder.parent / "lib/node_modules/npm/bin/npm-cli.js")], path


def install(agent_id: str, *, on_line=lambda _line: None, cancelled=lambda: False, fetch=fetch_script, run=run_streamed) -> str:
    """Download one agent and return its executable; raises AgentSetupError with words otherwise."""
    spec, plan = ai_agents.agent(agent_id), ai_agents.install_plan(agent_id)
    if plan is None:
        raise AgentSetupError(f"{spec.name} is provided by Setup. Run it with --repair if it is missing.")
    existing = ai_agents.find_agent(agent_id)
    if existing:
        return existing
    _check(cancelled)
    on_line(f"Contacting {plan.source}…")
    if plan.kind == "npm":
        command, extra = _npm_command(on_line=on_line, cancelled=cancelled)
        prefix = ai_agents.agent_prefix(agent_id)
        prefix.mkdir(parents=True, exist_ok=True)
        command += ["install", "--global", "--prefix", str(prefix), "--no-audit", "--no-fund", "--no-update-notifier",
                    "--loglevel=error", f"{plan.package}@latest"]
        on_line(f"Installing {plan.package}…")
        code, tail = run(command, env=_environment(agent_id, extra), cwd=prefix, on_line=on_line, cancelled=cancelled)
    else:
        script = fetch(plan.url, cancelled=cancelled)
        with tempfile.TemporaryDirectory(prefix="mcu-agent-") as folder:
            path = Path(folder) / plan.url.rsplit("/", 1)[1]
            path.write_bytes(script)
            if plan.shell == "powershell":
                command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)]
            else:
                program = shutil.which(plan.shell)
                if not program:
                    raise AgentSetupError(f"{plan.shell} is needed to run this installer.")
                command = [program, str(path)]
            code, tail = run([*command, *plan.arguments], env=_environment(agent_id, dict(plan.environment)), cwd=folder,
                             on_line=on_line, cancelled=cancelled)
    found = ai_agents.find_agent(agent_id)
    if found:
        return found
    reason = next((line for line in reversed(tail) if re.search(r"error|fail|cannot|not found|denied", line, re.I)),
                  tail[-1] if tail else "")
    raise AgentSetupError((f"{spec.name} was not installed (exit {code}). {reason}" if code else
                           f"The installer finished, but {spec.name} was not found. {reason}").strip())
