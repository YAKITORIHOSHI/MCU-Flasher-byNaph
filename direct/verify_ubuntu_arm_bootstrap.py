#!/usr/bin/env python3
"""Isolated amd64/ARM64 bootstrap checks; no apt, pip, board stores or GUI writes."""
from __future__ import annotations

from contextlib import ExitStack, nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.ubuntu import arm_qt5, preflight, runtime_architecture, setup, system_setup


def elf(machine):
    header = bytearray(20)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header)


class IsolatedChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp/audit/ubuntu-arm-bootstrap"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)


class HostChecks(IsolatedChecks):
    def test_arm_wheel_media_dependencies_are_reported_before_private_setup(self):
        # These SONAMEs are DT_NEEDED entries in Qt 6.12.0.140's native ARM64
        # WebEngine. A minimal Ubuntu host must prepare them before importing Qt.
        absent = {"opus", "lcms2", "minizip", "snappy", "webp", "webpdemux", "webpmux"}
        expected = {"libopus0", "liblcms2-2", "libminizip1t64", "libsnappy1v5",
                    "libwebp7", "libwebpdemux2", "libwebpmux3"}
        for release in ("24.04", "26.04"):
            for machine in ("aarch64", "x86_64"):
                with self.subTest(release=release, machine=machine), ExitStack() as stack:
                    stack.enter_context(patch.object(preflight, "sys", SimpleNamespace(platform="linux", version_info=(3, 14))))
                    stack.enter_context(patch.object(preflight.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": release}))
                    stack.enter_context(patch.object(preflight.os, "uname", return_value=SimpleNamespace(machine=machine), create=True))
                    stack.enter_context(patch.object(preflight.importlib.util, "find_spec", return_value=object()))
                    stack.enter_context(patch.object(preflight, "_check_tk"))
                    stack.enter_context(patch.object(preflight.shutil, "which", return_value="/fixture/tool"))
                    stack.enter_context(patch.object(preflight.ctypes.util, "find_library", side_effect=lambda name: None if name in absent else name))
                    stack.enter_context(patch.object(preflight.ctypes, "CDLL", return_value=object()))
                    stack.enter_context(patch.object(arm_qt5, "missing_system_packages", return_value=()))
                    stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("Installer was invoked")))
                    if machine == "aarch64":
                        with self.assertRaises(preflight.MissingSystemDependencies) as missing:
                            preflight.host_preflight()
                        self.assertEqual(set(missing.exception.packages), expected)
                        self.assertTrue(expected <= system_setup.ALLOWED_PACKAGES)
                    else:
                        preflight.host_preflight()

    def test_arm_extra_dependencies_are_allowlisted_for_visible_os_setup(self):
        missing = preflight.MissingSystemDependencies(["libopus0", "libminizip1t64", "libsnappy1v5"])
        with patch.object(preflight, "host_preflight", side_effect=[missing, None]) as host, \
                patch.object(system_setup, "sys", SimpleNamespace(platform="linux", stdin=SimpleNamespace(isatty=lambda: True))), \
                patch.object(system_setup.os, "geteuid", return_value=1000, create=True), \
                patch.object(system_setup.os, "access", return_value=True), \
                patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run, \
                redirect_stdout(io.StringIO()):
            system_setup.ensure_system_dependencies()
        self.assertEqual(host.call_count, 2)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][-3:], list(missing.packages))
        self.assertEqual(run.call_args.args[0][:2], ["/usr/bin/sudo", "/usr/bin/apt-get"])
        self.assertEqual(run.call_args.kwargs["cwd"], "/")

    def test_both_lts_hosts_admit_arm64_and_keep_amd64_dependencies(self):
        for release in ("24.04", "26.04"):
            for machine in ("x86_64", "aarch64"):
                with self.subTest(release=release, machine=machine), ExitStack() as stack:
                    stack.enter_context(patch.object(preflight, "sys", SimpleNamespace(platform="linux", version_info=(3, 14))))
                    stack.enter_context(patch.object(preflight.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": release}))
                    stack.enter_context(patch.object(preflight.os, "uname", return_value=SimpleNamespace(machine=machine), create=True))
                    stack.enter_context(patch.object(preflight.importlib.util, "find_spec", return_value=object()))
                    stack.enter_context(patch.object(preflight, "_check_tk"))
                    stack.enter_context(patch.object(preflight.shutil, "which", return_value="/fixture/tool"))
                    stack.enter_context(patch.object(preflight.ctypes.util, "find_library", side_effect=lambda name: name))
                    stack.enter_context(patch.object(preflight.ctypes, "CDLL", return_value=object()))
                    native_qt5 = stack.enter_context(patch.object(arm_qt5, "missing_system_packages", return_value=arm_qt5.SYSTEM_PACKAGES))
                    stack.enter_context(patch.object(subprocess, "run", side_effect=AssertionError("Installer was invoked")))
                    if machine == "aarch64":
                        with self.assertRaises(preflight.MissingSystemDependencies) as missing:
                            preflight.host_preflight()
                        self.assertEqual(missing.exception.packages, arm_qt5.SYSTEM_PACKAGES)
                        self.assertTrue(set(missing.exception.packages) <= system_setup.ALLOWED_PACKAGES)
                        native_qt5.assert_called_once()
                    else:
                        preflight.host_preflight()
                        native_qt5.assert_not_called()

    def test_32_bit_and_unsupported_hosts_stop_before_dependency_work(self):
        for machine, pointer_size in (("armv7l", 4), ("aarch64", 4), ("riscv64", 8)):
            with self.subTest(machine=machine), \
                    patch.object(preflight.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "24.04"}), \
                    patch.object(preflight, "sys", SimpleNamespace(platform="linux", version_info=(3, 12))), \
                    patch.object(preflight.os, "uname", return_value=SimpleNamespace(machine=machine), create=True), \
                    patch.object(preflight.struct, "calcsize", return_value=pointer_size), \
                    patch.object(preflight, "_check_tk", side_effect=AssertionError("Native check ran")):
                with self.assertRaisesRegex(RuntimeError, "64-bit"):
                    preflight.host_preflight()

    def test_system_provider_check_requires_matching_python_abi_and_arm64_elf(self):
        from importlib.machinery import EXTENSION_SUFFIXES
        package = self.root / "PyQt5"
        package.mkdir()
        with patch.object(arm_qt5, "SYSTEM_DIST_PACKAGES", self.root):
            self.assertEqual(arm_qt5.missing_system_packages(), arm_qt5.SYSTEM_PACKAGES)
            for module in ("QtCore", "QtWidgets", "Qsci", "sip"):
                (package / (module + EXTENSION_SUFFIXES[0])).write_bytes(elf(183))
            self.assertEqual(arm_qt5.missing_system_packages(), ())
            (package / ("Qsci" + EXTENSION_SUFFIXES[0])).write_bytes(elf(62))
            self.assertEqual(arm_qt5.missing_system_packages(), ("python3-pyqt5.qsci",))
            (package / ("sip" + EXTENSION_SUFFIXES[0])).unlink()
            (package / "sip.cpython-999-aarch64-linux-gnu.so").write_bytes(elf(183))
            self.assertEqual(arm_qt5.missing_system_packages(), ("python3-pyqt5.qsci", "python3-pyqt5.sip"))

    def test_pip_qt5_requirements_are_amd64_only(self):
        from pip._vendor.packaging.requirements import Requirement
        requirements = [Requirement(line) for line in (ROOT / "direct/ubuntu/requirements.txt").read_text().splitlines()
                        if line.strip() and not line.startswith("#")]
        for machine in ("x86_64", "aarch64", "arm64"):
            selected = {item.name for item in requirements if item.marker is None or item.marker.evaluate({"platform_machine": machine})}
            self.assertIn("PySide6", selected)
            self.assertEqual("PyQt5" in selected, machine == "x86_64")
            self.assertEqual("QScintilla" in selected, machine == "x86_64")


class QtNativePreflightChecks(IsolatedChecks):
    def qt_files(self, machine=183):
        root = self.root / "PySide6/Qt"
        paths = (root / "lib/libQt6Core.so.6", root / "plugins/platforms/libqxcb.so",
                 root / "lib/libQt6WebEngineCore.so.6", root / "libexec/QtWebEngineProcess")
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(elf(machine))
        return root, paths

    def test_foreign_cpu_in_any_required_file_stops_before_linker_or_native_import(self):
        root, paths = self.qt_files()
        for path in paths:
            with self.subTest(path=path.name), \
                    patch.object(subprocess, "run", side_effect=AssertionError("Foreign ELF reached ldd")):
                path.write_bytes(elf(62))
                with self.assertRaisesRegex(RuntimeError, "another CPU architecture"):
                    preflight._check_qt_runtime_files(root, "arm64")
                path.write_bytes(elf(183))

    def test_preimport_linker_reports_all_missing_sonames(self):
        root, paths = self.qt_files()
        replies = [SimpleNamespace(returncode=0, stdout="", stderr=""),
                   SimpleNamespace(returncode=0, stdout="libSM.so.6 => not found\n", stderr=""),
                   SimpleNamespace(returncode=0, stdout="libopus.so.0 => not found\nlibsnappy.so.1 => not found\n", stderr=""),
                   SimpleNamespace(returncode=0, stdout="libwebpdemux.so.2 => not found\n", stderr="")]
        private = str(self.root / ".venv-linux")
        with patch.object(preflight, "sys", SimpleNamespace(platform="linux", version_info=(3, 14), prefix=private, base_prefix="system")), \
                patch.object(preflight, "host_architecture", return_value="arm64"), \
                patch.object(preflight.importlib.util, "find_spec", return_value=SimpleNamespace(origin=str(root.parent / "__init__.py"))), \
                patch.object(preflight, "_check_tk", side_effect=AssertionError("Native imports ran before linker validation")), \
                patch.object(subprocess, "run", side_effect=replies) as run:
            with self.assertRaises(RuntimeError) as error:
                preflight.runtime_preflight(self.root)
        for name in ("libSM.so.6", "libopus.so.0", "libsnappy.so.1", "libwebpdemux.so.2"):
            self.assertIn(name + " => not found", str(error.exception))
        self.assertIn("libopus0", str(error.exception))
        self.assertEqual([call.args[0] for call in run.call_args_list], [["ldd", str(path)] for path in paths])


class Qt5BridgeChecks(IsolatedChecks):
    def test_bridge_is_self_contained_and_exposes_only_pyqt5(self):
        site = self.root / "private-site"
        native = self.root / "ubuntu-dist"
        copied = site / "PyQt5"
        native_package = native / "PyQt5"
        copied.mkdir(parents=True)
        native_package.mkdir(parents=True)
        (copied / "__init__.py").write_text("provider = 'foreign-copied-wheel'\n")
        (native_package / "__init__.py").write_text("provider = 'ubuntu-native'\n")
        (native / "mcu_unrelated_system_package.py").write_text("raise AssertionError('System packages exposed')\n")
        arm_qt5.install_bridge(site)
        command = ("import importlib.util, json, platform, site, sys; from pathlib import Path; "
                   "platform.machine=lambda: 'aarch64'; "
                   f"site.addsitedir({str(site)!r}); import {arm_qt5.HOOK_MODULE} as bridge; "
                   f"bridge.SYSTEM_DIST_PACKAGES=Path({str(native)!r}); "
                   "import PyQt5; "
                   "print(json.dumps([PyQt5.provider, str(bridge.SYSTEM_DIST_PACKAGES) in sys.path, "
                   "importlib.util.find_spec('mcu_unrelated_system_package') is None, sys.prefix != sys.base_prefix]))")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", command], check=True, capture_output=True, text=True, timeout=10)
        self.assertEqual(json.loads(result.stdout), ["ubuntu-native", False, True, sys.prefix != sys.base_prefix])

    def test_bridge_never_activates_on_amd64_or_windows(self):
        for host, machine in (("win32", "ARM64"), ("linux", "x86_64"), ("linux", "armv7l")):
            finders = []
            with patch.object(arm_qt5, "sys", SimpleNamespace(platform=host, meta_path=finders)), \
                    patch.object(arm_qt5.platform, "machine", return_value=machine):
                arm_qt5.activate()
            self.assertEqual(finders, [])

    def test_bridge_is_idempotent_and_does_not_rewrite_unchanged_files(self):
        arm_qt5.install_bridge(self.root)
        files = list(self.root.iterdir())
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
        arm_qt5.install_bridge(self.root)
        self.assertEqual(before, {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in files})

    @unittest.skipUnless(os.name == "posix", "POSIX symlink fixture")
    def test_bridge_refuses_external_symlink_files(self):
        outside = self.root / "preserved.py"
        outside.write_text("preserve\n")
        site = self.root / "site"
        site.mkdir()
        (site / (arm_qt5.HOOK_MODULE + ".py")).symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "local file"):
            arm_qt5.install_bridge(site)
        self.assertEqual(outside.read_text(), "preserve\n")


