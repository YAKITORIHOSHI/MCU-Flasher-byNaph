import inspect
import os
import tempfile
import unittest
from pathlib import Path

import mcu_flash_gui as gui_module


class BuildSourceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name)
        self.app = gui_module.MCUUploadGUI.__new__(gui_module.MCUUploadGUI)
        self.app.sketch_dir_path = self.project
        self.app._pio_env_name = lambda: "test_env"

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_sync_replaces_legacy_hard_link_with_frozen_copy(self):
        original = self.project / "sketch.ino"
        original.write_text("approved\n", encoding="utf-8")
        staged_dir = self.project / "src"
        staged_dir.mkdir()
        staged = staged_dir / original.name
        try:
            os.link(original, staged)
        except OSError as exc:
            self.skipTest(f"Hard links are unavailable on this filesystem: {exc}")
        self.assertTrue(os.path.samefile(original, staged))

        self.app._sync_src_dir()

        self.assertFalse(os.path.samefile(original, staged))
        original.write_text("late AI edit\n", encoding="utf-8")
        self.assertEqual(staged.read_text(encoding="utf-8"), "approved\n")
        self.assertFalse(any(".freeze-" in path.name for path in staged_dir.iterdir()))

    def test_boundary_scans_immediately_before_and_after_freeze(self):
        events = []
        scan_results = iter((False, True))

        def scan(action_name):
            events.append(("scan", action_name))
            return next(scan_results)

        self.app._block_action_for_pending_ai_review = scan
        self.app._sync_src_dir = lambda: events.append(("freeze", None))

        self.assertFalse(self.app._freeze_build_sources_at_boundary("Compile"))
        self.assertEqual(
            events,
            [("scan", "Compile"), ("freeze", None), ("scan", "Compile")],
        )

    def test_compile_and_cached_upload_use_the_frozen_boundary(self):
        compile_source = inspect.getsource(gui_module.MCUUploadGUI._run_compile)
        upload_source = inspect.getsource(gui_module.MCUUploadGUI._run_upload)

        self.assertIn("_freeze_build_sources_at_boundary(boundary_action)", compile_source)
        self.assertIn('_freeze_build_sources_at_boundary("Upload")', upload_source)
        self.assertIn('_block_action_for_pending_ai_review("Upload")', upload_source)


if __name__ == "__main__":
    unittest.main()
