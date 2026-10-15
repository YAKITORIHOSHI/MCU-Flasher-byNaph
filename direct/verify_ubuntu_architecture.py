#!/usr/bin/env python3
"""Hardware-free amd64/ARM64 boundaries; all files belong to temp/ fixtures."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import ubuntu_architecture as architecture
from src.modules import platform_runtime
from main.platforms import ubuntu_opencode


class UbuntuArchitectureChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/ubuntu-architecture"
        audit.mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)

    def elf(self, path, machine):
        path.parent.mkdir(parents=True, exist_ok=True)
        header = bytearray(64)
        header[:7] = b"\x7fELF\x02\x01\x01"
        header[18:20] = machine.to_bytes(2, "little")
        path.write_bytes(header)
        path.chmod(0o755)
        return path

    def test_linux_and_debian_cpu_names_select_same_assets(self):
        for machine, expected in (("x86_64", "amd64"), ("AMD64", "amd64"),
                                  ("aarch64", "arm64"), ("ARM64", "arm64")):
            self.assertEqual(architecture.host_architecture(machine), expected)
            self.assertEqual(architecture.linux_tool_architecture(machine), "linux-" + expected)

    def test_32bit_and_unknown_hosts_never_default_to_amd64(self):
        for machine in ("armv7l", "armv6l", "i686", "riscv64", "", "unknown"):
            with self.subTest(machine=machine), self.assertRaisesRegex(RuntimeError, "64-bit"):
                architecture.host_architecture(machine)

    def test_elf_rejects_foreign_cpu_class_endianness_and_truncation(self):
        for machine, cpu in (("x86_64", 62), ("aarch64", 183)):
            binary = self.elf(self.root / machine, cpu)
            self.assertTrue(architecture.elf_matches_host(binary, machine))
            foreign = "arm64" if machine == "x86_64" else "amd64"
            self.assertFalse(architecture.elf_matches_host(binary, foreign))
            for index, value in ((4, 1), (5, 2)):
                data = bytearray(binary.read_bytes())
                data[index] = value
                invalid = self.root / f"invalid-{index}"
                invalid.write_bytes(data)
                self.assertFalse(architecture.elf_matches_host(invalid, machine))
        for data in (b"", b"MZWindows", b"\x7fELF\x02\x01"):
            invalid = self.root / "truncated"
            invalid.write_bytes(data)
            self.assertFalse(architecture.elf_matches_host(invalid, "arm64"))

    def test_linux_stores_remain_disjoint_and_do_not_change_existing_names(self):
        with patch.object(platform_runtime, "_native_xdg_base", return_value=self.root):
            with patch.object(platform_runtime.platform, "machine", return_value="x86_64"):
                amd64 = platform_runtime.native_platformio_dir()
            with patch.object(platform_runtime.platform, "machine", return_value="aarch64"):
                arm64 = platform_runtime.native_platformio_dir()
        self.assertNotEqual(amd64, arm64)
        self.assertEqual(amd64.name, "x86_64")
        self.assertEqual(arm64.name, "aarch64")
        self.assertFalse(amd64.exists())
        self.assertFalse(arm64.exists())

    def test_path_discovery_rejects_wrong_cpu_but_accepts_script_wrappers(self):
        binary = self.elf(self.root / "opencode", 62)
        with patch.object(architecture.platform, "machine", return_value="aarch64"):
            self.assertIsNone(ubuntu_opencode._native_executable(binary))
            self.elf(binary, 183)
            self.assertEqual(ubuntu_opencode._native_executable(binary), str(binary))
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            self.assertEqual(ubuntu_opencode._native_executable(binary), str(binary))

    def test_managed_opencode_receipt_cannot_cross_host_architecture(self):
        binary = self.elf(self.root / ".ubuntu-tools/opencode/opencode", 183)
        state = {"owner": ubuntu_opencode.MANAGED_OWNER, "schema": 1,
                 "architecture": "linux-arm64", "size": binary.stat().st_size,
                 "mtime_ns": binary.stat().st_mtime_ns}
        certificate = binary.parent / "installation.json"
        certificate.write_text(json.dumps(state), encoding="utf-8")
        with patch.object(ubuntu_opencode, "ROOT", self.root), \
                patch.object(architecture.platform, "machine", return_value="aarch64"):
            self.assertEqual(ubuntu_opencode.managed_opencode_cli(), str(binary))
            state["architecture"] = "linux-amd64"
            certificate.write_text(json.dumps(state), encoding="utf-8")
            self.assertIsNone(ubuntu_opencode.managed_opencode_cli())
            state.pop("architecture")
            certificate.write_text(json.dumps(state), encoding="utf-8")
            self.assertIsNone(ubuntu_opencode.managed_opencode_cli())

    def test_legacy_amd64_receipt_remains_usable_on_amd64(self):
        binary = self.elf(self.root / ".ubuntu-tools/opencode/opencode", 62)
        state = {"owner": ubuntu_opencode.MANAGED_OWNER, "schema": 1,
                 "size": binary.stat().st_size, "mtime_ns": binary.stat().st_mtime_ns}
        (binary.parent / "installation.json").write_text(json.dumps(state), encoding="utf-8")
        with patch.object(ubuntu_opencode, "ROOT", self.root), \
                patch.object(architecture.platform, "machine", return_value="x86_64"):
            self.assertEqual(ubuntu_opencode.managed_opencode_cli(), str(binary))


if __name__ == "__main__":
    unittest.main(verbosity=2)
