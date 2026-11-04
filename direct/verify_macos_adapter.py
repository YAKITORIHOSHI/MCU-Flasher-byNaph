#!/usr/bin/env python3
"""macOS host adapters, with fabricated binaries and fake system tools only.

Runs on any POSIX host and never downloads, installs, opens a dialog, reads a real
Keychain or starts the application. other-platforms.yml runs the real first launch
on Intel and Apple Silicon Macs.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.ubuntu import arduino_cli, opencode_setup
from main.core.credential_store import CredentialStore, SecureStorageError
from main.platforms import get_platform_backend, ubuntu_arduino, ubuntu_opencode, ubuntu_pty_process, ubuntu_pty_supervisor
from src.modules import bootstrap_package_recovery, offline_runtime, platform_runtime, private_python_guard
from src.modules import ubuntu_architecture as architecture
from src.modules import ubuntu_download_manager as manager

THIN_ARM = struct.pack("<II", 0xFEEDFACF, 0x0100000C) + b"\0" * 24
THIN_INTEL = struct.pack("<II", 0xFEEDFACF, 0x01000007) + b"\0" * 24


def universal(*cpus, magic=0xCAFEBABE, entry=20):
    body = struct.pack(">II", magic, len(cpus))
    for cpu in cpus:
        body += struct.pack(">I", cpu) + b"\0" * (entry - 4)
    return body


def on_macos():
    return patch.object(sys, "platform", "darwin")


class BinaryIdentity(unittest.TestCase):
    def matches(self, data, machine):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "binary"
            path.write_bytes(data)
            with on_macos():
                return architecture.elf_matches_host(path, machine)

    def test_thin_and_universal_mach_o_must_contain_this_cpu(self):
        arm, intel = 0x0100000C, 0x01000007
        for data, machine, expected in ((THIN_ARM, "arm64", True), (THIN_ARM, "x86_64", False),
                                        (THIN_INTEL, "x86_64", True), (THIN_INTEL, "arm64", False),
                                        (universal(intel, arm), "arm64", True), (universal(intel, arm), "x86_64", True),
                                        (universal(intel), "arm64", False),
                                        (universal(intel, arm, magic=0xCAFEBABF, entry=32), "arm64", True),
                                        (universal(intel, magic=0xCAFEBABF, entry=32), "arm64", False)):
            with self.subTest(machine=machine, data=data[:12].hex()):
                self.assertEqual(self.matches(data, machine), expected)

    def test_linux_binaries_scripts_java_and_truncated_files_are_not_native(self):
        elf = b"\x7fELF\x02\x01\x01" + b"\0" * 11 + (183).to_bytes(2, "little")
        java_class = struct.pack(">II", 0xCAFEBABE, 65) + b"\0" * 32
        for data in (elf, b"#!/bin/bash\n", java_class, b"\xcf\xfa", b"", universal(0x0100000C)[:12]):
            with self.subTest(data=data[:8].hex()):
                self.assertFalse(self.matches(data, "arm64"))

    def test_macos_binaries_are_not_native_on_linux(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "binary"
            path.write_bytes(THIN_ARM)
            with patch.object(sys, "platform", "linux"):
                self.assertFalse(architecture.elf_matches_host(path, "arm64"))

    def test_cli_discovery_refuses_a_binary_for_the_other_mac_cpu(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tool"
            path.write_bytes(THIN_INTEL)
            path.chmod(0o755)
            wrapper = Path(folder) / "wrapper"
            wrapper.write_text("#!/bin/sh\n")
            wrapper.chmod(0o755)
            with on_macos(), patch.object(architecture.platform, "machine", return_value="arm64"):
                for module in (ubuntu_arduino, ubuntu_opencode):
                    self.assertIsNone(module._native_executable(path))
                    self.assertEqual(module._native_executable(wrapper), str(wrapper))

    def test_tool_receipts_separate_each_operating_system(self):
        with on_macos():
            self.assertEqual(architecture.native_tool_architecture("arm64"), "darwin-arm64")
            self.assertEqual(architecture.native_tool_architecture("x86_64"), "darwin-amd64")
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(architecture.native_tool_architecture("aarch64"), "linux-arm64")


class HostPaths(unittest.TestCase):
    def test_each_host_keeps_its_own_runtime_and_tools(self):
        with on_macos():
            self.assertEqual(platform_runtime.native_env_name(), ".venv-macos")
            self.assertEqual(platform_runtime.native_tools_name(), ".macos-tools")
            self.assertEqual(platform_runtime.repair_command(), "bash direct/macos/run.sh --repair")
            self.assertEqual(private_python_guard._native_env_dir().name, ".venv-macos")
            self.assertIn("direct/macos/run.sh --repair", offline_runtime.bootstrap_instruction("Missing"))
        with patch.object(sys, "platform", "linux"):
            self.assertEqual(platform_runtime.native_env_name(), ".venv-linux")
            self.assertEqual(platform_runtime.native_tools_name(), ".ubuntu-tools")
            self.assertEqual(platform_runtime.repair_command(), "bash direct/ubuntu/run.sh --repair")
            self.assertEqual(private_python_guard._native_env_dir().name, ".venv-linux")

    def test_release_and_source_updates_protect_macos_state(self):
        release = (ROOT / "direct/build_release.py").read_text(encoding="utf-8")
        self.assertIn("'.venv-macos'", release)
        self.assertIn("'.macos-tools'", release)
        update = (ROOT / "src/modules/app_update.py").read_text(encoding="utf-8")
        self.assertIn('".venv-macos"', update)
        self.assertIn('".macos-tools"', update)
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".venv-macos/", ignored)
        self.assertIn(".macos-tools/", ignored)

    def test_macos_uses_the_shared_native_backend(self):
        self.assertEqual(get_platform_backend("darwin").__name__, "main.platforms.ubuntu")


class ToolAssets(unittest.TestCase):
    def test_arduino_cli_selects_the_pinned_mac_asset_for_each_cpu(self):
        for machine, name, digest in (("arm64", "macOS_ARM64", "cb952e8c"), ("x86_64", "macOS_64bit", "c982e940")):
            with self.subTest(machine=machine), on_macos(), \
                    patch.object(architecture.platform, "machine", return_value=machine):
                url, sha256 = arduino_cli._archive_spec()
                self.assertIn(f"arduino-cli_{arduino_cli.VERSION}_{name}.tar.gz", url)
                self.assertTrue(sha256.startswith(digest))
                self.assertRegex(sha256, "^[0-9a-f]{64}$")

    def test_opencode_selects_the_pinned_mac_asset_for_each_cpu(self):
        for machine, name, digest, size in (("arm64", "opencode-darwin-arm64.zip", "80b05124", 45_540_968),
                                            ("x86_64", "opencode-darwin-x64-baseline.zip", "c29a3f70", 49_169_955)):
            with self.subTest(machine=machine), on_macos(), \
                    patch.object(architecture.platform, "machine", return_value=machine):
                asset, url, sha256, reviewed = opencode_setup._archive_spec()
                self.assertEqual((asset, reviewed), (name, size))
                self.assertTrue(url.endswith(f"/v{opencode_setup.VERSION}/{name}"))
                self.assertTrue(sha256.startswith(digest))

    def zip_with(self, folder, entries):
        path = Path(folder) / "release.zip"
        with zipfile.ZipFile(path, "w") as bundle:
            for name, data, mode in entries:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                bundle.writestr(info, data)
        return path

    def test_opencode_zip_yields_only_one_native_root_executable(self):
        with tempfile.TemporaryDirectory() as folder, on_macos(), \
                patch.object(architecture.platform, "machine", return_value="arm64"):
            archive = self.zip_with(folder, [("opencode", THIN_ARM, stat.S_IFREG | 0o755)])
            destination = Path(folder) / "opencode"
            opencode_setup._extract(archive, destination)
            self.assertEqual(destination.read_bytes(), THIN_ARM)
            self.assertTrue(os.access(destination, os.X_OK))

    def test_opencode_zip_refuses_extras_links_and_foreign_cpus(self):
        refused = ([("opencode", THIN_ARM, stat.S_IFREG | 0o755), ("extra", b"x", stat.S_IFREG | 0o644)],
                   [("../opencode", THIN_ARM, stat.S_IFREG | 0o755)],
                   [("nested/opencode", THIN_ARM, stat.S_IFREG | 0o755)],
                   [("opencode", b"target", stat.S_IFLNK | 0o777)],
                   [("opencode", THIN_INTEL, stat.S_IFREG | 0o755)])
        for entries in refused:
            with self.subTest(entries=[entry[0] for entry in entries]), tempfile.TemporaryDirectory() as folder, \
                    on_macos(), patch.object(architecture.platform, "machine", return_value="arm64"):
                archive = self.zip_with(folder, entries)
                with self.assertRaises(RuntimeError):
                    opencode_setup._extract(archive, Path(folder) / "opencode")


class Keychain(unittest.TestCase):
    def store(self):
        return CredentialStore(platform="darwin")

    def fake_tool(self):
        calls = []
        vault = {}

        def run(command, *, input=None, **_):
            calls.append((command, input))
            arguments = command[1:]
            if arguments == ["-i"]:
                words = input.split()
                vault[words[words.index("-a") + 1]] = words[words.index("-w") + 1]
                return subprocess.CompletedProcess(command, 0, "", "")
            account = arguments[arguments.index("-a") + 1]
            if arguments[0] == "find-generic-password":
                if account not in vault:
                    return subprocess.CompletedProcess(command, 44, "", "not found")
                return subprocess.CompletedProcess(command, 0, vault[account] + "\n", "")
            if arguments[0] == "delete-generic-password":
                return subprocess.CompletedProcess(command, 0 if vault.pop(account, None) else 44, "", "")
            raise AssertionError(arguments)
        return calls, vault, run

    def test_round_trip_keeps_unicode_and_never_puts_the_secret_in_argv(self):
        calls, vault, run = self.fake_tool()
        secret = {"password": 'p"a ss\\wörd 🔑 $(touch NEVER)', "user": "naph"}
        store = self.store()
        with patch.object(os, "access", return_value=True), patch("subprocess.run", side_effect=run):
            self.assertTrue(store.status[0])
            self.assertIsNone(store.get("cloud"))
            store.set("cloud", secret)
            self.assertEqual(store.get("cloud"), secret)
            store.delete("cloud")
            self.assertIsNone(store.get("cloud"))
            store.delete("cloud")  # Forgetting twice is not an error.
        for command, _ in calls:
            self.assertNotIn("wörd", " ".join(command))
            self.assertNotIn(json.dumps(secret, ensure_ascii=False).encode().hex(), " ".join(command))
        self.assertEqual(vault, {})

    def test_locked_or_failing_keychain_is_reported_and_never_treated_as_empty(self):
        store = self.store()
        locked = subprocess.CompletedProcess([], 36, "", "User interaction is not allowed.")
        with patch.object(os, "access", return_value=True), patch("subprocess.run", return_value=locked):
            for action in (lambda: store.get("cloud"), lambda: store.set("cloud", {"a": 1}), lambda: store.delete("cloud")):
                with self.assertRaises(SecureStorageError):
                    action()
        with patch.object(os, "access", return_value=False):
            self.assertFalse(store.status[0])
            with self.assertRaises(SecureStorageError):
                store.get("cloud")

    def test_damaged_stored_text_is_reported(self):
        store = self.store()
        with patch.object(os, "access", return_value=True), \
                patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "not-hex\n", "")):
            with self.assertRaisesRegex(SecureStorageError, "damaged"):
                store.get("cloud")


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Native sockets are required")
class DownloaderChannel(unittest.TestCase):
    def setUp(self):
        audit = Path(tempfile.gettempdir())
        self.temporary = tempfile.TemporaryDirectory(prefix="mcu-mac-", dir=audit)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "app"
        self.root.mkdir()

    def channel(self):
        return manager.DownloadManagerChannel(self.root)

    def test_address_is_a_short_path_in_the_private_temporary_folder(self):
        with on_macos(), patch.object(manager.tempfile, "gettempdir", return_value="/var/folders/ab/cdefghijklmnop/T"):
            address = manager._address(self.root)
        self.assertTrue(address.startswith("/var/folders/ab/cdefghijklmnop/T/mcu-dm-"))
        self.assertLessEqual(len(address.encode()), 104, "macOS's socket path limit")
        self.assertNotIn("\0", address)

    def test_peer_identity_reads_the_xucred_uid(self):
        peer = MagicMock()
        peer.getsockopt.return_value = struct.pack("IIh", 0, os.getuid(), 1) + b"\0" * 66
        with on_macos():
            self.assertTrue(manager._same_user(peer))
        peer.getsockopt.assert_called_once_with(0, 1, 76)
        peer.getsockopt.return_value = struct.pack("IIh", 0, os.getuid() + 1, 1) + b"\0" * 66
        with on_macos():
            self.assertFalse(manager._same_user(peer))

    def test_one_owner_and_a_crashed_owners_socket_never_blocks_the_next(self):
        with on_macos(), patch.object(manager, "_same_user", return_value=True), \
                patch.object(manager.tempfile, "gettempdir", return_value=self.temporary.name):
            address = manager._address(self.root)
            # A crash leaves the socket file behind with nothing listening.
            crashed = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            crashed.bind(address)
            crashed.close()
            self.assertTrue(os.path.exists(address))
            owner = self.channel()
            with self.assertRaises(manager.AlreadyRunning):
                self.channel()
            self.assertTrue(manager.request(self.root, "wake"))
            owner.close()
            self.assertFalse(os.path.exists(address))
            self.assertFalse(manager.request(self.root, "wake"))


class ExclusivePublish(unittest.TestCase):
    def test_macos_publish_uses_the_exclusive_rename(self):
        native = MagicMock()
        native.renamex_np.return_value = 0
        with tempfile.TemporaryDirectory() as folder, on_macos(), patch.object(ctypes, "CDLL", return_value=native), \
                patch.object(bootstrap_package_recovery, "_sync_dir"):
            source, destination = Path(folder) / "stage", Path(folder) / "final"
            source.mkdir()
            bootstrap_package_recovery._publish_absent(source, destination)
        native.renamex_np.assert_called_once_with(os.fsencode(source), os.fsencode(destination), 0x4)

    def test_macos_publish_failure_is_raised_and_existing_destination_is_kept(self):
        native = MagicMock()
        native.renamex_np.return_value = -1
        with tempfile.TemporaryDirectory() as folder, on_macos(), patch.object(ctypes, "CDLL", return_value=native), \
                patch.object(ctypes, "get_errno", return_value=17):
            source, destination = Path(folder) / "stage", Path(folder) / "final"
            source.mkdir()
            with self.assertRaises(OSError):
                bootstrap_package_recovery._publish_absent(source, destination)
            destination.mkdir()
            with self.assertRaisesRegex(RuntimeError, "existing files retained"):
                bootstrap_package_recovery._publish_absent(source, destination)


class TerminalContainment(unittest.TestCase):
    def test_macos_has_no_subreaper_call_and_other_hosts_still_refuse(self):
        with on_macos(), patch.object(ctypes, "CDLL", side_effect=AssertionError("prctl does not exist on macOS")):
            ubuntu_pty_supervisor._enable_subreaper()
        with patch.object(sys, "platform", "win32"):
            with self.assertRaises(RuntimeError):
                ubuntu_pty_supervisor._enable_subreaper()

    @unittest.skipIf(os.name == "nt", "Native PTYs are required")
    def test_native_pty_spawn_is_admitted_on_macos(self):
        with on_macos(), tempfile.TemporaryDirectory() as folder:
            process = ubuntu_pty_process.NativePtyProcess.spawn(["/bin/sh", "-c", "exit 0"], cwd=folder,
                                                                env=dict(os.environ), dimensions=(24, 80))
            process.close()


@unittest.skipIf(os.name == "nt", "Native Bash fixtures")
class RunScript(unittest.TestCase):
    """The real run.sh, with every system tool replaced by an owned recorder."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mcu-mac-run-")
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name)
        self.bin = self.fixture / "bin"
        self.bin.mkdir()
        (self.fixture / "app/direct/macos").mkdir(parents=True)
        self.log = self.fixture / "calls.log"
        self.script = (ROOT / "direct/macos/run.sh").read_text(encoding="utf-8")
        self.version = "15.7.1"
        self.translated = "0"
        self.machine = "arm64"
        self.digest = re.search(r"arm64\)\n\s+python_target=\S+\n\s+python_sha256=(\S+)", self.script).group(1)

    def tool(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/bash\nprintf '%s %s\\n' '{name}' \"$*\" >> '{self.log}'\n{body}\n")
        path.chmod(0o755)
        return path

    def run_script(self, *arguments):
        script = self.script
        self.tool("uname", f"if [[ \"$1\" == -m ]]; then echo {self.machine}; else echo Darwin; fi")
        self.tool("sw_vers", f"echo {self.version}")
        self.tool("sysctl", f"echo {self.translated}")
        # A prepared tarball's Python is a recorder, reached only after verification.
        self.tool("curl", 'while [[ $# -gt 0 ]]; do if [[ "$1" == --output ]]; then shift; echo payload > "$1"; fi; shift; done')
        self.tool("shasum", f"echo '{self.digest}  file'")
        python = self.tool("fake-python", "")
        python.write_text(f"#!/bin/bash\nprintf 'python %s\\n' \"$*\" >> '{self.log}'\n")
        self.tool("tar", 'while [[ $# -gt 0 ]]; do if [[ "$1" == -C ]]; then shift; mkdir -p "$1/python/bin"; '
                         f'cp \'{python}\' "$1/python/bin/python3"; fi; shift; done')
        self.tool("cut", "echo \"$(cat | sed 's/  .*//')\"")
        for system_path in ("/usr/bin/sw_vers", "/usr/sbin/sysctl", "/usr/bin/curl", "/usr/bin/shasum", "/usr/bin/tar", "/usr/bin/cut"):
            script = script.replace(system_path, str(self.bin / Path(system_path).name))
        run = self.fixture / "app/direct/macos/run.sh"
        run.write_text(script)
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.fixture)}
        return subprocess.run(["/bin/bash", str(run), *arguments], capture_output=True, text=True,
                              env=env, stdin=subprocess.DEVNULL, timeout=20)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def tools_dir(self):
        return self.fixture / "app/.macos-tools"

    def assert_refused_before_any_work(self, result, message):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(message, result.stderr)
        self.assertNotIn("curl ", self.calls())
        self.assertFalse(self.tools_dir().exists())

    def test_below_minimum_malformed_and_translated_hosts_stop_first(self):
        for version, message in (("13.7.4", "requires macOS 14 or newer"), ("broken", "missing or malformed"), ("", "missing or malformed")):
            with self.subTest(version=version):
                self.version = version
                self.assert_refused_before_any_work(self.run_script(), message)
        self.version = "15.7.1"
        self.translated = "1"
        self.assert_refused_before_any_work(self.run_script(), "Rosetta")
        self.translated = "0"
        self.machine = "i386"
        self.assert_refused_before_any_work(self.run_script(), "64-bit Mac")

    @unittest.skipIf(os.geteuid() == 0, "Run as a normal account")
    def test_first_launch_verifies_then_hands_off_to_the_private_python(self):
        result = self.run_script("--project", "Sketch Ω $(touch NEVER)", "")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertLess(calls.index("curl "), calls.index("shasum "))
        self.assertLess(calls.index("shasum "), calls.index("tar "))
        self.assertIn("releases/download/20261026/cpython-3.14.10%2B20261026-aarch64-apple-darwin-install_only_stripped.tar.gz", calls)
        self.assertIn("--proto =https", calls)
        self.assertIn("import ensurepip, ssl, venv", calls)
        self.assertRegex(calls, r"python -I -B .*direct/macos/launch\.py --project Sketch Ω \$\(touch NEVER\) \n")
        self.assertFalse((self.fixture / "NEVER").exists())
        self.assertEqual([path.name for path in self.tools_dir().iterdir()], ["python-3.14.10-aarch64-apple-darwin"])
        # A prepared runtime needs no download on the next launch.
        self.log.unlink()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertNotIn("curl ", self.calls())

    @unittest.skipIf(os.geteuid() == 0, "Run as a normal account")
    def test_failed_hash_installs_nothing_and_check_never_downloads(self):
        self.digest = "0" * 64
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("SHA-256", result.stderr)
        self.assertNotIn("tar ", self.calls())
        self.assertEqual(list(self.tools_dir().iterdir()), [], "The failed stage is removed")
        self.log.unlink()
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not prepared", result.stderr)
        self.assertNotIn("curl ", self.calls())

    def test_script_keeps_to_macos_bash_3_2(self):
        for name in ("direct/macos/run.sh", "LAUNCHER/MacOS.command"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/bin/bash\n"), name)
            for pattern in (r"\$\{[A-Za-z_]+,,", r"\$\{[A-Za-z_]+\^\^", r"\bdeclare -A\b", r"\bmapfile\b",
                            r"\breadarray\b", r"\|&", r"&>>", r"\$\{[A-Za-z_]+@"):
                self.assertIsNone(re.search(pattern, text), f"{name}: {pattern}")


@unittest.skipIf(os.name == "nt", "Native Bash fixtures")
class MacOSEntry(unittest.TestCase):
    def test_entry_forwards_literal_arguments_and_the_exit_status(self):
        with tempfile.TemporaryDirectory(prefix="mcu-mac-entry-") as folder:
            project = Path(folder) / "Moved Ω $cash"
            (project / "LAUNCHER").mkdir(parents=True)
            (project / "direct/macos").mkdir(parents=True)
            (project / "LAUNCHER/MacOS.command").write_bytes((ROOT / "LAUNCHER/MacOS.command").read_bytes())
            (project / "direct/macos/run.sh").write_text("printf '%s\\0' \"$@\"\nexit 23\n")
            result = subprocess.run(["/bin/bash", str(project / "LAUNCHER/MacOS.command"), "--project", "A $(touch NEVER)", ""],
                                    cwd=folder, capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
            self.assertEqual(result.returncode, 23)
            self.assertEqual(result.stdout.split(b"\0")[:-1], [b"--project", b"A $(touch NEVER)", b""])
            self.assertFalse((Path(folder) / "NEVER").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
