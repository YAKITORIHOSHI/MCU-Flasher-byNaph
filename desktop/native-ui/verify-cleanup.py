#!/usr/bin/env python3
"""Owned native-smoke process lifecycle regression checks, without a GUI."""
import importlib.util
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("native_smoke", Path(__file__).with_name("verify-smoke.py"))
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class CleanupChecks(unittest.TestCase):
    def fixture(self, ignore_term):
        child = "import signal,time;" + ("signal.signal(signal.SIGTERM,signal.SIG_IGN);" if ignore_term else "") + "print('ready',flush=True);time.sleep(60)"
        leader = "import subprocess,sys; child=subprocess.Popen([sys.executable,'-c',sys.argv[1]],stdout=subprocess.PIPE); assert child.stdout.readline()==b'ready\\n'; print(child.pid,flush=True)"
        process = SMOKE.spawn_owned([sys.executable, "-c", leader, child], stdout=subprocess.PIPE)
        self.addCleanup(SMOKE.stop_owned, process)
        self.addCleanup(process.stdout.close)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], "Owned child did not announce readiness.")
        child_pid = int(process.stdout.readline(32))
        child_fd = os.pidfd_open(child_pid)
        self.addCleanup(os.close, child_fd)

        def cleanup_child():
            try:
                signal.pidfd_send_signal(child_fd, signal.SIGKILL)
            except ProcessLookupError:
                pass

        self.addCleanup(cleanup_child)
        self.assertEqual(process.wait(timeout=5), 0, "Fixture leader must exit before cleanup.")
        return process, child_fd

    def test_exited_leader_does_not_leave_its_live_child(self):
        process, child_fd = self.fixture(False)
        SMOKE.stop_owned(process)
        self.assertTrue(select.select([child_fd], [], [], 1)[0], "Cleanup skipped a live child after the session leader exited.")

    def test_exited_leader_still_escalates_its_stubborn_child(self):
        process, child_fd = self.fixture(True)
        SMOKE.stop_owned(process)
        self.assertTrue(select.select([child_fd], [], [], 1)[0], "Cleanup did not kill the owned SIGTERM-ignoring child.")

    def test_wait_keeps_native_child_identity_until_cleanup(self):
        process, _ = self.fixture(False)
        self.assertIsNotNone(os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT))

    def test_cleanup_leaves_an_unrelated_session_alive(self):
        unrelated = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
        unrelated_fd = os.pidfd_open(unrelated.pid)
        try:
            process, _ = self.fixture(False)
            SMOKE.stop_owned(process)
            self.assertFalse(select.select([unrelated_fd], [], [], 0)[0], "Cleanup signaled an unrelated session.")
            with mock.patch.object(os, "killpg", side_effect=AssertionError("Closed group must not be signaled again.")):
                SMOKE.stop_owned(process)
        finally:
            signal.pidfd_send_signal(unrelated_fd, signal.SIGKILL)
            unrelated.wait(timeout=5)
            os.close(unrelated_fd)

    def test_externally_reaped_leader_refuses_group_signaling(self):
        process = SMOKE.spawn_owned(["/bin/true"])
        process.process.wait(timeout=5)
        with mock.patch.object(os, "killpg", side_effect=AssertionError("A stale numeric group must never be signaled.")):
            with self.assertRaisesRegex(RuntimeError, "reaped elsewhere"):
                SMOKE.stop_owned(process)


if __name__ == "__main__":
    unittest.main(verbosity=2)
