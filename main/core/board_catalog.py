#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import os
import json
import hashlib
import re
import difflib
import threading
from collections import OrderedDict
from copy import deepcopy
import subprocess
from pathlib import Path
from typing import Optional, Any

from main.core.constants import SCRIPT_DIR
from main.core.toolchain import _get_safe_platformio_core_dir

# Process-wide RAM caches for board catalogs and Arduino core parsing
_BOARD_CATALOG_CACHE_RAM: tuple[tuple, tuple, dict] | None = None
_BOARD_CATALOG_CACHE_RAM_PATH: str | None = None
_BOARD_CATALOG_RAM_LOCK = threading.Lock()
_BOARD_CATALOG_PARSE_GENERATION = 0

_PIO_BOARD_CATALOG_RAM_CACHE: dict[str, tuple[tuple, list[dict]]] = {}
_ARDUINO_BOARDS_TXT_RAM_CACHE: OrderedDict[str, tuple[tuple, list[dict], int]] = OrderedDict()
_ARDUINO_BOARDS_TXT_RAM_BYTES = 0
_ARDUINO_BOARDS_TXT_RAM_MAX_FILES = 128
_ARDUINO_BOARDS_TXT_RAM_MAX_BYTES = 16 * 1024 * 1024
_PIO_MANIFEST_RAM_CACHE: OrderedDict[str, tuple[tuple, dict, int]] = OrderedDict()
_PIO_MANIFEST_RAM_BYTES = 0
_PIO_MANIFEST_RAM_MAX_FILES = 8192
_PIO_MANIFEST_RAM_MAX_BYTES = 16 * 1024 * 1024
_PIO_CATALOG_RAM_MAX_ROOTS = 4

def _get_download_dir() -> str:
    """Read the download directory from the shared settings file.

    arduino_lib_req.py writes the user's chosen download folder to
    ``arduino_browser_settings.json`` next to this script.  This helper
    reads that file so every call-site in this GUI always uses the
    same, up-to-date path — even if the user changed it while the
    Download Manager was open.
    """
    settings_file = SCRIPT_DIR / "index_json" / "arduino_browser_settings.json"
    if not settings_file.exists():
        settings_file = SCRIPT_DIR / "src" / "dbs" / "arduino_browser_settings.json"
    if not settings_file.exists() and (SCRIPT_DIR / "arduino_browser_settings.json").exists():
        settings_file = SCRIPT_DIR / "arduino_browser_settings.json"
    default_dir = Path(os.path.expanduser("~")) / "Documents" / "_MCUFlasherByNaph_src"
    settings = {}
    if settings_file.exists():
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception:
            settings = {}

    if isinstance(settings, dict):
        download_dir = str(settings.get("download_dir", "") or "")
        if download_dir:
            current_dir = Path(os.path.expandvars(os.path.expanduser(download_dir)))
            if current_dir.is_dir():
                return str(current_dir)

        # The settings file is copied with the project. Replace a stale
        # absolute path from another account/machine with this user's default.
        if settings.get("download_dir") == str(default_dir):
            return str(default_dir)
        settings["download_dir"] = str(default_dir)
        temporary: Optional[Path] = None
        try:
            temporary = settings_file.with_name(
                settings_file.name + f".tmp-{os.getpid()}"
            )
            temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            os.replace(temporary, settings_file)
        except Exception:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except Exception:
                    pass
    return str(default_dir)


def _get_arduino_board_search_roots() -> list[Path]:
    """Return all directories containing installed or downloaded Arduino cores.

    Includes MCU Flasher's internal Download Manager directory (Boards/),
    as well as standard Arduino IDE / Arduino CLI package repositories
    (%LOCALAPPDATA%/Arduino15/packages, %APPDATA%/Arduino15/packages).
    """
    roots: list[Path] = []
    seen: set[str] = set()

    # 1. Downloaded boards managed by MCU Flasher
    try:
        mcu_boards = Path(_get_download_dir()) / "Boards"
        if mcu_boards.is_dir():
            resolved = mcu_boards.resolve()
            if str(resolved).lower() not in seen:
                seen.add(str(resolved).lower())
                roots.append(mcu_boards)
    except Exception:
        pass

    # 2. System Arduino15 package locations (Arduino IDE 2.x, 1.8.x, Arduino CLI)
    env_paths = [
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("APPDATA", ""),
        os.environ.get("USERPROFILE", ""),
    ]
    for env_base in env_paths:
        if not env_base:
            continue
        try:
            base_dir = Path(os.path.expandvars(os.path.expanduser(env_base)))
            candidate = base_dir / "Arduino15" / "packages"
            if candidate.is_dir():
                resolved = candidate.resolve()
                if str(resolved).lower() not in seen:
                    seen.add(str(resolved).lower())
                    roots.append(candidate)
        except Exception:
            pass

    try:
        home_arduino15 = Path.home() / ".arduino15" / "packages"
        if home_arduino15.is_dir():
            resolved = home_arduino15.resolve()
            if str(resolved).lower() not in seen:
                seen.add(str(resolved).lower())
                roots.append(home_arduino15)
    except Exception:
        pass

    return roots


def _normalize_board_identity(value: object) -> str:
    """Normalize a board/vendor/variant identifier for cross-ecosystem matching."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _normalize_arduino_define(value: object) -> str:
    """boards.txt build.board omits the ARDUINO_ prefix used in PIO flags."""
    normalized = _normalize_board_identity(value)
    return normalized.removeprefix("arduino")


def _board_name_without_vendor(name: object, vendor: object) -> str:
    """Remove only a manifest's declared vendor prefix, not hardware words."""
    name = str(name or "").strip()
    vendor = str(vendor or "").strip()
    if vendor:
        name = re.sub(r"^" + re.escape(vendor) + r"(?:\s+|\s*[-:]\s*)", "", name, flags=re.IGNORECASE)
    return _normalize_board_identity(name)


def _board_name_tokens(value: object) -> set[str]:
    """Return meaningful lowercase words from a board name or identifier."""
    words = set(re.findall(r"[a-z0-9]+", str(value or "").lower()))
    # Generic words add noise but no identity. Hardware family tokens such as
    # esp32c3/esp32s3 are deliberately retained because they are useful signals.
    return words - {
        "board", "module", "device", "development", "dev", "kit", "version",
        "rev", "revision", "the", "with", "for", "series",
    }


def _parse_downloaded_arduino_board_files(boards_path: Path, *, force_read: bool = False) -> list[dict]:
    """Parse every downloaded Arduino ``boards.txt`` into neutral board records.

    No PlatformIO board IDs are guessed here.  The Arduino identity (id, name,
    MCU, variant, USB IDs, etc.) is kept intact and is resolved against the
    PlatformIO board catalog in a separate step.

    Uses _ARDUINO_BOARDS_TXT_RAM_CACHE to avoid re-parsing multi-thousand line
    boards.txt files from disk on repeated queries.
    """
    global _ARDUINO_BOARDS_TXT_RAM_BYTES
    with _BOARD_CATALOG_RAM_LOCK:
        generation = _BOARD_CATALOG_PARSE_GENERATION
    records: list[dict] = []
    if not boards_path.is_dir():
        return records

    for boards_file in sorted(boards_path.glob("**/boards.txt"), key=lambda x: str(x).lower()):
        f_key = str(boards_file)
        try:
            st = boards_file.stat()
        except OSError:
            continue

        with _BOARD_CATALOG_RAM_LOCK:
            cached = _ARDUINO_BOARDS_TXT_RAM_CACHE.get(f_key)
            if (not force_read and cached is not None and cached[0] == _manifest_stat_identity(st)
                    and all(isinstance(row.get("source_sha256"), str)
                            and re.fullmatch(r"[a-f0-9]{64}", row["source_sha256"])
                            for row in cached[1])):
                _ARDUINO_BOARDS_TXT_RAM_CACHE.move_to_end(f_key)
                records.extend(deepcopy(cached[1]))
                continue

        props_by_id: dict[str, dict[str, str]] = {}
        try:
            source_bytes = boards_file.read_bytes()
            source_sha256 = hashlib.sha256(source_bytes).hexdigest()
            lines = source_bytes.decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            left, value = line.split("=", 1)
            if "." not in left:
                continue
            board_id, key = left.split(".", 1)
            board_id = board_id.strip()
            key = key.strip()
            if not board_id or not key or key.startswith("menu.") or ".menu." in key:
                continue
            props_by_id.setdefault(board_id, {})[key] = value.strip()

        file_records: list[dict] = []
        for board_id, props in props_by_id.items():
            name = str(props.get("name") or "").strip()
            if not name:
                continue

            hwids: set[tuple[int, int]] = set()
            usb_parts: dict[str, dict[str, int]] = {}
            for key, value in props.items():
                match = re.match(r"^(?:upload_port\.)?(vid|pid)\.(\d+)$", key, re.IGNORECASE)
                if not match:
                    continue
                field, index = match.groups()
                try:
                    usb_parts.setdefault(index, {})[field.lower()] = int(str(value), 0)
                except ValueError:
                    continue
            for pair in usb_parts.values():
                if "vid" in pair and "pid" in pair:
                    hwids.add((pair["vid"], pair["pid"]))

            file_records.append({
                "arduino_id": board_id,
                "name": name,
                "mcu": str(props.get("build.mcu") or "").strip().lower(),
                "variant": str(props.get("build.variant") or "").strip(),
                "build_board": str(props.get("build.board") or "").strip(),
                "core": str(props.get("build.core") or "").strip().lower(),
                "flash_size": str(
                    props.get("build.flash_size") or props.get("upload.flash_size") or ""
                ).strip(),
                "memory_type": str(props.get("build.memory_type") or "").strip(),
                "flash_mode": str(props.get("build.flash_mode") or "").strip(),
                "has_psram": any(
                    "BOARD_HAS_PSRAM" in str(v) for k, v in props.items()
                    if k == "build.defines" or k.startswith("build.extra_flags")
                ),
                "hwids": hwids,
                "source_file": str(boards_file),
                "source_sha256": source_sha256,
                "source_core": boards_file.parent.name,
                "properties": props,
            })

        with _BOARD_CATALOG_RAM_LOCK:
            if generation == _BOARD_CATALOG_PARSE_GENERATION:
                previous = _ARDUINO_BOARDS_TXT_RAM_CACHE.pop(f_key, None)
                if previous is not None:
                    _ARDUINO_BOARDS_TXT_RAM_BYTES -= previous[2]
                if st.st_size <= _ARDUINO_BOARDS_TXT_RAM_MAX_BYTES:
                    _ARDUINO_BOARDS_TXT_RAM_CACHE[f_key] = (_manifest_stat_identity(st), file_records, st.st_size)
                    _ARDUINO_BOARDS_TXT_RAM_BYTES += st.st_size
                while (_ARDUINO_BOARDS_TXT_RAM_BYTES > _ARDUINO_BOARDS_TXT_RAM_MAX_BYTES
                       or len(_ARDUINO_BOARDS_TXT_RAM_CACHE) > _ARDUINO_BOARDS_TXT_RAM_MAX_FILES):
                    _removed_key, removed = _ARDUINO_BOARDS_TXT_RAM_CACHE.popitem(last=False)
                    _ARDUINO_BOARDS_TXT_RAM_BYTES -= removed[2]
        records.extend(deepcopy(file_records))

    return records


