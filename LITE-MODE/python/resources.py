"""Read-only resource inventory shared with the main application's native store.

No toolkit, registry requests or package-manager writes. Paths exposed by the
protocol are opaque IDs from this inventory, never arbitrary file-read commands.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import zipfile

SOURCE_TYPES = {".ino", ".pde", ".c", ".cpp", ".h", ".hpp", ".txt"}
MAX_SOURCE = 512 * 1024


def main_download_directory(original, *, home=None):
    home = Path.home() if home is None else Path(home)
    for relative in ("index_json/arduino_browser_settings.json",
                     "src/dbs/arduino_browser_settings.json", "arduino_browser_settings.json"):
        path = Path(original) / relative
        try:
            if not path.is_file() or path.stat().st_size > 512 * 1024:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            value = data.get("download_dir") if isinstance(data, dict) else None
            if value:
                candidate = Path(os.path.expandvars(os.path.expanduser(value)))
                if candidate.is_absolute() and candidate.is_dir():
                    return candidate.resolve()
            break
        except (OSError, ValueError, TypeError):
            continue
    return home / "Documents/_MCUFlasherByNaph_src"


def bounded_json(path):
    try:
        if path.stat().st_size <= 256 * 1024:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def identifier(path):
    return hashlib.sha256(os.path.normcase(str(path)).encode()).hexdigest()[:24]


class Inventory:
    def __init__(self, state, main_core=None, main_download=None, *, home=None):
        self.state = Path(state)
        self.main_core = Path(main_core) if main_core else None
        self.main_download = Path(main_download) if main_download else None
        self.home = Path.home() if home is None else Path(home)
        self.packages = {}
        self.samples = {}
        self.lock = threading.RLock()

    @property
    def core(self):
        if self.main_core is not None:
            return self.main_core
        return self.state / ("engine/src/.platformio-mcu-gui" if os.name == "nt" else "platformio")

    def library_roots(self):
        roots = [(self.core / "lib", "Shared" if self.main_core else "Lite"), (self.state / "downloads/Libs", "Lite")]
        if self.main_core and self.main_core != self.core:
            roots.append((self.main_core / "lib", "Main"))
        if self.main_download:
            roots.append((self.main_download / "Libs", "Main"))
        roots.append((self.home / "Documents/Arduino/libraries", "Arduino"))
        for core, origin in ((self.core, "Shared framework" if self.main_core else "Lite framework"),):
            if core and (core / "packages").is_dir():
                for package in sorted((core / "packages").iterdir()):
                    if package.name.startswith("framework-"):
                        roots.append((package / "libraries", origin))
        return roots

    def scan(self, cancel=None):
        cancel = cancel or threading.Event()
        libraries, packages, seen = [], {}, set()
        for root, origin in self.library_roots():
            if cancel.is_set():
                raise InterruptedError("Resource scan stopped")
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
                if cancel.is_set():
                    raise InterruptedError("Resource scan stopped")
                if child.name.startswith((".", "_tmp")) or child.is_symlink():
                    continue
                path = child.resolve()
                if path in seen or not (child.is_dir() or child.suffix.lower() == ".zip"):
                    continue
                seen.add(path)
                data = bounded_json(child / "library.json") if child.is_dir() else {}
                try:
                    properties = child / "library.properties"
                    if properties.is_file() and properties.stat().st_size < 65536:
                        for line in properties.read_text(encoding="utf-8", errors="replace").splitlines():
                            key, sep, value = line.partition("=")
                            if sep and key.strip() in {"name", "version", "sentence"}:
                                data[key.strip()] = value.strip()
                except OSError:
                    continue
                key = identifier(path)
                packages[key] = path
                libraries.append({"id": key, "name": str(data.get("name") or child.stem)[:256],
                                  "version": str(data.get("version") or "Installed")[:80],
                                  "description": str(data.get("sentence") or data.get("description") or "")[:2048],
                                  "origin": origin})
        libraries.sort(key=lambda item: (item["name"].casefold(), item["origin"], item["version"]))
        with self.lock:
            self.packages = packages
        return libraries

    def examples(self, package_id, cancel=None):
        cancel = cancel or threading.Event()
        with self.lock:
            root = self.packages.get(package_id)
        if root is None:
            raise ValueError("Refresh Resources and select an installed library")
        samples = {}
        if root.is_file():
            with zipfile.ZipFile(root) as archive:
                if len(archive.infolist()) > 50000:
                    raise ValueError("Library archive exceeds the sample inventory budget")
                for entry in archive.infolist():
                    if cancel.is_set():
                        raise InterruptedError("Sample scan stopped")
                    relative = Path(entry.filename)
                    if (relative.is_absolute() or ".." in relative.parts or entry.is_dir()
                            or "examples" not in [part.casefold() for part in relative.parts]
                            or relative.suffix.lower() not in SOURCE_TYPES):
                        continue
                    key = identifier(str(root) + "!/" + entry.filename)
                    samples[key] = (root, entry.filename)
        else:
            for directory, names, files in os.walk(root, followlinks=False):
                if cancel.is_set():
                    raise InterruptedError("Sample scan stopped")
                names[:] = sorted(name for name in names if not name.startswith(".")
                                  and not (Path(directory) / name).is_symlink())
                relative = Path(directory).relative_to(root)
                if "examples" not in [part.casefold() for part in relative.parts]:
                    continue
                for name in sorted(files):
                    path = Path(directory) / name
                    if path.suffix.lower() in SOURCE_TYPES and not path.is_symlink():
                        key = identifier(path)
                        samples[key] = (root, path.relative_to(root).as_posix())
        if len(samples) > 12000:
            raise ValueError("Library exceeds the 12,000 sample-file budget")
        with self.lock:
            self.samples = samples
        return [{"id": key, "name": relative} for key, (_root, relative) in samples.items()]

    def source(self, sample_id):
        with self.lock:
            selected = self.samples.get(sample_id)
        if selected is None:
            raise ValueError("Select a sample from the current library")
        root, relative = selected
        if root.is_file():
            with zipfile.ZipFile(root) as archive:
                if archive.getinfo(relative).file_size > MAX_SOURCE:
                    raise ValueError("Sample exceeds the 512 KiB viewer budget")
                data = archive.read(relative)
        else:
            path = root / relative
            if not path.resolve(strict=True).is_relative_to(root) or path.stat().st_size > MAX_SOURCE:
                raise ValueError("Sample moved outside its library or exceeds 512 KiB")
            with path.open("rb") as stream:
                data = stream.read(MAX_SOURCE + 1)
        if len(data) > MAX_SOURCE or b"\0" in data:
            raise ValueError("Sample is too large or is not a text source file")
        return {"id": sample_id, "name": relative, "text": data.decode("utf-8-sig", errors="replace")}
