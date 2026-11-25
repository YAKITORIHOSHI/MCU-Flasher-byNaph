"""Take one AI edit back out of a sketch, from this session or a saved one.

The saved sessions are the dated folders the app keeps hidden inside the sketch
(``.mcu_flasher_build_cache/.mcu_ai_edits``). They are only read here: what a revert
changes is recorded by AIReviewManager, like every other decision.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import os
from pathlib import Path
import re

_MAX_RECORD = 24 * 1024 * 1024
_MAX_MIDDLE = 6000          # Lines between the common head and tail worth matching
_MAX_EDITS = 400
_HEADER_BYTES = 4096
_EDIT_NAME = re.compile(r"edit(\d{1,6})\.txt")
_SESSION = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{2})/session(\d{1,6})")
_BEFORE = "\n<<< BEFORE CONTENT >>>\n"
_AFTER = "\n<<< END BEFORE CONTENT >>>\n\n<<< AFTER / AI CONTENT >>>\n"
_APPLIED = "\n<<< END AFTER / AI CONTENT >>>\n\n<<< CURRENT APPLIED CONTENT >>>\n"


def _newlines_like(text: str, sample: str) -> str:
    """Give a recorded text the line endings the file uses now."""
    windows = "\r\n" in sample
    if windows == ("\r\n" in text) or "\n" not in sample or "\n" not in text:
        return text
    plain = text.replace("\r\n", "\n")
    return plain.replace("\n", "\r\n") if windows else plain


def _opcodes(first: list, second: list):
    """SequenceMatcher opcodes with the common head and tail trimmed first, or None when too large."""
    head, limit = 0, min(len(first), len(second))
    while head < limit and first[head] == second[head]:
        head += 1
    end_first, end_second = len(first), len(second)
    while end_first > head and end_second > head and first[end_first - 1] == second[end_second - 1]:
        end_first -= 1
        end_second -= 1
    if end_first - head + end_second - head > _MAX_MIDDLE:
        return None
    middle = SequenceMatcher(None, first[head:end_first], second[head:end_second], autojunk=False).get_opcodes()
    return ([("equal", 0, head, 0, head)] if head else []) + [
        (tag, i1 + head, i2 + head, j1 + head, j2 + head) for tag, i1, i2, j1, j2 in middle] + (
        [("equal", end_first, len(first), end_second, len(second))] if end_first < len(first) else [])


def undo_change(current: str, before: str, after: str) -> tuple[str, int, int]:
    """(text, undone, blocked): the edit before→after taken out of the current text.

    Later changes elsewhere stay. A change whose lines were edited again since is
    blocked and left alone; the caller decides what to do about it.
    """
    before, after = _newlines_like(before, current), _newlines_like(after, current)
    base, ours, theirs = (text.splitlines(keepends=True) for text in (after, current, before))
    wanted = _opcodes(base, theirs)
    survived = _opcodes(base, ours)
    if wanted is None or survived is None:
        return (before, 1, 0) if current == after else (current, 0, 1)
    position = {}
    for tag, i1, i2, j1, _j2 in survived:
        if tag == "equal":
            position.update((index, j1 + index - i1) for index in range(i1, i2))
    result, undone, blocked = list(ours), 0, 0
    for tag, i1, i2, j1, j2 in reversed(wanted):
        if tag == "equal":
            continue
        original = theirs[j1:j2]
        previous = position.get(i1 - 1) if i1 else -1
        following = position.get(i2) if i2 < len(base) else len(ours)
        places = [position.get(index) for index in range(i1, i2)]
        if None not in places and (not places or places == list(range(places[0], places[0] + len(places)))) and (
                places or previous is not None and following == previous + 1):
            start = places[0] if places else following
            result[start:start + len(places)] = original
        elif previous is None or following is None or ours[previous + 1:following] != original:
            blocked += 1
            continue
        undone += 1
    return "".join(result), undone, blocked


def parse_edit_record(text: str) -> dict:
    """The fields and both complete texts of one saved ``editN.txt``."""
    head, found, rest = text.partition(_BEFORE)
    before, found_after, rest = rest.partition(_AFTER)
    after, found_applied, _rest = rest.partition(_APPLIED)
    if not (found and found_after and found_applied):
        raise ValueError("This saved AI edit is incomplete.")
    return dict(parse_edit_header(head), beforeContent=before, content=after)


def parse_edit_header(text: str) -> dict:
    fields = {}
    for line in text.splitlines():
        name, separator, value = line.partition(": ")
        if separator and re.fullmatch(r"[A-Z][A-Za-z-]+", name):
            fields.setdefault(name, value.strip())
    return {"reviewId": fields.get("Review-ID", ""), "path": fields.get("File", ""), "project": fields.get("Project", ""),
            "status": fields.get("Status", "pending"), "timestamp": fields.get("Created", ""),
            "updatedAt": fields.get("Updated", ""), "beforeExists": fields.get("Before-Exists", "True") != "False",
            "afterExists": fields.get("After-Exists", "True") != "False"}


def project_path(record: dict, project: Path) -> Path | None:
    """Where this record's file is now, also after the sketch folder moved or was copied."""
    path, recorded = str(record.get("path") or ""), str(record.get("project") or "")
    if not path:
        return None
    # A record made on another system keeps that system's separators.
    parts = [part for part in re.split(r"[\\/]+", path)]
    root = [part for part in re.split(r"[\\/]+", recorded)] if recorded else []
    fold = str.casefold
    if root and [fold(part) for part in parts[:len(root)]] == [fold(part) for part in root]:
        relative = parts[len(root):]
    else:
        try:
            relative = list(Path(path).relative_to(project).parts)
        except ValueError:
            return None
    if not relative or any(part in ("", ".", "..") or ":" in part for part in relative):
        return None
    return project.joinpath(*relative)


