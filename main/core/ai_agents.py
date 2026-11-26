"""The coding agents the AI Assistant offers, found by their files alone.

Nothing here runs an agent to decide whether it is installed: a slow, locked or
signed-out program is still installed. Downloads live in src/modules/agent_setup.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shlex
import shutil
import sys

from main.core.compiled_cache import host_namespace
from src.modules.platform_runtime import app_data_dir
from src.modules.ubuntu_architecture import MACH_O_MAGICS

APP_ROOT = Path(__file__).resolve().parents[2]
# Agents this app downloads itself, inside its own folder, one subfolder per OS and CPU.
AGENTS_FOLDER = ".ai-agents"


@dataclass(frozen=True)
class InstallPlan:
    """How an agent's maker distributes it, for this account and without an administrator."""
    kind: str                       # "script": the maker's installer; "npm": its npm package
    source: str                     # Where it comes from, as shown before anything is downloaded
    command: str                    # What the maker documents, for doing it by hand
    url: str = ""
    shell: str = ""
    package: str = ""
    arguments: tuple = ()
    environment: tuple = ()


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    maker: str
    note: str = ""
    builtin: bool = False
    # Full-screen agents take over the terminal; inline ones print into it.
    inline: bool = True
    takes_project: bool = False     # Wants the project folder as an argument
    arguments: tuple = ()           # Always passed, before the project
    native: bool = False            # Only a native program counts, never a script of that name
    executables: tuple = ()
    markers: tuple = field(default=(), compare=False)   # Text that tells it from another program of that name
    # Kept whole inside this app's folder (see isolation()); a copy elsewhere never counts.
    isolated: bool = False
    launcher: str = ""              # An isolated agent's program, relative to its own home


@dataclass(frozen=True)
class Isolation:
    """Where an isolated agent keeps what its installer would spread across the home folder."""
    root: Path                      # The agent's whole folder
    data: Path                      # Its own home: program, tools, caches, settings and sign-ins
    home: Path                      # Stands in for the account's home while its installer runs
    temp: Path                      # Temporary files of the installer and of its sessions


AGENTS = (
    # The Antigravity editor installs a script called agy too; that one opens its window.
    Agent("antigravity", "Antigravity", "Google", "Signs in with Google and acts without asking permission",
          arguments=("--dangerously-skip-permissions",), native=True, executables=("agy",)),
    Agent("codex", "Codex", "OpenAI", "Signs in with ChatGPT or an API key", executables=("codex",)),
    Agent("claude", "Claude", "Anthropic", "Signs in with Claude or an API key", executables=("claude",)),
    # Hermes brings its own Python, Node.js, ffmpeg and caches, about 3 GB in all.
    Agent("hermes", "Hermes", "Nous Research", "Works with the model provider you choose",
          isolated=True, launcher="hermes-agent/.hermes/bin/hermes",
          executables=("hermes",), markers=("hermes-agent", "hermes_cli", "hermes_agent", ".hermes", "hermes\\bin", "hermes/bin")),
    Agent("opencode", "OpenCode", "Open source", "Comes with MCU Flasher", builtin=True, inline=False,
          takes_project=True, executables=("opencode",)),
)
DEFAULT_AGENT = "opencode"
_BY_ID = {agent.id: agent for agent in AGENTS}
_WINDOWS_SUFFIXES = (".cmd", ".exe", ".bat")


def agent(agent_id) -> Agent:
    return _BY_ID.get(str(agent_id or ""), _BY_ID[DEFAULT_AGENT])


def agents_root() -> Path:
    """Where this app keeps agents it downloaded itself; other installs stay where they are.

    Linux and macOS keep them in this app's own folder. Windows keeps its per-user data
    folder: an agent's deepest files already approach the 260-character path limit there.
    """
    if sys.platform == "win32":
        return app_data_dir() / "agents"
    return APP_ROOT / AGENTS_FOLDER / host_namespace()


def agent_prefix(agent_id: str) -> Path:
    return agents_root() / agent(agent_id).id


def isolation(agent_id: str) -> Isolation | None:
    """An isolated agent's folders, or None where it installs where its maker puts it."""
    spec = agent(agent_id)
    if not spec.isolated or sys.platform == "win32":
        return None
    root = agent_prefix(spec.id)
    return Isolation(root=root, data=root / "data", home=root / "home", temp=root / "tmp")


