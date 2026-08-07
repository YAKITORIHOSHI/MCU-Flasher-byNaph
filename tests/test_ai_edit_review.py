import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mcu_flash_gui as gui_module


class _ImmediateRoot:
    def after(self, _delay, callback):
        callback()
        return "job"


class _FakeAIController:
    def __init__(self):
        self.baselines = []

    def note_local_save(self, path, content=None):
        self.baselines.append((str(path), content))


class _FakeGui:
    def __init__(self, project_dir, state_path=True):
        self.sketch_dir_path = Path(project_dir)
        if state_path:
            self.ai_review_state_path = self.sketch_dir_path / "review-state.json"
        self.root = _ImmediateRoot()
        self.ai_controller = _FakeAIController()
        self.notifications = []
        self.update_count = 0

    def _update_skip_compile_state(self):
        self.update_count += 1

    def _update_editor_info(self):
        self.update_count += 1

    def _append_notif(self, text, tag, **metadata):
        self.notifications.append((text, tag, metadata))


def _write_exact(path, content):
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(content)


class AIEditReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name)
        self.gui = _FakeGui(self.project)
        self.api = gui_module.EditorApi(self.gui)

    def tearDown(self):
        try:
            self.temporary_directory.cleanup()
        except Exception:
            try:
                # pyrefly: ignore [missing-import]
                from bootstrap import safe_rmtree
                safe_rmtree(self.temporary_directory.name)
            except Exception:
                pass

    def _stage_modified(self, name="sketch.ino", before="old\r\n", after="new\r\n"):
        path = self.project / name
        _write_exact(path, after)
        self.assertTrue(
            self.api.queue_ai_edit_snapshot(
                path, before, after, before_exists=True, after_exists=True
            )
        )
        return path

    def test_snapshot_peek_is_non_destructive_and_persistent(self):
        path = self._stage_modified(before="void setup() {}\r\n", after="void setup() { pinMode(2, 1); }\r\n")

        first = self.api.consume_ai_edit_snapshot(path)
        second = self.api.consume_ai_edit_snapshot(path)
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(first["beforeContent"], "void setup() {}\r\n")
        self.assertEqual(first["pendingCount"], 1)

        reloaded_api = gui_module.EditorApi(_FakeGui(self.project))
        restored = reloaded_api.consume_ai_edit_snapshot(path)
        self.assertEqual(restored["revision"], first["revision"])
        self.assertEqual(restored["content"], "void setup() { pinMode(2, 1); }\r\n")

    def test_accept_modified_keeps_proposal_and_removes_review(self):
        path = self._stage_modified(before="old", after="AI result")
        review = self.api.consume_ai_edit_snapshot(path)

        result = self.api.accept_ai_edit(path, review["revision"])

        self.assertTrue(result["success"])
        self.assertEqual(path.read_text(encoding="utf-8"), "AI result")
        self.assertEqual(self.api.get_ai_edit_reviews(), [])
        self.assertEqual(gui_module.EditorApi(_FakeGui(self.project)).get_ai_edit_reviews(), [])

    def test_reject_modified_restores_unicode_and_crlf_exactly(self):
        before = "// café Ω\r\nvoid setup() {}\r\n"
        path = self._stage_modified(before=before, after="// AI\nvoid setup() { }\n")
        review = self.api.consume_ai_edit_snapshot(path)

        result = self.api.reject_ai_edit(path, review["revision"])

        self.assertTrue(result["success"])
        self.assertEqual(path.read_bytes(), before.encode("utf-8"))
        self.assertFalse(self.api.has_pending_ai_edit(path))

    def test_new_file_accept_and_reject(self):
        accepted = self.project / "accepted.txt"
        _write_exact(accepted, "created by AI\n")
        self.api.queue_ai_edit_snapshot(
            accepted, "", "created by AI\n", before_exists=False, after_exists=True
        )
        accepted_review = self.api.consume_ai_edit_snapshot(accepted)
        self.assertTrue(self.api.accept_ai_edit(accepted, accepted_review["revision"])["success"])
        self.assertTrue(accepted.exists())

        rejected = self.project / "rejected.txt"
        _write_exact(rejected, "temporary AI file\n")
        self.api.queue_ai_edit_snapshot(
            rejected, "", "temporary AI file\n", before_exists=False, after_exists=True
        )
        rejected_review = self.api.consume_ai_edit_snapshot(rejected)
        self.assertTrue(self.api.reject_ai_edit(rejected, rejected_review["revision"])["success"])
        self.assertFalse(rejected.exists())

    def test_deleted_file_accept_and_reject(self):
        accepted = self.project / "delete-accepted.h"
        before_accepted = "#define KEEP 1\r\n"
        self.api.queue_ai_edit_snapshot(
            accepted, before_accepted, "", before_exists=True, after_exists=False
        )
        accepted_review = self.api.consume_ai_edit_snapshot(accepted)
        self.assertTrue(self.api.accept_ai_edit(accepted, accepted_review["revision"])["success"])
        self.assertFalse(accepted.exists())

        rejected = self.project / "delete-rejected.h"
        before_rejected = "#define RESTORE 1\r\n"
        self.api.queue_ai_edit_snapshot(
            rejected, before_rejected, "", before_exists=True, after_exists=False
        )
        rejected_review = self.api.consume_ai_edit_snapshot(rejected)
        self.assertTrue(self.api.reject_ai_edit(rejected, rejected_review["revision"])["success"])
        self.assertEqual(rejected.read_bytes(), before_rejected.encode("utf-8"))

    def test_empty_existing_file_is_not_treated_as_new(self):
        path = self.project / "empty.txt"
        _write_exact(path, "AI text")
        self.api.queue_ai_edit_snapshot(
            path, "", "AI text", before_exists=True, after_exists=True
        )
        review = self.api.consume_ai_edit_snapshot(path)

        self.assertTrue(self.api.reject_ai_edit(path, review["revision"])["success"])
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), b"")

    def test_stale_revision_cannot_resolve_a_newer_edit(self):
        path = self._stage_modified(before="original", after="AI v1")
        stale_revision = self.api.consume_ai_edit_snapshot(path)["revision"]
        _write_exact(path, "AI v2")
        self.api.queue_ai_edit_snapshot(
            path, "AI v1", "AI v2", before_exists=True, after_exists=True
        )

        result = self.api.reject_ai_edit(path, stale_revision)

        self.assertFalse(result["success"])
        self.assertTrue(result["conflict"])
        self.assertEqual(path.read_text(encoding="utf-8"), "AI v2")
        latest = self.api.consume_ai_edit_snapshot(path)
        self.assertNotEqual(latest["revision"], stale_revision)
        self.assertEqual(latest["beforeContent"], "original")

    def test_local_disk_change_conflicts_without_overwrite(self):
        path = self._stage_modified(before="original", after="AI proposal")
        review = self.api.consume_ai_edit_snapshot(path)
        _write_exact(path, "user changed this")

        result = self.api.reject_ai_edit(path, review["revision"])

        self.assertFalse(result["success"])
        self.assertTrue(result["conflict"])
        self.assertEqual(path.read_text(encoding="utf-8"), "user changed this")
        self.assertTrue(self.api.has_pending_ai_edit(path))

        refreshed = result["snapshot"]
        self.assertNotEqual(refreshed["revision"], review["revision"])
        accepted = self.api.accept_ai_edit(path, refreshed["revision"])
        self.assertTrue(accepted["success"])
        self.assertEqual(path.read_text(encoding="utf-8"), "user changed this")

    def test_pending_review_blocks_editor_save_and_resolution_syncs_watcher(self):
        path = self._stage_modified(before="original", after="AI proposal")
        blocked = self.api.save_file(path, "autosave must not replace this")
        self.assertFalse(blocked["success"])
        self.assertTrue(blocked["reviewPending"])
        self.assertEqual(path.read_text(encoding="utf-8"), "AI proposal")

        review = self.api.consume_ai_edit_snapshot(path)
        self.api.reject_ai_edit(path, review["revision"])
        self.assertEqual(self.gui.ai_controller.baselines[-1], (str(path.resolve()), "original"))

    def test_edit_returning_to_original_cancels_review(self):
        path = self._stage_modified(before="original", after="AI proposal")
        _write_exact(path, "original")

        result = self.api.queue_ai_edit_snapshot(
            path, "AI proposal", "original", before_exists=True, after_exists=True
        )

        self.assertEqual(result, "cancelled")
        self.assertEqual(self.api.get_ai_edit_reviews(), [])

    def test_resolved_review_does_not_resurrect_if_atomic_journal_write_fails(self):
        path = self._stage_modified(before="original", after="AI proposal")
        review = self.api.consume_ai_edit_snapshot(path)

        with mock.patch.object(
            self.api, "_persist_pending_ai_edits_locked", side_effect=OSError("locked")
        ):
            result = self.api.accept_ai_edit(path, review["revision"])

        self.assertTrue(result["success"])
        fresh_api = gui_module.EditorApi(_FakeGui(self.project))
        self.assertFalse(fresh_api.has_pending_ai_edit(path))

    def test_corrupt_journal_recovers_from_backup_and_otherwise_fails_closed(self):
        path = self._stage_modified(before="original", after="AI proposal")
        state_path = self.gui.ai_review_state_path
        backup_path = state_path.with_suffix(state_path.suffix + ".bak")
        backup_path.write_bytes(state_path.read_bytes())
        state_path.write_text("{broken", encoding="utf-8")

        recovered = gui_module.EditorApi(_FakeGui(self.project))
        self.assertTrue(recovered.has_pending_ai_edit(path))
        self.assertEqual(recovered.get_ai_review_journal_error(), "")

        backup_path.write_text("also broken", encoding="utf-8")
        failed_closed = gui_module.EditorApi(_FakeGui(self.project))
        self.assertTrue(failed_closed.has_any_pending_ai_edits())
        self.assertTrue(failed_closed.get_ai_review_journal_error())

    def test_newest_backup_generation_preserves_every_pending_review(self):
        first = self._stage_modified("first.ino", "first", "AI first")
        state_path = self.gui.ai_review_state_path
        stale_primary = state_path.read_bytes()

        second = self._stage_modified("second.h", "second", "AI second")
        backup_path = state_path.with_suffix(state_path.suffix + ".bak")
        primary_data = json.loads(state_path.read_text(encoding="utf-8"))
        backup_data = json.loads(backup_path.read_text(encoding="utf-8"))

        self.assertEqual(primary_data["generation"], backup_data["generation"])
        self.assertEqual(len(backup_data["reviews"]), 2)

        # Simulate an interruption that leaves the primary one generation old.
        state_path.write_bytes(stale_primary)
        recovered = gui_module.EditorApi(_FakeGui(self.project))
        self.assertTrue(recovered.has_pending_ai_edit(first))
        self.assertTrue(recovered.has_pending_ai_edit(second))
        self.assertEqual(recovered.get_ai_review_journal_error(), "")

    def test_same_generation_replica_disagreement_fails_closed(self):
        self._stage_modified(before="original", after="AI proposal")
        state_path = self.gui.ai_review_state_path
        backup_path = state_path.with_suffix(state_path.suffix + ".bak")
        backup_data = json.loads(backup_path.read_text(encoding="utf-8"))
        backup_data["reviews"][0]["content"] = "different proposal"
        backup_path.write_text(json.dumps(backup_data), encoding="utf-8")

        failed_closed = gui_module.EditorApi(_FakeGui(self.project))

        self.assertTrue(failed_closed.has_any_pending_ai_edits())
        self.assertTrue(failed_closed.get_ai_review_journal_error())

    def test_corrupt_v2_commit_replica_does_not_fall_back_to_primary(self):
        self._stage_modified(before="original", after="AI proposal")
        state_path = self.gui.ai_review_state_path
        backup_path = state_path.with_suffix(state_path.suffix + ".bak")
        backup_path.write_text("{broken", encoding="utf-8")

        failed_closed = gui_module.EditorApi(_FakeGui(self.project))

        self.assertTrue(failed_closed.has_any_pending_ai_edits())
        self.assertTrue(failed_closed.get_ai_review_journal_error())

    def test_resolution_commit_failure_stays_pending_and_reports_failure(self):
        path = self._stage_modified(before="original", after="AI proposal")
        review = self.api.consume_ai_edit_snapshot(path)

        with mock.patch.object(
            self.api, "_persist_pending_ai_edits_locked", side_effect=OSError("locked")
        ), mock.patch.object(
            self.api, "_persist_pending_ai_edits_fallback_locked", side_effect=OSError("still locked")
        ):
            result = self.api.accept_ai_edit(path, review["revision"])

        self.assertFalse(result["success"])
        self.assertTrue(result["journalError"])
        self.assertTrue(self.api.has_pending_ai_edit(path))
        self.assertTrue(self.api.get_ai_review_journal_error())
        # Restart sees the same pending decision the user was told had failed;
        # it is not a supposedly resolved review being resurrected.
        self.assertTrue(gui_module.EditorApi(_FakeGui(self.project)).has_pending_ai_edit(path))

        retry = self.api.accept_ai_edit(path, review["revision"])
        self.assertTrue(retry["success"])
        self.assertFalse(gui_module.EditorApi(_FakeGui(self.project)).has_pending_ai_edit(path))

    def test_resolved_tombstone_wins_if_primary_is_later_corrupted(self):
        path = self._stage_modified(before="original", after="AI proposal")
        review = self.api.consume_ai_edit_snapshot(path)
        self.assertTrue(self.api.accept_ai_edit(path, review["revision"])["success"])

        state_path = self.gui.ai_review_state_path
        backup_path = state_path.with_suffix(state_path.suffix + ".bak")
        self.assertEqual(json.loads(backup_path.read_text(encoding="utf-8"))["reviews"], [])
        state_path.write_text("{broken", encoding="utf-8")

        recovered = gui_module.EditorApi(_FakeGui(self.project))
        self.assertEqual(recovered.get_ai_edit_reviews(), [])
        self.assertEqual(recovered.get_ai_review_journal_error(), "")

    def test_multifile_resolution_returns_the_next_review(self):
        first = self._stage_modified("first.ino", "a", "AI a")
        second = self._stage_modified("second.h", "b", "AI b")
        first_review = self.api.consume_ai_edit_snapshot(first)

        result = self.api.accept_ai_edit(first, first_review["revision"])

        self.assertEqual(Path(result["nextPath"]), second.resolve())
        self.assertEqual(result["pendingCount"], 1)
        self.assertTrue(self.api.has_pending_ai_edit(second))

    def test_project_binding_keeps_review_journals_isolated(self):
        second_project = self.project / "project-two"
        second_project.mkdir()

        def journal_for(project):
            return Path(project) / "journal.json"

        with mock.patch.object(gui_module, "get_ai_review_state_file", side_effect=journal_for):
            first_gui = _FakeGui(self.project, state_path=False)
            api = gui_module.EditorApi(first_gui)
            first_file = self.project / "first.ino"
            _write_exact(first_file, "AI first")
            api.queue_ai_edit_snapshot(first_file, "first", "AI first")

            first_gui.sketch_dir_path = second_project
            api.bind_project(second_project)
            self.assertEqual(api.get_ai_edit_reviews(), [])
            second_file = second_project / "second.ino"
            _write_exact(second_file, "AI second")
            api.queue_ai_edit_snapshot(second_file, "second", "AI second")

            first_gui.sketch_dir_path = self.project
            api.bind_project(self.project)
            self.assertTrue(api.has_pending_ai_edit(first_file))
            self.assertFalse(api.has_pending_ai_edit(second_file))

    def test_project_binding_failure_keeps_current_reviews_in_memory(self):
        second_project = self.project / "project-two"
        second_project.mkdir()

        with mock.patch.object(
            gui_module,
            "get_ai_review_state_file",
            side_effect=lambda project: Path(project) / "journal.json",
        ):
            gui = _FakeGui(self.project, state_path=False)
            api = gui_module.EditorApi(gui)
            first_file = self.project / "first.ino"
            _write_exact(first_file, "AI first")
            api.queue_ai_edit_snapshot(first_file, "first", "AI first")
            original_journal = api._ai_review_state_path

            gui.sketch_dir_path = second_project
            with mock.patch.object(
                api, "_persist_pending_ai_edits_locked", side_effect=OSError("locked")
            ), mock.patch.object(
                api, "_persist_pending_ai_edits_fallback_locked", side_effect=OSError("still locked")
            ):
                with self.assertRaises(OSError):
                    api.bind_project(second_project)

            self.assertEqual(api._ai_review_state_path, original_journal)
            self.assertTrue(api.has_pending_ai_edit(first_file))
            self.assertTrue(api.get_ai_review_journal_error())

    def test_project_binding_refuses_to_overwrite_unrecoverable_journal(self):
        self._stage_modified(before="original", after="AI proposal")
        state_path = self.gui.ai_review_state_path
        backup_path = state_path.with_suffix(state_path.suffix + ".bak")
        state_path.write_text("{broken", encoding="utf-8")
        backup_path.write_text("also broken", encoding="utf-8")
        failed_closed = gui_module.EditorApi(_FakeGui(self.project))
        original_primary = state_path.read_bytes()
        original_backup = backup_path.read_bytes()

        with self.assertRaises(OSError):
            failed_closed.bind_project(self.project / "other-project")

        self.assertEqual(state_path.read_bytes(), original_primary)
        self.assertEqual(backup_path.read_bytes(), original_backup)

    def test_project_file_listing_includes_nested_hpp_but_excludes_generated_src(self):
        include_dir = self.project / "include"
        generated_dir = self.project / "src"
        include_dir.mkdir()
        generated_dir.mkdir()
        _write_exact(include_dir / "motor.hpp", "#pragma once\n")
        _write_exact(generated_dir / "generated.hpp", "#pragma once\n")

        paths = {Path(item["path"]).resolve() for item in self.api.get_project_files()}

        self.assertIn((include_dir / "motor.hpp").resolve(), paths)
        self.assertNotIn((generated_dir / "generated.hpp").resolve(), paths)

    def test_diff_reports_first_changed_line_and_counts(self):
        diff = gui_module.build_ai_line_diff(
            "line 1\nold value\nline 3\n",
            "line 1\nnew value\nextra\nline 3\n",
        )

        self.assertEqual(diff["firstLine"], 2)
        self.assertGreaterEqual(diff["modified"], 1)
        self.assertGreaterEqual(diff["added"], 1)
        self.assertTrue(diff["changes"])

    def test_compile_and_upload_gate_reports_pending_review(self):
        app = gui_module.MCUUploadGUI.__new__(gui_module.MCUUploadGUI)
        opened = []
        notices = []
        app.editor_api = SimpleNamespace(
            has_any_pending_ai_edits=lambda: True,
            get_ai_edit_reviews=lambda: [{"path": str(self.project / "sketch.ino")}],
        )
        app.editor_window = SimpleNamespace(evaluate_js=lambda script: opened.append(script))
        app._append_notif = lambda *args, **kwargs: notices.append((args, kwargs))

        blocked = app._block_action_for_pending_ai_review("Upload")

        self.assertTrue(blocked)
        self.assertTrue(notices)
        self.assertIn("reloadActiveFileWithDiff", opened[0])

    def test_editor_file_api_rejects_paths_outside_project(self):
        outside = self.project.parent / f"{self.project.name}-outside-ai-review.txt"
        result = self.api.save_file(outside, "must not be written")
        self.assertFalse(result["success"])
        self.assertFalse(outside.exists())

    def test_monaco_bundle_uses_collision_free_tab_ids(self):
        bundle_path = Path(gui_module.__file__).resolve().parent / "src" / "editor" / "bundle.js"
        bundle = bundle_path.read_text(encoding="utf-8")
        old_helper = 'function zae(e){return e.replace(/[^a-zA-Z0-9-_]/g,"_")}'
        new_helper = (
            'function zae(e){return Array.from(new TextEncoder().encode(e))'
            '.map(e=>e.toString(16).padStart(2,"0")).join("")}'
        )

        self.assertNotIn(old_helper, bundle)
        self.assertIn(new_helper, bundle)
        self.assertEqual("folder/a-b.ino".replace("/", "_"), "folder_a-b.ino")
        self.assertEqual("folder_a-b.ino", "folder_a-b.ino")
        self.assertNotEqual(
            "folder/a-b.ino".encode("utf-8").hex(),
            "folder_a-b.ino".encode("utf-8").hex(),
        )


if __name__ == "__main__":
    unittest.main()
