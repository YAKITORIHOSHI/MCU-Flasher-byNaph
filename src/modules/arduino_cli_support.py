"""Prepared exact Arduino source targets and verified PlatformIO-absence fallback.

Online core installation runs in the separate board preparation process. The
workspace consumes byte-checked, app-owned certificates and never installs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

TARGETS_FILE = ".mcu-index-targets.json"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,159}$")
_HEX = re.compile(r"^[a-f0-9]{64}$")


def _core_directory(core=None):
    if core is None:
        from src.modules.package_jobs import package_core_directory
        core = package_core_directory()
    return Path(core)


def _missing_cli_message():
    if sys.platform.startswith("linux"):
        return ("Native Arduino CLI is unavailable. Prepare it in Ubuntu Bootstrap while online: "
                "bash direct/ubuntu/run.sh --repair. Then choose Prepare board support again")
    return "Arduino CLI is unavailable. Repair Bootstrap, then choose Prepare board support again"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(131072), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _within(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def unsupported_proof(row):
    """A failed matcher alone is never permission to switch compilers."""
    proof = row.get("platformio_support_proof")
    return bool(row.get("platformio_support") == "unsupported" and isinstance(proof, dict)
                and proof.get("schema") == 1 and proof.get("kind") == "registry-exact-target-absent"
                and isinstance(proof.get("catalog_count"), int) and proof["catalog_count"] > 0
                and _HEX.fullmatch(str(proof.get("catalog_sha256") or ""))
                and isinstance(proof.get("checked_at"), str) and proof["checked_at"])


def source_declaration_proof(record, metadata):
    """Plan an original Arduino target from caller-verified source metadata.

    The bootstrap caller validates the downloaded source receipt. Planning is
    not readiness: preparation must also verify matching installed declaration
    bytes, exact core/version and a successful compile of this original FQBN.
    """
    if not isinstance(record, dict) or not isinstance(metadata, dict):
        return {}
    package, architecture, version = (str(metadata.get(key) or "") for key in ("package", "architecture", "version"))
    identifier = str(record.get("arduino_id") or record.get("arduino_board_id") or "")
    digest = record.get("source_sha256") or record.get("arduino_source_sha256")
    if (not _IDENTIFIER.fullmatch(package) or not _IDENTIFIER.fullmatch(architecture)
            or not _VERSION.fullmatch(version) or not _IDENTIFIER.fullmatch(identifier)
            or not isinstance(digest, str) or not _HEX.fullmatch(digest)):
        return {}
    index_url = metadata.get("index_url") or ""
    if not isinstance(index_url, str):
        return {}
    if index_url:
        try:
            parsed = urlsplit(index_url)
            if (parsed.scheme not in ("http", "https") or not parsed.netloc
                    or parsed.username or parsed.password or parsed.fragment):
                return {}
        except ValueError:
            return {}
    return {"schema": 1, "kind": "arduino-source-declaration", "core": f"{package}:{architecture}",
            "version": version, "index_url": index_url, "source_sha256": digest,
            "fqbn": f"{package}:{architecture}:{identifier}"}


def source_target_proof(row):
    """Recognize primary source identity without claiming PlatformIO absence."""
    proof = row.get("arduino_source_proof")
    if row.get("arduino_backend_role") != "primary" or not isinstance(proof, dict):
        return False
    parts = str(proof.get("core") or "").split(":")
    if len(parts) != 2:
        return False
    expected = source_declaration_proof(row, {"package": parts[0], "architecture": parts[1],
        "version": proof.get("version"), "index_url": proof.get("index_url")})
    return bool(expected and proof == expected
                and row.get("arduino_fqbn", expected["fqbn"]) == expected["fqbn"])


def source_namespace_proof(row):
    """Recognize prior source intent without authorizing current source bytes."""
    proof = row.get("arduino_source_proof")
    return bool(isinstance(proof, dict) and source_target_proof(dict(row, source_sha256=proof.get("source_sha256"))))


def prepare_source_boards(core, directory, metadata, rows, *, emit, jobs=None):
    """Proactively prepare original declarations from a verified source receipt.

    The caller supplies only declarations without a unique PlatformIO mapping.
    Existing ready/explicit PlatformIO associations retain their backend. No
    runtime matching failure invokes this online preparation operation.
    """
    planned = [dict(row) for row in rows]
    for row in planned:
        if (row.get("platformio_support") == "supported" or row.get("explicit_board_id")
                or row.get("status") == "ready" and row.get("backend") == "platformio"):
            continue
        proof = source_declaration_proof(row, metadata)
        if not proof or row.get("arduino_source_proof") not in (None, proof):
            row.update(status="unavailable", reason="The original Arduino source declaration is unverified. Prepare board support again.")
            continue
        row.update(arduino_backend_role="primary", arduino_source_proof=proof)
    return prepare_unsupported_boards(core, directory, metadata, planned, emit=emit, jobs=jobs)


def _cli_environment(*, online=False):
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith("ARDUINO_"):
            environment.pop(key, None)
    if online:
        for key in ("MCU_FLASHER_OFFLINE_RUNTIME", "MCU_FLASHER_WORKSPACE_RUNTIME", "PIP_NO_INDEX"):
            environment.pop(key, None)
    else:
        # CLI startup must not fetch missing indexes/builtin tools either.
        # An incomplete prepared store fails locally instead of going online.
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            environment[key] = "http://127.0.0.1:9"
        environment["NO_PROXY"] = environment["no_proxy"] = "localhost,127.0.0.1,::1"
        environment["ARDUINO_NETWORK_PROXY"] = "http://127.0.0.1:9"
        environment["ARDUINO_NETWORK_CONNECTION_TIMEOUT"] = "3s"
    return environment


def _run_json(command, *, timeout=120, online=False, parse=True):
    result = subprocess.run(command, env=_cli_environment(online=online), capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout,
                            creationflags=0x08000000 if sys.platform == "win32" else 0)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or f"Arduino CLI exited with {result.returncode}")[-2000:])
    if len(result.stdout.encode("utf-8")) > 16 * 1024 * 1024:
        raise ValueError("Arduino CLI result exceeds the supported size")
    return json.loads(result.stdout) if parse else None


def _source_store(core, proof):
    """Isolate exact source versions and package-index namespaces."""
    identity = {key: proof[key] for key in ("core", "version", "index_url")}
    # Keep native compiler paths short; certificates retain the full identity.
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    return Path(core) / "arduino-cli/targets" / key


def _configuration(core, index_url, *, store=None):
    store = Path(store) if store is not None else Path(core) / "arduino-cli"
    config = store / "arduino-cli.yaml"
    if (not _within(store, core) or not _within(config, store)
            or any(not _within(store / name, store) for name in ("data", "downloads", "user"))):
        raise ValueError("Arduino CLI configuration escapes its app-owned store")
    for folder in (store / "data", store / "downloads", store / "user"):
        folder.mkdir(parents=True, exist_ok=True)
    urls = []
    if config.is_file():
        # Our YAML is JSON-compatible and contains no user configuration.
        try:
            previous = json.loads(config.read_text(encoding="utf-8"))
            urls = previous.get("board_manager", {}).get("additional_urls", [])
            if not isinstance(urls, list):
                urls = []
        except (ValueError, OSError, TypeError):
            pass
    if index_url and index_url not in urls:
        urls.append(index_url)
    # Arduino CLI's Windows hardware-platform scanner cannot reliably walk
    # packages when these directories are reached through our short-path
    # junction. Keep the config file/commands on the short app path, but give
    # CLI the canonical physical paths for its own directories.
    directories = {name: str((store / name).resolve()) for name in ("data", "downloads", "user")}
    payload = {"directories": directories,
               "board_manager": {"additional_urls": urls}, "metrics": {"enabled": False},
               "updater": {"enable_notification": False}}
    _atomic_json(config, payload)
    return config


def _store_configuration_valid(store):
    try:
        config = Path(store) / "arduino-cli.yaml"
        if not _within(config, store) or config.stat().st_size > 1024 * 1024:
            return False
        payload = json.loads(config.read_text(encoding="utf-8"))
        directories = payload.get("directories", {})
        return isinstance(directories, dict) and set(directories) == {"data", "downloads", "user"} and all(
                   _within(Path(store) / key, store)
                   and Path(str(directories.get(key) or "")).resolve() == (Path(store) / key).resolve()
                   for key in ("data", "downloads", "user"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _reported_installed_core_version(item):
    """Read the installed version across Arduino CLI's JSON schema variants.

    Recent releases report ``installed_version`` on the platform summary.
    Older/alternate payloads may instead include an ``installed`` flag and
    version, or mark the installed release inside ``releases``. In particular,
    never stringify a boolean ``installed`` flag as though it were a version.
    """
    installed_version = item.get("installed_version")
    if isinstance(installed_version, str) and installed_version.strip():
        return installed_version.strip()

    installed = item.get("installed")
    if isinstance(installed, str) and installed.strip():
        return installed.strip()
    if installed is not False:
        legacy_version = item.get("version")
        if isinstance(legacy_version, str) and legacy_version.strip():
            return legacy_version.strip()

    releases = item.get("releases")
    if isinstance(releases, dict):
        installed_releases = []
        for release_key, release in releases.items():
            if not isinstance(release, dict) or release.get("installed") is not True:
                continue
            release_version = release.get("version")
            if not isinstance(release_version, str) or not release_version.strip():
                release_version = release_key
            if isinstance(release_version, str) and release_version.strip():
                installed_releases.append(release_version.strip())
        if len(installed_releases) == 1:
            return installed_releases[0]
    return ""


def _verify_core_version(command, core_id, version, *, online=False):
    payload = _run_json(command + ["core", "list"], timeout=120, online=online)
    platforms = payload.get("platforms") if isinstance(payload, dict) else payload
    if not isinstance(platforms, list):
        raise RuntimeError("Arduino CLI did not confirm its installed core inventory")
    matches = [item for item in platforms if isinstance(item, dict) and item.get("id") == core_id]
    installed_version = _reported_installed_core_version(matches[0]) if len(matches) == 1 else ""
    if len(matches) != 1 or installed_version != version:
        reported = ", ".join(
            f"{item.get('id') or '?'}@{_reported_installed_core_version(item) or 'unknown'}"
            for item in platforms[:12] if isinstance(item, dict)
        ) or "none"
        raise RuntimeError(
            f"Arduino CLI did not confirm exact installed core {core_id}@{version}"
            f" (reported: {reported[:500]})"
        )


def _atomic_json(path, payload):
    _atomic_bytes(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def _atomic_bytes(path, payload):
    if len(payload) > 16 * 1024 * 1024:
        raise ValueError("Prepared board metadata exceeds the supported 16 MB limit")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("rb") as previous:
            if previous.read(len(payload) + 1) == payload:
                return
    except FileNotFoundError:
        pass
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _tool_inventory(core, details, *, store=None, hardware=None):
    """Check installed tools and the selected hardware core's build inputs."""
    core = Path(core)
    package_root = (Path(store) if store is not None else core / "arduino-cli") / "data/packages"
    dependencies = details.get("tools_dependencies") or []
    roots = []
    if dependencies:
        for item in dependencies:
            if not isinstance(item, dict) or not all(_IDENTIFIER.fullmatch(str(item.get(key) or "")) for key in ("packager", "name")) or not _VERSION.fullmatch(str(item.get("version") or "")):
                raise ValueError("Arduino CLI returned an invalid tool dependency")
            path = package_root / item["packager"] / "tools" / item["name"] / item["version"]
            if not path.is_dir():
                raise RuntimeError(f"Arduino CLI tool dependency is missing: {item['name']}")
            roots.append(path)
    else:
        # Older CLI releases omit this field. Inspect the app-owned tools only.
        roots = [path for path in package_root.glob("*/tools/*/*") if path.is_dir()]
    # CLI initialization also consumes shared builtin tools, which board
    # details can omit from the core's compiler/uploader dependencies.
    roots.extend(path for path in (package_root / "builtin/tools").glob("*/*") if path.is_dir())
    if not roots:
        raise RuntimeError("No installed Arduino CLI compiler/uploader tools were found")
    if hardware is not None:
        roots.append(Path(hardware))
    inventory = {}
    seen_roots = set()
    core_bytes = 0
    core_suffixes = {".h", ".hpp", ".hh", ".hxx", ".c", ".cpp", ".cc", ".cxx", ".ino", ".s",
                     ".asm", ".inc", ".ipp", ".tpp", ".ld", ".csv", ".bin", ".txt", ".properties",
                     ".json", ".py", ".sh", ".bat", ".cmd", ".cmake", ".mk", ".yaml", ".yml"}
    for folder in roots:
        if not _within(folder, package_root):
            raise ValueError("Arduino tool path escapes its prepared store")
        identity = os.path.normcase(str(folder.resolve()))
        if identity in seen_roots:
            continue
        seen_roots.add(identity)
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            if not _within(path, package_root):
                raise ValueError("Arduino tool path escapes its prepared store")
            stat = path.stat()
            entry = {"size": stat.st_size}
            core_input = hardware is not None and _within(path, hardware) and path.suffix.casefold() in core_suffixes
            if core_input:
                core_bytes += stat.st_size
                if core_bytes > 256 * 1024 * 1024:
                    raise ValueError("Arduino hardware core source inventory exceeds supported byte bounds")
            # Core source, variant and recipe bytes influence the firmware.
            # Unused SDK archives retain bounded size/existence checks.
            if core_input or path.suffix.casefold() in (".exe", ".dll", ".so", ".dylib", ".py", ".sh", ".bat", ".cmd", ".json") or (sys.platform != "win32" and stat.st_mode & 0o111):
                entry["sha256"] = sha256_file(path)
            inventory[str(path.relative_to(core))] = entry
            if len(inventory) > 50000:
                raise ValueError("Arduino CLI tool inventory exceeds the supported limit")
    if not inventory:
        raise RuntimeError("No installed Arduino CLI compiler/uploader tools were found")
    return inventory


