"""Interface size: the per-monitor policy, its lifecycle and a real Qt process applying it."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from main.core import config
from main.qt import interface_size as size
from src.modules.ui_metrics import interface_scale


class PolicyChecks(unittest.TestCase):
    def test_each_kind_of_screen_gets_the_scale_it_needs(self):
        for name, width, height, inches, ratio, expected in (
            # Small screen, high resolution, display left at 100%.
            ("13-inch 1080p", 1920, 1032, 165, 1, 1.25),
            ("15-inch 1080p", 1920, 1032, 141, 1, 1.125),
            ("14-inch 1440p", 2560, 1392, 210, 1, 1.25),
            # Large screen, low display scale.
            ("27-inch 4K", 3840, 2112, 163, 1, 1.25),
            ("32-inch 4K", 3840, 2112, 138, 1, 1.125),
            ("43-inch 4K", 3840, 2112, 102, 1, 1.0),
            # Small screen, high display scale: the chrome crowds the code.
            ("768p at 150%", 911, 480, 75, 1.5, .875),
            ("768p at 200%", 683, 360, 56, 2, .875),
            ("1080p at 200%", 960, 516, 84, 2, .875),
            # Left alone: already sensible.
            ("768p at 100%", 1366, 728, 112, 1, 1.0),
            ("768p at 125%", 1093, 576, 90, 1.25, 1.0),
            ("24-inch 1080p", 1920, 1032, 92, 1, 1.0),
            ("27-inch 1440p", 2560, 1392, 109, 1, 1.0),
            ("27-inch 4K at 150%", 2560, 1392, 109, 1.5, 1.0),
            ("1080p at 150%", 1280, 688, 113, 1.5, 1.0),
            ("Retina MacBook", 1440, 876, 127, 2, 1.0),
            # A small low-resolution desktop has no device pixels to spare.
            ("800x600 at 100%", 800, 552, 100, 1, 1.0),
            # Monitors that report nonsense are treated as ordinary.
            ("no physical size", 1920, 1032, 0, 1, 1.0),
            ("absurd physical size", 3840, 2112, 4000, 1, 1.0),
            ("not a number", 3840, 2112, float("nan"), 1, 1.0),
        ):
            with self.subTest(name):
                self.assertEqual(interface_scale(width, height, inches, ratio), expected)

    def test_a_scale_of_ours_never_takes_the_interface_below_one_device_pixel(self):
        # Sub-100% painting underdraws in Chromium: see verify_embedded_viewports.py.
        for ratio in (1.0, 1.25, 1.5, 2.0):
            scale = interface_scale(700, 400, 90, ratio)
            self.assertGreaterEqual(scale * ratio, 1.0)


class LifecycleChecks(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.environ.pop, "QT_SCALE_FACTOR", None)
        os.environ.pop("QT_SCALE_FACTOR", None)
        self.stored = {"shared": {}, "instances": {}}
        self.saved = []
        patch.object(config, "_load_raw_config", side_effect=lambda: self.stored).start()
        patch.object(config, "_save_raw_config", side_effect=lambda data: self.saved.append(json.dumps(data)) or True).start()
        self.addCleanup(patch.stopall)

    class Screen:
        def __init__(self, width, height, inches, ratio):
            self.values = width, height, inches, ratio

        def availableGeometry(self):
            from PySide6.QtCore import QRect
            return QRect(0, 0, self.values[0], self.values[1])

        def physicalDotsPerInch(self):
            return self.values[2]

        def devicePixelRatio(self):
            return self.values[3]

    def app(self, *values):
        return type("App", (), {"primaryScreen": lambda _self: self.Screen(*values)})()

    def test_an_existing_variable_is_left_to_whoever_set_it(self):
        os.environ["QT_SCALE_FACTOR"] = "2"
        self.stored["shared"]["interface_size"] = 1.25
        applied = size.prepare_interface_size()
        self.assertEqual(os.environ["QT_SCALE_FACTOR"], "2")
        size.settle_interface_size(self.app(1920, 1032, 165, 1), applied)
        self.assertEqual(os.environ["QT_SCALE_FACTOR"], "2")
        self.assertEqual(self.saved, [])

    def test_a_chosen_size_applies_and_child_processes_do_not_inherit_it(self):
        self.stored["shared"]["interface_size"] = 1.25
        applied = size.prepare_interface_size()
        self.assertEqual(os.environ["QT_SCALE_FACTOR"], "1.25")
        size.settle_interface_size(self.app(1536, 826, 132, 1.25), applied)
        self.assertNotIn("QT_SCALE_FACTOR", os.environ)
        self.assertEqual(self.saved, [], "a chosen size does not measure the monitor")

    def test_automatic_measures_now_and_applies_next_time(self):
        applied = size.prepare_interface_size()
        self.assertEqual(applied, 1.0)
        self.assertNotIn("QT_SCALE_FACTOR", os.environ)
        size.settle_interface_size(self.app(1920, 1032, 165, 1), applied)
        self.assertEqual(self.stored["shared"]["interface_scale_auto"], 1.25)
        self.assertEqual(len(self.saved), 1)
        applied = size.prepare_interface_size()
        self.assertEqual(os.environ["QT_SCALE_FACTOR"], "1.25")

    def test_automatic_is_stable_once_applied_and_writes_only_on_a_change(self):
        self.stored["shared"]["interface_scale_auto"] = 1.25
        applied = size.prepare_interface_size()
        # Qt now reports this monitor in scaled logical units.
        size.settle_interface_size(self.app(1536, 826, 132, 1.25), applied)
        self.assertEqual(self.saved, [], "the same monitor must not look different once scaled")

    def test_moving_to_another_monitor_updates_automatic(self):
        self.stored["shared"]["interface_scale_auto"] = 1.25
        applied = size.prepare_interface_size()
        size.settle_interface_size(self.app(1536 // 1, 826, 74, 1.25), applied)  # An ordinary 24-inch panel.
        self.assertEqual(self.stored["shared"]["interface_scale_auto"], 1.0)

    def test_damaged_or_missing_settings_mean_normal_size(self):
        for shared in ({"interface_size": "enormous"}, {"interface_size": 9}, {"interface_scale_auto": "x"},
                       {"interface_scale_auto": 7}, {"interface_size": None}):
            with self.subTest(shared):
                self.stored["shared"] = shared
                self.assertEqual(size.prepare_interface_size(), 1.0)
                self.assertNotIn("QT_SCALE_FACTOR", os.environ)
        self.stored = {"shared": "not a mapping"}
        self.assertEqual(size.prepare_interface_size(), 1.0)
        with patch.object(config, "_load_raw_config", side_effect=OSError("unreadable")):
            self.assertEqual(size.prepare_interface_size(), 1.0)

    def test_a_failed_save_cannot_stop_startup(self):
        with patch.object(config, "_save_raw_config", side_effect=OSError("read-only")):
            size.settle_interface_size(self.app(1920, 1032, 165, 1), 1.0)

    def test_every_offered_size_is_one_the_setting_accepts(self):
        values = [value for _label, value in size.INTERFACE_SIZES]
        self.assertEqual(values[0], "auto")
        for value in values:
            self.stored["shared"]["interface_size"] = value
            self.assertEqual(size.interface_choice(), value)


class SettingsChecks(unittest.TestCase):
    """Settings says what Automatic picks here and offers a restart when the size changes."""

    def setUp(self):
        self.addCleanup(os.environ.pop, "QT_SCALE_FACTOR", None)
        os.environ.pop("QT_SCALE_FACTOR", None)
        self.stored = {"shared": {}, "instances": {}}
        patch.object(config, "_load_raw_config", side_effect=lambda *a, **k: self.stored).start()
        patch.object(config, "_save_raw_config", return_value=True).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(size._STARTED.update, dict(size._STARTED))

    def screen(self, width, height, inches, ratio):
        return LifecycleChecks.Screen(width, height, inches, ratio)

    def test_started_scale_and_override_are_recorded(self):
        self.stored["shared"]["interface_size"] = 1.25
        size.prepare_interface_size()
        self.assertEqual((size.started_scale(), size.environment_override(), size.restart_can_apply()),
                         (1.25, False, True))
        os.environ["QT_SCALE_FACTOR"] = "1.5"
        size.prepare_interface_size()
        self.assertEqual((size.started_scale(), size.environment_override(), size.restart_can_apply()),
                         (1.5, True, False))

    def test_automatic_is_measured_in_native_units_whatever_this_window_started_at(self):
        # A 13-inch 1080p laptop: Automatic is 125% whether Qt shows it at 100% or 125%.
        self.assertEqual(size.automatic_scale_for(self.screen(1920, 1032, 165, 1), 1.0), 1.25)
        self.assertEqual(size.automatic_scale_for(self.screen(1536, 826, 132, 1.25), 1.25), 1.25)
        # The user's 24-inch 1080p monitor: Automatic leaves it at 100%.
        self.assertEqual(size.automatic_scale_for(self.screen(1920, 1048, 92, 1), 1.0), 1.0)

class RealProcessChecks(unittest.TestCase):
    CHILD = """
import os, sys
sys.path.insert(0, {root!r})
from unittest.mock import patch
from main.core import config
from main.qt import interface_size as size
from PySide6.QtWidgets import QApplication
with patch.object(config, "_load_raw_config", return_value={{"shared": {{"interface_size": {choice!r}}}}}):
    applied = size.prepare_interface_size()
    app = QApplication(["interface-size"])
    size.settle_interface_size(app, applied)
print(app.primaryScreen().devicePixelRatio(), os.environ.get("QT_SCALE_FACTOR"))
"""

    def run_child(self, choice):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("QT_SCALE_FACTOR", "DISPLAY", "WAYLAND_DISPLAY")}
        environment["QT_QPA_PLATFORM"] = "offscreen"
        result = subprocess.run([sys.executable, "-B", "-c", self.CHILD.format(root=str(ROOT), choice=choice)],
                                env=environment, capture_output=True, text=True, errors="replace", timeout=60,
                                stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        return result.stdout.strip().splitlines()[-1].split()

    def test_qt_really_scales_and_the_variable_is_gone_afterwards(self):
        self.assertEqual(self.run_child(1.25), ["1.25", "None"])
        self.assertEqual(self.run_child(1.0), ["1.0", "None"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
