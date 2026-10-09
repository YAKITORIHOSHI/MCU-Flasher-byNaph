#!/usr/bin/env python3
"""Native CLI preparation/discovery fixtures; no live downloads, stores or hardware."""
from __future__ import annotations

from contextlib import ExitStack
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.ubuntu import arduino_cli
from main.platforms import ubuntu_arduino


def archive_bytes(*, executable=None, entries=()):
    executable = executable or b"\x7fELF\x02\x01" + b"\0" * 12 + b"\x3e\0" + b"fixture native payload"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as bundle:
        member = tarfile.TarInfo("arduino-cli")
        member.size = len(executable)
        bundle.addfile(member, io.BytesIO(executable))
        for name, kind in entries:
            member = tarfile.TarInfo(name)
            if kind == "link":
                member.type = tarfile.SYMTYPE
                member.linkname = "arduino-cli"
                bundle.addfile(member)
            else:
                member.size = 1
                bundle.addfile(member, io.BytesIO(b"x"))
    return stream.getvalue()


@unittest.skipUnless(sys.platform.startswith("linux"), "Native Ubuntu CLI fixtures")
class ArduinoCliChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        audit = ROOT / "temp/audit/ubuntu-parity/bootstrap"
        audit.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=audit)))
        self.home = self.root / "fixture-home"
        self.home.mkdir()
        self.stack.enter_context(patch.object(arduino_cli, "ROOT", self.root))
        self.stack.enter_context(patch.object(ubuntu_arduino, "ROOT", self.root))
        self.stack.enter_context(patch.object(Path, "home", return_value=self.home))
        self.stack.enter_context(patch.object(arduino_cli.os, "geteuid", return_value=1000))
        self.stack.enter_context(patch.object(arduino_cli.platform, "machine", return_value="x86_64"))
        self.stack.enter_context(patch.object(ubuntu_arduino.shutil, "which", return_value=None))
        self.stack.enter_context(patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "", "MCU_FLASHER_WORKSPACE_RUNTIME": ""}))
        self.network = self.stack.enter_context(patch.object(arduino_cli, "urlopen", side_effect=AssertionError("Live network attempted")))
        self.version = self.stack.enter_context(patch.object(arduino_cli, "_version", return_value=arduino_cli.VERSION))
        self.messages = []

    def install(self, payload=None):
        payload = archive_bytes() if payload is None else payload
        with patch.object(arduino_cli, "ARCHIVE_SHA256", hashlib.sha256(payload).hexdigest()), \
                patch.object(arduino_cli, "urlopen", return_value=io.BytesIO(payload)):
            return arduino_cli.ensure_arduino_cli(env={"PATH": "/fixture"}, log=self.messages.append)

    def script(self, path, text='#!/bin/sh\nexit 0\n'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)
        return str(path)

    def test_empty_store_prepares_verified_native_cli_and_receipt(self):
        executable = self.install()
        path = Path(executable)
        self.assertTrue(arduino_cli._native_amd64(path))
        self.assertTrue(os.access(path, os.X_OK))
        receipt = json.loads((path.parent / "installation.json").read_text())
        self.assertEqual(receipt["owner"], arduino_cli.OWNER)
        self.assertEqual(receipt["executable_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(ubuntu_arduino.find_arduino_cli(), executable)
        self.assertFalse(any(path.parent.parent.glob(".arduino-cli-stage-*")))

    def test_warm_store_checks_binary_hash_and_version_without_download(self):
        executable = self.install()
        receipt = json.loads((Path(executable).parent / "installation.json").read_text())
        with patch.object(arduino_cli, "ARCHIVE_SHA256", receipt["archive_sha256"]):
            self.assertEqual(arduino_cli.ensure_arduino_cli(log=self.messages.append), executable)
        self.network.assert_not_called()
        self.version.assert_called()

    def test_damaged_owned_binary_is_repaired(self):
        executable = self.install()
        Path(executable).write_bytes(b"MZ corrupt Windows replacement")
        self.assertEqual(self.install(), executable)
        self.assertTrue(arduino_cli._native_amd64(Path(executable)))

    def test_failed_checksum_preserves_previous_owned_installation(self):
        executable = self.install()
        before = Path(executable).read_bytes()
        with patch.object(arduino_cli, "ARCHIVE_SHA256", "0" * 64), \
                patch.object(arduino_cli, "urlopen", return_value=io.BytesIO(archive_bytes())):
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                arduino_cli.ensure_arduino_cli(log=self.messages.append)
        self.assertEqual(Path(executable).read_bytes(), before)

    def test_failed_version_preserves_previous_owned_installation(self):
        executable = self.install()
        before = Path(executable).read_bytes()
        self.version.return_value = "0.1.0"
        with self.assertRaisesRegex(RuntimeError, "version validation"):
            self.install()
        self.assertEqual(Path(executable).read_bytes(), before)

    def test_failed_promotion_restores_previous_owned_files(self):
        executable = self.install()
        before = Path(executable).read_bytes()
        self.version.side_effect = ["0.1.0", arduino_cli.VERSION]
        original = Path.rename
        def rename(path, target):
            if path.name == "prepared":
                raise OSError("fixture promotion failure")
            return original(path, target)
        with patch.object(Path, "rename", rename):
            with self.assertRaisesRegex(OSError, "promotion failure"):
                self.install()
        self.assertEqual(Path(executable).read_bytes(), before)

    def test_failed_promotion_and_restore_preserve_previous_files_outside_staging(self):
        executable = self.install()
        installed = Path(executable).parent
        before = {path.name: path.read_bytes() for path in installed.iterdir()}
        self.version.side_effect = ["0.1.0", arduino_cli.VERSION]
        original = Path.rename
        def rename(path, target):
            if path.name == "prepared" or path.name.startswith(".arduino-cli-backup-"):
                raise OSError("fixture promotion and restoration failure")
            return original(path, target)
        with patch.object(Path, "rename", rename):
            with self.assertRaisesRegex(RuntimeError, "previous installation is preserved") as failure:
                self.install()
        tools = installed.parent
        backups = list(tools.glob(".arduino-cli-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn(str(backups[0]), str(failure.exception))
        self.assertEqual({path.name: path.read_bytes() for path in backups[0].iterdir()}, before)
        self.assertFalse(any(tools.glob(".arduino-cli-stage-*")))
        self.assertFalse(installed.exists())

    def test_healthy_account_cli_is_reused_without_owned_store_or_download(self):
        executable = self.script(self.home / ".local/bin/arduino-cli")
        self.assertEqual(arduino_cli.ensure_arduino_cli(log=self.messages.append), executable)
        self.assertFalse((self.root / ".ubuntu-tools").exists())
        self.network.assert_not_called()

    def test_broken_account_cli_is_preserved_and_native_store_is_prepared(self):
        external = Path(self.script(self.home / ".local/bin/arduino-cli"))
        before = external.read_bytes()
        self.version.side_effect = [None, arduino_cli.VERSION]
        installed = self.install()
        self.assertNotEqual(installed, str(external))
        self.assertEqual(external.read_bytes(), before)
        self.assertEqual(ubuntu_arduino.find_arduino_cli(), installed)

    def test_discovery_rejects_copied_windows_binary_and_suffixes(self):
        for name in ("arduino-cli.exe", "arduino-cli.cmd", "arduino-cli"):
            path = self.home / name
            path.write_bytes(b"MZ windows")
            path.chmod(0o755)
            self.assertIsNone(ubuntu_arduino._native_executable(path))
        link = self.home / "native-looking"
        link.symlink_to(self.home / "arduino-cli.exe")
        self.assertIsNone(ubuntu_arduino._native_executable(link))

    def test_discovery_never_starts_process_or_network_and_keeps_literal_path(self):
        executable = self.script(self.home / 'bin/arduino-cli')
        with patch.object(subprocess, "run", side_effect=AssertionError("Discovery spawned a child")):
            self.assertEqual(ubuntu_arduino.find_arduino_cli(), executable)
        self.network.assert_not_called()

    def test_path_lookup_accepts_a_native_account_wrapper(self):
        executable = self.script(self.root / 'path Ω with spaces $cash/arduino-cli')
        with patch.object(ubuntu_arduino.shutil, "which", return_value=executable):
            self.assertEqual(ubuntu_arduino.find_arduino_cli(), executable)

    def test_unsafe_or_wrong_architecture_archives_are_rejected(self):
        for payload in (archive_bytes(entries=(("../escape", "file"),)),
                        archive_bytes(entries=(("/absolute", "file"),)),
                        archive_bytes(entries=(("linked", "link"),)),
                        archive_bytes(executable=b"MZ wrong host"),
                        archive_bytes(executable=b"\x7fELF\x02\x01" + b"\0" * 12 + b"\xb7\0")):
            with self.subTest(payload=hashlib.sha256(payload).hexdigest()):
                with self.assertRaises(RuntimeError):
                    self.install(payload)
                self.assertFalse((self.root / ".ubuntu-tools/arduino-cli").exists())
        self.assertFalse((self.root / "escape").exists())

    def test_unrecognized_files_and_invalid_receipts_are_preserved(self):
        folder = self.root / ".ubuntu-tools/arduino-cli"
        folder.mkdir(parents=True)
        sentinel = folder / "user-sketch.ino"
        sentinel.write_text("user content")
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            arduino_cli.ensure_arduino_cli(log=self.messages.append)
        self.assertEqual(sentinel.read_text(), "user content")
        sentinel.unlink()
        (folder / "arduino-cli").write_bytes(b"old executable")
        (folder / "installation.json").write_text("[]")
        with self.assertRaisesRegex(RuntimeError, "ownership receipt"):
            arduino_cli.ensure_arduino_cli(log=self.messages.append)
        self.assertEqual((folder / "arduino-cli").read_bytes(), b"old executable")
        self.network.assert_not_called()

    def test_external_tools_symlink_is_preserved(self):
        external = self.root / "user-owned"
        external.mkdir()
        sentinel = external / "keep.txt"
        sentinel.write_text("keep")
        (self.root / ".ubuntu-tools").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "local directory"):
            arduino_cli.ensure_arduino_cli(log=self.messages.append)
        self.assertEqual(sentinel.read_text(), "keep")
        self.network.assert_not_called()

    def test_owned_binary_directory_is_not_removed(self):
        folder = self.root / ".ubuntu-tools/arduino-cli"
        (folder / "arduino-cli").mkdir(parents=True)
        sentinel = folder / "arduino-cli/user.txt"
        sentinel.write_text("keep")
        (folder / "installation.json").write_text(json.dumps({"owner": arduino_cli.OWNER}))
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            arduino_cli.ensure_arduino_cli(log=self.messages.append)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_root_unsupported_host_and_workspace_process_never_download(self):
        with patch.object(arduino_cli.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "desktop account"):
                arduino_cli.ensure_arduino_cli()
        with patch.object(arduino_cli.platform, "machine", return_value="aarch64"):
            with self.assertRaisesRegex(RuntimeError, "amd64"):
                arduino_cli.ensure_arduino_cli()
        with patch.dict(os.environ, {"MCU_FLASHER_WORKSPACE_RUNTIME": "1"}):
            with self.assertRaisesRegex(RuntimeError, "Bootstrap"):
                arduino_cli.ensure_arduino_cli()
        self.network.assert_not_called()

    def test_download_size_limit_cleans_stage(self):
        with patch.object(arduino_cli, "MAX_ARCHIVE_BYTES", 8):
            with self.assertRaisesRegex(RuntimeError, "size/time limit"):
                self.install()
        self.assertFalse(any((self.root / ".ubuntu-tools").glob(".arduino-cli-stage-*")))

    def test_real_version_probe_uses_literal_argv_and_disposable_arduino_directories(self):
        executable = self.script(self.root / 'CLI Ω $literal with spaces/arduino-cli',
            '#!/bin/sh\n[ "$1" = version ] || exit 3\n'
            '[ "$PWD" = "$ARDUINO_DIRECTORIES_DATA" ] || exit 4\n'
            '[ "$ARDUINO_DIRECTORIES_USER" = "$PWD" ] || exit 5\n'
            '[ "$ARDUINO_DIRECTORIES_DOWNLOADS" = "$PWD" ] || exit 6\n'
            'printf "arduino-cli  Version: 1.5.1 Commit: fixture\\n"\n')
        with patch.object(arduino_cli, "_version", ArduinoCliChecks.real_version):
            self.assertEqual(arduino_cli._version(executable, {"PATH": "/usr/bin"}), arduino_cli.VERSION)
        self.assertFalse((self.root / "literal").exists())


ArduinoCliChecks.real_version = staticmethod(arduino_cli._version)

if __name__ == "__main__":
    unittest.main(verbosity=2)