_BOARD_CATALOG_CACHE_VERSION = 5
_CATALOG_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REVIEWED_UNAVAILABLE_PROVIDERS = {
    "ebyte_e77_dev": ("zephyr", "framework-zephyr", "3.40402.0"),
    "sparkfun_micromod_f405": ("zephyr", "framework-zephyr", "3.40402.0"),
    "we_oceanus1": ("zephyr", "framework-zephyr", "3.40402.0"),
    "olimex_f103": ("mbed", "framework-mbed", "6.61700.231105"),
}


def _prepared_catalog_fingerprint(core_dir: str | Path | None = None) -> tuple:
    """Invalidate cached framework availability when bootstrap replaces its catalog."""
    try:
        root = Path(core_dir or _get_safe_platformio_core_dir(SCRIPT_DIR))
        snapshot = root / ".mcu-offline-catalog.json"
        fingerprint = []
        for metadata in (snapshot, root / ".mcu-index-targets.json", root / "platforms/ststm32/platform.json",
                         root / "packages/framework-zephyr/package.json",
                         root / "packages/framework-zephyr@3.40402.0/package.json",
                         root / "packages/framework-mbed/package.json",
                         root / "packages/framework-mbed@6.61700.231105/package.json"):
            try:
                stat = metadata.stat()
                fingerprint.extend((str(metadata), *_manifest_stat_identity(stat)))
            except OSError:
                fingerprint.extend((str(metadata), 0, 0, 0, 0, 0))
        return tuple(fingerprint)
    except (OSError, TypeError, ValueError):
        return ()


def _prepared_framework_availability(record: dict) -> dict | None:
    """Accept explicit exclusions only with a complete, consistent framework list."""
    declared, available = record.get("declared_frameworks"), record.get("frameworks")
    unavailable = record.get("unavailable_frameworks")
    if not isinstance(declared, list) or not isinstance(available, list) or not isinstance(unavailable, dict):
        return None
    if not 0 < len(declared) <= 64 or len(available) > 64 or len(unavailable) > 64:
        return None
    if not all(isinstance(value, str) and _CATALOG_IDENTIFIER.fullmatch(value)
               for value in [*declared, *available, *unavailable]):
        return None
    declared_set = {value.lower() for value in declared}
    available_set = {value.lower() for value in available}
    excluded = {value.lower(): reason for value, reason in unavailable.items()}
    if (len(declared_set) != len(declared) or len(available_set) != len(available)
            or len(excluded) != len(unavailable)
            or not all(isinstance(reason, str) and reason.strip() and len(reason) <= 2048
                       for reason in excluded.values())
            or not set(excluded).issubset(declared_set)
            or available_set != declared_set - set(excluded)):
        return None
    return {"declared_frameworks": sorted(declared_set), "unavailable_frameworks": excluded}


def _prepared_framework_overlay(root: Path) -> dict[tuple[str, str], dict]:
    """Read bounded bootstrap results without changing raw board identities."""
    try:
        with (root / ".mcu-offline-catalog.json").open("rb") as stream:
            payload = stream.read(32 * 1024 * 1024 + 1)
        if len(payload) > 32 * 1024 * 1024:
            return {}
        records = json.loads(payload)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(records, list):
        return {}
    overlay, seen = {}, set()
    for record in records:
        if not isinstance(record, dict):
            continue
        platform, board = record.get("platform"), record.get("id")
        if not all(isinstance(value, str) and _CATALOG_IDENTIFIER.fullmatch(value)
                   for value in (platform, board)):
            continue
        identity = (platform.lower(), board.lower())
        if identity in seen:
            overlay.pop(identity, None)
            continue
        seen.add(identity)
        availability = _prepared_framework_availability(record)
        if availability and availability["unavailable_frameworks"]:
            availability.update({key: record.get(key) for key in (
                "unavailability_manifest_sha256", "unavailability_platform_version", "unavailability_framework_versions",
            )})
            overlay[identity] = availability
    return overlay


def _prepared_overlay_matches(availability: dict, manifest: Path, root: Path) -> bool:
    """Require the exact prepared manifest and installed platform/framework versions."""
    digest = availability.get("unavailability_manifest_sha256")
    version = availability.get("unavailability_platform_version")
    framework_versions = availability.get("unavailability_framework_versions")
    reviewed = _REVIEWED_UNAVAILABLE_PROVIDERS.get(manifest.stem)
    if reviewed is None:
        return False
    framework, provider, provider_version = reviewed
    if (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or version != "20.0.0" or framework_versions != {framework: provider_version}
            or set(availability["unavailable_frameworks"]) != {framework}):
        return False
    try:
        with manifest.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != digest:
            return False
        with (manifest.parent.parent / "platform.json").open("rb") as stream:
            metadata = stream.read(16385)
        if len(metadata) > 16384 or json.loads(metadata).get("version") != version:
            return False
        for folder in (provider, f"{provider}@{provider_version}"):
            package = root / "packages" / folder / "package.json"
            if not package.is_file():
                continue
            with package.open("rb") as stream:
                metadata = stream.read(16385)
            if len(metadata) > 16384:
                return False
            metadata = json.loads(metadata)
            return metadata.get("name") == provider and metadata.get("version") == provider_version
    except (OSError, AttributeError, TypeError, ValueError):
        pass
    return False


def _framework_availability(record: dict) -> dict:
    return {
        "declared_frameworks": sorted(record.get("declared_frameworks") or record.get("frameworks") or []),
        "unavailable_frameworks": dict(record.get("unavailable_frameworks") or {}),
    }


def _board_catalog_cache_path() -> Path:
    from src.modules.platform_runtime import app_cache_dir
    return app_cache_dir() / "board_catalog.json"


def _json_safe_board_value(value):
    if isinstance(value, set):
        return sorted((_json_safe_board_value(item) for item in value), key=str)
    if isinstance(value, tuple):
        return [_json_safe_board_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe_board_value(item) for key, item in value.items()}
    return value


def _load_board_catalog_cache() -> dict | None:
    """Read the last known board catalog without scanning PlatformIO at launch."""
    global _BOARD_CATALOG_CACHE_RAM, _BOARD_CATALOG_CACHE_RAM_PATH
    try:
        path = _board_catalog_cache_path()
        if not path.is_file():
            return None
        st = path.stat()
        prepared = _prepared_catalog_fingerprint()
        with _BOARD_CATALOG_RAM_LOCK:
            if _BOARD_CATALOG_CACHE_RAM is not None:
                cached_identity, cached_prepared, cached_dict = _BOARD_CATALOG_CACHE_RAM
                if (_BOARD_CATALOG_CACHE_RAM_PATH == str(path) and cached_identity == _manifest_stat_identity(st)
                        and cached_prepared == prepared):
                    return deepcopy(cached_dict)

        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != _BOARD_CATALOG_CACHE_VERSION:
            return None
        if payload.get("prepared_catalog") != list(prepared):
            return None
        boards = payload.get("boards")
        if not isinstance(boards, dict):
            return None
        for info in boards.values():
            if not isinstance(info, dict):
                return None
            info["hwids"] = {
                tuple(pair) for pair in info.get("hwids", [])
                if isinstance(pair, (list, tuple)) and len(pair) >= 2
            }
            info["arduino_defines"] = set(info.get("arduino_defines", []))

        if (_manifest_stat_identity(path.stat()) != _manifest_stat_identity(st)
                or _prepared_catalog_fingerprint() != prepared):
            # The bytes were read during a replacement/preparation change.
            # Do not certify them under the older file's metadata identity.
            return None
        with _BOARD_CATALOG_RAM_LOCK:
            _BOARD_CATALOG_CACHE_RAM = (_manifest_stat_identity(st), prepared, boards)
            _BOARD_CATALOG_CACHE_RAM_PATH = str(path)
        return deepcopy(boards)
    except Exception:
        return None


