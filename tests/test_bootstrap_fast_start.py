import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = PROJECT_ROOT / "src" / "modules" / "bootstrap.py"


def load_bootstrap_module():
    spec = importlib.util.spec_from_file_location("bootstrap_fast_start", BOOTSTRAP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BootstrapFastStartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = load_bootstrap_module()

    def _good_record(self, fingerprint="fingerprint"):
        b = self.bootstrap
        return {
            "schema": b._FAST_START_RECORD_SCHEMA,
            "verified_at": time.time(),
            "app_path": str(b.SCRIPT_DIR.resolve()),
            "source_fingerprint": fingerprint,
            "venv_python": str((b.SCRIPT_DIR / "env" / "Scripts" / "python.exe").resolve()),
        }

    def test_fast_start_requires_enabled_setting_and_good_record(self):
        b = self.bootstrap
        record = self._good_record()
        with (
            mock.patch.object(b.sys, "platform", "win32"),
            mock.patch.object(b, "_fast_start_enabled_in_settings", return_value=True),
            mock.patch.object(b, "_read_fast_start_record", return_value=record),
            mock.patch.object(b, "_fast_start_source_fingerprint", return_value="fingerprint"),
            mock.patch.object(b, "_fast_start_venv_is_healthy", return_value=True),
        ):
            ok, reason = b._fast_start_record_is_good()

        self.assertTrue(ok)
        self.assertIn("health check passed", reason)

    def test_fast_start_rejects_changed_app_files(self):
        b = self.bootstrap
        record = self._good_record("old-fingerprint")
        with (
            mock.patch.object(b.sys, "platform", "win32"),
            mock.patch.object(b, "_fast_start_enabled_in_settings", return_value=True),
            mock.patch.object(b, "_read_fast_start_record", return_value=record),
            mock.patch.object(b, "_fast_start_source_fingerprint", return_value="new-fingerprint"),
            mock.patch.object(b, "_fast_start_venv_is_healthy") as health_check,
        ):
            ok, reason = b._fast_start_record_is_good()

        self.assertFalse(ok)
        self.assertIn("app files changed", reason)
        health_check.assert_not_called()

    def test_fast_start_is_disabled_without_the_setting(self):
        b = self.bootstrap
        with (
            mock.patch.object(b.sys, "platform", "win32"),
            mock.patch.object(b, "_fast_start_enabled_in_settings", return_value=False),
            mock.patch.object(b, "_read_fast_start_record") as read_record,
        ):
            ok, reason = b._fast_start_record_is_good()

        self.assertFalse(ok)
        self.assertIn("disabled", reason)
        read_record.assert_not_called()

    def test_verified_record_is_written_per_user(self):
        b = self.bootstrap
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir) / "app"
            venv_python = app_dir / "env" / "Scripts" / "python.exe"
            venv_python.parent.mkdir(parents=True)
            venv_python.touch()
            record_path = Path(temp_dir) / "bootstrap_health.json"
            with (
                mock.patch.object(b.sys, "platform", "win32"),
                mock.patch.object(b, "SCRIPT_DIR", app_dir),
                mock.patch.object(b, "_fast_start_record_file", return_value=record_path),
                mock.patch.object(b, "_fast_start_source_fingerprint", return_value="fingerprint"),
            ):
                self.assertTrue(b._write_fast_start_record())

            record = json.loads(record_path.read_text(encoding="utf-8"))

        self.assertEqual(record["schema"], b._FAST_START_RECORD_SCHEMA)
        self.assertEqual(record["source_fingerprint"], "fingerprint")


if __name__ == "__main__":
    unittest.main()
