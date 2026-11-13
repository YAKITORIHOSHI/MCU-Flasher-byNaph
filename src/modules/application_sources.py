"""Public application ownership for Bootstrap copies and release archives."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import subprocess

MANIFEST = ".mcu-flasher-sources.json"
EXCLUDED = frozenset({".git", "__pycache__", "node_modules", "_python", "env", ".venv", ".venv-linux", ".venv-macos",
    ".mcu_flasher_build_cache", ".mcu_ai_edits", ".pio", ".vscode", ".clangd", ".opencode", "temp", "logs", "test", "tests"})
DEVELOPMENT_ROOTS = frozenset({"audit", "desktop", "installers", ".github", ".migration-tools", ".tools", ".local"})
STATE_NAMES = frozenset({".mcu_gui_config.json", "gui_config.json", "dbs_notif.json", "bootstrap_config.json",
    "arduino_browser_settings.json", "arduino_cli_path.txt", ".owner_config.json", ".owner_tickets.json", ".mcu_cloud_link.json"})
LITE_PRIVATE = frozenset({"target", "bin", ".tools", ".local", ".venv", "evidence"})


def safe_relative(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or len(name) > 1024 or any(c in name for c in ("\\", "\0", "\n", "\r", ":")):
        raise RuntimeError("Invalid application source path")
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        raise RuntimeError("Unsafe application source path")
    return path


def is_application_source(name: str) -> bool:
    path = safe_relative(name)
    if path.parts[0] in DEVELOPMENT_ROOTS or any(part in EXCLUDED for part in path.parts):
        return False
    if path.name in STATE_NAMES or path.suffix in {".pyc", ".pyo"} or name == MANIFEST:
        return False
    if path.parts[0] == "LITE-MODE" and any(part in LITE_PRIVATE for part in path.parts[1:-1]):
        return False
    return True


def manifest_bytes(commit: str, committed_at: str, names) -> bytes:
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise RuntimeError("Invalid application source revision")
    files = sorted({name for name in names if is_application_source(name)})
    if not files or len(files) > 20_000:
        raise RuntimeError("Application source manifest exceeds its supported inventory")
    return (json.dumps({"schema": 1, "commit": commit, "committed_at": committed_at, "files": files}, indent=2) + "\n").encode("utf-8")


def source_inventory(root: Path) -> list[Path] | None:
    """Use tracked paths for checkouts, published paths for ZIPs, else legacy copy."""
    if (root / ".git").exists():
        raw = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"], timeout=30)
        names = [name for name in raw.decode("utf-8").split("\0") if name]
        names = [name for name in names if is_application_source(name)]
    elif (root / MANIFEST).is_file():
        path = root / MANIFEST
        if path.is_symlink() or path.stat().st_size > 4 * 1024**2:
            raise RuntimeError("Invalid public application source manifest")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("Invalid public application source manifest")
        names = data.get("files")
        if (data.get("schema") != 1 or not re.fullmatch(r"[a-f0-9]{40}", str(data.get("commit", "")))
                or not isinstance(names, list) or not names or len(names) > 20_000):
            raise RuntimeError("Invalid public application source inventory")
        if any(not is_application_source(name) for name in names):
            raise RuntimeError("Private state must not enter the public application inventory")
        names = names + [MANIFEST]
    else:
        return None
    if len(names) > 20_000 or len({name.casefold() for name in names}) != len(names):
        raise RuntimeError("Application source inventory contains colliding names or too many files")
    return [Path(*safe_relative(name).parts) for name in sorted(names)]
