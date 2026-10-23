"""Bootstrap-only preserved-package recovery and complete-tree publication.

Never manufacture .piopm provenance for a damaged tree. Preserve the original
outside PIO's enumerated stores, then let the normal checksum-validating package
manager install its replacement. Staging is retained on interruption.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from uuid import uuid4

KINDS = ("packages", "platforms", "lib")
MARKER = ".mcu-offline-ready.json"
LIMIT = 524288


def _bootstrap_only():
    if os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME") or os.environ.get("MCU_FLASHER_WORKSPACE_RUNTIME"):
        raise RuntimeError("Package recovery belongs to Bootstrap, outside the workspace process")


def _local(path, core):
    """Allow the application's short core junction; reject redirects below it."""
    path, core = Path(os.path.abspath(path)), Path(os.path.abspath(core))
    try:
        parts = path.relative_to(core).parts
    except ValueError as exc:
        raise RuntimeError(f"Package path leaves configured core: {path}") from exc
    current = core
    for part in parts:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise RuntimeError(f"Package path is redirected by a link: {current}")
    if not path.resolve().is_relative_to(core.resolve()):
        raise RuntimeError(f"Package path is redirected outside configured core: {path}")
    return path


def _sync_dir(path):
    if sys.platform == "win32":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json(path, value):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    # On failure preserve the old destination and the interrupted temporary.
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _sync_dir(path.parent)


def _read(path):
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("Package metadata exceeds the supported byte limit")
    return data


def _damage(folder, manager, core):
    """Return only proven metadata/manifest damage; access errors propagate."""
    from platformio.package.meta import PackageMetadata, PackageSpec
    metadata = next((folder / name for name in
                     (".git/.piopm", ".hg/.piopm", ".svn/.piopm", ".piopm")
                     if os.path.lexists(folder / name)), None)
    if metadata is None:
        return None  # Unknown local/user folders have no owned provenance.
    _local(metadata, core)
    raw = _read(metadata)  # Permission errors never authorize quarantine.
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("type") != manager.pkg_type
                or not isinstance(value.get("name"), str) or not value["name"]
                or not isinstance(value.get("version"), str) or not value["version"]
                or not isinstance(value.get("spec"), dict) or not value["spec"]):
            raise ValueError("Invalid PlatformIO metadata shape")
        spec = dict(value["spec"])
        if "url" in spec:
            spec["uri"] = spec.pop("url")
        value["spec"] = PackageSpec(**spec)
        parsed = PackageMetadata(**value)
    except (ValueError, TypeError, KeyError, AssertionError, AttributeError, UnicodeError) as exc:
        return f"Invalid {metadata.name}: {exc}"
    manifest = next((folder / name for name in manager.manifest_names if os.path.lexists(folder / name)), None)
    if manifest is None:
        return "Declared package manifest is missing"
    _local(manifest, core)
    raw = _read(manifest)
    try:
        if not raw:
            raise ValueError("Empty declared package manifest")
        if manifest.suffix == ".json":
            declared = json.loads(raw)
            if not isinstance(declared, dict):
                raise ValueError("Declared package manifest is not an object")
            if declared.get("name") and declared["name"] != parsed.name:
                raise ValueError("Package manifest identity differs from .piopm")
            if declared.get("version"):
                from platformio.package.version import cast_version_to_semver
                if cast_version_to_semver(declared["version"]) != parsed.version:
                    raise ValueError("Package manifest version differs from .piopm")
    except (ValueError, TypeError, AssertionError, UnicodeError) as exc:
        return f"Invalid {manifest.name}: {exc}"
    return None


