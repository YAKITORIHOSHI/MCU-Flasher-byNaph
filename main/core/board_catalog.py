#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCU Flasher by Naph — Modularized Architecture
"""
from __future__ import annotations

import os
import json
import re
from pathlib import Path


from main.core.constants import *
from main.core.theme import *
from main.core.config import *
from main.core.file_utils import *
from main.core.toolchain import *

def _get_download_dir() -> str:
    """Read the download directory from the shared settings file.

    arduino_lib_req.py writes the user's chosen download folder to
    ``arduino_browser_settings.json`` next to this script.  This helper
    reads that file so every call-site in this GUI always uses the
    same, up-to-date path — even if the user changed it while the
    Download Manager was open.
    """
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
        settings["download_dir"] = str(default_dir)
        try:
            temporary = settings_file.with_name(
                settings_file.name + f".tmp-{os.getpid()}"
            )
            temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            os.replace(temporary, settings_file)
        except Exception:
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


def _board_name_tokens(value: object) -> set[str]:
    """Return meaningful lowercase words from a board name or identifier."""
    words = set(re.findall(r"[a-z0-9]+", str(value or "").lower()))
    # Generic words add noise but no identity. Hardware family tokens such as
    # esp32c3/esp32s3 are deliberately retained because they are useful signals.
    return words - {
        "board", "module", "device", "development", "dev", "kit", "version",
        "rev", "revision", "the", "with", "for", "series",
    }


def _parse_downloaded_arduino_board_files(boards_path: Path) -> list[dict]:
    """Parse every downloaded Arduino ``boards.txt`` into neutral board records.

    No PlatformIO board IDs are guessed here.  The Arduino identity (id, name,
    MCU, variant, USB IDs, etc.) is kept intact and is resolved against the
    PlatformIO board catalog in a separate step.
    """
    records: list[dict] = []
    if not boards_path.is_dir():
        return records

    for boards_file in sorted(boards_path.glob("**/boards.txt"), key=lambda x: str(x).lower()):
        props_by_id: dict[str, dict[str, str]] = {}
        try:
            lines = boards_file.read_text(encoding="utf-8", errors="replace").splitlines()
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

            records.append({
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
                "source_core": boards_file.parent.name,
                "properties": props,
            })
    return records


_BOARD_CATALOG_CACHE_VERSION = 1


def _board_catalog_cache_path() -> Path:
    """Return a user-local cache path so startup never modifies the project tree."""
    base = Path(
        os.environ.get("LOCALAPPDATA", "").strip()
        or os.environ.get("APPDATA", "").strip()
        or Path.home() / "AppData" / "Local"
    )
    return base / ".mcuflasher-app" / "board_catalog.json"


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
    try:
        path = _board_catalog_cache_path()
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != _BOARD_CATALOG_CACHE_VERSION:
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
        return boards
    except Exception:
        return None


def _save_board_catalog_cache(boards: dict) -> None:
    """Atomically save the resolved catalog for the next fast launch."""
    temporary = None
    try:
        path = _board_catalog_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        payload = {
            "version": _BOARD_CATALOG_CACHE_VERSION,
            "boards": _json_safe_board_value(boards),
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass


def _load_platformio_board_catalog(core_dir: str | Path | None = None) -> list[dict]:
    """Read PlatformIO's *actual installed* board manifests dynamically.

    PlatformIO officially searches custom/global boards and each installed
    development platform's ``boards/*.json`` directory.  Scanning those same
    manifests gives this GUI the canonical board ID that PlatformIO itself will
    accept, instead of assuming an Arduino boards.txt key is interchangeable.
    """
    root_value = str(core_dir or os.environ.get("PLATFORMIO_CORE_DIR") or "").strip()
    if not root_value:
        try:
            root_value = str(_get_safe_platformio_core_dir(SCRIPT_DIR))
        except Exception:
            root_value = ""
    if not root_value:
        return []
    root = Path(os.path.expandvars(os.path.expanduser(root_value)))
    candidates: list[tuple[Path, str]] = []

    global_boards = root / "boards"
    if global_boards.is_dir():
        candidates.extend((p, "") for p in global_boards.glob("*.json"))

    platforms_root = root / "platforms"
    if platforms_root.is_dir():
        try:
            for platform_dir in platforms_root.iterdir():
                board_dir = platform_dir / "boards"
                if not platform_dir.is_dir() or not board_dir.is_dir():
                    continue
                platform_id = platform_dir.name.split("@", 1)[0]
                candidates.extend((p, platform_id) for p in board_dir.glob("*.json"))
        except OSError:
            pass

    catalog: list[dict] = []
    for manifest_path, platform_hint in candidates:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        build = data.get("build") if isinstance(data.get("build"), dict) else {}
        arduino_build = build.get("arduino") if isinstance(build.get("arduino"), dict) else {}
        upload = data.get("upload") if isinstance(data.get("upload"), dict) else {}
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

        catalog.append({
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
            "has_psram": any("BOARD_HAS_PSRAM" in str(flag) for flag in extra_flags),
            "hwids": hwids,
            "arduino_defines": defines,
            "manifest": str(manifest_path),
        })
    return catalog


def _score_arduino_to_pio_board(record: dict, candidate: dict) -> tuple[float, list[str]]:
    """Score one Arduino board record against one canonical PlatformIO board."""
    frameworks = candidate.get("frameworks") or set()
    if frameworks and "arduino" not in frameworks:
        return -1.0, []

    rec_mcu = str(record.get("mcu") or "").lower()
    pio_mcu = str(candidate.get("mcu") or "").lower()
    if rec_mcu and pio_mcu and _normalize_board_identity(rec_mcu) != _normalize_board_identity(pio_mcu):
        return -1.0, []

    score = 0.0
    reasons: list[str] = []
    rid = _normalize_board_identity(record.get("arduino_id"))
    rname = _normalize_board_identity(record.get("name"))
    rvariant = _normalize_board_identity(record.get("variant"))
    rbuild = _normalize_board_identity(record.get("build_board"))
    cid = _normalize_board_identity(candidate.get("id"))
    cname = _normalize_board_identity(candidate.get("name"))
    cvariant = _normalize_board_identity(candidate.get("variant"))

    if rec_mcu and pio_mcu:
        score += 45
        reasons.append("mcu")
    if rid and cid and rid == cid:
        score += 170
        reasons.append("id")
    if rvariant and cvariant and rvariant == cvariant:
        score += 190
        reasons.append("variant")
    if rname and cname and rname == cname:
        score += 165
        reasons.append("name")
    if record.get("hwids") and candidate.get("hwids") and (record["hwids"] & candidate["hwids"]):
        score += 185
        reasons.append("usb")
    if rbuild and rbuild in (candidate.get("arduino_defines") or set()):
        score += 135
        reasons.append("arduino-define")
    elif rid and rid in (candidate.get("arduino_defines") or set()):
        score += 120
        reasons.append("arduino-define")

    # Token/name similarity is a secondary signal only. Strong identity fields
    # above (variant, USB IDs, Arduino define, exact name/id) dominate it.
    import difflib
    rec_words = _board_name_tokens(f"{record.get('name','')} {record.get('arduino_id','')}")
    pio_words = _board_name_tokens(f"{candidate.get('name','')} {candidate.get('id','')} {candidate.get('vendor','')}")
    if rec_words and pio_words:
        overlap = len(rec_words & pio_words) / max(1, len(rec_words | pio_words))
        score += overlap * 70.0
    similarity = difflib.SequenceMatcher(
        None,
        str(record.get("name") or "").lower(),
        str(candidate.get("name") or "").lower(),
        autojunk=False,
    ).ratio()
    score += similarity * 45.0

    return score, reasons


def _resolve_arduino_board_record(record: dict, catalog: list[dict]) -> dict | None:
    """Resolve an Arduino board to a PlatformIO board, rejecting ambiguous guesses."""
    ranked: list[tuple[float, dict, list[str]]] = []
    for candidate in catalog:
        score, reasons = _score_arduino_to_pio_board(record, candidate)
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
    if not strong and best_score - second_score < 18.0:
        return None
    return {
        **best,
        "match_score": round(best_score, 2),
        "match_reasons": reasons,
    }


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
    return ""


def _fallback_board_id_for_platform(
    platform: str, arduino_id: str, mcu: str = "", display_name: str = ""
) -> str:
    """Derive a sensible default PlatformIO board identifier when no local manifest is installed yet."""
    aid = str(arduino_id or "").strip().lower()
    dname = str(display_name or "").strip().lower()

    if platform == "espressif8266":
        if aid == "generic" or "generic" in dname:
            return "esp01_1m"
        if aid in ("nodemcu", "nodemcuv2", "d1_mini", "d1", "esp12e", "esp01", "esp07", "thing", "huzzah"):
            return aid
        return aid or "esp01_1m"
    if platform == "espressif32":
        if aid in ("esp32", "esp32dev", "nodemcu-32s", "esp32-s2-saola-1", "esp32-s3-devkitc-1", "esp32-c3-devkitm-1"):
            return aid
        return aid or "esp32dev"
    if platform == "atmelavr":
        if aid in ("uno", "nano", "megaatmega2560", "leonardo", "pro16mhzatmega328", "promicro"):
            return aid
        return aid or "uno"
    return aid or "generic"


def load_dynamic_boards(default_boards: dict, *, prefer_cache: bool = False) -> dict:
    """Load downloaded/installed Arduino boards and resolve them to real PlatformIO IDs.

    Arduino ``boards.txt`` identifiers and PlatformIO board IDs are different
    namespaces.  The old loader treated them as interchangeable and could
    therefore generate ``UnknownBoard`` failures.  This loader reads
    PlatformIO's installed board JSON manifests and matches dynamically using
    MCU, variant, board name, Arduino build define and USB VID/PID information.
    """
    if prefer_cache:
        cached = _load_board_catalog_cache()
        if cached is not None:
            return cached
        # Import-time callers must never pay the full fuzzy matching cost. The
        # deferred refresh will populate the real catalog after Tk is visible.
        return default_boards.copy()

    boards = default_boards.copy()
    records: list[dict] = []
    for search_root in _get_arduino_board_search_roots():
        records.extend(_parse_downloaded_arduino_board_files(search_root))
    catalog = _load_platformio_board_catalog()

    resolved_rows: list[tuple[dict, dict | None]] = [
        (record, _resolve_arduino_board_record(record, catalog)) for record in records
    ]

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
        platform = str(
            (match or {}).get("platform")
            or inferred_source_platform.get(record["source_file"], "")
            or _fallback_platform_from_mcu(str(record.get("mcu") or ""))
        ).strip()
        arduino_id = str(record.get("arduino_id") or "")
        raw_id = str((match or {}).get("id") or "").strip()
        pio_id = raw_id or _fallback_board_id_for_platform(
            platform, arduino_id, str(record.get("mcu") or ""), display_name
        )
        pio_resolved = bool(match and platform and raw_id) or bool(platform and pio_id)

        if display_name in used_names:
            existing = boards.get(display_name)
            if (
                isinstance(existing, dict)
                and existing.get("arduino_board_id") == arduino_id
                and str(existing.get("platform", "")).lower() == platform.lower()
            ):
                continue
            display_name = f"{display_name} ({arduino_id})"
            if display_name in used_names:
                continue
        used_names.add(display_name)

        entry: dict = {
            "platform": platform,
            "board": pio_id,
            "framework": "arduino",
            "pio_resolved": pio_resolved,
            "pio_match_score": (match or {}).get("match_score", 0.0),
            "pio_match_reasons": list((match or {}).get("match_reasons") or []),
            "arduino_board_id": arduino_id,
            "arduino_variant": str(record.get("variant") or ""),
            "arduino_build_board": str(record.get("build_board") or ""),
            "mcu": str((match or {}).get("mcu") or record.get("mcu") or "").lower(),
            "pio_name": str((match or {}).get("name") or ""),
            "pio_vendor": str((match or {}).get("vendor") or ""),
            "pio_manifest": str((match or {}).get("manifest") or ""),
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
    _save_board_catalog_cache(boards)
    return boards


SUPPORTED_BOARDS = load_dynamic_boards({}, prefer_cache=True)

def load_downloaded_board_usb_ids(board_catalog: dict | None = None) -> dict[tuple[int, int], tuple[str, ...]]:
    """Map VID/PID pairs to every downloaded/installed board that declares them.

    ESP native-USB VID/PIDs are often shared or can be emitted by firmware, so
    a single ``dict[pair] = board`` silently made the last board in boards.txt
    win.  Preserve ambiguity and auto-select an exact board only when the pair
    uniquely identifies one currently resolved board.
    """
    values: dict[tuple[str, str], dict[str, int]] = {}
    property_re = re.compile(
        r"^([^.=]+)\.(?:upload_port\.)?(vid|pid)\.(\d+)\s*=\s*(0x[0-9a-f]+|\d+)\s*$",
        re.IGNORECASE,
    )
    for search_root in _get_arduino_board_search_roots():
        if not search_root.is_dir():
            continue
        for boards_file in search_root.glob("**/boards.txt"):
            try:
                for raw_line in boards_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    match = property_re.match(raw_line.strip())
                    if not match:
                        continue
                    board_id, field, index, raw_value = match.groups()
                    try:
                        values.setdefault((board_id.lower(), index), {})[field.lower()] = int(raw_value, 0)
                    except ValueError:
                        continue
            except OSError:
                continue

    board_names_by_id: dict[str, str] = {}
    catalog = board_catalog if board_catalog is not None else SUPPORTED_BOARDS
    for display_name, info in catalog.items():
        board_id = str(info.get("arduino_board_id") or info.get("board", "")).strip().lower()
        if board_id:
            board_names_by_id.setdefault(board_id, display_name)

    buckets: dict[tuple[int, int], set[str]] = {}
    for (board_id, _index), usb_id in values.items():
        if "vid" not in usb_id or "pid" not in usb_id:
            continue
        display_name = board_names_by_id.get(board_id)
        if display_name:
            buckets.setdefault((usb_id["vid"], usb_id["pid"]), set()).add(display_name)
    return {
        pair: tuple(sorted(names, key=str.lower))
        for pair, names in buckets.items()
        if names
    }

DOWNLOADED_BOARD_USB_IDS = load_downloaded_board_usb_ids(SUPPORTED_BOARDS)

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
    r"\bWriting\s+at\s+(?P<address>0x[0-9a-f]+)\s*"
    r"\[[^\]\r\n]*\]\s*"
    r"(?P<percent>\d{1,3}(?:\.\d+)?)\s*%"
    r"(?:\s*(?P<written>[\d,]+)\s*/\s*(?P<total>[\d,]+)\s+bytes)?",
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
    return {
        "address": match.group("address").lower(),
        "percent": max(0.0, min(100.0, float(match.group("percent")))),
        "written": int(written.replace(",", "")) if written else None,
        "total": int(total.replace(",", "")) if total else None,
        "version": version,
    }


def _parse_esptool_compressed(line: str) -> dict | None:
    clean = _strip_terminal_escapes(line)
    match = _ESPTOOL_COMPRESSED_RE.search(clean)
    if not match:
        return None
    return {
        "raw": int(match.group("raw").replace(",", "")),
        "compressed": int(match.group("compressed").replace(",", "")),
    }


def _parse_esptool_wrote(line: str) -> dict | None:
    clean = _strip_terminal_escapes(line)
    match = _ESPTOOL_WROTE_RE.search(clean)
    if not match:
        return None
    values = match.groupdict()
    return {
        "raw": int(values["raw"].replace(",", "")),
        "compressed": (
            int(values["compressed"].replace(",", ""))
            if values.get("compressed") else None
        ),
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
    bar = "█" * filled + "░" * (width - filled)
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
