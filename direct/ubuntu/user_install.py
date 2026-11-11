"""Persistent per-user Ubuntu application installation, independent of sketches.

Stage only application sources, verify copied bytes, then prepare the runtime at
its final path. Publishing the Applications entry is a separate success step.
An existing owned installation is repaired in place and follows the normal
forward-only source updater; an older portable copy never overwrites it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time

from src.modules.bootstrap_file_pool import Progress, run_all

RECEIPT = ".mcu-flasher-install.json"
ROOT_FILES = ("mcu_flash_gui.py", "README.md", "RELEASE_NOTES.md", "LICENSE")
SOURCE_FOLDERS = ("main", "src", "direct", "LAUNCHER", "cleaner", "index_json", ".agents/skills")
EXCLUDED = frozenset(("__pycache__", "_python", "env", "node_modules", ".git",
    "temp", "logs", "test", "tests", ".venv-linux", ".mcu_flasher_build_cache",
    ".mcu_ai_edits", ".pio", ".vscode", ".clangd", ".opencode"))
STATE = frozenset(("gui_config.json", ".mcu_gui_config.json", "bootstrap_config.json",
    "arduino_browser_settings.json", "arduino_cli_path.txt", "dbs_notif.json",
    ".owner_config.json", ".owner_tickets.json", ".mcu_cloud_link.json"))
# The copy's own record of the commit it holds, read by the forward-only source updater.
REVISION_RECORD = "src/dbs/source_revision.json"
REQUIRED = ("mcu_flash_gui.py", "main/web_bridge.py", "src/editor/index.html",
            "src/modules/bootstrap.py", "direct/ubuntu/setup.py", "direct/ubuntu/run.sh",
            "src/assets/mcu_icon.png", "LAUNCHER/UBUNTU")


def data_directory(value=None):
    path = Path(value or os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    if not path.is_absolute():
        raise RuntimeError("XDG_DATA_HOME must be absolute for the Ubuntu installation.")
    return path


def installation_directory(data_home=None):
    return data_directory(data_home) / "mcu-flasher/application"


def _plain(path, *, directory=False):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise RuntimeError(f"Redirected or invalid application installation input: {path.name}")
    return info


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_file(root, relative):
    current = root
    _plain(current, directory=True)
    for part in relative.parts[:-1]:
        current = current / part
        _plain(current, directory=True)
    path = current / relative.name
    return path, _plain(path)


def _inventory(root):
    _plain(root, directory=True)
    files = []
    for name in ROOT_FILES:
        if (root / name).is_file():
            files.append(Path(name))
    for folder in SOURCE_FOLDERS:
        base = root / folder
        if not base.exists():
            continue
        _plain(base, directory=True)
        for current, directories, names in os.walk(base, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in EXCLUDED)
            for name in directories:
                _plain(Path(current) / name, directory=True)
            for name in sorted(names):
                if name in STATE or name.endswith((".pyc", ".pyo")):
                    continue
                path = Path(current) / name
                _plain(path)
                files.append(path.relative_to(root))
                if len(files) > 20_000:
                    raise RuntimeError("The application source inventory exceeds its supported size.")
    names = {path.as_posix() for path in files}
    if set(REQUIRED) - names:
        raise RuntimeError("The source is not a complete MCU Flasher application.")
    total = sum((root / path).stat().st_size for path in files)
    if total > 2 * 1024**3:
        raise RuntimeError("The application sources exceed the supported installation size.")
    return sorted(files), total


def checkout_revision(source):
    """The commit a Git working copy holds, or None for a release folder.

    A checkout's tracked record lags behind on purpose (a commit cannot name itself),
    and once named a commit that was amended before pushing: GitHub could never compare
    it, so copies installed from that checkout stopped updating. Record HEAD instead.
    A release ZIP's own record comes from build_release.py and is copied unchanged.
    """
    git = shutil.which("git")
    if git is None or not (Path(source) / ".git").exists():
        return None
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    try:
        output = subprocess.run([git, "-C", str(source), "show", "-s", "--format=%H %ct", "HEAD"],
                                capture_output=True, timeout=15, check=True, env=environment,
                                stdin=subprocess.DEVNULL).stdout.decode("ascii").split()
        commit, seconds = output[0].casefold(), int(output[1])
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, IndexError):
        return None
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None
    return {"repository": "YAKITORIHOSHI/MCU-Flasher-byNaph", "commit": commit,
            "committed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))}


def is_owned_installation(root):
    try:
        _plain(root, directory=True)
        marker = root / RECEIPT
        _plain(marker)
        if marker.stat().st_size > 4 * 1024**2:
            return False
        receipt = json.loads(marker.read_text(encoding="utf-8"))
        records = receipt.get("files")
        return (receipt.get("schema") == 1 and receipt.get("application") == "MCU Flasher"
                and isinstance(records, dict) and set(REQUIRED) <= records.keys()
                and all((root / name).is_file() and not (root / name).is_symlink() for name in REQUIRED))
    except (OSError, ValueError, RuntimeError):
        return False


def ensure_user_installation(source, *, data_home=None, report=print):
    source = Path(source).absolute()
    destination = installation_directory(data_home)
    if source == destination and is_owned_installation(destination):
        return destination
    # Inspect source completely before creating any per-user installation path.
    files, total = _inventory(source)
    parent = destination.parent
    if parent.is_symlink() or destination.is_symlink():
        raise RuntimeError("The per-user application installation must not be redirected.")
    if destination.exists():
        if not is_owned_installation(destination):
            raise RuntimeError("The installation location contains unrecognized files; they have been preserved.")
        return destination
    parent.mkdir(parents=True, exist_ok=True)
    _plain(parent, directory=True)
    if shutil.disk_usage(parent).free < total + 256 * 1024**2:
        raise RuntimeError("There is not enough free space to stage the Ubuntu application.")
    stage = Path(tempfile.mkdtemp(prefix=".application-stage-", dir=parent))
    report("Installing MCU Flasher in per-user application storage…")
    receipts = {}
    progress = Progress(report, "Ubuntu application", len(files))

    def copy(relative):
        original, before = _source_file(source, relative)
        staged = stage / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, staged)
        # Independent source and staged-byte passes retain copy2 metadata and
        # detect same-size/coarse-timestamp changes during the copy.
        source_hash, stage_hash = _digest(original), _digest(staged)
        _, after = _source_file(source, relative)
        if (source_hash != stage_hash or
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise RuntimeError(f"Application source changed during installation: {relative}")
        receipts[relative.as_posix()] = {"sha256": stage_hash, "size": after.st_size}

    revision = checkout_revision(source)
    try:
        run_all(copy, files, progress=progress)
        if revision is not None:
            record = stage / REVISION_RECORD
            record.parent.mkdir(parents=True, exist_ok=True)
            data = (json.dumps(revision, indent=2) + "\n").encode("ascii")
            record.write_bytes(data)
            receipts[REVISION_RECORD] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        # These private preferences are data, outside the source manifest. The
        # per-user GUI store remains authoritative, and existing installations
        # retain their own settings. Never copy accounts, sketches or journals.
        for relative in (Path("src/dbs/bootstrap_config.json"), Path("src/gui_config.json")):
            original = source / relative
            if not original.is_file() or original.is_symlink() or original.stat().st_size > 1024**2:
                continue
            try:
                value = json.loads(original.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(value, dict):
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
                target.chmod(0o600)
        (stage / RECEIPT).write_text(json.dumps({"schema": 1, "application": "MCU Flasher",
            "files": receipts}, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        # Rename publishes only into an absent destination; an independently
        # completed installation remains intact if two launchers race.
        try:
            stage.rename(destination)
        except OSError:
            if not is_owned_installation(destination):
                raise
        return destination
    except Exception as error:
        raise RuntimeError(f"Ubuntu application staging failed. Sources and recovery stage were retained at {stage}: {error}") from error


def register_completed_installation(root, *, data_home=None, report=print):
    from direct.ubuntu.install_desktop import desktop_entry, write_if_changed
    root = Path(root).absolute()
    if root != installation_directory(data_home) or not is_owned_installation(root):
        return False
    # Call only after Bootstrap's complete runtime/package validation succeeds.
    data_home = data_directory(data_home)
    artwork = (root / "src/assets/mcu_icon.png").read_bytes()
    write_if_changed(data_home / "icons/hicolor/256x256/apps/mcu-flasher.png", artwork, 0o644)
    write_if_changed(data_home / "applications/mcu-flasher.desktop",
                     desktop_entry(root).encode("utf-8"), 0o755)
    report("MCU Flasher is installed and available in All Apps.")
    return True


def persistent_setup_root(source, *, report=print):
    from src.modules.ubuntu_release import linux_distribution
    if linux_distribution().family != "ubuntu":
        return Path(source)
    return ensure_user_installation(source, report=report)
