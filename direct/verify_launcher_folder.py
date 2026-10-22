#!/usr/bin/env python3
"""Verify portable LAUNCHER entries with owned, fake dispatchers only.

Never runs application startup, setup, metadata generation or hardware services.
Native Linux checks execute the actual matching ELF entry. Native Windows
checks execute the actual PE entry, WScript and private Python argument parsing.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = ROOT / "LAUNCHER"
EXPECTED = {"WINDOWS.exe", "UBUNTU", "UBUNTU_ARM64", "MacOS.command",
            "FEDORA", "FEDORA_ARM64", "LINUX_MINT", "README.md"}


def windows_probe():
    """Reuse the existing owned-process tracker without importing app services."""
    spec = importlib.util.spec_from_file_location("launcher_process_fixture", ROOT / "direct/verify_windows_launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def runtime_preflight_fixture(source: str) -> str:
    """Extract only read-only validation; replace process launch with an observer.

    WSH still performs its real environment expansion. No Python, repair,
    installer or venv update can run through this fixture.
    """
    initialization = re.search(r'^setupEnvironment\("MCU_FLASHER_LITERAL_PERCENT"\) = "%"$',
                               source, re.MULTILINE)
    validation = re.search(r'^Function IsPythonExeValid\(exePath\)\n.*?^End Function$',
                           source, re.MULTILINE | re.DOTALL)
    if initialization is None or validation is None:
        raise AssertionError("Missing production runtime preflight")
    if initialization.start() > source.index("If (Not fso.FileExists(portablePython))"):
        raise AssertionError("Literal percent must be initialized before runtime validation")
    preflight = validation.group().split("    Dim venvDir,", 1)[0]
    if 'runCmd = Replace(runCmd, "%", "%MCU_FLASHER_LITERAL_PERCENT%")' not in preflight:
        raise AssertionError("Runtime validation must preserve literal percent paths")
    return r'''Option Explicit
Dim fso, shell, realShell, setupEnvironment, expectedExe, receipt
Set fso = CreateObject("Scripting.FileSystemObject")
Set realShell = CreateObject("WScript.Shell")
Set setupEnvironment = realShell.Environment("PROCESS")
''' + initialization.group() + r'''
expectedExe = WScript.Arguments(0)
receipt = WScript.Arguments(1)
Set shell = New ObservedShell
If Not IsPythonExeValid(expectedExe) Then WScript.Quit 72
WScript.Quit 0

Class ObservedShell
    Public Function Run(command, windowStyle, waitForExit)
        Dim expanded, prefix, output
        expanded = realShell.ExpandEnvironmentStrings(command)
        prefix = Chr(34) & expectedExe & Chr(34) & " -I -B -c "
        Set output = fso.CreateTextFile(receipt, True, True)
        output.Write expanded
        output.Close
        If Left(expanded, Len(prefix)) = prefix Then
            Run = 0
        Else
            Run = 71
        End If
    End Function
End Class

''' + preflight + '    IsPythonExeValid = True\nEnd Function\n'


class ArtifactChecks(unittest.TestCase):
    def test_git_preserves_executable_launcher_entries_for_archive_export(self):
        # core.filemode=false can silently stage new 0755 files as 0644.
        # A working-tree mode alone cannot prove portable release permissions.
        result = subprocess.run(["git", "ls-files", "--stage", "-z", "--", "LAUNCHER"],
                                cwd=ROOT, capture_output=True, text=True, timeout=10, check=True)
        modes = {entry.split("\t", 1)[1]: entry.split(" ", 1)[0]
                 for entry in result.stdout.split("\0") if entry}
        for name in EXPECTED - {"WINDOWS.exe", "README.md"}:
            self.assertEqual(modes.get("LAUNCHER/" + name), "100755", name)

    def test_named_entries_and_executable_modes(self):
        self.assertEqual({path.name for path in LAUNCHERS.iterdir()}, EXPECTED)
        for name in EXPECTED:
            self.assertFalse((LAUNCHERS / name).is_symlink(), name)
        if os.name != "nt":
            for name in EXPECTED - {"WINDOWS.exe", "README.md"}:
                self.assertEqual((LAUNCHERS / name).stat().st_mode & 0o777, 0o755, name)

    def test_linux_payloads_are_native_elf_for_the_named_architecture(self):
        for name, machine in (("UBUNTU", 62), ("UBUNTU_ARM64", 183),
                              ("FEDORA", 62), ("FEDORA_ARM64", 183), ("LINUX_MINT", 62)):
            with self.subTest(entry=name):
                data = (LAUNCHERS / name).read_bytes()
                self.assertGreaterEqual(len(data), 64)
                self.assertEqual(data[:7], b"\x7fELF\x02\x01\x01")
                self.assertIn(int.from_bytes(data[16:18], "little"), (2, 3))
                self.assertEqual(int.from_bytes(data[18:20], "little"), machine)

    def test_windows_payload_is_a_gui_pe_executable(self):
        data = (LAUNCHERS / "WINDOWS.exe").read_bytes()
        self.assertEqual(data[:2], b"MZ")
        self.assertGreaterEqual(len(data), 64)
        offset = int.from_bytes(data[60:64], "little")
        self.assertGreaterEqual(offset, 64)
        self.assertGreaterEqual(len(data), offset + 94)
        self.assertEqual(data[offset:offset + 4], b"PE\0\0")
        self.assertEqual(int.from_bytes(data[offset + 4:offset + 6], "little"), 0x14c)
        self.assertEqual(int.from_bytes(data[offset + 92:offset + 94], "little"), 2)

    def test_existing_windows_assembly_fixture_extracts_quoting_without_bootstrap(self):
        module = windows_probe()
        source = (ROOT / "direct/windows/run.vbs").read_text(encoding="ascii")
        fixture = module.python_assembly_fixture(source)
        self.assertEqual(fixture.count("Function QuoteWindowsArgument(value)"), 1)
        self.assertIn('runCmd = Replace(runCmd, "%", "%MCU_FLASHER_LITERAL_PERCENT%")', fixture)
        for forbidden in ("AutoHealPrivatePython", "RepairVenvInPlace", "DeleteFile", "NUMBER_OF_PROCESSORS"):
            self.assertNotIn(forbidden, fixture)

    def test_runtime_preflight_extracts_literal_shielding_without_repair_or_execution(self):
        source = (ROOT / "direct/windows/run.vbs").read_text(encoding="ascii")
        fixture = runtime_preflight_fixture(source)
        self.assertIn("ExpandEnvironmentStrings(command)", fixture)
        for forbidden in ("AutoHealPrivatePython", "RepairVenvInPlace", "DeleteFolder", "OpenTextFile",
                          "realShell.Run(", "NUMBER_OF_PROCESSORS"):
            self.assertNotIn(forbidden, fixture)
        shield = '    runCmd = Replace(runCmd, "%", "%MCU_FLASHER_LITERAL_PERCENT%")\n'
        with self.assertRaisesRegex(AssertionError, "Runtime validation"):
            runtime_preflight_fixture(source.replace(shield, "", 1))


class OwnedFixture(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp/audit/launcher-folder"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.sandbox = Path(self.temporary.name)
        self.project = self.sandbox / "Original Ω %PATH% $cash `literal` & (safe)"
        shutil.copytree(LAUNCHERS, self.project / "LAUNCHER")
        (self.project / "direct/ubuntu").mkdir(parents=True)
        self.recorder = self.project / "direct/ubuntu/run.sh"
        self.recorder.write_text('printf \'%s\\0\' "$0" "$@"\n', encoding="utf-8")
        self.recorder.chmod(0o644)
        self.env = os.environ.copy()
        self.env.pop("BASH_ENV", None)
        self.env.pop("ENV", None)
        self.env["PATH"] = ""
        for name in ("DISPLAY", "WAYLAND_DISPLAY"):
            self.env.pop(name, None)


class ProcessCleanupChecks(OwnedFixture):
    def test_failed_owned_fixture_retains_bounded_stdout_and_stderr(self):
        fixture = self.sandbox / "failing-output.py"
        fixture.write_text(
            "import sys\n"
            "sys.stdout.write('fixture stdout: ' + 'x' * 20000)\n"
            "sys.stdout.flush()\n"
            "sys.stderr.write('fixture stderr: ' + 'y' * 20000)\n"
            "sys.stderr.flush()\n"
            "sys.exit(7)\n", encoding="utf-8")
        result = windows_probe().capture(
            Path(sys.executable), fixture, [], self.sandbox / "absent-output-result.json", self.sandbox,
            command_prefix=[sys.executable, "-I", "-B", str(fixture)], timeout=5)
        self.assertEqual(result["launcherExitCode"], 7, result)
        self.assertIn("code 7", result["error"])
        self.assertEqual(result["cleanupFailures"], [])
        for stream in ("stdout", "stderr"):
            diagnostic = result["diagnostics"][stream]
            self.assertTrue(diagnostic.startswith("fixture " + stream + ": "), result)
            self.assertLessEqual(len(diagnostic), 4096)
            self.assertTrue(result["diagnostics"][stream + "Truncated"])

    def test_timeout_stops_owned_parent_and_descendant_before_fixture_cleanup(self):
        import psutil

        marker = self.sandbox / "sleeping-child.json"
        parent = self.sandbox / "sleeping-parent.py"
        parent.write_text(
            "import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-I', '-B', '-c', "
            "'import time; time.sleep(60)', sys.argv[1]], stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "Path(sys.argv[1]).write_text(json.dumps({'parent':os.getpid(),'child':child.pid}))\n"
            "time.sleep(60)\n", encoding="utf-8")
        result = windows_probe().capture(
            Path(sys.executable), parent, [], self.sandbox / "absent-result.json", self.sandbox,
            command_prefix=[sys.executable, "-I", "-B", str(parent), str(marker)], timeout=3)
        self.assertIn("Timed out", result["error"])
        self.assertEqual(result["cleanupFailures"], [])
        self.assertFalse(result["passed"])
        self.assertTrue(marker.is_file(), "The fixture must spawn a real descendant before timing out")
        for pid in json.loads(marker.read_text()).values():
            try:
                process = psutil.Process(pid)
                # PID reuse cannot turn another process into an owned child.
                self.assertFalse(process.is_running() and str(marker) in process.cmdline())
            except psutil.NoSuchProcess:
                pass


@unittest.skipUnless(sys.platform.startswith("linux"), "Native Linux ELF execution is required")
class UbuntuDispatchChecks(OwnedFixture):
    def native_entry(self, name="UBUNTU"):
        machine = platform.machine().lower()
        if machine in ("aarch64", "arm64"):
            if name == "LINUX_MINT":
                self.skipTest("Linux Mint has an AMD64 entry only")
            name += "_ARM64"
        elif machine not in ("x86_64", "amd64"):
            self.skipTest("A supported native Linux CPU is required")
        return self.project / "LAUNCHER" / name

    def invoke(self, arguments=(), *, name="UBUNTU"):
        return subprocess.run([str(self.native_entry(name)), *arguments], cwd=self.sandbox,
                              env=self.env, capture_output=True, timeout=10, check=False)

    def assert_dispatch(self, result, arguments=()):
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout.decode().split("\0")[:-1],
                         [str(self.project / "direct/ubuntu/run.sh"), "--desktop", *arguments])
        self.assertFalse((self.sandbox / "NEVER").exists())

    def test_native_entry_preserves_empty_unicode_and_shell_arguments(self):
        arguments = ["--project", 'Sketch Ω $(touch NEVER) "quoted"', "", "line\nbreak",
                     "%PATH%", "tail\\", "日本語 🙂", "--repair"]
        self.assert_dispatch(self.invoke(arguments), arguments)

    def test_native_entry_survives_move_and_literal_quote_path_without_python_or_path(self):
        moved = self.sandbox / 'Moved Ω %20 %k $cash `literal` "quoted" $(touch NEVER)'
        self.project.rename(moved)
        self.project = moved
        arguments = ["--project", 'Project Ω "quote" $(touch NEVER)', "--new-window", ""]
        self.assert_dispatch(self.invoke(arguments), arguments)

    def test_native_entry_reports_dispatcher_exit_status(self):
        self.recorder.write_text("exit 23\n", encoding="utf-8")
        self.assertEqual(self.invoke().returncode, 23)

    def test_missing_application_script_refuses(self):
        self.recorder.unlink()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"direct/ubuntu/run.sh", result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_renamed_or_displaced_linux_entry_refuses_without_dispatch(self):
        entry = self.native_entry()
        for destination in (self.project / "LAUNCHER/UNKNOWN", self.project / entry.name):
            with self.subTest(destination=destination.name):
                shutil.copy2(entry, destination)
                result = subprocess.run([str(destination)], cwd=self.sandbox, env=self.env,
                                        capture_output=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"LAUNCHER", result.stderr)
                destination.unlink()

    def test_unavailable_platforms_only_report_notice_and_refuse(self):
        for name, display in (("FEDORA", "Fedora"), ("LINUX_MINT", "Linux Mint")):
            with self.subTest(name=name):
                result = self.invoke(name=name)
                self.assertEqual(result.returncode, 1)
                self.assertIn(("MCU Flasher for " + display + " is not available").encode(),
                              result.stdout + result.stderr)
                self.assertNotIn(b"\0", result.stdout + result.stderr)
                self.assertEqual(sorted(path.name for path in self.sandbox.iterdir()), [self.project.name])

    def test_macos_notice_does_not_invoke_the_application_dispatcher(self):
        result = subprocess.run(["/bin/bash", str(self.project / "LAUNCHER/MacOS.command")],
                                cwd=self.sandbox, env=self.env, capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"MCU Flasher for macOS is not available", result.stdout + result.stderr)
        self.assertNotIn(b"\0", result.stdout + result.stderr)


@unittest.skipUnless(os.name == "nt", "Native Windows Script Host is required")
class WindowsDispatchChecks(OwnedFixture):
    def write_windows_dispatcher(self, source):
        target = self.project / "direct/windows/run.vbs"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.replace('\n', '\r\n').encode('utf-16'))
        return target

    def capture_windows(self, target, arguments, receipt, *, timeout=30,
                        expected_arguments=None, extra_environment=None):
        executable = self.project / "LAUNCHER/WINDOWS.exe"
        return windows_probe().capture(
            executable, target, arguments, receipt, self.sandbox,
            command_prefix=[str(executable)],
            expected_cwd=self.project, expected_script=Path("-c"), timeout=timeout,
            expected_arguments=expected_arguments, extra_environment=extra_environment)

    def write_python_forwarding_dispatcher(self, receipt):
        source = (ROOT / "direct/windows/run.vbs").read_text(encoding="ascii")
        loop = re.search(r"Dim argIdx, allArgs\n.*?^Next$", source, re.MULTILINE | re.DOTALL).group()
        shielding = '\n'.join(('setupEnvironment("MCU_FLASHER_LITERAL_PERCENT") = "%"',
                               'runCmd = Replace(runCmd, "%", "%MCU_FLASHER_LITERAL_PERCENT%")'))
        self.assertIn(shielding, source)
        code = ('import json,os,sys; from pathlib import Path; '
                'target=Path(sys.argv[1]); partial=target.with_suffix(".partial"); '
                'partial.write_text(json.dumps({"arguments":sys.argv[2:],"cwd":os.getcwd(),'
                '"script":sys.argv[0],"forwardedEnvironment":'
                '[key for key in os.environ if key.startswith("MCU_FLASHER_WSH_ARG_")]},'
                'ensure_ascii=False),encoding="utf-8"); partial.rename(target)')
        base_command = subprocess.list2cmdline([sys.executable, "-B", "-c", code, str(receipt)])
        # Only the production argument loop/helper is executed. All Bootstrap,
        # runtime, installer, persistence and hardware code is replaced by this
        # owned Python argv recorder, which imports no application modules.
        fixture = '\n'.join(('Option Explicit', 'Dim shell, setupEnvironment, runCmd, launchArguments',
                             'Set shell = CreateObject("WScript.Shell")',
                             'Set setupEnvironment = shell.Environment("PROCESS")',
                             'launchArguments = ReadLauncherArguments(WScript.Arguments, setupEnvironment)', loop,
                             'runCmd = "' + base_command.replace('"', '""') + '" & allArgs',
                             shielding, 'WScript.Quit shell.Run(runCmd, 0, True)',
                             windows_probe().python_argument_helpers(source), ''))
        return self.write_windows_dispatcher(fixture)

    def test_native_windows_pe_and_production_wsh_preserve_literal_arguments_after_move(self):
        moved = self.sandbox / "Relocated Ω %PATH% $cash `literal` & (safe)"
        self.project.rename(moved)
        self.project = moved
        receipt = self.sandbox / "literal-arguments.json"
        target = self.write_python_forwarding_dispatcher(receipt)
        arguments = ["--project", 'Sketch Ω "quoted"', "", "C:\\tail\\", 'slash\\"quote',
                     "%PATH%", "%MCU_FLASHER_LITERAL_PERCENT%", "$(touch NEVER) & | ^ ! `literal`",
                     "--new-window", "emoji 🙂 and 日本語", "line one\nline two\ttab"]
        result = self.capture_windows(target, arguments, receipt,
                                      extra_environment={"MCU_FLASHER_WSH_ARG_stale": "must clear"})
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["observed"]["arguments"], arguments)
        self.assertEqual(result["observed"]["forwardedEnvironment"], [])
        self.assertFalse((self.sandbox / "NEVER").exists())

    def test_native_windows_pe_propagates_dispatcher_failure(self):
        target = self.write_windows_dispatcher("WScript.Quit 23\n")
        result = self.capture_windows(target, [], self.sandbox / "absent-failure.json")
        self.assertEqual(result["launcherExitCode"], 23, result)
        self.assertEqual(result["cleanupFailures"], [], result)

    def test_native_windows_portable_entry_preserves_representable_project_arguments(self):
        moved = self.sandbox / "Project path Ω %PATH% $cash `literal` & (safe)"
        self.project.rename(moved)
        self.project = moved
        receipt = self.sandbox / "project-arguments.json"
        target = self.write_python_forwarding_dispatcher(receipt)
        arguments = ["--project", str(moved / "user sketch Ω %PATH%/main.ino"),
                     "%PATH%", "%MCU_FLASHER_LITERAL_PERCENT%", "& | ^ ! $cash `literal`", "--new-window"]
        result = self.capture_windows(target, arguments, receipt)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["observed"]["arguments"], arguments)
        self.assertEqual(result["observed"]["forwardedEnvironment"], [])

    def test_native_windows_entry_without_arguments_consumes_empty_payload(self):
        receipt = self.sandbox / "empty-arguments.json"
        target = self.write_python_forwarding_dispatcher(receipt)
        result = self.capture_windows(target, [], receipt)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["observed"]["forwardedEnvironment"], [])

    def test_native_windows_malformed_or_oversized_payload_refuses_and_clears(self):
        source = (ROOT / "direct/windows/run.vbs").read_text(encoding="ascii")
        fixture = self.sandbox / "read-forwarded-arguments.vbs"
        fixture.write_bytes((r'''Option Explicit
Dim shell, environment, values, failure, remaining, entry, stream, fs
Set shell = CreateObject("WScript.Shell")
Set environment = shell.Environment("PROCESS")
On Error Resume Next
values = ReadLauncherArguments(WScript.Arguments, environment)
failure = Err.Number
Err.Clear
On Error GoTo 0
remaining = 0
For Each entry In environment
    If Left(UCase(entry), 20) = "MCU_FLASHER_WSH_ARG_" Then remaining = remaining + 1
Next
Set fs = CreateObject("Scripting.FileSystemObject")
Set stream = fs.CreateTextFile(environment("MCU_LAUNCHER_PROBE_RESULT"), True, True)
stream.Write "{""error"":" & CStr(failure) & ",""remaining"":" & CStr(remaining) & "}"
stream.Close
WScript.Quit 0
''' + windows_probe().python_argument_helpers(source)).replace('\n', '\r\n').encode('utf-16'))
        cscript = Path(os.environ["SystemRoot"]) / "System32/cscript.exe"
        cases = [({}, []), ({"MCU_FLASHER_WSH_ARG_COUNT": "not-a-count"}, []),
                 ({"MCU_FLASHER_WSH_ARG_COUNT": "0000"}, []),
                 ({"MCU_FLASHER_WSH_ARG_COUNT": "1025"}, []),
                 ({"MCU_FLASHER_WSH_ARG_COUNT": "1"}, []),
                 ({"MCU_FLASHER_WSH_ARG_COUNT": "1", "MCU_FLASHER_WSH_ARG_0": "v0:bad"}, []),
                 ({"MCU_FLASHER_WSH_ARG_COUNT": "2", "MCU_FLASHER_WSH_ARG_0": "v1:" + "x" * 20000,
                   "MCU_FLASHER_WSH_ARG_1": "v1:" + "y" * 20000}, []),
                 ({"MCU_FLASHER_WSH_ARG_COUNT": "0"}, ["unexpected-extra-argument"])]
        for index, (payload, extra_arguments) in enumerate(cases):
            with self.subTest(case=index):
                receipt = self.sandbox / f"refused-{index}.json"
                environment = {**os.environ, **payload, "MCU_FLASHER_WSH_ARG_stale": "must clear",
                               "MCU_LAUNCHER_PROBE_RESULT": str(receipt)}
                result = subprocess.run([str(cscript), "//nologo", str(fixture),
                                         "--mcu-flasher-wsh-arguments", *extra_arguments], cwd=self.sandbox,
                                        env=environment, capture_output=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                observed = json.loads(receipt.read_text(encoding="utf-16"))
                self.assertNotEqual(observed["error"], 0)
                self.assertEqual(observed["remaining"], 0)

    def test_native_runtime_preflight_preserves_literal_percent_without_launching_or_repair(self):
        source = (ROOT / "direct/windows/run.vbs").read_text(encoding="ascii")
        fixture = self.sandbox / "runtime-preflight.vbs"
        fixture.write_bytes(runtime_preflight_fixture(source).replace('\n', '\r\n').encode('utf-16'))
        executable = self.project / "%MCU_FLASHER_LITERAL_PERCENT%" / "python.exe"
        executable.parent.mkdir()
        executable.write_bytes(b"fixture only; never executable\n" * 512)
        receipt = self.sandbox / "expanded-command.txt"
        cscript = Path(os.environ["SystemRoot"]) / "System32/cscript.exe"
        result = subprocess.run([str(cscript), "//nologo", str(fixture), str(executable), str(receipt)],
                                cwd=self.sandbox, capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(receipt.is_file())
        self.assertTrue(receipt.read_text(encoding="utf-16").startswith(f'"{executable}" -I -B -c '))
        self.assertEqual(executable.read_bytes(), b"fixture only; never executable\n" * 512)

    def test_native_windows_timeout_stops_nested_wscript_and_python_child(self):
        import psutil

        marker = self.sandbox / "native-sleeping-child.json"
        code = ('import json,os,sys,time; from pathlib import Path; '
                'Path(sys.argv[1]).write_text(json.dumps({"pid":os.getpid()})); time.sleep(60)')
        command = subprocess.list2cmdline([sys.executable, "-I", "-B", "-c", code, str(marker)])
        fixture = '\n'.join(('Option Explicit', 'Dim shell, command',
                             'Set shell = CreateObject("WScript.Shell")',
                             'shell.Environment("PROCESS")("MCU_FLASHER_LITERAL_PERCENT") = "%"',
                             'command = "' + command.replace('"', '""') + '"',
                             'command = Replace(command, "%", "%MCU_FLASHER_LITERAL_PERCENT%")',
                             'WScript.Quit shell.Run(command, 0, True)', ''))
        target = self.write_windows_dispatcher(fixture)
        result = self.capture_windows(target, [], self.sandbox / "absent-result.json", timeout=5)
        self.assertIn("Timed out", result["error"])
        self.assertEqual(result["cleanupFailures"], [])
        self.assertTrue(marker.is_file(), "The owned nested WSH must start its Python child")
        pid = json.loads(marker.read_text())["pid"]
        try:
            process = psutil.Process(pid)
            self.assertFalse(process.is_running() and str(marker) in process.cmdline())
        except psutil.NoSuchProcess:
            pass
        self.assertEqual(windows_probe().owned_script_processes(psutil, target), {})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-only", action="store_true",
                        help="Run owned host payload dispatch fixtures before cross-host artifact assembly")
    options, remaining = parser.parse_known_args()
    if options.native_only:
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case)
                                   for case in (ProcessCleanupChecks, UbuntuDispatchChecks, WindowsDispatchChecks))
        sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
