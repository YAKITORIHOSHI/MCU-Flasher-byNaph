#!/usr/bin/env python3
"""Isolated update-environment checks; no setup imports, installs or network."""
import ast
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules.bootstrap_updates import installed_version, site_packages


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
        namespace["run_update_checks"](auto_update=True)
        self.assertTrue(any("not verified" in message for _, message in self.messages))
        self.assertFalse(any("upgraded and verified" in message for _, message in self.messages))

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
        self.assertEqual(namespace["run_update_checks"](auto_update=True), "incomplete")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
