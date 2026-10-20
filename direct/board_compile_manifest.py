#!/usr/bin/env python3
"""Resolve isolated index declarations to unique, pinned PlatformIO targets."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from direct.board_compile_indexes import scratch_path, write_receipt
from main.core.board_catalog import (_arduino_match_features, _load_platformio_board_catalog,
                                     diagnose_arduino_board_record)

CPU_ARCHITECTURE = re.compile(r"^cortex-m[0-9]+(?:plus)?(?:\+[a-z0-9]+)*$", re.I)
SAM_CHIP_DEFINE = re.compile(r"(?:^|\s)-D\s*__(SAM[A-Z0-9]+)__(?=\s|$)")


def canonical_chip(value: str) -> str:
    """Normalize spelling, including Atmel's explicit AT[91]SAM chip prefix."""
    value = re.sub(r"[^a-z0-9]", "", str(value or "").casefold())
    return re.sub(r"^at(?:91)?(?=sam[a-z0-9])", "", value)


def cpu_architecture(value: str) -> str:
    value = str(value or "").strip().casefold()
    return "cortex-m0plus" if value == "cortex-m0+" else value


def merge_platform_catalog(catalog: list, core: Path) -> tuple[list, dict]:
    """Read actual board manifests only from the explicit isolated metadata core.

    The production reader is forced to read actual bytes. Snapshot each bounded
    manifest before/after it runs, so its fields and our hash/CPU receipt describe
    the same bytes. Do not read a live package store or generate readiness state.
    """
    core = scratch_path(core)
    if not core.is_dir() or core == ROOT / "temp" or core == ROOT / "audit/migration/scratch":
        raise ValueError("--platform-core requires an existing isolated metadata core below a scratch root")
    platforms = scratch_path(core / "platforms")
    if not platforms.is_dir():
        raise ValueError("Metadata core has no installed platforms directory")
    global_boards = scratch_path(core / "boards")
    if global_boards.exists() and any(global_boards.iterdir()):
        raise ValueError("Metadata core must contain installed platform definitions, not global board overrides")
    snapshots, platform_metadata = {}, {}
    for directory in sorted(platforms.iterdir()):
        directory = scratch_path(directory)
        if not directory.is_dir():
            continue
        metadata_path = scratch_path(directory / "platform.json")
        blob = metadata_path.read_bytes()
        if len(blob) > 1024 * 1024:
            raise ValueError("Platform metadata exceeds 1 MiB")
        metadata = json.loads(blob)
        board_folder = scratch_path(directory / "boards")
        platform_metadata[directory] = {"version": metadata.get("version"),
                                        "sha256": hashlib.sha256(blob).hexdigest()}
        if not board_folder.is_dir():
            continue
        for manifest in sorted(board_folder.glob("*.json")):
            manifest = scratch_path(manifest)
            raw = manifest.read_bytes()
            if len(raw) > 65536 or len(snapshots) >= 100000:
                raise ValueError("Installed board manifest exceeds audit bounds")
            if not isinstance(json.loads(raw), dict):
                raise ValueError("Installed board manifest must be a JSON object")
            snapshots[manifest] = raw
    loaded = _load_platformio_board_catalog(core, force_read=True)
    if len(loaded) != len(snapshots):
        raise ValueError("Installed catalog omitted or duplicated a board manifest; incomplete enrichment is refused")
    merged = {(str(row.get("platform") or ""), str(row.get("id") or "")): row for row in catalog}
    receipts, identities = [], set()
    for row in loaded:
        manifest = scratch_path(Path(row.get("manifest") or ""))
        blob = snapshots.get(manifest)
        if blob is None or manifest.read_bytes() != blob:
            raise ValueError("Installed board manifest changed while it was being inventoried")
        source = json.loads(blob)
        identity = (str(row.get("platform") or ""), str(row.get("id") or ""))
        if not all(identity) or identity in identities:
            raise ValueError("Installed metadata has a duplicate or missing exact platform/board identity")
        identities.add(identity)
        package = platform_metadata[manifest.parent.parent]
        platform_path = manifest.parent.parent / "platform.json"
        if hashlib.sha256(platform_path.read_bytes()).hexdigest() != package["sha256"]:
            raise ValueError("Installed platform metadata changed while it was being inventoried")
        receipt = {"catalog_source": "installed-manifest", "manifest": str(manifest.relative_to(ROOT)),
                   "board_manifest_sha256": hashlib.sha256(blob).hexdigest(),
                   "build_cpu": str((source.get("build") or {}).get("cpu") or ""),
                   "installed_platform_version": package["version"], "platform_manifest_sha256": package["sha256"]}
        merged[identity] = {**merged.get(identity, {}), **row, **receipt}
        receipts.append({"platform": identity[0], "board": identity[1], **receipt})
    return list(merged.values()), {"core": str(core.relative_to(ROOT)), "boards": len(receipts),
                                  "manifest_receipts_sha256": hashlib.sha256(json.dumps(receipts, sort_keys=True).encode()).hexdigest(),
                                  "read_only": True}


