#!/usr/bin/env python3
"""Build this host's native Rust executable. Writes only inside LITE-MODE."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", help="Optional Rust cross target (requires a configured linker)")
    args = parser.parse_args()
    command = ["cargo", "build", "--locked", "--release", "-j", "4", "--manifest-path", str(ROOT / "Cargo.toml")]
    if args.target:
        command.extend(["--target", args.target])
    subprocess.run(command, cwd=ROOT, check=True)
    windows = "windows" in args.target if args.target else sys.platform == "win32"
    target = ROOT / "target"
    if args.target:
        target /= args.target
    source = target / "release" / ("mcu-flasher-lite.exe" if windows else "mcu-flasher-lite")
    name = "WINDOWS.exe" if windows else ("UBUNTU_ARM64" if (args.target or os.uname().machine).startswith(("aarch64", "arm64")) else "UBUNTU")
    destination = ROOT / name
    shutil.copy2(source, destination)
    if not windows:
        destination.chmod(destination.stat().st_mode | 0o111)
    print(f"Built {destination}")


if __name__ == "__main__":
    main()