def _save_board_catalog_cache(boards: dict) -> None:
    """Atomically save changed catalogs; avoid flash/HDD writes for no-op refreshes."""
    global _BOARD_CATALOG_CACHE_RAM, _BOARD_CATALOG_CACHE_RAM_PATH
    temporary = None
    try:
        # Keep the serialized payload and RAM publication the same snapshot if
        # a caller changes nested metadata while disk I/O is in progress.
        boards = deepcopy(boards)
        path = _board_catalog_cache_path()
        prepared = _prepared_catalog_fingerprint()
        try:
            st = path.stat()
        except OSError:
            st = None
        with _BOARD_CATALOG_RAM_LOCK:
            cached = _BOARD_CATALOG_CACHE_RAM
            if (st is not None and _BOARD_CATALOG_CACHE_RAM_PATH == str(path) and cached is not None
                    and cached[:2] == (_manifest_stat_identity(st), prepared) and cached[2] == boards):
                return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
        payload = {
            "version": _BOARD_CATALOG_CACHE_VERSION,
            "prepared_catalog": list(prepared),
            "boards": _json_safe_board_value(boards),
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        written_identity = _manifest_stat_identity(temporary.stat())
        os.replace(temporary, path)
        try:
            st = path.stat()
            # Another window/process may have replaced this file after our
            # atomic save. Never label our older payload with that file's ID.
            if st.st_ino and written_identity[3:] == _manifest_stat_identity(st)[3:]:
                with _BOARD_CATALOG_RAM_LOCK:
                    _BOARD_CATALOG_CACHE_RAM = (_manifest_stat_identity(st), prepared, boards)
                    _BOARD_CATALOG_CACHE_RAM_PATH = str(path)
        except OSError:
            pass
    except Exception:
        try:
            if temporary:
                temporary.unlink(missing_ok=True)
        except Exception:
            pass


def _manifest_stat_identity(stat) -> tuple:
    # ctime/file identity also catch replacement with unchanged size/mtime.
    # No freshness timeout: every request inventories the current directories.
    return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, stat.st_dev)


def _invalidate_parsed_board_catalogs() -> None:
    """Forget RAM parsing only; never remove installed packs or disk caches.

    Explicit Refresh must reread current bytes even on filesystems with coarse
    timestamps or after an in-place edit which deliberately restores mtime.
    The generation prevents older in-flight readers from refilling this cache.
    """
    global _BOARD_CATALOG_PARSE_GENERATION
    global _PIO_MANIFEST_RAM_BYTES, _ARDUINO_BOARDS_TXT_RAM_BYTES
    with _BOARD_CATALOG_RAM_LOCK:
        _BOARD_CATALOG_PARSE_GENERATION += 1
        _PIO_BOARD_CATALOG_RAM_CACHE.clear()
        _PIO_MANIFEST_RAM_CACHE.clear()
        _ARDUINO_BOARDS_TXT_RAM_CACHE.clear()
        _PIO_MANIFEST_RAM_BYTES = _ARDUINO_BOARDS_TXT_RAM_BYTES = 0


def _platformio_manifest_inventory(root: Path) -> tuple[tuple, list[tuple[Path, str, tuple]]]:
    """Enumerate each installed board directory once, reusing DirEntry metadata.

    Reuse enumeration metadata when it includes a file identity. Windows
    DirEntry.stat() omits that identity, so use a real stat in that case:
    replacement can preserve size, mtime and even creation time through NTFS
    tunneling. Each directory is still enumerated only once, and unchanged
    manifests need no JSON reads. Recompute the complete fingerprint every time
    so edits/additions/removals never depend on a TTL.
    """
    candidates: list[tuple[Path, str, tuple]] = []
    platform_stats = []
    complete = True

    def directory_stamp(path):
        nonlocal complete
        try:
            return path.stat().st_mtime_ns
        except FileNotFoundError:
            return 0
        except OSError:
            complete = False
            return 0

    def collect(board_dir, platform_hint):
        nonlocal complete
        try:
            with os.scandir(board_dir) as entries:
                for entry in entries:
                    if not entry.name.lower().endswith(".json"):
                        continue
                    try:
                        if entry.is_file():
                            manifest_path = Path(entry.path)
                            metadata = entry.stat()
                            if not metadata.st_ino:
                                metadata = manifest_path.stat()
                            candidates.append((manifest_path, platform_hint, _manifest_stat_identity(metadata)))
                    except OSError:
                        complete = False
        except FileNotFoundError:
            pass
        except OSError:
            complete = False

    global_boards, platforms_root = root / "boards", root / "platforms"
    global_stamp, platforms_stamp = directory_stamp(global_boards), directory_stamp(platforms_root)
    collect(global_boards, "")
    try:
        with os.scandir(platforms_root) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                    platform_dir = Path(entry.path)
                    board_dir, metadata = platform_dir / "boards", platform_dir / "platform.json"
                    try:
                        metadata_stat = metadata.stat()
                        metadata_identity = _manifest_stat_identity(metadata_stat)
                    except FileNotFoundError:
                        metadata_identity = ()
                    platform_stats.append((entry.name, entry.stat().st_mtime_ns,
                                           directory_stamp(board_dir), metadata_identity))
                    collect(board_dir, entry.name.split("@", 1)[0])
                except OSError:
                    complete = False
    except FileNotFoundError:
        pass
    except OSError:
        complete = False
    fingerprint = (global_stamp, platforms_stamp, tuple(sorted(platform_stats)),
                   tuple(sorted((str(path), identity) for path, _hint, identity in candidates)),
                   _prepared_catalog_fingerprint(root)) if complete else ()
    return fingerprint, candidates


def _read_platformio_manifest(path: Path, identity: tuple, *, force_read: bool = False) -> dict | None:
    """Reuse only records validated by this pass; bound retained raw metadata."""
    global _PIO_MANIFEST_RAM_BYTES
    key = str(path)
    with _BOARD_CATALOG_RAM_LOCK:
        generation = _BOARD_CATALOG_PARSE_GENERATION
        cached = _PIO_MANIFEST_RAM_CACHE.get(key)
        if not force_read and cached is not None and cached[0] == identity:
            _PIO_MANIFEST_RAM_CACHE.move_to_end(key)
            return cached[1]
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    size = len(raw.encode("utf-8"))
    with _BOARD_CATALOG_RAM_LOCK:
        if generation == _BOARD_CATALOG_PARSE_GENERATION:
            previous = _PIO_MANIFEST_RAM_CACHE.pop(key, None)
            if previous is not None:
                _PIO_MANIFEST_RAM_BYTES -= previous[2]
            if size <= _PIO_MANIFEST_RAM_MAX_BYTES:
                _PIO_MANIFEST_RAM_CACHE[key] = (identity, data, size)
                _PIO_MANIFEST_RAM_BYTES += size
            while (_PIO_MANIFEST_RAM_BYTES > _PIO_MANIFEST_RAM_MAX_BYTES
                   or len(_PIO_MANIFEST_RAM_CACHE) > _PIO_MANIFEST_RAM_MAX_FILES):
                _removed_key, removed = _PIO_MANIFEST_RAM_CACHE.popitem(last=False)
                _PIO_MANIFEST_RAM_BYTES -= removed[2]
    return data


