"""Exact target evidence for user-added Arduino indexes.

Index URLs and package declarations are data. Registry access is restricted to
the explicit preparation worker; imports and workspace discovery stay offline.
"""
from __future__ import annotations

import json
import hashlib
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from datetime import datetime, timezone

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_REGISTRY_SPEC = re.compile(r"^(?:([A-Za-z0-9][A-Za-z0-9_.-]*)/)?([A-Za-z0-9][A-Za-z0-9_.-]*)(?:@([^\r\n]+))?$")
_STRONG_EVIDENCE = {"id", "name", "variant", "usb", "arduino-define"}


def normalize_platformio_configuration(value):
    """Validate the optional user-supplied PlatformIO association for an index.

    ``platform`` is an exact registry specification or HTTPS source, and
    ``board_ids`` maps Arduino declaration IDs to installed manifest IDs. Empty
    configuration enables automatic exact matching against the registry.
    """
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("PlatformIO configuration must be an object")
    specification = value.get("platform", "")
    if not isinstance(specification, str):
        raise ValueError("PlatformIO platform must be a specification")
    specification = specification.strip()
    if len(specification) > 2048 or any(ord(c) < 32 for c in specification):
        raise ValueError("Invalid PlatformIO platform specification")
    if specification:
        registry = _REGISTRY_SPEC.fullmatch(specification)
        if not registry:
            parsed = urlsplit(specification[4:] if specification.startswith("git+") else specification)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("Use a PlatformIO registry specification or an HTTPS platform source without credentials")
    mappings = value.get("board_ids", {})
    if not isinstance(mappings, dict) or len(mappings) > 20000:
        raise ValueError("PlatformIO board IDs must be an object of Arduino ID to PlatformIO ID")
    result = {}
    for arduino_id, board_id in mappings.items():
        if not isinstance(arduino_id, str) or not _IDENTIFIER.fullmatch(arduino_id):
            raise ValueError("Invalid Arduino declaration ID in PlatformIO board mapping")
        if not isinstance(board_id, str) or not _IDENTIFIER.fullmatch(board_id):
            raise ValueError("Invalid PlatformIO board ID in board mapping")
        result[arduino_id] = board_id
    if result and not specification:
        raise ValueError("Board ID mappings require an explicit PlatformIO platform")
    return {"platform": specification, "board_ids": result} if specification else {}


