#!/usr/bin/env python3
"""Shared Bootstrap-to-Lite handoff in owned fixtures; never launches setup."""
from __future__ import annotations
import ast
from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import lite_launch
from direct.ubuntu import launch


class LiteLaunchChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        scratch = ROOT / "temp/audit/lite-launch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=scratch)))
        (self.root / "LITE-MODE").mkdir()
        for name in ("UBUNTU", "WINDOWS.exe"):
            (self.root / "LITE-MODE" / name).write_bytes(b"never executed")
        self.project = str(self.root / 'Sketch Ω & $cash [literal]')

    def test_both_hosts_return_to_lite_once_with_one_literal_project_argument(self):
        for system, machine, name in (("linux", "x86_64", "UBUNTU"), ("win32", "AMD64", "WINDOWS.exe")):
            command = lite_launch.lite_command(self.root, ["--lite", "--project", self.project], system=system, machine=machine)
            self.assertEqual(command, [str(self.root / "LITE-MODE" / name), "--from-bootstrap", "--project", self.project])
        with self.assertRaisesRegex(RuntimeError, "requires"):
            lite_launch.lite_command(self.root, [], system="win32", machine="ARM64")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Ubuntu coordinator fixture")
    def test_ubuntu_workspace_dispatch_runs_lite_after_bootstrap_without_opening_main(self):
        window = Mock()
        with patch.object(launch, "ROOT", self.root), patch.object(launch.os, "chdir"), \
                patch.object(launch.os, "execvpe", side_effect=RuntimeError("recorded exec")) as execute, \
                patch.object(launch.subprocess, "Popen", side_effect=AssertionError("Unexpected process")):
            with self.assertRaisesRegex(RuntimeError, "recorded exec"):
                launch.launch_workspace(["--lite", "--project", self.project], {}, desktop=False, window=window)
        self.assertEqual(execute.call_args.args[1], [str(self.root / "LITE-MODE/UBUNTU"), "--from-bootstrap", "--project", self.project])
        window.finish.assert_called_once_with(True, "Opening MCU Flasher Lite…")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Ubuntu coordinator fixture")
    def test_missing_runtime_repairs_then_keeps_lite_as_the_requested_workspace(self):
        with patch.object(launch, "ROOT", self.root), \
                patch.object(launch, "persistent_setup_root", return_value=self.root), \
                patch.object(launch, "owned_installation_elsewhere", return_value=None), \
                patch.object(launch.BootstrapWindow, "open", return_value=None), \
                patch.object(launch.BootstrapWindow, "adopt", return_value=None), \
                patch.object(launch, "available_application_update", return_value=None), \
                patch.object(launch, "runtime_problem", side_effect=["Private runtime missing", None]), \
                patch.object(launch.os, "geteuid", return_value=1000, create=True), \
                patch("direct.ubuntu.preflight.host_preflight"), \
                patch("src.modules.runtime_resources.enforce_minimum_cpu_requirement", return_value=True), \
                patch("direct.ubuntu.user_install.refresh_registered_lite"), \
                patch.object(launch.subprocess, "call", return_value=0) as setup, \
                patch.object(launch, "launch_workspace", return_value=0) as workspace:
            self.assertEqual(launch.main(["--lite", "--project", self.project]), 0)
        self.assertIn("direct/ubuntu/setup.py", setup.call_args.args[0][-1])
        self.assertEqual(workspace.call_args.args[0], ["--lite", "--project", self.project])

    @unittest.skipUnless(sys.platform.startswith("linux"), "Ubuntu coordinator fixture")
    def test_lite_refuses_other_linux_hosts_before_setup_or_updates(self):
        with patch("src.modules.ubuntu_release.linux_distribution", return_value=SimpleNamespace(family="fedora")), \
                patch("src.modules.ubuntu_release.enforce_supported_ubuntu", return_value=True), \
                patch("src.modules.ubuntu_release.linux_label", return_value="Fedora"), \
                patch.object(launch, "available_application_update", side_effect=AssertionError("Update on refused Lite host")), \
                patch.object(launch, "report_error") as report, \
                patch.object(launch.subprocess, "call", side_effect=AssertionError("Setup on refused Lite host")):
            self.assertEqual(launch.main(["--lite"]), 1)
        self.assertIn("native Ubuntu", report.call_args.args[0])

    def test_windows_bootstrap_returns_to_visible_lite_without_starting_main_gui(self):
        source = ROOT / "src/modules/bootstrap.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_spawn_main_gui")
        namespace = {"SCRIPT_DIR": self.root, "Path": Path, "tempfile": tempfile,
                     "sys": SimpleNamespace(platform="win32", argv=["bootstrap", "--lite", "--project", self.project])}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
        real = lite_launch.lite_command
        with patch.object(lite_launch, "lite_command", side_effect=lambda root, args: real(root, args, system="win32", machine="AMD64")), \
                patch.object(subprocess, "STARTUPINFO", side_effect=lambda: SimpleNamespace(dwFlags=0), create=True), \
                patch.object(subprocess, "STARTF_USESHOWWINDOW", 1, create=True), \
                patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch.object(subprocess, "Popen") as spawn:
            process, log = namespace["_spawn_main_gui"]()
        self.assertIs(process, spawn.return_value)
        self.assertTrue(log.is_relative_to(self.root))
        self.assertEqual(spawn.call_args.args[0], [str(self.root / "LITE-MODE/WINDOWS.exe"), "--from-bootstrap", "--project", self.project])
        self.assertEqual(spawn.call_args.kwargs["startupinfo"].wShowWindow, 4)

    def test_legacy_installed_launcher_reuses_its_runtime_and_keeps_new_lite_sources(self):
        installed = self.root / "older-owned-installation"
        script = installed / "direct/ubuntu/launch.py"
        script.parent.mkdir(parents=True)
        script.write_text("# older launcher has no Lite argument\n")
        with patch("direct.ubuntu.user_install.is_owned_installation", return_value=True), \
                patch.object(launch, "runtime_problem", return_value=None) as check, \
                patch.object(launch, "launch_workspace", return_value=0) as workspace, \
                patch.object(launch.os, "execvpe", side_effect=AssertionError("Old launcher was executed")), \
                patch.object(launch.subprocess, "call", side_effect=AssertionError("Unneeded setup")):
            self.assertEqual(launch.forward_to_installation(installed, ["--lite", "--project", self.project], {},
                                                            repair=False, project=self.project, window=None), 0)
        check.assert_called_once_with({}, lite=True, runtime_root=installed)
        self.assertEqual(workspace.call_args.args[0], ["--lite", "--project", self.project])
        self.assertEqual(workspace.call_args.args[1]["MCU_LITE_PYTHON"], str(installed / ".venv-linux/bin/python"))

    def test_lite_runtime_check_selects_the_headless_profile_and_shared_board_gate(self):
        python = self.root / ".venv-linux/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("fixture")
        with patch.object(launch, "ROOT", self.root), patch.object(launch.os, "access", return_value=True), \
                patch("src.modules.ubuntu_architecture.elf_matches_host", return_value=True), \
                patch.object(launch.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertIsNone(launch.runtime_problem({}, lite=True))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("--lite", commands[0])
        self.assertIn("lite_bootstrap_ready", commands[1][-1])

    def test_lite_native_qt_check_does_not_require_webengine(self):
        from direct.ubuntu import preflight
        qt = self.root / "Qt"
        files = [qt / "lib/libQt6Core.so.6", qt / "plugins/platforms/libqxcb.so"]
        for path in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture native Qt")
        with patch.object(preflight, "elf_matches_host", return_value=True), \
                patch.object(preflight, "_check_linked_libraries") as check:
            preflight._check_qt_runtime_files(qt, "amd64", lite=True)
        self.assertEqual(tuple(check.call_args.args[0]), tuple(files))


if __name__ == "__main__":
    unittest.main()
