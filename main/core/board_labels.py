"""One picker row per build target, and the names saved for it over time.

The same board is often described more than once: MCU Flasher's own download and
Arduino IDE's copy of one core, two versions of a core, and PlatformIO's built-in
definition. PlatformIO builds all of them identically, so they are one row. Arduino
CLI targets stay separate because there the copy is the compiler.
"""
from __future__ import annotations

import re
from collections import Counter

_VERSION = re.compile(r"\d+(?:\.\d+)+")
_SUFFIX = re.compile(r"^(?P<name>.*\S)\s+(?:\[(?P<square>[^\[\]]+)\]|\((?P<round>[^()]+)\))$")


def _text(info, key) -> str:
    return str(info.get(key) or "").strip()


def board_target(name: str, info: dict) -> tuple:
    """What choosing this row builds. Equal targets are one board."""
    platform, board = _text(info, "platform").lower(), _text(info, "board").lower()
    if info.get("backend") == "arduino-cli":
        return ("arduino-cli", _text(info, "arduino_fqbn").lower() or board,
                _text(info, "arduino_source_file"), _text(info, "arduino_source_sha256"))
    if platform and board:
        return ("platformio", platform, board)
    # No exact definition yet. Only identical declarations are the same row.
    source = _text(info, "arduino_source_sha256") or _text(info, "arduino_source_file")
    return ("declared", platform, _text(info, "arduino_board_id").lower() or name.casefold(), source)


def declared_label(name: str, info: dict) -> str:
    """The board's own name, without a suffix this application added to tell rows apart."""
    label = _text(info, "label")
    if label:
        return label
    match = _SUFFIX.match(name)
    if not match:
        return name
    identifier = _text(info, "arduino_board_id")
    board, platform = _text(info, "board"), _text(info, "platform")
    square, rounded = match["square"], match["round"]
    generated = (square is not None and (
        square == f"{platform}:{board}" or square == board or "@" in square
        or (identifier and (square == identifier or square.startswith(identifier + ", ")))
    )) or (rounded is not None and board and rounded == board)
    return match["name"] if generated else name


def _rank(name: str, info: dict) -> tuple:
    """Which copy names the row: MCU Flasher's download, Arduino IDE's, then PlatformIO's."""
    source = _text(info, "arduino_source_file")
    tier = 2 if not source else 1 if "arduino15" in source.replace("\\", "/").lower() else 0
    versions = _VERSION.findall(_text(info, "source_core"))
    newest = tuple(-int(part) for part in versions[-1].split(".")) if versions else ()
    return (tier, newest, len(name), name.casefold(), name)


def _suffixes(info: dict):
    """Labels for different boards that share one name, shortest first."""
    platform, board = _text(info, "platform"), _text(info, "board")
    identifier, core = _text(info, "arduino_board_id"), _text(info, "source_core")
    if info.get("backend") == "arduino-cli":
        proof = info.get("arduino_source_proof")
        if isinstance(proof, dict) and proof.get("core") and proof.get("version"):
            yield f"{proof['core']}@{proof['version']}"
        yield f"{identifier}, {core}"
    elif board:
        yield board
        yield f"{platform}:{board}"
    else:
        yield f"{identifier or platform}, {core}" if core else identifier or platform


def merge_board_rows(boards: dict, *, keep=lambda name, info: False) -> dict:
    """Collapse rows with one build target and give every remaining row its own label.

    ``keep`` names rows whose current label must not change (an Arduino CLI target's
    label is part of its preparation record).
    """
    groups: dict[tuple, list] = {}
    for name, info in boards.items():
        if isinstance(info, dict):
            groups.setdefault(board_target(name, info), []).append((name, info))
    chosen, fixed = [], set()
    for rows in groups.values():
        name, info = min(rows, key=lambda row: _rank(*row))
        if info.get("backend") == "arduino-cli" or keep(name, info):
            chosen.append((name, info, True))
            fixed.add(name.casefold())
        else:
            chosen.append((declared_label(name, info), info, False))
    shared = Counter(label.casefold() for label, _info, is_fixed in chosen if not is_fixed)
    used, result = set(fixed), {}
    for label, info, is_fixed in chosen:
        if is_fixed:
            result[label] = info
            continue
        final = label
        if shared[label.casefold()] > 1 or label.casefold() in fixed:
            options = [f"{label} [{suffix}]" for suffix in _suffixes(info)]
            final = next((option for option in options if option.casefold() not in used), "")
            number = 2
            while not final:
                candidate = f"{options[-1] if options else label} ({number})"
                final = candidate if candidate.casefold() not in used else ""
                number += 1
        used.add(final.casefold())
        result[final] = dict(info, label=label)
    return result


def resolve_label(label: str, boards: dict) -> str:
    """The current row for a label saved earlier, or "" when that is not certain.

    Rows have been renamed over time: exact copies merged into one row, Lite dropped
    its ``[platform:id]`` suffix, and downloading an Arduino core renames PlatformIO's
    built-in entry. A saved board must not be lost to any of those.
    """
    label = str(label or "").strip()
    if not label:
        return ""
    if label in boards:
        return label
    folded = label.casefold()
    match = _SUFFIX.match(label)
    base = match["name"].casefold() if match else folded
    tag = (match["square"] or match["round"] or "").strip() if match else ""
    exact, same_name, same_id, same_native = [], [], [], []
    for name, info in boards.items():
        if name.casefold() == folded:
            return name
        platform, board = _text(info, "platform"), _text(info, "board")
        declared, native = declared_label(name, info).casefold(), _text(info, "pio_name").casefold()
        if tag and tag == f"{platform}:{board}":
            exact.append(name)
        if declared == folded:
            same_name.append(name)
        elif native == folded:
            same_native.append(name)
        # A saved suffix says which of several boards it was: it must fit this row too.
        if tag and base in (declared, native):
            identifiers = {board.casefold(), _text(info, "arduino_board_id").casefold()} - {""}
            if tag.split(",", 1)[0].strip().casefold() in identifiers:
                same_id.append(name)
            elif "@" in tag:
                (same_name if base == declared else same_native).append(name)
    for matches in (exact, same_id, same_name, same_native):
        if len(matches) == 1:
            return matches[0]
        if matches:
            return ""  # Several different boards fit; never guess a target.
    return ""


__all__ = ["board_target", "declared_label", "merge_board_rows", "resolve_label"]
