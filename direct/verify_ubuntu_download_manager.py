#!/usr/bin/env python3
"""Ubuntu downloader reuse/teardown fixtures; no Tk startup, cache or installers."""
from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from main.qt import download_dialog
from src.modules import arduino_lib_req as browser
from src.modules import ubuntu_download_manager as lifecycle


@unittest.skipUnless(sys.platform.startswith("linux"), "Ubuntu downloader IPC")
class DownloaderChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = ROOT / "temp/audit/ubuntu-parity/features/downloader"
        directory.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=directory)))
        self.addCleanup(lifecycle.close_viewers)

    def channel(self, root=None):
        channel = lifecycle.DownloadManagerChannel(root or self.root)
        self.addCleanup(channel.close)
        return channel

    def test_wake_and_quit_reuse_one_endpoint_without_files(self):
        channel = self.channel()
        self.assertTrue(lifecycle.request(self.root, "wake"))
        self.assertTrue(lifecycle.request(self.root, "quit"))
        self.assertEqual(channel.poll(), ["wake", "quit"])
        self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaises(lifecycle.AlreadyRunning):
            lifecycle.DownloadManagerChannel(self.root)

    def test_other_checkouts_and_dead_instances_cannot_receive_commands(self):
        channel = self.channel()
        self.assertFalse(lifecycle.request(self.root / "other checkout", "quit"))
        self.assertEqual(channel.poll(), [])
        channel.close()
        self.assertFalse(lifecycle.request(self.root, "quit"))
        replacement = self.channel()
        self.assertTrue(lifecycle.request(self.root, "wake"))
        self.assertEqual(replacement.poll(), ["wake"])

    def test_untrusted_peer_and_unknown_commands_are_rejected(self):
        channel = self.channel()
        with patch.object(lifecycle, "_same_user", return_value=False):
            self.assertFalse(lifecycle.request(self.root, "quit"))
        self.assertEqual(channel.poll(), [])
        with self.assertRaises(ValueError):
            lifecycle.request(self.root, "anything")

    def test_partial_requests_never_block_and_malformed_payloads_do_nothing(self):
        channel = self.channel()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(lifecycle._address(self.root))
            client.sendall(b"MCU-DM-1 ")
            started = time.monotonic()
            self.assertEqual(channel.poll(), [])
            self.assertLess(time.monotonic() - started, .1)
            client.sendall(b"wake\n")
            self.assertEqual(channel.poll(), ["wake"])
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(lifecycle._address(self.root))
            client.sendall(b"MCU-DM-1 quit\nextra command")
            self.assertEqual(channel.poll(), [])

    def test_launcher_wakes_existing_manager_without_spawn_or_cache_writes(self):
        channel = self.channel()
        script = self.root / "src/modules/arduino_lib_req.py"
        script.parent.mkdir(parents=True)
        script.write_text("fixture")
        with patch.object(download_dialog, "ROOT", self.root), \
                patch.object(download_dialog.subprocess, "Popen", side_effect=AssertionError("Duplicate manager spawned")), \
                patch.object(download_dialog, "_find_python_executable", side_effect=AssertionError("Unneeded runtime probe")):
            self.assertTrue(download_dialog.launch_download_manager())
        self.assertEqual(channel.poll(), ["wake"])
        self.assertFalse((self.root / "index_json").exists())

    def test_tk_commands_restore_state_and_quit_without_windows_trigger_reads(self):
        app = browser.ArduinoBrowser.__new__(browser.ArduinoBrowser)
        app.root = Mock()
        app._linux_channel = Mock(poll=Mock(return_value=["wake"]))
        app._unhide_window = Mock()
        app._force_exit = Mock()
        with patch.object(browser.os.path, "exists", side_effect=AssertionError("Windows trigger read")):
            app._check_show_trigger()
            app._linux_channel.poll.return_value = ["quit", "wake"]
            app._check_show_trigger()
        app._unhide_window.assert_called_once()
        app._force_exit.assert_called_once()
        app.root.after.assert_called_once_with(150, app._check_show_trigger)

    def test_shutdown_cancels_workers_closes_socket_and_destroys_tk(self):
        app = browser.ArduinoBrowser.__new__(browser.ArduinoBrowser)
        app.root = Mock()
        app._cancel_event = threading.Event()
        app._linux_channel = Mock()
        channel = app._linux_channel
        with patch.object(lifecycle, "close_viewers") as viewers, \
                patch.object(browser.os.path, "exists", side_effect=AssertionError("Windows HWND read")):
            with self.assertRaises(SystemExit):
                app._force_exit()
        self.assertTrue(app._cancel_event.is_set())
        channel.close.assert_called_once()
        self.assertIsNone(app._linux_channel)
        viewers.assert_called_once()
        app.root.destroy.assert_called_once()

    def test_windows_launch_keeps_existing_subprocess_path(self):
        script = self.root / "src/modules/arduino_lib_req.py"
        script.parent.mkdir(parents=True)
        script.write_text("fixture")
        with patch.object(download_dialog, "ROOT", self.root), \
                patch.object(download_dialog.sys, "platform", "win32"), \
                patch.object(lifecycle, "request", side_effect=AssertionError("Linux IPC called on Windows")), \
                patch.object(download_dialog, "_find_python_executable", return_value=self.root / "pythonw.exe"), \
                patch.object(download_dialog.subprocess, "Popen") as spawn:
            self.assertTrue(download_dialog.launch_download_manager())
        self.assertEqual(spawn.call_args.args[0], [str(self.root / "pythonw.exe"), str(script)])
        self.assertEqual(spawn.call_args.kwargs["creationflags"], 0)

    def test_windows_trigger_and_exit_keep_their_original_behavior(self):
        app = browser.ArduinoBrowser.__new__(browser.ArduinoBrowser)
        app.root = Mock()
        app._linux_channel = Mock(poll=Mock(side_effect=AssertionError("Linux IPC on Windows")))
        app._cancel_event = threading.Event()
        app._is_hidden = True
        app._unhide_window = Mock()
        with patch.object(browser.sys, "platform", "win32"), \
                patch.object(browser.os.path, "exists", side_effect=lambda name: name.endswith(".show_dm_trigger")), \
                patch.object(browser.os, "remove") as remove, \
                patch.object(lifecycle, "close_viewers", side_effect=AssertionError("Linux cleanup on Windows")):
            app._check_show_trigger()
            with self.assertRaises(SystemExit):
                app._force_exit()
        app._unhide_window.assert_called_once()
        remove.assert_called_once_with(os.path.join(browser.INDEX_CACHE_DIR, ".show_dm_trigger"))
        self.assertFalse(app._cancel_event.is_set())
        app._linux_channel.close.assert_not_called()
        app.root.destroy.assert_called_once()

    def test_another_live_workspace_keeps_shared_manager_and_last_window_quits(self):
        from main.core import config
        channel = self.channel()
        instances = {"101": {"create_time": 10}, "202": {"create_time": 20}, "303": {"create_time": 30}}
        with patch.object(config, "_load_raw_config", return_value={"instances": instances}) as read, \
                patch.object(config, "_get_alive_pid_create_times", return_value={"101": 10, "202": 20, "303": 300}), \
                patch.object(lifecycle, "_workspace_belongs_to", return_value=True) as belongs:
            self.assertFalse(lifecycle.quit_if_last_workspace(self.root, 101))
            self.assertEqual(channel.poll(), [])
            instances.pop("202")
            self.assertTrue(lifecycle.quit_if_last_workspace(self.root, 101))
        self.assertTrue(all(call.kwargs == {"fresh": True} for call in read.call_args_list))
        belongs.assert_called_once_with(self.root, "202")
        self.assertEqual(channel.poll(), ["quit"])

    def test_a_live_workspace_from_another_checkout_does_not_keep_manager(self):
        from main.core import config
        channel = self.channel()
        with patch.object(config, "_load_raw_config", return_value={"instances": {"202": {"create_time": 20}}}), \
                patch.object(config, "_get_alive_pid_create_times", return_value={"202": 20}), \
                patch.object(lifecycle, "_workspace_belongs_to", return_value=False):
            self.assertTrue(lifecycle.quit_if_last_workspace(self.root, 101))
        self.assertEqual(channel.poll(), ["quit"])

    def test_real_fixture_process_wakes_then_exits_and_endpoint_disappears(self):
        ready = self.root / "ready"
        receipt = self.root / "receipt"
        source = """
import sys,time
from pathlib import Path
from src.modules.ubuntu_download_manager import DownloadManagerChannel
root, ready, receipt = map(Path, sys.argv[1:])
channel = DownloadManagerChannel(root)
ready.write_text('ready')
try:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for command in channel.poll():
            with receipt.open('a') as stream: stream.write(command + '\\n')
            if command == 'quit': raise SystemExit(0)
        time.sleep(.01)
    raise SystemExit(2)
finally:
    channel.close()
"""
        process = subprocess.Popen([sys.executable, "-B", "-c", source, str(self.root), str(ready), str(receipt)], cwd=ROOT)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(ready.exists())
        self.assertTrue(lifecycle.request(self.root, "wake"))
        self.assertTrue(lifecycle.request(self.root, "quit"))
        self.assertEqual(process.wait(timeout=3), 0)
        self.assertEqual(receipt.read_text(), "wake\nquit\n")
        self.assertFalse(lifecycle.request(self.root, "quit"))

    def test_owned_sample_viewer_exits_without_terminating_unowned_process(self):
        command = [sys.executable, "-B", "-c", "import time; time.sleep(10)"]
        viewer = subprocess.Popen(command)
        unowned = subprocess.Popen(command)
        self.addCleanup(lambda: viewer.kill() if viewer.poll() is None else None)
        self.addCleanup(lambda: unowned.kill() if unowned.poll() is None else None)
        self.addCleanup(lambda: unowned.wait(timeout=2))
        lifecycle.remember_viewer(viewer)
        lifecycle.close_viewers()
        self.assertIsNotNone(viewer.poll())
        self.assertIsNone(unowned.poll())
        unowned.terminate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