def _isolated_variables(agent_id: str, place: Isolation) -> dict[str, str]:
    if agent_id == "hermes":
        # Hermes keeps one gateway per account under ~/.local/state; this copy keeps its own.
        return {"HERMES_HOME": str(place.data),
                "HERMES_GATEWAY_LOCK_DIR": str(place.home / ".local/state/hermes/gateway-locks")}
    return {}


def node_directory() -> Path | None:
    """The private Node.js this app downloaded for npm agents, if there is one."""
    try:
        versions = sorted((path for path in (agents_root() / "node").iterdir() if path.name.startswith("v")),
                          key=lambda path: [int(part) for part in path.name[1:].split(".") if part.isdigit()], reverse=True)
    except OSError:
        return None
    for version in versions:
        folder = version if sys.platform == "win32" else version / "bin"
        if (folder / ("node.exe" if sys.platform == "win32" else "node")).is_file():
            return folder
    return None


def _posix_directories() -> list[Path]:
    home = Path.home()
    found = [home / name for name in (".local/bin", ".npm-global/bin", ".bun/bin", "bin", ".volta/bin")]
    # A desktop session often starts without Homebrew's folder on PATH.
    found += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/home/linuxbrew/.linuxbrew/bin")]
    try:
        found += sorted((home / ".nvm/versions/node").glob("*/bin"), reverse=True)[:4]
    except OSError:
        pass
    return found


def _windows_directories() -> list[Path]:
    found = []
    for variable, parts in (("APPDATA", ("npm",)), ("USERPROFILE", (".local/bin", "scoop/shims", "AppData/Roaming/npm")),
                            ("LOCALAPPDATA", ("agy/bin", "Programs/OpenAI/Codex/bin", "hermes/bin", "Microsoft/WinGet/Links", "npm",
                                              "Programs/nodejs")),
                            ("ProgramFiles", ("nodejs",))):
        base = os.environ.get(variable, "").strip()
        if base:
            found += [Path(base) / part for part in parts]
    return found


def _is_this(spec: Agent, path: Path) -> bool:
    """Tell an agent from another program of the same name, from its location or its first text."""
    if spec.native:
        try:
            with path.open("rb") as stream:
                head = stream.read(4)
        except OSError:
            return False
        return head[:2] == b"MZ" or head == b"\x7fELF" or head in MACH_O_MAGICS
    if not spec.markers:
        return True
    try:
        places = f"{path}\n{path.resolve()}".replace("\\", "/").casefold()
        if any(marker.replace("\\", "/") in places for marker in spec.markers):
            return True
        with path.open("rb") as stream:
            head = stream.read(8192)
    except OSError:
        return False
    return head[:2] != b"MZ" and head[:4] != b"\x7fELF" and any(marker.encode() in head.lower() for marker in spec.markers)


def _starts_inside(path: Path, place: Isolation) -> bool:
    """Whether an isolated agent's launcher starts a program inside its own folder.

    Its installer writes absolute paths, so after this app's folder moves the launcher still
    names the old place; it then counts as missing, and downloading again repairs it in place.
    A launcher without an ``exec`` line is not judged.
    """
    try:
        with path.open("rb") as stream:
            text = stream.read(16384).decode("utf-8", "replace")
    except OSError:
        return False
    line = next((line for line in text.splitlines() if line.startswith("exec ")), None)
    if line is None:
        return True
    try:
        words = shlex.shlex(line[5:], posix=True)
        words.whitespace_split = True
        program = Path(words.get_token() or "")
    except ValueError:
        return True
    if not program.is_absolute():
        return True         # A command found through PATH or built when it runs is not judged.
    try:
        return program.is_file() and program.resolve().is_relative_to(place.data.resolve())
    except (OSError, RuntimeError):
        return False


def _usable_posix(path: Path) -> str | None:
    from main.platforms.ubuntu_opencode import _native_executable
    return _native_executable(path)


def _usable_windows(path: Path) -> str | None:
    from src.modules.opencode_npm import is_valid_pe_binary, shim_usable
    try:
        if not path.is_file():
            # An app execution alias (winget, Store) is a reparse point that cannot be opened.
            return str(path) if path.suffix.lower() == ".exe" and "windowsapps" in str(path).lower() and path.exists() else None
        if path.suffix.lower() == ".exe":
            return str(path) if is_valid_pe_binary(path) or path.is_symlink() else None
        return str(path) if path.stat().st_size > 0 and shim_usable(path) else None
    except OSError:
        return None


