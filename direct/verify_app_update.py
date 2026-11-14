#!/usr/bin/env python3
"""Exercise Bootstrap's GitHub source updater in disposable fixtures only.

No GitHub request, installer, launcher, hardware, runtime or live state is
used.  The fixture proves the updater stages source before replacing files,
preserves local state, rejects unsafe inputs and leaves no partial update after
an install failure.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from src.modules import app_update


class UpdatePolicyChecks(unittest.TestCase):
    def test_skip_updates_covers_source_updates_without_disabling_required_repairs(self):
        self.assertTrue(app_update.automatic_updates_enabled({}, environ={}))
        self.assertFalse(app_update.automatic_updates_enabled({"skip_updates": True}, environ={}))
        self.assertTrue(app_update.automatic_updates_enabled({"skip_updates": False}, environ={}))
        self.assertFalse(app_update.automatic_updates_enabled({}, environ={"MCU_FLASH_GUI_SKIP_UPDATES": "true"}))
        self.assertFalse(app_update.automatic_updates_enabled({}, environ={"MCU_FLASHER_DISABLE_APP_UPDATE": "1"}))


class _Response:
    def __init__(self, payload: bytes, url: str, *, status: int = 200, headers: dict[str, str] | None = None):
        self._payload = payload
        self._url = url
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._payload)
        value, self._payload = self._payload[:size], self._payload[size:]
        return value

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status


class _SlowResponse(_Response):
    """Advance a fixture clock on each read without using real time or I/O."""

    def __init__(self, *args, clock: list[float], elapsed: float, **kwargs):
        super().__init__(*args, **kwargs)
        self._clock = clock
        self._elapsed = elapsed

    def read(self, size: int = -1) -> bytes:
        self._clock[0] += self._elapsed
        return super().read(size)


class SourceUpdateChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp" / "audit" / "app-update"
        audit.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(prefix="fixture-", dir=audit)
        self.addCleanup(self._temporary.cleanup)
        self.fixture = Path(self._temporary.name)
        self.install = self.fixture / "installation"
        self.source = self.fixture / "source"
        self.state = self.fixture / "state"
        self.install.mkdir()
        self.source.mkdir()
        self.candidate = app_update.SourceCandidate("a" * 40, "main")
        self._write_layout(self.install, "installed")
        self._write_layout(self.source, "staged")

    @staticmethod
    def _file(root: Path, relative: str, data: bytes) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _write_layout(self, root: Path, marker: str) -> None:
        for relative in app_update.REQUIRED_SOURCE_FILES:
            self._file(root, relative, f"{marker}:{relative}\n".encode("utf-8"))
        self._file(root, "feature.py", f"{marker}:feature\n".encode("utf-8"))
        self._file(root, "src/dbs/bootstrap_config.json", b'{"local": true}\n')
        self._file(root, "index_json/package_index.json", b'{"cached": true}\n')
        self._file(root, "installers/example.exe", b"local-installer")
        self._file(root, "AGENTS.md", b"user-owned instructions\n")
        self._file(root, "LAUNCHER/WINDOWS.exe", b"active-launcher")

    def _source_names(self, *, extra: tuple[str, ...] = ()) -> list[str]:
        return sorted((*app_update.REQUIRED_SOURCE_FILES, "feature.py", "src/dbs/bootstrap_config.json",
                       "index_json/package_index.json", "installers/example.exe", "AGENTS.md",
                       "LAUNCHER/WINDOWS.exe", *extra))

    def _assert_required_marker(self, root: Path, marker: str) -> None:
        for relative in app_update.REQUIRED_SOURCE_FILES:
            self.assertEqual((root / relative).read_bytes(), f"{marker}:{relative}\n".encode("utf-8"))

    def test_skip_selected_after_source_staging_leaves_installation_untouched(self):
        skipped = [False]

        def prepare(*_args, **_options):
            skipped[0] = True
            return self.source, self._source_names()

        with patch.object(app_update, "_prepare_git_source", side_effect=prepare), \
                self.assertRaisesRegex(app_update.ApplicationUpdateError, "skipped before installation"):
            app_update.apply_source_update(self.install, self.candidate, state_base=self.state,
                                           cancelled=lambda: skipped[0])
        self._assert_required_marker(self.install, "installed")
        self.assertFalse((self.install / app_update.BUNDLED_REVISION_FILE).exists())

    def test_staged_source_replaces_application_files_and_preserves_local_state(self):
        result = app_update.install_staged_source(
            self.install, self.source, self._source_names(), self.candidate,
            strategy="fixture", state_base=self.state,
        )
        self.assertEqual(result.strategy, "fixture")
        self.assertGreater(result.changed_files, 0)
        self._assert_required_marker(self.install, "staged")
        self.assertEqual((self.install / "feature.py").read_text(encoding="utf-8"), "staged:feature\n")
        self.assertEqual((self.install / "src/dbs/bootstrap_config.json").read_bytes(), b'{"local": true}\n')
        self.assertEqual((self.install / "index_json/package_index.json").read_bytes(), b'{"cached": true}\n')
        self.assertEqual((self.install / "installers/example.exe").read_bytes(), b"local-installer")
        self.assertEqual((self.install / "AGENTS.md").read_bytes(), b"user-owned instructions\n")
        self.assertEqual((self.install / "LAUNCHER/WINDOWS.exe").read_bytes(), b"active-launcher")
        state = json.loads(app_update.update_state_path(self.install, state_base=self.state).read_text(encoding="utf-8"))
        self.assertEqual(state["applied_commit"], self.candidate.commit)
        self.assertEqual(state["strategy"], "fixture")
        self.assertNotIn("AGENTS.md", state["managed_files"])
        self.assertNotIn("LAUNCHER/WINDOWS.exe", state["managed_files"])

    def test_mid_copy_failure_restores_every_changed_application_file(self):
        original = {relative: (self.install / relative).read_bytes() for relative in app_update.REQUIRED_SOURCE_FILES}
        self._file(self.source, "zz-broken.py", b"new source\n")
        (self.install / "zz-broken.py").mkdir()
        with self.assertRaisesRegex(app_update.ApplicationUpdateError, "redirected or invalid"):
            app_update.install_staged_source(
                self.install, self.source, self._source_names(extra=("zz-broken.py",)), self.candidate,
                strategy="fixture", state_base=self.state,
            )
        self.assertEqual({relative: (self.install / relative).read_bytes() for relative in original}, original)
        self.assertFalse(app_update.update_state_path(self.install, state_base=self.state).exists())

    def test_incomplete_rollback_reports_manual_recovery_need(self):
        self._file(self.source, "zz-broken.py", b"new source\n")
        (self.install / "zz-broken.py").mkdir()
        with patch.object(app_update, "_rollback", return_value=["mcu_flash_gui.py (PermissionError)"]):
            with self.assertRaisesRegex(app_update.ApplicationUpdateError, "manual recovery may be needed"):
                app_update.install_staged_source(
                    self.install, self.source, self._source_names(extra=("zz-broken.py",)), self.candidate,
                    strategy="fixture", state_base=self.state,
                )

    def test_unresolved_lfs_source_is_rejected_before_any_install_write(self):
        target = self.install / "mcu_flash_gui.py"
        before = target.read_bytes()
        (self.source / "mcu_flash_gui.py").write_bytes(
            b"version https://git-lfs.github.com/spec/v1\noid sha256:fixture\nsize 1\n"
        )
        with self.assertRaisesRegex(app_update.ApplicationUpdateError, "Git LFS"):
            app_update.install_staged_source(
                self.install, self.source, self._source_names(), self.candidate,
                strategy="fixture", state_base=self.state,
            )
        self.assertEqual(target.read_bytes(), before)

    def test_git_environment_skips_lfs_smudge(self):
        completed = SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")
        captured = {}

        def run(_command, **kwargs):
            captured.update(kwargs["env"])
            return completed

        with patch.object(app_update.shutil, "which", return_value="fixture-git"), \
                patch.object(app_update.subprocess, "run", side_effect=run):
            self.assertEqual(app_update._run_git(["--version"], cwd=self.fixture, timeout=1), b"ok")
        self.assertEqual(captured["GIT_LFS_SKIP_SMUDGE"], "1")
        self.assertEqual(captured["GIT_ALLOW_PROTOCOL"], "https")
        self.assertEqual(captured["GIT_TERMINAL_PROMPT"], "0")

    def test_only_missing_git_uses_the_archive_fallback(self):
        downloader = Mock()
        with patch.object(app_update, "_prepare_git_source",
                          side_effect=app_update.ApplicationUpdateError("clone integrity failed")), \
                patch.object(app_update, "_download_source_archive", downloader):
            with self.assertRaisesRegex(app_update.ApplicationUpdateError, "clone integrity failed"):
                app_update.apply_source_update(self.install, self.candidate, state_base=self.state)
        downloader.assert_not_called()

    def test_missing_git_uses_an_immutable_archive_for_the_verified_commit(self):
        def make_archive(_candidate, archive, **_kwargs):
            with zipfile.ZipFile(archive, "w") as bundle:
                # GitHub's real archives list the wrapper folder first, then each directory.
                bundle.writestr("MCU-Flasher-fixture/", b"")
                bundle.writestr("MCU-Flasher-fixture/src/", b"")
                for relative in self._source_names():
                    bundle.writestr("MCU-Flasher-fixture/" + relative,
                                    (self.source / relative).read_bytes())

        with patch.object(app_update, "_prepare_git_source", side_effect=app_update._GitUnavailable("no git")), \
                patch.object(app_update, "_download_source_archive", side_effect=make_archive):
            result = app_update.apply_source_update(self.install, self.candidate, state_base=self.state)
        self.assertEqual(result.strategy, "github-source-archive")
        self._assert_required_marker(self.install, "staged")

    def test_commit_check_is_current_without_rewriting_unchanged_state(self):
        state_file = app_update.update_state_path(self.install, state_base=self.state)
        state_file.parent.mkdir(parents=True)
        state_file.write_text(json.dumps({
            "schema": app_update.STATE_SCHEMA,
            "repository": app_update.REPOSITORY,
            "branch": "main",
            "latest_commit": self.candidate.commit,
            "applied_commit": self.candidate.commit,
            "applied_at": "fixture",
            "managed_files": [],
        }), encoding="utf-8")
        response = _Response(
            json.dumps({"sha": self.candidate.commit}).encode("utf-8"),
            "https://api.github.com/repos/YAKITORIHOSHI/MCU-Flasher-byNaph/commits/main",
        )
        with patch.object(app_update, "_write_json_atomically") as writer:
            result = app_update.check_for_update(
                self.install, state_base=self.state, opener=lambda *_args, **_kwargs: response,
            )
        self.assertEqual(result.status, "current")
        writer.assert_not_called()

    def bundled_check(self, *, bundled="1" * 40, remote="2" * 40, comparison="behind", state=None,
                      remote_date="", installed_date="", record_extra=None):
        """Check a copy's launch with GitHub's responses fabricated. ``comparison`` may be per-base."""
        revision = self.install / app_update.BUNDLED_REVISION_FILE
        revision.parent.mkdir(parents=True, exist_ok=True)
        revision.unlink(missing_ok=True)
        if bundled is not None:
            revision.write_text(json.dumps({"commit": bundled, **(record_extra or {})}), encoding="utf-8")
        if state is not None:
            path = app_update.update_state_path(self.install, state_base=self.state)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state), encoding="utf-8")
        requested = []

        def opener(request, **_kwargs):
            requested.append(request.full_url)
            if "/commits/" in request.full_url:
                body = {"sha": remote, "commit": {"committer": {"date": remote_date}}}
                return _Response(json.dumps(body).encode(), "https://api.github.com/commits")
            base = request.full_url.split("/compare/", 1)[1].split("...", 1)[0]
            answer = comparison[base] if isinstance(comparison, dict) else comparison
            if isinstance(answer, Exception):
                raise answer
            body = {"status": answer, "base_commit": {"commit": {"committer": {"date": installed_date}}}}
            return _Response(json.dumps(body).encode(), "https://api.github.com/compare")
        return app_update.check_for_update(self.install, state_base=self.state, opener=opener), requested

    def test_release_zip_is_never_replaced_by_an_older_update_branch(self):
        checked, requested = self.bundled_check(comparison="behind")
        self.assertEqual(checked.status, "current")
        self.assertIn(f"/compare/{'1' * 40}...{'2' * 40}?page=2&per_page=1", requested[1])

    def test_release_zip_on_the_branch_commit_needs_no_second_request_or_replacement(self):
        checked, requested = self.bundled_check(remote="1" * 40)
        self.assertEqual(checked.status, "current")
        self.assertEqual(len(requested), 1)

    def test_a_copy_follows_a_branch_that_is_newer(self):
        checked, _ = self.bundled_check(comparison="ahead")
        self.assertEqual(checked.status, "available")
        self.assertEqual(checked.candidate.commit, "2" * 40)

    def test_a_rewritten_branch_is_followed_only_when_it_is_the_later_work(self):
        later, earlier = "2026-11-06T00:00:00Z", "2026-10-23T00:00:00Z"
        for remote_date, installed_date, expected in ((later, earlier, "available"), (earlier, later, "current"),
                                                      ("", later, "current"), (later, "", "current")):
            with self.subTest(remote=remote_date, installed=installed_date):
                checked, _ = self.bundled_check(comparison="diverged", remote_date=remote_date,
                                                installed_date=installed_date)
                self.assertEqual(checked.status, expected)

    def test_a_copy_is_left_alone_when_the_comparison_cannot_be_trusted(self):
        for comparison in (OSError("offline"), "unknown-status"):
            with self.subTest(comparison=comparison):
                checked, _ = self.bundled_check(comparison=comparison)
                self.assertEqual(checked.status, "unavailable")

    def test_a_record_github_never_saw_is_judged_by_its_own_date(self):
        # What happened: a hand-made v11 ZIP recorded an amended commit GitHub never received.
        # Every comparison answered 404, so those copies silently stopped updating for good.
        from urllib.error import HTTPError
        unknown = HTTPError("https://api.github.com/compare", 404, "Not Found", {}, None)
        later, earlier = "2026-11-11T00:00:00Z", "2026-11-09T02:23:07Z"
        for key in ("committed_at", "timestamp"):
            with self.subTest(record_key=key):
                checked, requested = self.bundled_check(comparison=unknown, remote_date=later,
                                                        record_extra={key: earlier})
                self.assertEqual(checked.status, "available")
                self.assertEqual(len(requested), 2)
        checked, _ = self.bundled_check(comparison=unknown, remote_date=earlier,
                                        record_extra={"committed_at": later})
        self.assertEqual(checked.status, "current")
        # Without both dates nothing says the branch is later work.
        for remote_date, extra in ((later, {}), ("", {"committed_at": earlier}),
                                   (later, {"committed_at": "yesterday"})):
            with self.subTest(remote=remote_date, record=extra):
                checked, _ = self.bundled_check(comparison=unknown, remote_date=remote_date, record_extra=extra)
                self.assertEqual(checked.status, "unavailable")
        # Any other refusal still means no update.
        refused = HTTPError("https://api.github.com/compare", 403, "rate limited", {}, None)
        checked, _ = self.bundled_check(comparison=refused, remote_date=later, record_extra={"committed_at": earlier})
        self.assertEqual(checked.status, "unavailable")

    def test_an_applied_update_records_its_commit_date_for_later_checks(self):
        dated = app_update.SourceCandidate(commit=self.candidate.commit, branch="main",
                                           committed_at="2026-11-11T01:02:03Z")
        app_update.install_staged_source(self.install, self.source, self._source_names(), dated,
                                         strategy="fixture", state_base=self.state)
        record = json.loads((self.install / app_update.BUNDLED_REVISION_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record, {"repository": app_update.REPOSITORY, "commit": dated.commit,
                                  "committed_at": "2026-11-11T01:02:03Z"})
        state = json.loads(app_update.update_state_path(self.install, state_base=self.state).read_text(encoding="utf-8"))
        self.assertEqual(state["applied_committed_at"], "2026-11-11T01:02:03Z")

    def test_a_copy_with_no_record_is_never_replaced(self):
        # What happened: a ZIP packaged without its record went back to a two-week-old main on
        # every install, because a copy that could not say what it held was assumed to be older.
        checked, requested = self.bundled_check(bundled=None, comparison=AssertionError("Not asked"))
        self.assertEqual(checked.status, "unidentified")
        self.assertEqual(len(requested), 1)
        for malformed in ("not-a-commit", "A" * 40, "1" * 39):
            with self.subTest(record=malformed):
                checked, _ = self.bundled_check(bundled=malformed, comparison=AssertionError("Not asked"))
                self.assertEqual(checked.status, "unidentified")

    def test_a_newer_release_extracted_over_an_updated_folder_is_kept(self):
        # The per-user state outlives the folder's contents, and used to outrank the new release.
        old_state = {"schema": app_update.STATE_SCHEMA, "repository": app_update.REPOSITORY,
                     "branch": app_update.DEFAULT_BRANCH, "applied_commit": "3" * 40}
        checked, _ = self.bundled_check(bundled="1" * 40, remote="2" * 40, state=old_state,
                                        comparison={"1" * 40: "behind", "3" * 40: "ahead"})
        self.assertEqual(checked.status, "current")

    def test_an_update_applied_by_an_older_version_still_counts(self):
        # The release's own record is older than what that update installed.
        old_state = {"schema": app_update.STATE_SCHEMA, "repository": app_update.REPOSITORY,
                     "branch": app_update.DEFAULT_BRANCH, "applied_commit": "3" * 40}
        checked, _ = self.bundled_check(bundled="1" * 40, remote="2" * 40, state=old_state,
                                        comparison={"1" * 40: "ahead", "3" * 40: "behind"})
        self.assertEqual(checked.status, "current")
        checked, _ = self.bundled_check(bundled="1" * 40, remote="3" * 40, state=old_state,
                                        comparison=AssertionError("Not asked"))
        self.assertEqual(checked.status, "current")

    def test_state_alone_still_identifies_a_copy_an_older_version_updated(self):
        old_state = {"schema": 1, "repository": app_update.REPOSITORY, "branch": "main", "applied_commit": "3" * 40}
        checked, _ = self.bundled_check(bundled=None, remote="2" * 40, state=old_state, comparison="ahead")
        self.assertEqual(checked.status, "available")
        checked, _ = self.bundled_check(bundled=None, remote="2" * 40, state=old_state, comparison="behind")
        self.assertEqual(checked.status, "current")

    def test_every_applied_update_records_itself_in_the_copy(self):
        record = self.install / app_update.BUNDLED_REVISION_FILE
        record.write_text(json.dumps({"commit": "1" * 40}), encoding="utf-8")
        app_update.install_staged_source(self.install, self.source, self._source_names(), self.candidate,
                                         strategy="fixture", state_base=self.state)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["commit"], self.candidate.commit)
        # So the next launch knows without asking, even from a moved folder with no state.
        moved_state = self.fixture / "another-user"
        response = _Response(json.dumps({"sha": self.candidate.commit}).encode("utf-8"),
                             "https://api.github.com/repos/YAKITORIHOSHI/MCU-Flasher-byNaph/commits/main")
        checked = app_update.check_for_update(self.install, state_base=moved_state,
                                              opener=lambda *_args, **_kwargs: response)
        self.assertEqual(checked.status, "current")

    def test_a_failed_update_leaves_the_record_as_it_was(self):
        record = self.install / app_update.BUNDLED_REVISION_FILE
        record.write_text(json.dumps({"commit": "1" * 40}), encoding="utf-8")
        real_write = app_update._write_json_atomically

        def refuse_state(path, value):
            if path.name == "update-state.json":
                raise app_update.ApplicationUpdateError("no state")
            real_write(path, value)

        with patch.object(app_update, "_write_json_atomically", side_effect=refuse_state), \
                self.assertRaisesRegex(app_update.ApplicationUpdateError, "no state"):
            app_update.install_staged_source(self.install, self.source, self._source_names(), self.candidate,
                                             strategy="fixture", state_base=self.state)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["commit"], "1" * 40)
        self._assert_required_marker(self.install, "installed")

    def test_a_checkout_cannot_overwrite_this_copys_record(self):
        self._file(self.source, app_update.BUNDLED_REVISION_FILE, b'{"commit": "placeholder"}')
        app_update.install_staged_source(
            self.install, self.source, self._source_names(extra=(app_update.BUNDLED_REVISION_FILE,)), self.candidate,
            strategy="fixture", state_base=self.state)
        record = json.loads((self.install / app_update.BUNDLED_REVISION_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record["commit"], self.candidate.commit)

    def test_metadata_deadline_rejects_a_trickled_response(self):
        clock = [0.0]
        response = _SlowResponse(
            json.dumps({"sha": self.candidate.commit}).encode("utf-8"),
            "https://api.github.com/repos/YAKITORIHOSHI/MCU-Flasher-byNaph/commits/main",
            clock=clock, elapsed=0.75,
        )
        with patch.object(app_update.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(app_update.ApplicationUpdateError, "timed out"):
                app_update._read_remote_commit(branch="main", timeout=0.5,
                                               opener=lambda *_args, **_kwargs: response)

    def test_archive_deadline_rejects_a_trickled_response(self):
        clock = [0.0]
        response = _SlowResponse(
            b"partial archive",
            "https://codeload.github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/zip/" + self.candidate.commit,
            clock=clock, elapsed=0.75,
        )
        with patch.object(app_update, "ARCHIVE_TIMEOUT_SECONDS", 0.5), \
                patch.object(app_update.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(app_update.ApplicationUpdateError, "timed out"):
                app_update._download_source_archive(
                    self.candidate, self.fixture / "slow.zip", report=lambda _message: None,
                    opener=lambda *_args, **_kwargs: response,
                )

    def test_working_copy_is_left_to_source_control(self):
        (self.install / ".git").mkdir()
        result = app_update.check_for_update(self.install, state_base=self.state)
        self.assertEqual(result.status, "skipped")
        self.assertIn("working copy", result.detail)

    def test_archive_rejects_path_escape(self):
        archive = self.fixture / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("MCU-Flasher-fixture/../../outside.py", b"unsafe")
        with self.assertRaisesRegex(app_update.ApplicationUpdateError, "unsafe path"):
            app_update._extract_source_archive(archive, self.fixture / "extract")

    def test_windows_reserved_archive_filename_is_rejected_on_every_host(self):
        with self.assertRaisesRegex(app_update.ApplicationUpdateError, "unsupported path"):
            app_update._safe_relative("src/CON.py")

    def test_archive_still_rejects_a_file_beside_the_wrapper_and_a_second_root(self):
        loose = self.fixture / "loose.zip"
        with zipfile.ZipFile(loose, "w") as bundle:
            bundle.writestr("MCU-Flasher-fixture/", b"")
            bundle.writestr("loose.py", b"unsafe")
        with self.assertRaisesRegex(app_update.ApplicationUpdateError, "invalid layout"):
            app_update._extract_source_archive(loose, self.fixture / "extract-loose")
        second = self.fixture / "second.zip"
        with zipfile.ZipFile(second, "w") as bundle:
            bundle.writestr("MCU-Flasher-fixture/", b"")
            bundle.writestr("another-root/", b"")
        with self.assertRaisesRegex(app_update.ApplicationUpdateError, "multiple roots"):
            app_update._extract_source_archive(second, self.fixture / "extract-second")

    def test_startup_budget_covers_both_requests_and_archive_survives_a_brief_stall(self):
        self.assertGreaterEqual(app_update.STARTUP_CHECK_BUDGET_SECONDS, 2 * app_update.STARTUP_CHECK_TIMEOUT_SECONDS)
        self.assertGreater(app_update.METADATA_IO_SLICE_SECONDS, 1.0)
        deadline = app_update.time.monotonic() + 600
        self.assertEqual(app_update._remaining_network_timeout(deadline, "download", app_update.ARCHIVE_IO_SLICE_SECONDS),
                         app_update.ARCHIVE_IO_SLICE_SECONDS)

    def _sharing_violation(self, winerror):
        failure = PermissionError(13, "in use")
        failure.winerror = winerror
        return failure

    def test_brief_windows_sharing_violation_is_waited_out(self):
        source, destination = self.fixture / "new.txt", self.fixture / "target.txt"
        source.write_text("new", encoding="utf-8")
        real_replace = os.replace
        attempts = []

        def busy_once(first, second):
            attempts.append(first)
            if len(attempts) == 1:
                raise self._sharing_violation(32)
            real_replace(first, second)

        with patch.object(app_update.os, "replace", busy_once), patch.object(app_update.time, "sleep"):
            app_update._copy_file_atomically(source, destination)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(destination.read_text(encoding="utf-8"), "new")

    def test_other_and_persistent_replace_failures_stop(self):
        source, destination = self.fixture / "new.txt", self.fixture / "target.txt"
        source.write_text("new", encoding="utf-8")
        for failure, expected in ((PermissionError(13, "denied"), 1),
                                  (self._sharing_violation(32), app_update.REPLACE_ATTEMPTS)):
            replace = Mock(side_effect=failure)
            with patch.object(app_update.os, "replace", replace), patch.object(app_update.time, "sleep"), \
                    self.assertRaisesRegex(app_update.ApplicationUpdateError, "could not be installed"):
                app_update._copy_file_atomically(source, destination)
            self.assertEqual(replace.call_count, expected)
        self.assertFalse(destination.exists())


class BootstrapRestartChecks(unittest.TestCase):
    """Run the real restart function against a real child in a folder with spaces."""

    def setUp(self):
        temp_root = ROOT / "temp" / "audit" / "app-update"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.fixture = Path(tempfile.mkdtemp(prefix="restart ", dir=temp_root))
        self.addCleanup(shutil.rmtree, self.fixture, True)
        self.root = self.fixture / "Install Folder With Spaces"
        (self.root / "src" / "modules").mkdir(parents=True)
        self.health = self.fixture / "health.json"
        self.health.write_text("{}", encoding="utf-8")
        self.received = self.fixture / "received.json"
        self.log = []

    HANDOFF_NAMES = ("_write_bootstrap_slot_owner", "_adopt_bootstrap_slot_handoff")

    @staticmethod
    def _segments(names):
        source = (ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig")
        found = {}
        for node in ast.parse(source).body:
            name = getattr(node, "name", None)
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
            if name in names:
                found[name] = ast.get_source_segment(source, node)
        return "\n\n".join(found[name] for name in names)

    def _restart(self, child_source, argv):
        (self.root / "src" / "modules" / "bootstrap.py").write_text(child_source, encoding="utf-8")
        self.lock = self.fixture / "launcher.lock"
        self.lock.write_text(str(os.getpid()), encoding="utf-8")
        namespace = {"STARTUP_HEALTH_FILE": self.health, "SCRIPT_DIR": self.root, "sys": SimpleNamespace(
            argv=argv, executable=sys.executable, exit=sys.exit), "subprocess": subprocess, "os": os,
            "_BOOTSTRAP_LOCK_FILE": self.lock,
            "_record_bootstrap_log": lambda level, message: self.log.append(message)}
        code = self._segments(self.HANDOFF_NAMES[:1] + ("_restart_after_application_update",))
        exec(compile(code, "bootstrap.py", "exec"), namespace)
        return namespace["_restart_after_application_update"]

    def _adopting_child(self, then=""):
        """A child running the real handoff code, as the restarted Bootstrap's entry does."""
        return ("import json, os, sys\nfrom pathlib import Path\n"
                f"_BOOTSTRAP_LOCK_FILE = Path({str(self.fixture / 'launcher.lock')!r})\n"
                + self._segments(self.HANDOFF_NAMES) + "\n"
                "adopted = _adopt_bootstrap_slot_handoff()\n"
                f"json.dump({{'adopted': adopted, 'pid': os.getpid()}}, open({str(self.received)!r}, 'w'))\n"
                + then)

    def test_restarted_bootstrap_adopts_the_launcher_lock_instead_of_reporting_already_starting(self):
        # The updating process still holds the lock: the restarted one used to
        # see a live owner, say "already starting up" and exit, so nothing opened.
        # The parent passes only the resume flag, as older installed copies do.
        restart = self._restart(self._adopting_child(), ["launcher.py"])
        with self.assertRaises(SystemExit) as finished:
            restart()
        self.assertEqual(finished.exception.code, 0)
        received = json.loads(self.received.read_text(encoding="utf-8"))
        self.assertTrue(received["adopted"])
        self.assertEqual(self.lock.read_text(encoding="utf-8"), str(received["pid"]))

    def test_child_that_exits_early_returns_the_lock_to_the_continuing_bootstrap(self):
        self._restart(self._adopting_child("raise SystemExit(3)\n"), ["launcher.py"])()
        self.assertTrue(json.loads(self.received.read_text(encoding="utf-8"))["adopted"])
        self.assertEqual(self.lock.read_text(encoding="utf-8"), str(os.getpid()))

    def test_handoff_needs_the_resume_flag(self):
        source = self._segments(self.HANDOFF_NAMES)
        lock = self.fixture / "launcher.lock"
        lock.write_text(str(os.getppid()), encoding="utf-8")
        namespace = {"os": os, "sys": SimpleNamespace(argv=["bootstrap.py"]), "_BOOTSTRAP_LOCK_FILE": lock}
        exec(compile(source, "bootstrap.py", "exec"), namespace)
        self.assertFalse(namespace["_adopt_bootstrap_slot_handoff"]())
        self.assertEqual(lock.read_text(encoding="utf-8"), str(os.getppid()))

    def test_handoff_never_takes_a_lock_held_by_another_process(self):
        restart = self._restart(self._adopting_child("raise SystemExit(3)\n"), ["launcher.py"])
        self.lock.write_text("424242", encoding="utf-8")
        restart()
        self.assertFalse(json.loads(self.received.read_text(encoding="utf-8"))["adopted"])
        self.assertEqual(self.lock.read_text(encoding="utf-8"), "424242")

    def test_restart_keeps_spaced_paths_whole_and_leaves_once_the_child_runs(self):
        child = ("import json, sys\n"
                 f"json.dump(sys.argv, open({str(self.received)!r}, 'w'))\n")
        restart = self._restart(child, ["launcher.py", "--hidden", "--project", "C:\\My Sketch"])
        with self.assertRaises(SystemExit) as finished:
            restart()
        self.assertEqual(finished.exception.code, 0)
        arguments = json.loads(self.received.read_text(encoding="utf-8"))
        self.assertEqual(Path(arguments[0]), self.root / "src" / "modules" / "bootstrap.py")
        self.assertEqual(arguments[1:], ["--resume-application-update", "--hidden", "--project", "C:\\My Sketch"])
        self.assertFalse(self.health.exists(), "the restarted Bootstrap must recheck readiness")

    def test_failed_child_returns_so_the_installed_application_still_opens(self):
        self._restart("raise SystemExit(3)\n", ["launcher.py"])()
        self.assertIn("exited early (3)", self.log[-1])

    def test_unstartable_child_returns_with_a_recorded_reason(self):
        restart = self._restart("", ["launcher.py"])
        with patch.object(subprocess, "Popen", side_effect=OSError("no interpreter")):
            restart()
        self.assertIn("no interpreter", self.log[-1])



