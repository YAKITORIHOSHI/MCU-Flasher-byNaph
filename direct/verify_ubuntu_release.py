#!/usr/bin/env python3
"""Release admission fixtures only: no installers, GUI, live state or services."""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import ubuntu_release as release
from direct.ubuntu import launch, setup, preflight, install_desktop, system_setup, arduino_cli, opencode_setup
from cleaner.ubuntu import maintenance
from src.modules import offline_bootstrap, private_python_guard


def headless_environment():
    """Rejected release fixtures must never reach the desktop running this verifier."""
    return {name: value for name, value in os.environ.items()
            if name not in ("DISPLAY", "WAYLAND_DISPLAY")}


class ReleaseChecks(unittest.TestCase):
    def test_parsed_minimum_newer_and_below_minimum(self):
        for value, supported in (("22.04", False), ("24.03", False), ("24.04", True),
                                 ("24.4", True), ("24.04.1", True), ("24.10", True),
                                 ("26.04", True), ("100.01", True)):
            with self.subTest(value=value), patch.object(release.sys, "platform", "linux"), \
                    patch.object(release.platform, "freedesktop_os_release",
                                 return_value={"ID": "ubuntu", "VERSION_ID": value}):
                if supported:
                    self.assertEqual(release.require_supported_ubuntu(), tuple(map(int, value.split("."))))
                else:
                    with self.assertRaisesRegex(RuntimeError, "^MCU Flasher requires Ubuntu 24.04 or newer\\.$"):
                        release.require_supported_ubuntu()

    def test_missing_malformed_and_non_ubuntu_fail_explicitly(self):
        records = [None, {}, {"ID": "debian", "VERSION_ID": "26.04"}]
        records += [{"ID": "ubuntu", "VERSION_ID": value}
                    for value in (None, "", "24", "24.x", " 24.04", "24.04 LTS", "24.04.1.2", "99999.04")]
        for record in records:
            with self.subTest(record=record), patch.object(release.sys, "platform", "linux"), \
                    patch.object(release.platform, "freedesktop_os_release", return_value=record):
                with self.assertRaisesRegex(RuntimeError, "Cannot identify|missing or malformed"):
                    release.require_supported_ubuntu()
        with patch.object(release.sys, "platform", "linux"), \
                patch.object(release.platform, "freedesktop_os_release", side_effect=OSError("missing")):
            with self.assertRaisesRegex(RuntimeError, "Cannot read"):
                release.require_supported_ubuntu()

    def test_linux_mint_is_admitted_only_on_its_reviewed_ubuntu_base(self):
        # Fields match /usr/lib/os-release in Mint's own base-files 13ubuntu10mint22.3.0.
        for version in ("22", "22.1", "22.2", "22.3"):
            record = {"ID": "linuxmint", "ID_LIKE": "ubuntu debian", "VERSION_ID": version, "UBUNTU_CODENAME": "noble"}
            with self.subTest(version=version), patch.object(release.sys, "platform", "linux"), \
                    patch.object(release.platform, "freedesktop_os_release", return_value=record):
                self.assertEqual(release.require_supported_ubuntu(), (24, 4))
                distribution = release.linux_distribution()
                self.assertEqual((distribution.label, distribution.package_manager), ("Linux Mint", "apt"))
        refused = [{"VERSION_ID": "21.3", "UBUNTU_CODENAME": "jammy"},   # Below the Ubuntu 24.04 base.
                   {"VERSION_ID": "23", "UBUNTU_CODENAME": "resolute"},  # No reviewed policy yet.
                   {"VERSION_ID": "22.1", "UBUNTU_CODENAME": "jammy"},   # Version and base disagree.
                   {"VERSION_ID": "6", "DEBIAN_CODENAME": "bookworm"},   # LMDE has a separate base.
                   {"VERSION_ID": "22.1"}]
        for fields in refused:
            with self.subTest(fields=fields), patch.object(release.sys, "platform", "linux"), \
                    patch.object(release.platform, "freedesktop_os_release",
                                 return_value={"ID": "linuxmint", **fields}):
                with self.assertRaisesRegex(RuntimeError, "^MCU Flasher requires Linux Mint 22"):
                    release.require_supported_ubuntu()
        for value in ("", "broken", "22.x", " 22.1"):
            with self.subTest(value=value), patch.object(release.sys, "platform", "linux"), \
                    patch.object(release.platform, "freedesktop_os_release",
                                 return_value={"ID": "linuxmint", "VERSION_ID": value, "UBUNTU_CODENAME": "noble"}):
                with self.assertRaisesRegex(RuntimeError, "missing or malformed"):
                    release.require_supported_ubuntu()

    def test_fedora_is_admitted_by_its_own_integer_release(self):
        for value, supported in (("43", False), ("44", True), ("45", True), ("100", True)):
            with self.subTest(value=value), patch.object(release.sys, "platform", "linux"), \
                    patch.object(release.platform, "freedesktop_os_release",
                                 return_value={"ID": "fedora", "VERSION_ID": value}):
                if supported:
                    self.assertEqual(release.require_supported_ubuntu(), (int(value),))
                    distribution = release.linux_distribution()
                    self.assertEqual((distribution.label, distribution.package_manager), ("Fedora", "dnf"))
                else:
                    with self.assertRaisesRegex(RuntimeError, "^MCU Flasher requires Fedora 44 or newer\\.$"):
                        release.require_supported_ubuntu()
        for value in ("", "44.1", "rawhide", " 44"):
            with self.subTest(value=value), patch.object(release.sys, "platform", "linux"), \
                    patch.object(release.platform, "freedesktop_os_release",
                                 return_value={"ID": "fedora", "VERSION_ID": value}):
                with self.assertRaisesRegex(RuntimeError, "missing or malformed"):
                    release.require_supported_ubuntu()

    def test_id_like_never_admits_a_relative(self):
        # Pop!_OS, Zorin, RHEL and others name a supported family only in ID_LIKE.
        for record in ({"ID": "pop", "ID_LIKE": "ubuntu debian", "VERSION_ID": "24.04", "UBUNTU_CODENAME": "noble"},
                       {"ID": "rhel", "ID_LIKE": "fedora", "VERSION_ID": "44"},
                       {"ID": "UBUNTU", "VERSION_ID": "24.04"}):
            with self.subTest(record=record), patch.object(release.sys, "platform", "linux"), \
                    patch.object(release.platform, "freedesktop_os_release", return_value=record):
                with self.assertRaisesRegex(RuntimeError, "Cannot identify"):
                    release.require_supported_ubuntu()

    def test_macos_has_its_own_minimum_from_the_gui_wheels(self):
        for value, supported in (("13.7.4", False), ("14.0", True), ("15.7.1", True), ("26.1", True)):
            with self.subTest(value=value), patch.object(release.sys, "platform", "darwin"), \
                    patch.object(release.platform, "mac_ver", return_value=(value, ("", "", ""), "")), \
                    patch.object(release.platform, "freedesktop_os_release", side_effect=AssertionError("OS read")):
                if supported:
                    self.assertEqual(release.require_supported_ubuntu(), tuple(map(int, value.split("."))))
                else:
                    with self.assertRaisesRegex(RuntimeError, "^MCU Flasher requires macOS 14 or newer\\.$"):
                        release.require_supported_ubuntu()
        for value in ("", "fifteen", "15.x"):
            with self.subTest(value=value), patch.object(release.sys, "platform", "darwin"), \
                    patch.object(release.platform, "mac_ver", return_value=(value, ("", "", ""), "")):
                with self.assertRaisesRegex(RuntimeError, "missing or malformed"):
                    release.require_supported_ubuntu()

    def test_linux_labels_and_tables_select_the_distribution_adapter(self):
        for record, label, manager, venv, tool in (
                ({"ID": "ubuntu", "VERSION_ID": "24.04"}, "Ubuntu", "apt", "python3-venv", "sudo apt install"),
                ({"ID": "linuxmint", "VERSION_ID": "22.3", "UBUNTU_CODENAME": "noble"}, "Linux Mint", "apt", "python3-venv", "sudo apt install"),
                ({"ID": "fedora", "VERSION_ID": "44"}, "Fedora", "dnf", "python3", "sudo dnf install")):
            with self.subTest(label=label), patch.object(release.sys, "platform", "linux"), \
                    patch.object(release.platform, "freedesktop_os_release", return_value=record):
                self.assertEqual((release.linux_label(), release.package_manager()), (label, manager))
                self.assertTrue(preflight.system_install_command(["git-core"]).startswith(tool))
                packages = {package for _, package in preflight.system_libraries("amd64")}
                self.assertEqual("libglvnd-egl" in packages, manager == "dnf")
                self.assertEqual("libegl1" in packages, manager == "apt")
                error = preflight.MissingSystemDependencies([venv])
                self.assertIn(label + " system dependencies are missing", str(error))
                self.assertIn(tool, str(error))
        # Reading a release that cannot be identified must not choose Fedora tables.
        with patch.object(release.platform, "freedesktop_os_release", side_effect=OSError("missing")):
            self.assertEqual((release.linux_label(), release.package_manager()), ("Ubuntu", "apt"))

    def test_windows_never_reads_linux_release(self):
        with patch.object(release.sys, "platform", "win32"), \
                patch.object(release.platform, "freedesktop_os_release", side_effect=AssertionError("OS read")):
            self.assertIsNone(release.require_supported_ubuntu())

    def test_python_entries_reject_before_any_launch_install_or_state_write(self):
        for value in ("22.04", "24.03", "", "broken"):
            with self.subTest(value=value), patch.object(release.sys, "platform", "linux"), \
                    patch.object(release.platform, "freedesktop_os_release",
                                 return_value={"ID": "ubuntu", "VERSION_ID": value}), \
                    patch.object(subprocess, "run", side_effect=AssertionError("Child started")), \
                    patch.object(subprocess, "Popen", side_effect=AssertionError("Child started")), \
                    patch.object(subprocess, "call", side_effect=AssertionError("Child started")), \
                    patch.object(Path, "mkdir", side_effect=AssertionError("State created")), \
                    contextlib.redirect_stderr(io.StringIO()):
                for entry, args in ((launch.main, []), (launch.main, ["--repair"]),
                                    (launch.main, ["--check"]), (launch.main, ["--install-shortcut"]),
                                    (setup.main, []), (install_desktop.main, ["--install"]),
                                    (maintenance.main, ["runtime", "--apply", "--yes"]),
                                    (offline_bootstrap.main, ["--core", "unused"])):
                    self.assertEqual(entry(args), 1)
                for entry in (preflight.host_preflight, preflight.runtime_preflight):
                    with self.assertRaises(RuntimeError):
                        entry()
                for entry, arguments in ((system_setup.install_system_packages, (["python3"],)),
                                         (arduino_cli.ensure_arduino_cli, ()), (opencode_setup.ensure_opencode_cli, ()),
                                         (offline_bootstrap.prepare_runtime, (ROOT / "unused",)),
                                         (offline_bootstrap.prepare, (ROOT / "unused",))):
                    with self.assertRaisesRegex(RuntimeError, "requires Ubuntu 24.04 or newer"):
                        entry(*arguments)
                with self.assertRaises(SystemExit) as stopped:
                    private_python_guard.enforce_private_python(allow_repair=True)
                self.assertEqual(stopped.exception.code, 1)
                for file in ("mcu_flash_gui.py", "main/mcu_flash_gui.py", "src/modules/launcher.py",
                             "src/modules/arduino_lib_req.py", "direct/setup_ubuntu.py",
                             "src/qscintilla_viewer.py", "src/qscintilla_editor.py",
                             "src/modules/project_terminal.py", "src/modules/downloader.py"):
                    with self.assertRaises(SystemExit) as stopped:
                        runpy.run_path(str(ROOT / file), run_name="__main__")
                    self.assertEqual(stopped.exception.code, 1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Native Bash fixtures")
    def test_bash_guard_and_real_launch_copies_never_reach_python_on_rejection(self):
        audit = ROOT / "temp/audit/ubuntu-release"
        audit.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=audit) as directory:
            fixture = Path(directory)
            folder = fixture / "direct/ubuntu"
            folder.mkdir(parents=True)
            os_release = fixture / "os-release"
            text = (ROOT / "direct/ubuntu/release_guard.sh").read_text()
            # Only the isolated copy receives a path substitution; production
            # has no env/argv override capable of bypassing admission.
            text = text.replace("/etc/os-release", "'" + str(os_release) + "'").replace("/usr/lib/os-release", "'" + str(os_release) + "'")
            (folder / "release_guard.sh").write_text(text)
            for file in ("run.sh", "build_launcher.sh"):
                (folder / file).write_text((ROOT / "direct/ubuntu" / file).read_text())
            marker = fixture / "python-reached"
            python = fixture / "python"
            python.write_text("#!/bin/bash\ntouch '" + str(marker) + "'\nexit 91\n")
            python.chmod(0o700)
            # Every dialog tool in this copy is an owned recorder, so even a regression
            # here cannot open a real window for a deliberately wrong release.
            dialog_marker = fixture / "dialog-opened"
            dialog = fixture / "dialog"
            dialog.write_text("#!/bin/bash\nprintf '%s\\n' \"$*\" >> '" + str(dialog_marker) + "'\n")
            dialog.chmod(0o700)
            p = folder / "run.sh"
            quoted_dialog = "'" + str(dialog) + "'"
            p.write_text(p.read_text().replace("/usr/bin/python3", str(python))
                         .replace("/usr/bin/zenity", quoted_dialog).replace("/usr/bin/kdialog", quoted_dialog))
            env = headless_environment()
            # Each accepted distribution mirrors the Python policy in shell builtins.
            for fields, expected in (
                    ('ID=linuxmint\nVERSION_ID="22"\nUBUNTU_CODENAME=noble\n', "linuxmint 24 4 Linux Mint"),
                    ('ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_ID="22.3"\nUBUNTU_CODENAME=noble\n', "linuxmint 24 4 Linux Mint"),
                    ("ID=fedora\nVERSION_ID=44\n", "fedora 0 0 Fedora"),
                    ('ID=fedora\nVERSION_ID=45\nVARIANT_ID="workstation"\n', "fedora 0 0 Fedora"),
                    ('ID=ubuntu\nVERSION_ID="26.04"\n', "ubuntu 26 4 Ubuntu"),
                    ('ID=linuxmint\nVERSION_ID="21.3"\nUBUNTU_CODENAME=jammy\n', None),
                    ('ID=linuxmint\nVERSION_ID="22.1"\nUBUNTU_CODENAME=jammy\n', None),
                    ('ID=linuxmint\nVERSION_ID="23"\nUBUNTU_CODENAME=resolute\n', None),
                    ('ID=linuxmint\nVERSION_ID="6"\nDEBIAN_CODENAME=bookworm\n', None),
                    ('ID=linuxmint\nVERSION_ID="22.1"\n', None),
                    ('ID=linuxmint\nVERSION_ID="22.1"\nUBUNTU_CODENAME=noble\nUBUNTU_CODENAME=jammy\n', None),
                    ("ID=fedora\nVERSION_ID=43\n", None), ("ID=fedora\nVERSION_ID=44.1\n", None),
                    ("ID=fedora\nVERSION_ID=rawhide\n", None), ("ID=fedora\n", None),
                    ('ID=pop\nID_LIKE="ubuntu debian"\nVERSION_ID="24.04"\nUBUNTU_CODENAME=noble\n', None),
                    ('ID=rhel\nID_LIKE="fedora"\nVERSION_ID="44"\n', None),
                    ("ID=fedora\nID=ubuntu\nVERSION_ID=44\n", None),
                    ('VERSION_ID=44\n', None)):
                with self.subTest(fields=fields):
                    os_release.write_text(fields)
                    result = subprocess.run(
                        ["bash", "-c", 'source "$1"; require_supported_ubuntu || exit 1; '
                                       'printf "%s %s %s %s" "$mcu_linux_family" "$mcu_ubuntu_release_major" '
                                       '"$mcu_ubuntu_release_minor" "$mcu_linux_label"',
                         "fixture", str(folder / "release_guard.sh")], capture_output=True, text=True, env=env)
                    self.assertEqual(result.returncode, 0 if expected else 1, result.stderr)
                    if expected:
                        self.assertEqual(result.stdout, expected)
                    else:
                        self.assertEqual(result.stdout, "")
                        self.assertIn("MCU Flasher requires", result.stderr)
                        run = subprocess.run(["bash", str(p)], capture_output=True, text=True, env=env)
                        self.assertEqual(run.returncode, 1, run.stderr)
                        self.assertFalse(marker.exists())
            # An environment value cannot stand in for a rejected release's policy.
            os_release.write_text("ID=fedora\nVERSION_ID=43\n")
            forged = dict(env, mcu_linux_family="ubuntu", mcu_ubuntu_release_major="26", mcu_linux_label="Ubuntu")
            self.assertEqual(subprocess.run(["bash", str(p)], capture_output=True, env=forged).returncode, 1)
            self.assertFalse(marker.exists())
            for value, supported in (("22.04", False), ("24.03", False), ("24.04", True),
                                     ("26.04", True), ("broken", False), ("", False)):
                os_release.write_text('ID=ubuntu\nVERSION_ID="' + value + '"\n')
                result = subprocess.run(["bash", "-c", 'source "$1"; require_supported_ubuntu',
                                         "fixture", str(folder / "release_guard.sh")], capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 0 if supported else 1, result.stderr)
                if supported:
                    continue  # Never install or launch a supported fixture.
                for args in ([], ["--repair"], ["--check"], ["--install-shortcut"]):
                    result = subprocess.run(["bash", str(p), *args], capture_output=True, text=True, env=env)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(release.REQUIREMENT, result.stderr)
                    self.assertFalse(marker.exists())
                result = subprocess.run(["bash", str(folder / "build_launcher.sh")], capture_output=True, text=True, env=env)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse((fixture / "MCU_Flasher").exists())
            # With a display, a terminal-less refusal shows a dialog only for a LAUNCHER entry.
            os_release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n')
            display = dict(env, DISPLAY=":fixture")
            for args, opened in (([], False), (["--check"], False), (["--repair"], False), (["--desktop"], True)):
                with self.subTest(args=args):
                    dialog_marker.unlink(missing_ok=True)
                    result = subprocess.run(["bash", str(p), *args], capture_output=True, text=True, env=display)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(release.REQUIREMENT, result.stderr)
                    self.assertEqual(dialog_marker.exists(), opened)
                    self.assertFalse(marker.exists())
            dialog_marker.unlink(missing_ok=True)
            result = subprocess.run(["bash", str(p), "--desktop"], capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(dialog_marker.exists(), "No display means no dialog")
            os_release.unlink()
            result = subprocess.run(["bash", str(p)], capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Cannot read", result.stderr)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
