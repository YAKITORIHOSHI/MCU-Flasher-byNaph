#!/usr/bin/env python3
"""Inventory exact Arduino declarations for an isolated PlatformIO compile audit.

This helper never installs packages, resolves boards, touches a live package
store, or changes saved indexes. Only boards.txt metadata is extracted. Advisory
board names in an index are retained as provenance and never become board IDs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
GIB = 1024 * MIB
MAX_INDEX = 16 * MIB
MAX_METADATA = 32 * MIB
MAX_METADATA_FILE = 8 * MIB
MAX_MEMBERS = 300_000
MAX_EXPANDED = 16 * GIB
PROTECTED_COMPONENTS = {".mcu_flasher_build_cache", ".mcu_ai_edits", ".pio", ".vscode", ".clangd", ".opencode"}
VERSION = re.compile(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$")
PRERELEASE = re.compile(r"(?<![A-Za-z0-9])(alpha|beta|rc|pre|preview|dev|nightly|snapshot|test)(?=\d|[^A-Za-z0-9]|$)", re.I)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def scratch_path(value):
    """Reject escapes and existing symlink components before creating anything."""
    candidate = Path(value).absolute()
    if PROTECTED_COMPONENTS.intersection(part.casefold() for part in candidate.parts):
        raise ValueError("Protected build, cache, journal and editor paths are not scratch output")
    allowed = (ROOT / "temp", ROOT / "audit" / "migration" / "scratch")
    for base in allowed:
        try:
            suffix = candidate.relative_to(base)
        except ValueError:
            continue
        if ".." in suffix.parts:
            continue
        current = ROOT
        for component in candidate.relative_to(ROOT).parts:
            current /= component
            if current.is_symlink():
                raise ValueError("Scratch and output paths must not contain symlinks")
        if candidate.resolve().is_relative_to(base.resolve()) and base.resolve().is_relative_to(ROOT):
            return candidate
    raise ValueError("Scratch and output must be inside checkout temp/ or audit/migration/scratch/")


def public_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid public HTTP(S) URL")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise ValueError("Use an HTTP(S) URL without credentials or fragments")
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise ValueError("Local network URLs are outside this public-index audit")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError("Non-public IP addresses are outside this audit")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Invalid URL port") from exc
    return value


class PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, destination, *, limit, minimum_free, expected_size=None, algorithm="sha256", timeout=1800):
    """Stream one bounded artifact, reserving disk space before every write."""
    url = public_url(url)
    started = time.monotonic()
    digest, sha256 = hashlib.new(algorithm), hashlib.sha256()
    size = 0
    request = Request(url, headers={"User-Agent": "MCU-Flasher-Compile-Audit/1", "Accept-Encoding": "identity"})
    with build_opener(PublicRedirect()).open(request, timeout=30) as response:
        final_url = public_url(response.geturl())
        length = response.headers.get("Content-Length")
        if length is not None:
            length = int(length)
            if length < 0 or length > limit:
                raise ValueError("Download exceeds the artifact byte limit")
            if expected_size is not None and length != expected_size:
                raise ValueError("HTTP length differs from the declared archive size")
        budget = expected_size if expected_size is not None else length
        if budget is not None and shutil.disk_usage(destination.parent).free < minimum_free + budget:
            raise OSError("Insufficient free storage above the configured reserve")
        with destination.open("xb") as target:
            while True:
                if time.monotonic() - started > timeout:
                    raise TimeoutError("Artifact download exceeded the elapsed-time limit")
                chunk = response.read(MIB)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit or (expected_size is not None and size > expected_size):
                    raise ValueError("Download exceeds the declared size or artifact byte limit")
                if shutil.disk_usage(destination.parent).free < minimum_free + len(chunk):
                    raise OSError("Stopped download to preserve free storage")
                target.write(chunk)
                digest.update(chunk)
                sha256.update(chunk)
    if expected_size is not None and size != expected_size:
        raise ValueError("Downloaded byte count differs from the declared archive size")
    return {"url": url, "final_url": final_url, "bytes": size,
            "checksum_algorithm": algorithm, "checksum": digest.hexdigest(), "sha256": sha256.hexdigest()}


def stable_version(value, name=""):
    """Numeric releases and Arduino vendor suffixes; no preview-labelled builds."""
    if not isinstance(value, str) or len(value) > 160 or PRERELEASE.search(value) or PRERELEASE.search(name):
        return None
    match = VERSION.fullmatch(value.strip())
    if not match:
        return None
    suffix = match.group(4)
    # Semver build metadata is stable. Arduino's vendor packaging suffix is
    # also a release (e.g. 2.0.18-arduino.5), unlike arbitrary prereleases.
    if suffix and not (suffix.startswith("+") or re.fullmatch(r"-arduino(?:[.\d_-].*)?", suffix, re.I)):
        return None
    vendor = tuple(int(x) for x in re.findall(r"\d+", suffix.split("+", 1)[0]))
    return (*(int(match.group(i) or 0) for i in range(1, 4)), vendor)


def latest_platforms(data):
    if not isinstance(data, dict) or not isinstance(data.get("packages"), list):
        raise ValueError("Index must contain a packages array")
    if len(data["packages"]) > 10_000:
        raise ValueError("Index has too many packages")
    grouped = {}
    count = 0
    for package in data["packages"]:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            raise ValueError("Invalid package identity")
        platforms = package.get("platforms", [])
        if not isinstance(platforms, list):
            raise ValueError("Package platforms must be an array")
        for platform in platforms:
            count += 1
            if count > 100_000:
                raise ValueError("Index has too many platform versions")
            if not isinstance(platform, dict) or not isinstance(platform.get("architecture"), str):
                raise ValueError("Invalid platform architecture")
            identity = (package["name"], platform["architecture"])
            grouped.setdefault(identity, [])
            key = stable_version(platform.get("version"), str(platform.get("name") or ""))
            if key is not None:
                grouped[identity].append((key, platform))
    results = []
    for (package, architecture), versions in sorted(grouped.items()):
        entry = {"package": package, "architecture": architecture}
        if not versions:
            results.append({**entry, "status": "no_stable_version", "error": "No supported stable version in this index"})
            continue
        best = max(version[0] for version in versions)
        choices = [platform for version, platform in versions if version == best]
        identities = {(str(p.get("version")), str(p.get("url")), str(p.get("checksum"))) for p in choices}
        if len(identities) != 1:
            results.append({**entry, "status": "ambiguous_release", "error": "Latest release has conflicting archive declarations"})
            continue
        results.append({**entry, "status": "selected", "platform": choices[0]})
    return results


def archive_name(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value or "\x00" in value:
        raise ValueError("Invalid archive member path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or any(":" in part for part in path.parts):
        raise ValueError("Archive contains an escaping member path")
    return path


def extract_metadata(archive, destination, *, minimum_free=0):
    """Copy metadata bytes ourselves; never invoke an archive extract method."""
    members, expanded, metadata_size = 0, 0, 0
    extracted, seen = [], set()

    def inspect(name, size, regular, link=False):
        nonlocal members, expanded
        members += 1
        if members > MAX_MEMBERS or size < 0:
            raise ValueError("Archive member limit exceeded")
        expanded += size
        if expanded > MAX_EXPANDED:
            raise ValueError("Archive expanded-byte limit exceeded")
        path = archive_name(name)
        if link and path.name in {"boards.txt", "platform.txt"}:
            raise ValueError("Archive metadata links are not accepted")
        # Unrelated links are never opened, followed or copied. Some official
        # cores contain such links alongside their regular board declarations.
        return not link and regular and path.name == "boards.txt"

    def copy_member(name, size, stream):
        nonlocal metadata_size
        normalized = str(archive_name(name))
        if normalized in seen:
            raise ValueError("Archive contains duplicate boards.txt metadata")
        seen.add(normalized)
        if size > MAX_METADATA_FILE or metadata_size + size > MAX_METADATA:
            raise ValueError("Board metadata byte limit exceeded")
        raw = stream.read(MAX_METADATA_FILE + 1)
        if len(raw) != size or len(raw) > MAX_METADATA_FILE:
            raise ValueError("Truncated or oversized boards.txt")
        metadata_size += len(raw)
        if shutil.disk_usage(destination).free < minimum_free + len(raw):
            raise OSError("Stopped metadata extraction to preserve free storage")
        folder = destination / f"declaration-{len(extracted):04d}"
        folder.mkdir()
        output = folder / "boards.txt"
        output.write_bytes(raw)
        extracted.append({"member": name, "path": str(output.relative_to(ROOT)),
                          "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})

    destination.mkdir(parents=True, exist_ok=False)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                kind = stat.S_IFMT(member.external_attr >> 16)
                link = kind == stat.S_IFLNK
                regular = not member.is_dir() and kind in (0, stat.S_IFREG)
                if member.flag_bits & 1:
                    raise ValueError("Encrypted archive entries are not accepted")
                if inspect(member.filename, member.file_size, regular, link):
                    with bundle.open(member) as source:
                        copy_member(member.filename, member.file_size, source)
    else:
        with tarfile.open(archive, mode="r|*") as bundle:
            for member in bundle:
                if inspect(member.name, member.size, member.isfile(), member.issym() or member.islnk()):
                    source = bundle.extractfile(member)
                    if source is None:
                        raise ValueError("Unavailable board metadata member")
                    with source:
                        copy_member(member.name, member.size, source)
    if not extracted:
        raise ValueError("Archive has no boards.txt; advisory index names cannot supply exact board IDs")
    return {"members_scanned": members, "expanded_bytes_declared": expanded,
            "metadata_bytes": metadata_size, "sources": extracted}


def json_safe(value):
    if isinstance(value, set):
        return sorted((json_safe(item) for item in value), key=str)
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    return value


def inventory_package(entry, index, session, *, minimum_free, max_archive):
    platform = entry["platform"]
    receipt = {**{key: value for key, value in entry.items() if key != "platform"},
               "index_url": index["url"], "index_sha256": index["sha256"],
               "version": platform.get("version"), "platform_name": platform.get("name"),
               "archive_url": platform.get("url"), "declared_checksum": platform.get("checksum"),
               "declared_size": platform.get("size"), "advisory_board_names": platform.get("boards", []),
               "status": "error", "declarations": []}
    identity = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()[:20]
    directory = session / ("package-" + identity)
    directory.mkdir()
    archive = directory / "download.archive"
    try:
        checksum = platform.get("checksum")
        if not isinstance(checksum, str) or ":" not in checksum:
            raise ValueError("A declared archive checksum is required")
        algorithm, expected_digest = checksum.split(":", 1)
        algorithm = algorithm.casefold().replace("-", "")
        if algorithm not in {"sha256", "sha512", "sha1", "md5"}:
            raise ValueError("Unsupported declared checksum algorithm")
        if not re.fullmatch(r"[0-9a-fA-F]{" + str(hashlib.new(algorithm).digest_size * 2) + "}", expected_digest):
            raise ValueError("Malformed declared archive checksum")
        raw_size = platform.get("size")
        if isinstance(raw_size, bool) or not re.fullmatch(r"\d+", str(raw_size)):
            raise ValueError("A positive declared archive size is required")
        size = int(raw_size)
        if size <= 0 or size > max_archive:
            raise ValueError("Declared archive exceeds the configured byte limit")
        receipt["download"] = fetch(platform.get("url"), archive, limit=max_archive,
                                    minimum_free=minimum_free + MAX_METADATA, expected_size=size, algorithm=algorithm)
        if receipt["download"]["checksum"] != expected_digest.casefold():
            raise ValueError("Archive checksum does not match its index declaration")
        receipt["archive"] = extract_metadata(archive, directory / "metadata", minimum_free=minimum_free)
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from main.core.board_catalog import _parse_downloaded_arduino_board_files
        records = _parse_downloaded_arduino_board_files(directory / "metadata", force_read=True)
        source_members = {source["path"]: source["member"] for source in receipt["archive"]["sources"]}
        for record in records:
            record["source_file"] = str(Path(record["source_file"]).relative_to(ROOT))
            record["source_member"] = source_members[record["source_file"]]
            record["source_core"] = ":".join((entry["package"], entry["architecture"], str(platform["version"])))
        receipt["declarations"] = json_safe(records)
        if not records:
            raise ValueError("No visible exact board declarations were found")
        receipt["status"] = "inventoried"
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"[:4096]
    finally:
        archive.unlink(missing_ok=True)
    return receipt


def write_receipt(output, receipt):
    output = scratch_path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + f".tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", action="append", required=True, help="Public board-manager index URL; repeat for each source")
    parser.add_argument("--output", required=True, type=Path, help="JSON receipt inside an allowed scratch tree")
    parser.add_argument("--scratch", required=True, type=Path, help="Parent of a fresh helper-owned session")
    parser.add_argument("--minimum-free-gib", type=float, default=5.0, help="Stop before using this storage reserve (default: 5)")
    parser.add_argument("--max-archive-mib", type=int, default=1536, help="Maximum single declared archive (default: 1536)")
    args = parser.parse_args(argv)
    if not (0 <= args.minimum_free_gib <= 1_000_000) or not (1 <= args.max_archive_mib <= 16_384):
        parser.error("Invalid storage limit")
    try:
        scratch, output = scratch_path(args.scratch), scratch_path(args.output)
        urls = list(dict.fromkeys(public_url(url) for url in args.index))
    except ValueError as exc:
        parser.error(str(exc))
    scratch.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix="board-index-inventory-", dir=scratch))
    receipt = {"schema": 1, "kind": "board-compile-index-declarations", "started_at": utc_now(),
               "session": str(session.relative_to(ROOT)), "policy": {
                   "scope": "Latest stable version per index package and architecture; exact visible boards.txt declarations only",
                   "stable_versions": "Numeric releases, build metadata and Arduino vendor suffixes; preview names/versions excluded",
                   "installs": False, "hardware": False, "platformio_resolution": False,
                   "minimum_free_bytes": int(args.minimum_free_gib * GIB), "max_archive_bytes": args.max_archive_mib * MIB},
               "indexes": [], "packages": []}
    for ordinal, url in enumerate(urls):
        index = {"url": url, "status": "error"}
        receipt["indexes"].append(index)
        try:
            snapshot = session / f"index-{ordinal:03d}.json"
            index.update(fetch(url, snapshot, limit=MAX_INDEX, minimum_free=int(args.minimum_free_gib * GIB), timeout=120))
            index["snapshot"] = str(snapshot.relative_to(ROOT))
            data = json.loads(snapshot.read_text(encoding="utf-8-sig"))
            entries = latest_platforms(data)
            index.update(status="inventoried", package_architectures=len(entries))
            for entry in entries:
                if entry["status"] != "selected":
                    receipt["packages"].append({**entry, "index_url": url, "declarations": []})
                else:
                    receipt["packages"].append(inventory_package(entry, index, session,
                        minimum_free=int(args.minimum_free_gib * GIB), max_archive=args.max_archive_mib * MIB))
                write_receipt(output, receipt)
        except Exception as exc:
            index.update(status="error", error=f"{type(exc).__name__}: {exc}"[:4096])
        write_receipt(output, receipt)
    receipt["finished_at"] = utc_now()
    receipt["summary"] = {"indexes": len(receipt["indexes"]), "packages": len(receipt["packages"]),
                          "inventoried_packages": sum(p["status"] == "inventoried" for p in receipt["packages"]),
                          "declarations": sum(len(p["declarations"]) for p in receipt["packages"]),
                          "errors": sum(i["status"] != "inventoried" for i in receipt["indexes"]) +
                                    sum(p["status"] != "inventoried" for p in receipt["packages"])}
    write_receipt(output, receipt)
    print(json.dumps({"receipt": str(output), **receipt["summary"]}))
    return int(bool(receipt["summary"]["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
