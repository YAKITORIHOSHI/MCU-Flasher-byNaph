#!/usr/bin/env python3
"""Ubuntu launch coordinator. Bootstrap may use system Python; the GUI never does."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE",
                "MCU_FLASHER_OFFLINE_RUNTIME", "MCU_FLASHER_WORKSPACE_RUNTIME",
                "MCU_FLASHER_APP_ROOT", "PIP_NO_INDEX", "QT_PLUGIN_PATH",
                "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH"):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def report_error(message: str, *, desktop: bool) -> None:
    print(message, file=sys.stderr)
    if not desktop or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    zenity = shutil.which("zenity")
    if zenity:
        try:
            subprocess.run([zenity, "--error", "--title=MCU Flasher", "--width=620",
                            "--no-markup", "--text=" + message], check=False)
            return
        except OSError:
            pass
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        return
    try:
        window = tk.Tk()
        window.withdraw()
        messagebox.showerror("MCU Flasher", message, parent=window)
        window.destroy()
    except (tk.TclError, RuntimeError, OSError):
        pass  # The same complete diagnostic is already on stderr.


def runtime_problem(env: dict[str, str]) -> str | None:
    python = ROOT / ".venv-linux/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        return "The Ubuntu private runtime is missing or has a broken interpreter link."
    commands = (
        [str(python), "-B", str(ROOT / "direct/ubuntu/preflight.py"), "--runtime"],
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
            return f"The Ubuntu runtime cannot be validated: {exc}"
        if result.returncode:
            return result.stderr.strip() or "The Ubuntu native board packages need Bootstrap preparation."
    return None


def bootstrap_terminal(arguments: list[str], env: dict[str, str]) -> bool:
    child = ["/bin/bash", str(ROOT / "direct/ubuntu/run.sh"), "--bootstrap-window", *arguments]
    for name, separator in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"),
                            ("konsole", "-e"), ("xfce4-terminal", "--execute"),
                            ("xterm", "-e"), ("kitty", "--"), ("alacritty", "-e")):
        terminal = shutil.which(name)
        if terminal:
            try:
                subprocess.Popen([terminal, separator, *child], cwd=ROOT, env=env,
                                 start_new_session=True)
                return True
            except OSError:
                continue
    return False


def launch_workspace(arguments: list[str], env: dict[str, str], *, desktop: bool) -> int:
    command = [str(ROOT / ".venv-linux/bin/python"), "-B", str(ROOT / "mcu_flash_gui.py"), *arguments]
    if not desktop:
        os.chdir(ROOT)
        os.execvpe(command[0], command, env)
    # Desktop launches have no terminal. Retain immediate failures in a local log
    # and leave first-run's Bootstrap terminal free to close once setup succeeds.
    folder = ROOT / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="ubuntu-launch-", suffix=".log", dir=folder,
                                     delete=False) as log:
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
        report_error(f"MCU Flasher could not start.\n{detail}\nLaunch log: {path}", desktop=True)
        return process.returncode or 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true", help="Repair native runtime dependencies before opening")
    parser.add_argument("--check", action="store_true", help="Diagnose dependencies without installing or launching")
    parser.add_argument("--install-shortcut", action="store_true", help="Create a shortcut and Applications menu entry")
    parser.add_argument("--project", help="Open this sketch folder")
    parser.add_argument("sketch", nargs="?", help="Optional sketch file or folder (legacy launch syntax)")
    parser.add_argument("--new-window", action="store_true", help="Open an independent sketch window")
    parser.add_argument("--desktop", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--bootstrap-window", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    desktop = not args.check and (args.bootstrap_window or (args.desktop and not sys.stdin.isatty()))
    env = clean_environment()
    try:
        if not args.check and hasattr(os, "geteuid") and os.geteuid() == 0:
            raise RuntimeError("Run MCU Flasher as your normal Ubuntu desktop account, without sudo.")
        if args.install_shortcut and not args.check:
            from direct.ubuntu.install_desktop import main as install_shortcut
            return install_shortcut(["--install"])
        from direct.ubuntu.preflight import MissingSystemDependencies, host_preflight
        host_problem = None
        try:
            host_preflight(require_display=not args.check)
        except MissingSystemDependencies as missing:
            if args.check:
                raise
            host_problem = str(missing)
        if args.check:
            problem = runtime_problem(env)
            if problem:
                print(problem, file=sys.stderr)
                return 1
            print("Ubuntu system dependencies and the private runtime are ready.")
            return 0
        from src.modules.runtime_resources import enforce_minimum_cpu_requirement
        if not enforce_minimum_cpu_requirement():
            return 1
        arguments = []
        if args.project or args.sketch:
            arguments += ["--project", args.project or args.sketch]
        if args.new_window:
            arguments += ["--new-window"]
        problem = host_problem or ("Explicit Ubuntu repair requested." if args.repair else runtime_problem(env))
        if problem:
            print(problem, file=sys.stderr)
            if desktop and not args.bootstrap_window:
                terminal_arguments = (["--repair"] if args.repair else []) + arguments
                if bootstrap_terminal(terminal_arguments, env):
                    return 0
                raise RuntimeError("Bootstrap needs a terminal to display setup progress.\n"
                                   "Open a terminal in the application folder and run:\n"
                                   "bash direct/ubuntu/run.sh --repair")
            print("Preparing Ubuntu system prerequisites and the native private runtime.", flush=True)
            result = subprocess.call(["/usr/bin/python3", "-B", str(ROOT / "direct/ubuntu/setup.py")],
                                     cwd=ROOT, env=env)
            if result:
                if args.bootstrap_window:
                    print("\nBootstrap failed. Correct the error above, then launch again.", file=sys.stderr)
                    try:
                        input("Press Enter to close this setup window…")
                    except EOFError:
                        pass
                return result
            host_preflight(require_display=True)
            problem = runtime_problem(env)
            if problem:
                raise RuntimeError(problem)
        return launch_workspace(arguments, env, desktop=desktop)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        report_error(str(exc), desktop=desktop)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
