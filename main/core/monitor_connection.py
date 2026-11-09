"""Passive serial opens, independent of intentional programmer/reset sequences.

POSIX retains tty flags between opens. Disable hang-up-on-close during pySerial's
initial configuration (before it applies the control lines), and on subsequent
baud changes. This cannot suppress a USB driver's own transient at os.open().
"""
from __future__ import annotations

from functools import lru_cache
from contextlib import contextmanager
import os


@contextmanager
def idle_after_reset(conn):
    """Always attempt both idle lines, keeping an original waveform failure."""
    original = None
    try:
        yield conn
    except BaseException as exc:
        original = exc
        raise
    finally:
        cleanup = None
        for line in ("dtr", "rts"):
            try:
                setattr(conn, line, False)
            except Exception as exc:
                cleanup = cleanup or exc
        if cleanup is not None and original is None:
            raise cleanup


@lru_cache(maxsize=4)
def _passive_type(factory):
    class PassiveSerial(factory):
        def _reconfigure_port(self, force_update=False):
            super()._reconfigure_port(force_update=force_update)
            import termios
            attrs = termios.tcgetattr(self.fd)
            wanted = (attrs[2] | termios.CLOCAL) & ~termios.HUPCL
            if attrs[2] != wanted:
                attrs[2] = wanted
                termios.tcsetattr(self.fd, termios.TCSANOW, attrs)

    return PassiveSerial


def open_passive_serial(factory, port, baud, *, timeout=0.04, write_timeout=1.0):
    """Open with inactive DTR/RTS; fail visibly if native tty setup fails.

    Accept the serial factory so isolated callers can supply a simulated port.
    Windows keeps its native implementation and preconfigured control lines.
    """
    if os.name == "posix" and isinstance(factory, type) and hasattr(factory, "_reconfigure_port"):
        factory = _passive_type(factory)
    conn = factory()
    try:
        conn.port, conn.baudrate = port, baud
        conn.bytesize, conn.parity, conn.stopbits = 8, "N", 1
        conn.xonxoff = conn.dsrdtr = conn.rtscts = False
        conn.timeout, conn.write_timeout = timeout, write_timeout
        conn.dtr = conn.rts = False
        conn.open()
        return conn
    except BaseException:
        try:
            conn.close()
        except Exception:
            pass  # Keep the original open/configuration failure.
        raise