def normalize_index_url(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid Arduino board index URL")
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Arduino board index URL must be HTTP(S) without credentials or fragments")
    return value


def registry_platform_name(specification):
    match = _REGISTRY_SPEC.fullmatch(str(specification or ""))
    return match.group(2).casefold() if match else ""


def registry_platform_identity(specification):
    match = _REGISTRY_SPEC.fullmatch(str(specification or ""))
    if not match:
        return str(specification or "")
    return ((match.group(1) or "platformio") + "/" + match.group(2)).casefold()


def platform_source_bindings(core, plan=None):
    """Read certified source-to-platform names without guessing URL basenames."""
    from src.modules.offline_bootstrap import MARKER, SCHEMA
    bindings = {}
    try:
        path = Path(core) / MARKER
        if path.stat().st_size <= 16 * 1024 * 1024:
            certificate = json.loads(path.read_text(encoding="utf-8"))
            values = certificate.get("platform_sources", {}) if isinstance(certificate, dict) else {}
            if isinstance(certificate, dict) and certificate.get("schema") == SCHEMA and certificate.get("host") == sys.platform and isinstance(values, dict):
                for spec, name in values.items():
                    if isinstance(spec, str) and isinstance(name, str) and _IDENTIFIER.fullmatch(name):
                        bindings[spec] = name.casefold()
    except (OSError, ValueError, TypeError):
        pass
    # Plain registry specifications explicitly state the platform's real name.
    for specification in (plan or {}).get("platforms", []):
        name = registry_platform_name(specification)
        if name:
            bindings.setdefault(specification, name)
    return bindings


def installed_platform_specifications(core, catalog, bindings=None):
    """Recover exact installed source identities from PlatformIO's own metadata."""
    specifications = {name: spec for spec, name in (bindings or {}).items()}
    for row in catalog:
        name = str(row.get("platform") or "")
        if name in specifications or not _IDENTIFIER.fullmatch(name):
            continue
        try:
            path = Path(core) / "platforms" / name / ".piopm"
            if row.get("manifest"):
                directory = Path(row["manifest"]).parent.parent
                directory.resolve().relative_to((Path(core) / "platforms").resolve())
                path = directory / ".piopm"
            if path.stat().st_size > 65536:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            spec = data.get("spec")
            if not isinstance(spec, dict) or data.get("type") != "platform":
                continue
            if spec.get("uri"):
                candidate = spec["uri"]
            elif spec.get("name"):
                candidate = ((str(spec["owner"]) + "/") if spec.get("owner") else "") + str(spec["name"])
                if spec.get("requirements"):
                    candidate += "@" + str(spec["requirements"])
            else:
                continue
            normalized = normalize_platformio_configuration({"platform": candidate})
            if normalized:
                specifications[name] = normalized["platform"]
        except (OSError, ValueError, TypeError):
            continue
    return specifications


def fetch_preparation_catalog(core):
    """Read PlatformIO's online board catalog in one bounded bootstrap child.

    PlatformIO also returns installed entries; callers retain their own local
    manifests first. This operation never installs a development platform.
    """
    if os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME") or os.environ.get("MCU_FLASHER_WORKSPACE_RUNTIME"):
        raise RuntimeError("Online board discovery belongs to explicit preparation outside the workspace")
    from src.modules.offline_bootstrap import clean_bootstrap_environment
    environment = clean_bootstrap_environment()
    for key, folder in {"CORE": Path(core), "PLATFORMS": Path(core) / "platforms",
                        "PACKAGES": Path(core) / "packages", "CACHE": Path(core) / ".cache"}.items():
        environment[f"PLATFORMIO_{key}_DIR"] = str(folder)
    # The standard `boards` command silently suppresses network failures and
    # returns its installed subset. Query the registered catalog directly in a
    # separate private child so success means a complete registered catalog.
    result = subprocess.run([sys.executable, "-B", str(Path(__file__).absolute()), "--registry-catalog"], env=environment,
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=120, check=True,
                            creationflags=0x08000000 if sys.platform == "win32" else 0)
    if len(result.stdout.encode("utf-8")) > 16 * 1024 * 1024:
        raise ValueError("PlatformIO board catalog exceeds the supported 16 MB limit")
    values = json.loads(result.stdout)
    if not isinstance(values, list) or not values or len(values) > 100000:
        raise ValueError("PlatformIO returned an invalid board catalog")
    rows = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("PlatformIO returned an incomplete board catalog")
        platform, board_id = value.get("platform"), value.get("id")
        if not isinstance(platform, str) or not _IDENTIFIER.fullmatch(platform):
            raise ValueError("PlatformIO returned an invalid platform identity")
        if not isinstance(board_id, str) or not _IDENTIFIER.fullmatch(board_id):
            raise ValueError("PlatformIO returned an invalid board identity")
        frameworks = value.get("frameworks") or []
        if not isinstance(frameworks, (list, tuple)) or not all(isinstance(f, str) for f in frameworks):
            raise ValueError("PlatformIO returned invalid board frameworks")
        rows.append({**value, "frameworks": {f.casefold() for f in frameworks},
                     "platform_spec": "platformio/" + platform, "manifest": "",
                     "hwids": set(), "arduino_defines": set(), "catalog_source": "registry"})
    return rows


def registered_catalog_proof(catalog):
    """Bind an affirmative no-target result to one complete successful query."""
    content = [{"platform": row["platform"], "id": row["id"], "name": row.get("name", ""),
                "mcu": row.get("mcu", ""), "frameworks": sorted(row.get("frameworks") or [])}
               for row in catalog]
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"schema": 1, "kind": "registry-exact-target-absent", "catalog_sha256": digest,
            "catalog_count": len(catalog), "checked_at": datetime.now(timezone.utc).isoformat()}


def merge_catalogs(installed, registry):
    """Installed definition bytes take precedence over online brief records."""
    merged = {}
    for row in list(installed) + list(registry):
        key = (str(row.get("platform") or "").casefold(), str(row.get("id") or "").casefold())
        merged.setdefault(key, row)
    return list(merged.values())


