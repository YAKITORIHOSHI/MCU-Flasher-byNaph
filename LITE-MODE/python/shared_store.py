"""Consolidate Lite's old private packages into the main native tool store.

Package bytes are either moved once or independently compared before deleting a
duplicate. Settings, projects, source snapshots and recovery journals stay put.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

from preparation import copy_package, file_hash, inventory, read_only_packages


def legacy_cores(state):
    return (Path(state) / "platformio", Path(state) / "engine/src/.platformio-mcu-gui")


def _plain_directory(path):
    return path.is_dir() and not path.is_symlink() and not getattr(path, "is_junction", lambda: False)()


def _same_package(source, destination, log, *, deadline, cancel=None):
    before = inventory(source)
    if before != inventory(destination):
        return False, 0
    files, _links, _dirs = before
    def stamps(root):
        return [(path, (root / path).stat().st_size, (root / path).stat().st_mtime_ns,
                 (root / path).stat().st_ino) for path in files]
    source_stamps, destination_stamps = stamps(source), stamps(destination)
    total = 0
    log(f"Package files: 0/{len(files)}")
    last = time.monotonic()
    for index, relative in enumerate(files, 1):
        if time.monotonic() > deadline or (cancel is not None and cancel.is_set()):
            raise InterruptedError("Shared-store cleanup stopped before deleting the package")
        left, right = source / relative, destination / relative
        if left.stat().st_size != right.stat().st_size or file_hash(left) != file_hash(right):
            return False, 0
        total += left.stat().st_size
        if time.monotonic() - last >= 1:
            log(f"Package files: {index}/{len(files)}")
            last = time.monotonic()
    log(f"Package files: {len(files)}/{len(files)}")
    if (inventory(source) != before or inventory(destination) != before
            or stamps(source) != source_stamps or stamps(destination) != destination_stamps):
        raise RuntimeError("Package inventory changed during shared-store verification")
    return True, total


def consolidate(state, core, *, events=None, apply=False, log=print, cancel=None, own_pid=None):
    """Retire only proven duplicate native packages; return an audit receipt."""
    from worker import hold_lock, live_workers
    from src.modules.package_jobs import package_store_lease
    from platformio.package.manager.platform import PlatformPackageManager
    from platformio.package.manager.tool import ToolPackageManager
    from platformio.package.manager.library import LibraryPackageManager

    state, core = Path(state).resolve(), Path(core).resolve()
    if not (state / "engine-manifest.json").is_file() or not (state / "engine/main/web_bridge.py").is_file():
        raise RuntimeError("Shared-store cleanup requires an owned Lite backend snapshot")
    if core == state or any(core.is_relative_to(old) for old in legacy_cores(state)):
        raise RuntimeError("The shared store must be separate from Lite's retired store")
    result = {"schema": 1, "shared_core": str(core), "applied": apply, "packages": [],
              "removed_bytes": 0, "moved_bytes": 0, "deferred": False}
    deadline = time.monotonic() + 600
    with hold_lock(state / "engine.lock", budget=15, cancel=cancel):
        if live_workers(state, own_pid):
            result.update(deferred=True, reason="Close other Lite windows before consolidating their old packages")
            return result
        # The previous Lite implementation had its own event-root namespace.
        with package_store_lease(next((old for old in legacy_cores(state) if old.is_dir()), legacy_cores(state)[0]),
                                 "prepare", root=state / "package-events"):
            with package_store_lease(core, "prepare", root=events):
                for old in legacy_cores(state):
                    if not old.exists():
                        continue
                    if not _plain_directory(old):
                        raise RuntimeError("Refusing redirected legacy package storage")
                    for folder, manager_type in (("platforms", PlatformPackageManager),
                                                 ("packages", ToolPackageManager), ("lib", LibraryPackageManager)):
                        source_root, destination_root = old / folder, core / folder
                        if not _plain_directory(source_root):
                            continue
                        if apply:
                            destination_root.mkdir(parents=True, exist_ok=True)
                        manager = manager_type(str(source_root))
                        for package in read_only_packages(manager):
                            if time.monotonic() > deadline or (cancel is not None and cancel.is_set()):
                                raise InterruptedError("Shared-store cleanup reached its time budget")
                            source = Path(package.path)
                            destination = destination_root / source.name
                            log(f"Checking shared package: {package.metadata.name} @ {package.metadata.version}")
                            action, size = "move", 0
                            if destination.exists():
                                if not _plain_directory(destination):
                                    raise RuntimeError("Refusing redirected shared package storage")
                                same, size = _same_package(source, destination, log, deadline=deadline, cancel=cancel)
                                if same:
                                    action = "remove-duplicate"
                                else:
                                    # Keep different native versions/contents in the
                                    # one shared store; never replace the main copy.
                                    receipt = (source / ".piopm").read_bytes()
                                    suffix = hashlib.sha256(receipt + str(source).encode()).hexdigest()[:12]
                                    destination = destination_root / (source.name + "@lite-" + suffix)
                                    if destination.exists():
                                        same, size = _same_package(source, destination, log, deadline=deadline, cancel=cancel)
                                        if not same:
                                            result["packages"].append({"name": source.name, "action": "retained-conflict"})
                                            continue
                                        action = "remove-duplicate"
                            if action == "move":
                                files, package_links, _dirs = inventory(source)
                                size = sum((source / path).stat().st_size for path in files)
                            if apply:
                                if time.monotonic() > deadline or (cancel is not None and cancel.is_set()):
                                    raise InterruptedError("Shared-store cleanup stopped before retiring the package")
                                # rename is atomic on the normal same-filesystem
                                # native store. Cross-device input stays intact.
                                if action == "move":
                                    try:
                                        if any(Path(os.readlink(source / relative)).is_absolute() for relative in package_links):
                                            raise OSError(errno.EXDEV, "Package links require verified relocation")
                                        source.rename(destination)
                                    except OSError as error:
                                        if error.errno != errno.EXDEV:
                                            raise
                                        from types import SimpleNamespace
                                        manager = SimpleNamespace(package_dir=str(destination.parent), memcache_reset=lambda: None)
                                        if not copy_package(package, manager, log, destination_name=destination.name,
                                                            cancel=cancel, deadline=deadline):
                                            raise RuntimeError("Cannot publish the migrated package without overwriting shared content")
                                        same, _ = _same_package(source, destination, log, deadline=deadline, cancel=cancel)
                                        if not same:
                                            raise RuntimeError("Migrated package verification failed; the old copy was retained")
                                        shutil.rmtree(source)
                                else:
                                    shutil.rmtree(source)
                            result["packages"].append({"name": source.name, "action": action, "bytes": size})
                            result["removed_bytes" if action == "remove-duplicate" else "moved_bytes"] += size
                    if apply:
                        # Caches in the retired core are reproducible. No project
                        # paths, settings, journals or unknown directories enter
                        # this allowlist, and an active old worker blocks above.
                        for name in (".cache", ".tmp", ".lite-reuse"):
                            if time.monotonic() > deadline or (cancel is not None and cancel.is_set()):
                                raise InterruptedError("Shared-store cleanup stopped before cache retirement")
                            path = old / name
                            if _plain_directory(path):
                                try:
                                    inventory(path)  # Refuse links escaping an owned cache or junctions.
                                except (OSError, ValueError):
                                    result["packages"].append({"name": name, "action": "retained-redirected-cache"})
                                    continue
                                size = sum((Path(directory) / name).stat().st_size
                                           for directory, _dirs, names in os.walk(path, followlinks=False)
                                           for name in names if not (Path(directory) / name).is_symlink())
                                shutil.rmtree(path)
                                result["removed_bytes"] += size
                        for name in ("platforms", "packages", "lib"):
                            try:
                                (old / name).rmdir()
                            except OSError:
                                pass  # Unknown content remains available for review.
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    # Load only the main application's toolkit-free coordination and PM code.
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    result = consolidate(args.state, args.core, events=args.events, apply=args.apply)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "packages"}, indent=2))


if __name__ == "__main__":
    main()
