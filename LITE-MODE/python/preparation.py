"""Selected-target build/upload dependencies in the shared native package store.

This module prepares packages only. It never runs a compiler or uploader and
never opens a serial port. PlatformIO's selected platform owns the dependency
rules, including optional upload packages and exact version constraints.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sys
import threading
import time
from uuid import uuid4


def main_core_directory(original, environ, *, system=None, machine=None, home=None):
    """Match the main app's native store before Lite redirects XDG/Windows state."""
    import platform
    system = system or sys.platform
    if system == "win32":
        return Path(original) / "src/.platformio-mcu-gui"
    home = Path(home) if home is not None else Path.home()
    value = environ.get("XDG_DATA_HOME", "")
    base = Path(value) if value and PurePosixPath(value).is_absolute() and not PureWindowsPath(value).drive else home / ".local/share"
    return base / "mcu-flasher/platformio" / (machine or platform.machine())


def read_only_packages(manager):
    """Unlike get_installed(), never synthesize missing legacy .piopm records."""
    from platformio.package.meta import PackageItem
    from platformio.package.exception import PackageException
    root = Path(manager.package_dir)
    if not root.is_dir():
        return []
    packages = []
    for child in sorted(root.iterdir()):
        if child.name.startswith(("_tmp_installing", ".lite-reuse-")):
            continue
        if child.is_symlink() or getattr(child, "is_junction", lambda: False)() or not child.is_dir():
            continue
        if not (child / ".piopm").is_file():
            continue
        try:
            package = PackageItem(str(child))
            if not package.metadata or package.metadata.type != manager.pkg_type:
                continue
            manifest = manager.load_manifest(package)
            if manager.pkg_type == "tool" and not manager.is_system_compatible(manifest.get("system")):
                continue
            packages.append(package)
        except (OSError, ValueError, KeyError, TypeError, PackageException):
            continue
    return packages


def source_manager(manager_type, directory):
    class ReadOnly(manager_type):
        def get_installed(self):
            return read_only_packages(self)
    return ReadOnly(str(directory))


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def inventory(root):
    files, links, directories = [], {}, []
    ignored = {"__pycache__", ".git", ".hg", ".svn"}
    def unreadable(error):
        raise error
    for directory, names, filenames in os.walk(root, followlinks=False, onerror=unreadable):
        names[:] = sorted(name for name in names if name not in ignored)
        for name in list(names) + sorted(filenames):
            path = Path(directory) / name
            relative = path.relative_to(root)
            if path.is_symlink():
                target = path.resolve(strict=True)
                if not target.is_relative_to(root):
                    raise ValueError("Package link leaves its own package")
                links[relative] = os.path.relpath(target, path.parent)
                if name in names:
                    names.remove(name)
            elif getattr(path, "is_junction", lambda: False)():
                raise ValueError("Package contains a directory junction")
            elif path.is_dir():
                directories.append(relative)
            elif path.is_file():
                files.append(relative)
            else:
                raise ValueError("Package contains a non-file entry")
    return files, links, directories


def copy_package(package, manager, log=print, *, destination_name=None, cancel=None, deadline=None):
    """Publish an independent native copy only after byte/inventory verification."""
    from src.modules.bootstrap_file_pool import FilePool, file_workers
    source = Path(package.path).resolve(strict=True)
    parent = Path(manager.package_dir)
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / (destination_name or source.name)
    if os.path.lexists(destination):
        # Let PlatformIO handle its own side-by-side versions, never overwrite
        # a different installed package or a user's unknown directory here.
        return False
    # Interrupted copies must remain outside PlatformIO's scanned package
    # directories, even if .piopm was copied before the interruption.
    staging = parent.parent / ".lite-reuse"
    staging.mkdir(exist_ok=True)
    stage = staging / uuid4().hex
    stage.mkdir()
    try:
        files, links, directories = inventory(source)
        if not files:
            raise ValueError("Package is empty")
        for relative in directories:
            (stage / relative).mkdir(parents=True, exist_ok=True)
        done = 0
        last_progress = time.monotonic()
        lock = threading.Lock()
        log(f"Reusing main package: {package.metadata.name} @ {package.metadata.version}")
        log(f"Package files: 0/{len(files)}")

        def copy(relative):
            nonlocal done, last_progress
            if (cancel is not None and cancel.is_set()) or (deadline is not None and time.monotonic() > deadline):
                raise InterruptedError("Package migration stopped before publication")
            src, dst = source / relative, stage / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            before = file_hash(src)
            shutil.copy2(src, dst)
            if file_hash(dst) != before or file_hash(src) != before:
                raise OSError("Main package changed while Lite copied it")
            with lock:
                done += 1
                if time.monotonic() - last_progress >= 1.0:
                    log(f"Package files: {done}/{len(files)}")
                    last_progress = time.monotonic()

        with FilePool(workers=min(4, file_workers((source, parent)))) as pool:
            for relative in files:
                pool.submit(copy, relative, size=(source / relative).stat().st_size)
        if inventory(source) != (files, links, directories):
            raise OSError("Main package inventory changed while Lite copied it")
        for relative, target in links.items():
            os.symlink(target, stage / relative, target_is_directory=(source / relative).is_dir())
        log(f"Package files: {len(files)}/{len(files)}")
        if (cancel is not None and cancel.is_set()) or (deadline is not None and time.monotonic() > deadline):
            raise InterruptedError("Package migration stopped before publication")
        if os.path.lexists(destination):
            return False
        stage.rename(destination)
        manager.memcache_reset()
        return True
    finally:
        if stage.exists():
            shutil.rmtree(stage)


class MissingPackage(RuntimeError):
    pass


