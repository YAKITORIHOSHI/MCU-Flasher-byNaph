import importlib.metadata
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = PROJECT_ROOT / "src" / "modules" / "bootstrap.py"


def load_bootstrap_module():
    spec = importlib.util.spec_from_file_location("bootstrap_process_lifecycle", BOOTSTRAP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BootstrapProcessLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = load_bootstrap_module()

    def test_package_version_lookup_does_not_spawn_pip(self):
        b = self.bootstrap
        with (
            mock.patch.object(importlib.metadata, "version", return_value="1.2.3"),
            mock.patch.object(b.subprocess, "run") as run,
        ):
            self.assertEqual(b._pip_installed_version("unused", "example-package"), "1.2.3")

        run.assert_not_called()

    def test_startup_does_not_start_background_update_thread(self):
        source = Path(BOOTSTRAP_PATH).read_text(encoding="utf-8")
        self.assertNotIn("_updates_thread = threading.Thread", source)
        self.assertIn("run_update_checks(auto_update=False)", source)

    def test_update_checks_are_opt_in_by_default(self):
        self.assertTrue(self.bootstrap.DEFAULT_SKIP_UPDATES)

    def test_unchecked_checkbox_overrides_stale_config_value(self):
        b = self.bootstrap
        gui = type(
            "GUI",
            (),
            {"_skip_updates_var": type("Value", (), {"get": staticmethod(lambda: False)})()},
        )()
        with (
            mock.patch.object(b, "_gui", gui),
            mock.patch.object(b, "load_bootstrap_config", return_value={"skip_updates": True}),
            mock.patch.dict(os.environ, {"MCU_FLASH_GUI_SKIP_UPDATES": ""}),
        ):
            self.assertIsNone(b._update_check_skip_reason())

    def test_enabled_update_check_reports_offline_state_to_gui(self):
        b = self.bootstrap
        messages = []

        class GUI:
            _skip_updates_var = type("Value", (), {"get": staticmethod(lambda: False)})()

            def log_subsection(self, text):
                messages.append(("section", text))

            def log_status(self, text):
                messages.append(("status", text))

            def log_dim(self, text):
                messages.append(("dim", text))

        with (
            mock.patch.object(b, "_gui", GUI()),
            mock.patch.object(b, "_is_network_reachable", return_value=False),
            mock.patch.dict(os.environ, {"MCU_FLASH_GUI_SKIP_UPDATES": ""}),
        ):
            self.assertEqual(b.run_update_checks(), "offline")

        self.assertIn(("section", "Checking for updates"), messages)
        self.assertTrue(any("Update checks enabled" in text for _, text in messages))
        self.assertTrue(any("Offline detected" in text for _, text in messages))
        # The summary block header is always shown, even on offline path
        self.assertTrue(any("Update check results:" == text for _, text in messages))
        # Every managed package is listed, not just a single "offline" line
        offline_msgs = [text for _, text in messages if "not checked (offline)" in text]
        self.assertGreaterEqual(len(offline_msgs), 10)

    def test_skip_updates_path_still_renders_per_package_summary(self):
        b = self.bootstrap
        messages = []

        class GUI:
            _skip_updates_var = type("Value", (), {"get": staticmethod(lambda: True)})()

            def log_warn(self, text):
                messages.append(("warn", text))

            def log_dim(self, text):
                messages.append(("dim", text))

        with (
            mock.patch.object(b, "_gui", GUI()),
            mock.patch.dict(os.environ, {"MCU_FLASH_GUI_SKIP_UPDATES": ""}),
        ):
            self.assertEqual(b.run_update_checks(), "skipped")

        self.assertTrue(any("Online update checks skipped" in text for _, text in messages))
        # Summary block header is present
        self.assertTrue(any("Update check results:" == text for _, text in messages))
        # Every managed package is listed with the skip reason
        skipped_msgs = [text for _, text in messages if "check skipped" in text]
        self.assertGreaterEqual(len(skipped_msgs), 10)

    def test_run_log_is_timestamped_and_persistent(self):
        b = self.bootstrap
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "bootstrap.log"
            with mock.patch.object(b, "_BOOTSTRAP_LOG_FILE", log_file):
                b._record_bootstrap_log("STATUS", "bootstrap test event")

            contents = log_file.read_text(encoding="utf-8")

        self.assertIn("[STATUS] bootstrap test event", contents)


if __name__ == "__main__":
    unittest.main()
