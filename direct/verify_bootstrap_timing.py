#!/usr/bin/env python3
"""Check phase timing outcomes without setup, hardware or package stores."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules.bootstrap_timing import timed_stage


class TimingChecks(unittest.TestCase):
    def test_quiet_phase_can_finish_after_a_long_interval(self):
        messages = []
        with patch("src.modules.bootstrap_timing.time.monotonic", side_effect=[10, 1810]):
            with timed_stage(messages.append, "quiet extraction"):
                pass
        self.assertEqual(messages, ["Bootstrap timing: quiet extraction started",
                                    "Bootstrap timing: quiet extraction completed in 1800.0 s"])

    def test_failure_keeps_original_exception_and_reports_elapsed(self):
        messages = []
        failure = OSError("fixture failure")
        with patch("src.modules.bootstrap_timing.time.monotonic", side_effect=[100, 102.75]):
            with self.assertRaises(OSError) as caught:
                with timed_stage(messages.append, "verification"):
                    raise failure
        self.assertIs(caught.exception, failure)
        self.assertEqual(messages[-1], "Bootstrap timing: verification failed after 2.8 s")
        self.assertFalse(any("completed" in message for message in messages))

    def test_cancel_is_a_failed_phase_without_swallowing_interrupt(self):
        messages = []
        with patch("src.modules.bootstrap_timing.time.monotonic", side_effect=[20, 21]):
            with self.assertRaises(KeyboardInterrupt):
                with timed_stage(messages.append, "cancelled setup"):
                    raise KeyboardInterrupt
        self.assertEqual(messages[-1], "Bootstrap timing: cancelled setup failed after 1.0 s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
