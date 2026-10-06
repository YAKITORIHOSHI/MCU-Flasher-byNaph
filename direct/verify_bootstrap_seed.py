#!/usr/bin/env python3
"""Hardware-free seed fixtures; cleanup touches only temporary test archives."""
from __future__ import annotations

import ast
import errno
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.modules import bootstrap_seed as seed
from platformio.package.meta import PackageSpec


def bootstrap_scope(root):
    """Load selected real helpers without bootstrap import-time side effects."""
    source = (ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8")
    names = {"_platformio_core_is_populated", "_ensure_platformio_core_prebuilt",
             "_cleanup_platformio_prebuilt_archive",
             "_download_file", "_download_matches_expectations", "_normalize_sha256",
             "_file_sha256", "ensure_platformio_scons"}
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    logs = []
    scope = {"Path": Path, "sys": sys, "os": os, "time": time, "shutil": shutil,
             "re": __import__("re"), "_gui": None, "SCRIPT_DIR": root,
             "status": logs.append, "warn": logs.append, "ok": logs.append,
             "_record_bootstrap_exception": logs.append,
             "_record_bootstrap_log": lambda *args: logs.append(str(args)),
             "safe_replace_file": lambda source, destination: os.rename(source, destination) is None,
             "safe_unlink": Mock(side_effect=AssertionError("Seed attempted deletion")),
             "safe_rmtree": Mock(side_effect=AssertionError("Seed attempted recursive deletion")),
             "_is_network_reachable": Mock(side_effect=AssertionError("Unexpected external network probe")),
             "_PLATFORMIO_PREBUILT_ZIP_NAME": "seed.zip", "_PLATFORMIO_PREBUILT_EXPECTED_SIZE": 1,
             "_PLATFORMIO_PREBUILT_EXPECTED_SHA256": "", "_PLATFORMIO_PREBUILT_GITHUB_URL": "https://example.invalid/seed.zip",
             "_DOWNLOAD_STALL_TIMEOUT_SECONDS": 2}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<bootstrap-seed-helpers>", "exec"), scope)
    return scope, logs


def package_files(name, version="1.0.0", owner="vendor", *, metadata=True):
    files = {"package.json": json.dumps({"name": name, "version": version}), "payload.txt": "real fixture bytes"}
    if metadata:
        files[".piopm"] = json.dumps({"type": "tool", "name": name, "version": version,
                                     "spec": {"owner": owner, "name": name, "requirements": None, "uri": None}})
    if name == "tool-scons":
        files["scons.py"] = "# real fixture payload\n"
    return files


class SeedChecks(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "temp/audit/bootstrap-seed-fixtures"
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=parent))
        self.core = self.root / "core"
        self.core.mkdir()

    def archive(self, packages, extra=None):
        path = self.root / "release.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for directory, files in packages.items():
                for name, text in files.items():
                    archive.writestr(f".platformio-mcu-gui/packages/{directory}/{name}", text)
            for name, text in (extra or {}).items():
                archive.writestr(name, text)
        return path

    @staticmethod
    def windows_error(number):
        error = OSError(errno.EACCES, "Fixture Windows publication denial")
        error.winerror = number
        return error

    def publication_pair(self, suffix=""):
        source, destination = self.root / ("staged" + suffix), self.root / ("published" + suffix)
        source.mkdir()
        (source / "payload.txt").write_text("retained original payload", encoding="utf-8")
        return source, destination

    def test_publication_retries_only_transient_windows_errors_and_preserves_payload(self):
        real_rename = os.rename
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                source, destination = self.publication_pair(str(code))
                attempts = []
                def rename(source_path, destination_path):
                    attempts.append((source_path, destination_path))
                    if len(attempts) <= 2:
                        raise self.windows_error(code)
                    return real_rename(source_path, destination_path)
                messages = []
                with patch.object(seed, "sys_platform_is_windows", return_value=True), \
                        patch.object(seed.os, "rename", side_effect=rename), \
                        patch.object(seed.time, "sleep") as wait:
                    seed._publish_missing(source, destination, log=messages.append)
                self.assertEqual(len(attempts), 3)
                self.assertEqual([call.args[0] for call in wait.call_args_list], [.1, .2])
                self.assertFalse(source.exists())
                self.assertEqual((destination / "payload.txt").read_text(), "retained original payload")
                self.assertEqual(len(messages), 2)

    def test_publication_unknown_error_and_nonwindows_denial_are_not_retried(self):
        for suffix, windows, code in (("unknown", True, 87), ("native", False, 5)):
            with self.subTest(case=suffix):
                source, destination = self.publication_pair(suffix)
                error = self.windows_error(code)
                with patch.object(seed, "sys_platform_is_windows", return_value=windows), \
                        patch.object(seed.os, "rename", side_effect=error) as rename, \
                        patch.object(seed.time, "sleep") as wait:
                    with self.assertRaises(OSError) as failure:
                        seed._publish_missing(source, destination, log=lambda *_: None)
                self.assertIs(failure.exception, error)
                rename.assert_called_once()
                wait.assert_not_called()
                self.assertTrue((source / "payload.txt").is_file())
                self.assertFalse(destination.exists())

    def test_publication_destination_appearing_after_denial_is_never_overwritten(self):
        source, destination = self.publication_pair()
        def collision(*args):
            destination.mkdir()
            (destination / "other.txt").write_text("another publisher's package")
            raise self.windows_error(5)
        with patch.object(seed, "sys_platform_is_windows", return_value=True), \
                patch.object(seed.os, "rename", side_effect=collision) as rename, \
                patch.object(seed.time, "sleep") as wait:
            with self.assertRaisesRegex(RuntimeError, "destination appeared.*retained"):
                seed._publish_missing(source, destination, log=lambda *_: None)
        rename.assert_called_once()
        wait.assert_not_called()
        self.assertEqual((source / "payload.txt").read_text(), "retained original payload")
        self.assertEqual((destination / "other.txt").read_text(), "another publisher's package")
        self.assertFalse((destination / "payload.txt").exists())

    def test_publication_destination_appearing_during_backoff_stops_before_next_rename(self):
        source, destination = self.publication_pair()
        def during_wait(_seconds):
            destination.mkdir()
            (destination / "other.txt").write_text("retained competing payload")
        with patch.object(seed, "sys_platform_is_windows", return_value=True), \
                patch.object(seed.os, "rename", side_effect=self.windows_error(32)) as rename, \
                patch.object(seed.time, "sleep", side_effect=during_wait):
            with self.assertRaisesRegex(RuntimeError, "destination appeared.*retained"):
                seed._publish_missing(source, destination, log=lambda *_: None)
        rename.assert_called_once()
        self.assertTrue((source / "payload.txt").is_file())
        self.assertEqual((destination / "other.txt").read_text(), "retained competing payload")

    def test_publication_refuses_dangling_destination_entry_before_any_rename(self):
        source, destination = self.publication_pair()
        target = self.root / "nonexistent-target"
        if sys.platform == "win32":
            import _winapi
            target.mkdir()
            _winapi.CreateJunction(str(target), str(destination))
            # Keep the newly created empty target under this fixture while
            # making its original junction spelling dangling; delete nothing.
            os.rename(target, self.root / "retained-target")
        else:
            destination.symlink_to(target, target_is_directory=True)
        self.assertFalse(destination.exists())
        self.assertTrue(os.path.lexists(destination))
        with patch.object(seed.os, "rename") as rename, patch.object(seed.time, "sleep") as wait:
            with self.assertRaisesRegex(RuntimeError, "destination appeared.*retained"):
                seed._publish_missing(source, destination, log=lambda *_: None)
        rename.assert_not_called()
        wait.assert_not_called()
        self.assertTrue(os.path.lexists(destination))
        self.assertTrue((source / "payload.txt").is_file())

    def test_import_publication_exhaustion_retains_archive_and_complete_staging(self):
        archive = self.archive({"blocked": package_files("blocked")})
        original_archive = archive.read_bytes()
        error = self.windows_error(5)
        with patch.object(seed, "sys_platform_is_windows", return_value=True), \
                patch.object(seed.os, "rename", side_effect=error) as rename, \
                patch.object(seed.time, "sleep") as wait:
            with self.assertRaisesRegex(RuntimeError, "archive and staging retained") as failure:
                seed.import_missing(archive, self.core, {"packages": [PackageSpec("vendor/blocked@1.0.0")]},
                                    staging_parent=self.root / "retained", log=lambda *_: None)
        self.assertIs(failure.exception.__cause__, error)
        self.assertEqual(rename.call_count, 8)
        self.assertEqual(wait.call_count, 7)
        self.assertAlmostEqual(sum(call.args[0] for call in wait.call_args_list), 6.0)
        self.assertEqual(archive.read_bytes(), original_archive)
        stage = next((self.root / "retained").iterdir())
        self.assertEqual((stage / "packages/blocked/payload.txt").read_text(), package_files("blocked")["payload.txt"])
        self.assertTrue((stage / "packages/blocked/.piopm").is_file())
        self.assertFalse((self.core / "packages/blocked").exists())

    @unittest.skipUnless(sys.platform == "win32", "Requires a real Windows sharing handle")
    def test_real_windows_child_handle_lock_publication_resumes_after_release(self):
        import ctypes
        from ctypes import wintypes
        from src.modules.bootstrap_platformio import extended_windows_path
        source, destination = self.publication_pair()
        dll = ctypes.WinDLL("kernel32", use_last_error=True)
        dll.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        dll.CreateFileW.restype = wintypes.HANDLE
        dll.CloseHandle.argtypes = [wintypes.HANDLE]
        # This is a newly created fixture file. Denying FILE_SHARE_DELETE
        # reproduces the parent's real WinError 5 directory publication case.
        handle = dll.CreateFileW(extended_windows_path(source / "payload.txt"), 0x80000000,
                                3, None, 3, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        closed = threading.Event()
        def release():
            time.sleep(.15)
            dll.CloseHandle(handle)
            closed.set()
        thread = threading.Thread(target=release)
        messages = []
        thread.start()
        try:
            seed._publish_missing(source, destination, log=messages.append)
        finally:
            thread.join(2)
            if not closed.is_set():
                dll.CloseHandle(handle)
        self.assertTrue(closed.is_set())
        self.assertTrue(messages, "The real held child file must deny at least one directory rename")
        self.assertFalse(source.exists())
        self.assertEqual((destination / "payload.txt").read_text(), "retained original payload")
        (self.root / "native-publication-retry.json").write_text(json.dumps({
            "source": str(source), "destination": str(destination),
            "retry_messages": messages, "file_handle_released": closed.is_set(),
            "payload_preserved": True,
        }, indent=2), encoding="utf-8")

    def test_missing_import_keeps_existing_payload_and_original_metadata(self):
        existing = self.core / "packages/kept"
        existing.mkdir(parents=True)
        for name, text in package_files("kept").items():
            (existing / name).write_text(text)
        before = {path.name: path.read_bytes() for path in existing.iterdir()}
        files = package_files("missing")
        archive = self.archive({"kept": package_files("kept", "2.0.0"), "missing": files})
        result = seed.import_missing(archive, self.core, {"packages": [PackageSpec("vendor/missing@1.0.0")], "platforms": [], "lib": []},
                                     staging_parent=self.root / "retained")
        self.assertEqual(len(result["imported"]), 1)
        self.assertEqual(before, {path.name: path.read_bytes() for path in existing.iterdir()})
        self.assertEqual((self.core / "packages/missing/.piopm").read_text(), files[".piopm"])
        self.assertTrue(archive.is_file())
        self.assertTrue(Path(result["staging"]).is_dir())

    def test_older_package_is_preserved_when_missing_version_uses_suffix(self):
        older = self.core / "packages/compiler"
        older.mkdir(parents=True)
        for name, text in package_files("compiler").items():
            (older / name).write_text(text)
        archive = self.archive({"compiler": package_files("compiler", "2.0.0")})
        seed.import_missing(archive, self.core, {"packages": [PackageSpec("vendor/compiler@2.0.0")]}, staging_parent=self.root / "retained")
        self.assertEqual(json.loads((older / "package.json").read_text())["version"], "1.0.0")
        self.assertTrue((self.core / "packages/compiler@2.0.0/payload.txt").is_file())

    def test_archive_without_metadata_cannot_manufacture_readiness(self):
        archive = self.archive({"missing": package_files("missing", metadata=False)})
        result = seed.import_missing(archive, self.core, {"packages": [PackageSpec("vendor/missing@1.0.0")]}, staging_parent=self.root / "retained")
        self.assertEqual(result["imported"], [])
        self.assertFalse((self.core / "packages/missing").exists())
        self.assertTrue(archive.is_file())

    def test_deep_windows_package_paths_import_without_short_alias(self):
        files = package_files("deep")
        files[("very-long-nested-folder/" * 12) + "payload.txt"] = "deep bytes"
        archive = self.archive({"deep": files})
        result = seed.import_missing(archive, self.core, {"packages": [PackageSpec("vendor/deep@1.0.0")]},
                                     staging_parent=self.root / "retained")
        self.assertEqual(len(result["imported"]), 1)
        from src.modules.bootstrap_platformio import extended_windows_path
        with open(extended_windows_path(self.core / "packages/deep" / (("very-long-nested-folder/" * 12) + "payload.txt"))) as payload:
            self.assertEqual(payload.read(), "deep bytes")

    def test_existing_invalid_package_is_retained_with_precise_error(self):
        folder = self.core / "packages/tool-scons"
        folder.mkdir(parents=True)
        (folder / "package.json").write_text(json.dumps({"name": "tool-scons", "version": "4.41101.0"}))
        with self.assertRaisesRegex(RuntimeError, "Existing package is incomplete"):
            seed.missing_packages(self.core, {"schema": 1, "platforms": ["absent"], "libraries": []})
        self.assertTrue((folder / "package.json").is_file())
        self.assertFalse((folder / ".piopm").exists())

    def test_malformed_manifest_and_missing_scons_payload_stop_without_repair(self):
        for failure in ("manifest", "scons.py"):
            with self.subTest(failure=failure):
                core = self.root / failure.replace(".", "-")
                folder = core / "packages/tool-scons"
                folder.mkdir(parents=True)
                for name, text in package_files("tool-scons", "4.41101.0", "platformio").items():
                    if name == "scons.py" and failure == "scons.py":
                        continue
                    (folder / name).write_text("{broken" if name == "package.json" and failure == "manifest" else text)
                before = {path.name: path.read_bytes() for path in folder.iterdir()}
                with self.assertRaisesRegex(RuntimeError, "Existing.*(invalid|incomplete)"):
                    seed.missing_packages(core, {"schema": 1, "platforms": ["absent"], "libraries": []})
                manager = seed.managers(core)["packages"]
                with patch.object(manager, "install") as installer:
                    with self.assertRaisesRegex(RuntimeError, "Existing files were retained"):
                        seed.prepare_scons(core, manager=manager, log=lambda *args: None)
                    installer.assert_not_called()
                self.assertEqual(before, {path.name: path.read_bytes() for path in folder.iterdir()})

    def test_scons_install_must_return_real_manifest_and_payload(self):
        manager = seed.managers(self.core)["packages"]

        def incomplete_install(spec, **kwargs):
            folder = self.core / "packages/tool-scons"
            folder.mkdir(parents=True)
            for name, text in package_files("tool-scons", "4.41101.0", "platformio").items():
                if name != "scons.py":
                    (folder / name).write_text(text)
        with patch.object(manager, "install", side_effect=incomplete_install):
            with self.assertRaisesRegex(RuntimeError, "without a valid package/payload"):
                seed.prepare_scons(self.core, manager=manager, log=lambda *args: None)
        self.assertTrue((self.core / "packages/tool-scons/.piopm").is_file())

    def test_semantically_equal_manifest_versions_resolve_locally(self):
        folder = self.core / "packages/equivalent"
        folder.mkdir(parents=True)
        files = package_files("equivalent", "1.0.0")
        files["package.json"] = json.dumps({"name": "equivalent", "version": "1.0"})
        for name, text in files.items():
            (folder / name).write_text(text)
        self.assertIsNotNone(seed.valid_package(seed.managers(self.core)["packages"], PackageSpec("vendor/equivalent@1.0.0")))

    def test_traversal_fails_before_writes_and_retains_archive_staging(self):
        archive = self.archive({}, {"../outside.txt": "escape"})
        with self.assertRaisesRegex(RuntimeError, "Unsafe release archive path"):
            seed.import_missing(archive, self.core, {"packages": []}, staging_parent=self.root / "retained")
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertTrue(archive.is_file())
        self.assertTrue(any((self.root / "retained").iterdir()))

    def test_scons_metadata_and_payload_use_normal_manager_resolution(self):
        scope, _ = bootstrap_scope(self.root)
        manager = seed.managers(self.core)["packages"]
        package = self.core / "packages/tool-scons"
        package.mkdir(parents=True)
        for name, text in package_files("tool-scons", "4.41101.0", "platformio", metadata=False).items():
            (package / name).write_text(text)
        with patch.object(seed, "managers", return_value={"packages": manager}), patch.object(manager, "install") as install:
            self.assertFalse(scope["ensure_platformio_scons"](pio_core_dir=self.core))
            install.assert_not_called()
            self.assertFalse((package / ".piopm").exists())
        second = self.root / "absent-core"
        real_manager = seed.managers(second)["packages"]
        calls = []

        def install(spec, **kwargs):
            calls.append((spec.humanize(), kwargs))
            target = second / "packages/tool-scons"
            target.mkdir(parents=True)
            for name, text in package_files("tool-scons", "4.41101.0", "platformio").items():
                (target / name).write_text(text)
        with patch.object(seed, "managers", return_value={"packages": real_manager}), patch.object(real_manager, "install", side_effect=install):
            self.assertTrue(scope["ensure_platformio_scons"](pio_core_dir=second))
        self.assertEqual(calls, [(seed.scons_spec().humanize(), {"skip_dependencies": True})])

    def test_real_http_failure_retains_existing_partial_and_never_deletes(self):
        class Failure(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(503)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Failure)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        scope, _ = bootstrap_scope(self.root)
        destination = self.root / "download.zip"
        partial = self.root / "download.zip.part"
        partial.write_bytes(b"retained original partial")
        parts = self.root / "download.zip.part.parts"
        parts.mkdir()
        (parts / "checkpoint").write_bytes(b"checkpoint")
        try:
            with self.assertRaisesRegex(RuntimeError, "retained"):
                scope["_download_file"](f"http://127.0.0.1:{server.server_port}/seed.zip", destination,
                                        attempts=1, timeout=2, preserve_failed=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(partial.read_bytes(), b"retained original partial")
        self.assertEqual((parts / "checkpoint").read_bytes(), b"checkpoint")
        scope["safe_unlink"].assert_not_called()
        scope["safe_rmtree"].assert_not_called()

    def test_verified_cached_seed_is_used_before_network(self):
        scope, logs = bootstrap_scope(self.root)
        cached = self.root / "src/seed.zip"
        cached.parent.mkdir()
        cached.write_bytes(b"verified fixture archive")
        scope["_PLATFORMIO_PREBUILT_EXPECTED_SIZE"] = cached.stat().st_size
        scope["_PLATFORMIO_PREBUILT_EXPECTED_SHA256"] = hashlib.sha256(cached.read_bytes()).hexdigest()
        missing = {"packages": [PackageSpec("vendor/missing@1.0.0")], "platforms": [], "lib": []}
        with patch.object(seed, "missing_packages", side_effect=[missing, {"packages": [], "platforms": [], "lib": []}]), \
                patch.object(seed, "import_missing", return_value={"imported": ["fixture"]}) as importer:
            self.assertTrue(scope["_ensure_platformio_core_prebuilt"](script_dir=self.root,
                plan={"schema": 1, "platforms": ["demo"], "frameworks": ["arduino"], "libraries": []}))
        importer.assert_called_once()
        scope["_is_network_reachable"].assert_not_called()
        self.assertTrue(cached.is_file())
        self.assertTrue(any("verified cached" in line for line in logs))

    def test_complete_configured_scope_skips_seed_network_and_archive(self):
        scope, _ = bootstrap_scope(self.root)
        with patch.object(seed, "missing_packages", return_value={"packages": [], "platforms": [], "lib": []}), \
                patch.object(seed, "import_missing") as importer:
            self.assertTrue(scope["_ensure_platformio_core_prebuilt"](script_dir=self.root,
                plan={"schema": 1, "platforms": ["demo"], "libraries": []}))
        importer.assert_not_called()
        scope["_is_network_reachable"].assert_not_called()
        self.assertFalse((self.root / "src").exists())

    def test_successful_bootstrap_cleanup_removes_temporary_seed_archive(self):
        scope, logs = bootstrap_scope(self.root)
        archive = self.root / "src/seed.zip"
        archive.parent.mkdir()
        archive.write_bytes(b"verified seed fixture")
        scope["safe_unlink"] = lambda path: Path(path).unlink() is None
        self.assertTrue(scope["_cleanup_platformio_prebuilt_archive"](script_dir=self.root))
        self.assertFalse(archive.exists())
        self.assertTrue(any("removed after successful bootstrap" in line.lower() for line in logs))

    def test_cleanup_failure_is_nonfatal_and_retains_seed_archive(self):
        scope, logs = bootstrap_scope(self.root)
        archive = self.root / "src/seed.zip"
        archive.parent.mkdir()
        archive.write_bytes(b"verified seed fixture")
        scope["safe_unlink"] = Mock(return_value=False)
        self.assertFalse(scope["_cleanup_platformio_prebuilt_archive"](script_dir=self.root))
        self.assertTrue(archive.is_file())
        scope["safe_unlink"].assert_called_once_with(archive)
        self.assertTrue(any("was retained" in line.lower() for line in logs))

    def test_seed_archive_cleanup_runs_after_successful_gui_start_probe(self):
        source = (ROOT / "src/modules/bootstrap.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        worker = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef)
                      and node.name == "_run_setup_in_thread")
        spawn_line = next(node.lineno for node in ast.walk(worker)
                          if isinstance(node, ast.Call)
                          and isinstance(node.func, ast.Name)
                          and node.func.id == "_spawn_main_gui")
        poll_line = next(node.lineno for node in ast.walk(worker)
                         if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Attribute)
                         and node.func.attr == "poll"
                         and isinstance(node.func.value, ast.Name)
                         and node.func.value.id == "proc")
        cleanup_line = next(node.lineno for node in ast.walk(worker)
                            if isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name)
                            and node.func.id == "_cleanup_platformio_prebuilt_archive")
        self.assertGreater(cleanup_line, spawn_line)
        self.assertGreater(cleanup_line, poll_line)

    def test_new_platforms_receive_second_seed_pass_for_discovered_packages(self):
        scope, _ = bootstrap_scope(self.root)
        cached = self.root / "src/seed.zip"
        cached.parent.mkdir()
        cached.write_bytes(b"verified fixture archive")
        scope["_PLATFORMIO_PREBUILT_EXPECTED_SIZE"] = cached.stat().st_size
        scope["_PLATFORMIO_PREBUILT_EXPECTED_SHA256"] = hashlib.sha256(cached.read_bytes()).hexdigest()
        first = {"platforms": [PackageSpec("demo")], "packages": [], "lib": []}
        second = {"platforms": [], "packages": [PackageSpec("vendor/compiler@1.0.0")], "lib": []}
        complete = {"platforms": [], "packages": [], "lib": []}
        with patch.object(seed, "missing_packages", side_effect=[first, second, complete]), \
                patch.object(seed, "import_missing", side_effect=[{"imported": ["platform"]}, {"imported": ["compiler"]}]) as importer:
            self.assertTrue(scope["_ensure_platformio_core_prebuilt"](script_dir=self.root,
                plan={"schema": 1, "platforms": ["demo"], "libraries": []}))
        self.assertEqual(importer.call_count, 2)
        self.assertEqual(importer.call_args_list[1].args[2], second)

    def test_failed_seed_preserves_existing_partial_through_real_http_step(self):
        class Failure(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(503)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Failure)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        scope, logs = bootstrap_scope(self.root)
        partial = self.root / "src/seed.zip.part"
        partial.parent.mkdir()
        partial.write_bytes(b"original partial")
        scope["_PLATFORMIO_PREBUILT_EXPECTED_SIZE"] = 100
        try:
            self.assertFalse(scope["_ensure_platformio_core_prebuilt"](script_dir=self.root,
                plan={"schema": 1, "platforms": ["absent"], "libraries": []},
                seed_url=f"http://127.0.0.1:{server.server_port}/seed.zip", attempts=1))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(partial.read_bytes(), b"original partial")
        scope["safe_unlink"].assert_not_called()
        scope["safe_rmtree"].assert_not_called()
        self.assertTrue(any("download failed" in message.lower() for message in logs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
