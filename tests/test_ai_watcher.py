import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dedicated_AI


class _WatcherRoot:
    def after(self, delay, callback):
        if delay == 0:
            callback()
        return "job"


def _write_exact(path, content):
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(content)


class AIWatcherTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name)
        self.events = []
        self.controller = dedicated_AI.AIController(
            get_sketch_dir_func=lambda: self.project,
            root=_WatcherRoot(),
            on_ai_edit_func=lambda *args: self.events.append(args),
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_atomic_delete_recreate_preserves_original_baseline(self):
        path = self.project / "sketch.ino"
        _write_exact(path, "original\r\n")
        key = os.path.normcase(os.path.abspath(path))
        self.controller._file_mtimes = {key: 1.0}
        self.controller._file_contents = {key: "original\r\n"}
        self.controller._last_content_verification = 10.0

        with mock.patch.object(dedicated_AI, "is_opencode_running", return_value=True):
            with mock.patch.object(self.controller, "_scan_project_files", return_value={}):
                with mock.patch.object(dedicated_AI.time, "time", return_value=10.0):
                    path.unlink()
                    self.controller._monitor_step()

            _write_exact(path, "AI replacement\r\n")
            with mock.patch.object(
                self.controller, "_scan_project_files", return_value={key: 2.0}
            ):
                with mock.patch.object(dedicated_AI.time, "time", return_value=10.2):
                    self.controller._monitor_step()
                with mock.patch.object(dedicated_AI.time, "time", return_value=10.7):
                    self.controller._monitor_step()

        self.assertEqual(len(self.events), 1)
        path_arg, before, after, before_exists, after_exists = self.events[0]
        self.assertEqual(os.path.normcase(os.path.abspath(path_arg)), key)
        self.assertEqual(before, "original\r\n")
        self.assertEqual(after, "AI replacement\r\n")
        self.assertTrue(before_exists)
        self.assertTrue(after_exists)

    def test_periodic_content_check_detects_equal_timestamp_edit(self):
        path = self.project / "sketch.ino"
        _write_exact(path, "AI changed this\n")
        key = os.path.normcase(os.path.abspath(path))
        self.controller._file_mtimes = {key: 5.0}
        self.controller._file_contents = {key: "original\n"}
        self.controller._last_content_verification = 0.0

        with mock.patch.object(dedicated_AI, "is_opencode_running", return_value=True):
            with mock.patch.object(
                self.controller, "_scan_project_files", return_value={key: 5.0}
            ):
                with mock.patch.object(dedicated_AI.time, "time", return_value=100.0):
                    self.controller._monitor_step()
                with mock.patch.object(dedicated_AI.time, "time", return_value=100.6):
                    self.controller._monitor_step()

        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][1:3], ("original\n", "AI changed this\n"))

    def test_compile_gate_collection_bypasses_debounce_window(self):
        path = self.project / "sketch.ino"
        _write_exact(path, "AI changed immediately\n")
        key = self.controller._path_key(path)
        self.controller._file_mtimes = {key: 7.0}
        self.controller._file_contents = {key: "original\n"}
        self.controller._monitoring_initialized = True

        with mock.patch.object(dedicated_AI, "is_opencode_running", return_value=True):
            with mock.patch.object(
                self.controller, "_scan_project_files", return_value={key: 7.0}
            ):
                detected = self.controller.collect_unreported_edits()

        self.assertEqual(len(detected), 1)
        self.assertEqual(detected[0][1:3], ("original\n", "AI changed immediately\n"))

    def test_project_switch_rebaselines_existing_files(self):
        # Two different projects with the SAME file name but different content.
        project_a = self.project / "a"
        project_a.mkdir()
        project_b = self.project / "b"
        project_b.mkdir()
        _write_exact(project_a / "sketch.ino", "AAA project A\n" * 5)
        _write_exact(project_b / "sketch.ino", "BBB project B\n" * 8)

        # Controller now bound to project B (fresh monitor start baseline).
        controller = dedicated_AI.AIController(
            get_sketch_dir_func=lambda: project_b,
            root=_WatcherRoot(),
            on_ai_edit_func=lambda *args: self.events.append(args),
        )
        key = controller._path_key(project_b / "sketch.ino")
        controller._file_mtimes = {key: 3.0}
        controller._file_contents = {key: "BBB project B\n" * 8}
        controller._last_content_verification = 0.0

        # Project switch: baselines are dropped, then monitor ticks resume.
        # Existing files of the new project must NOT be flagged as AI edits
        # (that would queue a review showing the whole file as "added").
        controller.reset_monitoring_state()
        with mock.patch.object(dedicated_AI, "is_opencode_running", return_value=True):
            with mock.patch.object(dedicated_AI.time, "time", return_value=50.0):
                controller._monitor_step()
            with mock.patch.object(dedicated_AI.time, "time", return_value=50.6):
                controller._monitor_step()

        self.assertEqual(self.events, [])
        self.assertEqual(controller._file_contents.get(key), "BBB project B\n" * 8)

    def test_project_switch_still_detects_edit_made_after_switch(self):
        # Same as above, but a genuine AI edit AFTER the switch must still
        # be detected and queued with the real "before" baseline.
        project_a = self.project / "a"
        project_a.mkdir()
        project_b = self.project / "b"
        project_b.mkdir()
        _write_exact(project_a / "sketch.ino", "AAA project A\n" * 5)
        sketch_b = project_b / "sketch.ino"
        _write_exact(sketch_b, "BBB project B\n" * 8)

        controller = dedicated_AI.AIController(
            get_sketch_dir_func=lambda: project_b,
            root=_WatcherRoot(),
            on_ai_edit_func=lambda *args: self.events.append(args),
        )
        controller._last_content_verification = 0.0
        controller.reset_monitoring_state()

        key = controller._path_key(sketch_b)
        _write_exact(sketch_b, "BBB project B\n" * 8 + "AI added line\n")
        with mock.patch.object(dedicated_AI, "is_opencode_running", return_value=True):
            with mock.patch.object(dedicated_AI.time, "time", return_value=50.0):
                controller._monitor_step()
            with mock.patch.object(dedicated_AI.time, "time", return_value=50.6):
                controller._monitor_step()

        self.assertEqual(len(self.events), 1)
        _, before, after, before_exists, after_exists = self.events[0]
        self.assertEqual(before, "BBB project B\n" * 8)
        self.assertEqual(after, "BBB project B\n" * 8 + "AI added line\n")
        self.assertTrue(before_exists)
        self.assertTrue(after_exists)


if __name__ == "__main__":
    unittest.main()
