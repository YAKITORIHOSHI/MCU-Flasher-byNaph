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
SCHEMA = 3
MARKER = ".mcu-offline-ready.json"
ASSETS = (
    "src/editor/index.html", "src/editor/bundle.js", "src/editor/18.bundle.js",
    "src/editor/editor.worker.js", "src/editor/8f3abbcbc983396e1f13.ttf",
    "src/editor/qwebchannel.js", "src/editor/terminal.html",
    "src/assets/xterm/xterm.css", "src/assets/xterm/xterm.js", "src/assets/xterm/xterm-addon-fit.js",
)
_AVR_ARDUINO_SCRIPT = "builder/frameworks/arduino.py"
# These reviewed builders choose host packages by core, not MCU. Unknown
# revisions and locally modified scripts retain the conservative MCU probes.
# Hash decoded source with universal newlines so Windows/Linux packs agree.
_REVIEWED_AVR_BUILDERS = {
    ("atmelavr", "5.3.0"): {
        "platform.py": "051f3a1428c132005e25fd810e8a7c0397376c8fdd00657a1b2501ada457afda",
        "builder/main.py": "9be1c45718075fd5049ed30b10add528f8bbe452132552008469d0e70f37c888",
        _AVR_ARDUINO_SCRIPT: "ffcbab35f112ce9d75e958f505d29b499e78f46c3e341a3fde02fd402ef28b1a",
    },
    ("atmelmegaavr", "1.10.0"): {
        "platform.py": "18a653fc0f43bf57d83efd22ef9bb4eb2aac9d14400464a63d15ec0c417f1a0d",
        "builder/main.py": "ca2ac3f6bfab379d9c248275790acab1ed60031ea9e8ca9a5e211974baaec47b",
        _AVR_ARDUINO_SCRIPT: "e65a0c550813bbaef84c4276fcf8cf86472db36d0bf85ba0df706e186cf2f8ec",
        "builder/frameworks/_bare.py": "c4cc6d6ae1d00e97035ac3342d5eb0795fc8c9d47147453302e4a6593c6b45c9",
    },
}
_STM32_ARDUINO_SCRIPT = "builder/frameworks/arduino.py"
_REVIEWED_STM32_BUILDERS = {
    ("ststm32", "20.0.0"): {
        "platform.py": "970ee6fc6f2d650b5dc5ec98a68b762edb6a4d7ef6f5bd0f59805ae16fa35c81",
        "builder/main.py": "8c012f6d7f8d5e3065ef3157abc441c976ab7a765fd6399c41dd9ae1722f8c7b",
        _STM32_ARDUINO_SCRIPT: "e5e309e7f0275b8d346131536c503df64403e16103e4fb0c61c51f6f563b8d5f",
    },
}
# The platform wrapper dispatches into these installed framework scripts.
# Validate their exact package versions and source before sharing preparation.
_REVIEWED_STM32_ARDUINO_PACKAGES = {
    ("framework-arduinoststm32", "4.30000.0"): {
        "tools/platformio/platformio-build.py": "7b781cdd51d8b0995ca63b0c057fc9aaa7da9f6aba2e37937609eea362e04583",
    },
    ("framework-arduinoststm32-maple", "3.10000.201129"): {
        "tools/platformio-build-stm32f1.py": "bc854501f3f96d12b0308c298f13c27fe7f8fef546e3a77c5a1d1347009c34d8",
        "tools/platformio-build-stm32f4.py": "68584c6fb6f989077b3b32f891966267a0517dd2ca67d8f13757929c9e6a4cd3",
    },
    ("framework-arduinoststm32l0", "2.10.220528"): {
        "tools/platformio-build.py": "ef2c3b5969381fd9ac98af9b62a32e6ef3544c19aa41f4dccf0c5c9794f9e8cb",
    },
}
_REVIEWED_STM32_NATIVE_SCRIPTS = {
    "zephyr": {"builder/frameworks/zephyr.py": "ee841ccb8bef4d31664ac871a875c65733c76d842c1b172f090ce87761f0057d"},
    "mbed": {"builder/frameworks/mbed.py": "08d0bc4501765337f62f1571f123bae1f502b6b14f459ffcac90d27680ceefc8"},
}
_UNAVAILABLE_FRAMEWORK_VERSIONS = {"zephyr": "3.40402.0", "mbed": "6.61700.231105"}
_REVIEWED_STM32_NATIVE_PACKAGES = {
    "zephyr": {"scripts/platformio/platformio-build.py": "49741b887efb67252b6d7bb7be80ace7ac448f5888ae8513ab0e491b958e28d3"},
    "mbed": {"platformio/pio_mbed_adapter.py": "447a3d2beda11296e710935b3b2dee7a17d0cf3b7740b86484f3e69b9d601685"},
}