def declaration_record(raw: dict) -> tuple[dict, dict, str | None]:
    """Distinguish a compiler CPU architecture from an actual part number.

    No CPU-family to chip mapping is guessed. Only a literal SAM part define
    supplies a missing part number; every other Cortex declaration retains
    unknown chip identity and must also match an exact board ID/name below.
    Specific part-number disagreements remain hard refusals.
    """
    record = {**raw, "hwids": {tuple(value) for value in raw.get("hwids", [])}}
    original = str(raw.get("mcu") or "").strip()
    generic = bool(CPU_ARCHITECTURE.fullmatch(cpu_architecture(original)))
    evidence = {"original_mcu": original, "cpu_architecture": original if generic else None}
    chips = set()
    for key, value in raw.get("properties", {}).items():
        if key == "build.defines" or key == "build.extra_flags" or key.startswith("build.extra_flags."):
            chips.update(canonical_chip(match.group(1)) for match in SAM_CHIP_DEFINE.finditer(str(value)))
    if len(chips) > 1:
        return record, evidence, "Source declares conflicting literal SAM part-number macros"
    chip = next(iter(chips), None)
    if chip and original and not generic and canonical_chip(original) != chip:
        return record, evidence, "Specific source MCU conflicts with its literal SAM part-number macro"
    record["mcu"] = chip if generic and chip else "" if generic else canonical_chip(original)
    evidence.update(effective_mcu=record["mcu"], literal_sam_chip=chip)
    return record, evidence, None


def platform_pin(name: str) -> dict:
    from urllib.error import HTTPError
    from urllib.parse import urlencode

    identifier = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}"
    if not isinstance(name, str) or not re.fullmatch(identifier, name):
        raise ValueError("Invalid platform name")

    def fetch_json(url):
        with urlopen(Request(url, headers={"User-Agent": "MCU-Flasher-Compile-Audit/1"}), timeout=40) as response:
            blob = response.read(4 * 1024 * 1024 + 1)
        if len(blob) > 4 * 1024 * 1024:
            raise ValueError("Registry JSON exceeds 4 MiB")
        value = json.loads(blob)
        if not isinstance(value, dict):
            raise ValueError("Registry JSON must be an object")
        return value, hashlib.sha256(blob).hexdigest()

    owner = "platformio"
    official_url = f"https://api.registry.platformio.org/v3/packages/{owner}/platform/{name}"
    url = official_url
    resolution = {"method": "official-owner", "official_metadata_url": official_url}
    try:
        value, metadata_hash = fetch_json(url)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        exc.close()
        search_url = "https://api.registry.platformio.org/v3/search?" + urlencode({
            "query": f"type:platform name:{name}", "page": 1, "limit": 50})
        search, search_hash = fetch_json(search_url)
        total, items = search.get("total"), search.get("items")
        if (not isinstance(total, int) or isinstance(total, bool) or total < 0
                or not isinstance(items, list) or total != len(items) or len(items) > 50):
            raise ValueError("Registry owner search is incomplete or malformed")
        exact = [item for item in items if isinstance(item, dict)
                 and item.get("name") == name and item.get("type") == "platform"]
        owners = set()
        for item in exact:
            identity = item.get("owner")
            username = identity.get("username") if isinstance(identity, dict) else None
            if not isinstance(username, str) or not re.fullmatch(identifier, username):
                raise ValueError("Exact registry platform has an invalid owner identity")
            owners.add(username)
        if len(owners) != 1 or len(exact) != 1:
            raise ValueError("Registry search does not establish one unique exact platform owner")
        owner = owners.pop()
        # A contradictory result for the already-missing official owner cannot
        # authorize another fetch or silently select a different source.
        if owner == "platformio":
            raise ValueError("Registry search contradicts the missing official platform package")
        resolution = {"method": "unique-exact-registry-owner", "official_metadata_url": official_url,
                      "official_status": 404, "search_url": search_url, "search_sha256": search_hash,
                      "search_total": total, "exact_package_count": len(exact), "exact_owner_count": 1}
        url = f"https://api.registry.platformio.org/v3/packages/{owner}/platform/{name}"
        value, metadata_hash = fetch_json(url)
    identity = value.get("owner")
    actual_owner = identity.get("username") if isinstance(identity, dict) else None
    release = value.get("version")
    version = release.get("name") if isinstance(release, dict) else None
    if value.get("name") != name or value.get("type") != "platform" or actual_owner != owner:
        raise ValueError("Registry package identity differs from its exact owner/platform lookup")
    if not isinstance(version, str) or len(version) > 160:
        raise ValueError("Registry returned no exact platform version")
    stable = re.fullmatch(r"\d+(?:\.\d+){1,3}(?:\+[A-Za-z0-9.-]+)?", version)
    prerelease = re.fullmatch(r"\d+(?:\.\d+){1,3}-[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*(?:\+[A-Za-z0-9.-]+)?", version)
    if not stable and not prerelease:
        raise ValueError("Registry returned no exact safe platform release")
    return {"platform_spec": f"{owner}/{name}@{version}", "metadata_url": url,
            "metadata_sha256": metadata_hash, "version": version, "owner": owner,
            "experimental": bool(prerelease), "release_kind": "prerelease" if prerelease else "stable",
            "registry_tier": value.get("tier"), "owner_resolution": resolution}