def candidates(agent_id: str) -> list[Path]:
    """Every place this agent could be, the app's own download first."""
    spec, found = agent(agent_id), []
    place = isolation(agent_id)
    if place is not None:
        # Only its own copy: one elsewhere keeps another home that is not this app's to start.
        return [place.data / spec.launcher]
    prefix = agent_prefix(agent_id)
    windows = sys.platform == "win32"
    for name in spec.executables:
        names = [name + suffix for suffix in _WINDOWS_SUFFIXES] if windows else [name]
        for folder in ([prefix] if windows else [prefix / "bin"]):
            found += [folder / item for item in names]
        for item in names:
            try:
                located = shutil.which(item)
            except (OSError, ValueError):
                located = None
            if located:
                found.append(Path(located))
        for folder in (_windows_directories() if windows else _posix_directories()):
            found += [folder / item for item in names]
        if spec.id == "claude":
            found.append(Path.home() / ".claude/local" / (names[0] if not windows else "claude.exe"))
    return list(dict.fromkeys(found))


def find_agent(agent_id: str) -> str | None:
    """This agent's executable, or None. Reads files only; use a worker thread."""
    spec = agent(agent_id)
    if spec.id == "opencode":
        if sys.platform == "win32":
            from src.modules.dedicated_AI import find_opencode_cli
        else:
            from main.platforms.ubuntu_opencode import find_opencode_cli
        return find_opencode_cli()
    usable = _usable_windows if sys.platform == "win32" else _usable_posix
    place = isolation(spec.id)
    for path in candidates(spec.id):
        try:
            found = usable(path)
        except OSError:
            found = None
        if found and _is_this(spec, Path(found)) and (place is None or _starts_inside(Path(found), place)):
            return found
    return None


def internet_available(timeout: float = 2.0) -> bool:
    """Whether this computer reaches the internet: every assistant works through its maker's
    servers, so without it the list has nothing to offer. Never call from the GUI thread."""
    import socket
    import threading
    import urllib.request
    reached = threading.Event()

    def connect(host):
        try:
            socket.create_connection((host, 443), timeout=timeout).close()
            reached.set()
        except OSError:
            pass

    def fetch(url):
        # Honours a configured proxy, for networks that only allow web traffic through one.
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                if response.status in (200, 204):
                    reached.set()
        except Exception:
            pass
    probes = [threading.Thread(target=connect, args=(host,), daemon=True)
              for host in ("1.1.1.1", "8.8.8.8", "github.com")]
    probes.append(threading.Thread(target=fetch, args=("http://clients3.google.com/generate_204",), daemon=True))
    for probe in probes:
        probe.start()
    return reached.wait(timeout + .5)


def installed_agents() -> dict[str, str | None]:
    return {spec.id: find_agent(spec.id) for spec in AGENTS}


def agent_argv(agent_id: str, executable, project) -> list[str]:
    """Literal arguments: the executable, its fixed options, and the project only where the agent takes it."""
    executable, project = os.fspath(executable), os.fspath(project)
    if not executable or not project:
        raise ValueError("An assistant requires an executable and project directory.")
    spec = agent(agent_id)
    return [executable, *spec.arguments, *([os.path.abspath(project)] if spec.takes_project else [])]


def agent_environment(agent_id: str, base=None) -> dict[str, str | None]:
    """Changes to a session's environment, where None removes a variable.

    The app's own Node.js reaches only its own npm agents. An isolated agent gets its own home,
    temporary folder and gateway locks, and none of the app's bytecode folder, which is outside.
    """
    base = os.environ if base is None else base
    spec = agent(agent_id)
    additions: dict[str, str | None] = {"OPENCODE_DISABLE_AUTOUPDATE": "1"} if spec.id == "opencode" else {}
    place = isolation(spec.id)
    if place is not None:
        additions.update(_isolated_variables(spec.id, place))
        additions["PYTHONPYCACHEPREFIX"] = None
        try:
            place.temp.mkdir(parents=True, exist_ok=True)
            additions.update(dict.fromkeys(("TMPDIR", "TMP", "TEMP"), str(place.temp)))
        except OSError:
            pass            # The inherited temporary folder still works.
    folders = []
    node = node_directory()
    if node is not None and place is None and agent_prefix(agent_id).is_dir():
        folders.append(str(node))
    if sys.platform != "win32":
        local = str(Path.home() / ".local/bin")
        if local not in (base.get("PATH") or "").split(os.pathsep):
            folders.append(local)
    if folders:
        additions["PATH"] = os.pathsep.join([*folders, base.get("PATH") or os.defpath])
    return additions