def _reviewed_source_files(directory, signatures, limit=65536):
    """Match a finite source set without executing code or reading huge files."""
    try:
        for relative, expected in signatures.items():
            with (Path(directory) / relative).open("rb") as source:
                data = source.read(limit + 1)
            if len(data) > limit:
                return False
            text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected:
                return False
        return True
    except (OSError, TypeError, UnicodeError, ValueError):
        return False


def _reviewed_avr_builder(instance):
    """Read-only, bounded source validation for MCU-independent AVR setup."""
    try:
        signatures = _REVIEWED_AVR_BUILDERS.get((instance.name, instance.version))
        if not signatures or instance.frameworks.get("arduino", {}).get("script") != _AVR_ARDUINO_SCRIPT:
            return False
        return _reviewed_source_files(instance.get_dir(), signatures)
    except (AttributeError, OSError, TypeError, UnicodeError, ValueError):
        return False


def _reviewed_stm32_builder(instance):
    """Validate only the reviewed STM32 Arduino platform dispatch."""
    try:
        signatures = _REVIEWED_STM32_BUILDERS.get((instance.name, instance.version))
        return bool(signatures and
                    instance.frameworks.get("arduino", {}).get("script") == _STM32_ARDUINO_SCRIPT and
                    _reviewed_source_files(instance.get_dir(), signatures))
    except (AttributeError, OSError, TypeError, UnicodeError, ValueError):
        return False


def _reviewed_stm32_dispatch(instance, board, cache):
    """Return an exact installed Arduino dispatch, or retain MCU preparation."""
    try:
        framework = instance.frameworks.get("arduino", {})
        if framework.get("script") != _STM32_ARDUINO_SCRIPT:
            return None  # Arduino Mbed, MXChip and custom dispatches stay exact.
        core = board.get("build.core", "")
        if core == "maple":
            package = "framework-arduinoststm32-maple"
            relative = f"tools/platformio-build-{board.get('build.mcu', '')[:7]}.py"
        elif core == "stm32l0":
            package, relative = "framework-arduinoststm32l0", "tools/platformio-build.py"
        else:
            package, relative = "framework-arduinoststm32", "tools/platformio/platformio-build.py"
        if framework.get("package") != package or instance.packages.get(package, {}).get("owner") != "platformio":
            return None
        directory = instance.get_package_dir(package)
        if not directory:
            return None
        key = (str(directory), package, relative)
        if key not in cache:
            cache[key] = None
            with (Path(directory) / "package.json").open("rb") as manifest_file:
                raw = manifest_file.read(65537)
            if len(raw) > 65536:
                return None
            manifest = json.loads(raw.decode("utf-8-sig"))
            identity = (manifest.get("name"), manifest.get("version"))
            signatures = _REVIEWED_STM32_ARDUINO_PACKAGES.get(identity, {})
            expected = signatures.get(relative)
            if identity[0] == package and expected and _reviewed_source_files(directory, {relative: expected}):
                cache[key] = (package, identity[1], relative)
        return cache[key]
    except (AttributeError, OSError, TypeError, UnicodeError, ValueError):
        return None


def load_plan(path=None):
    path = Path(path or ROOT / "direct/offline-packages.json")
    plan = json.loads(path.read_text(encoding="utf-8"))
    return _validate_plan(plan)


def _validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema") != 1:
        raise ValueError("Invalid offline bootstrap package plan")
    for key in ("platforms", "libraries"):
        entries = plan.get(key, [])
        if not isinstance(entries, list) or (key == "platforms" and not entries):
            raise ValueError(f"Invalid {key} in offline package plan")
        for name in entries:
            if not isinstance(name, str) or not name.strip() or any(c in name for c in "\r\n"):
                raise ValueError(f"Invalid {key} specification in offline package plan")
    if "frameworks" in plan:
        entries = plan["frameworks"]
        if not isinstance(entries, list) or not entries:
            raise ValueError("Invalid frameworks in offline package plan")
        for name in entries:
            if not isinstance(name, str) or not name.strip() or any(c in name for c in "\r\n"):
                raise ValueError("Invalid frameworks specification in offline package plan")
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
    env["PLATFORMIO_SETTING_ENABLE_TELEMETRY"] = "false"
    env["PLATFORMIO_DISABLE_UPGRADE_CHECK"] = "true"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if sys.platform == "win32":
        # Zephyr's upstream module installer expands our short junction before
        # Git checkouts. Scope long-path support to bootstrap children only.
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
        if count < 0:
            raise ValueError("Invalid GIT_CONFIG_COUNT in bootstrap environment")
        env[f"GIT_CONFIG_KEY_{count}"] = "core.longpaths"
        env[f"GIT_CONFIG_VALUE_{count}"] = "true"
        env["GIT_CONFIG_COUNT"] = str(count + 1)
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


