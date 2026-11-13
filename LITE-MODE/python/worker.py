#!/usr/bin/env python3
"""Headless JSON-lines adapter to an unchanged MCU Flasher backend snapshot."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from isolation import child_setup, environment, protect_original, state_directory
from preparation import main_core_directory
from search import BoardSearch, rank_libraries

LITE = Path(__file__).resolve().parents[1]
ORIGINAL = LITE.parent
SOURCE_EXTENSIONS = {".ino", ".h", ".hpp", ".cpp", ".c"}
MAX_FRAME = 256 * 1024


def owned_portal_pid(process, pid):
    """Windows venv python.exe may be a redirector, not the GUI's real PID."""
    if type(pid) is not int or not 0 < pid < 0xFFFFFFFF:
        return False
    if pid == process.pid:
        return True
    if sys.platform != "win32":
        return False
    import psutil
    try:
        current = psutil.Process(pid)
        for _ in range(8):
            current = current.parent()
            if current is None:
                break
            if current.pid == process.pid:
                return True
    except psutil.Error:
        pass
    return False


def host_check():
    if sys.platform == "win32":
        if platform.machine().lower() not in {"amd64", "x86_64"}:
            raise RuntimeError("Lite currently supports Windows x64 only")
        if sys.getwindowsversion().major < 10:
            raise RuntimeError("Lite requires Windows 10 or 11")
    elif sys.platform.startswith("linux"):
        release = platform.freedesktop_os_release()
        if release.get("ID") != "ubuntu" or not re.fullmatch(r"\d+\.\d+", release.get("VERSION_ID", "")):
            raise RuntimeError("Lite supports native Ubuntu 24.04+ and Windows 10/11 x64 only")
        if tuple(map(int, release["VERSION_ID"].split("."))) < (24, 4):
            raise RuntimeError("Lite requires Ubuntu 24.04 or newer")
    else:
        raise RuntimeError("Lite supports Ubuntu and Windows only")
    try:
        import psutil
        cores = psutil.cpu_count(logical=False) or psutil.cpu_count() or 1
    except ImportError:
        cores = os.cpu_count() or 1
    if cores < 4:
        raise RuntimeError("The reused MCU Flasher tool backend requires at least four CPU cores")


def snapshot_engine(original, state):
    """Copy code, never settings/caches/runtimes, into the private backend root."""
    engine = state / "engine"
    paths = list((original / "main").rglob("*.py"))
    paths += list((original / "src/modules").glob("*.py"))
    paths += list((original / "src/dbs").glob("*.py"))
    paths += list((original / "src").glob("*.py"))
    paths += list((original / "src/assets/icons").rglob("*.svg"))
    if (original / "src/assets/mcu_icon.ico").is_file():
        paths.append(original / "src/assets/mcu_icon.ico")
    if not (original / "main/web_bridge.py").is_file():
        raise RuntimeError("Keep LITE-MODE beside the main application's main/ and src/ folders")
    manifest = {}
    for source in paths:
        if source.is_symlink() or "__pycache__" in source.parts:
            continue
        destination = engine / source.relative_to(original)
        data = source.read_bytes()
        manifest[source.relative_to(original).as_posix()] = hashlib.sha256(data).hexdigest()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or destination.read_bytes() != data:
            temporary = destination.with_suffix(".lite-tmp")
            temporary.write_bytes(data)
            temporary.replace(destination)
    manifest_path = state / "engine-manifest.json"
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        previous = {}
    for name, digest in previous.items():
        relative = Path(name)
        if name in manifest or relative.is_absolute() or ".." in relative.parts or relative.suffix not in {".py", ".svg", ".ico"}:
            continue
        target = engine / relative
        if (target.is_file() and not target.is_symlink() and target.resolve().is_relative_to(engine.resolve())
                and hashlib.sha256(target.read_bytes()).hexdigest() == digest):
            target.unlink()
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    site = (
        f"import sys\nsys.path.insert(0, {str(LITE / 'python')!r})\n"
        "from isolation import child_setup\nchild_setup()\n"
    )
    (engine / "sitecustomize.py").write_text(site, encoding="utf-8")
    # Package discovery must never inherit the main downloader's output folder.
    settings = engine / "src/dbs/arduino_browser_settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"download_dir": str(state / "downloads")}), encoding="utf-8")
    return engine


