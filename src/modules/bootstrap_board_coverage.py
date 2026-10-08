"""Audit Arduino picker declarations against the packages prepared by bootstrap.

This module runs only in the preparation worker. It never installs packages,
changes board declarations or converts ambiguous evidence into a board choice.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

REPORT = ".mcu-bootstrap-board-coverage.json"
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def requested_board_sources(argv=None):
    """Retain repeated explicit source roots when a host forwards bootstrap."""
    values = list(sys.argv[1:] if argv is None else argv)
    sources = []
    for index, value in enumerate(values):
        if value != "--board-source":
            continue
        if index + 1 >= len(values) or values[index + 1].startswith("--"):
            raise ValueError("--board-source requires an Arduino board source directory")
        sources.append(os.path.abspath(os.path.expandvars(os.path.expanduser(values[index + 1]))))
    return sources


def default_board_sources():
    """Read the normal picker roots without migrating or writing settings."""
    from main.core.constants import SCRIPT_DIR
    download = Path.home() / "Documents" / "_MCUFlasherByNaph_src"
    for path in (SCRIPT_DIR / "index_json/arduino_browser_settings.json",
                 SCRIPT_DIR / "src/dbs/arduino_browser_settings.json",
                 SCRIPT_DIR / "arduino_browser_settings.json"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_size <= 1024 * 1024:
                settings = json.loads(path.read_text(encoding="utf-8"))
                value = settings.get("download_dir") if isinstance(settings, dict) else None
                if isinstance(value, str) and value.strip():
                    candidate = Path(os.path.expandvars(os.path.expanduser(value)))
                    if candidate.is_dir():
                        download = candidate
        except (OSError, ValueError, TypeError):
            pass
        break
    sources = [download / "Boards"]
    for name in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        value = os.environ.get(name)
        if value:
            sources.append(Path(value) / "Arduino15/packages")
    sources.append(Path.home() / ".arduino15/packages")
    return _source_roots(sources)


def _source_roots(sources):
    roots = []
    seen = set()
    for source in sources:
        path = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(str(source)))))
        if not path.is_dir():
            continue
        identity = os.path.normcase(str(path.resolve()))
        if identity not in seen:
            roots.append(path)
            seen.add(identity)
    return roots


def retained_board_sources(core, sources=()):
    """Keep earlier reported source roots when another package is prepared."""
    previous = []
    # The certificate keeps the small source list even when the full report
    # has more board rows than a bounded metadata reader should load.
    from src.modules.offline_bootstrap import MARKER, SCHEMA
    try:
        marker = Path(core) / MARKER
        if marker.stat().st_size <= 16 * 1024 * 1024:
            certificate = json.loads(marker.read_text(encoding="utf-8"))
            coverage = certificate.get("board_coverage") if isinstance(certificate, dict) else None
            values = coverage.get("sources") if isinstance(coverage, dict) else None
            if (certificate.get("schema") == SCHEMA and certificate.get("host") == sys.platform
                    and isinstance(values, list) and len(values) <= 512
                    and all(isinstance(value, str) for value in values)):
                previous = values
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        path = Path(core) / REPORT
        if path.stat().st_size <= 16 * 1024 * 1024:
            report = json.loads(path.read_text(encoding="utf-8"))
            values = report.get("sources") if isinstance(report, dict) else None
            if (report.get("schema") == 1 and report.get("host") == sys.platform
                    and isinstance(values, list) and len(values) <= 512
                    and all(isinstance(value, str) for value in values)):
                previous = [*previous, *values]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return _source_roots([*previous, *sources])


def report_coverage(core, sources, *, plan, platform_sources, board_manifests, log=print, source_preparation=None):
    """Publish full source-bound coverage while retaining package readiness scope."""
    if os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME"):
        raise RuntimeError("Bootstrap board coverage belongs to the preparation worker")
    from main.core import board_catalog
    from main.core.file_utils import write_generated_text
    from src.modules.arduino_cli_support import (load_prepared_targets,
        prepared_target_index, prepared_target_for_record, planned_source_target_for_record, planned_source_target_index,
        source_namespace_target_for_record, source_namespace_proof)

    # Keep the verified short Windows package-store spelling.
    core = Path(os.path.abspath(core))
    roots = _source_roots(sources)
    catalog = board_catalog._load_platformio_board_catalog(core, force_read=True)
    features = {id(candidate): board_catalog._arduino_match_features(candidate, candidate=True)
                for candidate in catalog}
    prepared_rows = load_prepared_targets(core)
    associations = prepared_target_index(prepared_rows)
    primary_associations = planned_source_target_index(prepared_rows)
    validation_cache = {}
    digest_cache = {}
    covered_platforms = set(platform_sources.values())
    arduino_selected = "frameworks" not in plan or "arduino" in plan["frameworks"]
    rows = []
    seen = set()
    failures = {}
    for result in (source_preparation or {}).get("boards", []):
        if (isinstance(result, dict) and result.get("status") == "unavailable"
                and isinstance(result.get("reason"), str) and result["reason"]
                and isinstance(result.get("source_sha256"), str)
                and _DIGEST.fullmatch(result["source_sha256"])
                and result.get("source_file") and result.get("arduino_id")):
            key = (os.path.normcase(os.path.abspath(result["source_file"])),
                   str(result["arduino_id"]), result["source_sha256"])
            failures[key] = result

    def digest_file(path):
        identity = os.path.normcase(os.path.abspath(path))
        if identity not in digest_cache:
            with Path(path).open("rb") as source:
                digest = hashlib.sha256()
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            digest_cache[identity] = digest.hexdigest()
        return digest_cache[identity]

    for root in roots:
        records = board_catalog._parse_downloaded_arduino_board_files(root, force_read=True)
        for record in records:
            hidden = str((record.get("properties") or {}).get("hide", "")).strip().casefold()
            if hidden in {"true", "1", "yes"}:
                continue
            source_file = str(record.get("source_file") or "")
            identity = (os.path.normcase(os.path.abspath(source_file)), str(record.get("arduino_id") or ""))
            if identity in seen:
                continue
            seen.add(identity)
            row = {"name": str(record.get("name") or record.get("arduino_id") or "Unnamed board"),
                   "arduino_id": str(record.get("arduino_id") or ""),
                   "source_file": source_file, "source_sha256": record.get("source_sha256"),
                   "mcu": str(record.get("mcu") or ""), "framework": "arduino",
                   "status": "unavailable", "platform": "", "board": "", "candidates": []}
            rows.append(row)
            try:
                expected_source = record.get("source_sha256")
                if (not isinstance(expected_source, str) or not _DIGEST.fullmatch(expected_source)
                        or digest_file(source_file) != expected_source):
                    row["reason"] = "Arduino declarations changed or lack a verified source receipt; prepare board support again."
                    continue
                prepared = prepared_target_for_record(record, catalog, core=core,
                    rows=associations, validation_cache=validation_cache)
                if prepared and prepared.get("backend") == "arduino-cli":
                    row.update(platform=str(prepared.get("platform") or ""),
                               board=str(prepared.get("board") or ""), backend="arduino-cli",
                               arduino_cli=dict(prepared.get("arduino_cli") or {}),
                               arduino_backend_role=prepared.get("arduino_backend_role", "fallback"),
                               arduino_source_proof=prepared.get("arduino_source_proof"))
                    row.update(status="ready" if arduino_selected else "outside_plan",
                               reason=("Exact Arduino CLI board and source certificates verified."
                                       if arduino_selected else "Arduino is outside the explicit bootstrap framework plan."))
                    continue
                diagnosis = board_catalog.diagnose_arduino_board_record(record, catalog, match_features=features)
                row["candidates"] = diagnosis.get("candidates") or []
                failure = failures.get((*identity, expected_source))
                primary_intent = planned_source_target_for_record(record, core=core, rows=primary_associations,
                                                                  validation_cache=validation_cache)
                if not primary_intent:
                    primary_intent = source_namespace_target_for_record(record, core=core, rows=primary_associations,
                                                                        validation_cache=validation_cache)
                if not primary_intent and failure and source_namespace_proof(failure):
                    primary_intent = failure
                if primary_intent and not prepared:
                    row.update(status="unavailable", backend="arduino-cli", arduino_backend_role="primary",
                               arduino_source_proof=primary_intent["arduino_source_proof"],
                               platform=primary_intent["arduino_source_proof"]["core"], board=row["arduino_id"],
                               reason=(failure["reason"] if failure else
                                       "The original Arduino source target needs preparation; its exact compiler certificate is unavailable."))
                    continue
                match = prepared or diagnosis.get("match")
                if not match:
                    row.update(status="ambiguous" if diagnosis.get("status") == "ambiguous" else "unavailable",
                               reason=("Several installed definitions match this declaration; select an exact board or provide an explicit association."
                                       if diagnosis.get("status") == "ambiguous" else
                                       "No verified installed PlatformIO definition or prepared Arduino CLI association for this declaration."))
                    if failure:
                        row.update(status="unavailable", reason=failure["reason"])
                    continue
                row.update(platform=str(match.get("platform") or ""), board=str(match.get("id") or ""),
                           backend="platformio", manifest=str(match.get("manifest") or ""),
                           match_reasons=list(match.get("match_reasons") or []))
                if row["platform"] not in covered_platforms or not arduino_selected:
                    row.update(status="outside_plan", reason="This board/framework is outside the explicit bootstrap package plan.")
                    continue
                manifest = Path(row["manifest"])
                relative = str(manifest.relative_to(core))
                expected_manifest = board_manifests.get(relative)
                if (not isinstance(expected_manifest, str) or not _DIGEST.fullmatch(expected_manifest)
                        or digest_file(manifest) != expected_manifest):
                    row["reason"] = "Exact PlatformIO definition is missing, changed or was not certified by this preparation."
                    continue
                row.update(status="ready", manifest_sha256=expected_manifest,
                           reason="Exact installed board definition and configured offline package variants verified.")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                row.update(status="unavailable", reason=f"Board source or definition could not be verified: {exc}")

    ready_count = sum(row["status"] == "ready" for row in rows)
    report = {"schema": 1, "host": sys.platform, "sources": [str(root) for root in roots],
              "total": len(rows), "ready_count": ready_count,
              "unavailable_count": len(rows) - ready_count,
              "ambiguous_count": sum(row["status"] == "ambiguous" for row in rows),
              "outside_plan_count": sum(row["status"] == "outside_plan" for row in rows),
              "boards": rows}
    write_generated_text(core / REPORT, json.dumps(report, ensure_ascii=False, indent=2))
    if rows:
        log(f"Bootstrap Arduino board coverage: {ready_count} of {len(rows)} declarations ready.")
        if report["unavailable_count"]:
            log(f"WARNING: {report['unavailable_count']} Arduino declarations remain ambiguous, unavailable or outside the package plan. "
                f"Bootstrap package readiness does not certify those boards. Full coverage: {core / REPORT}")
    return report


def required_native_files(core, coverage):
    """Return cheap launch-gate paths from already byte-validated CLI receipts."""
    from src.modules.arduino_cli_support import TARGETS_FILE
    core = Path(os.path.abspath(core))
    files = set()
    guards = set()
    for row in coverage.get("boards", []):
        if row.get("status") != "ready" or row.get("backend") != "arduino-cli":
            continue
        receipt = row.get("arduino_cli") or {}
        certificate_path = core / str(receipt.get("certificate") or "")
        certificate_path.resolve().relative_to((core / "arduino-cli/certificates").resolve())
        if certificate_path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Arduino CLI certificate exceeds supported bounds")
        certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
        store = core / str(receipt.get("store") or "arduino-cli")
        store.resolve().relative_to((core / "arduino-cli").resolve())
        configuration = store / "arduino-cli.yaml"
        files.update({TARGETS_FILE, str(configuration.relative_to(core)), str(certificate_path.relative_to(core))})
        required = set(certificate.get("proof_files") or {})
        for relative, entry in (certificate.get("tool_files") or {}).items():
            path = Path(relative)
            parts = path.parts
            package_parts = Path(relative).relative_to(Path(str(store.relative_to(core))) / "data/packages").parts
            # Header/library inventories remain byte/size checked in the
            # selected-target worker. Startup needs package entry points only.
            runnable = (path.suffix.casefold() in {".exe", ".bat", ".cmd", ".sh"}
                        or not path.suffix and isinstance(entry, dict) and "sha256" in entry
                        or path.suffix.casefold() == ".py" and
                           ("bin" in package_parts or len(package_parts) == 5))
            metadata = path.name.casefold() in {"package.json", "installed.json"} and len(package_parts) == 5
            if runnable or metadata:
                required.add(relative)
        if len(required) > 512:
            raise ValueError("Prepared Arduino CLI entry points exceed the bounded startup path limit")
        for relative in required:
            path = core / relative
            path.resolve().relative_to((core / "arduino-cli").resolve())
            if not path.is_file():
                raise ValueError("A prepared Arduino CLI entry point became unavailable")
            files.add(str(path.relative_to(core)))
        cli = Path(str(receipt.get("cli_path") or certificate.get("cli_path") or ""))
        if not cli.is_file():
            raise ValueError("A prepared Arduino CLI executable became unavailable")
        # The actual executable was selected and byte-verified by the worker.
        # Keep its absolute spelling if it lives outside the package store.
        guards.add(str(cli))
        if len(files) > 1024 or len(guards) > 128:
            raise ValueError("Prepared Arduino CLI startup paths exceed the bounded launch gate")
    return sorted(files), sorted(guards)
