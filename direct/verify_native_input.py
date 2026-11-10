#!/usr/bin/env python3
"""Mocked ownership checks; --native adds an owned Windows input-queue fixture."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import subprocess
import time
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.qt.native_input import _GuiThreadInfo, handoff_owned_input

NATIVE = '--native' in sys.argv
if NATIVE:
    sys.argv.remove('--native')
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('--native requires an isolated GitHub Actions Windows runner')


def foreign_child():
    from PySide6.QtWidgets import QApplication, QWidget, QLineEdit, QVBoxLayout
    app = QApplication([])
    window = QWidget()
    layout = QVBoxLayout(window)
    field = QLineEdit(window)
    layout.addWidget(field)
    window.resize(360, 160)
    window.show()
    print(json.dumps({'root':int(window.winId()), 'input':int(field.winId())}), flush=True)
    return app.exec()


class NativeInputChecks(unittest.TestCase):
    def setUp(self):
        self.user, self.kernel = Mock(), Mock()
        self.focus, self.destination_pid = 101, os.getpid()
        self.user.IsWindow.return_value = True
        self.user.IsChild.side_effect = lambda root, child: root == 100 and child == 101
        self.kernel.GetCurrentThreadId.return_value = 11
        def thread(hwnd, pid):
            if pid is not None:
                pid._obj.value = self.destination_pid
            return 11 if hwnd == 200 else 22
        self.user.GetWindowThreadProcessId.side_effect = thread
        def info(_thread, pointer):
            self.assertEqual(pointer._obj.cbSize, ctypes.sizeof(_GuiThreadInfo))
            pointer._obj.hwndFocus = self.focus
            return True
        self.user.GetGUIThreadInfo.side_effect = info
        self.user.AttachThreadInput.return_value = True
        self.user.SetFocus.side_effect = lambda hwnd: setattr(self, 'focus', hwnd)

    def run_handoff(self):
        return handoff_owned_input(200, (100,), api=(self.user, self.kernel))

    def test_foreign_owned_input_is_returned_and_thread_attachment_is_balanced(self):
        self.assertTrue(self.run_handoff())
        self.assertEqual([call.args for call in self.user.AttachThreadInput.call_args_list],
                         [(11, 22, True), (11, 22, False)])
        self.user.SetFocus.assert_called_once_with(200)
        self.user.SetForegroundWindow.assert_not_called()
        self.user.SetActiveWindow.assert_not_called()

    def test_another_application_or_workspace_keeps_its_input(self):
        self.focus = 301
        self.assertFalse(self.run_handoff())
        self.user.SetFocus.assert_not_called()
        self.user.AttachThreadInput.assert_not_called()

    def test_only_a_destination_owned_by_this_process_can_receive_input(self):
        self.destination_pid += 1
        self.assertFalse(self.run_handoff())
        self.user.SetFocus.assert_not_called()

    def test_a_failed_native_focus_change_is_not_reported_as_success(self):
        self.user.SetFocus.side_effect = None
        self.assertFalse(self.run_handoff())
        self.assertEqual(self.user.AttachThreadInput.call_args.args, (11, 22, False))

    def test_a_reused_hwnd_with_a_different_process_is_not_owned(self):
        self.assertFalse(handoff_owned_input(200, ((100, os.getpid()+100),), api=(self.user,self.kernel)))
        self.user.SetFocus.assert_not_called()

    def test_failed_attachment_cannot_claim_input_and_exception_always_detaches(self):
        self.user.AttachThreadInput.return_value = False
        self.assertFalse(self.run_handoff())
        self.user.SetFocus.assert_not_called()
        self.user.AttachThreadInput.reset_mock()
        self.user.AttachThreadInput.return_value = True
        self.user.SetFocus.side_effect = OSError('fixture failure')
        self.assertFalse(self.run_handoff())
        self.assertEqual(self.user.AttachThreadInput.call_args.args, (11, 22, False))


@unittest.skipUnless(NATIVE and sys.platform == 'win32', 'Explicit native Windows fixture only')
class RealInputQueueChecks(unittest.TestCase):
    def test_foreign_input_returns_to_owned_editor_dialog_and_terminal_without_delayed_grabs(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QWidget, QLineEdit, QVBoxLayout, QDialog
        from main.qt.cursor_visibility import WorkspacePointerGuard
        from main.qt.native_input import _native_api
        from types import SimpleNamespace
        app = QApplication.instance() or QApplication([])
        user, kernel = _native_api()
        user.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
        user.SetParent.restype = wintypes.HWND
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        user.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetWindowTextLengthW.argtypes = [wintypes.HWND]

        class Keyboard(ctypes.Structure):
            _fields_ = [('vk', wintypes.WORD), ('scan', wintypes.WORD), ('flags', wintypes.DWORD),
                        ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]
        class Data(ctypes.Union):
            _fields_ = [('keyboard', Keyboard), ('padding', ctypes.c_byte * (32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 24))]
        class Input(ctypes.Structure):
            _fields_ = [('type', wintypes.DWORD), ('data', Data)]
        user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        user.SendInput.restype = wintypes.UINT
        window = QWidget()
        window.setWindowTitle('MCU Flasher owned input fixture')
        layout = QVBoxLayout(window)
        embed = QWidget(window); embed.setMinimumHeight(180); embed.setProperty('mcuForeignInput', True)
        editor, terminal = QLineEdit(window), QLineEdit(window)
        for item in (embed, editor, terminal): layout.addWidget(item)
        dialog = QDialog(window); dialog.setWindowTitle('Owned settings fixture')
        settings = QLineEdit(dialog); QVBoxLayout(dialog).addWidget(settings)
        window.resize(640, 400); window.show(); app.processEvents()
        child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--foreign-child'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            # The runner bounds this entire fixture; the child prints before
            # entering its message loop and owns no service or user state.
            import queue
            import threading
            startup = queue.Queue()
            threading.Thread(target=lambda: startup.put(child.stdout.readline()), daemon=True).start()
            handles = json.loads(startup.get(timeout=10))
            root, source = handles['root'], handles['input']
            user.SetParent(root, int(embed.winId()))
            user.SetWindowLongW(root, -16, 0x50000000)  # WS_CHILD | WS_VISIBLE
            window._ai_panel = SimpleNamespace(_ai_hwnd=root, _proc=child)
            guard = WorkspacePointerGuard(window)
            guard._scan_owned_widgets()
            current = kernel.GetCurrentThreadId()
            foreign = user.GetWindowThreadProcessId(source, None)
            for target, host in ((editor, window), (settings, dialog), (terminal, window), (editor, window)):
                host.show(); host.activateWindow(); user.SetForegroundWindow(int(host.winId()))
                QTest.qWait(100)
                self.assertTrue(user.AttachThreadInput(current, foreign, True))
                try: user.SetFocus(source)
                finally: user.AttachThreadInput(current, foreign, False)
                info = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
                self.assertTrue(user.GetGUIThreadInfo(0, ctypes.byref(info)))
                self.assertEqual(info.hwndFocus, source, 'Fixture did not establish foreign keyboard ownership')
                QTest.mouseClick(target, Qt.MouseButton.LeftButton)
                window.resize(window.width() + 1, window.height())
                QTest.qWait(400)
                info = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
                self.assertTrue(user.GetGUIThreadInfo(0, ctypes.byref(info)))
                destination = int(target.effectiveWinId())
                self.assertTrue(info.hwndFocus == destination or user.IsChild(destination, info.hwndFocus),
                                'Native input stayed in the foreign process after the owned input click')
                before = target.text()
                events = (Input * 2)(Input(type=1, data=Data(keyboard=Keyboard(vk=0x51))),
                                    Input(type=1, data=Data(keyboard=Keyboard(vk=0x51, flags=2))))
                self.assertEqual(user.SendInput(2, events, ctypes.sizeof(Input)), 2)
                deadline = time.monotonic() + 2
                while target.text() == before and time.monotonic() < deadline: QTest.qWait(10)
                self.assertEqual(target.text(), before + 'q')
                self.assertEqual(user.GetWindowTextLengthW(source), 0, 'Typing leaked to the embedded process')
                if host is dialog: dialog.hide()
            print('Native host:', sys.getwindowsversion(), flush=True)
        finally:
            child.terminate()
            child.communicate(timeout=5)
            dialog.close(); window.close(); app.processEvents()

if __name__ == '__main__':
    if sys.argv[1:] == ['--foreign-child']:
        raise SystemExit(foreign_child())
    unittest.main(verbosity=2)
