#!/usr/bin/env python3
"""MCU Flasher's Start menu entry on Windows: what Setup runs, and the shortcut Windows reads.

Everywhere: the child process Setup starts, its failures in words, Bootstrap's guard, and that
the writer needs only the standard library. On Windows also real shortcuts in a temporary
folder, written and read back through the shell; this account's Start menu is never touched.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.modules import start_menu  # noqa: E402


def app_folder(base: Path) -> Path:
    """A stand-in application folder with its launcher and icon."""
    root = base / "MCU Flasher"
    (root / "LAUNCHER").mkdir(parents=True)
    (root / "LAUNCHER" / "WINDOWS.exe").write_bytes(b"MZ")
    (root / "src" / "assets").mkdir(parents=True)
    (root / "src" / "assets" / "mcu_icon.ico").write_bytes(b"\0\0\1\0")
    return root


class SetupCall(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mcu-start-menu-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = app_folder(self.base)
        self.folder = self.base / "Programs"
        self.calls = []

    def ensure(self, result=None, error=None):
        def run(command, **options):
            self.calls.append((command, options))
            if error is not None:
                raise error
            return result or SimpleNamespace(returncode=0, stderr="")
        return start_menu.ensure_entry(self.root, run=run, folder=self.folder)

    def test_setup_writes_it_in_a_child_process(self):
        ok, message = self.ensure()
        self.assertTrue(ok)
        self.assertEqual(message, "MCU Flasher by Naph is in the Start menu, so Windows Search finds it by name.")
        [(command, options)] = self.calls
        self.assertEqual(command, [sys.executable, "-I", str(Path(start_menu.__file__).resolve()),
                                   str(self.folder / "MCU Flasher by Naph.lnk"),
                                   str(self.root / "LAUNCHER" / "WINDOWS.exe"),
                                   str(self.root / "src" / "assets" / "mcu_icon.ico"), str(self.root)])
        self.assertEqual((options["timeout"], options["capture_output"]), (start_menu.WRITE_SECONDS, True))

    def test_failures_are_words_and_never_exceptions(self):
        ok, message = self.ensure(SimpleNamespace(returncode=1, stderr="first\nSave failed (0x80070005)\n"))
        self.assertEqual((ok, message), (False, "The Start menu entry could not be written: Save failed (0x80070005)"))
        self.assertEqual(self.ensure(SimpleNamespace(returncode=3, stderr=""))[1],
                         "The Start menu entry could not be written: exit 3")
        ok, message = self.ensure(error=subprocess.TimeoutExpired("python", start_menu.WRITE_SECONDS))
        self.assertFalse(ok)
        self.assertIn("timed out", message)
        ok, message = self.ensure(error=OSError("no python"))
        self.assertEqual((ok, message), (False, "The Start menu entry could not be written: no python"))
        (self.root / "LAUNCHER" / "WINDOWS.exe").unlink()
        ok, message = self.ensure()
        self.assertFalse(ok)
        self.assertIn("WINDOWS.exe is missing", message)
        self.assertEqual(len(self.calls), 4, "Nothing runs without the launcher")

    def test_the_icon_falls_back_to_the_launcher(self):
        (self.root / "src" / "assets" / "mcu_icon.ico").unlink()
        self.ensure()
        self.assertEqual(self.calls[0][0][5], str(self.root / "LAUNCHER" / "WINDOWS.exe"))

    @unittest.skipIf(sys.platform == "win32", "Windows writes the entry")
    def test_other_hosts_do_nothing(self):
        self.assertEqual(start_menu.ensure_entry(self.root), (True, ""))
        self.assertIsNone(start_menu.programs_folder())
        self.assertFalse(self.folder.exists())

    def test_the_writer_needs_only_the_standard_library(self):
        tree = ast.parse(Path(start_menu.__file__).read_text(encoding="utf-8"))
        names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                 for alias in node.names}
        names |= {node.module.split(".")[0] for node in ast.walk(tree)
                  if isinstance(node, ast.ImportFrom) and node.module}
        self.assertLessEqual(names, set(sys.stdlib_module_names) | {"__future__"})

    def test_methods_are_where_their_interfaces_put_them(self):
        # Each interface's own methods follow IUnknown's QueryInterface, AddRef and Release.
        link = ["GetPath", "GetIDList", "SetIDList", "GetDescription", "SetDescription", "GetWorkingDirectory",
                "SetWorkingDirectory", "GetArguments", "SetArguments", "GetHotkey", "SetHotkey", "GetShowCmd",
                "SetShowCmd", "GetIconLocation", "SetIconLocation", "SetRelativePath", "Resolve", "SetPath"]
        persist = ["GetClassID", "IsDirty", "Load", "Save", "SaveCompleted", "GetCurFile"]
        store = ["GetCount", "GetAt", "GetValue", "SetValue", "Commit"]
        for constant, methods, name in (
                ("_LINK_GET_PATH", link, "GetPath"), ("_LINK_SET_DESCRIPTION", link, "SetDescription"),
                ("_LINK_SET_WORKING_DIRECTORY", link, "SetWorkingDirectory"),
                ("_LINK_GET_ARGUMENTS", link, "GetArguments"), ("_LINK_SET_ARGUMENTS", link, "SetArguments"),
                ("_LINK_SET_ICON_LOCATION", link, "SetIconLocation"), ("_LINK_SET_PATH", link, "SetPath"),
                ("_FILE_LOAD", persist, "Load"), ("_FILE_SAVE", persist, "Save"),
                ("_STORE_GET_VALUE", store, "GetValue"), ("_STORE_SET_VALUE", store, "SetValue"),
                ("_STORE_COMMIT", store, "Commit")):
            with self.subTest(constant):
                self.assertEqual(getattr(start_menu, constant), 3 + methods.index(name))
        self.assertEqual((start_menu._QUERY_INTERFACE, start_menu._RELEASE), (0, 2))
        from main.qt import taskbar
        self.assertEqual(start_menu.APP_USER_MODEL_ID, taskbar.APP_USER_MODEL_ID, "One ID groups the windows")
        self.assertEqual(start_menu.NAME, taskbar.DISPLAY_NAME)
        self.assertEqual(bytes(start_menu._APP_ID_FORMAT), bytes(taskbar._APP_MODEL_FORMAT))
        self.assertEqual(start_menu._APP_ID, taskbar._ID)
        self.assertEqual(start_menu.ctypes.sizeof(start_menu._PropVariant), 24 if sys.maxsize > 2 ** 32 else 16)

    def test_bootstrap_writes_it_on_windows_only_and_never_stops_for_it(self):
        tree = ast.parse((ROOT / "src" / "modules" / "bootstrap.py").read_text(encoding="utf-8"))
        [call] = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                  and getattr(node.func, "id", "") == "ensure_entry"]

        def holds(node):
            return any(child is call for child in ast.walk(node))
        guard = [node for node in ast.walk(tree) if isinstance(node, ast.If) and holds(node)]
        self.assertTrue(guard)
        self.assertEqual(ast.unparse(guard[-1].test), "sys.platform == 'win32'")
        [attempt] = [node for node in ast.walk(guard[-1]) if isinstance(node, ast.Try) and holds(node)]
        self.assertEqual([ast.unparse(handler.type) for handler in attempt.handlers], ["Exception"])


@unittest.skipUnless(sys.platform == "win32", "The shell writes and reads shortcuts on Windows")
class RealShortcut(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mcu-start-menu-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def test_a_shortcut_is_written_whole_and_read_back(self):
        link = self.base / "Programs" / "MCU Flasher by Naph.lnk"
        target = Path(sys.executable)
        start_menu.write_link(link, target, target, self.base, arguments="--fixture")
        self.assertTrue(link.is_file())
        self.assertFalse(link.with_name(link.name + ".partial").exists())
        found = start_menu.read_link(link)
        self.assertTrue(os.path.samefile(found["target"], target), found)
        self.assertEqual((found["arguments"], found["app_id"]), ("--fixture", start_menu.APP_USER_MODEL_ID))
        # Written again, as Setup does on every run, it is replaced in place.
        start_menu.write_link(link, target, target, self.base)
        self.assertEqual(start_menu.read_link(link)["arguments"], "")
        self.assertEqual(sorted(path.name for path in link.parent.iterdir()), [link.name])

    def test_setup_writes_one_through_its_child_process(self):
        root = app_folder(self.base)
        folder = self.base / "Programs"
        ok, message = start_menu.ensure_entry(root, folder=folder)
        self.assertTrue(ok, message)
        found = start_menu.read_link(folder / "MCU Flasher by Naph.lnk")
        self.assertTrue(os.path.samefile(found["target"], root / "LAUNCHER" / "WINDOWS.exe"), found)
        self.assertEqual(found["app_id"], start_menu.APP_USER_MODEL_ID)

    def test_the_accounts_programs_folder_is_found(self):
        folder = start_menu.programs_folder()
        self.assertIsNotNone(folder)
        self.assertEqual(folder.name, "Programs")
        self.assertEqual(start_menu.entry_path(folder), folder / "MCU Flasher by Naph.lnk")


if __name__ == "__main__":
    unittest.main(verbosity=2)
