"""Cancelled Compile -> Clean ownership regressions; no live builds/deletion."""
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from main import web_bridge
API = web_bridge.MCUWebBackendAPI


class LifecycleChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / "temp/audit").mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(dir=ROOT / "temp/audit")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        api = API.__new__(API)
        self.api = api
        api.is_busy = True
        api.active_operation = "compile"
        api._current_op_phase = "compiling"
        api._op_session_id = 1
        api._stop_requested = False
        api._active_process = Mock(pid=123, poll=Mock(return_value=None))
        api._operation_worker = Mock(is_alive=Mock(return_value=False))
        api._framework_download_active = False
        api.sketch_dir_path = self.root
        api.current_board = "isolated fixture target"
        api.current_port = ""
        api.emit = Mock()
        api._kill_active_process_tree = Mock()
        api._effective_cache_root = Mock(return_value=self.root)
        api._clean_temporary_compile_artifacts = Mock()
        api._block_if_pending_ai_edits = Mock(return_value=False)
        self.targets = []
        def thread(*args, **kwargs):
            self.targets.append(kwargs["target"])
            return Mock(start=Mock(), is_alive=Mock(return_value=False))
        self.thread_patch = patch.object(web_bridge.threading, "Thread", side_effect=thread)
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)

    def next_clean(self):
        self.api._begin_operation_session()
        self.api.is_busy = True
        self.api.active_operation = "clean"
        self.api._current_op_phase = "cleaning"
        self.api._operation_worker = Mock(is_alive=Mock(return_value=True))
        self.api._active_process = None

    def test_delayed_stop_before_kill_cannot_target_next_operation(self):
        self.api.stop_operation()
        self.next_clean()
        self.targets[0]()
        self.api._kill_active_process_tree.assert_not_called()
        self.api._clean_temporary_compile_artifacts.assert_not_called()
        self.assertTrue(self.api.is_busy)
        self.assertEqual(self.api.active_operation, "clean")

    def test_delayed_failsafe_after_kill_cannot_clear_clean(self):
        process = self.api._active_process
        self.api.stop_operation()
        with patch.object(web_bridge.time, "sleep", side_effect=lambda _: self.next_clean()):
            self.targets[0]()
        self.api._kill_active_process_tree.assert_called_once_with(process)
        self.api._clean_temporary_compile_artifacts.assert_not_called()
        self.assertEqual(self.api._current_op_phase, "cleaning")
        self.assertTrue(self.api.is_busy)

    def test_live_worker_keeps_stopping_state(self):
        self.api._operation_worker.is_alive.return_value = True
        self.api._active_process.poll.return_value = 0
        self.api.stop_operation()
        with patch.object(web_bridge.time, "sleep"):
            self.targets[0]()
        self.assertTrue(self.api.is_busy)
        self.api._clean_temporary_compile_artifacts.assert_not_called()

    def test_clean_waits_for_old_worker_after_phase_finished(self):
        self.api.is_busy = False
        self.api.active_operation = None
        self.api._current_op_phase = None
        self.api._operation_worker.is_alive.return_value = True
        self.api.clean_cache()
        self.assertEqual(self.targets, [])
        self.assertEqual(self.api._op_session_id, 1)

    def test_clean_owns_new_session_and_worker(self):
        self.api.is_busy = False
        self.api.active_operation = None
        with patch.object(web_bridge, "load_gui_config", return_value={}):
            self.api.clean_cache()
        self.assertEqual(self.api._op_session_id, 2)
        self.assertEqual(self.api.active_operation, "clean")
        self.assertIsNotNone(self.api._operation_worker)
        self.assertEqual(len(self.targets), 1)

    def test_compile_and_upload_get_new_sessions_at_request(self):
        for method in (self.api.compile_sketch, self.api.upload_sketch):
            self.api.is_busy = False
            self.api.active_operation = None
            self.api._operation_worker = None
            prior = self.api._op_session_id
            self.api._resolve_board_info = Mock(return_value={"upload_protocol": "custom"})
            with patch.object(web_bridge, "load_gui_config", return_value={}), \
                 patch("main.core.target_profile.upload_target_ready", return_value=True):
                method()
            self.assertEqual(self.api._op_session_id, prior + 1)

    def test_stop_during_resolution_is_not_erased_by_compile(self):
        self.api._resolve_requested_target = Mock(side_effect=lambda _: setattr(self.api, "_stop_requested", True) or True)
        self.api._compile_worker = Mock()
        API._compile_requested_worker.__wrapped__(self.api)
        self.api._compile_worker.assert_not_called()
        self.assertFalse(self.api.is_busy)
        self.assertTrue(self.api._stop_requested)
        self.api._stop_requested = True
        self.assertFalse(self.api._compile_worker_impl())

    def test_stopped_upload_never_enters_write_preparation(self):
        self.api._stop_requested = True
        self.api._resolve_board_info = Mock(side_effect=AssertionError("Cancelled upload inspected hardware"))
        self.api._start_resolved_upload({})
        self.assertFalse(self.api.is_busy)
        self.api._resolve_board_info.assert_not_called()

    def test_exited_process_never_uses_pid_termination(self):
        process = Mock(poll=Mock(return_value=0))
        with patch.object(web_bridge.subprocess, "run") as run, patch("psutil.Process") as process_lookup:
            API._kill_active_process_tree(self.api, process)
        run.assert_not_called()
        process_lookup.assert_not_called()
        process.kill.assert_not_called()

    def test_windows_kill_uses_captured_handle_and_children(self):
        process = Mock(pid=123, poll=Mock(return_value=None))
        child = Mock()
        with patch.object(web_bridge.sys, "platform", "win32"), \
             patch("psutil.Process", return_value=Mock(children=Mock(return_value=[child]))), \
             patch.object(web_bridge.subprocess, "run") as run:
            API._kill_active_process_tree(self.api, process)
        child.kill.assert_called_once()
        process.kill.assert_called_once()
        run.assert_not_called()

    def test_stop_thread_failure_preserves_busy_and_allows_retry(self):
        with patch.object(web_bridge.threading, "Thread", side_effect=RuntimeError("fixture thread limit")):
            self.api.stop_operation()
        self.assertTrue(self.api.is_busy)
        self.assertFalse(self.api._stop_requested)

    def test_deferred_stop_revalidates_write_phase(self):
        self.api.active_operation = "upload"
        self.api._current_op_phase = "connecting"
        self.api.stop_operation()
        self.api._current_op_phase = "flashing"
        self.targets[0]()
        self.api._kill_active_process_tree.assert_not_called()
        self.assertTrue(self.api.is_busy)

    def test_real_owned_process_tree_is_reaped_without_touching_next_target(self):
        import psutil
        # The only real processes are two private Python sleep fixtures.
        script = "import subprocess,sys,time; child=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(30)']); print(child.pid,flush=True); time.sleep(30)"
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
        process = subprocess.Popen([sys.executable, "-B", "-c", script], cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, **options)
        child = None
        try:
            child = psutil.Process(int(process.stdout.readline().strip()))
            # A replacement target is deliberately present; cancellation keeps
            # using the captured fixture process rather than the shared field.
            replacement = Mock(poll=Mock(return_value=None))
            self.api._active_process = replacement
            API._kill_active_process_tree(self.api, process)
            process.wait(timeout=5)
            deadline = time.monotonic() + 3
            while child.is_running() and time.monotonic() < deadline:
                if child.status() == psutil.STATUS_ZOMBIE:
                    break
                time.sleep(0.01)
            self.assertTrue(not child.is_running() or child.status() == psutil.STATUS_ZOMBIE)
            replacement.kill.assert_not_called()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            if child is not None and child.is_running():
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
