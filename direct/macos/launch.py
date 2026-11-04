#!/usr/bin/env python3
"""Native macOS launch coordinator, running in the private Python; the GUI uses its venv."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def runtime_problem(env: dict[str, str]) -> str | None:
    from src.modules.ubuntu_architecture import elf_matches_host, host_architecture

    python = ROOT / ".venv-macos/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        return "The macOS private runtime is missing or has a broken interpreter link."
    if not elf_matches_host(python):
        return f"The macOS private runtime does not match this Mac's {host_architecture()} CPU. Run --repair on this Mac."
    commands = (
        [str(python), "-B", str(ROOT / "direct/macos/preflight.py")],
        [str(python), "-B", "-c",
         "from direct.ubuntu.preflight import bootstrap_ready; "
         "from src.modules.platform_runtime import native_platformio_dir; "
         "raise SystemExit(0 if bootstrap_ready(native_platformio_dir()) else 1)"],
    )
    for command in commands:
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                    text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return f"The macOS runtime cannot be validated: {exc}"
        if result.returncode:
            return result.stderr.strip() or "The macOS native board packages need Bootstrap preparation."
    return None


def launch_workspace(arguments: list[str], env: dict[str, str]) -> int:
    """Detach from Terminal, retaining immediate failures in a local log."""
    command = [str(ROOT / ".venv-macos/bin/python"), "-B", str(ROOT / "mcu_flash_gui.py"), *arguments]
    folder = ROOT / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="macos-launch-", suffix=".log", dir=folder, delete=False) as log:
        path = Path(log.name)
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + 1.0
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.returncode not in (None, 0):
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 4000))
            detail = stream.read().decode("utf-8", errors="replace")
        print(f"MCU Flasher could not start.\n{detail}\nLaunch log: {path}", file=sys.stderr)
        return process.returncode or 1
    return 0


def main(argv=None) -> int:
    from src.modules.ubuntu_release import enforce_supported_ubuntu
    if not enforce_supported_ubuntu():
        return 1
    arguments_in = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true", help="Repair native runtime dependencies before opening")
    parser.add_argument("--check", action="store_true", help="Diagnose dependencies without installing or launching")
    parser.add_argument("--project", help="Open this sketch folder")
    parser.add_argument("sketch", nargs="?", help="Optional sketch file or folder")
    parser.add_argument("--new-window", action="store_true", help="Open an independent sketch window")
    parser.add_argument("--resume-application-update", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(arguments_in)
    from direct.ubuntu.launch import (available_application_update, clean_environment,
                                      install_application_update)
    env = clean_environment()
    try:
        if not args.check and os.geteuid() == 0:
            raise RuntimeError("Run MCU Flasher as your normal macOS account, without sudo.")
        if args.check:
            problem = runtime_problem(env)
            if problem:
                print(problem, file=sys.stderr)
                return 1
            print("The macOS private runtime is ready.")
            return 0
        candidate = available_application_update(arguments_in)
        if candidate is not None:
            install_application_update(candidate, arguments_in, env, run_script="direct/macos/run.sh")
        from src.modules.runtime_resources import enforce_minimum_cpu_requirement
        if not enforce_minimum_cpu_requirement():
            return 1
        arguments = []
        if args.project or args.sketch:
            arguments += ["--project", args.project or args.sketch]
        if args.new_window:
            arguments += ["--new-window"]
        problem = "Explicit macOS repair requested." if args.repair else runtime_problem(env)
        if problem:
            print(problem, file=sys.stderr)
            print("Preparing the macOS private runtime. This window shows its progress.", flush=True)
            if subprocess.call([sys.executable, "-I", "-B", str(ROOT / "direct/macos/setup.py")], cwd=ROOT, env=env):
                return 1
            problem = runtime_problem(env)
            if problem:
                raise RuntimeError(problem)
        return launch_workspace(arguments, env)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
