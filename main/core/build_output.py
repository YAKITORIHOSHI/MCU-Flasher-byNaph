"""Conservative, toolkit-independent presentation of retained build events.

Activity condenses only recognizable application progress and decoration. The
caller retains original entries for Details/copy and owns bounded histories.
Diagnostics and unrecognized output remain visible without changing their text.
"""
from __future__ import annotations

import re
from typing import Any


_UNIT = re.compile(r"^⚙\s+Compiling\s+(?P<name>[^\r\n]+?)\.\.\.$")
# PlatformIO's object-action record has a specific build path. Do not treat an
# arbitrary sentence containing 'Compiling' or ending in '.o' as progress.
_RAW_UNIT = re.compile(r"^Compiling\s+(?P<name>\.pio[\\/]build[\\/][^\r\n]+\.(?:o|obj))$")
_BUILD_HEADING = re.compile(r"^⚙\s+COMPILING\s*\((?P<tool>[^()\r\n]+)\)$")
_WORKERS = re.compile(r"^⚡\s+Running Parallel Compilation on (?P<count>[1-9]\d*) Logical Processors$")
_DIVIDER = re.compile(r"^(?:={3,}|═{3,}|─{3,}|━{3,}|┄{3,}|┈{3,}|-{3,})$")
_BOX_BORDER = re.compile(r"^(?:╔═+╗|╠═+╣|╚═+╝|┌─+┐|├─+┤|└─+┘|╭─+╮|╰─+╯)$")
_BOX_ROW = re.compile(r"^(?:║(?P<double>[^\r\n]*)║|│(?P<light>[^\r\n]*)│)$")
_DECORATION_TAGS = frozenset(("header", "purple_header", "purple_dim"))
_DIAGNOSTIC_TAGS = frozenset(("error", "warning", "severe_alert"))


class BuildOutputPresenter:
    """Project one console event into Activity without mutating its raw entry.

    ``replace_key`` identifies only a contiguous compilation run. Any visible
    noncompile entry ends that run, so later progress cannot replace a summary
    preceding a warning, note, source snippet, or another operation.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._generation = 0
        self._compile_count = 0
        self._compile_key: str | None = None
        self._boxed_values = False

    def _end_compile_run(self) -> None:
        self._compile_count = 0
        self._compile_key = None

    def present(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        record = dict(entry)
        text = str(record.get("text", ""))
        plain = text.strip()
        tag = record.get("tag", "normal")

        # Severity is authoritative. A warning containing a progress-looking
        # sentence or a box made of punctuation must never disappear.
        if tag in _DIAGNOSTIC_TAGS:
            self._end_compile_run()
            return record

        unit = _UNIT.fullmatch(plain) if tag == "info" else None
        if unit is None and tag in ("normal", "info", "dim"):
            unit = _RAW_UNIT.fullmatch(plain)
        if unit is not None:
            if self._compile_key is None:
                self._generation += 1
                self._compile_key = f"compile-run-{self._generation}"
            self._compile_count += 1
            record.update(
                text=f"Compiling source units · {self._compile_count} processed · latest {unit['name']}",
                tag="system",
                newline=record.get("newline", True),
                replace_key=self._compile_key,
            )
            return record

        if tag in _DECORATION_TAGS:
            if _DIVIDER.fullmatch(plain):
                return None
            if _BOX_BORDER.fullmatch(plain):
                if plain[0] in "╔┌╭╚└╰":
                    self._boxed_values = False
                return None
            # Only explicitly styled decorative blanks are omitted. Ordinary
            # empty records can carry separation within diagnostic output.
            if not plain:
                return None

        row = _BOX_ROW.fullmatch(plain) if tag in ("header", "purple_header") else None
        body = ""
        if row:
            body = (row['double'] if row['double'] is not None else row['light']).strip()
            if not body:
                return None

        self._end_compile_run()
        if tag in ("header", "purple_header"):
            heading_text = body if row else plain
            heading = _BUILD_HEADING.fullmatch(heading_text)
            if heading:
                record.update(text=f"Build · {heading['tool']}", tag="header")
                return record
            workers = _WORKERS.fullmatch(heading_text)
            if workers:
                record.update(text=f"Compiler workers: {workers['count']}", tag="normal")
                return record

        if row:
            if ':' in body:
                self._boxed_values = True
            record.update(text=body, tag="normal" if self._boxed_values else "header")
            return record

        return record


__all__ = ["BuildOutputPresenter"]
