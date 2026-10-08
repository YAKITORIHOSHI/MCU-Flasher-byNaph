#!/usr/bin/env python3
"""Explicit downloader-to-bootstrap handoff; workspace imports never install.

Arduino archives and PlatformIO packages describe different target ecosystems.
Every downloaded declaration is matched against installed and bootstrap-only
online definitions, with explicit associations for custom PlatformIO sources.
Every board in the archive receives a coverage result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_JOB_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_CORE_PLATFORMS = {
    ("arduino", "avr"): "atmelavr",
    ("arduino", "megaavr"): "atmelmegaavr",
    ("arduino", "sam"): "atmelsam",
    ("arduino", "samd"): "atmelsam",
    ("esp32", "esp32"): "espressif32",
    ("esp8266", "esp8266"): "espressif8266",
    ("stmicroelectronics", "stm32"): "ststm32",
    ("teensy", "avr"): "teensy",
}


def _small_metadata(metadata):
    """Bound the file handoff and retain only validated source configuration."""
    metadata = metadata if isinstance(metadata, dict) else {}
    result = {key: str(metadata.get(key) or "")[:256]
              for key in ("package", "architecture", "name", "version")}
    names = metadata.get("boards") or []
    if not isinstance(names, (list, tuple)):
        raise ValueError("Board package names must be a list")
    result["boards"] = [str(value.get("name") or "") if isinstance(value, dict) else str(value)
                        for value in names]
    result["boards"] = [value[:512] for value in result["boards"] if value]
    from src.modules.board_index_targets import normalize_platformio_configuration, normalize_index_url
    configuration = normalize_platformio_configuration(metadata.get("platformio"))
    if configuration:
        result["platformio"] = configuration
    index_url = normalize_index_url(metadata.get("index_url"))
    if index_url:
        result["index_url"] = index_url
    return result


def _default_core():
    from main.core.constants import SCRIPT_DIR
    from main.core.toolchain import _get_safe_platformio_core_dir
    return Path(_get_safe_platformio_core_dir(SCRIPT_DIR))


def start_preparation(board_directory, *, job_id, package_metadata=None, core=None, event_root=None):
    """Start one separate private-runtime bootstrap worker after extraction.

    This function performs no package/catalog scans and makes no installations.
    The returned process owns terminal job events and waits for active builds.
    """
    from src.modules.private_python_guard import is_running_private_python
    if not is_running_private_python():
        raise RuntimeError("Board preparation requires MCU Flasher's private Python runtime")
    if not _JOB_IDENTIFIER.fullmatch(str(job_id or "")):
        raise ValueError("Invalid board preparation job identity")
    from src.modules.package_jobs import write_request
    metadata = _small_metadata(package_metadata)
    if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > 16 * 1024 * 1024:
        raise ValueError("Board package metadata exceeds the supported 16 MB limit")
    request = write_request(job_id, metadata, root=event_root)
    # Preserve the verified interpreter and short Windows package-store alias.
    command = [sys.executable, "-B", str(Path(__file__).absolute()),
               "--boards", os.path.abspath(board_directory), "--job-id", str(job_id),
               "--package-request", str(request)]
    if core is not None:
        command.extend(["--core", os.path.abspath(core)])
    if event_root is not None:
        command.extend(["--events-root", os.path.abspath(event_root)])
    env = dict(os.environ)
    for key in ("MCU_FLASHER_OFFLINE_RUNTIME", "PIP_NO_INDEX", "PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
               PLATFORMIO_NO_TELEMETRY="1", PLATFORMIO_SETTING_ENABLE_TELEMETRY="false",
               PLATFORMIO_DISABLE_UPGRADE_CHECK="true")
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=0x08000000 if sys.platform == "win32" else 0)
    try:
        threading.Thread(target=_watch_preparation, args=(process, job_id, event_root),
                         name="MCU_PreparationProcessWatcher", daemon=True).start()
    except (OSError, RuntimeError) as exc:
        # The child has already started. Do not claim it failed, replay it or
        # regress a newer child stage when only the optional watcher failed.
        process.preparation_tracking_error = str(exc)
    return process


def _watch_preparation(process, job_id, event_root=None):
    """An early child import/guard failure must not leave a queued card forever."""
    from src.modules.package_jobs import get_job_snapshot, publish_event, TERMINAL
    process.wait()
    snapshot = get_job_snapshot(job_id, root=event_root)
    if not snapshot or snapshot.get("stage") not in TERMINAL:
        publish_event(job_id, "failed", root=event_root,
                      message="The preparation process stopped before reporting a result. Open the downloader to retry.",
                      progress=None)


def _merged_plan(base, previous=None, platforms=()):
    """Preserve the complete explicit plan instead of narrowing other targets."""
    from src.modules.offline_bootstrap import _validate_plan
    base = _validate_plan(base)
    previous = _validate_plan(previous) if previous else {}
    plan = {"schema": 1}
    for key in ("platforms", "libraries"):
        prior_entries = list(previous.get(key, []))
        def identity(specification):
            # Recognize only plain registry names/owners; custom URL/local
            # sources retain their exact specifications without reinterpretation.
            value = str(specification).split("@", 1)[0]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", value):
                return str(specification)
            if key == "platforms" and "/" not in value:
                value = "platformio/" + value
            return value.casefold()
        def explicit(specification):
            value = str(specification)
            return "@" in value or (key == "platforms" and "/" in value) or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", value)
        prior_by_identity = {identity(specification): specification for specification in prior_entries}
        current_identities = set()
        entries = []
        for specification in base.get(key, []):
            canonical = identity(specification)
            current_identities.add(canonical)
            prior = prior_by_identity.get(canonical)
            entries.append(prior if prior and not explicit(specification) else specification)
        entries.extend(specification for specification in prior_entries
                       if identity(specification) not in current_identities)
        if key == "platforms":
            # Installed PlatformIO platforms share a physical target name.
            # Different registry owners cannot safely prepare the same name
            # twice; report the conflict rather than changing either source.
            by_name = {}
            for specification in entries:
                canonical = identity(specification)
                if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", canonical):
                    name = canonical.rsplit("/", 1)[-1]
                    if name in by_name and by_name[name] != specification:
                        raise ValueError(f"Conflicting platform specifications for {name}; choose one exact source/version in the offline plan")
                    by_name[name] = specification
            existing = {identity(specification) for specification in entries}
            for specification in platforms:
                canonical = identity(specification)
                if canonical in existing:
                    continue
                if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", canonical):
                    name = canonical.rsplit("/", 1)[-1]
                    prior = by_name.get(name)
                    if prior:
                        if identity(prior) != canonical:
                            raise ValueError(f"Conflicting platform specifications for {name}; choose one exact source/version in the offline plan")
                        continue
                    by_name[name] = specification
                entries.append(specification)
                existing.add(canonical)
        plan[key] = list(dict.fromkeys(entries))
    # Omission means all declared frameworks; keep that complete scope.
    if "frameworks" in base and (not previous or "frameworks" in previous):
        plan["frameworks"] = list(dict.fromkeys(base["frameworks"] + previous.get("frameworks", [])))
    return _validate_plan(plan)


def preparation_plan(records, *, package_metadata=None, catalog=None, base_plan=None, previous_plan=None,
                     registry_catalog=None, platform_sources=None, installed_sources=None, registry_proof=None):
    """Plan arbitrary index targets from exact evidence, retaining all coverage.

    Registry matches authorize platform preparation, never readiness. Custom
    source associations are supplied by the user and verified after install.
    """
    from main.core.board_catalog import _arduino_match_features
    from src.modules.board_index_targets import (exact_arduino_match, explicit_board_match, merge_catalogs,
        registry_platform_name, registry_platform_identity, hardware_identity_present)
    from src.modules.offline_bootstrap import load_plan
    metadata = _small_metadata(package_metadata)
    configuration = metadata.get("platformio", {})
    explicit_spec = configuration.get("platform", "")
    mappings = configuration.get("board_ids", {})
    platform = (str((platform_sources or {}).get(explicit_spec) or registry_platform_name(explicit_spec))
                if explicit_spec else _CORE_PLATFORMS.get((metadata["package"].casefold(), metadata["architecture"].casefold())))
    catalog = merge_catalogs(list(catalog or []), list(registry_catalog or []))
    base = base_plan if base_plan is not None else load_plan()
    if explicit_spec:
        base = dict(base, platforms=[spec for spec in base["platforms"]
                    if registry_platform_identity(spec) != registry_platform_identity(explicit_spec)] + [explicit_spec])
    selected_platforms = [platform] if platform and not explicit_spec else []
    features = {id(candidate): _arduino_match_features(candidate, candidate=True) for candidate in catalog}
    rows = []
    for record in records:
        candidates = [candidate for candidate in catalog
                      if not platform or str(candidate.get("platform") or "").casefold() == platform.casefold()]
        # A URL is not a hardware identity. Wait for its actual installed
        # platform name before comparing any unrelated installed candidates.
        if explicit_spec and not platform:
            candidates = []
        mapped_id = mappings.get(str(record.get("arduino_id") or ""), "")
        match = (explicit_board_match(record, mapped_id, candidates) if mapped_id else
                 exact_arduino_match(record, candidates, features))
        if not match and platform and not explicit_spec:
            # Reviewed core families are a preparation hint. A user-added
            # declaration may have a unique canonical definition on another
            # development platform; exact global evidence takes precedence.
            match = exact_arduino_match(record, catalog, features)
        source = explicit_spec
        if match and not source:
            source = (installed_sources or {}).get(str(match.get("platform") or "")) or match.get("platform_spec") or ""
            if not source:
                configured_spec = next((spec for spec in _merged_plan(base, previous_plan)["platforms"]
                                        if registry_platform_name(spec) == str(match.get("platform") or "").casefold()), "")
                source = configured_spec
            if source:
                selected_platforms.append(source)
        row = {"name": str(record.get("name") or record.get("arduino_id") or "Unnamed board"),
               "arduino_id": str(record.get("arduino_id") or ""),
               "platform": str((match or {}).get("platform") or platform or ""),
               "board": str((match or {}).get("id") or ""),
               "platform_spec": source or platform or "", "explicit_board_id": mapped_id,
               "source_file": str(record.get("source_file") or ""),
               "source_sha256": str(record.get("source_sha256") or ""),
               "arduino_fqbn": ":".join((metadata["package"], metadata["architecture"], str(record.get("arduino_id") or ""))),
               "platformio_support": "supported" if match else "unknown",
               "backend": "platformio",
               "status": "preparation_required", "reason": "Platform package requires preparation"}
        if match:
            frameworks = sorted(match.get("frameworks") or [])
            row["frameworks"] = frameworks
            row["manifest"] = str(match.get("manifest") or "")
            row["match_reasons"] = list(match.get("match_reasons") or [])
            selected = set(_merged_plan(base, previous_plan).get("frameworks", frameworks))
            if "arduino" not in frameworks or "arduino" not in selected:
                row.update(status="unsupported", reason="Arduino is unavailable for this exact board or outside the explicit preparation plan")
            elif not source and not platform:
                row.update(status="unavailable", reason="Exact definition is installed, but its PlatformIO source is unverified. Add its exact platform specification in Board indexes.")
        elif explicit_spec:
            row["board"] = mapped_id
            row.update(reason="Verify the explicit PlatformIO source and board mapping after preparation")
        elif not platform:
            row.update(status="unavailable", reason="No unique exact PlatformIO target for this Arduino declaration. Add the platform specification and optional board ID mapping in Board indexes.")
        if (not match and not explicit_spec and registry_proof and registry_proof.get("catalog_count", 0) > 0
                and not hardware_identity_present(record, catalog, features)):
            row.update(status="unsupported", platformio_support="unsupported", backend="",
                       platformio_support_proof=dict(registry_proof),
                       reason="The complete PlatformIO catalog has no exact definition for this Arduino board. Preparing Arduino CLI support instead.")
        rows.append(row)
    # Index names are useful even when a malformed archive has no boards.txt.
    # Their names alone never grant a target identity or readiness.
    seen_names = {row["name"].casefold() for row in rows}
    for name in metadata["boards"]:
        if name.casefold() not in seen_names:
            rows.append({"name": name, "arduino_id": "", "platform": platform or "", "board": "",
                         "platformio_support": "unknown", "backend": "",
                         "status": "unavailable", "reason": "Board is listed in the index but has no declaration in this downloaded archive"})
            seen_names.add(name.casefold())
    plan = _merged_plan(base, previous_plan, list(dict.fromkeys(selected_platforms)))
    return {"plan": plan, "boards": rows, "platform": platform or ""}


def _previous_plan(core):
    from src.modules.offline_bootstrap import MARKER, SCHEMA, _validate_plan
    try:
        data = json.loads((Path(core) / MARKER).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if data.get("schema") != SCHEMA or data.get("host") != sys.platform:
            return None
        return _validate_plan(data["prepared_plan"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _certified_definitions(core, rows):
    """Verify requested source bytes in this worker without slowing startup."""
    from src.modules.offline_bootstrap import MARKER
    try:
        data = json.loads((Path(core) / MARKER).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return set()
        signatures = data.get("board_manifests")
        probes = data.get("exact_builder_targets")
        if not isinstance(signatures, dict) or not isinstance(probes, list):
            return set()
        prepared_targets = {(probe.get("platform"), probe.get("board")) for probe in probes
                            if isinstance(probe, dict) and probe.get("framework") == "arduino"}
        verified = set()
        for row in rows:
            if row["status"] != "preparation_required":
                continue
            try:
                manifest = Path(row.get("manifest") or "")
                if not row.get("board") or (row.get("platform"), row.get("board")) not in prepared_targets:
                    continue
                relative = str(manifest.relative_to(Path(core)))
                expected = signatures.get(relative)
                if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
                    continue
                with manifest.open("rb") as source:
                    raw = source.read(65537)
                if len(raw) <= 65536 and hashlib.sha256(raw).hexdigest() == expected:
                    # Carry these exact validated bytes into publication;
                    # rereading afterward could bind an unprepared mutation.
                    row["manifest_sha256"] = expected
                    verified.add((row["platform"], row["board"]))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return verified
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def run_preparation(core, board_directory, *, package_metadata=None, emit, jobs=None):
    """Worker-only operation; caller holds the exclusive package-store lease."""
    if os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME"):
        raise RuntimeError("Board packages must be prepared outside the workspace process")
    from main.core import board_catalog
    from src.modules.offline_bootstrap import load_plan, prepare, ready
    from src.modules.board_index_targets import (fetch_preparation_catalog, platform_source_bindings,
                                                installed_platform_specifications, registered_catalog_proof)
    from src.modules.arduino_cli_support import prepare_unsupported_boards, publish_prepared_targets
    directory = Path(board_directory)
    if not directory.is_dir():
        raise ValueError("Downloaded board directory is unavailable")
    emit("preparing", message="Reading all downloaded board declarations", progress=None)
    records = board_catalog._parse_downloaded_arduino_board_files(directory, force_read=True)
    catalog = board_catalog._load_platformio_board_catalog(core, force_read=True)
    previous = _previous_plan(core)
    base = load_plan()
    bindings = platform_source_bindings(core, _merged_plan(base, previous))
    sources = installed_platform_specifications(core, catalog, bindings)
    registry = []
    registry_proof = None
    report = preparation_plan(records, package_metadata=package_metadata, catalog=catalog,
                              base_plan=base, previous_plan=previous, platform_sources=bindings,
                              installed_sources=sources)
    # Unknown/custom Arduino packages use their actual declarations, rather
    # than a finite list of MCU families. The explicit bootstrap process is
    # the only place allowed to contact PlatformIO's online registry.
    unresolved = any(row["platformio_support"] == "unknown" and row.get("arduino_id") for row in report["boards"])
    if unresolved and not _small_metadata(package_metadata).get("platformio"):
        emit("preparing", message="Matching downloaded declarations against the PlatformIO board catalog", progress=None)
        try:
            registry = fetch_preparation_catalog(core)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            report["registry_error"] = str(exc)[:1024]
            for row in report["boards"]:
                if row["platformio_support"] == "unknown" and row.get("arduino_id"):
                    row["reason"] += " Online catalog discovery was unavailable; retry preparation or provide the exact platform association."
        else:
            registry_proof = registered_catalog_proof(registry)
            report = preparation_plan(records, package_metadata=package_metadata, catalog=catalog,
                registry_catalog=registry, base_plan=base, previous_plan=previous,
                platform_sources=bindings, installed_sources=sources, registry_proof=registry_proof)
    needs_preparation = any(row["status"] == "preparation_required" for row in report["boards"])
    prior_definitions = _certified_definitions(core, report["boards"])
    already_prepared = ready(core, report["plan"]) and all(
        (row["platform"], row["board"]) in prior_definitions
        for row in report["boards"] if row["status"] == "preparation_required")
    if needs_preparation and not already_prepared:
        last_log = [0.0]
        log_lock = threading.Lock()
        def log_status(message):
            # Builder output is retained in its diagnostic files. Coalesce
            # chatter here so HDD/removable event storage gets at most four
            # writes per second; explicit stage/terminal events always pass.
            with log_lock:
                now = time.monotonic()
                if now - last_log[0] < 0.25:
                    return
                last_log[0] = now
                emit("preparing", message=str(message)[:2048], progress=None)
        record_by_source = {(str(record.get("source_file") or ""), str(record.get("arduino_id") or "")): record
                            for record in records}
        requested = [{"platform_spec": row.get("platform_spec"), "platform": row.get("platform"),
                      "board": row.get("board"), "record": record_by_source.get((row.get("source_file"), row["arduino_id"]))}
                     for row in report["boards"] if row["status"] == "preparation_required"]
        from src.modules.bootstrap_board_coverage import retained_board_sources
        prepare(core, report["plan"], log=log_status,
                jobs=jobs, event=emit, requested_targets=requested,
                board_sources=retained_board_sources(core, [directory]))
        if not ready(core, report["plan"]):
            raise RuntimeError("Offline preparation completed without a valid package readiness certificate")
    emit("refreshing", message="Checking every included board against installed definitions", progress=None)
    catalog = board_catalog._load_platformio_board_catalog(core, force_read=True)
    bindings = platform_source_bindings(core, report["plan"])
    sources = installed_platform_specifications(core, catalog, bindings)
    result = preparation_plan(records, package_metadata=package_metadata, catalog=catalog,
                              base_plan=report["plan"], platform_sources=bindings, installed_sources=sources)
    if report.get("registry_error"):
        result["registry_error"] = report["registry_error"]
    original_rows = {(row.get("source_file"), row["arduino_id"]): row for row in report["boards"]}
    for number, row in enumerate(result["boards"]):
        original = original_rows.get((row.get("source_file"), row["arduino_id"]), {})
        # Registry proof is only a classification for fallback. Ready always
        # uses the final installed byte-validated manifests, never registry rows.
        if original.get("platformio_support") == "unsupported":
            result["boards"][number] = dict(original)
        elif original.get("platformio_support") == "supported" and row["platformio_support"] == "unknown":
            row["platformio_support"] = "supported"
            row["reason"] = "PlatformIO declares this exact board, but its installed definition is unavailable; preparation requires repair."
        elif row["platformio_support"] == "unknown" and report.get("registry_error"):
            row["reason"] = original.get("reason") or row["reason"]
    certified = ready(core, result["plan"])
    verified_definitions = _certified_definitions(core, result["boards"])
    for row in result["boards"]:
        if row["status"] != "preparation_required":
            continue
        if certified and (row["platform"], row["board"]) in verified_definitions:
            row.update(status="ready", reason="Exact Arduino target and offline dependencies prepared")
        else:
            reason = "Exact board definition or its offline preparation certificate is missing or changed"
            if result.get("registry_error") and row["platformio_support"] == "unknown":
                reason += "; online catalog discovery was unavailable. Retry preparation or provide the exact platform association."
            row.update(status="unavailable", reason=reason)
    if any(row.get("platformio_support") == "unsupported" for row in result["boards"]):
        result["boards"] = prepare_unsupported_boards(core, directory, _small_metadata(package_metadata),
                                                    result["boards"], emit=emit, jobs=jobs)
    metadata = _small_metadata(package_metadata)
    if (not metadata.get("platformio") and
            ("frameworks" not in result["plan"] or "arduino" in result["plan"]["frameworks"])):
        from src.modules.arduino_cli_support import source_declaration_proof, prepare_source_boards
        # This is explicit online source preparation, independent of a failed
        # runtime build or an absence claim about PlatformIO. The exact installed
        # Arduino declaration bytes must match this archive before certification.
        requested_sources = []
        for row in result["boards"]:
            if row["status"] == "ready" or row.get("platformio_support") != "unknown":
                continue
            proof = source_declaration_proof(row, metadata)
            if proof:
                requested_sources.append(dict(row, arduino_backend_role="primary", arduino_source_proof=proof))
        if requested_sources:
            prepared_sources = prepare_source_boards(core, directory, metadata, requested_sources, emit=emit, jobs=jobs)
            replacements = {(row.get("source_file"), row.get("arduino_id")): row for row in prepared_sources}
            result["boards"] = [replacements.get((row.get("source_file"), row.get("arduino_id")), row)
                                for row in result["boards"]]
    publish_prepared_targets(core, directory, result["boards"])
    from src.modules.bootstrap_board_coverage import retained_board_sources
    from src.modules.offline_bootstrap import finalize_board_coverage
    finalize_board_coverage(core, retained_board_sources(core, [directory]), plan=result["plan"],
                            source_preparation=result, log=lambda message: emit("refreshing", message=message, progress=None))
    total = len(result["boards"])
    ready_count = sum(row["status"] == "ready" for row in result["boards"])
    arduino_cli_count = sum(row["status"] == "ready" and row.get("backend") == "arduino-cli" for row in result["boards"])
    unavailable = total - ready_count
    stage = "ready" if total and not unavailable else "unavailable"
    message = f"Prepared {ready_count} of {total} boards"
    if arduino_cli_count:
        message += f"; {arduino_cli_count} use their prepared Arduino compiler targets"
    if unavailable:
        message += f"; {unavailable} have no supported prepared target. See details."
    emit(stage, message=message, progress=100 if stage == "ready" else None,
         boards=result["boards"], ready_count=ready_count, unavailable_count=unavailable,
         arduino_cli_count=arduino_cli_count)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path)
    parser.add_argument("--boards", required=True, type=Path)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--package-request", required=True, type=Path)
    parser.add_argument("--events-root", type=Path)
    parser.add_argument("--jobs", type=int)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    from src.modules.private_python_guard import is_running_private_python
    if not is_running_private_python():
        raise RuntimeError("Board preparation requires MCU Flasher's private Python runtime")
    from src.modules.package_jobs import publish_event, package_store_lease, write_report

    def emit(stage, **details):
        coverage = details.pop("boards", None)
        if coverage is not None:
            report = {"schema": 1, "job_id": args.job_id, "boards": coverage,
                      "ready_count": details.get("ready_count", 0),
                      "unavailable_count": details.get("unavailable_count", 0),
                      "arduino_cli_count": details.get("arduino_cli_count", 0)}
            details["coverage_report"] = str(write_report(args.job_id, report, root=args.events_root))
        return publish_event(args.job_id, stage, root=args.events_root, **details)

    try:
        core = args.core if args.core is not None else _default_core()
        if args.package_request.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Board package metadata exceeds the supported 16 MB limit")
        metadata = _small_metadata(json.loads(args.package_request.read_text(encoding="utf-8")))
        emit("preparing", title=metadata["name"] or "Board preparation", message="Waiting for the package store", progress=None)
        with package_store_lease(core, mode="prepare", wait=True, root=args.events_root,
                                 on_wait=lambda: emit("queued", message="Waiting for active build or upload to finish", progress=None)):
            run_preparation(core, args.boards, package_metadata=metadata, emit=emit, jobs=args.jobs)
        return 0
    except Exception as exc:
        emit("failed", message=f"Board preparation failed: {exc}", progress=None)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
