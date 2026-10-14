#!/usr/bin/env python3
"""Native Windows short-cwd regression checks in disposable temp/ fixtures.

The GCC probe compiles a tiny C file with the prepared compiler, without
PlatformIO, libraries, downloads, accounts, metadata generation or hardware.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.platforms import windows
from main.core.cloud_sketch_service import _io_path


@unittest.skipUnless(sys.platform == "win32", "Native Windows execution required")
class WindowsBuildPaths(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp" / "audit" / "windows-build-paths"
        audit.mkdir(parents=True, exist_ok=True)
        self.fixture = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.fixture.name = str(_io_path(self.root))
        self.workspace = self.root / "Cloud" / "BLE_Receiver-Final" / "board"
        self.workspace.mkdir(parents=True)
        # Keep aliases in this fixture. NTFS short names let the actual GCC
        # experiment stay under temp/ even when the checkout path is long.
        from src.modules.windows_tool_paths import python_install_path
        self.short_root = python_install_path(self.root)
        self.namespace = self.short_root / "app"
        patcher = patch.object(windows, "_mcuflasher_app_root", return_value=self.namespace)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.aliases = []
        self.addCleanup(self.remove_aliases)

    def remove_aliases(self):
        for alias in self.aliases:
            if windows._is_windows_reparse_point(alias):
                os.rmdir(alias)  # Remove only the fixture junction entry.

    def alias(self, workspace=None):
        alias = windows.compiler_working_directory(workspace or self.workspace)
        if alias != (workspace or self.workspace):
            self.aliases.append(alias)
        return alias

    def test_alias_reuses_the_same_workspace_and_separates_exact_boards(self):
        first = self.alias()
        self.assertNotEqual(first, self.workspace)
        self.assertEqual(first.resolve(), self.workspace.resolve())
        self.assertEqual(first, self.alias())
        (first / "probe.txt").write_bytes(b"same canonical cache")
        self.assertEqual((self.workspace / "probe.txt").read_bytes(), b"same canonical cache")
        other = self.workspace.with_name("other-board")
        other.mkdir()
        second = self.alias(other)
        self.assertNotEqual(first, second)
        self.assertEqual(second.resolve(), other.resolve())

    def test_occupied_alias_is_never_adopted_or_repointed(self):
        import hashlib
        key = hashlib.sha256(os.path.normcase(str(self.workspace.resolve())).encode()).hexdigest()[:24]
        occupied = self.namespace / "builds" / key
        occupied.mkdir(parents=True)
        (occupied / "keep.txt").write_bytes(b"unrelated material")
        with self.assertRaisesRegex(RuntimeError, "unrelated material"):
            self.alias()
        self.assertEqual((occupied / "keep.txt").read_bytes(), b"unrelated material")

    def test_alias_failure_stops_before_compiler_launch(self):
        with patch.object(windows, "_create_compiler_junction", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "could not create"):
                self.alias()

    def test_compiler_junction_preserves_literal_shell_characters(self):
        canonical = self.workspace / "percent% literal & quote'"
        canonical.mkdir()
        alias = self.alias(canonical)
        self.assertEqual(alias.resolve(), canonical.resolve())

    def test_installed_gcc_long_dependency_failure_is_fixed_by_short_cwd(self):
        gcc = ROOT / "src/.platformio-mcu-gui/packages/toolchain-xtensa-esp32/bin/xtensa-esp32-elf-gcc.exe"
        if not gcc.is_file():
            self.skipTest("Prepared ESP32 compiler is unavailable")
        if str(self.short_root).startswith("\\\\?\\"):
            self.skipTest("Fixture volume has no short names; production drive-root alias is covered separately")
        relative = Path(".pio/build/mcu_env/libc62/NimBLE_Arduino-2.5.1/nimble/nimble/transport/esp_ipc_legacy/src/hci_esp_ipc_legacy.c.d")
        self.workspace = self.workspace / ("w" * 32)
        _io_path(self.workspace / relative.parent).mkdir(parents=True)
        _io_path(self.workspace / "probe.c").write_bytes(b"int cloud_path_probe(void) { return 1; }\n")
        self.assertGreater(len(str(self.workspace / relative)), 260)
        command = [str(gcc), "-MMD", "-MF", relative.as_posix(), "-c", "probe.c", "-o", "probe.o"]
        failed = subprocess.run(command, cwd=self.workspace, capture_output=True, text=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertNotEqual(failed.returncode, 0, failed.stderr)
        self.assertIn("opening dependency file", failed.stderr)
        alias = self.alias()
        self.assertLess(len(str(alias / relative)), 260)
        fixed = subprocess.run(command, cwd=alias, capture_output=True, text=True, timeout=20,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(fixed.returncode, 0, fixed.stderr)
        self.assertTrue(_io_path(self.workspace / relative).is_file())
        self.assertTrue((self.workspace / "probe.o").is_file())
        print(f"Native ESP32 GCC: dependency path {len(str(self.workspace / relative))} chars fails; "
              f"{len(str(alias / relative))} chars succeeds.")


if __name__ == "__main__":
    unittest.main(verbosity=2)
