"""Isolated application-directory guard coverage and normalization budget.

No metadata, user sketch, settings or live package writes. Fixtures stay in temp/.
"""
from __future__ import annotations

import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from main.core import constants

INTERNALS = ("main", "src", "installers", "direct", ".agents", ".github")


class ApplicationGuardChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/application-guard"
        audit.mkdir(parents=True, exist_ok=True)
        self.fixture = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name).resolve()
        self.app = self.root / "application"
        self.app.mkdir()
        self.guard_root = patch.object(constants, "SCRIPT_DIR", self.app)
        self.guard_root.start()
        self.addCleanup(self.guard_root.stop)

    def test_actual_application_root_and_internal_directories_remain_blocked(self):
        with patch.object(constants, "SCRIPT_DIR", ROOT):
            self.assertTrue(constants.is_application_codebase_dir(ROOT))
            for internal in INTERNALS:
                self.assertTrue(constants.is_application_codebase_dir(ROOT / internal), internal)

    def test_internal_descendants_and_absent_reserved_names_are_blocked(self):
        self.assertTrue(constants.is_application_codebase_dir(self.app))
        for internal in INTERNALS:
            self.assertTrue(constants.is_application_codebase_dir(self.app / internal / "nested"), internal)
        self.assertFalse(constants.is_application_codebase_dir(self.app / "temporary-sketch"))
        self.assertFalse(constants.is_application_codebase_dir(self.app / "main-sketch"))

    def test_all_application_signatures_block_copied_codebase(self):
        for number, relative in enumerate(("src/modules/bootstrap.py", "main/mcu_flash_gui.py", "main/web_bridge.py")):
            copied = self.root / f"signature-copy-{number}"
            signature = copied / relative
            signature.parent.mkdir(parents=True)
            signature.write_text("# isolated signature fixture\n", encoding="utf-8")
            self.assertTrue(constants.is_application_codebase_dir(copied), relative)

    def test_ordinary_sketch_resolves_only_selected_path_not_root_or_internal_dirs(self):
        for internal in INTERNALS:
            (self.app / internal).mkdir()
        selected = self.root / "ordinary-sketch"
        selected.mkdir()
        calls = []
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            calls.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            self.assertFalse(constants.is_application_codebase_dir(selected))
        self.assertEqual(calls, [selected])

    def test_symlink_and_windows_junction_targets_resolve_fresh_on_every_call(self):
        alias = self.app / "main"
        targets = [self.root / "first-target", self.root / "retargeted-target"]
        for target in targets:
            target.mkdir()
            (target / "nested").mkdir()
        original_resolve, original_lstat = Path.resolve, Path.lstat
        active_target = targets[0]
        attributes = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        def lstat(path, *args, **kwargs):
            return attributes if path == alias else original_lstat(path, *args, **kwargs)
        def resolve(path, *args, **kwargs):
            return active_target if path == alias else original_resolve(path, *args, **kwargs)
        with patch.object(Path, "lstat", lstat), patch.object(Path, "resolve", resolve):
            self.assertTrue(constants.is_application_codebase_dir(targets[0] / "nested"))
            active_target = targets[1]
            self.assertFalse(constants.is_application_codebase_dir(targets[0] / "nested"))
            self.assertTrue(constants.is_application_codebase_dir(targets[1] / "nested"))
            # Native POSIX symlinks carry mode evidence instead of Windows attrs.
            attributes = SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            self.assertTrue(constants.is_application_codebase_dir(targets[1] / "nested"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
