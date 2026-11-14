#!/usr/bin/env python3
"""Native Linux launch coordinator. Bootstrap may use system Python; the GUI never does."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

# Imported below ROOT initialization so this file also works as a direct script.

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from direct.ubuntu.user_install import persistent_setup_root
from direct.ubuntu.bootstrap_view import ENVIRONMENT_KEY, BootstrapWindow, run_logged


def application_update_config() -> dict:
    """Read the optional Bootstrap source-update policy without rewriting it."""
    path = ROOT / "src" / "dbs" / "bootstrap_config.json"
    try:
        if not path.is_file() or path.stat().st_size > 512 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def workspace_is_running() -> bool:
    """Avoid replacing source underneath an already-open workspace."""
    try:
        from src.modules.crash_detector import running_sessions
        return bool(running_sessions(ROOT))
    except Exception:
        # Session tracking is advisory for source refresh. A failure to inspect
        # old state must not make launching the installed application fail.
        return False


def available_application_update(arguments: list[str]):
    """Return a verified source revision for a normal native launch, if any."""
    if "--resume-application-update" in arguments:
        return None
    try:
        if workspace_is_running():
            return None
        from src.modules import app_update
        config = application_update_config()
        if not app_update.automatic_updates_enabled(config):
            return None
        checked = app_update.check_for_update(ROOT, config=config, timeout=app_update.STARTUP_CHECK_TIMEOUT_SECONDS)
        return checked.candidate if checked.status == "available" else None
    except Exception:
        # A source check is never allowed to block an otherwise usable native
        # workspace, including when the network is offline.
        return None


class TerminalProgress:
    """One live progress line in a terminal, or occasional plain lines in a log.

    A source clone can take minutes on a slow link. The bar, phase, transfer rate
    and elapsed time keep moving so it never looks stuck.
    """

    LOG_INTERVAL_SECONDS = 10.0

    def __init__(self, stream, app_update):
        self._stream = stream
        self._app_update = app_update
        try:
            self._live = bool(stream.isatty())
        except (AttributeError, ValueError, OSError):
            self._live = False
        encoding = (getattr(stream, "encoding", "") or "").replace("-", "").casefold()
        self._ascii = encoding != "utf8"
        self._shown = False
        self._width = 0
        self._logged_at = 0.0
        self._logged_phase = None

    @property
    def live(self) -> bool:
        return self._live

    def update(self, progress) -> None:
        text = progress.describe()
        try:
            if self._live:
                columns = shutil.get_terminal_size((100, 24)).columns
                line = f"  {self._app_update.progress_bar(progress.percent, 24, ascii_only=self._ascii)} {text}"
                line = line[:max(20, columns - 1)]
                self._stream.write("\r" + line + " " * max(0, self._width - len(line)))
                self._stream.flush()
                self._width = len(line)
                self._shown = True
                return
            now = time.monotonic()
            if progress.phase != self._logged_phase or now - self._logged_at >= self.LOG_INTERVAL_SECONDS:
                self._logged_phase, self._logged_at = progress.phase, now
                print(f"  {text}", file=self._stream, flush=True)
        except (OSError, ValueError):
            self._live = False  # A closed terminal must not stop the update itself.

    def finish(self) -> None:
        if self._shown:
            try:
                self._stream.write("\n")
                self._stream.flush()
            except (OSError, ValueError):
                pass
        self._shown = False
        self._width = 0


class DesktopProgress:
    """A small native progress window for an update started from All Apps.

    A desktop launch has no terminal, so before this the download ran with nothing
    on screen at all. zenity ships with the Ubuntu, Mint and Fedora desktops; its
    Cancel skips this update and opens the installed version instead.
    """

    # zenity that ends this quickly never showed a window (no display, a broken
    # theme): that is not a person pressing Cancel, so the update carries on.
    STARTUP_FAILURE_SECONDS = 1.0

    def __init__(self, zenity: str):
        self._started = time.monotonic()
        self._finished = False
        self._process = subprocess.Popen(
            [zenity, "--progress", "--title=MCU Flasher", "--width=520", "--percentage=0", "--auto-close",
             "--text=Updating MCU Flasher from GitHub…"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", start_new_session=True,
        )

    @classmethod
    def open(cls, *, desktop: bool):
        if not desktop or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return None
        zenity = shutil.which("zenity")
        if not zenity:
            return None
        try:
            return cls(zenity)
        except OSError:
            return None

    def _write(self, text: str) -> None:
        if self._finished or self._process.poll() is not None:
            return
        try:
            self._process.stdin.write(text)
            self._process.stdin.flush()
        except (OSError, ValueError):
            pass  # The window is only a view; the update never depends on it.

    def update(self, progress) -> None:
        import html
        if progress.percent is not None:
            # 100 closes the window, and installing the files still follows the download.
            self._write(f"{min(99, int(progress.percent))}\n")
        self._write(f"# {html.escape(progress.describe())}\n")

    def message(self, text: str) -> None:
        import html
        self._write(f"# {html.escape(text)}\n")

    def cancelled(self) -> bool:
        code = self._process.poll()
        return (not self._finished and code is not None
                and time.monotonic() - self._started >= self.STARTUP_FAILURE_SECONDS)

    def finish(self) -> None:
        if self._finished:
            return
        self._write("100\n")
        self._finished = True
        try:
            self._process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()


def install_application_update(candidate, arguments: list[str], env: dict[str, str],
                               run_script: str = "direct/ubuntu/run.sh", *, desktop: bool = False,
                               window: BootstrapWindow | None = None) -> bool:
    """Refresh verified source before runtime checks, then re-exec the launcher.

    ``window`` is the Bootstrap window of a desktop launch. It stays open across the
    restart, so the same window goes on to show any repair and the workspace opening.
    Without one, a terminal shows a live line and a desktop launch gets zenity.
    """
    from src.modules import app_update

    bar = TerminalProgress(sys.stdout, app_update)
    bootstrap = window
    if window is None:
        window = None if bar.live else DesktopProgress.open(desktop=desktop)
    else:
        window.section("Updating MCU Flasher from GitHub")
        # Closing the window skips this update and opens the installed version.
        window.cancellable(True)

    def report(message: str) -> None:
        bar.finish()
        print(message, flush=True)
        if window is not None:
            window.message(message)

    def progress(update) -> None:
        bar.update(update)
        if window is not None:
            window.update(update)

    try:
        installed = app_update.apply_source_update(
            ROOT, candidate, report=report, progress=progress,
            cancelled=window.cancelled if window is not None else None)
    except Exception as exc:
        bar.finish()
        if bootstrap is not None:
            bootstrap.cancellable(False)
            bootstrap.log(f"The update was not installed: {exc}", "warn")
        elif window is not None:
            window.finish()
        print(f"MCU Flasher source update was not installed: {exc}", file=sys.stderr)
        return False
    bar.finish()
    if bootstrap is not None:
        bootstrap.cancellable(False)
        bootstrap.log("Update installed.", "ok")
        bootstrap.status("Restarting MCU Flasher…")
        bootstrap.hand_over(env)
    elif window is not None:
        window.message("Update installed. Opening MCU Flasher…")
        window.finish()

    print(
        f"Installed GitHub source {installed.candidate.branch}@{installed.candidate.commit[:12]} "
        f"({installed.strategy}); restarting Bootstrap…",
        flush=True,
    )
    retained = [str(argument) for argument in arguments if argument != "--resume-application-update"]
    command = ["/bin/bash", str(ROOT / run_script), "--resume-application-update", *retained]
    try:
        os.execvpe(command[0], command, env)
    except OSError as exc:
        # Source replacement is complete and verified. Continue through the
        # currently loaded coordinator if the shell cannot be re-executed.
        if bootstrap is not None:
            bootstrap.take_back(env)
        print(f"MCU Flasher could not restart Bootstrap after the source update: {exc}",
              file=sys.stderr)
    return True


def clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE",
                "PYTHONEXEPATH", "PIO_PYTHON_EXE", "PLATFORMIO_PYTHON_EXE", "PLATFORMIO_PENV_DIR",
                "MCU_FLASHER_OFFLINE_RUNTIME", "MCU_FLASHER_WORKSPACE_RUNTIME",
                "MCU_FLASHER_APP_ROOT", "PIP_NO_INDEX", "QT_PLUGIN_PATH",
                "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH", ENVIRONMENT_KEY):
        env.pop(key, None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def report_error(message: str, *, desktop: bool, window: BootstrapWindow | None = None) -> None:
    print(message, file=sys.stderr)
    if window is not None and window.alive():
        # The Bootstrap window keeps the error on screen until it is closed.
        window.log(message, "fail")
        window.finish(False, "MCU Flasher could not start. Correct the error above, then launch again.")
        return
    if not desktop or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    zenity = shutil.which("zenity")
    if zenity:
        try:
            subprocess.run([zenity, "--error", "--title=MCU Flasher", "--width=620",
                            "--no-markup", "--text=" + message], check=False)
        except OSError:
            pass  # The same complete diagnostic is already on stderr.


class BackgroundCheck:
    """Run a read-only check on a thread while the GitHub update check waits on the network."""

    def __init__(self, function, *arguments):
        import threading
        self._value = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(function, *arguments),
                                        name="MCULaunchCheck", daemon=True)
        self._thread.start()

    def _run(self, function, *arguments) -> None:
        try:
            self._value = function(*arguments)
        except BaseException as exc:  # Raised again where the answer is used.
            self._error = exc

    def wait(self) -> None:
        self._thread.join()

    def result(self):
        self._thread.join()
        if self._error is not None:
            raise self._error
        return self._value


def runtime_problem(env: dict[str, str]) -> str | None:
    from src.modules.ubuntu_architecture import elf_matches_host, host_architecture
    from src.modules.ubuntu_release import linux_label

    label = linux_label()
    python = ROOT / ".venv-linux/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        return f"The {label} private runtime is missing or has a broken interpreter link."
    if not elf_matches_host(python):
        return (f"The {label} private runtime interpreter does not match this host's "
                f"{host_architecture()} architecture. Run Bootstrap repair on this computer.")
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
            return f"The {label} runtime cannot be validated: {exc}"
        if result.returncode:
            return result.stderr.strip() or f"The {label} native board packages need Bootstrap preparation."
    return None


def bootstrap_terminal(arguments: list[str], env: dict[str, str]) -> bool:
    from src.modules.ubuntu_release import require_supported_ubuntu

    version = require_supported_ubuntu()
    child = ["/bin/bash", str(ROOT / "direct/ubuntu/run.sh"), "--bootstrap-window", *arguments]
    terminals = (("x-terminal-emulator", ("-e",)), ("gnome-terminal", ("--",)),
                 ("konsole", ("-e",)), ("xfce4-terminal", ("--execute",)),
                 ("xterm", ("-e",)), ("kitty", ("--",)), ("alacritty", ("-e",)))
    if version is not None and version[:2] >= (26, 4):
        # Ubuntu 26.04 and Fedora (whose admitted releases all compare above it) ship
        # Ptyxis. Use a separate instance and literal argv, rather than forwarding
        # setup to an existing terminal window.
        terminals = (("ptyxis", ("--standalone", "--")), *terminals)
    for name, options in terminals:
        terminal = shutil.which(name)
        if terminal:
            try:
                subprocess.Popen([terminal, *options, *child], cwd=ROOT, env=env,
                                 start_new_session=True)
                return True
            except OSError:
                continue
    return False


def launch_workspace(arguments: list[str], env: dict[str, str], *, desktop: bool,
                     window: BootstrapWindow | None = None) -> int:
    command = [str(ROOT / ".venv-linux/bin/python"), "-B", str(ROOT / "mcu_flash_gui.py"), *arguments]
    if window is not None:
        window.status("Opening MCU Flasher…")
    if not desktop:
        if window is not None:
            window.finish(True, "Opening MCU Flasher…")
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
        report_error(f"MCU Flasher could not start.\n{detail}\nLaunch log: {path}", desktop=True, window=window)
        return process.returncode or 1
    if window is not None:
        window.finish(True, "MCU Flasher is open.")
    return 0


def owned_installation_elsewhere() -> Path | None:
    """The per-user installation, when this launch is a different (portable) copy.

    A ZIP folder or checkout whose own runtime needs repair simply hands over to an
    installation that already exists; that copy checks and repairs itself.
    """
    from src.modules.ubuntu_release import linux_distribution
    from direct.ubuntu.user_install import installation_directory, is_owned_installation
    try:
        if linux_distribution().family != "ubuntu":
            return None
        destination = installation_directory()
    except (OSError, RuntimeError):
        return None
    if destination == ROOT or not is_owned_installation(destination):
        return None
    return destination


def forward_to_installation(installed: Path, arguments: list[str], env: dict[str, str], *,
                            repair: bool, project: str | None, window: BootstrapWindow | None) -> int:
    """Continue this desktop launch in the installed copy, keeping any open window."""
    forwarded = list(arguments)
    if project:
        forwarded[1] = str(Path(project).absolute())
    if repair:
        forwarded.insert(0, "--repair")
    command = [sys.executable, "-B", str(installed / "direct/ubuntu/launch.py"), "--desktop", *forwarded]
    if window is not None:
        window.hand_over(env)
    try:
        os.chdir(installed)
        os.execvpe(command[0], command, env)
    except OSError:
        if window is not None:
            window.take_back(env)
        # stdin is not a terminal, so the installed launcher still treats this as a desktop launch.
        return subprocess.call(command, cwd=installed, env=env, stdin=subprocess.DEVNULL)
    return 0  # Not reached: exec replaced this process.


def main(argv=None) -> int:
    from src.modules.ubuntu_release import enforce_supported_ubuntu, linux_label
    if not enforce_supported_ubuntu():
        return 1
    label = linux_label()
    arguments_in = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true", help="Repair native runtime dependencies before opening")
    parser.add_argument("--check", action="store_true", help="Diagnose dependencies without installing or launching")
    parser.add_argument("--install-shortcut", action="store_true", help="Create a shortcut and Applications menu entry")
    parser.add_argument("--project", help="Open this sketch folder")
    parser.add_argument("sketch", nargs="?", help="Optional sketch file or folder (legacy launch syntax)")
    parser.add_argument("--new-window", action="store_true", help="Open an independent sketch window")
    parser.add_argument("--desktop", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--bootstrap-window", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--resume-application-update", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(arguments_in)
    desktop = not args.check and (args.bootstrap_window or (args.desktop and not sys.stdin.isatty()))
    env = clean_environment()
    window: BootstrapWindow | None = None
    try:
        if not args.check and hasattr(os, "geteuid") and os.geteuid() == 0:
            raise RuntimeError(f"Run MCU Flasher as your normal {label} desktop account, without sudo.")
        if args.install_shortcut and not args.check:
            from direct.ubuntu.install_desktop import main as install_shortcut
            return install_shortcut(["--install"])
        if not args.check:
            # A launcher restarted after an update keeps the window that showed it.
            window = BootstrapWindow.adopt(env)
        from direct.ubuntu.preflight import MissingSystemDependencies, host_preflight

        def host_check() -> str | None:
            try:
                host_preflight(require_display=not args.check)
            except MissingSystemDependencies as missing:
                if args.check:
                    raise
                return str(missing)
            return None

        # Native libraries come first (a tenth of a second): the Qt runtime is never
        # validated on a host whose system prerequisites still need repair.
        host_problem = host_check()
        runtime_check = None
        if not args.check:
            # The private runtime's check (about half a second of child processes) and
            # GitHub's answer (one or two network round trips) used to run one after
            # the other before any window could appear. Neither changes anything.
            if not args.repair and host_problem is None:
                runtime_check = BackgroundCheck(runtime_problem, env)
            candidate = available_application_update(arguments_in)
            if candidate is not None:
                if runtime_check is not None:
                    # Never replace source files under a check that is reading them.
                    runtime_check.wait()
                if window is None and desktop and not args.bootstrap_window:
                    window = BootstrapWindow.open(ROOT, env=env, subtitle="Updating from GitHub")
                if install_application_update(candidate, arguments_in, env, desktop=desktop, window=window):
                    # Normally the launcher has restarted by now. If it could not, the
                    # earlier answers described the replaced source: ask again.
                    runtime_check = None
                    host_problem = host_check()
        if args.check:
            problem = runtime_problem(env)
            if problem:
                print(problem, file=sys.stderr)
                return 1
            print(f"{label} system dependencies and the private runtime are ready.")
            return 0
        from src.modules.runtime_resources import enforce_minimum_cpu_requirement
        if not enforce_minimum_cpu_requirement():
            return 1
        arguments = []
        project = args.project or args.sketch
        if project:
            arguments += ["--project", project]
        if args.new_window:
            arguments += ["--new-window"]
        problem = host_problem or (f"Explicit {label} repair requested." if args.repair else
                                   runtime_check.result() if runtime_check is not None else runtime_problem(env))
        if problem:
            print(problem, file=sys.stderr)
            if desktop and not args.bootstrap_window:
                installed = owned_installation_elsewhere()
                if installed is not None:
                    # This portable copy's runtime needs work, but the installed copy
                    # checks and repairs itself: no terminal, no second setup.
                    return forward_to_installation(installed, arguments, env, repair=args.repair,
                                                   project=project, window=window)
                if window is None:
                    window = BootstrapWindow.open(ROOT, env=env, subtitle="Runtime setup · Prepare your workspace")
                if window is None:
                    # Before the private runtime has Qt (a first run), setup shows in a terminal.
                    terminal_arguments = (["--repair"] if args.repair else []) + arguments
                    if bootstrap_terminal(terminal_arguments, env):
                        return 0
                    raise RuntimeError("Bootstrap needs a terminal to display setup progress.\n"
                                       "Open a terminal in the application folder and run:\n"
                                       "bash direct/ubuntu/run.sh --repair")
            preparing = f"Preparing {label} system prerequisites and the native private runtime."
            print(preparing, flush=True)
            if window is not None:
                window.subtitle("Runtime setup · Prepare your workspace")
                window.section("Preparing your workspace")
                window.log(problem, "dim")
                window.status(preparing)
            installed = persistent_setup_root(ROOT, report=window.message if window is not None else print)
            if installed != ROOT:
                if window is not None:
                    return forward_to_installation(installed, arguments, env, repair=args.repair,
                                                   project=project, window=window)
                forwarded = list(arguments)
                if project:
                    forwarded[1] = str(Path(project).absolute())
                if args.repair:
                    forwarded.insert(0, "--repair")
                # The installed launcher prepares its runtime at this final
                # location; a copied venv's absolute scripts are never reused.
                mode = ["--bootstrap-window"] if desktop else []
                return subprocess.call([sys.executable, "-B", str(installed / "direct/ubuntu/launch.py"),
                                        *mode, *forwarded], cwd=installed, env=env)
            setup = ["/usr/bin/python3", "-B", str(ROOT / "direct/ubuntu/setup.py")]
            if window is not None:
                log_path = ROOT / "logs" / f"ubuntu-bootstrap-{time.strftime('%Y%m%d-%H%M%S')}.log"
                result = run_logged(setup, cwd=ROOT, env=env, window=window, log_path=log_path)
            else:
                result = subprocess.call(setup, cwd=ROOT, env=env)
            if result:
                if window is not None:
                    window.finish(False, "Bootstrap failed. Correct the error above, then launch again.")
                elif args.bootstrap_window:
                    print("\nBootstrap failed. Correct the error above, then launch again.", file=sys.stderr)
                    try:
                        input("Press Enter to close this setup window…")
                    except EOFError:
                        pass
                return result
            # apt/pip ran in a child. Drop this launcher's pre-install negative
            # import results before checking freshly installed ARM Qt5.
            importlib.invalidate_caches()
            host_preflight(require_display=True)
            problem = runtime_problem(env)
            if problem:
                raise RuntimeError(problem)
            if window is not None:
                window.log("The private runtime is ready.", "ok")
        if label == "Ubuntu":
            from direct.ubuntu.user_install import refresh_registered_lite
            refresh_registered_lite(ROOT)
        return launch_workspace(arguments, env, desktop=desktop, window=window)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        report_error(str(exc), desktop=desktop, window=window)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
