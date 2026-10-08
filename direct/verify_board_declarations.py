#!/usr/bin/env python3
"""Isolated Arduino declaration/PlatformIO evidence checks, with no hardware.

All catalogs, manifests and persistence live in temporary fixtures. These
checks never install packages, query registries or alter live sketch state.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections import OrderedDict
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
AUDIT = ROOT / "temp/audit/board-declarations"
AUDIT.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(dir=AUDIT) as _import_directory, \
        patch("src.modules.platform_runtime.app_cache_dir", return_value=Path(_import_directory)):
    from main.core import board_catalog as catalog
    from src.modules import arduino_cli_support


class BoardDeclarationChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=AUDIT)))
        self.core = self.root / "store"
        self.board_dir = self.core / "platforms/espressif32/boards"
        self.board_dir.mkdir(parents=True)
        self.arduino = self.root / "arduino"
        self.arduino.mkdir()
        self.cache = self.root / "catalog.json"
        for name, value in (
                ("_PIO_BOARD_CATALOG_RAM_CACHE", {}), ("_PIO_MANIFEST_RAM_CACHE", OrderedDict()),
                ("_PIO_MANIFEST_RAM_BYTES", 0), ("_ARDUINO_BOARDS_TXT_RAM_CACHE", OrderedDict()),
                ("_ARDUINO_BOARDS_TXT_RAM_BYTES", 0), ("_BOARD_CATALOG_CACHE_RAM", None),
                ("_BOARD_CATALOG_CACHE_RAM_PATH", None), ("_BOARD_CATALOG_PARSE_GENERATION", 0)):
            self.stack.enter_context(patch.object(catalog, name, value))
        self.stack.enter_context(patch.object(catalog, "_board_catalog_cache_path", return_value=self.cache))
        self.stack.enter_context(patch.object(catalog, "_get_safe_platformio_core_dir", return_value=str(self.core)))
        self.stack.enter_context(patch.object(catalog, "_get_arduino_board_search_roots", return_value=[self.arduino]))
        self.stack.enter_context(patch.object(catalog, "_prepared_catalog_fingerprint", return_value=()))
        self.stack.enter_context(patch.object(arduino_cli_support, "_core_directory", return_value=self.core))

    def declarations(self, text):
        (self.arduino / "boards.txt").write_text(text, encoding="utf-8")
        return catalog._parse_downloaded_arduino_board_files(self.arduino, force_read=True)

    def manifest(self, identifier, *, name="Fixture", mcu="esp32s3", variant="",
                 flags=None, arduino_flags=None, flash="4MB", frameworks=None):
        build = {"mcu": mcu, "variant": variant, "extra_flags": flags or []}
        if arduino_flags is not None:
            build["arduino"] = {"extra_flags": arduino_flags}
        (self.board_dir / f"{identifier}.json").write_text(json.dumps({
            "name": name, "vendor": "Espressif", "build": build,
            "frameworks": frameworks or ["arduino", "espidf"],
            "upload": {"flash_size": flash, "require_upload_port": True, "speed": 460800},
        }), encoding="utf-8")

    def test_usb_declarations_accept_official_and_existing_layouts(self):
        records = self.declarations("""
original.name=Original USB layout
original.vid.0=0x303A
original.pid.0=0x1001
official.name=Official upload layout
official.upload_port.0.vid=0x239A
official.upload_port.0.pid=0x80AB
official.upload_port.1.vid=0x303A
official.upload_port.1.pid=0x1001
legacy.name=Legacy upload layout
legacy.upload_port.vid.0=0x1A86
legacy.upload_port.pid.0=0x7523
""")
        identities = {row["arduino_id"]: row["hwids"] for row in records}
        self.assertEqual(identities["original"], {(0x303A, 0x1001)})
        self.assertEqual(identities["official"], {(0x239A, 0x80AB), (0x303A, 0x1001)})
        self.assertEqual(identities["legacy"], {(0x1A86, 0x7523)})

    def test_incomplete_or_invalid_usb_declarations_do_not_invent_pairs(self):
        records = self.declarations("""
partial.name=Separate incomplete declarations
partial.vid.0=0x303A
partial.upload_port.0.pid=0x1001
partial.upload_port.1.vid=invalid
partial.upload_port.1.pid=0x80AB
partial.upload_port.2.vid=0x239A
partial.upload_port.3.pid=0x80AB
""")
        self.assertEqual(records[0]["hwids"], set())

    def test_hidden_discovery_entries_are_excluded_from_cold_and_warm_parse(self):
        records = self.declarations("""
esp32_family.name=ESP32 Family Device
esp32_family.hide=true
esp32_family.vid.0=0x303A
esp32_family.pid.0=0x1001
hidden_case.name=Hidden case variation
hidden_case.hide= TRUE
visible.name=Visible board
visible.hide=false
default.name=Default visible board
""")
        self.assertEqual([row["arduino_id"] for row in records], ["visible", "default"])
        self.assertEqual(catalog._parse_downloaded_arduino_board_files(self.arduino), records)
        boards = catalog.load_dynamic_boards({})
        self.assertEqual(set(boards), {"Visible board", "Default visible board"})

    def test_all_compound_defines_and_nested_string_or_list_flags_are_retained(self):
        configurations = [
            ("-DBASE=1 -DARDUINO_TOP -D SECOND=2", "-DNESTED=1 -DARDUINO_NESTED"),
            (["-DBASE=1 -DARDUINO_TOP", "-D SECOND=2"],
             ["-DNESTED=1 -DARDUINO_NESTED"]),
        ]
        for index, (flags, arduino_flags) in enumerate(configurations):
            self.manifest(f"flags-{index}", flags=flags, arduino_flags=arduino_flags)
        rows = catalog._load_platformio_board_catalog(self.core, force_read=True)
        expected = {"base", "arduinotop", "second", "nested", "arduinonested"}
        self.assertEqual(len(rows), len(configurations))
        for row in rows:
            self.assertEqual(row["arduino_defines"], expected)

    def test_board_define_after_an_unrelated_nested_flag_resolves(self):
        record = self.declarations("""
