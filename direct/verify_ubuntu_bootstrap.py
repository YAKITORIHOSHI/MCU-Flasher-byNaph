#!/usr/bin/env python3
"""Hardware-free Ubuntu prerequisites/venv repair fixtures; no live installers."""
from __future__ import annotations

import os
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import venv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.ubuntu import preflight, setup


class UbuntuReleasePackageChecks(unittest.TestCase):
    """OS-release/package fixtures also run on Windows, without Linux syscalls."""

    def test_release_packages_cover_22_24_and_26_without_installing(self):
        for version, expected in (("22.04", ("libasound2", "libglib2.0-0")),
                                  ("24.04", ("libasound2t64", "libglib2.0-0t64")),
                                  ("26.04", ("libasound2t64", "libglib2.0-0t64"))):
            with self.subTest(version=version), \
                    patch.object(preflight.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": version}), \
                    patch.object(subprocess, "run", side_effect=AssertionError("Installer was invoked")):
                missing = preflight.MissingSystemDependencies(["libasound2", "libglib2.0-0", "libasound2"])
                self.assertEqual(missing.packages, expected)
                self.assertIn("sudo apt install " + " ".join(expected), str(missing))

    def test_ubuntu_26_python_314_wayland_host_reports_precise_missing_packages(self):
        # Every platform, native loader and package lookup is a fixture.
        with patch.object(preflight, "sys", SimpleNamespace(platform="linux", version_info=(3, 14))), \
                patch.object(preflight.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "26.04"}), \
                patch.object(preflight.os, "uname", return_value=SimpleNamespace(machine="x86_64"), create=True), \
                patch.object(preflight.importlib.util, "find_spec", return_value=object()), \
                patch.object(preflight, "_check_tk"), \
                patch.object(preflight.shutil, "which", return_value="/fixture/tool"), \
                patch.object(preflight.ctypes.util, "find_library", side_effect=lambda name: None if name in {"asound", "glib-2.0"} else name), \
                patch.object(preflight.ctypes, "CDLL", return_value=object()), \
                patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": "wayland-fixture"}), \
                patch.object(subprocess, "run", side_effect=AssertionError("Installer was invoked")):
            with self.assertRaises(preflight.MissingSystemDependencies) as missing:
                preflight.host_preflight(require_display=True)
        self.assertEqual(missing.exception.packages, ("libglib2.0-0t64", "libasound2t64"))


class BootstrapEnvironmentChecks(unittest.TestCase):
    def test_private_pip_and_platformio_interpreters_ignore_foreign_destinations(self):
        from platformio.proc import get_pythonexe_path

        values = {"PIP_TARGET": "/external-target", "PIP_PREFIX": "/external-prefix", "PIP_ROOT": "/external-root",
                  "PIP_USER": "1", "PIP_CONFIG_FILE": "/user/pip.conf", "PATH": "/account/bin:/usr/bin",
                  "PYTHONEXEPATH": "C:/copied/python.exe", "PIO_PYTHON_EXE": "C:/copied/python.exe",
                  "PLATFORMIO_PYTHON_EXE": "C:/copied/python.exe", "PLATFORMIO_PENV_DIR": "C:/copied/penv",
                  "HTTPS_PROXY": "https://proxy.invalid", "PIP_INDEX_URL": "https://packages.invalid/simple",
                  "PIP_CERT": "/account/certificate.pem", "REQUESTS_CA_BUNDLE": "/account/requests.pem"}
        with patch.dict(os.environ, values):
            env = setup.clean_native_bootstrap_environment()
        for name in ("PIP_TARGET", "PIP_PREFIX", "PIP_ROOT", "PIP_USER", "PYTHONEXEPATH",
                     "PIO_PYTHON_EXE", "PLATFORMIO_PYTHON_EXE", "PLATFORMIO_PENV_DIR"):
            self.assertNotIn(name, env)
        self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)
        for name in ("PATH", "HTTPS_PROXY", "PIP_INDEX_URL", "PIP_CERT", "REQUESTS_CA_BUNDLE"):
            self.assertEqual(env[name], values[name])
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(get_pythonexe_path(), os.path.normpath(sys.executable))
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")


