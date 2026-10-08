"""Isolated multiple-window session and launch regressions; no app startup."""
from __future__ import annotations

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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.modules import crash_detector as detector


class SessionChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp/scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name)
        self.installation = self.fixture / "application"
        self.installation.mkdir()
        self.live = {101, 202}
        paths = {"_USER_STATE_DIR": self.fixture,
                 "_SESSION_SENTINEL_FILE": self.fixture / "session_sentinel.json",
                 "_CRASH_MARKER_FILE": self.fixture / "crash_marker.json",
                 "_SESSION_DIR": self.fixture / "sessions",
                 "_CRASH_DIR": self.fixture / "crashes"}
        for name, value in paths.items():
            guard = patch.object(detector, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        for guard in (patch.object(detector, "is_process_alive", side_effect=lambda pid: pid in self.live),
                      patch.object(detector, "_process_create_time", side_effect=lambda pid: float(pid))):
            guard.start()
            self.addCleanup(guard.stop)

    def start(self, pid):
        detector.mark_session_started(pid, self.installation)

    def test_clean_sibling_does_not_overwrite_active_session(self):
        self.start(101)
        self.start(202)
        detector.mark_session_clean_exit(202)
        sessions = detector.running_sessions(self.installation)
        self.assertEqual([data["pid"] for data in sessions], [101])
        self.assertFalse(detector.detect_previous_crash(self.installation)["crashed"])

    def test_crashed_window_visible_while_other_window_is_alive(self):
        self.start(101)
        self.start(202)
        native_log = detector._CRASH_DIR / "native-101.log"
        native_log.parent.mkdir(parents=True)
        native_log.write_text("Windows fatal exception: access violation\ncompile thread", encoding="utf-8")
        self.live.remove(101)
        result = detector.detect_previous_crash(self.installation)
        self.assertEqual(result["type"], "abnormal_termination")
        self.assertIn("access violation", result["details"])
        detector.clear_crash_state(self.installation)
        self.assertEqual([data["pid"] for data in detector.running_sessions(self.installation)], [202])
        self.assertFalse(detector.detect_previous_crash(self.installation)["crashed"])

    def test_legacy_dead_session_survives_new_window_until_repair(self):
        detector._SESSION_SENTINEL_FILE.write_text(json.dumps({"pid": 303, "status": "running"}))
        self.start(202)
        detector.mark_session_clean_exit(202)
        self.assertTrue(detector.detect_previous_crash(self.installation)["crashed"])
        detector.clear_crash_state(self.installation)
        self.assertFalse(detector._SESSION_SENTINEL_FILE.exists())

    def test_reused_pid_is_not_a_live_gui_session(self):
        self.start(101)
        with patch.object(detector, "_process_create_time", return_value=999.0):
            self.assertEqual(detector.running_sessions(self.installation), [])
            self.assertTrue(detector.detect_previous_crash(self.installation)["crashed"])

    def test_installations_do_not_share_lifecycle_or_crash_records(self):
        self.start(101)
        other = self.fixture / "other"
        detector.mark_session_started(202, other)
        detector.record_crash_event("fixture", "different copy", pid=202, workspace_dir=other)
        self.assertEqual([data["pid"] for data in detector.running_sessions(self.installation)], [101])
        self.assertFalse(detector.detect_previous_crash(self.installation)["crashed"])
        detector.clear_crash_state(self.installation)
        self.assertTrue((detector._CRASH_DIR / "202.json").exists())

    def test_repair_preserves_live_legacy_session(self):
        detector._SESSION_SENTINEL_FILE.write_text(json.dumps({"pid": 202, "status": "running"}))
        detector.clear_crash_state(self.installation)
        self.assertEqual(json.loads(detector._SESSION_SENTINEL_FILE.read_text())["status"], "running")

    def test_legacy_gui_output_requires_crash_evidence(self):
        logs = self.installation / "logs"
        logs.mkdir()
        log = logs / "gui_crash.log"
        log.write_text("Qt WebEngine initialized\nWARNING: optional browser capability unavailable")
        self.assertFalse(detector.detect_previous_crash(self.installation)["crashed"])
        log.write_text("Traceback (most recent call last):\nRuntimeError: fixture")
        self.assertEqual(detector.detect_previous_crash(self.installation)["type"], "gui_crash_log")

    def test_worker_exception_tracking_is_idempotent_and_records_traceback(self):
        previous = Mock()
        with patch.object(detector, "_TRACKING_INSTALLED", False), \
             patch.object(detector, "_DIAGNOSTIC_STREAM", None), \
             patch.object(threading, "excepthook", previous), \
             patch("faulthandler.enable") as enable:
            detector.install_crash_tracking(self.installation)
            detector.install_crash_tracking(self.installation)
            enable.assert_called_once()
            args = SimpleNamespace(exc_type=RuntimeError, exc_value=RuntimeError("worker fixture"), exc_traceback=None)
            threading.excepthook(args)
            previous.assert_called_once_with(args)
            data = json.loads((detector._CRASH_DIR / f"{os.getpid()}.json").read_text())
            self.assertEqual(data["crash_type"], "unhandled_worker_exception")
            self.assertIn("worker fixture", data["details"])
            detector._DIAGNOSTIC_STREAM.close()

    def test_concurrent_exception_records_remain_atomic(self):
        threads = [threading.Thread(target=detector.record_crash_event,
                                    args=("fixture", f"failure {index}"),
                                    kwargs={"pid": 101, "workspace_dir": self.installation})
                   for index in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        data = json.loads((detector._CRASH_DIR / "101.json").read_text())
        self.assertTrue(data["details"].startswith("failure "))
        self.assertEqual(list(detector._CRASH_DIR.glob("*.tmp-*")), [])


def bootstrap_helpers(fixture):
    names = {"_is_main_gui_running", "_try_running_instance_launch", "_spawn_main_gui"}
    tree = ast.parse((ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig"))
    module = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                             and node.name in names], type_ignores=[])
    gui = fixture / "mcu_flash_gui.py"
    gui.write_text("# fixture only")
    scope = {"Path": Path, "os": os, "sys": sys, "subprocess": subprocess, "tempfile": tempfile,
             "SCRIPT_DIR": fixture, "GUI_SCRIPT": gui,
             "STARTUP_HEALTH_FILE": fixture / "health.json",
             "_startup_installation_identity": lambda: str(fixture.resolve()).casefold(),
             "_explicit_setup_requested": Mock(return_value=False),
             "_startup_required_paths": lambda: [gui],
             "_get_safe_platformio_core_dir": lambda _: str(fixture / "core"),
             "_record_bootstrap_log": Mock(),
             "_read_startup_health_snapshot": Mock(side_effect=AssertionError("Live launch must not read stale fingerprints"))}
    exec(compile(module, "<isolated startup helpers>", "exec"), scope)
    return scope


class LaunchChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp/scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name)
        self.scope = bootstrap_helpers(self.fixture)
        self.mode_ready = Mock(return_value=True)
        self.crash = Mock(return_value={"crashed": False})
        self.sessions = Mock(return_value=[])
        guard = patch.dict(sys.modules, {
            "src.modules.offline_mode": SimpleNamespace(startup_ready=self.mode_ready),
            "src.modules.crash_detector": SimpleNamespace(detect_previous_crash=self.crash,
                                                           running_sessions=self.sessions)})
        guard.start()
        self.addCleanup(guard.stop)
        self.scope["_is_main_gui_running"] = Mock(return_value=True)
        self.child = Mock()
        self.child.wait.side_effect = subprocess.TimeoutExpired("fixture", 1.0)
        self.scope["_spawn_main_gui"] = Mock(return_value=(self.child, None))

    def test_running_workspace_bypasses_stale_health_but_observes_child(self):
        self.assertTrue(self.scope["_try_running_instance_launch"]())
        self.scope["_read_startup_health_snapshot"].assert_not_called()
        self.child.wait.assert_called_once_with(timeout=1.0)
        self.crash.assert_called_once_with(self.fixture)

    def test_explicit_setup_pending_mode_and_recorded_crash_keep_repair(self):
        self.scope["_explicit_setup_requested"].return_value = True
        self.assertFalse(self.scope["_try_running_instance_launch"]())
        self.scope["_explicit_setup_requested"].return_value = False
        self.mode_ready.return_value = False
        self.assertFalse(self.scope["_try_running_instance_launch"]())
        self.mode_ready.return_value = True
        self.crash.return_value = {"crashed": True}
        self.assertFalse(self.scope["_try_running_instance_launch"]())
        self.scope["_spawn_main_gui"].assert_not_called()

    def test_missing_runtime_and_immediate_child_failure_keep_repair(self):
        self.scope["GUI_SCRIPT"].unlink()
        self.assertFalse(self.scope["_try_running_instance_launch"]())
        self.scope["GUI_SCRIPT"].write_text("# restored fixture")
        self.child.wait.side_effect = None
        self.child.wait.return_value = 1
        self.scope["STARTUP_HEALTH_FILE"].write_text("fixture readiness")
        self.assertFalse(self.scope["_try_running_instance_launch"]())
        self.assertFalse(self.scope["STARTUP_HEALTH_FILE"].exists())
        self.scope["_spawn_main_gui"].return_value = (None, None)
        self.assertFalse(self.scope["_try_running_instance_launch"]())

    def test_live_installation_scan_rejects_other_portable_copies(self):
        actual_scan = bootstrap_helpers(self.fixture)["_is_main_gui_running"]
        other = self.fixture / "other/mcu_flash_gui.py"
        process = SimpleNamespace(info={"pid": 303, "name": "python.exe", "cmdline": ["python.exe", str(other)]})
        with patch.dict(sys.modules, {"psutil": SimpleNamespace(process_iter=lambda _: [process])}):
            self.assertFalse(actual_scan())
            process.info["cmdline"][-1] = str(self.scope["GUI_SCRIPT"])
            self.assertTrue(actual_scan())
            process.info["cmdline"][-1] = "mcu_flash_gui.py"
            process.cwd = lambda: str(self.fixture)
            self.assertTrue(actual_scan())

    @unittest.skipUnless(sys.platform == "win32", "Windows detached child log contract")
    def test_spawned_windows_never_truncate_each_others_output_logs(self):
        actual_spawn = bootstrap_helpers(self.fixture)["_spawn_main_gui"]
        scripts = self.fixture / "env/Scripts"
        scripts.mkdir(parents=True)
        (scripts / "pythonw.exe").write_bytes(b"fixture only")
        outputs = []

        def spawn(command, **kwargs):
            kwargs["stdout"].write(f"workspace {len(outputs) + 1}\n")
            outputs.append(kwargs["env"]["MCU_FLASHER_GUI_LOG"])
            return SimpleNamespace(pid=313)

        native = SimpleNamespace(windll=SimpleNamespace(user32=SimpleNamespace(AllowSetForegroundWindow=Mock())))
        with patch.dict(sys.modules, {"ctypes": native}), \
             patch.object(subprocess, "_orig_popen", side_effect=spawn, create=True), \
             patch.object(sys, "argv", ["bootstrap.py", "--new-window"]):
            _, first = actual_spawn()
            _, second = actual_spawn()
        self.assertNotEqual(first, second)
        self.assertEqual(outputs, [str(first), str(second)])
        self.assertEqual(first.read_text(), "workspace 1\n")
        self.assertEqual(second.read_text(), "workspace 2\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
