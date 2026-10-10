#!/usr/bin/env python3
"""Isolated Ubuntu administrator/setup fixtures; never run real sudo, apt or the GUI."""
from __future__ import annotations

from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.ubuntu import preflight, setup, system_setup


class UbuntuReleaseInstallerChecks(unittest.TestCase):
    def test_ubuntu_26_uses_allowlisted_t64_packages_with_no_gui_or_real_apt(self):
        with patch.object(preflight.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "26.04"}), \
                patch.object(system_setup, "sys", SimpleNamespace(platform="linux", stdin=SimpleNamespace(isatty=lambda: True))), \
                patch.object(system_setup.os, "geteuid", return_value=1000, create=True), \
                patch.object(system_setup.os, "access", return_value=True), \
                patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run, \
                redirect_stdout(io.StringIO()):
            system_setup.install_system_packages(["libasound2", "libglib2.0-0", "libasound2t64"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][-2:], ["libasound2t64", "libglib2.0-0t64"])
        self.assertNotIn("shell", run.call_args.kwargs)


@unittest.skipUnless(sys.platform.startswith("linux"), "Ubuntu system-package bootstrap")
class SystemSetupChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(system_setup.os, "geteuid", return_value=1000))
        self.stack.enter_context(patch.object(system_setup.os, "access", return_value=True))
        self.stack.enter_context(patch.object(system_setup.sys, "stdin", SimpleNamespace(isatty=lambda: True)))
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.run = self.stack.enter_context(patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0)))

    def test_installs_only_detected_packages_through_sudo_then_rechecks(self):
        missing = preflight.MissingSystemDependencies(["libxcb-cursor0", "libxcb-xinerama0", "libxcb-cursor0"])
        with patch.object(preflight, "host_preflight", side_effect=[missing, None]) as host:
            system_setup.ensure_system_dependencies(require_display=True, env={"PATH": "/usr/bin"})
        self.assertEqual(host.call_count, 2)
        self.assertEqual(self.run.call_count, 2)
        update, install = [call.args[0] for call in self.run.call_args_list]
        self.assertEqual(update[:2], ["/usr/bin/sudo", "/usr/bin/apt-get"])
        self.assertEqual(update[-1], "update")
        self.assertEqual(install[-6:], ["install", "--yes", "--no-remove", "--no-install-recommends",
                                       "libxcb-cursor0", "libxcb-xinerama0"])
        self.assertEqual(self.run.call_args.kwargs["cwd"], "/")
        self.assertNotIn("shell", self.run.call_args.kwargs)
        self.assertIn("administrator prompt", self.output.getvalue())

    def test_healthy_host_never_authenticates(self):
        with patch.object(preflight, "host_preflight"):
            system_setup.ensure_system_dependencies()
        self.run.assert_not_called()

    def test_clipboard_packages_use_the_same_allowlisted_os_authentication(self):
        system_setup.install_system_packages(["xclip", "wl-clipboard", "ripgrep"])
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.run.call_args.args[0][-3:], ["xclip", "wl-clipboard", "ripgrep"])
        self.assertEqual(self.run.call_args.args[0][:2], ["/usr/bin/sudo", "/usr/bin/apt-get"])

    def test_non_tty_desktop_uses_os_authentication_agent(self):
        with patch.object(system_setup.sys, "stdin", SimpleNamespace(isatty=lambda: False)), \
                patch.dict(os.environ, {"DISPLAY": ":fixture"}):
            system_setup.install_system_packages(["python3-tk"])
        self.assertTrue(all(call.args[0][:2] == ["/usr/bin/pkexec", "/usr/bin/apt-get"]
                            for call in self.run.call_args_list))

    def test_ubuntu_alsa_rename_and_deduplication(self):
        with patch.object(preflight.platform, "freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "24.04"}):
            system_setup.install_system_packages(["libasound2", "libasound2t64"])
        self.assertEqual(self.run.call_args.args[0][-1], "libasound2t64")
        self.assertEqual(self.run.call_args.args[0].count("libasound2t64"), 1)

    def test_unknown_package_and_root_execution_never_authenticate(self):
        for package in ("--allow-unauthenticated", "libxcb-cursor0; touch NEVER", "/tmp/untrusted.deb", "curl"):
            with self.assertRaisesRegex(RuntimeError, "unknown"):
                system_setup.install_system_packages([package])
        with patch.object(system_setup.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "normal desktop account"):
                system_setup.install_system_packages(["python3-tk"])
        self.run.assert_not_called()

    def test_no_authentication_surface_stops_with_exact_manual_alternative(self):
        with patch.object(system_setup.sys, "stdin", SimpleNamespace(isatty=lambda: False)), \
                patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}):
            with self.assertRaisesRegex(RuntimeError, "sudo apt install libxcb-cursor0"):
                system_setup.install_system_packages(["libxcb-cursor0"])
        self.run.assert_not_called()

    def test_cancelled_authentication_stops_without_install_or_recheck(self):
        self.run.return_value = SimpleNamespace(returncode=126)
        with patch.object(preflight, "host_preflight", side_effect=preflight.MissingSystemDependencies(["libxcb-cursor0"])) as host:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                system_setup.ensure_system_dependencies()
        self.run.assert_called_once()
        host.assert_called_once()

    def test_failed_install_does_not_retry_or_claim_ready(self):
        self.run.side_effect = [SimpleNamespace(returncode=0), SimpleNamespace(returncode=100)]
        with patch.object(preflight, "host_preflight", side_effect=preflight.MissingSystemDependencies(["libxcb-xinerama0"])) as host:
            with self.assertRaisesRegex(RuntimeError, "exit 100"):
                system_setup.ensure_system_dependencies()
        self.assertEqual(self.run.call_count, 2)
        host.assert_called_once()

    def test_successful_apt_with_missing_library_stops_before_private_environment(self):
        missing = preflight.MissingSystemDependencies(["libxcb-cursor0"])
        output = io.StringIO()
        with patch.object(preflight, "host_preflight", side_effect=[missing, missing]), \
                patch("src.modules.runtime_resources.enforce_minimum_cpu_requirement", return_value=True), \
                patch.object(setup.venv, "EnvBuilder", side_effect=AssertionError("Venv prepared before host ready")), \
                patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "", "MCU_FLASHER_WORKSPACE_RUNTIME": ""}), \
                redirect_stderr(output):
            self.assertEqual(setup.main([]), 1)
        self.assertEqual(self.run.call_count, 2)
        self.assertIn("dependency checks still fail", output.getvalue())

    def test_system_install_continues_into_private_setup_and_native_toolchains(self):
        audit = ROOT / "temp/audit/ubuntu-system-setup"
        audit.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=audit) as folder:
            root = Path(folder)
            missing = preflight.MissingSystemDependencies(["libxcb-cursor0", "libxcb-xinerama0"])
            with patch.object(preflight, "host_preflight", side_effect=[missing, None]), \
                    patch.object(setup, "ROOT", root), patch.object(setup, "ENV_DIR", root / ".venv-linux"), \
                    patch.object(setup.venv, "EnvBuilder") as builder, \
                    patch("direct.ubuntu.arduino_cli.ensure_arduino_cli", return_value="fixture-arduino") as arduino, \
                    patch("direct.ubuntu.opencode_setup.ensure_opencode_cli", return_value="fixture-opencode") as opencode, \
                    patch("src.modules.runtime_resources.enforce_minimum_cpu_requirement", return_value=True), \
                    patch("src.modules.platform_runtime.native_platformio_dir", return_value=root / "native-core"), \
                    patch("src.modules.offline_mode.offline_enabled", return_value=False), \
                    patch("src.modules.offline_mode.finish_bootstrap") as finish, \
                    patch("src.modules.package_jobs.package_store_lease", return_value=nullcontext()), \
                    patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "", "MCU_FLASHER_WORKSPACE_RUNTIME": ""}):
                self.assertEqual(setup.main([]), 0)
            builder.return_value.create.assert_called_once_with(root / ".venv-linux")
            finish.assert_called_once_with(root / "native-core")
            arduino.assert_called_once()
            opencode.assert_called_once()
        commands = [call.args[0] for call in self.run.call_args_list]
        self.assertEqual(len(commands), 5)  # apt update/install, private pip, imports, toolchain preparation.
        self.assertEqual(commands[1][-2:], ["libxcb-cursor0", "libxcb-xinerama0"])
        self.assertEqual(commands[2][1:4], ["-m", "pip", "install"])
        self.assertIn("--no-user", commands[2])
        self.assertEqual(self.run.call_args.kwargs["env"]["PIP_CONFIG_FILE"], os.devnull)
        self.assertIn("runtime_preflight()", commands[3][-1])
        self.assertNotIn("--runtime-only", commands[4])
        self.assertTrue(all("sudo" not in str(command[0]) and "pkexec" not in str(command[0])
                            for command in commands[2:]))
        self.assertIn("Ubuntu runtime ready", self.output.getvalue())

    def test_native_cli_failure_stops_before_board_preparation_or_launch(self):
        audit = ROOT / "temp/audit/ubuntu-system-setup"
        audit.mkdir(parents=True, exist_ok=True)
        for stage in ("native Arduino CLI", "OpenCode AI Assistant"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(dir=audit) as folder:
                self.run.reset_mock()
                root = Path(folder)
                error = io.StringIO()
                with patch.object(preflight, "host_preflight"), \
                        patch.object(setup, "ROOT", root), patch.object(setup, "ENV_DIR", root / ".venv-linux"), \
                        patch.object(setup.venv, "EnvBuilder"), \
                        patch("src.modules.runtime_resources.enforce_minimum_cpu_requirement", return_value=True), \
                        patch("direct.ubuntu.arduino_cli.ensure_arduino_cli", side_effect=RuntimeError("fixture CLI failure") if stage == "native Arduino CLI" else None), \
                        patch("direct.ubuntu.opencode_setup.ensure_opencode_cli", side_effect=RuntimeError("fixture CLI failure") if stage == "OpenCode AI Assistant" else AssertionError("OpenCode prepared after Arduino failure")), \
                        patch("src.modules.package_jobs.package_store_lease", side_effect=AssertionError("Board preparation started")), \
                        patch.object(subprocess, "call", side_effect=AssertionError("GUI launched")), \
                        patch.dict(os.environ, {"MCU_FLASHER_OFFLINE_RUNTIME": "", "MCU_FLASHER_WORKSPACE_RUNTIME": ""}), \
                        redirect_stderr(error):
                    self.assertEqual(setup.main(["--launch"]), 1)
                self.assertEqual(self.run.call_count, 2)  # private pip/import validation only.
                self.assertIn("failed during " + stage, error.getvalue())
                self.assertIn("existing files are preserved", error.getvalue())

    def test_host_constraints_never_authenticate(self):
        with patch.object(preflight, "host_preflight", side_effect=RuntimeError("Unsupported architecture")):
            with self.assertRaisesRegex(RuntimeError, "Unsupported architecture"):
                system_setup.ensure_system_dependencies()
        self.run.assert_not_called()


class MissingPythonChecks(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        if not self.bash and os.name == "nt":
            git = shutil.which("git")
            candidate = Path(git).parent.parent / "bin/bash.exe" if git else None
            if candidate is not None and candidate.is_file():
                self.bash = str(candidate)
        if not self.bash:
            self.skipTest("Native Bash or installed Git Bash is unavailable")
        audit = ROOT / "temp/audit/ubuntu-system-setup"
        audit.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        folder = self.root / "direct/ubuntu"
        folder.mkdir(parents=True)
        self.python = self.root / "fixture-python3"
        self.sudo = self.root / "fixture-sudo"
        self.apt = self.root / "fixture-apt-get"
        self.receipt = self.root / "receipt.txt"
        self.script = folder / "run.sh"
        source = (ROOT / "direct/ubuntu/run.sh").read_text()
        # Only the copied fixture receives replacement executable paths.
        source = source.replace("/usr/bin/python3", shlex.quote(self.shell_path(self.python))).replace("/usr/bin/sudo", shlex.quote(self.shell_path(self.sudo))).replace("/usr/bin/apt-get", shlex.quote(self.shell_path(self.apt)))
        if os.name == "nt":
            # Git Bash checks shell orchestration with fake native executables;
            # it does not prove Ubuntu host admission or native Python support.
            source = source.replace('[[ "$(uname -s)" != "Linux" ]]', "false")
        # Root CI uses an isolated fake desktop uid for this shell-only fixture.
        source = source.replace("(( EUID == 0 ))", "false")
        self.script.write_text(source, encoding="utf-8", newline="\n")
        self.env = os.environ.copy()
        self.env.pop("BASH_ENV", None)
        self.env.pop("ENV", None)
        self.env["MCU_PYTHON_FIXTURE_RECEIPT"] = self.shell_path(self.receipt)

    @staticmethod
    def shell_path(path):
        path = Path(path)
        if os.name == "nt":
            return "/" + path.drive.rstrip(":").lower() + path.as_posix()[2:]
        return str(path)

    def test_missing_python_check_never_starts_installer(self):
        result = subprocess.run([self.bash, self.shell_path(self.script), "--check"], env=self.env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("system Python is missing", result.stderr)
        self.assertFalse(self.receipt.exists())

    def test_missing_python_installs_then_continues_with_literal_arguments(self):
        self.sudo.write_text('#!/bin/bash\nexec "$@"\n', newline="\n")
        self.sudo.chmod(0o755)
        self.apt.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$MCU_PYTHON_FIXTURE_RECEIPT"\n'
                            'if [[ "$*" == *" install "* ]]; then\n'
                            'cp -- "$MCU_PYTHON_FIXTURE_TEMPLATE" "$MCU_PYTHON_FIXTURE_TARGET"\nfi\n', newline="\n")
        self.apt.chmod(0o755)
        template = self.root / "python-template"
        template.write_text('#!/bin/bash\n'
                            'if [[ -n ${PYTHONEXEPATH-}${PIO_PYTHON_EXE-}${PLATFORMIO_PYTHON_EXE-}${PLATFORMIO_PENV_DIR-} ]]; then\n'
                            '  printf "Foreign interpreter hint reached the coordinator\\n" >&2; exit 71\n'
                            'fi\n'
                            'printf "python argument: %s\\n" "$@" >> "$MCU_PYTHON_FIXTURE_RECEIPT"\n', newline="\n")
        template.chmod(0o755)
        self.env.update(MCU_PYTHON_FIXTURE_TEMPLATE=self.shell_path(template), MCU_PYTHON_FIXTURE_TARGET=self.shell_path(self.python))
        self.env.update({name: "C:/copied/python.exe" for name in
                         ("PYTHONEXEPATH", "PIO_PYTHON_EXE", "PLATFORMIO_PYTHON_EXE", "PLATFORMIO_PENV_DIR")})
        project = "Sketch Ω $(touch NEVER) with spaces"
        result = subprocess.run([self.bash, self.shell_path(self.script), "--bootstrap-window", "--project", project],
                                env=self.env, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = self.receipt.read_text(encoding="utf-8")
        self.assertIn("update", receipt)
        self.assertIn("install --yes --no-remove --no-install-recommends python3 python3-venv", receipt)
        self.assertIn("python argument: " + project, receipt)
        self.assertFalse((self.root / "NEVER").exists())

    def test_cancelled_python_setup_never_continues_to_coordinator(self):
        self.sudo.write_text('#!/bin/bash\nprintf "cancelled\\n" >> "$MCU_PYTHON_FIXTURE_RECEIPT"\nexit 1\n', newline="\n")
        self.sudo.chmod(0o755)
        self.apt.write_text('#!/bin/bash\nexit 0\n', newline="\n")
        self.apt.chmod(0o755)
        result = subprocess.run([self.bash, self.shell_path(self.script), "--bootstrap-window"], input="\n",
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("authentication was cancelled", result.stderr)
        self.assertEqual(self.receipt.read_text(), "cancelled\n")
        self.assertFalse(self.python.exists())

    def test_missing_python_desktop_recovery_uses_each_terminal_command_option(self):
        tools = self.root / "fake-terminals"
        tools.mkdir()
        env = dict(self.env, PATH=self.shell_path(tools) + ":/usr/bin:/bin")
        arguments = ["--desktop", "--project", 'Sketch Ω $(touch NEVER) "quoted"']
        for name, separator in (("gnome-terminal", "--"), ("kitty", "--"), ("alacritty", "-e")):
            with self.subTest(terminal=name):
                terminal = tools / name
                terminal.write_text('#!/bin/bash\nprintf \'%s\\0\' "$@" > "$MCU_PYTHON_FIXTURE_RECEIPT"\n',
                                    encoding="utf-8", newline="\n")
                terminal.chmod(0o755)
                # Restrict the copied fixture's lookup to this one fake terminal;
                # the caller must never open an installed desktop terminal.
                source = self.script.read_text(encoding="utf-8")
                fixture_lookup = ('command() {\n'
                                  '  if [[ "$1" == -v && "$2" == "$MCU_PYTHON_FIXTURE_TERMINAL" ]]; then\n'
                                  '    printf "%s\\n" "$2"; return 0\n'
                                  '  fi\n'
                                  '  return 1\n'
                                  '}\n')
                coordinator = self.script.parent / ("recovery-" + name + ".sh")
                coordinator.write_text(source.replace("set -euo pipefail\n", "set -euo pipefail\n" + fixture_lookup, 1),
                                       encoding="utf-8", newline="\n")
                env["MCU_PYTHON_FIXTURE_TERMINAL"] = name
                result = subprocess.run([self.bash, self.shell_path(coordinator), *arguments], env=env,
                                        capture_output=True, text=True, encoding="utf-8", timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                recorded = self.receipt.read_text(encoding="utf-8").split("\0")[:-1]
                self.assertEqual(recorded, [separator, "/bin/bash", self.shell_path(self.script),
                                            "--bootstrap-window", *arguments])
                self.assertFalse((self.root / "NEVER").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
