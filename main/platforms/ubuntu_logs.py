"""Present native Ubuntu build/upload output without changing programmer work.

These presenters consume only recognized child-output records. They never open
ports, inspect package stores, start processes or change operation phases.
"""
from __future__ import annotations

import re


_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DIAGNOSTIC = re.compile(
    r"\b(?:fatal\s+error|error|warning|failed|failure|exception|timed?\s*out|"
    r"permission\s+denied|cannot\s+open|can't\s+open)\b", re.IGNORECASE,
)
_PACKAGE = re.compile(
    r"\s+-\s+((?:framework|toolchain|tool|platform)-[\w.+-]+)\s+@\s+"
    r"(\S+(?:\s+\([^()\r\n]+\))?)\s*", re.IGNORECASE,
)
_MEMORY = re.compile(
    r"(RAM|Flash):\s*\[[=\s]*\]\s*[\d.]+%\s+"
    r"\(used\s+\d+\s+bytes\s+from\s+\d+\s+bytes\)", re.IGNORECASE,
)


def _clean(line: str) -> str:
    return _ESCAPES.sub("", str(line)).strip()


def _bounded(value: object, size: int = 1024) -> str:
    return str(value).encode("utf-8")[:size].decode("utf-8", errors="ignore")


class UbuntuBuildInfo:
    """Collect the exact selected target and its actual PlatformIO metadata."""

    def __init__(self, api, board_info: dict, board_name: str):
        self.api = api
        self.info = dict(board_info or {})
        self.board_name = _bounded(board_name)
        self.platform = ""
        self.hardware = ""
        self.packages: list[str] = []
        self.package_bytes = 0
        self.packages_omitted = False
        self._in_packages = False
        self._have_metadata = False
        self._shown = False

    def consume(self, line: str) -> bool:
        clean = _clean(line)
        if not clean:
            return False
        if _DIAGNOSTIC.search(clean):
            if self._have_metadata:
                self.flush()
            self._in_packages = False
            return False
        for pattern, attribute in (
            (r"PLATFORM:\s+(.+\(\d[^()\r\n]*\)\s*>\s*\S.+)", "platform"),
            (r"HARDWARE:\s+(.+\b\d+(?:\.\d+)?\s*[kmg]?Hz\b.+(?:RAM|Flash)\b.*)", "hardware"),
        ):
            match = re.fullmatch(pattern, clean, re.IGNORECASE)
            if match:
                if not self._shown:
                    setattr(self, attribute, _bounded(match.group(1)))
                self._have_metadata = True
                self._in_packages = False
                return True
        if clean.upper() == "PACKAGES:":
            self._in_packages = self._have_metadata = True
            return True
        package = _PACKAGE.fullmatch(_ESCAPES.sub("", str(line)).rstrip("\r\n")) if self._in_packages else None
        if package:
            row = f"{package.group(1)} @ {package.group(2)}"
            if not self._shown and row not in self.packages:
                row_bytes = len(row.encode("utf-8")) + (2 if self.packages else 0)
                if len(self.packages) < 24 and self.package_bytes + row_bytes <= 4096:
                    self.packages.append(row)
                    self.package_bytes += row_bytes
                else:
                    self.packages_omitted = True
            return True
        self._in_packages = False
        # DEBUG normally sits between HARDWARE and PACKAGES. Leave its own
        # routing to the backend while collecting the complete standard block.
        if re.fullmatch(r"DEBUG:\s+Current\s+\([^)]+\)(?:\s+.*)?", clean, re.IGNORECASE):
            return False
        if self._have_metadata:
            self.flush()
        return False

    def flush(self) -> None:
        if self._shown:
            return
        self._shown = True
        fields: list[tuple[str, str]] = []
        if self.board_name:
            fields.append(("Board", self.board_name))
        platform = str(self.info.get("platform") or "").strip()
        board = str(self.info.get("board") or "").strip()
        if platform and board:
            fields.append(("Target", _bounded(f"{platform}:{board}")))
        if self.info.get("framework"):
            fields.append(("Framework", _bounded(self.info["framework"])))
        if self.platform:
            fields.append(("Platform", self.platform))
        if self.hardware:
            fields.append(("Hardware", self.hardware))
        if self.packages:
            fields.append(("Packages", "; ".join(self.packages)))
        if self.packages_omitted:
            fields.append(("Package details", "Additional package metadata omitted"))
        if fields:
            self.api._print_info_box("Board Information", fields)


# Compatibility name for the existing Ubuntu presentation probes.
from main.core.upload_log import UploadLog as UbuntuUploadLog
