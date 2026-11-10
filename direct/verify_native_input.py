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
    field.textChanged.connect(lambda value: print(json.dumps({'text':value}),flush=True))
    window.resize(360, 160)
    window.show()
    print(json.dumps({'root':int(window.winId()), 'input':int(field.winId()),'pid':os.getpid()}), flush=True)
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
        user.GetWindowRect.argtypes = [wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
        user.GetClientRect.argtypes = [wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
        user.SetWindowPos.argtypes = [wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,
                                     ctypes.c_int,ctypes.c_int,wintypes.UINT]
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.SetCursorPos.argtypes = [ctypes.c_int,ctypes.c_int]
        user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user.PostMessageW.argtypes = [wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]

        class Keyboard(ctypes.Structure):
            _fields_ = [('vk', wintypes.WORD), ('scan', wintypes.WORD), ('flags', wintypes.DWORD),
                        ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]
        class Data(ctypes.Union):
            class Mouse(ctypes.Structure):
                _fields_ = [('x',wintypes.LONG),('y',wintypes.LONG),('buttons',wintypes.DWORD),
                            ('flags',wintypes.DWORD),('time',wintypes.DWORD),('extra',ctypes.c_size_t)]
            _fields_ = [('keyboard', Keyboard), ('mouse',Mouse)]
        class Input(ctypes.Structure):
            _fields_ = [('type', wintypes.DWORD), ('data', Data)]
        user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        user.SendInput.restype = wintypes.UINT
        original_cursor=wintypes.POINT(); user.GetCursorPos(ctypes.byref(original_cursor))
        def click(hwnd):
            self.assertTrue(user.IsWindowVisible(hwnd),'The owned fixture input is not visible')
            rect=wintypes.RECT(); self.assertTrue(user.GetWindowRect(hwnd,ctypes.byref(rect)))
            self.assertTrue(user.SetCursorPos((rect.left+rect.right)//2,(rect.top+rect.bottom)//2))
            events=(Input*2)(Input(type=0,data=Data(mouse=Data.Mouse(flags=2))),
                             Input(type=0,data=Data(mouse=Data.Mouse(flags=4))))
            self.assertEqual(user.SendInput(2,events,ctypes.sizeof(Input)),2)
            QTest.qWait(150)
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
        foreign_process=None
        root=None
        try:
            # The runner bounds this entire fixture; the child prints before
            # entering its message loop and owns no service or user state.
            import queue
            import threading
            startup = queue.Queue()
            def read_child():
                for line in child.stdout:
                    startup.put(line)
            threading.Thread(target=read_child, daemon=True).start()
            handles = json.loads(startup.get(timeout=10))
            root, source = handles['root'], handles['input']
            # Windows venv python.exe is a redirector; Popen.pid can belong to
            # that launcher rather than the Python process owning the HWND.
            # The private child pipe reports its actual OS identity.
            import psutil
            foreign_process=psutil.Process(handles['pid'])
            embed_hwnd=int(embed.winId())
            user.SetParent(root,embed_hwnd)
            user.SetWindowLongW(root, -16, 0x50000000)  # WS_CHILD | WS_VISIBLE
            client=wintypes.RECT(); self.assertTrue(user.GetClientRect(embed_hwnd,ctypes.byref(client)))
            # SetParent retains the old screen position as client coordinates.
            # Match the shipping embedder: explicitly fill the parent's client
            # area so a physical click is inside the visible child, not clipped.
            self.assertTrue(user.SetWindowPos(root,None,0,0,client.right,client.bottom,0x70))
            self.assertTrue(user.IsChild(embed_hwnd,root))
            QTest.qWait(150)
            window._ai_panel = SimpleNamespace(_ai_hwnd=root, _proc=SimpleNamespace(pid=handles['pid']))
            guard = WorkspacePointerGuard(window)
            guard._scan_owned_widgets()
            for target, host in ((editor, window), (settings, dialog), (terminal, window), (editor, window)):
                host.show()
                if host is dialog:
                    dialog.move(window.frameGeometry().right()+20,window.y())
                destination=int(target.winId())
                window.activateWindow(); user.SetForegroundWindow(int(window.winId()))
                QTest.qWait(100)
                click(source)
                info = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
                self.assertTrue(user.GetGUIThreadInfo(0, ctypes.byref(info)))
                focus_pid=wintypes.DWORD()
                user.GetWindowThreadProcessId(info.hwndFocus,ctypes.byref(focus_pid))
                print('Owned input fixture:',dict(root=root,input=source,focus=info.hwndFocus,
                      focus_pid=focus_pid.value,child_pid=handles['pid'],launcher_pid=child.pid,target=destination),flush=True)
                self.assertTrue(info.hwndFocus==root or user.IsChild(root,info.hwndFocus),
                                'Fixture did not establish focus inside the owned foreign window')
                self.assertEqual(focus_pid.value,handles['pid'])
                # Qt may keep native focus on its foreign top-level while its
                # logical line edit has focus. Prove actual key delivery rather
                # than requiring one particular Qt HWND arrangement.
                calibration=(Input*2)(Input(type=1,data=Data(keyboard=Keyboard(scan=ord('z'),flags=4))),
                                      Input(type=1,data=Data(keyboard=Keyboard(scan=ord('z'),flags=6))))
                self.assertEqual(user.SendInput(2,calibration,ctypes.sizeof(Input)),2)
                self.assertEqual(json.loads(startup.get(timeout=2)),{'text':'z'})
                erase=(Input*2)(Input(type=1,data=Data(keyboard=Keyboard(vk=8))),
                                Input(type=1,data=Data(keyboard=Keyboard(vk=8,flags=2))))
                self.assertEqual(user.SendInput(2,erase,ctypes.sizeof(Input)),2)
                self.assertEqual(json.loads(startup.get(timeout=2)),{'text':''})
                click(destination)
                window.resize(window.width() + 1, window.height())
                QTest.qWait(400)
                info = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
                self.assertTrue(user.GetGUIThreadInfo(0, ctypes.byref(info)))
                destination = int(target.effectiveWinId())
                self.assertTrue(info.hwndFocus == destination or user.IsChild(destination, info.hwndFocus),
                                'Native input stayed in the foreign process after the owned input click')
                before = target.text()
                events = (Input * 2)(Input(type=1, data=Data(keyboard=Keyboard(scan=ord('q'),flags=4))),
                                    Input(type=1, data=Data(keyboard=Keyboard(scan=ord('q'),flags=6))))
                self.assertEqual(user.SendInput(2, events, ctypes.sizeof(Input)), 2)
                deadline = time.monotonic() + 2
                while target.text() == before and time.monotonic() < deadline: QTest.qWait(10)
                self.assertEqual(target.text(), before + 'q')
                QTest.qWait(60)
                self.assertTrue(startup.empty(),'The embedded process reported unexpected text input')
                if host is dialog: dialog.hide()
            print('Native host:', sys.getwindowsversion(), flush=True)
        finally:
            if root and user.IsWindow(root):
                user.PostMessageW(root,0x10,0,0)  # WM_CLOSE to this fixture's child.
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if foreign_process is not None:
                    try: foreign_process.terminate()  # psutil rechecks captured creation identity.
                    except psutil.NoSuchProcess: pass
                child.terminate()
                child.wait(timeout=5)
            child.stdout.close(); child.stderr.close()
            user.SetCursorPos(original_cursor.x,original_cursor.y)
            dialog.close(); window.close(); app.processEvents()

if __name__ == '__main__':
    if sys.argv[1:] == ['--foreign-child']:
        raise SystemExit(foreign_child())
    unittest.main(verbosity=2)
