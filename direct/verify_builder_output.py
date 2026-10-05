#!/usr/bin/env python3
"""Verify bootstrap builder streaming using isolated, hardware-free children."""
from __future__ import annotations

import os
from pathlib import Path
from queue import Queue
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import bootstrap_builders as builders


class BuilderOutputChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/builder-output"
        audit.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "builder.log"

    def run_child(self, code, log, **options):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1",
                   TMP=str(self.root), TEMP=str(self.root), TMPDIR=str(self.root))
        return builders.run_builder([sys.executable, "-B", "-c", code], env=env,
                                    label="fixture:board (native)", output_path=self.output,
                                    log=log, timeout=options.pop("timeout", 10),
                                    heartbeat=options.pop("heartbeat", 2), **options)

    def test_output_is_visible_before_child_exit_and_success_reports_elapsed(self):
        records = []
        started = time.monotonic()
        elapsed = self.run_child("import time; print('READY', flush=True); time.sleep(0.8); print('DONE')",
                                 lambda text: records.append((time.monotonic(), text)))
        ready = next(timestamp for timestamp, text in records if text == "READY")
        self.assertLess(ready - started, elapsed - 0.5)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "READY\nDONE\n")
        self.assertTrue(records[-1][1].startswith("Builder ready: fixture:board (native) ("))

    def test_failure_keeps_early_diagnostics_beyond_bounded_tail(self):
        records = []
        with self.assertRaises(RuntimeError) as caught:
            self.run_child("import sys; print('EARLY FAILURE DETAIL'); print('x' * 12000); sys.exit(7)",
                           records.append)
        failure = str(caught.exception)
        self.assertIn("exit code 7", failure)
        self.assertIn(str(self.output), failure)
        self.assertNotIn("EARLY FAILURE DETAIL", failure)
        self.assertIn("EARLY FAILURE DETAIL", self.output.read_text(encoding="utf-8"))
        self.assertLessEqual(max(map(len, records)), builders.DISPLAY_CHARS)
        self.assertTrue(any("display shortened" in text for text in records))
        self.assertFalse(any(text.startswith("Builder ready") for text in records))

    def test_split_unicode_and_percentages_keep_live_record_boundaries(self):
        records = []
        code = ("import sys,time; b=sys.stdout.buffer; "
                "b.write(b'Unpacking 25%'); b.flush(); time.sleep(0.15); "
                "b.write(b' 50%\\nUnicode: \\xf0'); b.flush(); time.sleep(0.15); "
                "b.write(b'\\x9f\\x9a\\x80\\n'); b.flush()")
        self.run_child(code, records.append)
        self.assertEqual(records[:3], ["Unpacking 25%", "50%", "Unicode: 🚀"])
        self.assertEqual(self.output.read_text(encoding="utf-8"), "Unpacking 25% 50%\nUnicode: 🚀\n")

    def test_silent_timeout_reports_heartbeat_and_stops_descendant(self):
        import psutil
        records = []
        code = ("import subprocess,sys,time; "
                "p=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(120)']); "
                "print('DESCENDANT=' + str(p.pid),flush=True); time.sleep(120)")
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "timed out after 0.6 seconds"):
            self.run_child(code, records.append, timeout=0.6, heartbeat=0.15)
        self.assertLess(time.monotonic() - started, 8)
        descendant = int(next(text.split("=", 1)[1] for text in records if text.startswith("DESCENDANT=")))
        if psutil.pid_exists(descendant):
            process = psutil.Process(descendant)
            self.assertIn(process.status(), (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD))
        self.assertTrue(any(text.startswith("Still preparing builder") for text in records))
        self.assertFalse(any(text.startswith("Builder ready") for text in records))

    def test_burst_reader_queue_and_callback_records_stay_bounded(self):
        metrics = {"peak": 0, "chunk": 0, "display": 0, "count": 0}
        class ObservedQueue(Queue):
            def __init__(self, maxsize):
                super().__init__(maxsize)
                metrics["capacity"] = maxsize
            def put(self, item, **kwargs):
                super().put(item, **kwargs)
                metrics["peak"] = max(metrics["peak"], self.qsize())
                metrics["chunk"] = max(metrics["chunk"], len(item[1]))
        def log(text):
            metrics["display"] = max(metrics["display"], len(text))
            metrics["count"] += 1
            time.sleep(0.003)
        with patch.object(builders, "Queue", ObservedQueue):
            self.run_child("import sys; sys.stdout.buffer.write((b'x' * 4095 + b'\\n') * 96); sys.stdout.flush()", log)
        self.assertEqual(metrics["capacity"], 32)
        self.assertLessEqual(metrics["peak"], 32)
        self.assertGreater(metrics["peak"], 1)
        self.assertLessEqual(metrics["chunk"], 4096)
        self.assertLessEqual(metrics["display"], 3000)
        self.assertEqual(self.output.stat().st_size, 4096 * 96)


if __name__ == "__main__":
    unittest.main(verbosity=2)
