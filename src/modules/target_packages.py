#!/usr/bin/env python3
"""Prepare one selected board's build and upload tools the first time it is used.

Bootstrap prepares the private runtime and the board list (platform definitions
only), so the first launch takes a minute or two. A board's compiler, framework
and uploader download the first time that board is compiled, and only the
packages its PlatformIO platform requires for building and uploading it; other
chips' toolchains and optional debuggers are never fetched. Offline Mode keeps
Bootstrap's complete preparation instead.

This runs in its own process, outside the workspace's installed-only guard. It
reads the store as a build does and takes the store's writer lease only while
something is missing, so running builds finish first and new ones wait.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = 1
STAMPS = ".mcu-targets"


class MissingPackage(RuntimeError):
    pass


def _key(platform, board, framework, protocol=""):
    frameworks = ",".join(part.strip() for part in str(framework or "").split(",") if part.strip())
    return [str(platform or "").strip(), str(board or "").strip(), frameworks, str(protocol or "").strip()]


def stamp_path(core, platform, board, framework, protocol="") -> Path:
    digest = hashlib.sha256(json.dumps(_key(platform, board, framework, protocol)).encode("utf-8")).hexdigest()
    return Path(core) / STAMPS / f"{digest[:32]}.json"


def target_ready(core, platform, board, framework, protocol="") -> bool:
    """A few stat calls before each build: was this exact target prepared, and is it intact?"""
    try:
        path = stamp_path(core, platform, board, framework, protocol)
        if path.stat().st_size > 256 * 1024:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA or data.get("host") != sys.platform:
            return False
        if data.get("target") != _key(platform, board, framework, protocol):
            return False
        files = data.get("files")
        if not isinstance(files, list) or not files:
            return False
        core = Path(core)
        for item in files:
            relative = Path(str(item))
            if relative.is_absolute() or ".." in relative.parts or not (core / relative).is_file():
                return False
        manifest = core / str(data.get("platform_manifest") or "")
        return json.loads(manifest.read_text(encoding="utf-8")).get("version") == data.get("platform_version")
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def configuration_text(platform, board, framework, extra="", env_name="mcu_env") -> str:
    """The build's own target lines, so upload protocol choices select the same uploader."""
    return (f"[platformio]\ndefault_envs = {env_name}\n\n[env:{env_name}]\n"
            f"platform = {platform}\nboard = {board}\nframework = {framework}\n{extra}")


def _relative(path, core) -> str:
    return Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(core))).as_posix()


