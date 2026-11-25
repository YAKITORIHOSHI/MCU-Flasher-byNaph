"""Where Windows components come from, and when Windows may ask for permission.

winget supplies what it packages (Arduino CLI, WebView2, Node.js LTS) and verifies
each download itself; the bundled installers remain for PCs without winget and for
the USB drivers, which winget does not package. Arduino CLI and Node.js can also be
unpacked for the current user alone, checked against their publisher's checksums, so
neither they nor their updates need an administrator.

A permission prompt is asked at most once per component and installer. A declined or
failed one is remembered for this user until setup or repair is run on purpose.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

WINGET_IDS = {
    "arduino_cli": "ArduinoSA.CLI",
    "webview2": "Microsoft.EdgeWebView2Runtime",
    "node": "OpenJS.NodeJS.LTS",
    "python": "Python.Python.3.14",
}
# Installed already, or nothing newer to install: the component check decides.
WINGET_ALREADY = frozenset((0x8A150061, 0x8A15002B, 0x8A15010D))
ARDUINO_RELEASES = "https://github.com/arduino/arduino-cli/releases/download"
NODE_DIST = "https://nodejs.org/dist"
LEDGER_NAME = "machine_setup.json"
_MAX_PROMPTS = 2


class PromptLedger:
    """What this user was already asked to allow, one small JSON file."""

    def __init__(self, path):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        components = value.get("components") if isinstance(value, dict) and value.get("schema") == 1 else None
        return components if isinstance(components, dict) else {}

    def _write(self, components: dict) -> bool:
        temporary = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({"schema": 1, "components": components}, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
            return True
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def settled(self, component: str, installer: str = "") -> str:
        """Why this component must not prompt again, or ""."""
        entry = self._read().get(component)
        if not isinstance(entry, dict) or entry.get("installer", "") != installer:
            return ""                                   # A different installer is a new question.
        outcome = str(entry.get("outcome") or "")
        if outcome in ("declined", "failed"):
            return outcome
        # "asked" without an answer means Setup was closed; ask once more, never forever.
        return "unanswered" if outcome == "asked" and int(entry.get("count") or 0) >= _MAX_PROMPTS else ""

    def record(self, component: str, outcome: str, installer: str = "") -> bool:
        components = self._read()
        if outcome == "done":
            return components.pop(component, None) is None or self._write(components)
        entry = components.get(component)
        same = isinstance(entry, dict) and entry.get("installer", "") == installer
        count = int(entry.get("count") or 0) if same else 0
        components[component] = {"outcome": outcome, "installer": installer, "count": count + (outcome == "asked"),
                                 "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        return self._write(components)

    def forget(self) -> None:
        """An explicit setup or repair asks again."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def installer_fingerprint(path) -> str:
    """Name an installer by its bytes, so an app update with the same one asks nothing new."""
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        return f"{Path(path).name.casefold()}:{digest.hexdigest()[:24]}"
    except OSError:
        return ""


def winget_path() -> str | None:
    found = shutil.which("winget") or shutil.which("winget.exe")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    candidate = Path(local) / "Microsoft" / "WindowsApps" / "winget.exe" if local else None
    try:
        return str(candidate) if candidate is not None and candidate.exists() else None
    except OSError:
        return None


def winget_command(winget: str, component: str, *, override: str = "", scope: str = "", version: str = "") -> list[str]:
    command = [winget, "install", "--exact", "--id", WINGET_IDS[component], "--source", "winget", "--silent",
               "--accept-source-agreements", "--accept-package-agreements", "--disable-interactivity"]
    if scope:
        command += ["--scope", scope]
    if version:
        command += ["--version", version]
    if override:
        command += ["--override", override]
    return command


