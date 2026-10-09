#!/usr/bin/env python3
"""Bootstrap OpenCode preparation using fixture archives and mocked version probes."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from direct.ubuntu import opencode_setup as setup
from main.platforms import ubuntu_opencode as discovery


def elf_binary():
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = b"\x3e\x00"
    return bytes(header) + b"Native OpenCode fixture; never executed."


def archive_bytes(members=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, content, kind in members or [("opencode", elf_binary(), tarfile.REGTYPE)]:
            member = tarfile.TarInfo(name)
            member.type, member.size = kind, len(content)
            if kind == tarfile.SYMTYPE:
                member.linkname = "/tmp/unrelated"
                member.size = 0
            archive.addfile(member, io.BytesIO(content) if kind == tarfile.REGTYPE else None)
    return stream.getvalue()


class Response(io.BytesIO):
    def geturl(self):
        return "https://release-assets.githubusercontent.com/fixture/archive"


class OpenCodeBootstrapChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        audit = ROOT / "temp/audit/ubuntu-opencode-setup"
        audit.mkdir(parents=True, exist_ok=True)
        self.fixture = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="checks-", dir=audit)))
        self.app, self.home = self.fixture / "application", self.fixture / "user"
        self.app.mkdir()
        self.home.mkdir()
        self.directory = self.app / ".ubuntu-tools/opencode"
        self.stack.enter_context(patch.object(setup, "ROOT", self.app))
        self.stack.enter_context(patch.object(discovery, "ROOT", self.app))
        self.stack.enter_context(patch.object(discovery.Path, "home", return_value=self.home))
        self.which = self.stack.enter_context(patch.object(discovery.shutil, "which", return_value=None))
        self.stack.enter_context(patch.object(setup.platform, "machine", return_value="x86_64"))
        self.stack.enter_context(patch.object(setup.os, "geteuid", return_value=1000))
        self.stack.enter_context(patch.dict(os.environ, {"MCU_FLASHER_WORKSPACE_RUNTIME": "", "MCU_FLASHER_OFFLINE_RUNTIME": ""}))
        self.run = self.stack.enter_context(patch.object(setup.subprocess, "run", return_value=
            subprocess.CompletedProcess([], 0, setup.VERSION + "\n")))
        self.archive = archive_bytes()
        self.stack.enter_context(patch.object(setup, "ARCHIVE_SIZE", len(self.archive)))
        self.stack.enter_context(patch.object(setup, "ARCHIVE_SHA256", hashlib.sha256(self.archive).hexdigest()))
        self.open = self.stack.enter_context(patch.object(setup.urllib.request, "urlopen",
                                                        side_effect=lambda *_args, **_kwargs: Response(self.archive)))
        self.log = Mock()

    def ensure(self):
        return setup.ensure_opencode_cli(env={"PATH": "/fixture/bin", "LANG": "C.UTF-8"}, log=self.log)

    def test_missing_cli_is_prepared_and_runtime_discovered_without_auth(self):
        result = self.ensure()
        self.assertEqual(result, str(self.directory / "opencode"))
        self.assertEqual((self.directory / "opencode").read_bytes(), elf_binary())
        self.assertEqual((self.directory / "opencode").stat().st_mode & 0o777, 0o755)
        state = json.loads((self.directory / "installation.json").read_text())
        self.assertEqual(state["owner"], discovery.MANAGED_OWNER)
        self.assertEqual(state["binary_sha256"], hashlib.sha256(elf_binary()).hexdigest())
        self.assertEqual(discovery.find_opencode_cli(), result)
        self.open.assert_called_once()
        self.assertEqual(self.run.call_args.args[0][1:], ["--version"])
        self.assertEqual(self.run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_working_user_installation_is_reused_without_download_or_profile_changes(self):
        binary = self.home / ".local/bin/opencode"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"#!/bin/sh\n# Never executed\n")
        binary.chmod(0o755)
        self.which.return_value = str(binary)
        self.run.return_value = subprocess.CompletedProcess([], 0, "opencode 1.17.2\n")
        self.assertEqual(self.ensure(), str(binary))
        self.open.assert_not_called()
        self.assertFalse(self.directory.exists())
        self.assertEqual(binary.read_bytes(), b"#!/bin/sh\n# Never executed\n")
        self.assertEqual(self.run.call_args.args[0], [str(binary), "--version"])
        self.assertNotEqual(self.run.call_args.kwargs["cwd"], str(self.app))

    def test_managed_certificate_hash_and_version_are_checked_without_redownload(self):
        installed = self.ensure()
        self.open.reset_mock()
        self.run.reset_mock()
        self.assertEqual(self.ensure(), installed)
        self.open.assert_not_called()
        self.assertEqual(self.run.call_args.args[0], [installed, "--version"])

    def test_failed_external_command_uses_owned_binary_without_overwriting_external(self):
        binary = self.home / ".local/bin/opencode"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"#!/bin/sh\n# Broken command fixture\n")
        binary.chmod(0o755)
        self.which.return_value = str(binary)
        self.run.side_effect = lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1 if argv[0] == str(binary) else 0, setup.VERSION)
        installed = self.ensure()
        self.assertEqual(installed, str(self.directory / "opencode"))
        self.assertEqual(discovery.find_opencode_cli(), installed)
        self.assertEqual(binary.read_bytes(), b"#!/bin/sh\n# Broken command fixture\n")

    def test_failed_hash_truncation_and_download_errors_do_not_publish_installation(self):
        for failure in ("digest", "truncation", "http"):
            with self.subTest(failure=failure):
                if failure == "digest":
                    override = patch.object(setup, "ARCHIVE_SHA256", "0" * 64)
                elif failure == "truncation":
                    override = patch.object(setup, "ARCHIVE_SIZE", len(self.archive) + 1)
                else:
                    override = patch.object(setup.urllib.request, "urlopen", side_effect=OSError("HTTP fixture failure"))
                with override, self.assertRaises((RuntimeError, OSError)):
                    self.ensure()
                self.assertFalse(self.directory.exists())
                self.assertFalse(any(path.name.startswith(".opencode-prepare-") for path in self.directory.parent.iterdir()))

    def test_unrecognized_folder_and_symlink_are_preserved_and_not_downloaded(self):
        self.directory.mkdir(parents=True)
        user_file = self.directory / "user.txt"
        user_file.write_text("Keep me")
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            self.ensure()
        self.assertEqual(user_file.read_text(), "Keep me")
        user_file.unlink()
        self.directory.rmdir()
        target = self.fixture / "external"
        target.mkdir()
        self.directory.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symbolic links"):
            self.ensure()
        self.assertTrue(self.directory.is_symlink())
        self.open.assert_not_called()

    def test_failed_staged_version_keeps_previous_owned_binary_and_certificate(self):
        self.ensure()
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        self.run.side_effect = lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1 if argv[0] == str(self.directory / "opencode") else 0, "0.0.1")
        with self.assertRaisesRegex(RuntimeError, "--version"):
            self.ensure()
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_failed_atomic_promotion_restores_the_previous_owned_installation(self):
        self.ensure()
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        self.run.side_effect = lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1 if argv[0] == str(self.directory / "opencode") else 0, setup.VERSION)
        replace = setup.os.replace
        def fail_promotion(source, target):
            if Path(source).name == "installation":
                raise OSError("promotion fixture failure")
            return replace(source, target)
        with patch.object(setup.os, "replace", side_effect=fail_promotion), self.assertRaises(OSError):
            self.ensure()
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_tar_traversal_links_extra_files_and_foreign_binaries_are_rejected(self):
        members = [
            [("../opencode", elf_binary(), tarfile.REGTYPE)],
            [("/opencode", elf_binary(), tarfile.REGTYPE)],
            [("opencode", b"", tarfile.SYMTYPE)],
            [("unrelated", elf_binary(), tarfile.REGTYPE)],
            [("opencode", b"MZWindows binary", tarfile.REGTYPE)],
            [("opencode", elf_binary(), tarfile.REGTYPE), ("extra", b"x", tarfile.REGTYPE)],
        ]
        for index, entries in enumerate(members):
            with self.subTest(entries=entries):
                archive = self.fixture / f"invalid-{index}.tar.gz"
                archive.write_bytes(archive_bytes(entries))
                with self.assertRaises(RuntimeError):
                    setup._extract(archive, self.fixture / f"extracted-{index}")
        self.assertFalse((self.fixture.parent / "opencode").exists())

    def test_extraction_limits_are_enforced(self):
        archive = self.fixture / "limited.tar.gz"
        archive.write_bytes(self.archive)
        with patch.object(setup, "MAX_BINARY_BYTES", 1), self.assertRaisesRegex(RuntimeError, "limit"):
            setup._extract(archive, self.fixture / "limited")

    def test_bootstrap_guard_and_unsupported_architecture_stop_before_installation(self):
        with patch.dict(os.environ, {"MCU_FLASHER_WORKSPACE_RUNTIME": "1"}), self.assertRaisesRegex(RuntimeError, "Bootstrap-only"):
            self.ensure()
        with patch.object(setup.platform, "machine", return_value="aarch64"), self.assertRaisesRegex(RuntimeError, "amd64"):
            self.ensure()
        with patch.object(setup.os, "geteuid", return_value=0), self.assertRaisesRegex(RuntimeError, "without sudo"):
            self.ensure()
        self.open.assert_not_called()
        self.run.assert_not_called()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Native Linux flock check")
    def test_concurrent_bootstraps_download_once_and_recheck_owned_installation(self):
        downloading, release = Event(), Event()
        download = setup._download
        def held_download(destination, log):
            downloading.set()
            if not release.wait(5):
                raise RuntimeError("Fixture download release was not received")
            return download(destination, log)
        with patch.object(setup, "_download", side_effect=held_download) as downloads:
            with ThreadPoolExecutor(max_workers=2) as workers:
                first = workers.submit(self.ensure)
                try:
                    self.assertTrue(downloading.wait(5))
                    second = workers.submit(self.ensure)
                    self.assertFalse(second.done())
                    self.assertEqual(downloads.call_count, 1)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=5), second.result(timeout=5))
            self.assertEqual(downloads.call_count, 1)
        self.open.assert_called_once()
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(discovery.find_opencode_cli(), str(self.directory / "opencode"))
        self.assertEqual({p.name for p in self.directory.parent.iterdir()}, {"opencode", ".opencode.lock"})

    @unittest.skipUnless(sys.platform.startswith("linux"), "Native Linux flock check")
    def test_lock_contention_is_bounded_and_releases_after_failure(self):
        import fcntl
        tools = self.directory.parent
        tools.mkdir(parents=True)
        descriptor = os.open(tools / ".opencode.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(setup, "INSTALL_LOCK_TIMEOUT", 0.05), self.assertRaisesRegex(RuntimeError, "still active"):
                self.ensure()
            self.open.assert_not_called()
            self.run.assert_not_called()
        finally:
            os.close(descriptor)
        self.assertEqual(self.ensure(), str(self.directory / "opencode"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Native Linux lock path check")
    def test_symlink_lock_is_preserved_without_opening_target_or_downloading(self):
        tools = self.directory.parent
        tools.mkdir(parents=True)
        target = self.fixture / "unrelated-lock.txt"
        target.write_text("Preserve unrelated bytes")
        (tools / ".opencode.lock").symlink_to(target)
        with self.assertRaises(OSError):
            self.ensure()
        self.assertEqual(target.read_text(), "Preserve unrelated bytes")
        self.open.assert_not_called()
        self.run.assert_not_called()

    def test_explicit_fixture_root_contains_all_owned_installation_paths(self):
        app = self.fixture / "explicit-root"
        app.mkdir()
        installed = setup.ensure_opencode_cli(root=app, env={"PATH": "/fixture/bin"}, log=self.log)
        self.assertEqual(installed, str(app / ".ubuntu-tools/opencode/opencode"))
        self.assertFalse((self.app / ".ubuntu-tools").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
