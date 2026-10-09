"""Bootstrap-only native OpenCode installation; never opens an AI session."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from main.platforms.ubuntu_opencode import MANAGED_OWNER, _native_executable, find_opencode_cli

# Official immutable release and GitHub asset SHA-256, reviewed 2026-10-09:
# https://github.com/anomalyco/opencode/releases/tag/v1.18.35
# https://api.github.com/repos/anomalyco/opencode/releases/tags/v1.18.35
# The official installer selects baseline builds on older x64 CPUs.
VERSION = "1.18.35"
ARCHIVE_NAME = "opencode-linux-x64-baseline.tar.gz"
ARCHIVE_URL = f"https://github.com/anomalyco/opencode/releases/download/v{VERSION}/{ARCHIVE_NAME}"
ARCHIVE_SHA256 = "90c97d4a24d36437bce36f27195c9cd2e0b70ed037a04d1c6a6740eb246c2a73"
ARCHIVE_SIZE = 60_669_350
MAX_DOWNLOAD_BYTES = 96 * 1024 * 1024
MAX_BINARY_BYTES = 384 * 1024 * 1024
INSTALL_LOCK_TIMEOUT = 60.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version(executable: str, env) -> str | None:
    """Probe only --version in an empty directory, without provider/auth calls."""
    try:
        with tempfile.TemporaryDirectory(prefix="mcu-opencode-version-") as cwd:
            result = subprocess.run([executable, "--version"], cwd=cwd, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", timeout=20, check=False)
        if result.returncode:
            return None
        output = result.stdout.strip()
        if len(output) > 4096:
            return None
        match = re.fullmatch(r"(?:opencode\s+)?v?(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", output,
                             flags=re.IGNORECASE)
        return match.group(1) if match else None
    except (OSError, subprocess.SubprocessError):
        return None


def _installation_state(directory: Path) -> dict | None:
    certificate = directory / "installation.json"
    if directory.is_symlink() or certificate.is_symlink():
        raise RuntimeError("The OpenCode installation must be local, without symbolic links.")
    if not directory.exists():
        return None
    if not directory.is_dir():
        raise RuntimeError("The app-owned OpenCode location contains a file; preserve it and move it aside before Bootstrap.")
    try:
        if certificate.stat().st_size > 8192:
            raise ValueError("oversized certificate")
        state = json.loads(certificate.read_text(encoding="utf-8"))
        if state.get("owner") != MANAGED_OWNER or state.get("schema") != 1:
            raise ValueError("unrecognized certificate")
        if any(path.name not in {"opencode", "installation.json"} or path.is_symlink()
               or not path.is_file() for path in directory.iterdir()):
            raise ValueError("unrecognized content")
        return state
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("The app-owned OpenCode folder contains unrecognized content. "
                           "Preserve it and move it aside before Bootstrap.") from exc


def _download(destination: Path, log) -> None:
    request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "MCU-Flasher-Ubuntu-Bootstrap"})
    digest, count = hashlib.sha256(), 0
    deadline, last_log = time.monotonic() + 300.0, 0.0
    with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as stream:
        if not response.geturl().startswith("https://"):
            raise RuntimeError("OpenCode download redirected outside HTTPS.")
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError("OpenCode download exceeded its five-minute limit.")
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > MAX_DOWNLOAD_BYTES or count > ARCHIVE_SIZE:
                raise RuntimeError("OpenCode archive exceeded its reviewed size.")
            stream.write(chunk)
            digest.update(chunk)
            now = time.monotonic()
            if now - last_log >= 5:
                log(f"Downloading OpenCode {VERSION}: {count * 100 // ARCHIVE_SIZE}%")
                last_log = now
    if count != ARCHIVE_SIZE or digest.hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError("OpenCode archive failed its pinned size/SHA-256 verification.")


def _extract(archive: Path, destination: Path) -> None:
    """Copy just the bounded root executable; never extract arbitrary tar paths."""
    found, entries = False, 0
    with tarfile.open(archive, mode="r:gz") as bundle:
        for member in bundle:
            entries += 1
            name = PurePosixPath(member.name)
            if entries > 8 or name.is_absolute() or ".." in name.parts:
                raise RuntimeError("OpenCode archive contains an unexpected path.")
            if member.isdir() and str(name) == ".":
                continue
            if name.parts != ("opencode",) or not member.isfile() or found:
                raise RuntimeError("OpenCode archive contains unexpected files or links.")
            if not 0 < member.size <= MAX_BINARY_BYTES:
                raise RuntimeError("OpenCode executable exceeds its extraction limit.")
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError("OpenCode archive executable is unreadable.")
            with source, destination.open("xb") as stream:
                shutil.copyfileobj(source, stream, length=256 * 1024)
            if destination.stat().st_size != member.size:
                raise RuntimeError("OpenCode executable extraction was truncated.")
            found = True
    if not found:
        raise RuntimeError("OpenCode archive does not contain its native executable.")
    destination.chmod(0o755)
    with destination.open("rb") as stream:
        header = stream.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4:6] != b"\x02\x01" or header[18:20] != b"\x3e\x00":
        raise RuntimeError("OpenCode archive does not contain a native Linux amd64 executable.")


@contextmanager
def _installation_lock(tools: Path):
    """Serialize the complete owned-install check and promotion, with bounded wait."""
    import fcntl  # Linux-only operation; Windows source fixtures may import the helper.
    descriptor = os.open(tools / ".opencode.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("The OpenCode preparation lock must be a local regular file.")
        deadline = time.monotonic() + INSTALL_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another Ubuntu OpenCode preparation is still active; retry Bootstrap afterward.")
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        os.close(descriptor)


def ensure_opencode_cli(*, root: Path | None = None, env=None, log=print) -> str:
    """Reuse a working native CLI or atomically prepare the pinned owned binary."""
    if not sys.platform.startswith("linux") or platform.machine().casefold() not in {"x86_64", "amd64"}:
        raise RuntimeError("Native OpenCode Bootstrap supports Ubuntu amd64.")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RuntimeError("Prepare OpenCode as the desktop account, without sudo.")
    if os.environ.get("MCU_FLASHER_WORKSPACE_RUNTIME") or os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME"):
        raise RuntimeError("OpenCode installation is Bootstrap-only. Close the workspace and run Ubuntu Bootstrap.")
    environment = dict(os.environ if env is None else env)
    for key in ("PYTHONHOME", "PYTHONPATH", "MCU_FLASHER_WORKSPACE_RUNTIME", "MCU_FLASHER_OFFLINE_RUNTIME", "PIP_NO_INDEX"):
        environment.pop(key, None)
    directory = (ROOT if root is None else Path(root)) / ".ubuntu-tools" / "opencode"
    tools = directory.parent
    if tools.is_symlink() or tools.exists() and not tools.is_dir():
        raise RuntimeError(".ubuntu-tools must be a local directory, without symbolic links.")
    tools.mkdir(parents=True, exist_ok=True)
    with _installation_lock(tools):
        state = _installation_state(directory)
        # Check owned state only after acquiring the lock. Another Bootstrap may
        # have completed while this one waited, and discovery must see that state.
        candidate = _native_executable(directory / "opencode") if state else None
        if candidate and _sha256(Path(candidate)) == state.get("binary_sha256"):
            version = _version(candidate, environment)
            if version:
                log(f"OpenCode {version} is verified and ready ({candidate}).")
                return candidate
        existing = find_opencode_cli()
        if existing and Path(existing) != directory / "opencode":
            version = _version(existing, environment)
            if version:
                log(f"OpenCode {version} is verified and ready ({existing}).")
                return existing
            log("The current OpenCode command could not be verified; preparing the app-owned native binary.")
        with tempfile.TemporaryDirectory(prefix=".opencode-prepare-", dir=tools) as temporary:
            staging = Path(temporary)
            archive = staging / ARCHIVE_NAME
            log(f"Preparing OpenCode {VERSION} for native Ubuntu…")
            _download(archive, log)
            prepared = staging / "installation"
            prepared.mkdir()
            binary = prepared / "opencode"
            _extract(archive, binary)
            version = _version(str(binary), environment)
            if version != VERSION:
                raise RuntimeError("The prepared OpenCode binary failed its pinned --version check.")
            stat = binary.stat()
            certificate = {"schema": 1, "owner": MANAGED_OWNER, "version": VERSION,
                           "archive": ARCHIVE_NAME, "archive_sha256": ARCHIVE_SHA256,
                           "binary_sha256": _sha256(binary), "size": stat.st_size,
                           "mtime_ns": stat.st_mtime_ns}
            (prepared / "installation.json").write_text(json.dumps(certificate, indent=2) + "\n", encoding="utf-8")
            backup = tools / (".opencode-backup-" + uuid4().hex)
            if directory.exists():
                os.replace(directory, backup)
            try:
                os.replace(prepared, directory)
            except OSError:
                if backup.exists():
                    os.replace(backup, directory)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            log(f"OpenCode {VERSION} is verified and ready ({directory / 'opencode'}).")
            return str(directory / "opencode")
