#!/usr/bin/env python3
"""One physical store, concurrent builds and conservative cleanup in owned fixtures."""
from __future__ import annotations
import json
import errno
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
sys.path.insert(0, str(ROOT.parent))
from shared_store import consolidate
from worker import Worker, snapshot_engine, ORIGINAL, try_lock
from isolation import environment, child_setup
from preparation import prepare
from verify_preparation import write_package, tree_hash


def main():
    checks = []
    def check(value, name):
        assert value, name
        checks.append(name)
        print("PASS", name, flush=True)
    (ROOT / "temp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shared-store-", dir=ROOT / "temp") as temporary:
        base = Path(temporary)
        state, core = base / "state", base / "main-core"
        state.mkdir()
        legacy = state / "platformio"
        snapshot_engine(ORIGINAL, state)
        from platformio import util
        from platformio.package.manager.platform import PlatformPackageManager
        from platformio.package.manager.tool import ToolPackageManager
        from src.modules.package_jobs import package_store_lease
        # Exact duplicate, unique version, differing contents and unknown state.
        shared = write_package(core, "tool", "same-tool", "1.0.0", system=[util.get_systype()])
        duplicate = write_package(legacy, "tool", "same-tool", "1.0.0", system=[util.get_systype()])
        unique = write_package(legacy, "tool", "only-in-lite", "2.0.0", system=[util.get_systype()])
        different = write_package(legacy, "tool", "different-tool", "1.0.0", system=[util.get_systype()])
        main_different = write_package(core, "tool", "different-tool", "1.0.0", system=[util.get_systype()])
        (different / "payload.txt").write_text("Lite-specific bytes must survive")
        unknown = legacy / "packages/unknown-user-folder"
        unknown.mkdir()
        (unknown / "keep.txt").write_text("keep")
        settings = state / "settings.json"
        settings.write_text('{"keep":"settings"}')
        recovery = state / "projects/Sketch/.mcu_flasher_build_cache/.mcu_ai_edits"
        recovery.mkdir(parents=True)
        (recovery / "recovery.txt").write_text("do not touch")
        before = tree_hash(shared)
        busy = try_lock(state / "workers" / f"{os.getpid()}.lock")
        report = consolidate(state, core, apply=True, log=lambda _text: None)
        check(report["deferred"] and duplicate.exists(), "an active Lite worker prevents deleting or moving its packages")
        busy.close()
        report = consolidate(state, core, apply=False, log=lambda _text: None)
        check(duplicate.exists() and unique.exists() and report["removed_bytes"] > 0, "cleanup planning leaves all package bytes untouched")
        with package_store_lease(core, root=base / "main-events"):
            try:
                consolidate(state, core, events=base / "lite-events", apply=True, log=lambda _text: None)
            except RuntimeError:
                check(duplicate.exists(), "cleanup cannot enter while the main app is compiling from the store")
            else:
                check(False, "cleanup excluded by active build")
        report = consolidate(state, core, events=base / "main-events", apply=True, log=lambda _text: None)
        check(not duplicate.exists() and tree_hash(shared) == before, "verified duplicates are removed without changing main package bytes")
        check(not unique.exists() and (core / "packages/only-in-lite/payload.txt").is_file(), "a Lite-only package moves into the one shared native store")
        variants = list((core / "packages").glob("different-tool@lite-*"))
        check(len(variants) == 1 and (variants[0] / "payload.txt").read_text() == "Lite-specific bytes must survive"
              and (main_different / "payload.txt").read_text() == "different-tool-payload", "different package contents are retained side by side without overwriting main")
        check(unknown.is_dir() and settings.read_text() == '{"keep":"settings"}' and (recovery / "recovery.txt").read_text() == "do not touch",
              "cleanup preserves unknown folders, settings, projects and recovery history")
        cross = write_package(legacy, "tool", "another-drive-tool", "1.0.0", system=[util.get_systype()])
        native_rename = Path.rename
        def cross_device(path, target):
            if path == cross:
                raise OSError(errno.EXDEV, "fixture cross-device move")
            return native_rename(path, target)
        with patch.object(Path, "rename", cross_device):
            consolidate(state, core, apply=True, log=lambda _text: None)
        check(not cross.exists() and (core / "packages/another-drive-tool/payload.txt").read_text() == "another-drive-tool-payload",
              "different-drive migration verifies an independent staged copy before retiring the old package")
        if os.name != "nt":
            linked = write_package(legacy, "tool", "absolute-internal-link", "1.0.0", system=[util.get_systype()])
            (linked / "link.txt").symlink_to(linked / "payload.txt")
            consolidate(state, core, apply=True, log=lambda _text: None)
            target = core / "packages/absolute-internal-link/link.txt"
            check(target.read_text() == "absolute-internal-link-payload" and target.resolve().is_relative_to(core),
                  "absolute package-internal links are relocated without leaving broken links to the retired store")
        # A ready preparation holds only a shared reader and never copies/installs.
        manifest = {"title": "Shared store fixture", "engines": {"platformio": ">=6"},
                    "frameworks": {"arduino": {"package": "framework-fixture", "script": "framework.py"}},
                    "packages": {"framework-fixture": {"type": "framework", "owner": "platformio", "version": "~1.0.0"}}}
        write_package(core, "platform", "lite_fixture", "1.0.0", manifest=manifest)
        write_package(core, "tool", "framework-fixture", "1.0.0", system=[util.get_systype()])
        write_package(core, "tool", "tool-scons", "4.41101.0", system=[util.get_systype()])
        config = state / "prepare.ini"
        config.write_text("[env:lite]\nplatform=platformio/lite_fixture\nboard=fixture\nframework=arduino\n")
        for name in ("tmp", "data", "cache", "config"):
            (state / name).mkdir(exist_ok=True)
        os.environ.update(environment(state))
        os.environ["MCU_LITE_MAIN_CORE"] = str(core)
        os.environ["MCU_LITE_MAIN_EVENTS"] = str(base / "main-events")
        sys.path.insert(0, str(state / "engine"))
        child_setup()
        with patch.object(PlatformPackageManager, "install", side_effect=AssertionError("Unexpected copy/download")), \
                patch.object(ToolPackageManager, "install", side_effect=AssertionError("Unexpected copy/download")):
            with package_store_lease(core, root=base / "main-events"), package_store_lease(core, root=base / "lite-events"):
                report = prepare(config, core, install=False, log=lambda _text: None)
        check(not report["changed"], "already prepared tools are read from one store while both products hold build leases")
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication([])
        class Record:
            def __init__(self): self.events = []
            def send(self, event, data=None): self.events.append((event, data))
        output = Record()
        with patch("serial.tools.list_ports.comports", return_value=[]), patch("main.web_bridge.MCUWebBackendAPI._scan_ports", return_value=[]):
            worker = Worker(state, output, main_core=core)
            try:
                check(Path(worker.pio_environment()["PLATFORMIO_CORE_DIR"]) == core, "Lite's compiler environment selects the main physical package store")
                worker.run_tool(["-c", "print('Package files: 0/9');print('Package files: 4/9');print('Package files: 9/9');print('Compiler diagnostic');print('Package files: 0/2');print('Package files: 2/2')"])
                progress = [data for event, data in output.events if event == "console:log" and data.get("text", "").startswith("Package files:")]
                check(len({data["progress_key"] for data in progress[:3]}) == 1
                      and len({data["progress_key"] for data in progress[3:]}) == 1
                      and progress[0]["progress_key"] != progress[3]["progress_key"], "each package's file progress replaces one row and distinct packages keep separate rows")
                check(any(data.get("text") == "Compiler diagnostic" and not data.get("progress_key") for event, data in output.events if event == "console:log"),
                      "progress replacement never hides normal compiler diagnostics")
                entered = threading.Event()
                def slow_cleanup(*_args, cancel, **_kwargs):
                    entered.set()
                    assert cancel.wait(5), "Cleanup did not receive Stop"
                    raise InterruptedError("fixture cleanup cancelled")
                first = len(output.events)
                with patch("shared_store.consolidate", side_effect=slow_cleanup), \
                        patch.object(worker.api, "refresh_board_catalog"):
                    check(worker.consolidate_packages() and entered.wait(3) and worker.dispatch({"cmd": "status"})
                          and worker.stop_state()["enabled"], "automatic cleanup leaves protocol commands responsive and advertises Stop")
                    worker.dispatch({"cmd": "stop"})
                    worker.catalog_thread.join(timeout=5)
                    check(not worker.catalog_thread.is_alive() and not worker.resource_busy
                          and not any(data.get("tag") == "success" for event, data in output.events[first:] if event == "console:log"),
                          "Stop cancels startup cleanup without publishing success or handing off a build")
            finally:
                worker.close()
                worker.protected[1:] = []
    print(f"\n{len(checks)} shared-store checks passed; no network or devices.")


if __name__ == "__main__":
    main()