@unittest.skipUnless(sys.platform.startswith("linux"), "Native Linux venv and SONAME fixtures")
class UbuntuBootstrapChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp/audit/ubuntu-bootstrap"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def host_mocks(self):
        patches = (
            patch.object(preflight.os, "uname", return_value=SimpleNamespace(machine="x86_64")),
            patch.object(preflight.importlib.util, "find_spec", return_value=object()),
            patch.object(preflight, "_check_tk"),
            patch.object(preflight.shutil, "which", return_value="/fixture/native-tool"),
            patch.object(preflight.ctypes.util, "find_library", side_effect=lambda name: "lib" + name + ".so"),
            patch.object(preflight.ctypes, "CDLL", return_value=object()),
        )
        for mock in patches:
            mock.start()
            self.addCleanup(mock.stop)

    def test_available_native_prerequisites_never_start_installers(self):
        self.host_mocks()
        with patch.object(subprocess, "run", side_effect=AssertionError("Installer was invoked")):
            self.assertIsNone(preflight.host_preflight())

    def test_setup_stops_before_venv_and_pip_when_host_is_incomplete(self):
        output = io.StringIO()
        with patch.object(setup.os, "geteuid", return_value=1000), \
                patch("src.modules.runtime_resources.enforce_minimum_cpu_requirement", return_value=True), \
                patch.object(preflight, "host_preflight", side_effect=RuntimeError("sudo apt install libxcb-cursor0")), \
                patch.object(setup.venv, "EnvBuilder", side_effect=AssertionError("Live environment was touched")), \
                patch.object(subprocess, "run", side_effect=AssertionError("Installer was invoked")), \
                patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "", "MCU_FLASHER_WORKSPACE_RUNTIME": ""}), \
                contextlib.redirect_stderr(output):
            self.assertEqual(setup.main([]), 1)
        self.assertIn("Ubuntu prerequisites", output.getvalue())
        self.assertIn("sudo apt install libxcb-cursor0", output.getvalue())

    def test_missing_native_libraries_show_exact_recovery_packages(self):
        self.host_mocks()
        with patch.object(preflight.ctypes.util, "find_library", side_effect=lambda name: None if name in ("asound", "xcb-cursor") else name), \
                patch.object(preflight.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "24.04"}), \
                patch.object(subprocess, "run", side_effect=AssertionError("Installer was invoked")):
            with self.assertRaisesRegex(RuntimeError, "sudo apt install libasound2t64 libxcb-cursor0"):
                preflight.host_preflight()

    def test_missing_tk_and_native_tools_report_once(self):
        self.host_mocks()
        with patch.object(preflight, "_check_tk", side_effect=RuntimeError("Tk missing")), \
                patch.object(preflight.shutil, "which", side_effect=lambda name: None if name in ("make", "gcc", "g++", "git") else name):
            with self.assertRaises(RuntimeError) as result:
                preflight.host_preflight()
        self.assertIn("sudo apt install python3-tk git build-essential", str(result.exception))
        self.assertEqual(str(result.exception).split("sudo apt install ")[1].split("\n")[0].count("build-essential"), 1)

    def test_webengine_direct_libraries_are_detected_before_private_imports(self):
        self.host_mocks()
        names = {"Xcomposite", "Xdamage", "Xtst", "xkbfile", "gbm", "xcb-dri3", "glib-2.0"}
        with patch.object(preflight.ctypes.util, "find_library", side_effect=lambda name: None if name in names else name), \
                patch.object(preflight.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "24.04"}):
            with self.assertRaises(preflight.MissingSystemDependencies) as missing:
                preflight.host_preflight()
        self.assertEqual(set(missing.exception.packages), {"libxcomposite1", "libxdamage1", "libxtst6",
                         "libxkbfile1", "libgbm1", "libxcb-dri3-0", "libglib2.0-0t64"})

    def test_ubuntu_22_library_names_are_preserved(self):
        with patch.object(preflight.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "22.04"}):
            self.assertEqual(preflight.normalize_packages(["libasound2", "libglib2.0-0"]),
                             ("libasound2", "libglib2.0-0"))

    def test_unsupported_architecture_stops_before_dependency_work(self):
        with patch.object(preflight.os, "uname", return_value=SimpleNamespace(machine="aarch64")), \
                patch.object(preflight, "_check_tk", side_effect=AssertionError("Dependency work ran")), \
                patch.object(subprocess, "run", side_effect=AssertionError("Installer was invoked")):
            with self.assertRaisesRegex(RuntimeError, "64-bit Intel/AMD"):
                preflight.host_preflight()

    def test_launch_requires_a_desktop_but_setup_can_be_headless(self):
        self.host_mocks()
        with patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}):
            preflight.host_preflight()
            with self.assertRaisesRegex(RuntimeError, "desktop session"):
                preflight.host_preflight(require_display=True)

    def test_runtime_must_use_the_owning_private_environment(self):
        with self.assertRaisesRegex(RuntimeError, "own .venv-linux"):
            preflight.runtime_preflight(self.root)

    def test_scons_only_install_requires_board_bootstrap_in_online_mode(self):
        with patch("main.platforms.ubuntu_arduino.find_arduino_cli", return_value="fixture-arduino"), \
                patch("main.platforms.ubuntu_opencode.find_opencode_cli", return_value="fixture-opencode"), \
                patch("src.modules.offline_bootstrap.ready", return_value=False) as boards, \
                patch("src.modules.offline_mode.startup_ready", return_value=True):
            self.assertFalse(preflight.bootstrap_ready(self.root))
        boards.assert_called_once_with(self.root)

    def test_certified_boards_still_require_selected_runtime_mode_ready(self):
        with patch("main.platforms.ubuntu_arduino.find_arduino_cli", return_value="fixture-arduino"), \
                patch("main.platforms.ubuntu_opencode.find_opencode_cli", return_value="fixture-opencode"), \
                patch("src.modules.offline_bootstrap.ready", return_value=True), \
                patch("src.modules.offline_mode.startup_ready", return_value=False):
            self.assertFalse(preflight.bootstrap_ready(self.root))
        with patch("main.platforms.ubuntu_arduino.find_arduino_cli", return_value="fixture-arduino"), \
                patch("main.platforms.ubuntu_opencode.find_opencode_cli", return_value="fixture-opencode"), \
                patch("src.modules.offline_bootstrap.ready", return_value=True), \
                patch("src.modules.offline_mode.startup_ready", return_value=True):
            self.assertTrue(preflight.bootstrap_ready(self.root))

    def test_missing_native_cli_routes_a_certified_installation_to_bootstrap(self):
        for arduino, opencode in ((None, "opencode"), ("arduino", None)):
            with self.subTest(arduino=arduino, opencode=opencode), \
                    patch("main.platforms.ubuntu_arduino.find_arduino_cli", return_value=arduino), \
                    patch("main.platforms.ubuntu_opencode.find_opencode_cli", return_value=opencode), \
                    patch("src.modules.offline_bootstrap.ready", return_value=True), \
                    patch("src.modules.offline_mode.startup_ready", return_value=True):
                self.assertFalse(preflight.bootstrap_ready(self.root))

    def test_clipboard_requirements_cover_x11_and_wayland_without_live_processes(self):
        self.host_mocks()
        for absent, expected in (({"xclip", "xsel"}, {"xclip"}),
                                 ({"wl-copy"}, {"wl-clipboard"}),
                                 ({"wl-paste"}, {"wl-clipboard"}),
                                 ({"xclip", "xsel", "wl-copy", "wl-paste"}, {"xclip", "wl-clipboard"})):
            with self.subTest(absent=absent), \
                    patch.object(preflight.shutil, "which", side_effect=lambda name: None if name in absent else name), \
                    patch.object(subprocess, "run", side_effect=AssertionError("Clipboard probe spawned a child")):
                with self.assertRaises(preflight.MissingSystemDependencies) as missing:
                    preflight.host_preflight()
                self.assertEqual(set(missing.exception.packages), expected)

    def test_missing_assistant_search_tool_is_prepared_through_bootstrap(self):
        self.host_mocks()
        with patch.object(preflight.shutil, "which", side_effect=lambda name: None if name == "rg" else name):
            with self.assertRaises(preflight.MissingSystemDependencies) as missing:
                preflight.host_preflight()
        self.assertEqual(missing.exception.packages, ("ripgrep",))

    def test_actual_qt_linker_failure_reports_the_missing_soname(self):
        library = self.root / "libqxcb.so"
        library.write_bytes(b"fixture")
        with patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="libxcb-cursor.so.0 => not found\n", stderr="")):
            with self.assertRaisesRegex(RuntimeError, "libxcb-cursor.so.0 => not found"):
                preflight._check_linked_libraries((library,))

    def test_dead_interpreter_links_are_repaired_without_clearing_packages(self):
        directory = self.root / ".venv-linux"
        (directory / "bin").mkdir(parents=True)
        (directory / "pyvenv.cfg").write_text("home = /old/computer/python\n")
        retained = directory / "lib/retained-dependency.txt"
        retained.parent.mkdir()
        retained.write_bytes(b"keep the existing dependency bytes")
        names = {"python", "python3", f"python3.{sys.version_info[1]}", Path(sys._base_executable).name}
        for name in names:
            (directory / "bin" / name).symlink_to("/missing/old/python")
        setup.prepare_environment_directory(directory)
        self.assertFalse(any((directory / "bin" / name).is_symlink() for name in names))
        # This standard-library venv fixture installs no pip or application deps.
        venv.EnvBuilder(with_pip=False, symlinks=True).create(directory)
        result = subprocess.run([str(directory / "bin/python"), "-B", "-c", "import sys; print(sys.prefix)"],
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(Path(result.stdout.strip()), directory)
        self.assertEqual(retained.read_bytes(), b"keep the existing dependency bytes")

    def test_unknown_content_and_external_links_are_preserved(self):
        directory = self.root / ".venv-linux"
        directory.mkdir()
        sentinel = directory / "user.txt"
        sentinel.write_text("user-owned")
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            setup.prepare_environment_directory(directory)
        self.assertEqual(sentinel.read_text(), "user-owned")
        (directory / "pyvenv.cfg").write_text("home = /fixture\n")
        target = self.root / "outside"
        target.mkdir()
        (directory / "lib").symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "local directory"):
            setup.prepare_environment_directory(directory)
        self.assertTrue((directory / "lib").is_symlink())
        self.assertEqual(sentinel.read_text(), "user-owned")

    def test_escaped_metadata_and_lib64_are_rejected_before_link_repair(self):
        directory = self.root / ".venv-linux"
        (directory / "bin").mkdir(parents=True)
        metadata = self.root / "external.cfg"
        metadata.write_text("preserve me")
        (directory / "pyvenv.cfg").symlink_to(metadata)
        with self.assertRaisesRegex(RuntimeError, "local file"):
            setup.prepare_environment_directory(directory)
        self.assertEqual(metadata.read_text(), "preserve me")
        (directory / "pyvenv.cfg").unlink()
        (directory / "pyvenv.cfg").write_text("home = /fixture\n")
        (directory / "bin/python").symlink_to("/missing/python")
        (directory / "lib64").symlink_to(self.root / "external-library")
        with self.assertRaisesRegex(RuntimeError, "outside this environment"):
            setup.prepare_environment_directory(directory)
        self.assertTrue((directory / "bin/python").is_symlink())


if __name__ == "__main__":
    unittest.main(verbosity=2)
