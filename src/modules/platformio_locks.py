"""Keep PlatformIO's Windows package locks safe under concurrent builders."""
from contextlib import contextmanager
import errno
from functools import wraps
import os
from pathlib import Path
import sys
from types import MethodType


def _release_owned(instance):
    # Keep the inode/path stable for waiting processes. Deleting the file on
    # every release races an OPEN_ALWAYS in another Windows builder and can
    # itself cause PermissionError while the entry is pending deletion.
    instance._unlock()


@contextmanager
def package_locks():
    """Adapt only native Windows locks inside the configured app core.

    Upstream opens locks with ``w`` before taking the byte lock. Truncation
    can fail when another builder owns that byte, and also for hidden files.
    Open without truncating and let the original OS lock arbitrate ownership.
    No lock file is deleted early, no permission/ACL is widened, and Linux
    retains its native implementation.
    """
    if sys.platform != "win32":
        yield
        return
    from platformio.package import lockfile
    if lockfile.LOCKFILE_CURRENT_INTERFACE != lockfile.LOCKFILE_INTERFACE_MSVCRT:
        yield
        return
    original = lockfile.LockFile._lock

    @wraps(original)
    def acquire(instance):
        core = os.environ.get("PLATFORMIO_CORE_DIR")
        try:
            owned = bool(core and Path(instance._lock_path).resolve().is_relative_to(Path(core).resolve()))
        except (OSError, ValueError):
            owned = False
        if not owned:
            return original(instance)
        # Retain this release policy on the instance after context teardown;
        # upstream __del__ also calls release during interpreter shutdown.
        instance.release = MethodType(_release_owned, instance)
        # O_CREAT handles both first use and a previous worker's normal
        # cleanup. Omitting O_TRUNC preserves a held lock and hidden files.
        try:
            descriptor = os.open(instance._lock_path, os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o600)
        except PermissionError as exc:
            raise PermissionError(exc.errno,
                "PlatformIO cannot write its package lock. Check write access to this application's toolchain folder; "
                "a read-only file or another program blocking access must be resolved before setup can continue",
                instance._lock_path) from exc
        instance._fp = os.fdopen(descriptor, "r+b")
        try:
            instance._fp.seek(0)
            lockfile.msvcrt.locking(instance._fp.fileno(), lockfile.msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            instance._fp.close()
            instance._fp = None
            # Native LK_NBLCK reports EACCES when another process owns the
            # byte. Invalid descriptors/arguments and other failures must
            # reach setup immediately, rather than its long lock timeout.
            if exc.errno == errno.EACCES:
                raise lockfile.LockFileExists from exc
            raise
        return True

    lockfile.LockFile._lock = acquire
    try:
        yield
    finally:
        lockfile.LockFile._lock = original
