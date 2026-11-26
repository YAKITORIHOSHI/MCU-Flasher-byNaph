#!/usr/bin/env python3
"""A Bootstrap ended part-way through board preparation heals on its next start.

Every store, board folder and process here is a disposable fixture under the checkout's temp/;
no real tool store, download or network is touched.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psutil
from src.modules import bootstrap_recovery as recovery
from src.modules.offline_bootstrap import CATALOG_MARKER, MARKER


def _dead_process() -> tuple[int, float]:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    created = psutil.Process(process.pid).create_time()
    process.wait()
    return process.pid, created


class Store(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp"
        scratch.mkdir(exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix="bootstrap-recovery-", dir=scratch))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.core = self.folder / "core"
        self.boards = self.folder / "Boards"
        for kind in ("packages", "platforms", "lib"):
            (self.core / kind).mkdir(parents=True)
        self.boards.mkdir()

    def package(self, name, *, metadata=True, manifest=True, kind="packages", kind_type="tool"):
        folder = self.core / kind / name
        folder.mkdir()
        if metadata:
            (folder / ".piopm").write_text(json.dumps({"type": kind_type, "name": name, "version": "1.0.0",
                                                       "spec": {"name": name}}), encoding="utf-8")
        if manifest:
            (folder / ("platform.json" if kind == "platforms" else "package.json")).write_text(
                json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8")
        return folder

    def interrupted_record(self, children=()):
        pid, created = _dead_process()
        (self.core / recovery.JOURNAL).write_text(json.dumps(
            {"schema": 1, "pid": pid, "created": created, "stage": "boards", "started": time.time(),
             "children": list(children)}), encoding="utf-8")

    def test_without_an_unfinished_preparation_nothing_is_touched(self):
        damaged = self.package("toolchain-a", manifest=False)
        self.assertFalse(recovery.interrupted(self.core))
        self.assertEqual(recovery.heal(self.core, self.boards), [])
        self.assertTrue(damaged.exists())

    def test_a_preparation_still_running_is_not_healed_under_it(self):
        recovery.begin(self.core)
        unfinished = self.core / "packages" / "_tmp_installing-123"
        unfinished.mkdir()
        self.assertFalse(recovery.interrupted(self.core))
        self.assertEqual(recovery.heal(self.core, self.boards), [])
        self.assertTrue(unfinished.exists())

    def test_a_stopped_preparation_returns_to_its_last_safe_point(self):
        good = self.package("toolchain-good")
        good_platform = self.package("espressif32", kind="platforms", kind_type="platform")
        damaged = self.package("toolchain-damaged", manifest=False)
        empty = self.package("framework-empty", metadata=False, manifest=False)
        users_library = self.core / "lib" / "MyLibrary"
        users_library.mkdir()
        (users_library / "MyLibrary.h").write_text("#pragma once\n", encoding="utf-8")
        unfinished = self.core / "platforms" / "_tmp_installing-9f"
        unfinished.mkdir()
        (self.core / ".cache" / "tmp").mkdir(parents=True)
        (self.core / ".cache" / "tmp" / "chunk").write_bytes(b"x")
        for marker in (MARKER, CATALOG_MARKER):
            (self.core / marker).write_text("{}", encoding="utf-8")
        staging = self.boards / (recovery.STAGING_PREFIX + "esp32-3.1.0")
        (staging / "variants").mkdir(parents=True)
        partial = self.boards / "esp32-3.1.0.zip.part"
        partial.write_bytes(b"resumable")
        self.interrupted_record()

        self.assertTrue(recovery.interrupted(self.core))
        actions = recovery.heal(self.core, self.boards)

        self.assertTrue(good.exists() and good_platform.exists(), "finished packages are kept")
        self.assertTrue(users_library.exists(), "a library without metadata may be the user's own")
        self.assertFalse(damaged.exists() or empty.exists() or unfinished.exists() or staging.exists())
        self.assertTrue((self.core / recovery.RECOVERY).is_dir(), "damaged packages are set aside, not lost")
        self.assertFalse((self.core / MARKER).exists() or (self.core / CATALOG_MARKER).exists(),
                         "readiness is proven again, not inherited")
        self.assertFalse((self.core / ".cache" / "tmp" / "chunk").exists())
        self.assertEqual(partial.read_bytes(), b"resumable", "downloads resume where they stopped")
        self.assertTrue(any("toolchain-damaged" in action for action in actions), actions)
        self.assertTrue(any("esp32-3.1.0" in action for action in actions), actions)

        recovery.begin(self.core)
        recovery.finish(self.core)
        self.assertIsNone(recovery.read(self.core))
        self.assertFalse((self.core / recovery.RECOVERY).exists())

    def test_what_a_stopped_preparation_left_running_is_stopped_and_nothing_else(self):
        marker = "offline_bootstrap.py"
        leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", marker])
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: [p.kill() for p in (leftover, unrelated) if p.poll() is None])
        children = [{"pid": process.pid, "created": psutil.Process(process.pid).create_time(), "marker": marker}
                    for process in (leftover, unrelated)]
        children.append({"pid": os.getpid(), "created": 1.0, "marker": ""})  # A reused process ID.
        self.interrupted_record(children)
        stopped = recovery.stop_leftovers(self.core)
        leftover.wait(timeout=10)
        self.assertIsNotNone(leftover.poll())
        self.assertIsNone(unrelated.poll(), "a command line without the recorded marker is left alone")
        self.assertEqual(stopped, [f"{marker} ({leftover.pid})"])

    def test_a_forced_heal_after_its_own_failed_attempt_keeps_the_record(self):
        recovery.begin(self.core)
        unfinished = self.core / "packages" / "_tmp_installing-1"
        unfinished.mkdir()
        recovery.heal(self.core, self.boards, force=True)
        self.assertFalse(unfinished.exists())
        self.assertEqual(recovery.read(self.core)["pid"], os.getpid())


class BoardCores(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp"
        scratch.mkdir(exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix="board-staging-", dir=scratch))
        self.addCleanup(shutil.rmtree, self.folder, True)
        names = {"_extract_board_archive_folder_only"}
        source = ast.parse((ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig"))
        selected = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.namespace = {"Path": Path, "os": os, "shutil": shutil, "_gui": None, "warn": lambda _m: None,
                          "safe_rmtree": lambda path: shutil.rmtree(path, ignore_errors=True)}
        exec(compile(ast.Module(body=selected, type_ignores=[]), "isolated-extract", "exec"), self.namespace)
        self.archive = self.folder / "esp32-3.1.0.zip"
        with zipfile.ZipFile(self.archive, "w") as bundle:
            bundle.writestr("esp32-3.1.0/boards.txt", "esp32.name=ESP32\n")
            bundle.writestr("esp32-3.1.0/platform.txt", "version=3.1.0\n")
            bundle.writestr("esp32-3.1.0/cores/esp32/main.cpp", "int main() {}\n")

    def test_a_core_is_never_half_present_under_its_own_name(self):
        final = self.folder / "esp32-3.1.0"
        seen = []
        original = zipfile.ZipFile.extract

        def extract(bundle, member, path=None, pwd=None):
            seen.append((final.exists(), Path(path).name))
            return original(bundle, member, path, pwd)
        zipfile.ZipFile.extract = extract
        try:
            self.assertTrue(self.namespace["_extract_board_archive_folder_only"](self.archive, final))
        finally:
            zipfile.ZipFile.extract = original
        self.assertTrue(seen and not any(present for present, _name in seen))
        self.assertTrue(all(name.startswith(recovery.STAGING_PREFIX) for _present, name in seen))
        self.assertTrue((final / "boards.txt").is_file() and (final / "cores/esp32/main.cpp").is_file())
        self.assertFalse((self.folder / (recovery.STAGING_PREFIX + final.name)).exists())
        self.assertFalse(self.archive.exists())

    def test_a_failed_unpack_leaves_no_staging_behind(self):
        broken = self.folder / "broken.zip"
        broken.write_bytes(b"not an archive")
        final = self.folder / "broken"
        self.assertFalse(self.namespace["_extract_board_archive_folder_only"](broken, final))
        self.assertFalse(final.exists() or (self.folder / (recovery.STAGING_PREFIX + "broken")).exists())


class Wiring(unittest.TestCase):
    def test_bootstrap_sets_a_safe_point_heals_and_retries(self):
        text = (ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8")
        stage = text[text.index("bootstrap_recovery.stop_leftovers(offline_core)"):]
        order = ["bootstrap_recovery.stop_leftovers(offline_core)", "_bootstrap_tool_store_lease(gui, offline_core)",
                 "_heal_board_preparation(gui, offline_core, boards_dir)", "bootstrap_recovery.begin(offline_core)",
                 "for attempt in (1, 2):", "bootstrap_recovery.add_child(offline_core, process.pid",
                 "_heal_board_preparation(gui, offline_core, boards_dir, force=True)",
                 "finish_bootstrap(offline_core)", "bootstrap_recovery.finish(offline_core)"]
        positions = [stage.find(item) for item in order]
        self.assertTrue(all(position >= 0 for position in positions), list(zip(order, positions)))
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main(verbosity=2)