def prepare_unsupported_boards(core, directory, metadata, rows, *, emit, jobs=None):
    """Prepare source-primary rows or legacy definitively unsupported rows.

    Returns independent row copies; failures retain ready PIO results and expose
    the fallback preparation reason. The caller holds the package-store lease.
    """
    prepared = [dict(row) for row in rows]
    targets = [row for row in prepared if source_target_proof(row) or unsupported_proof(row)]
    if not targets:
        return prepared
    if os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME") or os.environ.get("MCU_FLASHER_WORKSPACE_RUNTIME"):
        raise RuntimeError("Arduino core preparation belongs to the separate online worker")
    notice = ("Preparing original Arduino source targets with Arduino CLI. Board identities and vendor defaults are preserved."
              if any(source_target_proof(row) for row in targets) else
              "PlatformIO does not support these exact boards yet; preparing Arduino CLI fallback.")
    emit("preparing", message=notice, progress=None, fallback_boards=[row.get("name") for row in targets])
    try:
        if jobs is not None and (not isinstance(jobs, int) or isinstance(jobs, bool) or jobs <= 0):
            raise ValueError("Arduino CLI compiler jobs must be a positive integer")
        from main.core.build_resources import get_optimal_compiler_jobs
        safe_jobs = get_optimal_compiler_jobs(storage_paths=(Path(core), Path(directory)), storage_wait=True)
        total_jobs = min(jobs or safe_jobs, safe_jobs)
        package, architecture, version = (str(metadata.get(key) or "") for key in ("package", "architecture", "version"))
        if not _IDENTIFIER.fullmatch(package) or not _IDENTIFIER.fullmatch(architecture) or not _VERSION.fullmatch(version):
            raise ValueError("The Arduino index has no valid exact package, architecture and version")
        index_url = str(metadata.get("index_url") or "")
        if index_url:
            parsed = urlsplit(index_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("The Arduino index URL is invalid")
        from main.core.toolchain import find_arduino_cli_executable
        cli = find_arduino_cli_executable()
        if not cli or not Path(cli).is_file():
            raise RuntimeError(_missing_cli_message())
        primary_rows = [row for row in targets if source_target_proof(row)]
        store = _source_store(core, primary_rows[0]["arduino_source_proof"]) if primary_rows else Path(core) / "arduino-cli"
        if primary_rows and any(_source_store(core, row["arduino_source_proof"]) != store for row in primary_rows):
            raise ValueError("Arduino primary preparation requires one exact source core version and index")
        config = _configuration(core, index_url, store=store)
        command = [str(cli), "--config-file", str(config), "--json"]
        emit("downloading", message=f"Preparing Arduino CLI core {package}:{architecture}@{version}…", progress=None)
        _run_json(command + ["core", "update-index"], timeout=300, online=True, parse=False)
        _run_json(command + ["core", "install", f"{package}:{architecture}@{version}"], timeout=1800, online=True, parse=False)
        _verify_core_version(command, f"{package}:{architecture}", version, online=True)
        platform_dir = store / "data/packages" / package / "hardware" / architecture / version
        proof_paths = [platform_dir / name for name in ("boards.txt", "platform.txt")]
        if not all(path.is_file() for path in proof_paths):
            raise RuntimeError("Arduino CLI did not install the exact declared core")
        proof_files = {str(path.relative_to(Path(core))): sha256_file(path) for path in proof_paths}
        installed_source_digest = proof_files[str(proof_paths[0].relative_to(Path(core)))]
        source_digests = {}
        for row in targets:
            if not source_target_proof(row):
                continue
            source = str(row.get("source_file") or "")
            if source and source not in source_digests:
                try:
                    source_digests[source] = sha256_file(source)
                except Exception as exc:
                    source_digests[source] = exc

        worker_count = max(1, min(total_jobs, len(targets)))
        compiler_jobs = max(1, total_jobs // worker_count)
        emit("verifying", message=(f"Checking {len(targets)} Arduino fallback targets in parallel "
                                   f"({worker_count} workers; up to {compiler_jobs} compiler jobs each)…"),
             progress=0)
        probes = Path(core) / "arduino-cli/probes"
        probes.mkdir(parents=True, exist_ok=True)
        cli_digest = sha256_file(cli)
        cli_path = str(Path(cli).resolve())
        tool_inventory_cache = {}
        tool_inventory_lock = threading.Lock()

        def prepare_target(row):
            row = dict(row)
            arduino_id = str(row.get("arduino_id") or "")
            if not _IDENTIFIER.fullmatch(arduino_id):
                row.update(status="unavailable", reason="The Arduino declaration has no valid exact board ID")
                return row
            fqbn = f"{package}:{architecture}:{arduino_id}"
            try:
                primary = source_target_proof(row)
                if primary:
                    proof = row["arduino_source_proof"]
                    if proof != source_declaration_proof(row, metadata) or proof["fqbn"] != fqbn:
                        raise RuntimeError("The original Arduino source metadata changed before preparation")
                    source = str(row.get("source_file") or "")
                    if not source or not _within(source, directory):
                        raise RuntimeError("The original Arduino declaration escapes its verified source folder")
                    observed_digest = source_digests.get(source)
                    if isinstance(observed_digest, Exception):
                        raise RuntimeError(f"The original Arduino declaration could not be read: {observed_digest}")
                    if observed_digest != proof["source_sha256"]:
                        raise RuntimeError("The original Arduino board declarations changed before preparation")
                    if installed_source_digest != proof["source_sha256"]:
                        raise RuntimeError("The exact installed Arduino core does not match the original source declaration bytes")
                details = _run_json(command + ["board", "details", "--fqbn", fqbn], timeout=120, online=True)
                if not isinstance(details, dict) or details.get("fqbn") != fqbn:
                    raise RuntimeError("Arduino CLI did not confirm the exact requested board")
                with tempfile.TemporaryDirectory(prefix="target-", dir=probes) as probe_directory:
                    sketch = Path(probe_directory) / "Probe"
                    sketch.mkdir()
                    (sketch / "Probe.ino").write_text("void setup() {}\nvoid loop() {}\n", encoding="utf-8")
                    _run_json(command + ["compile", "--fqbn", fqbn, "--build-path", str(Path(probe_directory) / "build"),
                                         "--jobs", str(compiler_jobs), str(sketch)], timeout=900, online=True, parse=False)
                dependency_key = json.dumps(details.get("tools_dependencies") or [], sort_keys=True,
                                            separators=(",", ":"), ensure_ascii=False)
                with tool_inventory_lock:
                    tool_files = tool_inventory_cache.get(dependency_key)
                    if tool_files is None:
                        tool_files = _tool_inventory(core, details, store=store, hardware=platform_dir)
                        tool_inventory_cache[dependency_key] = tool_files
                certificate = {"schema": 1, "host": sys.platform, "core": f"{package}:{architecture}",
                               "version": version, "proof_files": proof_files, "tool_files": tool_files,
                               "cli_sha256": cli_digest, "cli_path": cli_path, "fqbn": fqbn,
                               "store": str(store.relative_to(Path(core)))}
                if primary:
                    certificate.update(arduino_backend_role="primary", arduino_source_proof=dict(row["arduino_source_proof"]))
                certificate_bytes = json.dumps(certificate, sort_keys=True).encode("utf-8")
                certificate_digest = hashlib.sha256(certificate_bytes).hexdigest()
                certificate_path = Path(core) / "arduino-cli/certificates" / (certificate_digest + ".json")
                _atomic_bytes(certificate_path, certificate_bytes)
                # Board menu choices are deliberately left at vendor defaults.
                row.update(status="ready", backend="arduino-cli", platform=f"{package}:{architecture}", board=arduino_id, arduino_fqbn=fqbn,
                           arduino_cli={"fqbn": fqbn, "core": f"{package}:{architecture}", "version": version,
                                        "certificate": str(certificate_path.relative_to(Path(core))),
                                        "certificate_sha256": certificate_digest, "cli_sha256": certificate["cli_sha256"],
                                        "cli_path": certificate["cli_path"], "store": certificate["store"]},
                           require_upload_port=True,
                           reason=("Ready through the original Arduino source target using Arduino CLI; vendor board defaults are preserved."
                                   if primary else "PlatformIO does not support this exact board yet. Ready through Arduino CLI."))
            except Exception as exc:
                prefix = "Original Arduino source target preparation failed" if source_target_proof(row) else "PlatformIO does not support this exact board yet. Arduino CLI preparation failed"
                row.update(status="unavailable", reason=f"{prefix}: {exc}")
            return row

        result_index = {id(row): index for index, row in enumerate(prepared)}
        completed = 0
        report_interval = max(1, len(targets) // 20)
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="arduino-target") as executor:
            futures = {executor.submit(prepare_target, row): (result_index[id(row)], row) for row in targets}
            for future in as_completed(futures):
                index, original = futures[future]
                try:
                    prepared[index] = future.result()
                except Exception as exc:
                    prefix = "Original Arduino source target preparation failed" if source_target_proof(original) else "PlatformIO does not support this exact board yet. Arduino CLI preparation failed"
                    prepared[index].update(status="unavailable", reason=f"{prefix}: {exc}")
                completed += 1
                if completed % report_interval == 0 or completed == len(targets):
                    emit("verifying", message=f"Arduino CLI fallback checks completed: {completed}/{len(targets)}.",
                         progress=round(completed * 100 / len(targets), 1))
    except Exception as exc:
        for row in targets:
            prefix = "Original Arduino source target preparation failed" if source_target_proof(row) else "PlatformIO does not support this exact board yet. Arduino CLI preparation failed"
            row.update(status="unavailable", reason=f"{prefix}: {exc}")
    return prepared


def publish_prepared_targets(core, directory, rows):
    """Atomically replace one downloaded source's proofs, retaining other cores."""
    core = _core_directory(core)
    path = core / TARGETS_FILE
    values = load_prepared_targets(core)
    source_root = Path(directory).resolve()
    # Changed/unavailable targets invalidate prior certificates for this source.
    values = [row for row in values if not _within(row.get("source_file", ""), source_root)]
    for item in rows:
        source_file = str(item.get("source_file") or "")
        arduino_id = str(item.get("arduino_id") or "")
        primary_intent = source_namespace_proof(item) and item.get("status") == "unavailable"
        if (not source_file or not _within(source_file, source_root) or not _IDENTIFIER.fullmatch(arduino_id)
                or item.get("status") != "ready" and not primary_intent):
            continue
        if item.get("backend") == "arduino-cli" and not (source_target_proof(item) or unsupported_proof(item) or primary_intent):
            continue
        try:
            source_digest = item.get("source_sha256")
            if not isinstance(source_digest, str) or not _HEX.fullmatch(source_digest):
                item.update(status="unavailable", reason="The exact parsed board declaration receipt is missing. Prepare board support again.")
                continue
            if sha256_file(source_file) != source_digest:
                item.update(status="unavailable", reason="Board declarations changed during preparation. Prepare board support again.")
                continue
            if item.get("backend") != "arduino-cli" and not primary_intent:
                manifest = str(item.get("manifest") or "")
                manifest_digest = item.get("manifest_sha256")
                if (not manifest or not _within(manifest, core / "platforms")
                        or not isinstance(manifest_digest, str) or not _HEX.fullmatch(manifest_digest)
                        or sha256_file(manifest) != manifest_digest):
                    item.update(status="unavailable", reason="The verified PlatformIO board definition changed or is unavailable. Prepare board support again.")
                    continue
            row = {key: value for key, value in item.items() if key in {
                "name", "arduino_id", "source_file", "platform", "board", "platform_spec", "match_reasons",
                "manifest", "manifest_sha256", "backend", "arduino_fqbn", "arduino_cli",
                "arduino_backend_role", "arduino_source_proof",
                "platformio_support", "platformio_support_proof", "status", "require_upload_port", "reason", "source_sha256"}}
            if primary_intent:
                row.update(backend="arduino-cli", arduino_fqbn=item["arduino_source_proof"]["fqbn"])
            row["source_file"] = str(Path(source_file).resolve())
            row.setdefault("backend", "platformio")
            values.append(row)
        except OSError as exc:
            item.update(status="unavailable", reason=f"Board declarations could not be verified: {exc}. Prepare board support again.")
            continue
    if len(values) > 50000:
        raise ValueError("Too many prepared board associations")
    _atomic_json(path, {"schema": 1, "host": sys.platform, "rows": values})


def load_prepared_targets(core=None):
    try:
        path = _core_directory(core) / TARGETS_FILE
        if path.stat().st_size > 16 * 1024 * 1024:
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("rows")
        if payload.get("schema") != 1 or payload.get("host") != sys.platform or not isinstance(rows, list) or len(rows) > 50000:
            return []
        return [row for row in rows if isinstance(row, dict)]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def prepared_target_index(rows):
    index = {}
    for row in rows:
        if row.get("status") == "ready":
            key = (os.path.normcase(os.path.abspath(str(row.get("source_file") or ""))), str(row.get("arduino_id") or ""))
            index.setdefault(key, []).append(row)
    return index


def planned_source_target_index(rows):
    index = {}
    for row in rows:
        if source_namespace_proof(row):
            key = (os.path.normcase(os.path.abspath(str(row.get("source_file") or ""))),
                   str(row.get("arduino_id") or ""))
            index.setdefault(key, []).append(row)
    return index


def planned_source_target_for_record(record, *, core=None, rows=None, validation_cache=None):
    """Retain an original source namespace without claiming compiler readiness."""
    source = str(record.get("source_file") or record.get("arduino_source_file") or "")
    identifier = str(record.get("arduino_id") or record.get("arduino_board_id") or "")
    parsed_digest = record.get("source_sha256") or record.get("arduino_source_sha256")
    if not source or not identifier or not isinstance(parsed_digest, str) or not _HEX.fullmatch(parsed_digest):
        return None
    source_key = os.path.normcase(os.path.abspath(source))
    values = planned_source_target_index(load_prepared_targets(core)) if rows is None else rows
    if isinstance(values, dict):
        values = values.get((source_key, identifier), [])
    choices = [row for row in values if source_target_proof(row)
               and os.path.normcase(os.path.abspath(str(row.get("source_file") or ""))) == source_key
               and str(row.get("arduino_id") or "") == identifier
               and row.get("source_sha256") == parsed_digest]
    if len(choices) != 1:
        return None
    try:
        key = ("file-sha256", source)
        digest = validation_cache.get(key) if validation_cache is not None else None
        if digest is None:
            digest = sha256_file(source)
            if validation_cache is not None:
                validation_cache[key] = digest
        return dict(choices[0]) if digest == parsed_digest else None
    except (OSError, ValueError, TypeError):
        return None


def source_namespace_target_for_record(record, *, core=None, rows=None, validation_cache=None):
    """Block namespace substitution after source edits, without reusing old proof."""
    source = str(record.get("source_file") or record.get("arduino_source_file") or "")
    identifier = str(record.get("arduino_id") or record.get("arduino_board_id") or "")
    parsed_digest = record.get("source_sha256") or record.get("arduino_source_sha256")
    if not source or not identifier or not isinstance(parsed_digest, str) or not _HEX.fullmatch(parsed_digest):
        return None
    source_key = os.path.normcase(os.path.abspath(source))
    values = planned_source_target_index(load_prepared_targets(core)) if rows is None else rows
    if isinstance(values, dict):
        values = values.get((source_key, identifier), [])
    choices = [row for row in values if source_namespace_proof(row)
               and os.path.normcase(os.path.abspath(str(row.get("source_file") or ""))) == source_key
               and str(row.get("arduino_id") or "") == identifier]
    if len(choices) != 1:
        return None
    try:
        key = ("file-sha256", source)
        digest = validation_cache.get(key) if validation_cache is not None else None
        if digest is None:
            digest = sha256_file(source)
            if validation_cache is not None:
                validation_cache[key] = digest
        if digest != parsed_digest:
            return None
        prior = choices[0]
        reason = "The original Arduino source target needs preparation; its exact compiler certificate is unavailable."
        # Keep a concrete preparation failure with the blocked namespace so
        # Compile/Upload can tell the user why retrying preparation is needed.
        # A stale "Ready" reason is never valid after its certificate failed.
        if prior.get("status") == "unavailable":
            detail = " ".join(str(prior.get("reason") or "").split())[:600]
            if detail and not detail.casefold().startswith("ready"):
                reason = detail
        intent = dict(prior, status="unavailable", source_sha256=parsed_digest, reason=reason)
        intent.pop("arduino_cli", None)
        return intent
    except (OSError, ValueError, TypeError):
        return None


def prepared_target_for_record(record, catalog, *, core=None, rows=None, validation_cache=None):
    """Apply exact source-bound mappings in a discovery/build worker only."""
    core = _core_directory(core)
    source = str(record.get("source_file") or record.get("arduino_source_file") or "")
    arduino_id = str(record.get("arduino_id") or record.get("arduino_board_id") or "")
    if not source or not arduino_id:
        return None
    source_key = os.path.normcase(os.path.abspath(source))
    if rows is None:
        rows = prepared_target_index(load_prepared_targets(core))
    choices = rows.get((source_key, arduino_id), []) if isinstance(rows, dict) else prepared_target_index(rows).get((source_key, arduino_id), [])
    if len(choices) != 1:
        return None
    row = choices[0]
    def digest_file(path):
        key = ("file-sha256", str(path))
        if validation_cache is not None and key in validation_cache:
            return validation_cache[key]
        value = sha256_file(path)
        if validation_cache is not None:
            validation_cache[key] = value
        return value
    try:
        if validation_cache is not None and source_key in validation_cache:
            source_digest = validation_cache[source_key]
        else:
            source_digest = sha256_file(source)
            if validation_cache is not None:
                validation_cache[source_key] = source_digest
        if source_digest != row.get("source_sha256"):
            return None
        parsed_digest = record.get("source_sha256") or record.get("arduino_source_sha256")
        if (not isinstance(parsed_digest, str) or not _HEX.fullmatch(parsed_digest)
                or parsed_digest != source_digest):
            return None
        if row.get("backend") == "arduino-cli":
            from src.modules.board_index_targets import hardware_identity_present
            primary = source_target_proof(row)
            if not primary and catalog and hardware_identity_present(record, catalog):
                return None
            if not primary and not unsupported_proof(row):
                return None
            arduino = row.get("arduino_cli")
            if (not isinstance(arduino, dict) or not valid_fqbn(arduino.get("fqbn"))
                    or arduino["fqbn"].split(":")[-1] != arduino_id
                    or ":".join(arduino["fqbn"].split(":")[:2]) != arduino.get("core")):
                return None
            if primary:
                store = _source_store(core, row["arduino_source_proof"])
                if (arduino.get("store") != str(store.relative_to(core)) or not _within(store, core / "arduino-cli")
                        or not _store_configuration_valid(store)):
                    return None
                package, architecture = row["arduino_source_proof"]["core"].split(":")
                hardware = store / "data/packages" / package / "hardware" / architecture
                installed = [path.name for path in hardware.iterdir() if path.is_dir()]
                if installed != [row["arduino_source_proof"]["version"]]:
                    return None
                cli_path = arduino.get("cli_path")
                if (not isinstance(cli_path, str) or not Path(cli_path).is_absolute()
                        or not Path(cli_path).is_file() or not _HEX.fullmatch(str(arduino.get("cli_sha256") or ""))
                        or digest_file(cli_path) != arduino["cli_sha256"]):
                    return None
            certificate_path = core / str(arduino.get("certificate") or "")
            certificate_key = (str(certificate_path), arduino.get("certificate_sha256"), arduino.get("fqbn"),
                               arduino.get("core"), arduino.get("version"), arduino.get("cli_sha256"),
                               arduino.get("store"),
                               arduino.get("cli_path") if primary else None,
                               json.dumps(row.get("arduino_source_proof") if primary else None, sort_keys=True))
            if validation_cache is not None and validation_cache.get(certificate_key):
                return dict(row)
            if (not _within(certificate_path, core / "arduino-cli/certificates")
                    or certificate_path.stat().st_size > 16 * 1024 * 1024
                    or digest_file(certificate_path) != arduino.get("certificate_sha256")):
                return None
            certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
            if (certificate.get("schema") != 1 or certificate.get("host") != sys.platform
                    or certificate.get("core") != arduino.get("core") or certificate.get("version") != arduino.get("version")
                    or certificate.get("cli_sha256") != arduino.get("cli_sha256") or certificate.get("fqbn") != arduino.get("fqbn")):
                return None
            if primary and (certificate.get("arduino_backend_role") != "primary"
                            or certificate.get("store") != arduino.get("store")
                            or certificate.get("cli_path") != arduino.get("cli_path")
                            or certificate.get("arduino_source_proof") != row.get("arduino_source_proof")
                            or certificate.get("core") != row["arduino_source_proof"]["core"]
                            or certificate.get("version") != row["arduino_source_proof"]["version"]
                            or certificate.get("fqbn") != row["arduino_source_proof"]["fqbn"]):
                return None
            proof_files = certificate.get("proof_files")
            if not isinstance(proof_files, dict) or len(proof_files) < 2 or len(proof_files) > 128:
                return None
            if primary:
                package, architecture = row["arduino_source_proof"]["core"].split(":")
                platform_dir = store / "data/packages" / package / "hardware" / architecture / row["arduino_source_proof"]["version"]
                declarations = str((platform_dir / "boards.txt").relative_to(core))
                platform_recipe = str((platform_dir / "platform.txt").relative_to(core))
                if (proof_files.get(declarations) != source_digest or platform_recipe not in proof_files):
                    return None
            for relative, digest in proof_files.items():
                path = core / relative
                if not _within(path, store if primary else core / "arduino-cli") or not _HEX.fullmatch(str(digest)) or digest_file(path) != digest:
                    return None
            tool_files = certificate.get("tool_files")
            if not isinstance(tool_files, dict) or not tool_files or len(tool_files) > 50000:
                return None
            for relative, entry in tool_files.items():
                path = core / relative
                package_root = store / "data/packages" if primary else core / "arduino-cli/data/packages"
                if not isinstance(entry, dict) or not _within(path, package_root) or path.stat().st_size != entry.get("size"):
                    return None
                if "sha256" in entry and (not _HEX.fullmatch(str(entry["sha256"])) or digest_file(path) != entry["sha256"]):
                    return None
            if validation_cache is not None:
                validation_cache[certificate_key] = True
            return dict(row)
        candidates = [candidate for candidate in catalog if candidate.get("platform") == row.get("platform")
                      and candidate.get("id") == row.get("board")]
        if len(candidates) != 1:
            return None
        match = candidates[0]
        from main.core.board_catalog import _normalize_board_identity
        if (record.get("mcu") and match.get("mcu")
                and _normalize_board_identity(record["mcu"]) != _normalize_board_identity(match["mcu"])):
            return None
        manifest = str(match.get("manifest") or "")
        if not manifest or not _within(manifest, core / "platforms") or digest_file(manifest) != row.get("manifest_sha256"):
            return None
        return {**match, "match_reasons": list(row.get("match_reasons") or ["prepared-index-association"]),
                "match_score": 1000.0}
    except (OSError, ValueError, TypeError):
        return None


def valid_fqbn(value):
    return isinstance(value, str) and len(value.split(":")) == 3 and all(_IDENTIFIER.fullmatch(part) for part in value.split(":"))


def arduino_catalog_entry(record, prepared):
    """Retain Arduino identity while making the alternative compiler explicit."""
    return {"backend": "arduino-cli", "platform": prepared.get("platform") or "", "board": str(record.get("arduino_id") or ""), "pio_resolved": False,
            "framework": "arduino", "frameworks": ["arduino"],
            "arduino_board_id": str(record.get("arduino_id") or ""),
            "arduino_name": str(record.get("name") or ""),
            "arduino_source_file": str(record.get("source_file") or ""),
            "arduino_source_sha256": str(record.get("source_sha256") or prepared.get("source_sha256") or ""),
            "arduino_variant": str(record.get("variant") or ""),
            "arduino_build_board": str(record.get("build_board") or ""),
            "source_core": str(record.get("source_core") or ""), "mcu": str(record.get("mcu") or ""),
            "arduino_fqbn": prepared.get("arduino_fqbn"), "arduino_cli": prepared.get("arduino_cli"),
            "platformio_support": prepared.get("platformio_support", "unknown"), "platformio_support_proof": prepared.get("platformio_support_proof"),
            "arduino_backend_role": prepared.get("arduino_backend_role"), "arduino_source_proof": prepared.get("arduino_source_proof"),
            "require_upload_port": True, "fallback_notice": prepared.get("reason")}


def runtime_command(info, *, core=None):
    """Validate an exact prepared Arduino target before invoking its offline CLI."""
    core = _core_directory(core)
    from main.core.board_catalog import _load_platformio_board_catalog
    catalog = _load_platformio_board_catalog(core, force_read=True)
    record = {**info, "name": info.get("arduino_name") or "", "mcu": info.get("mcu"),
              "variant": info.get("arduino_variant"), "build_board": info.get("arduino_build_board")}
    prepared = prepared_target_for_record(record, catalog, core=core)
    if (not prepared or prepared.get("backend") != "arduino-cli"
            or not (source_target_proof(info) or unsupported_proof(info))
            or source_target_proof(info) and info.get("arduino_source_proof") != prepared.get("arduino_source_proof")):
        raise RuntimeError("Arduino CLI target is not verified for this exact board. Prepare board support again")
    if info.get("framework") != "arduino":
        raise RuntimeError("Arduino CLI fallback requires the Arduino framework")
    from main.core.toolchain import find_arduino_cli_executable
    cli = find_arduino_cli_executable()
    if not cli or not Path(cli).is_file():
        raise RuntimeError(_missing_cli_message())
    if sha256_file(cli) != prepared["arduino_cli"].get("cli_sha256"):
        raise RuntimeError("The prepared Arduino CLI executable changed. Prepare board support again")
    store = _source_store(core, prepared["arduino_source_proof"]) if source_target_proof(prepared) else core / "arduino-cli"
    config = store / "arduino-cli.yaml"
    if not config.is_file():
        raise RuntimeError("The prepared Arduino CLI configuration is unavailable")
    # Configuration is validated too: it must never switch to a user/global store.
    if not _store_configuration_valid(store):
        raise RuntimeError("The prepared Arduino CLI store configuration changed. Prepare board support again")
    _verify_core_version([str(cli), "--config-file", str(config), "--json"],
                         prepared["arduino_cli"]["core"], prepared["arduino_cli"]["version"])
    return [str(cli), "--config-file", str(config), "--no-color"], _cli_environment(), prepared["arduino_fqbn"]
