"""Native 64-bit Ubuntu CPU and executable identities (standard library only).

Keep this separate from Windows runtime selection. Architecture names from Linux,
Debian and upstream download assets differ; normalize only at those boundaries.
"""
from __future__ import annotations

import platform
from pathlib import Path


def host_architecture(machine: str | None = None) -> str:
    """Return the upstream asset name, rejecting 32-bit and unknown hosts."""
    value = (platform.machine() if machine is None else machine).strip().casefold()
    architecture = {"x86_64": "amd64", "amd64": "amd64",
                    "aarch64": "arm64", "arm64": "arm64"}.get(value)
    if architecture is None:
        raise RuntimeError(
            f"Unsupported Ubuntu architecture: {value or 'unknown'}. "
            "MCU Flasher requires 64-bit Intel/AMD (amd64) or ARM64 (aarch64); "
            "install 64-bit Ubuntu on Raspberry Pi."
        )
    return architecture


def linux_tool_architecture(machine: str | None = None) -> str:
    return "linux-" + host_architecture(machine)


def elf_matches_host(path: str | Path, machine: str | None = None) -> bool:
    """Inspect ELF64 little-endian CPU identity without executing the file."""
    try:
        expected = {"amd64": 62, "arm64": 183}[host_architecture(machine)]
        with Path(path).open("rb") as stream:
            header = stream.read(20)
        return (len(header) == 20 and header[:6] == b"\x7fELF\x02\x01"
                and int.from_bytes(header[18:20], "little") == expected)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
