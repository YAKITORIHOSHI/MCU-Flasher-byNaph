#!/usr/bin/env python3
"""Real PlatformIO recovery and interrupted publication fixtures; no downloads."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from platformio.package.meta import PackageItem, PackageMetadata, PackageSpec
from platformio.package.manager.tool import ToolPackageManager


class RecoveryChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = Path(self.temp.name) / "core"
        self.core.mkdir()

    def recovery(self):
        name = "src.modules.bootstrap_package_recovery"
        self.assertIsNotNone(importlib.util.find_spec(name), "Bootstrap lacks preserved-package recovery")
        return __import__(name, fromlist=["recover_packages"])

    def package(self, name="demo", parent="packages"):
        folder = self.core / parent / name
        folder.mkdir(parents=True)
        (folder / "package.json").write_text(json.dumps({"name": name, "version": "1.0.0"}))
        meta = PackageMetadata("tool", name, "1.0.0", PackageSpec(owner="vendor", name=name))
        meta.dump(folder / ".piopm")
        (folder / "payload").write_bytes(b"original binary bytes\x00\xff")
        return folder

    def test_bad_metadata_preserves_originals_and_healthy_neighbor_then_enumerates(self):
        recovery = self.recovery()
        neighbor = self.package("healthy")
        stamp = (neighbor / ".piopm").stat().st_mtime_ns
        for number, raw in enumerate((b"", b'{"type":', b"[]", b"null", b"{}",
                b'{"type":"tool","name":"demo","version":"1.0.0","spec":{}}')):
            folder = self.package("bad" + str(number))
            (folder / ".piopm").write_bytes(raw)
            (self.core / ".mcu-offline-ready.json").write_text("old success")
            receipts = recovery.recover_packages(self.core, log=lambda _: None)
            self.assertEqual(len(receipts), 1)
            saved = Path(receipts[0]["preserved"])
            self.assertEqual((saved / ".piopm").read_bytes(), raw)
            self.assertEqual((saved / "payload").read_bytes(), b"original binary bytes\x00\xff")
            self.assertFalse(folder.exists())
            self.assertFalse((self.core / ".mcu-offline-ready.json").exists())
            receipt = json.loads(Path(receipts[0]["receipt"]).read_text())
            self.assertEqual(receipt["metadata"][".piopm"]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(recovery.recover_packages(self.core, log=lambda _: None), [])
        self.assertEqual((neighbor / ".piopm").stat().st_mtime_ns, stamp)
        self.assertEqual([p.metadata.name for p in ToolPackageManager(str(self.core / "packages")).get_installed()], ["healthy"])

    def test_bad_manifest_is_preserved_but_unknown_folders_are_untouched(self):
        recovery = self.recovery()
        folder = self.package()
        (folder / "package.json").write_bytes(b"")
        unknown = self.core / "packages/user-files"
        unknown.mkdir()
        (unknown / "notes.txt").write_text("retain")
        result = recovery.recover_packages(self.core, log=lambda _: None)
        self.assertEqual(len(result), 1)
        self.assertEqual((Path(result[0]["preserved"]) / "package.json").read_bytes(), b"")
        self.assertEqual((unknown / "notes.txt").read_text(), "retain")

    def test_redirected_metadata_and_permission_errors_never_quarantine(self):
        recovery = self.recovery()
        folder = self.package()
        (folder / ".piopm").unlink()
        outside = self.core / "outside"
        outside.write_bytes(b"")
        try:
            (folder / ".piopm").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"This host cannot create the redirected metadata fixture: {exc}")
        with self.assertRaisesRegex(RuntimeError, "redirect|link"):
            recovery.recover_packages(self.core)
        self.assertTrue(folder.is_dir())
        (folder / ".piopm").unlink()
        (folder / ".piopm").write_bytes(b"")
        real = Path.open
        def denied(path, *args, **kwargs):
            if path == folder / ".piopm":
                raise PermissionError("fixture denied")
            return real(path, *args, **kwargs)
        with patch.object(Path, "open", denied), self.assertRaises(PermissionError):
            recovery.recover_packages(self.core)
        self.assertTrue(folder.is_dir())

    def test_interrupted_metadata_write_keeps_old_bytes(self):
        recovery = self.recovery()
        folder = self.package()
        path = folder / ".piopm"
        original = path.read_bytes()
        meta = PackageItem(str(folder)).metadata
        with recovery.atomic_package_writes(self.core), patch.object(recovery.os, "replace", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                meta.dump(path)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(list(folder.glob(".piopm.*.tmp")))

    def test_interrupted_receipt_completion_keeps_entire_original_recoverable(self):
        recovery = self.recovery()
        folder = self.package()
        (folder / ".piopm").write_bytes(b"")
        marker = self.core / ".mcu-offline-ready.json"
        marker.write_text("old success")
        original = recovery._write_json
        calls = []
        def interrupted(path, value):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("receipt interrupted after preserved rename")
            return original(path, value)
        with patch.object(recovery, "_write_json", interrupted), self.assertRaisesRegex(OSError, "receipt interrupted"):
            recovery.recover_packages(self.core)
        self.assertFalse(folder.exists())
        self.assertFalse(marker.exists())
        receipt = json.loads(next((self.core / ".mcu-package-recovery").glob("*/receipt.json")).read_text())
        self.assertEqual(receipt["state"], "pending")
        saved = Path(receipt["preserved"])
        self.assertEqual((saved / "payload").read_bytes(), b"original binary bytes\x00\xff")
        self.assertEqual((saved / ".piopm").read_bytes(), b"")
        self.assertEqual(recovery.recover_packages(self.core), [])

    def test_windows_readonly_payload_verification_does_not_flush_read_handles(self):
        recovery = self.recovery()
        source = self.package()
        with patch.object(recovery, "sys", SimpleNamespace(platform="win32")), \
                patch.object(recovery.os, "fsync", side_effect=AssertionError("Read handles cannot be flushed on Windows")):
            signature = recovery._tree_signature(source, sync=True)
        self.assertIn("payload", signature)

    def test_interrupted_payload_copy_is_absent_from_store_and_retry_succeeds(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source = self.package(parent=".cache/tmp")
        target = self.core / "packages/demo"
        target.parent.mkdir()
        def interrupted(src, dst, *args, **kwargs):
            Path(dst).mkdir()
            (Path(dst) / ".piopm").write_bytes((Path(src) / ".piopm").read_bytes())
            raise OSError("copy interrupted")
        with patch.object(shutil, "copytree", interrupted), recovery.atomic_package_writes(self.core):
            with self.assertRaisesRegex(OSError, "copy interrupted"):
                installer.shutil.copytree(str(source), str(target), symlinks=True)
        self.assertFalse(target.exists())
        self.assertTrue(list((self.core / ".mcu-package-staging").glob("*/.piopm")))
        with recovery.atomic_package_writes(self.core):
            installer.shutil.copytree(str(source), str(target), symlinks=True)
        self.assertEqual((target / "payload").read_bytes(), (source / "payload").read_bytes())
        self.assertEqual(PackageItem(str(target)).metadata.name, "demo")
        self.assertEqual(recovery.recover_packages(self.core), [])

    def test_completed_copy_is_checked_before_publication_and_existing_target_is_retained(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source = self.package(parent=".cache/tmp")
        target = self.core / "packages/demo"
        target.parent.mkdir()
        real = shutil.copytree
        def damaged(src, dst, *args, **kwargs):
            result = real(src, dst, *args, **kwargs)
            (Path(dst) / "payload").write_bytes(b"silently damaged")
            return result
        with patch.object(shutil, "copytree", damaged), recovery.atomic_package_writes(self.core):
            with self.assertRaisesRegex(RuntimeError, "bytes|verification"):
                installer.shutil.copytree(str(source), str(target), symlinks=True)
        self.assertFalse(target.exists())
        target.mkdir()
        (target / "user-data").write_text("retain")
        with recovery.atomic_package_writes(self.core), self.assertRaisesRegex(RuntimeError, "existing|appeared"):
            installer.shutil.copytree(str(source), str(target), symlinks=True)
        self.assertEqual((target / "user-data").read_text(), "retain")


if __name__ == "__main__":
    unittest.main(verbosity=2)