def exact_arduino_match(record, candidates, features=None):
    """Retain the shared ambiguity margin and require actual identity evidence."""
    from main.core.board_catalog import _resolve_arduino_board_record
    match = _resolve_arduino_board_record(record, candidates, match_features=features)
    if match and _STRONG_EVIDENCE.intersection(match.get("match_reasons") or []):
        return match
    return None


def hardware_identity_present(record, candidates, candidate_features=None):
    """Presence/ambiguity in any framework prevents a false unsupported claim.

    Inspect identity independently of framework and MCU exclusions. A matching
    ID/name with conflicting MCU data is unresolved evidence, not proof that
    PlatformIO lacks this board. Brief registry records omit USB/variant data,
    so plausible cosmetic name differences also leave support unknown. This
    guard never selects a target; MCU similarity alone never constitutes it.
    """
    from main.core.board_catalog import _arduino_match_features, _board_name_tokens
    features = _arduino_match_features(record)
    record_tokens = _board_name_tokens(record.get("name")) - {"generic", "mcu", "microcontroller"}
    for candidate in candidates:
        other = candidate_features.get(id(candidate)) if candidate_features is not None else None
        if other is None:
            other = _arduino_match_features(candidate, candidate=True)
        if features["id"] and features["id"] == other["id"]:
            return True
        if features["name"] and features["name"] in (other["name"], other["name_without_vendor"]):
            return True
        if features["variant"] and features["variant"] == other["variant"]:
            return True
        if record.get("hwids") and candidate.get("hwids") and record["hwids"] & candidate["hwids"]:
            return True
        if features["build_define"] and features["build_define"] in other["defines"]:
            return True
        # A registered brief name may abbreviate "Development" to "Dev" or
        # omit a vendor prefix. Token equality/very high name similarity is
        # unresolved evidence even when MCU fields conflict or are absent.
        # Remove chip-only tokens so boards on one MCU remain independent.
        candidate_tokens = _board_name_tokens(f"{candidate.get('name') or ''} {candidate.get('vendor') or ''}") - {"generic", "mcu", "microcontroller"}
        chip_identities = {value for value in (features["normalized_mcu"], other["normalized_mcu"]) if value}
        def meaningful(tokens):
            return {token for token in tokens if not any(token == chip or (len(token) >= 4 and chip.startswith(token))
                                                       for chip in chip_identities)}
        left, right = meaningful(record_tokens), meaningful(candidate_tokens)
        if not left or not right:
            continue
        if left == right:
            return True
        name = features["name"]
        alternatives = (other["name"], other["name_without_vendor"])
        similarity = max((difflib.SequenceMatcher(None, name, alternative, autojunk=False).ratio()
                          for alternative in alternatives if alternative), default=0.0)
        if similarity >= 0.94:
            return True
        shared = len(left & right) / max(1, len(left | right))
        same_mcu = bool(features["normalized_mcu"] and features["normalized_mcu"] == other["normalized_mcu"])
        if same_mcu and shared >= 0.75 and similarity >= 0.88:
            return True
    return False


def explicit_board_match(record, board_id, candidates):
    """Validate an authorized ID mapping against a unique installed definition."""
    from main.core.board_catalog import _normalize_board_identity
    choices = [row for row in candidates if str(row.get("id") or "").casefold() == board_id.casefold()]
    if len(choices) != 1:
        return None
    match = choices[0]
    if record.get("mcu") and match.get("mcu") and _normalize_board_identity(record["mcu"]) != _normalize_board_identity(match["mcu"]):
        return None
    return {**match, "match_reasons": ["explicit-index-board-id"], "match_score": 1000.0}


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.modules.private_python_guard import is_running_private_python
    if (sys.argv[1:] != ["--registry-catalog"] or not is_running_private_python() or
            os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME") or os.environ.get("MCU_FLASHER_WORKSPACE_RUNTIME")):
        raise SystemExit("Registered board discovery requires the explicit private preparation runtime")
    from platformio.package.manager.platform import PlatformPackageManager
    # No installed fallback, no one-day cached subset, and no platform
    # installation. Only a fresh complete response can establish absence.
    client = PlatformPackageManager().get_registry_client_instance()
    print(json.dumps(client.fetch_json_data("get", "/v2/boards"), ensure_ascii=False))
