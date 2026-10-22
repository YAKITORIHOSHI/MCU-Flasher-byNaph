"""Isolated, settings-free SCons child for the pinned development AVR entry.

Native runtime admission must cover both this file and its fixed sibling.
This entry grants no real-compiler or package-readiness authority.
"""

import sys

for stream in (sys.stdout, sys.stderr):
    if stream is not None and hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
    print("Tool entry requires the admitted private Python with -I -S -B.", file=sys.stderr)
    raise SystemExit(2)

from pathlib import Path
import runpy


if __name__ == "__main__":
    boundary = runpy.run_path(str(Path(__file__).with_name("platformio_compile.py")),
                             run_name="__mcu_compile_boundary__")
    raise SystemExit(boundary["scons_main"](sys.argv[1:]))
