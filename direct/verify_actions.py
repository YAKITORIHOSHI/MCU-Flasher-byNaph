"""Hardware-free action regression checks; execute backend methods without startup."""
from __future__ import annotations

import ast
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from main.core.constants import board_reset_capabilities
from src.modules.package_jobs import package_store_lease


class ActionChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/actions"
        audit.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        methods = {"_start_reset_worker", "hard_reset", "soft_reset", "clean_cache",
                   "_new_upload_progress_state", "_fast_upload_retry_allowed",
                   "compile_sketch", "upload_sketch", "_release_requested_operation",
                   "_start_resolved_upload", "set_timestamp_enabled",
                   "_operation_worker_alive", "_begin_operation_session"}
        tree = ast.parse((ROOT / "main/web_bridge.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MCUWebBackendAPI")
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in methods]
        cls.bases = []
        self.pending = []
        self.ns = dict(Path=Path, os=os, sys=sys, time=SimpleNamespace(sleep=lambda _: None),
                       threading=SimpleNamespace(Thread=lambda **kw: SimpleNamespace(
                           start=lambda: self.pending.append(kw["target"]), is_alive=lambda: False)),
                       board_reset_capabilities=board_reset_capabilities, load_gui_config=lambda: {},
                       port_occupied_owner=lambda _: None, _try_acquire_reset_cache_lock=lambda: object(),
                       _release_reset_cache_lock=Mock(), SCRIPT_DIR=self.root,
                       package_store_lease=package_store_lease,
                       package_core_directory=lambda: self.root / "tool-store",
                       get_project_build_cache_root=lambda *a, **k: self.root / ".mcu_flasher_build_cache",
                       robust_rmtree=shutil.rmtree, _sketch_ram_cache=SimpleNamespace(invalidate=Mock()),
                       subprocess=SimpleNamespace(PIPE=-1, STDOUT=-2, CREATE_NO_WINDOW=0))
        self.ns["_HOST_RUNTIME"] = SimpleNamespace(use_native_upload=lambda _: False)
        self.ns["DEFAULT_UPLOAD_SPEED"] = 460800
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "isolated_backend", "exec"), self.ns)
        self.api = self.ns["MCUWebBackendAPI"]()
        b = self.api
        b._package_event_root = self.root / "events"
        b.is_busy = False
        b.active_operation = None
        b._operation_worker = None
        b._stop_requested = False
        b.current_board, b.current_port = "Demo", "COM99"
        b.sketch_dir_path = self.root
        b.emit = Mock()
        self.info = dict(platform="espressif32", board="esp32dev", framework="arduino")
        b._resolve_board_info = lambda *a: self.info
        b._check_target = lambda *a: True
        b._stop_serial_monitor = Mock()
        b._start_serial_monitor = Mock()
        b._remote_workspace_root = lambda _: None
        b._block_if_pending_ai_edits = lambda _: False
        b._compile_requested_worker = Mock()
        b._upload_requested_worker = Mock()

    def run_worker(self):
        self.pending.pop(0)()

    def test_reset_reserves_before_thread_and_clears_stale_stop(self):
        b = self.api
        b._stop_requested = True
        job = Mock()
        self.assertTrue(b._start_reset_worker("soft", job))
        self.assertTrue(b.is_busy)
        self.assertFalse(b._stop_requested)
        self.assertFalse(b._start_reset_worker("hard", job))
        self.run_worker()
        job.assert_called_once()
        self.assertFalse(b.is_busy)
        self.assertIsNone(b.active_operation)

    def test_reset_early_lock_failure_releases_busy(self):
        self.ns["_try_acquire_reset_cache_lock"] = lambda: None
        self.api.soft_reset()
        self.run_worker()
        self.assertFalse(self.api.is_busy)
        self.api._stop_serial_monitor.assert_not_called()

    def test_preparation_lease_rejects_reset_without_hardware_calls(self):
        with package_store_lease(self.root / "tool-store", "prepare", root=self.api._package_event_root):
            self.api.soft_reset()
            self.run_worker()
        self.assertFalse(self.api.is_busy)
        self.api._stop_serial_monitor.assert_not_called()

    def test_worker_start_failure_releases_busy(self):
        self.ns["threading"].Thread = Mock(side_effect=RuntimeError("No worker available"))
        self.assertFalse(self.api._start_reset_worker("soft", Mock()))
        self.assertFalse(self.api.is_busy)
        self.api.clean_cache()
        self.assertFalse(self.api.is_busy)

    def test_compile_upload_worker_start_failure_releases_reservation(self):
        self.ns["threading"].Thread = Mock(side_effect=RuntimeError("No worker available"))
        for action in (self.api.compile_sketch, self.api.upload_sketch):
            action()
            self.assertFalse(self.api.is_busy)
            self.assertIsNone(self.api.active_operation)

    def test_unchecked_skip_compile_never_uses_the_available_cache(self):
        b = self.api
        b.skip_compile = False
        b.check_can_skip_compile_for_upload = Mock(return_value=True)
        b._native_upload_worker = Mock()
        b._upload_worker = Mock()
        b._start_resolved_upload({})
        b.check_can_skip_compile_for_upload.assert_not_called()
        b._upload_worker.assert_called_once_with(False)
        b.skip_compile = True
        b._start_resolved_upload({})
        b.check_can_skip_compile_for_upload.assert_called_once_with("Demo")
        self.assertEqual(b._upload_worker.call_args.args, (True,))

    def test_timestamp_save_failure_remains_visible(self):
        self.ns["save_gui_config"] = Mock(return_value=False)
        self.api.timestamp_enabled = False
        self.assertFalse(self.api.set_timestamp_enabled(True))
        self.assertFalse(self.api.timestamp_enabled)
        messages = [call.args[1] for call in self.api.emit.call_args_list
                    if call.args[0] == "notification"]
        self.assertEqual(messages[-1]["title"], "Settings not saved")

    def test_clean_deletion_failure_is_reported(self):
        cache = self.root / ".mcu_flasher_build_cache"
        (cache / "boards").mkdir(parents=True)
        self.ns["robust_rmtree"] = Mock(side_effect=PermissionError("Locked"))
        self.api.clean_cache()
        self.run_worker()
        idle = [c.args[1] for c in self.api.emit.call_args_list if c.args[0] == "operation:phase"][-1]
        self.assertFalse(idle["success"])
        self.assertTrue((cache / "boards").exists())

    def test_reset_monitor_failure_releases_lock(self):
        self.api._stop_serial_monitor.side_effect = RuntimeError("serial failure")
        self.api.soft_reset()
        self.run_worker()
        self.ns["_release_reset_cache_lock"].assert_called_once()
        self.assertFalse(self.api.is_busy)

    def prepare_hard(self, erase_code=0, write_ok=True):
        b = self.api
        images = {"bootloader": self.root / "bootloader.bin", "partitions": self.root / "partitions.bin"}
        boot = self.root / "boot_app0.bin"
        boot.write_bytes(b"fixture")
        b._locate_hard_reset_recovery_images = Mock(return_value=(images, ""))
        b._build_hard_reset_recovery_images = Mock(return_value=(None, "build failed"))
        b._locate_esp32_boot_app0 = lambda: boot
        b._esptool_target = lambda *a: ("esp32", "0x1000")
        b._is_native_usb_port = lambda _: False
        b._emit_boot_connection_progress = Mock()
        b._get_esptool_cmd = lambda: ["fixture-esptool"]
        b._write_esptool_connect_config = lambda *a: None
        b._soft_reset_esptool_write = Mock(return_value=(write_ok, "write failed", 1))
        b._trigger_actual_board_reset = Mock()
        self.process = Mock(stdout=io.StringIO(""), returncode=erase_code)
        self.ns["subprocess"].Popen = Mock(return_value=self.process)

    def test_hard_prepares_before_erase_and_uses_captured_target(self):
        self.prepare_hard()
        b = self.api
        b.hard_reset()
        b.current_board, b.current_port = "Changed", "COM100"
        self.run_worker()
        cmd = self.ns["subprocess"].Popen.call_args.args[0]
        self.assertIn("COM99", cmd)
        bins, port = b._soft_reset_esptool_write.call_args.args
        self.assertTrue(bins["recovery_only"])
        self.assertEqual((bins["board_name"], port), ("Demo", "COM99"))
        self.assertEqual(len(b._new_upload_progress_state(bins)["stages"]), 3)
        b._start_serial_monitor.assert_not_called()

    def test_failed_preparation_never_erases(self):
        self.prepare_hard()
        self.api._locate_hard_reset_recovery_images.return_value = None, "missing"
        self.api.hard_reset()
        self.run_worker()
        self.ns["subprocess"].Popen.assert_not_called()
        self.assertFalse(self.api.is_busy)

    def test_failed_erase_never_writes_recovery(self):
        self.prepare_hard(erase_code=1)
        self.api.hard_reset()
        self.run_worker()
        self.api._soft_reset_esptool_write.assert_not_called()

    def test_failed_recovery_does_not_report_success(self):
        self.prepare_hard(write_ok=False)
        self.api.hard_reset()
        self.run_worker()
        idle = [c.args[1] for c in self.api.emit.call_args_list if c.args[0] == "operation:phase"][-1]
        self.assertFalse(idle["success"])

    def test_esptool_retry_is_forbidden_after_erase_or_write_starts(self):
        retry = self.api._fast_upload_retry_allowed
        common = dict(return_code=1, attempt_connected=True, all_images_verified=False,
                      retry_used=False, operation="upload", stop_requested=False)
        self.assertTrue(retry(**common, write_started=False))
        self.assertFalse(retry(**common, write_started=True))
        self.assertFalse(retry(**{**common, "stop_requested": True}, write_started=False))

    def test_failed_upload_cleanup_does_not_reset_the_board(self):
        tree = ast.parse((ROOT / "main/web_bridge.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "MCUWebBackendAPI")
        worker = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_upload_worker")
        releases = [node for node in ast.walk(worker)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_release_port_lines"]
        self.assertEqual(len(releases), 1)
        reset = next((kw.value for kw in releases[0].keywords if kw.arg == "pulse_reset"), None)
        self.assertIsInstance(reset, ast.Constant)
        self.assertFalse(reset.value)

    def test_clean_preserves_sources_settings_and_journal(self):
        cache = self.root / ".mcu_flasher_build_cache"
        keep = [self.root / "src/user.cpp", self.root / "platformio.ini",
                cache / ".mcu_ai_edits/session/edit.txt", cache / "project_state.json"]
        for path in keep:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep", encoding="utf-8")
        (cache / "boards").mkdir()
        (cache / "boards/object.o").write_bytes(b"object")
        self.api.clean_cache()
        self.run_worker()
        self.assertFalse((cache / "boards").exists())
        for path in keep:
            self.assertEqual(path.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main(verbosity=2)
