#!/usr/bin/env python3
"""One picker row per build target in both apps, with saved names carried over."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.core.board_labels import board_target, declared_label, merge_board_rows, resolve_label

DOWNLOAD = "/home/u/Documents/_MCUFlasherByNaph_src/Boards/avr-1.8.8/boards.txt"
IDE = "/home/u/.arduino15/packages/arduino/hardware/avr/1.8.8/boards.txt"
IDE_WINDOWS = r"C:\Users\u\AppData\Local\Arduino15\packages\arduino\hardware\avr\1.8.8\boards.txt"


def arduino(board="uno", platform="atmelavr", source=DOWNLOAD, core="avr-1.8.8", identifier="uno", **extra):
    return {"platform": platform, "board": board, "pio_resolved": bool(board), "arduino_board_id": identifier,
            "arduino_source_file": source, "arduino_source_sha256": "a" * 64, "source_core": core,
            "pio_name": "Arduino Uno", "mcu": "atmega328p", **extra}


def native(board="uno", platform="atmelavr", name="Arduino Uno"):
    return {"platform": platform, "board": board, "pio_resolved": True, "arduino_board_id": board,
            "source_core": "platformio-installed", "pio_name": name, "mcu": "atmega328p"}


class BoardLabelChecks(unittest.TestCase):
    def test_every_copy_of_one_target_is_one_row_named_by_the_downloaded_core(self):
        for ide in (IDE, IDE_WINDOWS):
            rows = {"Arduino Uno [atmelavr:uno]": native(), "Arduino UNO [uno, 1.8.8]": arduino(source=ide, core="1.8.8"),
                    "Arduino UNO": arduino()}
            for order in (list(rows), list(reversed(rows))):
                merged = merge_board_rows({name: rows[name] for name in order})
                self.assertEqual(list(merged), ["Arduino UNO"])
                self.assertEqual(merged["Arduino UNO"]["arduino_source_file"], DOWNLOAD)
                self.assertEqual(list(merge_board_rows(merged)), ["Arduino UNO"], "Merging is stable")

    def test_ide_copy_names_the_row_when_nothing_was_downloaded(self):
        merged = merge_board_rows({"Arduino Uno": native(), "Arduino UNO [uno, 1.8.8]": arduino(source=IDE, core="1.8.8")})
        self.assertEqual(list(merged), ["Arduino UNO"])

    def test_newest_version_of_one_core_wins(self):
        merged = merge_board_rows({
            "ESP32 Dev Module": arduino("esp32dev", "espressif32", core="arduino-esp32-2.0.14", identifier="esp32"),
            "ESP32 Dev Module [esp32, arduino-esp32-3.10.2]":
                arduino("esp32dev", "espressif32", core="arduino-esp32-3.10.2", identifier="esp32"),
            "ESP32 Dev Module [esp32, arduino-esp32-3.9.0]":
                arduino("esp32dev", "espressif32", core="arduino-esp32-3.9.0", identifier="esp32")})
        self.assertEqual(list(merged), ["ESP32 Dev Module"])
        self.assertEqual(merged["ESP32 Dev Module"]["source_core"], "arduino-esp32-3.10.2")

    def test_different_boards_that_share_a_name_keep_their_id(self):
        rows = {f"HelvePic32 [microchippic32:{board}]": native(board, "microchippic32", "HelvePic32")
                for board in ("helvepic32", "helvepic32_smd", "helvepic32_breadboardside")}
        merged = merge_board_rows(rows)
        self.assertEqual(sorted(merged), ["HelvePic32 [helvepic32]", "HelvePic32 [helvepic32_breadboardside]",
                                          "HelvePic32 [helvepic32_smd]"])
        self.assertEqual({info["label"] for info in merged.values()}, {"HelvePic32"})
        twins = merge_board_rows({"A": native("same", "p1", "Twin"), "B": native("same", "p2", "Twin")})
        twins = merge_board_rows({name: dict(info, label="Twin") for name, info in twins.items()})
        self.assertEqual(sorted(twins), ["Twin [p2:same]", "Twin [same]"])
        self.assertEqual(len({name.casefold() for name in twins}), 2)

    def test_names_differing_only_in_case_are_told_apart(self):
        merged = merge_board_rows({"3D printer controller": native("remram_v1", "ststm32", "3D printer controller"),
                                   "3D Printer Controller": native("armed_v1", "ststm32", "3D Printer Controller")})
        self.assertEqual(sorted(merged), ["3D Printer Controller [armed_v1]", "3D printer controller [remram_v1]"])

    def test_unresolved_rows_merge_only_for_identical_declarations(self):
        same = {"Arduino UNO WiFi": arduino("", identifier="unowifi"),
                "Arduino UNO WiFi [unowifi, 1.8.8]": arduino("", source=IDE, core="1.8.8", identifier="unowifi")}
        self.assertEqual(list(merge_board_rows(same)), ["Arduino UNO WiFi"])
        same["Arduino UNO WiFi [unowifi, 1.8.8]"]["arduino_source_sha256"] = "b" * 64
        self.assertEqual(len(merge_board_rows(same)), 2, "Different declarations stay separate until one resolves")

    def test_arduino_cli_targets_are_never_merged_or_renamed(self):
        cli = dict(arduino(board="unoq", platform="arduino:zephyr"), backend="arduino-cli", arduino_fqbn="arduino:zephyr:unoq")
        rows = {"Arduino UNO Q": cli, "Arduino UNO Q [arduino:zephyr@0.2]": dict(cli, arduino_source_file=IDE),
                "Native": dict(native("uno_q", "arduinoq", "Arduino UNO Q"), label="Arduino UNO Q")}
        merged = merge_board_rows(rows)
        self.assertEqual([name for name, info in merged.items() if info.get("backend") == "arduino-cli"],
                         ["Arduino UNO Q", "Arduino UNO Q [arduino:zephyr@0.2]"])
        self.assertEqual(len(merged), 3)

    def test_inputs_are_not_modified(self):
        rows = {"Arduino UNO": arduino(), "Arduino Uno": native()}
        before = copy.deepcopy(rows)
        merge_board_rows(rows)
        self.assertEqual(rows, before)

    def test_a_board_name_with_its_own_brackets_is_not_a_generated_suffix(self):
        info = native("digispark-pro32", "atmelavr", "Digispark Pro (32 byte buffer)")
        self.assertEqual(declared_label("Digispark Pro (32 byte buffer)", info), "Digispark Pro (32 byte buffer)")
        self.assertEqual(declared_label("Digispark Pro (32 byte buffer) [atmelavr:digispark-pro32]", info),
                         "Digispark Pro (32 byte buffer)")
        self.assertEqual(declared_label("Board [rev B]", native("board_b", name="Board [rev B]")), "Board [rev B]")

    def test_saved_names_follow_their_board(self):
        boards = merge_board_rows({
            "Arduino UNO": arduino(), "Arduino Uno [atmelavr:uno]": native(),
            "a": native("helvepic32", "microchippic32", "HelvePic32") | {"label": "HelvePic32"},
            "b": native("helvepic32_smd", "microchippic32", "HelvePic32") | {"label": "HelvePic32"},
            "SweetPea ESP-210 [espressif8266:esp210]": native("esp210", "espressif8266", "SweetPea ESP-210")})
        for saved, expected in (
                ("Arduino UNO", "Arduino UNO"), ("arduino uno", "Arduino UNO"), ("Arduino Uno", "Arduino UNO"),
                ("Arduino UNO [uno, 1.8.8]", "Arduino UNO"), ("Arduino Uno [atmelavr:uno]", "Arduino UNO"),
                ("SweetPea ESP-210 [espressif8266:esp210]", "SweetPea ESP-210"),
                ("HelvePic32 [microchippic32:helvepic32_smd]", "HelvePic32 [helvepic32_smd]"),
                ("HelvePic32 (helvepic32)", "HelvePic32 [helvepic32]"),
                ("HelvePic32", ""), ("Removed board", ""), ("", "")):
            self.assertEqual(resolve_label(saved, boards), expected, saved)

    def test_a_suffix_that_names_another_board_is_never_guessed(self):
        boards = merge_board_rows({
            "Arduino UNO": arduino(), "Arduino Uno [atmelavr:uno]": native(),
            "SweetPea ESP-210 [espressif8266:esp210]": native("esp210", "espressif8266", "SweetPea ESP-210")})
        for saved in ("Arduino Uno [another:uno]", "Arduino UNO (atmega168)", "SweetPea ESP-210 (esp211)",
                      "SweetPea ESP-210 [espressif32:esp210]", "SweetPea ESP-210 [esp211]"):
            self.assertEqual(resolve_label(saved, boards), "", saved)
        self.assertEqual(resolve_label("SweetPea ESP-210 (esp210)", boards), "SweetPea ESP-210")

    def test_catalog_publishes_merged_rows_and_resolves_old_names(self):
        from main.core.board_catalog import BoardCatalog
        catalog = BoardCatalog(merge_board_rows({"Arduino UNO": arduino(), "Arduino Uno [atmelavr:uno]": native()}))
        self.assertEqual(catalog.canonical("Arduino UNO [uno, 1.8.8]"), "Arduino UNO")
        self.assertEqual(catalog.canonical("Unknown"), "")
        self.assertEqual(catalog.label_for(board_target("Arduino Uno", native())), "Arduino UNO")
        self.assertEqual(catalog.label_for(("platformio", "none", "none")), "")

    def test_lites_bundled_list_has_one_row_per_target(self):
        records = json.loads((ROOT / "LITE-MODE/assets/boards.json").read_text(encoding="utf-8"))
        rows = {f"{row['name']} [{row['platform']}:{row['id']}]": native(row["id"], row["platform"], row["name"])
                for row in records if row.get("frameworks")}
        merged = merge_board_rows(rows | {"Arduino UNO": arduino(), "Arduino UNO [uno, 1.8.8]": arduino(source=IDE)})
        targets = [board_target(name, info) for name, info in merged.items()]
        self.assertEqual(len(targets), len(set(targets)))
        self.assertEqual(sum(info["board"] == "uno" and info["platform"] == "atmelavr" for info in merged.values()), 1)
        self.assertFalse([name for name in merged if ":" in name and name.endswith("]")], "No platform:id suffix remains")
        self.assertTrue(all(resolve_label(name, merged) for name in rows), "Every earlier Lite name still resolves")


if __name__ == "__main__":
    unittest.main(verbosity=2)
