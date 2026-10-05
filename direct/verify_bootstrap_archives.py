#!/usr/bin/env python3
"""Check local bootstrap archive copy/cleanup in isolated owned fixtures."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import bootstrap_platformio as entry
from platformio.package.exception import PackageException
from platformio.package.manager import _install as installer
from platformio.package.manager.tool import ToolPackageManager
from platformio.package.meta import PackageSpec
from platformio.package.unpack import FileUnpacker


class BootstrapArchiveChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit"
        audit.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="bootstrap-archives-", dir=audit))
        self.core = self.root / ".platformio-mcu-gui"
        (self.core / "packages").mkdir(parents=True)
        (self.core / ".cache/tmp").mkdir(parents=True)
        environment = patch.dict(os.environ, {"PLATFORMIO_CORE_DIR": str(self.core)})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("MCU_FLASHER_OFFLINE_RUNTIME", None)
        self.addCleanup(self.cleanup_fixture)

    def cleanup_fixture(self):
        self.assertEqual(self.root.parent.resolve(), (ROOT / "temp/audit").resolve())
        self.assertEqual(self.root.resolve().parent, (ROOT / "temp/audit").resolve())
        path = entry.extended_windows_path(self.root) if sys.platform == "win32" else self.root
        shutil.rmtree(path)

    def archive(self, kind="zip", version="1.0.0", insecure=False):
        path = self.root / (f"tool-{version}." + kind)
        self.relative = ("arm-none-eabi/include/c++/8.2.1/ext/pb_ds/detail/"
                         "left_child_next_sibling_heap_/point_const_iterator."
                         + ("hpp" if kind == "zip" else ""))
        self.deep = "deep/" + "/".join(["segment-" + "x" * 25] * 8) + "/payload.txt"
        files = {
            "package.json": json.dumps({"name": "tool-fixture", "version": version,
                                         "description": "isolated verifier package"}).encode(),
            self.relative: b"literal trailing-dot file",
            self.deep: b"deep archived file",
        }
        if insecure:
            files["../outside.txt"] = b"must stay blocked"
        if kind == "zip":
            with zipfile.ZipFile(path, "w") as writer:
                for name, payload in files.items():
                    writer.writestr(name, payload)
        else:
            with tarfile.open(path, "w:gz") as writer:
                for name, payload in files.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    writer.addfile(info, io.BytesIO(payload))
        return path

    def manager(self):
        manager = ToolPackageManager(str(self.core / "packages"))
        manager._tmp_dir = str(self.core / ".cache/tmp")
        return manager

    def install(self, manager, archive, version):
        return manager.install_from_uri(
            "file://" + str(archive),
            PackageSpec(owner="fixture", name="tool-fixture", requirements=version))

    def assert_payloads(self, package):
        for relative, content in ((self.relative, b"literal trailing-dot file"),
                                  (self.deep, b"deep archived file")):
            base = entry.extended_windows_path(package.path) if sys.platform == "win32" else package.path
            native = os.path.normpath(os.path.join(base, relative))
            with open(native, "rb") as stream:
                self.assertEqual(stream.read(), content)
        self.assertEqual(list((self.core / ".cache/tmp").iterdir()), [])

    def test_real_zip_installs_exact_names_deep_paths_and_cleans_staging(self):
        with entry.archive_paths():
            package = self.install(self.manager(), self.archive(), "1.0.0")
        self.assert_payloads(package)

    def test_real_tar_installs_exact_names_deep_paths_and_cleans_staging(self):
        with entry.archive_paths():
            package = self.install(self.manager(), self.archive("tar.gz"), "1.0.0")
        self.assert_payloads(package)

    def test_version_detach_copies_exact_names_and_overwrite_cleanup(self):
        manager = self.manager()
        with entry.archive_paths():
            for version in ("2.0.0", "1.0.0", "3.0.0", "3.0.0"):
                package = self.install(manager, self.archive("tar.gz", version=version), version)
                self.assert_payloads(package)
        for folder in ("tool-fixture", "tool-fixture@1.0.0", "tool-fixture@2.0.0"):
            self.assertTrue((self.core / "packages" / folder).is_dir())

    def test_zip_and_tar_still_block_traversal(self):
        for kind in ("zip", "tar.gz"):
            archive = self.archive(kind, insecure=True)
            destination = self.core / ".cache/tmp/pkg-installing-traversal"
            destination.mkdir(exist_ok=True)
            with entry.archive_paths(), FileUnpacker(str(archive)) as unpacker:
                with self.assertRaises((PackageException, tarfile.FilterError)):
                    unpacker.unpack(str(destination), with_progress=False)
            self.assertFalse((self.core / ".cache/tmp/outside.txt").exists())
            self.assertFalse((self.root / "outside.txt").exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows namespace adaptation")
    def test_installer_only_adapts_contained_package_and_staging_paths(self):
        copy, remove = Mock(), Mock()
        with patch.object(shutil, "copytree", copy), patch.object(installer.fs, "rmtree", remove), \
                entry.archive_paths():
            source = self.core / ".cache/tmp/pkg-installing-fixture/source"
            destination = self.core / "packages/tool-fixture"
            installer.shutil.copytree(source, destination, symlinks=True)
            copy.assert_called_once_with(entry.extended_windows_path(source),
                                         entry.extended_windows_path(destination), symlinks=True)
            installer.fs.rmtree(source)
            remove.assert_called_once_with(entry.extended_windows_path(source))
            copy.reset_mock()
            remove.reset_mock()
            outside = self.root / "outside"
            installer.shutil.copytree(outside, destination)
            copy.assert_called_once_with(outside, destination)
            for target in (self.core, self.core / "packages", self.core / ".cache/tmp",
                           self.core / ".cache/tmp/unrelated", outside,
                           self.root / ".platformio-mcu-gui-sibling/packages/tool"):
                installer.fs.rmtree(target)
                self.assertEqual(remove.call_args.args, (target,))
            with patch.dict(os.environ, {}, clear=True):
                installer.fs.rmtree(source)
                self.assertEqual(remove.call_args.args, (source,))

    @unittest.skipUnless(sys.platform == "win32", "Windows junction containment")
    def test_canonical_containment_keeps_junction_escape_unadapted(self):
        outside = self.root / "external-package"
        outside.mkdir()
        junction = self.core / "packages/tool-fixture"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        try:
            remove, copy = Mock(), Mock()
            with patch.object(shutil, "copytree", copy), patch.object(installer.fs, "rmtree", remove), \
                    entry.archive_paths():
                installer.fs.rmtree(junction)
                remove.assert_called_once_with(junction)
                source = self.core / ".cache/tmp/pkg-installing-fixture"
                installer.shutil.copytree(source, junction)
                copy.assert_called_once_with(source, junction)
        finally:
            self.assertEqual(junction.parent.resolve(), (self.core / "packages").resolve())
            os.rmdir(junction)  # Remove only the owned junction entry, not its target.
        self.assertTrue(outside.is_dir())

    def test_hooks_restore_after_failure_and_linux_leaves_installer_native(self):
        original_fs, original_shutil, original_unpack = installer.fs, installer.shutil, FileUnpacker.unpack
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with entry.archive_paths():
                raise RuntimeError("fixture failure")
        self.assertIs(installer.fs, original_fs)
        self.assertIs(installer.shutil, original_shutil)
        self.assertIs(FileUnpacker.unpack, original_unpack)
        with patch.object(entry.sys, "platform", "linux"), entry.archive_paths():
            self.assertIs(installer.fs, original_fs)
            self.assertIs(installer.shutil, original_shutil)
            self.assertIs(FileUnpacker.unpack, original_unpack)

    def test_offline_runtime_still_refuses_installer_context(self):
        with patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "1"}):
            with self.assertRaisesRegex(RuntimeError, "outside the workspace"):
                with entry.archive_paths():
                    self.fail("offline runtime must not install packages")


if __name__ == "__main__":
    unittest.main(verbosity=2)