def installer_environment(agent_id: str, base=None) -> dict[str, str | None]:
    """A session's changes plus a stand-in home folder for the maker's installer.

    Installers write commands to ~/.local/bin, PATH lines to shell profiles and caches under
    ~/.cache; with HOME and the XDG folders inside the agent's folder, all of that stays there.
    """
    additions = agent_environment(agent_id, base)
    place = isolation(agent_id)
    if place is not None:
        home = place.home
        additions.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                          "XDG_CACHE_HOME": str(home / ".cache"), "XDG_DATA_HOME": str(home / ".local/share"),
                          "XDG_STATE_HOME": str(home / ".local/state")})
    return additions


def without_removed(environment: dict) -> dict[str, str]:
    """Apply agent_environment's None removals to a complete environment."""
    return {key: value for key, value in environment.items() if value is not None}


def install_plan(agent_id: str) -> InstallPlan | None:
    """How to download this agent, or None for the built-in one, which Bootstrap provides."""
    windows = sys.platform == "win32"
    shell = "powershell" if windows else "bash"
    suffix = "ps1" if windows else "sh"
    if agent_id == "antigravity":
        return InstallPlan("script", "antigravity.google (Google's installer)",
                           "irm https://antigravity.google/cli/install.ps1 | iex" if windows else
                           "curl -fsSL https://antigravity.google/cli/install.sh | bash",
                           url=f"https://antigravity.google/cli/install.{suffix}", shell=shell)
    if agent_id == "codex":
        return InstallPlan("script", "chatgpt.com (OpenAI's installer)",
                           "irm https://chatgpt.com/codex/install.ps1 | iex" if windows else
                           "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
                           url=f"https://chatgpt.com/codex/install.{suffix}", shell=shell if windows else "sh",
                           environment=(("CODEX_NON_INTERACTIVE", "1"),))
    if agent_id == "claude":
        return InstallPlan("script", "claude.ai (Anthropic's installer)",
                           "irm https://claude.ai/install.ps1 | iex" if windows else
                           "curl -fsSL https://claude.ai/install.sh | bash",
                           url=f"https://claude.ai/install.{suffix}", shell=shell)
    if agent_id == "hermes":
        # The browser and computer-use tools are large and play no part in editing sketches.
        place = isolation(agent_id)
        inside = () if place is None else ("--hermes-home", str(place.data))
        return InstallPlan("script", "hermes-agent.nousresearch.com (Nous Research's installer)",
                           "irm https://hermes-agent.nousresearch.com/install.ps1 | iex" if windows else
                           "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash" +
                           (f" -s -- --hermes-home {shlex.quote(str(place.data))}" if place else ""),
                           url=f"https://hermes-agent.nousresearch.com/install.{suffix}", shell=shell,
                           arguments=("-NonInteractive", "-SkipBrowser", "-SkipComputerUse") if windows else
                           ("--non-interactive", "--skip-browser", "--skip-computer-use", *inside))
    return None


def last_agent() -> str:
    """The agent chosen last, so the list opens on it."""
    try:
        from main.core.config import _load_raw_config
        chosen = _load_raw_config().get("shared", {}).get("ai_assistant_agent", DEFAULT_AGENT)
    except Exception:
        chosen = DEFAULT_AGENT
    return chosen if chosen in _BY_ID else DEFAULT_AGENT


def remember_agent(agent_id: str) -> None:
    try:
        from main.core.config import _load_raw_config, _save_raw_config
        config = _load_raw_config()
        shared = config.setdefault("shared", {})
        if shared.get("ai_assistant_agent") != agent_id and agent_id in _BY_ID:
            shared["ai_assistant_agent"] = agent_id
            _save_raw_config(config)
    except Exception:
        pass            # A preference that cannot be saved never blocks the assistant.
