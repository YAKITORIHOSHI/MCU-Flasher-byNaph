"""Read-only native compiler checks for the explicitly prepared ESP32 fixture.

This is verifier support, not a board catalog or an installer. The real Compile
button must still build the declared esp32dev/Arduino fixture afterwards. Package
names changed with Espressif32 8; a missing legacy path is not foreign CPU proof.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.modules.ubuntu_architecture import elf_matches_host, host_architecture


LAYOUTS = (
    ("toolchain-xtensa-esp32", "xtensa-esp32-elf-g++"),
    ("toolchain-xtensa-esp-elf", "xtensa-esp-elf-g++"),
)


def _metadata(path: Path) -> dict:
    with path.open("rb") as stream:
        data = stream.read(512 * 1024 + 1)
    if len(data) > 512 * 1024:
        raise RuntimeError(f"Fixture package metadata exceeds its byte limit: {path}")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise RuntimeError(f"Fixture package metadata must be an object: {path}")
    return value


def native_esp32_compilers(core: str | Path, machine: str | None = None) -> tuple[Path, ...]:
    """Validate every installed, declared known ESP32 C++ compiler without execution.

    Caller supplies its owned fixture store. Neither absent/undeclared packages
    nor another package's filename can certify a compiler. Multiple installed
    generations are all checked; an old native package cannot mask a foreign
    new one. Unknown future layouts fail closed for explicit fixture review.
    """
    root = Path(core).resolve(strict=True)
    architecture = host_architecture(machine)
    platform = root / "platforms/espressif32"
    if not platform.resolve(strict=True).is_relative_to(root / "platforms"):
        raise RuntimeError("The ESP32 fixture platform escaped its store")
    if not (platform / "boards/esp32dev.json").is_file():
        raise RuntimeError("The prepared ESP32 fixture board definition is absent")
    if not (platform / "platform.json").resolve(strict=True).is_relative_to(platform.resolve()):
        raise RuntimeError("The ESP32 fixture platform metadata escaped its package")
    declarations = _metadata(platform / "platform.json").get("packages")
    if not isinstance(declarations, dict):
        raise RuntimeError("The ESP32 fixture platform has no package declarations")
    compilers = []
    for name, executable in LAYOUTS:
        if name not in declarations:
            continue
        declaration = declarations[name]
        if not isinstance(declaration, dict) or declaration.get("type") != "toolchain":
            raise RuntimeError(f"Fixture compiler declaration is not a toolchain: {name}")
        package = root / "packages" / name
        manifest = package / "package.json"
        if not manifest.is_file():
            continue
        resolved = package.resolve(strict=True)
        if not resolved.is_relative_to(root / "packages"):
            raise RuntimeError(f"Fixture compiler package escaped its store: {name}")
        if not manifest.resolve(strict=True).is_relative_to(resolved):
            raise RuntimeError(f"Fixture compiler metadata escaped its package: {name}")
        if _metadata(manifest).get("name") != name:
            raise RuntimeError(f"Fixture compiler package identity does not match {name}")
        compiler = (package / "bin" / executable).resolve(strict=True)
        if not compiler.is_relative_to(resolved):
            raise RuntimeError(f"Fixture compiler escaped its declared package: {name}")
        if not elf_matches_host(compiler, machine):
            raise RuntimeError(f"Fixture compiler is not native ELF64 for {architecture}: {compiler}")
        compilers.append(compiler)
    if not compilers:
        raise RuntimeError("No installed declared ESP32 fixture compiler layout is available; no packages will be installed")
    return tuple(compilers)
