#!/usr/bin/env python3
"""Verify Windows component sources and permission prompts with owned fixtures.

No installer, winget, network or prompt is used: archives are built here, commands
are recorded, and the Windows functions run extracted with recorded native calls.
"""
from __future__ import annotations

import ast
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from src.modules import windows_components as components


def bootstrap_functions(names, scope):
    """Run named Bootstrap functions without importing Windows Bootstrap."""
    source = ROOT / "src/modules/bootstrap.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in nodes:
        node.returns = None
        for argument in (*node.args.args, *node.args.kwonlyargs):
            argument.annotation = None
    assert {node.name for node in nodes} == set(names), names
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), scope)
    return scope


class Fixture(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "temp/audit/windows-components"
        parent.mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)


class LedgerChecks(Fixture):
    def setUp(self):
        super().setUp()
        self.ledger = components.PromptLedger(self.root / "state/machine_setup.json")

    def test_a_declined_or_failed_question_is_settled_for_the_same_installer(self):
        self.assertEqual(self.ledger.settled("ch34x", "a"), "")
        for outcome in ("declined", "failed"):
            self.ledger.record("ch34x", outcome, "a")
            self.assertEqual(self.ledger.settled("ch34x", "a"), outcome)
            self.assertEqual(components.PromptLedger(self.ledger.path).settled("ch34x", "a"), outcome)
        self.assertEqual(self.ledger.settled("ch34x", "b"), "", "A new installer is a new question")
        self.assertEqual(self.ledger.settled("cp210x", "a"), "")

    def test_setup_closed_during_the_question_asks_once_more_only(self):
        self.ledger.record("cp210x", "asked", "a")
        self.assertEqual(self.ledger.settled("cp210x", "a"), "")
        self.ledger.record("cp210x", "asked", "a")
        self.assertEqual(self.ledger.settled("cp210x", "a"), "unanswered")

    def test_success_and_explicit_setup_clear_the_answer(self):
        self.ledger.record("cp210x", "failed")
        self.ledger.record("cp210x", "done")
        self.assertEqual(self.ledger.settled("cp210x"), "")
        self.ledger.record("node", "declined")
        self.ledger.forget()
        self.assertEqual(self.ledger.settled("node"), "")
        self.ledger.forget()

    def test_damaged_state_asks_rather_than_failing(self):
        self.ledger.path.parent.mkdir(parents=True)
        for text in ("", "[]", '{"schema": 2}', '{"schema": 1, "components": {"node": 4}}'):
            self.ledger.path.write_text(text, encoding="utf-8")
            self.assertEqual(self.ledger.settled("node"), "")
            self.assertTrue(self.ledger.record("node", "declined"))
            self.assertEqual(self.ledger.settled("node"), "declined")
        with patch.object(components.os, "replace", side_effect=OSError("read only")):
            self.assertFalse(self.ledger.record("node", "failed"))
        self.assertEqual([item.name for item in self.ledger.path.parent.iterdir()], [self.ledger.path.name])

    def test_installers_are_named_by_their_bytes(self):
        first, second = self.root / "CH341SER.exe", self.root / "copy/ch341ser.EXE"
        second.parent.mkdir()
        first.write_bytes(b"MZ one")
        second.write_bytes(b"MZ one")
        self.assertEqual(components.installer_fingerprint(first), components.installer_fingerprint(second))
        second.write_bytes(b"MZ two")
        self.assertNotEqual(components.installer_fingerprint(first), components.installer_fingerprint(second))
        self.assertEqual(components.installer_fingerprint(self.root / "absent.exe"), "")