def package_plan(platform, boards, log=None, unavailable=None, allowed_frameworks=None):
    """Plan exact variants; an absent framework filter retains full coverage."""
    from platformio.package.meta import PackageSpec
    from platformio.platform.factory import PlatformFactory
    specs = {}
    primary_owners = {}
    optional_specs = []
    probes = {}
    active_packages = set()
    selected_requirements = set()
    excluded_requirements = set()
    excluded_framework_packages = set()
    selected_framework_packages = set()
    reviewed_avr = None
    reviewed_stm32 = None
    stm32_dispatches = {}
    if allowed_frameworks is not None:
        allowed_frameworks = set(allowed_frameworks)
    configurations = [(None, None)] + [(board.id, framework) for board in boards.values()
                                      for framework in board.get("frameworks", [])]
    for board_id, framework in configurations:
        instance = PlatformFactory.new(platform)
        if reviewed_avr is None:
            reviewed_avr = _reviewed_avr_builder(instance)
        if reviewed_stm32 is None:
            reviewed_stm32 = _reviewed_stm32_builder(instance)
        if board_id:
            options = {"board": board_id, "framework": [framework], "pioframework": [framework]}
            instance.configure_default_packages(options, ["upload", "buildfs", "bootloader", "debug"])
        selected = (not board_id or
                    ((allowed_frameworks is None or framework in allowed_frameworks)
                     and framework not in (unavailable or {}).get(board_id, {})))
        if allowed_frameworks is not None:
            for declared, metadata in getattr(instance, "frameworks", {}).items():
                package = metadata.get("package")
                if package:
                    (selected_framework_packages if declared in allowed_frameworks
                     else excluded_framework_packages).add(package)
        required = []
        for name, metadata in instance.packages.items():
            if selected and not metadata.get("optional"):
                active_packages.add((metadata.get("owner"), name))
            version = metadata.get("version")
            if version:
                spec = PackageSpec(owner=metadata.get("owner"), name=name, requirements=version)
                if not metadata.get("optional"):
                    if not spec.external:
                        identity = (spec.name, str(spec.requirements))
                        primary_owners.setdefault(identity, set()).add(spec.owner)
                    if board_id:
                        (selected_requirements if selected else excluded_requirements).add(spec.humanize())
                    if selected:
                        specs[spec.humanize()] = spec
                        required.append(spec.humanize())
                elif selected:
                    optional_specs.append((spec, False, metadata.get("type")))
            if selected:
                for version in metadata.get("optionalVersions", []):
                    spec = PackageSpec(owner=metadata.get("owner"), name=name, requirements=version)
                    # An authored owner/source must not be replaced by inferred
                    # ownership, even if a duplicate bare variant appears later.
                    bare = not spec.external and not spec.raw
                    optional_specs.append((spec, bare, metadata.get("type")))
        if board_id and selected:
            board = boards[board_id]
            key = (framework, board.get("build.mcu", ""), board.get("build.core", ""), tuple(sorted(required)))
            if reviewed_avr and framework == "arduino":
                script = instance.frameworks.get(framework, {}).get("script", "")
                if script == _AVR_ARDUINO_SCRIPT:
                    key = ("reviewed-avr", framework, board.get("build.core", ""), script, tuple(sorted(required)))
            if reviewed_stm32 and framework == "arduino":
                dispatch = _reviewed_stm32_dispatch(instance, board, stm32_dispatches)
                if dispatch:
                    key = ("reviewed-stm32", framework, board.get("build.core", ""),
                           _STM32_ARDUINO_SCRIPT, dispatch, tuple(sorted(required)))
            probes.setdefault(key, (board_id, framework))
    if allowed_frameworks is not None and not probes:
        raise RuntimeError("No declared board/framework matches the configured offline framework filter: "
                           + ", ".join(sorted(allowed_frameworks)))
    omitted_gperf = False
    for spec, bare, kind in optional_specs:
        if bare:
            owners = primary_owners.get((spec.name, str(spec.requirements)), set())
            if len(owners) == 1 and None not in owners:
                spec.owner = next(iter(owners))
        if allowed_frameworks is not None:
            if spec.name in excluded_framework_packages - selected_framework_packages:
                continue
            # Frameworks/compiler variants may share a package name. Omit
            # only exact versions proven exclusive to excluded configurations.
            if (kind in ("framework", "toolchain") and
                    spec.humanize() in excluded_requirements - selected_requirements):
                continue
        if (sys.platform == "win32" and (spec.owner, spec.name) == ("platformio", "tool-gperf")
                and (spec.owner, spec.name) not in active_packages):
            omitted_gperf = True
            continue
        specs.setdefault(spec.humanize(), spec)
    if omitted_gperf and log:
        log("Skipping optional platformio/tool-gperf: no selected configuration requires this Unix-only package on Windows.")
    if allowed_frameworks is not None:
        excluded = excluded_framework_packages - selected_framework_packages
        specs = {identity: spec for identity, spec in specs.items()
                 if spec.name not in excluded or identity in selected_requirements}
    return list(specs.values()), list(probes.values())


