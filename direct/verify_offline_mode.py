#!/usr/bin/env python3
"""Verify offline mode transitions without live settings, installers or packages."""
from __future__ import annotations
import copy
import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import offline_mode as mode, offline_runtime as runtime, offline_bootstrap as setup
from src.modules import bootstrap_builders


class ModeChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/offline-mode"
        audit.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.core = self.root / "core"
        self.core.mkdir()

    def online_inputs(self):
        for name in ("packages/tool-scons/package.json", "packages/tool-scons/.piopm", "packages/tool-scons/scons.py"):
            target = self.core / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture")

    def record(self):
        (self.core / setup.MARKER).write_text(json.dumps({"fixture": "prepared certificate"}))
        mode.record_preparation(self.core, root=self.root, host="win32")
        return mode.extras_directory(self.root, "win32")

    def test_offline_is_explicitly_opt_in_and_pending_blocks_startup(self):
        self.online_inputs()
        with patch.object(mode, "ROOT", self.root), patch.object(setup, "ASSETS", ()):
            self.assertFalse(mode.offline_enabled({}))
            self.assertFalse(mode.offline_enabled({"shared": {"offline_enabled": "true"}}))
            self.assertTrue(mode.startup_ready(self.core, config={}))
            self.assertFalse(mode.startup_ready(self.core, config={"shared": {"offline_preparation_pending": True}}))
            with patch.object(setup, "ready", return_value=False) as ready:
                self.assertFalse(mode.startup_ready(self.core, config={"shared": {"offline_enabled": True}}))
                ready.assert_called_once_with(self.core, None)
        (self.core / "packages/tool-scons/scons.py").unlink()
        self.assertFalse(mode.startup_ready(self.core, config={}))

    def test_cleanup_removes_only_owned_extras_and_preserves_every_shared_input(self):
        extra = self.record()
        (extra / "archives").mkdir()
        (extra / "archives" / "prepared.zip").write_text("owned archive")
        protected = [self.root / relative for relative in (
            "src/_python/runtime.txt", "core/packages/toolchain/package.json",
            "sketch/main.ino", "sketch/.mcu_flasher_build_cache/.mcu_ai_edits/edit.txt",
            "src/gui_config.json",
        )]
        for target in protected:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("keep exactly")
        mode.cleanup_extras(root=self.root, host="win32")
        self.assertFalse(extra.exists())
        self.assertTrue(all(target.read_text() == "keep exactly" for target in protected))
        self.assertTrue((self.core / setup.MARKER).exists())

    def test_cleanup_validates_all_targets_before_deleting_and_keeps_other_host(self):
        extra = self.record()
        other = mode.extras_directory(self.root, "linux")
        other.mkdir()
        (other / "user.txt").write_text("other host")
        (extra / "user.txt").write_text("unknown content")
        with self.assertRaisesRegex(RuntimeError, "Unrecognized"):
            mode.cleanup_extras(root=self.root, host="win32")
        self.assertTrue((extra / "readiness.json").exists())
        self.assertEqual((extra / "user.txt").read_text(), "unknown content")
        self.assertEqual((other / "user.txt").read_text(), "other host")

    def test_cleanup_requires_owner_and_rejects_moved_ownership(self):
        extra = self.record()
        owner = extra / mode.OWNER
        data = json.loads(owner.read_text())
        data["installation"] = "foreign installation"
        owner.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError, "ownership"):
            mode.cleanup_extras(root=self.root, host="win32")
        self.assertTrue((extra / "readiness.json").exists())

    def test_cleanup_rejects_reparse_or_linked_material_before_deleting(self):
        extra = self.record()
        with patch.object(mode, "_is_link", side_effect=lambda path: path.name == "readiness.json"):
            with self.assertRaisesRegex(RuntimeError, "Linked"):
                mode.cleanup_extras(root=self.root, host="win32")
        self.assertTrue((extra / "readiness.json").exists())

    def test_finish_keeps_pending_on_preparation_or_persistence_failure(self):
        config = {"shared": {"offline_enabled": True, "offline_preparation_pending": True}}
        with patch.object(mode, "_configuration", side_effect=lambda: copy.deepcopy(config)), \
                patch.object(mode, "startup_ready", return_value=False), \
                patch("main.core.config._save_raw_config") as save:
            with self.assertRaisesRegex(RuntimeError, "not fully prepared"):
                mode.finish_bootstrap(self.core)
            save.assert_not_called()
        with patch.object(mode, "_configuration", side_effect=lambda: copy.deepcopy(config)), \
                patch.object(mode, "startup_ready", return_value=True), \
                patch.object(mode, "record_preparation") as record, \
                patch("main.core.config._save_raw_config", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "could not be saved"):
                mode.finish_bootstrap(self.core)
            record.assert_called_once_with(self.core)
        self.assertTrue(config["shared"]["offline_preparation_pending"])

    def test_finish_cleanup_failure_does_not_clear_pending(self):
        config = {"shared": {"offline_enabled": False, "offline_preparation_pending": True}}
        with patch.object(mode, "_configuration", return_value=config), \
                patch.object(mode, "startup_ready", return_value=True), \
                patch.object(mode, "cleanup_extras", side_effect=OSError("files are busy")), \
                patch("main.core.config._save_raw_config") as save:
            with self.assertRaisesRegex(OSError, "busy"):
                mode.finish_bootstrap(self.core)
            save.assert_not_called()
        self.assertTrue(config["shared"]["offline_preparation_pending"])

    def test_cancel_restores_only_mode_and_reports_failed_save(self):
        config = {"shared": {"offline_enabled": True, "offline_preparation_pending": True, "theme_mode": "light"}}
        with patch.object(mode, "_configuration", side_effect=lambda: copy.deepcopy(config)), \
                patch("main.core.config._save_raw_config", return_value=False) as save:
            self.assertFalse(mode.cancel_transition(False))
        self.assertEqual(save.call_args.args[0]["shared"], {"offline_enabled": False,
                          "offline_preparation_pending": False, "theme_mode": "light"})

    def test_busy_and_other_window_checks_reject_changes(self):
        self.assertIn("operation", mode.transition_blocker(SimpleNamespace(is_busy=True)))
        config = {"instances": {"999": {"active_sketch_dir": "fixture", "hwnd": 123}}}
        with patch.object(mode, "_configuration", return_value=config), \
                patch("main.core.config._get_alive_pid_create_times", return_value={}), \
                patch("main.core.config._instance_is_alive", return_value=True):
            self.assertIn("other", mode.transition_blocker())

    def test_online_network_is_allowed_but_installers_still_require_bootstrap(self):
        for blocked in (False, True):
            with patch.object(runtime, "_network_blocked", blocked):
                if blocked:
                    with self.assertRaises(runtime.OfflineDependencyError):
                        runtime._audit("socket.connect", (None, ("192.0.2.1", 443)))
                else:
                    runtime._audit("socket.connect", (None, ("192.0.2.1", 443)))
                with self.assertRaisesRegex(runtime.OfflineDependencyError, "bootstrap"):
                    runtime._audit("subprocess.Popen", (sys.executable,
                        [sys.executable, "-m", "pip", "install", "fixture"], None, {}))

    def test_online_activation_also_guards_nested_python_package_managers(self):
        from platformio.package.manager._install import PackageManagerInstallMixin
        from platformio.package.exception import PackageException
        original = Mock()
        specification = SimpleNamespace(uri=None, humanize=lambda: "vendor/missing")
        manager = SimpleNamespace(ensure_spec=lambda value: value, get_package=lambda value: None)
        with patch.object(runtime, "_enabled", False), patch.object(runtime, "_network_blocked", False), \
                patch.object(runtime.sys, "addaudithook"), \
                patch("src.modules.windows_tool_paths.install_espidf_component_relpaths"), \
                patch("src.modules.mbed_compat.install_mbed_compat"), \
                patch.dict(os.environ, setup.clean_bootstrap_environment(), clear=True), \
                patch.object(PackageManagerInstallMixin, "_install", original):
            runtime.activate(False)
            self.assertFalse(runtime._network_blocked)
            self.assertEqual(os.environ["MCU_FLASHER_WORKSPACE_RUNTIME"], "1")
            self.assertNotIn("MCU_FLASHER_OFFLINE_RUNTIME", os.environ)
            with self.assertRaisesRegex(PackageException, "bootstrap"):
                PackageManagerInstallMixin._install(manager, specification)
            original.assert_not_called()

    def test_online_preparation_skips_offline_planners_and_clears_both_markers(self):
        with patch.object(setup, "prepare_runtime") as minimal, \
                patch.object(setup, "prepare", side_effect=AssertionError("Full package installer ran")), \
                patch.object(setup, "refresh_board_coverage", side_effect=AssertionError("Full board audit ran")):
            self.assertEqual(setup.main(["--core", str(self.core), "--runtime-only"]), 0)
        minimal.assert_called_once_with(self.core)
        env = setup.clean_bootstrap_environment({"MCU_FLASHER_WORKSPACE_RUNTIME": "1", "MCU_FLASHER_OFFLINE_RUNTIME": "1"})
        self.assertNotIn("MCU_FLASHER_WORKSPACE_RUNTIME", env)
        self.assertNotIn("MCU_FLASHER_OFFLINE_RUNTIME", env)

    def test_workspace_role_cannot_prepare_in_either_mode(self):
        for marker in ("MCU_FLASHER_WORKSPACE_RUNTIME", "MCU_FLASHER_OFFLINE_RUNTIME"):
            with patch.dict(os.environ, {marker: "1"}):
                with self.assertRaisesRegex(RuntimeError, "workspace"):
                    setup.prepare_runtime(self.core)
                with self.assertRaisesRegex(RuntimeError, "workspace"):
                    setup.prepare(self.core)

    def test_esp8266_parallelism_requires_exact_platform_package_and_sources(self):
        platform_dir = self.root / "platform"
        package_dir = self.root / "framework"
        signatures = {}
        package_signatures = {}
        for directory, selected, names in ((platform_dir, signatures, ("platform.py", "builder/main.py", "builder/frameworks/arduino.py")),
                                          (package_dir, package_signatures, ("tools/platformio-build.py",))):
            for name in names:
                target = directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# reviewed fixture\n")
                selected[name] = hashlib.sha256(target.read_text().encode()).hexdigest()
        manifest = package_dir / "package.json"
        manifest.write_text(json.dumps({"name": "framework-arduinoespressif8266", "version": "3.30102.0"}))
        platform = SimpleNamespace(name="espressif8266", version="4.2.1", get_dir=lambda: platform_dir,
            frameworks={"arduino": {"script": "builder/frameworks/arduino.py", "package": "framework-arduinoespressif8266"}},
            get_package_dir=lambda name: package_dir)
        with patch.dict(setup._REVIEWED_ESP8266_BUILDERS, {("espressif8266", "4.2.1"): signatures}), \
                patch.object(setup, "_ESP8266_PACKAGE_SCRIPT", package_signatures):
            self.assertTrue(setup._parallel_builder_safe(platform, [("nodemcuv2", "arduino")]))
            self.assertFalse(setup._parallel_builder_safe(platform, [("nodemcuv2", "rtos")]))
            platform.version = "4.2.2"
            self.assertFalse(setup._parallel_builder_safe(platform, [("nodemcuv2", "arduino")]))
            platform.version = "4.2.1"
            (package_dir / "tools/platformio-build.py").write_text("# modified script\n")
            self.assertFalse(setup._parallel_builder_safe(platform, [("nodemcuv2", "arduino")]))

    def test_builder_cancellation_does_not_launch_or_leave_running_children(self):
        cancel = threading.Event()
        cancel.set()
        arguments = dict(env={}, label="fixture", output_path=self.root / "cancelled.log", log=lambda _: None,
                         cancel=cancel, timeout=3, heartbeat=1)
        with patch.object(bootstrap_builders.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(InterruptedError, "before launch"):
                bootstrap_builders.run_builder(["fixture"], **arguments)
            launch.assert_not_called()
        cancel.clear()
        process = SimpleNamespace(stdout=io.StringIO("fixture started\n"), pid=123)
        ended = [False]
        process.poll = lambda: -1 if ended[0] else None
        def terminate(child):
            self.assertIs(child, process)
            ended[0] = True
        timer = threading.Timer(0.1, cancel.set)
        with patch.object(bootstrap_builders.subprocess, "Popen", return_value=process), \
                patch.object(bootstrap_builders, "_terminate_tree", side_effect=terminate):
            timer.start()
            try:
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    bootstrap_builders.run_builder(["fixture"], **arguments)
            finally:
                timer.cancel()
                timer.join()
        self.assertTrue(ended[0])
        self.assertIn("fixture started", (self.root / "cancelled.log").read_text())


class RestartChecks(unittest.TestCase):
    """Exercise the real restart callbacks with acknowledged editor fixtures."""
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        audit = ROOT / "temp/audit/offline-restart"
        audit.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=audit)))
        self.config = {"shared": {"offline_enabled": True, "offline_preparation_pending": True, "theme_mode": "light"}}
        self.save = self.stack.enter_context(patch("main.core.config._save_raw_config", return_value=True))
        self.stack.enter_context(patch("main.core.config._load_raw_config", side_effect=lambda **_: copy.deepcopy(self.config)))
        self.blocker = self.stack.enter_context(patch.object(mode, "transition_blocker", return_value=""))
        self.child = SimpleNamespace(poll=Mock(return_value=None), terminate=Mock())
        self.stack.enter_context(patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True))
        self.launch = self.stack.enter_context(patch.object(subprocess, "Popen", return_value=self.child))
        self.warning = Mock()
        self.timers = []
        self.window = SimpleNamespace(_backend=SimpleNamespace(sketch_dir_path=self.root / "Sketch With Spaces"),
            _is_busy=Mock(return_value=False), setEnabled=Mock(), close=Mock(return_value=True),
            _editor_panel=SimpleNamespace(trigger_save_all=Mock()))
        self.scope = {"os": os, "sys": SimpleNamespace(platform="win32", executable="fixture-python"),
            "_project_root": self.root, "QMessageBox": SimpleNamespace(warning=self.warning),
            "QTimer": SimpleNamespace(singleShot=lambda delay, callback: self.timers.append(callback))}
        source = ROOT / "main/qt/main_window.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        window_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MCUMainWindow")
        method = next(node for node in window_class.body if isinstance(node, ast.FunctionDef) and node.name == "restart_for_offline_mode")
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), self.scope)

    def request(self):
        self.scope["restart_for_offline_mode"](self.window, False)

    def acknowledge(self):
        self.window._editor_panel.trigger_save_all.call_args.kwargs["callback"]()

    def assert_restored(self):
        self.assertEqual(self.save.call_args.args[0]["shared"], {"offline_enabled": False,
            "offline_preparation_pending": False, "theme_mode": "light"})

    def test_save_failure_restores_mode_and_keeps_workspace_open(self):
        self.request()
        self.launch.assert_not_called()
        self.window._editor_panel.trigger_save_all.call_args.kwargs["failure_callback"]("fixture save failed")
        self.assert_restored()
        self.window.close.assert_not_called()
        self.window.setEnabled.assert_called_with(True)
        self.warning.assert_called_once()

    def test_busy_or_other_window_blocks_before_editor_save_and_child_launch(self):
        for reason in ("other window", ""):
            self.blocker.return_value = reason
            self.window._is_busy.return_value = not bool(reason)
            self.request()
            self.window._editor_panel.trigger_save_all.assert_not_called()
            self.launch.assert_not_called()
            self.assert_restored()

    def test_success_waits_for_save_then_clears_environment_and_closes(self):
        inherited = {key: "fixture" for key in ("PYTHONHOME", "PYTHONPATH", "MCU_FLASHER_WORKSPACE_RUNTIME",
            "MCU_FLASHER_OFFLINE_RUNTIME", "MCU_FLASHER_APP_ROOT", "PIP_NO_INDEX")}
        with patch.dict(os.environ, inherited):
            self.request()
            self.launch.assert_not_called()
            self.acknowledge()
        command = self.launch.call_args.args[0]
        self.assertEqual(command[:2], ["fixture-python", "-B"])
        self.assertEqual(command[2], str(self.root / "direct/restart_workspace.py"))
        self.assertEqual(command[-2:], ["--project", str(self.root / "Sketch With Spaces")])
        self.assertTrue(all(key not in self.launch.call_args.kwargs["env"] for key in inherited))
        self.window.close.assert_not_called()
        self.timers.pop()()
        self.window.close.assert_called_once()
        self.save.assert_not_called()
        self.child.terminate.assert_not_called()

    def test_new_busy_state_after_handoff_stops_child_and_restores_mode(self):
        self.request()
        self.acknowledge()
        self.window._is_busy.return_value = True
        self.timers.pop()()
        self.child.terminate.assert_called_once()
        self.window.close.assert_not_called()
        self.assert_restored()

    def test_launch_failure_and_failed_rollback_are_reported(self):
        self.launch.side_effect = OSError("fixture cannot launch helper")
        self.save.return_value = False
        self.request()
        self.acknowledge()
        self.assertIn("previous mode could not be restored", self.warning.call_args.args[2])
        self.window.close.assert_not_called()

    def helper(self):
        spec = importlib.util.spec_from_file_location("fixture_restart_workspace", ROOT / "direct/restart_workspace.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        helper.ROOT = self.root
        return helper

    def test_host_restart_helper_waits_for_parent_before_bootstrap_and_preserves_project(self):
        import psutil
        for host in ("win32", "linux"):
            helper = self.helper()
            helper.sys = SimpleNamespace(platform=host)
            events = []
            parent = SimpleNamespace(wait=Mock(side_effect=lambda timeout: events.append("parent exited")))
            with patch.object(psutil, "Process", return_value=parent), \
                    patch.object(helper.subprocess, "Popen", side_effect=lambda *args, **kwargs: events.append("bootstrap started")) as launch, \
                    patch.dict(os.environ, {"MCU_FLASHER_WORKSPACE_RUNTIME": "1", "MCU_FLASHER_OFFLINE_RUNTIME": "1", "PIP_NO_INDEX": "1"}):
                self.assertEqual(helper.restart(123, str(self.root / "Sketch With Spaces")), 0)
            self.assertEqual(events, ["parent exited", "bootstrap started"])
            parent.wait.assert_called_once_with(timeout=60)
            command = launch.call_args.args[0]
            self.assertIn("--repair", command)
            self.assertEqual(command[-2:], ["--project", str(self.root / "Sketch With Spaces")])
            self.assertTrue(command[1].endswith("run.vbs" if host == "win32" else "run.sh"))
            for key in ("MCU_FLASHER_WORKSPACE_RUNTIME", "MCU_FLASHER_OFFLINE_RUNTIME", "PIP_NO_INDEX"):
                self.assertNotIn(key, launch.call_args.kwargs["env"])

    def test_restart_helper_timeout_does_not_start_bootstrap(self):
        import psutil
        helper = self.helper()
        parent = SimpleNamespace(wait=Mock(side_effect=psutil.TimeoutExpired(60)))
        with patch.object(psutil, "Process", return_value=parent):
            self.assertEqual(helper.restart(123, "fixture"), 1)
        self.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
