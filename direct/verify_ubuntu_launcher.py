#!/usr/bin/env python3
"""Hardware-free Ubuntu launcher checks; setup, GUI and user registration are mocked."""
from __future__ import annotations

import json
import ast
import ctypes
import ctypes.util
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import quote
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from direct.ubuntu import launch, install_desktop, preflight
from src.modules import ubuntu_architecture


class FirstLaunchPackagingChecks(unittest.TestCase):
    def test_shipped_launchers_are_distinct_native_64_bit_executables(self):
        for name, machine, other_machine in (("MCU_Flasher", "x86_64", "aarch64"),
                                             ("MCU_Flasher-arm64", "aarch64", "x86_64")):
            with self.subTest(launcher=name):
                launcher = ROOT / name
                self.assertTrue(ubuntu_architecture.elf_matches_host(launcher, machine))
                self.assertFalse(ubuntu_architecture.elf_matches_host(launcher, other_machine))
                header = launcher.read_bytes()[:20]
                self.assertIn(int.from_bytes(header[16:18], "little"), (2, 3))
                if os.name != "nt":
                    self.assertTrue(os.access(launcher, os.X_OK))

    def test_clean_checkout_copy_does_not_require_or_inherit_generated_guidance(self):
        tree = ast.parse((ROOT / "direct/verify_ubuntu_first_run.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_copy_source")
        scratch = ROOT / "temp/audit/ubuntu-launcher"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            source = Path(temporary) / "source"
            for relative in ("main", "direct", "src/modules", "src/editor", "src/assets", "src/fonts", "src/dbs"):
                (source / relative).mkdir(parents=True, exist_ok=True)
            for name in ("MCU_Flasher", "mcu_flash_gui.py", "README.md"):
                (source / name).write_text("fixture", encoding="utf-8")
            for name in ("verify_runtime.py", "verify_target_resolution.py"):
                (source / "direct" / name).write_text('sys.path.insert(0, str(ROOT))\n', encoding="utf-8")
            native_sources = ("direct/ubuntu/architecture_guard.sh", "direct/ubuntu/arm_qt5.py",
                              "direct/ubuntu/runtime_architecture.py", "src/modules/ubuntu_architecture.py")
            for relative in native_sources:
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("native ARM64 support fixture\n", encoding="utf-8")
            (source / "src/dbs/bootstrap_config.json").write_text('{"live settings":true}\n', encoding="utf-8")
            (source / "main/mcu_flash_gui.py").write_text(
                'from main.web_bridge import MCUWebBackendAPI\n'
                'def launch():\n        window.show()\n', encoding="utf-8")
            scope = {"ROOT": source, "Path": Path, "shutil": shutil,
                     "HOME_ISOLATION": "# isolated home\n", "GUI_SERIAL_ISOLATION": "# isolated serial\n",
                     "GUI_SMOKE": "        # isolated smoke\n"}
            exec(compile(ast.Module(body=[function], type_ignores=[]), "<first-launch-copy>", "exec"), scope)
            for has_guidance, has_arm64 in ((False, False), (True, True)):
                with self.subTest(local_guidance_present=has_guidance, arm64_present=has_arm64):
                    if has_guidance:
                        (source / "AGENTS.md").write_text("Local hardware/settings fixture", encoding="utf-8")
                    if has_arm64:
                        (source / "MCU_Flasher-arm64").write_bytes(b"ARM64 executable fixture")
                    destination = Path(temporary) / ("with-guidance" if has_guidance else "clean-checkout")
                    scope["_copy_source"](destination)
                    self.assertTrue((destination / "MCU_Flasher").is_file())
                    self.assertEqual((destination / "MCU_Flasher-arm64").exists(), has_arm64)
                    if has_arm64:
                        self.assertEqual((destination / "MCU_Flasher-arm64").read_bytes(),
                                         (source / "MCU_Flasher-arm64").read_bytes())
                    self.assertFalse((destination / "AGENTS.md").exists())
                    self.assertFalse((destination / "src/dbs/bootstrap_config.json").exists())
                    for relative in native_sources:
                        self.assertEqual((destination / relative).read_bytes(), (source / relative).read_bytes())
                    self.assertIn("isolated home", (destination / "src/__init__.py").read_text())


class LaunchEnvironmentChecks(unittest.TestCase):
    def test_foreign_interpreter_hints_cannot_override_platformio_children(self):
        from platformio.proc import get_pythonexe_path

        values = {"PYTHONHOME": "C:/Python", "PYTHONPATH": "foreign", "QT_PLUGIN_PATH": "Qt5",
                  "PYTHONEXEPATH": "C:/copied/python.exe", "PIO_PYTHON_EXE": "C:/copied/python.exe",
                  "PLATFORMIO_PYTHON_EXE": "C:/copied/python.exe", "PLATFORMIO_PENV_DIR": "C:/copied/penv",
                  "MCU_FLASHER_WORKSPACE_RUNTIME": "1", "PIP_NO_INDEX": "1", "PATH": "custom-cli-path"}
        with patch.dict(os.environ, values):
            # Exercise PlatformIO's actual interpreter selector, which otherwise
            # trusts PYTHONEXEPATH even when invoked by the native private Python.
            self.assertEqual(get_pythonexe_path(), values["PYTHONEXEPATH"])
            env = launch.clean_environment()
        self.assertEqual(env["PATH"], "custom-cli-path")
        for key in values.keys() - {"PATH"}:
            self.assertNotIn(key, env)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(get_pythonexe_path(), os.path.normpath(sys.executable))
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")


@unittest.skipUnless(sys.platform.startswith("linux"), "Native Bash architecture fixtures")
class ArchitectureEntryChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        audit = ROOT / "temp/audit/ubuntu-launcher"
        audit.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=audit)))
        self.folder = self.root / "direct/ubuntu"
        self.folder.mkdir(parents=True)
        for name in ("run.sh", "build_launcher.sh", "architecture_guard.sh"):
            shutil.copy2(ROOT / "direct/ubuntu" / name, self.folder / name)
        (self.folder / "release_guard.sh").write_text(
            "require_supported_ubuntu() { return 0; }\n", encoding="utf-8")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}

    def architecture(self, machine: str, bits: str = "64"):
        (self.bin / "uname").write_text(
            '#!/bin/bash\nif [[ "$1" == -s ]]; then printf Linux; else printf "%s" '
            + machine + '; fi\n', encoding="utf-8")
        (self.bin / "getconf").write_text('#!/bin/bash\nprintf "%s" ' + bits + '\n', encoding="utf-8")
        for name in ("uname", "getconf"):
            (self.bin / name).chmod(0o755)

    def run_script(self, name: str, *arguments: str):
        return subprocess.run(["/bin/bash", str(self.folder / name), *arguments],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def test_amd64_arm64_and_aliases_reach_same_python_dispatch_with_literal_arguments(self):
        recorder = self.root / "python"
        recorder.write_text('#!/bin/bash\nexec /usr/bin/python3 -c '
                            "'import json,sys; print(json.dumps(sys.argv[1:]))' \"$@\"\n", encoding="utf-8")
        recorder.chmod(0o755)
        runner = self.folder / "run.sh"
        runner.write_text(runner.read_text().replace("/usr/bin/python3", shlex.quote(str(recorder))), encoding="utf-8")
        arguments = ["--project", 'Sketch Ω $(touch NEVER) "quoted"', "--check"]
        for machine in ("x86_64", "amd64", "aarch64", "arm64"):
            with self.subTest(machine=machine):
                self.architecture(machine)
                result = self.run_script("run.sh", *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), ["-B", str(self.folder / "launch.py"), *arguments])
        self.assertFalse((self.root / "NEVER").exists())

    def test_unsupported_or_32_bit_userspace_never_reaches_python_or_compiler(self):
        for machine, bits in (("armv7l", "32"), ("aarch64", "32"), ("riscv64", "64"), ("x86_64", "unknown")):
            with self.subTest(machine=machine, bits=bits):
                self.architecture(machine, bits)
                for name in ("run.sh", "build_launcher.sh"):
                    result = self.run_script(name, "--check")
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("64-bit Ubuntu", result.stderr)
                self.assertFalse((self.root / "--check").exists())

    def test_native_build_selects_host_filename_and_preserves_windows_entries(self):
        protected = {"MCU_Flasher.exe": b"MZ Windows launcher\n",
                     "direct/windows/run.vbs": b"Windows launcher fixture\r\n"}
        for name, contents in protected.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        compiler = self.bin / "cc"
        compiler.write_text('#!/bin/bash\nwhile (( $# > 0 )); do\n'
                            '  if [[ "$1" == -o ]]; then shift; printf native-fixture > "$1"; exit 0; fi\n'
                            '  shift\ndone\nexit 9\n', encoding="utf-8")
        compiler.chmod(0o755)
        self.env["CC"] = str(compiler)
        for machine, architecture in (("x86_64", "amd64"), ("aarch64", "arm64")):
            with self.subTest(machine=machine):
                self.architecture(machine)
                result = self.run_script("build_launcher.sh")
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = "MCU_Flasher" if architecture == "amd64" else "MCU_Flasher-arm64"
                self.assertEqual((self.root / expected).read_bytes(), b"native-fixture")
                for name, contents in protected.items():
                    self.assertEqual((self.root / name).read_bytes(), contents)
                # An ARM user may intentionally rebuild the root name locally.
                explicit = self.run_script("build_launcher.sh", str(self.root / "MCU_Flasher"))
                self.assertEqual(explicit.returncode, 0, explicit.stderr)
                self.assertEqual((self.root / "MCU_Flasher").read_bytes(), b"native-fixture")
                result = self.run_script("build_launcher.sh", str(self.root / "MCU_Flasher.exe"))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual((self.root / "MCU_Flasher.exe").read_bytes(), protected["MCU_Flasher.exe"])


