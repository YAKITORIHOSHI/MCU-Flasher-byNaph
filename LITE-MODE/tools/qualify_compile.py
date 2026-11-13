#!/usr/bin/env python3
"""Opt-in real compiler qualification in an owned, hardware-free fixture."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from worker import ORIGINAL, Worker, snapshot_engine
from preparation import main_core_directory
from isolation import environment, child_setup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="Download this host's Uno compiler into the private qualification fixture")
    parser.add_argument("--reuse-main", action="store_true", help="Read compatible prepared main-app packages into the private fixture")
    args = parser.parse_args()
    main_core = main_core_directory(ORIGINAL, os.environ) if args.reuse_main else None
    base = ROOT / "temp/compile-proof"
    state = base / "state"
    state.mkdir(parents=True, exist_ok=True)
    for folder in ("tmp", "data", "cache", "config"):
        (state / folder).mkdir(exist_ok=True)
    source = base / "FixtureBlink"
    source.mkdir(exist_ok=True)
    sketch = source / "FixtureBlink.ino"
    sketch.write_text("void setup() {}\nvoid loop() { delay(1); }\n")
    engine = snapshot_engine(ORIGINAL, state)
    os.environ.update(environment(state))
    sys.path.insert(0, str(engine))
    child_setup()
    from PySide6.QtCore import QCoreApplication
    application = QCoreApplication.instance() or QCoreApplication([])

    class Record:
        def __init__(self):
            self.events = []
            self.log = (base / "qualification.log").open("w", encoding="utf-8")

        def send(self, event, data=None):
            self.events.append((event, data))
            self.log.write(json.dumps({"event": event, "data": data}, default=str) + "\n")
            self.log.flush()
            if event == "error" or (event == "console:log" and data.get("tag") in {"error", "success"}):
                print(str(data)[:350], flush=True)

    def wait(worker):
        deadline = time.monotonic() + 1250
        while worker.busy():
            if time.monotonic() > deadline:
                raise TimeoutError("Qualification exceeded its elapsed budget")
            time.sleep(.05)
            application.processEvents()

    with patch("serial.tools.list_ports.comports", return_value=[]):
        output = Record()
        worker = Worker(state, output, main_core=main_core)
        try:
            worker.select_project(str(source))
            worker.dispatch({"cmd": "board", "name": "Arduino Uno [atmelavr:uno]"})
            if args.prepare:
                worker.dispatch({"cmd": "board_install"})
                wait(worker)
                if any(event == "error" for event, _ in output.events):
                    raise RuntimeError("Board preparation failed. See qualification.log")
            worker.api.refresh_board_catalog(invalidate_parsed=True)
            wait(worker)
            results = []
            for label in ("initial", "unchanged", "source edit"):
                if label == "source edit":
                    # Model an external editor. The worker's own process is
                    # deliberately forbidden from writing the original source.
                    subprocess.run([sys.executable, "-B", "-c",
                        "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])",
                        str(sketch), "void setup() {}\nvoid loop() { delay(2); }\n"], check=True, timeout=15)
                start = len(output.events)
                started = time.monotonic()
                worker.dispatch({"cmd": "compile"})
                wait(worker)
                assert worker.api._last_compiled_board == worker.api.current_board
                assert worker.api._last_source_hash == worker.api._hash_sources(worker.api.current_board)
                logs = [str(data.get("text", "")) for event, data in output.events[start:] if event == "console:log"]
                assert any("Compilation successful" in text for text in logs), "Backend did not certify the compiled output"
                compiled = [text for text in logs if "Compiling " in text and ".o" in text]
                if label == "unchanged":
                    assert not compiled, f"Unchanged build recompiled objects: {compiled}"
                if label == "source edit":
                    assert all("FixtureBlink.ino.cpp.o" in text for text in compiled), f"Unrelated framework objects rebuilt: {compiled}"
                results.append({"phase": label, "seconds": round(time.monotonic() - started, 3), "compiled_objects": compiled, "success": True})
            report = json.loads((state / "prepare/result.json").read_text())
            assert "tool-avrdude" in report["uploader_packages"]
            from src.modules.platformio_build_env import build_environment, RUN_OPTIONS
            workspace = worker.api._board_workspace_dir(worker.api.current_board)
            core = Path(worker.pio_environment()["PLATFORMIO_CORE_DIR"])
            # Construct the real backend's uploader recipe without dispatching
            # an upload target. Serial.open is explicitly refused in this child.
            probe = (
                "import sys,runpy,serial,serial.tools.list_ports; "
                "serial.tools.list_ports.comports=lambda: []; "
                "serial.Serial.open=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('Physical serial access refused by fixture')); "
                "entry=sys.argv.pop(1); runpy.run_path(entry,run_name='__main__')"
            )
            dump = subprocess.run([sys.executable, "-B", "-c", probe, str(engine / "src/modules/offline_platformio.py"),
                "run", *RUN_OPTIONS, "--project-dir", str(workspace), "--target", "envdump"],
                cwd=workspace, env=build_environment(worker.pio_environment(), core, 4), capture_output=True, text=True, timeout=120, check=True)
            assert "avrdude" in dump.stdout and "UPLOADER" in dump.stdout, "Offline uploader recipe was not resolved"
            recipe, recording = [], False
            for line in dump.stdout.splitlines():
                if re.match(r"\s{2}'[^']+':", line):
                    recording = bool(re.match(r"\s{2}'(?:UPLOADER|UPLOADERFLAGS|UPLOADCMD|UPLOADEEPCMD)':", line))
                if recording:
                    recipe.append(line)
            # The complete SCons dump contains the inherited environment. Keep
            # only the uploader recipe in the reviewable qualification record.
            (base / "uploader-envdump.log").write_text("\n".join(recipe) + "\n", encoding="utf-8")
            uno = worker.api.current_board
            uno_workspace = worker.api._board_workspace_dir(uno)
            uno_firmware = worker.api._board_build_dir(uno) / "firmware.hex"
            certified_uno = uno_firmware.read_bytes()
            nano = next(name for name, info in worker.boards.items()
                if info.get("platform") == "atmelavr" and info.get("board") == "nanoatmega328")
            worker.dispatch({"cmd": "board", "name": nano})
            wait(worker)
            started = time.monotonic()
            worker.dispatch({"cmd": "compile"})
            wait(worker)
            assert worker.api._last_compiled_board == nano
            assert not worker.api._needs_recompile(nano)[0], "Second board build was not certified"
            nano_workspace = worker.api._board_workspace_dir(nano)
            assert nano_workspace != uno_workspace, "Boards shared a build workspace"
            assert uno_firmware.read_bytes() == certified_uno, "Compiling another board altered the Uno firmware"
            results.append({"phase": "separate Nano build", "seconds": round(time.monotonic() - started, 3), "success": True})
            uploads = []

            def cached_upload(label, board, expected_skip):
                worker.dispatch({"cmd": "board", "name": board})
                wait(worker)
                worker.api.current_port = "fixture-port"
                # Clean and other main-workspace actions can clear this flag.
                # Lite must enable certified reuse on every Upload itself.
                worker.api.skip_compile = False
                decisions = []

                def device_boundary(can_skip=False):
                    decisions.append(bool(can_skip))
                    worker.api._release_requested_operation()

                with patch.object(worker.api, "_scan_ports", return_value=[{"device": "fixture-port"}]), \
                        patch.object(worker.api, "_native_upload_worker", side_effect=device_boundary), \
                        patch.object(worker.api, "_upload_worker", side_effect=device_boundary):
                    worker.dispatch({"cmd": "upload"})
                    wait(worker)
                assert decisions == [expected_skip], f"{label}: expected skip={expected_skip}, got {decisions}"
                uploads.append({"phase": label, "board": board, "skip_compile": expected_skip, "dispatches": 1})

            cached_upload("Nano saved build", nano, True)
            cached_upload("return to Uno saved build", uno, True)
            subprocess.run([sys.executable, "-B", "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])",
                str(sketch), "void setup() {}\nvoid loop() { delay(3); }\n"], check=True, timeout=15)
            cached_upload("changed source requires Uno compilation", uno, False)
            cached_upload("changed source requires Nano compilation", nano, False)
            subprocess.run([sys.executable, "-B", "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])",
                str(sketch), "void setup() {}\nvoid loop() { delay(2); }\n"], check=True, timeout=15)
            cached_upload("restored source verifies saved Uno bytes", uno, True)
            uno_firmware.write_bytes(certified_uno + b"\n")
            cached_upload("corrupt Uno firmware cannot be reused", uno, False)
            cached_upload("Uno corruption leaves Nano firmware reusable", nano, True)
            uno_firmware.write_bytes(certified_uno)
            cached_upload("restored Uno firmware verifies again", uno, True)
            worker.dispatch({"cmd": "clean"})
            wait(worker)
            cached_upload("Clean requires compilation on next upload", uno, False)
            evidence = {"host": platform.platform(), "target": "atmelavr:uno / arduino", "ports": "mocked empty; never opened", "results": results,
                "automatic_preparation": True, "upload_dependency": "tool-avrdude", "offline_uploader_envdump": True,
                "upload_dispatch": "exactly once per request; final device boundary mocked", "packages": report["packages"],
                "separate_board_workspaces": True, "boards": [uno, nano], "automatic_cached_uploads": uploads,
                "certified_uno_sha256": hashlib.sha256(certified_uno).hexdigest(),
                "reused_from_main": [data["text"] for event, data in output.events if event == "console:log" and data.get("text", "").startswith("Reusing main package:")]}
            (base / "result.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            print(json.dumps(evidence, indent=2))
        finally:
            worker.close()
            output.log.close()


if __name__ == "__main__":
    main()
