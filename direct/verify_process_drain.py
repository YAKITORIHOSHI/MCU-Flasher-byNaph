"""Owned pipe drainage with harmless children and no attached hardware."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.core.process_drain import finish_with_output_drain


class DrainChecks(unittest.TestCase):
    def spawn(self, source):
        parent = ROOT / "temp/audit/process-drain"
        parent.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(scratch.cleanup)
        child = subprocess.Popen([sys.executable, "-B", "-c", source], cwd=scratch.name,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        def close():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
            child.stdout.close()
        self.addCleanup(close)
        return child

    def finish(self, child, guard=None, operation=None):
        result, errors = [], []
        def work():
            try:
                result.append(operation() if operation else finish_with_output_drain(child, guard))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(timeout=8)
        if worker.is_alive():
            child.kill()
            worker.join(timeout=2)
            self.fail("Owned output drain blocked on a full or quiet pipe")
        self.assertFalse(errors, errors)
        return result[0]

    def test_missing_reader_drains_more_than_pipe_capacity_without_killing_writer(self):
        child = self.spawn("import os; os.write(1, b'x' * (1024 * 1024))")
        guard = Mock()
        self.assertEqual(self.finish(child, guard), 0)
        guard.poll.assert_called()
        guard.finish.assert_not_called()

    def test_quiet_pipe_still_polls_connection_and_reaps_lost_writer(self):
        child = self.spawn("import time; time.sleep(30)")
        guard = Mock()
        def lost():
            child.kill()
            raise ConnectionError("Fixture transport lost")
        guard.poll.side_effect = lost
        guard.finish.side_effect = lambda: child.wait(timeout=3)
        self.assertNotEqual(self.finish(child, guard), 0)
        guard.finish.assert_called_once()

    def test_consumer_failure_keeps_single_reader_draining_owned_writer(self):
        from main.web_bridge import _iter_process_output
        child = self.spawn("import os, time; print('fixture ready', flush=True); time.sleep(.1); os.write(1, b'x' * (1024 * 1024))")
        def consume():
            output = _iter_process_output(child, lambda: False, child.kill, lambda _: None)
            self.assertIn("fixture ready", next(output))
            output.close()
            return child.wait(timeout=3)
        self.assertEqual(self.finish(child, operation=consume), 0)

    def test_reader_start_failure_drains_child_and_preserves_error(self):
        from main import web_bridge
        child = self.spawn("import os; os.write(1, b'x' * (1024 * 1024))")
        original = threading.Thread
        def make_reader(*args, **kwargs):
            reader = original(*args, **kwargs)
            if kwargs.get("name") == "MCU_BuildOutputReader":
                reader.start = Mock(side_effect=RuntimeError("Fixture reader startup failed"))
            return reader
        def consume():
            with patch.object(web_bridge.threading, "Thread", side_effect=make_reader):
                with self.assertRaisesRegex(RuntimeError, "Fixture reader startup failed"):
                    list(web_bridge._iter_process_output(child, lambda: False, child.kill, lambda _: None))
            return child.returncode
        self.assertEqual(self.finish(child, operation=consume), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
