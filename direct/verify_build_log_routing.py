#!/usr/bin/env python3
"""Verify compile stdout presentation without importing or running the backend.

Only the existing parser initialization, nested formatters and stdout loop are
extracted from its AST. StringIO and an event collector replace the process and
signal bus; no compilation, hardware, package store or live persistence runs.
"""
from __future__ import annotations

import ast
import io
import queue
from pathlib import Path
import re
import threading
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
            namespace = {
                "Path": Path, "re": re, "time": time,
                "_iter_process_output": lambda process, *_args, **_kwargs: iter(process.stdout.readline, ""),
            }
            exec(compile(module, str(source), "exec"), namespace)
            return namespace["route_fixture"]
    raise AssertionError("Compile output parser boundary was not found")


ROUTE = parser_fixture()


def process_output_fixture():
    source = ROOT / "main/web_bridge.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_iter_process_output")
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = {"queue": queue, "threading": threading, "time": time}
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["_iter_process_output"]


ITER_PROCESS_OUTPUT = process_output_fixture()


def upload_line_classifier_fixture():
    source = ROOT / "main/web_bridge.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "_classify_platformio_upload_line")
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = {"re": re}
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["_classify_platformio_upload_line"]


CLASSIFY_UPLOAD_LINE = upload_line_classifier_fixture()


def upload_write_started_fixture():
    source = ROOT / "main/web_bridge.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "_platformio_upload_write_started")
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = {"re": re}
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["_platformio_upload_write_started"]


