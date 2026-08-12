import ctypes
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import mcu_flash_gui as gui_module


class ProjectMetadataVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary_directory.name) / "sketch"
        self.project.mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_generated_file_is_hidden_and_readonly_bit_is_cleared(self):
        generated_file = self.project / ".mcu_gui_compat_cache.json"
        generated_file.write_text("{}", encoding="utf-8")
        kernel32 = mock.Mock()
        kernel32.GetFileAttributesW.return_value = 0x20
        fake_windll = SimpleNamespace(kernel32=kernel32)

        with (
            mock.patch.object(gui_module.sys, "platform", "win32"),
            mock.patch.object(ctypes, "windll", fake_windll),
        ):
            gui_module.hide_hidden_attribute(generated_file)

        kernel32.SetFileAttributesW.assert_called_once_with(
            str(generated_file), 0x22
        )

    def test_generated_directory_is_hidden_without_touching_children(self):
        generated_dir = self.project / ".pio"
        generated_dir.mkdir()
        child = generated_dir / "project.checksum"
        child.write_text("checksum", encoding="utf-8")
        kernel32 = mock.Mock()
        kernel32.GetFileAttributesW.return_value = 0x10
        fake_windll = SimpleNamespace(kernel32=kernel32)

        with (
            mock.patch.object(gui_module.sys, "platform", "win32"),
            mock.patch.object(ctypes, "windll", fake_windll),
        ):
            gui_module.hide_generated_directory(generated_dir)

        kernel32.GetFileAttributesW.assert_called_once_with(str(generated_dir))
        kernel32.SetFileAttributesW.assert_called_once_with(
            str(generated_dir), 0x12
        )
        self.assertEqual(child.read_text(encoding="utf-8"), "checksum")

    def test_ensure_file_writable_clears_readonly_but_preserves_hidden_system(self):
        generated_file = self.project / "platformio.ini"
        generated_file.write_text("[platformio]\n", encoding="utf-8")
        kernel32 = mock.Mock()
        # READONLY + HIDDEN + SYSTEM. Writability repair must clear only the
        # readonly bit so generated files do not flash visible during updates.
        kernel32.GetFileAttributesW.return_value = 0x07
        fake_windll = SimpleNamespace(kernel32=kernel32)

        with (
            mock.patch.object(gui_module.sys, "platform", "win32"),
            mock.patch.object(ctypes, "windll", fake_windll),
        ):
            gui_module.ensure_file_writable(generated_file)

        self.assertEqual(
            kernel32.SetFileAttributesW.call_args_list,
            [mock.call(str(generated_file), 0x06)],
        )
        with generated_file.open("a", encoding="utf-8") as stream:
            stream.write("default_envs = test\n")
        self.assertIn("default_envs", generated_file.read_text(encoding="utf-8"))

    def test_metadata_reconciliation_hides_only_generated_allowlist(self):
        generated_file_names = {
            "platformio.ini",
            ".mcu_gui_cache.json",
            ".mcu_flash_syntax_errors.json",
            ".mcu_gui_compat_cache.json",
            ".mcu_flash_tab_order.json",
            ".ai_edit_signal",
        }
        generated_dir_names = {
            ".pio",
            "src",
            "MCU-FLASHER-SRC",
            "MCU_FLASHER_SRC",
            "compiled_builds",
            "build_artifacts",
            ".build_artifacts",
            ".pio_cache",
            ".mcu_ai_edits",
        }
        generated_files = {self.project / name for name in generated_file_names}
        generated_dirs = {self.project / name for name in generated_dir_names}
        user_files = {
            self.project / "sketch.ino",
            self.project / "module.cpp",
            self.project / "driver.c",
            self.project / "pins.h",
            self.project / "types.hpp",
            self.project / "NOTE.txt",
            self.project / "README.md",
            self.project / "wiring.pdf",
            self.project / "settings.json",
            self.project / ".opencodeignore",
            self.project / ".ignore",
            self.project / "AGENTS.md",
            self.project / "OPENCODE.md",
            self.project / "temp.json",
            self.project / "here.txt",
            self.project / "compile_commands.json",
            self.project / "READ-FIRST.md",
            self.project / ".READ-FIRST.md",
            self.project / "SKILL.md",
            self.project / ".SKILL.md",
        }
        user_dirs = {
            self.project / "data",
            self.project / ".vscode",
            self.project / ".clangd",
            self.project / ".cache",
            self.project / "_temp",
            self.project / "logs",
        }

        for path in generated_files | user_files:
            path.write_text("test", encoding="utf-8")
        for path in generated_dirs | user_dirs:
            path.mkdir()

        with (
            mock.patch.object(gui_module, "ensure_hidden_read_first_md"),
            mock.patch.object(gui_module, "ensure_file_writable") as make_writable,
            mock.patch.object(gui_module, "hide_hidden_attribute") as hide_file,
            mock.patch.object(gui_module, "hide_generated_directory") as hide_dir,
            mock.patch.object(gui_module, "unhide_hidden_attribute") as show_file,
        ):
            gui_module.hide_internal_project_metadata(self.project)

        hidden_paths = {Path(call.args[0]) for call in hide_file.call_args_list}
        hidden_dirs = {Path(call.args[0]) for call in hide_dir.call_args_list}
        shown_paths = {Path(call.args[0]) for call in show_file.call_args_list}

        self.assertTrue(generated_files <= hidden_paths)
        self.assertTrue(generated_dirs <= hidden_dirs)
        self.assertTrue(generated_dirs.isdisjoint(hidden_paths))
        self.assertEqual(shown_paths, user_files | {
            path for path in user_dirs if not path.name.startswith(".")
        })
        self.assertTrue(user_files.isdisjoint(hidden_paths))
        self.assertTrue(user_dirs.isdisjoint(hidden_paths))
        self.assertTrue(user_dirs.isdisjoint(hidden_dirs))
        make_writable.assert_called_once_with(self.project / "platformio.ini")

    def test_generated_ai_scope_files_are_written_writable_then_hidden(self):
        with (
            mock.patch.object(gui_module, "ensure_file_writable") as make_writable,
            mock.patch.object(gui_module, "hide_hidden_attribute") as hide_file,
        ):
            gui_module.ensure_hidden_read_first_md(self.project)

        ignore_file = self.project / ".opencodeignore"
        agents_file = self.project / "AGENTS.md"
        self.assertTrue(ignore_file.is_file())
        self.assertTrue(agents_file.is_file())
        self.assertIn(".pio/", ignore_file.read_text(encoding="utf-8"))
        self.assertIn("src/", ignore_file.read_text(encoding="utf-8"))
        self.assertIn(
            "ONLY READ & EDIT MAIN SKETCH FILES",
            agents_file.read_text(encoding="utf-8"),
        )
        writable_paths = {Path(call.args[0]) for call in make_writable.call_args_list}
        hidden_paths = {Path(call.args[0]) for call in hide_file.call_args_list}
        self.assertEqual(writable_paths, {ignore_file, agents_file})
        self.assertEqual(hidden_paths, {ignore_file, agents_file})

    def test_ai_scope_generation_preserves_user_instruction_files(self):
        user_agents = self.project / "AGENTS.md"
        user_ignore = self.project / ".opencodeignore"
        user_skill = self.project / "SKILL.md"
        legacy_generated = self.project / "READ-FIRST.md"
        user_agents.write_text("# My project rules\n", encoding="utf-8")
        user_ignore.write_text("vendor/\n", encoding="utf-8")
        user_skill.write_text("# My board skill\n", encoding="utf-8")
        legacy_generated.write_text(
            "# Auto-generated by MCU Flash GUI for OpenCode AI\n",
            encoding="utf-8",
        )

        gui_module.ensure_hidden_read_first_md(self.project)

        self.assertEqual(user_agents.read_text(encoding="utf-8"), "# My project rules\n")
        self.assertEqual(user_ignore.read_text(encoding="utf-8"), "vendor/\n")
        self.assertEqual(user_skill.read_text(encoding="utf-8"), "# My board skill\n")
        self.assertFalse(legacy_generated.exists())

    def test_codebase_root_keeps_source_tree_visible(self):
        codebase = self.project / "codebase"
        (codebase / "src" / "modules").mkdir(parents=True)
        (codebase / "platformio.ini").write_text("[platformio]\n", encoding="utf-8")
        (codebase / "index_json").mkdir()

        with (
            mock.patch.object(gui_module, "SCRIPT_DIR", codebase),
            mock.patch.object(gui_module, "ensure_hidden_read_first_md"),
            mock.patch.object(gui_module, "ensure_file_writable"),
            mock.patch.object(gui_module, "hide_hidden_attribute") as hide_file,
            mock.patch.object(gui_module, "hide_generated_directory") as hide_dir,
            mock.patch.object(gui_module, "unhide_hidden_attribute") as show_path,
        ):
            gui_module.hide_internal_project_metadata(codebase)

        hidden_dirs = {Path(call.args[0]) for call in hide_dir.call_args_list}
        shown_paths = {Path(call.args[0]) for call in show_path.call_args_list}
        self.assertIn(codebase / "index_json", hidden_dirs)
        self.assertNotIn(codebase / "src", hidden_dirs)
        self.assertNotIn(codebase / "src" / "modules", hidden_dirs)
        self.assertIn(codebase, shown_paths)
        self.assertIn(codebase / "src", shown_paths)
        self.assertIn(codebase / "src" / "modules", shown_paths)
        self.assertIn(codebase / "platformio.ini", shown_paths)
        self.assertFalse(hide_file.called)

    def test_copied_download_setting_is_repaired_for_current_user(self):
        codebase = self.project / "codebase"
        codebase.mkdir()
        settings_file = codebase / "arduino_browser_settings.json"
        settings_file.write_text(
            '{"download_dir": "C:\\\\Users\\\\PreviousUser\\\\Documents"}',
            encoding="utf-8",
        )

        with mock.patch.object(gui_module, "SCRIPT_DIR", codebase):
            download_dir = Path(gui_module._get_download_dir())

        expected = Path.home() / "Documents" / "_MCUFlasherByNaph_src"
        self.assertEqual(download_dir, expected)
        repaired = json.loads(settings_file.read_text(encoding="utf-8"))
        self.assertEqual(Path(repaired["download_dir"]), expected)

    def test_temp_cache_migration_does_not_delete_unknown_user_file(self):
        user_file = self.project / "notes.json"
        user_file.write_text("{\"keep\": true}", encoding="utf-8")

        gui_module.get_project_temp_file(self.project, "notes.json")

        self.assertEqual(user_file.read_text(encoding="utf-8"), '{"keep": true}')


if __name__ == "__main__":
    unittest.main()
