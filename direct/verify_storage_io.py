"""Isolated slow-storage I/O checks; never write live settings or caches."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from main.core import board_compat, config, file_utils


class StorageIOChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/storage-io"
        audit.mkdir(parents=True, exist_ok=True)
        fixture = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(fixture.cleanup)
        self.directory = Path(fixture.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(Path, "home", return_value=self.directory))
        self.stack.enter_context(patch.object(config, "LOCAL_GUI_CONFIG", self.directory / "portable.json"))
        self.stack.enter_context(patch.object(config, "_CONFIG_MEM_SIGNATURE", None))
        self.stack.enter_context(patch.object(config, "_CONFIG_MEM_CACHE", {}))
        self.stack.enter_context(patch.object(board_compat, "SUPPORTED_BOARDS", {
            "Fixture Uno": {"platform": "atmelavr", "board": "uno"},
            "Fixture ESP32": {"platform": "espressif32", "board": "esp32dev"},
        }))

    def test_equal_generated_text_creates_no_temp_or_replacement(self):
        target = self.directory / "metadata.json"
        file_utils.write_generated_text(target, "generated μ\r\n")
        stamp = target.stat().st_mtime_ns
        with patch.object(file_utils.tempfile, "mkstemp") as temporary, \
                patch.object(file_utils.os, "replace") as replace, \
                patch.object(file_utils.os, "chmod") as chmod:
            for _ in range(20):
                file_utils.write_generated_text(target, "generated μ\r\n")
        temporary.assert_not_called()
        replace.assert_not_called()
        chmod.assert_not_called()
        self.assertEqual(target.stat().st_mtime_ns, stamp)

    def test_equal_size_content_and_coarse_timestamp_do_not_hide_change(self):
        target = self.directory / "metadata.json"
        file_utils.write_generated_text(target, "old")
        old = target.stat()
        file_utils.unhide_hidden_attribute(target)
        target.write_text("bad", encoding="utf-8")
        os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))
        with patch.object(file_utils.os, "replace", wraps=os.replace) as replace:
            file_utils.write_generated_text(target, "new")
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")

    @unittest.skipUnless(sys.platform == "win32", "Windows attributes")
    def test_equal_generated_text_repairs_readonly_without_rewriting(self):
        import ctypes
        target = self.directory / "metadata.json"
        file_utils.write_generated_text(target, "keep")
        file_utils._set_windows_file_attributes(target, 0x01)
        self.addCleanup(file_utils.ensure_file_writable, target)
        with patch.object(file_utils.os, "replace") as replace:
            file_utils.write_generated_text(target, "keep")
        replace.assert_not_called()
        flags = ctypes.windll.kernel32.GetFileAttributesW(str(target))
        self.assertEqual(flags & 0x03, 0x02)

    def test_repeated_settings_transactions_do_not_write_either_copy(self):
        initial = {"shared": {"editor_font_size": 22}, "instances": {}}
        self.assertTrue(config._save_raw_config(initial))
        with patch.object(os, "replace", wraps=os.replace) as replace:
            for _ in range(20):
                self.assertTrue(config._save_raw_config(config._load_raw_config()))
        replace.assert_not_called()
        for target in (config.LOCAL_GUI_CONFIG, self.directory / ".mcu_gui_config.json"):
            self.assertEqual(json.loads(target.read_text()), initial)

    def test_missing_portable_copy_is_repaired_without_rewriting_user_copy(self):
        initial = {"shared": {"monitor_font_size": 18}}
        self.assertTrue(config._save_raw_config(initial))
        config.LOCAL_GUI_CONFIG.unlink()
        with patch.object(os, "replace", wraps=os.replace) as replace:
            self.assertTrue(config._save_raw_config(config._load_raw_config()))
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(replace.call_args.args[1], config.LOCAL_GUI_CONFIG)

    def test_fresh_transaction_reads_content_even_if_timestamp_is_reused(self):
        initial = {"shared": {"editor_font_size": 22, "monitor_font_size": 18}}
        self.assertTrue(config._save_raw_config(initial))
        stale = config._load_raw_config()
        target = self.directory / ".mcu_gui_config.json"
        old = target.stat()
        external = {"shared": {"editor_font_size": 24, "monitor_font_size": 18}}
        target.write_text(json.dumps(external, indent=2), encoding="utf-8")
        os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))
        stale["shared"]["monitor_font_size"] = 20
        self.assertTrue(config._save_raw_config(stale))
        self.assertEqual(config._load_raw_config(fresh=True)["shared"],
                         {"editor_font_size": 24, "monitor_font_size": 20})

    def test_config_load_uses_one_stat_per_location(self):
        self.assertTrue(config._save_raw_config({"shared": {"editor_font_size": 22}}))
        targets = {config.LOCAL_GUI_CONFIG, self.directory / ".mcu_gui_config.json"}
        original = Path.stat
        counts = {target: 0 for target in targets}
        def measured(path, *args, **kwargs):
            if path in counts:
                counts[path] += 1
            return original(path, *args, **kwargs)
        with patch.object(Path, "stat", measured):
            self.assertEqual(config._load_raw_config(fresh=True)["shared"]["editor_font_size"], 22)
        self.assertEqual(list(counts.values()), [1, 1])

    def test_compatibility_and_gpio_share_one_source_read_and_directory_pass(self):
        target = self.directory / "sketch.ino"
        target.write_text("void setup(){ pinMode(999, OUTPUT); }\nvoid loop(){}", encoding="utf-8")
        (self.directory / "image.png").write_text("not code", encoding="utf-8")
        original_read = Path.read_text
        with patch.object(board_compat.os, "scandir", wraps=os.scandir) as scan, \
                patch.object(Path, "read_text", autospec=True, wraps=Path.read_text) as read:
            # Call the original implementation while recording paths.
            read.side_effect = lambda path, *args, **kwargs: original_read(path, *args, **kwargs)
            boards, reasons = board_compat.detect_board_compatibility(self.directory)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual([call.args[0] for call in read.call_args_list], [target])
        self.assertTrue(reasons)

    def test_standalone_gpio_analysis_does_not_keep_stale_source(self):
        target = self.directory / "sketch.ino"
        target.write_text("pinMode(999, OUTPUT);", encoding="utf-8")
        before = target.stat()
        first = board_compat._analyze_gpio_compatibility(self.directory)
        target.write_text("pinMode(001, OUTPUT);", encoding="utf-8")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        second = board_compat._analyze_gpio_compatibility(self.directory)
        self.assertTrue(first["excluded"])
        self.assertNotEqual(first, second)

    def test_partial_source_scan_failure_retains_readable_sources(self):
        target = self.directory / "sketch.ino"
        target.write_text("pinMode(999, OUTPUT);", encoding="utf-8")
        good = SimpleNamespace(name=target.name, path=str(target), is_file=lambda: True)
        bad = SimpleNamespace(name="locked.h", path=str(self.directory / "locked.h"),
                              is_file=Mock(side_effect=OSError("temporary metadata failure")))
        def interrupted():
            yield good
            raise OSError("directory interrupted")
        for entries in ([good, bad], interrupted()):
            scan = MagicMock()
            scan.__enter__.return_value = entries
            with patch.object(board_compat.os, "scandir", return_value=scan):
                result = board_compat._analyze_gpio_compatibility(self.directory)
            self.assertEqual(result["excluded"], {"Fixture Uno", "Fixture ESP32"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
