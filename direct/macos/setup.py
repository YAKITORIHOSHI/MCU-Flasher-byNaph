#!/usr/bin/env python3
"""Create or repair the native macOS environment using the private Python."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[2]
ENV_DIR = ROOT / ".venv-macos"
sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    from src.modules.ubuntu_release import enforce_supported_ubuntu
    if not enforce_supported_ubuntu():
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, help="Offline board/library package plan")
    parser.add_argument("--board-source", type=Path, action="append",
                        help="Arduino declaration source root; repeat for multiple roots")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        print("This setup is for macOS. Use the matching entry in LAUNCHER on other systems.", file=sys.stderr)
        return 1
    from src.modules.runtime_resources import enforce_minimum_cpu_requirement
    if not enforce_minimum_cpu_requirement():
        return 1
    if os.geteuid() == 0:
        print("Run setup as your normal macOS account, without sudo.", file=sys.stderr)
        return 1
    from direct.ubuntu.setup import clean_native_bootstrap_environment, prepare_environment_directory
    from src.modules.platform_runtime import native_platformio_dir
    env = clean_native_bootstrap_environment()
    stage = "private Python environment"
    try:
        from src.modules.ubuntu_architecture import host_architecture
        from direct.ubuntu.runtime_architecture import needs_architecture_repair, record_runtime_architecture
        architecture = host_architecture()
        # Never clear a repaired environment or install outside it.
        prepare_environment_directory(ENV_DIR)
        venv.EnvBuilder(with_pip=True, symlinks=True).create(ENV_DIR)
        python = ENV_DIR / "bin/python"
        reinstall = ["--force-reinstall"] if needs_architecture_repair(ENV_DIR, architecture) else []
        stage = "Python dependencies"
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-user",
                        "--prefer-binary",
                        "--only-binary=PySide6,PySide6-Essentials,PySide6-Addons,shiboken6,PyQt5,PyQt5-Qt5,QScintilla",
                        *reinstall, "-r", str(ROOT / "direct/macos/requirements.txt")],
                       check=True, env=env, cwd=ROOT)
        record_runtime_architecture(ENV_DIR, architecture)
        stage = "native runtime validation"
        subprocess.run([str(python), "-c", "from direct.macos.preflight import runtime_preflight; runtime_preflight()"],
                       check=True, env=env, cwd=ROOT)
        stage = "native Arduino CLI"
        from direct.ubuntu.arduino_cli import ensure_arduino_cli
        ensure_arduino_cli(env=env)
        stage = "OpenCode AI Assistant"
        from direct.ubuntu.opencode_setup import ensure_opencode_cli
        ensure_opencode_cli(env=env)
        core = native_platformio_dir()
        command = [str(python), "-B", str(ROOT / "src/modules/offline_bootstrap.py"), "--core", str(core)]
        from src.modules.offline_mode import finish_bootstrap
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
                                 on_wait=lambda: print("Waiting for active builds or board preparation before repairing toolchains...")):
            subprocess.run(command, check=True, env=env, cwd=ROOT)
            finish_bootstrap(core)
        from src.modules.default_board_folders import prepare_default_board_folders
        prepare_default_board_folders(python, env=env)
        print("macOS runtime ready. Launch with: bash direct/macos/run.sh")
        return 0
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        print(f"macOS Bootstrap failed during {stage}: {exc}\n"
              "Check network access and the output above, then launch again; prepared files are preserved.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
