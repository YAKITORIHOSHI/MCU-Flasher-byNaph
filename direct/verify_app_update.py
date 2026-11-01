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
        install = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == "_install_application_update")
        install_source = ast.get_source_segment(source, install) or ""
        self.assertIn("completed.wait()", install_source)
        self.assertIn("apply_source_update", install_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
