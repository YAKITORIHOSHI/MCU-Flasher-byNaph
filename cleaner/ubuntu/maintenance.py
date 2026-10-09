#!/usr/bin/env python3
"""Preview and explicitly remove owned Ubuntu maintenance resources only."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
SOURCE_TREES = ("main", "src/modules", "src/dbs", "direct", "cleaner", "DANGER-ZONE")
PROTECTED = frozenset((".mcu_flasher_build_cache", ".mcu_ai_edits", ".pio", ".vscode",
                       ".clangd", ".opencode", ".git", "node_modules", "env", "_python",
                       ".venv-linux", ".ubuntu-tools", "offline-extras", ".platformio-mcu-gui"))
CORE_NAMES = frozenset(("packages", "platforms", "lib", "penv", "arduino-cli", ".cache",
                        ".tmp", "appstate.json", "homestate.json", ".mcu-offline-ready.json",
                        ".mcu-offline-catalog.json", ".mcu-bootstrap-board-coverage.json",
                        ".mcu-index-targets.json", "packages.lock", "platforms.lock", "lib.lock",
                        "appstate.json.lock", "homestate.json.lock"))
CORE_LOCK_NAMES = ("packages.lock", "platforms.lock", "lib.lock", "appstate.json.lock", "homestate.json.lock")
MAX_ENTRIES = 250000
MAX_DEPTH = 64
SCAN_SECONDS = 30
DELETE_SECONDS = 120
PROC_LIMIT = 16384


class MaintenanceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    path: Path
    anchor: Path
    kind: str


class Budget:
    def __init__(self, seconds=SCAN_SECONDS, entries=MAX_ENTRIES):
        self.deadline = time.monotonic() + seconds
        self.remaining = entries

    def check(self):
        self.remaining -= 1
        if self.remaining < 0 or time.monotonic() > self.deadline:
            raise MaintenanceError("Maintenance limit reached; remaining files were retained.")


def _exists(path):
    return path.exists() or path.is_symlink()


def _linked(path):
    value = path.lstat()
    return stat.S_ISLNK(value.st_mode) or bool(getattr(value, "st_file_attributes", 0) & 0x400)


def _contained(path, anchor):
    path, anchor = Path(path), Path(anchor)
    try:
        path.relative_to(anchor)
    except ValueError as error:
        raise MaintenanceError(f"Target leaves its approved namespace: {path}") from error
    if path == anchor or anchor == Path(anchor.anchor):
        raise MaintenanceError(f"Refusing a namespace or filesystem root: {path}")
    current = path
    while True:
        if _exists(current) and _linked(current):
            raise MaintenanceError(f"Linked deletion root or parent was retained: {current}")
        if current == anchor:
            break
        current = current.parent
    if path.resolve() != path.absolute() or anchor.resolve() != anchor.absolute():
        raise MaintenanceError(f"The target resolves through a linked namespace: {path}")


def _read_json(path, limit=8192):
    if not path.is_file() or _linked(path):
        raise MaintenanceError(f"Ownership receipt is unavailable: {path}")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise MaintenanceError(f"Ownership receipt exceeds its limit: {path}")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise MaintenanceError(f"Ownership receipt is invalid: {path}") from error
    if not isinstance(result, dict):
        raise MaintenanceError(f"Ownership receipt is invalid: {path}")
    return result


def _children(path, limit=MAX_ENTRIES):
    result = []
    deadline = time.monotonic() + SCAN_SECONDS
    with os.scandir(path) as entries:
        for entry in entries:
            if len(result) >= limit or time.monotonic() > deadline:
                raise MaintenanceError(f"Directory inventory exceeds its limit: {path}")
            result.append(entry)
    return result


def _known_children(path, allowed):
    names = {entry.name for entry in _children(path)}
    unknown = sorted(names - allowed)
    if unknown:
        raise MaintenanceError(f"Unrecognized content was retained in {path}: {', '.join(unknown[:8])}")


def _validate_venv(path):
    cfg = path / "pyvenv.cfg"
    if not cfg.is_file() or _linked(cfg) or cfg.stat().st_size > 8192:
        raise MaintenanceError(f"The native environment has no valid pyvenv.cfg: {path}")
    fields = {}
    for line in cfg.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    if (not fields.get("home") or not re.fullmatch(r"3\.\d+(?:\.\d+)?", fields.get("version", ""))
            or fields.get("include-system-site-packages", "").lower() != "false"):
        raise MaintenanceError(f"The native environment does not match an isolated Python 3 venv: {path}")
    _known_children(path, {"bin", "lib", "lib64", "include", "share", "etc", "pyvenv.cfg"})
    for name in ("bin", "lib", "include", "share", "etc"):
        child = path / name
        if _exists(child) and (not child.is_dir() or _linked(child)):
            raise MaintenanceError(f"Unexpected native environment directory was retained: {child}")


def _validate_tool(path):
    name = path.name
    receipt = _read_json(path / "installation.json")
    owner = "mcu-flasher-ubuntu-" + name
    if receipt.get("owner") != owner:
        raise MaintenanceError(f"Native tool ownership does not match: {path}")
    executable = path / name
    if not executable.is_file() or _linked(executable):
        raise MaintenanceError(f"Native tool executable is unavailable: {path}")
    if name == "arduino-cli":
        valid = (receipt.get("architecture") == "linux-amd64"
                 and receipt.get("executable") == name
                 and re.fullmatch(r"[a-fA-F0-9]{64}", str(receipt.get("executable_sha256", ""))))
    else:
        info = executable.stat()
        valid = (receipt.get("schema") == 1 and receipt.get("size") == info.st_size
                 and receipt.get("mtime_ns") == info.st_mtime_ns
                 and re.fullmatch(r"[a-fA-F0-9]{64}", str(receipt.get("binary_sha256", ""))))
    with executable.open("rb") as stream:
        valid = valid and stream.read(4) == b"\x7fELF"
    if not valid:
        raise MaintenanceError(f"Native tool receipt is invalid: {path}")
    _known_children(path, {name, "installation.json"})


def _validate_extras(path, root):
    expected = {"schema": 1, "installation": str(root).casefold(), "host": "linux",
                "purpose": "MCU Flasher offline preparation extras"}
    if _read_json(path / ".mcu-offline-extras.json") != expected:
        raise MaintenanceError(f"Linux extras belong to another installation: {path}")
    _known_children(path, {".mcu-offline-extras.json", "readiness.json", "archives"})


def _validate_core(path, architecture):
    receipt = _read_json(path / ".mcu-offline-ready.json", limit=8 * 1024 * 1024)
    if (receipt.get("host") != "linux" or receipt.get("architecture") != architecture
            or receipt.get("schema") != 4 or not isinstance(receipt.get("files"), list)
            or not receipt["files"] or not re.fullmatch(r"[a-fA-F0-9]{64}", str(receipt.get("plan", "")))):
        raise MaintenanceError(f"The package store lacks a matching Linux architecture receipt: {path}")
    _known_children(path, CORE_NAMES)


def _allowed_venv_link(path, target):
    relative = path.relative_to(target.path)
    if relative == Path("lib64"):
        return path.resolve() == (target.path / "lib").resolve()
    if relative.parent == Path("bin") and re.fullmatch(r"python(?:3(?:\.\d+)?)?", relative.name):
        resolved = path.resolve()
        return resolved.is_file() and re.fullmatch(r"python(?:3(?:\.\d+)?)?", resolved.name) is not None
    return False


def _snapshot(target, budget):
    """Validate the complete finite tree before deleting its first entry."""
    _contained(target.path, target.anchor)
    if target.kind == "bytecode-directory" and not target.path.is_dir():
        raise MaintenanceError(f"Unrecognized bytecode cache entry was retained: {target.path}")
    nodes = []
    pending = [(target.path, 0)]
    while pending:
        path, depth = pending.pop()
        budget.check()
        if depth > MAX_DEPTH:
            raise MaintenanceError(f"Directory nesting exceeds its limit: {path}")
        info = path.lstat()
        linked = _linked(path)
        if linked and not (target.kind == "venv" and _allowed_venv_link(path, target)):
            raise MaintenanceError(f"Unexpected link was retained: {path}")
        if not linked and not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise MaintenanceError(f"Special filesystem entry was retained: {path}")
        nodes.append((path, (info.st_dev, info.st_ino, info.st_mode)))
        if stat.S_ISDIR(info.st_mode) and not linked:
            children = _children(path)
            if target.kind == "bytecode-directory" and any(
                    not entry.is_file(follow_symlinks=False) or Path(entry.name).suffix not in (".pyc", ".pyo")
                    for entry in children):
                raise MaintenanceError(f"Unrecognized bytecode cache content was retained: {path}")
            pending.extend((Path(entry.path), depth + 1) for entry in children)
    return nodes


def _bytecode_targets(root, budget):
    targets = []
    for entry in _children(root):
        budget.check()
        path = Path(entry.path)
        if entry.name == "__pycache__":
            targets.append(Target(path, root, "bytecode-directory"))
        elif path.suffix in (".pyc", ".pyo") and entry.is_file(follow_symlinks=False):
            targets.append(Target(path, root, "bytecode-file"))
    for relative in SOURCE_TREES:
        base = root / relative
        if not _exists(base):
            continue
        _contained(base, root)
        if not base.is_dir():
            raise MaintenanceError(f"Expected application source directory is unavailable: {base}")
        pending = [(base, 0)]
        while pending:
            directory, depth = pending.pop()
            if depth > MAX_DEPTH:
                raise MaintenanceError(f"Source nesting exceeds its limit: {directory}")
            for entry in _children(directory):
                budget.check()
                path = Path(entry.path)
                if entry.name in PROTECTED or entry.is_symlink() or _linked(path):
                    continue
                if entry.name == "__pycache__":
                    targets.append(Target(path, root, "bytecode-directory"))
                elif entry.is_dir(follow_symlinks=False):
                    pending.append((path, depth + 1))
                elif path.suffix in (".pyc", ".pyo") and entry.is_file(follow_symlinks=False):
                    targets.append(Target(path, root, "bytecode-file"))
    return targets


def native_store(environment=None, home=None, architecture=None):
    environment = os.environ if environment is None else environment
    home = Path.home() if home is None else Path(home)
    architecture = platform.machine() if architecture is None else architecture
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", architecture) or architecture in (".", ".."):
        raise MaintenanceError("Native architecture is invalid.")
    base = Path(environment.get("XDG_DATA_HOME") or home / ".local/share")
    if not base.is_absolute() or base == Path(base.anchor) or base.resolve() != base.absolute():
        raise MaintenanceError("XDG_DATA_HOME must be an absolute local namespace without links.")
    return base / "mcu-flasher/platformio" / architecture, base


def build_plan(root=ROOT, mode="bytecode", include_native_store=False, *, environment=None,
               home=None, architecture=None):
    root = Path(root).absolute()
    if root.resolve() != root or root == Path(root.anchor):
        raise MaintenanceError("The application root must be a local directory without links.")
    for name in ("mcu_flash_gui.py", "direct/ubuntu/run.sh"):
        marker = root / name
        if not marker.is_file() or _linked(marker):
            raise MaintenanceError("The script is not in a verified MCU Flasher application checkout.")
    if mode not in ("bytecode", "fresh", "runtime"):
        raise MaintenanceError("Unknown maintenance mode.")
    if include_native_store and mode != "runtime":
        raise MaintenanceError("--include-native-store requires runtime mode.")
    budget = Budget()
    targets = _bytecode_targets(root, budget)
    if mode in ("fresh", "runtime"):
        venv = root / ".venv-linux"
        if _exists(venv):
            _contained(venv, root)
            _validate_venv(venv)
            targets.append(Target(venv, root, "venv"))
    if mode == "runtime":
        tools = root / ".ubuntu-tools"
        if _exists(tools):
            _contained(tools, root)
            _known_children(tools, {"arduino-cli", "opencode", ".arduino-cli.lock", ".opencode.lock"})
            for name in ("arduino-cli", "opencode"):
                tool = tools / name
                if _exists(tool):
                    _contained(tool, root)
                    _validate_tool(tool)
                    targets.append(Target(tool, root, "tool"))
        extras = root / "src/offline-extras/linux"
        if _exists(extras):
            _contained(extras, root)
            _validate_extras(extras, root)
            targets.append(Target(extras, root, "extras"))
        if include_native_store:
            core, anchor = native_store(environment, home, architecture)
            if _exists(core):
                _contained(core, anchor)
                _validate_core(core, architecture or platform.machine())
                targets.append(Target(core, anchor, "shared-native-store"))
    # Cache roots cannot overlap; source traversal intentionally excludes runtime trees.
    result = []
    for target in targets:
        result.append((target, _snapshot(target, budget)))
    return result


def running_processes(root, plan, *, proc_root=Path("/proc")):
    """Passively inventory same-account processes; do not stop any process."""
    proc_root = Path(proc_root)
    if not proc_root.is_dir():
        raise MaintenanceError("Native /proc process inventory is unavailable; cleanup was refused.")
    roots = [Path(root) / ".venv-linux", Path(root) / ".ubuntu-tools"]
    native_roots = [target.path for target, _ in plan if target.kind == "shared-native-store"]
    roots.extend(native_roots)
    entry_points = ("mcu_flash_gui.py", "launcher.py", "bootstrap.py", "launch.py", "setup.py",
                    "offline_bootstrap.py", "board_preparation.py")
    blockers = []
    budget = Budget(seconds=5, entries=PROC_LIMIT)
    for entry in _children(proc_root, limit=PROC_LIMIT + 128):
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        budget.check()
        directory = Path(entry.path)
        try:
            if hasattr(os, "geteuid") and directory.stat().st_uid != os.geteuid():
                continue
            with (directory / "cmdline").open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise MaintenanceError(f"Process {entry.name} command line exceeds its inspection limit.")
            argv = [item.decode("utf-8", errors="replace") for item in raw.split(b"\0") if item]
            try:
                executable = os.readlink(directory / "exe")
            except FileNotFoundError:
                continue
            candidates = argv + [executable]
            owned_resource = any(value == str(base) or value.startswith(str(base) + os.sep)
                                 for value in candidates for base in roots)
            app_entry = any(value == str(Path(root) / name) or
                            value.startswith(str(root) + os.sep) and Path(value).name in entry_points
                            for value in argv for name in entry_points)
            shared_environment = False
            if native_roots:
                with (directory / "environ").open("rb") as stream:
                    environment = stream.read(262145)
                if len(environment) > 262144:
                    raise MaintenanceError(f"Process {entry.name} environment exceeds its inspection limit.")
                keys = {b"PLATFORMIO_CORE_DIR", b"PLATFORMIO_PACKAGES_DIR", b"PLATFORMIO_PLATFORMS_DIR",
                        b"PLATFORMIO_CACHE_DIR", b"PLATFORMIO_BUILD_CACHE_DIR", b"PLATFORMIO_GLOBALLIB_DIR",
                        b"PLATFORMIO_PENV_DIR"}
                for variable in environment.split(b"\0"):
                    key, separator, value = variable.partition(b"=")
                    if not separator or key not in keys:
                        continue
                    spelling = value.decode("utf-8", errors="replace")
                    if not os.path.isabs(spelling):
                        continue
                    normalized = os.path.normcase(os.path.abspath(spelling))
                    if any(normalized == os.path.normcase(str(base)) or
                           normalized.startswith(os.path.normcase(str(base)) + os.sep)
                           for base in native_roots):
                        shared_environment = True
                        break
            if owned_resource or app_entry or shared_environment:
                blockers.append(f"PID {entry.name}: {Path(executable).name}")
                if len(blockers) >= 16:
                    break
        except FileNotFoundError:
            continue
        except OSError as error:
            raise MaintenanceError(f"Process {entry.name} could not be checked; cleanup was refused.") from error
    return blockers


def assert_native_locks_idle(plan):
    """Probe only finite existing lockfiles, without truncating or creating them."""
    paths = [target.path / name for target, _ in plan if target.kind == "shared-native-store"
             for name in CORE_LOCK_NAMES if _exists(target.path / name)]
    if not paths:
        return
    try:
        import fcntl
    except ImportError as error:
        raise MaintenanceError("Native file-lock inspection is unavailable; the package store was retained.") from error
    for path in paths:
        if _linked(path) or not path.is_file():
            raise MaintenanceError(f"Unexpected native lock entry was retained: {path}")
        descriptor = None
        acquired = False
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            raise MaintenanceError(f"Native package lock is held or cannot be checked; store retained: {path}") from error
        finally:
            if descriptor is not None:
                try:
                    if acquired:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)


def _delete_node(path, identity, anchor):
    """Use native descriptor-relative deletion so links cannot redirect traversal."""
    parent = path.parent
    relative = parent.relative_to(anchor)
    descriptors = []
    if os.unlink in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptors.append(os.open(anchor, flags))
            for component in relative.parts:
                descriptors.append(os.open(component, flags, dir_fd=descriptors[-1]))
            current = os.stat(path.name, dir_fd=descriptors[-1], follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_mode) != identity:
                raise MaintenanceError(f"A maintenance target changed; remaining files were retained: {path}")
            if stat.S_ISDIR(current.st_mode):
                os.rmdir(path.name, dir_fd=descriptors[-1])
            else:
                os.unlink(path.name, dir_fd=descriptors[-1])
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
    else:
        # Used only by isolated verification on hosts without descriptor APIs.
        _contained(parent, anchor) if parent != anchor else None
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_mode) != identity:
            raise MaintenanceError(f"A maintenance target changed: {path}")
        path.rmdir() if stat.S_ISDIR(current.st_mode) else path.unlink()


def apply_plan(root, plan, *, proc_root=Path("/proc")):
    if not sys.platform.startswith("linux"):
        raise MaintenanceError("Ubuntu maintenance mutations require native Linux.")
    venv = Path(root) / ".venv-linux"
    if (Path(sys.executable).absolute().is_relative_to(venv)
            or Path(sys.prefix).absolute().is_relative_to(venv)):
        raise MaintenanceError("Run maintenance with system Python, outside .venv-linux.")
    blockers = running_processes(root, plan, proc_root=proc_root)
    if blockers:
        raise MaintenanceError("Close MCU Flasher and its package workers first: " + "; ".join(blockers))
    assert_native_locks_idle(plan)
    # Revalidate ownership and every tree before any mutation, including changes
    # made while the user was reviewing or confirming the preview.
    scan_budget = Budget()
    snapshots = []
    for target, _ in plan:
        _contained(target.path, target.anchor)
        if target.kind == "venv":
            _validate_venv(target.path)
        elif target.kind == "tool":
            _validate_tool(target.path)
        elif target.kind == "extras":
            _validate_extras(target.path, Path(root))
        elif target.kind == "shared-native-store":
            _validate_core(target.path, target.path.name)
        snapshots.append((target, _snapshot(target, scan_budget)))
    blockers = running_processes(root, plan, proc_root=proc_root)
    if blockers:
        raise MaintenanceError("An MCU Flasher process started; cleanup was refused: " + "; ".join(blockers))
    assert_native_locks_idle(plan)
    budget = Budget(seconds=DELETE_SECONDS)
    removed = 0
    last_process_check = time.monotonic()
    for target, nodes in snapshots:
        _contained(target.path, target.anchor)
        for path, identity in reversed(nodes):
            budget.check()
            if time.monotonic() - last_process_check >= 1:
                blockers = running_processes(root, plan, proc_root=proc_root)
                if blockers:
                    raise MaintenanceError("An MCU Flasher process started; remaining files were retained: " + "; ".join(blockers))
                assert_native_locks_idle(plan)
                last_process_check = time.monotonic()
            _delete_node(path, identity, target.anchor)
            removed += 1
    return removed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("bytecode", "fresh", "runtime"))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--preview", action="store_true", help="Inspect only (the default).")
    action.add_argument("--apply", action="store_true", help="Apply the displayed maintenance plan.")
    parser.add_argument("--yes", action="store_true", help="Skip typed confirmation for intentional automation.")
    parser.add_argument("--include-native-store", action="store_true",
                        help="Also remove the user-wide native package store for this architecture.")
    args = parser.parse_args(argv)
    try:
        if args.apply and not sys.platform.startswith("linux"):
            raise MaintenanceError("Use Windows maintenance utilities on Windows; Ubuntu mutation was refused.")
        if args.yes and not args.apply:
            raise MaintenanceError("--yes requires --apply.")
        plan = build_plan(ROOT, mode=args.mode, include_native_store=args.include_native_store)
        print(f"Ubuntu {args.mode} maintenance: {'APPLY' if args.apply else 'PREVIEW'}")
        print("Preserved: sketches, settings, logs, recovery journals, protected build caches and Windows resources.")
        if args.include_native_store:
            print("The native package store is shared by all MCU Flasher checkouts for this account and architecture.")
        for target, nodes in plan:
            print(f"  {target.path} ({len(nodes)} entries)")
        if not plan:
            print("No owned resources require cleanup.")
            return 0
        if not args.apply:
            print("Preview only. Add --apply to perform this plan.")
            return 0
        phrase = "RESET MCU UBUNTU" if args.mode == "runtime" else "CLEAN MCU UBUNTU"
        if not args.yes and input(f"Type {phrase} to continue: ") != phrase:
            print("Cancelled. Nothing was changed.")
            return 2
        removed = apply_plan(ROOT, plan)
        print(f"Removed {removed} owned entries. Relaunch Ubuntu Bootstrap online to prepare removed dependencies.")
        return 0
    except (MaintenanceError, OSError, EOFError, UnicodeError, ValueError) as error:
        print(f"Maintenance refused or incomplete: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
