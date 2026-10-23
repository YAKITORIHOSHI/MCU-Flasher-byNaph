#!/usr/bin/env python3
"""Real PlatformIO recovery and interrupted publication fixtures; no downloads."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from platformio.package.meta import PackageItem, PackageMetadata, PackageSpec
from platformio.package.manager.tool import ToolPackageManager


class RecoveryChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/bootstrap-package-recovery"
        audit.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(self.temp.cleanup)
        self.core = Path(self.temp.name) / "core"
        self.core.mkdir()
        # Tiny corruption fixtures exercise streaming without large allocations;
        # the production threshold has a separate boundary/fallback check.
        recovery = self.recovery()
        self.thresholds = recovery.STREAM_MIN_BYTES, recovery.STREAM_MIN_AVERAGE
        threshold_patch = patch.multiple(recovery, STREAM_MIN_BYTES=0, STREAM_MIN_AVERAGE=0)
        threshold_patch.start()
        self.addCleanup(threshold_patch.stop)

    def recovery(self):
        name = "src.modules.bootstrap_package_recovery"
        self.assertIsNotNone(importlib.util.find_spec(name), "Bootstrap lacks preserved-package recovery")
        return __import__(name, fromlist=["recover_packages"])

    def package(self, name="demo", parent="packages"):
        folder = self.core / parent / name
        folder.mkdir(parents=True)
        (folder / "package.json").write_text(json.dumps({"name": name, "version": "1.0.0"}))
        meta = PackageMetadata("tool", name, "1.0.0", PackageSpec(owner="vendor", name=name))
        meta.dump(folder / ".piopm")
        (folder / "payload").write_bytes(b"original binary bytes\x00\xff")
        return folder

    def copy_fixture(self):
        source = self.package(parent=".cache/tmp/pkg-installing-fixture")
        target = self.core / "packages/demo"
        target.parent.mkdir()
        return source, target

    def copy_proxy(self, after=None, copystat=None):
        class Proxy:
            def __getattr__(self, name):
                return getattr(shutil, name)

            def copytree(self, source, destination, *args, **kwargs):
                result = shutil.copytree(source, destination, *args, **kwargs)
                if after:
                    after(Path(source), Path(destination))
                return result
        proxy = Proxy()
        if copystat is not None:
            proxy.copystat = copystat
        return proxy

    def test_bad_metadata_preserves_originals_and_healthy_neighbor_then_enumerates(self):
        recovery = self.recovery()
        neighbor = self.package("healthy")
        stamp = (neighbor / ".piopm").stat().st_mtime_ns
        for number, raw in enumerate((b"", b'{"type":', b"[]", b"null", b"{}",
                b'{"type":"tool","name":"demo","version":"1.0.0","spec":{}}')):
            folder = self.package("bad" + str(number))
            (folder / ".piopm").write_bytes(raw)
            (self.core / ".mcu-offline-ready.json").write_text("old success")
            receipts = recovery.recover_packages(self.core, log=lambda _: None)
            self.assertEqual(len(receipts), 1)
            saved = Path(receipts[0]["preserved"])
            self.assertEqual((saved / ".piopm").read_bytes(), raw)
            self.assertEqual((saved / "payload").read_bytes(), b"original binary bytes\x00\xff")
            self.assertFalse(folder.exists())
            self.assertFalse((self.core / ".mcu-offline-ready.json").exists())
            receipt = json.loads(Path(receipts[0]["receipt"]).read_text())
            self.assertEqual(receipt["metadata"][".piopm"]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(recovery.recover_packages(self.core, log=lambda _: None), [])
        self.assertEqual((neighbor / ".piopm").stat().st_mtime_ns, stamp)
        self.assertEqual([p.metadata.name for p in ToolPackageManager(str(self.core / "packages")).get_installed()], ["healthy"])

    def test_bad_manifest_is_preserved_but_unknown_folders_are_untouched(self):
        recovery = self.recovery()
        folder = self.package()
        (folder / "package.json").write_bytes(b"")
        unknown = self.core / "packages/user-files"
        unknown.mkdir()
        (unknown / "notes.txt").write_text("retain")
        result = recovery.recover_packages(self.core, log=lambda _: None)
        self.assertEqual(len(result), 1)
        self.assertEqual((Path(result[0]["preserved"]) / "package.json").read_bytes(), b"")
        self.assertEqual((unknown / "notes.txt").read_text(), "retain")

    def test_redirected_metadata_and_permission_errors_never_quarantine(self):
        recovery = self.recovery()
        folder = self.package()
        (folder / ".piopm").unlink()
        outside = self.core / "outside"
        outside.write_bytes(b"")
        try:
            (folder / ".piopm").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"This host cannot create the redirected metadata fixture: {exc}")
        with self.assertRaisesRegex(RuntimeError, "redirect|link"):
            recovery.recover_packages(self.core)
        self.assertTrue(folder.is_dir())
        (folder / ".piopm").unlink()
        (folder / ".piopm").write_bytes(b"")
        real = Path.open
        def denied(path, *args, **kwargs):
            if path == folder / ".piopm":
                raise PermissionError("fixture denied")
            return real(path, *args, **kwargs)
        with patch.object(Path, "open", denied), self.assertRaises(PermissionError):
            recovery.recover_packages(self.core)
        self.assertTrue(folder.is_dir())

    def test_interrupted_metadata_write_keeps_old_bytes(self):
        recovery = self.recovery()
        folder = self.package()
        path = folder / ".piopm"
        original = path.read_bytes()
        meta = PackageItem(str(folder)).metadata
        with recovery.atomic_package_writes(self.core), patch.object(recovery.os, "replace", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                meta.dump(path)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(list(folder.glob(".piopm.*.tmp")))

    def test_interrupted_receipt_completion_keeps_entire_original_recoverable(self):
        recovery = self.recovery()
        folder = self.package()
        (folder / ".piopm").write_bytes(b"")
        marker = self.core / ".mcu-offline-ready.json"
        marker.write_text("old success")
        original = recovery._write_json
        calls = []
        def interrupted(path, value):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("receipt interrupted after preserved rename")
            return original(path, value)
        with patch.object(recovery, "_write_json", interrupted), self.assertRaisesRegex(OSError, "receipt interrupted"):
            recovery.recover_packages(self.core)
        self.assertFalse(folder.exists())
        self.assertFalse(marker.exists())
        receipt = json.loads(next((self.core / ".mcu-package-recovery").glob("*/receipt.json")).read_text())
        self.assertEqual(receipt["state"], "pending")
        saved = Path(receipt["preserved"])
        self.assertEqual((saved / "payload").read_bytes(), b"original binary bytes\x00\xff")
        self.assertEqual((saved / ".piopm").read_bytes(), b"")
        self.assertEqual(recovery.recover_packages(self.core), [])

    def test_windows_readonly_payload_verification_does_not_flush_read_handles(self):
        recovery = self.recovery()
        source = self.package()
        with patch.object(recovery, "sys", SimpleNamespace(platform="win32")), \
                patch.object(recovery.os, "fsync", side_effect=AssertionError("Read handles cannot be flushed on Windows")):
            signature = recovery._tree_signature(source, sync=True)
        self.assertIn("payload", signature)

    def test_interrupted_payload_copy_is_absent_from_store_and_retry_succeeds(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source = self.package(parent=".cache/tmp")
        target = self.core / "packages/demo"
        target.parent.mkdir()
        def interrupted(src, dst, *args, **kwargs):
            Path(dst).mkdir()
            (Path(dst) / ".piopm").write_bytes((Path(src) / ".piopm").read_bytes())
            raise OSError("copy interrupted")
        with patch.object(shutil, "copytree", interrupted), recovery.atomic_package_writes(self.core):
            with self.assertRaisesRegex(OSError, "copy interrupted"):
                installer.shutil.copytree(str(source), str(target), symlinks=True)
        self.assertFalse(target.exists())
        self.assertTrue(list((self.core / ".mcu-package-staging").glob("*/.piopm")))
        with recovery.atomic_package_writes(self.core):
            installer.shutil.copytree(str(source), str(target), symlinks=True)
        self.assertEqual((target / "payload").read_bytes(), (source / "payload").read_bytes())
        self.assertEqual(PackageItem(str(target)).metadata.name, "demo")
        self.assertEqual(recovery.recover_packages(self.core), [])

    def test_completed_copy_is_checked_before_publication_and_existing_target_is_retained(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source = self.package(parent=".cache/tmp")
        target = self.core / "packages/demo"
        target.parent.mkdir()
        real = shutil.copytree
        def damaged(src, dst, *args, **kwargs):
            result = real(src, dst, *args, **kwargs)
            (Path(dst) / "payload").write_bytes(b"silently damaged")
            return result
        with patch.object(shutil, "copytree", damaged), recovery.atomic_package_writes(self.core):
            with self.assertRaisesRegex(RuntimeError, "bytes|verification"):
                installer.shutil.copytree(str(source), str(target), symlinks=True)
        self.assertFalse(target.exists())
        target.mkdir()
        (target / "user-data").write_text("retain")
        with recovery.atomic_package_writes(self.core), self.assertRaisesRegex(RuntimeError, "existing|appeared"):
            installer.shutil.copytree(str(source), str(target), symlinks=True)
        self.assertEqual((target / "user-data").read_text(), "retain")

    @unittest.skipIf(sys.platform == "win32", "Native Windows copy2 retains its complete metadata contract")
    def test_stream_reads_each_source_and_stage_payload_once_with_a_bounded_buffer(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source, target = self.copy_fixture()
        (source / "payload").write_bytes(b"a" * (2 * recovery.COPY_BUFFER + 37))
        (source / "empty").touch()
        expected = sum(path.stat().st_size for path in source.iterdir())
        counts = {"source": 0, "stage": 0}
        requests = []
        original_open = Path.open

        class Reader:
            def __init__(self, stream, kind):
                self.stream, self.kind = stream, kind
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def __enter__(self):
                self.stream.__enter__()
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def read(self, count=-1):
                requests.append(count)
                data = self.stream.read(count)
                counts[self.kind] += len(data)
                return data

        def observed_open(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            if mode == "rb" and path.is_relative_to(source):
                return Reader(stream, "source")
            if mode == "rb" and path.is_relative_to(self.core / ".mcu-package-staging"):
                return Reader(stream, "stage")
            return stream

        with patch.object(Path, "open", observed_open), recovery.atomic_package_writes(self.core):
            installer.shutil.copytree(source, target, symlinks=True)
        self.assertEqual(counts, {"source": expected, "stage": expected})
        self.assertTrue(requests)
        self.assertTrue(all(0 < count <= recovery.COPY_BUFFER for count in requests))
        self.assertEqual((target / "empty").stat().st_size, 0)

    def test_zero_length_files_empty_directories_and_modes_are_preserved(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source, target = self.copy_fixture()
        (source / "empty").touch()
        (source / "empty-dir").mkdir()
        (source / "payload").chmod(0o444)
        (source / "empty-dir").chmod(0o750)
        with recovery.atomic_package_writes(self.core):
            installer.shutil.copytree(source, target, symlinks=True)
        self.assertEqual(recovery._tree_signature(source), recovery._tree_signature(target))
        self.assertEqual((target / "empty").read_bytes(), b"")
        self.assertEqual(stat.S_IMODE((target / "payload").stat().st_mode),
                         stat.S_IMODE((source / "payload").stat().st_mode))

    def test_file_directory_and_dangling_links_keep_literal_targets(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source, target = self.copy_fixture()
        (source / "directory").mkdir()
        try:
            (source / "file-link").symlink_to("payload")
            (source / "directory-link").symlink_to("directory", target_is_directory=True)
            (source / "dangling-link").symlink_to("absent")
        except OSError as exc:
            self.skipTest(f"This host cannot create package link fixtures: {exc}")
        with recovery.atomic_package_writes(self.core):
            installer.shutil.copytree(source, target, symlinks=True)
        for name in ("file-link", "directory-link", "dangling-link"):
            self.assertTrue((target / name).is_symlink())
            self.assertEqual(os.readlink(target / name), os.readlink(source / name))

    def test_silently_truncated_or_mode_changed_staging_never_publishes(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source, target = self.copy_fixture()
        for damage in (lambda stage: (stage / "payload").write_bytes(b""),
                       lambda stage: (stage / "payload").chmod(0o444)):
            proxy = self.copy_proxy(after=lambda src, stage: damage(stage))
            with self.subTest(damage=damage), patch.object(installer, "shutil", proxy), \
                    recovery.atomic_package_writes(self.core), self.assertRaisesRegex(RuntimeError, "verification"):
                installer.shutil.copytree(source, target, symlinks=True)
            self.assertFalse(target.exists())
        self.assertEqual(len(list((self.core / ".mcu-package-staging").iterdir())), 2)

    @unittest.skipIf(sys.platform == "win32", "Native Windows copy2 retains its complete metadata contract")
    def test_source_content_change_during_streaming_is_rejected(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source, target = self.copy_fixture()
        original_open = Path.open
        changed = []

        class MutatingReader:
            def __init__(self, stream):
                self.stream = stream
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def read(self, count=-1):
                data = self.stream.read(count)
                if data and not changed:
                    changed.append(True)
                    with original_open(source / "payload", "ab") as writer:
                        writer.write(b"late source write")
                return data

        def observed_open(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            return MutatingReader(stream) if path == source / "payload" and mode == "rb" else stream

        with patch.object(Path, "open", observed_open), recovery.atomic_package_writes(self.core), \
                self.assertRaisesRegex((RuntimeError, shutil.Error), "source changed"):
            installer.shutil.copytree(source, target, symlinks=True)
        self.assertTrue(changed)
        self.assertFalse(target.exists())

    @unittest.skipIf(sys.platform == "win32", "Native Windows retains original copy+hash behavior")
    def test_added_removed_or_replaced_source_entries_are_rejected_after_copy(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source, target = self.copy_fixture()
        def replace_payload(src):
            replacement = src / "replacement"
            replacement.write_bytes((src / "payload").read_bytes())
            os.replace(replacement, src / "payload")
        mutations = (lambda src: (src / "added").write_text("late"),
                     lambda src: (src / "payload").unlink(), replace_payload)
        for number, mutate in enumerate(mutations):
            if number:
                shutil.rmtree(source)
                source = self.package(parent=str(source.parent.relative_to(self.core)))
            proxy = self.copy_proxy(after=lambda src, stage: mutate(src))
            with self.subTest(mutation=number), patch.object(installer, "shutil", proxy), \
                    recovery.atomic_package_writes(self.core), self.assertRaisesRegex(RuntimeError, "source changed"):
                installer.shutil.copytree(source, target, symlinks=True)
            self.assertFalse(target.exists())

    @unittest.skipIf(sys.platform == "win32", "Native Windows retains original copy+hash behavior")
    def test_source_change_during_stage_verification_is_rejected_before_publication(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source, target = self.copy_fixture()
        original = recovery._tree_signature
        def during_verification(folder, **kwargs):
            result = original(folder, **kwargs)
            (source / "late-entry").write_text("late source edit")
            return result
        with patch.object(recovery, "_tree_signature", during_verification), \
                recovery.atomic_package_writes(self.core), self.assertRaisesRegex(RuntimeError, "before publication"):
            installer.shutil.copytree(source, target, symlinks=True)
        self.assertFalse(target.exists())
        self.assertTrue(list((self.core / ".mcu-package-staging").iterdir()))

    @unittest.skipIf(sys.platform == "win32", "Native Windows copy2 retains its complete metadata contract")
    def test_interrupted_stream_write_retains_stage_and_never_publishes(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source, target = self.copy_fixture()
        original_open = Path.open
        class InterruptedWriter:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def write(self, data):
                self.stream.write(data[:2])
                raise OSError("fixture interrupted stream write")
        def interrupted(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            return InterruptedWriter(stream) if mode == "xb" and path.name == "payload" else stream
        with patch.object(Path, "open", interrupted), recovery.atomic_package_writes(self.core), \
                self.assertRaisesRegex((OSError, shutil.Error), "interrupted stream write"):
            installer.shutil.copytree(source, target, symlinks=True)
        self.assertFalse(target.exists())
        self.assertEqual(next((self.core / ".mcu-package-staging").glob("*/payload")).read_bytes(), b"or")

    def test_custom_keyword_and_positional_copy_functions_keep_legacy_verification(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source, target = self.copy_fixture()
        calls = []
        def custom(src, dst):
            calls.append((str(src), str(dst)))
            return shutil.copy2(src, dst)
        for positional in (False, True):
            with self.subTest(positional=positional), \
                    patch.object(recovery, "_tree_signature", wraps=recovery._tree_signature) as signature, \
                    recovery.atomic_package_writes(self.core):
                if positional:
                    installer.shutil.copytree(source, target, True, None, custom)
                else:
                    installer.shutil.copytree(source, target, symlinks=True, copy_function=custom)
                self.assertEqual(signature.call_count, 2)
                self.assertEqual(Path(signature.call_args_list[0].args[0]), source)
            shutil.rmtree(target)
        self.assertEqual(len(calls), 6)

    @unittest.skipIf(sys.platform == "win32", "Native Windows copy2 retains its complete metadata contract")
    def test_captured_archive_proxy_and_extended_stream_paths_are_preserved(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        from src.modules import bootstrap_platformio
        source, target = self.copy_fixture()
        proxy_calls, extended = [], []
        class ArchiveProxy:
            def __getattr__(self, name):
                return getattr(shutil, name)
            def copytree(self, src, dst, *args, **kwargs):
                proxy_calls.append((src, dst, kwargs.get("copy_function")))
                return shutil.copytree(src, dst, *args, **kwargs)
        native_path = bootstrap_platformio.extended_windows_path
        def observed(path):
            extended.append(Path(path))
            return native_path(path)
        with patch.object(installer, "shutil", ArchiveProxy()), \
                patch.object(bootstrap_platformio, "extended_windows_path", observed), \
                recovery.atomic_package_writes(self.core):
            installer.shutil.copytree(source, target, symlinks=True)
        self.assertEqual(len(proxy_calls), 1)
        self.assertTrue(callable(proxy_calls[0][2]))
        self.assertIn(source / "payload", extended)
        self.assertTrue(any(path.name == "payload" and path.is_relative_to(self.core / ".mcu-package-staging")
                            for path in extended))
        self.assertEqual((target / "payload").read_bytes(), (source / "payload").read_bytes())

    def test_windows_keeps_native_copy2_and_full_verification_without_global_shutil_changes(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source, target = self.copy_fixture()
        calls = []
        class NativeProxy:
            def __getattr__(self, name):
                return getattr(shutil, name)
            def copytree(self, src, dst, *args, **kwargs):
                calls.append(kwargs)
                return shutil.copytree(src, dst, *args, **kwargs)
        original = shutil.copytree
        with patch.object(installer, "shutil", NativeProxy()), \
                patch.object(recovery, "sys", SimpleNamespace(platform="win32")), \
                patch.object(recovery, "_tree_signature", wraps=recovery._tree_signature) as signature, \
                recovery.atomic_package_writes(self.core):
            installer.shutil.copytree(source, target, symlinks=True)
        self.assertEqual(calls, [{"symlinks": True}])
        self.assertEqual(signature.call_count, 2)
        self.assertIs(shutil.copytree, original)
        self.assertEqual((target / "payload").read_bytes(), (source / "payload").read_bytes())

    @unittest.skipIf(sys.platform == "win32", "Native Windows always retains CopyFile2")
    def test_small_payload_keeps_native_copier_and_two_hashes_at_production_threshold(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source, target = self.copy_fixture()
        with patch.multiple(recovery, STREAM_MIN_BYTES=self.thresholds[0], STREAM_MIN_AVERAGE=self.thresholds[1]), \
                patch.object(recovery, "_tree_signature", wraps=recovery._tree_signature) as signature, \
                patch.object(recovery, "_source_inventory", wraps=recovery._source_inventory) as inventory, \
                recovery.atomic_package_writes(self.core):
            installer.shutil.copytree(source, target, symlinks=True)
        self.assertEqual(signature.call_count, 2)
        self.assertEqual(inventory.call_count, 1, "Fallback must not add repeated source walks")

    @unittest.skipIf(sys.platform == "win32", "Native Windows always retains CopyFile2")
    def test_streaming_requires_both_total_payload_and_average_size_thresholds(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source = self.core / ".cache/tmp/pkg-installing-threshold"
        source.mkdir(parents=True)
        for number in range(4):
            (source / str(number)).write_bytes(b"12345678")
        target = self.core / "packages/demo"
        target.parent.mkdir()
        for minimum, average, hashes in ((32, 9, 2), (33, 8, 2), (32, 8, 1)):
            with self.subTest(minimum=minimum, average=average), \
                    patch.multiple(recovery, STREAM_MIN_BYTES=minimum, STREAM_MIN_AVERAGE=average), \
                    patch.object(recovery, "_tree_signature", wraps=recovery._tree_signature) as signature, \
                    recovery.atomic_package_writes(self.core):
                installer.shutil.copytree(source, target, symlinks=True)
                self.assertEqual(signature.call_count, hashes)
            shutil.rmtree(target)

    @unittest.skipIf(sys.platform == "win32", "Native Windows retains CopyFile2 without POSIX fsync")
    def test_staged_files_and_directories_are_synchronized_before_publication(self):
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        source, target = self.copy_fixture()
        (source / "directory").mkdir()
        (source / "directory/nested").write_bytes(b"nested")
        synchronized = set()
        original_sync = os.fsync
        def observed_sync(descriptor):
            info = os.fstat(descriptor)
            synchronized.add((info.st_dev, info.st_ino))
            return original_sync(descriptor)
        original_publish = recovery._publish_absent
        def before_publish(stage, destination):
            for path in [stage, *stage.rglob("*")]:
                info = path.stat()
                self.assertIn((info.st_dev, info.st_ino), synchronized)
            return original_publish(stage, destination)
        with patch.object(recovery.os, "fsync", observed_sync), \
                patch.object(recovery, "_publish_absent", before_publish), recovery.atomic_package_writes(self.core):
            installer.shutil.copytree(source, target, symlinks=True)

    @unittest.skipUnless(sys.platform == "win32", "Requires native Windows file attributes and streams")
    def test_native_windows_archive_proxy_matches_native_copier_metadata_and_streams(self):
        import ctypes
        recovery = self.recovery()
        from platformio.package.manager import _install as installer
        from src.modules.bootstrap_platformio import archive_paths, extended_windows_path
        source, target = self.copy_fixture()
        payload = source / "payload"
        stream_name = extended_windows_path(payload) + ":fixture-stream"
        try:
            with open(stream_name, "wb") as stream:
                stream.write(b"retained alternate stream")
            with open(stream_name, "rb") as stream:
                self.assertEqual(stream.read(), b"retained alternate stream")
        except OSError as exc:
            self.skipTest(f"This fixture filesystem does not support alternate streams: {exc}")
        extra = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM | stat.FILE_ATTRIBUTE_READONLY
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        api.SetFileAttributesW.restype = ctypes.c_int
        self.assertTrue(api.SetFileAttributesW(extended_windows_path(payload), extra))
        # Python 3.11 uses copyfile+copystat; newer interpreters can use native
        # CopyFile2 and preserve more Windows metadata/streams. Match the real
        # unwrapped copier on this exact host instead of assuming one version.
        baseline = self.core / "native-copy-baseline"
        shutil.copytree(extended_windows_path(source), extended_windows_path(baseline), symlinks=True)

        def alternate_stream(folder):
            try:
                with open(extended_windows_path(folder / "payload") + ":fixture-stream", "rb") as stream:
                    return stream.read()
            except FileNotFoundError:
                return None

        baseline_info = (baseline / "payload").stat()
        baseline_stream = alternate_stream(baseline)
        with patch.dict(os.environ, {"PLATFORMIO_CORE_DIR": str(self.core)}), \
                archive_paths(), recovery.atomic_package_writes(self.core):
            installer.shutil.copytree(source, target, symlinks=True)
        actual_info = (target / "payload").stat()
        self.assertEqual(actual_info.st_file_attributes, baseline_info.st_file_attributes)
        self.assertEqual(stat.S_IMODE(actual_info.st_mode), stat.S_IMODE(baseline_info.st_mode))
        self.assertEqual(actual_info.st_mtime_ns, baseline_info.st_mtime_ns)
        self.assertEqual(alternate_stream(target), baseline_stream)
        if hasattr(getattr(shutil, "_winapi", None), "CopyFile2"):
            self.assertEqual(actual_info.st_file_attributes & extra, extra)
            self.assertEqual(baseline_stream, b"retained alternate stream")


if __name__ == "__main__":
    unittest.main(verbosity=2)
