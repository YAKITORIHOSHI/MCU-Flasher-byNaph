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
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from src.modules import app_update


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
                      remote_date="", installed_date=""):
        """Check a copy's launch with GitHub's responses fabricated. ``comparison`` may be per-base."""
        revision = self.install / app_update.BUNDLED_REVISION_FILE
        revision.parent.mkdir(parents=True, exist_ok=True)
        revision.unlink(missing_ok=True)
        if bundled is not None:
            revision.write_text(json.dumps({"commit": bundled}), encoding="utf-8")
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

    def _restart(self, child_source, argv):
        (self.root / "src" / "modules" / "bootstrap.py").write_text(child_source, encoding="utf-8")
        source = (ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig")
        function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)
                        and node.name == "_restart_after_application_update")
        namespace = {"STARTUP_HEALTH_FILE": self.health, "SCRIPT_DIR": self.root, "sys": SimpleNamespace(
            argv=argv, executable=sys.executable, exit=sys.exit), "subprocess": subprocess, "os": os,
            "_record_bootstrap_log": lambda level, message: self.log.append(message)}
        exec(compile(ast.get_source_segment(source, function), "bootstrap.py", "exec"), namespace)
        return namespace["_restart_after_application_update"]

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
