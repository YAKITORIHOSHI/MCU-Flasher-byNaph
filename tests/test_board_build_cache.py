import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mcu_flash_gui as gui_module


FAKE_BOARDS = {
    "Board A": {
        "platform": "espressif32",
        "board": "shared_board_id",
        "framework": "arduino",
        "variant": "variant_a",
    },
    "Board B": {
        "platform": "espressif32",
        "board": "shared_board_id",
        "framework": "arduino",
        "variant": "variant_b",
    },
    "Arduino Uno": {
        "platform": "atmelavr",
        "board": "uno",
        "framework": "arduino",
    },
}


class _MutableVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class BoardBuildCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temporary_directory.name)
        self.project = self.temp_root / "sketch"
        self.project.mkdir()
        self.script_root = self.temp_root / "app"
        self.script_root.mkdir()

        self.app = gui_module.MCUUploadGUI.__new__(gui_module.MCUUploadGUI)
        self.app.sketch_dir_path = self.project
        self.app.board_var = _MutableVar("Board A")
        self.app._compile_cache_by_board = {}
        self.app._build_config_hash_by_board = {}
        self.app._build_metadata_by_board = {}
        self.app._last_compiled_board = None
        self.messages = []
        self.app._append = lambda text, tag="": self.messages.append((text, tag))

        self.board_patch = mock.patch.object(
            gui_module, "SUPPORTED_BOARDS", FAKE_BOARDS
        )
        self.board_patch.start()

    def tearDown(self):
        self.board_patch.stop()
        self.temporary_directory.cleanup()

    def test_canonical_board_cache_key_resists_slug_and_config_collisions(self):
        # These exact board definitions intentionally have the same readable
        # platform/board prefix. The full definition must still distinguish them.
        key_a = self.app._board_cache_key("Board A")
        key_b = self.app._board_cache_key("Board B")
        self.assertNotEqual(key_a, key_b)
        # The readable portion is intentionally truncated to keep deeply
        # nested Windows build paths below legacy MAX_PATH limits.
        self.assertTrue(key_a.startswith("espressif32_shared_"))
        self.assertTrue(key_b.startswith("espressif32_shared_"))
        self.assertEqual(key_a, self.app._board_cache_key("Board A"))

        # Unknown display names can normalize to the same slug too. The name
        # fallback is included in the digest so those folders cannot collide.
        punctuation_key = self.app._board_cache_key("Foo-Bar")
        whitespace_key = self.app._board_cache_key("Foo Bar")
        self.assertNotEqual(punctuation_key, whitespace_key)
        self.assertTrue(punctuation_key.startswith("foo_bar_"))
        self.assertTrue(whitespace_key.startswith("foo_bar_"))

    def test_identical_board_definitions_under_different_names_are_isolated(self):
        identical_definition = dict(FAKE_BOARDS["Board A"])
        aliases = {
            "Alias Alpha": dict(identical_definition),
            "Alias Beta": dict(identical_definition),
        }

        with mock.patch.object(gui_module, "SUPPORTED_BOARDS", aliases):
            key_alpha = self.app._board_cache_key("Alias Alpha")
            key_beta = self.app._board_cache_key("Alias Beta")
            env_alpha = self.app._pio_env_name("Alias Alpha")
            env_beta = self.app._pio_env_name("Alias Beta")
            workspace_alpha = self.app._board_workspace(
                board_name="Alias Alpha"
            )
            workspace_beta = self.app._board_workspace(
                board_name="Alias Beta"
            )

        self.assertNotEqual(key_alpha, key_beta)
        self.assertNotEqual(env_alpha, env_beta)
        self.assertNotEqual(workspace_alpha, workspace_beta)
        self.assertEqual(workspace_alpha.name, key_alpha)
        self.assertEqual(workspace_beta.name, key_beta)

    def test_long_board_name_uses_bounded_stable_environment_and_paths(self):
        long_name = "Downloaded ESP32 Board " + ("Very-Long-Name-" * 40)
        boards = {
            long_name: {
                "platform": "espressif32",
                "board": "esp32-s3-devkitc-1",
                "framework": "arduino",
            }
        }

        with mock.patch.object(gui_module, "SUPPORTED_BOARDS", boards):
            env_name = self.app._pio_env_name(long_name)
            workspace = self.app._board_workspace(board_name=long_name)
            build_dir = self.app._board_build_dir(board_name=long_name)

            self.assertEqual(env_name, self.app._pio_env_name(long_name))

        self.assertTrue(env_name.startswith("mcu_"))
        self.assertEqual(len(env_name), 14)
        self.assertTrue(env_name.removeprefix("mcu_").isalnum())
        self.assertLessEqual(len(workspace.name), 31)
        self.assertEqual(build_dir.name, env_name)
        self.assertNotIn(long_name, str(workspace))

    def test_platformio_environment_is_distinct_per_board_without_scons_cache(self):
        inherited_cache = self.temp_root / "obsolete-global-scons-cache"
        with mock.patch.dict(
            os.environ,
            {"PLATFORMIO_BUILD_CACHE_DIR": str(inherited_cache)},
            clear=False,
        ):
            env_a = self.app._platformio_subprocess_env(
                board_name="Board A", jobs=0
            )
            env_b = self.app._platformio_subprocess_env(
                board_name="Board B", jobs=2
            )

        workspace_a = self.app._board_workspace(board_name="Board A")
        workspace_b = self.app._board_workspace(board_name="Board B")
        self.assertNotEqual(workspace_a, workspace_b)
        self.assertEqual(Path(env_a["PLATFORMIO_WORKSPACE_DIR"]), workspace_a)
        self.assertEqual(Path(env_b["PLATFORMIO_WORKSPACE_DIR"]), workspace_b)
        self.assertEqual(Path(env_a["PLATFORMIO_BUILD_DIR"]), workspace_a / "build")
        self.assertEqual(Path(env_b["PLATFORMIO_BUILD_DIR"]), workspace_b / "build")
        self.assertEqual(Path(env_a["PLATFORMIO_LIBDEPS_DIR"]), workspace_a / "libdeps")
        self.assertEqual(Path(env_b["PLATFORMIO_LIBDEPS_DIR"]), workspace_b / "libdeps")
        self.assertNotIn("PLATFORMIO_BUILD_CACHE_DIR", env_a)
        self.assertNotIn("PLATFORMIO_BUILD_CACHE_DIR", env_b)
        self.assertEqual(env_a["PLATFORMIO_BUILD_JOBS"], "1")
        self.assertEqual(env_b["PLATFORMIO_BUILD_JOBS"], "2")

    def test_a_to_b_to_a_lookup_returns_to_each_exact_build(self):
        build_a = self.app._board_build_dir()
        build_a.mkdir(parents=True)
        (build_a / "firmware.bin").write_bytes(b"firmware-a")
        self.assertTrue(self.app._has_prior_build())

        self.app.board_var.set("Board B")
        build_b = self.app._board_build_dir()
        self.assertNotEqual(build_a, build_b)
        self.assertFalse(self.app._has_prior_build())
        build_b.mkdir(parents=True)
        (build_b / "firmware.elf").write_bytes(b"firmware-b")
        self.assertTrue(self.app._has_prior_build())

        self.app.board_var.set("Board A")
        self.assertTrue(self.app._has_prior_build())
        self.assertEqual((build_a / "firmware.bin").read_bytes(), b"firmware-a")
        self.assertEqual((build_b / "firmware.elf").read_bytes(), b"firmware-b")

    def test_has_prior_build_never_restores_family_or_shared_artifacts(self):
        family_build = self.project / "compiled_builds" / "ESP32"
        family_build.mkdir(parents=True)
        (family_build / "firmware.bin").write_bytes(b"wrong-board-family-binary")

        legacy_shared = (
            self.project / ".pio" / "build" / self.app._pio_env_name()
        )
        legacy_shared.mkdir(parents=True)
        (legacy_shared / "firmware.bin").write_bytes(b"legacy-shared-binary")
        exact_build = self.app._board_build_dir()

        with mock.patch.object(shutil, "copy2") as copy_file:
            self.assertFalse(self.app._has_prior_build())

        copy_file.assert_not_called()
        self.assertFalse(exact_build.exists())
        self.assertEqual(
            (family_build / "firmware.bin").read_bytes(),
            b"wrong-board-family-binary",
        )
        self.assertEqual(
            (legacy_shared / "firmware.bin").read_bytes(), b"legacy-shared-binary"
        )

    def test_source_failures_are_not_misclassified_as_cache_corruption(self):
        source_failures = (
            [r"src\main.cpp:42:9: error: 'missingName' was not declared"],
            ["xtensa-esp32-elf-ld.exe: undefined reference to `setup()'"],
            ["region `dram0_0_seg' overflowed by 2048 bytes"],
            ["collect2.exe: error: ld returned 1 exit status"],
        )
        for output in source_failures:
            with self.subTest(output=output[0]):
                self.assertEqual(
                    gui_module.classify_platformio_failure(output), "source"
                )

        # A bare SCons wrapper does not prove corruption and must not trigger a
        # destructive cache repair either.
        self.assertEqual(
            gui_module.classify_platformio_failure(
                [r"*** [.pio\boards\a\build\env\src\main.cpp.o] Error 1"]
            ),
            "tool",
        )

        # A real compiler error remains authoritative even if a cache marker
        # appears elsewhere in the same failed run.
        self.assertEqual(
            gui_module.classify_platformio_failure(
                [
                    "sqlite3.DatabaseError: database disk image is malformed",
                    "src/main.cpp:17:4: error: expected ';' before '}' token",
                ]
            ),
            "source",
        )

    def test_only_explicit_cache_corruption_signatures_request_cache_repair(self):
        cache_failures = (
            ["sqlite3.DatabaseError: database disk image is malformed"],
            ["SCons: pickle data was truncated while reading .sconsign.dblite"],
            ["sconsign file is corrupt"],
        )
        for output in cache_failures:
            with self.subTest(output=output[0]):
                self.assertEqual(
                    gui_module.classify_platformio_failure(output), "cache"
                )

        self.assertEqual(
            gui_module.classify_platformio_failure(
                ["UnknownBoard: Unknown board ID 'not-installed'"]
            ),
            "configuration",
        )

        # Warnings and notes do not explain the non-zero exit and therefore
        # must not mask an explicit corrupt-cache signature.
        warning_plus_cache = (
            [
                "src/main.cpp:4:2: warning: unused variable 'led'",
                "sqlite3.DatabaseError: database disk image is malformed",
            ],
            [
                "src/main.cpp:8: note: in expansion of macro 'PIN'",
                "SCons: pickle data was truncated while reading .sconsign.dblite",
            ],
        )
        for output in warning_plus_cache:
            with self.subTest(output=output):
                self.assertEqual(
                    gui_module.classify_platformio_failure(output), "cache"
                )

    def test_stale_path_cleanup_preserves_active_env_and_rejects_outside_root(self):
        build_root = self.temp_root / "selected-board-workspace" / "build"
        active_env = build_root / "active_env"
        stale_sibling = build_root / "stale_env"
        outside_root = self.temp_root / "different-board-workspace"
        active_env.mkdir(parents=True)
        stale_sibling.mkdir(parents=True)
        outside_root.mkdir()
        (active_env / "firmware.bin").write_bytes(b"successful-build")
        (stale_sibling / "partial.o").write_bytes(b"stale")
        (outside_root / "firmware.bin").write_bytes(b"other-board")

        with mock.patch.object(
            gui_module, "robust_rmtree", wraps=gui_module.robust_rmtree
        ) as remove_tree:
            self.app._auto_clean_stale_build_paths(
                [str(build_root), str(stale_sibling), str(outside_root)],
                "active_env",
                build_ok=True,
                build_root=build_root,
            )

        remove_tree.assert_called_once_with(stale_sibling.resolve(strict=False))
        self.assertEqual(
            (active_env / "firmware.bin").read_bytes(), b"successful-build"
        )
        self.assertFalse(stale_sibling.exists())
        self.assertEqual(
            (outside_root / "firmware.bin").read_bytes(), b"other-board"
        )
        rendered = "\n".join(message for message, _tag in self.messages)
        self.assertIn("Preserved the successful selected-board build", rendered)
        self.assertIn("Ignored unsafe stale-cache path outside", rendered)

    def test_sync_preserves_generated_ino_and_scons_state_while_breaking_hardlink(self):
        original = self.project / "sketch.ino"
        original.write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
        staged_dir = self.project / "src"
        staged_dir.mkdir()
        staged = staged_dir / original.name
        try:
            os.link(original, staged)
        except OSError as exc:
            self.skipTest(f"Hard links are unavailable on this filesystem: {exc}")

        generated = staged_dir / "sketch.ino.cpp"
        generated.write_text("// generated by PlatformIO\n", encoding="utf-8")
        build_dir = self.app._board_build_dir()
        build_dir.mkdir(parents=True)
        sconsign = build_dir / ".sconsign39.dblite"
        sconsign.write_bytes(b"valid-scons-state")
        generated_stat = generated.stat()
        sconsign_stat = sconsign.stat()

        self.app._sync_src_dir()

        self.assertFalse(os.path.samefile(original, staged))
        self.assertEqual(generated.read_text(encoding="utf-8"), "// generated by PlatformIO\n")
        self.assertEqual(sconsign.read_bytes(), b"valid-scons-state")
        self.assertEqual(generated.stat().st_mtime_ns, generated_stat.st_mtime_ns)
        self.assertEqual(sconsign.stat().st_mtime_ns, sconsign_stat.st_mtime_ns)
        self.assertFalse(any(".freeze-" in path.name for path in staged_dir.iterdir()))

    def test_sync_detects_same_size_same_mtime_edits_and_stages_hpp(self):
        source = self.project / "board_traits.hpp"
        source.write_bytes(b"new-value\n")
        staged_dir = self.project / "src"
        staged_dir.mkdir()
        staged = staged_dir / source.name
        staged.write_bytes(b"old-value\n")

        # FAT/exFAT can report the same coarse timestamp for two different
        # same-length writes. Content comparison must still refresh the stage.
        fixed_time_ns = 1_700_000_000_000_000_000
        os.utime(source, ns=(fixed_time_ns, fixed_time_ns))
        os.utime(staged, ns=(fixed_time_ns, fixed_time_ns))
        self.assertEqual(source.stat().st_size, staged.stat().st_size)
        self.assertEqual(source.stat().st_mtime_ns, staged.stat().st_mtime_ns)

        self.app._sync_src_dir()

        self.assertEqual(staged.read_bytes(), source.read_bytes())
        self.assertEqual(staged.read_bytes(), b"new-value\n")

    def test_dynamic_s3_memory_and_flash_options_do_not_leak_across_switches(self):
        boards = {
            "S3 Native PSRAM": {
                "platform": "espressif32",
                "board": "esp32-s3-devkitc-1",
                "framework": "arduino",
                "flash_mb": 16.0,
                "has_psram": True,
                "memory_type": "qio_opi",
                "flash_mode": "dio",
            },
            "S3 Serial PSRAM": {
                "platform": "espressif32",
                "board": "esp32-s3-devkitc-1",
                "framework": "arduino",
                "flash_mb": 4,
                "has_psram": True,
                "memory_type": "opi_opi",
                "flash_mode": "dout",
            },
            "S3 Dynamic Template": {
                "platform": "espressif32",
                "board": "esp32-s3-devkitc-1",
                "framework": "arduino",
                "flash_mb": 8,
                "has_psram": False,
                "memory_type": "{build.boot}_{build.psram_type}",
                "flash_mode": "qio",
            },
            "Plain ESP32": {
                "platform": "espressif32",
                "board": "esp32dev",
                "framework": "arduino",
            },
            "Arduino Uno": dict(FAKE_BOARDS["Arduino Uno"]),
        }
        native_usb = {"enabled": True}
        self.app.upload_speed_var = _MutableVar("460800")
        self.app._scan_includes_for_libs = lambda: []
        self.app._append_notif = lambda *args, **kwargs: None
        self.app._is_native_usb_port = lambda: native_usb["enabled"]

        def prepare(board_name):
            self.app.board_var.set(board_name)
            self.assertTrue(self.app._ensure_platformio_ini())
            return (self.project / "platformio.ini").read_text(encoding="utf-8")

        with (
            mock.patch.object(gui_module, "SUPPORTED_BOARDS", boards),
            mock.patch.object(gui_module, "hide_internal_project_metadata"),
            mock.patch.object(
                gui_module,
                "heal_platformio_ini_symlinks_and_dirs",
                return_value=False,
            ),
            mock.patch.object(
                gui_module, "_get_download_dir", return_value=str(self.temp_root)
            ),
        ):
            native_ini = prepare("S3 Native PSRAM")
            self.assertIn("board_build.flash_size = 16MB", native_ini)
            self.assertIn("board_upload.flash_size = 16MB", native_ini)
            self.assertIn("board_build.arduino.memory_type = qio_opi", native_ini)
            self.assertIn("board_build.flash_mode = dio", native_ini)
            self.assertIn("-D BOARD_HAS_PSRAM", native_ini)
            self.assertIn("-DARDUINO_USB_MODE=1", native_ini)
            self.assertIn("-DARDUINO_USB_CDC_ON_BOOT=1", native_ini)
            self.assertNotIn("-mfix-esp32-psram-cache-issue", native_ini)
            self.assertNotIn("upload_speed =", native_ini)

            native_usb["enabled"] = False
            serial_ini = prepare("S3 Serial PSRAM")
            self.assertIn("board_build.flash_size = 4MB", serial_ini)
            self.assertIn("board_upload.flash_size = 4MB", serial_ini)
            self.assertIn("board_build.arduino.memory_type = opi_opi", serial_ini)
            self.assertIn("board_build.flash_mode = dout", serial_ini)
            self.assertIn("-D BOARD_HAS_PSRAM", serial_ini)
            self.assertIn("upload_speed = 460800", serial_ini)
            self.assertNotIn("ARDUINO_USB_MODE", serial_ini)
            self.assertNotIn("ARDUINO_USB_CDC_ON_BOOT", serial_ini)
            self.assertNotIn("qio_opi", serial_ini)

            template_ini = prepare("S3 Dynamic Template")
            self.assertIn("board_build.flash_mode = qio", template_ini)
            self.assertIn("board_build.flash_size = 8MB", template_ini)
            self.assertNotIn("board_build.arduino.memory_type", template_ini)
            self.assertNotIn("{build.", template_ini)
            self.assertNotIn("BOARD_HAS_PSRAM", template_ini)

            plain_ini = prepare("Plain ESP32")
            self.assertNotIn("board_build.flash_mode", plain_ini)
            self.assertNotIn("board_build.flash_size", plain_ini)
            self.assertNotIn("board_upload.flash_size", plain_ini)
            self.assertNotIn("board_build.arduino.memory_type", plain_ini)
            self.assertNotIn("BOARD_HAS_PSRAM", plain_ini)
            self.assertNotIn("ARDUINO_USB_MODE", plain_ini)

            uno_ini = prepare("Arduino Uno")
            self.assertIn("platform = atmelavr", uno_ini)
            self.assertIn("board = uno", uno_ini)
            self.assertIn("monitor_speed = 9600", uno_ini)
            self.assertIn("upload_speed = 115200", uno_ini)
            self.assertNotIn("upload_protocol = esptool", uno_ini)
            self.assertNotIn("board_build.flash_mode", uno_ini)
            self.assertNotIn("board_build.flash_size", uno_ini)
            self.assertNotIn("board_build.arduino.memory_type", uno_ini)
            self.assertNotIn("BOARD_HAS_PSRAM", uno_ini)
            self.assertNotIn("ARDUINO_USB_MODE", uno_ini)

    def test_reset_cache_uses_canonical_template_and_manifest_gated_fast_path(self):
        board_name = "Reset S3"
        board_info = {
            "platform": "espressif32",
            "board": "esp32-s3-devkitc-1",
            "framework": "arduino",
            "flash_mb": 8,
            "has_psram": True,
            "memory_type": "opi_opi",
            "flash_mode": "qout",
        }
        project_dir = self.temp_root / "reset-cache"
        project_dir.mkdir()
        build_dir = project_dir / ".pio" / "build" / "mcu_flash"
        build_dir.mkdir(parents=True)
        boot_app0 = self.temp_root / "boot_app0.bin"
        boot_app0.write_bytes(b"boot-app")
        self.app._is_native_usb_port = lambda: False

        with (
            mock.patch.object(
                gui_module, "SUPPORTED_BOARDS", {board_name: board_info}
            ),
            mock.patch.object(
                self.app,
                "_installed_platform_version",
                return_value="test-platform-1.2.3",
            ),
            mock.patch.object(gui_module, "hide_hidden_attribute"),
        ):
            first_template = self.app._reset_project_contents(
                board_name, board_info
            )
            second_template = self.app._reset_project_contents(
                board_name, dict(board_info)
            )
            self.assertEqual(first_template, second_template)
            ini_content, cpp_content, monitor_speed = first_template
            self.assertEqual(monitor_speed, "115200")
            self.assertIn("board_build.flash_mode = qout", ini_content)
            self.assertIn("board_build.flash_size = 8MB", ini_content)
            self.assertIn(
                "board_build.arduino.memory_type = opi_opi", ini_content
            )
            self.assertIn("-D BOARD_HAS_PSRAM", ini_content)

            (project_dir / "platformio.ini").write_text(
                ini_content, encoding="utf-8"
            )
            (project_dir / "main.cpp").write_text(cpp_content, encoding="utf-8")
            (build_dir / "bootloader.bin").write_bytes(b"bootloader")
            (build_dir / "partitions.bin").write_bytes(b"partitions")
            firmware = build_dir / "firmware.bin"
            firmware.write_bytes(b"firmware-v1")

            wrote_manifest, manifest_error = self.app._write_reset_manifest(
                project_dir, board_name, board_info
            )
            self.assertTrue(wrote_manifest, manifest_error)
            manifest_data = json.loads(
                (project_dir / "hard_reset_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest_data["board_key"],
                self.app._board_cache_key(board_name),
            )
            self.assertEqual(
                manifest_data["platform_version"], "test-platform-1.2.3"
            )

            validated, validation_error = self.app._validate_reset_manifest(
                project_dir,
                board_name,
                board_info,
                ("bootloader.bin", "partitions.bin", "firmware.bin"),
            )
            self.assertIsNotNone(validated, validation_error)

            with (
                mock.patch.object(
                    self.app,
                    "_soft_reset_project_dir",
                    return_value=project_dir,
                ),
                mock.patch.object(
                    self.app,
                    "_migrate_legacy_reset_project",
                ),
            ):
                hard_images, hard_error = (
                    self.app._locate_hard_reset_recovery_images(
                        board_name, board_info
                    )
                )
                self.assertIsNotNone(hard_images, hard_error)

                manifest_path = project_dir / "hard_reset_manifest.json"
                missing_hash_manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                del missing_hash_manifest["sha256"]["bootloader.bin"]
                manifest_path.write_text(
                    json.dumps(missing_hash_manifest), encoding="utf-8"
                )
                missing_hash_images, missing_hash_error = (
                    self.app._locate_hard_reset_recovery_images(
                        board_name, board_info
                    )
                )
                self.assertIsNone(missing_hash_images)
                self.assertIn("validated hash", missing_hash_error)
                manifest_path.write_text(
                    json.dumps(manifest_data), encoding="utf-8"
                )

            with (
                mock.patch.object(
                    self.app,
                    "_locate_esp32_boot_app0",
                    return_value=boot_app0,
                ),
                mock.patch.object(
                    self.app,
                    "_esptool_target",
                    return_value=("esp32s3", "0x0"),
                ),
            ):
                fast_path = self.app._locate_soft_reset_fast_binaries(
                    project_dir,
                    board_name,
                    "espressif32",
                    require_reset_manifest=True,
                )
                self.assertIsNotNone(fast_path)
                self.assertEqual(fast_path["firmware"], firmware)

                firmware.write_bytes(b"firmware-tampered")
                self.assertIsNone(
                    self.app._locate_soft_reset_fast_binaries(
                        project_dir,
                        board_name,
                        "espressif32",
                        require_reset_manifest=True,
                    )
                )
                # The binary is otherwise discoverable; rejection above is
                # specifically the manifest integrity gate, not a missing file.
                self.assertIsNotNone(
                    self.app._locate_soft_reset_fast_binaries(
                        project_dir,
                        board_name,
                        "espressif32",
                        require_reset_manifest=False,
                    )
                )

    def test_user_build_flags_invalidate_cache_but_upload_settings_do_not(self):
        source = self.project / "sketch.ino"
        source.write_text(
            "void setup() {}\nvoid loop() {}\n", encoding="utf-8"
        )
        build_dir = self.app._board_build_dir(board_name="Board A")
        build_dir.mkdir(parents=True)
        (build_dir / "firmware.bin").write_bytes(b"cached-firmware")
        self.app._is_framework_downloaded = lambda _board: True
        self.app._just_created_envs = set()

        def write_ini(flag_value, upload_speed, monitor_speed):
            env_name = self.app._pio_env_name("Board A")
            (self.project / "platformio.ini").write_text(
                "\n".join(
                    (
                        "[platformio]",
                        f"default_envs = {env_name}",
                        "",
                        f"[env:{env_name}]",
                        "platform = espressif32",
                        "board = shared_board_id",
                        "framework = arduino",
                        "build_flags =",
                        f"    -D FEATURE_LEVEL={flag_value}",
                        f"upload_speed = {upload_speed}",
                        f"monitor_speed = {monitor_speed}",
                        "",
                    )
                ),
                encoding="utf-8",
            )

        write_ini(1, 460800, 115200)
        initial_fingerprint = self.app._build_config_fingerprint(
            "Board A", allow_cached=False
        )
        initial_hash = self.app._hash_sources()
        board_key = self.app._board_cache_key("Board A")
        self.app._compile_cache_by_board[board_key] = initial_hash

        write_ini(1, 921600, 57600)
        self.assertEqual(
            self.app._build_config_fingerprint(
                "Board A", allow_cached=False
            ),
            initial_fingerprint,
        )
        needs_recompile, reason = self.app._needs_recompile()
        self.assertFalse(needs_recompile, reason)

        write_ini(2, 921600, 57600)
        self.assertNotEqual(
            self.app._build_config_fingerprint(
                "Board A", allow_cached=False
            ),
            initial_fingerprint,
        )
        needs_recompile, reason = self.app._needs_recompile()
        self.assertTrue(needs_recompile)
        self.assertIn("source files have changed", reason)

    def test_soft_reset_projects_are_exact_board_paths(self):
        with mock.patch.object(gui_module, "SCRIPT_DIR", self.script_root):
            reset_a = self.app._soft_reset_project_dir(
                "Board A", FAKE_BOARDS["Board A"]
            )
            reset_b = self.app._soft_reset_project_dir(
                "Board B", FAKE_BOARDS["Board B"]
            )
            reset_uno = self.app._soft_reset_project_dir(
                "Arduino Uno", FAKE_BOARDS["Arduino Uno"]
            )

        self.assertEqual(
            reset_a,
            self.script_root
            / "soft_reset_project"
            / "boards"
            / self.app._board_cache_key("Board A"),
        )
        self.assertEqual(
            reset_b,
            self.script_root
            / "soft_reset_project"
            / "boards"
            / self.app._board_cache_key("Board B"),
        )
        self.assertEqual(
            reset_uno,
            self.script_root
            / "soft_reset_project_uno"
            / "boards"
            / self.app._board_cache_key("Arduino Uno"),
        )
        self.assertNotEqual(reset_a, reset_b)

    def test_clean_removes_temp_metadata_compiled_and_reset_caches_only_in_temp_roots(self):
        metadata_root = self.temp_root / "system-temp-metadata"
        metadata_root.mkdir()

        def temp_metadata_path(_project, filename):
            return metadata_root / filename

        targets = (
            self.project / ".pio" / "boards" / "cached-board",
            self.project / "compiled_builds" / "ESP32",
            metadata_root / ".mcu_gui_cache.json",
            metadata_root / ".mcu_flash_syntax_errors.json",
            self.script_root / "soft_reset_project" / "boards" / "esp-cache",
            self.script_root / "soft_reset_project_uno" / "boards" / "uno-cache",
            self.script_root / ".pio_cache" / "legacy-cache",
        )
        for target in targets:
            if target.suffix == ".json":
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}", encoding="utf-8")
            else:
                target.mkdir(parents=True, exist_ok=True)
                (target / "artifact.bin").write_bytes(b"cache")
        source = self.project / "sketch.ino"
        source.write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")

        with (
            mock.patch.object(gui_module, "SCRIPT_DIR", self.script_root),
            mock.patch.object(
                gui_module,
                "get_project_temp_file",
                side_effect=temp_metadata_path,
            ),
        ):
            clean_targets = {path for path, _label in self.app._clean_targets()}
            self.assertIn(self.project / "compiled_builds", clean_targets)
            self.assertIn(metadata_root / ".mcu_gui_cache.json", clean_targets)
            self.assertIn(
                metadata_root / ".mcu_flash_syntax_errors.json", clean_targets
            )
            self.assertIn(
                self.script_root / "soft_reset_project" / "boards", clean_targets
            )
            self.assertIn(
                self.script_root / "soft_reset_project_uno" / "boards",
                clean_targets,
            )
            removed, errors = self.app._perform_clean()

        self.assertEqual(errors, [])
        self.assertIn("legacy compiled binaries", removed)
        self.assertIn("compile metadata", removed)
        self.assertIn("syntax metadata", removed)
        self.assertIn("Soft/Hard Reset board caches", removed)
        self.assertIn("Arduino reset board caches", removed)
        self.assertIn("legacy app-wide SCons cache", removed)
        for target in targets:
            self.assertFalse(target.exists(), target)
        self.assertTrue(source.is_file())
        self.assertEqual(self.app._compile_cache_by_board, {})
        self.assertEqual(self.app._build_metadata_by_board, {})
        self.assertIsNone(self.app._last_compiled_board)


if __name__ == "__main__":
    unittest.main()
