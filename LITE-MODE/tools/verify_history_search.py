#!/usr/bin/env python3
"""Hardware-free history, search and action readiness regressions."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from isolation import child_setup, environment
from search import BoardSearch, normalize, rank_libraries
from worker import ORIGINAL, Worker, snapshot_engine


class Recorder:
    def __init__(self):
        self.events = []

    def send(self, event, data=None):
        self.events.append((event, data))

    def latest(self, name):
        return next(data for event, data in reversed(self.events) if event == name)


def main():
    (ROOT / "temp").mkdir(exist_ok=True)
    checks = []

    def check(condition, label):
        assert condition, label
        checks.append(label)
        print("PASS", label, flush=True)

    def settle(worker):
        deadline = time.monotonic() + 15
        while worker.busy():
            assert time.monotonic() < deadline, "Fixture operation timed out"
            application.processEvents()
            time.sleep(.01)

    with tempfile.TemporaryDirectory(prefix="history-search-", dir=ROOT / "temp") as temporary:
        base = Path(temporary)
        state = base / "state"
        state.mkdir()
        for folder in ("tmp", "data", "cache", "config"):
            (state / folder).mkdir()
        sources = []
        for index in range(12):
            source = base / f"Sketch{index}"
            source.mkdir()
            (source / f"{source.name}.ino").write_text("void setup() {}\nvoid loop() {}\n")
            sources.append(source)
        engine = snapshot_engine(ORIGINAL, state)
        os.environ.update(environment(state))
        sys.path.insert(0, str(engine))
        child_setup()
        from PySide6.QtCore import QCoreApplication
        from main.core import config
        from main.core.board_catalog import BoardCatalog
        application = QCoreApplication.instance() or QCoreApplication([])
        with patch("serial.tools.list_ports.comports", return_value=[]):
            output = Recorder()
            worker = Worker(state, output)
            try:
                for source in sources:
                    worker.select_project(str(source))
                recent = output.latest("history")["projects"]
                check(recent == [str(p) for p in reversed(sources[2:])], "recent sketches retain the newest 10 original paths")
                worker.select_project(str(sources[4]))
                recent = output.latest("history")["projects"]
                check(recent[0] == str(sources[4]) and len(recent) == len(set(recent)) == 10, "reopening a sketch moves it first without duplicates")
                check(all(not Path(path).is_relative_to(state) for path in recent), "recent sketches never expose private staged paths")
                saved = json.loads((state / "settings.json").read_text())
                check(saved["shared"]["recent_projects"] == recent, "recent sketches persist in Lite's settings")

                uno = "Arduino Uno [atmelavr:uno]"
                names = [uno] + [name for name in worker.boards if name != uno][:5]
                for name in names:
                    worker.dispatch({"cmd": "board", "name": name})
                    settle(worker)
                check(output.latest("history")["boards"] == list(reversed(names[1:])), "recent boards retain the newest 5 selections")
                worker.dispatch({"cmd": "board", "name": names[3]})
                settle(worker)
                recent_boards = output.latest("history")["boards"]
                check(recent_boards[0] == names[3] and len(recent_boards) == len(set(recent_boards)) == 5, "reselecting a board moves it first without duplicates")
                child = "from main.core.config import load_recent_projects,load_recent_boards; import json; print(json.dumps([load_recent_projects(),load_recent_boards()]))"
                restored = subprocess.run([sys.executable, "-B", "-c", child], env=environment(state), capture_output=True, text=True, check=True, timeout=30)
                check(json.loads(restored.stdout) == [recent, recent_boards], "both histories survive a fresh Python process")
                factory = config._lite_config_lock_factory
                with patch.object(config, "_lite_config_lock_factory", wraps=factory) as lock_factory:
                    child_setup()
                    config.add_recent_board(names[3])
                    check(lock_factory.call_args.args == (state / "settings.json",), "history transactions lock only Lite's private settings")
                child_setup()
                missing = base / "MissingSketch"
                config.add_recent_project(str(missing))
                worker.history()
                check(str(missing) not in output.latest("history")["projects"], "missing sketch folders are pruned like the main app")

                search = worker.board_search
                result = search.find(worker.boards, "uno")
                check(result["items"][0]["name"] == uno, "exact board IDs rank first")
                result = search.find(worker.boards, "ARDUINO-UNO")
                check(result["items"][0]["name"] == uno, "search tolerates case and punctuation")
                check(normalize("Héltec") == "heltec", "search ignores accent marks without splitting words")
                result = search.find(worker.boards, "esp32 s3")
                check(result["total"] > 0 and all(all(word in normalize(row["name"] + " " + row["mcu"]).replace(" ", "") for word in ("esp32", "s3")) for row in result["items"]), "multiple query words match together across names and MCU metadata")
                result = search.find(worker.boards, "ardunio uno")
                check(result["fuzzy"] and result["items"][0]["name"] == uno, "board-name typos offer clearly marked closest matches")
                catalog = BoardCatalog({"Board A": {"board": "test-a", "pio_name": "Board A", "pio_vendor": "Naph", "platform": "avr", "mcu": "atmega328p", "frameworks": {"arduino"}}})
                indexed = BoardSearch()
                check(indexed.find(catalog, "naph atmega328p arduino")["total"] == 1, "vendor, MCU and framework metadata are searchable together")
                rows = indexed.rows
                indexed.find(catalog, "test-a")
                check(indexed.rows is rows, "unchanged catalogs reuse their normalized search index")
                catalog.replace({"Board B": {"board": "test-b", "pio_name": "Board B"}})
                check(indexed.find(catalog, "test-b")["items"][0]["name"] == "Board B" and indexed.rows is not rows, "catalog revisions replace stale search entries")
                check(len(search.find(worker.boards, "")["items"]) <= 80, "board search results remain bounded")
                libraries = [{"spec": "owner/Other", "name": "Other", "owner": "owner", "description": "Servo helper"}, {"spec": "owner/Servo", "name": "Servo", "owner": "owner", "description": "Motor"}]
                ranked = rank_libraries(libraries + libraries, "name:Servo")
                check(ranked[0]["name"] == "Servo" and len(ranked) == 2, "library search prioritizes exact names and removes duplicate packages")

                worker.dispatch({"cmd": "board", "name": uno})
                settle(worker)
                for action in ("compile", "upload"):
                    worker.api.current_board = ""
                    with patch.object(worker, "prepare_board") as prepare:
                        try:
                            worker.dispatch({"cmd": action})
                        except ValueError:
                            pass
                        else:
                            raise AssertionError("Missing board was accepted")
                        check(not prepare.called, f"{action} rejects an unconfigured board before tool preparation")
                worker.api.current_board = uno
                worker.api.current_port = ""
                worker.require_action_ready("compile")
                check(True, "Compile requires a valid board but no port")
                for port in ("", "disconnected-fixture-port"):
                    worker.api.current_port = port
                    with patch.object(worker, "prepare_board") as prepare:
                        try:
                            worker.dispatch({"cmd": "upload"})
                        except ValueError:
                            pass
                        else:
                            raise AssertionError("Missing or disconnected port was accepted")
                        check(not prepare.called, "Upload rejects " + ("a disconnected port" if port else "a missing port") + " before tool preparation")
                worker.api.current_port = "fixture-port"
                worker.api.skip_compile = False
                with patch.object(worker.api, "_scan_ports", return_value=[{"device": "fixture-port"}]), patch.object(worker, "prepare_board", return_value={}), patch.object(worker.api, "upload_sketch") as upload:
                    worker.dispatch({"cmd": "upload"})
                    settle(worker)
                    check(upload.call_count == 1 and worker.api.skip_compile, "a ready Upload enables automatic certified-build reuse before one handoff")
            finally:
                worker.close()
                worker.protected[:] = [ORIGINAL]
    print(f"\n{len(checks)} history/search/readiness checks passed. No downloads or device access.")


if __name__ == "__main__":
    main()