class ArchitectureRepairChecks(IsolatedChecks):
    def test_runtime_receipt_is_bounded_and_unchanged_records_are_not_rewritten(self):
        directory = self.root / ".venv-linux"
        runtime_architecture.record_runtime_architecture(directory, "arm64")
        receipt = runtime_architecture.private_site_packages(directory) / runtime_architecture.RECEIPT_NAME
        before = receipt.stat().st_mtime_ns
        with patch.object(runtime_architecture.os, "replace", side_effect=AssertionError("Unchanged receipt rewritten")):
            runtime_architecture.record_runtime_architecture(directory, "arm64")
        self.assertEqual(receipt.stat().st_mtime_ns, before)
        receipt.write_bytes(b" " * (runtime_architecture.MAX_RECEIPT_BYTES + 1))
        with self.assertRaisesRegex(RuntimeError, "damaged"):
            runtime_architecture.needs_architecture_repair(directory, "arm64")
        with self.assertRaisesRegex(RuntimeError, "damaged"):
            runtime_architecture.check_runtime_architecture(directory, "arm64")

    def test_receipt_detects_both_migration_directions_without_removing_files(self):
        directory = self.root / ".venv-linux"
        for previous, current in (("amd64", "arm64"), ("arm64", "amd64")):
            runtime_architecture.record_runtime_architecture(directory, previous)
            self.assertTrue(runtime_architecture.needs_architecture_repair(directory, current))
            with self.assertRaisesRegex(RuntimeError, "another CPU architecture"):
                runtime_architecture.check_runtime_architecture(directory, current)
            runtime_architecture.record_runtime_architecture(directory, current)
            self.assertFalse(runtime_architecture.needs_architecture_repair(directory, current))
            runtime_architecture.check_runtime_architecture(directory, current)

    def test_legacy_native_extension_bytes_force_reinstall_before_pip_satisfied(self):
        directory = self.root / ".venv-linux"
        site = runtime_architecture.private_site_packages(directory)
        site.mkdir(parents=True)
        native = site / "native-extension.so"
        for machine, architecture in ((62, "arm64"), (183, "amd64")):
            native.write_bytes(elf(machine))
            self.assertTrue(runtime_architecture.needs_architecture_repair(directory, architecture))
            native.write_bytes(elf(183 if architecture == "arm64" else 62))
            self.assertFalse(runtime_architecture.needs_architecture_repair(directory, architecture))

    def test_unused_copied_qt5_wheel_does_not_cause_reinstall_loop_on_arm64(self):
        directory = self.root / ".venv-linux"
        package = runtime_architecture.private_site_packages(directory) / "PyQt5"
        package.mkdir(parents=True)
        (package / "QtCore.so").write_bytes(elf(62))
        self.assertFalse(runtime_architecture.needs_architecture_repair(directory, "arm64"))

    def test_unknown_receipt_is_preserved_and_stops_setup(self):
        directory = self.root / ".venv-linux"
        site = runtime_architecture.private_site_packages(directory)
        site.mkdir(parents=True)
        receipt = site / runtime_architecture.RECEIPT_NAME
        receipt.write_text('{"owner":"user-content"}\n')
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            runtime_architecture.needs_architecture_repair(directory, "arm64")
        self.assertEqual(receipt.read_text(), '{"owner":"user-content"}\n')

    @unittest.skipUnless(os.name == "posix", "POSIX symlink fixture")
    def test_broken_receipt_symlink_is_rejected_before_missing_record_fallback(self):
        directory = self.root / ".venv-linux"
        site = runtime_architecture.private_site_packages(directory)
        site.mkdir(parents=True)
        receipt = site / runtime_architecture.RECEIPT_NAME
        receipt.symlink_to(self.root / "absent-external-file")
        with self.assertRaisesRegex(RuntimeError, "local file"):
            runtime_architecture.check_runtime_architecture(directory, "arm64")
        with self.assertRaisesRegex(RuntimeError, "local file"):
            runtime_architecture.needs_architecture_repair(directory, "arm64")
        self.assertTrue(receipt.is_symlink())

    def test_setup_only_forces_reinstall_for_foreign_native_dependencies(self):
        for architecture, foreign in (("amd64", False), ("arm64", False), ("arm64", True), ("amd64", True)):
            with self.subTest(architecture=architecture, foreign=foreign), ExitStack() as stack:
                directory = self.root / f"{architecture}-{foreign}" / ".venv-linux"
                site = runtime_architecture.private_site_packages(directory)
                site.mkdir(parents=True)
                (directory / "pyvenv.cfg").write_text("home = /fixture\n")
                if foreign:
                    (site / "extension.so").write_bytes(elf(62 if architecture == "arm64" else 183))
                stack.enter_context(patch.object(setup, "ROOT", directory.parent))
                stack.enter_context(patch.object(setup, "ENV_DIR", directory))
                stack.enter_context(patch("src.modules.ubuntu_release.enforce_supported_ubuntu", return_value=True))
                stack.enter_context(patch.object(setup, "sys", SimpleNamespace(platform="linux", version_info=sys.version_info, _base_executable=sys._base_executable)))
                stack.enter_context(patch.object(setup.os, "geteuid", return_value=1000, create=True))
                stack.enter_context(patch.object(setup.venv, "EnvBuilder"))
                stack.enter_context(patch("src.modules.runtime_resources.enforce_minimum_cpu_requirement", return_value=True))
                stack.enter_context(patch("src.modules.ubuntu_architecture.host_architecture", return_value=architecture))
                stack.enter_context(patch("src.modules.platform_runtime.native_platformio_dir", return_value=directory.parent / "core"))
                stack.enter_context(patch("direct.ubuntu.system_setup.ensure_system_dependencies"))
                stack.enter_context(patch("direct.ubuntu.arduino_cli.ensure_arduino_cli"))
                stack.enter_context(patch("direct.ubuntu.opencode_setup.ensure_opencode_cli"))
                stack.enter_context(patch("src.modules.offline_bootstrap.ready", return_value=False))
                stack.enter_context(patch("src.modules.offline_mode.finish_bootstrap"))
                stack.enter_context(patch("src.modules.package_jobs.package_store_lease", return_value=nullcontext()))
                stack.enter_context(patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "", "MCU_FLASHER_WORKSPACE_RUNTIME": ""}))
                stack.enter_context(redirect_stdout(io.StringIO()))
                run = stack.enter_context(patch.object(subprocess, "run"))
                bridge = stack.enter_context(patch.object(arm_qt5, "install_bridge"))
                self.assertEqual(setup.main([]), 0)
                pip = run.call_args_list[0].args[0]
                self.assertEqual("--force-reinstall" in pip, foreign)
                self.assertIn("--no-user", pip)
                self.assertTrue(pip[0].endswith(".venv-linux/bin/python"))
                self.assertNotIn("sudo", pip)
                self.assertNotIn("--system-site-packages", pip)
                if architecture == "arm64":
                    bridge.assert_called_once_with(site)
                else:
                    bridge.assert_not_called()
                runtime_architecture.check_runtime_architecture(directory, architecture)


if __name__ == "__main__":
    unittest.main(verbosity=2)
