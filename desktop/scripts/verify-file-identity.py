"""Compare metadata-only native identities inside disposable audit fixtures.

The Python controller alone mutates its temporary files. Stable identities are
not content hashes, locks, safe-replacement receipts or production path authority.
"""
from pathlib import Path
import ctypes
import json
import os
import platform
import queue
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "desktop/target/debug" / (
    "file_identity_probe.exe" if sys.platform == "win32" else "file_identity_probe")
FRAME_LIMIT = 8192
WAIT_SECONDS = 5
MAX_COMMANDS = 12
WORKER_DEADLINE_SECONDS = 10
FRAME_KEYS = {
    "schemaVersion", "fixtureOnly", "ready", "rootIdentity", "sourceIdentity",
    "rootValid", "sourceMatches", "currentIdentity",
}


def reference_identity(path):
    """Use Python's OS metadata API independently of the Rust adapter."""
    if sys.platform == "linux":
        metadata = path.stat(follow_symlinks=False)
        return dict(backend="linux", device=str(metadata.st_dev), inode=str(metadata.st_ino))
    if sys.platform != "win32":
        raise RuntimeError("File identity fixtures require native Linux or Windows")

    from ctypes import wintypes

    class FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class FileIdInfo(ctypes.Structure):
        _fields_ = [("VolumeSerialNumber", ctypes.c_ulonglong), ("FileId", FileId128)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                  ctypes.c_void_p, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # READ_ATTRIBUTES, all sharing modes, OPEN_EXISTING, BACKUP_SEMANTICS and
    # OPEN_REPARSE_POINT. This controller opens only the files it created.
    handle = kernel.CreateFileW(str(path), 0x80, 0x7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = FileIdInfo()
        if not kernel.GetFileInformationByHandleEx(
                handle, 18, ctypes.byref(information), ctypes.sizeof(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        return dict(backend="windows", volume_serial=str(information.VolumeSerialNumber),
                    file_id=list(information.FileId.Identifier))
    finally:
        if not kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def identity(value):
    assert isinstance(value, dict), value
    if sys.platform == "linux":
        assert set(value) == {"backend", "device", "inode"} and value["backend"] == "linux", value
        numbers = (value["device"], value["inode"])
    else:
        assert set(value) == {"backend", "volume_serial", "file_id"}, value
        assert value["backend"] == "windows", value
        numbers = (value["volume_serial"],)
        assert isinstance(value["file_id"], list) and len(value["file_id"]) == 16, value
        assert all(type(byte) is int and 0 <= byte <= 255 for byte in value["file_id"]), value
    assert all(isinstance(number, str) and number.isascii() and number.isdecimal()
               and str(int(number)) == number and 0 <= int(number) < 2 ** 64
               for number in numbers), value
    return value


def refusal(value):
    assert isinstance(value, dict) and set(value) == {"error"}, value
    error = value["error"]
    assert isinstance(error, dict) and set(error) == {"kind", "message"}, value
    assert all(isinstance(error[key], str) and error[key] for key in ("kind", "message")), value
    assert len(error["message"]) <= 256, value


def snapshot(value, ready):
    assert isinstance(value, dict) and set(value) == FRAME_KEYS, value
    assert type(value["schemaVersion"]) is int and value["schemaVersion"] == 1, value
    assert value["fixtureOnly"] is True and value["ready"] is ready, value
    identity(value["rootIdentity"])
    identity(value["sourceIdentity"])
    for key in ("rootValid", "sourceMatches"):
        if type(value[key]) is not bool:
            refusal(value[key])
    assert isinstance(value["currentIdentity"], dict), value
    if "error" in value["currentIdentity"]:
        refusal(value["currentIdentity"])
    else:
        identity(value["currentIdentity"])
    return value


def failure(value, expected=None):
    assert isinstance(value, dict), value
    assert set(value) == {"schemaVersion", "fixtureOnly", "passed", "error"}, value
    assert type(value["schemaVersion"]) is int and value["schemaVersion"] == 1, value
    assert value["fixtureOnly"] is True and value["passed"] is False, value
    assert isinstance(value["error"], str) and 0 < len(value["error"]) <= 512, value
    if expected is not None:
        assert expected in value["error"], value


def directory_alias(link, target):
    """Create only an owned fixture symlink or Windows mount-point reparse.

    Windows junctions avoid requiring Developer Mode for symbolic links. The
    caller must remove the alias with rmdir before recursive fixture cleanup.
    This controller has write authority over its disposable fixtures only; the
    Rust worker continues to request metadata capability only.
    """
    if sys.platform == "linux":
        link.symlink_to(target, target_is_directory=True)
        return

    from ctypes import wintypes

    assert sys.platform == "win32" and target.is_absolute(), target
    # Mount-point junction targets require a local drive. Do not silently skip
    # reparse qualification on a filesystem where the fixture cannot be made.
    assert len(target.drive) == 2 and target.drive[1] == ":", target
    substitute = ("\\??\\" + str(target)).encode("utf-16-le")
    printable = str(target).encode("utf-16-le")
    paths = substitute + b"\0\0" + printable + b"\0\0"
    payload = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(printable)) + paths
    # REPARSE_DATA_BUFFER / MountPointReparseBuffer, documented by Microsoft:
    # https://learn.microsoft.com/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_reparse_data_buffer
    data = struct.pack("<IHH", 0xA0000003, len(payload), 0) + payload
    assert len(data) <= 16384
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    link.mkdir()
    try:
        # GENERIC_WRITE, all sharing modes, OPEN_EXISTING and directory/reparse
        # flags. The live buffer and synchronous handle outlive DeviceIoControl.
        handle = kernel.CreateFileW(str(link), 0x40000000, 0x7, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            buffer = ctypes.create_string_buffer(data)
            returned = wintypes.DWORD()
            if not kernel.DeviceIoControl(handle, 0x900A4, buffer, len(data), None, 0,
                                          ctypes.byref(returned), None):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if not kernel.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        assert link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        assert link.lstat().st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT, link
    except BaseException:
        link.rmdir()
        raise


def remove_directory_alias(link):
    if sys.platform == "win32":
        link.rmdir()
    else:
        link.unlink()


class FixedProbe:
    def __init__(self, fixture):
        self.child = subprocess.Popen(
            [str(EXE), str(ROOT), str(fixture)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.frames = queue.Queue(maxsize=16)
        self.stderr = b""
        self.stopping = threading.Event()

        def read_frames():
            while not self.stopping.is_set():
                line = self.child.stdout.readline(FRAME_LIMIT + 1)
                while not self.stopping.is_set():
                    try:
                        self.frames.put(line, timeout=0.1)
                        break
                    except queue.Full:
                        continue
                if not line or len(line) > FRAME_LIMIT:
                    return

        def read_errors():
            self.stderr = self.child.stderr.read(FRAME_LIMIT + 1)

        self.readers = [threading.Thread(target=read_frames, daemon=True),
                        threading.Thread(target=read_errors, daemon=True)]
        for reader in self.readers:
            reader.start()

    def frame(self, timeout=WAIT_SECONDS):
        line = self.frames.get(timeout=timeout)
        assert line and len(line) <= FRAME_LIMIT and line.endswith(b"\n"), line
        return json.loads(line.decode("utf-8"))

    def inspect(self):
        self.child.stdin.write(b"inspect\n")
        self.child.stdin.flush()
        return snapshot(self.frame(), False)

    def release(self):
        self.child.stdin.write(b"release\n")
        self.child.stdin.flush()
        assert self.frame() == dict(schemaVersion=1, fixtureOnly=True, released=True)
        self.finish(0)

    def finish(self, expected_code):
        self.child.stdin.close()
        assert self.child.wait(timeout=WAIT_SECONDS) == expected_code
        assert self.frames.get(timeout=WAIT_SECONDS) == b""
        for reader in self.readers:
            reader.join(timeout=WAIT_SECONDS)
            assert not reader.is_alive()
        assert not self.stderr, self.stderr

    def close(self):
        self.stopping.set()
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(timeout=WAIT_SECONDS)
        for reader in self.readers:
            reader.join(timeout=WAIT_SECONDS)
            assert not reader.is_alive(), "Native fixture pipe reader did not stop"
        for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
            stream.close()


def protocol_refusal(fixture, command, expected, inspections=0):
    result = subprocess.run([str(EXE), str(ROOT), str(fixture)], input=command,
                            capture_output=True, timeout=WAIT_SECONDS, check=False)
    assert result.returncode != 0 and not result.stderr, (result.returncode, result.stderr)
    assert len(result.stdout) <= (2 + inspections) * FRAME_LIMIT, result.stdout
    lines = result.stdout.splitlines()
    assert len(lines) == 2 + inspections and all(len(line) <= FRAME_LIMIT for line in lines), lines
    snapshot(json.loads(lines[0]), True)
    for line in lines[1:-1]:
        snapshot(json.loads(line), False)
    failure(json.loads(lines[-1]), expected)


def startup_refusal(fixture):
    result = subprocess.run([str(EXE), str(ROOT), str(fixture)], input=b"release\n",
                            capture_output=True, timeout=WAIT_SECONDS, check=False)
    assert result.returncode != 0 and not result.stderr, (result.returncode, result.stderr)
    assert 0 < len(result.stdout) <= FRAME_LIMIT and result.stdout.endswith(b"\n"), result.stdout
    lines = result.stdout.splitlines()
    assert len(lines) == 1, lines
    failure(json.loads(lines[0]))


def main():
    sandbox = ROOT / "audit/migration/scratch"
    # Check existing fixture ancestors before creating controller-owned data.
    # This is a conservative point-in-time guard, not race-free path authority.
    current = ROOT
    for leaf in ("audit", "migration", "scratch"):
        current = current / leaf
        if not current.exists():
            current.mkdir()
        metadata = current.lstat()
        assert stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode), current
        assert not getattr(metadata, "st_file_attributes", 0) & 0x400, current
    checks = []
    original = b"// fixture identity A\n"
    changed = b"// fixture identity B\n"
    with tempfile.TemporaryDirectory(prefix="file-identity-", dir=sandbox) as directory:
        base = Path(directory)
        fixture = base / "sketch"
        fixture.mkdir()
        source = fixture / "Main.ino"
        source.write_bytes(original)
        baseline_stat = source.stat()
        root_id, source_id = reference_identity(fixture), reference_identity(source)
        child = FixedProbe(fixture)
        try:
            first = snapshot(child.frame(), True)
            assert first["rootIdentity"] == root_id, (first, root_id)
            checks.append("root-handle-identity-matches-independent-native-reference")
            assert first["sourceIdentity"] == source_id, (first, source_id)
            checks.append("source-handle-identity-matches-independent-native-reference")
            assert first["rootValid"] is True and first["sourceMatches"] is True, first
            assert first["currentIdentity"] == source_id, first
            checks.append("initial-fixed-source-matches-retained-handle")

            source.write_bytes(changed)
            os.utime(source, ns=(baseline_stat.st_atime_ns, baseline_stat.st_mtime_ns))
            edited = child.inspect()
            assert source.read_bytes() == changed and source.stat().st_mtime_ns == baseline_stat.st_mtime_ns
            assert reference_identity(source) == source_id, edited
            assert edited["rootValid"] is True and edited["sourceMatches"] is True, edited
            assert edited["sourceIdentity"] == edited["currentIdentity"] == source_id, edited
            checks.append("in-place-restored-timestamp-edit-keeps-identity-not-content-proof")

            retained = fixture / "Original.ino"
            source.rename(retained)
            source.write_bytes(original)
            os.utime(source, ns=(baseline_stat.st_atime_ns, baseline_stat.st_mtime_ns))
            replacement_id = reference_identity(source)
            replaced = child.inspect()
            assert source.read_bytes() == original and source.stat().st_mtime_ns == baseline_stat.st_mtime_ns
            assert replacement_id != source_id, (replacement_id, source_id)
            assert replaced["sourceIdentity"] == reference_identity(retained) == source_id, replaced
            checks.append("retained-handle-survives-original-file-rename")
            assert replaced["rootValid"] is True and replaced["sourceMatches"] is False, replaced
            assert replaced["currentIdentity"] == replacement_id, replaced
            checks.append("same-bytes-and-mtime-replacement-has-distinct-identity")

            source.unlink()
            os.link(retained, source)
            linked = child.inspect()
            assert linked["rootValid"] is True and linked["sourceMatches"] is True, linked
            assert linked["sourceIdentity"] == linked["currentIdentity"] == source_id, linked
            assert reference_identity(source) == source_id
            checks.append("hard-link-to-retained-original-matches-native-identity")

            source.unlink()
            missing = child.inspect()
            assert missing["rootValid"] is True and missing["sourceMatches"] is False, missing
            refusal(missing["currentIdentity"])
            assert missing["sourceIdentity"] == reference_identity(retained) == source_id, missing
            checks.append("missing-source-is-distinct-from-invalid-root")

            if sys.platform == "linux":
                source.symlink_to(retained)
            else:
                directory_alias(source, fixture)
            try:
                aliased = child.inspect()
                assert aliased["rootValid"] is True, aliased
                refusal(aliased["sourceMatches"])
                refusal(aliased["currentIdentity"])
                assert aliased["sourceIdentity"] == source_id, aliased
                checks.append("source-alias-refused-with-original-handle-retained")
            finally:
                if sys.platform == "linux":
                    source.unlink()
                else:
                    remove_directory_alias(source)
            os.link(retained, source)

            moved = base / "retained-sketch"
            fixture.rename(moved)
            fixture.mkdir()
            source.write_bytes(original)
            moved_frame = child.inspect()
            assert reference_identity(fixture) != root_id
            refusal(moved_frame["rootValid"])
            refusal(moved_frame["sourceMatches"])
            refusal(moved_frame["currentIdentity"])
            checks.append("renamed-and-replaced-root-refuses-new-path-observations")
            assert moved_frame["rootIdentity"] == reference_identity(moved) == root_id, moved_frame
            assert moved_frame["sourceIdentity"] == reference_identity(moved / "Main.ino") == source_id
            checks.append("retained-root-and-source-handle-identities-stay-stable")

            source.unlink()
            fixture.rmdir()
            directory_alias(fixture, moved)
            try:
                aliased_root = child.inspect()
                refusal(aliased_root["rootValid"])
                refusal(aliased_root["sourceMatches"])
                refusal(aliased_root["currentIdentity"])
                assert aliased_root["rootIdentity"] == root_id, aliased_root
                assert aliased_root["sourceIdentity"] == source_id, aliased_root
                checks.append("root-alias-refused-even-when-it-targets-retained-original")
            finally:
                remove_directory_alias(fixture)
            fixture.mkdir()
            source.write_bytes(original)
            child.release()
            checks.append("fixed-release-closes-native-worker-cleanly")
        finally:
            child.close()

        before = source.read_bytes(), reference_identity(fixture), reference_identity(source)
        for label, command, expected in (
                ("unknown-command-refused", b"unknown\n", "Only fixed inspect/release"),
                ("oversized-command-refused", b"x" * 33 + b"\n", "Incomplete or oversized"),
                ("controller-eof-refused", b"", "Incomplete or oversized"),
                ("unterminated-command-refused", b"inspect", "Incomplete or oversized"),
                ("invalid-utf8-command-refused", b"\xff\n", "UTF-8")):
            protocol_refusal(fixture, command, expected)
            assert before == (source.read_bytes(), reference_identity(fixture), reference_identity(source))
            checks.append(label)

        protocol_refusal(fixture, b"inspect\n" * MAX_COMMANDS,
                         "command budget exhausted", inspections=MAX_COMMANDS)
        checks.append("fixed-command-budget-exits-after-final-observation")

        child = FixedProbe(fixture)
        try:
            snapshot(child.frame(), True)
            child.child.stdin.write(b"inspect\r\nrelease\r\n")
            child.child.stdin.flush()
            snapshot(child.frame(), False)
            assert child.frame() == dict(schemaVersion=1, fixtureOnly=True, released=True)
            child.finish(0)
            checks.append("windows-crlf-fixed-commands-release-cleanly")
        finally:
            child.close()

        child = FixedProbe(fixture)
        try:
            snapshot(child.frame(), True)
            started = time.monotonic()
            failure(child.frame(timeout=WORKER_DEADLINE_SECONDS + WAIT_SECONDS),
                    "Metadata fixture controller exceeded its deadline")
            elapsed = time.monotonic() - started
            assert WORKER_DEADLINE_SECONDS - 1 <= elapsed <= WORKER_DEADLINE_SECONDS + WAIT_SECONDS
            child.finish(1)
            checks.append("silent-controller-deadline-releases-worker-and-handles")
        finally:
            child.close()
        assert before == (source.read_bytes(), reference_identity(fixture), reference_identity(source))

        for label, path in (("sandbox-root-itself-refused", sandbox),
                            ("parent-aliased-root-refused", fixture / ".."),
                            ("regular-file-root-refused", source),
                            ("missing-fixture-root-refused", base / "missing-root")):
            startup_refusal(path)
            checks.append(label)

        blocked = base / "blocked-sketch"
        blocked.mkdir()
        startup_refusal(blocked)
        checks.append("missing-initial-source-refused")
        blocked_source = blocked / "Main.ino"
        blocked_source.mkdir()
        startup_refusal(blocked)
        checks.append("directory-initial-source-refused")
        blocked_source.rmdir()
        if sys.platform == "linux":
            blocked_source.symlink_to(source)
        else:
            directory_alias(blocked_source, fixture)
        try:
            startup_refusal(blocked)
            checks.append("alias-initial-source-refused")
        finally:
            if sys.platform == "linux":
                blocked_source.unlink()
            else:
                remove_directory_alias(blocked_source)

        alias = base / "root-alias"
        directory_alias(alias, fixture)
        try:
            startup_refusal(alias)
            checks.append("symlink-root-refused" if sys.platform == "linux" else "junction-root-refused")
            nested = fixture / "nested-sketch"
            nested.mkdir()
            (nested / "Main.ino").write_bytes(original)
            startup_refusal(alias / nested.name)
            checks.append("symlink-ancestor-refused" if sys.platform == "linux"
                          else "junction-ancestor-refused")
        finally:
            remove_directory_alias(alias)

        if sys.platform == "linux":
            os.mkfifo(blocked_source)
            try:
                startup_refusal(blocked)
                checks.append("fifo-initial-source-refused-without-data-open")
            finally:
                blocked_source.unlink()
        assert before == (source.read_bytes(), reference_identity(fixture), reference_identity(source))

    print(json.dumps(dict(
        schemaVersion=1, fixtureOnly=True, platform=sys.platform,
        architecture=platform.machine(), passed=True, checks=checks, checksPassed=len(checks),
        reference=("os.stat(st_dev, st_ino)" if sys.platform == "linux"
                   else "ctypes GetFileInformationByHandleEx(FILE_ID_INFO)"),
        identityIsContentProof=False, productionFilesystemQualified=False,
        raceFreeTraversalQualified=False, durableReplacementQualified=False,
        metadataPreservationQualified=False,
        nativeAlias=("symlink" if sys.platform == "linux" else "mount-point-junction"),
        liveSettingsAccess=False, liveProjectsAccess=False, hardwareAccess=False)))


if __name__ == "__main__":
    main()