def recover_packages(core, log=print):
    """Preserve provably damaged direct package groups before PIO enumeration."""
    _bootstrap_only()
    from src.modules.bootstrap_seed import managers
    core = Path(os.path.abspath(core))
    for kind in KINDS:
        _local(core / kind, core)
    if not any((core / kind).exists() for kind in KINDS):
        return []
    stores = managers(core)
    results = []
    for kind, manager in stores.items():
        if not (core / kind).exists():
            continue
        manager.lock()
        try:
            for folder in sorted((core / kind).iterdir()):
                if folder.name.startswith("_tmp_installing"):
                    continue
                _local(folder, core)
                if not folder.is_dir():
                    continue
                reason = _damage(folder, manager, core)
                if reason is None:
                    continue
                recovery = _local(core / ".mcu-package-recovery", core)
                recovery.mkdir(exist_ok=True)
                group = recovery / uuid4().hex
                group.mkdir()
                destination = group / kind / folder.name
                destination.parent.mkdir()
                _sync_dir(recovery)
                _sync_dir(group)
                metadata = {}
                for relative in (".piopm", ".git/.piopm", ".hg/.piopm", ".svn/.piopm", *manager.manifest_names):
                    path = folder / relative
                    if path.is_file():
                        _local(path, core)
                        data = _read(path)
                        metadata[relative] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                receipt = {"schema": 1, "original": str(folder), "preserved": str(destination),
                           "receipt": str(group / "receipt.json"), "reason": reason,
                           "metadata": metadata, "state": "pending"}
                _write_json(group / "receipt.json", receipt)
                _local(core / MARKER, core).unlink(missing_ok=True)
                _sync_dir(core)
                os.rename(folder, destination)  # Same filesystem, entire original retained.
                _sync_dir(folder.parent)
                _sync_dir(destination.parent)
                receipt["state"] = "preserved"
                _write_json(group / "receipt.json", receipt)
                log(f"Preserved damaged package {folder.name}: {reason}. Original files: {destination}. "
                    "Bootstrap will verify a complete replacement before marking it ready.")
                results.append(receipt)
            manager.memcache_reset()
        finally:
            manager.unlock()
    return results


def _tree_signature(folder, *, sync=False):
    from src.modules.bootstrap_platformio import extended_windows_path
    folder = Path(extended_windows_path(folder))
    result = {}
    for root, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            path = Path(root) / name
            info = path.lstat()
            relative = str(path.relative_to(folder))
            if stat.S_ISLNK(info.st_mode):
                result[relative] = ("link", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                result[relative] = ("directory", stat.S_IMODE(info.st_mode))
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    while data := stream.read(1024 * 1024):
                        digest.update(data)
                    # Windows FlushFileBuffers needs a writable handle; source
                    # archives may legitimately contain read-only payloads.
                    # Publication still waits for the complete verified copy.
                    if sync and sys.platform != "win32":
                        os.fsync(stream.fileno())
                result[relative] = ("file", info.st_size, stat.S_IMODE(info.st_mode), digest.hexdigest())
            else:
                raise RuntimeError(f"Unsupported package payload entry: {path}")
        if sync:
            _sync_dir(root)
    return result


def _publish_absent(source, destination):
    if os.path.lexists(destination):
        raise RuntimeError(f"Package destination appeared; existing files retained: {destination}")
    if sys.platform.startswith("linux"):
        # renameat2 prevents replacing even an empty directory in the check/rename gap.
        import ctypes
        native = ctypes.CDLL(None, use_errno=True)
        if native.renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(destination))
    else:
        from src.modules.bootstrap_platformio import extended_windows_path
        # Windows rename refuses an existing destination, including empty trees.
        os.rename(extended_windows_path(source), extended_windows_path(destination))
    _sync_dir(destination.parent)


@contextmanager
def atomic_package_writes(core):
    """Scope durable metadata and verified tree publication to this app core."""
    _bootstrap_only()
    if core is None:
        yield
        return
    from platformio.package.meta import PackageMetadata
    from platformio.package.manager import _install as installer
    core = Path(os.path.abspath(core))
    original_dump, original_shutil = PackageMetadata.dump, installer.shutil

    def owned(path):
        return Path(os.path.abspath(path)).is_relative_to(core)

    def dump(item, path):
        if not owned(path):
            return original_dump(item, path)
        path = _local(path, core)
        _write_json(path, item.as_dict())

    def copytree(source, destination, *args, **kwargs):
        destination_path = Path(os.path.abspath(destination))
        direct = any(destination_path.parent == core / kind for kind in KINDS)
        if not direct:
            return original_shutil.copytree(source, destination, *args, **kwargs)
        destination_path = _local(destination_path, core)
        if os.path.lexists(destination_path):
            raise RuntimeError(f"Package destination already exists; existing files retained: {destination_path}")
        stage_parent = _local(core / ".mcu-package-staging", core)
        stage_parent.mkdir(exist_ok=True)
        staging = stage_parent / uuid4().hex
        # Upstream handles native extended Windows paths via its captured proxy.
        original_shutil.copytree(source, str(staging), *args, **kwargs)
        if _tree_signature(Path(source)) != _tree_signature(staging, sync=True):
            raise RuntimeError(f"Package copy bytes failed verification; staging retained: {staging}")
        _sync_dir(stage_parent)
        _publish_absent(staging, destination_path)
        _sync_dir(stage_parent)
        return destination

    class ShutilProxy:
        def __getattr__(self, name):
            return copytree if name == "copytree" else getattr(original_shutil, name)

    PackageMetadata.dump, installer.shutil = dump, ShutilProxy()
    try:
        yield
    finally:
        PackageMetadata.dump, installer.shutil = original_dump, original_shutil
