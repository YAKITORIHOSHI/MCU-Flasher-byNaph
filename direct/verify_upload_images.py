#!/usr/bin/env python3
"""Upload with "nobuild" must write the same images a full build declares.

PlatformIO's framework script is what lists an ESP32's bootloader, partition
table and boot_app0, and "nobuild" skips that script: uploads wrote only the
application. src/modules/platformio_build_hook.py records the list when a build
runs and restores it for nobuild uploads; it can also leave the chip in its
bootloader when the application restarts the board through the Serial Monitor.

The unit checks run the real hook against a simulated SCons environment. The
optional check runs real PlatformIO on the installed espressif32 platform, linked
into an owned core, with the "envdump" target: PlatformIO prints the assembled
uploader command and exits before any build or upload step. No port is opened.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.modules.platformio_build_env import HOOK, RUN_OPTIONS, build_environment  # noqa: E402
from src.modules.platform_runtime import native_platformio_dir  # noqa: E402

AUDIT = ROOT / "temp/audit/upload-images"


class FakeEnv(dict):
    """The parts of a SCons environment the hook reads and writes."""

    def __init__(self, build_dir: Path, **values):
        super().__init__(values)
        self.build_dir = str(build_dir)

    def subst(self, value):
        return str(value).replace("${BUILD_DIR}", self.build_dir).replace("$BUILD_DIR", self.build_dir)

    def Replace(self, **values):  # noqa: N802 - SCons API
        self.update(values)

    def GetLibBuilders(self):  # noqa: N802 - SCons API
        return []

    def CacheDir(self, path):  # noqa: N802 - SCons API
        self["cache"] = path


def run_hook(env: FakeEnv, targets, environ=None):
    """Execute the hook once, exactly as SCons does for each extra-script pass."""
    script = types.ModuleType("SCons.Script")
    script.COMMAND_LINE_TARGETS = list(targets)
    package = types.ModuleType("SCons")
    package.Script = script
    namespace = {"Import": lambda _name: None, "env": env, "__name__": "platformio_build_hook"}
    with patch.dict(sys.modules, {"SCons": package, "SCons.Script": script}), \
            patch.dict(os.environ, environ or {}, clear=False):
        if not environ:
            os.environ.pop("MCU_FLASHER_ESPTOOL_AFTER", None)
        exec(compile(HOOK.read_text(encoding="utf-8"), str(HOOK), "exec"), namespace)


class HookChecks(unittest.TestCase):
    def setUp(self):
        AUDIT.mkdir(parents=True, exist_ok=True)
        self.temp = Path(tempfile.mkdtemp(dir=AUDIT))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.temp, ignore_errors=True))
        self.build = self.temp / ".pio/build/mcu_env"
        self.build.mkdir(parents=True)
        self.framework = self.temp / "framework"
        self.framework.mkdir()
        self.declared = [("0x1000", "$BUILD_DIR/bootloader.bin"), ("0x8000", "$BUILD_DIR/partitions.bin"),
                         ("0xe000", str(self.framework / "boot_app0.bin"))]

    def record_build(self):
        env = FakeEnv(self.build)
        run_hook(env, [])  # pre pass of a build
        env["FLASH_EXTRA_IMAGES"] = list(self.declared)  # the framework script ran
        run_hook(env, [])  # post pass
        return json.loads((self.build / "mcu-flash-images.json").read_text(encoding="utf-8"))

    def create_images(self):
        for name in ("bootloader.bin", "partitions.bin"):
            (self.build / name).write_bytes(b"fixture image")
        (self.framework / "boot_app0.bin").write_bytes(b"fixture otadata")

    def test_build_records_and_nobuild_upload_restores_every_image(self):
        record = self.record_build()
        self.assertEqual(record["images"], [
            ["0x1000", str(self.build / "bootloader.bin")], ["0x8000", str(self.build / "partitions.bin")],
            ["0xe000", str(self.framework / "boot_app0.bin")]])
        self.create_images()
        upload = FakeEnv(self.build)
        run_hook(upload, ["nobuild", "upload"])  # pre pass, before the platform script
        self.assertEqual(upload["FLASH_EXTRA_IMAGES"], [(offset, path) for offset, path in record["images"]])

    def test_a_missing_recorded_image_fails_instead_of_writing_a_partial_set(self):
        self.record_build()
        self.create_images()
        (self.build / "partitions.bin").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "partitions.bin"):
            run_hook(FakeEnv(self.build), ["nobuild", "upload"])

    def test_without_a_valid_record_platformio_keeps_its_own_behavior(self):
        for content in (None, "{not json", json.dumps({"schema": 2, "images": []}),
                        json.dumps({"schema": 1, "images": [["0x0"]]})):
            with self.subTest(content=content):
                path = self.build / "mcu-flash-images.json"
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(content, encoding="utf-8")
                env = FakeEnv(self.build)
                run_hook(env, ["nobuild", "upload"])
                self.assertNotIn("FLASH_EXTRA_IMAGES", env)

    def test_a_full_build_upload_keeps_the_framework_list(self):
        self.record_build()
        env = FakeEnv(self.build, FLASH_EXTRA_IMAGES=[("0x0", "framework-chosen.bin")])
        run_hook(env, ["upload"])
        self.assertEqual(env["FLASH_EXTRA_IMAGES"], [("0x0", "framework-chosen.bin")])

    def test_monitor_owned_restart_leaves_the_chip_in_its_bootloader(self):
        for spelling, expected in (("hard-reset", "no-reset"), ("hard_reset", "no_reset")):
            for targets, environ, result in (
                    (["nobuild", "upload"], {"MCU_FLASHER_ESPTOOL_AFTER": "no-reset"}, expected),
                    (["nobuild", "upload"], None, spelling),
                    (["nobuild"], {"MCU_FLASHER_ESPTOOL_AFTER": "no-reset"}, spelling)):
                with self.subTest(spelling=spelling, targets=targets, environ=environ):
                    env = FakeEnv(self.build, MCU_FLASHER_HOOK_PRE_DONE=True,
                                  UPLOADERFLAGS=["--chip", "esp32", "--before", "default-reset",
                                                 "--after", spelling, "write-flash", "-z"])
                    run_hook(env, targets, environ)
                    flags = env["UPLOADERFLAGS"]
                    self.assertEqual(flags[flags.index("--after") + 1], result)
                    self.assertEqual(flags[:4], ["--chip", "esp32", "--before", "default-reset"])


ESP32_PLATFORM = "espressif32"


def prepared_esp32_store() -> Path | None:
    store = native_platformio_dir()
    needed = [store / "platforms" / ESP32_PLATFORM / "platform.json",
              store / "packages/framework-arduinoespressif32/package.json",
              store / "packages/tool-scons/package.json"]
    return store if sys.platform.startswith("linux") and all(path.is_file() for path in needed) else None


def dumped_list(output: str, name: str) -> list[str]:
    """Pull one list variable out of PlatformIO's envdump text."""
    match = re.search(rf"'{name}':\s*(\[.*?\])\s*,\s*\n\s*'", output, re.DOTALL)
    if not match:
        return []
    # The dump is a Python repr; long paths are split into adjacent literals.
    import ast
    return [str(value) for value in ast.literal_eval(match.group(1))]


