#!/usr/bin/env python3
"""Qt PTY readiness fixtures using owned socketpairs, never shells or hardware."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QSocketNotifier
from main.qt import posix_terminal_panel as terminal

APP = QCoreApplication.instance() or QCoreApplication([])


def events_for(milliseconds):
    deadline = time.monotonic() + milliseconds / 1000
    while time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(.001)


def wait_for(predicate, message, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        time.sleep(.001)
    raise AssertionError(message)


class SocketProcess:
    def __init__(self, connection):
        self.connection = connection
        self.fd = connection.fileno()
        self.pid = 0
        self.read_sizes = []
        self.setwinsize = Mock()

    def read(self, size):
        self.read_sizes.append(size)
        return os.read(self.fd, size)

    def close(self):
        self.connection.close()
        self.fd = -1


class InlineCleanupThread:
    """Exercise the existing cleanup ordering without launching any thread."""
    def __init__(self, *, target, args, name, daemon):
        self.target, self.args = target, args
        assert name == "MCU_ClosePtyTree" and not daemon

    def start(self):
        self.target(*self.args)


@unittest.skipUnless(sys.platform.startswith("linux"), "Native Linux descriptor readiness is required")
class PtyReadinessChecks(unittest.TestCase):
    def setUp(self):
        from main.platforms.ubuntu_pty_process import NativePtyProcess

        scratch = ROOT / "temp/audit/pty-readiness"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.local, self.peer = socket.socketpair()
        self.local.setblocking(False)
        self.peer.setblocking(False)
        self.addCleanup(self.peer.close)
        self.addCleanup(self.local.close)
        self.process = SocketProcess(self.local)
        self.cleaned = []
        self.session = None

        def cleanup(process):
            self.assertIsNone(self.session._read_notifier)
            self.assertIsNone(self.session._write_notifier)
            self.assertFalse(self.session.timer.isActive())
            self.cleaned.append(process)
            process.close()

        self.observer = Mock()
        for patcher in (patch.object(terminal, "performance_profile", return_value=SimpleNamespace(
                constrained=False, terminal_interval_ms=8)),
                        patch.object(terminal, "PromptObserver", return_value=self.observer),
                        patch.object(terminal, "close_pty_tree", side_effect=cleanup),
                        patch("threading.Thread", InlineCleanupThread),
                        patch.object(NativePtyProcess, "spawn", return_value=self.process)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.session = terminal.PtySession(str(self.root), ["fixture-not-executed"])
        self.addCleanup(self.session.close)
        self.output, self.ended = [], []
        self.session.output.connect(self.output.append)
        self.session.ended.connect(self.ended.append)

    def start(self):
        self.assertTrue(self.session.start())
        self.assertFalse(os.get_blocking(self.process.fd))
        return self.session

    def test_idle_has_no_poll_timer_or_read_calls_and_no_extra_thread(self):
        session = self.start()
        self.assertTrue(session.timer.isSingleShot())
        self.assertFalse(session.timer.isActive())
        self.assertTrue(session._read_notifier.isEnabled())
        self.assertFalse(session._write_notifier.isEnabled())
        wakeups = []
        session.timer.timeout.connect(lambda: wakeups.append(True))
        events_for(45)
        self.assertEqual(wakeups, [])
        self.assertEqual(self.process.read_sizes, [])
        self.assertEqual(self.cleaned, [])

    def test_readiness_delivers_incremental_utf8_and_returns_to_idle(self):
        session = self.start()
        self.peer.sendall(b"ready \xf0\x9f")
        wait_for(lambda: bool(self.output), "Read readiness did not deliver bytes")
        self.assertEqual("".join(self.output), "ready ")
        self.peer.sendall(b"\x98\x80 \xce\xa9\r\n")
        wait_for(lambda: "😀 Ω\r\n" in "".join(self.output), "Split Unicode was corrupted")
        self.assertEqual(session._pending_chars, len("".join(self.output).encode("utf-16-le")) // 2)
        events_for(25)
        self.assertFalse(session.timer.isActive())
        self.assertTrue(session._read_notifier.isEnabled())
        self.assertFalse(session._write_notifier.isEnabled())

    def test_read_frame_is_bounded_and_uses_single_shot_cooldown(self):
        session = self.start()
        self.peer.sendall(b"x" * 96_000)
        session._read_ready()
        self.assertLessEqual(len(self.process.read_sizes), 8)
        self.assertTrue(all(size == 4096 for size in self.process.read_sizes))
        self.assertLessEqual(len("".join(self.output)), 8 * 4096)
        self.assertTrue(session.timer.isActive())
        self.assertFalse(session._read_notifier.isEnabled())
        self.assertTrue(session.timer.isSingleShot())

    def test_constrained_profile_bounds_reads_to_four_chunks(self):
        session = self.start()
        session._performance.constrained = True
        self.peer.sendall(b"x" * 96_000)
        session._read_ready()
        self.assertEqual(len(self.process.read_sizes), 4)
        self.assertEqual(len("".join(self.output)), 4 * 4096)
        self.assertTrue(session.timer.isActive())

    def test_already_queued_readiness_cannot_bypass_active_frame_cooldown(self):
        session = self.start()
        self.peer.sendall(b"x" * 96_000)
        session._read_ready()
        before = list(self.process.read_sizes)
        session._read_ready()
        session._write_ready()
        self.assertEqual(self.process.read_sizes, before)
        self.assertEqual(len("".join(self.output)), 8 * 4096)

    def test_queued_writes_have_a_bounded_frame_before_cooldown(self):
        session = self.start()
        payload = "x" * 96_000
        session.write(payload)
        transmitted = self.peer.recv(96_000)
        self.assertEqual(transmitted, b"x" * (8 * 4096))
        self.assertEqual(session._input_bytes, len(payload) - len(transmitted))
        self.assertTrue(session.timer.isActive())
        self.assertFalse(session._write_notifier.isEnabled())

    def test_renderer_backpressure_disables_read_until_consumption_rearms(self):
        session = self.start()
        session._send_output("x" * 128_000)
        self.assertFalse(session._read_notifier.isEnabled())
        self.peer.sendall(b"after-consumption")
        events_for(20)
        self.assertEqual(self.process.read_sizes, [])
        self.assertFalse(session.timer.isActive())
        session.consumed(128_000)
        self.assertTrue(session._read_notifier.isEnabled())
        wait_for(lambda: "after-consumption" in self.output, "Consumption did not rearm read readiness")

    def test_partial_eagain_writes_preserve_unicode_paste_capability_and_exact_order(self):
        session = self.start()
        transmitted = bytearray()
        calls = [0]

        def partial(fd, data):
            self.assertEqual(fd, self.process.fd)
            calls[0] += 1
            if calls[0] in (2, 5):
                raise BlockingIOError(errno.EAGAIN, "full")
            count = min(7, len(data))
            transmitted.extend(data[:count])
            return count

        first = "\x1b[?1;2c\x1b[200~Unicode Ω😀漢字\n"
        second = "more\x1b[201~\r\x1b[12;34R"
        with patch.object(terminal.os, "write", side_effect=partial):
            session.write(first)
            session.write(second)
            wait_for(lambda: not session._input_bytes, "Write readiness did not drain queued input")
        self.assertEqual(bytes(transmitted), (first + second).encode())
        self.assertEqual(session._input_offset, 0)
        self.assertFalse(session._input_chunks)
        self.assertFalse(session._write_notifier.isEnabled())
        self.observer.feed.assert_any_call(first, 0)
        self.observer.feed.assert_any_call(second, 0)

    def test_eagain_enables_only_queued_writer_and_backpressure_does_not_block_input(self):
        session = self.start()
        session._send_output("x" * 128_000)
        with patch.object(terminal.os, "write", side_effect=BlockingIOError(errno.EAGAIN, "full")):
            session.write("\x1b[12;34R")
        self.assertTrue(session._write_notifier.isEnabled())
        self.assertFalse(session._read_notifier.isEnabled())
        wait_for(lambda: not session._input_bytes, "Output backlog stopped capability reply input")
        self.assertEqual(self.peer.recv(4096), b"\x1b[12;34R")
        self.assertFalse(session._write_notifier.isEnabled())
        self.assertFalse(session._read_notifier.isEnabled())

    def test_input_limit_rejects_without_replacing_pending_bytes(self):
        session = self.start()
        with patch.object(terminal.os, "write", side_effect=BlockingIOError(errno.EAGAIN, "full")):
            session.write("x" * (4 * 1024 * 1024))
            session.write("rejected")
            session.write("also rejected")
        self.assertEqual(session._input_bytes, 4 * 1024 * 1024)
        self.assertEqual(len(session._input_chunks), 1)
        self.assertEqual(sum("input was not sent" in text for text in self.output), 1)

    def test_eof_removes_watches_before_cleanup_and_old_callbacks_cannot_read_reused_fd(self):
        session = self.start()
        original_fd = self.process.fd
        self.peer.sendall(b"unfinished \xf0\x9f")
        self.peer.close()
        wait_for(lambda: session._closed, "EOF did not close the session")
        self.assertEqual(self.cleaned, [self.process])
        self.assertEqual(len(self.ended), 1)
        self.assertEqual("".join(self.output), "unfinished �")
        self.assertIsNone(session.process)
        before = list(self.process.read_sizes)
        replacement, peer = socket.socketpair()
        try:
            self.assertEqual(replacement.fileno(), original_fd, "Fixture did not reuse the closed descriptor")
            peer.sendall(b"unrelated new descriptor")
            session._read_ready()
            session._write_ready()
            session._drain()
            events_for(20)
            self.assertEqual(self.process.read_sizes, before)
            self.assertEqual(replacement.recv(4096), b"unrelated new descriptor")
        finally:
            replacement.close()
            peer.close()

    def test_partial_notifier_failure_reports_and_uses_stoppable_timer_fallback(self):
        created = []

        def notifier(fd, kind, parent):
            if created:
                raise RuntimeError("fixture notifier failure")
            watch = QSocketNotifier(fd, kind, parent)
            created.append(watch)
            return watch

        factory = Mock(side_effect=notifier)
        factory.Type = QSocketNotifier.Type
        with patch.object(terminal, "QSocketNotifier", factory):
            session = self.start()
        self.assertIsNone(session._read_notifier)
        self.assertIsNone(session._write_notifier)
        self.assertFalse(created[0].isEnabled())
        self.assertTrue(session.timer.isActive())
        self.assertFalse(session.timer.isSingleShot())
        self.assertTrue(any("polling fallback" in text for text in self.output))
        self.peer.sendall(b"fallback output")
        wait_for(lambda: "fallback output" in self.output, "Fallback timer did not deliver output")
        session.close()
        self.assertFalse(session.timer.isActive())
        self.assertEqual(self.cleaned, [self.process])

    def test_failure_after_spawn_closes_owned_handle_without_watches_or_timer(self):
        with patch.object(terminal.os, "set_blocking", side_effect=OSError("fixture nonblocking failure")):
            self.assertFalse(self.session.start())
        self.assertTrue(self.session._closed)
        self.assertIsNone(self.session.process)
        self.assertIsNone(self.session._read_notifier)
        self.assertIsNone(self.session._write_notifier)
        self.assertFalse(self.session.timer.isActive())
        self.assertEqual(self.cleaned, [self.process])
        self.assertEqual(len(self.ended), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