def _load_platformio_board_catalog(core_dir: str | Path | None = None, *, force_read: bool = False) -> list[dict]:
    """Read PlatformIO's *actual installed* board manifests dynamically.

    PlatformIO officially searches custom/global boards and each installed
    development platform's ``boards/*.json`` directory.  Scanning those same
    manifests gives this GUI the canonical board ID that PlatformIO itself will
    accept, instead of assuming an Arduino boards.txt key is interchangeable.

    Uses _PIO_BOARD_CATALOG_RAM_CACHE in RAM to eliminate repeated disk I/O
    and JSON parsing across hundreds of board files.
    """
    root_value = str(core_dir or "").strip()
    if not root_value:
        try:
            # The build resolves this application's store. Discovery must use
            # the same root rather than an unrelated inherited environment.
            root_value = str(_get_safe_platformio_core_dir(SCRIPT_DIR))
        except Exception:
            root_value = ""
    if not root_value:
        return []
    root = Path(os.path.expandvars(os.path.expanduser(root_value)))

    with _BOARD_CATALOG_RAM_LOCK:
        generation = _BOARD_CATALOG_PARSE_GENERATION

    fp, candidates = _platformio_manifest_inventory(root)

    cache_key = str(root)
    with _BOARD_CATALOG_RAM_LOCK:
        cached = _PIO_BOARD_CATALOG_RAM_CACHE.get(cache_key)
        if not force_read and cached is not None and fp and cached[0] == fp:
            return deepcopy(cached[1])

    prepared_overlay = _prepared_framework_overlay(root)
    catalog: list[dict] = []
    readable = True
    for manifest_path, platform_hint, identity in candidates:
        data = _read_platformio_manifest(manifest_path, identity, force_read=force_read)
        if data is None:
            # A transient read failure is not a stable missing-board catalog.
            # Retry this manifest on the next explicit refresh even when file
            # metadata has not changed; never certify a partial inventory.
            readable = False
            continue
        build_raw = data.get("build")
        build: dict[str, Any] = build_raw if isinstance(build_raw, dict) else {}
        arduino_build_raw = build.get("arduino")
        arduino_build: dict[str, Any] = arduino_build_raw if isinstance(arduino_build_raw, dict) else {}
        upload_raw = data.get("upload")
        upload: dict[str, Any] = upload_raw if isinstance(upload_raw, dict) else {}
        frameworks = data.get("frameworks") or []
        if isinstance(frameworks, str):
            frameworks = [frameworks]
        platform_value = data.get("platform") or data.get("platforms") or platform_hint
        if isinstance(platform_value, list):
            platform_value = platform_value[0] if platform_value else platform_hint

        hwids: set[tuple[int, int]] = set()
        for pair in build.get("hwids") or []:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            try:
                hwids.add((int(str(pair[0]), 0), int(str(pair[1]), 0)))
            except ValueError:
                pass

        extra_flags = build.get("extra_flags") or []
        if isinstance(extra_flags, str):
            extra_flags = [extra_flags]
        defines = {
            _normalize_board_identity(match.group(1))
            for flag in extra_flags
            for match in [re.search(r"-D\s*([A-Za-z0-9_]+)", str(flag))]
            if match
        }
        for flag in arduino_build.get("extra_flags", []) if isinstance(arduino_build.get("extra_flags"), list) else []:
            match = re.search(r"-D\s*([A-Za-z0-9_]+)", str(flag))
            if match:
                defines.add(_normalize_board_identity(match.group(1)))

        record = {
            "id": manifest_path.stem,
            "name": str(data.get("name") or manifest_path.stem),
            "vendor": str(data.get("vendor") or ""),
            "platform": str(platform_value or "").strip(),
            "frameworks": {str(f).lower() for f in frameworks},
            "mcu": str(build.get("mcu") or "").strip().lower(),
            "variant": str(build.get("variant") or arduino_build.get("variant") or "").strip(),
            "memory_type": str(arduino_build.get("memory_type") or build.get("memory_type") or "").strip(),
            "flash_mode": str(build.get("flash_mode") or "").strip(),
            "flash_size": str(upload.get("flash_size") or "").strip(),
            "upload_protocol": str(upload.get("protocol") or "").strip(),
            "upload_speed": upload.get("speed"),
            "require_upload_port": upload.get("require_upload_port"),
            "has_psram": any("BOARD_HAS_PSRAM" in str(flag) for flag in extra_flags),
            "hwids": hwids,
            "arduino_defines": defines,
            "manifest": str(manifest_path),
        }
        availability = prepared_overlay.get((record["platform"].lower(), record["id"].lower()))
        # A changed upstream declaration must be prepared again, not inherit an
        # exclusion from a different manifest or a guessed board substitute.
        if (availability and record["platform"] == "ststm32"
                and set(availability["declared_frameworks"]) == record["frameworks"]
                and _prepared_overlay_matches(availability, manifest_path, root)):
            record.update(_framework_availability(availability))
            record["frameworks"] -= set(availability["unavailable_frameworks"])
        catalog.append(record)

    with _BOARD_CATALOG_RAM_LOCK:
        if generation != _BOARD_CATALOG_PARSE_GENERATION:
            return catalog
        if fp and readable:
            _PIO_BOARD_CATALOG_RAM_CACHE.pop(cache_key, None)
            _PIO_BOARD_CATALOG_RAM_CACHE[cache_key] = (fp, deepcopy(catalog))
            while len(_PIO_BOARD_CATALOG_RAM_CACHE) > _PIO_CATALOG_RAM_MAX_ROOTS:
                del _PIO_BOARD_CATALOG_RAM_CACHE[next(iter(_PIO_BOARD_CATALOG_RAM_CACHE))]
        elif not readable:
            _PIO_BOARD_CATALOG_RAM_CACHE.pop(cache_key, None)
    return catalog



def _arduino_match_features(record: dict, *, candidate: bool = False) -> dict:
    """Normalize matching evidence once for this discovery pass, never persist it."""
    name = str(record.get("name") or "")
    identifier = record.get("id" if candidate else "arduino_id")
    mcu = str(record.get("mcu") or "").lower()
    features = {
        "mcu": mcu, "normalized_mcu": _normalize_board_identity(mcu),
        "id": _normalize_board_identity(identifier),
        "name": _normalize_board_identity(name),
        "name_lower": name.lower(),
        "variant": _normalize_board_identity(record.get("variant")),
    }
    if candidate:
        features.update({
            "name_without_vendor": _board_name_without_vendor(name, record.get("vendor")),
            "defines": {_normalize_arduino_define(value) for value in record.get("arduino_defines") or ()},
            "words": _board_name_tokens(f"{record.get('name','')} {record.get('id','')} {record.get('vendor','')}"),
            # Reuse the unchanged candidate sequence's character index. The
            # matcher is owned by this one pass, not shared across workers.
            "similarity": difflib.SequenceMatcher(None, "", name.lower(), autojunk=False),
        })
    else:
        features.update({
            "build": _normalize_board_identity(record.get("build_board")),
            "build_define": _normalize_arduino_define(record.get("build_board")),
            "words": _board_name_tokens(f"{record.get('name','')} {record.get('arduino_id','')}"),
        })
    return features


def _score_arduino_to_pio_board(record: dict, candidate: dict, *,
                              record_features: dict | None = None,
                              candidate_features: dict | None = None) -> tuple[float, list[str]]:
    """Score one Arduino board record against one canonical PlatformIO board."""
    frameworks = candidate.get("frameworks") or set()
    if frameworks and "arduino" not in frameworks:
        return -1.0, []

    rec = record_features or _arduino_match_features(record)
    pio = candidate_features or _arduino_match_features(candidate, candidate=True)
    rec_mcu, pio_mcu = rec["mcu"], pio["mcu"]
    if rec_mcu and pio_mcu and rec["normalized_mcu"] != pio["normalized_mcu"]:
        return -1.0, []

    score = 0.0
    reasons: list[str] = []
    rid, rname, rvariant, rbuild = rec["id"], rec["name"], rec["variant"], rec["build"]
    cid, cname, cname_without_vendor, cvariant = pio["id"], pio["name"], pio["name_without_vendor"], pio["variant"]

    if rec_mcu and pio_mcu:
        score += 45
        reasons.append("mcu")
    if rid and cid and rid == cid:
        score += 170
        reasons.append("id")
    if rvariant and cvariant and rvariant == cvariant:
        score += 190
        reasons.append("variant")
    if rname and (rname == cname or rname == cname_without_vendor):
        score += 165
        reasons.append("name")
    if record.get("hwids") and candidate.get("hwids") and (record["hwids"] & candidate["hwids"]):
        score += 185
        reasons.append("usb")
    defines = pio["defines"]
    if rbuild and rec["build_define"] in defines:
        score += 135
        reasons.append("arduino-define")
    elif rid and rid in defines:
        score += 120
        reasons.append("arduino-define")

    # Token/name similarity is a secondary signal only. Strong identity fields
    # above (variant, USB IDs, Arduino define, exact name/id) dominate it.
    rec_words, pio_words = rec["words"], pio["words"]
    if rec_words and pio_words:
        overlap = len(rec_words & pio_words) / max(1, len(rec_words | pio_words))
        score += overlap * 70.0
    matcher = pio["similarity"]
    matcher.set_seq1(rec["name_lower"])
    similarity = matcher.ratio()
    score += similarity * 45.0

    return score, reasons


def _resolve_arduino_board_record(record: dict, catalog: list[dict], *,
                                  match_features: dict[int, dict] | None = None) -> dict | None:
    """Resolve an Arduino board to a PlatformIO board, rejecting ambiguous guesses."""
    ranked: list[tuple[float, dict, list[str]]] = []
    record_features = _arduino_match_features(record)
    for candidate in catalog:
        features = match_features.get(id(candidate)) if match_features is not None else None
        score, reasons = _score_arduino_to_pio_board(
            record, candidate, record_features=record_features, candidate_features=features,
        )
        if score >= 0:
            ranked.append((score, candidate, reasons))
    if not ranked:
        return None
    ranked.sort(key=lambda row: (-row[0], str(row[1].get("platform", "")), str(row[1].get("id", ""))))
    best_score, best, reasons = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else -999.0
    strong = any(x in reasons for x in ("id", "variant", "name", "usb", "arduino-define"))
    if best_score < (120.0 if strong else 105.0):
        return None
    if best_score - second_score < 18.0:
        return None
    return {
        **best,
        "match_score": round(best_score, 2),
        "match_reasons": reasons,
    }


