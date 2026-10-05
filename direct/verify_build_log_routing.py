#!/usr/bin/env python3
"""Verify compile stdout presentation without importing or running the backend.

Only the existing parser initialization, nested formatters and stdout loop are
extracted from its AST. StringIO and an event collector replace the process and
signal bus; no compilation, hardware, package store or live persistence runs.
"""
from __future__ import annotations

import ast
import io
from pathlib import Path
import re
import time
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]


def parser_fixture():
    source = ROOT / "main/web_bridge.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "_compile_worker_impl")
    for parent in ast.walk(method):
        for _, value in ast.iter_fields(parent):
            if not isinstance(value, list):
                continue
            start = next((index for index, node in enumerate(value)
                          if isinstance(node, ast.AnnAssign)
                          and isinstance(node.target, ast.Name)
                          and node.target.id == "output_lines"), None)
            if start is None:
                continue
            end = next((index for index, node in enumerate(value[start:], start)
                        if isinstance(node, ast.For)
                        and isinstance(node.target, ast.Name)
                        and node.target.id == "line"), None)
            if end is None:
                continue
            function = ast.FunctionDef(
                name="route_fixture",
                args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="self")],
                                   kwonlyargs=[], kw_defaults=[], defaults=[]),
                body=value[start:end + 1] + [ast.Return(value=ast.Name(id="output_lines", ctx=ast.Load()))],
                decorator_list=[],
            )
            module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
            namespace = {"Path": Path, "re": re, "time": time}
            exec(compile(module, str(source), "exec"), namespace)
            return namespace["route_fixture"]
    raise AssertionError("Compile output parser boundary was not found")


ROUTE = parser_fixture()


class ParserBackend:
    def __init__(self, lines):
        self._active_process = SimpleNamespace(stdout=io.StringIO("\n".join(lines) + "\n"))
        self._stop_requested = False
        self._framework_download_active = False
        self.active_operation = "compile"
        self.events = []

    def emit(self, event, payload):
        self.events.append((event, payload))

    def _kill_active_process_tree(self):
        raise AssertionError("The isolated parser must never terminate a process")


