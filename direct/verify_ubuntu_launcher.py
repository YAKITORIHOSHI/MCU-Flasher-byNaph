#!/usr/bin/env python3
"""Hardware-free Ubuntu launcher checks; setup, GUI and user registration are mocked."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.ubuntu import launch, install_desktop, preflight


@unittest.skipUnless(sys.platform.startswith("linux"), "Native Ubuntu launcher")
class LauncherChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        audit = ROOT / "temp/audit/ubuntu-launcher"
        audit.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=audit)))
        self.stack.enter_context(patch.object(launch, "ROOT", self.root))
        self.stack.enter_context(patch("direct.ubuntu.preflight.host_preflight"))
        self.stack.enter_context(patch("src.modules.runtime_resources.enforce_minimum_cpu_requirement", return_value=True))
        self.stack.enter_context(patch.object(launch.os, "geteuid", return_value=1000))
        self.stack.enter_context(patch.object(launch.sys, "stdin", SimpleNamespace(isatty=lambda: True)))

    def test_native_executable_preserves_location_and_literal_arguments(self):
        folder = self.root / 'Moved Ubuntu Ω folder $cash `literal`'
        (folder / "direct/ubuntu").mkdir(parents=True)
        target = folder / "MCU_Flasher"
        if not (ROOT / "MCU_Flasher").is_file():
            self.skipTest("Build native MCU_Flasher before this check")
        shutil.copy2(ROOT / "MCU_Flasher", target)
        recorder = folder / "direct/ubuntu/run.sh"
        recorder.write_text('#!/bin/bash\nexec /usr/bin/python3 -c '
                            "'import json,sys; print(json.dumps(sys.argv[1:]))' \"$@\"\n", encoding="utf-8")
        arguments = ["--project", 'Sketch Ω with spaces $(touch NEVER) "quotes"', "--new-window"]
        result = subprocess.run([str(target), *arguments], cwd=self.root,
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(result.stdout), ["--desktop", *arguments])
        self.assertFalse((self.root / "NEVER").exists())

    def test_missing_runtime_repairs_once_then_launches_with_same_arguments(self):
        with patch.object(launch, "runtime_problem", side_effect=["Missing runtime", None]), \
                patch.object(launch.subprocess, "call", return_value=0) as setup, \
                patch.object(launch, "launch_workspace", return_value=0) as gui:
            self.assertEqual(launch.main(["--project", "Sketch With Spaces", "--new-window"]), 0)
        self.assertEqual(setup.call_count, 1)
        self.assertEqual(setup.call_args.args[0][-1], str(self.root / "direct/ubuntu/setup.py"))
        self.assertEqual(gui.call_args.args[0], ["--project", "Sketch With Spaces", "--new-window"])

    def test_ready_runtime_does_not_run_installer(self):
        with patch.object(launch, "runtime_problem", return_value=None), \
                patch.object(launch.subprocess, "call", side_effect=AssertionError("Installer ran")), \
                patch.object(launch, "launch_workspace", return_value=0) as gui:
            self.assertEqual(launch.main([]), 0)
        gui.assert_called_once()

    def test_runtime_with_only_scons_routes_to_native_board_bootstrap(self):
        python = self.root / ".venv-linux/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("fixture")
        python.chmod(0o755)
        with patch.object(launch.subprocess, "run", side_effect=[
                SimpleNamespace(returncode=0, stderr=""), SimpleNamespace(returncode=1, stderr="")]) as run:
            self.assertIn("native board packages", launch.runtime_problem({}))
        self.assertIn("bootstrap_ready", run.call_args.args[0][-1])

    def test_failed_setup_never_starts_workspace(self):
        with patch.object(launch, "runtime_problem", return_value="Broken runtime"), \
                patch.object(launch.subprocess, "call", return_value=7) as setup, \
                patch.object(launch, "launch_workspace", side_effect=AssertionError("GUI launched")):
            self.assertEqual(launch.main(["--repair"]), 7)
        setup.assert_called_once()

    def test_check_never_installs_or_opens_a_dialog(self):
        with patch.object(launch.sys, "stdin", SimpleNamespace(isatty=lambda: False)), \
                patch("direct.ubuntu.preflight.host_preflight", side_effect=RuntimeError("Missing Qt library")), \
                patch.object(launch, "report_error") as error, \
                patch.object(launch.subprocess, "call", side_effect=AssertionError("Installer ran")):
            self.assertEqual(launch.main(["--desktop", "--check"]), 1)
        error.assert_called_once_with("Missing Qt library", desktop=False)

    def test_desktop_first_run_opens_one_setup_terminal(self):
        with patch.object(launch.sys, "stdin", SimpleNamespace(isatty=lambda: False)), \
                patch.object(launch, "runtime_problem", return_value="No runtime"), \
                patch.object(launch, "bootstrap_terminal", return_value=True) as terminal, \
                patch.object(launch, "launch_workspace", side_effect=AssertionError("GUI launched early")):
            self.assertEqual(launch.main(["--desktop", "--project", "Sketch Ω", "--new-window"]), 0)
        self.assertEqual(terminal.call_args.args[0], ["--project", "Sketch Ω", "--new-window"])

    def test_missing_system_libraries_enter_desktop_bootstrap_before_runtime_validation(self):
        missing = preflight.MissingSystemDependencies(["libxcb-cursor0", "libxcb-xinerama0"])
        with patch.object(launch.sys, "stdin", SimpleNamespace(isatty=lambda: False)), \
                patch.object(preflight, "host_preflight", side_effect=missing), \
                patch.object(launch, "runtime_problem", side_effect=AssertionError("Qt checked before OS repair")), \
                patch.object(launch, "bootstrap_terminal", return_value=True) as terminal, \
                patch.object(launch, "launch_workspace", side_effect=AssertionError("GUI started early")):
            self.assertEqual(launch.main(["--desktop", "--project", "Sketch Ω", "--new-window"]), 0)
        self.assertEqual(terminal.call_args.args[0], ["--project", "Sketch Ω", "--new-window"])

    def test_terminal_system_repair_rechecks_then_launches_without_restarting_bootstrap(self):
        missing = preflight.MissingSystemDependencies(["libxcb-cursor0"])
        with patch.object(preflight, "host_preflight", side_effect=[missing, None]) as host, \
                patch.object(launch, "runtime_problem", return_value=None) as runtime, \
                patch.object(launch.subprocess, "call", return_value=0) as setup, \
                patch.object(launch, "bootstrap_terminal", side_effect=AssertionError("Recursive terminal")), \
                patch.object(launch, "launch_workspace", return_value=0) as gui:
            self.assertEqual(launch.main(["--bootstrap-window", "--project", "Sketch Ω"]), 0)
        self.assertEqual(host.call_count, 2)
        runtime.assert_called_once()
        setup.assert_called_once()
        self.assertEqual(gui.call_args.args[0], ["--project", "Sketch Ω"])

    def test_missing_system_libraries_in_check_never_enter_bootstrap(self):
        missing = preflight.MissingSystemDependencies(["libxcb-cursor0"])
        with patch.object(preflight, "host_preflight", side_effect=missing), \
                patch.object(launch, "report_error") as error, \
                patch.object(launch, "bootstrap_terminal", side_effect=AssertionError("Terminal opened")), \
                patch.object(launch.subprocess, "call", side_effect=AssertionError("Setup invoked")):
            self.assertEqual(launch.main(["--desktop", "--check"]), 1)
        error.assert_called_once_with(str(missing), desktop=False)

    def test_check_takes_precedence_over_shortcut_registration_and_repair(self):
        with patch.object(launch, "runtime_problem", return_value=None), \
                patch.object(install_desktop, "main", side_effect=AssertionError("Shortcut written")), \
                patch.object(launch.subprocess, "call", side_effect=AssertionError("Setup invoked")):
            self.assertEqual(launch.main(["--check", "--install-shortcut", "--repair"]), 0)

    def test_root_shortcut_registration_is_rejected_without_writes(self):
        with patch.object(launch.os, "geteuid", return_value=0), \
                patch.object(launch, "report_error") as error, \
                patch.object(install_desktop, "main", side_effect=AssertionError("Root shortcut written")):
            self.assertEqual(launch.main(["--install-shortcut"]), 1)
        self.assertIn("normal Ubuntu desktop account", error.call_args.args[0])

    def test_setup_terminal_receives_arguments_without_shell_interpolation(self):
        arguments = ["--project", "Sketch $(not-a-command) with spaces", "--new-window"]
        with patch.object(launch.shutil, "which", side_effect=lambda name: "/usr/bin/gnome-terminal" if name == "gnome-terminal" else None), \
                patch.object(launch.subprocess, "Popen") as child:
            self.assertTrue(launch.bootstrap_terminal(arguments, {"PATH": "/usr/bin"}))
        self.assertEqual(child.call_args.args[0], ["/usr/bin/gnome-terminal", "--", "/bin/bash",
                         str(self.root / "direct/ubuntu/run.sh"), "--bootstrap-window", *arguments])
        self.assertNotIn("shell", child.call_args.kwargs)

    def test_native_shortcut_and_registration_use_fixture_locations(self):
        (self.root / "MCU_Flasher").write_bytes(b"fixture")
        data_home = self.root / "local-data"
        with patch.object(install_desktop, "ROOT", self.root), \
                patch.dict(os.environ, {"XDG_DATA_HOME": str(data_home)}):
            self.assertEqual(install_desktop.main(["--install"]), 0)
        text = (self.root / "MCU Flasher.desktop").read_text(encoding="utf-8")
        self.assertEqual(text, (data_home / "applications/mcu-flasher.desktop").read_text(encoding="utf-8"))
        self.assertIn("Terminal=false", text)
        self.assertIn(" --repair", text)
        self.assertTrue(os.access(self.root / "MCU Flasher.desktop", os.X_OK))

    def test_desktop_parser_launches_an_executable_with_literal_path_characters(self):
        gio = shutil.which("gio")
        if not gio:
            self.skipTest("GLib desktop-entry launcher unavailable")
        folder = self.root / 'Ubuntu Ω %folder $cash `literal` "quoted"'
        folder.mkdir()
        recorder = folder / "MCU_Flasher"
        recorder.write_text('#!/bin/bash\nexec /usr/bin/python3 -c '
                            "'from pathlib import Path; import os; "
                            "Path(os.environ[\"MCU_LAUNCHER_FIXTURE_RECEIPT\"]).write_text(\"launched\")'\n",
                            encoding="utf-8")
        recorder.chmod(0o755)
        desktop = folder / "fixture.desktop"
        desktop.write_text(install_desktop.desktop_entry(folder), encoding="utf-8")
        receipt = self.root / "receipt.txt"
        env = os.environ.copy()
        env["MCU_LAUNCHER_FIXTURE_RECEIPT"] = str(receipt)
        result = subprocess.run([gio, "launch", str(desktop)], cwd=self.root, env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        deadline = time.monotonic() + 3
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(receipt.read_text(), "launched")

    def test_foreign_runtime_variables_are_removed_but_cli_path_is_retained(self):
        values = {"PYTHONHOME": "C:/Python", "PYTHONPATH": "foreign", "QT_PLUGIN_PATH": "Qt5",
                  "MCU_FLASHER_WORKSPACE_RUNTIME": "1", "PIP_NO_INDEX": "1", "PATH": "custom-cli-path"}
        with patch.dict(os.environ, values):
            env = launch.clean_environment()
        self.assertEqual(env["PATH"], "custom-cli-path")
        for key in values.keys() - {"PATH"}:
            self.assertNotIn(key, env)
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