def prepare(config_path, core, *, env_name="mcu_env", protocol="", log=print, install=True) -> dict:
    """Install the selected target's required packages; never builds, uploads or opens a port."""
    from platformio import app
    from platformio.dependencies import get_core_dependencies
    from platformio.package.manager.platform import PlatformPackageManager
    from platformio.package.manager.tool import ToolPackageManager
    from platformio.package.meta import PackageSpec
    from platformio.platform.factory import PlatformFactory
    from platformio.project.config import ProjectConfig
    from src.modules.offline_bootstrap import prefetch_package_archives

    core = Path(os.path.abspath(core))
    app.set_session_var("custom_project_conf", str(config_path))
    config = ProjectConfig.get_instance(str(config_path))
    section = f"env:{env_name}"
    board = config.get(section, "board")
    framework = ",".join(config.get(section, "framework") or [])
    platform_spec = PackageSpec(config.get(section, "platform"))
    platforms = PlatformPackageManager(str(core / "platforms"))
    tools = ToolPackageManager(str(core / "packages"))
    changed = False
    package = platforms.get_package(platform_spec)
    if package is None:
        if not install:
            raise MissingPackage(f"Board platform {platform_spec.humanize()} is not installed")
        log(f"Downloading the {platform_spec.humanize()} board platform...")
        # Only the platform definition: its packages are chosen for this board below.
        package = platforms.install(platform_spec, skip_dependencies=True)
        if package is None:
            raise RuntimeError(f"PlatformIO did not install {platform_spec.humanize()}")
        changed = True
    platform = PlatformFactory.new(package)
    platform.project_env = env_name
    platform.ensure_engine_compatible()
    board_config = platform.board_config(board)  # Raises for a board this platform does not define.
    # Dependency selection only: never platform.run(), never -t upload.
    platform.configure_project_packages(env_name, ["upload"])
    requirements = [("tool-scons", PackageSpec(owner="platformio", name="tool-scons",
                                               requirements=get_core_dependencies()["tool-scons"]))]
    requirements += [(name, platform.get_package_spec(name))
                     for name, options in platform.packages.items() if not options.get("optional")]
    missing = [(name, spec) for name, spec in requirements if tools.get_package(spec) is None]
    if missing and not install:
        raise MissingPackage("Missing " + ", ".join(name for name, _ in missing))
    if missing:
        names = ", ".join(name for name, _ in missing)
        log(f"{board_config.get('name', board)} needs {len(missing)} tool package(s): {names}")
        prefetch_package_archives(core, [spec for _, spec in missing], log=log)
        for name, spec in missing:
            log(f"Installing {spec.humanize()}...")
            if tools.install(spec) is None:
                raise RuntimeError(f"PlatformIO did not install {spec.humanize()}")
        tools.memcache_reset()
        changed = True
    files = [_relative(Path(package.path) / "platform.json", core),
             _relative(Path(package.path) / "boards" / f"{board}.json", core)]
    for name, spec in requirements:
        installed = tools.get_package(spec)
        if installed is None:
            raise RuntimeError(f"{board} is still missing {name} after preparation")
        for item in ("package.json", ".piopm"):
            if (Path(installed.path) / item).is_file():
                files.append(_relative(Path(installed.path) / item, core))
    platform_manifest = _relative(Path(package.path) / "platform.json", core)
    stamp = stamp_path(core, config.get(section, "platform"), board, framework, protocol)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    temporary = stamp.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "schema": SCHEMA, "host": sys.platform,
        "target": _key(config.get(section, "platform"), board, framework, protocol),
        "platform_manifest": platform_manifest, "platform_version": str(package.metadata.version),
        "files": sorted(set(files)),
    }, indent=2), encoding="utf-8")
    os.replace(temporary, stamp)
    return {"changed": changed, "board": board, "framework": framework,
            "packages": [name for name, _ in requirements]}


def _environment(core: Path) -> None:
    for name, value in {"CORE": core, "PACKAGES": core / "packages", "PLATFORMS": core / "platforms",
                        "GLOBALLIB": core / "lib", "CACHE": core / ".cache"}.items():
        os.environ[f"PLATFORMIO_{name}_DIR"] = str(value)
    for name in ("PLATFORMIO_NO_TELEMETRY", "PLATFORMIO_DISABLE_UPGRADE_CHECK", "PLATFORMIO_DISABLE_PROMPTS"):
        os.environ[name] = "1"
    os.environ["PLATFORMIO_SETTING_ENABLE_TELEMETRY"] = "false"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--env", default="mcu_env")
    parser.add_argument("--protocol", default="")
    parser.add_argument("--events-root", type=Path)
    args = parser.parse_args(argv)
    if os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME") or os.environ.get("MCU_FLASHER_WORKSPACE_RUNTIME"):
        print("Board tool preparation runs in its own process, outside the workspace.", file=sys.stderr)
        return 2
    core = Path(os.path.abspath(args.core))
    try:
        core.mkdir(parents=True, exist_ok=True)
        _environment(core)
        from src.modules.bootstrap_package_recovery import atomic_package_writes
        from src.modules.bootstrap_platformio import archive_paths
        from src.modules.offline_bootstrap import _bootstrap_http_timeouts
        from src.modules.package_jobs import package_store_lease
        from src.modules.platformio_locks import package_locks
        root = args.events_root
        report = None
        # A prepared target is a reader, like a build: no waiting on other windows.
        with package_store_lease(core, wait=True, root=root):
            try:
                report = prepare(args.config, core, env_name=args.env, protocol=args.protocol, install=False)
            except MissingPackage:
                report = None
        if report is None:
            with package_store_lease(core, "prepare", wait=True, root=root,
                                     on_wait=lambda: print("Waiting for other builds to finish before adding tools...",
                                                           flush=True)):
                with _bootstrap_http_timeouts(), package_locks(), archive_paths(), atomic_package_writes(core):
                    report = prepare(args.config, core, env_name=args.env, protocol=args.protocol,
                                     log=lambda text: print(text, flush=True))
        print(("Board tools are ready." if report["changed"] else "Board tools were already prepared."), flush=True)
        return 0
    except Exception as exc:  # Reported to the build console; nothing is half-published.
        print(f"Board tool preparation failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
