"""Hardware-free checks of the Windows taskbar properties in main/qt/taskbar.py.

The shell is a fake here, but it is built from real ctypes callbacks, so vtable slots,
structure layouts and call order are exercised rather than mocked away. Only the last test
class talks to the real shell, on Windows, using a hidden window that is never shown.
None of this proves what Explorer draws: that needs a person looking at a real taskbar.
"""
from __future__ import annotations

import ast
import contextlib
import ctypes
import io
import os
import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QWidget

from main.qt import taskbar

APP = QApplication.instance() or QApplication([])
APP_MODEL_FORMAT = uuid.UUID("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3").bytes_le
PROPERTY_STORE_IID = uuid.UUID("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99").bytes_le
COMMAND, ICON, NAME, IDENTITY = 2, 3, 4, 5
E_FAIL = -2147467259
FAKE_HWND = 0x1234
OPENER = taskbar._FUNCTION(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(taskbar._Guid),
                           ctypes.POINTER(ctypes.c_void_p))


class FakeShell:
    """An IPropertyStore laid out as COM expects, so a wrong slot calls the wrong function."""

    def __init__(self, open_result=0, value_result=0):
        self.open_result, self.value_result = open_result, value_result
        self.opened = []
        self.writes = []
        self.released = 0
        self._release = taskbar._RELEASE(self._on_release)
        self._set_value = taskbar._SET_VALUE(self._on_set_value)
        self._trap = ctypes.CFUNCTYPE(ctypes.c_long)(self._on_wrong_slot)
        self.wrong_slot_called = False
        self._vtable = (ctypes.c_void_p * 8)(*[ctypes.cast(self._trap, ctypes.c_void_p)] * 8)
        # Fixed by IPropertyStore's definition, independent of the code under test.
        self._vtable[2] = ctypes.cast(self._release, ctypes.c_void_p)
        self._vtable[6] = ctypes.cast(self._set_value, ctypes.c_void_p)
        self._instance = ctypes.c_void_p(ctypes.addressof(self._vtable))
        self.address = ctypes.addressof(self._instance)
        self.opener = OPENER(self._on_open)

    def _on_wrong_slot(self):
        self.wrong_slot_called = True
        return E_FAIL

    def _on_open(self, hwnd, iid, output):
        self.opened.append((hwnd, bytes(iid.contents)))
        if self.open_result >= 0:
            output[0] = self.address
        return self.open_result

    def _on_release(self, this):
        assert this == self.address
        self.released += 1
        return 0

    def _on_set_value(self, this, key, value):
        assert this == self.address
        assert bytes(key.contents.format) == APP_MODEL_FORMAT
        variant = value.contents
        self.writes.append((key.contents.id, variant.type, variant.text))
        return self.value_result

    def installed(self):
        return patch.object(ctypes, "windll", SimpleNamespace(
            shell32=SimpleNamespace(SHGetPropertyStoreForWindow=self.opener)), create=True)


