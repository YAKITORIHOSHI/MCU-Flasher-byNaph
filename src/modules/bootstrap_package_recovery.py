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
COPY_BUFFER = 1024 * 1024
# Small-file trees benefit from the platform's native copier. Stream only a
# substantial payload whose average file size amortizes the identity checks.
STREAM_MIN_BYTES = 16 * 1024 * 1024
STREAM_MIN_AVERAGE = 256 * 1024


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


def _file_identity(info):
    # Access times change when we read. Keep change/write times, file identity,
    # size and permissions instead; no timestamp-only payload reuse is allowed.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns,
            getattr(info, "st_file_attributes", None), getattr(info, "st_reparse_tag", None))


def _source_inventory(folder):
    """Observe source identities/structure without another payload read."""
    from src.modules.bootstrap_platformio import extended_windows_path
    folder = Path(extended_windows_path(folder))
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"Package copy source is not a directory: {folder}")
    result = {"": ("directory", _file_identity(info))}

    def failed(error):
        raise error

    for root, dirs, files in os.walk(folder, followlinks=False, onerror=failed):
        for name in dirs + files:
            path = Path(root) / name
            info = path.lstat()
            relative = str(path.relative_to(folder))
            identity = _file_identity(info)
            if stat.S_ISLNK(info.st_mode):
                result[relative] = ("link", identity, os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                result[relative] = ("directory", identity)
            elif stat.S_ISREG(info.st_mode):
                result[relative] = ("file", identity)
            else:
                raise RuntimeError(f"Unsupported package payload entry: {path}")
    return result


def _stream_copy_options(args, kwargs, copy2):
    """Preserve custom copier/filter semantics through the original slow path."""
    # Python's native Windows copy2 uses CopyFile2, including attributes and
    # alternate data streams which copystat alone cannot preserve. Keep that
    # native copier and the original two-hash verification until a matching
    # Windows streaming metadata contract is independently qualified.
    if sys.platform == "win32":
        return None
    names = ("symlinks", "ignore", "copy_function", "ignore_dangling_symlinks", "dirs_exist_ok")
    if len(args) > len(names) or set(kwargs) - set(names):
        return None
    options = dict(zip(names, args))
    if set(options) & set(kwargs):
        return None  # Let the original copytree report duplicate arguments.
    options.update(kwargs)
    if (options.get("copy_function", copy2) is not copy2 or options.get("ignore") is not None
            or options.get("ignore_dangling_symlinks", False) is not False
            or options.get("dirs_exist_ok", False) is not False
            or type(options.get("symlinks", False)) is not bool):
        return None
    return options


def _copy_package_stream(source, staging, copier, args, kwargs):
    """Hash the exact source copy stream, then independently verify staged bytes.

    Source staging belongs to the installer. Identity checks detect ordinary
    writes/replacements while copying and before publication; they are not an
    immutable snapshot against another process restoring all native metadata.
    Unsupported/custom copy policies retain the two independent tree hashes.
    """
    def original_copy():
        copier.copytree(source, str(staging), *args, **kwargs)
        if _tree_signature(Path(source)) != _tree_signature(staging, sync=True):
            raise RuntimeError(f"Package copy bytes failed verification; staging retained: {staging}")
        return None

    options = _stream_copy_options(args, kwargs, copier.copy2)
    if options is None:
        return original_copy()
    from src.modules.bootstrap_platformio import extended_windows_path
    source_root = Path(extended_windows_path(source))
    root_info = source_root.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or getattr(root_info, "st_reparse_tag", 0):
        return original_copy()
    inventory = _source_inventory(source_root)
    file_sizes = [item[1][3] for item in inventory.values() if item[0] == "file"]
    total = sum(file_sizes)
    if (not file_sizes or total < STREAM_MIN_BYTES
            or total < len(file_sizes) * STREAM_MIN_AVERAGE):
        return original_copy()

    expected = {}
    for relative, item in inventory.items():
        if relative and item[0] == "directory":
            expected[relative] = ("directory", stat.S_IMODE(item[1][2]))
        elif item[0] == "link":
            expected[relative] = ("link", item[2])

    def changed():
        raise RuntimeError(f"Package source changed during copying; staging retained: {staging}")

    def copy_file(src, dst):
        src = Path(extended_windows_path(src))
        dst = Path(extended_windows_path(dst))
        relative = str(src.relative_to(source_root))
        item = inventory.get(relative)
        if not item or item[0] != "file" or _file_identity(src.lstat()) != item[1]:
            changed()
        identity = item[1]
        digest, copied = hashlib.sha256(), 0
        with src.open("rb") as input_stream, dst.open("xb") as output_stream:
            if _file_identity(os.fstat(input_stream.fileno())) != identity:
                changed()
            while data := input_stream.read(COPY_BUFFER):
                if output_stream.write(data) != len(data):
                    raise OSError(f"Incomplete package staging write: {dst}")
                digest.update(data)
                copied += len(data)
            if copied != identity[3] or _file_identity(os.fstat(input_stream.fileno())) != identity:
                changed()
        if _file_identity(src.lstat()) != identity:
            changed()
        copier.copystat(src, dst)  # Retain copy2 permissions/timestamps/xattrs.
        expected[relative] = ("file", copied, stat.S_IMODE(identity[2]), digest.hexdigest())
        return str(dst)

    options["copy_function"] = copy_file
    # Keep the captured archive_paths proxy: it owns native Windows spellings.
    copier.copytree(source, str(staging), **options)
    if _source_inventory(source_root) != inventory:
        changed()
    if (expected != _tree_signature(staging, sync=True)
            or stat.S_IMODE(staging.lstat().st_mode) != stat.S_IMODE(inventory[""][1][2])):
        raise RuntimeError(f"Package copy bytes failed verification; staging retained: {staging}")
    return source_root, inventory


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
        source_receipt = _copy_package_stream(source, staging, original_shutil, args, kwargs)
        _sync_dir(stage_parent)
        if source_receipt and _source_inventory(source_receipt[0]) != source_receipt[1]:
            raise RuntimeError(f"Package source changed before publication; staging retained: {staging}")
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