class CloneProgressChecks(unittest.TestCase):
    """A slow clone reports what git is doing, and never looks frozen or runs backwards."""

    def setUp(self):
        audit = ROOT / "temp" / "audit" / "app-update"
        audit.mkdir(parents=True, exist_ok=True)
        self.fixture = Path(tempfile.mkdtemp(prefix="clone-progress-", dir=audit))
        self.addCleanup(shutil.rmtree, self.fixture, True)

    def _fake_git(self, body: str) -> str:
        program = self.fixture / "fake_git.py"
        program.write_text(f"import sys, time\n{body}\n", encoding="utf-8")
        # A shebang cannot hold the space this project's own path may contain.
        script = self.fixture / "git"
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{program}" "$@"\n', encoding="utf-8")
        script.chmod(0o755)
        return str(script)

    def test_git_records_become_one_rising_bar(self):
        parse = app_update.parse_git_progress
        self.assertEqual(parse("remote: Counting objects:  50% (500/1000)"),
                         ("GitHub is preparing the source", None, ""))
        phase, percent, detail = parse("Receiving objects:  40% (400/1000), 6.40 MiB | 1.10 MiB/s")
        self.assertEqual((phase, round(percent)), ("Downloading", 34))
        self.assertEqual(detail, "6.40 MiB | 1.10 MiB/s")
        self.assertEqual(round(parse("Resolving deltas: 100% (52/52), done.")[1]), 93)
        self.assertEqual(parse("Updating files: 100% (964/964), done.")[1], 100.0)
        self.assertIsNone(parse("Cloning into 'repository'..."))
        self.assertEqual(app_update.progress_bar(50, 10, ascii_only=True), "[#####-----]")
        self.assertEqual(app_update.progress_bar(None, 4, ascii_only=True), "[----]")
        text = app_update.TransferProgress("Receiving objects", 42.4, "3.20 MiB | 1.10 MiB/s", 75).describe()
        self.assertEqual(text, "Receiving objects · 42% · 3.20 MiB | 1.10 MiB/s · 1:15 elapsed")

    @unittest.skipIf(os.name == "nt", "the fake git is a POSIX script")
    def test_clone_streams_progress_and_heartbeats_while_github_is_silent(self):
        git = self._fake_git(
            "w = sys.stderr.write\n"
            "w(\"Cloning into 'repository'...\\n\"); sys.stderr.flush(); time.sleep(1.4)\n"
            "w('remote: Enumerating objects: 1000, done.\\n')\n"
            "for n in (10, 55, 100):\n"
            "    w(f'Receiving objects: {n:3d}% ({n*10}/1000), {n/10:.2f} MiB | 1.00 MiB/s\\r'); sys.stderr.flush(); time.sleep(0.3)\n"
            "w('Receiving objects: 100% (1000/1000), 10.00 MiB | 1.00 MiB/s, done.\\n')\n"
            "w('Resolving deltas: 100% (52/52), done.\\n'); w('Updating files: 100% (964/964), done.\\n')\n"
        )
        updates = []
        with patch.object(app_update, "_git_executable", return_value=git):
            app_update._clone_with_progress(["clone"], cwd=self.fixture, timeout=60,
                                            progress=updates.append, cancelled=None)
        self.assertEqual(updates[0].phase, "Connecting to GitHub")
        # GitHub said nothing for over a second, yet the elapsed time kept moving.
        quiet = [update for update in updates if update.percent is None]
        self.assertGreaterEqual(len(quiet), 2)
        self.assertGreater(quiet[-1].elapsed, quiet[0].elapsed)
        percents = [update.percent for update in updates if update.percent is not None]
        self.assertEqual(percents, sorted(percents))
        self.assertTrue(any(0 < value < 85 for value in percents))
        self.assertTrue(any("MiB/s" in update.detail for update in updates))
        self.assertEqual((updates[-1].phase, updates[-1].percent), ("Source downloaded", 100.0))

    @unittest.skipIf(os.name == "nt", "the fake git is a POSIX script")
    def test_failed_clone_reports_gits_own_reason(self):
        git = self._fake_git("sys.stderr.write('fatal: unable to access GitHub\\n'); sys.exit(128)")
        with patch.object(app_update, "_git_executable", return_value=git), \
                self.assertRaisesRegex(app_update.ApplicationUpdateError, "unable to access GitHub"):
            app_update._clone_with_progress(["clone"], cwd=self.fixture, timeout=60, progress=None, cancelled=None)

    @unittest.skipIf(os.name == "nt", "the fake git is a POSIX script")
    def test_a_dropped_connection_is_told_apart_from_a_repository_problem(self):
        unreachable = self._fake_git("sys.stderr.write(\"fatal: unable to access 'https://github.com/x.git/': "
                                     "Could not resolve host: github.com\\n\"); sys.exit(128)")
        with patch.object(app_update, "_git_executable", return_value=unreachable), \
                self.assertRaises(app_update._NetworkFailure):
            app_update._clone_with_progress(["clone"], cwd=self.fixture, timeout=60, progress=None, cancelled=None)
        local = self._fake_git("sys.stderr.write(\"fatal: could not create work tree dir '/home/russlee/x'\\n\"); sys.exit(128)")
        with patch.object(app_update, "_git_executable", return_value=local), \
                self.assertRaises(app_update.ApplicationUpdateError) as caught:
            app_update._clone_with_progress(["clone"], cwd=self.fixture, timeout=60, progress=None, cancelled=None)
        self.assertNotIsInstance(caught.exception, app_update._NetworkFailure)

    @unittest.skipIf(os.name == "nt", "the fake git is a POSIX script")
    def test_cancelled_clone_stops_git(self):
        marker = self.fixture / "still-running"
        git = self._fake_git(
            f"time.sleep(30); open({str(marker)!r}, 'w').close()"
        )
        calls = [0]

        def cancelled():
            calls[0] += 1
            return calls[0] > 3

        with patch.object(app_update, "_git_executable", return_value=git), \
                self.assertRaisesRegex(app_update.ApplicationUpdateError, "cancelled"):
            app_update._clone_with_progress(["clone"], cwd=self.fixture, timeout=60, progress=None,
                                            cancelled=cancelled)
        self.assertFalse(marker.exists())

    def test_ubuntu_terminal_shows_one_live_line(self):
        import io
        from direct.ubuntu.launch import TerminalProgress

        class Terminal(io.StringIO):
            encoding = "utf-8"

            def isatty(self):
                return True

        stream = Terminal()
        bar = TerminalProgress(stream, app_update)
        bar.update(app_update.TransferProgress("Receiving objects", 40.0, "4.00 MiB | 1.00 MiB/s", 5))
        bar.update(app_update.TransferProgress("Receiving objects", 80.0, "8.00 MiB | 1.00 MiB/s", 9))
        bar.finish()
        written = stream.getvalue()
        self.assertEqual(written.count("\r"), 2)
        self.assertTrue(written.endswith("\n"))
        self.assertIn("80%", written)
        self.assertIn("█", written)


