#!/usr/bin/env python3
"""Prepare the complete declared offline package set, in bootstrap only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as host_platform
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SCHEMA = 1
MARKER = ".mcu-offline-ready.json"
ASSETS = (
    "src/editor/index.html", "src/editor/bundle.js", "src/editor/18.bundle.js",
    "src/editor/editor.worker.js", "src/editor/8f3abbcbc983396e1f13.ttf",
    "src/editor/qwebchannel.js", "src/editor/terminal.html",
    "src/assets/xterm/xterm.css", "src/assets/xterm/xterm.js", "src/assets/xterm/xterm-addon-fit.js",
)


def load_plan(path=None):
    path = Path(path or ROOT / "direct/offline-packages.json")
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict) or plan.get("schema") != 1:
        raise ValueError("Invalid offline bootstrap package plan")
    for key in ("platforms", "libraries"):
        entries = plan.get(key, [])
        if not isinstance(entries, list) or (key == "platforms" and not entries):
            raise ValueError(f"Invalid {key} in offline package plan")
        for name in entries:
            if not isinstance(name, str) or not name.strip() or any(c in name for c in "\r\n"):
                raise ValueError(f"Invalid {key} specification in offline package plan")
    return plan


def requested_plan(argv=None):
    """Return an explicit bootstrap plan and its path, or the default plan."""
    argv = list(sys.argv[1:] if argv is None else argv)
    path = None
    if "--plan" in argv:
        index = argv.index("--plan")
        if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
            raise ValueError("--plan requires the path to an offline package plan")
        path = Path(argv[index + 1]).resolve()
    return load_plan(path), path


def plan_hash(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def ready(core, plan=None):
    """Read-only launch gate; changed/deleted packages require bootstrap repair."""
    try:
        data = json.loads((Path(core) / MARKER).read_text(encoding="utf-8"))
        expected = plan_hash(plan or load_plan())
        actual = data.get("plan") if plan is not None else data.get("default_plan")
        if data.get("schema") != SCHEMA or actual != expected:
            return False
        if data.get("host") != sys.platform:
            return False
        if data.get("architecture") != host_platform.machine():
            return False
        if not all((ROOT / item).is_file() for item in ASSETS):
            return False
        if not all((ROOT / item).is_file() for item in data.get("guards", [])):
            return False
        return all((Path(core) / item).is_file() for item in data["files"]) and bool(data["files"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def clean_bootstrap_environment(env=None):
    env = dict(os.environ if env is None else env)
    for name in ("MCU_FLASHER_OFFLINE_RUNTIME", "PIP_NO_INDEX", "PYTHONHOME", "PYTHONPATH"):
        env.pop(name, None)
    env["PLATFORMIO_NO_TELEMETRY"] = "1"
    env["PLATFORMIO_DISABLE_UPGRADE_CHECK"] = "1"
    return env


def install_runtime_guard(core=None):
    """Guard Python children of the app; bootstrap itself remains online."""
    import sysconfig
    directories = {Path(sysconfig.get_paths()["purelib"])}
    if sys.platform == "win32":
        directories.update(ROOT / item for item in ("env/Lib/site-packages", "src/_python/Lib/site-packages"))
        if core:
            directories.add(Path(core) / "penv/Lib/site-packages")
    code = "import os, sys; sys.path.insert(0, os.environ.get('MCU_FLASHER_APP_ROOT', '')) if os.environ.get('MCU_FLASHER_OFFLINE_RUNTIME') else None; __import__('src.modules.offline_runtime', fromlist=['activate']).activate() if os.environ.get('MCU_FLASHER_OFFLINE_RUNTIME') else None\n"
    hooks = []
    for directory in directories:
        if not directory.is_dir():
            continue
        hook = directory / "mcu_flasher_offline.pth"
        if not hook.resolve().is_relative_to(ROOT.resolve()):
            raise RuntimeError("Bootstrap must use this application's private Python environment")
        hook.write_text(code, encoding="utf-8")
        hooks.append(str(hook.resolve().relative_to(ROOT.resolve())))
    return hooks


def package_plan(platform, boards):
    """Include optional upload/debug tools and every board/framework variant."""
    from platformio.package.meta import PackageSpec
    from platformio.platform.factory import PlatformFactory
    specs = {}
    probes = {}
    configurations = [(None, None)] + [(board.id, framework) for board in boards.values()
                                      for framework in board.get("frameworks", [])]
    for board_id, framework in configurations:
        instance = PlatformFactory.new(platform)
        if board_id:
            options = {"board": board_id, "framework": [framework], "pioframework": [framework]}
            instance.configure_default_packages(options, ["upload", "buildfs", "bootloader", "debug"])
        required = []
        for name, metadata in instance.packages.items():
            # Optional packages are needed for a board/framework/transport the
            # user may choose later. Do not assume Arduino/serial defaults.
            for version in [metadata.get("version"), *metadata.get("optionalVersions", [])]:
                if not version:
                    continue
                spec = PackageSpec(owner=metadata.get("owner"), name=name, requirements=version)
                specs[spec.humanize()] = spec
                if not metadata.get("optional"):
                    required.append(spec.humanize())
        if board_id:
            # Identical package sets need one builder-preparation probe. MCU
            # and core still distinguish framework-generated host tools.
            board = boards[board_id]
            key = (framework, board.get("build.mcu", ""), board.get("build.core", ""), tuple(sorted(required)))
            probes.setdefault(key, (board_id, framework))
    return list(specs.values()), list(probes.values())


def verify_package_dependencies(manager, package):
    """Do not certify dependencies that PlatformIO skipped with a warning."""
    pending, seen = [package], set()
    while pending:
        item = pending.pop()
        path = str(Path(item.path).resolve())
        if path in seen:
            continue
        seen.add(path)
        for dependency in manager.get_pkg_dependencies(item) or ():
            spec = manager.dependency_to_spec(dependency)
            installed = manager.get_package(spec)
            if not installed:
                builtin = getattr(manager, "is_builtin_lib", None)
                if callable(builtin) and not spec.external and not spec.owner and builtin(spec.name):
                    continue  # Provided by a prepared framework, e.g. Wire/SPI.
                raise RuntimeError(f"Offline dependency preparation incomplete: {spec.humanize()}")
            pending.append(installed)


def prepare(core, plan=None, log=print):
    if os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME"):
        raise RuntimeError("Offline packages can only be prepared by bootstrap, outside the workspace process")
    from platformio.package.manager.platform import PlatformPackageManager
    from platformio.package.manager.tool import ToolPackageManager
    from platformio.package.manager.library import LibraryPackageManager
    from platformio.platform.factory import PlatformFactory
    from platformio.package.manager.core import get_core_package_dir
    import subprocess

    core = Path(core).resolve()
    plan = plan or load_plan()
    env = clean_bootstrap_environment()
    for name, value in {"CORE": core, "PACKAGES": core / "packages", "PLATFORMS": core / "platforms",
                        "GLOBALLIB": core / "lib", "CACHE": core / ".cache"}.items():
        os.environ[f"PLATFORMIO_{name}_DIR"] = str(value)
        env[f"PLATFORMIO_{name}_DIR"] = str(value)
    core.mkdir(parents=True, exist_ok=True)
    # Do not retain a prior success certificate after a partial repair.
    (core / MARKER).unlink(missing_ok=True)
    get_core_package_dir("tool-scons")
    platform_manager = PlatformPackageManager(str(core / "platforms"))
    tools = ToolPackageManager(str(core / "packages"))
    libraries = LibraryPackageManager(str(core / "lib"))
    files = set()
    if not all((ROOT / item).is_file() for item in ASSETS):
        raise RuntimeError("Offline editor/terminal assets are missing from this application copy")
    for name in plan["platforms"]:
        log(f"Bootstrap offline pack: {name} (all declared frameworks and upload/debug tools)")
        package = platform_manager.install(name, skip_dependencies=True)
        platform = PlatformFactory.new(package)
        specs, probes = package_plan(package, platform.get_boards())
        files.add(str((Path(package.path) / "platform.json").relative_to(core)))
        for spec in specs:
            log(f"Preparing {spec.humanize()}")
            installed = tools.install(spec)
            verify_package_dependencies(tools, installed)
            files.add(str((Path(installed.path) / "package.json").relative_to(core)))
            files.add(str((Path(installed.path) / ".piopm").relative_to(core)))
        for board, framework in probes:
            # envdump executes builder/framework setup (including its Python
            # dependencies) without uploading or requiring a user's sketch.
            with tempfile.TemporaryDirectory(prefix="offline-prewarm-", dir=core) as scratch:
                project = Path(scratch)
                (project / "src").mkdir()
                source = "#include <Arduino.h>\nvoid setup() {}\nvoid loop() {}\n" if framework == "arduino" else "int main(void) { return 0; }\n"
                (project / "src/main.cpp").write_text(source)
                (project / "platformio.ini").write_text(f"[env:offline]\nplatform = {name}\nboard = {board}\nframework = {framework}\n")
                log(f"Preparing builder {name}:{board} ({framework})")
                # Builder dumps can be large; keep only a bounded failure tail
                # in memory instead of retaining every environment variable.
                with tempfile.TemporaryFile() as output:
                    result = subprocess.run([sys.executable, "-m", "platformio", "run", "-d", str(project),
                                             "-e", "offline", "-t", "envdump"], env=env, stdout=output,
                                            stderr=subprocess.STDOUT, timeout=1800,
                                            creationflags=0x08000000 if sys.platform == "win32" else 0)
                    if result.returncode:
                        length = output.seek(0, 2)
                        output.seek(max(0, length - 4000))
                        tail = output.read().decode("utf-8", errors="replace")
                        raise RuntimeError(f"Builder preparation failed for {name}:{board} ({framework}):\n{tail}")
    for spec in plan.get("libraries", []):
        log(f"Preparing offline sketch library {spec}")
        installed = libraries.install(spec)
        verify_package_dependencies(libraries, installed)
        files.add(str((Path(installed.path) / ".piopm").relative_to(core)))
    # Include all builder-created host packages, not only manifest defaults.
    for manifest in (core / "packages").glob("*/package.json"):
        files.add(str(manifest.relative_to(core)))
    for folder in (core / "packages", core / "lib"):
        for metadata in folder.glob("*/.piopm"):
            files.add(str(metadata.relative_to(core)))
    catalog = subprocess.run([sys.executable, "-m", "platformio", "boards", "--json-output"],
                             capture_output=True, text=True, env=env, check=True, timeout=120,
                             creationflags=0x08000000 if sys.platform == "win32" else 0)
    json.loads(catalog.stdout)
    (core / ".mcu-offline-catalog.json").write_text(catalog.stdout, encoding="utf-8")
    files.add(".mcu-offline-catalog.json")
    guards = install_runtime_guard(core)
    payload = {"schema": SCHEMA, "plan": plan_hash(plan), "host": sys.platform,
               "default_plan": plan_hash(load_plan()), "prepared_plan": plan,
               "architecture": host_platform.machine(), "files": sorted(files), "guards": guards}
    temporary = core / (MARKER + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, core / MARKER)
    log("Offline bootstrap package preparation complete. Workspace downloads are disabled.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args(argv)
    try:
        prepare(args.core, load_plan(args.plan))
        return 0
    except Exception as exc:
        print(f"Offline bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
