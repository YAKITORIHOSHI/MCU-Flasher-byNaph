"""Read-only storage hints with bounded, asynchronous discovery.

No timing benchmarks, drive writes, WMI or shell commands are used. Device
queries may stall in a driver, so they never execute on the caller's thread.
Unknown storage keeps the ordinary CPU/RAM policy instead of pretending to be
an HDD. The cache expires so a removable drive letter is not classified forever.
"""
from __future__ import annotations

import ctypes
import ntpath
import os
import re
import struct
import sys
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from itertools import islice
from pathlib import Path


@dataclass(frozen=True)
class StorageProfile:
    kind: str = "unknown"
    worker_limit: int | None = None


UNKNOWN = StorageProfile()
SOLID_STATE = StorageProfile("solid-state")
ROTATIONAL = StorageProfile("rotational", 2)
REMOVABLE = StorageProfile("removable", 2)
NETWORK = StorageProfile("network", 1)


def _windows_network_path(path: str) -> bool:
    value = path.replace("/", "\\")
    return value.lower().startswith("\\\\?\\unc\\") or (
        value.startswith("\\\\") and not value.startswith(("\\\\?\\", "\\\\.\\")))


def _windows_profile(path: str) -> StorageProfile:
    # The lexical UNC check does not touch a potentially disconnected server.
    if _windows_network_path(path):
        return NETWORK
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    api.GetVolumePathNameW.restype = wintypes.BOOL
    api.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    api.GetDriveTypeW.restype = wintypes.UINT
    api.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    api.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    api.DeviceIoControl.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL

    # Resolve aliases only in this worker; compiler arguments keep their short
    # original spelling. Mounted-folder volumes need their own GUID, not C:.
    canonical = os.path.realpath(path)
    root = ctypes.create_unicode_buffer(32768)
    if not api.GetVolumePathNameW(canonical, root, len(root)):
        return UNKNOWN
    drive_type = api.GetDriveTypeW(root.value)
    if drive_type == 4:  # DRIVE_REMOTE
        return NETWORK
    if drive_type == 2:  # DRIVE_REMOVABLE
        return REMOVABLE
    if drive_type == 6:  # DRIVE_RAMDISK
        return SOLID_STATE
    if drive_type != 3:  # DRIVE_FIXED; unknown/offline/optical are not HDD proof
        return UNKNOWN

    volume = ctypes.create_unicode_buffer(1024)
    if api.GetVolumeNameForVolumeMountPointW(root.value, volume, len(volume)):
        device = volume.value.rstrip("\\")
    elif re.fullmatch(r"[A-Za-z]:\\", root.value):
        device = "\\\\.\\" + ntpath.splitdrive(root.value)[0]
    else:
        return UNKNOWN
    handle = api.CreateFileW(device, 0, 7, None, 3, 0, None)
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        return UNKNOWN
    try:
        def query(property_id: int, size: int) -> bytes:
            # STORAGE_PROPERTY_QUERY includes one AdditionalParameters byte,
            # plus DWORD alignment: sizeof(query) is 12, not 8.
            request = ctypes.create_string_buffer(struct.pack("<IIB3x", property_id, 0, 0))
            output = ctypes.create_string_buffer(size)
            returned = wintypes.DWORD()
            ok = api.DeviceIoControl(handle, 0x002D1400, request, 12, output,
                                     size, ctypes.byref(returned), None)
            return output.raw[:min(returned.value, size)] if ok else b""

        descriptor = query(0, 1024)  # StorageDeviceProperty
        if len(descriptor) >= 36 and struct.unpack_from("<I", descriptor, 4)[0] >= 36:
            if descriptor[10] or struct.unpack_from("<I", descriptor, 28)[0] == 7:
                # USB enclosures may advertise DRIVE_FIXED. Keep their random
                # I/O bounded even when their underlying medium reports SSD.
                return REMOVABLE
        seek = query(7, 12)  # StorageDeviceSeekPenaltyProperty
        if len(seek) >= 9 and struct.unpack_from("<I", seek, 4)[0] >= 9:
            return ROTATIONAL if seek[8] else SOLID_STATE
        return UNKNOWN
    finally:
        api.CloseHandle(handle)


def _small_text(path: Path, limit: int = 64) -> str:
    try:
        with path.open("r", encoding="ascii", errors="replace") as stream:
            return stream.read(limit).strip()
    except OSError:
        return ""


def _linux_block_profile(device: Path, depth: int = 0) -> StorageProfile:
    if depth > 4:
        return UNKNOWN
    device = device.resolve(strict=True)
    profiles = []
    # A RAID/LVM queue may say non-rotational while one backing disk is HDD.
    try:
        slaves = list(islice((device / "slaves").iterdir(), 16))
    except OSError:
        slaves = []
    for child in slaves:
        try:
            profiles.append(_linux_block_profile(child, depth + 1))
        except OSError:
            pass
    limits = [item for item in profiles if item.worker_limit is not None]
    if limits:
        return min(limits, key=lambda item: item.worker_limit)
    # Partition entries inherit the queue/removable flag from their parent.
    for candidate in (device, device.parent):
        if _small_text(candidate / "removable") == "1" or "/usb" in str(candidate):
            return REMOVABLE
        rotational = _small_text(candidate / "queue/rotational")
        if rotational in ("0", "1"):
            return ROTATIONAL if rotational == "1" else SOLID_STATE
    return UNKNOWN