class BuildLogRoutingChecks(unittest.TestCase):
    def route(self, *lines):
        backend = ParserBackend(lines)
        captured = ROUTE(backend)
        self.assertEqual(captured, list(lines))
        return [payload for event, payload in backend.events if event == "console:log"]

    def test_diagnostic_source_words_and_carets_are_preserved(self):
        sources = (
            '  12 | building(); // looking for a library while linking',
            '     | ^~~~~~~~~',
            '  13 | const char *s = "undefined reference to building";',
            '  14 | const char *s = "test.cpp:1: warning: source literal";',
        )
        records = self.route(
            "src/Main.cpp:12:5: error: building was not declared",
            *sources,
            "src/Main.cpp:4: note: declared while linking",
            '     |      ^~~~~~~',
        )
        text = "\n".join(item["text"] for item in records)
        for line in sources:
            self.assertIn(line, text)
        self.assertIn("Error at src/Main.cpp:12:5", text)
        self.assertIn("building was not declared", text)
        self.assertIn("Note at src/Main.cpp:4", text)
        self.assertIn("declared while linking", text)
        self.assertEqual(next(item["tag"] for item in records
                              if "undefined reference to building" in item["text"]), "error")

    def test_unknown_framework_output_and_scons_failure_remain_visible(self):
        lines = (
            "CMake could not locate the configured board source",
            "Looking for a custom target configuration",
            "custom builder finished linking metadata; no object created",
            "*** [.pio/build/native/firmware.elf] Error 2",
            "Tool Manager: tool-scons custom resolver failed",
            "Tool Manager: Error: tool-scons failed to initialize",
            "unrecognized framework output Ω",
        )
        records = self.route(*lines)
        text = "\n".join(item["text"] for item in records)
        for line in lines:
            self.assertIn(line, text)
        self.assertEqual(next(item["tag"] for item in records
                              if "firmware.elf] Error 2" in item["text"]), "error")
        self.assertEqual(next(item["tag"] for item in records
                              if "tool-scons failed to initialize" in item["text"]), "error")

    def test_progress_records_preserve_filename_case_across_hosts(self):
        records = self.route(
            r"Compiling .pio\build\native\src\Main.cpp.o",
            "Compiling .pio/build/native/src/Main.cpp.o",
            "Compiling .pio/build/native/src/Linking.cpp.o",
            r"Building .pio\build\native\Bootloader.bin",
            "Linking .pio/build/native/firmware.elf",
        )
        text = "\n".join(item["text"] for item in records)
        self.assertEqual(text.count("Compiling Main.cpp.o..."), 1)
        self.assertIn("Bootloader.bin", text)
        self.assertIn("Compiling Linking.cpp.o...", text)
        self.assertIn("Linking...", text)
        self.assertNotIn("main.cpp.o", text)

    def test_routine_promotion_and_standard_outcome_do_not_duplicate_summary(self):
        records = self.route(
            "*****",
            "* CLI  > https://docs.platformio.org",
            "* WEB  > https://platformio.org",
            "Looking for FastAccelStepper library",
            "If you like PlatformIO, please star it on GitHub",
            "Check our library registry",
            "RAM: [==       ] 20.0% (used 20 bytes from 100 bytes)",
            "Flash: [===      ] 30.0% (used 30 bytes from 100 bytes)",
            "================= [SUCCESS] Took 1.20 seconds =================",
            "================= [FAILED] Took 1.20 seconds ==================",
        )
        self.assertEqual(records, [])

    def test_progress_looking_sentences_and_unnumbered_source_remain_exact(self):
        lines = (
            "Compiling failed to create target .o",
            "Compiling failed to create target Main.cpp.o",
            "Archiving failed to write output libArduino.a",
            "Linking failed to create target firmware.elf",
            "Building failed to create target firmware.bin",
            "Checking size failed for firmware.elf",
            "Retrieving maximum program size failed for firmware.elf",
            "Compiling something();",
            "Linking callback();",
            "Building in release mode",
            "Building in debug mode...",
        )
        records = self.route(*lines)
        self.assertEqual([item["text"] for item in records], list(lines))
        self.assertTrue(all(item["tag"] == "dim" for item in records))

    def test_artifact_records_accept_native_and_quoted_paths_with_spaces(self):
        records = self.route(
            r"Compiling C:\Project with spaces\.pio\build\native\src\Main.cpp.o",
            'Compiling ".pio/build/native/src/Other file.cpp.o"',
            "Archiving /home/user/Project with spaces/.pio/build/native/libArduino.a",
            "Linking firmware.elf",
            "Checking size /home/user/Project with spaces/.pio/build/native/firmware.elf",
        )
        text = "\n".join(item["text"] for item in records)
        self.assertIn("Compiling Main.cpp.o...", text)
        self.assertIn("Compiling Other file.cpp.o...", text)
        self.assertIn("Archiving...", text)
        self.assertIn("Linking...", text)
        self.assertIn("Checking firmware size...", text)

    def test_promotion_words_inside_diagnostics_are_not_suppressed(self):
        source = '   7 | puts("Check our library registry; Looking for FastAccelStepper library");'
        records = self.route("src/Main.cpp:7: warning: Looking for a library while building", source)
        text = "\n".join(item["text"] for item in records)
        self.assertIn("Looking for a library while building", text)
        self.assertIn(source, text)
        self.assertTrue(all(item["tag"] == "warning" for item in records))

    def test_warnings_context_and_next_progress_are_separate(self):
        records = self.route(
            "src/Main.cpp: In function 'void building()':",
            "src/Main.cpp:9: warning: linking option is ignored",
            "  9 | building();",
            "    | ^~~~~~~~~~",
            "Compiling .pio/build/native/src/Main.cpp.o",
            "after-build: generated symbols successfully",
        )
        self.assertEqual(records[-1]["text"], "after-build: generated symbols successfully")
        self.assertEqual(records[-1]["tag"], "dim")
        self.assertEqual(records[-2]["text"], "  ⚙ Compiling Main.cpp.o...")
        self.assertEqual(records[-2]["tag"], "info")

    def test_linker_executable_prefix_is_separate_from_custom_build_status(self):
        records = self.route(
            "/usr/bin/arm-none-eabi-ld: cannot open linker script file custom.ld",
            r"C:\Tools\xtensa-esp32-elf-ld.exe: cannot open linker script file custom.ld",
            "after-build: generated symbols successfully",
            "custom builder saved world: done",
        )
        self.assertEqual([item["tag"] for item in records], ["error", "error", "dim", "dim"])
        self.assertEqual(records[-1]["text"], "custom builder saved world: done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
