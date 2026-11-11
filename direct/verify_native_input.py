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
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from main.qt.native_input import _GuiThreadInfo, handoff_owned_input, return_input_to_qt

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

    def test_containers_cannot_take_input_and_explicit_text_surfaces_can(self):
        from types import SimpleNamespace
        def widget(kind='',foreign=False,parent=None):
            return SimpleNamespace(property=lambda name: foreign if name=='mcuForeignInput' else False,
                inherits=lambda name:name==kind,parentWidget=lambda:parent,
                focusProxy=lambda:None,effectiveWinId=lambda:200)
        owner=SimpleNamespace(_ai_panel=SimpleNamespace(_ai_hwnd=100,_proc=SimpleNamespace(pid=300)))
        with patch('main.qt.native_input.sys.platform','win32'), \
             patch('main.qt.native_input.handoff_owned_input',return_value=True) as move:
            self.assertFalse(return_input_to_qt(owner,widget()))
            move.assert_not_called()
            self.assertTrue(return_input_to_qt(owner,widget('QLineEdit')))
            self.assertEqual(move.call_args.args,(200,[(100,300)]))
            move.reset_mock()
            self.assertFalse(return_input_to_qt(owner,widget('QWebEngineView',parent=widget(foreign=True))))
            move.assert_not_called()


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
        user.ClientToScreen.argtypes = [wintypes.HWND,ctypes.POINTER(wintypes.POINT)]
        user.SetWindowPos.argtypes = [wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,
                                     ctypes.c_int,ctypes.c_int,wintypes.UINT]
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.SetCursorPos.argtypes = [ctypes.c_int,ctypes.c_int]
        user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user.WindowFromPoint.argtypes = [wintypes.POINT]
        user.WindowFromPoint.restype = wintypes.HWND
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
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
            press()
        def click_qt(widget):
            # Match the shipping controls: ordinary Qt fields do not create
            # their own HWND. Forcing winId() here changes the geometry and
            # input routing that this click is meant to verify.
            host = widget.window()
            origin = wintypes.POINT()
            self.assertTrue(user.ClientToScreen(int(host.effectiveWinId()), ctypes.byref(origin)))
            point = widget.mapTo(host, widget.rect().center())
            ratio = host.devicePixelRatioF()
            physical = wintypes.POINT(origin.x+round(point.x()*ratio),
                                     origin.y+round(point.y()*ratio))
            hit = user.WindowFromPoint(physical)
            hit_pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hit, ctypes.byref(hit_pid))
            native = wintypes.RECT()
            user.GetWindowRect(int(host.effectiveWinId()), ctypes.byref(native))
            client = wintypes.RECT()
            user.GetClientRect(int(host.effectiveWinId()), ctypes.byref(client))
            print('Qt click geometry:', dict(field=widget.objectName(), host=int(host.effectiveWinId()),
                  logical_rect=widget.geometry().getRect(), logical_center=(point.x(), point.y()),
                  ratio=ratio, client_origin=(origin.x, origin.y),
                  native_rect=(native.left, native.top, native.right, native.bottom),
                  native_client=(client.left, client.top, client.right, client.bottom),
                  physical=(physical.x, physical.y), hit=hit, hit_root=user.GetAncestor(hit, 2),
                  hit_pid=hit_pid.value), flush=True)
            if widget.objectName() == 'settings':
                capture('settings-before-click')
                self.assertEqual(user.GetAncestor(hit, 2), int(host.effectiveWinId()),
                                 'The Settings input click is obscured or outside its owned dialog')
            self.assertTrue(user.SetCursorPos(physical.x, physical.y))
            press()
        def press():
            events=(Input*2)(Input(type=0,data=Data(mouse=Data.Mouse(flags=2))),
                             Input(type=0,data=Data(mouse=Data.Mouse(flags=4))))
            self.assertEqual(user.SendInput(2,events,ctypes.sizeof(Input)),2)
            QTest.qWait(150)
        window = QWidget()
        window.setWindowTitle('MCU Flasher owned input fixture')
        layout = QVBoxLayout(window)
        embed = QWidget(window); embed.setMinimumHeight(180); embed.setProperty('mcuForeignInput', True)
        editor, terminal = QLineEdit(window), QLineEdit(window)
        editor.setObjectName('editor'); terminal.setObjectName('terminal')
        for item in (embed, editor, terminal): layout.addWidget(item)
        dialog = QDialog(window); dialog.setWindowTitle('Owned settings fixture')
        settings = QLineEdit(dialog); QVBoxLayout(dialog).addWidget(settings)
        settings.setObjectName('settings')
        work = app.primaryScreen().availableGeometry()
        window.resize(min(640, work.width()-40), min(400, work.height()-60))
        window.move(work.left()+20, work.top()+20)
        window.show(); app.processEvents()
        # Reserve a separate on-screen column for the nonmodal fixture dialog.
        # Calibration explicitly activates the parent, so overlapping its
        # input fields makes a physical click dependent on native z-order.
        # Measure actual frame/client borders, converting pixels to logical Qt
        # units once; the same fixture must fit at every declared scale.
        import math
        dialog.adjustSize(); dialog.show(); app.processEvents()
        def native_frame_size(host):
            native = wintypes.RECT(); client = wintypes.RECT()
            self.assertTrue(user.GetWindowRect(int(host.effectiveWinId()), ctypes.byref(native)))
            self.assertTrue(user.GetClientRect(int(host.effectiveWinId()), ctypes.byref(client)))
            ratio = host.devicePixelRatioF()
            return (math.ceil((native.right-native.left)/ratio),
                    math.ceil((native.bottom-native.top)/ratio),
                    math.ceil(((native.right-native.left)-(client.right-client.left))/ratio),
                    math.ceil(((native.bottom-native.top)-(client.bottom-client.top))/ratio))
        dialog_frame_width, _height, _dx, _dy = native_frame_size(dialog)
        _width, _height, window_border_width, window_border_height = native_frame_size(window)
        margin, gap = 16, 16
        client_width = min(640, work.width()-margin*2-gap-dialog_frame_width-window_border_width)
        client_height = min(400, work.height()-margin*2-window_border_height)
        minimum = window.minimumSizeHint()
        self.assertGreaterEqual(client_width, minimum.width(), 'The fixture cannot fit both owned windows')
        self.assertGreaterEqual(client_height, minimum.height(), 'The fixture cannot fit its input fields')
        window.resize(client_width, client_height)
        window.move(work.left()+margin, work.top()+margin)
        app.processEvents()
        dialog.move(window.frameGeometry().right()+gap+1, work.top()+margin)
        app.processEvents()
        self.assertFalse(window.frameGeometry().intersects(dialog.frameGeometry()),
                         'The owned input fixture windows must not overlap')
        dialog.hide()
        child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--foreign-child'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
        foreign_process=None
        root=None
        def capture(label):
            directory = ROOT / 'temp/audit'
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'native-input-{os.environ.get("QT_SCALE_FACTOR", "1")}-{label}.png'
            print('Native capture:', str(path), app.primaryScreen().grabWindow(0).save(str(path)), flush=True)
        def describe_focus(label):
            current = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
            user.GetGUIThreadInfo(0, ctypes.byref(current))
            focus_pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(current.hwndFocus, ctypes.byref(focus_pid))
            logical, active = app.focusWidget(), app.activeWindow()
            print('Native focus phase:', dict(phase=label, native=current.hwndFocus,
                  native_pid=focus_pid.value, logical=(logical.objectName() if logical else None),
                  active=(active.windowTitle() if active else None),
                  dialog_visible=dialog.isVisible()), flush=True)
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
            def next_child_message():
                deadline=time.monotonic()+2
                while startup.empty() and time.monotonic()<deadline:
                    QTest.qWait(10)
                self.assertFalse(startup.empty(),'The owned foreign input did not acknowledge the calibration key')
                return json.loads(startup.get_nowait())
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
            def trace_handoff(destination, roots, **options):
                before = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
                user.GetGUIThreadInfo(0, ctypes.byref(before))
                moved = handoff_owned_input(destination, roots, **options)
                after = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
                user.GetGUIThreadInfo(0, ctypes.byref(after))
                print('Owned handoff:', dict(destination=destination, roots=roots,
                      before=before.hwndFocus, after=after.hwndFocus, moved=moved), flush=True)
                return moved
            tracer = patch('main.qt.native_input.handoff_owned_input', side_effect=trace_handoff)
            tracer.start(); self.addCleanup(tracer.stop)
            for target, host in ((editor, window), (settings, dialog), (terminal, window), (editor, window)):
                host.show()
                if host is dialog:
                    dialog.move(window.frameGeometry().right()+gap+1, work.top()+margin)
                destination=int(target.effectiveWinId())
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
                self.assertEqual(next_child_message(),{'text':'z'})
                erase=(Input*2)(Input(type=1,data=Data(keyboard=Keyboard(vk=8))),
                                Input(type=1,data=Data(keyboard=Keyboard(vk=8,flags=2))))
                self.assertEqual(user.SendInput(2,erase,ctypes.sizeof(Input)),2)
                self.assertEqual(next_child_message(),{'text':''})
                click_qt(target)
                describe_focus('immediately-after-click')
                window.resize(window.width() + 1, window.height())
                QTest.qWait(400)
                describe_focus('after-parent-resize')
                info = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
                self.assertTrue(user.GetGUIThreadInfo(0, ctypes.byref(info)))
                native_pid = wintypes.DWORD()
                user.GetWindowThreadProcessId(info.hwndFocus, ctypes.byref(native_pid))
                owned_root = int(host.winId())
                print('Clicked Qt input:', dict(focus=info.hwndFocus, focus_pid=native_pid.value,
                      owned_pid=os.getpid(), root=owned_root, target=int(target.effectiveWinId())), flush=True)
                # Qt can keep Win32 focus on its top-level and route input to
                # the logical field, just as the calibrated foreign Qt child
                # does. Require owned native focus, the intended Qt field and
                # actual key delivery; a particular Qt HWND layout is incidental.
                self.assertEqual(native_pid.value, os.getpid(),
                                 'Native input stayed outside the owned Qt process')
                self.assertTrue(info.hwndFocus == owned_root or user.IsChild(owned_root, info.hwndFocus))
                self.assertIs(app.focusWidget(), target)
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
            print('Native host:', sys.getwindowsversion(), 'Qt scale:', os.environ.get('QT_SCALE_FACTOR','1'), flush=True)
        except BaseException:
            describe_focus('failure')
            capture('failure')
            raise
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
