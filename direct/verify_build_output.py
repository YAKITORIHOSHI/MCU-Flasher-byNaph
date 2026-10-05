#!/usr/bin/env python3
"""Toolkit-independent checks for conservative Build Console presentation.

Uses isolated dictionaries only; no Qt, backend, persistence, or hardware starts.
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from main.core.build_output import BuildOutputPresenter


class BuildOutputChecks(unittest.TestCase):
    def setUp(self):
        self.presenter = BuildOutputPresenter()

    def unit(self, name="main.cpp.o", **fields):
        entry = {"text": f"  ⚙ Compiling {name}...", "tag": "info", "newline": True}
        entry.update(fields)
        return self.presenter.present(entry)

    def test_contiguous_units_share_a_key_and_keep_latest_name(self):
        first = self.unit()
        second = self.unit("driver.cpp.o")
        self.assertEqual(first["replace_key"], second["replace_key"])
        self.assertIn("1 processed", first["text"])
        self.assertIn("2 processed", second["text"])
        self.assertTrue(second["text"].endswith("driver.cpp.o"))
        self.assertEqual(second["tag"], "system")

    def test_each_visible_record_is_a_compile_run_barrier(self):
        for barrier in (
            {"text": "main.cpp:3: note: candidate declared here", "tag": "info"},
            {"text": "  3 | lookup(\"building / looking for\");", "tag": "normal"},
            {"text": "      ^~~~~~", "tag": "dim"},
            {"text": "🔗 Linking...", "tag": "dim"},
        ):
            with self.subTest(barrier=barrier):
                first = self.unit()
                self.assertEqual(self.presenter.present(barrier), barrier)
                next_run = self.unit("next.cpp.o")
                self.assertNotEqual(first["replace_key"], next_run["replace_key"])
                self.assertIn("1 processed", next_run["text"])

    def test_severity_records_never_disappear_or_aggregate(self):
        for tag in ("warning", "error", "severe_alert"):
            for text in ("⚙ Compiling missing.cpp.o...", "╔════╗", "────"):
                with self.subTest(tag=tag, text=text):
                    first = self.unit()
                    entry = {"text": text, "tag": tag, "newline": True, "ts": "[12:34:56]"}
                    self.assertEqual(self.presenter.present(entry), entry)
                    next_run = self.unit()
                    self.assertNotEqual(first["replace_key"], next_run["replace_key"])

    def test_only_expected_decorations_are_removed_without_breaking_a_run(self):
        first = self.unit()
        for text in ("╔════╗", "╠════╣", "╚════╝", "┌────┐", "├────┤", "└────┘",
                     "╭────╮", "╰────╯", "────", "====", "║    ║", ""):
            with self.subTest(text=text):
                self.assertIsNone(self.presenter.present({"text": text, "tag": "purple_header"}))
        second = self.unit()
        self.assertEqual(first["replace_key"], second["replace_key"])
        self.assertIn("2 processed", second["text"])
        for text in ("────", "╔════╗", "│ source │", "===="):
            entry = {"text": text, "tag": "normal"}
            self.assertEqual(self.presenter.present(entry), entry)

    def test_boxed_titles_workers_and_timing_values_are_readable(self):
        for tag in ("header", "purple_header"):
            for frame in ("║ {} ║", "│ {} │"):
                with self.subTest(tag=tag, frame=frame):
                    self.presenter.reset()
                    build = self.presenter.present({"text": frame.format("⚙ COMPILING (PlatformIO)"), "tag": tag})
                    workers = self.presenter.present({"text": frame.format("⚡ Running Parallel Compilation on 4 Logical Processors"), "tag": tag})
                    self.assertEqual(build["text"], "Build · PlatformIO")
                    self.assertEqual(build["tag"], "header")
                    self.assertEqual(workers["text"], "Compiler workers: 4")
                    self.assertEqual(workers["tag"], "normal")
        self.presenter.reset()
        self.presenter.present({"text": "╔════════╗", "tag": "purple_header"})
        title = self.presenter.present({"text": "║ Compilation Time Breakdown ║", "tag": "purple_header"})
        self.assertEqual(title["text"], "Compilation Time Breakdown")
        self.assertEqual(title["tag"], "header")
        self.presenter.present({"text": "╠════════╣", "tag": "purple_header"})
        for label, value in (("Framework & Tool Download", "2.4s"),
                             ("Code Build & Compilation", "26.4s"),
                             ("Total Elapsed Time", "30.1s")):
            row = self.presenter.present({"text": f"║ {label} : {value} ║", "tag": "purple_header"})
            self.assertEqual(row["text"], f"{label} : {value}")
            self.assertEqual(row["tag"], "normal")

    def test_raw_object_paths_are_kept_and_unknown_compile_sentences_pass(self):
        for path in (".pio/build/env/src/main.cpp.o", r".pio\build\env\src\main.cpp.obj"):
            with self.subTest(path=path):
                entry = self.presenter.present({"text": f"Compiling {path}", "tag": "normal"})
                self.assertTrue(entry["text"].endswith(path))
        for text, tag in (("Compiling failed to create target .o", "normal"),
                          ("toolchain: building firmware failed", "info"),
                          ("  9 | Compiling .pio/build/env/src/main.cpp.o", "dim"),
                          ("⚙ Compiling source.cpp.o...", "normal"),
                          ("Path: C:/my project/complete/source.cpp", "info")):
            entry = {"text": text, "tag": tag, "newline": False, "ts": "[01:02:03]"}
            self.assertEqual(self.presenter.present(entry), entry)

    def test_reset_restarts_unit_count_and_preserves_input_fields(self):
        entry = {"text": "⚙ Compiling main.cpp.o...", "tag": "info", "newline": False,
                 "ts": "[01:02:03]", "extra": {"target": "fixture"}}
        original = copy.deepcopy(entry)
        result = self.presenter.present(entry)
        self.presenter.present(entry)
        self.assertEqual(entry, original)
        self.assertIsNot(result, entry)
        self.assertEqual(result["ts"], entry["ts"])
        self.assertFalse(result["newline"])
        self.assertEqual(result["extra"], entry["extra"])
        self.assertNotIn("replace_key", entry)
        self.presenter.reset()
        reset = self.presenter.present(entry)
        self.assertIn("1 processed", reset["text"])
        self.assertEqual(entry, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