class WingetChecks(Fixture):
    def test_commands_name_one_exact_package_and_never_ask(self):
        for component, package in components.WINGET_IDS.items():
            command = components.winget_command("winget", component)
            self.assertEqual(command[:5], ["winget", "install", "--exact", "--id", package])
            for option in ("--silent", "--accept-source-agreements", "--accept-package-agreements", "--disable-interactivity"):
                self.assertIn(option, command)
            self.assertEqual(command[command.index("--source") + 1], "winget")
        with self.assertRaises(ValueError):
            components.winget_install("cp210x", winget="winget")

    def test_success_already_installed_failure_timeout_and_absence(self):
        def result(code, text=""):
            return lambda *_args, **_options: SimpleNamespace(returncode=code, stdout=text)
        self.assertEqual(components.winget_install("node", winget="winget", run=result(0)), (True, ""))
        self.assertEqual(components.winget_install("node", winget="winget", run=result(0x8A150061 - 2**32)), (True, ""))
        installed, detail = components.winget_install("node", winget="winget", run=result(0x8A150014 - 2**32, "No package found\n"))
        self.assertFalse(installed)
        self.assertIn("0x8a150014", detail)
        self.assertIn("No package found", detail)
        timeout = Mock(side_effect=subprocess.TimeoutExpired("winget", 5))
        self.assertIn("did not finish", components.winget_install("node", winget="winget", run=timeout, timeout=5)[1])
        with patch.object(components, "winget_path", return_value=None):
            self.assertEqual(components.winget_install("node"), (False, "winget is not available on this PC"))

    def test_private_python_goes_only_into_the_private_folder(self):
        run = Mock(return_value=SimpleNamespace(returncode=0, stdout=""))
        target = Path("C:/Naph's Tools/src/_python")
        with patch.object(components, "winget_path", return_value="winget"):
            self.assertTrue(components.install_private_python(target, run=run)[0])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--scope") + 1], "user")
        override = command[command.index("--override") + 1]
        self.assertIn(f'TargetDir="{target}"', override)
        for switch in ("InstallAllUsers=0", "PrependPath=0", "Include_launcher=0", "AssociateFiles=0"):
            self.assertIn(switch, override)

    def test_windows_launcher_uses_the_same_private_install_without_a_bundled_one(self):
        text = (ROOT / "direct/windows/run.vbs").read_text(encoding="utf-8")
        heal = text[text.index("Function AutoHealPrivatePython"):]
        heal = heal[:heal.index("End Function")]
        self.assertIn("--id Python.Python.3.14 --source winget --scope user", heal)
        self.assertIn(r'TargetDir=\""" & targetDir & "\""', heal)
        self.assertLess(heal.index("Not fso.FileExists(wingetExe)"), heal.index("DeleteFolder"),
                        "A damaged runtime may go only when something can replace it")
        self.assertNotIn("runas", text.lower())


