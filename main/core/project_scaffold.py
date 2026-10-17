"""Create-only sketch templates shared by the backend and project selector."""
from __future__ import annotations

import re
from pathlib import Path

from main.core.constants import is_application_codebase_dir


def sanitized_project_name(name: str, fallback: str = "NewSketch") -> str:
    """Use one safe root filename for the folder, sources and quoted includes."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name.strip()) or fallback


def create_project_scaffold(
    parent_dir: str,
    name: str,
    *,
    include_h: bool = False,
    include_cpp: bool = False,
    template_type: str = "standard",
) -> Path:
    """Write a new sketch folder, refusing any existing target directory."""
    clean_name = sanitized_project_name(name)
    target_dir = Path(parent_dir).resolve() / clean_name
    if is_application_codebase_dir(parent_dir) or is_application_codebase_dir(target_dir):
        raise ValueError("Cannot create sketch inside MCU Flasher application folder.")

    # This is the ownership gate: never adopt or overwrite an existing folder.
    target_dir.mkdir(parents=True, exist_ok=False)
    header_include = f'#include "{clean_name}.h"\n' if include_h else ""
    if template_type == "bare":
        body = (
            "void setup() {\n"
            "  \n"
            "}\n\n"
            "void loop() {\n"
            "  \n"
            "}\n"
        )
    elif template_type == "blink":
        body = (
            "void setup() {\n"
            "  pinMode(LED_BUILTIN, OUTPUT);\n"
            "}\n\n"
            "void loop() {\n"
            "  digitalWrite(LED_BUILTIN, HIGH);\n"
            "  delay(1000);\n"
            "  digitalWrite(LED_BUILTIN, LOW);\n"
            "  delay(1000);\n"
            "}\n"
        )
    else:
        body = "void setup() {\n\n}\n\nvoid loop() {\n\n}\n"

    files = {f"{clean_name}.ino": (header_include + "\n" if header_include else "") + body}
    if include_h:
        files[f"{clean_name}.h"] = "#pragma once\n"
    if include_cpp:
        files[f"{clean_name}.cpp"] = header_include
    for filename, content in files.items():
        with (target_dir / filename).open("x", encoding="utf-8") as stream:
            stream.write(content)
    return target_dir