def _parallel_builder_safe(platform, probes):
    """Only reviewed AVR Arduino setup writes exclusively inside each project.

    Native frameworks can mutate shared Python environments, framework module
    trees or generated package files. Separate cache/temp directories do not
    isolate those writes. Unknown and package-dispatched builders stay serial.
    """
    return bool(probes and all(framework == "arduino" for _, framework in probes)
                and _reviewed_avr_builder(platform))


def _unavailable_frameworks(platform):
    """Reject reviewed stale upstream declarations without changing hardware."""
    from src.modules.zephyr_compat import unavailable_framework_reason as zephyr_reason
    from src.modules.mbed_compat import unavailable_framework_reason as mbed_reason
    if not _reviewed_stm32_builder(platform):
        return {}
    result = {}
    for framework, reason_function in (("zephyr", zephyr_reason), ("mbed", mbed_reason)):
        scripts = _REVIEWED_STM32_NATIVE_SCRIPTS[framework]
        if (platform.frameworks.get(framework, {}).get("script") not in scripts
                or not _reviewed_source_files(platform.get_dir(), scripts)):
            continue
        directory = platform.get_package_dir("framework-" + framework)
        if not directory or not _reviewed_source_files(directory, _REVIEWED_STM32_NATIVE_PACKAGES[framework], limit=131072):
            continue
        for board in platform.get_boards().values():
            if framework not in board.get("frameworks", []):
                continue
            reason = reason_function(platform.name, platform.version, board, directory)
            if reason:
                result.setdefault(board.id, {})[framework] = reason
    return result


def _installed_builder_probes(package, platform, specs, probes, log, unavailable=None, allowed_frameworks=None):
    """Enable reviewed package-script sharing only after installation finishes."""
    identity = (getattr(platform, "name", None), getattr(platform, "version", None))
    if identity not in _REVIEWED_STM32_BUILDERS and not unavailable:
        return probes
    installed_specs, installed_probes = package_plan(package, platform.get_boards(), allowed_frameworks=allowed_frameworks)
    before = {spec.humanize() for spec in specs}
    after = {spec.humanize() for spec in installed_specs}
    if before != after:
        raise RuntimeError("Offline package specifications changed after installation; restart bootstrap preparation")
    if unavailable:
        filtered_specs, installed_probes = package_plan(package, platform.get_boards(), unavailable=unavailable, allowed_frameworks=allowed_frameworks)
        if not {spec.humanize() for spec in filtered_specs}.issubset(after):
            raise RuntimeError("Supported framework planning introduced unprepared offline packages")
    if len(installed_probes) < len(probes):
        log(f"Reviewed STM32 preparation: {len(probes)} to {len(installed_probes)} builder probes; supported MCU-dependent frameworks retain their targets.")
    return installed_probes


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


def prepare(core, plan=None, log=print, jobs=None):
    if os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME"):
        raise RuntimeError("Offline packages can only be prepared by bootstrap, outside the workspace process")
    from src.modules.bootstrap_platformio import archive_paths
    with archive_paths():
        return _prepare(core, plan, log, jobs=jobs)


