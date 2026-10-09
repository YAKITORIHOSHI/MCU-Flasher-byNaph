"""Bootstrap-only, checksum-verified native Arduino CLI preparation."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen
from uuid import uuid4

from main.platforms import ubuntu_arduino

ROOT = Path(__file__).resolve().parents[2]
VERSION = "1.5.1"
# Official stable v1.5.1 release asset, pinned independently of network metadata:
# https://github.com/arduino/arduino-cli/releases/expanded_assets/v1.5.1
ARCHIVE_NAME = f"arduino-cli_{VERSION}_Linux_64bit.tar.gz"
ARCHIVE_URL = f"https://github.com/arduino/arduino-cli/releases/download/v{VERSION}/{ARCHIVE_NAME}"
ARCHIVE_SHA256 = "28a8e119c498a25607821c36cb2dc49e8463941b261a0d99091baa7bc692dd2b"
OWNER = "mcu-flasher-ubuntu-arduino-cli"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXECUTABLE_BYTES = 128 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _native_amd64(path: Path) -> bool:
    with path.open("rb") as stream:
        header = stream.read(20)
    return (len(header) == 20 and header[:6] == b"\x7fELF\x02\x01"
            and int.from_bytes(header[18:20], "little") == 62)


def _version(executable: str | Path, env) -> str | None:
    """A version command with disposable Arduino directories, never a core install."""
    with tempfile.TemporaryDirectory(prefix="mcu-arduino-version-") as folder:
        probe_env = dict(os.environ if env is None else env)
        probe_env.update(ARDUINO_UPDATER_ENABLE_NOTIFICATION="false",
                         ARDUINO_DIRECTORIES_DATA=folder,
                         ARDUINO_DIRECTORIES_DOWNLOADS=folder,
                         ARDUINO_DIRECTORIES_USER=folder)
        try:
            result = subprocess.run([os.fspath(executable), "version"], cwd=folder, env=probe_env,
                                    capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
    if result.returncode:
        return None
    match = re.search(r"arduino-cli\s+Version:\s*(\d+\.\d+\.\d+)(?:\s|$)", result.stdout)
    return match.group(1) if match else None


def _download_archive(path: Path) -> None:
    digest = hashlib.sha256()
    request = Request(ARCHIVE_URL, headers={"User-Agent": "MCU-Flasher-Ubuntu-Bootstrap"})
    deadline = time.monotonic() + 180
    with urlopen(request, timeout=30) as response, path.open("xb") as stream:
        size = 0
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > MAX_ARCHIVE_BYTES or time.monotonic() > deadline:
                raise RuntimeError("Arduino CLI download exceeded its size/time limit.")
            stream.write(block)
            digest.update(block)
    if digest.hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError("Arduino CLI archive SHA-256 verification failed; the previous installation is preserved.")


def _extract_executable(archive: Path, destination: Path) -> None:
    """Extract only the exact root binary, rejecting links and unsafe archive names."""
    binary = None
    with tarfile.open(archive, "r:gz") as bundle:
        for count, member in enumerate(bundle, start=1):
            name = PurePosixPath(member.name)
            if (count > 256 or name.is_absolute() or ".." in name.parts
                    or "\\" in member.name or member.issym() or member.islnk()
                    or not (member.isfile() or member.isdir())):
                raise RuntimeError("Arduino CLI archive contains an unsafe entry.")
            if name == PurePosixPath("arduino-cli"):
                if binary is not None or not member.isfile() or not 0 < member.size <= MAX_EXECUTABLE_BYTES:
                    raise RuntimeError("Arduino CLI archive contains an invalid executable.")
                binary = member
        if binary is None:
            raise RuntimeError("Arduino CLI archive has no native arduino-cli executable.")
        source = bundle.extractfile(binary)
        if source is None:
            raise RuntimeError("Arduino CLI executable could not be read.")
        with source, destination.open("xb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)
    if not _native_amd64(destination):
        raise RuntimeError("Arduino CLI archive executable is not native Linux amd64.")
    destination.chmod(0o755)


def _owned_directory(folder: Path) -> None:
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise RuntimeError(f"Ubuntu CLI location must be a local directory: {folder}")
    if not folder.exists() or not any(folder.iterdir()):
        return
    if any(item.is_symlink() or not item.is_file() or item.name not in {"arduino-cli", "installation.json"}
           for item in folder.iterdir()):
        raise RuntimeError("Ubuntu Arduino CLI folder contains unrecognized files; Bootstrap will preserve it.")
    receipt = folder / "installation.json"
    try:
        if receipt.stat().st_size > 8192:
            raise ValueError("invalid receipt")
        data = json.loads(receipt.read_text())
        if not isinstance(data, dict) or data.get("owner") != OWNER:
            raise ValueError("invalid receipt")
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("Ubuntu Arduino CLI folder has no valid ownership receipt; Bootstrap will preserve it.") from exc


@contextmanager
def _installation_lock(tools: Path):
    import fcntl  # Linux-only operation; Windows source fixtures may import this module.
    descriptor = os.open(tools / ".arduino-cli.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another Ubuntu Arduino CLI preparation is still active; retry Bootstrap afterward.")
                time.sleep(0.1)
        yield
    finally:
        os.close(descriptor)


def ensure_arduino_cli(*, env=None, log=print) -> str:
    """Reuse a healthy install or atomically prepare the pinned CLI in app-owned files."""
    if (not sys.platform.startswith("linux") or platform.machine().lower() not in {"x86_64", "amd64"}
            or os.geteuid() == 0):
        raise RuntimeError("Prepare Arduino CLI as the normal Ubuntu amd64 desktop account.")
    if os.environ.get("MCU_FLASHER_WORKSPACE_RUNTIME") or os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME"):
        raise RuntimeError("Arduino CLI downloads belong to Ubuntu Bootstrap; restart with --repair.")
    found = ubuntu_arduino.find_arduino_cli()
    managed = ubuntu_arduino.managed_arduino_cli(ROOT)
    if found and found != managed and _version(found, env):
        log("Native Arduino CLI is already installed.")
        return found
    tools = ROOT / ".ubuntu-tools"
    if tools.is_symlink() or (tools.exists() and not tools.is_dir()):
        raise RuntimeError(".ubuntu-tools must be an app-owned local directory.")
    tools.mkdir(exist_ok=True)
    folder = tools / "arduino-cli"
    with _installation_lock(tools):
        _owned_directory(folder)
        managed = ubuntu_arduino.managed_arduino_cli(ROOT)
        if managed:
            receipt = json.loads((folder / "installation.json").read_text())
            if (_sha256(Path(managed)) == receipt.get("executable_sha256")
                    and receipt.get("archive_sha256") == ARCHIVE_SHA256
                    and _native_amd64(Path(managed)) and _version(managed, env) == VERSION):
                log(f"Native Arduino CLI {VERSION} is ready.")
                return managed
        log(f"Preparing native Arduino CLI {VERSION}…")
        with tempfile.TemporaryDirectory(prefix=".arduino-cli-stage-", dir=tools) as temporary:
            stage = Path(temporary)
            archive = stage / "release.tar.gz"
            _download_archive(archive)
            prepared = stage / "prepared"
            prepared.mkdir()
            executable = prepared / "arduino-cli"
            _extract_executable(archive, executable)
            if _version(executable, env) != VERSION:
                raise RuntimeError("Downloaded Arduino CLI failed native version validation; previous files are preserved.")
            receipt = {"owner": OWNER, "architecture": "linux-amd64", "version": VERSION,
                       "executable": "arduino-cli", "executable_sha256": _sha256(executable),
                       "archive_url": ARCHIVE_URL, "archive_sha256": ARCHIVE_SHA256}
            (prepared / "installation.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            # Keep the last installation outside TemporaryDirectory so a failed
            # promotion followed by a failed rollback cannot erase its only copy.
            backup = tools / (".arduino-cli-backup-" + uuid4().hex)
            if folder.exists():
                folder.rename(backup)
            try:
                prepared.rename(folder)
            except OSError:
                if backup.exists():
                    try:
                        backup.rename(folder)
                    except OSError as exc:
                        raise RuntimeError(
                            "Arduino CLI promotion and restoration failed; the previous "
                            f"installation is preserved at {backup}. Resolve the filesystem "
                            "error before restoring that folder."
                        ) from exc
                raise
            if backup.exists():
                shutil.rmtree(backup)
    log(f"Native Arduino CLI {VERSION} is ready.")
    return str(folder / "arduino-cli")