class PropertyStoreChecks(unittest.TestCase):
    def test_structures_match_the_windows_layouts(self):
        pointer = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(taskbar._Guid), 16)
        self.assertEqual(ctypes.sizeof(taskbar._PropertyKey), 20)
        self.assertEqual(taskbar._PropVariant.text.offset, 8)
        self.assertEqual(ctypes.sizeof(taskbar._PropVariant), 8 + 2 * pointer)
        self.assertEqual(bytes(taskbar._APP_MODEL_FORMAT), APP_MODEL_FORMAT)
        self.assertEqual(bytes(taskbar._PROPERTY_STORE_IID), PROPERTY_STORE_IID)

    def test_every_property_is_stored_with_the_id_last(self):
        shell = FakeShell()
        with shell.installed():
            self.assertTrue(taskbar.set_window_identity(FAKE_HWND))
        self.assertEqual(shell.opened, [(FAKE_HWND, PROPERTY_STORE_IID)])
        self.assertEqual([write[0] for write in shell.writes], [COMMAND, NAME, ICON, IDENTITY])
        self.assertTrue(all(write[1] == 31 for write in shell.writes), "VT_LPWSTR")
        stored = {pid: text for pid, _, text in shell.writes}
        self.assertEqual(stored[IDENTITY], taskbar.APP_USER_MODEL_ID)
        self.assertEqual(stored[NAME], "MCU Flasher by Naph")
        self.assertEqual(stored[ICON], f"{ROOT / 'src' / 'assets' / 'mcu_icon.ico'},0")
        self.assertTrue((ROOT / "src/assets/mcu_icon.ico").is_file())
        self.assertEqual(shell.released, 1)
        self.assertFalse(shell.wrong_slot_called)

    def test_a_group_without_a_launcher_stores_no_half_of_the_relaunch_pair(self):
        shell = FakeShell()
        with shell.installed():
            self.assertTrue(taskbar.set_window_identity(FAKE_HWND, app_id="Other.Group", relaunch=False))
        self.assertEqual([write[0] for write in shell.writes], [ICON, IDENTITY])
        self.assertEqual(shell.writes[-1][2], "Other.Group")

    def test_clearing_removes_every_property(self):
        shell = FakeShell()
        with shell.installed():
            self.assertTrue(taskbar.clear_window_identity(FAKE_HWND))
        self.assertCountEqual([write[0] for write in shell.writes], [COMMAND, ICON, NAME, IDENTITY])
        self.assertTrue(all(write[1] == 0 and write[2] is None for write in shell.writes), "VT_EMPTY")
        self.assertEqual(shell.released, 1)

    def test_failures_are_reported_without_raising_or_leaking_the_store(self):
        refused = FakeShell(open_result=E_FAIL)
        with refused.installed():
            self.assertFalse(taskbar.set_window_identity(FAKE_HWND))
        self.assertEqual((refused.writes, refused.released), ([], 0))

        rejected = FakeShell(value_result=E_FAIL)
        with rejected.installed():
            self.assertFalse(taskbar.set_window_identity(FAKE_HWND))
        self.assertEqual(rejected.released, 1)

        with patch.object(ctypes, "windll", SimpleNamespace(shell32=SimpleNamespace()), create=True):
            self.assertFalse(taskbar.set_window_identity(FAKE_HWND))

        def crash(*_):
            raise OSError("exception: access violation")
        with patch.object(ctypes, "windll", SimpleNamespace(shell32=SimpleNamespace(
                SHGetPropertyStoreForWindow=crash)), create=True):
            self.assertFalse(taskbar.clear_window_identity(FAKE_HWND))

    def test_relaunch_goes_through_the_launcher_in_a_folder_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="mcu taskbar ") as folder:
            root = Path(folder)
            with patch.object(taskbar, "ROOT", root):
                script = root / "direct" / "windows" / "run.vbs"
                fallback = taskbar.relaunch_command()
                self.assertTrue(fallback.endswith(f'//nologo "{script}"'), fallback)
                self.assertIn('wscript.exe" ', fallback)

                launcher = root / "LAUNCHER" / "WINDOWS.exe"
                launcher.parent.mkdir()
                launcher.write_bytes(b"")
                self.assertEqual(taskbar.relaunch_command(), f'"{launcher}"')


class Window(QWidget):
    """The same two calls every real top-level window makes."""

    def __init__(self):
        super().__init__()
        self.native_visible_during_show = None

    def showEvent(self, event):
        super().showEvent(event)
        self.native_visible_during_show = self.windowHandle().isVisible()
        taskbar.window_shown(self)

    def hideEvent(self, event):
        super().hideEvent(event)
        taskbar.window_hidden(self, event)


def spin(milliseconds):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


class WindowHookChecks(unittest.TestCase):
    def setUp(self):
        self.identity = patch.object(taskbar, "set_window_identity", return_value=True).start()
        self.cleared = patch.object(taskbar, "clear_window_identity", return_value=True).start()
        patch.object(taskbar.sys, "platform", "win32").start()
        patch.object(taskbar, "REFRESH_DELAYS_MS", (1, 2)).start()
        self.addCleanup(patch.stopall)
        self.window = Window()
        self.addCleanup(self.window.deleteLater)
        self.window.setWindowIcon(QIcon(str(ROOT / "src/assets/mcu_icon.ico")))

    def test_properties_exist_before_the_native_window_appears(self):
        self.window.show()
        self.assertIs(self.window.native_visible_during_show, False)
        self.identity.assert_called_once_with(int(self.window.winId()), app_id=taskbar.APP_USER_MODEL_ID, relaunch=True)
        self.window.hide()

    def test_the_icon_and_id_are_sent_again_after_the_button_exists(self):
        with patch.object(self.window, "setWindowIcon") as resend:
            self.window.show()
            self.assertEqual(self.identity.call_count, 1)
            spin(40)
        self.assertEqual(resend.call_count, 2)
        self.assertEqual(self.identity.call_count, 3)
        self.window.hide()

    def test_closing_clears_the_properties_and_cancels_pending_refreshes(self):
        self.window.show()
        hwnd = int(self.window.winId())
        with patch.object(self.window, "setWindowIcon") as resend:
            self.window.close()
            self.cleared.assert_called_once_with(hwnd)
            spin(40)
        resend.assert_not_called()
        self.assertEqual(self.identity.call_count, 1)

    def test_showing_again_after_hiding_restores_the_properties(self):
        self.window.show()
        self.window.hide()
        self.window.show()
        self.assertEqual(self.identity.call_count, 2)
        self.window.hide()
        self.assertEqual(self.cleared.call_count, 2)

    def test_minimizing_and_restoring_keeps_the_button_untouched(self):
        self.window.show()
        taskbar.window_hidden(self.window, SimpleNamespace(spontaneous=lambda: True))
        self.cleared.assert_not_called()
        taskbar.window_shown(self.window)
        self.assertEqual(self.identity.call_count, 1)
        self.window.hide()

    def test_a_refusal_is_logged_and_the_window_still_shows(self):
        self.identity.return_value = False
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.window.show()
        self.assertIn("refused the taskbar properties", output.getvalue())
        self.assertTrue(self.window.isVisible())
        self.window.hide()

    def test_other_hosts_never_touch_the_window(self):
        window = MagicMock()
        with patch.object(taskbar.sys, "platform", "linux"):
            taskbar.window_shown(window)
            taskbar.window_hidden(window, SimpleNamespace(spontaneous=lambda: False))
        window.winId.assert_not_called()
        self.identity.assert_not_called()
        self.cleared.assert_not_called()