def desktop_command(text: str, *, action: bool = False) -> list[str]:
    """Decode string/Exec quoting and literal percent escapes, leaving %k alone."""
    values = [line.removeprefix("Exec=") for line in text.splitlines() if line.startswith("Exec=")]
    value = values[1 if action else 0]
    escapes = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    value = re.sub(r"\\([sntr\\])", lambda match: escapes[match.group(1)], value)
    arguments = []
    current = []
    quoted = False
    index = 0
    while index < len(value):
        char = value[index]
        if char == '"':
            quoted = not quoted
        elif quoted and char == "\\" and index + 1 < len(value) and value[index + 1] in '\\"`$':
            index += 1
            current.append(value[index])
        elif not quoted and char.isspace():
            if current:
                arguments.append("".join(current))
                current = []
        else:
            current.append(char)
        index += 1
    if quoted:
        raise ValueError("Unclosed desktop Exec argument")
    if current:
        arguments.append("".join(current))
    return [argument.replace("%%", "%") for argument in arguments]


class ShortcutChecks(unittest.TestCase):
    """Portable shortcut source/argument checks also run on the Windows host."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        audit = ROOT / "temp/audit/ubuntu-launcher"
        audit.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=audit)))
        self.bash = shutil.which("bash")
        if not self.bash and os.name == "nt":
            git = shutil.which("git")
            candidate = Path(git).parent.parent / "bin/bash.exe" if git else None
            if candidate is not None and candidate.is_file():
                self.bash = str(candidate)

    @staticmethod
    def shell_path(path):
        path = Path(path)
        if os.name == "nt":
            return "/" + path.drive.rstrip(":").lower() + path.as_posix()[2:]
        return str(path)

    def execute_portable(self, location=None, *, arguments=()):
        if not self.bash:
            self.skipTest("Native Bash or installed Git Bash is unavailable")
        command = desktop_command(install_desktop.desktop_entry(Path("/old/location"), portable=True))
        self.assertEqual(command[:2], ["/bin/bash", "-c"])
        self.assertEqual(command[2], install_desktop.PORTABLE_LAUNCH_CODE)
        self.assertEqual(command[3:], ["mcu-flasher", "%k"])
        env = os.environ.copy()
        env.pop("BASH_ENV", None)
        env.pop("ENV", None)
        # The dispatcher must reach the existing shell Bootstrap rescue without
        # finding Python (or any other command) through the account's PATH.
        env.update(PATH="", PYTHONHOME="missing-foreign-python", PYTHONPATH="missing-foreign-packages")
        return subprocess.run([self.bash, *command[1:4], *([location] if location is not None else []), *arguments],
                              cwd=self.root, env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=10, check=False)

    def launched_arguments(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split("\0")[:-1]

    def fixture(self, name="Moved Ubuntu Ω %folder $cash `literal` & (safe)"):
        folder = self.root / name
        (folder / "direct/ubuntu").mkdir(parents=True)
        (folder / "direct/ubuntu/run.sh").write_text('printf \'%s\\0\' "$0" "$@"\n', encoding="utf-8")
        return folder

    def test_portable_shortcut_resolves_moved_folder_with_literal_path_characters(self):
        folder = self.fixture()
        child = self.execute_portable(self.shell_path(folder / "MCU Flasher.desktop"))
        self.assertEqual(self.launched_arguments(child), [self.shell_path(folder / "direct/ubuntu/run.sh"), "--desktop"])
        text = install_desktop.desktop_entry(Path("/old/location"), portable=True)
        self.assertNotIn("/old/location", text)
        self.assertNotIn("Path=", text)

    def test_dispatcher_reaches_shell_bootstrap_without_system_python(self):
        folder = self.fixture()
        # Both URI and filename dispatch use only Bash builtins before run.sh.
        self.assertNotIn("python", install_desktop.PORTABLE_LAUNCH_CODE.lower())
        uri = "file://" + quote(self.shell_path(folder / "MCU Flasher.desktop"))
        result = self.execute_portable(uri, arguments=["--repair"])
        self.assertEqual(self.launched_arguments(result), [self.shell_path(folder / "direct/ubuntu/run.sh"), "--desktop", "--repair"])

    def test_portable_shortcut_accepts_local_uri_and_decodes_percent_only_once(self):
        folder = self.fixture("Ubuntu Ω spaces %20 %k $cash `literal`")
        uri = "file://" + quote(self.shell_path(folder / "MCU Flasher.desktop"))
        child = self.execute_portable(uri)
        self.assertEqual(self.launched_arguments(child)[0], self.shell_path(folder / "direct/ubuntu/run.sh"))

    @unittest.skipIf(os.name == "nt", "Quoted filenames require native Linux")
    def test_portable_shortcut_preserves_quotes_and_shell_syntax_in_linux_filename(self):
        folder = self.fixture('Moved Ubuntu Ω %k $cash `literal` "quoted" $(touch NEVER)')
        child = self.execute_portable(self.shell_path(folder / "MCU Flasher.desktop"))
        self.assertEqual(self.launched_arguments(child), [self.shell_path(folder / "direct/ubuntu/run.sh"), "--desktop"])
        self.assertFalse((self.root / "NEVER").exists())

    def test_portable_shortcut_accepts_explicit_localhost_uri(self):
        folder = self.fixture()
        uri = "file://localhost" + quote(self.shell_path(folder / "MCU Flasher.desktop"))
        child = self.execute_portable(uri)
        self.assertEqual(self.launched_arguments(child)[0], self.shell_path(folder / "direct/ubuntu/run.sh"))

    def test_portable_shortcut_forwards_repair_and_literal_project_arguments(self):
        folder = self.fixture()
        args = ["--repair", "--project", 'Sketch Ω $(touch NEVER) "quotes"', "--new-window"]
        child = self.execute_portable(self.shell_path(folder / "MCU Flasher.desktop"), arguments=args)
        self.assertEqual(self.launched_arguments(child), [self.shell_path(folder / "direct/ubuntu/run.sh"), "--desktop", *args])
        repair = desktop_command(install_desktop.desktop_entry(self.root, portable=True), action=True)
        self.assertEqual(repair[3:], ["mcu-flasher", "%k", "--repair"])
        self.assertFalse((self.root / "NEVER").exists())

    def test_unknown_missing_relative_or_remote_shortcut_location_never_executes(self):
        for location in (None, "", "--repair", "shortcut.desktop", "file://remote-host/home/user/MCU.desktop",
                         "file://[malformed/MCU.desktop", "file:///tmp/MCU.desktop?query", "file:///tmp/MCU.desktop#fragment",
                         "file:///tmp/%00/MCU.desktop", "file:///tmp/%GG/MCU.desktop", "file:///tmp/%/MCU.desktop"):
            with self.subTest(location=location):
                result = self.execute_portable(location)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("cannot locate its application folder", result.stderr)

    def test_missing_application_script_never_executes(self):
        result = self.execute_portable(self.shell_path(self.root / "MCU Flasher.desktop"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Keep this shortcut beside", result.stderr)

    def test_installer_creates_distinct_portable_folder_and_fixed_menu_shortcuts(self):
        (self.root / "direct/ubuntu").mkdir(parents=True)
        (self.root / "direct/ubuntu/run.sh").write_text("fixture", encoding="utf-8")
        data_home = self.root / "local-data"
        with patch.object(install_desktop, "ROOT", self.root), \
                patch.dict(os.environ, {"XDG_DATA_HOME": str(data_home)}):
            self.assertEqual(install_desktop.main(["--install"]), 0)
        local = (self.root / "MCU Flasher.desktop").read_text(encoding="utf-8")
        registered = (data_home / "applications/mcu-flasher.desktop").read_text(encoding="utf-8")
        self.assertEqual(desktop_command(local)[3:], ["mcu-flasher", "%k"])
        self.assertEqual(desktop_command(registered), ["/bin/bash", str(self.root / "direct/ubuntu/run.sh"), "--desktop"])
        self.assertNotIn("%k", registered)
        self.assertNotIn("MCU_Flasher\"", registered)


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

    def launcher_arguments(self, target: Path):
        recorder = target.parent / "direct/ubuntu/run.sh"
        recorder.parent.mkdir(parents=True, exist_ok=True)
        recorder.write_text('#!/bin/bash\nexec /usr/bin/python3 -c '
                            "'import json,sys; print(json.dumps(sys.argv[1:]))' \"$@\"\n", encoding="utf-8")
        arguments = ["--project", 'Sketch Ω with spaces $(touch NEVER) "quotes"', "--new-window"]
        result = subprocess.run([str(target), *arguments], cwd=self.root,
                                capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(result.stdout), ["--desktop", *arguments])
        self.assertFalse((self.root / "NEVER").exists())

    def test_shipped_host_launcher_preserves_location_and_literal_arguments_after_relocation(self):
        folder = self.root / 'Moved Ubuntu Ω folder $cash `literal`'
        (folder / "direct/ubuntu").mkdir(parents=True)
        name = "MCU_Flasher-arm64" if ubuntu_architecture.host_architecture() == "arm64" else "MCU_Flasher"
        target = folder / name
        shutil.copy2(ROOT / name, target)
        self.assertTrue(ubuntu_architecture.elf_matches_host(target))
        self.launcher_arguments(target)
        moved = self.root / 'Relocated Ubuntu Ω folder %literal "quoted"'
        folder.rename(moved)
        target = moved / name
        self.launcher_arguments(target)
        link = self.root / "linked-launcher"
        link.symlink_to(target)
        self.launcher_arguments(link)

    def test_shipped_host_launcher_is_an_executable_to_ubuntu_files(self):
        gio = shutil.which("gio")
        if not gio:
            self.skipTest("Ubuntu GLib file information CLI unavailable")
        name = "MCU_Flasher-arm64" if ubuntu_architecture.host_architecture() == "arm64" else "MCU_Flasher"
        target = self.root / name
        shutil.copy2(ROOT / name, target)
        result = subprocess.run([gio, "info", "-a", "standard::content-type,access::can-execute", str(target)],
                                capture_output=True, text=True, timeout=10, check=True,
                                env={**os.environ, "LC_ALL": "C"})
        self.assertIn("standard::content-type: application/x-executable", result.stdout)
        self.assertIn("access::can-execute: TRUE", result.stdout)

    def test_rebuilt_native_wrapper_preserves_location_and_literal_arguments(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.skipTest("Native C compiler unavailable")
        folder = self.root / 'Native Ubuntu Ω folder $cash `literal`'
        folder.mkdir()
        target = folder / "MCU_Flasher-native"
        subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                        str(ROOT / "src/launcher_ubuntu.c"), "-o", str(target)],
                       capture_output=True, text=True, timeout=30, check=True)
        self.launcher_arguments(target)

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
                SimpleNamespace(returncode=0, stderr=""), SimpleNamespace(returncode=1, stderr="")]) as run, \
                patch.object(ubuntu_architecture, "elf_matches_host", return_value=True):
            self.assertIn("native board packages", launch.runtime_problem({}))
        self.assertIn("bootstrap_ready", run.call_args.args[0][-1])

    def test_foreign_architecture_runtime_is_rejected_before_executing_it(self):
        python = self.root / ".venv-linux/bin/python"
        python.parent.mkdir(parents=True)
        for machine, foreign in (("x86_64", 183), ("aarch64", 62)):
            with self.subTest(machine=machine):
                header = bytearray(20)
                header[:6] = b"\x7fELF\x02\x01"
                header[18:20] = foreign.to_bytes(2, "little")
                python.write_bytes(header)
                python.chmod(0o755)
                with patch.object(ubuntu_architecture.platform, "machine", return_value=machine), \
                        patch.object(launch.subprocess, "run", side_effect=AssertionError("Foreign Python executed")):
                    self.assertIn("does not match this host", launch.runtime_problem({}))

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

    def test_ubuntu_26_prefers_standalone_ptyxis_and_preserves_literal_arguments(self):
        arguments = ["--repair", "--project", 'Sketch Ω $(touch NEVER) "quoted"', "--new-window"]
        env = {"PATH": "/fixture/bin"}
        with patch("src.modules.ubuntu_release.require_supported_ubuntu", return_value=(26, 4)), \
                patch.object(launch.shutil, "which", side_effect=lambda name: "/fixture/bin/" + name) as lookup, \
                patch.object(launch.subprocess, "Popen") as child:
            self.assertTrue(launch.bootstrap_terminal(arguments, env))
        lookup.assert_called_once_with("ptyxis")
        child.assert_called_once_with(["/fixture/bin/ptyxis", "--standalone", "--", "/bin/bash",
                                      str(self.root / "direct/ubuntu/run.sh"), "--bootstrap-window", *arguments],
                                     cwd=self.root, env=env, start_new_session=True)
        self.assertFalse((self.root / "NEVER").exists())

    def test_ubuntu_24_retains_existing_terminal_preference(self):
        with patch("src.modules.ubuntu_release.require_supported_ubuntu", return_value=(24, 4)), \
                patch.object(launch.shutil, "which", side_effect=lambda name: "/fixture/bin/" + name) as lookup, \
                patch.object(launch.subprocess, "Popen") as child:
            self.assertTrue(launch.bootstrap_terminal([], {}))
        lookup.assert_called_once_with("x-terminal-emulator")
        self.assertEqual(child.call_args.args[0][:2], ["/fixture/bin/x-terminal-emulator", "-e"])

    def test_ubuntu_26_missing_or_unlaunchable_ptyxis_falls_back(self):
        for installed in (False, True):
            with self.subTest(installed=installed), \
                    patch("src.modules.ubuntu_release.require_supported_ubuntu", return_value=(26, 4)), \
                    patch.object(launch.shutil, "which", side_effect=lambda name: "/fixture/bin/" + name
                                 if name == "x-terminal-emulator" or (installed and name == "ptyxis") else None), \
                    patch.object(launch.subprocess, "Popen", side_effect=[OSError("Cannot start Ptyxis"), object()]
                                 if installed else [object()]) as child:
                self.assertTrue(launch.bootstrap_terminal(["--repair"], {}))
            self.assertEqual(child.call_args.args[0][:2], ["/fixture/bin/x-terminal-emulator", "-e"])
            self.assertEqual(child.call_count, 2 if installed else 1)

    def test_no_terminal_available_reports_failure_without_spawning(self):
        with patch("src.modules.ubuntu_release.require_supported_ubuntu", return_value=(26, 4)), \
                patch.object(launch.shutil, "which", return_value=None), \
                patch.object(launch.subprocess, "Popen") as child:
            self.assertFalse(launch.bootstrap_terminal([], {}))
        child.assert_not_called()

    def test_native_shortcut_and_registration_use_fixture_locations(self):
        (self.root / "direct/ubuntu").mkdir(parents=True)
        (self.root / "direct/ubuntu/run.sh").write_text("fixture", encoding="utf-8")
        data_home = self.root / "local-data"
        with patch.object(install_desktop, "ROOT", self.root), \
                patch.dict(os.environ, {"XDG_DATA_HOME": str(data_home)}):
            self.assertEqual(install_desktop.main(["--install"]), 0)
        text = (self.root / "MCU Flasher.desktop").read_text(encoding="utf-8")
        registered = (data_home / "applications/mcu-flasher.desktop").read_text(encoding="utf-8")
        self.assertNotEqual(text, registered)
        self.assertIn(" %k", text)
        self.assertIn(str(self.root / "direct/ubuntu/run.sh"), registered)
        self.assertIn("Terminal=false", text)
        self.assertIn(" --repair", text)
        self.assertTrue(os.access(self.root / "MCU Flasher.desktop", os.X_OK))

    def test_desktop_parser_launches_moved_folder_without_script_execute_permission(self):
        library = ctypes.util.find_library("gio-2.0")
        if not library:
            self.skipTest("GLib desktop-entry API unavailable")
        gio = ctypes.CDLL(library)
        gio.g_desktop_app_info_new_from_filename.argtypes = [ctypes.c_char_p]
        gio.g_desktop_app_info_new_from_filename.restype = ctypes.c_void_p
        gio.g_app_info_launch.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_void_p)]
        gio.g_app_info_launch.restype = ctypes.c_int
        gio.g_object_unref.argtypes = [ctypes.c_void_p]
        gio.g_object_unref.restype = None
        gio.g_error_free.argtypes = [ctypes.c_void_p]
        gio.g_error_free.restype = None

        class GError(ctypes.Structure):
            _fields_ = [("domain", ctypes.c_uint), ("code", ctypes.c_int), ("message", ctypes.c_char_p)]

        folder = self.root / 'Ubuntu Ω %folder $cash `literal` "quoted"'
        folder.mkdir()
        (folder / "direct/ubuntu").mkdir(parents=True)
        recorder = folder / "direct/ubuntu/run.sh"
        # Check desktop path dispatch independently of the runner's separately
        # installed Python and inherited setup-python environment.
        recorder.write_text('#!/bin/bash\nprintf launched > "$MCU_LAUNCHER_FIXTURE_RECEIPT"\n',
                            encoding="utf-8")
        recorder.chmod(0o644)
        desktop = folder / "fixture.desktop"
        desktop.write_text(install_desktop.desktop_entry(folder, portable=True), encoding="utf-8")
        moved = self.root / 'Relocated Ω %folder $cash `literal` "quoted"'
        folder.rename(moved)
        desktop = moved / "fixture.desktop"
        receipt = self.root / "receipt.txt"
        # GLib's gio CLI through 2.80 creates this object from a keyfile without
        # its filename property, silently dropping %k. Use the desktop API,
        # which preserves the actual filename on Ubuntu 22/24/26 alike.
        app_info = gio.g_desktop_app_info_new_from_filename(os.fsencode(desktop))
        self.assertTrue(app_info, "GLib could not parse the generated desktop entry")
        error = ctypes.c_void_p()
        try:
            with patch.dict(os.environ, {"MCU_LAUNCHER_FIXTURE_RECEIPT": str(receipt)}):
                launched = gio.g_app_info_launch(app_info, None, None, ctypes.byref(error))
            message = ctypes.cast(error, ctypes.POINTER(GError)).contents.message.decode(errors="replace") if error.value else ""
            self.assertTrue(launched, message)
        finally:
            if error.value:
                gio.g_error_free(error)
            gio.g_object_unref(app_info)
        deadline = time.monotonic() + 3
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(receipt.is_file(), "The native desktop API launched its child without a receipt")
        self.assertEqual(receipt.read_text(), "launched")

if __name__ == "__main__":
    unittest.main(verbosity=2)
