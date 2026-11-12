#!/usr/bin/env python3
"""Ubuntu Bootstrap window checks with the real Qt setup view, offscreen only.

Run with the private runtime: ``.venv-linux/bin/python -B direct/verify_ubuntu_bootstrap_window.py``.
``--render-dir temp/...`` saves what the window shows. No setup, update or
installer runs; the events come from fixtures, exactly as the launcher sends them.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.pop("DISPLAY", None)
os.environ.pop("WAYLAND_DISPLAY", None)
os.environ["QT_QPA_PLATFORM"] = "offscreen"

RENDER_DIR: Path | None = None


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(["verify"])


def _settle(app, seconds=0.25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def _events(*records) -> io.StringIO:
    return io.StringIO("".join(json.dumps({"event": name, "args": list(args)}) + "\n" for name, *args in records))


class WindowChecks(unittest.TestCase):
    def setUp(self):
        from direct.ubuntu import bootstrap_window
        self.module = bootstrap_window
        self.app = _app()
        self.window, self.state = bootstrap_window.build_window("Updating from GitHub")
        self.window.show()
        self.window.bind_dispatcher()
        self.addCleanup(self._dispose)

    def _dispose(self):
        self.window._allow_close = True
        self.window.close()
        self.window.deleteLater()
        _settle(self.app, 0.05)

    def feed(self, *records, seconds=0.3, end=True):
        """Send records as the launcher does; ``end`` also closes its pipe afterwards."""
        if end:
            self.module.read_events(_events(*records), self.state, self.window)
        else:
            for line in _events(*records):
                self.module.forward_event(line, self.state, self.window)
        _settle(self.app, seconds)

    def render(self, name):
        if RENDER_DIR is not None:
            RENDER_DIR.mkdir(parents=True, exist_ok=True)
            self.window.grab().save(str(RENDER_DIR / f"{name}.png"))

    def test_launcher_events_appear_in_the_shared_setup_view(self):
        self.feed(("log", "Updating MCU Flasher from GitHub", "section"),
                  ("log", "Reusing 905 unchanged files already installed.", "normal"),
                  ("status", "Downloading 15 changed files · 40% · 0:03 elapsed"),
                  ("progress", 40.0), ("cancellable", True))
        self.assertIn("Updating MCU Flasher from GitHub", self.window.summary_edit.toPlainText())
        self.assertIn("Reusing 905 unchanged files", self.window.log_edit.toPlainText())
        self.assertEqual(self.window.prog_bar.value(), 40)
        self.assertIn("Downloading 15 changed files", self.window.status_lbl.toolTip())
        self.assertEqual(self.window.sub_lbl.text(), "Updating from GitHub")
        # Updates are always on: the window offers no Skip Updates choice.
        self.assertTrue(self.window.skip_cb.isHidden())
        self.assertTrue(self.window.cancellable)
        self.render("update-in-progress")

    def test_setup_cannot_be_closed_until_it_finishes(self):
        self.feed(("log", "Python packages", "section"), end=False)
        self.window.close()
        _settle(self.app)
        self.assertTrue(self.window.isVisible())
        self.assertIn("closes by itself", self.window.log_edit.toPlainText())

    def test_a_download_can_be_closed_which_skips_the_update(self):
        self.feed(("cancellable", True), end=False)
        self.assertTrue(self.window.isVisible())
        self.window.close()
        _settle(self.app)
        self.assertFalse(self.window.isVisible())

    def test_failure_stays_on_screen_and_success_closes_by_itself(self):
        self.feed(("log", "Ubuntu Bootstrap failed during Python dependencies: no network", "fail"),
                  ("finish", False, "Bootstrap failed. Correct the error above, then launch again."), seconds=1.2)
        self.assertTrue(self.window.isVisible())
        self.render("setup-failed")
        self.window.close()
        _settle(self.app)
        self.assertFalse(self.window.isVisible())

    def test_success_closes_the_window(self):
        self.feed(("finish", True, "MCU Flasher is open."),
                  seconds=(self.module.SUCCESS_CLOSE_DELAY_MS + 500) / 1000)
        self.assertFalse(self.window.isVisible())


class ProcessChecks(unittest.TestCase):
    def test_window_process_exits_after_success_and_at_end_of_input(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        for records in ((("log", "Preparing your workspace", "section"), ("finish", True, "Ready")),
                        (("log", "Launcher ended without finishing", "normal"),)):
            started = time.monotonic()
            result = subprocess.run([sys.executable, "-B", str(ROOT / "direct/ubuntu/bootstrap_window.py")],
                                    input=_events(*records).getvalue(), text=True, env=environment,
                                    capture_output=True, timeout=60, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(time.monotonic() - started, 30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--render-dir", type=Path)
    known, remaining = parser.parse_known_args()
    if known.render_dir is not None:
        RENDER_DIR = known.render_dir.resolve()
        if not RENDER_DIR.is_relative_to((ROOT / "temp").resolve()):
            raise SystemExit("--render-dir must be inside this checkout's temp/ folder")
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