def snapshot_sources(source, state):
    """A bounded root-only snapshot; original source and caches stay read-only."""
    source = Path(source).expanduser().resolve(strict=True)
    if source.is_file():
        source = source.parent
    if not source.is_dir():
        raise ValueError("Choose an existing sketch folder")
    contents = {}
    size = 0
    for entry in source.iterdir():
        if entry.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
            raise ValueError(f"Linked sources are not supported: {entry.name}")
        if not entry.is_file():
            continue
        if entry.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("A source file exceeds Lite's 8 MiB snapshot budget")
        contents[entry.name] = entry.read_bytes()
        size += len(contents[entry.name])
        if len(contents) > 128 or size > 8 * 1024 * 1024:
            raise ValueError("Root sources exceed Lite's 128-file / 8 MiB snapshot budget")
    if not any(Path(name).suffix.lower() in {".ino", ".cpp", ".c"} and data.strip()
               for name, data in contents.items()):
        raise ValueError("Choose a folder containing a non-empty root .ino, .cpp or .c file")
    # Refuse a changing source rather than publishing a knowingly mixed snapshot.
    for name, data in contents.items():
        if (source / name).read_bytes() != data:
            raise ValueError("Source changed during capture. Try the action again")
    key = hashlib.sha256(os.path.normcase(str(source)).encode()).hexdigest()[:20]
    destination = state / "projects" / key / source.name
    destination.mkdir(parents=True, exist_ok=True)
    for entry in destination.iterdir():
        if entry.is_file() and entry.suffix.lower() in SOURCE_EXTENSIONS and entry.name not in contents:
            entry.unlink()
    for name, data in contents.items():
        target = destination / name
        if not target.is_file() or target.read_bytes() != data:
            temporary = target.with_suffix(target.suffix + ".lite-tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
    return source, destination, sorted(contents)


class Output:
    def __init__(self):
        self.stream = sys.stdout
        self.queue = queue.Queue(maxsize=1024)
        self.broken = threading.Event()
        self.thread = threading.Thread(target=self._write, name="LiteProtocol", daemon=True)
        self.thread.start()

    def send(self, event, data=None):
        while not self.broken.is_set():
            try:
                self.queue.put({"event": event, "data": data}, timeout=.2)
                return
            except queue.Full:
                continue

    def _write(self):
        while True:
            record = self.queue.get()
            try:
                if self.broken.is_set():
                    continue
                self.stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                self.stream.flush()
            except (OSError, BrokenPipeError):
                # Continue discarding queued records so shutdown can flush even
                # if the UI has vanished while a programmer is finishing.
                self.broken.set()
            finally:
                self.queue.task_done()


class Worker:
    def __init__(self, state, output, *, main_core=None):
        self.state, self.output = state, output
        self.main_core = Path(main_core).resolve() if main_core is not None else None
        self._action_lock = threading.RLock()
        self.board_search = BoardSearch()
        self.source = None
        self.resource_busy = False
        self.process = None
        self.catalog_thread = None
        self.developer_process = None
        self.developer_ready = False
        self.developer_pid = None
        self.closing = False
        self.cancel = threading.Event()
        self.last_ports = []
        self.protected = protect_original(ORIGINAL)
        if self.main_core is not None:
            self.protected.append(self.main_core)
        from main.web_bridge import MCUWebBackendAPI, SUPPORTED_BOARDS
        self.boards = SUPPORTED_BOARDS
        # Official PlatformIO catalog, bundled for offline board selection.
        # It is a picker catalog, never evidence that a board is attached or a
        # toolchain is installed. Native installed manifests override it later.
        from main.core.board_catalog import _native_board_entry
        catalog = {}
        records = json.loads((LITE / "assets/boards.json").read_text(encoding="utf-8"))
        for record in records:
            if not record.get("frameworks"):
                continue
            record = dict(record)
            record["frameworks"] = set(record["frameworks"])
            record["hwids"] = record["arduino_defines"] = set()
            name = f"{record['name']} [{record['platform']}:{record['id']}]"
            catalog[name] = _native_board_entry(record)
        self.boards.replace({**catalog, **dict(self.boards)})
        owner = self

        class LiteBackend(MCUWebBackendAPI):
            def emit(self, name, data=None):
                # No Qt windows, notifications database, hardware-state generation
                # or journal persistence is driven by this view's event transport.
                if name == "boards:updated" and isinstance(data, dict) and isinstance(data.get("boards"), dict):
                    owner.boards.replace({**dict(owner.boards), **data["boards"]})
                if name not in {"console:log", "serial:log", "console:clear", "serial:clear", "operation:phase", "serial:status", "console:progress", "notification"}:
                    return
                if name == "notification" and isinstance(data, dict):
                    data = {key: str(data.get(key, ""))[:2048] for key in ("title", "message", "type")}
                if name in {"console:log", "serial:log"} and isinstance(data, dict):
                    data = dict(data)
                    text = str(data.get("text", ""))
                    if name == "console:log" and "Compatible Devices" in text and "analysis updated" in text:
                        return  # This optional main-workspace tab is not in Lite.
                    # Legacy status emoji may occur in backend messages. Preserve
                    # all user serial bytes, replacing only generated status marks.
                    if name == "console:log" or not data.get("stream", False):
                        # Keep the backend's character-counted table padding.
                        text = re.sub("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", " ", text)
                    data["text"] = text
                    if name == "console:log" and data.get("replace_pattern") and not data.get("progress_key"):
                        data["progress_key"] = f"{getattr(self, '_op_session_id', 0)}:pattern:{data['replace_pattern']}"
                output.send(name, data)

            def _sync_project_hardware_state(self, *args, **kwargs):
                pass  # Lite has no AI-generated project instructions or live cache journal.

            def _get_qt_signals(self):
                return None

        self.api = LiteBackend()
        self.api.reset_on_baud_change = False
        self.api.clear_console_on_action = False
        self.api.skip_compile = True
        self.api._package_event_root = str(state / "package-events")
        # AI review/editor services are outside Lite's monitor-only scope.
        self.api.ai_watcher = None
        self.api.ai_review_manager = None

    def busy(self):
        return (self.resource_busy or self.api.is_busy or self.api._operation_worker_alive()
                or self.api._catalog_refresh_running)

    def log(self, text, tag="info"):
        self.output.send("console:log", {"text": text, "tag": tag, "newline": True})

    def status(self):
        self.output.send("state", {
            "busy": self.busy(), "board": self.api.current_board,
            "port": self.api.current_port, "baud": self.api.current_baud,
            "monitor": self.api.serial_running, "phase": self.api.active_operation or "idle",
            "developer_access": self.developer_process is not None and self.developer_process.poll() is None,
            "state_path": str(self.state), "source": str(self.source or ""),
        })

    def ports(self):
        ports = self.api._scan_ports()
        devices = {p["device"] for p in ports}
        if self.api.current_port and self.api.current_port not in devices and not self.api.is_busy:
            self.api._stop_serial_monitor()
            self.api.current_port = ""
            self.log("Selected port disconnected", "warning")
        self.last_ports = ports
        self.output.send("ports", ports)

    def select_project(self, path):
        source = Path(path).expanduser().resolve(strict=True)
        if source.is_relative_to(self.state):
            raise ValueError("Choose an original sketch folder, outside Lite's private state")
        source, staged, files = snapshot_sources(source, self.state)
        if source not in self.protected:
            self.protected.append(source)
        self.source = source
        self.api.sketch_dir_path = staged
        self.api.active_file_path = str(staged / files[0])
        self.api.modified_files.clear()
        self.api._restore_project_compile_state()
        from main.core.config import add_recent_project
        add_recent_project(str(source))
        self.output.send("project", {"path": str(source), "name": source.name, "files": files})
        self.history(expected_project=str(source))
        self.log(f"Opened {source.name}. {len(files)} root source files captured; originals stay read-only.")

    def history(self, *, expected_project=None, expected_board=None):
        from main.core.config import load_recent_projects, load_recent_boards
        projects = load_recent_projects()[:10]
        boards = [name for name in load_recent_boards() if name in self.boards][:5]
        self.output.send("history", {"projects": projects, "boards": boards})
        if ((expected_project and expected_project not in projects)
                or (expected_board and expected_board not in boards)):
            self.log("The selection is active, but recent history could not be saved. Check Lite's settings permissions.", "warning")

    def require_action_ready(self, action):
        if self.source is None:
            raise ValueError("Open a sketch folder first")
        if not self.api.current_board or self.api.current_board not in self.boards:
            raise ValueError("Select a board first")
        from main.core.target_profile import target_problem
        problem = target_problem(self.api._resolve_board_info(), arduino_sketch=False)
        if problem:
            raise ValueError(problem)
        if action == "upload":
            if not self.api.current_port:
                raise ValueError("Select a connected serial port before Upload")
            if self.api.current_port not in {item["device"] for item in self.api._scan_ports()}:
                raise ValueError("The selected port disconnected. Select a connected port before Upload")

    def pio_environment(self):
        core = self.state / "platformio" if sys.platform != "win32" else self.state / "engine/src/.platformio-mcu-gui"
        env = dict(os.environ)
        for name, directory in {
            "PLATFORMIO_CORE_DIR": core, "PLATFORMIO_PLATFORMS_DIR": core / "platforms",
            "PLATFORMIO_PACKAGES_DIR": core / "packages", "PLATFORMIO_CACHE_DIR": core / ".cache",
            "PLATFORMIO_GLOBALLIB_DIR": core / "lib",
        }.items():
            directory.mkdir(parents=True, exist_ok=True)
            env[name] = str(directory)
        env["PLATFORMIO_SETTING_ENABLE_TELEMETRY"] = "no"
        env["PLATFORMIO_SETTING_ENABLE_CACHE"] = "yes"
        if self.main_core is not None:
            env["MCU_LITE_MAIN_CORE"] = str(self.main_core)
        else:
            env.pop("MCU_LITE_MAIN_CORE", None)
        return env

    def run_pio(self, arguments, *, timeout=1200):
        return self.run_tool(["-m", "platformio", *arguments], timeout=timeout)

    def run_tool(self, arguments, *, timeout=1200):
        env = self.pio_environment()
        for key in ("MCU_FLASHER_OFFLINE_RUNTIME", "MCU_FLASHER_WORKSPACE_RUNTIME", "PIP_NO_INDEX"):
            env.pop(key, None)
        options = {"creationflags": 0x08000000} if sys.platform == "win32" else {"start_new_session": True}
        process = subprocess.Popen([sys.executable, "-B", "-u", *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", env=env, cwd=self.state, **options)
        self.process = process
        # A draining reader lets the owner enforce elapsed budgets even when a
        # downloader is silent. Cancellation is of this process tree only.
        lines = queue.Queue(maxsize=256)
        abandoned = threading.Event()
        def publish(line):
            while not abandoned.is_set():
                try:
                    lines.put(line, timeout=.1)
                    return
                except queue.Full:
                    continue
        def drain():
            try:
                while line := process.stdout.readline(8192):
                    publish(line)
            finally:
                publish(None)
        threading.Thread(target=drain, daemon=True).start()
        deadline = time.monotonic() + timeout
        try:
            while True:
                if self.cancel.is_set() or time.monotonic() >= deadline:
                    self.stop_owned_process(process)
                    raise RuntimeError("Resource operation cancelled" if self.cancel.is_set() else f"Resource operation exceeded its {timeout:g}-second budget")
                try:
                    line = lines.get(timeout=.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                self.log(line.rstrip())
            code = process.wait(timeout=10)
            if code:
                raise RuntimeError(f"PlatformIO exited with code {code}; see Build monitor")
        finally:
            abandoned.set()
            if process.poll() is None:
                self.stop_owned_process(process)
            process.stdout.close()
            self.process = None

    @staticmethod
    def stop_owned_process(process):
        if process.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000, timeout=15)
        else:
            import signal
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)

    def prepare_board(self, info):
        from main.core.target_profile import target_problem
        problem = target_problem(info, arduino_sketch=False)
        if problem:
            raise ValueError(problem)
        target = self.state / "prepare"
        target.mkdir(parents=True, exist_ok=True)
        values = [str(info[key]) for key in ("platform", "board", "framework")]
        if any("\n" in value or "\r" in value for value in values):
            raise ValueError("Invalid target definition")
        config = target / "platformio.ini"
        text = "[env:lite]\nplatform = " + values[0] + "\nboard = " + values[1] + "\nframework = " + values[2] + "\n"
        protocol = str(info.get("upload_protocol") or "")
        if protocol:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", protocol):
                raise ValueError("Invalid upload protocol")
            text += "upload_protocol = " + protocol + "\n"
        if not config.is_file() or config.read_text(encoding="utf-8") != text:
            config.write_text(text, encoding="utf-8")
        self.log("Checking compiler, framework and upload tools for " + values[1])
        self.output.send("console:progress", {"action": "Checking build and upload tools"})
        result = target / "result.json"
        self.run_tool([str(LITE / "python/preparation.py"), "--config", str(config), "--result", str(result)])
        report = json.loads(result.read_text(encoding="utf-8"))
        # Publish the real installed manifests before computing a build key.
        # An asynchronous catalog change must never race firmware certification.
        self.api.refresh_board_catalog(invalidate_parsed=True)
        deadline = time.monotonic() + 60
        while self.api._catalog_refresh_running:
            if self.cancel.is_set():
                raise InterruptedError("Preparation cancelled before the action started")
            if time.monotonic() > deadline:
                raise RuntimeError("Board catalog is still loading; try again when it finishes")
            time.sleep(.05)
        return report

    def resources(self, request, *, after=None):
        action = request["cmd"]
        board = self.api.current_board
        info = dict(self.api._resolve_board_info(board)) if action == "board_install" and board else None
        if action == "board_install" and not info:
            raise ValueError("Select a board first")
        self.resource_busy = True
        self.cancel.clear()
        self.output.send("resource:busy", True)
        def work():
            handed_off = False
            try:
                if action == "library_search":
                    query = str(request.get("query", ""))[:200]
                    parameters = urllib.parse.urlencode({"query": "type:library " + query, "limit": 100, "page": 1, "sort": "relevance"})
                    url = "https://api.registry.platformio.org/v3/search?" + parameters
                    with urllib.request.urlopen(url, timeout=20) as response:
                        raw = response.read(2 * 1024 * 1024 + 1)
                    if len(raw) > 2 * 1024 * 1024:
                        raise ValueError("Registry response exceeds the 2 MiB budget")
                    results = []
                    for item in json.loads(raw).get("items", [])[:100]:
                        owner = item.get("owner", {}).get("username", "")
                        name = str(item.get("name", ""))
                        version = str(item.get("version", {}).get("name", ""))
                        results.append({"name": name, "owner": owner, "version": version,
                            "description": str(item.get("description", ""))[:500],
                            "spec": f"{owner}/{name}@{version}"})
                    self.output.send("libraries", {"query": query, "items": rank_libraries(results, query)})
                elif action == "library_install":
                    spec = str(request.get("spec", ""))
                    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_. +()-]+@[A-Za-z0-9_.+-]+", spec):
                        raise ValueError("Select a versioned library from the registry results")
                    self.log(f"Installing {spec} into Lite's private library store")
                    self.run_pio(["pkg", "install", "--global", "--library", spec])
                    self.output.send("resource:installed", {"spec": spec})
                else:
                    self.prepare_board(info)
                    self.output.send("resource:installed", {"spec": board})
                if after is None:
                    self.log("Resource operation completed", "success")
                else:
                    # Stop and the handoff share a lock: a late Stop either
                    # cancels preparation or reaches the original operation.
                    with self._action_lock:
                        if self.cancel.is_set():
                            raise InterruptedError("Preparation cancelled before the action started")
                        if self.api.current_board != board:
                            raise RuntimeError("The selected board changed; try the action again")
                        self.require_action_ready(after)
                        snapshot_sources(self.source, self.state)
                        if after == "upload":
                            self.api.skip_compile = True
                        {"compile": self.api.compile_sketch, "upload": self.api.upload_sketch}[after]()
                        self.resource_busy = False
                        self.output.send("resource:busy", False)
                        self.status()
                        handed_off = True
            except Exception as exc:
                self.output.send("error", {"message": str(exc)})
                self.log(str(exc), "error")
            finally:
                if not handed_off:
                    with self._action_lock:
                        self.resource_busy = False
                        self.output.send("resource:busy", False)
                        self.status()
        self.catalog_thread = threading.Thread(target=work, name="LiteResources", daemon=True)
        self.catalog_thread.start()

    def dispatch(self, request):
        with self._action_lock:
            return self._dispatch(request)

    def open_developer_access(self):
        process = self.developer_process
        if process is not None and process.poll() is None:
            if self.developer_ready:
                self.output.send("developer:ready", {"pid": self.developer_pid})
            return
        log_path = self.state / "developer-access.log"
        log = log_path.open("wb")
        try:
            command = [sys.executable, "-B", str(LITE / "python/developer.py")]
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
            process = subprocess.Popen(command, env=environment(self.state), cwd=self.state / "engine",
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, **options)
        except Exception:
            log.close()
            raise
        self.developer_process = process
        self.developer_ready = False
        self.developer_pid = None
        self.output.send("developer:status", True)
        self.log("Opening Developer Access...", "info")

        def startup_slow():
            if self.developer_process is process and process.poll() is None and not self.developer_ready:
                self.output.send("error", {"message": f"Developer Access is taking longer to open. Details: {log_path}"})

        notice = threading.Timer(20, startup_slow)
        notice.daemon = True
        notice.start()

        def read_ready():
            line = process.stdout.readline(256)
            try:
                frame = json.loads(line)
                if (isinstance(frame, dict) and set(frame) == {"event", "pid"}
                        and frame["event"] == "ready" and process.poll() is None
                        and owned_portal_pid(process, frame["pid"])):
                    self.developer_pid = frame["pid"]
                    self.developer_ready = True
                    notice.cancel()
                    self.output.send("developer:ready", {"pid": self.developer_pid})
                    self.log("Developer Access is ready.", "info")
            except (ValueError, TypeError):
                pass

        threading.Thread(target=read_ready, name="LiteDeveloperReady", daemon=True).start()

        def watch():
            result = process.wait()
            notice.cancel()
            log.close()
            if self.developer_process is process:
                self.developer_ready = False
                self.developer_pid = None
                self.output.send("developer:status", False)
            if result and not self.closing:
                self.output.send("error", {"message": f"Developer Access could not start. Details: {log_path}"})

        threading.Thread(target=watch, name="LiteDeveloperAccess", daemon=True).start()

    def activate_developer_access(self):
        process = self.developer_process
        if process is None or process.poll() is not None or not self.developer_ready:
            raise RuntimeError("Developer Access is not ready. Retry the five-click gesture.")
        try:
            process.stdin.write(b"activate\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise RuntimeError("Developer Access is closing. Retry the five-click gesture.") from None

    def _dispatch(self, request):
        cmd = request.get("cmd")
        if cmd == "shutdown":
            if self.busy():
                raise RuntimeError("Wait for the current operation before closing Lite")
            return False
        if cmd == "stop":
            if self.resource_busy:
                self.cancel.set()
            else:
                self.api.stop_operation()
            return True
        if cmd == "status":
            self.status()
            return True
        if cmd == "ports":
            self.ports()
            return True
        if cmd == "history":
            self.history()
            return True
        if cmd == "developer_access":
            self.open_developer_access()
            return True
        if cmd == "developer_activate":
            self.activate_developer_access()
            return True
        if cmd == "boards":
            query = str(request.get("query", ""))[:200]
            self.output.send("boards", self.board_search.find(self.boards, query))
            return True
        if self.busy():
            raise RuntimeError("Wait for the current operation to finish")
        if cmd == "project":
            self.select_project(str(request.get("path", "")))
        elif cmd == "board":
            name = str(request.get("name", ""))
            if name not in self.boards:
                raise ValueError("Select an exact board from search results")
            self.api.select_board(name)
            info = self.api._resolve_board_info(name)
            self.output.send("target", {"name": name, "platform": info.get("platform", ""),
                "frameworks": info.get("frameworks") or [info.get("framework", "arduino")],
                "framework": info.get("framework", "arduino")})
            self.history(expected_board=name)
        elif cmd == "framework":
            if not self.api.set_board_framework(self.api.current_board, str(request.get("value", ""))):
                raise ValueError("Framework is not available for this board")
        elif cmd == "port":
            value = str(request.get("value", ""))
            # Select without opening: only the explicit Connect action attaches.
            if value and value not in {item["device"] for item in self.api._scan_ports()}:
                raise ValueError("Port is no longer connected")
            self.api._stop_serial_monitor()
            from main.core.config import claim_serial_port
            if not claim_serial_port(value):
                raise ValueError("Port is in use by another Lite workspace")
            self.api.current_port = value
        elif cmd == "monitor":
            if request.get("connect"):
                if not self.api.current_port:
                    raise ValueError("Select a connected port first")
                self.api._start_serial_monitor()
            else:
                self.api._stop_serial_monitor()
        elif cmd == "baud":
            self.api.set_baud_rate(int(request["value"]), allow_reset=False)
        elif cmd == "upload_speed":
            self.api.set_upload_speed(int(request["value"]))
        elif cmd == "send":
            self.api.serial_send(str(request.get("text", ""))[:16384], str(request.get("ending", "both")))
        elif cmd in {"compile", "upload", "clean"}:
            if self.source is None:
                raise ValueError("Open a sketch folder first")
            if cmd != "clean":
                self.require_action_ready(cmd)
                snapshot_sources(self.source, self.state)
                self.resources({"cmd": "board_install"}, after=cmd)
            else:
                self.api.clean_cache()
        elif cmd in {"library_search", "library_install", "board_install"}:
            self.resources(request)
        else:
            raise ValueError("Unknown Lite command")
        self.status()
        return True

    def close(self):
        self.closing = True
        # The workspace asks users to close the portal first. A lost workspace
        # leaves its portal alive rather than losing an unsaved draft or write.
        self.api._stop_port_monitor.set()
        self.cancel.set()
        if self.resource_busy and self.catalog_thread:
            self.catalog_thread.join(timeout=25)
        if self.api.is_busy:
            self.api.stop_operation()
            # Never terminate a programmer during its write. Lost UI waits for
            # the original backend's flash/restart worker to complete.
            while self.api._operation_worker_alive():
                time.sleep(.2)
        self.api._stop_serial_monitor()
        self.api._serial_send_queue.stop()
        self.api._stop_serial_notifications()
        from main.core.config import set_active_sketch_dir
        set_active_sketch_dir(None)


def main():
    host_check()
    main_core = main_core_directory(ORIGINAL, os.environ)
    state = state_directory()
    state.mkdir(parents=True, exist_ok=True)
    for folder in ("tmp", "data", "cache", "config", "windows-local", "windows-roaming"):
        (state / folder).mkdir(exist_ok=True)
    # Hold a native lock for the worker lifetime: one Lite workspace owns the
    # shared snapshot, settings and packages. A crash releases the lock.
    lock = (state / "workspace.lock").open("a+b")
    if sys.platform == "win32":
        import msvcrt
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("Another Lite workspace is already using this state directory. Close it first.") from exc
    else:
        import fcntl
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Lite workspace is already using this state directory. Close it first.") from exc
    engine = snapshot_engine(ORIGINAL, state)
    os.environ.update(environment(state))
    sys.path.insert(0, str(engine))
    child_setup()
    # Backend startup diagnostics never enter the JSON protocol.
    output = Output()
    sys.stdout = sys.stderr
    from PySide6.QtCore import QCoreApplication
    application = QCoreApplication.instance() or QCoreApplication([])
    worker = Worker(state, output, main_core=main_core)
    worker.api.refresh_board_catalog()
    output.send("ready", {"protocol": 1, "state_path": str(state), "host": platform.system()})
    worker.history()
    worker.ports()
    worker.status()
    try:
        while line := sys.stdin.buffer.readline(MAX_FRAME + 1):
            if len(line) > MAX_FRAME:
                output.send("error", {"message": "Command exceeds the 256 KiB limit"})
                break
            request = None
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("Expected a JSON object")
                if not worker.dispatch(request):
                    break
            except Exception as exc:
                output.send("error", {"message": str(exc)})
                worker.status()
            finally:
                if isinstance(request, dict) and isinstance(request.get("id"), int):
                    output.send("ack", {"id": request["id"]})
            application.processEvents()
    finally:
        worker.close()
        output.send("closed")
        output.queue.join()
        lock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Fatal startup is still machine-readable; Rust keeps its window open.
        print(json.dumps({"event": "fatal", "data": {"message": str(exc)}}), file=sys.__stdout__, flush=True)
        raise SystemExit(1)