def resolve_board_definition(display_name: str, info: dict, catalog: list[dict], *,
                             match_features: dict[int, dict] | None = None,
                             prepared_targets=None, prepared_validation=None) -> dict:
    """Repair one cached row using canonical definitions, without family guesses.

    This also works before downloaded-core discovery completes: the cached
    Arduino identity contains the evidence needed to match installed manifests.
    """
    resolved = dict(info)
    platform = "" if info.get("backend") == "arduino-cli" else str(info.get("platform") or "").lower()
    board_id = str(info.get("board") or "").lower()
    exact = [row for row in catalog if platform and board_id and
             str(row.get("platform") or "").lower() == platform and
             str(row.get("id") or "").lower() == board_id]
    match = exact[0] if len(exact) == 1 else None
    if match is None and info.get("arduino_board_id"):
        record = {
            "name": display_name, "arduino_id": info.get("arduino_board_id"),
            "mcu": info.get("mcu"), "variant": info.get("arduino_variant"),
            "build_board": info.get("arduino_build_board"), "hwids": info.get("hwids") or set(),
        }
        candidates = [row for row in catalog if not platform or str(row.get("platform") or "").lower() == platform]
        match = _resolve_arduino_board_record(record, candidates, match_features=match_features)
    from src.modules.arduino_cli_support import prepared_target_for_record, arduino_catalog_entry
    record = {"name": display_name, "arduino_id": info.get("arduino_board_id"),
              "source_file": info.get("arduino_source_file"), "mcu": info.get("mcu"),
              "source_sha256": info.get("arduino_source_sha256"),
              "variant": info.get("arduino_variant"), "build_board": info.get("arduino_build_board"),
              "source_core": info.get("source_core")}
    prepared = prepared_target_for_record(record, catalog, rows=prepared_targets, validation_cache=prepared_validation)
    if prepared and prepared.get("backend") != "arduino-cli":
        match = prepared
    if not match:
        if prepared and prepared.get("backend") == "arduino-cli":
            return arduino_catalog_entry(record, prepared)
        if resolved.get("backend") == "arduino-cli":
            for key in ("backend", "arduino_cli", "arduino_fqbn", "platformio_support", "platformio_support_proof", "fallback_notice"):
                resolved.pop(key, None)
        return resolved
    for key in ("arduino_cli", "arduino_fqbn", "platformio_support", "platformio_support_proof", "fallback_notice"):
        resolved.pop(key, None)
    resolved["backend"] = "platformio"
    frameworks = sorted(match.get("frameworks") or [])
    resolved.update({
        "platform": str(match.get("platform") or ""), "board": str(match.get("id") or ""),
        "pio_resolved": True, "pio_manifest": str(match.get("manifest") or ""),
        "pio_name": str(match.get("name") or ""), "pio_vendor": str(match.get("vendor") or ""),
        "pio_match_score": match.get("match_score", 1000.0),
        "pio_match_reasons": list(match.get("match_reasons") or ["platformio-native-manifest"]),
        "frameworks": frameworks, **_framework_availability(match),
        "upload_protocol": str(match.get("upload_protocol") or ""),
        "upload_speed": match.get("upload_speed"),
        "require_upload_port": match.get("require_upload_port"),
        "mcu": str(match.get("mcu") or info.get("mcu") or "").lower(),
        "has_psram": bool(match.get("has_psram")),
        "memory_type": match.get("memory_type") or None, "flash_mode": match.get("flash_mode") or None,
    })
    if resolved.get("framework") not in frameworks:
        resolved["framework"] = "arduino" if "arduino" in frameworks else (frameworks[0] if len(frameworks) == 1 else "")
    flash = re.match(r"(\d+(?:\.\d+)?)\s*MB", str(match.get("flash_size") or ""), re.IGNORECASE)
    if flash:
        resolved["flash_mb"] = float(flash.group(1))
    return resolved


def _fallback_platform_from_mcu(mcu: str) -> str:
    """Last-resort architecture fallback when PlatformIO catalog discovery is unavailable."""
    value = _normalize_board_identity(mcu)
    if value.startswith("esp32"):
        return "espressif32"
    if value.startswith("esp8266"):
        return "espressif8266"
    if value.startswith("atmega") or value.startswith("attiny"):
        return "atmelavr"
    if value.startswith("stm32"):
        return "ststm32"
    if value.startswith("rp2040") or value.startswith("rp2350"):
        return "raspberrypi"
    if value.startswith("samd") or value.startswith("sam"):
        return "atmelsam"
    if value.startswith("nrf"):
        return "nordicnrf52"
    if value.startswith("teensy") or "imxrt" in value:
        return "teensy"
    if value.startswith("ch32v") or value.startswith("ch5"):
        return "ch32v"
    return ""


def _fallback_board_id_for_platform(
    platform: str, arduino_id: str, mcu: str = "", display_name: str = ""
) -> str:
    """Derive a sensible default PlatformIO board identifier when no local manifest is installed yet."""
    aid = str(arduino_id or "").strip().lower()
    dname = str(display_name or "").strip().lower()
    mcu_norm = _normalize_board_identity(mcu)

    if platform == "espressif8266":
        if aid == "generic" or "generic" in dname:
            return "esp01_1m"
        if aid in ("nodemcu", "nodemcuv2", "d1_mini", "d1", "esp12e", "esp01", "esp07", "thing", "huzzah"):
            return aid
        return aid or "esp01_1m"
    if platform == "espressif32":
        if aid in ("esp32", "esp32dev", "nodemcu-32s", "esp32-s2-saola-1", "esp32-s3-devkitc-1", "esp32-c3-devkitm-1"):
            return aid
        if "s3" in mcu_norm or "s3" in aid or "s3" in dname:
            return "esp32-s3-devkitc-1"
        if "s2" in mcu_norm or "s2" in aid or "s2" in dname:
            return "esp32-s2-saola-1"
        if "c3" in mcu_norm or "c3" in aid or "c3" in dname:
            return "esp32-c3-devkitm-1"
        if "c6" in mcu_norm or "c6" in aid or "c6" in dname:
            return "esp32-c6-devkitc-1"
        return aid or "esp32dev"
    if platform == "atmelavr":
        if aid in ("uno", "nano", "megaatmega2560", "leonardo", "pro16mhzatmega328", "promicro"):
            return aid
        if "328" in mcu_norm or "uno" in dname:
            return "uno"
        if "2560" in mcu_norm or "mega" in dname:
            return "megaatmega2560"
        if "32u4" in mcu_norm or "leonardo" in dname:
            return "leonardo"
        return aid or "uno"
    if platform == "ststm32":
        if "f103c8" in aid or "f103c8" in mcu_norm or "bluepill" in dname:
            return "bluepill_f103c8"
        if "f411" in aid or "f411" in mcu_norm or "blackpill" in dname:
            return "blackpill_f411ce"
        if "f401" in aid or "f401" in mcu_norm:
            return "nucleo_f401re"
        return aid or "genericSTM32F103C8"
    if platform == "raspberrypi":
        if "pico2" in aid or "rp2350" in mcu_norm:
            return "pico2"
        return "pico"
    if platform == "atmelsam":
        if "mkr" in aid or "mkr" in dname:
            return "mkrzero"
        if "samd21" in mcu_norm or "zero" in aid or "zero" in dname:
            return "zero"
        return aid or "zero"
    if platform == "nordicnrf52":
        return aid or "nrf52840_dk_adafruit"
    if platform == "teensy":
        if "41" in aid or "41" in dname or "41" in mcu_norm:
            return "teensy41"
        if "40" in aid or "40" in dname or "40" in mcu_norm:
            return "teensy40"
        return aid or "teensy31"
    return aid or "generic"


