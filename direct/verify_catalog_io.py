"""Hardware-free catalog I/O counts, no-op writes and exact-identity freshness.

All paths and caches are isolated in temp/. No live settings, metadata, packages
or network requests are touched by these fixtures.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from collections import Counter, OrderedDict
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
_IMPORT_FIXTURES = ROOT / "temp/audit/catalog-io"
_IMPORT_FIXTURES.mkdir(parents=True, exist_ok=True)
# Import-time discovery normally reads the user's saved catalog. Point that
# initial read at an empty fixture too, before per-check mocks are available.
with tempfile.TemporaryDirectory(dir=_IMPORT_FIXTURES) as _import_fixture, \
        patch("src.modules.platform_runtime.app_cache_dir", return_value=Path(_import_fixture)):
    from main.core import board_catalog as module


class CatalogIOChecks(unittest.TestCase):
    def setUp(self):
        audit = ROOT / "temp/audit/catalog-io"
        audit.mkdir(parents=True, exist_ok=True)
        self.fixture = tempfile.TemporaryDirectory(dir=audit)
        self.addCleanup(self.fixture.cleanup)
        self.directory = Path(self.fixture.name)
        self.core = self.directory / "core"
        self.manifests = self.core / "platforms/exact-platform/boards"
        self.manifests.mkdir(parents=True)
        self.downloads = self.directory / "arduino"
        self.downloads.mkdir()
        self.cache = self.directory / "catalog.json"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (
                ("_PIO_BOARD_CATALOG_RAM_CACHE", {}), ("_PIO_MANIFEST_RAM_CACHE", OrderedDict()),
                ("_PIO_MANIFEST_RAM_BYTES", 0), ("_ARDUINO_BOARDS_TXT_RAM_CACHE", OrderedDict()),
                ("_ARDUINO_BOARDS_TXT_RAM_BYTES", 0), ("_BOARD_CATALOG_CACHE_RAM", None),
                ("_BOARD_CATALOG_CACHE_RAM_PATH", None), ("_BOARD_CATALOG_PARSE_GENERATION", 0)):
            self.stack.enter_context(patch.object(module, name, value))
        self.stack.enter_context(patch.object(module, "_board_catalog_cache_path", return_value=self.cache))
        self.stack.enter_context(patch.object(module, "_get_safe_platformio_core_dir", return_value=self.core))
        self.stack.enter_context(patch.object(module, "_get_arduino_board_search_roots", return_value=[self.downloads]))

    def manifest(self, name, *, mcu="mcu-old", protocol="exact-programmer", require_port=False):
        path = self.manifests / f"{name}.json"
        path.write_text(json.dumps({
            "name": f"Exact target {name}", "frameworks": ["arduino", "native-sdk"],
            "build": {"mcu": mcu, "extra_flags": f"-DARDUINO_{name.upper()}"},
            "upload": {"protocol": protocol, "require_upload_port": require_port, "speed": 57600},
        }), encoding="utf-8")
        return path

    def test_warm_inventory_one_pass_and_changed_manifest_only_one_read(self):
        paths = [self.manifest(f"board{number}") for number in range(40)]
        cold = module._load_platformio_board_catalog(self.core)
        reads, scans = Counter(), Counter()
        original_read, original_scan = Path.read_text, os.scandir
        def read(path, *args, **kwargs):
            reads[str(path)] += 1
            return original_read(path, *args, **kwargs)
        def scan(path):
            scans[str(path)] += 1
            return original_scan(path)
        with patch.object(Path, "read_text", read), patch.object(os, "scandir", scan):
            warm = module._load_platformio_board_catalog(self.core)
        self.assertEqual(warm, cold)
        self.assertEqual(reads, {})
        self.assertEqual(scans, {str(self.core / "boards"): 1, str(self.core / "platforms"): 1,
                                 str(self.manifests): 1})
        self.manifest("board7", mcu="mcu-new", protocol="updated-protocol", require_port=True)
        reads.clear()
        with patch.object(Path, "read_text", read):
            changed = module._load_platformio_board_catalog(self.core)
        self.assertEqual(reads, {str(paths[7]): 1})
        record = next(record for record in changed if record["id"] == "board7")
        self.assertEqual((record["platform"], record["mcu"], record["upload_protocol"], record["require_upload_port"]),
                         ("exact-platform", "mcu-new", "updated-protocol", True))
        self.assertEqual(record["frameworks"], {"arduino", "native-sdk"})
        print("Catalog I/O fixture: warm=3 inventory passes/0 manifest reads; changed=1 manifest read")

    def test_replacement_preserving_size_and_mtime_is_not_stale(self):
        path = self.manifest("exact")
        before = module._load_platformio_board_catalog(self.core)
        stamp = path.stat()
        replacement = path.with_suffix(".replacement")
        replacement.write_text(path.read_text().replace("mcu-old", "mcu-new"), encoding="utf-8")
        os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        os.replace(replacement, path)
        after = module._load_platformio_board_catalog(self.core)
        self.assertEqual(path.stat().st_size, stamp.st_size)
        self.assertEqual(path.stat().st_mtime_ns, stamp.st_mtime_ns)
        self.assertEqual(before[0]["mcu"], "mcu-old")
        self.assertEqual(after[0]["mcu"], "mcu-new")

    def test_add_remove_invalid_manifest_and_transient_read_error_refresh(self):
        first = self.manifest("first")
        self.assertEqual([row["id"] for row in module._load_platformio_board_catalog(self.core)], ["first"])
        second = self.manifest("second")
        self.assertEqual({row["id"] for row in module._load_platformio_board_catalog(self.core)}, {"first", "second"})
        first.unlink()
        self.assertEqual([row["id"] for row in module._load_platformio_board_catalog(self.core)], ["second"])
        # Invalidate the full-catalog cache, then lose a manifest read once.
        module._PIO_BOARD_CATALOG_RAM_CACHE.clear()
        module._PIO_MANIFEST_RAM_CACHE.clear()
        module._PIO_MANIFEST_RAM_BYTES = 0
        original = Path.read_text
        def unavailable(path, *args, **kwargs):
            if path == second:
                raise PermissionError("fixture transient read failure")
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", unavailable):
            self.assertEqual(module._load_platformio_board_catalog(self.core), [])
        self.assertEqual([row["id"] for row in module._load_platformio_board_catalog(self.core)], ["second"])

    def test_same_path_symlink_retarget_uses_new_exact_manifest(self):
        first, second = self.directory / "first.json", self.directory / "second.json"
        first.write_text(json.dumps({"name": "First exact", "frameworks": ["arduino"],
                                    "build": {"mcu": "mcu-old"}}))
        second.write_text(json.dumps({"name": "Other exact", "frameworks": ["arduino"],
                                     "build": {"mcu": "mcu-new"}}))
        link = self.manifests / "exact-id.json"
        try:
            link.symlink_to(first)
        except OSError as exc:
            self.skipTest(f"Native symlinks unavailable: {exc}")
        before = module._load_platformio_board_catalog(self.core)
        link.unlink()
        link.symlink_to(second)
        after = module._load_platformio_board_catalog(self.core)
        self.assertEqual((before[0]["id"], after[0]["id"]), ("exact-id", "exact-id"))
        self.assertEqual((before[0]["mcu"], after[0]["mcu"]), ("mcu-old", "mcu-new"))
        self.assertEqual(after[0]["manifest"], str(link), "Retain the app-owned short alias spelling")

    def test_mocked_alias_retarget_preserves_short_path_and_exact_identity(self):
        # Windows may deny native symlinks without Developer Mode. Exercise the
        # same followed-DirEntry metadata contract on every host, without any
        # privileged link creation or changes outside this fixture.
        first = self.manifest("first", mcu="mcu-old", protocol="first-protocol")
        second = self.manifest("second", mcu="mcu-new", protocol="other-protocol", require_port=True)
        alias = self.manifests / "exact-id.json"
        target = [first]
        original_scan, original_read = os.scandir, Path.read_text

        class AliasEntry:
            name, path = alias.name, str(alias)
            def is_file(self):
                return target[0].is_file()
            def stat(self):
                return target[0].stat()

        class AliasScan:
            def __enter__(self):
                return iter([AliasEntry()])
            def __exit__(self, *_args):
                return False

        def scan(path):
            return AliasScan() if Path(path) == self.manifests else original_scan(path)

        def read(path, *args, **kwargs):
            return original_read(target[0] if path == alias else path, *args, **kwargs)

        with patch.object(os, "scandir", scan), patch.object(Path, "read_text", read):
            before = module._load_platformio_board_catalog(self.core)
            target[0] = second
            after = module._load_platformio_board_catalog(self.core)
        self.assertEqual((before[0]["id"], after[0]["id"]), ("exact-id", "exact-id"))
        self.assertEqual((before[0]["mcu"], after[0]["mcu"]), ("mcu-old", "mcu-new"))
        self.assertEqual(after[0]["manifest"], str(alias))
        self.assertEqual((after[0]["platform"], after[0]["upload_protocol"], after[0]["require_upload_port"]),
                         ("exact-platform", "other-protocol", True))
        self.assertEqual(after[0]["frameworks"], {"arduino", "native-sdk"})

    def test_usb_inventory_reuses_boards_parser_without_losing_shared_ids(self):
        source = self.downloads / "boards.txt"
        source.write_text("exact.name=Exact\nexact.build.mcu=mcu-old\n"
                          "exact.vid.0=0x1234\nexact.pid.0=0x5678\n"
                          "other.name=Other\nother.upload_port.vid.0=0x1234\nother.upload_port.pid.0=0x5678\n")
        module._parse_downloaded_arduino_board_files(self.downloads)
        catalog = {"Exact": {"arduino_board_id": "exact"}, "Exact alias": {"arduino_board_id": "exact"},
                   "Other": {"arduino_board_id": "other"}}
        reads = []
        original = Path.read_text
        def read(path, *args, **kwargs):
            reads.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, "read_text", read):
            pairs = module.load_downloaded_board_usb_ids(catalog)
        self.assertEqual(reads, [])
        self.assertEqual(pairs[(0x1234, 0x5678)], ("Exact", "Exact alias", "Other"))
        stamp = source.stat()
        source.write_text(source.read_text().replace("0x5678", "0x9999"))
        # A normal metadata-observable edit; timestamp-preserved edits exercise
        # explicit invalidation below instead of depending on host clock ticks.
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))
        self.assertIn((0x1234, 0x9999), module.load_downloaded_board_usb_ids(catalog))

    def test_explicit_refresh_forces_current_pio_arduino_and_prepared_bytes(self):
        manifest = self.manifest("exact")
        source = self.downloads / "boards.txt"
        source.write_text("exact.name=Exact Arduino\nexact.build.mcu=mcu-old\n"
                          "exact.vid.0=0x1234\nexact.pid.0=0x5678\n")
        snapshot = self.core / ".mcu-offline-catalog.json"
        snapshot.write_text("[]")
        old = module.load_dynamic_boards({})
        identities = {str(path): module._manifest_stat_identity(path.stat())
                      for path in (manifest, source, snapshot)}
        stamps = {path: path.stat() for path in (manifest, source, snapshot)}
        manifest.write_text(manifest.read_text().replace("mcu-old", "mcu-new"))
        source.write_text(source.read_text().replace("mcu-old", "mcu-new").replace("0x5678", "0x9999"))
        for path, stamp in stamps.items():
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        original_identity = module._manifest_stat_identity
        reads = Counter()
        original_read = Path.read_text
        def read(path, *args, **kwargs):
            reads[str(path)] += 1
            return original_read(path, *args, **kwargs)
        # Model a filesystem retaining the same complete metadata identity; the
        # forced path must bypass both whole-catalog and per-file RAM records.
        def retained_identity(stat):
            current = original_identity(stat)
            return next((identity for identity in identities.values()
                         if identity[3:] == current[3:]), current)
        with patch.object(module, "_manifest_stat_identity", retained_identity), \
                patch.object(Path, "read_text", read), \
                patch.object(module, "_prepared_framework_overlay", wraps=module._prepared_framework_overlay) as prepared:
            fresh = module.load_dynamic_boards({}, prefer_cache=True, invalidate_parsed=True)
        self.assertTrue(any(info["mcu"] == "mcu-old" for info in old.values()))
        self.assertTrue(fresh)
        self.assertTrue(all(info["mcu"] == "mcu-new" for info in fresh.values()))
        self.assertEqual(reads[str(manifest)], 1)
        self.assertEqual(reads[str(source)], 1)
        prepared.assert_called_once_with(self.core)
        self.assertIn((0x1234, 0x9999), module.load_downloaded_board_usb_ids(fresh))

    def test_noop_explicit_refresh_does_not_rewrite_disk_snapshot(self):
        self.manifest("exact")
        before = module.load_dynamic_boards({})
        with patch.object(module.os, "replace", wraps=os.replace) as replace:
            after = module.load_dynamic_boards({}, invalidate_parsed=True)
        self.assertEqual(after, before)
        replace.assert_not_called()

    def test_reader_predating_invalidation_does_not_refill_stale_ram(self):
        manifest = self.manifest("exact")
        source = self.downloads / "boards.txt"
        source.write_text("exact.name=Exact\n")
        original_read = Path.read_text
        for operation in (
                lambda: module._read_platformio_manifest(manifest, module._manifest_stat_identity(manifest.stat())),
                lambda: module._parse_downloaded_arduino_board_files(self.downloads),
                lambda: module._load_platformio_board_catalog(self.core)):
            module._invalidate_parsed_board_catalogs()
            def read(path, *args, **kwargs):
                result = original_read(path, *args, **kwargs)
                module._invalidate_parsed_board_catalogs()
                return result
            with patch.object(Path, "read_text", read):
                self.assertTrue(operation())
            self.assertEqual(module._PIO_MANIFEST_RAM_CACHE, {})
            self.assertEqual(module._PIO_BOARD_CATALOG_RAM_CACHE, {})
            self.assertEqual(module._ARDUINO_BOARDS_TXT_RAM_CACHE, {})
            self.assertEqual(module._PIO_MANIFEST_RAM_BYTES, 0)
            self.assertEqual(module._ARDUINO_BOARDS_TXT_RAM_BYTES, 0)

    def test_noop_save_and_publication_have_no_replacement_or_revision_churn(self):
        boards = {"Exact": {"board": "exact", "frameworks": ["arduino"]}}
        module._save_board_catalog_cache(boards)
        before = self.cache.stat().st_mtime_ns
        with patch.object(module.os, "replace", wraps=os.replace) as replace:
            for _ in range(20):
                module._save_board_catalog_cache(boards)
        replace.assert_not_called()
        self.assertEqual(self.cache.stat().st_mtime_ns, before)
        catalog = module.BoardCatalog(boards)
        revision, _ = catalog.snapshot()
        self.assertFalse(catalog.replace(boards))
        self.assertFalse(catalog.set_definition("Exact", boards["Exact"]))
        self.assertEqual(catalog.snapshot()[0], revision)
        changed = {"Exact": {"board": "exact", "frameworks": ["native-sdk"]}}
        self.assertTrue(catalog.replace(changed))
        self.assertEqual(catalog.snapshot()[0], revision + 1)

    def test_disk_snapshot_atomic_replacement_invalidates_ram_with_coarse_timestamp(self):
        boards = {"Exact": {"board": "old", "hwids": set(), "arduino_defines": set()}}
        module._save_board_catalog_cache(boards)
        self.assertEqual(module._load_board_catalog_cache()["Exact"]["board"], "old")
        stamp = self.cache.stat()
        replacement = self.cache.with_suffix(".replacement")
        replacement.write_text(self.cache.read_text().replace('"old"', '"new"'))
        os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        os.replace(replacement, self.cache)
        self.assertEqual(module._load_board_catalog_cache()["Exact"]["board"], "new")

    def test_failed_atomic_save_preserves_old_file_and_ram(self):
        old = {"Exact": {"board": "old", "hwids": set(), "arduino_defines": set()}}
        module._save_board_catalog_cache(old)
        original_bytes = self.cache.read_bytes()
        with patch.object(module.os, "replace", side_effect=OSError("fixture save failure")):
            module._save_board_catalog_cache({"Exact": dict(old["Exact"], board="new")})
        self.assertEqual(self.cache.read_bytes(), original_bytes)
        self.assertEqual(module._load_board_catalog_cache()["Exact"]["board"], "old")
        self.assertEqual(list(self.cache.parent.glob("catalog.json.tmp-*")), [])

    def test_cold_snapshot_read_during_replacement_is_not_certified(self):
        module._save_board_catalog_cache({"Exact": {"board": "old", "hwids": set(), "arduino_defines": set()}})
        module._BOARD_CATALOG_CACHE_RAM = None
        original_read = Path.read_text
        def changed_during_read(path, *args, **kwargs):
            if path == self.cache:
                replacement = self.cache.with_suffix(".replacement")
                replacement.write_text(original_read(path).replace('"old"', '"new"'))
                os.replace(replacement, self.cache)
            return original_read(path, *args, **kwargs)
        with patch.object(Path, "read_text", changed_during_read):
            self.assertIsNone(module._load_board_catalog_cache())
        self.assertIsNone(module._BOARD_CATALOG_CACHE_RAM)
        self.assertEqual(module._load_board_catalog_cache()["Exact"]["board"], "new")

    def test_racing_replacement_is_not_cached_under_the_other_writers_identity(self):
        old = {"Exact": {"board": "old", "hwids": set(), "arduino_defines": set()}}
        other = {"Exact": dict(old["Exact"], board="new")}
        original_replace = os.replace
        def replaced_twice(source, destination):
            original_replace(source, destination)
            # Simulate another window finishing its save between our replace
            # and publication, without relying on scheduler timing.
            with patch.object(module.os, "replace", original_replace):
                module._save_board_catalog_cache(other)
        with patch.object(module.os, "replace", replaced_twice):
            module._save_board_catalog_cache(old)
        self.assertEqual(module._load_board_catalog_cache()["Exact"]["board"], "new")

    def test_save_snapshots_nested_caller_metadata_before_io(self):
        boards = {"Exact": {"board": "exact", "frameworks": ["arduino"],
                            "hwids": set(), "arduino_defines": set()}}
        original_replace = os.replace
        def mutate_caller(source, destination):
            original_replace(source, destination)
            boards["Exact"]["frameworks"][0] = "native-sdk"
        with patch.object(module.os, "replace", mutate_caller):
            module._save_board_catalog_cache(boards)
        self.assertEqual(module._load_board_catalog_cache()["Exact"]["frameworks"], ["arduino"])

    def test_prepared_snapshot_replacement_and_provider_replacement_invalidate(self):
        snapshot = self.core / ".mcu-offline-catalog.json"
        snapshot.write_text("[]")
        before = module._prepared_catalog_fingerprint(self.core)
        stamp = snapshot.stat()
        replacement = snapshot.with_suffix(".replacement")
        replacement.write_text("{}")
        os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        os.replace(replacement, snapshot)
        self.assertNotEqual(module._prepared_catalog_fingerprint(self.core), before)
        provider = self.core / "packages/framework-mbed/package.json"
        provider.parent.mkdir(parents=True)
        provider.write_text('{"version": "old"}')
        before = module._prepared_catalog_fingerprint(self.core)
        stamp = provider.stat()
        replacement = provider.with_suffix(".replacement")
        replacement.write_text('{"version": "new"}')
        os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        os.replace(replacement, provider)
        self.assertNotEqual(module._prepared_catalog_fingerprint(self.core), before)

    def test_caller_nested_mutations_do_not_poison_catalog_or_boards_parser(self):
        self.manifest("exact")
        first = module._load_platformio_board_catalog(self.core)
        first[0]["frameworks"].add("bad")
        self.assertNotIn("bad", module._load_platformio_board_catalog(self.core)[0]["frameworks"])
        (self.downloads / "boards.txt").write_text("exact.name=Exact\nexact.vid.0=0x1234\nexact.pid.0=0x5678\n")
        first = module._parse_downloaded_arduino_board_files(self.downloads)
        first[0]["hwids"].clear()
        first[0]["properties"]["name"] = "changed"
        second = module._parse_downloaded_arduino_board_files(self.downloads)
        self.assertEqual(second[0]["properties"]["name"], "Exact")
        self.assertEqual(second[0]["hwids"], {(0x1234, 0x5678)})

    def test_parsed_metadata_caches_are_bounded_and_removed_files_not_returned(self):
        paths = [self.manifest(f"board{number}") for number in range(4)]
        with patch.object(module, "_PIO_MANIFEST_RAM_MAX_FILES", 2):
            self.assertEqual(len(module._load_platformio_board_catalog(self.core)), 4)
        self.assertLessEqual(len(module._PIO_MANIFEST_RAM_CACHE), 2)
        self.assertEqual(module._PIO_MANIFEST_RAM_BYTES, sum(row[2] for row in module._PIO_MANIFEST_RAM_CACHE.values()))
        with patch.object(module, "_PIO_MANIFEST_RAM_MAX_BYTES", 1):
            module._read_platformio_manifest(paths[0], module._manifest_stat_identity(paths[0].stat()))
        self.assertEqual(module._PIO_MANIFEST_RAM_BYTES, 0)
        paths[0].unlink()
        self.assertNotIn("board0", {row["id"] for row in module._load_platformio_board_catalog(self.core)})

        for number in range(4):
            directory = self.downloads / f"core{number}"
            directory.mkdir()
            (directory / "boards.txt").write_text(f"board{number}.name=Exact {number}\n")
        with patch.object(module, "_ARDUINO_BOARDS_TXT_RAM_MAX_FILES", 2):
            self.assertEqual(len(module._parse_downloaded_arduino_board_files(self.downloads)), 4)
        self.assertLessEqual(len(module._ARDUINO_BOARDS_TXT_RAM_CACHE), 2)
        self.assertEqual(module._ARDUINO_BOARDS_TXT_RAM_BYTES,
                         sum(row[2] for row in module._ARDUINO_BOARDS_TXT_RAM_CACHE.values()))
        with patch.object(module, "_ARDUINO_BOARDS_TXT_RAM_MAX_BYTES", 1):
            module._parse_downloaded_arduino_board_files(self.downloads)
        self.assertEqual(module._ARDUINO_BOARDS_TXT_RAM_BYTES, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