@unittest.skipUnless(shutil.which("git"), "Git is not installed")
class IncrementalSourceChecks(unittest.TestCase):
    """A kept blobless checkout downloads only the files that changed, with real Git.

    A local bare repository stands in for GitHub with partial clone enabled, as
    GitHub has it. Nothing here reaches the network.
    """

    def setUp(self):
        audit = ROOT / "temp" / "audit" / "app-update"
        audit.mkdir(parents=True, exist_ok=True)
        self.fixture = Path(tempfile.mkdtemp(prefix="incremental-", dir=audit))
        self.addCleanup(app_update._remove_tree, self.fixture)
        work = self.fixture / "work"
        self.git(None, "init", "-q", "-b", "main", str(work))
        for relative in app_update.REQUIRED_SOURCE_FILES:
            (work / relative).parent.mkdir(parents=True, exist_ok=True)
            (work / relative).write_text(f"{relative} 1\n", encoding="utf-8")
        (work / "large.bin").write_bytes(os.urandom(256 * 1024))
        (work / "retired.py").write_text("old\n", encoding="utf-8")
        self.first = self.commit(work, "first")
        (work / "mcu_flash_gui.py").write_text("changed\n", encoding="utf-8")
        (work / "retired.py").unlink()
        self.second = self.commit(work, "second")
        self.server = self.fixture / "server.git"
        self.git(None, "clone", "-q", "--bare", str(work), str(self.server))
        self.git(self.server, "config", "uploadpack.allowFilter", "true")
        self.git(self.server, "config", "uploadpack.allowAnySHA1InWant", "true")
        environment = app_update._git_environment()

        def local_environment():
            value = dict(environment)
            value["GIT_ALLOW_PROTOCOL"] = "file"
            return value

        patcher = patch.multiple(app_update, REPOSITORY_URL=self.server.as_uri(),
                                 _git_environment=local_environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cache = self.fixture / "state" / app_update.SOURCE_CACHE_DIRECTORY
        self.cache.parent.mkdir(parents=True)

    @staticmethod
    def git(cwd, *arguments):
        command = ["git", *(["-C", str(cwd)] if cwd else []), "-c", "user.name=Fixture",
                   "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", *arguments]
        return subprocess.run(command, check=True, capture_output=True, text=True,
                              env=dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")).stdout

    def commit(self, work, message):
        self.git(work, "add", "-A")
        self.git(work, "commit", "-q", "-m", message)
        return self.git(work, "rev-parse", "HEAD").strip()

    def stage(self, name):
        stage = self.fixture / name
        stage.mkdir()
        return stage

    def prepare(self, stage, commit, installed=None):
        updates, messages = [], []
        checkout, names = app_update._prepare_git_source(
            stage, app_update.SourceCandidate(commit, "main"), messages.append, updates.append, None, self.cache,
            installed=installed)
        return checkout, names, updates, messages

    def installed_copy(self, commit, name="installed"):
        """An installation holding exactly ``commit``'s files, as an earlier update left it."""
        install = self.fixture / name
        install.mkdir()
        archive = subprocess.run(["git", "-C", str(self.fixture / "work"), "archive", commit],
                                 check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(install)], input=archive, check=True)
        return install

    def test_first_update_reuses_unchanged_installed_files_instead_of_downloading_them(self):
        install = self.installed_copy(self.first)
        checkout, names, updates, messages = self.prepare(self.stage("one"), self.second, installed=install)
        self.assertEqual(checkout.name, "repository")
        # Six files (including the 256 KiB one) were already installed; one changed.
        self.assertIn("Reusing 5 unchanged files already installed.", messages)
        self.assertTrue(any(update.phase == "Downloading 1 changed file" for update in updates))
        self.assertEqual((checkout / "mcu_flash_gui.py").read_text(encoding="utf-8"), "changed\n")
        self.assertEqual((checkout / "large.bin").read_bytes(), (install / "large.bin").read_bytes())
        self.assertNotIn("retired.py", names)

    def test_a_locally_edited_installed_file_is_downloaded_not_reused(self):
        install = self.installed_copy(self.first)
        (install / "large.bin").write_bytes(b"edited locally")
        checkout, _names, updates, messages = self.prepare(self.stage("one"), self.second, installed=install)
        self.assertIn("Reusing 4 unchanged files already installed.", messages)
        self.assertTrue(any(update.phase == "Downloading 2 changed files" for update in updates))
        self.assertNotEqual((checkout / "large.bin").read_bytes(), b"edited locally")

    def test_installers_and_catalogs_are_never_downloaded_or_written(self):
        work = self.fixture / "work"
        (work / "installers").mkdir()
        (work / "installers/driver.exe").write_bytes(os.urandom(128 * 1024))
        (work / "index_json").mkdir()
        (work / "index_json/package_index.json").write_text("{}\n", encoding="utf-8")
        third = self.commit(work, "third")
        self.git(work, "push", "-q", str(self.server), "main")
        checkout, names, updates, _messages = self.prepare(self.stage("one"), third)
        # Git still lists them, so the installer knows to leave the local copies alone…
        self.assertIn("installers/driver.exe", names)
        # …but their contents never crossed the wire and nothing was written for them.
        self.assertFalse((checkout / "installers").exists())
        self.assertFalse((checkout / "index_json").exists())
        self.assertTrue(any(update.phase == "Downloading 6 files" for update in updates))
        blob = self.git(checkout, "rev-parse", f"{third}:installers/driver.exe").strip()
        missing = subprocess.run(["git", "-C", str(checkout), "cat-file", "-e", blob],
                                 capture_output=True, env=dict(os.environ, GIT_NO_LAZY_FETCH="1"))
        self.assertNotEqual(missing.returncode, 0)
        install = self.installed_copy(self.first, "install-protected")
        (install / "installers").mkdir()
        (install / "installers/driver.exe").write_bytes(b"local driver")
        result = app_update.install_staged_source(install, checkout, names, app_update.SourceCandidate(third, "main"),
                                                  strategy="git-clone", state_base=self.fixture / "user-state")
        self.assertGreater(result.preserved_files, 0)
        self.assertEqual((install / "installers/driver.exe").read_bytes(), b"local driver")
        self.assertEqual((install / "mcu_flash_gui.py").read_text(encoding="utf-8"), "changed\n")

    def test_a_second_launch_waits_for_the_first_update_instead_of_downloading_again(self):
        import threading
        install = self.installed_copy(self.first)
        state = self.fixture / "user-state"
        candidate = app_update.SourceCandidate(self.second, "main")
        lock_directory = app_update.update_state_path(install, state_base=state).parent
        holding, release, messages, results = threading.Event(), threading.Event(), [], []

        def first_launch():
            with app_update._exclusive_update(lock_directory, report=lambda _m: None, cancelled=None, timeout=30):
                holding.set()
                release.wait(10)
                # The first launch finishes installing this very revision.
                record = install / app_update.BUNDLED_REVISION_FILE
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text(json.dumps({"repository": app_update.REPOSITORY, "commit": self.second}),
                                  encoding="utf-8")

        first = threading.Thread(target=first_launch)
        first.start()
        self.assertTrue(holding.wait(10))
        second = threading.Thread(target=lambda: results.append(app_update.apply_source_update(
            install, candidate, state_base=state, report=messages.append)))
        with patch.object(app_update, "_prepare_git_source", side_effect=AssertionError("downloaded twice")):
            second.start()
            deadline = time.monotonic() + 10
            while "Another MCU Flasher launch is installing this update; waiting for it…" not in messages:
                self.assertLess(time.monotonic(), deadline, "the second launch did not wait")
                time.sleep(0.02)
            release.set()
            first.join(10)
            second.join(10)
        self.assertEqual(results[0].strategy, "already-installed")
        self.assertIn("This update was installed by another launch.", messages)

    def test_second_update_downloads_only_the_changed_file(self):
        checkout, names, updates, _messages = self.prepare(self.stage("one"), self.first)
        self.assertEqual(checkout.name, "repository")
        self.assertIn("retired.py", names)
        self.assertEqual((checkout / "mcu_flash_gui.py").read_text(encoding="utf-8"), "mcu_flash_gui.py 1\n")
        self.assertTrue(any(update.phase == "Downloading 7 files" for update in updates))
        app_update._keep_source_cache(checkout, self.cache)
        self.assertTrue((self.cache / ".git").is_dir())

        checkout, names, updates, messages = self.prepare(self.stage("two"), self.second)
        self.assertIn("only what changed", messages[0])
        self.assertFalse(self.cache.exists(), "the kept checkout is claimed while it is in use")
        self.assertEqual((checkout / "mcu_flash_gui.py").read_text(encoding="utf-8"), "changed\n")
        self.assertNotIn("retired.py", names)
        self.assertFalse((checkout / "retired.py").exists())
        # One changed file crossed the wire, not the 256 KiB that stayed the same.
        self.assertTrue(any(update.phase == "Downloading 1 changed file" for update in updates))
        percents = [update.percent for update in updates if update.percent is not None]
        self.assertEqual(percents, sorted(percents))
        self.assertEqual((updates[-1].phase, updates[-1].percent), ("Source downloaded", 100.0))

    def test_a_tampered_kept_file_is_written_again_from_git(self):
        checkout, _names, _updates, _messages = self.prepare(self.stage("one"), self.first)
        (checkout / "src/modules/bootstrap.py").write_text("tampered\n", encoding="utf-8")
        (checkout / "stray.py").write_text("stray\n", encoding="utf-8")
        app_update._keep_source_cache(checkout, self.cache)
        checkout, names, _updates, _messages = self.prepare(self.stage("two"), self.second)
        self.assertEqual((checkout / "src/modules/bootstrap.py").read_text(encoding="utf-8"),
                         "src/modules/bootstrap.py 1\n")
        self.assertFalse((checkout / "stray.py").exists())
        self.assertNotIn("stray.py", names)

    def test_a_foreign_kept_checkout_falls_back_to_a_full_clone(self):
        checkout, _names, _updates, _messages = self.prepare(self.stage("one"), self.first)
        self.git(checkout, "remote", "set-url", "origin", "https://example.invalid/other.git")
        app_update._keep_source_cache(checkout, self.cache)
        checkout, names, _updates, messages = self.prepare(self.stage("two"), self.second)
        self.assertEqual(checkout.name, "repository-full")
        self.assertTrue(any("cloning the full revision" in message for message in messages))
        self.assertEqual((checkout / "mcu_flash_gui.py").read_text(encoding="utf-8"), "changed\n")
        self.assertNotIn("retired.py", names)

    def test_a_dropped_connection_retries_the_small_download_not_a_full_clone(self):
        checkout, _names, _updates, _messages = self.prepare(self.stage("one"), self.first)
        app_update._keep_source_cache(checkout, self.cache)
        real = app_update._cached_git_source
        attempts = []

        def flaky(*arguments, **options):
            attempts.append(options["reused"])
            if len(attempts) == 1:
                raise app_update._NetworkFailure("The GitHub source update failed: Could not resolve host")
            return real(*arguments, **options)

        with patch.object(app_update, "_cached_git_source", side_effect=flaky), \
                patch.object(app_update, "NETWORK_RETRY_DELAY_SECONDS", 0.05):
            checkout, names, _updates, messages = self.prepare(self.stage("two"), self.second)
        self.assertEqual(attempts, [True, True])
        self.assertEqual(checkout.name, "repository")
        self.assertIn("GitHub could not be reached; trying again…", messages)
        self.assertFalse(any("full revision" in message for message in messages))
        self.assertNotIn("retired.py", names)

    def test_github_unreachable_leaves_the_update_and_keeps_the_download(self):
        checkout, _names, _updates, _messages = self.prepare(self.stage("one"), self.first)
        app_update._keep_source_cache(checkout, self.cache)
        failure = app_update._NetworkFailure("The GitHub source update failed: Could not resolve host")
        with patch.object(app_update, "_cached_git_source", side_effect=failure) as attempt, \
                patch.object(app_update, "_git_with_progress", side_effect=AssertionError("full clone")), \
                patch.object(app_update, "NETWORK_RETRY_DELAY_SECONDS", 0.05), \
                self.assertRaises(app_update._NetworkFailure):
            self.prepare(self.stage("two"), self.second)
        self.assertEqual(attempt.call_count, 2)
        # What was downloaded before stays for the next launch.
        self.assertTrue((self.cache / ".git").is_dir())

    def test_a_server_without_partial_clone_still_updates(self):
        self.git(self.server, "config", "uploadpack.allowFilter", "false")
        checkout, names, _updates, _messages = self.prepare(self.stage("one"), self.second)
        self.assertEqual((checkout / "mcu_flash_gui.py").read_text(encoding="utf-8"), "changed\n")
        self.assertIn("large.bin", names)

    def test_apply_keeps_a_verified_checkout_for_the_next_attempt(self):
        install = self.fixture / "install"
        install.mkdir()
        for relative in app_update.REQUIRED_SOURCE_FILES:
            (install / relative).parent.mkdir(parents=True, exist_ok=True)
            (install / relative).write_text("installed\n", encoding="utf-8")
        state = self.fixture / "user-state"
        candidate = app_update.SourceCandidate(self.second, "main")
        cache = app_update.update_state_path(install, state_base=state).parent / app_update.SOURCE_CACHE_DIRECTORY
        with patch.object(app_update, "install_staged_source",
                          side_effect=app_update.ApplicationUpdateError("install failed")):
            with self.assertRaisesRegex(app_update.ApplicationUpdateError, "install failed"):
                app_update.apply_source_update(install, candidate, state_base=state)
        # The download was verified, so the retry reuses it instead of downloading again.
        self.assertTrue((cache / ".git").is_dir())
        self.assertEqual((install / "mcu_flash_gui.py").read_text(encoding="utf-8"), "installed\n")
        messages = []
        result = app_update.apply_source_update(install, candidate, state_base=state, report=messages.append)
        self.assertIn("only what changed", messages[0])
        self.assertEqual(result.strategy, "git-clone")
        self.assertEqual((install / "mcu_flash_gui.py").read_text(encoding="utf-8"), "changed\n")
        self.assertTrue((cache / ".git").is_dir())
        self.assertEqual(list((cache.parent / "staging").iterdir()), [])

class BootstrapSourceUpdateWiringChecks(unittest.TestCase):
    def test_bootstrap_checks_before_fast_launch_and_invalidates_warm_health_after_refresh(self):
        source = (ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        main_source = ast.get_source_segment(source, main) or ""
        self.assertLess(main_source.index("_available_application_update()"), main_source.index("_try_fast_normal_launch()"))
        restart = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == "_restart_after_application_update")
        restart_source = ast.get_source_segment(source, restart) or ""
        self.assertIn("STARTUP_HEALTH_FILE.unlink", restart_source)
        self.assertIn("except OSError", restart_source)
        # The Windows exec family does not quote, so it breaks on folders with spaces.
        self.assertNotIn("os.exec", restart_source)
        install = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == "_install_application_update")
        install_source = ast.get_source_segment(source, install) or ""
        self.assertIn("completed.wait()", install_source)
        self.assertIn("apply_source_update", install_source)

    def test_bootstrap_window_shows_a_live_row_that_clears_before_the_next_message(self):
        import threading
        from src.modules import bootstrap
        calls = []

        class Window:
            TOTAL_STEPS = 9

            def __init__(self):
                self._closed = False
                self._step_index = 0
                self._done = threading.Event()
                self.root = SimpleNamespace(after=lambda _delay, callback: callback())

            def __getattr__(self, name):
                def record(*arguments, **_options):
                    calls.append((name, *arguments))
                    if name == "close_after_delay":
                        self._done.set()
                return record

            def mainloop_until_done(self):
                self._done.wait(10)

        def apply(_root, _candidate, *, report, progress, cancelled):
            self.assertFalse(cancelled())
            report("Downloading only what changed…")
            progress(app_update.TransferProgress("Downloading 2 changed files", 50.0, "1.00 MiB | 1.00 MiB/s", 3))
            report("GitHub source update verified. Restarting MCU Flasher…")
            return SimpleNamespace(candidate=SimpleNamespace(branch="main", commit="a" * 40),
                                   strategy="git-clone", changed_files=2, preserved_files=1)

        with patch.object(bootstrap, "BootstrapGUI", Window), patch.object(bootstrap, "_record_bootstrap_log"), \
                patch.object(bootstrap, "_update_check_skip_reason", return_value=None), \
                patch.object(app_update, "apply_source_update", side_effect=apply):
            self.assertTrue(bootstrap._install_application_update(SimpleNamespace(branch="main", commit="a" * 40)))
        names = [call[0] for call in calls]
        row = calls[names.index("update_platformio_progress_block")][1]
        self.assertIn("50%", row)
        self.assertIn("█", row)
        self.assertIn(("set_progress_percent", 50.0), calls)
        # The live row goes away before the next line is written under it.
        self.assertLess(names.index("clear_platformio_progress_block"),
                        calls.index(("log_status", "GitHub source update verified. Restarting MCU Flasher…")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
