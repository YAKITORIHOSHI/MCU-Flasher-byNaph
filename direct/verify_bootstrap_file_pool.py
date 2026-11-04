#!/usr/bin/env python3
"""Verify parallel package work and the selective release download.

Uses owned archives and a loopback HTTP server. No installer, hardware, live store
or network service is touched. Timing checks inject a per-file wait that releases
the GIL, as real-time antivirus does, because a warm local disk would hide the
serial cost this work removes.
"""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src/modules"))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from src.modules import bootstrap_file_pool as pool_module
from src.modules.bootstrap_file_pool import FilePool, Progress, run_all
from src.modules.bootstrap_output import PackageOutput
from src.modules.bootstrap_package_recovery import _tree_signature, atomic_package_writes
from src.modules.bootstrap_platformio import parallel_archive_writes
from src.modules.bootstrap_seed import import_missing, open_release_tail


def load_bootstrap():
    """Import bootstrap.py without its handoff to the private runtime, which CI lacks."""
    modules = str(ROOT / "src/modules")
    if modules not in sys.path:
        sys.path.insert(0, modules)
    import private_python_guard
    with patch.object(private_python_guard, "enforce_private_python"):
        from src.modules import bootstrap
    return bootstrap


def tree(path):
    result = {}
    for item in sorted(Path(path).rglob("*")):
        relative = str(item.relative_to(path))
        if item.is_dir():
            result[relative] = "directory"
        else:
            info = item.lstat()
            result[relative] = (item.read_bytes(), stat.S_IMODE(info.st_mode), int(info.st_mtime))
    return result


