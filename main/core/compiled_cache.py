"""Bounded, worker-only receipts for an exact board's successful native build."""
from __future__ import annotations

import configparser
import hashlib
import json
import os
import platform
import re
import stat
import sys
from pathlib import Path

_RECEIPT = "build-receipt.json"
_INPUT_STATE = "build-inputs.json"
_MAX_CONFIG = 128 * 1024
_MAX_RECEIPT = 1024 * 1024
_MAX_FILES = 128
_MAX_ENTRIES = 4096
_MAX_BYTES = 256 * 1024 * 1024
_CHUNK = 1024 * 1024
_OUTPUT_SUFFIXES = {".bin", ".hex", ".uf2", ".elf"}
_TRANSPORT_OPTIONS = {"monitor_speed", "upload_speed", "upload_port"}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_DEVICE_NAME = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)


def host_namespace() -> str:
    """Keep cached binaries separate for the native operating system and CPU."""
    host = "windows" if sys.platform == "win32" else "ubuntu" if sys.platform.startswith("linux") else sys.platform
    machine = platform.machine().strip().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    host = re.sub(r"[^a-z0-9_]+", "-", host.lower()).strip("-") or "unknown"
    machine = re.sub(r"[^a-z0-9_]+", "-", machine).strip("-") or "unknown"
    return f"{host}-{machine}"