def _linux_profile(path: str) -> StorageProfile:
    canonical = os.path.realpath(path)
    candidate = Path(canonical)
    for _ in range(16):
        try:
            info = candidate.stat()
            break
        except FileNotFoundError:
            if candidate == candidate.parent:
                return UNKNOWN
            candidate = candidate.parent
    else:
        return UNKNOWN
    # Linux reports network devices with virtual device IDs, not block sysfs.
    mount_length = -1
    mount_fs = ""
    text = _small_text(Path("/proc/self/mountinfo"), 512_000)
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields:
            continue
        mount = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        if canonical == mount or canonical.startswith(mount.rstrip("/") + "/"):
            if len(mount) > mount_length:
                separator = fields.index("-")
                if separator + 1 < len(fields):
                    mount_length, mount_fs = len(mount), fields[separator + 1]
    if mount_fs in {"nfs", "nfs4", "cifs", "smb3", "9p", "fuse.sshfs", "fuse.rclone"}:
        return NETWORK
    if mount_fs in {"tmpfs", "ramfs"}:
        return SOLID_STATE
    device = Path(f"/sys/dev/block/{os.major(info.st_dev)}:{os.minor(info.st_dev)}")
    try:
        return _linux_block_profile(device)
    except OSError:
        return UNKNOWN


def _probe(path: str) -> StorageProfile:
    try:
        if sys.platform == "win32":
            return _windows_profile(path)
        if sys.platform.startswith("linux"):
            return _linux_profile(path)
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return UNKNOWN


class StorageProbeCache:
    """One lazy daemon, bounded queue/cache, no periodic polling or disk state."""
    def __init__(self, probe=_probe, clock=time.monotonic):
        self._probe = probe
        self._clock = clock
        self._lock = threading.Lock()
        self._cache = OrderedDict()
        self._pending = {}
        self._queue = deque()
        self._worker = None

    def request(self, path: str, wait_seconds: float = 0) -> StorageProfile:
        if sys.platform == "win32" and _windows_network_path(path):
            # Known UNC paths need neither a queued probe nor a server access.
            return NETWORK
        with self._lock:
            cached = self._cache.get(path)
            if cached and cached[0] > self._clock():
                self._cache.move_to_end(path)
                return cached[1]
            pending = self._pending.get(path)
            if pending is None and len(self._pending) < 32:
                pending = threading.Event()
                self._pending[path] = pending
                self._queue.append(path)
                if self._worker is None:
                    try:
                        self._worker = threading.Thread(target=self._drain, name="storage-hints", daemon=True)
                        self._worker.start()
                    except Exception:
                        self._worker = None
                        self._queue.clear()
                        for event in self._pending.values():
                            event.set()
                        self._pending.clear()
            fallback = cached[1] if cached else UNKNOWN
        if pending is not None and wait_seconds > 0:
            pending.wait(min(0.35, wait_seconds))
            with self._lock:
                cached = self._cache.get(path)
                if cached:
                    return cached[1]
        return fallback

    def _drain(self):
        while True:
            with self._lock:
                if not self._queue:
                    self._worker = None
                    return
                path = self._queue.popleft()
            try:
                profile = self._probe(path)
            except Exception:
                profile = UNKNOWN
            with self._lock:
                # Unknown/permission failure is retried less aggressively than
                # each build; drive swaps refresh known hints within 5 minutes.
                self._cache[path] = (self._clock() + (30 if profile == UNKNOWN else 300), profile)
                self._cache.move_to_end(path)
                while len(self._cache) > 128:
                    self._cache.popitem(last=False)
                event = self._pending.pop(path, None)
                if event:
                    event.set()


_CACHE = StorageProbeCache()


def relevant_storage_paths(paths=None) -> tuple[str, ...]:
    app = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    values = [app, os.environ.get("PLATFORMIO_CORE_DIR"), os.environ.get("PLATFORMIO_PACKAGES_DIR")]
    if paths is not None:
        values.extend((paths,) if isinstance(paths, (str, os.PathLike)) else islice(paths, 16))
    return tuple(dict.fromkeys(os.path.normcase(os.path.abspath(os.fspath(item)))
                               for item in values if item))


def storage_worker_limit(paths=None, wait: bool = False) -> int | None:
    """Return the slowest known relevant path's cap; never wait by default.

    Build/bootstrap workers may opt into at most 350 ms total waiting. A
    misbehaving filesystem/driver therefore cannot hold the UI or a build
    decision indefinitely. Callers do not need to identify the physical disk.
    """
    deadline = time.monotonic() + (0.35 if wait else 0)
    profiles = [_CACHE.request(path, max(0, deadline - time.monotonic()))
                for path in relevant_storage_paths(paths)]
    limits = [profile.worker_limit for profile in profiles if profile.worker_limit is not None]
    return min(limits) if limits else None


__all__ = ["StorageProfile", "storage_worker_limit", "relevant_storage_paths"]
