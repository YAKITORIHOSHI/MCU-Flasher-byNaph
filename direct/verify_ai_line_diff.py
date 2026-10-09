"""Verify AI line classifications and deletion anchors using in-memory sources."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main.core.ai_review import build_ai_line_diff


class AILineDiffChecks(unittest.TestCase):
    def assert_counts(self, diff, *, added=0, removed=0, modified=0):
        self.assertEqual(
            (diff["added"], diff["removed"], diff["modified"]),
            (added, removed, modified),
        )

    def assert_anchor(self, change, line, before_start, before_end):
        self.assertEqual(change, {
            "type": "removed", "startLine": line, "endLine": line,
            "anchorOnly": True, "removedCount": before_end - before_start + 1,
            "beforeStartLine": before_start, "beforeEndLine": before_end,
        })

    def test_unchanged_source_has_no_decorations(self):
        for source in ("", "void setup() {}\nvoid loop() {}\n"):
            with self.subTest(source=source):
                diff = build_ai_line_diff(source, source)
                self.assertEqual(diff["changes"], [])
                self.assert_counts(diff)

    def test_insertion_has_only_added_lines(self):
        diff = build_ai_line_diff("keep\ntail\n", "keep\nnew one\nnew two\ntail\n")
        self.assert_counts(diff, added=2)
        self.assertEqual(diff["changes"], [{"type": "added", "startLine": 2, "endLine": 3}])
        self.assertEqual(diff["firstLine"], 2)

    def test_middle_deletion_marks_the_next_surviving_line_without_tinting_it(self):
        diff = build_ai_line_diff("keep\nold one\nold two\ntail\n", "keep\ntail\n")
        self.assert_counts(diff, removed=2)
        self.assert_anchor(diff["changes"][0], 2, 2, 3)
        self.assertEqual(diff["firstLine"], 2)

    def test_equal_size_replacement_remains_modified(self):
        diff = build_ai_line_diff("keep\nold one\nold two\ntail\n", "keep\nnew one\nnew two\ntail\n")
        self.assert_counts(diff, modified=2)
        self.assertEqual(diff["changes"], [{"type": "modified", "startLine": 2, "endLine": 3}])

    def test_replacement_surplus_additions_have_their_own_green_range(self):
        diff = build_ai_line_diff("keep\nold\ntail\n", "keep\nnew one\nnew two\nnew three\ntail\n")
        self.assert_counts(diff, added=2, modified=1)
        self.assertEqual(diff["changes"], [
            {"type": "modified", "startLine": 2, "endLine": 2},
            {"type": "added", "startLine": 3, "endLine": 4},
        ])

    def test_replacement_surplus_deletions_have_a_separate_red_anchor(self):
        diff = build_ai_line_diff("keep\nold one\nold two\nold three\ntail\n", "keep\nnew\ntail\n")
        self.assert_counts(diff, removed=2, modified=1)
        self.assertEqual(diff["changes"][0], {"type": "modified", "startLine": 2, "endLine": 2})
        self.assert_anchor(diff["changes"][1], 3, 3, 4)

    def test_eof_deletion_anchor_can_share_a_modified_line_without_reclassifying_it(self):
        diff = build_ai_line_diff("keep\nold one\nold two\n", "keep\nnew\n")
        self.assert_counts(diff, removed=1, modified=1)
        self.assertEqual(diff["changes"][0], {"type": "modified", "startLine": 2, "endLine": 2})
        self.assert_anchor(diff["changes"][1], 2, 3, 3)
        self.assertEqual(diff["firstLine"], 2)

    def test_whole_file_deletion_anchors_to_monacos_empty_first_line(self):
        diff = build_ai_line_diff("old one\nold two\n", "")
        self.assert_counts(diff, removed=2)
        self.assert_anchor(diff["changes"][0], 1, 1, 2)
        self.assertEqual(diff["firstLine"], 1)

    def test_multiple_hunks_keep_surviving_lines_out_of_modified_ranges(self):
        diff = build_ai_line_diff(
            "old first\nkeep A\nremove\nkeep B\nold last\n",
            "new first\nkeep A\nkeep B\nnew last\ninsert\n",
        )
        self.assert_counts(diff, added=1, removed=1, modified=2)
        self.assertEqual([item["type"] for item in diff["changes"]],
                         ["modified", "removed", "modified", "added"])
        self.assert_anchor(diff["changes"][1], 3, 3, 3)
        self.assertEqual(diff["changes"][-1], {"type": "added", "startLine": 5, "endLine": 5})

    def test_large_pure_deletions_use_bounded_path_and_never_turn_blue(self):
        lines = [f"unique source line {index}" for index in range(5000)]
        before = "\n".join(lines)
        for index in (0, 3, len(lines) - 1):
            with self.subTest(index=index), patch("main.core.ai_review.difflib.SequenceMatcher",
                                                  side_effect=AssertionError("unbounded matcher")):
                after = "\n".join(lines[:index] + lines[index + 1:])
                diff = build_ai_line_diff(before, after)
                self.assert_counts(diff, removed=1)
                self.assert_anchor(diff["changes"][0], min(index + 1, 4999), index + 1, index + 1)

    def test_large_pure_insertions_use_bounded_path_and_never_turn_blue(self):
        lines = [f"unique source line {index}" for index in range(5000)]
        before = "\n".join(lines)
        for index in (0, 3, len(lines)):
            with self.subTest(index=index), patch("main.core.ai_review.difflib.SequenceMatcher",
                                                  side_effect=AssertionError("unbounded matcher")):
                after = "\n".join(lines[:index] + ["inserted"] + lines[index:])
                diff = build_ai_line_diff(before, after)
                self.assert_counts(diff, added=1)
                self.assertEqual(diff["changes"], [{"type": "added", "startLine": index + 1, "endLine": index + 1}])

    def test_large_replacement_splits_shared_and_surplus_rows(self):
        lines = [f"unique source line {index}" for index in range(5000)]
        before = "\n".join(lines)
        with patch("main.core.ai_review.difflib.SequenceMatcher", side_effect=AssertionError("unbounded matcher")):
            fewer = build_ai_line_diff(before, "\n".join(lines[:3] + ["replacement"] + lines[6:]))
            more = build_ai_line_diff(before, "\n".join(lines[:3] + ["replacement", "addition"] + lines[4:]))
        self.assert_counts(fewer, removed=2, modified=1)
        self.assert_anchor(fewer["changes"][1], 5, 5, 6)
        self.assert_counts(more, added=1, modified=1)
        self.assertEqual(more["changes"], [
            {"type": "modified", "startLine": 4, "endLine": 4},
            {"type": "added", "startLine": 5, "endLine": 5},
        ])

    def test_large_line_ending_change_does_not_create_a_zero_count_hunk(self):
        lines = [f"unique source line {index}" for index in range(5000)]
        with patch("main.core.ai_review.difflib.SequenceMatcher", side_effect=AssertionError("unbounded matcher")):
            diff = build_ai_line_diff("\n".join(lines), "\r\n".join(lines))
        self.assert_counts(diff)
        self.assertEqual(diff["changes"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
