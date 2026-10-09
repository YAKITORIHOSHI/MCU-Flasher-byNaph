"""Ubuntu downloader ownership and wake-up IPC, without Tk or Qt imports."""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import threading
import time


class AlreadyRunning(RuntimeError):
    """Another downloader in this account already owns this checkout."""


def _address(root) -> str:
    identity = hashlib.sha256(os.fsencode(str(Path(root).resolve()))).hexdigest()[:24]
    # Abstract sockets vanish with their process: no stale PID, lock or socket
    # file can accidentally identify a later process or block a moved checkout.
    return f"\0mcu-flasher-downloader-{os.getuid()}-{identity}"


def _same_user(connection: socket.socket) -> bool:
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", credentials)[1] == os.getuid()


def request(root, command: str) -> bool:
    """Send a bounded command to this checkout's owned downloader, if present."""
    if command not in {"wake", "quit"}:
        raise ValueError("Unknown downloader command")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(.15)
        try:
            connection.connect(_address(root))
            if not _same_user(connection):
                return False
            connection.sendall(("MCU-DM-1 " + command + "\n").encode("ascii"))
            return True
        except OSError:
            return False


def _workspace_belongs_to(root, pid: str) -> bool:
    import psutil
    expected = {str(Path(root).resolve() / name)
                for name in ("mcu_flash_gui.py", "main/mcu_flash_gui.py")}
    try:
        return any(argument in expected for argument in psutil.Process(int(pid)).cmdline())
    except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def quit_if_last_workspace(root, current_pid) -> bool:
    """Keep the shared downloader while another live checkout window owns it."""
    from main.core import config
    data = config._load_raw_config(fresh=True)
    alive = config._get_alive_pid_create_times()
    for pid, instance in data.get("instances", {}).items():
        if (str(pid) == str(current_pid) or not isinstance(instance, dict)
                or not config._instance_is_alive(str(pid), instance, alive)):
            continue
        if _workspace_belongs_to(root, str(pid)):
            return False
    return request(root, "quit")


class DownloadManagerChannel:
    """One manager per checkout; drain commands only from the Tk thread."""

    def __init__(self, root):
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._clients = {}
        try:
            self._socket.bind(_address(root))
            self._socket.listen(16)
            self._socket.setblocking(False)
        except OSError as exc:
            self._socket.close()
            if exc.errno == errno.EADDRINUSE:
                raise AlreadyRunning("The Ubuntu Download Manager is already running") from exc
            raise

    def poll(self) -> list[str]:
        """Never block the UI on an incomplete or excessive local request."""
        commands = []
        now = time.monotonic()
        for _ in range(16):
            try:
                connection, _ = self._socket.accept()
            except BlockingIOError:
                break
            connection.setblocking(False)
            if len(self._clients) >= 16 or not _same_user(connection):
                connection.close()
            else:
                self._clients[connection] = (b"", now + 1)
        for connection, (buffer, deadline) in list(self._clients.items()):
            try:
                chunk = connection.recv(32)
            except BlockingIOError:
                if now < deadline:
                    continue
                chunk = b""
            except OSError:
                chunk = b""
            buffer += chunk
            if b"\n" not in buffer and chunk and len(buffer) < 32 and now < deadline:
                self._clients[connection] = (buffer, deadline)
                continue
            connection.close()
            self._clients.pop(connection, None)
            if buffer == b"MCU-DM-1 wake\n":
                commands.append("wake")
            elif buffer == b"MCU-DM-1 quit\n":
                commands.append("quit")
        return commands

    def close(self) -> None:
        for connection in self._clients:
            connection.close()
        self._clients.clear()
        self._socket.close()


_CHILDREN = {}
_CHILD_LOCK = threading.Lock()


def remember_viewer(process: subprocess.Popen) -> None:
    """Retain only direct sample-viewer handles, never arbitrary process IDs."""
    with _CHILD_LOCK:
        if process in _CHILDREN:
            return
        for child, descriptor in list(_CHILDREN.items()):
            if child.poll() is not None:
                if descriptor is not None:
                    os.close(descriptor)
                _CHILDREN.pop(child, None)
        descriptor = None
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                descriptor = os.pidfd_open(process.pid)
            except (OSError, TypeError):
                pass  # Popen retains ownership while an unreaped child exists.
        _CHILDREN[process] = descriptor


def close_viewers() -> None:
    """Close this manager's sample windows with identity-safe, bounded teardown."""
    with _CHILD_LOCK:
        children = dict(_CHILDREN)
        _CHILDREN.clear()
    try:
        for child, descriptor in children.items():
            if child.poll() is None:
                try:
                    if descriptor is not None:
                        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                    else:
                        child.terminate()
                except (OSError, ProcessLookupError):
                    pass
        deadline = time.monotonic() + .5
        remaining = {}
        for child, descriptor in children.items():
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                remaining[child] = descriptor
        for child, descriptor in remaining.items():
            try:
                if descriptor is not None:
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                else:
                    child.kill()
            except OSError:
                pass
        deadline = time.monotonic() + .2
        for child in remaining:
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired):
                pass
    finally:
        for descriptor in children.values():
            if descriptor is not None:
                os.close(descriptor)
