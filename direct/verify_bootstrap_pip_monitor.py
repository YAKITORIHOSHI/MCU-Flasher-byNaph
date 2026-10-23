#!/usr/bin/env python3
"""Real owned children and source-isolated pip checks; no pip/network/install.

Only fixtures below temp/audit are written. No production Bootstrap import,
GUI, hardware, settings, package store or installer is activated.
"""
from __future__ import annotations

import ast
import ctypes
from ctypes import wintypes
import importlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import bootstrap_pip_monitor as monitor


def definitions(names, folder):
    source = ROOT / "src/modules/bootstrap.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    scope = dict(Optional=Optional, BootstrapGUI=object, Path=Path, os=os, sys=sys,
                 shutil=shutil, subprocess=subprocess, threading=threading, re=re,
                 importlib=importlib, SCRIPT_DIR=folder, _gui=None,
                 _get_target_python=lambda: Path(sys.executable),
                 _ensure_pywin32_system32_dlls=Mock(),
                 _is_network_reachable=Mock(return_value=True),
                 _sync_private_python_site_packages=Mock(),
                 _record_bootstrap_log=Mock(), status=Mock(), ok=Mock(),
                 warn=Mock(), fail=Mock(), _LAST_PIP_ERROR="")
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), scope)
    return scope


class PipMonitorChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp/audit/bootstrap-pip-monitor"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.log = self.folder / "pip.log"
        self.processes = []

    def run_child(self, code, **kwargs):
        original = subprocess.Popen
        def capture(command, **options):
            process = original(command, cwd=self.folder, **options)
            self.processes.append(process)
            return process
        with patch.object(subprocess, "Popen", side_effect=capture):
            result = monitor.run([sys.executable, "-B", "-u", "-c", code],
                                 env=os.environ.copy(), log_path=self.log,
                                 **kwargs)
        for process in self.processes:
            self.assertIsNotNone(process.poll(), "Owned parent was not reaped")
        return result

    def assert_descendant_stopped(self, result):
        pid = int(re.search(r"DESCENDANT=(\d+)", result.output).group(1))
        if sys.platform != "win32":
            status = Path(f"/proc/{pid}/status")
            if status.is_file():
                self.assertRegex(status.read_text(), r"State:\s+[ZX]", "Owned descendant survived")
        else:
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
            kernel.WaitForSingleObject.restype = ctypes.c_ulong
            kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
            handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            if handle:
                try:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0)
                finally:
                    kernel.CloseHandle(handle)

    def test_healthy_quiet_child_survives_without_silence_deadline(self):
        result = self.run_child("import time; print('started'); time.sleep(.45); print('finished')",
                                timeout=3)
        self.assertTrue(result.ok, result.summary)
        self.assertGreaterEqual(result.elapsed, .4)
        self.assertIn("finished", result.output)
        self.assertIn("completed", self.log.read_text())

    def test_exited_child_output_backlog_is_drained_without_a_false_pipe_failure(self):
        seen = []
        def slow_display(line):
            seen.append(line)
            time.sleep(.07)
        result = self.run_child("for i in range(40): print('record-' + str(i))",
                                timeout=8, on_line=slow_display)
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(seen, [f"record-{index}" for index in range(40)])
        self.assertGreater(result.elapsed, 2)

    def test_output_does_not_reset_absolute_deadline_and_timeout_output_is_saved(self):
        result = self.run_child("import time\nfor i in range(200):\n print('working', flush=True)\n time.sleep(.03)",
                                timeout=.3)
        self.assertFalse(result.ok)
        self.assertTrue(result.reason.startswith("absolute deadline"), result.summary)
        self.assertGreaterEqual(result.elapsed, .3)
        self.assertLess(result.elapsed, 5)
        text = self.log.read_text()
        self.assertIn("working", text)
        self.assertIn("0.3s budget", text)
        self.assertIn(f"{result.elapsed:.2f}s elapsed", text)

    def test_explicit_cancel_stops_original_and_descendant_without_psutil(self):
        ready = threading.Event()
        code = ("import subprocess,sys,time\n"
                "p=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(120)'])\n"
                "print('DESCENDANT=' + str(p.pid),flush=True)\n"
                "time.sleep(120)")
        with patch.dict(sys.modules, {"psutil": None}):
            result = self.run_child(code, timeout=5, cancelled=ready.is_set,
                                    on_line=lambda line: ready.set() if line.startswith("DESCENDANT=") else None)
        self.assertEqual(result.reason, "cancelled")
        self.assertLess(result.elapsed, 5)
        self.assert_descendant_stopped(result)
        self.assertIn("cancelled", self.log.read_text())

    def test_exited_parent_cannot_leave_inherited_pipe_child_without_psutil(self):
        code = ("import subprocess,sys\n"
                "p=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(120)'])\n"
                "print('DESCENDANT=' + str(p.pid),flush=True)")
        with patch.dict(sys.modules, {"psutil": None}):
            result = self.run_child(code, timeout=20)
        self.assertEqual(result.reason, "output pipe remained open after child exit")
        self.assertLess(result.elapsed, 7)
        self.assert_descendant_stopped(result)

    @unittest.skipIf(sys.platform == "win32", "Native POSIX signal fixture")
    def test_deadline_kills_term_ignoring_descendant_without_psutil(self):
        code = ("import subprocess,sys,time\n"
                "p=subprocess.Popen([sys.executable,'-B','-u','-c',"
                "\"import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('descendant ready',flush=True); time.sleep(120)\"])\n"
                "print('DESCENDANT=' + str(p.pid),flush=True)\n"
                "time.sleep(120)")
        with patch.dict(sys.modules, {"psutil": None}):
            result = self.run_child(code, timeout=.5)
        self.assertTrue(result.reason.startswith("absolute deadline"))
        self.assertIn("descendant ready", result.output)
        self.assert_descendant_stopped(result)

    def test_fast_success_and_ordinary_failure_preserve_real_outcomes(self):
        success = self.run_child("pass", timeout=3)
        self.assertTrue(success.ok, success.summary)
        failure = self.run_child("print('ERROR: fixture rejected'); raise SystemExit(7)", timeout=3)
        self.assertEqual(failure.returncode, 7)
        self.assertEqual(failure.reason, "completed")
        self.assertIn("ERROR: fixture rejected", self.log.read_text())

    def test_closed_setup_never_starts_child(self):
        with patch.object(subprocess, "Popen") as start:
            result = monitor.run(["not-an-installer"], env={}, timeout=3, cancelled=lambda: True)
        start.assert_not_called()
        self.assertEqual(result.reason, "cancelled before launch")

    def test_cleanup_failure_waits_for_stop_then_is_terminal(self):
        original_stop = monitor._stop
        attempts = []
        reported = []
        def denied_once(process, job):
            attempts.append(process)
            if len(attempts) == 1:
                raise PermissionError("fixture stop denied")
            return original_stop(process, job)
        with patch.object(monitor, "_stop", side_effect=denied_once):
            with self.assertRaises(monitor.PipCleanupError):
                self.run_child("import time; print('before cleanup'); time.sleep(120)",
                               timeout=.2, on_cleanup_failure=reported.append)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(reported), 1)
        self.assertTrue(all(process.poll() is not None for process in self.processes))
        self.assertIn("cleanup incomplete", self.log.read_text())
        self.assertIn("before cleanup", self.log.read_text())

    def test_failed_job_handle_close_retains_handle_until_confirmed_closed(self):
        job = monitor._WindowsJob.__new__(monitor._WindowsJob)
        job.handle = 123
        job.kernel = SimpleNamespace(CloseHandle=Mock(side_effect=[False, True]))
        job.ctypes = SimpleNamespace(WinError=lambda error: OSError("fixture CloseHandle failed"),
                                     get_last_error=lambda: 5)
        with self.assertRaises(OSError):
            job.close()
        self.assertEqual(job.handle, 123)
        self.assertIn(job, monitor._RETAINED_JOBS)
        job.close()
        self.assertIsNone(job.handle)
        self.assertNotIn(job, monitor._RETAINED_JOBS)

    def test_running_helper_before_suspended_thread_cannot_complete_resume(self):
        for counts, succeeds in (([0, 1], True), ([0, 0], False), ([0, 2], False)):
            with self.subTest(counts=counts):
                job = monitor._WindowsJob.__new__(monitor._WindowsJob)
                job.handle = 100
                job.ctypes = SimpleNamespace(Structure=ctypes.Structure, POINTER=ctypes.POINTER,
                    byref=ctypes.byref, sizeof=ctypes.sizeof, c_void_p=ctypes.c_void_p,
                    WinError=lambda error: OSError("fixture kernel error"), get_last_error=lambda: 5)
                job.wintypes = wintypes
                index = [0]
                resumed, closed = [], []
                def entry(pointer):
                    pointer._obj.process_id = 42
                    pointer._obj.thread_id = index[0] + 1
                    return True
                def first(snapshot, pointer):
                    index[0] = 0
                    return entry(pointer)
                def following(snapshot, pointer):
                    index[0] += 1
                    return entry(pointer) if index[0] < len(counts) else False
                def resume(handle):
                    resumed.append(handle)
                    return counts[handle - 1]
                job.kernel = SimpleNamespace(AssignProcessToJobObject=lambda *args: True,
                    CreateToolhelp32Snapshot=lambda *args: 200,
                    Thread32First=first, Thread32Next=following,
                    OpenThread=lambda access, inherit, thread_id: thread_id,
                    GetProcessIdOfThread=lambda handle: 42,
                    WaitForSingleObject=lambda handle, timeout: 258,
                    ResumeThread=resume, CloseHandle=lambda handle: closed.append(handle))
                if succeeds:
                    job.assign_and_resume(SimpleNamespace(pid=42, _handle=300))
                else:
                    with self.assertRaises(OSError):
                        job.assign_and_resume(SimpleNamespace(pid=42, _handle=300))
                self.assertEqual(resumed, [1, 2])
                self.assertEqual(closed, [1, 2, 200])

    def test_snapshot_threads_are_pinned_and_foreign_or_vanished_helpers_are_skipped(self):
        job = monitor._WindowsJob.__new__(monitor._WindowsJob)
        job.handle = 100
        job.ctypes = SimpleNamespace(Structure=ctypes.Structure, POINTER=ctypes.POINTER,
            byref=ctypes.byref, sizeof=ctypes.sizeof, c_void_p=ctypes.c_void_p,
            WinError=lambda error: OSError("fixture kernel error"), get_last_error=lambda: 87)
        job.wintypes = wintypes
        events, closed, index = [], [], [0]
        def entry(pointer):
            pointer._obj.process_id = 42
            pointer._obj.thread_id = index[0] + 1
            return True
        def first(snapshot, pointer):
            return entry(pointer)
        def following(snapshot, pointer):
            index[0] += 1
            return entry(pointer) if index[0] < 4 else False
        def opened(access, inherit, tid):
            self.assertEqual(access & 0x802, 0x802)
            events.append(("open", tid))
            return 0 if tid == 2 else tid  # A helper vanished after snapshot.
        def resumed(handle):
            events.append(("resume", handle))
            return 1
        job.kernel = SimpleNamespace(AssignProcessToJobObject=lambda *args: True,
            CreateToolhelp32Snapshot=lambda *args: 200, Thread32First=first, Thread32Next=following,
            OpenThread=opened, GetProcessIdOfThread=lambda handle: 999 if handle == 1 else 42,
            WaitForSingleObject=lambda handle, timeout: 0 if handle == 3 else 258,
            ResumeThread=resumed, CloseHandle=lambda handle: closed.append(handle))
        job.assign_and_resume(SimpleNamespace(pid=42, _handle=300))
        self.assertEqual(events, [("open", 1), ("open", 2), ("open", 3), ("open", 4), ("resume", 4)])
        self.assertEqual(closed, [1, 3, 4, 200])

    @unittest.skipUnless(sys.platform == "win32", "Native Windows suspended/Job fixture")
    def test_containment_failure_reaps_suspended_child_before_it_runs(self):
        original_close = monitor._WindowsJob.close
        with patch.object(monitor._WindowsJob, "assign_and_resume",
                          side_effect=OSError("fixture containment refused")), \
                patch.object(monitor._WindowsJob, "close", autospec=True,
                             side_effect=original_close) as closed:
            result = self.run_child("from pathlib import Path; Path('must-not-run').write_text('ran')",
                                    timeout=3)
        self.assertEqual(result.reason, "monitor failed")
        self.assertIn("fixture containment refused", result.output)
        self.assertFalse((self.folder / "must-not-run").exists())
        closed.assert_called_once()

    @unittest.skipUnless(sys.platform == "win32", "Native Windows multiple-thread fixture")
    def test_native_running_helper_thread_does_not_leave_initial_thread_suspended(self):
        original_resume = monitor._WindowsJob.assign_and_resume
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        kernel.GetProcAddress.argtypes = (wintypes.HMODULE, ctypes.c_char_p)
        kernel.GetProcAddress.restype = ctypes.c_void_p
        kernel.CreateRemoteThread.argtypes = (wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        kernel.CreateRemoteThread.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        def add_helper(job, process):
            # Only our owned suspended fixture child is affected. A native
            # helper already running must not be mistaken for initial resume.
            sleep = kernel.GetProcAddress(kernel.GetModuleHandleW("kernel32.dll"), b"Sleep")
            self.assertTrue(sleep)
            thread = kernel.CreateRemoteThread(int(process._handle), None, 0, sleep,
                                               ctypes.c_void_p(10000), 0, None)
            self.assertTrue(thread, f"CreateRemoteThread failed: {ctypes.get_last_error()}")
            try:
                return original_resume(job, process)
            finally:
                kernel.CloseHandle(thread)
        with patch.object(monitor._WindowsJob, "assign_and_resume", new=add_helper):
            result = self.run_child("print('initial thread ran')", timeout=5)
        self.assertTrue(result.ok, result.summary)
        self.assertIn("initial thread ran", result.output)

    @unittest.skipUnless(sys.platform == "win32", "Native Windows foreign-thread identity fixture")
    def test_native_foreign_thread_handle_is_never_resumed(self):
        original_resume = monitor._WindowsJob.assign_and_resume
        def foreign_handles(job, process):
            kernel = job.kernel
            kernel.GetCurrentThreadId.argtypes = ()
            kernel.GetCurrentThreadId.restype = wintypes.DWORD
            current_tid = kernel.GetCurrentThreadId()
            original_open = kernel.OpenThread
            original_open.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            original_open.restype = wintypes.HANDLE
            def foreign(access, inherit, thread_id):
                return original_open(access, inherit, current_tid)
            with patch.object(kernel, "OpenThread", new=foreign), \
                    patch.object(kernel, "ResumeThread", side_effect=AssertionError("Foreign thread resumed")) as resume:
                try:
                    return original_resume(job, process)
                finally:
                    resume.assert_not_called()
        with patch.object(monitor._WindowsJob, "assign_and_resume", new=foreign_handles):
            result = self.run_child("print('must not execute')", timeout=5)
        self.assertEqual(result.reason, "monitor failed")
        self.assertIn("no resumable thread", result.output)
        self.assertNotIn("must not execute", result.output)

    def test_diagnostic_tail_is_bounded_and_credentials_are_redacted(self):
        result = self.run_child("print('x'*400000); print('https://user:secret@example.invalid/a?token=hidden'); "
                                "print('Authorization: Bearer private'); raise SystemExit(1)", timeout=3)
        self.assertLessEqual(len(result.output), monitor._TAIL_CHARS)
        for secret in ("user:secret", "token=hidden", "Bearer private"):
            self.assertNotIn(secret, result.output)
            self.assertNotIn(secret, self.log.read_text())

    def test_large_qt_budgets_are_finite_and_distinct_from_small_packages(self):
        for sid in ("pyside6", "pyqt5_qscintilla"):
            self.assertEqual(monitor.download_timeout(sid), 900)
            self.assertEqual(monitor.install_timeout({sid}), 900)
            self.assertEqual(monitor.install_timeout({sid}, recovery=True), 900)
        self.assertEqual(monitor.download_timeout("pyserial"), 180)
        self.assertEqual(monitor.download_timeout("platformio"), 360)
        self.assertEqual(monitor.install_timeout({"pyserial"}), 300)
        with self.assertRaises(ValueError):
            monitor.run([], env={}, timeout=float("inf"))

    def test_qt_update_transactions_share_the_finite_large_package_budget(self):
        scope = definitions({"_pip_upgrade"}, self.folder)
        scope["_run_pip_install"] = Mock(return_value=True)
        for package in ("PySide6", "pyqt5", "QScintilla"):
            self.assertTrue(scope["_pip_upgrade"](package))
            scope["_run_pip_install"].assert_called_with(["--upgrade", package], timeout=900)
        self.assertTrue(scope["_pip_upgrade"]("pyserial"))
        scope["_run_pip_install"].assert_called_with(["--upgrade", "pyserial"])

    def test_installed_dependencies_bypass_all_download_install_and_temp_work(self):
        scope = definitions({"ensure_pip_packages_parallel"}, self.folder)
        scope["PIP_PACKAGES_SPEC"] = [dict(id="pyside6", name="Qt fixture",
            check=lambda: True, pip_args=["not-a-real-package"], critical=True)]
        scope["_run_pip_install"] = Mock(side_effect=AssertionError("Unexpected install"))
        with patch.object(monitor, "run", side_effect=AssertionError("Unexpected child")), \
                patch.object(tempfile, "mkdtemp", side_effect=AssertionError("Unexpected temporary files")):
            self.assertTrue(scope["ensure_pip_packages_parallel"]())
        scope["_run_pip_install"].assert_not_called()
        scope["_is_network_reachable"].assert_not_called()

    def test_download_wrapper_uses_qt_budget_and_closed_setup_never_installs(self):
        scope = definitions({"ensure_pip_packages_parallel"}, self.folder)
        gui = SimpleNamespace(_closed=False, set_status=Mock(), set_progress_percent=Mock(),
                              update_pip_table_block=Mock(), commit_pip_table_block=Mock())
        scope["PIP_PACKAGES_SPEC"] = [dict(id="pyside6", name="Qt fixture",
            check=lambda: False, pip_args=["not-a-real-package"], critical=True)]
        scope["_run_pip_install"] = Mock(side_effect=AssertionError("Closed setup tried installation"))
        from src.modules import windows_tool_paths
        original_run = monitor.run
        def cancelled_download(command, **options):
            self.assertEqual(command[1:4], ["-m", "pip", "download"])
            self.assertEqual(options["timeout"], 900)
            self.assertIn("--only-binary", command)
            self.assertIn("--retries", command)
            options["on_line"] = lambda line: setattr(gui, "_closed", True)
            return original_run([sys.executable, "-B", "-u", "-c",
                                 "import time; print('fixture download started'); time.sleep(120)"],
                                **options)
        temporary = self.folder / "download-fixture"
        temporary.mkdir()
        with patch.object(windows_tool_paths, "pip_environment", return_value=os.environ.copy()), \
                patch.object(monitor, "run", side_effect=cancelled_download), \
                patch.object(tempfile, "mkdtemp", return_value=str(temporary)):
            self.assertFalse(scope["ensure_pip_packages_parallel"](gui))
        scope["_run_pip_install"].assert_not_called()
        self.assertIn("cancelled", (self.folder / "logs/bootstrap_pip.log").read_text())

    def test_download_cleanup_failure_cannot_enter_unified_install_or_delete_staging(self):
        scope = definitions({"ensure_pip_packages_parallel"}, self.folder)
        scope["PIP_PACKAGES_SPEC"] = [dict(id="pyside6", name="Qt fixture",
            check=lambda: False, pip_args=["not-a-real-package"], critical=True)]
        scope["_run_pip_install"] = Mock(side_effect=AssertionError("Fatal cleanup started install"))
        from src.modules import windows_tool_paths
        temporary = self.folder / "retained-download"
        temporary.mkdir()
        with patch.object(windows_tool_paths, "pip_environment", return_value={}), \
                patch.object(monitor, "run", side_effect=monitor.PipCleanupError("fixture cleanup failed")) as run, \
                patch.object(tempfile, "mkdtemp", return_value=str(temporary)):
            with self.assertRaises(monitor.PipCleanupError):
                scope["ensure_pip_packages_parallel"]()
        run.assert_called_once()
        scope["_run_pip_install"].assert_not_called()
        self.assertTrue(temporary.is_dir())

    def test_install_cleanup_failure_cannot_enter_no_cache_or_individual_recovery(self):
        scope = definitions({"ensure_pip_packages_parallel", "_run_pip_install"}, self.folder)
        scope["PIP_PACKAGES_SPEC"] = [dict(id="pyside6", name="Qt fixture",
            check=lambda: False, pip_args=["not-a-real-package"], critical=True)]
        from src.modules import windows_tool_paths
        temporary = self.folder / "retained-install"
        temporary.mkdir()
        def outcomes(command, **options):
            if command[3] == "download":
                return monitor.PipResult(0, .01, "completed", "")
            raise monitor.PipCleanupError("fixture cleanup failed")
        with patch.object(windows_tool_paths, "pip_environment", return_value={}), \
                patch.object(monitor, "run", side_effect=outcomes) as run, \
                patch.object(tempfile, "mkdtemp", return_value=str(temporary)):
            with self.assertRaises(monitor.PipCleanupError):
                scope["ensure_pip_packages_parallel"]()
        self.assertEqual(run.call_count, 2)  # Exactly one prefetch, one install.
        self.assertEqual(scope["_LAST_PIP_ERROR"], "fixture cleanup failed")
        self.assertTrue(temporary.is_dir())

    def test_actual_install_wrapper_records_deadline_elapsed_and_stops_on_close(self):
        scope = definitions({"_run_pip_install"}, self.folder)
        from src.modules import windows_tool_paths
        original = subprocess.Popen
        processes = []
        def fixture_child(command, **options):
            self.assertEqual(command[1:4], ["-m", "pip", "install"])
            self.assertIn("--timeout", command)
            self.assertIn("--retries", command)
            process = original([sys.executable, "-B", "-u", "-c",
                                "import time; print('fixture install output'); time.sleep(120)"],
                               cwd=self.folder, **options)
            processes.append(process)
            return process
        with patch.object(windows_tool_paths, "pip_environment", return_value=os.environ.copy()), \
                patch.object(subprocess, "Popen", side_effect=fixture_child):
            self.assertFalse(scope["_run_pip_install"](["fixture"], timeout=.25))
        self.assertIn("absolute deadline (0.25s budget)", scope["_LAST_PIP_ERROR"])
        self.assertIn("elapsed", scope["_LAST_PIP_ERROR"])
        self.assertIn("fixture install output", (self.folder / "logs/bootstrap_pip.log").read_text())
        self.assertTrue(all(process.poll() is not None for process in processes))
        scope["_gui"] = SimpleNamespace(_closed=True, start_busy=Mock(), stop_busy=Mock())
        with patch.object(windows_tool_paths, "pip_environment", return_value={}):
            with patch.object(subprocess, "Popen") as start:
                self.assertFalse(scope["_run_pip_install"](["fixture"], timeout=1))
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