@unittest.skipUnless(prepared_esp32_store(), "Real PlatformIO ESP32 packages are not prepared on this host")
class RealPlatformIO(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        AUDIT.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=AUDIT)))
        store = prepared_esp32_store()
        self.core = self.root / "core"
        for folder in ("platforms", "packages", "lib", ".tmp"):
            (self.core / folder).mkdir(parents=True)
        # Packages stay read-only; only this owned core's state is written.
        os.symlink(store / "platforms" / ESP32_PLATFORM, self.core / "platforms" / ESP32_PLATFORM)
        for package in (store / "packages").iterdir():
            if package.is_dir() and not package.name.startswith("."):
                os.symlink(package, self.core / "packages" / package.name)
        self.project = self.root / "workspace"
        (self.project / "src").mkdir(parents=True)
        (self.project / "src/main.cpp").write_text(
            "#include <Arduino.h>\nvoid setup() {}\nvoid loop() {}\n", encoding="utf-8")
        (self.project / "platformio.ini").write_text(
            "[env:mcu_env]\nplatform = espressif32\nboard = esp32dev\nframework = arduino\n"
            "upload_protocol = esptool\nupload_speed = 460800\n", encoding="utf-8")
        home = self.root / "home"
        home.mkdir()
        self.stack.enter_context(patch.dict(os.environ, {"HOME": str(home), "XDG_CACHE_HOME": str(self.root / "xdg")}))

    def pio(self, *arguments, extra=None):
        from main.core.toolchain import find_pio_executable
        environment = build_environment(os.environ, self.core, 2)
        environment.update(extra or {})
        command = [*(find_pio_executable() or [sys.executable, "-m", "platformio"]),
                   "run", *RUN_OPTIONS, "-e", "mcu_env", *arguments]
        return subprocess.run(command, cwd=self.project, env=environment, capture_output=True,
                              text=True, timeout=600)

    def test_nobuild_upload_command_carries_all_four_images_and_monitor_restart(self):
        reading = self.pio("-t", "envdump")
        self.assertEqual(reading.returncode, 0, (reading.stdout + reading.stderr)[-4000:])
        record = self.project / ".pio/build/mcu_env/mcu-flash-images.json"
        self.assertTrue(record.is_file(), "The build pass did not record its flash images")
        images = json.loads(record.read_text(encoding="utf-8"))["images"]
        names = [Path(path).name for _offset, path in images]
        self.assertIn("partitions.bin", names)
        self.assertIn("boot_app0.bin", names)
        self.assertTrue(any(name.startswith("bootloader") for name in names), names)
        for _offset, path in images:
            # envdump stops before compiling; stand in for the generated images.
            if not Path(path).exists():
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_bytes(b"fixture image")
        dump = self.pio("-t", "nobuild", "-t", "upload", "-t", "envdump", "--upload-port", "/dev/null",
                        extra={"MCU_FLASHER_ESPTOOL_AFTER": "no-reset"})
        self.assertEqual(dump.returncode, 0, (dump.stdout + dump.stderr)[-4000:])
        flags = dumped_list(dump.stdout, "UPLOADERFLAGS")
        self.assertTrue(flags, dump.stdout[-3000:])
        for offset, path in images:
            self.assertIn(offset, flags)
            self.assertIn(path, flags)
            self.assertEqual(flags[flags.index(path) - 1], offset)
        self.assertIn(flags[flags.index("--after") + 1], ("no-reset", "no_reset"))
        # Without the request, esptool keeps restarting the chip itself.
        plain = self.pio("-t", "nobuild", "-t", "upload", "-t", "envdump", "--upload-port", "/dev/null")
        self.assertEqual(plain.returncode, 0, (plain.stdout + plain.stderr)[-4000:])
        plain_flags = dumped_list(plain.stdout, "UPLOADERFLAGS")
        self.assertIn(plain_flags[plain_flags.index("--after") + 1], ("hard-reset", "hard_reset"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