def _linked(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _workspace(workspace: Path) -> Path:
    root = Path(os.path.abspath(workspace))
    for path in reversed((root, *root.parents)):
        info = path.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("The compiled board workspace contains a linked or invalid directory")
    return root


def _file_info(path: Path):
    info = path.lstat()
    if _linked(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"The compiled cache file is linked or invalid: {path.name}")
    return info


def _same_file(first, second) -> bool:
    # Windows Python can expose creation time through lstat's deprecated ctime
    # but change time through fstat. Compare native file identity and write time;
    # actual bytes remain authoritative even when write times are coarse.
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if os.name != "nt":
        fields += ("st_ctime_ns",)
    return all(getattr(first, field) == getattr(second, field) for field in fields)


def _open_checked(path: Path):
    info = _file_info(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    if not _same_file(info, os.fstat(stream.fileno())) or not _same_file(info, _file_info(path)):
        stream.close()
        raise ValueError(f"The compiled cache file changed while opening: {path.name}")
    return stream, info


def _read_bounded(path: Path, limit: int) -> bytes:
    stream, before = _open_checked(path)
    with stream:
        if before.st_size > limit:
            raise ValueError(f"The compiled cache file exceeds its read limit: {path.name}")
        content = stream.read(limit + 1)
        if len(content) > limit or not _same_file(before, os.fstat(stream.fileno())):
            raise ValueError(f"The compiled cache file changed or exceeds its read limit: {path.name}")
    if not _same_file(before, _file_info(path)):
        raise ValueError(f"The compiled cache file changed while reading: {path.name}")
    return content


def _config_hash(root: Path) -> str:
    text = _read_bounded(root / "platformio.ini", _MAX_CONFIG).decode("utf-8-sig")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    defaults = {key: value for key, value in parser.defaults().items() if key not in _TRANSPORT_OPTIONS}
    sections = {
        section: {key: value for key, value in parser.items(section, raw=True) if key not in _TRANSPORT_OPTIONS}
        for section in parser.sections()
    }
    normalized = json.dumps({"defaults": defaults, "sections": sections}, sort_keys=True,
                            ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def input_signature(workspace: Path) -> dict:
    """Bind configuration and configured library trees before and after a build.

    PlatformIO's SCons database is not a safe dependency interchange format.
    Conservatively hash each configured collection, including custom recipe
    scripts/assets, with the existing 40,000-entry / 256 MiB worker bounds.
    """
    root = _workspace(workspace)
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(_read_bounded(root / "platformio.ini", _MAX_CONFIG).decode("utf-8-sig"))
    roots = []
    for section in parser.sections():
        values = re.split(r"[,\r\n]", parser.get(section, "lib_extra_dirs", fallback=""))
        for dependency in parser.get(section, "lib_deps", fallback="").splitlines():
            dependency = dependency.strip()
            if not dependency:
                continue
            if not dependency.startswith("symlink://"):
                raise ValueError("Saved-build reuse requires a directly prepared library source")
            values.append(dependency[len("symlink://"):])
        for value in values:
            value = value.strip()
            if not value:
                continue
            path = Path(value)
            roots.append((path if path.is_absolute() else root / path).resolve())
    collections = []
    for path in sorted(set(roots), key=lambda item: (len(item.parts), str(item))):
        if not any(path.is_relative_to(parent) for parent in collections):
            collections.append(path)
    from main.core.arduino_inputs import snapshot
    libraries = snapshot(collections, all_files=True)
    encoded = json.dumps(libraries, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"config_sha256": _config_hash(root), "libraries_sha256": hashlib.sha256(encoded).hexdigest()}


def read_input_signature(workspace: Path) -> dict | None:
    """Remember object inputs independently of a successful firmware receipt."""
    try:
        root = _workspace(workspace)
        value = json.loads(_read_bounded(root / _INPUT_STATE, _MAX_CONFIG))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def write_input_signature(workspace: Path, inputs: dict) -> None:
    root = _workspace(workspace)
    try:
        _file_info(root / _INPUT_STATE)
    except FileNotFoundError:
        pass
    from main.core.file_utils import write_generated_text
    write_generated_text(root / _INPUT_STATE, json.dumps(inputs, sort_keys=True) + "\n")


def file_digest(path: Path, *, byte_limit: int = _MAX_BYTES) -> str:
    """Hash input bytes with bounded memory, reads and checked file identity."""
    path = Path(path)
    limit = min(byte_limit, _MAX_BYTES)
    stream, before = _open_checked(path)
    with stream:
        if before.st_size > limit:
            raise ValueError("The build input exceeds the verification byte limit")
        digest, size = hashlib.sha256(), 0
        while chunk := stream.read(_CHUNK):
            size += len(chunk)
            if size > limit:
                raise ValueError("The build input exceeds the verification byte limit")
            digest.update(chunk)
        if size != before.st_size or not _same_file(before, os.fstat(stream.fileno())):
            raise ValueError("The build input changed while reading")
    if not _same_file(before, _file_info(path)):
        raise ValueError("The build input changed while reading")
    return digest.hexdigest()


def _safe_name(name) -> bool:
    return (isinstance(name, str) and 0 < len(name) <= 240 and name not in (".", "..")
            and not name.endswith((".", " ")) and not _UNSAFE_NAME.search(name)
            and not _DEVICE_NAME.match(name) and Path(name).suffix.lower() in _OUTPUT_SUFFIXES)


def _output_hashes(root: Path) -> dict:
    directory = root
    for name in (".pio", "build", "mcu_env"):
        directory /= name
        info = directory.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("The compiled output directory is linked or invalid")
    names = []
    with os.scandir(directory) as entries:
        for count, entry in enumerate(entries, 1):
            if count > _MAX_ENTRIES:
                raise ValueError("The compiled output directory exceeds its entry limit")
            if Path(entry.name).suffix.lower() not in _OUTPUT_SUFFIXES:
                continue
            if not _safe_name(entry.name):
                raise ValueError("The compiled output filename is unsafe")
            names.append(entry.name)
            if len(names) > _MAX_FILES:
                raise ValueError("The compiled outputs exceed the file limit")
    if not any(name.lower() in {"firmware" + suffix for suffix in _OUTPUT_SUFFIXES} for name in names):
        raise ValueError("No compiled firmware image is available")
    files, total = {}, 0
    for name in sorted(names):
        path = directory / name
        stream, before = _open_checked(path)
        with stream:
            if before.st_size <= 0 or total + before.st_size > _MAX_BYTES:
                raise ValueError("The compiled outputs are empty or exceed the byte limit")
            digest, size = hashlib.sha256(), 0
            while chunk := stream.read(_CHUNK):
                size += len(chunk)
                if total + size > _MAX_BYTES:
                    raise ValueError("The compiled outputs exceed the byte limit")
                digest.update(chunk)
            if size != before.st_size or not _same_file(before, os.fstat(stream.fileno())):
                raise ValueError(f"The compiled output changed while reading: {name}")
        if not _same_file(before, _file_info(path)):
            raise ValueError(f"The compiled output changed while reading: {name}")
        total += size
        files[name] = digest.hexdigest()
    _workspace(root)
    for path in (directory, directory.parent, directory.parent.parent):
        if _linked(path.lstat()):
            raise ValueError("The compiled output directory became linked")
    return files


def _identity(value, label: str, limit: int):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"The compiled receipt {label} is invalid")
    return value


def write_receipt(workspace: Path, *, board_key: str, board_name: str, source_hash: str,
                  metadata: dict | None = None, build_inputs: dict | None = None) -> dict:
    """Publish only after a successful build; failures leave no usable new receipt."""
    root = _workspace(workspace)
    inputs = input_signature(root)
    if build_inputs is not None and build_inputs != inputs:
        raise ValueError("Build configuration or library inputs changed during compilation")
    receipt = {"schema": 1, "host": host_namespace(),
               "board_key": _identity(board_key, "board key", 512),
               "board": _identity(board_name, "board name", 1024),
               "source_hash": _identity(source_hash, "source hash", 4096),
               **inputs, "files": _output_hashes(root),
               "metadata": {} if metadata is None else metadata}
    if not isinstance(receipt["metadata"], dict):
        raise ValueError("The compiled receipt metadata is invalid")
    text = json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    if len(text.encode("utf-8")) > _MAX_RECEIPT:
        raise ValueError("The compiled receipt exceeds its size limit")
    _workspace(root)
    try:
        _file_info(root / _RECEIPT)
    except FileNotFoundError:
        pass
    from main.core.file_utils import write_generated_text
    write_generated_text(root / _RECEIPT, text)
    return receipt


def read_receipt(workspace: Path) -> dict | None:
    """Read a bounded receipt without migrating or changing its workspace."""
    try:
        root = _workspace(workspace)
        receipt = json.loads(_read_bounded(root / _RECEIPT, _MAX_RECEIPT))
        return receipt if isinstance(receipt, dict) else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def validate_receipt(workspace: Path, *, board_key: str, source_hash: str) -> tuple[bool, str]:
    """Recheck actual bytes before reuse, including on coarse-timestamp storage."""
    try:
        root = _workspace(workspace)
        receipt = read_receipt(root)
        if receipt is None:
            return False, "No valid successful-build receipt is available"
        if type(receipt.get("schema")) is not int or receipt["schema"] != 1:
            return False, "The compiled receipt version is unsupported"
        for key, expected, reason in (
                ("host", host_namespace(), "The compiled build belongs to another host"),
                ("board_key", board_key, "The compiled build belongs to another board"),
                ("source_hash", source_hash, "The sketch sources changed since compilation")):
            if not isinstance(expected, str) or not expected or receipt.get(key) != expected:
                return False, reason
        inputs = input_signature(root)
        if receipt.get("config_sha256") != inputs["config_sha256"]:
            return False, "The board build configuration changed since compilation"
        if receipt.get("libraries_sha256") != inputs["libraries_sha256"]:
            return False, "The configured library files changed since compilation"
        files = receipt.get("files")
        if not isinstance(files, dict) or not 0 < len(files) <= _MAX_FILES:
            return False, "The compiled receipt has no valid output inventory"
        if any(not _safe_name(name) or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
               for name, digest in files.items()):
            return False, "The compiled receipt contains an invalid output entry"
        if files != _output_hashes(root):
            return False, "The compiled firmware outputs changed since compilation"
        return True, "Verified compiled firmware is available"
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        return False, str(exc) or "The compiled build could not be verified"


def invalidate_receipt(workspace: Path) -> None:
    """Retire only this workspace's receipt, retaining its incremental objects."""
    try:
        root = _workspace(workspace)
    except FileNotFoundError:
        return
    receipt = root / _RECEIPT
    try:
        _file_info(receipt)
    except FileNotFoundError:
        return
    from main.core.file_utils import ensure_file_writable
    ensure_file_writable(receipt)
    _workspace(root)
    _file_info(receipt)
    receipt.unlink()