class Fixture(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/bootstrap-file-pool"
        audit.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def tar_archive(self, name="package.tar.gz", count=300, extra=()):
        path = self.root / name
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("package/nested")
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 1700000000
            archive.addfile(info)
            for index in range(count):
                body = f"payload {index}\n".encode() * (1 + index % 7)
                info = tarfile.TarInfo(f"package/nested/file{index:04d}.hex")
                info.size, info.mode, info.mtime = len(body), 0o640 if index % 2 else 0o755, 1700000000 + index
                archive.addfile(info, io.BytesIO(body))
            for member, body in extra:
                info = tarfile.TarInfo(member)
                info.size, info.mode, info.mtime = len(body), 0o644, 1700000000
                archive.addfile(info, io.BytesIO(body))
        return path


class FilePoolChecks(Fixture):
    def test_runs_everything_and_reports_the_first_failure(self):
        seen = []
        run_all(seen.append, list(range(500)), workers=8)
        self.assertEqual(sorted(seen), list(range(500)))

        def fail(item):
            if item == 7:
                raise ValueError("fixture failure")
        with self.assertRaisesRegex(ValueError, "fixture failure"):
            run_all(fail, list(range(50)), workers=4)

    def test_queue_stays_bounded_for_count_and_bytes(self):
        running = threading.Event()
        release = threading.Event()
        peak = [0]
        with FilePool(2, max_pending=4, max_bytes=100) as pool:
            lock = threading.Lock()

            def job():
                running.set()
                release.wait(5)
            threads = []

            def feed():
                for _ in range(40):
                    pool.submit(job, size=30)
                    with lock:
                        peak[0] = max(peak[0], pool._pending)
            feeder = threading.Thread(target=feed)
            feeder.start()
            threads.append(feeder)
            running.wait(5)
            time.sleep(0.1)
            self.assertLessEqual(pool._pending, 4)
            self.assertLessEqual(pool._bytes, 100)
            release.set()
            feeder.join(10)

    def test_oversized_single_job_cannot_deadlock(self):
        with FilePool(2, max_bytes=10) as pool:
            done = []
            pool.submit(done.append, 1, size=1000)
            pool.submit(done.append, 2, size=1000)
        self.assertEqual(sorted(done), [1, 2])

    def test_worker_count_honours_the_slow_drive_limit(self):
        with patch("main.core.storage_resources.storage_worker_limit", return_value=2):
            self.assertEqual(pool_module.file_workers(), 2)
        with patch("main.core.storage_resources.storage_worker_limit", return_value=None):
            self.assertGreaterEqual(pool_module.file_workers(), 2)


class ParallelTarChecks(Fixture):
    def unpack(self, archive, destination, parallel):
        from platformio.package.unpack import FileUnpacker
        destination.mkdir(parents=True)
        if parallel:
            with parallel_archive_writes(), FileUnpacker(str(archive)) as unpacker:
                unpacker.unpack(str(destination), with_progress=False, silent=True)
        else:
            with FileUnpacker(str(archive)) as unpacker:
                unpacker.unpack(str(destination), with_progress=False, silent=True)

    def test_parallel_output_matches_upstream_bytes_modes_and_times(self):
        archive = self.tar_archive()
        self.unpack(archive, self.root / "serial", False)
        self.unpack(archive, self.root / "parallel", True)
        self.assertEqual(tree(self.root / "serial"), tree(self.root / "parallel"))
        self.assertEqual(len(tree(self.root / "parallel")), 302)

    def test_traversal_member_is_still_refused(self):
        archive = self.tar_archive("hostile.tar.gz", count=3, extra=(("../escaped.txt", b"nope"),))
        with self.assertRaises(Exception):
            self.unpack(archive, self.root / "hostile", True)
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_hooks_restore_after_failure(self):
        from platformio.package.unpack import FileUnpacker, TARArchiver
        before = FileUnpacker.unpack, TARArchiver.extract_item
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with parallel_archive_writes():
                raise RuntimeError("fixture")
        self.assertEqual((FileUnpacker.unpack, TARArchiver.extract_item), before)

    def test_a_write_failure_fails_the_unpack(self):
        archive = self.tar_archive(count=40)
        with patch("src.modules.bootstrap_platformio._ParallelTarWriter._write", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.unpack(archive, self.root / "failed", True)

    def test_real_installer_publishes_the_same_tree_with_progress(self):
        from platformio.package.manager.tool import ToolPackageManager
        core = self.root / "core"
        (core / "packages").mkdir(parents=True)
        (core / ".cache").mkdir()
        archive = self.tar_archive("tool.tar.gz", count=200, extra=(
            ("package/package.json", json.dumps({"name": "tool-fixture", "version": "1.2.3"}).encode()),))
        messages = []
        environment = {"PLATFORMIO_CORE_DIR": str(core), "PLATFORMIO_PACKAGES_DIR": str(core / "packages"),
                       "PLATFORMIO_CACHE_DIR": str(core / ".cache")}
        with patch.dict(os.environ, environment), patch.object(pool_module, "PROGRESS_SECONDS", 0):
            with parallel_archive_writes(), atomic_package_writes(core, log=messages.append):
                manager = ToolPackageManager(str(core / "packages"))
                installed = manager.install(f"file://{archive}")
        package = Path(installed.path)
        self.assertEqual((package / "nested/file0002.hex").read_bytes(), b"payload 2\n" * 3)
        self.assertEqual(sum(1 for item in package.rglob("*.hex")), 200)
        self.assertTrue(any(re.fullmatch(r"Package files: Copying \d+%", text) for text in messages), messages[:5])
        self.assertTrue(any(re.fullmatch(r"Package files: Verifying \d+%", text) for text in messages), messages[:5])
        for phase in ("Copying", "Verifying"):
            percents = [int(text.rsplit(" ", 1)[1][:-1]) for text in messages if f": {phase} " in text]
            self.assertEqual(percents, sorted(percents), f"{phase} went backwards")
        events = []
        output = PackageOutput(lambda kind, text: events.append((kind, text)))
        for text in messages:
            output.feed(text + "\n")
        self.assertFalse([text for kind, text in events if "interrupted" in text], "A healthy install looked interrupted")
        leftovers = [path for path in (core / ".cache/tmp").glob("pkg-installing-*")] if (core / ".cache/tmp").exists() else []
        self.assertEqual(leftovers, [], "Unpacked staging was not removed")
        self.assertEqual(list((core / ".mcu-package-staging").iterdir()), [], "Publication left staging behind")

    def test_a_corrupt_copy_is_still_rejected_and_retained(self):
        from src.modules import bootstrap_package_recovery as recovery
        source = self.root / "source"
        source.mkdir()
        (source / "payload").write_bytes(b"original")
        staging = self.root / "staging"

        class Copier:
            copytree, copystat = staticmethod(shutil.copytree), staticmethod(shutil.copystat)

            @staticmethod
            def copy2(src, dst, **_kwargs):
                shutil.copy2(src, dst)
                Path(dst).write_bytes(b"corrupt!")
        with self.assertRaisesRegex(RuntimeError, "failed verification"):
            with patch.object(recovery, "_stream_copy_options", return_value=None):
                recovery._copy_package_stream(str(source), staging, Copier, (), {"symlinks": True})
        self.assertTrue(staging.is_dir(), "Failed staging must stay available for diagnosis")


class ThroughputChecks(Fixture):
    def test_per_file_wait_no_longer_multiplies_serially(self):
        files = []
        for index in range(160):
            path = self.root / f"file{index}"
            path.write_bytes(b"x")
            files.append(path)
        wait = 0.002

        def slow(path):
            time.sleep(wait)
            path.read_bytes()
        started = time.monotonic()
        run_all(slow, files, workers=8)
        elapsed = time.monotonic() - started
        serial = wait * len(files)
        self.assertLess(elapsed, serial / 2.5, f"{elapsed:.3f}s against a serial {serial:.3f}s")

    def test_signature_is_identical_regardless_of_concurrency(self):
        source = self.root / "tree"
        (source / "a/b").mkdir(parents=True)
        for index in range(64):
            (source / "a/b" / f"f{index}").write_bytes(os.urandom(64))
        with patch.object(pool_module, "file_workers", return_value=1):
            serial = _tree_signature(source)
        with patch.object(pool_module, "file_workers", return_value=12):
            parallel = _tree_signature(source)
        self.assertEqual(serial, parallel)
        self.assertEqual(serial[os.path.join("a", "b", "f0")][0], "file")


class ProgressChecks(unittest.TestCase):
    def test_silent_phases_drive_the_visible_progress_row(self):
        events = []
        output = PackageOutput(lambda kind, text: events.append((kind, text)))
        for text in ("Tool Manager: Installing framework-arduino-avr-megacore", "Unpacking 100%",
                     "Package files: Copying 40%", "Package files: Verifying 75%",
                     "Tool Manager: framework-arduino-avr-megacore has been installed!"):
            output.feed(text + "\n")
        output.finish()
        rows = [text for kind, text in events if kind == "progress"]
        self.assertTrue(any("Copying" in row and "40%" in row for row in rows), rows)
        self.assertTrue(any("Verifying" in row and "75%" in row for row in rows), rows)
        self.assertFalse(any("Package files" in text for kind, text in events if kind in ("dim", "ok")), events)
        self.assertTrue(any(kind == "ok" and "has been installed" in text for kind, text in events))

    def test_progress_is_rate_limited_and_finishes_at_100(self):
        lines = []
        with patch.object(pool_module, "PROGRESS_SECONDS", 3600):
            progress = Progress(lines.append, "Copying", 1000)
            for _ in range(1000):
                progress.step()
        self.assertEqual(lines, ["Package files: Copying 100%"])


class _RangeHandler(BaseHTTPRequestHandler):
    body = b""
    requests = []

    def do_GET(self):
        header = self.headers.get("Range", "")
        match = re.fullmatch(r"bytes=(\d+)-(\d+)?", header)
        type(self).requests.append(header)
        if not match:
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)
            return
        start = int(match[1])
        end = int(match[2]) if match[2] else len(self.body) - 1
        payload = self.body[start:end + 1]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.body)}")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


