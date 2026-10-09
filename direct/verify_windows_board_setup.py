#!/usr/bin/env python3
"""Verify Windows board setup without live installers, settings or hardware.

The production preparation block is extracted from Bootstrap's worker. Every
external action is mocked, and fixture files live only below temp/.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager, ExitStack
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.modules import offline_bootstrap as setup, offline_mode as mode
from src.modules import bootstrap_board_coverage, platformio_locks


def preparation_code():
    """Extract the real package phase without importing Windows Bootstrap."""
    source = ROOT / "src/modules/bootstrap.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    worker = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_run_setup_in_thread")
    statements = next(node.body for node in worker.body if isinstance(node, ast.Try))
    start = next(index for index, node in enumerate(statements)
                 if isinstance(node, ast.Assign) and any(
                     isinstance(target, ast.Name) and target.id == "offline_core"
                     for target in node.targets))
    end = next(index for index in range(start, len(statements))
               if isinstance(statements[index], ast.With) and any(
                   isinstance(item.context_expr, ast.Call)
                   and isinstance(item.context_expr.func, ast.Name)
                   and item.context_expr.func.id == "_bootstrap_tool_store_lease"
                   for item in statements[index].items))
    function = ast.parse("def run_preparation(gui):\n    pass\n").body[0]
    function.body = copy.deepcopy(statements[start:end + 1])
    function.body.append(ast.Return(value=ast.Constant(value=True)))
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    return compile(module, str(source), "exec")


class WindowsBoardSetupChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = preparation_code()

    def setUp(self):
        parent = ROOT / "temp/audit/windows-board-setup"
        parent.mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.core = self.root / "core"
        self.core.mkdir()
        self.plan = {"schema": 1, "platforms": ["atmelavr", "espressif32", "espressif8266"],
                     "frameworks": ["arduino"], "libraries": []}

    def run_preparation(self, *, offline=False, initial_ready=False, final_ready=True,
                        explicit=False, plan_path=None, board_sources=(), child_exit=0,
                        closed=False, close_during_child=False):
        gui = Mock()
        gui._closed = closed
        gui.root.after.side_effect = lambda delay, callback: callback()
        state = {"leased": False}

        @contextmanager
        def lease(*args):
            self.assertFalse(state["leased"])
            state["leased"] = True
            try:
                yield
            finally:
                state["leased"] = False

        @contextmanager
        def package_lock():
            self.assertTrue(state["leased"])
            yield

        process = SimpleNamespace(wait=Mock(return_value=child_exit))

        def spawn(command, **kwargs):
            self.assertTrue(state["leased"], "Preparation child escaped the package-store lease")
            self.assertNotIn("--runtime-only", command)
            return process

        def finish(*args):
            self.assertTrue(state["leased"], "Readiness must be finalized before releasing the store")

        launch = Mock(side_effect=spawn)
        def stream_child(*args):
            if close_during_child:
                gui._closed = True
            return child_exit
        fail = Mock()
        finish_setup = Mock(side_effect=finish)
        scope = dict(Path=Path, os=os,
                     sys=SimpleNamespace(executable=str(self.root / "private-python.exe"), platform="win32"),
                     subprocess=SimpleNamespace(Popen=launch, PIPE=subprocess.PIPE,
                         STDOUT=subprocess.STDOUT, CREATE_NO_WINDOW=0x08000000),
                     SCRIPT_DIR=self.root, _bootstrap_tool_store_lease=lease,
                     _get_safe_platformio_core_dir=Mock(return_value=str(self.core)),
                     _ensure_platformio_core_prebuilt=Mock(return_value=True),
                     ensure_arduino_cli=Mock(return_value=True), ensure_platformio=Mock(return_value=True),
                     ensure_arduino_avr_board=Mock(return_value=True), ensure_esp32_board_folder=Mock(return_value=True),
                     _record_bootstrap_exception=Mock(), _fail_and_exit=fail,
                     _explicit_setup_requested=Mock(return_value=explicit),
                     _apply_bootstrap_compiler_budget=Mock(return_value=2),
                     _stream_offline_setup_output=Mock(side_effect=stream_child))
        exec(self.code, scope)
        config = {"shared": {"offline_enabled": offline}}
        original_config = copy.deepcopy(config)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"PLATFORMIO_CORE_DIR": str(self.core)}))
            stack.enter_context(patch.object(platformio_locks, "package_locks", package_lock))
            stack.enter_context(patch.object(setup, "requested_plan", return_value=(self.plan, plan_path)))
            ready = stack.enter_context(patch.object(setup, "ready", side_effect=[initial_ready, final_ready]))
            stack.enter_context(patch.object(bootstrap_board_coverage, "requested_board_sources", return_value=board_sources))
            stack.enter_context(patch.object(setup, "clean_bootstrap_environment", return_value={"FIXTURE": "1"}))
            stack.enter_context(patch.object(setup, "install_runtime_guard",
                side_effect=AssertionError("SCons-only setup cannot prepare Windows board support")))
            stack.enter_context(patch.object(mode, "_configuration", return_value=config))
            stack.enter_context(patch.object(mode, "finish_bootstrap", finish_setup))
            stack.enter_context(patch("main.core.config._save_raw_config",
                side_effect=AssertionError("Preparation must not change the saved Online/Offline Mode")))
            result = scope["run_preparation"](gui)
        self.assertFalse(state["leased"])
        self.assertEqual(config, original_config)
        return SimpleNamespace(result=result, launch=launch, ready=ready, fail=fail,
                               finish=finish_setup, process=process, gui=gui)

    def assert_full_child(self, result):
        result.launch.assert_called_once()
        command = result.launch.call_args.args[0]
        self.assertEqual(command[:3], [str(self.root / "private-python.exe"), "-B",
                                      str(self.root / "src/modules/offline_bootstrap.py")])
        self.assertEqual(command[command.index("--core") + 1], str(self.core))
        self.assertEqual(command[command.index("--jobs") + 1], "2")
        self.assertNotIn("--runtime-only", command)
        self.assertEqual(result.launch.call_args.kwargs["env"], {"FIXTURE": "1", "PYTHONUNBUFFERED": "1"})
        result.finish.assert_called_once_with(self.core)
        result.fail.assert_not_called()
        self.assertTrue(result.result)
        return command

    def test_empty_online_setup_prepares_common_board_plan(self):
        result = self.run_preparation()
        command = self.assert_full_child(result)
        self.assertNotIn("--coverage-only", command)
        self.assertEqual(result.ready.call_args_list[0].args, (self.core, None))

    def test_empty_offline_setup_keeps_full_preparation(self):
        self.assert_full_child(self.run_preparation(offline=True))

    def test_closed_setup_never_starts_board_preparation_or_finalizes_readiness(self):
        result = self.run_preparation(closed=True)
        result.launch.assert_not_called()
        result.finish.assert_not_called()
        result.fail.assert_not_called()

    def test_setup_closed_during_child_never_finalizes_readiness(self):
        result = self.run_preparation(close_during_child=True)
        result.launch.assert_called_once()
        result.finish.assert_not_called()
        result.ready.assert_called_once()
        result.fail.assert_not_called()

    def test_explicit_ready_online_repair_reuses_certified_packages(self):
        command = self.assert_full_child(self.run_preparation(initial_ready=True, explicit=True))
        self.assertIn("--coverage-only", command)

    def test_ready_online_normal_setup_reuses_certificate_without_child(self):
        result = self.run_preparation(initial_ready=True)
        result.launch.assert_not_called()
        result.ready.assert_called_once_with(self.core, None)
        result.finish.assert_called_once_with(self.core)
        result.fail.assert_not_called()
        self.assertTrue(result.result)

    def test_custom_plan_and_every_board_source_are_forwarded(self):
        plan_path = self.root / "Custom plan.json"
        sources = (self.root / "Boards one", self.root / "Boards two")
        result = self.run_preparation(plan_path=plan_path, board_sources=sources)
        command = self.assert_full_child(result)
        self.assertEqual(command[command.index("--plan") + 1], str(plan_path))
        self.assertEqual([command[index + 1] for index, value in enumerate(command)
                          if value == "--board-source"], list(map(str, sources)))
        self.assertEqual([call.args for call in result.ready.call_args_list],
                         [(self.core, self.plan), (self.core, self.plan)])

    def test_ready_custom_plan_source_refresh_uses_coverage_only(self):
        command = self.assert_full_child(self.run_preparation(initial_ready=True,
            plan_path=self.root / "custom.json", board_sources=(self.root / "Boards",)))
        self.assertIn("--coverage-only", command)

    def test_failed_child_never_finalizes_successful_setup(self):
        result = self.run_preparation(child_exit=1)
        result.launch.assert_called_once()
        result.ready.assert_called_once_with(self.core, None)
        result.finish.assert_not_called()
        result.fail.assert_called_once()
        self.assertIsNone(result.result)

    def test_incomplete_certificate_after_child_never_finalizes_setup(self):
        result = self.run_preparation(final_ready=False)
        result.launch.assert_called_once()
        self.assertEqual(result.ready.call_count, 2)
        result.finish.assert_not_called()
        result.fail.assert_called_once()
        self.assertIsNone(result.result)

    def test_windows_online_startup_requires_native_board_certificate(self):
        for certified in (False, True):
            with self.subTest(certified=certified), patch.object(mode.sys, "platform", "win32"), \
                    patch.object(setup, "ready", return_value=certified) as ready:
                self.assertEqual(mode.startup_ready(self.core, self.plan, config={}), certified)
                ready.assert_called_once_with(self.core, self.plan)

    def test_pending_windows_transition_stops_before_readiness_check(self):
        with patch.object(mode.sys, "platform", "win32"), patch.object(setup, "ready") as ready:
            self.assertFalse(mode.startup_ready(self.core, config={"shared": {"offline_preparation_pending": True}}))
            ready.assert_not_called()

    def test_linux_online_runtime_gate_retains_its_existing_policy(self):
        for name in ("packages/tool-scons/package.json", "packages/tool-scons/.piopm", "packages/tool-scons/scons.py"):
            target = self.core / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture", encoding="utf-8")
        with patch.object(mode.sys, "platform", "linux"), patch.object(mode, "ROOT", self.root), \
                patch.object(setup, "ASSETS", ()), patch.object(setup, "ready") as ready:
            self.assertTrue(mode.startup_ready(self.core, config={}))
            ready.assert_not_called()
            (self.core / "packages/tool-scons/scons.py").unlink()
            self.assertFalse(mode.startup_ready(self.core, config={}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
