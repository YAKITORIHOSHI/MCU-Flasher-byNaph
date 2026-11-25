"""Passive serial opens, independent of intentional programmer/reset sequences.

POSIX retains tty flags between opens. Disable hang-up-on-close during pySerial's
initial configuration (before it applies the control lines), and on subsequent
baud changes. This cannot suppress a USB driver's own transient at os.open().
"""
from __future__ import annotations

from functools import lru_cache
from contextlib import contextmanager
import os
import threading

_RETIRE_LOCK = threading.Lock()


def track_reader(conn):
    """Register the one reader thread that will use `conn`; it sets this on leaving."""
    left = threading.Event()
    conn._mcu_reader_left = left
    return left


def retire_connection(conn, *, timeout=1.5) -> bool:
    """Close one monitor connection exactly once, never from under its own reader.

    The operating system reuses a handle number as soon as it is closed. Closing a
    port while its reader is still inside a read lets that read land on whatever
    opened next, including one of the embedded browser's own pipes, and a second
    close can shut an unrelated handle. Wake the reader, wait for it to leave the
    driver, then close. A removed device whose driver never returns is left to a
    small owned thread, so no caller waits longer than `timeout`.
    """
    if conn is None:
        return True
    with _RETIRE_LOCK:
        done = getattr(conn, "_mcu_retired", None)
        first = done is None
        if first:
            done = conn._mcu_retired = threading.Event()
    if not first:
        return done.wait(timeout)
    left = getattr(conn, "_mcu_reader_left", None)
    if left is not None and not left.is_set():
        cancel = getattr(conn, "cancel_read", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass  # The finite read timeout ends this read as well.

    def close():
        try:
            if left is not None:
                left.wait()
            conn.close()
        except Exception:
            pass  # A removed device can refuse its own close.
        finally:
            done.set()

    try:
        threading.Thread(target=close, name="MCU_SerialCloser", daemon=True).start()
    except RuntimeError:
        close()
    return done.wait(timeout)


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
        def _clear_idle_lines(self):
            if self._dtr_state or self._rts_state:
                return False
            import fcntl
            import struct
            from serial.serialposix import TIOCMBIC, TIOCM_DTR, TIOCM_RTS
            # Clearing the two lines separately can briefly select reset on
            # two-line auto-reset circuits. Preserve other modem-control bits.
            fcntl.ioctl(self.fd, TIOCMBIC, struct.pack('I', TIOCM_DTR | TIOCM_RTS))
            return True

        def _update_dtr_state(self):
            if not self._clear_idle_lines():
                super()._update_dtr_state()

        def _update_rts_state(self):
            if not self._clear_idle_lines():
                super()._update_rts_state()

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