def load_registry_board_catalog() -> list[dict]:
    """Read the canonical catalog saved by bootstrap; never contact a registry."""
    from main.core.target_profile import target_problem
    root = Path(_get_safe_platformio_core_dir(SCRIPT_DIR))
    snapshot = root / ".mcu-offline-catalog.json"
    if not snapshot.is_file():
        return _load_platformio_board_catalog()
    records = json.loads(snapshot.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("PlatformIO returned an invalid board catalog.")
    catalog = []
    for record in records:
        if not isinstance(record, dict):
            continue
        identity = {"platform": record.get("platform"), "board": record.get("id"), "framework": "arduino"}
        if target_problem(identity):
            continue
        frameworks = record.get("frameworks") or []
        if not isinstance(frameworks, (list, tuple)):
            continue
        availability = _prepared_framework_availability(record)
        if availability and availability["unavailable_frameworks"]:
            proof = {**record, **availability}
            manifest = root / "platforms" / str(record["platform"]) / "boards" / f"{record['id']}.json"
            if not _prepared_overlay_matches(proof, manifest, root):
                frameworks = availability["declared_frameworks"]
                availability["unavailable_frameworks"] = {}
        try:
            flash_size = f"{float(record.get('rom') or 0) / (1024 * 1024):g}MB"
        except (TypeError, ValueError, OverflowError):
            flash_size = "unknown"
        catalog.append({
            **record, "frameworks": {str(f).lower() for f in frameworks},
            **(availability or {
                "declared_frameworks": sorted({str(f).lower() for f in frameworks}), "unavailable_frameworks": {},
            }),
            "flash_size": flash_size,
            "hwids": set(), "arduino_defines": set(), "manifest": "",
        })
    return catalog


def _native_board_entry(pio_board: dict) -> dict:
    """One exact installed target's picker metadata, shared by preview/final."""
    frameworks = set(pio_board.get("frameworks") or [])
    framework = "arduino" if "arduino" in frameworks else (next(iter(frameworks)) if len(frameworks) == 1 else "")
    board_id = str(pio_board.get("id") or "").strip()
    entry = {
        "platform": str(pio_board.get("platform") or "").strip(),
        "board": board_id,
        "framework": framework,
        "frameworks": sorted(frameworks),
        **_framework_availability(pio_board),
        "pio_resolved": True,
        "pio_match_score": 100.0,
        "pio_match_reasons": ["platformio-native-manifest"],
        "arduino_board_id": board_id,
        "arduino_variant": "",
        "arduino_build_board": "",
        "mcu": str(pio_board.get("mcu") or "").lower(),
        "pio_name": str(pio_board.get("name") or board_id).strip(),
        "pio_vendor": str(pio_board.get("vendor") or ""),
        "pio_manifest": str(pio_board.get("manifest") or ""),
        "upload_protocol": str(pio_board.get("upload_protocol") or ""),
        "upload_speed": pio_board.get("upload_speed"),
        "require_upload_port": pio_board.get("require_upload_port"),
        "flash_mb": None,
        "has_psram": bool(pio_board.get("has_psram")),
        "memory_type": str(pio_board.get("memory_type") or "") or None,
        "flash_mode": str(pio_board.get("flash_mode") or "") or None,
        "source_core": "platformio-installed",
    }
    flash = re.match(r"(\d+(?:\.\d+)?)\s*MB", str(pio_board.get("flash_size") or ""), re.IGNORECASE)
    if flash:
        entry["flash_mb"] = float(flash.group(1))
    return entry


class _BoardBatchDelivery:
    """Bound each delivery, detach metadata and let closed consumers cancel."""

    def __init__(self, callback):
        self.callback = callback
        self.pending = {}
        self.first = True

    def add(self, name, info):
        if self.callback is None:
            return
        self.pending[name] = info
        if len(self.pending) >= (16 if self.first else 128):
            self.flush()

    def flush(self):
        if not self.pending or self.callback is None:
            return
        batch = deepcopy(self.pending)
        self.pending.clear()
        self.first = False
        if self.callback(batch) is False:
            raise InterruptedError("Board discovery cancelled.")

    def checkpoint(self):
        # An empty page is a cancellation probe, not a visual update.
        if self.callback is not None and self.callback({}) is False:
            raise InterruptedError("Board discovery cancelled.")


def load_dynamic_boards(default_boards: dict, *, prefer_cache: bool = False, registry_catalog=None,
                        on_batch=None, invalidate_parsed: bool = False) -> dict:
    """Load downloaded/installed Arduino boards and resolve them to real PlatformIO IDs.

    Arduino ``boards.txt`` identifiers and PlatformIO board IDs are different
    namespaces.  The old loader treated them as interchangeable and could
    therefore generate ``UnknownBoard`` failures.  This loader reads
    PlatformIO's installed board JSON manifests and matches dynamically using
    MCU, variant, board name, Arduino build define and USB VID/PID information.
    ``on_batch`` receives detached preview metadata in pages of at most 128
    rows (16 first). Only the returned full catalog is authoritative: Arduino
    aliases can supersede native display names during final reconciliation.
    Empty pages are cancellation probes; return False to stop this refresh.
    Explicit user Refresh sets ``invalidate_parsed`` to bypass all parsed RAM
    records, including same-size/timestamp-preserved in-place edits. Ordinary
    startup and resolver discovery keep metadata-validated warm reuse.
    """
    if invalidate_parsed:
        _invalidate_parsed_board_catalogs()
    if prefer_cache and not invalidate_parsed:
        cached = _load_board_catalog_cache()
        if cached is not None:
            return cached
        # Import-time callers must never pay the full fuzzy matching cost. The
        # deferred refresh will populate the real catalog after Tk is visible.
        return default_boards.copy()

    boards = {name: dict(info) for name, info in default_boards.items() if isinstance(info, dict)}
    delivery = _BoardBatchDelivery(on_batch)
    catalog = _load_platformio_board_catalog(force_read=invalidate_parsed)
    if registry_catalog:
        installed = {(b["platform"], b["id"]) for b in catalog}
        catalog.extend(b for b in registry_catalog if (b["platform"], b["id"]) not in installed)
    if on_batch is not None:
        # Show exact native definitions before the expensive all-pairs Arduino
        # identity pass. These previews never mutate the published build catalog.
        preview_names = set(boards)
        preview_identities = {(str(info.get("platform") or "").lower(), str(info.get("board") or "").lower())
                              for info in boards.values()}
        for name, info in boards.items():
            delivery.add(name, info)
        for candidate in catalog:
            board_id = str(candidate.get("id") or "").strip()
            platform = str(candidate.get("platform") or "").strip()
            identity = (platform.lower(), board_id.lower())
            if not board_id or not platform or identity in preview_identities:
                continue
            name = str(candidate.get("name") or board_id).strip()
            if name in preview_names:
                name = f"{name} ({board_id})"
                if name in preview_names:
                    name = f"{candidate.get('name') or board_id} [{platform}:{board_id}]"
            preview_names.add(name)
            preview_identities.add(identity)
            delivery.add(name, _native_board_entry(candidate))
        delivery.flush()
    records: list[dict] = []
    for search_root in _get_arduino_board_search_roots():
        records.extend(_parse_downloaded_arduino_board_files(search_root, force_read=invalidate_parsed))
    match_features = {id(candidate): _arduino_match_features(candidate, candidate=True)
                      for candidate in catalog}
    from src.modules.arduino_cli_support import load_prepared_targets, prepared_target_for_record, arduino_catalog_entry, prepared_target_index
    prepared_targets = prepared_target_index(load_prepared_targets())
    prepared_validation = {}
    # Empty board IDs in an older Arduino row cannot be refreshed by the
    # native (platform, board ID) merge below. Resolve their retained identity.
    boards = {name: resolve_board_definition(name, info, catalog, match_features=match_features,
                                           prepared_targets=prepared_targets, prepared_validation=prepared_validation)
              if info.get("pio_resolved") is False or not info.get("board") else info
              for name, info in boards.items()}

    resolved_rows: list[tuple[dict, dict | None]] = []
    fallback_rows = {}
    for position, record in enumerate(records):
        if position % 32 == 0:
            delivery.checkpoint()
        match = _resolve_arduino_board_record(record, catalog, match_features=match_features)
        prepared = prepared_target_for_record(record, catalog, rows=prepared_targets, validation_cache=prepared_validation) if prepared_targets else None
        if prepared and prepared.get("backend") != "arduino-cli":
            match = prepared
        elif prepared and not match:
            fallback_rows[id(record)] = prepared
        resolved_rows.append((record, match))

    # Infer the PlatformIO platform for an entire downloaded Arduino core from
    # the boards that matched confidently.  This lets an unsupported/new board
    # retain the correct family without a folder-name -> platform hardcode.
    source_platform_counts: dict[str, dict[str, int]] = {}
    for record, match in resolved_rows:
        if not match or not match.get("platform"):
            continue
        bucket = source_platform_counts.setdefault(record["source_file"], {})
        platform = str(match["platform"])
        bucket[platform] = bucket.get(platform, 0) + 1
    inferred_source_platform: dict[str, str] = {}
    for source_file, counts in source_platform_counts.items():
        if counts:
            inferred_source_platform[source_file] = max(
                counts.items(), key=lambda item: (item[1], item[0])
            )[0]

    # If no boards in a source_file matched an installed PlatformIO manifest
    # (e.g. platform is not yet installed in PlatformIO's core store), infer
    # the platform from the MCU declared in the Arduino core.
    for record in records:
        src = record.get("source_file", "")
        if src and src not in inferred_source_platform:
            fb = _fallback_platform_from_mcu(str(record.get("mcu") or ""))
            if fb:
                inferred_source_platform[src] = fb

    used_names: set[str] = set(boards)
    for record, match in resolved_rows:
        display_name = str(record.get("name") or record.get("arduino_id") or "Unknown board")
        if id(record) in fallback_rows:
            entry = arduino_catalog_entry(record, fallback_rows[id(record)])
            existing = boards.get(display_name)
            if existing and (existing.get("arduino_board_id") != entry["arduino_board_id"]
                             or existing.get("arduino_source_file") not in (None, entry["arduino_source_file"])):
                display_name = f"{display_name} ({entry['arduino_board_id']})"
            boards[display_name] = entry
            used_names.add(display_name)
            delivery.add(display_name, entry)
            continue
        platform = str(
            (match or {}).get("platform")
            or inferred_source_platform.get(record["source_file"], "")
            or _fallback_platform_from_mcu(str(record.get("mcu") or ""))
        ).strip()
        arduino_id = str(record.get("arduino_id") or "")
        raw_id = str((match or {}).get("id") or "").strip()
        # Arduino and PlatformIO IDs are different namespaces. A plausible
        # family/id is not sufficient evidence to flash a board safely.
        pio_id = raw_id
        pio_resolved = bool(match and platform and raw_id)

        if display_name in used_names:
            existing = boards.get(display_name)
            if (
                isinstance(existing, dict)
                and existing.get("arduino_board_id") == arduino_id
                and str(existing.get("platform", "")).lower() == platform.lower()
            ):
                if not match and existing.get("pio_resolved"):
                    continue  # Preserve a verified registry row during offline discovery.
            else:
                display_name = f"{display_name} ({arduino_id})"
                if display_name in used_names:
                    continue
        used_names.add(display_name)

        entry: dict = {
            "platform": platform,
            "board": pio_id,
            "framework": "arduino",
            "frameworks": sorted((match or {}).get("frameworks") or ["arduino"]),
            **_framework_availability(match or {}),
            "pio_resolved": pio_resolved,
            "pio_match_score": (match or {}).get("match_score", 0.0),
            "pio_match_reasons": list((match or {}).get("match_reasons") or []),
            "arduino_board_id": arduino_id,
            "arduino_source_file": str(record.get("source_file") or ""),
            "arduino_source_sha256": str(record.get("source_sha256") or ""),
            "arduino_variant": str(record.get("variant") or ""),
            "arduino_build_board": str(record.get("build_board") or ""),
            "mcu": str((match or {}).get("mcu") or record.get("mcu") or "").lower(),
            "pio_name": str((match or {}).get("name") or ""),
            "pio_vendor": str((match or {}).get("vendor") or ""),
            "pio_manifest": str((match or {}).get("manifest") or ""),
            "upload_protocol": str((match or {}).get("upload_protocol") or ""),
            "upload_speed": (match or {}).get("upload_speed"),
            "require_upload_port": (match or {}).get("require_upload_port"),
            "flash_mb": None,
            # Compile-time options come from the resolved PlatformIO manifest
            # first, because PlatformIO (not the downloaded Arduino core copy)
            # is the compiler actually consuming them.
            "has_psram": bool((match or {}).get("has_psram") or record.get("has_psram")),
            "memory_type": str((match or {}).get("memory_type") or record.get("memory_type") or "") or None,
            "flash_mode": str((match or {}).get("flash_mode") or record.get("flash_mode") or "") or None,
            "source_core": str(record.get("source_core") or ""),
        }
        flash_raw = str((match or {}).get("flash_size") or record.get("flash_size") or "")
        m = re.match(r"(\d+(?:\.\d+)?)\s*MB", flash_raw, re.IGNORECASE)
        if m:
            entry["flash_mb"] = float(m.group(1))
        boards[display_name] = entry
        delivery.add(display_name, entry)

    # Index aliases once: refreshing thousands of manifests must stay linear.
    identities: dict[tuple[str, str], list[dict]] = {}
    for prior in boards.values():
        identity = (str(prior.get("platform", "")).lower(), str(prior.get("board", "")).lower())
        identities.setdefault(identity, []).append(prior)

    # ── Register native PlatformIO board manifests from installed platforms ───
    for pio_board in catalog:
        b_id = str(pio_board.get("id") or "").strip()
        b_platform = str(pio_board.get("platform") or "").strip()
        b_name = str(pio_board.get("name") or b_id).strip()
        if not b_id or not b_platform:
            continue

        # Refresh cached identities from the current host's canonical manifest.
        # A copied Windows cache must not mask freshly installed Linux metadata.
        identity = (b_platform.lower(), b_id.lower())
        for prior in identities.get(identity, ()):
            frameworks = sorted(pio_board.get("frameworks") or [])
            prior.update({
                "pio_resolved": True, "pio_manifest": str(pio_board.get("manifest") or ""),
                "frameworks": frameworks, **_framework_availability(pio_board),
                "upload_protocol": str(pio_board.get("upload_protocol") or ""),
                "upload_speed": pio_board.get("upload_speed"),
                "require_upload_port": pio_board.get("require_upload_port"),
                "has_psram": bool(pio_board.get("has_psram")),
                "memory_type": pio_board.get("memory_type") or None,
                "flash_mode": pio_board.get("flash_mode") or None,
            })
            if prior.get("framework") not in frameworks:
                prior["framework"] = "arduino" if "arduino" in frameworks else (frameworks[0] if len(frameworks) == 1 else "")

        # Skip duplicate display rows for identities already registered.
        if identity in identities:
            continue

        disp_name = b_name
        if disp_name in used_names:
            disp_name = f"{b_name} ({b_id})"
            if disp_name in used_names:
                disp_name = f"{b_name} [{b_platform}:{b_id}]"
        used_names.add(disp_name)

        entry = _native_board_entry(pio_board)
        boards[disp_name] = entry
        identities.setdefault(identity, []).append(entry)
        delivery.add(disp_name, entry)

    delivery.flush()
    _save_board_catalog_cache(boards)
    return boards


class BoardCatalog(dict):
    """Keep imported catalog references stable and iteration safe during refresh."""

    def __init__(self, initial):
        self._lock = threading.RLock()
        self._revision = 0
        super().__init__(initial)

    def replace(self, catalog):
        with self._lock:
            if isinstance(catalog, dict) and super().__eq__(catalog):
                return False
            super().clear()
            super().update(catalog)
            self._revision += 1
            return True

    def snapshot(self):
        """Return one coherent revision for background search indexing."""
        with self._lock:
            return self._revision, dict(super().items())

    def set_definition(self, name, info):
        """Publish a resolved row without losing concurrent catalog additions."""
        with self._lock:
            if super().get(name) == info:
                return False
            super().__setitem__(name, dict(info))
            self._revision += 1
            return True

    def items(self):
        with self._lock:
            return tuple(super().items())

    def keys(self):
        with self._lock:
            return tuple(super().keys())

    def values(self):
        with self._lock:
            return tuple(super().values())

    def __iter__(self):
        return iter(self.keys())

    def get(self, key, default=None):
        with self._lock:
            return super().get(key, default)

    def __getitem__(self, key):
        with self._lock:
            return super().__getitem__(key)

    def __contains__(self, key):
        with self._lock:
            return super().__contains__(key)


SUPPORTED_BOARDS = BoardCatalog(load_dynamic_boards({}, prefer_cache=True))

def load_downloaded_board_usb_ids(board_catalog: dict | None = None) -> dict[tuple[int, int], tuple[str, ...]]:
    """Map VID/PID pairs to every downloaded/installed board that declares them.

    ESP native-USB VID/PIDs are often shared or can be emitted by firmware, so
    a single ``dict[pair] = board`` silently made the last board in boards.txt
    win.  Preserve ambiguity and auto-select an exact board only when the pair
    uniquely identifies one currently resolved board.
    """
    # Reuse the same current-file-validated parser as discovery instead of
    # rereading large boards.txt files immediately after catalog refresh.
    records = []
    for search_root in _get_arduino_board_search_roots():
        records.extend(_parse_downloaded_arduino_board_files(search_root))

    board_names_by_id: dict[str, set[str]] = {}
    catalog = board_catalog if board_catalog is not None else SUPPORTED_BOARDS
    for display_name, info in catalog.items():
        board_id = str(info.get("arduino_board_id") or info.get("board", "")).strip().lower()
        if board_id:
            board_names_by_id.setdefault(board_id, set()).add(display_name)

    buckets: dict[tuple[int, int], set[str]] = {}
    for record in records:
        names = board_names_by_id.get(str(record.get("arduino_id") or "").lower(), ())
        for pair in record.get("hwids", ()):
            if names:
                buckets.setdefault(pair, set()).update(names)
    return {
        pair: tuple(sorted(names, key=str.lower))
        for pair, names in buckets.items()
        if names
    }

# Importing the GUI must never traverse package trees. The background catalog
# refresh populates this shared mapping once discovery has completed.
DOWNLOADED_BOARD_USB_IDS: dict[tuple[int, int], tuple[str, ...]] = {}

# Generic WCH bridge IDs commonly used by Arduino UNO/Nano clones. Explicit
# ESP32/ESP8266/NodeMCU descriptor text is checked first and remains authoritative.
KNOWN_UNO_CLONE_USB_IDS = {
    (0x1A86, 0x7523),
    (0x1A86, 0x5523),
}

# ─── Canonical chip-feature descriptions ─────────────────────────
# PlatformIO bundles its own esptool build per platform version, and older
# bundled copies print a much shorter "Features:" line (e.g. "WiFi, BLE,
# Embedded PSRAM 8MB (AP_3v3)") than a current standalone esptool CLI does
# ("Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz, Embedded PSRAM 8MB
# (AP_3v3)"). Rather than depend on whichever wording that bundled version
# happens to use, fill in the well-known hardware description for the
# detected chip family ourselves, and keep only the live-detected memory
# info (PSRAM/flash) from the tool's own output since that part is
# genuinely board-specific.
_CHIP_FEATURE_TEMPLATES = {
    "ESP32-S3": "Wi-Fi, BT 5 (LE), Dual Core + LP Core, 240MHz",
    "ESP32-C6": "Wi-Fi 6, BT 5 (LE), 802.15.4, Dual Core (RISC-V), 160MHz",
    "ESP32-C3": "Wi-Fi, BT 5 (LE), Single Core (RISC-V), 160MHz",
    "ESP32-H2": "BT 5 (LE), 802.15.4, Single Core (RISC-V), 96MHz",
    "ESP32-S2": "Wi-Fi, Single Core, 240MHz",
    "ESP32":    "Wi-Fi, BT/BLE (Classic + LE), Dual Core, 240MHz",
}

def _enrich_chip_features(chip_model: str, raw_features: str) -> str:
    """Swap a terse esptool 'Features:' line for the fuller, canonical
    description of the detected chip family, preserving any live-detected
    PSRAM/flash mention from the tool's own output. Falls back to the raw
    string unchanged if the chip family isn't recognized."""
    if not raw_features or not chip_model:
        return raw_features
    upper_model = chip_model.upper()
    # Check longer/more-specific names first — "ESP32" is a substring of
    # "ESP32-S3", so a naive lookup would misidentify every S-series/C-series
    # chip as plain "ESP32".
    family = next(
        (name for name in sorted(_CHIP_FEATURE_TEMPLATES, key=len, reverse=True)
         if name in upper_model),
        None,
    )
    if not family:
        return raw_features
    template = _CHIP_FEATURE_TEMPLATES[family]
    mem_match = re.search(r'(embedded\s+psram.*)$', raw_features, re.IGNORECASE)
    return f"{template}, {mem_match.group(1).strip()}" if mem_match else template

_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_ESPTOOL_V5_WRITE_PROGRESS_RE = re.compile(
    r"\bWriting\s+at\s+(?P<address>0x[0-9a-f]+)\b"
    r"[^\d\r\n]*"
    r"(?P<percent>\d{1,3}(?:\.\d+)?)\s*%"
    r"(?:[^\d\r\n/]+(?P<written>[\d,]+(?:\.\d+)?\s*[kmg]?b?)\s*/\s*(?P<total>[\d,]+(?:\.\d+)?\s*[kmg]?b?))?",
    re.IGNORECASE,
)
_ESPTOOL_V4_WRITE_PROGRESS_RE = re.compile(
    r"\bWriting\s+at\s+(?P<address>0x[0-9a-f]+)\.{3}\s*"
    r"\(\s*(?P<percent>\d{1,3}(?:\.\d+)?)\s*%\s*\)",
    re.IGNORECASE,
)
_ESPTOOL_IMAGE_START_RE = re.compile(
    r"^\s*Writing\s+(?P<source>.+)\s+at\s+"
    r"(?P<address>0x[0-9a-f]+)\.{3}\s*$",
    re.IGNORECASE,
)
_ESPTOOL_COMPRESSED_RE = re.compile(
    r"\bCompressed\s+(?P<raw>[\d,]+)\s+bytes\s+to\s+"
    r"(?P<compressed>[\d,]+)",
    re.IGNORECASE,
)
_ESPTOOL_WROTE_RE = re.compile(
    r"\bWrote\s+(?P<raw>[\d,]+)\s+bytes"
    r"(?:\s+\((?P<compressed>[\d,]+)\s+compressed\))?"
    r"\s+at\s+(?P<address>0x[0-9a-f]+)"
    r"\s+in\s+(?P<seconds>[\d.]+)\s+seconds"
    r"(?:\s+\((?:effective\s+)?(?P<rate>[\d.]+)\s+kbit/s\))?",
    re.IGNORECASE,
)


def _strip_terminal_escapes(text: str) -> str:
    """Remove ANSI styling and carriage returns before parsing tool output."""
    return _ANSI_ESCAPE_RE.sub("", str(text or "")).replace("\r", "").strip()

def _validate_img(path, label, min_size, magic=None) -> bool:
    """Compatibility fallback for legacy Hard Reset call sites.

    Older copies of the Hard Reset routine called ``_validate_img`` after the
    validator was renamed or accidentally scoped inside another branch.  Keep
    this module-level implementation so that even a remaining legacy call can
    never terminate the burn with ``NameError``.
    """
    try:
        data = Path(path).read_bytes()
    except Exception:
        return False
    if len(data) < int(min_size):
        return False
    if magic is not None and (not data or data[0] != int(magic)):
        return False
    return True


def _parse_esptool_image_start(line: str) -> dict | None:
    """Return the image path/address from a v5 ``Writing 'file' at`` row."""
    clean = _strip_terminal_escapes(line)
    match = _ESPTOOL_IMAGE_START_RE.search(clean)
    if not match:
        return None
    source = match.group("source").strip()
    if len(source) >= 2 and source[0] in ("'", '"') and source[-1] == source[0]:
        source = source[1:-1]
    return {"source": source, "address": match.group("address").lower()}


def _parse_byte_size(val: str | None) -> int | None:
    """Parse byte count safely from raw integers or unit strings like '11.91kB', '369.26kB', '137B'."""
    if not val:
        return None
    val = str(val).strip().lower().replace(",", "")
    if val.endswith("kb"):
        try:
            return int(float(val[:-2]) * 1024)
        except Exception:
            return None
    elif val.endswith("mb"):
        try:
            return int(float(val[:-2]) * 1024 * 1024)
        except Exception:
            return None
    elif val.endswith("b"):
        try:
            return int(float(val[:-1]))
        except Exception:
            return None
    try:
        return int(float(val))
    except Exception:
        return None


def _parse_esptool_write_progress(line: str) -> dict | None:
    """Parse one esptool 4.x/5.x flash-progress row.

    Byte counters are exact in esptool 5.x.  Older 4.x rows expose only a
    percentage, so ``written`` and ``total`` deliberately remain ``None``.
    """
    clean = _strip_terminal_escapes(line)
    match = _ESPTOOL_V5_WRITE_PROGRESS_RE.search(clean)
    version = 5
    if not match:
        match = _ESPTOOL_V4_WRITE_PROGRESS_RE.search(clean)
        version = 4
    if not match:
        return None
    written = match.groupdict().get("written")
    total = match.groupdict().get("total")
    if written and total and not re.search(r"[a-zA-Z]", written):
        unit_match = re.search(r"([a-zA-Z]+)$", total.strip())
        if unit_match:
            written = f"{written.strip()}{unit_match.group(1)}"
    return {
        "address": match.group("address").lower(),
        "percent": max(0.0, min(100.0, float(match.group("percent")))),
        "written": _parse_byte_size(written),
        "total": _parse_byte_size(total),
        "version": version,
    }


def _parse_esptool_compressed(line: str) -> dict | None:
    clean = _strip_terminal_escapes(line)
    match = _ESPTOOL_COMPRESSED_RE.search(clean)
    if not match:
        return None
    return {
        "raw": _parse_byte_size(match.group("raw")),
        "compressed": _parse_byte_size(match.group("compressed")),
    }


def _parse_esptool_wrote(line: str) -> dict | None:
    clean = _strip_terminal_escapes(line)
    match = _ESPTOOL_WROTE_RE.search(clean)
    if not match:
        return None
    values = match.groupdict()
    return {
        "raw": _parse_byte_size(values.get("raw")),
        "compressed": _parse_byte_size(values.get("compressed")),
        "address": values["address"].lower(),
        "seconds": float(values["seconds"]),
        "rate": float(values["rate"]) if values.get("rate") else None,
    }


def _format_upload_progress_row(label: str, stage: int, stage_total: int,
                                percent: float, written: int | None = None,
                                total: int | None = None, bar_width: int = 30) -> str:
    """Build the app's compact flash-progress row (no timestamp)."""
    pct = max(0.0, min(100.0, float(percent)))
    width = max(8, int(bar_width))
    filled = max(0, min(width, int(round(width * pct / 100.0))))
    bar = "▰" * filled + "▱" * (width - filled)
    complete = pct >= 99.95
    status = "✔ Flashed" if complete else "⚡ Flashing"
    row = f"  {status} [{stage}/{stage_total}] {label} [ {bar} ] | {pct:.1f}%"
    if written is not None and total is not None:
        row += f" | {int(written):,}/{int(total):,} bytes"
    return row

USB_CHIP_BOARD_FAMILIES = {
    # keyword found in port description → (set of platforms it's valid for, human label)
    "ch340":        ({"atmelavr", "espressif8266"}, "CH340 (Arduino/ESP8266-style USB-serial)"),
    "ch341":        ({"atmelavr", "espressif8266"}, "CH341 (Arduino/ESP8266-style USB-serial)"),
    "cp210":        ({"espressif32", "espressif8266"}, "CP210x (Espressif USB-serial)"),
    "silicon labs":({"espressif32", "espressif8266"}, "Silicon Labs CP210x (Espressif USB-serial)"),
    "ch9102":       ({"espressif32"}, "CH9102 (ESP32-S2/S3/C3 USB-serial)"),
    "ftdi":         ({"atmelavr", "espressif8266", "espressif32"}, "FTDI (generic USB-serial)"),
    "wch.cn":       ({"atmelavr", "espressif8266", "espressif32"}, "WCH USB-serial (generic)"),
    "esp32-s3":     ({"espressif32"}, "ESP32-S3 Native USB"),
    "esp32s3":      ({"espressif32"}, "ESP32-S3 Native USB"),
    "jtag":         ({"espressif32"}, "USB JTAG/serial debug unit"),
    "usb bridge":   ({"espressif32"}, "ESP32 USB Bridge"),
    "usb serial":   ({"espressif32", "espressif8266", "atmelavr"}, "USB Serial (generic/CDC)"),
    "usb-to-serial":({"espressif32", "espressif8266", "atmelavr"}, "USB-to-Serial (generic)"),
    "usb to serial":({"espressif32", "espressif8266", "atmelavr"}, "USB to Serial (generic)"),
    "esp32":        ({"espressif32"}, "ESP32 Device"),
    "esp8266":      ({"espressif8266"}, "ESP8266 Device"),
}


_PIO_EXECUTABLE_CACHE: list[str] | None = None


__all__ = [
    "DOWNLOADED_BOARD_USB_IDS",
    "KNOWN_UNO_CLONE_USB_IDS",
    "SUPPORTED_BOARDS",
    "USB_CHIP_BOARD_FAMILIES",
    "_ANSI_ESCAPE_RE",
    "_BOARD_CATALOG_CACHE_VERSION",
    "_CHIP_FEATURE_TEMPLATES",
    "_ESPTOOL_COMPRESSED_RE",
    "_ESPTOOL_IMAGE_START_RE",
    "_ESPTOOL_V4_WRITE_PROGRESS_RE",
    "_ESPTOOL_V5_WRITE_PROGRESS_RE",
    "_ESPTOOL_WROTE_RE",
    "_board_catalog_cache_path",
    "_board_name_tokens",
    "_enrich_chip_features",
    "_fallback_board_id_for_platform",
    "_fallback_platform_from_mcu",
    "_format_upload_progress_row",
    "_get_arduino_board_search_roots",
    "_get_download_dir",
    "_json_safe_board_value",
    "_load_board_catalog_cache",
    "_load_platformio_board_catalog",
    "_normalize_board_identity",
    "_parse_downloaded_arduino_board_files",
    "_parse_esptool_compressed",
    "_parse_esptool_image_start",
    "_parse_esptool_write_progress",
    "_parse_esptool_wrote",
    "_resolve_arduino_board_record",
    "_save_board_catalog_cache",
    "_score_arduino_to_pio_board",
    "_strip_terminal_escapes",
    "_validate_img",
    "load_downloaded_board_usb_ids",
    "load_dynamic_boards"
]
