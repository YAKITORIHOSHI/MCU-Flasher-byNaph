"""The coding agents the AI Assistant offers, found by their files alone.

Nothing here runs an agent to decide whether it is installed: a slow, locked or
signed-out program is still installed. Downloads live in src/modules/agent_setup.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import sys

from src.modules.platform_runtime import app_data_dir


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
    executables: tuple = ()
    markers: tuple = field(default=(), compare=False)   # Text that tells it from another program of that name


AGENTS = (
    Agent("gemini", "Gemini", "Google", "Needs a Gemini Code Assist Standard or Enterprise licence, or an API key",
          executables=("gemini",), markers=("gemini-cli", "@google/gemini")),
    Agent("codex", "Codex", "OpenAI", "Signs in with ChatGPT or an API key", executables=("codex",)),
    Agent("claude", "Claude", "Anthropic", "Signs in with Claude or an API key", executables=("claude",)),
    Agent("hermes", "Hermes", "Nous Research", "Works with the model provider you choose",
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
    """Where this app keeps agents it downloaded itself; other installs stay where they are."""
    return app_data_dir() / "agents"


def agent_prefix(agent_id: str) -> Path:
    return agents_root() / agent(agent_id).id


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
                            ("LOCALAPPDATA", ("Programs/OpenAI/Codex/bin", "hermes/bin", "Microsoft/WinGet/Links", "npm",
                                              "Programs/nodejs")),
                            ("ProgramFiles", ("nodejs",))):
        base = os.environ.get(variable, "").strip()
        if base:
            found += [Path(base) / part for part in parts]
    return found


def _is_this(spec: Agent, path: Path) -> bool:
    """Tell an agent from another program of the same name, from its location or its first text."""
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
    for path in candidates(spec.id):
        try:
            found = usable(path)
        except OSError:
            found = None
        if found and _is_this(spec, Path(found)):
            return found
    return None


def installed_agents() -> dict[str, str | None]:
    return {spec.id: find_agent(spec.id) for spec in AGENTS}


def agent_argv(agent_id: str, executable, project) -> list[str]:
    """Literal arguments: the executable, and the project only where the agent takes it."""
    executable, project = os.fspath(executable), os.fspath(project)
    if not executable or not project:
        raise ValueError("An assistant requires an executable and project directory.")
    return [executable, os.path.abspath(project)] if agent(agent_id).takes_project else [executable]


def agent_environment(agent_id: str, base=None) -> dict[str, str]:
    """Additions to a session's environment; the app's own Node.js only for its own npm agents."""
    base = os.environ if base is None else base
    additions = {"OPENCODE_DISABLE_AUTOUPDATE": "1"} if agent(agent_id).id == "opencode" else {}
    folders = []
    node = node_directory()
    if node is not None and agent_prefix(agent_id).is_dir():
        folders.append(str(node))
    if sys.platform != "win32":
        local = str(Path.home() / ".local/bin")
        if local not in (base.get("PATH") or "").split(os.pathsep):
            folders.append(local)
    if folders:
        additions["PATH"] = os.pathsep.join([*folders, base.get("PATH") or os.defpath])
    return additions


def install_plan(agent_id: str) -> InstallPlan | None:
    """How to download this agent, or None for the built-in one, which Bootstrap provides."""
    windows = sys.platform == "win32"
    shell = "powershell" if windows else "bash"
    suffix = "ps1" if windows else "sh"
    if agent_id == "gemini":
        return InstallPlan("npm", "npm (@google/gemini-cli)", "npm install -g @google/gemini-cli", package="@google/gemini-cli")
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
        return InstallPlan("script", "hermes-agent.nousresearch.com (Nous Research's installer)",
                           "irm https://hermes-agent.nousresearch.com/install.ps1 | iex" if windows else
                           "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
                           url=f"https://hermes-agent.nousresearch.com/install.{suffix}", shell=shell,
                           arguments=("-NonInteractive", "-SkipBrowser", "-SkipComputerUse") if windows else
                           ("--non-interactive", "--skip-browser", "--skip-computer-use"))
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
