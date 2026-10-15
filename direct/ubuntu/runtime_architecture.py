"""Detect a private Ubuntu environment copied from a different CPU architecture."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from src.modules.ubuntu_architecture import elf_matches_host, host_architecture

RECEIPT_NAME = "_mcu_ubuntu_runtime.json"
OWNER = "mcu-flasher-ubuntu-runtime"
MAX_RECEIPT_BYTES = 8192


def _read_receipt(path: Path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECEIPT_BYTES + 1)
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError("Ubuntu runtime architecture record exceeds its size limit")
    return json.loads(raw)


def private_site_packages(directory: Path) -> Path:
    return Path(directory) / f"lib/python{sys.version_info[0]}.{sys.version_info[1]}/site-packages"


def needs_architecture_repair(directory: Path, architecture: str | None = None) -> bool:
    """Inspect bytes before pip can accept incompatible installed wheels as satisfied."""
    architecture = architecture or host_architecture()
    site = private_site_packages(directory)
    receipt = site / RECEIPT_NAME
    if receipt.is_symlink():
        raise RuntimeError("Ubuntu runtime architecture record must be a local file.")
    if receipt.exists():
        try:
            data = _read_receipt(receipt)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError("Ubuntu runtime architecture record is damaged. "
                               "Move the owned .venv-linux aside and rerun Bootstrap.") from exc
        if not isinstance(data, dict) or data.get("owner") != OWNER:
            raise RuntimeError("Ubuntu runtime architecture record is unrecognized; it was preserved.")
        if data.get("architecture") != architecture:
            return True
    if site.is_dir():
        # Native extension and bundled Qt/tool libraries need the host's ELF64 ABI.
        # Bootstrap alone performs this scan; startup only reads the small receipt.
        for path in site.rglob("*.so*"):
            if architecture == "arm64" and path.relative_to(site).parts[0] == "PyQt5":
                # ARM64's narrow finder always selects Ubuntu's Qt5 package.
                # Preserve an unused wheel copied from amd64 without reinstall loops.
                continue
            if path.is_file() and not elf_matches_host(path, architecture):
                return True
    return False


def check_runtime_architecture(directory: Path, architecture: str | None = None) -> None:
    architecture = architecture or host_architecture()
    receipt = private_site_packages(directory) / RECEIPT_NAME
    if receipt.is_symlink():
        raise RuntimeError("Ubuntu runtime architecture record must be a local file.")
    if not receipt.exists():
        return  # Existing amd64 installations are validated by their actual Qt ELF.
    try:
        data = _read_receipt(receipt)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError("Ubuntu runtime architecture record is damaged. Rerun Ubuntu Bootstrap.") from exc
    if (not isinstance(data, dict) or data.get("owner") != OWNER
            or data.get("architecture") != architecture):
        raise RuntimeError("Ubuntu's private runtime belongs to another CPU architecture. "
                           "Run bash direct/ubuntu/run.sh --repair on this computer.")


def record_runtime_architecture(directory: Path, architecture: str | None = None) -> None:
    site = private_site_packages(directory)
    site.mkdir(parents=True, exist_ok=True)
    receipt = site / RECEIPT_NAME
    temporary = receipt.with_suffix(".new")
    if receipt.is_symlink() or temporary.is_symlink():
        raise RuntimeError("Ubuntu runtime architecture record must be a local file.")
    payload = json.dumps({"owner": OWNER, "architecture": architecture or host_architecture()},
                         sort_keys=True) + "\n"
    if receipt.is_file() and receipt.stat().st_size == len(payload):
        with receipt.open("rb") as stream:
            if stream.read(MAX_RECEIPT_BYTES + 1) == payload.encode("utf-8"):
                return
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, receipt)
    finally:
        temporary.unlink(missing_ok=True)
