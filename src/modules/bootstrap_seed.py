"""Read-only seed planning and missing-only import of a verified Windows release.

The caller verifies the whole archive digest. Archived package metadata travels
with the original payload; this module never invents PlatformIO provenance.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import time
import zipfile

from src.modules.bootstrap_platformio import extended_windows_path


def managers(core):
    from platformio.package.manager.platform import PlatformPackageManager
    from platformio.package.manager.tool import ToolPackageManager
    from platformio.package.manager.library import LibraryPackageManager
    core = Path(core)
    canonical = core.resolve()
    for kind in ("platforms", "packages", "lib"):
        if not (core / kind).resolve().is_relative_to(canonical):
            raise RuntimeError(f"Package store leaves the configured core: {core / kind}")
    return {"platforms": PlatformPackageManager(str(core / "platforms")),
            "packages": ToolPackageManager(str(core / "packages")),
            "lib": LibraryPackageManager(str(core / "lib"))}


def scons_spec():
    from platformio.dependencies import get_core_dependencies
    from platformio.package.meta import PackageSpec
    return PackageSpec(owner="platformio", name="tool-scons",
                       requirements=get_core_dependencies()["tool-scons"])


def _usable_manifest(manager, item):
    if not item or not item.metadata:
        return False
    folder = Path(item.path)
    if not any((folder / name).is_file() for name in manager.manifest_names):
        return False
    try:
        manifest = manager.load_manifest(item)
        if not manifest:
            return False
        if manifest.get("name") and manifest["name"] != item.metadata.name:
            return False
        if manifest.get("version"):
            from platformio.package.version import cast_version_to_semver
            if cast_version_to_semver(manifest["version"]) != item.metadata.version:
                return False
    except Exception:
        return False
    if item.metadata.name == "tool-scons" and not (folder / "scons.py").is_file():
        return False
    return True


def valid_package(manager, spec):
    """Resolve genuine package metadata and a usable declared manifest locally."""
    item = manager.get_package(spec)
    if not _usable_manifest(manager, item):
        return None
    return item


def prepare_scons(core, *, manager=None, log=print):
    """Use normal SCons resolution/install without existing-package pruning."""
    core = Path(core)
    manager = manager if manager is not None else managers(core)["packages"]
    spec = scons_spec()
    installed = valid_package(manager, spec)
    if installed:
        log("PlatformIO SCons package and payload verified locally.")
        return installed
    destination = core / "packages" / spec.name
    if destination.exists():
        raise RuntimeError(f"SCons does not satisfy {spec.humanize()}: {destination}. Existing files were retained; package repair needs an explicit decision.")
    log(f"Installing missing SCons through PlatformIO: {spec.humanize()}")
    manager.install(spec, skip_dependencies=True)
    manager.memcache_reset()
    installed = valid_package(manager, spec)
    if not installed:
        raise RuntimeError("PlatformIO SCons installation returned without a valid package/payload; installed files were retained.")
    log("PlatformIO SCons build engine is verified and ready.")
    return installed


def missing_packages(core, plan):
    """Inspect the configured scope without registry calls, installs or writes."""
    from platformio.package.meta import PackageItem
    from platformio.platform.factory import PlatformFactory
    from src.modules.offline_bootstrap import package_plan, _validate_plan
    plan = _validate_plan(plan)
    core = Path(core)
    stores = managers(core)
    missing = {kind: [] for kind in stores}
    allowed = set(plan["frameworks"]) if "frameworks" in plan else None
    specifications = {scons_spec().humanize(): scons_spec()}
    inspected = set()

    def inspect(kind, specification):
        manager = stores[kind]
        specification = manager.ensure_spec(specification)
        identity = (kind, specification.humanize())
        if identity in inspected:
            return valid_package(manager, specification)
        inspected.add(identity)
        item = valid_package(manager, specification)
        if item:
            manifest = manager.load_manifest(item)
            for dependency in manifest.get("dependencies", []) if kind != "platforms" else []:
                # Match PlatformIO's own conditional dependency processing,
                # without executing hooks or querying an online registry.
                if isinstance(dependency, dict) and dependency.get("optional"):
                    continue
                if isinstance(dependency, dict) and dependency.get("platforms") and not manager.is_system_compatible(dependency["platforms"]):
                    continue
                if isinstance(dependency, dict):
                    dependency = manager.dependency_to_spec(dependency)
                dependency = manager.ensure_spec(dependency)
                builtin = getattr(manager, "is_builtin_lib", None)
                if callable(builtin) and not dependency.external and not dependency.owner and builtin(dependency.name):
                    continue
                inspect(kind, dependency)
            return item
        # An existing invalid package is a repair decision, never permission
        # to overwrite/remove a directory or manufacture its missing .piopm.
        resolved = manager.get_package(specification)
        candidates = [Path(resolved.path)] if resolved else []
        if specification.name and re.fullmatch(r"[\w.+@ -]+", specification.name):
            candidates.append(core / kind / specification.name)
        for candidate in candidates:
            if candidate.exists():
                existing = PackageItem(str(candidate))
                if not existing.metadata or not any((candidate / name).is_file() for name in manager.manifest_names):
                    raise RuntimeError(f"Existing package is incomplete: {candidate}. It was retained; repair requires an explicit decision.")
                if existing.metadata.name == "tool-scons" and not (candidate / "scons.py").is_file():
                    raise RuntimeError(f"Existing SCons payload is incomplete: {candidate}. It was retained.")
                if not _usable_manifest(manager, existing):
                    raise RuntimeError(f"Existing package manifest or payload is invalid: {candidate}. It was retained; repair requires an explicit decision.")
        missing[kind].append(specification)
        return None

    for name in plan["platforms"]:
        platform = inspect("platforms", name)
        if platform:
            instance = PlatformFactory.new(platform)
            specs, _ = package_plan(platform, instance.get_boards(), allowed_frameworks=allowed)
            specifications.update((spec.humanize(), spec) for spec in specs)
    for spec in specifications.values():
        inspect("packages", spec)
    for spec in plan.get("libraries", []):
        inspect("lib", spec)
    return missing


def reject_unresolved_collisions(core, missing):
    """Normal installation must not replace an existing unmatched directory."""
    stores = managers(core)
    for kind, specs in missing.items():
        for spec in specs:
            name = getattr(spec, "name", None)
            if name and re.fullmatch(r"[\w.+@ -]+", name):
                destination = Path(core) / kind / name
                if destination.exists() and not valid_package(stores[kind], spec):
                    raise RuntimeError(f"Existing package does not satisfy {spec.humanize()}: {destination}. It was retained; replacement requires an explicit decision.")


def _archive_groups(archive):
    """Validate paths before reading metadata or creating any payload files."""
    infos = archive.infolist()
    first = {info.filename.replace("\\", "/").split("/", 1)[0] for info in infos}
    wrapper = next(iter(first)) if len(first) == 1 else ""
    strip_wrapper = wrapper.replace(".", "").replace("-", "").replace("_", "").lower() in (
        "platformiomcugui", "platformiomcuguiprebuilt")
    groups = {}
    for info in infos:
        raw = info.filename.replace("\\", "/")
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts or ":" in raw or "\x00" in raw:
            raise RuntimeError(f"Unsafe release archive path: {info.filename}")
        parts = path.parts[1:] if strip_wrapper else path.parts
        if len(parts) < 3 or parts[0] not in ("platforms", "packages", "lib") or info.is_dir():
            continue
        if not re.fullmatch(r"[\w.+@ -]+", parts[1]):
            raise RuntimeError(f"Unsafe release package directory: {parts[1]}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise RuntimeError(f"Release package contains a symlink: {info.filename}")
        groups.setdefault((parts[0], parts[1]), []).append((info, parts[2:]))
    return groups


def _publish_missing(source, destination, *, log=print, attempts=8):
    """Publish one validated tree; briefly wait out Windows sharing/access locks.

    Rename is still the only mutation. Never overwrite a destination, change its
    permissions, or remove staging. Persistent denial and other errors retain
    the original exception after a small, bounded retry window.
    """
    source, destination = Path(source), Path(destination)
    windows = sys_platform_is_windows()
    source_path = extended_windows_path(source) if windows else source
    destination_path = extended_windows_path(destination) if windows else destination
    attempts = max(1, min(8, int(attempts)))
    delays = (.1, .2, .4, .8, 1.2, 1.5, 1.8)
    for attempt in range(attempts):
        # lexists also rejects a dangling junction/symlink. Windows rename
        # refuses a destination created after this check, including a race.
        if os.path.lexists(destination_path):
            raise RuntimeError(f"Release destination appeared during import: {destination}. It was retained without changes.")
        try:
            os.rename(source_path, destination_path)
            return
        except OSError as error:
            if os.path.lexists(destination_path):
                raise RuntimeError(f"Release destination appeared during import: {destination}. It was retained without changes.") from error
            if (not windows or getattr(error, "winerror", None) not in (5, 32, 33)
                    or attempt + 1 >= attempts):
                raise
            delay = delays[attempt]
            log(f"Windows denied release package publication (error {error.winerror}); "
                f"retry {attempt + 1}/{attempts - 1} in {delay:.1f}s: {destination.name}")
            time.sleep(delay)


def import_missing(zip_path, core, missing, *, staging_parent, log=print, progress=None):
    """Retain staging/ZIP and publish only absent, genuinely identified groups.

    Complete existing packages are preserved, including older versions. A new
    version uses an absent version-suffixed directory when its base is occupied.
    Bootstrap's normal managers subsequently validate dependencies and prepare
    the copied, already-prebuilt package without changing its provenance.
    """
    core = Path(os.path.abspath(core))
    staging_parent = Path(staging_parent)
    staging_parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="prebuilt-seed-", dir=staging_parent))
    started = time.monotonic()
    imported, copied_bytes = [], 0
    log(f"Release seed staging retained at {stage}")
    try:
        with zipfile.ZipFile(zip_path) as archive:
            groups = _archive_groups(archive)
            # Only small provenance/manifests are indexed first. No unselected
            # toolchains, frameworks, caches or Python environments are copied.
            for (kind, directory), members in groups.items():
                for info, relative in members:
                    if len(relative) != 1 or relative[0] not in (
                            ".piopm", "package.json", "platform.json", "library.json", "library.properties"):
                        continue
                    if info.file_size > 524288:
                        raise RuntimeError(f"Release metadata exceeds bounded size: {info.filename}")
                    target = stage / kind / directory / relative[0]
                    os.makedirs(extended_windows_path(target.parent), exist_ok=True)
                    with archive.open(info) as source, open(extended_windows_path(target), "wb") as output:
                        output.write(source.read(524289))
            staged_stores, live_stores = managers(stage), managers(core)
            for kind, specs in missing.items():
                for spec in specs:
                    if valid_package(live_stores[kind], spec):
                        continue
                    # SCons's payload has not been copied yet, so resolve its
                    # genuine metadata here and validate the full tree below.
                    item = staged_stores[kind].get_package(spec)
                    if not item or not item.metadata:
                        log(f"Release has no matching {spec.humanize()}; normal package setup will resolve this missing item.")
                        continue
                    source_dir = Path(item.path)
                    key = (kind, source_dir.name)
                    if key not in groups:
                        raise RuntimeError(f"Release package group is missing: {key}")
                    if not re.fullmatch(r"[\w.+@ -]+", item.metadata.name):
                        raise RuntimeError(f"Unsafe release package name: {item.metadata.name}")
                    destination = core / kind / source_dir.name
                    if destination.exists():
                        destination = core / kind / (item.metadata.name + "@" + str(item.metadata.version))
                    if destination.exists():
                        raise RuntimeError(f"Release destination already exists: {destination}. It was retained without changes.")
                    if not destination.resolve().is_relative_to(core.resolve()):
                        raise RuntimeError(f"Release destination leaves the configured core: {destination}")
                    group_bytes = sum(info.file_size for info, _ in groups[key])
                    log(f"Importing release package {spec.humanize()} ({group_bytes / 1048576:.1f} MiB) -> {destination}")
                    for info, relative in groups[key]:
                        target = source_dir.joinpath(*relative)
                        os.makedirs(extended_windows_path(target.parent), exist_ok=True)
                        with archive.open(info) as source, open(extended_windows_path(target), "wb") as output:
                            import shutil
                            shutil.copyfileobj(source, output)
                        copied_bytes += info.file_size
                        if progress:
                            progress(copied_bytes, info.filename)
                    staged_stores[kind].memcache_reset()
                    if not valid_package(staged_stores[kind], spec):
                        raise RuntimeError(f"Release package payload is incomplete: {spec.humanize()}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _publish_missing(source_dir, destination, log=log)
                    live_stores[kind].memcache_reset()
                    imported.append(str(destination))
            log(f"Release seed imported {len(imported)} missing groups, {copied_bytes / 1048576:.1f} MiB in {time.monotonic() - started:.1f}s; archive retained.")
        return {"imported": imported, "staging": str(stage), "bytes": copied_bytes}
    except Exception as exc:
        raise RuntimeError(f"Release seed failed; archive and staging retained at {zip_path} and {stage}: {exc}") from exc


def sys_platform_is_windows():
    import sys
    return sys.platform == "win32"