def resolve(inventory: dict, catalog: list, pins: dict) -> dict:
    # Preserve exact manifest evidence when callers enrich the brief registry.
    # Registry rows normally have no variants, USB IDs or Arduino defines.
    rows = [{**row, "mcu": canonical_chip(row.get("mcu")),
             "frameworks": set(row.get("frameworks") or []),
             "hwids": {tuple(value) for value in row.get("hwids", [])},
             "arduino_defines": set(row.get("arduino_defines") or [])} for row in catalog]
    features = {id(row): _arduino_match_features(row, candidate=True) for row in rows}
    declarations, targets = [], {}
    strong = {"id", "name", "variant", "usb", "arduino-define"}
    for package in inventory.get("packages", []):
        source = {key: package.get(key) for key in ("index_url", "index_sha256", "package", "architecture",
                  "version", "archive_url", "declared_checksum")}
        if package.get("status") != "inventoried":
            declarations.append({"source": source, "status": "source_unavailable", "error": package.get("error")})
            continue
        for raw in package.get("declarations", []):
            record, normalization, problem = declaration_record(raw)
            # Full manifests can prove an architecture conflict even when the
            # source's build.mcu names a CPU family rather than a part number.
            compatible_rows = rows
            if normalization["cpu_architecture"]:
                compatible_rows = [row for row in rows if not (
                    row.get("catalog_source") == "installed-manifest" and row.get("build_cpu")
                    and cpu_architecture(row["build_cpu"]) != cpu_architecture(normalization["cpu_architecture"]))]
            diagnosis = diagnose_arduino_board_record(record, compatible_rows, match_features=features)
            receipt = {"source": source, "declaration": raw, "candidates": diagnosis["candidates"],
                       "resolution_evidence": normalization}
            match = diagnosis["match"]
            full_architecture = bool(match and match.get("catalog_source") == "installed-manifest"
                                     and match.get("board_manifest_sha256") and match.get("build_cpu")
                                     and cpu_architecture(match["build_cpu"]) == cpu_architecture(normalization["cpu_architecture"]))
            normalization["matching_installed_architecture"] = full_architecture
            if problem:
                receipt.update(status="unresolved_mapping", reason=problem)
            elif diagnosis["status"] == "ambiguous" or len(diagnosis["candidates"]) > 1:
                receipt["status"] = "ambiguous_mapping"
            elif not match or not strong.intersection(match.get("match_reasons", [])):
                receipt.update(status="unresolved_mapping", reason="No unique exact identity is established. Brief registry records omit variant, USB and Arduino-define evidence and cannot certify target absence.")
            elif not {"id", "name"}.intersection(match.get("match_reasons", [])):
                receipt.update(status="unresolved_mapping", reason="Shared CPU, chip, variant, USB IDs or Arduino defines do not establish this exact board. Require an exact board ID/name or a separately reviewed explicit association.")
            elif "arduino" not in match.get("frameworks", []):
                receipt["status"] = "no_compatible_arduino_framework"
            elif "platform_spec" not in pins.get(match["platform"], {}):
                receipt.update(status="platform_version_unavailable", platform=match["platform"],
                               error=pins.get(match["platform"], {}).get("error", "Exact version was not pinned"))
            elif (match.get("installed_platform_version") and pins[match["platform"]].get("version")
                  and str(match["installed_platform_version"]) != str(pins[match["platform"]]["version"])):
                receipt.update(status="platform_manifest_version_mismatch", platform=match["platform"],
                               reason="Installed manifest version differs from the pinned registry platform version")
            else:
                pin = pins[match["platform"]]
                key = (pin["platform_spec"], match["id"], "arduino")
                receipt.update(status="eligible", target={"platform_spec": key[0], "board": key[1], "framework": key[2]},
                               match_reasons=match["match_reasons"], match_score=match["match_score"])
                target = targets.setdefault(key, {**receipt["target"], "name": match.get("name"),
                                             "platform_pin": pin, "index_declarations": []})
                target["catalog_source"] = match.get("catalog_source", "registry")
                if match.get("board_manifest_sha256"):
                    target.update(expected_board_manifest_sha256=match["board_manifest_sha256"],
                                  expected_platform_manifest_sha256=match.get("platform_manifest_sha256"),
                                  build_cpu=match.get("build_cpu"), catalog_manifest=match.get("manifest"))
                target["index_declarations"].append({"source": source, "arduino_id": raw.get("arduino_id"),
                                                    "source_sha256": raw.get("source_sha256"),
                                                    "source_member": raw.get("source_member"),
                                                    "resolution_evidence": normalization,
                                                    "match_reasons": match["match_reasons"]})
            declarations.append(receipt)
    return {"schema": 1, "scope": "All uniquely matched Arduino declarations from the configured indexes; latest stable per source package/architecture",
            "policy": "Require exact ID/name identity and a single candidate within the resolver ambiguity margin; shared CPU, chip, variant, USB or defines alone never establish board equivalence. Preserve specific-part conflicts and never infer target absence from brief registry metadata; no family substitution or Arduino CLI fallback",
            "summary": {"unique_targets": len(targets), "declarations": dict(Counter(row["status"] for row in declarations))},
            "targets": [targets[key] for key in sorted(targets)], "declarations": declarations, "platform_pins": pins}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform-core", type=Path,
                        help="Read-only isolated scratch core with actual installed platform manifests")
    args = parser.parse_args(argv)
    inventory_path, registry_path, output = (scratch_path(value) for value in (args.inventory, args.registry, args.output))
    inventory_blob, registry_blob = inventory_path.read_bytes(), registry_path.read_bytes()
    if len(inventory_blob) > 64 * 1024 * 1024 or len(registry_blob) > 16 * 1024 * 1024:
        parser.error("Inventory or registry exceeds the supported input bound")
    inventory, registry = json.loads(inventory_blob), json.loads(registry_blob)
    enrichment = None
    if args.platform_core is not None:
        registry, enrichment = merge_platform_catalog(registry, args.platform_core)
    # Find required platforms first; resolution itself is offline and never writes settings.
    probe = resolve(inventory, registry, {})
    names = sorted({row["platform"] for row in probe["declarations"] if row["status"] == "platform_version_unavailable"})
    def fetch(name):
        try:
            return name, platform_pin(name)
        except Exception as exc:
            return name, {"error": f"{type(exc).__name__}: {exc}"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        pins = dict(pool.map(fetch, names))
    result = resolve(inventory, registry, pins)
    result.update(inventory_sha256=hashlib.sha256(inventory_blob).hexdigest(),
                  registry_sha256=hashlib.sha256(registry_blob).hexdigest(),
                  registry_boards=len(registry), source_indexes=inventory.get("indexes", []))
    if enrichment is not None:
        result["platform_manifest_enrichment"] = enrichment
    write_receipt(output, result)
    print(json.dumps(result["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
