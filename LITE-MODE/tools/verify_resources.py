#!/usr/bin/env python3
"""Installed-resource, sample and shared-catalog checks. No devices or network."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from resources import Inventory, main_download_directory
from worker import ORIGINAL, Worker, snapshot_engine
from isolation import environment, child_setup


def main():
    checks = []
    def check(condition, name):
        assert condition, name
        checks.append(name)
        print("PASS", name, flush=True)

    (ROOT / "temp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="resources-", dir=ROOT / "temp") as temporary:
        base = Path(temporary)
        state, shared, downloads, home = [base / name for name in ("state", "main-core", "downloaded", "home")]
        for path in (state, shared, downloads, home):
            path.mkdir()
        for name in ("tmp", "data", "cache", "config"):
            (state / name).mkdir()
        original = base / "original"
        settings = original / "src/dbs/arduino_browser_settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"download_dir": str(downloads)}))
        check(main_download_directory(original, home=home) == downloads, "main's configured download location is read without rewriting settings")
        library = downloads / "Libs/Fixture-1.2.3"
        library.mkdir(parents=True)
        (library / "library.properties").write_text("name=Fixture library\nversion=1.2.3\nsentence=Offline example library\n")
        for relative, value in (("examples/Demo/Demo.ino", '#include "pins.h"\nvoid setup() {}\nvoid loop() {}\n'),
                                ("examples/Demo/pins.h", "#pragma once\n#define PIN 2\n"),
                                ("examples/Demo/helper.cpp", "int helper() { return 2; }\n"),
                                ("examples/Other/Other.pde", "void setup() {}\nvoid loop() {}\n"),
                                ("src/private.cpp", "Not a sample")):
            path = library / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        own = shared / "lib/Local"
        own.mkdir(parents=True)
        (own / "library.json").write_text('{"name":"Local library","version":"2.0.0"}')
        archive = downloads / "Libs/Archive-1.0.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Archive/examples/Blink/Blink.ino", "void setup() {}\nvoid loop() {}")
            output.writestr("../examples/escape.ino", "outside")
        inventory = Inventory(state, shared, downloads, home=home)
        libraries = inventory.scan()
        check({item["name"] for item in libraries} == {"Fixture library", "Local library", "Archive-1.0"},
              "offline inventory lists the shared library store and archived downloaded libraries")
        key = next(item["id"] for item in libraries if item["name"] == "Fixture library")
        samples = inventory.examples(key)
        check(len(samples) == 4 and any(row["name"].endswith("pins.h") for row in samples),
              "all example sketch, C++ and header files are listed without library implementation files")
        selected = next(row for row in samples if row["name"].endswith("Demo.ino"))
        code = inventory.source(selected["id"])
        check(code["text"].startswith('#include "pins.h"'), "sample viewer receives the exact read-only source bytes")
        try:
            inventory.source(str(library / "src/private.cpp"))
        except ValueError:
            check(True, "the protocol cannot read arbitrary paths or files outside its sample inventory")
        else:
            check(False, "arbitrary path refused")
        archive_key = next(item["id"] for item in libraries if item["name"] == "Archive-1.0")
        archived = inventory.examples(archive_key)
        check(len(archived) == 1 and "void setup()" in inventory.source(archived[0]["id"])["text"],
              "ZIP samples open without extraction, ignoring traversal names")
        cancelled = threading.Event()
        cancelled.set()
        try:
            inventory.scan(cancelled)
        except InterruptedError:
            check(True, "superseded resource scans stop before publishing")
        else:
            check(False, "cancelled scan refused")

        platform = shared / "platforms/lite_fixture"
        board = platform / "boards/fixture_exact.json"
        board.parent.mkdir(parents=True)
        (platform / "platform.json").write_text('{"name":"lite_fixture","version":"1.0.0"}')
        board.write_text(json.dumps({"name": "Exact fixture board", "vendor": "Fixture",
            "frameworks": ["arduino"], "build": {"mcu": "fixture32", "f_cpu": "16000000L"},
            "upload": {"maximum_size": 32768, "maximum_ram_size": 2048, "protocol": "serial"}}))
        before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                  for root in (downloads, shared) for path in root.rglob("*") if path.is_file()}
        snapshot_engine(ORIGINAL, state)
        os.environ.update(environment(state))
        os.environ["MCU_LITE_MAIN_CORE"] = str(shared)
        os.environ["MCU_LITE_MAIN_DOWNLOAD"] = str(downloads)
        sys.path.insert(0, str(state / "engine"))
        with patch("pathlib.Path.home", return_value=home):
            child_setup()
            from PySide6.QtCore import QCoreApplication
            app = QCoreApplication.instance() or QCoreApplication([])
            from main.core import board_catalog
            from main.core.target_profile import target_problem
            rows = board_catalog._load_platformio_board_catalog()
            check(any(row["id"] == "fixture_exact" for row in rows),
                  "Lite discovers an exact board from main's prepared native store before preparation")
            class Record:
                def __init__(self): self.events = []
                def send(self, event, data=None): self.events.append((event, data))
            output = Record()
            with patch("serial.tools.list_ports.comports", return_value=[]), \
                    patch("main.web_bridge.MCUWebBackendAPI._scan_ports", return_value=[]):
                worker = Worker(state, output, main_core=shared, main_download=downloads)
                worker.inventory.home = home
                try:
                    exact = "Arduino Uno [atmelavr:uno]"
                    worker.api.emit("boards:updated", {"boards": {exact: {"pio_resolved": False, "platform": "atmelavr", "board": ""}}})
                    check(not target_problem(worker.boards[exact]), "unresolved refresh rows cannot erase exact catalog definitions")
                    worker.dispatch({"cmd": "resources", "token": 1})
                    deadline = time.monotonic() + 5
                    while worker._reader_thread is not None and time.monotonic() < deadline:
                        time.sleep(.01)
                        app.processEvents()
                    check(any(event == "resources:loaded" for event, data in output.events), "installed-resource scanning completes off the protocol thread")
                    installed = [row for event, data in output.events if event == "resources:boards" for row in data["items"]]
                    check(len(installed) == 1 and installed[0]["name"].startswith("Exact fixture board"),
                          "Resources Boards contains downloaded definitions, excluding the bundled picker catalog")
                    worker.dispatch({"cmd": "board", "name": installed[0]["name"]})
                    check(not target_problem(worker.api._resolve_board_info()), "a main-store board selects as a valid target for automatic preparation")
                    check(downloads / "Libs" in worker.api._library_search_directories(worker.inventory.core),
                          "compilation discovers the same custom downloaded libraries as Resources")
                    check(all(len(json.dumps({"event": event, "data": data}, ensure_ascii=False).encode()) < 1024 * 1024
                              for event, data in output.events), "resource publications fit the bounded Rust protocol")
                finally:
                    worker.close()
                    # Release only fixture-owned audit roots for temporary
                    # cleanup after their byte-preservation assertion.
                    worker.protected[1:] = []
        after = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                 for root in (downloads, shared) for path in root.rglob("*") if path.is_file()}
        check(before == after, "discovery and sample viewing leave main resources byte-for-byte unchanged")
    print(f"\n{len(checks)} resource checks passed. No network or physical devices.")


if __name__ == "__main__":
    main()