def ensure_package(manager, spec, shared=None, log=print, *, install=True):
    from platformio.package.exception import PackageException
    existing = manager.get_package(spec)
    if existing is not None:
        return existing, False, "installed"
    if not install:
        raise MissingPackage(f"Missing shared package: {spec}")
    if shared is not None:
        try:
            candidate = shared.get_package(spec)
            if candidate is not None and copy_package(candidate, manager, log):
                existing = manager.get_package(spec)
                if existing is not None:
                    return existing, True, "main"
        except (OSError, ValueError, PackageException) as exc:
            log(f"Main package could not be reused ({exc}); preparing it privately.")
    log(f"Preparing missing package: {spec}")
    package = manager.install(spec)
    if package is None:
        raise RuntimeError(f"Package installation did not provide {spec}")
    return package, True, "download"


def prepare(config_path, core, main_core=None, log=print, *, install=True):
    """Prepare upload and build requirements without any device I/O."""
    from platformio import app
    from platformio.dependencies import get_core_dependencies
    from platformio.package.manager.platform import PlatformPackageManager
    from platformio.package.manager.tool import ToolPackageManager
    from platformio.package.meta import PackageSpec
    from platformio.platform.factory import PlatformFactory
    from platformio.project.config import ProjectConfig

    app.set_session_var("custom_project_conf", str(config_path))
    config = ProjectConfig.get_instance(str(config_path))
    platform_spec = PackageSpec(config.get("env:lite", "platform"))
    platforms = PlatformPackageManager(str(core / "platforms"))
    tools = ToolPackageManager(str(core / "packages"))
    shared_platforms = source_manager(PlatformPackageManager, main_core / "platforms") if main_core else None
    shared_tools = source_manager(ToolPackageManager, main_core / "packages") if main_core else None
    actions = []
    # Platform installation must not eagerly download its dependencies before
    # the main store has had a chance to supply their exact matching versions.
    class PlatformOnly(PlatformPackageManager):
        def install(self, spec):
            return super().install(spec, skip_dependencies=True)
    platforms = PlatformOnly(platforms.package_dir)
    if not install:
        platforms = source_manager(PlatformPackageManager, core / "platforms")
        tools = source_manager(ToolPackageManager, core / "packages")
    pkg, changed, origin = ensure_package(platforms, platform_spec, shared_platforms, log, install=install)
    actions.append({"name": pkg.metadata.name, "version": str(pkg.metadata.version), "changed": changed, "origin": origin})
    platform = PlatformFactory.new(pkg)
    platform.project_env = "lite"
    platform.ensure_engine_compatible()
    # This is dependency selection only: never platform.run(), never -t upload.
    platform.configure_project_packages("lite", ["upload"])
    requirements = [("tool-scons", PackageSpec(owner="platformio", name="tool-scons",
        requirements=get_core_dependencies()["tool-scons"]))]
    requirements += [(name, platform.get_package_spec(name)) for name, options in platform.packages.items() if not options.get("optional")]
    for name, spec in requirements:
        pkg, changed, origin = ensure_package(tools, spec, shared_tools, log, install=install)
        actions.append({"name": name, "version": str(pkg.metadata.version), "changed": changed, "origin": origin})
    missing = [name for name, spec in requirements if tools.get_package(spec) is None]
    if missing:
        raise RuntimeError("Target is still missing required tools: " + ", ".join(missing))
    log("Build and upload tools are ready.")
    return {"changed": any(item["changed"] for item in actions), "packages": actions,
        "board": config.get("env:lite", "board"), "framework": config.get("env:lite", "framework"),
        "uploader_packages": [name for name, options in platform.packages.items() if options.get("type") == "uploader" and not options.get("optional")]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--library")
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    state = Path(os.environ["MCU_LITE_STATE"]).resolve(strict=True)
    config = args.config.resolve(strict=True) if args.config else None
    result = args.result.resolve()
    if (config is not None and not config.is_relative_to(state)) or not result.is_relative_to(state):
        raise RuntimeError("Tool preparation accepts only private Lite paths")
    core = Path(os.environ["PLATFORMIO_CORE_DIR"]).resolve()
    shared_value = os.environ.get("MCU_LITE_MAIN_CORE")
    shared = Path(shared_value).resolve() if shared_value else None
    if core != shared and not core.is_relative_to(state):
        raise RuntimeError("Tool preparation accepts only the selected native shared store")
    from isolation import protect_original
    original = Path(__file__).resolve().parents[2]
    events = Path(os.environ.get("MCU_LITE_MAIN_EVENTS") or state / "package-events")
    protect_original(original, writable=(core, events))
    from src.modules.package_jobs import package_store_lease
    report = None
    if args.library:
        import re
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_. +()-]+@[A-Za-z0-9_.+-]+", args.library):
            raise ValueError("Select a versioned library from the registry results")
        from platformio.package.manager.library import LibraryPackageManager
        from platformio.package.meta import PackageSpec
        with package_store_lease(core, "prepare", wait=True, root=events):
            package = LibraryPackageManager(str(core / "lib")).install(PackageSpec(args.library))
            if package is None:
                raise RuntimeError("Library preparation did not return an installed package")
            report = {"library": args.library, "path": package.path}
    elif config is not None:
        # An already prepared target is a reader, just like a build. Only a
        # missing dependency reserves the writer gate and waits for readers.
        with package_store_lease(core, wait=True, root=events):
            try:
                report = prepare(config, core, log=print, install=False)
            except MissingPackage:
                pass
        if report is None:
            with package_store_lease(core, "prepare", wait=True, root=events):
                report = prepare(config, core)
    else:
        raise ValueError("Choose a board configuration or a library")
    scratch = result.with_suffix(".tmp")
    scratch.write_text(json.dumps(report, indent=2), encoding="utf-8")
    scratch.replace(result)


if __name__ == "__main__":
    main()
