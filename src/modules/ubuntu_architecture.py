"""Native 64-bit CPU and executable identities for Linux and macOS (standard library only).

Keep this separate from Windows runtime selection. Architecture names from Linux,
Debian, macOS and upstream download assets differ; normalize only at those boundaries.
"""
from __future__ import annotations

import platform
from pathlib import Path
import struct
import sys

_MACH_O_CPU = {"amd64": 0x01000007, "arm64": 0x0100000C}
# 64-bit thin Mach-O (little-endian) and the big-endian universal headers.
MACH_O_MAGICS = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf")


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


def native_tool_architecture(machine: str | None = None) -> str:
    """Receipt identity of a tool prepared for this operating system and CPU."""
    return ("darwin-" if sys.platform == "darwin" else "linux-") + host_architecture(machine)


def _mach_o_matches(path, machine) -> bool:
    """Accept a thin binary or a universal one with a slice for this CPU."""
    expected = _MACH_O_CPU[host_architecture(machine)]
    with Path(path).open("rb") as stream:
        header = stream.read(8)
        if len(header) != 8:
            return False
        if header[:4] == MACH_O_MAGICS[0]:
            return struct.unpack("<I", header[4:8])[0] == expected
        if header[:4] not in MACH_O_MAGICS[2:]:
            return False
        count = struct.unpack(">I", header[4:8])[0]
        entry = 32 if header[:4] == MACH_O_MAGICS[3] else 20
        # Java class files share this magic; their "count" is far above any real binary's.
        if not 0 < count <= 16:
            return False
        for _ in range(count):
            slice_header = stream.read(entry)
            if len(slice_header) != entry:
                return False
            if struct.unpack(">I", slice_header[:4])[0] == expected:
                return True
    return False


def elf_matches_host(path: str | Path, machine: str | None = None) -> bool:
    """Inspect the native binary's CPU identity without executing the file.

    Linux hosts require ELF64 little-endian and macOS hosts Mach-O, so a binary
    copied from the other operating system never counts as native.
    """
    try:
        if sys.platform == "darwin":
            return _mach_o_matches(path, machine)
        expected = {"amd64": 62, "arm64": 183}[host_architecture(machine)]
        with Path(path).open("rb") as stream:
            header = stream.read(20)
        return (len(header) == 20 and header[:6] == b"\x7fELF\x02\x01"
                and int.from_bytes(header[18:20], "little") == expected)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