class SelectiveReleaseChecks(Fixture):
    def setUp(self):
        super().setUp()
        package = self.root / "seed-source/packages/tool-fixture"
        package.mkdir(parents=True)
        piopm = {"type": "tool", "name": "tool-fixture", "version": "1.0.0",
                 "spec": {"owner": "platformio", "name": "tool-fixture", "requirements": None, "uri": None}}
        self.archive = self.root / "seed.zip"
        with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_STORED) as bundle:
            bundle.writestr(".cache/downloads/junk.bin", os.urandom(4 * 1024 * 1024))
            self.tail_start = bundle.fp.tell()
            for name in ("package.json", ".piopm"):
                pass
            bundle.writestr("packages/tool-fixture/.piopm", json.dumps(piopm))
            bundle.writestr("packages/tool-fixture/package.json",
                            json.dumps({"name": "tool-fixture", "version": "1.0.0"}))
            for index in range(120):
                bundle.writestr(f"packages/tool-fixture/data/{index}.bin", f"member {index}".encode())
        self.bytes = self.archive.read_bytes()
        self.tail = self.bytes[self.tail_start:]

    def serve(self):
        _RangeHandler.body = self.bytes
        _RangeHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/seed.zip"

    def bootstrap(self):
        return load_bootstrap()

    def test_only_the_verified_tail_is_transferred_and_imports_normally(self):
        bootstrap = self.bootstrap()
        url = self.serve()
        source = self.root / "src"
        source.mkdir()
        with patch.object(bootstrap, "_gui", None), \
                patch.object(bootstrap, "_PLATFORMIO_PREBUILT_GITHUB_URL", url), \
                patch.object(bootstrap, "_PLATFORMIO_PREBUILT_EXPECTED_SIZE", len(self.bytes)), \
                patch.object(bootstrap, "_PLATFORMIO_PREBUILT_TAIL_OFFSET", self.tail_start), \
                patch.object(bootstrap, "_PLATFORMIO_PREBUILT_TAIL_SIZE", len(self.tail)), \
                patch.object(bootstrap, "_PLATFORMIO_PREBUILT_TAIL_SHA256", hashlib.sha256(self.tail).hexdigest()):
            stored = bootstrap._download_release_packages(self.root)
        self.assertEqual(stored.read_bytes(), self.tail)
        for header in _RangeHandler.requests:
            self.assertGreaterEqual(int(re.match(r"bytes=(\d+)", header)[1]), self.tail_start,
                                    "Unused leading bytes were requested")

        from platformio.package.meta import PackageSpec
        core = self.root / "core"
        core.mkdir()
        spec = PackageSpec(owner="platformio", name="tool-fixture")
        result = import_missing(open_release_tail(stored, self.tail_start, len(self.bytes)), core,
                                {"packages": [spec], "platforms": [], "lib": []},
                                staging_parent=self.root / "staging", log=lambda *_: None)
        self.assertEqual(len(result["imported"]), 1)
        self.assertEqual((core / "packages/tool-fixture/data/7.bin").read_bytes(), b"member 7")

    def test_wrong_tail_digest_is_refused_and_selects_the_full_archive(self):
        bootstrap = self.bootstrap()
        url = self.serve()
        (self.root / "src").mkdir()
        with patch.object(bootstrap, "_gui", None), \
                patch.object(bootstrap, "_PLATFORMIO_PREBUILT_GITHUB_URL", url), \
                patch.object(bootstrap, "_PLATFORMIO_PREBUILT_EXPECTED_SIZE", len(self.bytes)), \
                patch.object(bootstrap, "_PLATFORMIO_PREBUILT_TAIL_OFFSET", self.tail_start), \
                patch.object(bootstrap, "_PLATFORMIO_PREBUILT_TAIL_SIZE", len(self.tail)), \
                patch.object(bootstrap, "_PLATFORMIO_PREBUILT_TAIL_SHA256", "0" * 64):
            self.assertIsNone(bootstrap._download_release_packages(self.root))
        self.assertFalse((self.root / "src" / bootstrap._PLATFORMIO_PREBUILT_TAIL_NAME).exists())

    def test_reads_below_the_tail_never_return_substituted_bytes(self):
        path = self.root / "tail.bin"
        path.write_bytes(self.tail)
        with zipfile.ZipFile(open_release_tail(path, self.tail_start, len(self.bytes))) as bundle:
            self.assertEqual(bundle.read("packages/tool-fixture/data/3.bin"), b"member 3")
            with self.assertRaises(OSError):
                bundle.read(".cache/downloads/junk.bin")


class AvrReleasePin(unittest.TestCase):
    def test_exact_release_needs_no_index_download(self):
        bootstrap = load_bootstrap()
        with patch.object(bootstrap, "_fetch_url", side_effect=AssertionError("Fetched the whole index")):
            release = bootstrap._load_arduino_avr_board_release("1.8.6")
        self.assertEqual(release["size"], 7127080)
        self.assertRegex(release["sha256"], r"^[0-9a-f]{64}$")
        with patch.object(bootstrap, "_fetch_url", return_value=None) as fetch:
            self.assertIsNone(bootstrap._load_arduino_avr_board_release("1.8.5"))
        fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=1)
