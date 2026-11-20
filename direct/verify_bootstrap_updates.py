#!/usr/bin/env python3
"""Isolated update-environment checks; no setup imports, installs or network."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules.bootstrap_updates import installed_version


def update_functions(**values):
    source = ROOT / "src/modules/bootstrap.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    names = {"_pip_installed_version", "_version_tuple", "check_pip_package_update",
             "_pio_installed_version", "_pio_upgrade", "run_update_checks", "_preseed_venv_site_packages"}
    namespace = dict(sys=sys, Path=Path, **values)
    exec(compile(ast.Module(body=[node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[]), str(source), "exec"), namespace)
    return namespace


class UpdateChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "temp/audit").mkdir(parents=True, exist_ok=True)
        self.fixture = tempfile.TemporaryDirectory(prefix="update-versions-", dir=ROOT / "temp/audit")
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.host = self.root / "host"
        self.env = self.root / "env"
        self.python = self.env / "Scripts/python.exe"
        self.host_site = self.host / "Lib/site-packages"
        self.target_site = self.env / "Lib/site-packages"
        self.target_site.mkdir(parents=True)
        self.host_site.mkdir(parents=True)
        self.messages = []
        self.functions = update_functions(_get_target_python=lambda: self.python,
            _pip_latest_version=lambda name: "17.2", _pip_upgrade=Mock(return_value=True))

    def distribution(self, folder, name, version):
        metadata = folder / f"{name}-{version}.dist-info/METADATA"
        metadata.parent.mkdir()
        metadata.write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
        return metadata

    def test_target_version_ignores_older_host_and_loaded_module(self):
        self.distribution(self.host_site, "websockets", "17.1")
        self.distribution(self.target_site, "websockets", "17.2")
        with patch.object(sys, "path", [str(self.host_site), *sys.path]), \
                patch.dict(sys.modules, {"websockets": Mock(__version__="17.1")}):
            for _ in range(2):
                result = self.functions["check_pip_package_update"]("websockets")
                self.assertEqual(result["installed"], "17.2")
                self.assertFalse(result["update_available"])

    def test_metadata_is_fresh_after_version_changes(self):
        metadata = self.distribution(self.target_site, "websockets", "17.1")
        self.assertTrue(self.functions["check_pip_package_update"]("websockets")["update_available"])
        metadata.write_text("Metadata-Version: 2.1\nName: websockets\nVersion: 17.2\n", encoding="utf-8")
        self.assertFalse(self.functions["check_pip_package_update"]("websockets")["update_available"])

    def test_conflicting_target_metadata_does_not_guess_highest(self):
        self.distribution(self.target_site, "websockets", "17.1")
        self.distribution(self.target_site, "websockets", "17.2")
        with self.assertRaisesRegex(RuntimeError, "Conflicting websockets"):
            installed_version(self.python, "websockets")

    def test_missing_target_does_not_fall_back_to_host(self):
        self.distribution(self.host_site, "websockets", "17.1")
        with patch.object(sys, "path", [str(self.host_site), *sys.path]):
            self.assertIsNone(installed_version(self.python, "websockets"))

    def test_preseed_keeps_installed_metadata_payload_and_scripts_unchanged(self):
        self.distribution(self.host_site, "websockets", "17.1")
        self.distribution(self.target_site, "websockets", "17.2")
        for folder, payload in ((self.host_site, "old payload"), (self.target_site, "new payload")):
            (folder / "websockets").mkdir()
            (folder / "websockets/__init__.py").write_text(payload)
        scripts = self.host / "Scripts"
        scripts.mkdir()
        (scripts / "stale-console.exe").write_bytes(b"host launcher")
        (self.env / "Scripts").mkdir()
        (self.env / "Scripts/current-console.exe").write_bytes(b"target launcher")
        before = {str(path.relative_to(self.env)): path.read_bytes() for path in self.env.rglob("*") if path.is_file()}
        self.functions.update(SCRIPT_DIR=self.root, _ensure_pywin32_system32_dlls=Mock())
        with patch.object(sys, "base_prefix", str(self.host)):
            self.assertEqual(self.functions["_preseed_venv_site_packages"](self.env), 0)
        after = {str(path.relative_to(self.env)): path.read_bytes() for path in self.env.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(installed_version(self.python, "websockets"), "17.2")
        self.functions["_ensure_pywin32_system32_dlls"].assert_not_called()

    def test_preseed_does_not_merge_ambiguous_base_into_fresh_environment(self):
        self.distribution(self.host_site, "websockets", "17.1")
        self.distribution(self.host_site, "websockets", "17.2")
        self.functions.update(SCRIPT_DIR=self.root)
        with patch.object(sys, "base_prefix", str(self.host)):
            self.assertEqual(self.functions["_preseed_venv_site_packages"](self.env), 0)
        self.assertEqual(list(self.target_site.iterdir()), [])
        self.assertIsNone(installed_version(self.python, "websockets"))

    def test_fresh_environment_uses_missing_dependency_installation(self):
        self.distribution(self.host_site, "example", "1.0")
        self.functions.update(SCRIPT_DIR=self.root)
        with patch.object(sys, "base_prefix", str(self.host)):
            self.assertEqual(self.functions["_preseed_venv_site_packages"](self.env), 0)
        self.assertIsNone(installed_version(self.python, "example"))
        self.assertEqual(list(self.target_site.iterdir()), [])

    def test_normalized_distribution_names_and_native_linux_layout(self):
        self.distribution(self.target_site, "Example_Package", "1.0")
        self.assertEqual(installed_version(self.python, "example-package"), "1.0")
        native = self.root / "native/lib/python3.12/site-packages"
        native.mkdir(parents=True)
        self.distribution(native, "example", "2.0")
        self.assertEqual(installed_version(self.root / "native/bin/python", "example"), "2.0")

    def test_prerelease_postrelease_and_equal_versions(self):
        version = self.functions["_version_tuple"]
        self.assertLess(version("2.0rc1"), version("2.0"))
        self.assertGreater(version("2.0.post1"), version("2.0"))
        self.assertEqual(version("2.0.0"), version("2.0"))

    def test_platformio_detection_and_upgrade_share_target(self):
        self.distribution(self.host_site, "platformio", "5.0")
        self.distribution(self.target_site, "platformio", "6.2")
        self.assertEqual(self.functions["_pio_installed_version"](), "6.2")
        self.assertTrue(self.functions["_pio_upgrade"]())
        self.functions["_pip_upgrade"].assert_called_once_with("platformio")

    def test_installer_success_requires_fresh_version_verification(self):
        updates = [{"name": "websockets", "installed": "17.1", "latest": "17.2"}]
        namespace = self.functions
        namespace.update(_update_check_skip_reason=lambda: None, section=Mock(), status=Mock(),
            dim=Mock(), warn=lambda message: self.messages.append(("warning", message)),
            ok=lambda message: self.messages.append(("ok", message)), DIM="", _gui=None,
            _is_network_reachable=lambda **kwargs: True,
            _render_update_summary_block=lambda results, **kwargs: updates,
            check_python_update=lambda: {}, check_pio_update=lambda: {}, check_arduino_cli_update=lambda: {})
        self.distribution(self.target_site, "websockets", "17.1")
        # This fake installer intentionally does not update metadata: a zero
        # exit/result alone must not produce an 'upgraded' success message.
        namespace["run_update_checks"]()
        self.assertTrue(any("not verified" in message for _, message in self.messages))
        self.assertFalse(any("upgraded and verified" in message for _, message in self.messages))

    def arduino_cli_update_fixture(self, versions, *, msi_ok=True):
        """run_update_checks with an arduino-cli update whose executables report ``versions``."""
        source = ROOT / "src/modules/bootstrap.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        extra = {"_verified_arduino_cli_upgrade", "_arduino_cli_version_of"}
        namespace = self.functions
        exec(compile(ast.Module(body=[node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in extra], type_ignores=[]), str(source), "exec"), namespace)
        update = {"name": "arduino-cli", "installed": "1.4.0", "latest": "1.5.1"}
        runs = []
        cached = []

        def run(command, **_options):
            runs.append(list(command))
            return SimpleNamespace(stdout=f"arduino-cli  Version: {versions.get(command[0], '')} Commit: fixture\n")

        namespace.update(
            _update_check_skip_reason=lambda: None, section=Mock(), status=Mock(), dim=Mock(), DIM="", _gui=None,
            warn=lambda message: self.messages.append(("warning", message)),
            ok=lambda message: self.messages.append(("ok", message)),
            _is_network_reachable=lambda **kwargs: True,
            _render_update_summary_block=lambda results, **kwargs: [update],
            check_python_update=lambda: {}, check_pio_update=lambda: {}, check_arduino_cli_update=lambda: {},
            check_pip_package_update=lambda name, *args: {}, SCRIPT_DIR=self.root, CYAN="", RESET="",
            find_arduino_cli=lambda: "C:/old/arduino-cli.exe",
            _search_arduino_cli_install_dirs=lambda: "C:/Program Files/Arduino CLI/arduino-cli.exe",
            _refresh_bundled_msi=Mock(return_value=True), _run_arduino_cli_msi=Mock(return_value=msi_ok),
            _cache_arduino_cli_path=cached.append,
            subprocess=SimpleNamespace(run=run, CREATE_NO_WINDOW=0),
            shutil=SimpleNamespace(which=lambda name: None))
        with patch.object(sys, "platform", "win32"):
            namespace["run_update_checks"]()
        return runs, cached

    def test_arduino_cli_update_never_runs_core_upgrade_and_verifies_the_installed_cli(self):
        runs, cached = self.arduino_cli_update_fixture(
            {"C:/old/arduino-cli.exe": "1.4.0", "C:/Program Files/Arduino CLI/arduino-cli.exe": "1.5.1"})
        # `arduino-cli upgrade` upgrades cores and libraries, never the CLI.
        self.assertFalse([command for command in runs if "upgrade" in command], runs)
        self.assertEqual(cached, ["C:/Program Files/Arduino CLI/arduino-cli.exe"],
                         "The next update check must read the upgraded executable")
        self.assertTrue(any("upgraded and verified at 1.5.1" in message for _, message in self.messages))

    def test_arduino_cli_update_is_not_reported_when_the_version_stayed(self):
        runs, cached = self.arduino_cli_update_fixture(
            {"C:/old/arduino-cli.exe": "1.4.0", "C:/Program Files/Arduino CLI/arduino-cli.exe": "1.4.0"})
        self.assertFalse([command for command in runs if "upgrade" in command])
        self.assertEqual(cached, [])
        self.assertFalse(any("upgraded" in message for kind, message in self.messages if kind == "ok"))
        self.assertTrue(any("reports 1.4.0" in message for _, message in self.messages))

    def test_an_already_installed_newer_cli_is_adopted_instead_of_offered(self):
        source = ROOT / "src/modules/bootstrap.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        names = {"_verified_arduino_cli_upgrade", "_arduino_cli_version_of", "_arduino_cli_installed_version",
                 "check_arduino_cli_update", "_version_tuple"}
        cached = []
        versions = {"C:/old/arduino-cli.exe": "1.4.0", "C:/Program Files/Arduino CLI/arduino-cli.exe": "1.5.1"}
        namespace = dict(sys=sys, Path=Path,
            subprocess=SimpleNamespace(run=lambda command, **_: SimpleNamespace(
                stdout=f"arduino-cli  Version: {versions.get(command[0], '')} Commit: fixture\n"), CREATE_NO_WINDOW=0),
            shutil=SimpleNamespace(which=lambda name: None), find_arduino_cli=lambda: "C:/old/arduino-cli.exe",
            _search_arduino_cli_install_dirs=lambda: "C:/Program Files/Arduino CLI/arduino-cli.exe",
            _cache_arduino_cli_path=cached.append, _arduino_cli_latest_version=lambda: "1.5.1")
        exec(compile(ast.Module(body=[node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[]), str(source), "exec"), namespace)
        result = namespace["check_arduino_cli_update"]()
        self.assertFalse(result["update_available"], result)
        self.assertEqual(result["installed"], "1.5.1")
        self.assertEqual(cached, ["C:/Program Files/Arduino CLI/arduino-cli.exe"])
        versions["C:/Program Files/Arduino CLI/arduino-cli.exe"] = "1.4.0"
        cached.clear()
        self.assertTrue(namespace["check_arduino_cli_update"]()["update_available"])
        self.assertEqual(cached, [])

    def test_latest_version_probe_has_no_upgrade_side_effect(self):
        source = (ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        for name in ("_arduino_cli_latest_version", "run_update_checks", "check_arduino_cli_update"):
            node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
            calls = [ast.unparse(call) for call in ast.walk(node) if isinstance(call, ast.Call)]
            self.assertFalse([call for call in calls if "'upgrade'" in call], (name, calls))

    def test_no_updates_with_metadata_error_reports_incomplete(self):
        namespace = self.functions
        summary = Mock(return_value=[])
        namespace.update(_update_check_skip_reason=lambda: None, section=Mock(), status=Mock(),
            dim=Mock(), warn=lambda message: self.messages.append(("warning", message)),
            ok=lambda message: self.messages.append(("ok", message)), DIM="", _gui=None,
            _is_network_reachable=lambda **kwargs: True,
            _render_update_summary_block=summary,
            check_python_update=lambda: {}, check_pio_update=lambda: {}, check_arduino_cli_update=lambda: {},
            check_pip_package_update=lambda name, *args: {"name": name, "update_available": False,
                "error": "Conflicting websockets version metadata" if name == "websockets" else None})
        self.assertEqual(namespace["run_update_checks"](), "incomplete")
        self.assertTrue(summary.call_args.args[0]["websockets"]["error"])
        self.assertTrue(any("incomplete for websockets" in message for _, message in self.messages))
        self.assertFalse(any("All utilities are up to date" in message for _, message in self.messages))
        namespace["_pip_upgrade"].assert_not_called()

    def update_stage_fixture(self, gui, *, skip=None, online=False):
        summary = Mock(return_value=[])
        reachable = Mock(return_value=online)
        checks = Mock(return_value={"update_available": False, "error": None})
        namespace = self.functions
        namespace.update(_gui=gui, _update_check_skip_reason=lambda: skip,
                         section=Mock(), status=Mock(), dim=Mock(), warn=Mock(), ok=Mock(), DIM="",
                         _is_network_reachable=reachable, _render_update_summary_block=summary,
                         check_python_update=checks, check_pio_update=checks,
                         check_arduino_cli_update=checks, check_pip_package_update=checks)
        return namespace, reachable, checks, summary

    def test_gui_update_stage_is_top_level_once_for_enabled_skipped_and_offline(self):
        for skip, online, expected in (("Skip Updates enabled", False, "skipped"),
                                       (None, False, "offline"), (None, True, "up_to_date")):
            with self.subTest(result=expected):
                callbacks = []
                gui = SimpleNamespace(log_section=Mock(), log_subsection=Mock(),
                                      root=SimpleNamespace(after=lambda delay, callback, *args:
                                                           callbacks.append((delay, callback, args))))
                namespace, reachable, checks, summary = self.update_stage_fixture(gui, skip=skip, online=online)
                self.assertEqual(namespace["run_update_checks"](), expected)
                self.assertEqual(len(callbacks), 1)
                self.assertEqual(callbacks[0][0], 0)
                gui.log_section.assert_not_called()
                callbacks[0][1](*callbacks[0][2])
                gui.log_section.assert_called_once_with("Checking for updates")
                gui.log_subsection.assert_not_called()
                namespace["section"].assert_not_called()
                if skip:
                    reachable.assert_not_called()
                    checks.assert_not_called()
                elif not online:
                    reachable.assert_called_once_with(timeout=2.0)
                    checks.assert_not_called()
                summary.assert_called_once()
                namespace["_pip_upgrade"].assert_not_called()

    def test_console_update_stage_has_one_heading_even_when_skipped(self):
        namespace, reachable, checks, _summary = self.update_stage_fixture(None, skip="Skip Updates enabled")
        self.assertEqual(namespace["run_update_checks"](), "skipped")
        namespace["section"].assert_called_once_with("Checking for updates")
        reachable.assert_not_called()
        checks.assert_not_called()

    def test_skip_during_slow_checks_returns_without_prompt_or_install(self):
        namespace, _, checks, _ = self.update_stage_fixture(None, online=True)
        skipped, release, started = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        namespace["_update_check_skip_reason"] = lambda: "live checkbox" if skipped.is_set() else None

        def slow_check():
            started.set()
            release.wait(3)
            return {"update_available": True}

        checks.side_effect = slow_check
        result = []
        worker = threading.Thread(target=lambda: result.append(namespace["run_update_checks"]()))
        worker.start()
        self.assertTrue(started.wait(1))
        skipped.set()
        worker.join(1)
        self.assertFalse(worker.is_alive(), "Skip waited for stalled read-only network work")
        self.assertEqual(result, ["skipped"])
        namespace["_pip_upgrade"].assert_not_called()

    def test_found_updates_install_without_asking(self):
        # The GUI has no question to ask any more: an ask_update call would fail here.
        gui = SimpleNamespace(log_section=Mock(), root=SimpleNamespace(after=Mock()))
        namespace, _, _, summary = self.update_stage_fixture(gui, online=True)
        summary.return_value = [{"name": "websockets", "installed": "17.1", "latest": "17.2"}]
        namespace["_pip_installed_version"] = Mock(return_value="17.2")
        namespace["_version_tuple"] = lambda value: tuple(int(part) for part in value.split("."))
        self.assertEqual(namespace["run_update_checks"](), "updates_available")
        namespace["_pip_upgrade"].assert_called_once_with("websockets")
        namespace["ok"].assert_called_once_with("websockets upgraded and verified at 17.2")

    def test_skip_chosen_during_installs_stops_the_remaining_ones(self):
        gui = SimpleNamespace(log_section=Mock(), root=SimpleNamespace(after=Mock()))
        namespace, _, _, summary = self.update_stage_fixture(gui, online=True)
        skipped = [False]
        namespace["_update_check_skip_reason"] = lambda: "live checkbox" if skipped[0] else None
        summary.return_value = [{"name": "websockets", "installed": "17.1", "latest": "17.2"},
                                {"name": "psutil", "installed": "7.0", "latest": "7.1"}]
        namespace["_pip_installed_version"] = Mock(return_value=None)

        def upgrade(_name):
            skipped[0] = True
            return True

        namespace["_pip_upgrade"].side_effect = upgrade
        self.assertEqual(namespace["run_update_checks"](), "skipped")
        namespace["_pip_upgrade"].assert_called_once_with("websockets")

    def test_updates_default_on_and_no_view_can_ask_first(self):
        bootstrap = (ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig")
        view = (ROOT / "src/modules/bootstrap_qt.py").read_text(encoding="utf-8-sig")
        self.assertIn("DEFAULT_SKIP_UPDATES = False", bootstrap)
        for source in (bootstrap, view):
            self.assertNotIn("Would you like to install them now?", source)
            self.assertNotIn("def ask_update", source)

    def test_update_target_display_expands_alias_without_changing_installer_target(self):
        namespace, _reachable, checks, _summary = self.update_stage_fixture(None, online=False)
        target = Mock()
        target.resolve.return_value = self.root / "readable project name/env/Scripts/python.exe"
        namespace["_get_target_python"] = Mock(return_value=target)
        self.assertEqual(namespace["run_update_checks"](), "offline")
        namespace["dim"].assert_any_call(f"Update target: {target.resolve.return_value}")
        namespace["_get_target_python"].assert_called_once_with()
        target.resolve.assert_called_once_with()
        checks.assert_not_called()
        namespace["_pip_upgrade"].assert_not_called()

    def test_update_target_display_resolution_failure_does_not_block_checks(self):
        namespace, _reachable, _checks, _summary = self.update_stage_fixture(None, online=False)
        target = Mock()
        target.resolve.side_effect = OSError("Drive temporarily unavailable")
        namespace["_get_target_python"] = Mock(return_value=target)
        self.assertEqual(namespace["run_update_checks"](), "offline")
        namespace["dim"].assert_any_call(f"Update target: {target}")

    def test_opencode_is_never_checked_or_updated(self):
        namespace, _reachable, _checks, summary = self.update_stage_fixture(None, online=True)
        namespace["check_opencode_update"] = Mock(side_effect=AssertionError("OpenCode update check"))
        namespace["_upgrade_opencode"] = Mock(side_effect=AssertionError("OpenCode upgrade"))
        self.assertEqual(namespace["run_update_checks"](), "up_to_date")
        self.assertNotIn("opencode", summary.call_args.args[0])
        source = (ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig")
        for removed in ("def check_opencode_update", "def _upgrade_opencode"):
            self.assertNotIn(removed, source)


sys.path.insert(0, str(ROOT / "src/modules"))
import opencode_npm  # noqa: E402

# opencode-ai's bin/opencode.exe before its postinstall ran: a shell script under 1 KB.
PLACEHOLDER = b'echo "Error: opencode-ai\'s postinstall script was not run." >&2\nexit 1\n'
EXECUTABLE = b"MZ" + bytes(4096)


def npm_shims(directory, package="opencode-ai"):
    """Write the three shims npm makes for a package whose bin is bin/opencode.exe."""
    windows = "node_modules\\" + package.replace("/", "\\") + "\\bin\\opencode.exe"
    posix = windows.replace("\\", "/")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "opencode.cmd").write_text(
        "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\nSETLOCAL\r\n"
        f'CALL :find_dp0\r\n"%dp0%\\{windows}"   %*\r\n', encoding="utf-8")
    (directory / "opencode").write_text(
        '#!/bin/sh\nbasedir=$(dirname "$(echo "$0" | sed -e \'s,\\\\,/,g\')")\n\n'
        f'exec "$basedir/{posix}"   "$@"\n', encoding="utf-8")
    (directory / "opencode.ps1").write_text(
        "#!/usr/bin/env pwsh\n$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent\n"
        f'if ($MyInvocation.ExpectingInput) {{\n  $input | & "$basedir/{posix}"   $args\n}}\n'
        "exit $LASTEXITCODE\n", encoding="utf-8")
    target = directory.joinpath("node_modules", *package.split("/"), "bin", "opencode.exe")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def bootstrap_functions(names, **values):
    source = ROOT / "src/modules/bootstrap.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    namespace = dict(Path=Path, os=os, **values)
    exec(compile(ast.Module(body=[node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[]),
        str(source), "exec"), namespace)
    return namespace


class OpenCodeDamageChecks(unittest.TestCase):
    """A damaged npm OpenCode is recognised by its files alone; nothing is run."""

    def setUp(self):
        (ROOT / "temp/audit").mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(prefix="opencode-damage-", dir=ROOT / "temp/audit")
        self.addCleanup(fixture.cleanup)
        self.npm = Path(fixture.name) / "npm"

    def test_every_shim_format_names_its_package_executable(self):
        target = npm_shims(self.npm)
        for name in opencode_npm.SHIM_NAMES:
            self.assertEqual(opencode_npm.shim_target(self.npm / name), target, name)

    def test_placeholder_or_missing_executable_is_damage(self):
        target = npm_shims(self.npm)
        self.assertEqual(opencode_npm.find_damaged([self.npm]), (self.npm, self.npm / "opencode.cmd"))
        target.write_bytes(PLACEHOLDER)
        self.assertEqual(opencode_npm.find_damaged([self.npm]), (self.npm, self.npm / "opencode.cmd"))
        self.assertFalse(opencode_npm.shim_usable(self.npm / "opencode.cmd"))

    def test_real_executable_is_kept(self):
        npm_shims(self.npm).write_bytes(EXECUTABLE)
        self.assertIsNone(opencode_npm.find_damaged([self.npm]))
        self.assertTrue(all(opencode_npm.shim_usable(self.npm / name) for name in opencode_npm.SHIM_NAMES))

    def test_rerun_postinstall_counts_before_any_repair(self):
        target = npm_shims(self.npm)
        target.write_bytes(PLACEHOLDER)
        healed = []

        def heal(directory):
            healed.append(directory)
            target.write_bytes(EXECUTABLE)
            return True

        self.assertIsNone(opencode_npm.find_damaged([self.npm], heal=heal))
        self.assertEqual(healed, [target.parent])

    def test_half_removed_package_without_shims_is_damage(self):
        package = self.npm / "node_modules/opencode-ai"
        package.mkdir(parents=True)
        (package / "package.json").write_text("{}", encoding="utf-8")
        self.assertEqual(opencode_npm.find_damaged([self.npm]), (self.npm, package))

    def test_wrong_package_is_judged_by_its_own_executable(self):
        npm_shims(self.npm, "@opencode/cli").write_bytes(PLACEHOLDER)
        self.assertEqual(opencode_npm.shim_target(self.npm / "opencode.cmd"),
                         self.npm / "node_modules/@opencode/cli/bin/opencode.exe")
        self.assertIsNotNone(opencode_npm.find_damaged([self.npm]))

    def test_other_launchers_are_not_npm_shims(self):
        self.npm.mkdir(parents=True)
        launcher = self.npm / "opencode.cmd"
        launcher.write_text('@echo off\r\n"C:\\Tools\\opencode\\opencode.exe" %*\r\n', encoding="utf-8")
        self.assertIsNone(opencode_npm.shim_target(launcher))
        self.assertTrue(opencode_npm.shim_usable(launcher))
        self.assertIsNone(opencode_npm.find_damaged([self.npm, self.npm / "absent"]))

    def test_leftover_shims_are_removed_but_folders_and_other_tools_stay(self):
        npm_shims(self.npm)
        (self.npm / "opencode2.cmd").write_text("@echo off\r\n", encoding="utf-8")
        (self.npm / "npm.cmd").write_text("@echo off\r\n", encoding="utf-8")
        (self.npm / "opencode-data").mkdir()
        removed = opencode_npm.remove_shims([self.npm, self.npm])
        self.assertEqual(sorted(path.name for path in removed),
                         ["opencode", "opencode.cmd", "opencode.ps1", "opencode2.cmd"])
        self.assertTrue((self.npm / "npm.cmd").is_file())
        self.assertTrue((self.npm / "opencode-data").is_dir())
        self.assertTrue((self.npm / "node_modules/opencode-ai/bin").is_dir())

    def test_bootstrap_skips_a_broken_shim_for_a_working_executable(self):
        npm_shims(self.npm).write_bytes(PLACEHOLDER)
        working = self.npm.parent / "tools/opencode.exe"
        working.parent.mkdir(parents=True)
        working.write_bytes(EXECUTABLE)
        found = {"opencode.cmd": str(self.npm / "opencode.cmd"), "opencode.exe": str(working)}
        namespace = bootstrap_functions(
            {"check_opencode_cli", "_opencode_shim_usable"},
            sys=SimpleNamespace(platform="linux"), find_running_opencode_exe=lambda: None,
            shutil=SimpleNamespace(which=found.get), _is_valid_pe_binary=opencode_npm.is_valid_pe_binary,
            _heal_opencode_postinstall=lambda directory: False)
        self.assertEqual(namespace["check_opencode_cli"](), str(working))
        del found["opencode.exe"]
        self.assertIsNone(namespace["check_opencode_cli"]())

    def test_failed_postinstall_rerun_is_not_repeated(self):
        package = self.npm / "node_modules/opencode-ai"
        (package / "bin").mkdir(parents=True)
        (package / "postinstall.mjs").write_text("", encoding="utf-8")
        run = Mock(return_value=SimpleNamespace(returncode=1))
        namespace = bootstrap_functions(
            {"_heal_opencode_postinstall"}, _FAILED_OPENCODE_HEALS=set(),
            shutil=SimpleNamespace(which=lambda name: "node"), _hidden_subprocess_flags=lambda: 0,
            subprocess=SimpleNamespace(run=run, DEVNULL=-3, PIPE=-1, STDOUT=-2))
        self.assertFalse(namespace["_heal_opencode_postinstall"](package / "bin"))
        self.assertFalse(namespace["_heal_opencode_postinstall"](package / "bin"))
        run.assert_called_once()

    def test_npm_runs_with_scripts_allowed_and_a_hung_npm_is_stopped(self):
        with patch.dict(os.environ, {"npm_config_ignore_scripts": "true"}):
            result = opencode_npm.run_npm(
                [sys.executable, "-c", "import os; print(os.environ.get('npm_config_ignore_scripts'))"], timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip(), result.timed_out), (0, "None", False))
        result = opencode_npm.run_npm([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, 1)


class OpenCodeConfigChecks(unittest.TestCase):
    """The assistant turns OpenCode's autoupdate off without replacing settings it cannot read."""

    def test_unreadable_settings_are_left_untouched(self):
        (ROOT / "temp/audit").mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(prefix="opencode-config-", dir=ROOT / "temp/audit")
        self.addCleanup(fixture.cleanup)
        home = Path(fixture.name)
        jsonc = home / ".config/opencode/config.json"
        jsonc.parent.mkdir(parents=True)
        text = '{\n  // providers configured by hand\n  "model": "fixture/model",\n}\n'
        jsonc.write_text(text, encoding="utf-8")
        plain = home / ".opencode/config.json"
        plain.parent.mkdir(parents=True)
        plain.write_text('\ufeff{"theme": "fixture"}', encoding="utf-8")
        source = ROOT / "src/modules/dedicated_AI.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        namespace = dict(Path=Path, json=json)
        exec(compile(ast.Module(body=[node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_ensure_opencode_config"],
            type_ignores=[]), str(source), "exec"), namespace)
        with patch.object(Path, "home", return_value=home):
            namespace["_ensure_opencode_config"]()
        self.assertEqual(jsonc.read_text(encoding="utf-8"), text)
        self.assertEqual(json.loads(plain.read_text(encoding="utf-8")), {"theme": "fixture", "autoupdate": False})


class OpenCodeInstallChecks(unittest.TestCase):
    """Windows Bootstrap installs OpenCode once, repairs only a damaged one and never touches a running one."""

    NPM = "C:/nodejs/npm.cmd"

    def setUp(self):
        (ROOT / "temp/audit").mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(prefix="opencode-install-", dir=ROOT / "temp/audit")
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.npm_dir = self.root / "npm"
        self.commands = []
        self.timeouts = []
        self.installed = False
        self.returncode = 0
        self.timed_out = False
        self.messages = []
        import subprocess as real_subprocess
        self.namespace = bootstrap_functions(
            {"_existing_opencode_installation", "ensure_opencode_cli", "is_any_opencode_process_running",
             "_damaged_opencode_installation", "_opencode_install_command", "_repair_damaged_opencode",
             "_opencode_version"},
            sys=SimpleNamespace(platform="win32"), SCRIPT_DIR=self.root, _gui=None,
            section=Mock(), status=Mock(),
            warn=lambda message: self.messages.append(("warn", message)),
            ok=lambda message: self.messages.append(("ok", message)),
            fail=lambda message: self.messages.append(("fail", message)),
            find_running_opencode_exe=Mock(return_value=None), _opencode_image_running=Mock(return_value=False),
            check_opencode_cli=lambda: "C:/npm/opencode.cmd" if self.installed else None,
            _find_usable_npm_cmd=Mock(return_value=self.NPM), _install_nodejs_lts=Mock(),
            _refresh_node_npm_path=Mock(), _hidden_subprocess_flags=lambda: 0,
            _windows_node_dirs=lambda: [self.npm_dir], _FAILED_OPENCODE_HEALS=set(),
            _OPENCODE_INSTALL_TIMEOUT=1200,
            _is_valid_pe_binary=lambda path: False, _heal_opencode_postinstall=lambda path: False,
            _summarize_process_output=lambda result: "",
            shutil=SimpleNamespace(which=lambda name: None),
            subprocess=SimpleNamespace(run=Mock(side_effect=AssertionError("version probe")),
                                       CompletedProcess=real_subprocess.CompletedProcess,
                                       TimeoutExpired=real_subprocess.TimeoutExpired, PIPE=-1, STDOUT=-2,
                                       DEVNULL=-3, list2cmdline=real_subprocess.list2cmdline))
        # psutil may be absent in the runtime that runs Bootstrap.
        self.namespace["is_any_opencode_process_running"] = Mock(return_value=False)
        self.run_npm = self.fake_run_npm
        runner = patch.object(opencode_npm, "run_npm", lambda command, timeout: self.run_npm(command, timeout))
        runner.start()
        self.addCleanup(runner.stop)
        environment = patch.dict(os.environ, {"APPDATA": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def fake_run_npm(self, command, timeout):
        self.commands.append(list(command))
        self.timeouts.append(timeout)
        if command[1] == "install" and not self.returncode:
            self.installed = True
        result = subprocess.CompletedProcess(list(command), self.returncode, "")
        result.timed_out = self.timed_out
        return result

    def damage(self):
        target = npm_shims(self.npm_dir)
        target.write_bytes(PLACEHOLDER)
        return target

    def test_installed_opencode_is_kept_even_when_it_would_not_answer(self):
        self.installed = True
        self.assertTrue(self.namespace["ensure_opencode_cli"]())
        self.assertEqual(self.commands, [])
        self.namespace["_find_usable_npm_cmd"].assert_not_called()
        self.namespace["subprocess"].run.assert_not_called()
        self.assertIn("keeps it as it is", self.messages[-1][1])

    def test_opencode_running_in_a_terminal_counts_as_installed(self):
        self.namespace["is_any_opencode_process_running"].return_value = True
        self.assertTrue(self.namespace["ensure_opencode_cli"]())
        self.assertEqual(self.commands, [])
        self.namespace["_find_usable_npm_cmd"].assert_not_called()

    def test_running_detection_does_not_need_psutil(self):
        detect = self.namespace["_opencode_image_running"]
        exec(compile(ast.Module(body=[node for node in ast.parse(
            (ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig")).body
            if isinstance(node, ast.FunctionDef) and node.name == "is_any_opencode_process_running"],
            type_ignores=[]), "bootstrap.py", "exec"), self.namespace)
        detect.return_value = True
        with patch.dict(sys.modules, {"psutil": None}):
            self.assertTrue(self.namespace["is_any_opencode_process_running"]())
        detect.assert_called_once_with()

    def test_missing_opencode_is_installed_once_without_uninstall(self):
        self.assertTrue(self.namespace["ensure_opencode_cli"]())
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0][:3], [self.NPM, "install", "-g"])
        self.assertEqual(self.commands[0][-1], "opencode-ai")
        self.assertIn("--ignore-scripts=false", self.commands[0])
        self.assertGreaterEqual(self.timeouts[0], 600)

    def test_stopped_install_is_not_followed_by_a_second_npm(self):
        self.returncode, self.timed_out = 1, True
        self.assertFalse(self.namespace["ensure_opencode_cli"]())
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.messages[-1][0], "fail")

    def test_refused_install_is_retried_once_in_the_user_prefix(self):
        self.returncode = 1
        self.assertFalse(self.namespace["ensure_opencode_cli"]())
        self.assertEqual(len(self.commands), 2)
        self.assertIn(f"--prefix={self.root / 'npm'}", self.commands[1])

    def test_damaged_opencode_is_repaired_with_the_documented_steps(self):
        shims_during_cache_verify = []

        def run(command, timeout):
            if command[1:3] == ["cache", "verify"]:
                shims_during_cache_verify.extend(path.name for path in self.npm_dir.glob("opencode*"))
            return self.fake_run_npm(command, timeout)

        self.run_npm = run
        self.damage()
        version = self.namespace["subprocess"].run
        version.side_effect = None
        version.return_value = subprocess.CompletedProcess([], 0, "1.19.6\n")
        self.assertTrue(self.namespace["ensure_opencode_cli"]())
        self.assertEqual(self.commands[:3], [[self.NPM, "uninstall", "-g", "@opencode/cli"],
                                             [self.NPM, "uninstall", "-g", "opencode-ai"],
                                             [self.NPM, "cache", "verify"]])
        install = self.commands[3]
        self.assertEqual((install[:3], install[-1]), ([self.NPM, "install", "-g"], "opencode-ai@latest"))
        self.assertIn("--ignore-scripts=false", install)
        self.assertFalse({"--ignore-scripts", "--ignore-scripts=true"} & set(install))
        self.assertEqual(len(self.commands), 4)
        self.assertEqual(shims_during_cache_verify, [])
        self.assertEqual(version.call_args.args[0][-1], "--version")
        self.assertEqual(self.messages[-1][0], "ok")
        self.assertIn("1.19.6", self.messages[-1][1])

    def test_damaged_but_running_opencode_is_left_alone(self):
        target = self.damage()
        self.namespace["is_any_opencode_process_running"].return_value = True
        self.assertTrue(self.namespace["ensure_opencode_cli"]())
        self.assertEqual(self.commands, [])
        self.assertTrue((self.npm_dir / "opencode.cmd").is_file())
        self.assertEqual(target.read_bytes(), PLACEHOLDER)

    def test_failed_repair_install_is_reported(self):
        self.damage()
        self.run_npm = lambda command, timeout: (
            setattr(self, "returncode", 1 if command[1] == "install" else 0),
            self.fake_run_npm(command, timeout))[1]
        self.assertFalse(self.namespace["ensure_opencode_cli"]())
        self.assertEqual(len(self.commands), 4)
        self.assertEqual(self.messages[-1][0], "fail")

if __name__ == "__main__":
    unittest.main(verbosity=2)
