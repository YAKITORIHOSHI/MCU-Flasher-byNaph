#!/usr/bin/env python3
"""Preparation/reuse regressions with fake native packages, no network/devices."""
from pathlib import Path
from contextlib import contextmanager
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from unittest.mock import Mock
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from isolation import environment, child_setup
from preparation import main_core_directory, source_manager, prepare, ensure_package, copy_package
from worker import Worker, snapshot_engine, ORIGINAL
import registry


def write_package(root, kind, name, version, *, owner="platformio", system=None, manifest=None):
    folder = root / ("platforms" if kind == "platform" else "packages") / name
    folder.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "version": version, **(manifest or {})}
    if system:
        data["system"] = system
    (folder / ("platform.json" if kind == "platform" else "package.json")).write_text(json.dumps(data))
    (folder / ".piopm").write_text(json.dumps({"type": kind, "name": name, "version": version,
        "spec": {"owner": owner, "name": name, "requirements": None, "uri": None}}))
    (folder / "payload.txt").write_text(name + "-payload")
    return folder


def tree_hash(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


@contextmanager
def unrelated_tool():
    """An owned sibling outside the resource job/session must survive Stop."""
    options = {"creationflags": 0x08000000} if sys.platform == "win32" else {"start_new_session": True}
    child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    try:
        yield child
    finally:
        child.terminate()
        child.wait(timeout=5)


def main():
    (ROOT / "temp").mkdir(exist_ok=True)
    checks = []
    def check(condition, name):
        assert condition, name
        checks.append(name)
        print("PASS", name)

    with tempfile.TemporaryDirectory(prefix="preparation-", dir=ROOT / "temp") as temporary:
        base = Path(temporary)
        state = base / "state"
        state.mkdir()
        for folder in ("tmp", "data", "cache", "config"):
            (state / folder).mkdir()
        engine = snapshot_engine(ORIGINAL, state)
        os.environ.update(environment(state))
        sys.path.insert(0, str(engine))
        child_setup()
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication([])
        from platformio import util
        from platformio.package.manager.tool import ToolPackageManager
        from platformio.package.manager.platform import PlatformPackageManager
        from platformio.package.meta import PackageSpec
        from platformio.dependencies import get_core_dependencies
        shared = base / "main-store"
        core = state / "platformio"
        (core / "platforms").mkdir(parents=True)
        (core / "packages").mkdir()
        os.environ["PLATFORMIO_CORE_DIR"] = str(core)
        os.environ["PLATFORMIO_PLATFORMS_DIR"] = str(core / "platforms")
        os.environ["PLATFORMIO_PACKAGES_DIR"] = str(core / "packages")
        manifest = {"title": "Lite preparation fixture", "engines": {"platformio": ">=6"},
            "frameworks": {"arduino": {"package": "framework-fixture", "script": "framework.py"}},
            "packages": {
                "compiler-fixture": {"type": "toolchain", "owner": "platformio", "version": "~1.0.0"},
                "framework-fixture": {"type": "framework", "owner": "platformio", "version": "~1.0.0", "optional": True},
                "tool-avrdude": {"type": "uploader", "owner": "platformio", "version": "~1.60300.0", "optional": True}}}
        write_package(shared, "platform", "lite_fixture", "1.0.0", manifest=manifest)
        for name, version in [("compiler-fixture", "1.0.0"), ("framework-fixture", "1.0.0"), ("tool-avrdude", "1.60300.200527"), ("tool-scons", "4.41101.0")]:
            write_package(shared, "tool", name, version, system=[util.get_systype()])
        legacy = shared / "packages/unowned-legacy"
        legacy.mkdir()
        (legacy / "package.json").write_text('{"name":"unowned-legacy","version":"1.0.0"}')
        write_package(shared, "tool", "foreign-tool", "1.0.0", system=["windows_amd64" if sys.platform != "win32" else "linux_x86_64"])
        broken = write_package(shared, "tool", "broken-unrelated-package", "1.0.0")
        (broken / "package.json").unlink()
        source_before = tree_hash(shared)
        config = state / "prepare/platformio.ini"
        config.parent.mkdir()
        config.write_text("[env:lite]\nplatform = platformio/lite_fixture\nboard = fixture_uno\nframework = arduino\n")
        def no_install(*args, **kwargs):
            raise AssertionError("Complete shared fixtures must not download anything")
        with patch.object(ToolPackageManager, "install", no_install), patch.object(PlatformPackageManager, "install", no_install):
            report = prepare(config, core, shared, log=lambda text: None)
            check("tool-avrdude" in report["uploader_packages"], "upload dependency selection includes the optional AVR uploader")
            check(all(item["origin"] == "main" for item in report["packages"]), "all compatible requirements are reused without network installation")
            check(report["changed"], "first preparation reports the newly copied closure")
            check((core / "packages/tool-avrdude/package.json").is_file(), "the missing-upload-package reproduction is repaired before dispatch")
            second = prepare(config, core, shared, log=lambda text: None)
            check(not second["changed"] and all(item["origin"] == "installed" for item in second["packages"]), "a prepared target performs no repeated copies or downloads")
        check(tree_hash(shared) == source_before, "main package files remain byte-for-byte unchanged")
        check(not (legacy / ".piopm").exists(), "legacy shared packages are never claimed by generating metadata")
        own = core / "packages/compiler-fixture/payload.txt"
        own.write_text("independent Lite edit")
        check((shared / "packages/compiler-fixture/payload.txt").read_text() == "compiler-fixture-payload", "reused package files are independent copies, not links to the main app")
        reader = source_manager(ToolPackageManager, shared / "packages")
        check(reader.get_package(PackageSpec("platformio/foreign-tool")) is None, "foreign-host package payloads are excluded from reuse")
        check(reader.get_package(PackageSpec("platformio/tool-avrdude@~1.80100.0")) is None, "incompatible uploader versions are excluded from reuse")
        check(reader.get_package(PackageSpec("platformio/tool-avrdude@~1.60300.0")) is not None, "unrelated incomplete main packages do not prevent compatible uploader reuse")
        local_manager = ToolPackageManager(str(core / "packages"))
        write_package(core / ".lite-reuse/interrupted", "tool", "incomplete-tool", "1.0.0", system=[util.get_systype()])
        check(local_manager.get_package(PackageSpec("platformio/incomplete-tool")) is None, "interrupted reuse stages can never masquerade as installed packages")
        installs = []
        def install_only_missing(spec):
            installs.append(str(spec))
            folder = write_package(core, "tool", "missing-tool", "1.0.0", system=[util.get_systype()])
            local_manager.memcache_reset()
            return local_manager.get_package(spec)
        with patch.object(local_manager, "install", install_only_missing):
            pkg, changed, origin = ensure_package(local_manager, PackageSpec("platformio/missing-tool@~1.0.0"), reader, log=lambda text: None)
            check(pkg is not None and changed and origin == "download" and len(installs) == 1, "only a missing compatible requirement goes through private installation")
        if os.name != "nt":
            linked = write_package(shared, "tool", "linked-tool", "1.0.0", system=[util.get_systype()])
            (linked / "inside-link").symlink_to("payload.txt")
            item = reader.get_package(PackageSpec("platformio/linked-tool"))
            check(copy_package(item, local_manager, log=lambda text: None), "internal native package links can be reused")
            link = core / "packages/linked-tool/inside-link"
            check(link.resolve().is_relative_to(core) and link.read_text() == "linked-tool-payload", "copied package links resolve inside Lite, never to original files")
        check(main_core_directory(ROOT.parent, {"XDG_DATA_HOME": "/fixture/data"}, system="linux", machine="x86_64") == Path("/fixture/data/mcu-flasher/platformio/x86_64"), "main Linux store discovery uses the original XDG base before redirection")
        check(main_core_directory(ROOT.parent, {}, system="win32") == ROOT.parent / "src/.platformio-mcu-gui", "main Windows store discovery uses the original application root")

        # Lifecycle regressions: failures/cancellation never dispatch Upload,
        # and success dispatches the requested action exactly once.
        source = base / "Sketch"
        source.mkdir()
        (source / "Sketch.ino").write_text("void setup() {}\nvoid loop() {}\n")
        class Record:
            def __init__(self): self.events = []
            def send(self, event, data=None): self.events.append((event, data))
        output = Record()
        with patch("serial.tools.list_ports.comports", return_value=[]), \
                patch("main.web_bridge.MCUWebBackendAPI._scan_ports", return_value=[]):
            worker = Worker(state, output)
            worker.select_project(str(source))
            worker.api.current_board = "Arduino Uno [atmelavr:uno]"
            worker.api.current_port = "fixture-port"
            worker.api._scan_ports = lambda: [{"device": "fixture-port"}]
            def join():
                worker.catalog_thread.join(timeout=5)
                assert not worker.catalog_thread.is_alive()
                app.processEvents()
            response = {"items": [
                {"name": "Fixture Library", "owner": {"username": "fixture"},
                 "version": {"name": "1.0.0"}, "description": "Search fixture"},
                {"name": "Optional metadata", "owner": None, "version": None},
                {"name": "x" * 100000, "owner": {}, "version": {}},
            ]}
            with patch.object(registry.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as fetch:
                results = registry.search("LED & query=https://unexpected.invalid")
                check(results[0]["spec"] == "fixture/Fixture Library@1.0.0" and len(results) == 2,
                      "bounded registry results tolerate absent metadata without huge protocol fields")
                check(fetch.call_args.args[0].startswith("https://api.registry.platformio.org/v3/search?")
                      and "query%3Dhttps" in fetch.call_args.args[0], "library query stays encoded within the fixed registry endpoint")
            with patch.object(registry.urllib.request, "urlopen", return_value=io.BytesIO(b"x" * (registry.MAX_RESPONSE + 1))):
                try:
                    registry.search("fixture")
                except ValueError:
                    check(True, "registry rejects oversized responses before JSON parsing")
                else:
                    check(False, "registry rejects oversized responses before JSON parsing")

            real_tool = worker.run_tool
            marker = state / "registry-started.json"
            drip_program = '''
import json, os, runpy, subprocess, sys, time, urllib.request
from pathlib import Path
marker = Path(sys.argv.pop(1))
mode = sys.argv.pop(1)
script = sys.argv.pop(1)
sys.argv[0] = script
def refuse(*args, **kwargs):
    raise AssertionError("Live fixture network refused")
urllib.request.OpenerDirector.open = refuse
class Drip:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, limit):
        child = None
        if mode == "descendant":
            child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"])
        staged = marker.with_suffix(".tmp")
        staged.write_text(json.dumps({"parent": os.getpid(), "child": child.pid if child else None}))
        staged.replace(marker)
        if mode == "success":
            return b'{"items":[{"name":"fixture","owner":{"username":"fixture"},"version":{"name":"1.0.0"}}]}'
        if child is not None:
            os._exit(0)
        while True:
            print("Receiving fixture registry bytes", flush=True)
            time.sleep(.03)
urllib.request.urlopen = lambda *args, **kwargs: Drip()
runpy.run_path(script, run_name="__main__")
'''
            for mode in ("stop", "deadline", "descendant"):
                marker.unlink(missing_ok=True)
                before = len(output.events)
                worker.cancel.clear()
                def mock_registry(arguments, *, timeout=1200):
                    check(Path(arguments[0]) == ROOT / "python/registry.py" and timeout == 30,
                          "search uses the fixed cancellable helper and finite elapsed budget: " + mode)
                    real_tool(["-c", drip_program, str(marker), mode, *arguments],
                              timeout=.8 if mode == "deadline" else 10)
                started = time.monotonic()
                with patch.object(worker, "run_tool", side_effect=mock_registry), unrelated_tool() as sibling:
                    worker.dispatch({"cmd": "library_search", "query": "fixture"})
                    deadline = time.monotonic() + 4
                    while not marker.exists() and worker.catalog_thread.is_alive() and time.monotonic() < deadline:
                        time.sleep(.01)
                    check(marker.exists(), "owned registry fixture starts without network: " + mode)
                    identity = json.loads(marker.read_text())
                    if mode != "deadline":
                        worker.dispatch({"cmd": "stop"})
                    join()
                    check(sibling.poll() is None, "resource Stop/deadline leaves an unrelated fixture process running: " + mode)
                events = output.events[before:]
                check(("console:progress", {"action": "Searching libraries"}) in events,
                      "resource search reports work instead of stale Ready status: " + mode)
                check(time.monotonic() - started < 5 and not worker.busy() and worker.process is None,
                      "registry ownership releases after Stop/deadline: " + mode)
                check(not any(name in {"libraries", "resource:installed"} or
                              name == "console:log" and data.get("tag") == "success"
                              for name, data in events), "cancelled search cannot publish results or success: " + mode)
                for pid in identity.values():
                    if pid is None:
                        continue
                    try:
                        stopped = psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
                    except psutil.NoSuchProcess:
                        stopped = True
                    check(stopped, "owned registry parent/descendants have stopped: " + mode)

            worker.cancel.clear()
            before = len(output.events)
            def registry_success(arguments, *, timeout=1200):
                real_tool(["-c", drip_program, str(marker), "success", *arguments], timeout=5)
            with patch.object(worker, "run_tool", side_effect=registry_success):
                worker.dispatch({"cmd": "library_search", "query": "--fixture &"})
                join()
            events = output.events[before:]
            check(any(name == "libraries" and data["query"] == "--fixture &"
                      and data["items"][0]["spec"] == "fixture/fixture@1.0.0" for name, data in events)
                  and not worker.busy(), "search after cancellation publishes only its new helper result, including literal option-like queries")

            worker.cancel.clear()
            owned_job = Mock()
            owned_job.assign_and_resume.side_effect = OSError("fixture job assignment failed")
            suspended = Mock()
            with patch("sys.platform", "win32"), \
                    patch("src.modules.bootstrap_pip_monitor._WindowsJob", return_value=owned_job), \
                    patch("worker.subprocess.Popen", return_value=suspended) as launch:
                try:
                    worker.run_tool(["-c", "raise AssertionError('must not run')"])
                except OSError:
                    pass
                else:
                    raise AssertionError("Failed job assignment must refuse tool execution")
                check(launch.call_args.kwargs["creationflags"] & 4 and suspended.terminate.call_count == 1
                      and suspended.wait.call_count == 1 and owned_job.close.call_count == 1,
                      "failed Windows job assignment retires the suspended original handle before release")

            with patch.object(worker.api, "upload_sketch") as upload, patch.object(worker, "prepare_board", side_effect=RuntimeError("offline fixture")):
                worker.dispatch({"cmd": "upload"})
                join()
                check(upload.call_count == 0 and not worker.busy(), "failed preparation releases ownership without attempting an upload")
            def cancelled_after_prepare(_):
                worker.cancel.set()
                return {}
            before = len(output.events)
            with patch.object(worker.api, "upload_sketch") as upload, patch.object(worker, "prepare_board", side_effect=cancelled_after_prepare):
                worker.dispatch({"cmd": "upload"})
                join()
                check(upload.call_count == 0 and not any(name == "resource:installed" for name, _ in output.events[before:]),
                      "cancellation at preparation completion publishes no installed result and cannot hand off Upload")
            entered = threading.Event()
            def blocked(_):
                entered.set()
                worker.cancel.wait(timeout=4)
                raise InterruptedError("cancelled fixture")
            with patch.object(worker.api, "upload_sketch") as upload, patch.object(worker, "prepare_board", side_effect=blocked):
                worker.dispatch({"cmd": "upload"})
                assert entered.wait(timeout=2)
                check(worker.busy(), "preparation owns the action through the entire tool check")
                try:
                    worker.dispatch({"cmd": "board", "name": worker.api.current_board})
                except RuntimeError:
                    check(True, "target changes remain locked throughout preparation")
                else:
                    check(False, "target changes remain locked throughout preparation")
                worker.dispatch({"cmd": "stop"})
                join()
                check(upload.call_count == 0 and not worker.busy(), "Stop during preparation prevents any upload or replay")
            with patch.object(worker.api, "upload_sketch") as upload, patch.object(worker, "prepare_board", return_value={}) as preparation:
                worker.dispatch({"cmd": "upload"})
                join()
                check(upload.call_count == 1 and preparation.call_count == 1, "successful automatic preparation hands off exactly one upload")
            def active_upload():
                worker.api.is_busy = True
            with patch.object(worker.api, "upload_sketch", side_effect=active_upload), patch.object(worker.api, "stop_operation") as stop, patch.object(worker, "prepare_board", return_value={}):
                worker.dispatch({"cmd": "upload"})
                join()
                worker.dispatch({"cmd": "stop"})
                check(not worker.resource_busy and worker.api.is_busy and stop.call_count == 1, "Stop after handoff reaches the backend's active-operation protection")
                worker.api.is_busy = False
            worker.api.emit("console:log", {"text": "Compatible devices analysis updated: see the 'Compatible Devices' tab."})
            check(not any(event == "console:log" and "Compatible Devices" in data.get("text", "") for event, data in output.events), "Lite does not direct users to absent main-workspace tabs")
            worker.api.emit("console:log", {"text": "Compiler error mentioning Compatible Devices", "tag": "error"})
            check(output.events[-1][1]["text"] == "Compiler error mentioning Compatible Devices", "compiler diagnostics are retained even when they contain a main-tab name")
            worker.close()
            worker.protected.remove(source)  # Release this owned fixture for test cleanup only.
    print(f"\n{len(checks)} preparation checks passed. No network requests or physical device access.")


if __name__ == "__main__":
    main()
