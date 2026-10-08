"""Bounded, worker-only input receipts for offline Arduino CLI libraries."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from src.modules.arduino_cli_support import sha256_file

_CODE_SUFFIXES = {".h", ".hpp", ".hh", ".hxx", ".c", ".cpp", ".cc", ".cxx",
                  ".s", ".a", ".so", ".lib", ".o", ".ld", ".inc", ".ipp", ".tpp"}
_METADATA = {"library.properties", "library.json"}
_IGNORED = {".git", ".pio", "examples", "extras", "docs", "test", "tests"}
_MAX_FILES = 40000
_MAX_BYTES = 256 * 1024 * 1024


def _prepared_core_spelling(config, directories, info, core):
    """Recover a verified Bootstrap alias without creating or selecting one."""
    relative = Path(info["arduino_cli"].get("store") or "arduino-cli")
    if (relative.is_absolute() or not relative.parts or relative.parts[0] != "arduino-cli"
            or ".." in relative.parts):
        raise RuntimeError("The Arduino library store identity is invalid")
    expected = Path(core) / relative
    if (Path(config).resolve() != (expected / "arduino-cli.yaml").resolve()
            or any(Path(directories[name]).resolve() != (expected / name).resolve()
                   for name in ("data", "downloads", "user"))):
        raise RuntimeError("The Arduino library configuration does not select the app-owned package store")
    spelling = Path(directories["data"]).parent
    for _ in relative.parts:
        spelling = spelling.parent
    if spelling.resolve() != Path(core).resolve():
        raise RuntimeError("The Arduino library package store escapes its prepared core")
    return spelling


def library_collections(command, info):
    """Use the validated CLI configuration and the app-owned PlatformIO store."""
    from main.core.board_catalog import _get_download_dir
    from src.modules.package_jobs import package_core_directory
    config = Path(command[command.index("--config-file") + 1])
    if config.stat().st_size > 128 * 1024:
        raise RuntimeError("The Arduino CLI configuration is too large")
    directories = json.loads(config.read_text(encoding="utf-8"))["directories"]
    core = _prepared_core_spelling(config, directories, info, package_core_directory())
    extra = [Path(_get_download_dir()) / "Libs", core / "lib"]
    implicit = [Path(directories["user"]) / "libraries"]
    package, architecture = info["arduino_cli"]["core"].split(":")
    implicit.append(Path(directories["data"]) / "packages" / package / "hardware" /
                    architecture / info["arduino_cli"]["version"] / "libraries")
    seen, collections, flags = set(), [], []
    for index, path in enumerate(extra + implicit):
        resolved = path.resolve()
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        # Include missing roots in receipts: creating a new library collection
        # must invalidate a firmware built before that collection existed.
        collections.append(resolved)
        if index < len(extra) and resolved.is_dir():
            flags += ["--libraries", str(path)]
    return collections, flags


def snapshot(collections, *, byte_hashes=True):
    """Inventory searchable code names; read code bytes only in build workers."""
    files, discovery, total_bytes, count = {}, [], 0, 0
    for root in collections:
        root = Path(root).resolve()
        discovery.append([str(root), root.is_dir()])
        if not root.is_dir():
            continue
        for directory, dirs, names in os.walk(root):
            directory = Path(directory)
            dirs[:] = sorted(name for name in dirs if name.casefold() not in _IGNORED)
            if any((directory / name).resolve() != directory / name for name in dirs):
                raise RuntimeError("Arduino library links require a directly prepared source collection")
            count += len(dirs) + len(names)
            if count > _MAX_FILES:
                raise RuntimeError("Arduino library inputs exceed the bounded verification limit")
            discovery.extend([str(directory.relative_to(root) / name), "directory"] for name in dirs)
            for name in sorted(names):
                path = directory / name
                if path.suffix.lower() not in _CODE_SUFFIXES and name not in _METADATA:
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    raise RuntimeError("An Arduino library input escapes its configured collection")
                size = path.stat().st_size
                total_bytes += size
                if total_bytes > _MAX_BYTES:
                    raise RuntimeError("Arduino library source bytes exceed the bounded verification limit")
                digest = sha256_file(path) if byte_hashes or name in _METADATA else None
                discovery.append([str(path.relative_to(root)), digest if name in _METADATA else "source"])
                if byte_hashes:
                    files[str(resolved)] = digest
    return {"schema": 1, "roots": [str(Path(root).resolve()) for root in collections],
            "discovery": hashlib.sha256(json.dumps(discovery, separators=(",", ":")).encode()).hexdigest(),
            "files": files}


def dependency_tokens(text):
    """Read GCC make dependencies, including escaped spaces and drive paths."""
    text = re.sub(r"\\\r?\n", "", text)
    separator = re.search(r"(?<!\\):(?:\s|$)", text)
    if separator is None:
        return []
    text = text[separator.end():]
    tokens, current, index = [], [], 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and (text[index + 1].isspace() or text[index + 1] in "\\#:"):
            index += 1
            current.append(text[index])
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current.clear()
        else:
            current.append(char)
        index += 1
    if current:
        tokens.append("".join(current))
    return tokens


def selected_inputs(build, workspace, before):
    """Bind reused images to used library trees and actual compiler inputs."""
    build, workspace = Path(build).resolve(), Path(workspace).resolve()
    roots = [Path(root) for root in before["roots"]]
    dependencies, selected, dependency_count, total_bytes, count = set(), set(), 0, 0, 0
    for directory, dirs, names in os.walk(build):
        count += len(dirs) + len(names)
        if count > _MAX_FILES:
            raise RuntimeError("Arduino build inputs exceed the bounded verification limit")
        for name in names:
            if not name.endswith(".d"):
                continue
            dependency_file = Path(directory) / name
            size = dependency_file.stat().st_size
            total_bytes += size
            dependency_count += 1
            if size > 2 * 1024 * 1024 or total_bytes > 16 * 1024 * 1024 or dependency_count > 4096:
                raise RuntimeError("Arduino dependency records exceed the bounded verification limit")
            for token in dependency_tokens(dependency_file.read_text(encoding="utf-8", errors="strict")):
                path = Path(token)
                if not path.is_absolute():
                    path = next((base / path for base in (workspace, build, dependency_file.parent)
                                 if (base / path).is_file()), workspace / path)
                path = path.resolve()
                if not path.is_file() or path.is_relative_to(workspace):
                    continue
                dependencies.add(path)
                for root in roots:
                    if path.is_relative_to(root):
                        relative = path.relative_to(root)
                        if len(relative.parts) > 1:
                            selected.add(root / relative.parts[0])
                        else:
                            selected.add(root)
    # Custom cores without make dependency output still receive safe library
    # receipts; they conservatively bind every supplied library source.
    files = {path: digest for path, digest in before["files"].items()
             if not dependencies or any(Path(path).is_relative_to(root) for root in selected)}
    current_bytes = 0
    for path in sorted(dependencies):
        current_bytes += path.stat().st_size
        if len(files) >= _MAX_FILES or current_bytes > _MAX_BYTES:
            raise RuntimeError("Selected Arduino inputs exceed the bounded verification limit")
        files[str(path)] = sha256_file(path)
    # Library bytes used by the compiler must agree with the pre-build snapshot.
    for path, digest in files.items():
        if path in before["files"] and sha256_file(path) != before["files"][path]:
            raise RuntimeError("Arduino library bytes changed during compilation. Compile again")
    result = {key: value for key, value in before.items() if key != "files"}
    result["files"] = files
    return result


def matches(receipt, current):
    """Re-read selected bytes even when file size and timestamps are unchanged."""
    try:
        if (not isinstance(receipt, dict) or receipt.get("schema") != 1
                or receipt.get("roots") != current["roots"] or receipt.get("discovery") != current["discovery"]):
            return False
        files = receipt.get("files")
        if not isinstance(files, dict) or len(files) > _MAX_FILES:
            return False
        total_bytes = 0
        for path, digest in files.items():
            if not Path(path).is_absolute() or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                return False
            total_bytes += Path(path).stat().st_size
            if total_bytes > _MAX_BYTES or sha256_file(path) != digest:
                return False
        return True
    except (OSError, ValueError, TypeError):
        return False


def previous_dependency_bytes(receipt_path, collections):
    """Snapshot previously selected core/tool inputs before another build."""
    try:
        if Path(receipt_path).stat().st_size > 16 * 1024 * 1024:
            return {}
        receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
        inputs = receipt.get("inputs", {})
        files = inputs.get("files") if isinstance(inputs, dict) else None
        if not isinstance(files, dict) or len(files) > _MAX_FILES:
            return {}
        roots = [Path(root).resolve() for root in collections]
        selected, total_bytes = {}, 0
        for value in files:
            path = Path(value)
            if not path.is_absolute() or not path.is_file() or any(path.is_relative_to(root) for root in roots):
                continue
            total_bytes += path.stat().st_size
            if total_bytes > _MAX_BYTES:
                raise RuntimeError("Selected Arduino inputs exceed the bounded verification limit")
            selected[str(path)] = sha256_file(path)
        return selected
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