UPLOAD_WRITE_STARTED = upload_write_started_fixture()


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
        self.assertEqual(text.count("Compiling Main.cpp.o..."), 2)
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
            "CONFIGURATION: https://docs.platformio.org/page/boards/espressif32/esp32dev.html",
            "PLATFORM: Espressif 32 (6.12.0) > Espressif ESP32 Dev Module",
            "HARDWARE: ESP32 240MHz, 320KB RAM, 4MB Flash",
            "PACKAGES:",
            "DEBUG: Current (esp-prog) External (cmsis-dap, esp-prog)",
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
        )
        records = self.route(*lines)
        self.assertEqual([item["text"] for item in records], list(lines))
        self.assertTrue(all(item["tag"] == "dim" for item in records))

    def test_routine_platformio_chatter_is_hidden_without_hiding_files(self):
        chatter = (
            "Verbose mode can be enabled via `-v, --verbose` option",
            " - framework-arduinoespressif32 @ 3.20017.241212+sha.dcc1105b ",
            " - tool-esptoolpy @ 2.41100.0 (4.11.0) ",
            " - toolchain-xtensa-esp32 @ 8.4.0+2021r2-patch5",
            "LDF: Library Dependency Finder -> https://bit.ly/configure-pio-ldf",
            "LDF Modes: Finder ~ chain, Compatibility ~ soft",
            "Found 47 compatible libraries",
            "Scanning dependencies...",
            "No dependencies",
            'Advanced Memory Usage is available via "PlatformIO Home > Project Inspect"',
            "esptool.py v4.11.0",
            "Creating esp32 image...",
            "Merged 1 ELF section",
            "Successfully created esp32 image.",
        )
        records = self.route(*chatter, "Compiling .pio/build/native/src/Main.cpp.o")
        self.assertEqual(records, [{
            "text": "  ⚙ Compiling Main.cpp.o...", "tag": "info", "newline": True,
        }])

    def test_dependency_graph_is_hidden_and_following_build_status_is_kept(self):
        records = self.route(
            "Processing mcu_env (platform: espressif32; board: esp32dev; framework: arduino)",
            "Dependency Graph",
            "|-- ESP32Servo @ 3.2.1",
            "|-- FastAccelStepper @ 1.2.7",
            "|-- HX711 @ 0.6.4",
            "  ──────────────────────────────────────────────────",
            "Building in release mode...",
        )
        text = "\n".join(record["text"] for record in records)
        self.assertNotIn("Dependency Graph", text)
        self.assertNotIn("ESP32Servo @ 3.2.1", text)
        self.assertNotIn("FastAccelStepper @ 1.2.7", text)
        self.assertNotIn("HX711 @ 0.6.4", text)
        self.assertIn("────────────────", text)
        self.assertIn("  ⚙ Building in release mode...", text)

    def test_boilerplate_matching_is_narrow_and_diagnostic_context_wins(self):
        similar_custom_lines = (
            "Verbose mode can be enabled by editing the custom build script",
            " - custom-runtime @ 1.0.0",
            "LDF Modes: custom dependency search failed",
            "Found 47 compatible libraries, but no target match",
            "No dependencies could be loaded from the custom manifest",
            "esptool.py v4.11.0: custom builder output",
            "Creating esp32 image failed",
            "Merged 1 ELF section with custom metadata",
            "Successfully created esp32 image. Custom checksum: 123",
            "Configuration: custom builder could not load its manifest",
            "Hardware: custom builder has no configured output device",
            "RAM: custom builder memory allocation failed",
        )
        records = self.route(*similar_custom_lines)
        self.assertEqual([record["text"] for record in records], list(similar_custom_lines))

        records = self.route(
            "src/Main.cpp:2: error: invalid custom script",
            "No dependencies",
            "Merged 1 ELF section",
            "LDF Modes: Finder ~ chain, Compatibility ~ soft",
            "RAM: [==       ] 20.0% (used 20 bytes from 100 bytes)",
            "Compiling .pio/build/native/src/Main.cpp.o",
            "No dependencies",
        )
        text = "\n".join(record["text"] for record in records)
        self.assertEqual(text.count("No dependencies"), 1)
        self.assertIn("Merged 1 ELF section", text)
        self.assertIn("LDF Modes: Finder ~ chain, Compatibility ~ soft", text)
        self.assertIn("RAM: [==       ] 20.0% (used 20 bytes from 100 bytes)", text)
        self.assertEqual(records[-1]["text"], "  ⚙ Compiling Main.cpp.o...")

    def test_phase_dividers_and_each_compiling_file_are_retained(self):
        records = self.route(
            "Processing native (platform: native)",
            "Library Manager: MyLibrary@1.0.0 has been installed!",
            "Building in release mode",
            "Compiling .pio/build/native/src/Main.cpp.o",
            "Compiling .pio/build/native/lib123/MyLibrary/Main.cpp.o",
            "Generating partitions .pio/build/native/partitions.bin",
            "Linking .pio/build/native/firmware.elf",
        )
        texts = [record["text"] for record in records]
        self.assertEqual(sum("─" * 10 in text for text in texts), 2)
        self.assertIn("  ⚙ Building in release mode...", texts)
        self.assertEqual(texts.count("  ⚙ Compiling Main.cpp.o..."), 2)
        self.assertIn("  ⚡ Building partition table (partitions.bin)...", texts)
        self.assertEqual(texts[-1], "  🔗 Linking...")

    def test_local_symlink_library_manager_messages_are_preserved_as_dependency_setup(self):
        linked_path = "Library Manager: Linking symlink://C:/Users/example/Arduino/libraries/NimBLE-Arduino"
        linked_version = "Library Manager: NimBLE-Arduino@2.5.1 has been linked!"
        records = self.route(linked_path, linked_version)
        texts = [record["text"] for record in records]
        self.assertIn(f"    {linked_path}", texts)
        self.assertIn(f"    {linked_version}", texts)
        self.assertTrue(all(record["tag"] == "info" for record in records if record["text"].startswith("    Library Manager:")))

    def test_generic_severity_wins_over_routine_and_promotion_filters(self):
        lines = (
            " - tool-custom @ 1.2.3 (warning: missing version metadata)",
            " - tool-custom @ 1.2.3 (error: failed version validation)",
            "Looking for FastAccelStepper library: error: missing required headers",
            "Check our library registry: warning: dependency metadata is incomplete",
        )
        records = self.route(*lines)
        self.assertEqual([record["tag"] for record in records], ["warning", "error", "error", "warning"])
        for line, record in zip(lines, records):
            self.assertIn(line, record["text"])

    def test_generic_severity_wins_over_metadata_and_summary_filters(self):
        lines = (
            "Hardware: 240MHz warning: custom failure",
            "Hardware: ESP32 240MHz, 320KB RAM error: custom validation failed",
            "========= [FAILED] Took 1.20 seconds error: unresolved symbols =========",
            "========= [SUCCESS] Took 1.20 seconds warning: output metadata incomplete =========",
        )
        records = self.route(*lines)
        self.assertEqual([record["tag"] for record in records], ["warning", "error", "error", "warning"])
        for line, record in zip(lines, records):
            self.assertIn(line, record["text"])

    def test_partition_artifacts_and_build_action_paths_are_recognized(self):
        records = self.route(
            r"Generating partitions C:\Project with spaces\.pio\build\native\custom.bin",
            'Building ".pio/build/native/Bootloader.bin" with action: bootloader_action',
            "Generating partitions failed to create table partitions.bin",
        )
        self.assertEqual([record["text"] for record in records], [
            "  ⚡ Building partition table (custom.bin)...",
            "  ⚡ Building bootloader image (Bootloader.bin)...",
            "Generating partitions failed to create table partitions.bin",
        ])

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

    def test_platformio_upload_filter_hides_scan_and_keeps_outcomes_and_diagnostics(self):
        self.assertEqual(
            CLASSIFY_UPLOAD_LINE("================ [SUCCESS] Took 1.20 seconds ================"),
            ("outcome", "SUCCESS"),
        )
        self.assertEqual(
            CLASSIFY_UPLOAD_LINE("================ [FAILED] Took 1.20 seconds ================"),
            ("outcome", "FAILED"),
        )
        for line in (
            "Processing mcu_env (platform: espressif32; board: esp32dev)",
            "Dependency Graph",
            "|-- NimBLE-Arduino @ 2.5.1",
            "Building in release mode...",
            "Found 3 compatible libraries",
            "Looking for a custom library",
        ):
            self.assertEqual(CLASSIFY_UPLOAD_LINE(line), ("suppress", None), line)
        for line in (
            "src/Main.cpp:4: error: missing symbol",
            "warning: programmer firmware is outdated",
            "Connecting to programmer: .",
            'Found programmer: Id = "AVR ISP"',
            "Found device: Signature = 0x1e950f",
            "Writing at 0x00010000...",
            "Library Manager: Linking symlink://local/library",
        ):
            self.assertEqual(CLASSIFY_UPLOAD_LINE(line), ("show", None), line)

        tree = ast.parse((ROOT / "main/web_bridge.py").read_text(encoding="utf-8-sig"))
        cls = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == "MCUWebBackendAPI")
        for name in ("_upload_worker", "_native_upload_worker"):
            worker = next(node for node in cls.body
                          if isinstance(node, ast.FunctionDef) and node.name == name)
            self.assertTrue(any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_classify_platformio_upload_line"
                for node in ast.walk(worker)
            ), f"{name} must share the PlatformIO output filter")

    def test_upload_write_boundary_preserves_programmer_discovery_and_detects_writes(self):
        for line in (
            "Processing mcu_env (platform: atmelavr; board: uno)",
            "Found 3 compatible libraries",
            'Found programmer: Id = "AVR ISP"',
            "Found device: Signature = 0x1e950f",
            "Connecting to programmer: .",
        ):
            self.assertFalse(UPLOAD_WRITE_STARTED(line), line)
        for line in (
            "Erasing flash (this may take a while)...",
            "Writing at 0x00010000...",
            "Writing | ############################",
            "Programming flash memory",
            "Downloading element to address = 0x08000000",
        ):
            self.assertTrue(UPLOAD_WRITE_STARTED(line), line)

    def test_process_output_reports_quiet_periods_without_losing_lines(self):
        class DelayedStdout:
            def __init__(self):
                self.calls = 0

            def readline(self, size=-1):
                self.calls += 1
                if self.calls == 1:
                    return "Processing mcu_env (platform: native)\n"
                time.sleep(0.15)
                return ""

        process = SimpleNamespace(stdout=DelayedStdout())
        quiet = []
        lines = list(ITER_PROCESS_OUTPUT(
            process, lambda: False, lambda: self.fail("Unexpected process termination"),
            quiet.append, poll_interval=0.005, notice_after=0.01, notice_every=0.01,
        ))
        self.assertEqual(lines, ["Processing mcu_env (platform: native)\n"])
        self.assertTrue(quiet)

    def test_process_output_stop_kills_silent_process_promptly(self):
        released = threading.Event()
        stop = threading.Event()
        terminated = []

        class BlockingStdout:
            def readline(self, size=-1):
                released.wait(1.0)
                return ""

        timer = threading.Timer(0.03, stop.set)
        timer.start()
        try:
            started = time.monotonic()
            list(ITER_PROCESS_OUTPUT(
                SimpleNamespace(stdout=BlockingStdout()), stop.is_set,
                lambda: (terminated.append(True), released.set()), lambda _seconds: None,
                poll_interval=0.005, notice_after=1.0,
            ))
        finally:
            timer.cancel()
        self.assertEqual(terminated, [True])
        self.assertLess(time.monotonic() - started, 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
