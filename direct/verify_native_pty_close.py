#!/usr/bin/env python3
"""Verify native PTY shutdown on either host using only mocked descriptors/processes."""
from __future__ import annotations

import errno
import importlib.util
import io
import signal
import subprocess
import sys
import unittest
import warnings
from contextlib import redirect_stderr
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "main/platforms/ubuntu_pty_process.py"


def load_native_pty():
    # Import POSIX-only dependencies as inert fixtures on Windows as well. No
    # descriptor, process, terminal, hardware or application state is created.
    spec = importlib.util.spec_from_file_location("native_pty_close_fixture", SOURCE)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"fcntl": ModuleType("fcntl"),
                                 "termios": ModuleType("termios")}), \
            warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        exec(compile(SOURCE.read_text(encoding="utf-8-sig"), str(SOURCE), "exec"),
             module.__dict__)
    return module


native_pty = load_native_pty()


def load_supervisor():
    source = ROOT / "main/platforms/ubuntu_pty_supervisor.py"
    spec = importlib.util.spec_from_file_location("native_pty_supervisor_fixture", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"fcntl": ModuleType("fcntl"),
                                 "termios": ModuleType("termios")}):
        exec(compile(source.read_text(encoding="utf-8-sig"), str(source), "exec"),
             module.__dict__)
    return module


supervisor = load_supervisor()


class NativePtyCloseChecks(unittest.TestCase):
    def make_process(self, results):
        handle = Mock(spec=subprocess.Popen)
        handle.pid = 1234
        handle.wait.side_effect = results
        return native_pty.NativePtyProcess(handle, 42), handle

    def timeout(self, seconds):
        return subprocess.TimeoutExpired("fixture-supervisor", seconds)

    def test_exited_supervisor_is_reaped_without_signals(self):
        process, handle = self.make_process([0])
        with patch.object(native_pty.os, "close") as close:
            process.close()
            process.close()
        close.assert_called_once_with(42)
        handle.wait.assert_called_once_with(timeout=1.75)
        handle.send_signal.assert_not_called()
        handle.kill.assert_not_called()
        self.assertTrue(process.closed)
        self.assertTrue(process.terminated)
        self.assertEqual(process.fd, -1)
        self.assertEqual(process.exitstatus, 0)
        self.assertIsNone(process.signalstatus)

    def test_descriptor_failure_propagates_after_reaping(self):
        process, handle = self.make_process([-signal.SIGTERM])
        failure = OSError(errno.EBADF, "fixture descriptor close failed")
        with patch.object(native_pty.os, "close", side_effect=failure) as close:
            with self.assertRaises(OSError) as caught:
                process.close()
        self.assertIs(caught.exception, failure)
        close.assert_called_once_with(42)
        handle.wait.assert_called_once_with(timeout=1.75)
        handle.send_signal.assert_not_called()
        handle.kill.assert_not_called()
        self.assertTrue(process.terminated)
        self.assertIsNone(process.exitstatus)
        self.assertEqual(process.signalstatus, signal.SIGTERM)

    def test_timeout_sends_term_then_reaps(self):
        process, handle = self.make_process([self.timeout(1.75), 0])
        with patch.object(native_pty.os, "close"):
            process.close()
        self.assertEqual(handle.mock_calls, [call.wait(timeout=1.75),
                         call.send_signal(signal.SIGTERM), call.wait(timeout=.4)])
        handle.kill.assert_not_called()
        self.assertEqual(process.exitstatus, 0)

    def test_forced_timeout_escalates_through_owned_handle(self):
        process, handle = self.make_process([self.timeout(1.75), self.timeout(.4), -9])
        with patch.object(native_pty.os, "close"):
            process.close()
        self.assertEqual(handle.mock_calls, [call.wait(timeout=1.75),
                         call.send_signal(signal.SIGTERM), call.wait(timeout=.4),
                         call.kill(), call.wait(timeout=.4)])
        self.assertTrue(process.terminated)
        self.assertEqual(process.signalstatus, 9)
        self.assertIsNone(process.exitstatus)

    def test_unforced_timeout_reports_failure_without_kill(self):
        process, handle = self.make_process([self.timeout(1.75), self.timeout(.4)])
        with patch.object(native_pty.os, "close"):
            with self.assertRaisesRegex(OSError, "The native terminal did not close"):
                process.close(force=False)
        self.assertEqual(handle.mock_calls, [call.wait(timeout=1.75),
                         call.send_signal(signal.SIGTERM), call.wait(timeout=.4)])
        handle.kill.assert_not_called()
        self.assertFalse(process.terminated)

    def test_final_reaping_timeout_is_not_suppressed(self):
        final_timeout = self.timeout(.4)
        process, handle = self.make_process([self.timeout(1.75), self.timeout(.4),
                                            final_timeout])
        with patch.object(native_pty.os, "close"):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                process.close()
        self.assertIs(caught.exception, final_timeout)
        handle.kill.assert_called_once_with()
        self.assertFalse(process.terminated)


