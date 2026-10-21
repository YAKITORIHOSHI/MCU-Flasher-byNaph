#!/usr/bin/env python3
"""Verify portable LAUNCHER entries with owned, fake dispatchers only.

Never runs application startup, setup, metadata generation or hardware services.
Native Windows checks exercise WScript and private Python argument parsing;
Linux checks include the installed GLib desktop-entry parser when available.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = ROOT / "LAUNCHER"
EXPECTED = {"WINDOWS.vbs", "UBUNTU", "UBUNTU.desktop", "MacOS.command",
            "FEDORA.desktop", "LINUX_MINT.desktop", "README.md"}


def desktop_command(text: str, *, action: bool = False) -> list[str]:
    """Apply the desktop string/Exec quoting layers, retaining %k as a token."""
    values = [line.removeprefix("Exec=") for line in text.splitlines() if line.startswith("Exec=")]
    value = values[1 if action else 0]
    escapes = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    value = re.sub(r"\\([sntr\\])", lambda match: escapes[match.group(1)], value)
    arguments, current = [], []
    quoted = False
    index = 0
    while index < len(value):
        character = value[index]
        if character == '"':
            quoted = not quoted
        elif quoted and character == "\\" and index + 1 < len(value) and value[index + 1] in '\\"`$':
            index += 1
            current.append(value[index])
        elif not quoted and character.isspace():
            if current:
                arguments.append("".join(current))
                current = []
        else:
            current.append(character)
        index += 1
    if quoted:
        raise ValueError("Unclosed desktop Exec argument")
    if current:
        arguments.append("".join(current))
    return [argument.replace("%%", "%") for argument in arguments]


def windows_helper(text: str) -> str:
    match = re.search(r"^Function QuoteWindowsArgument\(value\)\n.*?^End Function$", text,
                      re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError("Missing Windows argument quoting helper")
    return match.group()


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
        for name in EXPECTED - {"WINDOWS.vbs", "README.md"}:
            self.assertEqual(modes.get("LAUNCHER/" + name), "100755", name)

    def test_named_entries_and_executable_modes(self):
        self.assertEqual({path.name for path in LAUNCHERS.iterdir()}, EXPECTED)
        for name in EXPECTED:
            self.assertFalse((LAUNCHERS / name).is_symlink(), name)
        if os.name != "nt":
            for name in EXPECTED - {"WINDOWS.vbs", "README.md"}:
                self.assertEqual((LAUNCHERS / name).stat().st_mode & 0o777, 0o755, name)

    def test_windows_entries_are_ascii_crlf_and_separate_wsh_from_crt_quoting(self):
        paths = (LAUNCHERS / "WINDOWS.vbs", ROOT / "direct/windows/run.vbs")
        texts = []
        for path in paths:
            raw = path.read_bytes()
            raw.decode("ascii")
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
            text = raw.decode("ascii").replace("\r\n", "\n")
            texts.append(text)
            self.assertIn('Replace(', text)
            self.assertIn('"%MCU_FLASHER_LITERAL_PERCENT%"', text)
        self.assertNotIn('QuoteWindowsArgument', texts[0])
        self.assertIn('QuoteWshPath(entry)', texts[0])
        self.assertIn('QuoteWindowsArgument(launchArguments(argIdx))', texts[1])
        self.assertIn('"direct\\windows\\run.vbs"', texts[0])
        self.assertIn('fs.GetParentFolderName(fs.GetParentFolderName(WScript.ScriptFullName))', texts[0])
        code = "\n".join(line for line in texts[0].splitlines() if not line.lstrip().startswith("'"))
        self.assertNotIn("cmd.exe", code)

    def test_desktop_entries_have_no_fixed_checkout_or_working_directory(self):
        for name in ("UBUNTU.desktop", "FEDORA.desktop", "LINUX_MINT.desktop"):
            text = (LAUNCHERS / name).read_text(encoding="utf-8")
            self.assertNotIn("Path=", text)
            self.assertNotIn(str(ROOT), text)
            self.assertNotIn("/home/", text)
            self.assertEqual(desktop_command(text)[:2], ["/bin/bash", "-c"])
        ubuntu = (LAUNCHERS / "UBUNTU.desktop").read_text(encoding="utf-8")
        self.assertEqual(desktop_command(ubuntu)[3:], ["mcu-flasher-launcher", "%k"])
        self.assertEqual(desktop_command(ubuntu, action=True)[3:], ["mcu-flasher-launcher", "%k", "--repair"])

    def test_native_desktop_file_validation(self):
        validator = shutil.which("desktop-file-validate")
        if not validator:
            self.skipTest("desktop-file-validate is unavailable")
        result = subprocess.run([validator, *(str(LAUNCHERS / name) for name in sorted(EXPECTED)
                                            if name.endswith(".desktop"))], capture_output=True,
                                text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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


@unittest.skipIf(os.name == "nt", "Portable Bash/desktop dispatch runs on native Linux")
class UbuntuDispatchChecks(OwnedFixture):
    def invoke_desktop(self, location=None, arguments=(), *, action=False):
        command = desktop_command((self.project / "LAUNCHER/UBUNTU.desktop").read_text(), action=action)
        # %k is one literal argument supplied by the file manager, not source.
        self.assertEqual(command[4], "%k")
        if location is None:
            command.pop(4)
        else:
            command[4] = location
        return subprocess.run([*command, *arguments], cwd=self.sandbox, env=self.env,
                              capture_output=True, timeout=10, check=False)

    def assert_dispatch(self, result, arguments=()):
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout.decode().split("\0")[:-1],
                         [str(self.project / "direct/ubuntu/run.sh"), "--desktop", *arguments])
        self.assertFalse((self.sandbox / "NEVER").exists())

    def test_shell_entry_resolves_parent_and_preserves_empty_unicode_and_shell_arguments(self):
        arguments = ["--project", 'Sketch Ω $(touch NEVER) "quoted"', "", "line\nbreak", "%PATH%", "tail\\"]
        result = subprocess.run(["/bin/bash", str(self.project / "LAUNCHER/UBUNTU"), *arguments],
                                cwd=self.sandbox, env=self.env, capture_output=True,
                                timeout=10, check=False)
        self.assert_dispatch(result, arguments)

    def test_desktop_survives_move_and_literal_quote_path_without_python_or_path(self):
        moved = self.sandbox / 'Moved Ω %20 %k $cash `literal` "quoted" $(touch NEVER)'
        self.project.rename(moved)
        self.project = moved
        arguments = ["--project", 'Project Ω "quote" $(touch NEVER)', "--new-window", ""]
        result = self.invoke_desktop(str(moved / "LAUNCHER/UBUNTU.desktop"), arguments)
        self.assert_dispatch(result, arguments)

    def test_local_uri_decodes_percent_only_once_and_preserves_arguments(self):
        location = quote(str(self.project / "LAUNCHER/UBUNTU.desktop"))
        for prefix in ("file://", "file://localhost"):
            with self.subTest(prefix=prefix):
                self.assert_dispatch(self.invoke_desktop(prefix + location, ["--check", "%20"]), ["--check", "%20"])

    def test_repair_action_forwards_after_desktop_location(self):
        result = self.invoke_desktop(str(self.project / "LAUNCHER/UBUNTU.desktop"), action=True)
        self.assert_dispatch(result, ["--repair"])

    def test_invalid_remote_or_missing_location_refuses_without_dispatch(self):
        for location in (None, "", "relative.desktop", "--repair", "file://remote/shortcut.desktop",
                         "file:///tmp/a?query", "file:///tmp/a#fragment", "file:///tmp/%00/a",
                         "file:///tmp/%GG/a", "file:///tmp/%/a"):
            with self.subTest(location=location):
                result = self.invoke_desktop(location)
                self.assertEqual(result.returncode, 1)
                self.assertIn(b"cannot locate", result.stderr)
                self.assertEqual(result.stdout, b"")

    def test_missing_application_script_refuses(self):
        self.recorder.unlink()
        result = self.invoke_desktop(str(self.project / "LAUNCHER/UBUNTU.desktop"))
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"cannot locate direct/ubuntu/run.sh", result.stderr)

    def test_unavailable_platforms_only_print_notice_and_refuse(self):
        for name, platform in (("FEDORA.desktop", "Fedora"), ("LINUX_MINT.desktop", "Linux Mint"),
                               ("MacOS.command", "macOS")):
            with self.subTest(name=name):
                entry = self.project / "LAUNCHER" / name
                command = desktop_command(entry.read_text()) if name.endswith(".desktop") else ["/bin/bash", str(entry)]
                result = subprocess.run(command, cwd=self.sandbox, env=self.env,
                                        capture_output=True, timeout=10, check=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn(("MCU Flasher for " + platform + " is not available").encode(), result.stdout)
                self.assertNotIn(b"\0", result.stdout)
                self.assertEqual(sorted(path.name for path in self.sandbox.iterdir()), [self.project.name])

    def test_native_glib_parser_launches_only_moved_fake_dispatcher(self):
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
        gio.g_error_free.argtypes = [ctypes.c_void_p]
        self.recorder.write_text('printf \'%s\\0\' "$0" "$@" > "$MCU_LAUNCHER_FOLDER_RECEIPT"\n')
        moved = self.sandbox / 'GLib moved Ω %20 $cash `literal` "quoted"'
        self.project.rename(moved)
        self.project = moved
        receipt = self.sandbox / "receipt"
        app_info = gio.g_desktop_app_info_new_from_filename(os.fsencode(moved / "LAUNCHER/UBUNTU.desktop"))
        self.assertTrue(app_info, "GLib did not parse UBUNTU.desktop")
        error = ctypes.c_void_p()
        try:
            with patch.dict(os.environ, {"MCU_LAUNCHER_FOLDER_RECEIPT": str(receipt)}):
                launched = gio.g_app_info_launch(app_info, None, None, ctypes.byref(error))
            self.assertTrue(launched, "GLib refused the owned desktop fixture")
        finally:
            if error.value:
                gio.g_error_free(error)
            gio.g_object_unref(app_info)
        deadline = time.monotonic() + 3
        while not receipt.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(receipt.is_file(), "GLib child did not reach the fake dispatcher")
        self.assertEqual(receipt.read_bytes().decode().split("\0")[:-1],
                         [str(moved / "direct/ubuntu/run.sh"), "--desktop"])


@unittest.skipUnless(os.name == "nt", "Native Windows Script Host is required")
class WindowsDispatchChecks(OwnedFixture):
    def write_windows_dispatcher(self, source):
        target = self.project / "direct/windows/run.vbs"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.replace('\n', '\r\n').encode('utf-16'))
        return target

    def capture_windows(self, target, arguments, receipt, *, timeout=30,
                        expected_arguments=None, extra_environment=None):
        cscript = Path(os.environ["SystemRoot"]) / "System32/cscript.exe"
        return windows_probe().capture(
            cscript, target, arguments, receipt, self.sandbox,
            command_prefix=[str(cscript), "//nologo", str(self.project / "LAUNCHER/WINDOWS.vbs")],
            expected_cwd=self.sandbox, expected_script=Path("-c"), timeout=timeout,
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

    def test_native_windows_both_forwarding_layers_preserve_received_arguments_after_move(self):
        moved = self.sandbox / "Relocated Ω %PATH% $cash `literal` & (safe)"
        self.project.rename(moved)
        self.project = moved
        receipt = self.sandbox / "arguments.json"
        target = self.write_python_forwarding_dispatcher(receipt)
        arguments = ["--project", 'Sketch Ω "quoted"', "", "C:\\tail\\", 'slash\\"quote',
                     "%PATH%", "%MCU_FLASHER_LITERAL_PERCENT%", "$(touch NEVER) & | ^ ! `literal`", "--new-window"]
        # Python serializes an argv list using CRT rules; WSH parses its own
        # command line differently. Characterize the actual outer WSH input
        # independently, then require production to preserve every received bit.
        baseline = self.sandbox / "outer-wsh-arguments.vbs"
        baseline.write_bytes(windows_probe().RECORDER.replace('\n', '\r\n').encode('utf-16'))
        cscript = Path(os.environ["SystemRoot"]) / "System32/cscript.exe"
        received = windows_probe().capture(
            cscript, baseline, arguments, self.sandbox / "outer-arguments.json", self.sandbox,
            command_prefix=[str(cscript), "//nologo", str(baseline)],
            expected_cwd=self.sandbox, expected_script=baseline)
        self.assertIsNone(received["error"], received)
        self.assertEqual(received["cleanupFailures"], [], received)
        self.assertEqual(received["launcherExitCode"], 0, received)
        self.assertIsNotNone(received["observed"], received)
        expected = received["observed"]["arguments"]
        print("Outer WSH parser baseline: " + json.dumps(
            {"crtInput": arguments, "wshReceived": expected}, ensure_ascii=True), flush=True)
        result = self.capture_windows(target, arguments, receipt, expected_arguments=expected)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["observed"]["forwardedEnvironment"], [])

    def test_native_windows_both_forwarding_layers_preserve_seeded_literal_arguments(self):
        moved = self.sandbox / "Literal Ω %PATH% $cash `literal` & (safe)"
        self.project.rename(moved)
        self.project = moved
        receipt = self.sandbox / "literal-arguments.json"
        target = self.write_python_forwarding_dispatcher(receipt)
        arguments = ["--project", 'Sketch Ω "quoted"', "", "C:\\tail\\", 'slash\\"quote',
                     "%PATH%", "%MCU_FLASHER_LITERAL_PERCENT%", "$(touch NEVER) & | ^ ! `literal`", "--new-window",
                     "emoji 🙂 and 日本語", "line one\nline two\ttab"]
        wrapper = self.project / "LAUNCHER/WINDOWS.vbs"
        source = wrapper.read_text(encoding="ascii")
        call = 'StoreForwardedArguments WScript.Arguments, shell.Environment("PROCESS")'
        self.assertEqual(source.count(call), 1)
        # Seed exactly the strings already available to production WScript code;
        # only the outer host's argument collection is replaced, after parsing.
        source = source.replace('Option Explicit\n', 'Option Explicit\nDim literalArguments\n'
                                'Set literalArguments = New SeededArgumentCollection\n', 1)
        source = source.replace(call, call.replace('WScript.Arguments', 'literalArguments'), 1)
        source += r'''
Class SeededArgumentCollection
    Public Property Get Count
        Count = CLng(CreateObject("WScript.Shell").Environment("PROCESS")("MCU_LAUNCHER_PROBE_ARG_COUNT"))
    End Property
    Public Default Property Get Item(index)
        Item = CreateObject("WScript.Shell").Environment("PROCESS")("MCU_LAUNCHER_PROBE_ARG_" & CStr(index))
    End Property
End Class
'''
        wrapper.write_bytes(source.replace('\n', '\r\n').encode('utf-16'))
        environment = {"MCU_LAUNCHER_PROBE_ARG_COUNT": str(len(arguments)),
                       **{f"MCU_LAUNCHER_PROBE_ARG_{index}": argument
                          for index, argument in enumerate(arguments)}}
        result = self.capture_windows(target, [], receipt, expected_arguments=arguments,
                                      extra_environment=environment)
        self.assertTrue(result["passed"], result)
        self.assertTrue(receipt.is_file(), "Windows entry did not reach the fake argv recorder")
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["arguments"], arguments)
        self.assertEqual(result["observed"]["forwardedEnvironment"], [])

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
    unittest.main(verbosity=2)
