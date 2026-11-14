#!/usr/bin/env python3
"""Hardware-free integration checks in owned disposable Lite fixtures."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import psutil  # Load the native backend before host-policy fixtures change sys.platform.

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from isolation import child_setup, environment
from worker import ORIGINAL, Worker, snapshot_engine, snapshot_sources, host_check


class Recorder:
    def __init__(self):
        self.events = []

    def send(self, event, data=None):
        self.events.append((event, data))


def main():
    scratch = ROOT / "temp"
    scratch.mkdir(exist_ok=True)
    checks = []

    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print("PASS", name)

    with patch("sys.platform", "linux"), patch("platform.freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "24.04"}), patch("psutil.cpu_count", return_value=4):
        host_check()
        check(True, "Ubuntu 24.04 is admitted by explicit host policy")
    for release in ({"ID": "fedora", "ID_LIKE": "ubuntu", "VERSION_ID": "44"}, {"ID": "ubuntu", "VERSION_ID": "invalid"}):
        with patch("sys.platform", "linux"), patch("platform.freedesktop_os_release", return_value=release):
            try:
                host_check()
            except RuntimeError:
                check(True, "unsupported or malformed Linux release is refused: " + release["ID"] + "/" + release["VERSION_ID"])
            else:
                check(False, "unsupported or malformed Linux release is refused")
    with patch("sys.platform", "darwin"):
        try:
            host_check()
        except RuntimeError:
            check(True, "macOS is outside Lite's two-OS policy")
        else:
            check(False, "macOS is outside Lite's two-OS policy")
    with patch("sys.platform", "win32"), patch("platform.machine", return_value="ARM64"):
        try:
            host_check()
        except RuntimeError:
            check(True, "Windows ARM64 is refused before any runtime mutation")
        else:
            check(False, "Windows ARM64 is refused before any runtime mutation")

    with tempfile.TemporaryDirectory(prefix="verify-", dir=scratch) as temporary:
        base = Path(temporary)
        state = base / "state"
        state.mkdir()
        for folder in ("tmp", "data", "cache", "config"):
            (state / folder).mkdir()
        source = base / "FixtureSketch"
        source.mkdir()
        sketch = source / "FixtureSketch.ino"
        sketch.write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
        (source / "pins.h").write_text("#define PIN 2\n", encoding="utf-8")
        nested = source / "unrelated"
        nested.mkdir()
        (nested / "ignore.cpp").write_text("not part of the root sketch")
        digest = hashlib.sha256(sketch.read_bytes()).hexdigest()
        original, staged, files = snapshot_sources(source, state)
        check(original == source and staged != source, "source project is staged separately")
        check(files == ["FixtureSketch.ino", "pins.h"], "only supported root source files are captured")
        check(hashlib.sha256(sketch.read_bytes()).hexdigest() == digest, "original sketch bytes are preserved")
        check((staged / sketch.name).read_bytes() == sketch.read_bytes(), "staged bytes match the saved source")
        sketch.write_text("void setup() {}\nvoid loop() { delay(1); }\n")
        snapshot_sources(source, state)
        check((staged / sketch.name).read_bytes() == sketch.read_bytes(), "saved source changes are captured before a build")
        (source / "pins.h").unlink()
        snapshot_sources(source, state)
        check(not (staged / "pins.h").exists(), "deleted sources do not linger in the staged sketch")
        if os.name != "nt":
            (source / "linked.cpp").symlink_to(sketch)
            try:
                snapshot_sources(source, state)
            except ValueError:
                check(True, "linked root sources are refused")
            else:
                check(False, "linked root sources are refused")
            (source / "linked.cpp").unlink()
        engine = snapshot_engine(ORIGINAL, state)
        check((engine / "main/web_bridge.py").read_bytes() == (ORIGINAL / "main/web_bridge.py").read_bytes(), "backend source is reused byte-for-byte")
        check((engine / "src/dbs/dbs_create.py").is_file(), "headless persistence modules are present in the snapshot")
        os.environ.update(environment(state))
        sys.path.insert(0, str(engine))
        child_setup()
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication([])
        with patch("serial.tools.list_ports.comports", return_value=[]), \
                patch("main.web_bridge.MCUWebBackendAPI._scan_ports", return_value=[]):
            output = Recorder()
            worker = Worker(state, output)
            worker.ports()
            check(not worker.api.current_board and not worker.api.current_port, "no board or port is automatically selected")
            check(output.events[-1] == ("ports", []), "port enumeration uses a hardware-free fixture")
            worker.dispatch({"cmd": "boards", "query": "Arduino Uno [atmelavr:uno]"})
            found = [data for event, data in output.events if event == "boards"][-1]["items"]
            check(any(item["name"] == "Arduino Uno [atmelavr:uno]" for item in found), "exact boards are searchable before packages are installed")
            check(len(worker.boards) > 1000, "bundled catalog supplies a broad board picker")
            worker.source = source
            worker.api.sketch_dir_path = staged
            for cmd, method in [("compile", "compile_sketch"), ("upload", "upload_sketch"), ("clean", "clean_cache")]:
                worker.api.current_board = "Arduino Uno [atmelavr:uno]"
                worker.api.current_port = "fixture-port" if cmd == "upload" else ""
                with patch.object(worker.api, "_scan_ports", return_value=[{"device": "fixture-port"}]), patch.object(worker.api, method) as called, patch.object(worker, "prepare_board", return_value={}) as prepared:
                    worker.dispatch({"cmd": cmd})
                    deadline = time.monotonic() + 5
                    while worker.catalog_thread and worker.catalog_thread.is_alive() and time.monotonic() < deadline:
                        time.sleep(.01)
                    check(called.call_count == 1, f"{cmd} delegates to the original backend action")
                    if cmd != "clean":
                        check(prepared.call_count == 1, f"{cmd} automatically prepares tools before dispatch")
            with patch.object(worker.api, "set_baud_rate") as baud:
                worker.dispatch({"cmd": "baud", "value": 9600})
                check(baud.call_args.kwargs == {"allow_reset": False}, "baud changes do not request a reset")
            with patch.object(worker.api, "_start_serial_monitor") as monitor:
                worker.api.current_port = "fixture-port"
                worker.dispatch({"cmd": "monitor", "connect": True})
                check(monitor.call_count == 1, "Connect delegates to the original passive monitor")
            with patch.object(worker.api, "serial_send") as send:
                worker.dispatch({"cmd": "send", "text": "hello", "ending": "nl"})
                check(send.call_args.args == ("hello", "nl"), "serial sends retain the requested line ending")
            worker.api.current_port = ""
            worker.api.is_busy = True
            try:
                worker.dispatch({"cmd": "board", "name": "Arduino Uno [atmelavr:uno]"})
            except RuntimeError:
                check(True, "target changes are refused during an active operation")
            else:
                check(False, "target changes are refused during an active operation")
            worker.api.is_busy = False
            worker.api._catalog_refresh_running = True
            check(worker.busy(), "catalog refresh retains operation ownership until target definitions settle")
            worker.api._catalog_refresh_running = False
            check(not worker.stop_state()["enabled"], "idle and Developer Access do not advertise a cancellable operation")
            with patch.object(worker.api, "stop_operation") as stop:
                worker.api.is_busy = True
                worker.api.active_operation = "upload"
                for phase in ("resolving", "compiling", "connecting"):
                    worker.api._current_op_phase = phase
                    worker.api._stop_requested = False
                    check(worker.stop_state()["enabled"], "Stop is enabled during " + phase)
                    worker.dispatch({"cmd": "stop"})
                    check(stop.call_count > 0, "Stop reaches the cancellable backend during " + phase)
                for operation, phase in (("upload", "flashing"), ("upload", "writing"), ("upload", "erasing"), ("upload", "resetting"), ("clean", "cleaning")):
                    worker.api.active_operation = operation
                    worker.api._current_op_phase = phase
                    stop.reset_mock()
                    check(not worker.stop_state()["enabled"] and bool(worker.stop_state()["reason"]), "protected " + phase + " explains why Stop is disabled")
                    worker.resource_busy = True
                    check(not worker.stop_state()["enabled"], "protected " + phase + " takes precedence during preparation handoff")
                    worker.resource_busy = False
                    worker.dispatch({"cmd": "stop"})
                    check(not stop.called, "Stop never interrupts " + phase)
                worker.api.active_operation = "compile"
                worker.api._current_op_phase = "compiling"
                worker.api._stop_requested = True
                check(not worker.stop_state()["enabled"], "a repeated Stop is disabled while cancellation completes")
                worker.api._stop_requested = False
                worker.api._framework_download_active = True
                check(not worker.stop_state()["enabled"], "native package publication retains the backend Stop guard")
                worker.api._framework_download_active = False
                worker.api.is_busy = False
                worker.api.active_operation = worker.api._current_op_phase = None
                worker.resource_busy = True
                check(worker.stop_state()["enabled"], "resource preparation remains cancellable")
                worker.dispatch({"cmd": "stop"})
                check(worker.cancel.is_set() and not worker.stop_state()["enabled"], "resource Stop reports cancellation in progress without repeated handoffs")
                worker.resource_busy = False
                worker.cancel.clear()
                worker.api.emit("operation:phase", {"is_busy": False, "phase": "idle"})
                check(output.events[-1] == ("stop:state", worker.stop_state()), "operation transitions publish Stop authority without waiting for polling")
            try:
                worker.dispatch({"cmd": "arbitrary_python_method"})
            except ValueError:
                check(True, "protocol rejects arbitrary method dispatch")
            else:
                check(False, "protocol rejects arbitrary method dispatch")
            worker.api.emit("boards:updated", {"boards": dict(worker.boards)})
            check(output.events[-1][0] != "boards:updated", "full catalogs never overflow the bounded UI protocol")
            worker.api.emit("serial:log", {"text": "user unicode: \u03bb \U0001f680\n", "stream": True})
            check(output.events[-1][1]["text"].endswith("\U0001f680\n"), "user Unicode serial data is preserved")
            worker.api.emit("console:log", {"text": "\U0001f680 Ready"})
            check("\U0001f680" not in output.events[-1][1]["text"], "generated status messages do not contain decorative emoji")
            table_line = "║ Restart : ✓ Ready             ║"
            worker.api.emit("console:log", {"text": table_line})
            check(len(output.events[-1][1]["text"]) == len(table_line), "decorative-mark removal preserves boxed log columns")
            worker.api.emit("console:log", {"text": "Writing 10%", "replace_pattern": "Writing", "newline": True})
            progress_key = output.events[-1][1].get("progress_key")
            worker.api.emit("console:log", {"text": "Writing 100%", "replace_pattern": "Writing", "newline": True})
            check(bool(progress_key) and progress_key == output.events[-1][1].get("progress_key"), "legacy progress updates carry a stable operation-owned row key")
            worker.api.emit("console:log", {"text": "Verified", "progress_key": "fixture:verify", "newline": True})
            check(output.events[-1][1]["progress_key"] == "fixture:verify", "uploader progress identities reach the Rust monitor unchanged")
            # Run the real Clean worker against disposable caches only.
            cache = staged / ".mcu_flasher_build_cache"
            (cache / "boards/dummy").mkdir(parents=True)
            (cache / "boards/dummy/object.o").write_bytes(b"fixture")
            protected = cache / ".mcu_ai_edits"
            protected.mkdir()
            (protected / "retained.txt").write_text("retained history fixture")
            worker.dispatch({"cmd": "clean"})
            deadline = time.monotonic() + 10
            while worker.api._operation_worker_alive() and time.monotonic() < deadline:
                time.sleep(.01)
                app.processEvents()
            check(not worker.busy(), "real Clean completes and releases operation ownership")
            check(not (cache / "boards").exists(), "real Clean removes only private build workspaces")
            check((protected / "retained.txt").read_text() == "retained history fixture", "Clean preserves private AI recovery history")
            check(sketch.read_bytes() == (staged / sketch.name).read_bytes(), "Clean preserves original and staged source files")
            check(not (ORIGINAL / "src" / "lite-settings.json").exists(), "Lite does not create settings in the original source tree")
            check((state / "settings.json").exists(), "settings are redirected to private Lite state")
            # Check actual Python child startup and package-manager environment.
            env = worker.pio_environment()
            probe = subprocess.run([sys.executable, "-B", "-c", "import main.core.config as c; print(c.LOCAL_GUI_CONFIG)"],
                env=env, capture_output=True, text=True, timeout=15, check=True)
            check(str(state / "settings.json") in probe.stdout, "Python tool children inherit isolated settings")
            check("Error in sitecustomize" not in probe.stderr, "Python child isolation initializes without startup errors")
            try:
                (ORIGINAL / "src/lite-write-probe-never-created.txt").write_text("must be refused")
            except PermissionError:
                check(True, "audit guard refuses writes to original application files before mutation")
            else:
                check(False, "audit guard refuses writes to original application files before mutation")
            worker.close()
            child_state = base / "protocol-state"
            program = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                # Host admission is checked above. The protocol fixture must not
                # depend on the hosted VM's physical/logical CPU topology.
                "import psutil; psutil.cpu_count=lambda logical=True: 4; "
                "import serial.tools.list_ports; serial.tools.list_ports.comports=lambda: []; "
                "import main.web_bridge; main.web_bridge.MCUWebBackendAPI._scan_ports=lambda self: []; "
                "import worker; worker.main()"
            )
            child_env = dict(os.environ, MCU_LITE_STATE=str(child_state))
            child = subprocess.run([sys.executable, "-B", "-u", "-c", program, str(ROOT / "python")],
                env=child_env, input='{"cmd":"status","id":101}\ninvalid-json\n{"cmd":"shutdown","id":102}\n',
                capture_output=True, text=True, timeout=20, check=False)
            assert child.returncode == 0, f"Worker fixture exited {child.returncode}:\n{child.stdout}\n{child.stderr}"
            frames = [json.loads(line) for line in child.stdout.splitlines()]
            check(any(frame["event"] == "ready" for frame in frames), "real worker startup publishes a protocol handshake")
            acknowledgements = [frame["data"]["id"] for frame in frames if frame["event"] == "ack"]
            check(acknowledgements == [101, 102], "command acknowledgements match exact requests and do not replay on malformed JSON")
            check(any(frame["event"] == "error" for frame in frames) and frames[-1]["event"] == "closed", "malformed requests report errors without preventing graceful shutdown")
            check(max(len(json.dumps(frame)) for frame in frames) < 1024 * 1024, "all real protocol frames fit the Rust reader budget")
    print(f"\n{len(checks)} hardware-free checks passed. No device was opened and no package was downloaded.")


if __name__ == "__main__":
    main()
