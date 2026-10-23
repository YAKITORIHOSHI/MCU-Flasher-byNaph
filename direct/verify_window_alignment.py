#!/usr/bin/env python3
"""Check native frame-aware half-monitor sizing without a live app backend.

Run each display scale in a fresh process. Native Wayland controls window
positions, so its checks assert accepted frame sizes and retained editor state;
the other native platforms additionally check left/right placement requests.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QLineEdit, QMainWindow

from main.qt.main_window import MCUMainWindow
from main.qt.responsive import active_screen, ScreenWatcher
from src.modules.ui_metrics import WorkArea


APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
RENDER_DIR = None
REQUIRE_HORIZONTAL_FRAME = False
RECEIPT = {"platform": APP.platformName(), "scale": os.environ.get("QT_SCALE_FACTOR", "native"),
           "measurements": [], "wayland_positions_owned_by_compositor": APP.platformName().startswith("wayland")}


def pump():
    for _ in range(6):
        APP.processEvents()
        time.sleep(.025)


def wait_for_resize(predicate):
    """Allow native configure delivery, retaining a finite assertion deadline."""
    deadline = time.monotonic() + 3
    pump()
    while not predicate() and time.monotonic() < deadline:
        pump()


def frame_margins(window):
    # Wayland's client-side decoration can be measurable on QWindow before
    # QWidget's cached frameGeometry changes during its first native resize.
    margins = window.windowHandle().frameMargins()
    return margins.left(), margins.top(), margins.right(), margins.bottom()


class AlignmentWindow(QMainWindow):
    """Real decorated container using the production minimum-size method."""

    _minimum_width_for_display = staticmethod(MCUMainWindow._minimum_width_for_display)
    _update_minimum_window_size = MCUMainWindow._update_minimum_window_size

    def _get_active_screen(self):
        return active_screen(self)


class AlignmentChecks(unittest.TestCase):
    @contextmanager
    def window(self, *, watch=False, maximized=False):
        window = AlignmentWindow()
        window.setWindowTitle("MCU Flasher — isolated window alignment check")
        field = QLineEdit(window)
        field.setText("Unsaved alignment fixture — \u03bb")
        window.setCentralWidget(field)
        area = active_screen(window).availableGeometry()
        if watch:
            window._update_minimum_window_size()
            window._pre_show_minimum = window.minimumWidth()
            window._screen_watcher = ScreenWatcher(window, window._update_minimum_window_size)
        # A near-screen-size first window can be automatically maximized by
        # Mutter at high scales. Keep this normal-window sizing fixture well
        # inside the actual desktop rather than testing the WM's policy.
        window.resize(max(1, round(area.width() * .65)), max(1, round(area.height() * .50)))
        if maximized:
            window.showMaximized()
        else:
            window.show()
        pump()
        # Reading native margins also handles a late Wayland decoration before
        # the production update. It never scales or invents native metrics.
        frame_margins(window)
        pump()
        try:
            yield window, field
        finally:
            window.close()
            window.deleteLater()
            pump()

    def test_show_refreshes_minimum_after_native_decoration_arrives(self):
        with self.window(watch=True) as (window, _field):
            area = window.screen().availableGeometry()
            left, _top, right, _bottom = frame_margins(window)
            self.assertEqual(window._pre_show_minimum, area.width() // 2,
                             "Before a native handle exists, fitting must preserve the half-monitor fallback")
            self.assertEqual(window.minimumWidth() + left + right, area.width() // 2,
                             "The coalesced show refresh must account for the native frame")
            self.assertFalse(window._screen_watcher._timer.isActive(),
                             "The screen refresh must become idle after native decoration settles")

    def test_restoring_after_maximized_screen_refresh_remeasures_native_frame(self):
        with self.window(watch=True) as (window, _field):
            window.showMaximized()
            pump()
            self.assertTrue(window.isMaximized(), "The native maximize request must be accepted")
            # A monitor/work-area update can legitimately arrive while the
            # window is maximized, when the native side decoration is absent.
            window._screen_watcher._schedule()
            pump()
            window.showNormal()
            pump()
            self.assertFalse(window.isMaximized())
            left, _top, right, _bottom = frame_margins(window)
            area = window.screen().availableGeometry()
            outer_width = area.width() // 2
            self.assertLessEqual(window.minimumWidth() + left + right, outer_width,
                "Restoring must refresh the client minimum after normal-window decoration returns")
            window.resize(outer_width - left - right, max(120, round(area.height() * .4)))
            pump()
            self.assertLessEqual(window.frameGeometry().width(), outer_width,
                                "Restored native frames must still accept the half-monitor width")
            self.assertFalse(window._screen_watcher._timer.isActive())

    def test_initially_maximized_window_restores_half_frame_minimum(self):
        # This matches opening a saved maximized workspace: the first Show
        # refresh observes the maximized native frame, not normal decoration.
        with self.window(watch=True, maximized=True) as (window, _field):
            self.assertTrue(window.isMaximized())
            self.assertFalse(window._screen_watcher._timer.isActive())
            window.showNormal()
            pump()
            self.assertFalse(window.isMaximized())
            left, _top, right, _bottom = frame_margins(window)
            area = window.screen().availableGeometry()
            outer_width = area.width() // 2
            self.assertEqual(window.minimumWidth() + left + right, outer_width,
                             "A workspace opened maximized must regain the complete-frame minimum on restore")
            window.resize(outer_width - left - right, max(120, round(area.height() * .4)))
            pump()
            self.assertLessEqual(window.frameGeometry().width(), outer_width)
            self.assertFalse(window._screen_watcher._timer.isActive())

    def test_half_frame_minimum_accepts_native_resize_and_restore(self):
        with self.window() as (window, field):
            actual = window.screen().availableGeometry()
            self.assertFalse(window.isMaximized(), "The owned normal-window fixture was unexpectedly maximized")
            left, top, right, bottom = frame_margins(window)
            frame_width, frame_height = left + right, top + bottom
            print(f"Native platform={APP.platformName()} screen={window.screen().name()} "
                  f"work_area={actual.getRect()} DPR={window.devicePixelRatioF()} "
                  f"frame_margins={(left, top, right, bottom)}", flush=True)
            if REQUIRE_HORIZONTAL_FRAME:
                self.assertGreater(frame_width, 0, "The controlled CSD fixture requires measurable horizontal decoration")
            widths = [actual.width()]
            odd = actual.width() if actual.width() % 2 else actual.width() - 1
            if odd not in widths:
                widths.append(odd)
            field.setFocus()
            field.setCursorPosition(8)
            before = (field.text(), field.cursorPosition(), field.font().toString())
            for width in widths:
                area = WorkArea(actual.x(), actual.y(), width, actual.height())
                with self.subTest(width=width), patch("main.qt.main_window.work_area", return_value=area):
                    window._update_minimum_window_size(window.screen())
                    half_frame_width = width // 2
                    requested_client_width = max(1, half_frame_width - frame_width)
                    normal_width = max(requested_client_width, min(1440, round(width * .72) - frame_width))
                    client_height = max(120, min(420, round(actual.height() * .55) - frame_height))
                    for iteration in range(3):
                        for side in ("left", "right"):
                            outer_width = half_frame_width if side == "left" else width - half_frame_width
                            outer_x = area.x if side == "left" else area.x + half_frame_width
                            window.setGeometry(outer_x + left, area.y + top + 8,
                                               max(1, outer_width - frame_width), client_height)
                            pump()
                            client, frame = window.geometry(), window.frameGeometry()
                            RECEIPT["measurements"].append(dict(area_width=width, iteration=iteration,
                                side=side, requested_frame_width=outer_width, client=list(client.getRect()),
                                frame=list(frame.getRect()), native_margins=[left, top, right, bottom],
                                minimum=window.minimumWidth(), dpr=window.devicePixelRatioF()))
                            if RENDER_DIR and iteration == 0:
                                self.assertTrue(window.grab().save(str(RENDER_DIR / f"native-{width}-{side}.png")))
                            self.assertLessEqual(frame.width(), outer_width,
                                f"Native frame {frame.width()} exceeds {side} tile budget {outer_width}")
                            self.assertEqual(window.minimumWidth(), requested_client_width,
                                "The normal-window minimum must retain the complete half-monitor frame budget")
                            # Wayland deliberately exposes no global positioning.
                            if not APP.platformName().startswith("wayland"):
                                self.assertLessEqual(abs(frame.x() - outer_x), 1,
                                    "An owned left/right placement request drifted")
                            self.assertEqual((field.text(), field.cursorPosition(), field.font().toString()), before)
                        window.resize(normal_width, client_height)
                        wait_for_resize(lambda: abs(window.width() - normal_width) <= 1)
                        self.assertLessEqual(abs(window.width() - normal_width), 1,
                            "Restoring a normal width must remain available after repeated half-frame sizes")
                    self.assertEqual(window.maximumWidth(), 16777215)


def main():
    global RENDER_DIR, REQUIRE_HORIZONTAL_FRAME
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--require-wayland", action="store_true")
    parser.add_argument("--require-horizontal-frame", action="store_true")
    args = parser.parse_args()
    if args.require_wayland and not APP.platformName().startswith("wayland"):
        parser.error(f"Expected native Wayland, got {APP.platformName()}")
    REQUIRE_HORIZONTAL_FRAME = args.require_horizontal_frame
    RENDER_DIR = args.render_dir
    if RENDER_DIR:
        RENDER_DIR.mkdir(parents=True, exist_ok=True)
    report = args.report or (RENDER_DIR / "alignment-results.json" if RENDER_DIR else None)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AlignmentChecks)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    RECEIPT.update(passed=result.wasSuccessful(), cases=result.testsRun,
                   failures=len(result.failures), errors=len(result.errors))
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(RECEIPT, indent=2) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
