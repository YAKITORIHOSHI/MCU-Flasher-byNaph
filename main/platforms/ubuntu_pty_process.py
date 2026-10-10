"""Native PTY handles without executing Python after a threaded GUI fork.

The standalone supervisor acquires the controlling terminal after exec. Keep
the original Popen handle as the only owner of the child PID and exit status.
"""
from __future__ import annotations

import errno
import fcntl
import os
import signal
import struct
import subprocess
import sys
import termios


class NativePtyProcess:
    def __init__(self, handle: subprocess.Popen, fd: int):
        self._handle = handle
        self.pid = handle.pid
        self.fd = fd
        self.closed = False
        self.terminated = False
        self.exitstatus = None
        self.signalstatus = None

    @classmethod
    def spawn(cls, command, *, cwd, env, dimensions):
        if not sys.platform.startswith("linux"):
            raise OSError("Native PTYs require Linux")
        master, slave = os.openpty()
        try:
            cls._set_dimensions(master, *dimensions)
            os.set_blocking(master, False)
            # Popen performs descriptor/session setup in its native launcher;
            # no Python preexec callback or forkpty runs in the Qt process.
            handle = subprocess.Popen(command, cwd=cwd, env=env, stdin=slave,
                                      stdout=slave, stderr=slave,
                                      start_new_session=True, close_fds=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        return cls(handle, master)

    @staticmethod
    def _set_dimensions(fd, rows, columns):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def setwinsize(self, rows, columns):
        self._set_dimensions(self.fd, rows, columns)

    def read(self, size=1024):
        try:
            data = os.read(self.fd, size)
        except OSError as exc:
            if exc.errno == errno.EIO:
                raise EOFError("The native terminal ended") from exc
            raise
        if not data:
            raise EOFError("The native terminal ended")
        return data

    def _record_status(self, result):
        if result is not None:
            self.terminated = True
            self.exitstatus = result if result >= 0 else None
            self.signalstatus = -result if result < 0 else None
        return result

    def isalive(self):
        return self._record_status(self._handle.poll()) is None

    def close(self, force=True):
        if self.closed:
            return
        self.closed = True
        fd, self.fd = self.fd, -1
        try:
            os.close(fd)
        finally:
            # Closing the controlling terminal sends HUP. Give the supervisor
            # its bounded child-reaping window before escalating through the
            # original handle; Popen checks status before sending a signal.
            try:
                self._record_status(self._handle.wait(timeout=1.75))
            except subprocess.TimeoutExpired:
                self._handle.send_signal(signal.SIGTERM)
                try:
                    self._record_status(self._handle.wait(timeout=.4))
                except subprocess.TimeoutExpired:
                    if not force:
                        raise OSError("The native terminal did not close")
                    self._handle.kill()
                    self._record_status(self._handle.wait(timeout=.4))