def edit_file(root, edit_id: str) -> Path:
    """The saved record an archive id names; nothing outside the history folder."""
    relative = edit_id.removeprefix("archive:")
    folder, _, name = relative.rpartition("/")
    if not _SESSION.fullmatch(folder) or not _EDIT_NAME.fullmatch(name):
        raise ValueError("This is not a saved AI edit.")
    path = Path(root) / folder / name
    if path.is_symlink() or path.resolve().parent != (Path(root) / folder).resolve():
        raise ValueError("This is not a saved AI edit.")
    return path


def read_edit(root, edit_id: str) -> dict:
    with edit_file(root, edit_id).open("r", encoding="utf-8", errors="replace", newline="") as stream:
        text = stream.read(_MAX_RECORD + 1)
    if len(text) > _MAX_RECORD:
        raise ValueError("This saved AI edit is too large to open.")
    return dict(parse_edit_record(text), id=edit_id)


def list_sessions(root, *, skip=None, limit: int = _MAX_EDITS) -> list[dict]:
    """Saved sessions, newest first, from their folder names and record headers only."""
    root, sessions, remaining = Path(root), [], limit
    try:
        days = [entry for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    found = []
    for day in days:
        try:
            for entry in os.scandir(day.path):
                match = _SESSION.fullmatch(f"{day.name}/{entry.name}")
                if match and entry.is_dir(follow_symlinks=False) and Path(entry.path) != skip:
                    month, date, year, number = (int(value) for value in match.groups())
                    found.append(((2000 + year, month, date, number), f"{day.name}/{entry.name}", Path(entry.path)))
        except OSError:
            continue
    for key, name, folder in sorted(found, reverse=True):
        if remaining <= 0:
            break
        edits = []
        try:
            files = sorted((int(_EDIT_NAME.fullmatch(item.name)[1]), item) for item in folder.iterdir()
                           if _EDIT_NAME.fullmatch(item.name) and item.is_file() and not item.is_symlink())
        except OSError:
            continue
        for number, path in reversed(files[-remaining:]):
            try:
                with path.open("r", encoding="utf-8", errors="replace", newline="") as stream:
                    header = stream.read(_HEADER_BYTES).partition(_BEFORE)[0]
            except OSError:
                continue
            edits.append(dict(parse_edit_header(header), id=f"archive:{name}/{path.name}", number=number))
        remaining -= len(edits)
        if edits:
            sessions.append({"id": name, "date": key[:3], "session": key[3], "edits": edits})
    return sessions