def _prepare(core, plan, log, jobs=None):
    from platformio.package.manager.platform import PlatformPackageManager
    from platformio.package.manager.tool import ToolPackageManager
    from platformio.package.manager.library import LibraryPackageManager
    from platformio.platform.factory import PlatformFactory
    from src.modules.bootstrap_seed import prepare_scons
    from src.modules.bootstrap_platformio import command
    from src.modules import bootstrap_builders
    import subprocess
    import time

    if jobs is not None and (not isinstance(jobs, int) or isinstance(jobs, bool) or jobs <= 0):
        raise ValueError("Bootstrap builder jobs must be a positive integer")
    try:
        from main.core.build_resources import get_optimal_compiler_jobs
        safe_jobs = get_optimal_compiler_jobs(storage_paths=(ROOT, core), storage_wait=True)
    except Exception:
        safe_jobs = max(1, min((os.cpu_count() or 4) - 2, 8))
    jobs = safe_jobs if jobs is None else min(jobs, safe_jobs)

    # Resolve relative components without expanding the short Windows junction.
    core = Path(os.path.abspath(core))
    plan = _validate_plan(plan) if plan is not None else load_plan()
    env = clean_bootstrap_environment()
    # Package managers also run in this bootstrap process, outside CLI children.
    for name in ("PLATFORMIO_SETTING_ENABLE_TELEMETRY", "PLATFORMIO_DISABLE_UPGRADE_CHECK"):
        os.environ[name] = env[name]
    for name, value in {"CORE": core, "PACKAGES": core / "packages", "PLATFORMS": core / "platforms",
                        "GLOBALLIB": core / "lib", "CACHE": core / ".cache"}.items():
        os.environ[f"PLATFORMIO_{name}_DIR"] = str(value)
        env[f"PLATFORMIO_{name}_DIR"] = str(value)
    core.mkdir(parents=True, exist_ok=True)
    # Do not retain a prior success certificate after a partial repair.
    (core / MARKER).unlink(missing_ok=True)
    platform_manager = PlatformPackageManager(str(core / "platforms"))
    tools = ToolPackageManager(str(core / "packages"))
    libraries = LibraryPackageManager(str(core / "lib"))
    prepare_scons(core, manager=tools, log=log)
    files = set()
    unavailable_targets = {}
    unavailable_manifests = {}
    builder_output_dir = ROOT / "logs" / f"offline-builders-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    builder_number = 0
    if not all((ROOT / item).is_file() for item in ASSETS):
        raise RuntimeError("Offline editor/terminal assets are missing from this application copy")
    allowed_frameworks = set(plan["frameworks"]) if "frameworks" in plan else None
    for name in plan["platforms"]:
        scope = ", ".join(sorted(allowed_frameworks)) if allowed_frameworks is not None else "all declared frameworks"
        log(f"Bootstrap offline pack: {name} ({scope}; upload/debug tools)")
        package = platform_manager.install(name, skip_dependencies=True)
        platform = PlatformFactory.new(package)
        specs, probes = package_plan(package, platform.get_boards(), log=log, allowed_frameworks=allowed_frameworks)
        files.add(str((Path(package.path) / "platform.json").relative_to(core)))
        for spec in specs:
            log(f"Preparing {spec.humanize()}")
            installed = tools.install(spec)
            verify_package_dependencies(tools, installed)
            files.add(str((Path(installed.path) / "package.json").relative_to(core)))
            files.add(str((Path(installed.path) / ".piopm").relative_to(core)))
        unavailable = _unavailable_frameworks(platform)
        for board, frameworks in unavailable.items():
            unavailable_targets[(platform.name, board)] = frameworks
            with (Path(platform.get_dir()) / "boards" / (board + ".json")).open("rb") as manifest_file:
                manifest = manifest_file.read(65537)
            if len(manifest) > 65536:
                raise RuntimeError("Unavailable target manifest exceeds reviewed bounds")
            unavailable_manifests[(platform.name, board)] = hashlib.sha256(manifest).hexdigest()
            for framework, reason in frameworks.items():
                log(f"Unavailable upstream target {platform.name}:{board} ({framework}): {reason}")
        probes = _installed_builder_probes(package, platform, specs, probes, log, unavailable=unavailable, allowed_frameworks=allowed_frameworks)

        def _run_single_probe(item):
            local_number, (board, framework) = item
            probe_num = builder_number + local_number
            with tempfile.TemporaryDirectory(prefix="offline-prewarm-", dir=core) as scratch:
                project = Path(scratch)
                (project / "src").mkdir()
                source = "#include <Arduino.h>\nvoid setup() {}\nvoid loop() {}\n" if framework == "arduino" else "int main(void) { return 0; }\n"
                (project / "src/main.cpp").write_text(source)
                (project / "platformio.ini").write_text(f"[env:offline]\nplatform = {name}\nboard = {board}\nframework = {framework}\n")
                label = f"{name}:{board} ({framework})"
                log(f"Preparing builder {label} [{local_number}/{len(probes)}]")
                output_path = builder_output_dir / f"builder-{probe_num:04d}.log"
                probe_env = dict(env)
                worker_cache = project / ".pio-cache"
                worker_cache.mkdir(parents=True, exist_ok=True)
                probe_env["PLATFORMIO_CACHE_DIR"] = str(worker_cache)
                probe_env["PLATFORMIO_BUILD_CACHE_DIR"] = str(worker_cache / "build")
                probe_tmp = project / ".tmp"
                probe_tmp.mkdir(parents=True, exist_ok=True)
                probe_env["TMP"] = str(probe_tmp)
                probe_env["TEMP"] = str(probe_tmp)
                probe_env["TMPDIR"] = str(probe_tmp)
                probe_env["PLATFORMIO_BUILD_JOBS"] = "1"
                probe_env["PLATFORMIO_RUN_JOBS"] = "1"
                probe_env["SCONSFLAGS"] = "-j1"
                bootstrap_builders.run_builder(command() + ["run", "--jobs", "1", "-d", str(project),
                                              "-e", "offline", "-t", "envdump"], env=probe_env, label=label,
                                              output_path=output_path, log=log)

        worker_count = max(1, min(jobs, len(probes))) if _parallel_builder_safe(platform, probes) else 1
        probe_items = list(enumerate(probes, 1))
        if worker_count > 1 and len(probes) > 1:
            log(f"Running {len(probes)} builder probes in parallel ({worker_count} concurrent workers)...")
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                list(executor.map(_run_single_probe, probe_items))
        else:
            for item in probe_items:
                _run_single_probe(item)
        builder_number += len(probes)
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
    catalog = subprocess.run(command() + ["boards", "--json-output"],
                             capture_output=True, text=True, env=env, check=True, timeout=120,
                             creationflags=0x08000000 if sys.platform == "win32" else 0)
    records = json.loads(catalog.stdout)
    if not isinstance(records, list):
        raise RuntimeError("PlatformIO returned an invalid offline board catalog")
    for record in records:
        unavailable = unavailable_targets.get((record.get("platform"), record.get("id")))
        if unavailable:
            declared = list(record.get("frameworks") or [])
            record["declared_frameworks"] = declared
            record["frameworks"] = [name for name in declared if name not in unavailable]
            record["unavailable_frameworks"] = unavailable
            record["unavailability_manifest_sha256"] = unavailable_manifests[(record["platform"], record["id"])]
            record["unavailability_platform_version"] = "20.0.0"
            record["unavailability_framework_versions"] = {name: _UNAVAILABLE_FRAMEWORK_VERSIONS[name]
                                                            for name in unavailable}
    (core / ".mcu-offline-catalog.json").write_text(json.dumps(records), encoding="utf-8")
    files.add(".mcu-offline-catalog.json")
    guards = install_runtime_guard(core)
    guards.extend(["src/modules/zephyr_compat.py", "src/modules/zephyr_board_aliases.cmake", "src/modules/mbed_compat.py"])
    payload = {"schema": SCHEMA, "plan": plan_hash(plan), "host": sys.platform,
               "default_plan": plan_hash(load_plan()), "prepared_plan": plan,
               "architecture": host_platform.machine(), "files": sorted(files), "guards": guards,
               "unavailable_targets": [{"platform": platform, "board": board, "frameworks": frameworks}
                                       for (platform, board), frameworks in sorted(unavailable_targets.items())]}
    temporary = core / (MARKER + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, core / MARKER)
    log("Offline bootstrap package preparation complete. Workspace downloads are disabled.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", required=True, type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--jobs", type=int, default=None, help="Number of parallel builder workers")
    args = parser.parse_args(argv)
    try:
        prepare(args.core, load_plan(args.plan), jobs=args.jobs)
        return 0
    except Exception as exc:
        print(f"Offline bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
