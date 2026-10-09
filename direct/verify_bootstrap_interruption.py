#!/usr/bin/env python3
"""Isolated Bootstrap child cancellation and HTTP timeout checks; no installs."""
from __future__ import annotations

import ast
from collections import deque
from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
from src.modules import offline_bootstrap as preparation


def monitor_definitions():
    source = ROOT / "src/modules/bootstrap.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    names = {"_stop_offline_setup_process", "_stream_offline_setup_output"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    scope = dict(threading=threading, time=time, os=os, sys=sys, subprocess=subprocess,
                 _record_bootstrap_log=Mock())
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), scope)
    return scope


class PreparationMonitorChecks(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "temp/audit/bootstrap-interruption"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.scope = monitor_definitions()
        self.gui = SimpleNamespace(_closed=False,
            _signals=SimpleNamespace(_closed=False, thread_id=threading.get_ident()),
            _offline_setup_done=threading.Event())
        self.gui._offline_setup_done.set()
        self.pending = deque()
        self.gui.root = SimpleNamespace(after=lambda delay, callback: self.pending.append(callback))
        self.events = []
        for kind in ("dim", "ok", "warn", "fail"):
            setattr(self.gui, "log_" + kind, lambda text, kind=kind: self.events.append((kind, text)))
        self.gui.update_platformio_progress_block = lambda text: self.events.append(("progress", text))
        self.gui.set_status = lambda text: self.events.append(("status", text))
        self.gui.clear_platformio_progress_block = lambda: self.events.append(("clear", ""))

    def child(self, code):
        options = dict(cwd=self.folder, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            options["start_new_session"] = True
        process = subprocess.Popen([sys.executable, "-B", "-u", "-c", code], **options)
        self.addCleanup(lambda: self.scope["_stop_offline_setup_process"](process))
        return process

    def monitor(self, process, *, close_when=None, completion=.2, drain=.2):
        result, errors = [], []
        def run():
            try:
                result.append(self.scope["_stream_offline_setup_output"](
                    self.gui, process, completion_timeout=completion, drain_timeout=drain))
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        start = time.monotonic()
        thread.start()
        while thread.is_alive() and time.monotonic() - start < 8:
            while self.pending:
                self.pending.popleft()()
            if close_when is not None and close_when():
                self.gui._closed = True
            time.sleep(.01)
        thread.join(.1)
        self.assertFalse(thread.is_alive(), "Preparation monitor did not finish within its fixture bound")
        while self.pending:
            self.pending.popleft()()
        self.assertTrue(self.gui._offline_setup_done.is_set())
        return result, errors, time.monotonic() - start

    def test_quiet_local_work_is_allowed_without_an_output_silence_watchdog(self):
        process = self.child("import time; time.sleep(.7); print('Builder ready: local fixture')")
        result, errors, elapsed = self.monitor(process, completion=.1, drain=.1)
        self.assertEqual(errors, [])
        self.assertEqual(result, [0])
        self.assertGreater(elapsed, .6)
        self.assertIn(("ok", "Builder ready: local fixture"), self.events)

    def test_output_eof_does_not_wait_forever_for_a_live_child(self):
        process = self.child("import os,time; os.close(1); os.close(2); time.sleep(120)")
        result, errors, elapsed = self.monitor(process)
        self.assertEqual(result, [])
        self.assertIsInstance(errors[0], TimeoutError)
        self.assertLess(elapsed, 6)
        self.assertIsNotNone(process.poll())

    def test_closed_setup_stops_and_reaps_original_child_and_descendants(self):
        import psutil
        process = self.child("import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(120)']); "
            "print('DESCENDANT=' + str(p.pid),flush=True); time.sleep(120)")
        def started():
            return any(text.startswith("DESCENDANT=") for _, text in self.events)
        result, errors, elapsed = self.monitor(process, close_when=started)
        self.assertEqual(result, [])
        self.assertIsInstance(errors[0], InterruptedError)
        self.assertLess(elapsed, 6)
        self.assertIsNotNone(process.poll())
        pid = int(next(text.split("=", 1)[1] for _, text in self.events if text.startswith("DESCENDANT=")))
        if psutil.pid_exists(pid):
            self.assertIn(psutil.Process(pid).status(), (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD))

    def test_exited_parent_cannot_leave_a_pipe_owning_descendant_running(self):
        import psutil
        process = self.child("import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(120)']); "
            "print('DESCENDANT=' + str(p.pid),flush=True); time.sleep(.7)")
        with patch.object(process, "terminate", wraps=process.terminate) as terminate, \
                patch.object(process, "kill", wraps=process.kill) as kill:
            result, errors, elapsed = self.monitor(process)
        self.assertEqual(result, [])
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertRegex(str(errors[0]), "held its output|owned children finished")
        self.assertLess(elapsed, 6)
        terminate.assert_not_called()
        kill.assert_not_called()
        pid = int(next(text.split("=", 1)[1] for _, text in self.events if text.startswith("DESCENDANT=")))
        if psutil.pid_exists(pid):
            self.assertIn(psutil.Process(pid).status(), (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD))

    def test_reader_error_reaps_child_and_reports_no_success(self):
        process = self.child("import time; time.sleep(120)")
        with patch("src.modules.bootstrap_output.output_chunks", side_effect=OSError("Fixture pipe failed")):
            result, errors, _ = self.monitor(process)
        self.assertEqual(result, [])
        self.assertIsInstance(errors[0], OSError)
        self.assertIsNotNone(process.poll())
        self.assertFalse(any(kind == "ok" for kind, _ in self.events))

    def test_printing_descendant_cannot_extend_teardown_after_parent_exit(self):
        import psutil
        child_code = "import time; exec('while True:\\n print(\\\"still writing\\\",flush=True); time.sleep(.02)')"
        code = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-B','-c',"
                + repr(child_code) + "],stdout=sys.stdout,stderr=sys.stderr); "
                "print('DESCENDANT=' + str(p.pid),flush=True); time.sleep(.7)")
        process = self.child(code)
        result, errors, elapsed = self.monitor(process)
        self.assertEqual(result, [])
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIn("owned children finished", str(errors[0]))
        self.assertLess(elapsed, 6)
        pid = int(next(text.split("=", 1)[1] for _, text in self.events if text.startswith("DESCENDANT=")))
        if psutil.pid_exists(pid):
            self.assertIn(psutil.Process(pid).status(), (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD))

    def test_failed_termination_retains_lifetime_until_captured_child_stops(self):
        process = self.child("import time; time.sleep(120)")
        stop = self.scope["_stop_offline_setup_process"]
        attempts = []
        def delayed_stop(child, descendants):
            attempts.append(child)
            self.assertFalse(self.gui._offline_setup_done.is_set())
            if len(attempts) == 1:
                raise PermissionError("Fixture temporary termination denial")
            return stop(child, descendants)
        self.scope["_stop_offline_setup_process"] = delayed_stop
        result, errors, _ = self.monitor(process, close_when=lambda: True)
        self.assertEqual(result, [])
        self.assertIsInstance(errors[0], InterruptedError)
        self.assertEqual(len(attempts), 2)
        self.assertIsNotNone(process.poll())
        diagnostics = [call for call in self.scope["_record_bootstrap_log"].call_args_list
                       if call.args[0] == "WARN"]
        self.assertEqual(len(diagnostics), 1)
        self.scope["_stop_offline_setup_process"] = stop


class BootstrapHTTPTimeoutChecks(unittest.TestCase):
    def setUp(self):
        from platformio import http
        import requests
        self.http = http
        self.requests = requests
        self.original = http.HTTPSession.request
        self.addCleanup(patch.stopall)
        patch.object(http.app, "get_setting", return_value=True).start()
        patch.object(http.app, "get_user_agent", return_value="MCU local verification").start()
        self.transport = patch.object(requests.Session, "request", return_value=Mock()).start()

    def test_effective_manager_calls_cap_unbounded_and_preserve_shorter_timeouts(self):
        session = self.http.HTTPSession()
        with preparation._bootstrap_http_timeouts():
            for value, expected in ((None, (10, 30)), ((2, 3), (2, 3)), ((120, 300), (10, 30)), (4, (4, 4))):
                with self.subTest(timeout=value):
                    session.get("https://fixture.invalid/archive", timeout=value,
                                verify="fixture-ca.pem", proxies={"https": "fixture-proxy"})
                    arguments = self.transport.call_args.kwargs
                    self.assertEqual(arguments["timeout"], expected)
                    self.assertEqual(arguments["verify"], "fixture-ca.pem")
                    self.assertEqual(arguments["proxies"], {"https": "fixture-proxy"})
            session.get("https://fixture.invalid/archive")
            self.assertEqual(self.transport.call_args.kwargs["timeout"], (10, 30))
            self.requests.Session().get("https://fixture.invalid/unrelated")
            self.assertNotIn("timeout", self.transport.call_args.kwargs)
        self.assertIs(self.http.HTTPSession.request, self.original)
        session.get("https://fixture.invalid/archive")
        self.assertEqual(self.transport.call_args.kwargs["timeout"], self.http.__default_requests_timeout__)

    def test_actual_file_downloader_uses_finite_default_without_installing(self):
        from platformio.package.download import FileDownloader
        response = Mock(status_code=200, headers={"content-length": "1"}, close=Mock())
        self.transport.return_value = response
        with preparation._bootstrap_http_timeouts():
            downloader = FileDownloader("https://fixture.invalid/board.zip")
            self.assertEqual(self.transport.call_args.kwargs["timeout"], (10, 30))
            self.assertTrue(self.transport.call_args.kwargs["stream"])
            downloader._http_response.close()
            downloader._http_session.close()
        self.assertIs(self.http.HTTPSession.request, self.original)

    def test_failure_and_nested_context_restore_original_session_method(self):
        with self.assertRaisesRegex(RuntimeError, "Fixture download failed"):
            with preparation._bootstrap_http_timeouts(read_timeout=12):
                outer = self.http.HTTPSession.request
                with preparation._bootstrap_http_timeouts(read_timeout=8):
                    self.http.HTTPSession().get("https://fixture.invalid/archive")
                    self.assertEqual(self.transport.call_args.kwargs["timeout"], (10, 8))
                self.assertIs(self.http.HTTPSession.request, outer)
                raise RuntimeError("Fixture download failed")
        self.assertIs(self.http.HTTPSession.request, self.original)

    def test_prepare_wraps_managers_and_does_not_change_builder_environment(self):
        environment = dict(os.environ)
        def manager(*args, **kwargs):
            self.http.HTTPSession().get("https://fixture.invalid/archive")
            self.assertEqual(self.transport.call_args.kwargs["timeout"], (10, 30))
            raise RuntimeError("Fixture preparation failed")
        with patch("src.modules.bootstrap_platformio.archive_paths", return_value=nullcontext()), \
                patch("src.modules.platformio_locks.package_locks", return_value=nullcontext()), \
                patch.object(preparation, "_prepare", side_effect=manager):
            with self.assertRaisesRegex(RuntimeError, "Fixture preparation failed"):
                preparation.prepare(ROOT / "temp/audit/bootstrap-interruption/fake-store")
        self.assertIs(self.http.HTTPSession.request, self.original)
        self.assertEqual(dict(os.environ), environment)

    def test_waiting_prepare_thread_cannot_overlap_or_leak_global_adapter(self):
        first_active, release = threading.Event(), threading.Event()
        errors, methods = [], []
        def manager(core, *args, **kwargs):
            methods.append(self.http.HTTPSession.request)
            self.http.HTTPSession().get("https://fixture.invalid/archive")
            self.assertEqual(self.transport.call_args.kwargs["timeout"], (10, 30))
            if str(core).endswith("first"):
                first_active.set()
                self.assertTrue(release.wait(2))
            return str(core)
        def run(name):
            try:
                preparation.prepare(ROOT / "temp/audit/bootstrap-interruption" / name)
            except Exception as error:
                errors.append(error)
        with patch("src.modules.bootstrap_platformio.archive_paths", side_effect=nullcontext), \
                patch("src.modules.platformio_locks.package_locks", side_effect=nullcontext), \
                patch.object(preparation, "_prepare", side_effect=manager):
            first = threading.Thread(target=run, args=("first",), daemon=True)
            second = threading.Thread(target=run, args=("second",), daemon=True)
            first.start()
            self.assertTrue(first_active.wait(1))
            first_method = self.http.HTTPSession.request
            second.start()
            time.sleep(.05)
            self.assertIs(self.http.HTTPSession.request, first_method)
            self.assertEqual(len(methods), 1)
            release.set()
            first.join(2)
            second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(methods), 2)
        self.assertIs(self.http.HTTPSession.request, self.original)

    def test_builder_child_does_not_inherit_the_manager_adapter(self):
        scratch = ROOT / "temp/audit/bootstrap-interruption"
        scratch.mkdir(parents=True, exist_ok=True)
        with preparation._bootstrap_http_timeouts():
            result = subprocess.run([sys.executable, "-B", "-c",
                "import platformio.http; print(platformio.http.__default_requests_timeout__)"],
                cwd=scratch, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.http.__default_requests_timeout__))


if __name__ == "__main__":
    unittest.main(verbosity=2)
