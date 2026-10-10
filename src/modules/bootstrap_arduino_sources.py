"""Prepare exact Arduino source targets that have no unique PlatformIO identity.

Only the online bootstrap worker calls this module. Source receipts authorize
the requested core/version; installed source and builder certificates prove it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

RECEIPT = ".mcu-board-source.json"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,159}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_BUILTIN_INDEXES = {
    ("esp32", "esp32"): "https://espressif.github.io/arduino-esp32/package_esp32_index.json",
    ("arduino", "avr"): "https://downloads.arduino.cc/packages/package_index.json",
}


def source_receipt_metadata(receipt):
    """Validate receipt structure and source authority without filesystem writes."""
    if not isinstance(receipt, dict) or receipt.get("schema") != 1:
        return {}
    authority = receipt.get("authority")
    if authority not in {"official-index", "official-source-tag"}:
        return {}
    metadata = {key: str(receipt.get(key) or "") for key in ("package", "architecture", "version", "index_url")}
    if (not _IDENTIFIER.fullmatch(metadata["package"]) or not _IDENTIFIER.fullmatch(metadata["architecture"])
            or not _VERSION.fullmatch(metadata["version"])):
        return {}
    try:
        parsed = urlsplit(metadata["index_url"])
    except ValueError:
        return {}
    if (len(metadata["index_url"]) > 2048 or parsed.scheme != "https" or not parsed.netloc
            or parsed.username or parsed.password or parsed.fragment
            or any(ord(char) < 32 for char in metadata["index_url"])):
        return {}
    identity = (metadata["package"], metadata["architecture"])
    official = _BUILTIN_INDEXES.get(identity)
    if (official and metadata["index_url"] != official) or (authority == "official-source-tag" and not official):
        return {}
    source_files = receipt.get("source_files")
    if (not isinstance(source_files, dict) or not source_files or len(source_files) > 10000
            or not _SHA256.fullmatch(str(receipt.get("archive_sha256") or ""))):
        return {}
    for relative, expected in source_files.items():
        if (not isinstance(relative, str) or len(relative) > 2048 or not relative
                or Path(relative).anchor or PureWindowsPath(relative).anchor or ".." in Path(relative).parts
                or any(ord(char) < 32 for char in relative) or not _SHA256.fullmatch(str(expected))):
            return {}
    from src.modules.board_index_targets import normalize_platformio_configuration
    try:
        configuration = normalize_platformio_configuration(receipt.get("platformio"))
    except ValueError:
        return {}
    metadata.update(authority=authority, name=str(receipt.get("name") or "")[:512],
                    source_files=dict(source_files), archive_sha256=receipt["archive_sha256"])
    if configuration:
        metadata["platformio"] = configuration
    return metadata


def _digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(131072), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(source, arduino_id):
    return os.path.normcase(os.path.abspath(source)), str(arduino_id or "")


def _within(path, directory):
    try:
        Path(path).resolve().relative_to(Path(directory).resolve())
        return True
    except (OSError, ValueError):
        return False


def _source_roots(sources):
    roots = []
    seen = set()
    for source in sources:
        path = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(str(source)))))
        if path.is_dir() and os.path.normcase(str(path.resolve())) not in seen:
            roots.append(path)
            seen.add(os.path.normcase(str(path.resolve())))
    return roots


def _receipt_for_record(record, roots, *, cache):
    source = Path(str(record.get("source_file") or ""))
    if not source.is_file() or not any(_within(source, root) for root in roots):
        return {}, None, "The declaration source is outside the requested board directories."
    directory = source.parent
    while any(_within(directory, root) for root in roots):
        receipt_path = directory / RECEIPT
        if receipt_path.is_file():
            key = os.path.normcase(str(receipt_path.resolve()))
            if key not in cache:
                try:
                    if receipt_path.stat().st_size > 2 * 1024 * 1024:
                        raise ValueError("source receipt exceeds the supported size")
                    metadata = source_receipt_metadata(json.loads(receipt_path.read_text(encoding="utf-8")))
                    if not metadata:
                        raise ValueError("source receipt has no verified core/version/index identity")
                    bindings = {}
                    for relative, expected in metadata["source_files"].items():
                        path = directory / relative
                        if not _within(path, directory) or not path.is_file() or _digest(path) != expected:
                            raise ValueError("receipt source bytes are missing or changed")
                        bindings[os.path.normcase(str(path.resolve()))] = expected
                    cache[key] = metadata, directory, bindings, ""
                except (OSError, ValueError, TypeError) as exc:
                    cache[key] = {}, directory, {}, f"Arduino source receipt could not be verified: {exc}."
            metadata, receipt_root, bindings, reason = cache[key]
            if not metadata:
                return {}, receipt_root, reason
            source_key = os.path.normcase(str(source.resolve()))
            platform_key = os.path.normcase(str((source.parent / "platform.txt").resolve()))
            if (bindings.get(source_key) != record.get("source_sha256") or platform_key not in bindings):
                return {}, receipt_root, "The source receipt does not bind this exact boards.txt and platform.txt pair."
            return metadata, receipt_root, ""
        parent = directory.parent
        if parent == directory:
            break
        directory = parent
    return {}, None, "No verified Arduino source receipt; prepare or download the exact source package again."


def _platformio_preferences():
    """Read existing explicit source associations without migrating settings."""
    from main.core.constants import SCRIPT_DIR
    index = SCRIPT_DIR / "index_json/arduino_browser_settings.json"
    database = SCRIPT_DIR / "src/dbs/arduino_browser_settings.json"
    # Match the downloader's current owner even when its file is absent.
    path = (index if index.is_file() or index.parent.is_dir() else
            database if database.is_file() or database.parent.is_dir() else
            SCRIPT_DIR / "arduino_browser_settings.json")
    try:
        if path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
            data = json.loads(path.read_text(encoding="utf-8"))
            values = data.get("board_platformio_associations", {}) if isinstance(data, dict) else {}
            return values if isinstance(values, dict) else {}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _explicit_platformio(metadata, preferences):
    key = json.dumps([metadata.get("index_url", ""), metadata.get("package", ""),
                      metadata.get("architecture", "")], ensure_ascii=False, separators=(",", ":"))
    return bool(metadata.get("platformio") or preferences.get(key))


def prepare_sources(core, sources, *, emit, jobs=None):
    """Prepare primary exact FQBNs; the caller owns the package-store lease."""
    if os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME") or os.environ.get("MCU_FLASHER_WORKSPACE_RUNTIME"):
        raise RuntimeError("Arduino source preparation belongs to the separate bootstrap worker")
    from main.core import board_catalog
    from src.modules.arduino_board_selection import board_selected, disabled_reason, load_preferences
    from src.modules.arduino_cli_support import (load_prepared_targets, prepared_target_index,
        prepared_target_for_record, source_declaration_proof, prepare_source_boards, publish_prepared_targets,
        planned_source_target_index, source_namespace_target_for_record)

    core = Path(os.path.abspath(core))  # Preserve the verified short package-store spelling.
    roots = _source_roots(sources)
    catalog = board_catalog._load_platformio_board_catalog(core, force_read=True)
    features = {id(candidate): board_catalog._arduino_match_features(candidate, candidate=True) for candidate in catalog}
    previous = load_prepared_targets(core)
    associations = prepared_target_index(previous)
    intentions = planned_source_target_index(previous)
    prior_cli = {_identity(row.get("source_file", ""), row.get("arduino_id")) for row in previous
                 if row.get("backend") == "arduino-cli"}
    preferences = _platformio_preferences()
    cli_preferences = load_preferences(force_read=True)
    receipt_cache = {}
    source_digests = {}
    validation_cache = {}
    rows = []
    groups = {}
    affected = set()
    affected_directories = set()
    seen = set()

    for root in roots:
        for record in board_catalog._parse_downloaded_arduino_board_files(root, force_read=True):
            if str((record.get("properties") or {}).get("hide", "")).strip().casefold() in {"true", "1", "yes"}:
                continue
            source_file = str(record.get("source_file") or "")
            identity = _identity(source_file, record.get("arduino_id"))
            if identity in seen:
                continue
            seen.add(identity)
            row = {"name": str(record.get("name") or record.get("arduino_id") or "Unnamed board"),
                   "arduino_id": str(record.get("arduino_id") or ""), "source_file": source_file,
                   "source_sha256": record.get("source_sha256"), "framework": "arduino",
                   "status": "unavailable", "backend": "", "platform": "", "board": ""}
            rows.append(row)
            try:
                if (not _SHA256.fullmatch(str(record.get("source_sha256") or ""))
                        or not any(_within(source_file, source_root) for source_root in roots)):
                    row["reason"] = "Arduino declarations lack a verified source receipt inside the requested directories."
                    continue
                if identity[0] not in source_digests:
                    source_digests[identity[0]] = _digest(source_file)
                if source_digests[identity[0]] != record["source_sha256"]:
                    row["reason"] = "Arduino declarations changed or lack a verified parsed source receipt."
                    continue
                metadata, directory, receipt_reason = _receipt_for_record(record, roots, cache=receipt_cache)
                prepared = prepared_target_for_record(record, catalog, core=core, rows=associations,
                                                       validation_cache=validation_cache)
                intended = source_namespace_target_for_record(record, core=core, rows=intentions,
                                                             validation_cache=validation_cache)
                if metadata and _explicit_platformio(metadata, preferences):
                    row.update(status="not_required", reason="An explicit PlatformIO source association takes precedence.")
                    # Retain verified PIO aliases. Retire any older CLI
                    # association when this source now has an explicit PIO target.
                    if identity in prior_cli and not (prepared and prepared.get("backend") != "arduino-cli"):
                        affected.add(identity)
                        affected_directories.add(Path(source_file).parent)
                    continue
                diagnosis = board_catalog.diagnose_arduino_board_record(record, catalog, match_features=features)
                match = diagnosis.get("match")
                if prepared and prepared.get("backend") != "arduino-cli":
                    row.update(status="not_required", backend="platformio", platform=prepared.get("platform", ""),
                               board=prepared.get("id", ""), reason="An exact prepared PlatformIO association is already available.")
                    continue
                if match:
                    if identity in prior_cli:
                        affected.add(identity)
                        affected_directories.add(Path(source_file).parent)
                    row.update(status="not_required", backend="platformio", platform=match.get("platform", ""),
                               board=match.get("id", ""), reason="A unique installed PlatformIO definition represents this declaration.")
                    continue
                if diagnosis.get("status") == "ambiguous":
                    if identity in prior_cli:
                        affected.add(identity)
                        affected_directories.add(Path(source_file).parent)
                    row.update(status="ambiguous", backend="platformio",
                               candidates=diagnosis.get("candidates") or [],
                               reason="PlatformIO recognizes multiple concrete boards for this declaration; select an exact board or provide an explicit association.")
                    continue
                if prepared:
                    if not metadata or not board_selected(metadata, row["arduino_id"], cli_preferences):
                        if identity in prior_cli:
                            affected.add(identity)
                            affected_directories.add(Path(source_file).parent)
                        row.update(status="unavailable", backend="", reason=(receipt_reason if not metadata else
                            disabled_reason(metadata, row["arduino_id"])))
                        continue
                    row.update(prepared)
                    row.update(status="ready", reason="Exact prepared Arduino source target certificates reused.")
                    continue
                if intended:
                    row.update(backend="arduino-cli", arduino_backend_role="primary",
                               arduino_source_proof=intended["arduino_source_proof"],
                               arduino_fqbn=intended["arduino_source_proof"]["fqbn"])
                row["candidates"] = diagnosis.get("candidates") or []
                affected.add(identity)
                affected_directories.add(Path(source_file).parent)
                if not metadata:
                    row["reason"] = receipt_reason
                    continue
                if not board_selected(metadata, row["arduino_id"], cli_preferences):
                    row.update(status="unavailable", backend="", reason=disabled_reason(metadata, row["arduino_id"]))
                    for key in ("arduino_backend_role", "arduino_source_proof", "arduino_fqbn"):
                        row.pop(key, None)
                    continue
                proof = source_declaration_proof(record, metadata)
                if not proof:
                    row["reason"] = "The exact Arduino source declaration could not authorize a primary FQBN."
                    continue
                row.update(status="preparation_required", arduino_backend_role="primary", arduino_source_proof=proof,
                           arduino_fqbn=proof["fqbn"],
                           platformio_support="unknown", reason="Preparing the exact source-declared Arduino target.")
                group = groups.setdefault(identity[0], {"directory": directory, "metadata": metadata, "rows": []})
                group["rows"].append(row)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                row.update(status="unavailable", reason=f"Arduino source declaration could not be verified: {exc}.")

    for group in groups.values():
        try:
            prepared_rows = prepare_source_boards(core, group["directory"], group["metadata"], group["rows"],
                                                 emit=emit, jobs=jobs)
            prepared_by_id = {_identity(row.get("source_file", ""), row.get("arduino_id")): row for row in prepared_rows}
            for row in group["rows"]:
                result = prepared_by_id.get(_identity(row["source_file"], row["arduino_id"]))
                if result is None:
                    row.update(status="unavailable", reason="Arduino source preparation omitted the exact requested target.")
                else:
                    row.update(result)
                    if row.get("status") not in {"ready", "unavailable"}:
                        row.update(status="unavailable", reason="Arduino source preparation did not verify the exact requested target.")
        except Exception as exc:
            for row in group["rows"]:
                row.update(status="unavailable", reason=f"Arduino source preparation failed: {exc}.")

    if affected_directories:
        # A source-level promotion retains unaffected proofs, including other
        # boards beneath nested source directories from the same archive.
        retained = [dict(row) for row in previous
                    if _identity(row.get("source_file", ""), row.get("arduino_id")) not in affected]
        updated = [row for row in rows if _identity(row["source_file"], row["arduino_id"]) in affected]
        publication = retained + updated
        directories = sorted(affected_directories, key=lambda path: (len(path.parts), str(path)))
        published = []
        for directory in directories:
            if any(_within(directory, parent) for parent in published):
                continue
            publish_prepared_targets(core, directory, publication)
            published.append(directory)

    summary = {"schema": 1, "total": len(rows), "boards": rows,
               "ready_count": sum(row["status"] == "ready" for row in rows),
               "unavailable_count": sum(row["status"] == "unavailable" for row in rows),
               "ambiguous_count": sum(row["status"] == "ambiguous" for row in rows),
               "not_required_count": sum(row["status"] == "not_required" for row in rows)}
    emit("preparing", message=(f"Arduino source targets: {summary['ready_count']} prepared; "
                               f"{summary['unavailable_count']} unavailable; "
                               f"{summary['ambiguous_count']} need an exact PlatformIO board selection; "
                               f"{summary['not_required_count']} represented by PlatformIO or an explicit association."),
         source_ready_count=summary["ready_count"], source_unavailable_count=summary["unavailable_count"],
         source_ambiguous_count=summary["ambiguous_count"])
    return summary
