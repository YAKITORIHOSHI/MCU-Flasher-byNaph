#!/usr/bin/env python3
"""Isolated Arduino library visibility, input bytes and firmware reuse checks."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from direct import verify_arduino_fallback as fixtures
from main.core import arduino_backend, arduino_inputs


class LibraryInputChecks(unittest.TestCase):
    setUp = fixtures.FallbackChecks.setUp
    ready = fixtures.FallbackChecks.ready
    record = fixtures.FallbackChecks.record
    info = fixtures.FallbackChecks.info
    api = fixtures.FallbackChecks.api

    def library(self, root=None, name="Servo"):
        root = root or self.core / "lib"
        folder = root / name
        (folder / "src").mkdir(parents=True)
        (folder / "library.properties").write_text(f"name={name}\nversion=1.0.0\n", encoding="utf-8")
        header = folder / f"src/{name}.h"
        header.write_bytes(b"const int value = 1;\n")
        (folder / f"src/{name}.cpp").write_bytes(b"int example = 1;\n")
        return header

    def used_dependencies(self, paths):
        original = self.stream.side_effect
        def stream(owner, command, environment, cwd, *, upload=False):
            original(owner, command, environment, cwd, upload=upload)
            if not upload:
                build = Path(command[command.index("--build-path") + 1])
                escaped = [str(path).replace("\\", "/").replace(" ", "\\ ") for path in paths]
                (build / "library.d").write_text("library.o: " + " ".join(escaped) + "\n", encoding="utf-8")
        self.stream.side_effect = stream

    def compile_then_upload(self, api):
        self.assertTrue(arduino_backend.run_arduino_operation(api))
        api._active_skip_compile = True
        self.operations.clear()

    def test_both_library_collections_use_exact_app_store(self):
        api = self.api()
        downloaded = self.fixture / "libraries/Libs"
        self.library(downloaded, "Downloaded")
        self.library()
        foreign = self.fixture / "foreign"
        with patch.dict(os.environ, {"PLATFORMIO_CORE_DIR": str(foreign)}):
            self.assertTrue(arduino_backend.run_arduino_operation(api))
        command = self.operations[0][0]
        libraries = [command[index + 1] for index, flag in enumerate(command) if flag == "--libraries"]
        self.assertEqual(libraries, [str(downloaded), str(self.core / "lib")])
        self.assertNotIn(str(foreign / "lib"), command)
        self.assertFalse(any("install" in command or "update-index" in command for command, _ in self.operations))

    def test_validated_short_alias_preserves_flags_and_canonical_receipt_identity(self):
        api = self.api()
        self.library()
        config = self.core / "arduino-cli/arduino-cli.yaml"
        payload = json.loads(config.read_text())
        alias = self.fixture / "short"
        payload["directories"] = {name: str(alias / "arduino-cli" / name) for name in ("data", "downloads", "user")}
        fixtures.web_bridge.write_generated_text(config, json.dumps(payload))
        original_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            if path.is_relative_to(alias):
                path = self.core / path.relative_to(alias)
            return original_resolve(path, *args, **kwargs)
        info = api._resolve_board_info()
        command = [str(self.cli), "--config-file", str(config)]
        with patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            roots, flags = arduino_inputs.library_collections(command, info)
        self.assertEqual(flags, ["--libraries", str(alias / "lib")])
        self.assertIn((self.core / "lib").resolve(), roots)
        self.assertNotIn(alias / "lib", roots)
        # Per-version stores recover the same equivalent Bootstrap alias.
        relative = Path("arduino-cli/targets/verified-version")
        primary_info = {"arduino_cli": {"store": str(relative)}}
        primary_directories = {name: str(alias / relative / name) for name in ("data", "downloads", "user")}
        with patch.object(Path, "resolve", autospec=True, side_effect=resolve):
            spelling = arduino_inputs._prepared_core_spelling(self.core / relative / "arduino-cli.yaml",
                                                               primary_directories, primary_info, self.core)
        self.assertEqual(spelling, alias)

    def test_foreign_library_configuration_cannot_expose_unrelated_store(self):
        api = self.api()
        config = self.core / "arduino-cli/arduino-cli.yaml"
        payload = json.loads(config.read_text())
        payload["directories"]["data"] = str(self.fixture / "foreign/arduino-cli/data")
        fixtures.web_bridge.write_generated_text(config, json.dumps(payload))
        command = [str(self.cli), "--config-file", str(config)]
        with self.assertRaisesRegex(RuntimeError, "app-owned package store"):
            arduino_inputs.library_collections(command, api._resolve_board_info())

    def test_unchanged_selected_library_reuses_only_firmware(self):
        api = self.api()
        header = self.library()
        self.used_dependencies([header])
        self.compile_then_upload(api)
        self.assertTrue(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertEqual([upload for _, upload in self.operations], [True])

    def test_same_size_same_timestamp_header_change_rebuilds_cleanly(self):
        api = self.api()
        header = self.library()
        self.used_dependencies([header])
        self.compile_then_upload(api)
        stat = header.stat()
        header.write_bytes(header.read_bytes().replace(b"1", b"2"))
        os.utime(header, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(header.stat().st_size, stat.st_size)
        self.assertTrue(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertEqual([upload for _, upload in self.operations], [False, True])
        self.assertIn("--clean", self.operations[0][0])

    def test_selected_library_cpp_not_named_in_header_dependency_is_verified(self):
        api = self.api()
        header = self.library()
        self.used_dependencies([header])
        self.compile_then_upload(api)
        (header.parent / "Servo.cpp").write_bytes(b"int example = 2;\n")
        self.assertTrue(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertEqual([upload for _, upload in self.operations], [False, True])

    def test_unused_library_byte_edit_keeps_selected_build_reusable(self):
        api = self.api()
        header = self.library()
        unused = self.library(name="Unused")
        self.used_dependencies([header])
        self.compile_then_upload(api)
        unused.write_bytes(unused.read_bytes().replace(b"1", b"2"))
        self.assertTrue(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertEqual([upload for _, upload in self.operations], [True])

    def test_added_header_or_cpp_invalidates_discovery_and_reuse(self):
        for suffix in (".h", ".cpp"):
            with self.subTest(suffix=suffix):
                api = self.api()
                header = self.library(name="Library" + suffix[1:])
                self.used_dependencies([header])
                self.compile_then_upload(api)
                (header.parent / ("added" + suffix)).write_bytes(b"int additional = 2;\n")
                self.assertTrue(arduino_backend.run_arduino_operation(api, upload=True))
                self.assertEqual([upload for _, upload in self.operations], [False, True])
                self.assertIn("--clean", self.operations[0][0])

    def test_new_library_root_invalidates_prior_empty_collection(self):
        api = self.api()
        self.compile_then_upload(api)
        self.library()
        self.assertTrue(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertEqual([upload for _, upload in self.operations], [False, True])

    def test_library_changed_during_compile_blocks_receipt_and_upload(self):
        api = self.api()
        header = self.library()
        self.used_dependencies([header])
        original = self.stream.side_effect
        def stream(*args, **kwargs):
            original(*args, **kwargs)
            header.write_bytes(b"const int value = 2;\n")
        self.stream.side_effect = stream
        self.assertFalse(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertFalse(any(upload for _, upload in self.operations))
        self.assertEqual(list((self.fixture / "build-cache").rglob("build-receipt.json")), [])

    def test_library_changed_at_write_boundary_never_uploads(self):
        api = self.api()
        header = self.library()
        self.used_dependencies([header])
        self.compile_then_upload(api)
        original = arduino_backend.runtime_command
        calls = []
        def validate(info):
            result = original(info)
            calls.append(info)
            if len(calls) == 2:
                header.write_bytes(b"const int value = 2;\n")
            return result
        with patch.object(arduino_backend, "runtime_command", side_effect=validate):
            self.assertFalse(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertEqual(self.operations, [])
        api._stop_serial_monitor.assert_not_called()

    def test_implicit_cli_user_and_core_libraries_are_byte_checked(self):
        api = self.api()
        for root, name in ((self.core / "arduino-cli/user/libraries", "UserLibrary"),
                           (self.platform / "libraries", "CoreLibrary")):
            self.library(root, name)
        self.compile_then_upload(api)
        header = self.platform / "libraries/CoreLibrary/src/CoreLibrary.h"
        header.write_bytes(b"const int value = 2;\n")
        self.assertTrue(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertEqual([upload for _, upload in self.operations], [False, True])

    def test_selected_core_dependency_bytes_invalidate_old_firmware(self):
        api = self.api()
        dependency = self.platform / "cores/Core.h"
        dependency.parent.mkdir()
        dependency.write_bytes(b"const int coreValue = 1;\n")
        self.used_dependencies([dependency])
        self.compile_then_upload(api)
        dependency.write_bytes(b"const int coreValue = 2;\n")
        self.assertTrue(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertEqual([upload for _, upload in self.operations], [False, True])
        self.assertIn("--clean", self.operations[0][0])

    def test_source_change_preserves_incremental_objects_when_libraries_unchanged(self):
        api = self.api()
        header = self.library()
        self.used_dependencies([header])
        self.compile_then_upload(api)
        (api.sketch_dir_path / "sketch.ino").write_text("void setup(){int x=1;} void loop(){}\n", encoding="utf-8")
        self.assertTrue(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertNotIn("--clean", self.operations[0][0])

    def test_previously_selected_core_dependency_change_during_build_blocks_upload(self):
        api = self.api()
        dependency = self.platform / "cores/Core.h"
        dependency.parent.mkdir()
        dependency.write_bytes(b"const int coreValue = 1;\n")
        self.used_dependencies([dependency])
        self.assertTrue(arduino_backend.run_arduino_operation(api))
        original = self.stream.side_effect
        def stream(*args, **kwargs):
            original(*args, **kwargs)
            dependency.write_bytes(b"const int coreValue = 2;\n")
        self.stream.side_effect = stream
        self.operations.clear()
        self.assertFalse(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertEqual([upload for _, upload in self.operations], [False])

    def test_previous_receipt_without_library_proof_cannot_skip_compile(self):
        api = self.api()
        self.compile_then_upload(api)
        path = next((self.fixture / "build-cache").rglob("build-receipt.json"))
        receipt = json.loads(path.read_text())
        receipt.pop("inputs")
        # Native Windows hidden-file writes use the same atomic helper as runtime.
        fixtures.web_bridge.write_generated_text(path, json.dumps(receipt))
        self.assertTrue(arduino_backend.run_arduino_operation(api, upload=True))
        self.assertEqual([upload for _, upload in self.operations], [False, True])
        self.assertIn("--clean", self.operations[0][0])

    def test_dependency_spaces_drive_paths_and_continuations(self):
        text = "C:/build/my\\ object.o: C:/library/my\\ header.h \\\n C:\\library\\source.cpp\n"
        self.assertEqual(arduino_inputs.dependency_tokens(text),
                         ["C:/library/my header.h", "C:\\library\\source.cpp"])

    def test_library_inventory_and_byte_work_are_bounded(self):
        header = self.library()
        with patch.object(arduino_inputs, "_MAX_FILES", 2):
            with self.assertRaisesRegex(RuntimeError, "bounded verification limit"):
                arduino_inputs.snapshot([header.parent.parent.parent])
        snapshot = arduino_inputs.snapshot([header.parent.parent.parent])
        with patch.object(arduino_inputs, "_MAX_BYTES", 1):
            self.assertFalse(arduino_inputs.matches(snapshot, snapshot))


if __name__ == "__main__":
    unittest.main(verbosity=2)
