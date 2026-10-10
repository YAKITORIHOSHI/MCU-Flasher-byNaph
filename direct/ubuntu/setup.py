#!/usr/bin/env python3
"""Create/repair the native Ubuntu environment without changing system Python."""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_DIR = ROOT / ".venv-linux"
sys.path.insert(0, str(ROOT))


def clean_native_bootstrap_environment() -> dict[str, str]:
    """Keep proxy/certificate/PATH choices while forcing private pip destinations."""
    from src.modules.offline_bootstrap import clean_bootstrap_environment
    env = clean_bootstrap_environment()
    for name in ("PIP_TARGET", "PIP_PREFIX", "PIP_ROOT", "PIP_USER",
                 "PYTHONEXEPATH", "PIO_PYTHON_EXE", "PLATFORMIO_PYTHON_EXE", "PLATFORMIO_PENV_DIR"):
        env.pop(name, None)
    # A user pip.conf can otherwise redirect venv installs with target/prefix.
    # Environment proxy, index and certificate settings remain available.
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PYTHONNOUSERSITE"] = "1"
    return env


def prepare_environment_directory(directory: Path) -> None:
    """Repair only interpreter links inside the owned native venv; never clear it."""
    if directory.is_symlink():
        raise RuntimeError(".venv-linux must be a local directory, not a symbolic link.")
    if not directory.exists():
        return
    if not directory.is_dir():
        raise RuntimeError(".venv-linux is not a directory. Preserve it and choose a writable application folder.")
    if any(directory.iterdir()) and not (directory / "pyvenv.cfg").is_file():
        raise RuntimeError(".venv-linux contains unrecognized files without pyvenv.cfg. "
                           "Move that directory aside before Bootstrap; it will not be deleted.")
    if (directory / "pyvenv.cfg").is_symlink():
        raise RuntimeError(".venv-linux/pyvenv.cfg must be a local file, not a symbolic link.")
    for folder in ("bin", "lib", "include", f"lib/python3.{sys.version_info[1]}",
                   f"lib/python3.{sys.version_info[1]}/site-packages"):
        if (directory / folder).is_symlink():
            raise RuntimeError(f".venv-linux/{folder} must be a local directory. "
                               "Move the linked environment aside before Bootstrap.")
    # Standard venv's lib64 alias is safe only when it stays in this venv.
    alias = directory / "lib64"
    if alias.is_symlink() and alias.resolve() != (directory / "lib").resolve():
        raise RuntimeError(".venv-linux/lib64 points outside this environment. "
                           "Move the linked environment aside before Bootstrap.")
    base = Path(sys._base_executable).resolve()
    bin_dir = directory / "bin"
    # EnvBuilder leaves existing symlinks untouched. Copied or moved checkouts
    # can therefore retain dead interpreter links or a different Python ABI.
    names = {"python", "python3", f"python3.{sys.version_info[1]}", Path(sys._base_executable).name}
    for name in names:
        candidate = bin_dir / name
        if candidate.is_symlink() and candidate.resolve() != base:
            candidate.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--project", help="Sketch folder to open after setup")
    parser.add_argument("--new-window", action="store_true")
    parser.add_argument("--plan", type=Path, help="Offline board/library package plan")
    parser.add_argument("--board-source", type=Path, action="append",
                        help="Arduino declaration source root; repeat for multiple roots")
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        print("This setup is for Ubuntu/Linux. Use direct/windows/run.vbs on Windows.", file=sys.stderr)
        return 1
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required.", file=sys.stderr)
        return 1
    from src.modules.runtime_resources import enforce_minimum_cpu_requirement
    if not enforce_minimum_cpu_requirement():
        return 1
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("Run setup as your normal desktop account, without sudo.", file=sys.stderr)
        return 1
    from src.modules.platform_runtime import native_platformio_dir
    env = clean_native_bootstrap_environment()
    if os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME") or os.environ.get("MCU_FLASHER_WORKSPACE_RUNTIME"):
        # A terminal inside the app can inherit its conditional Python guard.
        # Relaunch bootstrap before running installers in that interpreter.
        return subprocess.call([sys.executable, "-B", str(Path(__file__).resolve()),
                                *(sys.argv[1:] if argv is None else argv)], env=env, cwd=ROOT)
    stage = "Ubuntu prerequisites"
    try:
        from direct.ubuntu.system_setup import ensure_system_dependencies
        ensure_system_dependencies(require_display=args.launch, env=env)
        stage = "private Python environment"
        # Never clear a repaired environment or install into system Python.
        prepare_environment_directory(ENV_DIR)
        venv.EnvBuilder(with_pip=True, symlinks=True).create(ENV_DIR)
        python = ENV_DIR / "bin/python"
        stage = "Python dependencies"
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        "--no-user", "--prefer-binary", "--only-binary=PySide6,PySide6-Essentials,PySide6-Addons,shiboken6,PyQt5,PyQt5-Qt5,QScintilla",
                        "-r", str(ROOT / "direct/ubuntu/requirements.txt")], check=True, env=env, cwd=ROOT)
        stage = "native runtime validation"
        subprocess.run([str(python), "-c",
                        "from direct.ubuntu.preflight import runtime_preflight; runtime_preflight()"],
                       check=True, env=env, cwd=ROOT)
        stage = "native Arduino CLI"
        from direct.ubuntu.arduino_cli import ensure_arduino_cli
        ensure_arduino_cli(env=env)
        stage = "OpenCode AI Assistant"
        from direct.ubuntu.opencode_setup import ensure_opencode_cli
        ensure_opencode_cli(env=env)
        core = native_platformio_dir()
        command = [str(python), "-B", str(ROOT / "src/modules/offline_bootstrap.py"),
                   "--core", str(core)]
        from src.modules.offline_mode import finish_bootstrap
        # Native Ubuntu cannot reuse Windows toolchains. Prepare the configured
        # common board packs in online mode too; SCons alone cannot resolve or
        # compile a retained Arduino board selection such as ESP32 Dev Module.
        if args.plan:
            command += ["--plan", str(args.plan)]
        for source in args.board_source or ():
            command += ["--board-source", str(source)]
        # Existing package certificates need only a fresh declaration audit.
        from src.modules.offline_bootstrap import load_plan, ready
        if ready(core, load_plan(args.plan) if args.plan else None):
            command += ["--coverage-only"]
        from src.modules.package_jobs import package_store_lease
        stage = "native board toolchains"
        with package_store_lease(core, mode="prepare", wait=True,
                                 on_wait=lambda: print("Waiting for active builds or board preparation before repairing toolchains…")):
            subprocess.run(command, check=True, env=env, cwd=ROOT)
            finish_bootstrap(core)
        print("Ubuntu runtime ready. Launch with: bash direct/ubuntu/run.sh")
        if args.launch:
            command = [str(python), str(ROOT / "mcu_flash_gui.py")]
            if args.project:
                command += ["--project", args.project]
            if args.new_window:
                command += ["--new-window"]
            return subprocess.call(command, cwd=ROOT, env=env)
        return 0
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        recovery = {
            "Ubuntu prerequisites": "",
            "private Python environment": "Install python3-venv (or the matching python3.X-venv for a custom interpreter), then rerun Bootstrap.",
            "Python dependencies": "Check network access and available disk space, then rerun Bootstrap. Native GUI wheels require amd64 Python 3.10 or newer.",
            "native runtime validation": "Read the dependency error above, install its Ubuntu prerequisites, then rerun Bootstrap.",
            "native Arduino CLI": "Check network access and the native CLI error above, then rerun Bootstrap; existing files are preserved.",
            "OpenCode AI Assistant": "Check network access and the native CLI error above, then rerun Bootstrap; existing files are preserved.",
            "native board toolchains": "Check the toolchain output above and network access, then rerun Bootstrap; prepared packages are preserved.",
        }
        print(f"Ubuntu Bootstrap failed during {stage}: {exc}\n{recovery[stage]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
