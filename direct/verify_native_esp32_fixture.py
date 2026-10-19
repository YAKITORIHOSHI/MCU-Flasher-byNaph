#!/usr/bin/env python3
"""Synthetic compiler layouts only; no installed packages, downloads or execution."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.native_esp32_fixture import LAYOUTS, native_esp32_compilers


class NativeCompilerFixtureChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "audit/migration/scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="native-compiler-", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.core = Path(temporary.name)
        self.platform = self.core / "platforms/espressif32"
        (self.platform / "boards").mkdir(parents=True)
        (self.platform / "boards/esp32dev.json").write_text('{}')
        self.declare([name for name, _ in LAYOUTS])

    def declare(self, names):
        (self.platform / "platform.json").write_text(json.dumps({"packages": {name: {"type": "toolchain"} for name in names}}))

    def compiler(self, layout, machine=183):
        name, executable = layout
        package = self.core / "packages" / name
        (package / "bin").mkdir(parents=True, exist_ok=True)
        (package / "package.json").write_text(json.dumps({"name": name}))
        header = bytearray(64)
        header[:7] = b"\x7fELF\x02\x01\x01"
        header[18:20] = machine.to_bytes(2, "little")
        path = package / "bin" / executable
        path.write_bytes(header)
        return path

    def test_legacy_and_unified_layouts_are_supported_independently(self):
        for layout in LAYOUTS:
            path = self.compiler(layout)
            self.declare([layout[0]])
            self.assertEqual(native_esp32_compilers(self.core, "aarch64"), (path.resolve(),))

    def test_unified_native_compiler_does_not_require_absent_legacy_package(self):
        path = self.compiler(LAYOUTS[1])
        self.assertEqual(native_esp32_compilers(self.core, "arm64"), (path.resolve(),))

    def test_amd64_and_arm64_cannot_certify_each_others_compilers(self):
        path = self.compiler(LAYOUTS[1], 62)
        self.assertEqual(native_esp32_compilers(self.core, "x86_64"), (path.resolve(),))
        with self.assertRaisesRegex(RuntimeError, "not native ELF64"):
            native_esp32_compilers(self.core, "aarch64")

    def test_native_old_package_cannot_mask_foreign_unified_package(self):
        self.compiler(LAYOUTS[0])
        self.compiler(LAYOUTS[1], 62)
        with self.assertRaisesRegex(RuntimeError, "not native ELF64"):
            native_esp32_compilers(self.core, "arm64")

    def test_missing_package_is_distinct_from_wrong_architecture(self):
        with self.assertRaisesRegex(RuntimeError, "No installed declared"):
            native_esp32_compilers(self.core, "arm64")

    def test_undeclared_package_and_wrong_package_identity_are_refused(self):
        path = self.compiler(LAYOUTS[1])
        self.declare([])
        with self.assertRaisesRegex(RuntimeError, "No installed declared"):
            native_esp32_compilers(self.core, "arm64")
        self.declare([LAYOUTS[1][0]])
        (path.parent.parent / "package.json").write_text('{"name":"unrelated"}')
        with self.assertRaisesRegex(RuntimeError, "identity does not match"):
            native_esp32_compilers(self.core, "arm64")

    def test_malformed_metadata_and_non_elf_executables_are_refused(self):
        path = self.compiler(LAYOUTS[1])
        path.write_text('#!/bin/sh\nexit 0\n')
        with self.assertRaisesRegex(RuntimeError, "not native ELF64"):
            native_esp32_compilers(self.core, "arm64")
        (self.platform / "platform.json").write_text('[]')
        with self.assertRaisesRegex(RuntimeError, "must be an object"):
            native_esp32_compilers(self.core, "arm64")

    def test_declared_name_without_toolchain_metadata_cannot_certify_a_compiler(self):
        self.compiler(LAYOUTS[1])
        (self.platform / "platform.json").write_text(json.dumps({"packages": {LAYOUTS[1][0]: {"type": "uploader"}}}))
        with self.assertRaisesRegex(RuntimeError, "not a toolchain"):
            native_esp32_compilers(self.core, "arm64")

    @unittest.skipIf(sys.platform == "win32", "Unix fixture symlinks require no privilege")
    def test_compiler_and_package_metadata_cannot_escape_the_owned_package(self):
        path = self.compiler(LAYOUTS[1])
        outside = self.core / "outside-compiler"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "compiler escaped"):
            native_esp32_compilers(self.core, "arm64")
        path.unlink()
        self.compiler(LAYOUTS[1])
        manifest = path.parent.parent / "package.json"
        outside = self.core / "outside-package.json"
        outside.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "metadata escaped"):
            native_esp32_compilers(self.core, "arm64")


if __name__ == "__main__":
    unittest.main(verbosity=2)