class NativePtySupervisorChecks(unittest.TestCase):
    def signals(self):
        names = ("SIGINT", "SIGQUIT", "SIGTERM", "SIGHUP", "SIGTSTP",
                 "SIGTTIN", "SIGTTOU", "SIGCHLD", "SIG_IGN", "SIG_DFL")
        handlers = {}
        values = {name: index for index, name in enumerate(names, 1)}
        return SimpleNamespace(**values, signal=lambda number, handler: handlers.update({number: handler})), handlers

    def test_signal_cleanup_reconciles_the_owned_command_handle(self):
        for name in ("SIGHUP", "SIGTERM"):
            for cleaned, expected in ((True, None), (False, 125)):
                with self.subTest(signal=name, cleaned=cleaned):
                    signals, handlers = self.signals()
                    child = Mock(spec=subprocess.Popen)
                    order = []

                    def interrupted_wait(*, timeout):
                        handlers[getattr(signals, name)](getattr(signals, name), None)
                        raise subprocess.TimeoutExpired("fixture-command", timeout)

                    child.wait.side_effect = interrupted_wait
                    child.poll.side_effect = lambda: order.append("handle reconciled")

                    def cleanup(*_):
                        order.append("descendants reaped")
                        return cleaned

                    with patch.object(supervisor, "signal", signals), \
                            patch.object(supervisor, "_enable_subreaper"), \
                            patch("psutil.Process", return_value=Mock()), \
                            patch.object(supervisor.subprocess, "Popen", return_value=child), \
                            patch.object(supervisor, "_close_descendants", side_effect=cleanup), \
                            redirect_stderr(io.StringIO()):
                        result = supervisor.supervise(["fixture-command"])
                    self.assertEqual(result, expected if expected is not None else 128 + getattr(signals, name))
                    self.assertEqual(order, ["descendants reaped", "handle reconciled"])
                    child.poll.assert_called_once_with()

    def test_completed_command_keeps_its_exit_status_after_cleanup(self):
        signals, _ = self.signals()
        child = Mock(spec=subprocess.Popen)
        child.wait.return_value = 17
        with patch.object(supervisor, "signal", signals), \
                patch.object(supervisor, "_enable_subreaper"), \
                patch("psutil.Process", return_value=Mock()), \
                patch.object(supervisor.subprocess, "Popen", return_value=child), \
                patch.object(supervisor, "_close_descendants", return_value=True):
            self.assertEqual(supervisor.supervise(["fixture-command"]), 17)
        child.poll.assert_called_once_with()

    def test_failed_containment_never_spawns_a_command(self):
        with patch.object(supervisor, "_enable_subreaper", side_effect=OSError("fixture custody failure")), \
                patch.object(supervisor.subprocess, "Popen") as spawn, \
                redirect_stderr(io.StringIO()) as diagnostic:
            self.assertEqual(supervisor.supervise(["fixture-command"]), 125)
        spawn.assert_not_called()
        self.assertIn("Bootstrap --repair", diagnostic.getvalue())

if __name__ == "__main__":
    unittest.main(verbosity=2)
