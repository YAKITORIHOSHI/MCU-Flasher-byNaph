"""Trusted Arduino CLI choices for UNO Q and Pico 2 only.

Only the local downloader settings enable the alternate compiler. Package index
metadata and previously prepared certificates describe identity, never consent.
The host-selected package stores remain independent of these portable choices.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

FIELD = "board_arduino_cli_selections"
_ALLOWED_BOARDS = {("arduino", "zephyr"): frozenset({"unoq"}),
                   ("rp2040", "rp2040"): frozenset({"rpipico2"})}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_LIMIT = 2 * 1024 * 1024
_LOCK = threading.Lock()
_CACHE = None


def settings_file():
    """Match the downloader's settings owner without migration or writes."""
    from main.core.constants import SCRIPT_DIR
    root = Path(SCRIPT_DIR)
    index = root / "index_json/arduino_browser_settings.json"
    database = root / "src/dbs/arduino_browser_settings.json"
    return (index if index.exists() or index.parent.is_dir() else
            database if database.exists() else root / "arduino_browser_settings.json")


def association_key(metadata):
    if not isinstance(metadata, dict):
        return ""
    package, architecture = (metadata.get(key) for key in ("package", "architecture"))
    url = metadata.get("index_url", "")
    if (not isinstance(package, str) or not _IDENTIFIER.fullmatch(package)
            or not isinstance(architecture, str) or not _IDENTIFIER.fullmatch(architecture)
            or not isinstance(url, str) or len(url) > 4096):
        return ""
    if url:
        try:
            parsed = urlsplit(url)
            if (parsed.scheme not in ("http", "https") or not parsed.netloc
                    or parsed.username or parsed.password or parsed.fragment or url != url.strip()):
                return ""
        except ValueError:
            return ""
    return json.dumps([url, package, architecture], ensure_ascii=False, separators=(",", ":"))


def allowed_board_ids(metadata):
    """Use exact core/board IDs, never display names or broad MCU families."""
    if not association_key(metadata):
        return frozenset()
    return _ALLOWED_BOARDS.get((metadata["package"], metadata["architecture"]), frozenset())


def board_allowed(metadata, board_id):
    return isinstance(board_id, str) and board_id in allowed_board_ids(metadata)


def disabled_reason(metadata, board_id):
    if not board_allowed(metadata, board_id):
        return ("Arduino CLI is available only for Arduino UNO Q and Raspberry Pi Pico 2 / RP2350. "
                "Use an exact PlatformIO definition for this board.")
    return ("Arduino CLI is disabled for this board. Use Choose Arduino CLI boards in "
            "Libraries & boards, then prepare board support.")


def _normalized(values):
    if not isinstance(values, dict) or len(values) > 4096:
        return {}
    result, count = {}, 0
    for key, identifiers in values.items():
        if not isinstance(key, str) or len(key) > 4608 or not isinstance(identifiers, (list, tuple, frozenset)):
            continue
        try:
            parts = json.loads(key)
            if not isinstance(parts, list) or len(parts) != 3:
                continue
            metadata = dict(zip(("index_url", "package", "architecture"), parts))
            if association_key(metadata) != key or len(identifiers) > 50000:
                continue
            selected = frozenset(value for value in identifiers
                                 if isinstance(value, str) and _IDENTIFIER.fullmatch(value)
                                 and board_allowed(metadata, value))
            count += len(selected)
            if count > 50000:
                return {}
            if selected:
                result[key] = selected
        except (ValueError, TypeError):
            continue
    return result


def invalidate_preferences():
    global _CACHE
    with _LOCK:
        _CACHE = None


def load_preferences(*, force_read=False):
    """Bound reads off the UI thread; action boundaries bypass the warm cache."""
    global _CACHE
    path = Path(settings_file())
    try:
        stat = path.stat()
        stamp = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if stat.st_size > _LIMIT:
            return {}
        with _LOCK:
            if not force_read and _CACHE is not None and _CACHE[0] == stamp:
                return dict(_CACHE[1])
        with path.open("rb") as stream:
            raw = stream.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            return {}
        data = json.loads(raw)
        values = _normalized(data.get(FIELD, {}) if isinstance(data, dict) else {})
        with _LOCK:
            _CACHE = (stamp, values, hashlib.sha256(raw).hexdigest())
        return dict(values)
    except (OSError, ValueError, TypeError):
        invalidate_preferences()
        return {}


def preferences_fingerprint():
    """Only compiler selections affect catalog identity, not unrelated settings."""
    values = load_preferences(force_read=True)
    payload = {"allowed_targets": sorted(f"{package}:{architecture}:{board}"
                for (package, architecture), boards in _ALLOWED_BOARDS.items() for board in boards),
               "selections": {key: sorted(value) for key, value in values.items()}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def selected_boards(metadata, preferences=None):
    key = association_key(metadata)
    if not key:
        return frozenset()
    values = load_preferences() if preferences is None else preferences
    identifiers = values.get(key, ()) if isinstance(values, dict) else ()
    if not isinstance(identifiers, (list, tuple, frozenset)) or len(identifiers) > 50000:
        return frozenset()
    return frozenset(value for value in identifiers if isinstance(value, str) and _IDENTIFIER.fullmatch(value)
                     and board_allowed(metadata, value))


def board_selected(metadata, board_id, preferences=None):
    return bool(board_allowed(metadata, board_id)
                and board_id in selected_boards(metadata, preferences))


def selection_identity(metadata):
    """Portable namespace identity for certificates; this does not enable CLI."""
    return ({key: metadata.get(key, "") for key in ("index_url", "package", "architecture")}
            if association_key(metadata) else {})


def _row_identity(row):
    if not isinstance(row, dict):
        return {}, ""
    arduino = row.get("arduino_cli")
    declared_fqbn = row.get("arduino_fqbn")
    prepared_fqbn = arduino.get("fqbn") if isinstance(arduino, dict) else None
    proof = row.get("arduino_source_proof")
    if isinstance(proof, dict):
        if row.get("arduino_backend_role") != "primary":
            return {}, ""
        parts = str(proof.get("core") or "").split(":")
        metadata = ({"package": parts[0], "architecture": parts[1], "index_url": proof.get("index_url", "")}
                    if len(parts) == 2 else {})
        fqbn = proof.get("fqbn")
    else:
        metadata = row.get("arduino_cli_selection")
        fqbn = declared_fqbn or prepared_fqbn
    identifier = row.get("arduino_id") or row.get("arduino_board_id")
    if not association_key(metadata) or fqbn != ":".join((metadata["package"], metadata["architecture"], str(identifier or ""))):
        return {}, ""
    if any(value is not None and value != fqbn for value in (declared_fqbn, prepared_fqbn)):
        return {}, ""
    return metadata, identifier


def row_allowed(row):
    metadata, identifier = _row_identity(row)
    return board_allowed(metadata, identifier)


def selection_for_row(row, preferences=None):
    metadata, identifier = _row_identity(row)
    return board_selected(metadata, identifier, preferences)