class WiringChecks(unittest.TestCase):
    OWNERS = {"main/qt/main_window.py": "MCUMainWindow",
              "main/qt/project_dialog.py": "ProjectDialog",
              "src/modules/bootstrap_qt.py": "BootstrapDialog",
              "main/qt/package_browser.py": "ArduinoBrowser"}

    def test_every_window_with_its_own_button_makes_both_calls(self):
        for path, owner in self.OWNERS.items():
            tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
            cls = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == owner)
            methods = {node.name: {call.func.id for call in ast.walk(node)
                                   if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}
                       for node in cls.body if isinstance(node, ast.FunctionDef)}
            self.assertIn("window_shown", methods.get("showEvent", ()), owner)
            self.assertIn("window_hidden", methods.get("hideEvent", ()), owner)

    def test_the_browser_keeps_its_own_group_and_offers_no_relaunch(self):
        source = (ROOT / "main/qt/package_browser.py").read_text(encoding="utf-8")
        self.assertIn("window_shown(self, app_id=BROWSER_USER_MODEL_ID, relaunch=False)", source)
        self.assertIn("SetCurrentProcessExplicitAppUserModelID(BROWSER_USER_MODEL_ID)", source)

    def test_process_and_window_ids_cannot_split_the_group(self):
        pattern = re.compile(r'SetCurrentProcessExplicitAppUserModelID\("([^"]+)"\)')
        for path in ("main/mcu_flash_gui.py", "src/modules/launcher.py", "src/modules/dedicated_AI.py"):
            found = pattern.findall((ROOT / path).read_text(encoding="utf-8"))
            self.assertTrue(found, path)
            self.assertEqual(set(found), {taskbar.APP_USER_MODEL_ID}, path)


@unittest.skipUnless(sys.platform == "win32", "real shell property store")
class NativeWindowsChecks(unittest.TestCase):
    """A real store on a real window, which stays hidden: WS_VISIBLE is never set."""

    GET_VALUE = taskbar._FUNCTION(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(taskbar._PropertyKey),
                                  ctypes.POINTER(taskbar._PropVariant))
    GET_VALUE_SLOT = 5
    WS_POPUP = 0x80000000

    def setUp(self):
        from ctypes import wintypes
        ctypes.windll.ole32.CoInitialize(None)
        create = ctypes.windll.user32.CreateWindowExW
        create.restype = wintypes.HWND
        create.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
        self.hwnd = create(0, "STATIC", "MCU Flasher taskbar probe", self.WS_POPUP, 0, 0, 1, 1, None, None, None, None)
        self.assertTrue(self.hwnd)
        destroy = ctypes.windll.user32.DestroyWindow
        destroy.argtypes = (wintypes.HWND,)
        self.addCleanup(destroy, self.hwnd)
        self.assertFalse(ctypes.windll.user32.IsWindowVisible(wintypes.HWND(self.hwnd)))

    def read(self, pid):
        store = ctypes.c_void_p()
        opener = ctypes.windll.shell32.SHGetPropertyStoreForWindow
        self.assertGreaterEqual(opener(self.hwnd, ctypes.byref(taskbar._PROPERTY_STORE_IID), ctypes.byref(store)), 0)
        vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        try:
            value = taskbar._PropVariant()
            key = taskbar._PropertyKey(taskbar._APP_MODEL_FORMAT, pid)
            self.assertGreaterEqual(self.GET_VALUE(vtable[self.GET_VALUE_SLOT])(store, ctypes.byref(key), ctypes.byref(value)), 0)
            return value.type, value.text
        finally:
            taskbar._RELEASE(vtable[2])(store)

    def test_the_shell_stores_what_was_written_and_forgets_it_when_cleared(self):
        self.assertTrue(taskbar.set_window_identity(self.hwnd))
        self.assertEqual(self.read(IDENTITY)[1], taskbar.APP_USER_MODEL_ID)
        self.assertEqual(self.read(NAME)[1], taskbar.DISPLAY_NAME)
        self.assertEqual(self.read(COMMAND)[1], taskbar.relaunch_command())
        self.assertTrue(self.read(ICON)[1].endswith(r"src\assets\mcu_icon.ico,0"))
        self.assertTrue(taskbar.clear_window_identity(self.hwnd))
        for pid in (IDENTITY, NAME, COMMAND, ICON):
            self.assertEqual(self.read(pid)[0], 0, f"property {pid} must be VT_EMPTY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
