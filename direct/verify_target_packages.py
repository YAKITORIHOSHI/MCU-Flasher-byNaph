#!/usr/bin/env python3
"""Quick Bootstrap and first-compile board tools, in owned fixtures; never downloads.

Bootstrap prepares SCons and the board list; a board's own build/upload packages
are prepared the first time it is compiled (src/modules/target_packages.py).
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import offline_bootstrap, target_packages


class TargetStampChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        scratch = ROOT / "temp/audit/target-packages"
        scratch.mkdir(parents=True, exist_ok=True)
        self.core = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=scratch)))

    def publish(self, *, version="5.3.0", protocol=""):
        platform = self.core / "platforms/atmelavr"
        (platform / "boards").mkdir(parents=True)
        (platform / "platform.json").write_text(json.dumps({"name": "atmelavr", "version": version}))
        (platform / "boards/uno.json").write_text("{}")
        tool = self.core / "packages/toolchain-atmelavr"
        tool.mkdir(parents=True)
        for name in ("package.json", ".piopm"):
            (tool / name).write_text("{}")
        stamp = target_packages.stamp_path(self.core, "atmelavr", "uno", "arduino", protocol)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps({
            "schema": target_packages.SCHEMA, "host": sys.platform,
            "target": target_packages._key("atmelavr", "uno", "arduino", protocol),
            "platform_manifest": "platforms/atmelavr/platform.json", "platform_version": "5.3.0",
            "files": ["platforms/atmelavr/platform.json", "platforms/atmelavr/boards/uno.json",
                      "packages/toolchain-atmelavr/package.json", "packages/toolchain-atmelavr/.piopm"]}))
        return tool

    def test_unprepared_target_is_not_ready(self):
        self.assertFalse(target_packages.target_ready(self.core, "atmelavr", "uno", "arduino"))

    def test_prepared_target_is_ready_in_a_few_stat_calls(self):
        self.publish()
        self.assertTrue(target_packages.target_ready(self.core, "atmelavr", "uno", "arduino"))
        # Framework spelling and surrounding spaces never force another preparation.
        self.assertTrue(target_packages.target_ready(self.core, " atmelavr", "uno ", " arduino "))
        self.assertFalse(target_packages.target_ready(self.core, "atmelavr", "nano", "arduino"))
        self.assertFalse(target_packages.target_ready(self.core, "atmelavr", "uno", "arduino", "usbasp"))

    def test_removed_package_or_changed_platform_prepares_again(self):
        tool = self.publish()
        (tool / ".piopm").unlink()
        self.assertFalse(target_packages.target_ready(self.core, "atmelavr", "uno", "arduino"))
        (tool / ".piopm").write_text("{}")
        self.assertTrue(target_packages.target_ready(self.core, "atmelavr", "uno", "arduino"))
        (self.core / "platforms/atmelavr/platform.json").write_text(json.dumps({"version": "5.4.0"}))
        self.assertFalse(target_packages.target_ready(self.core, "atmelavr", "uno", "arduino"))

    def test_stamp_cannot_point_outside_the_store(self):
        self.publish()
        stamp = target_packages.stamp_path(self.core, "atmelavr", "uno", "arduino")
        data = json.loads(stamp.read_text())
        data["files"].append("../outside.json")
        stamp.write_text(json.dumps(data))
        (self.core.parent / "outside.json").write_text("{}")
        self.addCleanup(lambda: (self.core.parent / "outside.json").unlink(missing_ok=True))
        self.assertFalse(target_packages.target_ready(self.core, "atmelavr", "uno", "arduino"))

    def test_missing_platform_is_reported_without_installing(self):
        config = self.core / "platformio.ini"
        config.write_text(target_packages.configuration_text("atmelavr", "uno", "arduino"))
        from platformio.package.manager.platform import PlatformPackageManager
        with patch.object(PlatformPackageManager, "install", side_effect=AssertionError("Downloaded")):
            with self.assertRaises(target_packages.MissingPackage):
                target_packages.prepare(config, self.core, install=False)

    def test_child_refuses_the_workspace_runtime(self):
        with patch.dict(os.environ, {"MCU_FLASHER_WORKSPACE_RUNTIME": "1"}):
            self.assertEqual(target_packages.main(["--core", str(self.core), "--config", str(self.core / "x.ini")]), 2)


class QuickBootstrapChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        scratch = ROOT / "temp/audit/target-packages"
        scratch.mkdir(parents=True, exist_ok=True)
        self.core = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=scratch)))

    def test_bootstrap_downloads_every_toolchain_only_for_offline_mode_or_an_explicit_plan(self):
        cases = ((True, False, None, ["--coverage-only"]),
                 (False, True, None, []),
                 (False, False, "plan.json", []),
                 (False, False, None, ["--catalog-only"]))
        for certified, offline, plan, expected in cases:
            with self.subTest(certified=certified, offline=offline, plan=plan), \
                    patch.object(offline_bootstrap, "ready", return_value=certified), \
                    patch.object(offline_bootstrap, "load_plan", return_value={"schema": 1, "platforms": ["x"]}), \
                    patch("src.modules.offline_mode.offline_enabled", return_value=offline):
                self.assertEqual(offline_bootstrap.board_stage_arguments(self.core, plan), expected)

    def test_quick_store_needs_its_marker_files_and_this_host(self):
        self.assertFalse(offline_bootstrap.catalog_ready(self.core))
        (self.core / "platforms/atmelavr").mkdir(parents=True)
        (self.core / "platforms/atmelavr/platform.json").write_text("{}")
        marker = {"schema": offline_bootstrap.CATALOG_SCHEMA, "host": sys.platform,
                  "architecture": offline_bootstrap.host_platform.machine(),
                  "plan": offline_bootstrap.plan_hash(offline_bootstrap.load_plan()),
                  "files": ["platforms/atmelavr/platform.json"]}
        (self.core / offline_bootstrap.CATALOG_MARKER).write_text(json.dumps(marker))
        with patch.object(offline_bootstrap, "_store_metadata_readable", return_value=True):
            self.assertTrue(offline_bootstrap.catalog_ready(self.core))
            for key, value in (("host", "other"), ("architecture", "other"), ("plan", "0" * 64)):
                with self.subTest(key=key):
                    (self.core / offline_bootstrap.CATALOG_MARKER).write_text(json.dumps(dict(marker, **{key: value})))
                    self.assertFalse(offline_bootstrap.catalog_ready(self.core))
            (self.core / offline_bootstrap.CATALOG_MARKER).write_text(json.dumps(marker))
            (self.core / "platforms/atmelavr/platform.json").unlink()
            self.assertFalse(offline_bootstrap.catalog_ready(self.core))

    def test_offline_mode_still_requires_the_complete_certificate(self):
        with patch.object(offline_bootstrap, "ready", return_value=False), \
                patch.object(offline_bootstrap, "catalog_ready", return_value=True):
            with patch("src.modules.offline_mode.offline_enabled", return_value=False):
                self.assertTrue(offline_bootstrap.board_store_ready(self.core))
            with patch("src.modules.offline_mode.offline_enabled", return_value=True):
                self.assertFalse(offline_bootstrap.board_store_ready(self.core))

    def test_setup_lets_the_private_runtime_choose_the_mode(self):
        # System Python cannot import PlatformIO, so it must never decide readiness itself.
        for path in ("direct/ubuntu/setup.py", "direct/macos/setup.py"):
            text = (ROOT / path).read_text(encoding="utf-8")
            self.assertIn('command += ["--auto"]', text, path)
            self.assertNotIn("ready(core, load_plan", text, path)


@unittest.skipIf(sys.platform == "win32", "Windows keeps Bootstrap's complete preparation")
class FirstCompileChecks(unittest.TestCase):
    """The workspace prepares a board's tools before its build takes the package lease."""

    @classmethod
    def setUpClass(cls):
        from main import web_bridge
        cls.bridge = web_bridge

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        scratch = ROOT / "temp/audit/target-packages"
        scratch.mkdir(parents=True, exist_ok=True)
        self.core = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=scratch)))
        self.events = []
        self.info = {"platform": "atmelavr", "board": "uno", "framework": "arduino"}
        self.stack.enter_context(patch.object(self.bridge, "_get_safe_platformio_core_dir", return_value=str(self.core)))
        self.stack.enter_context(patch("src.modules.offline_mode.offline_enabled", return_value=False))
        self.stack.enter_context(patch.object(self.bridge.sys, "platform", "linux"))

    def backend(self, info=None):
        return SimpleNamespace(current_board="Arduino Uno", _stop_requested=False, upload_speed="",
                               _active_process=None, _current_op_phase=None,
                               _resolve_requested_target=lambda action: True,
                               _resolve_board_info=lambda name=None: dict(info or self.info),
                               emit=lambda name, data: self.events.append((name, data)))

    def prepare(self, backend):
        return self.bridge.MCUWebBackendAPI._prepare_target_tools(backend, "Compile")

    def test_prepared_or_foreign_targets_start_no_process(self):
        with patch.object(self.bridge.subprocess, "Popen", side_effect=AssertionError("Preparation started")):
            with patch("src.modules.target_packages.target_ready", return_value=True):
                self.assertTrue(self.prepare(self.backend()))
            self.assertTrue(self.prepare(self.backend({"backend": "arduino-cli", "board": "x", "platform": "y"})))
            self.assertTrue(self.prepare(self.backend({"platform": "", "board": ""})))
            with patch("src.modules.offline_mode.offline_enabled", return_value=True):
                self.assertTrue(self.prepare(self.backend()))
            with patch.object(self.bridge.sys, "platform", "win32"):
                self.assertTrue(self.prepare(self.backend()))

    def test_unprepared_target_runs_one_clean_preparation_process(self):
        started = []

        class Process:
            returncode = 0
            pid = 4321

            def __init__(self, command, **options):
                started.append((command, options))
                config = Path(command[command.index("--config") + 1])
                self.config_text = config.read_text(encoding="utf-8")
                self.stdout = iter(["Installing platformio/toolchain-atmelavr...\n", "Board tools are ready.\n", ""])
                self.stdout = SimpleNamespace(readline=lambda lines=self.stdout: next(lines))

            def wait(self):
                return 0

            def poll(self):
                return 0

        workspace = {"MCU_FLASHER_WORKSPACE_RUNTIME": "1", "MCU_FLASHER_OFFLINE_RUNTIME": "1", "PIP_NO_INDEX": "1"}
        with patch.dict(os.environ, workspace), patch.object(self.bridge.subprocess, "Popen", Process):
            backend = self.backend()
            self.assertTrue(self.prepare(backend))
        self.assertEqual(len(started), 1)
        command, options = started[0]
        self.assertEqual(command[2], str(self.bridge.SCRIPT_DIR / "src/modules/target_packages.py"))
        self.assertEqual(command[command.index("--core") + 1], str(self.core))
        for name in workspace:
            self.assertNotIn(name, options["env"])
        self.assertTrue(options.get("start_new_session"))
        self.assertEqual(list((self.core / ".tmp").glob("target-*.ini")), [])  # Config removed afterwards.
        logged = [data["text"] for name, data in self.events if name == "console:log"]
        self.assertTrue(any("Installing platformio/toolchain-atmelavr" in text for text in logged))
        self.assertIsNone(backend._active_process)

    def test_failed_preparation_stops_before_the_build(self):
        class Failed:
            returncode = 1
            pid = 1

            def __init__(self, command, **options):
                self.stdout = SimpleNamespace(readline=iter(["Board tool preparation failed: offline\n", ""]).__next__)

            def wait(self):
                return 1

        with patch.object(self.bridge.subprocess, "Popen", Failed):
            self.assertFalse(self.prepare(self.backend()))
        self.assertTrue(any(name == "notification" for name, _ in self.events))

    def test_compile_and_upload_threads_prepare_before_the_guarded_workers(self):
        api = self.bridge.MCUWebBackendAPI
        self.assertTrue(hasattr(api._compile_requested_worker, "__wrapped__"))
        self.assertTrue(hasattr(api._upload_requested_worker, "__wrapped__"))
        self.assertFalse(hasattr(api._compile_requested_entry, "__wrapped__"))
        self.assertFalse(hasattr(api._upload_requested_entry, "__wrapped__"))
        source = (ROOT / "main/web_bridge.py").read_text(encoding="utf-8")
        self.assertIn("target=self._compile_requested_entry", source)
        self.assertIn("target=self._upload_requested_entry", source)


if __name__ == "__main__":
    unittest.main()