class UserInstallChecks(Fixture):
    def archive(self, members):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as bundle:
            for name, content in members.items():
                bundle.writestr(name, content)
        return stream.getvalue()

    def publisher(self, name, payload, listing_name):
        digest = hashlib.sha256(payload).hexdigest()
        urls = []

        def fetch(url, **_options):
            urls.append(url)
            if url.endswith("index.json"):
                return json.dumps([{"version": "v27.0.0", "lts": False}, {"version": "v26.13.1", "lts": "Fixture"}])
            return f"{'0' * 64}  other.zip\n{digest}  {listing_name}\n"

        def download(url, target, expected):
            urls.append(url)
            self.assertEqual(expected, digest)
            self.assertTrue(url.endswith(name), url)
            Path(target).write_bytes(payload)
        return fetch, download, urls

    def test_arduino_cli_is_verified_and_replaces_only_its_own_files(self):
        folder = self.root / "Programs/Arduino CLI"
        name = "arduino-cli_1.6.0_Windows_64bit.zip"
        payload = self.archive({"arduino-cli.exe": b"MZ new", "LICENSE.txt": b"license", "unrelated.dll": b"x"})
        fetch, download, urls = self.publisher(name, payload, name)
        path = components.install_arduino_cli_for_user("1.6.0", folder=folder, download=download, fetch=fetch, machine="AMD64")
        self.assertEqual(path.read_bytes(), b"MZ new")
        self.assertEqual(sorted(item.name for item in folder.iterdir()), ["LICENSE.txt", "arduino-cli.exe"])
        self.assertEqual(urls[0], "https://github.com/arduino/arduino-cli/releases/download/v1.6.0/1.6.0-checksums.txt")
        (folder / "notes.txt").write_text("kept", encoding="utf-8")
        path.write_bytes(b"MZ old")
        components.install_arduino_cli_for_user("1.6.0", folder=folder, download=download, fetch=fetch, machine="AMD64")
        self.assertEqual(path.read_bytes(), b"MZ new")
        self.assertTrue((folder / "notes.txt").is_file())
        self.assertEqual([item.name for item in folder.parent.iterdir()], ["Arduino CLI"], "No staging folder may remain")

    def test_unlisted_unsafe_or_wrong_archives_leave_the_installed_copy(self):
        folder = self.root / "cli"
        folder.mkdir()
        (folder / "arduino-cli.exe").write_bytes(b"MZ current")
        name = "arduino-cli_1.6.0_Windows_ARM64.zip"
        for members, listing, error in (
            ({"arduino-cli.exe": b"MZ new"}, "something-else.zip", "no checksum"),
            ({"arduino-cli.exe": b"not a program"}, name, "no Windows executable"),
            ({"readme.txt": b"nothing"}, name, "arduino-cli.exe"),
        ):
            with self.subTest(error=error):
                fetch, download, _ = self.publisher(name, self.archive(members), listing)
                with self.assertRaisesRegex((OSError, ValueError), error):
                    components.install_arduino_cli_for_user("1.6.0", folder=folder, download=download, fetch=fetch, machine="arm64")
                self.assertEqual((folder / "arduino-cli.exe").read_bytes(), b"MZ current")
        for version in ("", "1.6", "../1.6.0", "1.6.0/evil"):
            with self.assertRaises(ValueError):
                components.install_arduino_cli_for_user(version, folder=folder, download=Mock(), fetch=Mock())
        with self.assertRaisesRegex(ValueError, "leaves its folder"):
            archive = self.root / "bad.zip"
            archive.write_bytes(self.archive({"../escaped.txt": b"x"}))
            components._unpack(archive, self.root / "cli")

    def test_a_locked_executable_is_renamed_aside(self):
        source, target = self.root / "new.exe", self.root / "tool.exe"
        source.write_bytes(b"new")
        target.write_bytes(b"old")
        real, calls = os.replace, []

        def locked(first, second):
            calls.append(Path(second).name)
            if len(calls) == 1:
                raise PermissionError("in use")
            real(first, second)
        with patch.object(components.os, "replace", side_effect=locked):
            components._publish(source, target)
        self.assertEqual(target.read_bytes(), b"new")
        source.write_bytes(b"newer")
        with patch.object(components.os, "replace", side_effect=[PermissionError("in use"), None, PermissionError("still"), None]), \
                self.assertRaises(PermissionError):
            components._publish(source, target)

    def test_node_is_unpacked_once_for_this_account(self):
        folder = self.root / "Programs/nodejs"
        stem = "node-v26.13.1-win-x64"
        payload = self.archive({f"{stem}/node.exe": b"MZ", f"{stem}/npm.cmd": b"@echo", f"{stem}/node_modules/npm/x": b""})
        fetch, download, _ = self.publisher(f"{stem}.zip", payload, f"{stem}.zip")
        path = components.install_node_for_user(folder=folder, download=download, fetch=fetch, machine="AMD64")
        self.assertEqual(path, folder / "node.exe")
        self.assertTrue((folder / "npm.cmd").is_file())
        with self.assertRaisesRegex(OSError, "already holds files"):
            components.install_node_for_user(folder=folder, download=download, fetch=fetch, machine="AMD64")
        self.assertEqual([item.name for item in folder.parent.iterdir()], ["nodejs"])


