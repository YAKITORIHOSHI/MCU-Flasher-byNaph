"""Passive, bounded prompt grouping for integrated assistant terminals."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4


# Executable names of the coding agents, as main.core.ai_agents names them.
AGENT_PROCESSES = {"codex": "codex", "agy": "antigravity", "opencode": "opencode", "claude": "claude", "hermes": "hermes"}
# Commands that leave the conversation for a new or an earlier one: what the agent
# edits afterwards belongs to another session.
SESSION_COMMANDS = frozenset({"/new", "/clear", "/reset", "/resume"})


def assistant_process(pid):
    """(agent, identity) of the coding agent running in a terminal, or (None, None).

    Found by executable name; arguments are never inspected. The identity (process ID
    and start time) tells one run of an agent from the next. Hermes runs as Python, so
    a terminal cannot name it.
    """
    try:
        import psutil
        if int(pid) <= 0:       # Process 0's descendants are every process on the computer.
            return None, None
        process = psutil.Process(int(pid))
        for child in [process, *process.children(recursive=True)]:
            agent = AGENT_PROCESSES.get(Path(child.name()).stem.casefold())
            if agent:
                return agent, (child.pid, child.create_time())
    except Exception:
        pass
    return None, None


# What a Windows assistant's cmd.exe loop also runs between two runs of its agent.
_LOOP_HELPERS = frozenset({"timeout", "conhost", "openconsole"})


def run_identity(pid):
    """(process ID, start time) of the program a shell runs now, or None.

    The Windows assistant runs its agent in a cmd.exe loop that starts it again after
    it ends (/exit, Ctrl+C, a crash): another identity is another run of the agent,
    whatever its executable is called.
    """
    try:
        import psutil
        if int(pid) <= 0:
            return None
        children = [child for child in psutil.Process(int(pid)).children()
                    if Path(child.name()).stem.casefold() not in _LOOP_HELPERS]
        if children:
            child = min(children, key=lambda child: child.create_time())
            return child.pid, child.create_time()
    except Exception:
        pass
    return None


def assistant_process_agent(pid):
    """The coding agent running in a terminal, by its executable name, or None."""
    return assistant_process(pid)[0]


def assistant_process_active(pid):
    """Identify a running assistant by its executable; never inspect arguments."""
    return assistant_process_agent(pid) is not None


class PromptInputTracker:
    def __init__(self, project, *, source="cli", agent=None):
        if source not in {"cli", "assistant"}:
            raise ValueError("Prompt source must be cli or assistant")
        self.project = Path(project)
        self.source = source
        # Which coding agent received the prompt ("claude", ...), when known.
        self.agent = agent if agent in AGENT_PROCESSES.values() else None
        self.line = ""
        self.escape = ""
        self.paste = False
        self.uncertain = False
        self._process = None
        # One tracker watches one run of an agent: its prompts form one session.
        self.new_session()

    def new_session(self):
        """What the agent edits from now on belongs to a new session in AI Changes."""
        self.session = uuid4().hex
        self.session_started = time.time()

    def follow(self, agent, identity=None):
        """A terminal names the agent it runs now; another run of one is another session."""
        self.agent = agent if agent in AGENT_PROCESSES.values() else None
        if identity != self._process:
            self._process = identity
            self.new_session()

    def follow_run(self, identity):
        """The same agent ended and started again (a known ``identity`` changed): a new session."""
        if identity is None:
            return
        if self._process is not None and identity != self._process:
            self.new_session()
        self._process = identity

    def reset(self):
        """Forget the line being typed, as after observation overflowed; the session stays."""
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
                    command = prompt.split(maxsplit=1)[0].casefold() if prompt.startswith("/") else ""
                    if active and command in SESSION_COMMANDS and not self.uncertain:
                        self.new_session()
                    elif active and len(prompt) > 3 and not prompt.startswith("/") and prompt.casefold() not in {"yes", "no", "exit"}:
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
                       "source": self.source, "sourceVersion": 1,
                       "session": self.session, "sessionStarted": self.session_started}
            if self.agent in AGENT_PROCESSES.values():
                payload["agent"] = self.agent
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
