"""Passive, bounded prompt grouping for integrated assistant terminals."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4


def assistant_process_active(pid):
    """Identify a running assistant by its executable; never inspect arguments."""
    try:
        import psutil
        process = psutil.Process(int(pid))
        names = {Path(child.name()).stem.casefold() for child in [process, *process.children(recursive=True)]}
        return bool(names & {"codex", "agy", "opencode", "claude", "gemini"})
    except Exception:
        return False


class PromptInputTracker:
    def __init__(self, project):
        self.project = Path(project)
        self.line = ""
        self.escape = ""
        self.paste = False
        self.uncertain = False

    def feed(self, data, *, active=True):
        # Observe input without ever changing what reaches the PTY.
        for char in str(data or ""):
            if self.escape:
                self.escape += char
                if len(self.escape) == 2 and char != "[":
                    self.escape = ""
                    self.uncertain = True
                elif len(self.escape) > 2 and "@" <= char <= "~":
                    if self.escape == "\x1b[200~":
                        self.paste = True
                    elif self.escape == "\x1b[201~":
                        self.paste = False
                    elif not re.fullmatch(r"\x1b\[\??[\d;]*[Rcn]", self.escape):
                        self.uncertain = True
                    self.escape = ""
                if len(self.escape) > 128:
                    self.escape = ""
                continue
            if char == "\x1b":
                self.escape = char
            elif char in "\r\n":
                if self.paste:
                    self.line = (self.line + "\n")[-4000:]
                else:
                    prompt = self.line.strip()
                    if active and len(prompt) > 3 and not prompt.startswith("/") and prompt.casefold() not in {"yes", "no", "exit"}:
                        self._publish("Assistant prompt (title unavailable)" if self.uncertain else prompt)
                    self.line = ""
                    self.uncertain = False
            elif char in "\x08\x7f":
                self.line = self.line[:-1]
            elif char in "\x03\x15":
                self.line = ""
                self.uncertain = False
            elif char.isprintable():
                self.line = (self.line + char)[-4000:]

    def _publish(self, prompt):
        cache = self.project / ".mcu_flasher_build_cache"
        # Only the actual selected sketch's app-owned container is writable.
        if not cache.is_dir():
            return
        target = cache / "ai_prompt.json"
        temp = cache / f".ai-prompt-{os.getpid()}-{uuid4().hex}.tmp"
        try:
            payload = {"id": uuid4().hex, "prompt": prompt[:500], "time": time.time(),
                       "source": "cli"}
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            from main.core.file_utils import ensure_file_writable
            ensure_file_writable(target)
            os.replace(temp, target)
        except OSError:
            pass
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
