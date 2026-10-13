#!/usr/bin/env python3
"""Release admission fixtures only: no installers, GUI, live state or services."""
from __future__ import annotations

import contextlib
import io
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
            p = folder / "run.sh"
            p.write_text(p.read_text().replace("/usr/bin/python3", str(python)))
            for value, supported in (("22.04", False), ("24.03", False), ("24.04", True),
                                     ("26.04", True), ("broken", False), ("", False)):
                os_release.write_text('ID=ubuntu\nVERSION_ID="' + value + '"\n')
                result = subprocess.run(["bash", "-c", 'source "$1"; require_supported_ubuntu',
                                         "fixture", str(folder / "release_guard.sh")], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if supported else 1, result.stderr)
                if supported:
                    continue  # Never install or launch a supported fixture.
                for args in ([], ["--repair"], ["--check"], ["--install-shortcut"]):
                    result = subprocess.run(["bash", str(p), *args], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(release.REQUIREMENT, result.stderr)
                    self.assertFalse(marker.exists())
                result = subprocess.run(["bash", str(folder / "build_launcher.sh")], capture_output=True, text=True)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse((fixture / "MCU_Flasher").exists())
            os_release.unlink()
            result = subprocess.run(["bash", str(p)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Cannot read", result.stderr)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
