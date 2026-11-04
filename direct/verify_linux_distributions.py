#!/usr/bin/env python3
"""Linux distribution adapters, with fake package managers only.

Never installs a package, authenticates, opens a dialog or starts the application.
Real Fedora and Linux Mint userlands run the same code in other-platforms.yml.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.ubuntu import arm_qt5, fedora_packages, launch, preflight, system_setup
from src.modules import ubuntu_release as release

FEDORA = {"ID": "fedora", "VERSION_ID": "44"}
MINT = {"ID": "linuxmint", "ID_LIKE": "ubuntu debian", "VERSION_ID": "22.3", "UBUNTU_CODENAME": "noble"}
UBUNTU = {"ID": "ubuntu", "VERSION_ID": "24.04"}


def host(record):
    return patch.object(release.platform, "freedesktop_os_release", return_value=record)


class PackageTables(unittest.TestCase):
    def test_fedora_checks_exactly_the_libraries_and_tools_ubuntu_checks(self):
        for fedora, ubuntu in ((fedora_packages.SYSTEM_LIBRARIES, preflight.SYSTEM_LIBRARIES),
                               (fedora_packages.ARM_SYSTEM_LIBRARIES, preflight.ARM_SYSTEM_LIBRARIES),
                               (fedora_packages.SYSTEM_TOOLS, preflight.SYSTEM_TOOLS)):
            self.assertEqual([name for name, _ in fedora], [name for name, _ in ubuntu])
        self.assertEqual(fedora_packages.CLIPBOARD_PACKAGES, preflight.CLIPBOARD_PACKAGES)

    def test_no_ubuntu_package_name_reaches_fedora_setup(self):
        ubuntu_only = {package for _, package in preflight.SYSTEM_LIBRARIES + preflight.ARM_SYSTEM_LIBRARIES}
        ubuntu_only |= {"python3-venv", "build-essential", "libsecret-tools", *arm_qt5.SYSTEM_PACKAGES}
        ubuntu_only -= {"bash", "git"}
        self.assertFalse(ubuntu_only & system_setup.ALLOWED_FEDORA_PACKAGES)
        with host(FEDORA), patch.object(preflight, "host_architecture", return_value="arm64"):
            command = preflight.system_install_command()
        self.assertTrue(command.startswith("sudo dnf install "))
        self.assertFalse(ubuntu_only & set(command.split()))
        for name in ("libglvnd-egl", "systemd-libs", "libsecret", "git-core", "python3-qt5", "zlib-ng-compat"):
            self.assertIn(name, command.split())

    def test_mint_uses_the_noble_names_of_its_ubuntu_base(self):
        with host(MINT):
            self.assertEqual(preflight.system_libraries("amd64"), preflight.SYSTEM_LIBRARIES)
            self.assertEqual(preflight.system_tools(), preflight.SYSTEM_TOOLS)
            self.assertEqual(preflight.normalize_packages(["libasound2"]), ("libasound2t64",))


class PreflightOnEachDistribution(unittest.TestCase):
    def missing_everything(self, record, machine="x86_64"):
        with host(record), patch.object(preflight.sys, "version_info", (3, 14)), \
                patch.object(preflight.os, "uname", return_value=type("U", (), {"machine": machine})), \
                patch.object(preflight.importlib.util, "find_spec", return_value=None), \
                patch.object(preflight.shutil, "which", return_value=None), \
                patch.object(preflight.ctypes.util, "find_library", return_value=None), \
                patch.object(arm_qt5.PathFinder, "find_spec", return_value=None):
            with self.assertRaises(preflight.MissingSystemDependencies) as caught:
                preflight.host_preflight()
        return caught.exception

    def test_fedora_reports_only_its_own_packages_and_command(self):
        for machine, expected in (("x86_64", "libglvnd-egl"), ("aarch64", "python3-qscintilla-qt5")):
            with self.subTest(machine=machine):
                missing = self.missing_everything(FEDORA, machine)
                self.assertIn(expected, missing.packages)
                self.assertIn("python3", missing.packages)
                self.assertNotIn("python3-venv", missing.packages)
                self.assertLessEqual(set(missing.packages), system_setup.ALLOWED_FEDORA_PACKAGES)
                self.assertIn("Fedora system dependencies are missing", str(missing))
                self.assertIn("Fedora Bootstrap", str(missing))
                self.assertIn("sudo dnf install", str(missing))
                self.assertNotIn("apt", str(missing))

    def test_mint_reports_apt_packages_under_its_own_name(self):
        missing = self.missing_everything(MINT)
        self.assertIn("python3-venv", missing.packages)
        self.assertLessEqual(set(missing.packages), system_setup.ALLOWED_PACKAGES)
        self.assertIn("Linux Mint system dependencies are missing", str(missing))
        self.assertIn("sudo apt install", str(missing))

    def test_fedora_arm_viewer_uses_its_architecture_library_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            absent = Path(folder) / "ubuntu-dist-packages"
            with patch.object(arm_qt5, "SYSTEM_DIST_PACKAGES", absent):
                root = arm_qt5._system_packages_root()
                self.assertEqual(root, Path(f"/usr/lib64/python{sys.version_info[0]}.{sys.version_info[1]}/site-packages"))
                with patch.object(arm_qt5.PathFinder, "find_spec", return_value=None):
                    self.assertEqual(arm_qt5.missing_system_packages(fedora=True), arm_qt5.FEDORA_SYSTEM_PACKAGES)
            ubuntu = Path(folder) / "dist-packages"
            (ubuntu / "PyQt5").mkdir(parents=True)
            with patch.object(arm_qt5, "SYSTEM_DIST_PACKAGES", ubuntu):
                self.assertEqual(arm_qt5._system_packages_root(), ubuntu)
                with patch.object(arm_qt5.PathFinder, "find_spec", return_value=None):
                    self.assertEqual(arm_qt5.missing_system_packages(), arm_qt5.SYSTEM_PACKAGES)


class InstallCommands(unittest.TestCase):
    def install(self, record, packages, *, atomic=False, tools=True):
        calls = []

        def run(command, **_):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "ostree-booted"
            if atomic:
                marker.touch()
            with host(record), patch.object(system_setup.sys, "platform", "linux"), \
                    patch.object(system_setup.os, "geteuid", return_value=1000), \
                    patch.object(system_setup.os, "access", return_value=tools), \
                    patch.object(system_setup.sys.stdin, "isatty", return_value=True), \
                    patch.object(system_setup, "ATOMIC_MARKER", marker), \
                    patch.object(system_setup.subprocess, "run", side_effect=run):
                system_setup.install_system_packages(packages)
        return calls

    def test_fedora_installs_through_dnf_and_never_apt(self):
        calls = self.install(FEDORA, ["libglvnd-egl", "git-core"])
        self.assertEqual(len(calls), 1)
        command = calls[0]
        self.assertEqual(command[:3], ["/usr/bin/sudo", "/usr/bin/dnf", "install"])
        self.assertIn("--assumeyes", command)
        self.assertIn("--refresh", command)
        self.assertIn("--setopt=install_weak_deps=False", command)
        self.assertEqual(command[-2:], ["libglvnd-egl", "git-core"])
        self.assertNotIn("apt-get", " ".join(command))

    def test_mint_and_ubuntu_install_through_apt_get(self):
        for record in (MINT, UBUNTU):
            with self.subTest(record=record["ID"]):
                calls = self.install(record, ["git"])
                self.assertEqual([command[1] for command in calls], ["/usr/bin/apt-get", "/usr/bin/apt-get"])
                self.assertIn("update", calls[0])
                self.assertEqual(calls[1][-1], "git")
                self.assertIn("--no-install-recommends", calls[1])

    def test_each_distribution_refuses_another_distributions_package(self):
        for record, foreign in ((FEDORA, "python3-venv"), (MINT, "libglvnd-egl"), (UBUNTU, "git-core")):
            with self.subTest(record=record["ID"]):
                with self.assertRaisesRegex(RuntimeError, "unknown .* prerequisite package"):
                    self.install(record, [foreign])

    def test_fedora_atomic_gets_guidance_instead_of_a_failing_dnf(self):
        with self.assertRaisesRegex(RuntimeError, "rpm-ostree install git-core"):
            self.install(FEDORA, ["git-core"], atomic=True)

    def test_missing_package_manager_reports_the_manual_command(self):
        with self.assertRaisesRegex(RuntimeError, "Fedora's dnf.*\nsudo dnf install git-core"):
            self.install(FEDORA, ["git-core"], tools=False)


class NewerPython(unittest.TestCase):
    def source_files(self):
        for folder in ("main", "src/modules"):
            yield from (ROOT / folder).rglob("*.py")

    def test_thread_pools_come_from_their_defining_module(self):
        # Fedora 45 ships Python 3.15, whose concurrent.futures exposes the pool as a lazy
        # import. Inside the PySide6 process that form reached the caller unresolved, and
        # catalog loading failed with "'lazy_import' object is not callable".
        for path in self.source_files():
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                self.assertNotRegex(text, r"from concurrent\.futures import [^\n]*ThreadPoolExecutor")
                self.assertNotRegex(text, r"\bfutures\.ThreadPoolExecutor\b")


class Terminals(unittest.TestCase):
    def chosen(self, record):
        started = []
        available = {"ptyxis", "x-terminal-emulator", "gnome-terminal"}
        with host(record), patch.object(release.sys, "platform", "linux"), \
                patch.object(launch.sys, "platform", "linux"), \
                patch.object(launch.shutil, "which", side_effect=lambda name: name if name in available else None), \
                patch.object(launch.subprocess, "Popen", side_effect=lambda command, **_: started.append(command)):
            self.assertTrue(launch.bootstrap_terminal([], {}))
        return started[0][0]

    def test_terminal_preference_follows_the_distribution(self):
        self.assertEqual(self.chosen(FEDORA), "ptyxis")
        self.assertEqual(self.chosen({"ID": "ubuntu", "VERSION_ID": "26.04"}), "ptyxis")
        self.assertEqual(self.chosen(MINT), "x-terminal-emulator")
        self.assertEqual(self.chosen(UBUNTU), "x-terminal-emulator")


@unittest.skipUnless(sys.platform.startswith("linux"), "Native Bash fixtures")
class PythonRescueScript(unittest.TestCase):
    """A stripped desktop without Python recovers through its own package manager."""

    def rescue(self, release_text):
        audit = ROOT / "temp/audit/linux-distributions"
        audit.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=audit) as directory:
            fixture = Path(directory)
            folder = fixture / "direct/ubuntu"
            folder.mkdir(parents=True)
            os_release = fixture / "os-release"
            os_release.write_text(release_text)
            guard = (ROOT / "direct/ubuntu/release_guard.sh").read_text()
            quoted = "'" + str(os_release) + "'"
            (folder / "release_guard.sh").write_text(guard.replace("/etc/os-release", quoted).replace("/usr/lib/os-release", quoted))
            (folder / "architecture_guard.sh").write_text((ROOT / "direct/ubuntu/architecture_guard.sh").read_text())
            log = fixture / "sudo.log"
            sudo = fixture / "sudo"
            sudo.write_text("#!/bin/bash\nprintf '%s\\n' \"$*\" >> '" + str(log) + "'\n")
            sudo.chmod(0o700)
            tool = fixture / "tool"
            tool.write_text("#!/bin/bash\n")
            tool.chmod(0o700)
            script = (ROOT / "direct/ubuntu/run.sh").read_text()
            # This checkout's own path can contain spaces, so each substitute stays one shell word.
            for old, replacement in (("/usr/bin/python3", fixture / "absent-python"), ("/usr/bin/sudo", sudo),
                                     ("/usr/bin/dnf", tool), ("/usr/bin/apt-get", tool),
                                     ("/usr/bin/zenity", fixture / "no-dialog"), ("/usr/bin/kdialog", fixture / "no-dialog")):
                script = script.replace(old, "'" + str(replacement) + "'")
            run = folder / "run.sh"
            run.write_text(script)
            env = {name: value for name, value in os.environ.items() if name not in ("DISPLAY", "WAYLAND_DISPLAY")}
            result = subprocess.run(["bash", str(run), "--bootstrap-window"], capture_output=True, text=True,
                                    env=env, stdin=subprocess.DEVNULL, timeout=20)
            self.assertFalse(result.returncode == 0)
            return result.stdout + result.stderr, log.read_text() if log.exists() else ""

    def test_fedora_rescue_uses_dnf_and_only_python3(self):
        output, sudo = self.rescue("ID=fedora\nVERSION_ID=44\n")
        self.assertIn("Fedora Bootstrap will install python3.", output)
        self.assertIn("install --assumeyes --refresh --setopt=install_weak_deps=False", sudo)
        self.assertNotIn("python3-venv", sudo)
        self.assertEqual(len(sudo.splitlines()), 1)

    def test_mint_rescue_uses_apt_under_its_own_name(self):
        output, sudo = self.rescue('ID=linuxmint\nVERSION_ID="22.3"\nUBUNTU_CODENAME=noble\n')
        self.assertIn("Linux Mint Bootstrap will install python3 python3-venv.", output)
        self.assertIn("python3-venv", sudo)
        self.assertEqual(len(sudo.splitlines()), 2)  # update, then install.


if __name__ == "__main__":
    unittest.main(verbosity=2)