class PromptChecks(Fixture):
    """The extracted Bootstrap functions, with recorded native calls."""

    def setUp(self):
        super().setUp()
        self.ledger = components.PromptLedger(self.root / "machine_setup.json")
        self.launched = []
        self.missing = ["ch34x"]
        self.messages = []
        say = lambda kind: lambda text="", *_: self.messages.append((kind, text))
        self.scope = {
            "Path": Path, "sys": SimpleNamespace(platform="win32"), "os": os, "subprocess": subprocess,
            "status": say("status"), "ok": say("ok"), "warn": say("warn"), "fail": say("fail"),
            "_prompt_ledger": lambda: self.ledger, "_component_installer": lambda name: f"{name}-installer",
            "_is_process_elevated": lambda: False, "_explicit_setup_requested": lambda: self.explicit,
            "_missing_machine_components": lambda: list(self.missing),
            "_ORIGINAL_PYTHON_EXECUTABLE": "C:/app/src/_python/pythonw.exe", "__file__": str(ROOT / "src/modules/bootstrap.py"),
        }
        self.explicit = False

    def request(self, elevated):
        self.scope["_shell_execute_elevated_wait"] = elevated
        return bootstrap_functions({"_request_first_run_privileged_setup"}, self.scope)["_request_first_run_privileged_setup"]()

    def test_declined_setup_is_asked_once_until_repair(self):
        elevated = Mock(side_effect=PermissionError("UAC_DECLINED: fixture"))
        self.assertFalse(self.request(elevated))
        self.assertTrue(self.request(elevated), "A settled question lets the start continue")
        self.assertTrue(self.request(elevated))
        elevated.assert_called_once()
        self.explicit = True
        self.assertFalse(self.request(elevated))
        self.assertEqual(elevated.call_count, 2)

    def test_installer_that_changes_nothing_is_not_repeated_on_every_start(self):
        elevated = Mock(return_value=0)
        self.assertFalse(self.request(elevated))
        for _ in range(3):
            self.assertTrue(self.request(elevated))
        elevated.assert_called_once()
        self.missing = ["ch34x", "cp210x"]
        self.request(elevated)
        self.assertEqual(elevated.call_count, 2, "A component never asked about is a new question")

    def test_success_leaves_nothing_to_ask(self):
        def elevated(*_args, **_options):
            self.missing.clear()
            return 0
        self.assertTrue(self.request(elevated))
        self.assertEqual(self.ledger.settled("ch34x", "ch34x-installer"), "")
        self.assertTrue(self.request(Mock(side_effect=AssertionError("Nothing is missing"))))

    def test_only_drivers_are_worth_a_prompt(self):
        tree = ast.parse((ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig"))
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "_missing_machine_components")
        called = {ast.unparse(call.func) for call in ast.walk(node) if isinstance(call, ast.Call)}
        self.assertEqual(called & {"find_arduino_cli", "check_webview2_runtime", "_find_usable_npm_cmd"}, set())
        scope = bootstrap_functions({"_missing_machine_components"}, {
            "check_cp210x_driver": lambda: False, "check_ch34x_driver": lambda: False, "_is_valid_exe": lambda path: True,
            "_cp210x_installer": lambda: Path("x"), "_ch34x_installer_available": lambda: False})
        self.assertEqual(scope["_missing_machine_components"](), ["cp210x"])

    def test_every_named_prompt_is_gated_and_records_its_answer(self):
        shell = SimpleNamespace(ShellExecuteExW=Mock(return_value=0))
        kernel = SimpleNamespace(GetLastError=lambda: 1223, WaitForSingleObject=Mock(return_value=0), CloseHandle=Mock(),
                                 GetExitCodeProcess=Mock(), TerminateProcess=Mock())
        scope = bootstrap_functions({"_shell_execute_elevated_wait"}, dict(self.scope))
        run = scope["_shell_execute_elevated_wait"]
        with patch.object(ctypes, "windll", SimpleNamespace(shell32=shell, kernel32=kernel), create=True):
            with self.assertRaisesRegex(PermissionError, "UAC_DECLINED"):
                run("installer.exe", ["/s"], component="ch34x")
            with self.assertRaisesRegex(PermissionError, "UAC_SETTLED"):
                run("installer.exe", ["/s"], component="ch34x")
            shell.ShellExecuteExW.assert_called_once()
            shell.ShellExecuteExW.return_value = 1
            self.assertEqual(run("installer.exe", ["/s"], component="cp210x"), 0)
            self.assertEqual(self.ledger.settled("cp210x", "cp210x-installer"), "")
            run("helper.exe", [])           # The first-run helper keeps its own record.
        source = (ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig")
        calls = [call for call in ast.walk(ast.parse(source)) if isinstance(call, ast.Call)
                 and ast.unparse(call.func) == "_shell_execute_elevated_wait"]
        ungated = [ast.unparse(call) for call in calls if not any(word.arg == "component" for word in call.keywords)]
        self.assertEqual(len(ungated), 1, ungated)
        self.assertIn("--privileged-first-run", ungated[0])

    def test_private_runtime_is_never_repaired_with_a_prompt_or_under_itself(self):
        source = (ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig")
        node = next(item for item in ast.parse(source).body
                    if isinstance(item, ast.FunctionDef) and item.name == "_heal_private_python_runtime")
        self.assertNotIn("_shell_execute_elevated_wait", ast.unparse(node))
        scope = bootstrap_functions({"_heal_private_python_runtime"}, {
            "Path": Path, "sys": SimpleNamespace(platform="win32"), "SCRIPT_DIR": self.root,
            "_ORIGINAL_PYTHON_EXECUTABLE": str((self.root / "src/_python/pythonw.exe").resolve()),
            "subprocess": SimpleNamespace(run=Mock(side_effect=AssertionError("A running runtime is not probed"))),
            "shutil": SimpleNamespace(rmtree=Mock(side_effect=AssertionError("A running runtime is not removed")))})
        self.assertTrue(scope["_heal_private_python_runtime"]())


if __name__ == "__main__":
    unittest.main(verbosity=2)