def winget_install(component: str, *, timeout: float = 600, winget: str | None = None, run=subprocess.run,
                   override: str = "", scope: str = "", version: str = "") -> tuple[bool, str]:
    """Run winget for one known package; callers still verify the component itself."""
    if component not in WINGET_IDS:
        raise ValueError(f"No winget package is known for {component}")
    winget = winget or winget_path()
    if not winget:
        return False, "winget is not available on this PC"
    try:
        result = run(winget_command(winget, component, override=override, scope=scope, version=version),
                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                     encoding="utf-8", errors="replace", timeout=timeout,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, f"winget did not finish within {int(timeout)} seconds"
    except OSError as exc:
        return False, f"winget could not start: {exc}"
    if result.returncode == 0 or result.returncode & 0xFFFFFFFF in WINGET_ALREADY:
        return True, ""
    lines = [line.strip() for line in (result.stdout or "").splitlines() if len(line.strip()) > 3]
    return False, f"winget exit {result.returncode & 0xFFFFFFFF:#010x}" + (f": {lines[-1][:200]}" if lines else "")


def _fetch_text(url: str, *, limit: int = 4 * 1024 * 1024) -> str:
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": "mcu-flasher-setup/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(limit).decode("utf-8", errors="replace")


def published_sha256(listing: str, name: str) -> str:
    """The publisher's checksum for one exact file name, or ""."""
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return ""


def arduino_cli_archive(version: str, machine: str | None = None) -> str:
    machine = (machine or platform.machine()).lower()
    return f"arduino-cli_{version}_Windows_{'ARM64' if machine in ('arm64', 'aarch64') else '64bit'}.zip"


def arduino_cli_user_folder() -> Path | None:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(local) / "Programs" / "Arduino CLI" if local else None


def _unpack(archive: Path, staging: Path, wanted=None) -> None:
    """Unpack a verified ZIP whose members stay inside the staging folder."""
    root = staging.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if wanted is not None and not wanted(member.filename):
                continue
            target = (staging / member.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"Archive member leaves its folder: {member.filename}")
            bundle.extract(member, staging)


def install_arduino_cli_for_user(version: str, *, folder: Path | None = None, download, fetch=_fetch_text,
                                 machine: str | None = None) -> Path:
    """Publish the official Arduino CLI for this user; needs no administrator.

    ``download(url, path, sha256)`` must verify the checksum it is given.
    """
    folder = Path(folder) if folder is not None else arduino_cli_user_folder()
    if folder is None:
        raise OSError("This account has no per-user program folder (LOCALAPPDATA).")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[\w.]+)?", version):
        raise ValueError(f"Not an Arduino CLI version: {version!r}")
    name = arduino_cli_archive(version, machine)
    digest = published_sha256(fetch(f"{ARDUINO_RELEASES}/v{version}/{version}-checksums.txt"), name)
    if not digest:
        raise ValueError(f"Arduino publishes no checksum for {name}")
    folder.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".arduino-cli-", dir=folder.parent) as staging:
        archive = Path(staging) / name
        download(f"{ARDUINO_RELEASES}/v{version}/{name}", archive, digest)
        content = Path(staging) / "content"
        content.mkdir()
        _unpack(archive, content, lambda member: member in ("arduino-cli.exe", "LICENSE.txt"))
        executable = content / "arduino-cli.exe"
        with executable.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise ValueError("The Arduino CLI archive holds no Windows executable")
        folder.mkdir(parents=True, exist_ok=True)
        for item in content.iterdir():
            _publish(item, folder / item.name)
    return folder / "arduino-cli.exe"


def _publish(source: Path, target: Path) -> None:
    """Replace one file; a running executable is renamed aside first, as Windows allows."""
    try:
        os.replace(source, target)
        return
    except OSError:
        if not target.exists():
            raise
    retired = target.with_name(f"{target.name}.old-{os.getpid()}")
    os.replace(target, retired)
    try:
        os.replace(source, target)
    except OSError:
        os.replace(retired, target)
        raise
    try:
        retired.unlink()
    except OSError:
        pass        # Still in use; the next update replaces this file again.


def node_lts_release(fetch=_fetch_text) -> str:
    for entry in json.loads(fetch(f"{NODE_DIST}/index.json", limit=16 * 1024 * 1024)):
        if entry.get("lts") and re.fullmatch(r"v\d+\.\d+\.\d+", str(entry.get("version"))):
            return entry["version"]
    raise ValueError("Node.js publishes no LTS release")


def node_user_folder() -> Path | None:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(local) / "Programs" / "nodejs" if local else None


def install_node_for_user(*, folder: Path | None = None, download, fetch=_fetch_text, version: str = "",
                          machine: str | None = None) -> Path:
    """Publish the official Node.js LTS for this user; needs no administrator."""
    folder = Path(folder) if folder is not None else node_user_folder()
    if folder is None:
        raise OSError("This account has no per-user program folder (LOCALAPPDATA).")
    if folder.exists() and any(folder.iterdir()):
        raise OSError(f"{folder} already holds files; they were left unchanged")
    version = version or node_lts_release(fetch)
    machine = (machine or platform.machine()).lower()
    stem = f"node-{version}-win-{'arm64' if machine in ('arm64', 'aarch64') else 'x64'}"
    digest = published_sha256(fetch(f"{NODE_DIST}/{version}/SHASUMS256.txt"), f"{stem}.zip")
    if not digest:
        raise ValueError(f"Node.js publishes no checksum for {stem}.zip")
    folder.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".nodejs-", dir=folder.parent) as staging:
        archive = Path(staging) / f"{stem}.zip"
        download(f"{NODE_DIST}/{version}/{stem}.zip", archive, digest)
        content = Path(staging) / "content"
        content.mkdir()
        _unpack(archive, content)
        unpacked = content / stem
        if not (unpacked / "node.exe").is_file() or not (unpacked / "npm.cmd").is_file():
            raise ValueError("The Node.js archive holds no node.exe and npm.cmd")
        if folder.exists():
            folder.rmdir()                              # Empty, as checked above.
        os.replace(unpacked, folder)
    return folder / "node.exe"


def python_override(target: Path, log: Path | None = None) -> str:
    """The bundled Python installer's own switches, for winget's --override."""
    switches = ["/quiet", f'TargetDir="{target}"', "InstallAllUsers=0", "PrependPath=0", "AssociateFiles=0", "Shortcuts=0",
                "Include_test=0", "Include_doc=0", "Include_launcher=0", "InstallLauncherAllUsers=0"]
    if log is not None:
        switches += ["/log", f'"{log}"']
    return " ".join(switches)


def install_private_python(target: Path, *, log: Path | None = None, run=subprocess.run, timeout: float = 600) -> tuple[bool, str]:
    """Let winget fetch and verify Python, installed only into the private folder."""
    return winget_install("python", timeout=timeout, run=run, scope="user", override=python_override(target, log))


if __name__ == "__main__":
    sys.exit("This module is used by Setup.")