generic.name=Requested declaration
generic.build.mcu=esp32s3
generic.build.board=UNIQUE_BOARD
""")[0]
        self.manifest("unique-target", name="Different display label",
                      arduino_flags="-DUSB_MODE=1 -DARDUINO_UNIQUE_BOARD")
        match = catalog._resolve_arduino_board_record(
            record, catalog._load_platformio_board_catalog(self.core, force_read=True))
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], "unique-target")
        self.assertIn("arduino-define", match["match_reasons"])

    def development_targets(self):
        # Reviewed PlatformIO v7.0.1 identities: the generic Arduino variants
        # and defines are shared by different concrete development boards.
        for identifier, name, mcu, flash in (
                ("esp32-s3-devkitc-1", "Espressif ESP32-S3-DevKitC-1-N8 (8 MB QD, No PSRAM)", "esp32s3", "8MB"),
                ("esp32-s3-devkitm-1", "Espressif ESP32-S3-DevKitM-1", "esp32s3", "8MB"),
                ("esp32-c3-devkitc-02", "Espressif ESP32-C3-DevKitC-02", "esp32c3", "4MB"),
                ("esp32-c3-devkitm-1", "Espressif ESP32-C3-DevKitM-1", "esp32c3", "4MB")):
            self.manifest(identifier, name=name, mcu=mcu, variant=mcu, flash=flash,
                          flags=f"-DARDUINO_{mcu.upper()}_DEV")
        return catalog._load_platformio_board_catalog(self.core, force_read=True)

    def test_generic_s3_and_c3_remain_ambiguous_without_arbitrary_hardware_selection(self):
        candidates = self.development_targets()
        records = self.declarations("""
esp32s3.name=ESP32S3 Dev Module
esp32s3.build.mcu=esp32s3
esp32s3.build.variant=esp32s3
esp32s3.build.board=ESP32S3_DEV
esp32s3.build.flash_size=4MB
esp32c3.name=ESP32C3 Dev Module
esp32c3.build.mcu=esp32c3
esp32c3.build.variant=esp32c3
esp32c3.build.board=ESP32C3_DEV
esp32c3.build.flash_size=4MB
""")
        for record in records:
            self.assertIsNone(catalog._resolve_arduino_board_record(record, candidates))
            diagnosis = catalog.diagnose_arduino_board_record(record, candidates)
            self.assertEqual(diagnosis["status"], "ambiguous")
            self.assertEqual(len(diagnosis["candidates"]), 2)
            self.assertEqual(record["flash_size"], "4MB")

    def test_exact_named_development_targets_still_resolve(self):
        candidates = self.development_targets()
        for target in candidates:
            record = {"arduino_id": "custom-declaration", "name": target["name"],
                      "mcu": target["mcu"], "variant": target["variant"],
                      "build_board": f"{target['mcu'].upper()}_DEV", "hwids": set()}
            match = catalog._resolve_arduino_board_record(record, candidates)
            self.assertIsNotNone(match)
            self.assertEqual(match["id"], target["id"])
            self.assertEqual(match["flash_size"], target["flash_size"])

    def test_matching_define_cannot_override_mcu_or_framework_constraints(self):
        record = {"arduino_id": "generic", "name": "Requested declaration", "mcu": "esp32s3",
                  "variant": "esp32s3", "build_board": "ESP32S3_DEV", "hwids": set()}
        self.manifest("wrong-mcu", mcu="esp32c3", variant="esp32s3", flags="-DARDUINO_ESP32S3_DEV")
        self.manifest("wrong-framework", mcu="esp32s3", variant="esp32s3",
                      flags="-DARDUINO_ESP32S3_DEV", frameworks=["espidf"])
        self.assertIsNone(catalog._resolve_arduino_board_record(
            record, catalog._load_platformio_board_catalog(self.core, force_read=True)))

    def test_previous_catalog_schema_requires_fresh_declaration_discovery(self):
        self.cache.write_text(json.dumps({"version": 5, "prepared_catalog": [],
                                          "boards": {"ESP32 Family Device": {"board": ""}}}), encoding="utf-8")
        self.assertEqual(catalog._BOARD_CATALOG_CACHE_VERSION, 6)
        self.assertIsNone(catalog._load_board_catalog_cache())
        catalog._save_board_catalog_cache({"Verified fixture": {"board": "verified", "hwids": set()}})
        self.assertEqual(catalog._load_board_catalog_cache()["Verified fixture"]["board"], "verified")
        self.assertEqual(json.loads(self.cache.read_text(encoding="utf-8"))["version"], 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
