"""Bounded reader of exact board names for explicit Arduino CLI permissions."""
from __future__ import annotations

import os
import re
from pathlib import Path

_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,159})\.name\s*=\s*(.+)$")
_MAX_BOARDS = 4096
_MAX_ENTRIES = 32768
_MAX_DIRECTORIES = 2048
_MAX_BYTES = 16 * 1024 * 1024


def read_board_choices(folder, cancel=None):
    """Read names/IDs only, off the GUI thread, without following package links."""
    root = Path(folder)
    if not root.is_dir():
        raise ValueError('Download and extract this version before choosing Arduino CLI boards.')
    if root.is_symlink() or (hasattr(root, 'is_junction') and root.is_junction()):
        raise ValueError('Choose an extracted board package without folder links.')
    pending, boards = [root], {}
    directories = entries = total = files = 0
    while pending:
        if cancel is not None and cancel.is_set():
            return []
        directory = pending.pop()
        directories += 1
        if directories > _MAX_DIRECTORIES:
            raise ValueError('This board package has too many folders to list safely.')
        with os.scandir(directory) as contents:
            for entry in contents:
                entries += 1
                if entries > _MAX_ENTRIES:
                    raise ValueError('This board package has too many files to list safely.')
                if cancel is not None and cancel.is_set():
                    return []
                if entry.is_symlink() or (hasattr(os.path, 'isjunction') and os.path.isjunction(entry.path)):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.name == 'boards.txt' and entry.is_file(follow_symlinks=False):
                    files += 1
                    if files > 128:
                        raise ValueError('This board package has too many board declarations.')
                    with open(entry.path, 'rb') as stream:
                        raw = stream.read(_MAX_BYTES - total + 1)
                    total += len(raw)
                    if total > _MAX_BYTES:
                        raise ValueError('The board declarations are too large to list safely.')
                    for line in raw.decode('utf-8-sig', errors='replace').splitlines():
                        match = _NAME.fullmatch(line.strip())
                        if match:
                            identifier, name = match.groups()
                            boards[identifier] = name[:512]
                            if len(boards) > _MAX_BOARDS:
                                raise ValueError('This board package has too many boards to list safely.')
    if not boards:
        raise ValueError('No exact board IDs were found. Download and extract this version first.')
    return sorted(boards.items(), key=lambda pair: (pair[1].casefold(), pair[0]))
