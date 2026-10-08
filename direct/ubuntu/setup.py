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
    from src.modules.offline_bootstrap import clean_bootstrap_environment
    from src.modules.platform_runtime import native_platformio_dir
    env = clean_bootstrap_environment()
    env["PYTHONNOUSERSITE"] = "1"
    if os.environ.get("MCU_FLASHER_OFFLINE_RUNTIME") or os.environ.get("MCU_FLASHER_WORKSPACE_RUNTIME"):
        # A terminal inside the app can inherit its conditional Python guard.
        # Relaunch bootstrap before running installers in that interpreter.
        return subprocess.call([sys.executable, "-B", str(Path(__file__).resolve()),
                                *(sys.argv[1:] if argv is None else argv)], env=env, cwd=ROOT)
    try:
        # Never clear a repaired environment or install into system Python.
        venv.EnvBuilder(with_pip=True, symlinks=True).create(ENV_DIR)
        python = ENV_DIR / "bin/python"
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        "--prefer-binary", "-r", str(ROOT / "direct/ubuntu/requirements.txt")], check=True, env=env)
        subprocess.run([str(python), "-c",
                        "import PySide6.QtWebEngineWidgets, serial, psutil, platformio, ptyprocess; "
                        "from src.modules.mbed_compat import prepare_dependencies; prepare_dependencies()"],
                       check=True, env=env, cwd=ROOT)
        core = native_platformio_dir()
        command = [str(python), "-B", str(ROOT / "src/modules/offline_bootstrap.py"),
                   "--core", str(core)]
        from src.modules.offline_mode import offline_enabled, finish_bootstrap
        prepare_offline = offline_enabled() or bool(args.plan or args.board_source)
        if not prepare_offline:
            command += ["--runtime-only"]
        if args.plan:
            command += ["--plan", str(args.plan)]
        for source in args.board_source or ():
            command += ["--board-source", str(source)]
        # Existing package certificates need only a fresh declaration audit.
        from src.modules.offline_bootstrap import load_plan, ready
        if prepare_offline and ready(core, load_plan(args.plan) if args.plan else None):
            command += ["--coverage-only"]
        from src.modules.package_jobs import package_store_lease
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
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        print(f"Setup failed: {exc}\nCheck network access and install python3-venv, then rerun setup.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
