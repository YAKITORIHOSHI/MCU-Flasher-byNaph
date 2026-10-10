"""Small, bounded setup summaries shared by the native and Qt displays.

This module presents existing setup events; it never runs or changes installers.
The caller must continue sending the original events to its technical-details
view and durable log.  Package percentages retain the pipeline's meaning (pip
phase progress or a reported download percentage), not an estimated byte count.
"""
from collections import deque
from dataclasses import asdict, dataclass
import re

from src.modules.bootstrap_output import known_builder_notice


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_PERCENT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%")
_DIVIDER = re.compile(r"^[\s─━═┄┈=\-]+$")
_PREFIX = re.compile(r"^[\s▸▹►▶✔✓⚠✖✕×↑⬇⏳⚙⟳•–]+")
_BAR = re.compile(r"^[\s▰▱█░▓▒■□▮▯#=\[\]>|]+")
_PHASE = re.compile(
    r"^(?:(?P<label>.+?):\s*)?"
    r"(?P<phase>Downloading|Unpacking|Extracting|Installing|Preparing|Importing)"
    r"(?:\s+[—–-]\s*|\s+)?(?P<name>.*)$", re.I)
_RAW_PIP = re.compile(
    r"^(?:Collecting\b|Downloading\s+.*(?:\.whl|\.tar\.|\.zip|\.metadata)\b|"
    r"Using cached\b|Saved\s+(?:\.?[\\/]|[A-Za-z]:[\\/])|Requirement already satisfied\b|"
    r"Obtaining dependency information\b|Installing collected packages:|"
    r"Building wheels? for\b|Building wheel for\b|Created wheel for\b|"
    r"Stored in directory:|Preparing metadata\b|Getting requirements to build\b|"
    r"Installing build dependencies\b|INFO: pip\b|\[notice\]|"
    r"Running setup\.py\b|Attempting uninstall:|Found existing installation:|"
    r"Uninstalling\s+|Successfully uninstalled\b)", re.I)
_ERROR = re.compile(
    r"(?:^|:\s*)(?:ERROR\b|Error!|Traceback\b|Offline bootstrap failed:|"
    r"[\w.]*Error:|[\w.]*Exception:)", re.I)
_WARNING = re.compile(r"(?:^|:\s*)(?:WARNING\b|Warning!)", re.I)
_DEPENDENCY_ERROR = re.compile(
    r"(?:^|:\s*)(?:No matching distribution found\b|"
    r"Could not find a version that satisfies\b|ResolutionImpossible\b)", re.I)
_TAGS = frozenset(("normal", "dim", "section", "subsection", "ok", "warn", "fail", "update"))


@dataclass(frozen=True)
class PackageRow:
    """One compact package row, independent of a GUI toolkit."""

    name: str
    status: str
    percent: int | None = None
    tone: str = "normal"
    detail: str = ""


@dataclass(frozen=True)
class SummaryEvent:
    text: str
    tag: str = "normal"


def _plain(value, limit=8192):
    return _ANSI.sub("", str(value or "")[:limit]).replace("\r\n", "\n").replace("\r", "\n")


def _label(value, limit=240):
    return _PREFIX.sub("", str(value).strip()).strip()[:limit]


def _percent(value):
    matches = _PERCENT.findall(value)
    return max(0, min(100, round(float(matches[-1])))) if matches else None


def _tone(status):
    low = status.lower()
    if re.search(r"\b(?:failed|failure|offline|error)\b", low) and not re.search(r"\bretry|\brepair", low):
        return "fail"
    if re.search(r"\b(?:retrying|timed out|interrupted|warning)\b", low):
        return "warn"
    if re.match(r"(?:installed|verified|ready|complete|downloaded|unpacked)\b", low):
        return "ok"
    if re.match(r"(?:waiting|checking)\b", low):
        return "dim"
    return "normal"


def concise_status(raw, limit=180):
    """Shorten the visible status while the caller retains the raw tooltip."""
    text = " ".join(_plain(raw).split())
    limit = max(48, min(1000, int(limit)))
    match = re.fullmatch(
        r"Installing (?:(?P<count>\d+) )?Python dependencies(?: via pip)?"
        r"\s*\((?P<names>.+)\)(?:\.{3}|…)?", text, re.I)
    if match and len(text) > limit:
        count = int(match["count"]) if match["count"] else len([name for name in match["names"].split(",") if name.strip()])
        return f"Installing Python dependencies ({count} packages)…"
    if len(text) <= limit:
        return text
    # Keep the action and final context visible; full paths remain in details.
    tail = min(48, limit // 3)
    return text[:limit - tail - 3].rstrip() + " … " + text[-tail:].lstrip()


def parse_package_rows(block_type, text):
    """Parse the existing pip tables and PIO/download replace-in-place bars.

    The setup worker emits complete blocks, including percentages whose input
    stream was split across reads.  This parser does not consume pipe chunks.
    Unknown block types use the same safe single-row fallback.
    """
    raw = _plain(text, 65536)
    lines = [line.strip()[:2000] for line in raw.splitlines()[:384] if line.strip()]
    content = [line for line in lines if not _DIVIDER.fullmatch(line)]
    if not content:
        return ()
    rows = []
    if block_type == "pip":
        index = 0
        while index < len(content) and len(rows) < 96:
            header = content[index]
            if _BAR.match(header) and _percent(header) is not None:
                index += 1
                continue
            fields = re.split(r"\s{2,}", header, maxsplit=1)
            if len(fields) == 2:
                name, status = fields
                if name.casefold() == "package" and status.casefold() == "status":
                    index += 1
                    continue
            else:
                # Legacy fixtures and shorter external blocks may omit padding.
                marked = re.split(r"\s+(?=[✔✓✖⚠⬇⏳⚙⟳])", header, maxsplit=1)
                name, status = marked if len(marked) == 2 else (header, "Preparing")
            pct = _percent(header)
            detail = ""
            if index + 1 < len(content) and _BAR.match(content[index + 1]) and _percent(content[index + 1]) is not None:
                index += 1
                pct = _percent(content[index])
            if status == "Preparing" and pct is not None:
                name = _PERCENT.sub("", name).strip()
            if re.search(r"[▰▱█░▓▒■□▮▯]", status):
                status = _PERCENT.sub("", re.sub(r"[▰▱█░▓▒■□▮▯]+", "", status)).strip() or "Preparing"
            name, status = _label(name), _label(status)
            if name:
                rows.append(PackageRow(name, status or "Preparing", pct, _tone(status), detail))
            index += 1
        if rows:
            return tuple(rows)

    header, *details = content
    pct = _percent("\n".join(content))
    match = _PHASE.match(_label(header, 2000))
    if match:
        status = match["phase"].capitalize()
        name = match["name"].strip(" .…") or match["label"] or "Package preparation"
        name = _PERCENT.sub("", name).strip(" .…") or match["label"] or "Package preparation"
        if match["label"] and match["name"] and match["label"].strip().lower() != "platformio":
            # Keep a board/platform label as useful secondary context.
            label = match["label"].strip()
            details.insert(0, label)
    else:
        name = _PERCENT.sub("", _label(header, 2000)).strip(" .…") or "Package preparation"
        status = "Preparing"
    detail = " · ".join(_BAR.sub("", line).strip() for line in details)
    return (PackageRow(_label(name), status, pct, _tone(status), detail[:480]),)


def compact_package_text(rows):
    """Plain compact rows for native/text fallback surfaces."""
    return "\n".join(f"{row.name}  ·  {row.status}" +
                     (f"  ·  {row.percent}%" if row.percent is not None else "")
                     for row in rows)


class BootstrapPresentation:
    """Presentation-only state, bounded independently of the full run log.

    ``summaries`` returns only newly accepted events. ``events`` is the retained
    summary history. Feed original log text to technical details separately.
    Successes and known informational notices are deduplicated for one stage;
    warnings/errors always pass.
    Snapshots contain only this display state and can transfer across Tk/Qt.
    """

    MAX_EVENTS = 700
    MAX_CHARS = 128000
    MAX_ROWS = 96
    MAX_SUCCESS_KEYS = 512

    def __init__(self):
        self._events = deque()
        self._chars = 0
        self._successes = {}
        self._context = deque(maxlen=4)
        self._failure_remaining = 0
        self._rows = ()
        self.block_type = None

    @property
    def events(self):
        return tuple(self._events)

    @property
    def rows(self):
        return self._rows

    def _append(self, text, tag, accepted):
        text = text.strip()[:2000]
        if not text:
            return
        if tag == "ok" or (tag == "normal" and known_builder_notice(text)):
            key = " ".join(text.casefold().split())
            if key in self._successes:
                return
            self._successes[key] = None
            if len(self._successes) > self.MAX_SUCCESS_KEYS:
                del self._successes[next(iter(self._successes))]
        event = SummaryEvent(text, tag)
        self._events.append(event)
        self._chars += len(text)
        while self._events and (len(self._events) > self.MAX_EVENTS or self._chars > self.MAX_CHARS):
            self._chars -= len(self._events.popleft().text)
        accepted.append(event)

    def summaries(self, text, tag="normal"):
        tag = tag if tag in _TAGS else "normal"
        accepted = []
        if tag == "section":
            self._successes.clear()
            self._context.clear()
            self._failure_remaining = 0
        lines = _plain(text).splitlines()[:96]
        for raw in lines:
            message = _label(raw, 2000)
            if not message or _DIVIDER.fullmatch(message):
                continue
            if tag in ("section", "subsection"):
                title = message.strip("─━═ ")
                if not title or "MCU Flasher by Naph" in title:
                    continue
                self._append(title, tag, accepted)
                continue

            notice = known_builder_notice(_label(raw, 8192))
            if notice and tag != "fail" and not self._failure_remaining:
                self._append(notice, "normal", accepted)
                continue

            # Package-prefixed pip events still have their recognizable body.
            body = message.split(": ", 1)[-1]
            severity = "fail" if tag == "fail" or _ERROR.search(message) or _DEPENDENCY_ERROR.search(message) else (
                "warn" if tag == "warn" or _WARNING.search(message) else tag)
            if severity == "fail":
                # A few preceding technical records explain an otherwise terse
                # error. Resolver/download noise is never used as context.
                for context in self._context:
                    self._append(context, "dim", accepted)
                self._context.clear()
                self._failure_remaining = 6
                self._append(message, "fail", accepted)
                continue
            if severity == "warn":
                self._append(message, "warn", accepted)
                continue

            if _RAW_PIP.match(message) or _RAW_PIP.match(body):
                continue
            if re.search(r": not installed, will download and install\.$", message, re.I):
                continue
            if _PERCENT.fullmatch(message) or (_BAR.match(message) and _percent(message) is not None):
                continue
            if re.match(r"Successfully installed\b", body, re.I):
                self._append("Python packages installed; verifying the environment…", "normal", accepted)
                continue
            if re.match(r"Successfully (?:downloaded|built)\b", body, re.I):
                continue
            if message.startswith("Detailed run log: "):
                filename = re.split(r"[\\/]", message[len("Detailed run log: "):])[-1]
                message = f"Detailed run log: {filename} · location in technical details"
            if message.startswith("Release seed staging retained at "):
                self._context.append(message[:480])
                continue
            if message.startswith("Importing release package ") and " -> " in message:
                message = message.split(" -> ", 1)[0]
            if re.match(r"Installing\s+(?:\d+\s+)?Python dependencies\b", message, re.I):
                message = concise_status(message)
            if "is up to date" in message.lower() and tag == "dim":
                # The independent verification outcome is useful; preserve it.
                self._append(message, "ok", accepted)
                continue
            if tag == "dim":
                meaningful = re.match(
                    r"(?:Detailed run log:|(?:Tool|Platform|Library) Manager:\s*Installing\b|"
                    r"Preparing (?:builder|Zephyr Git modules)\b|Still preparing builder\b|"
                    r"Builder ready:|(?:Downloading|Unpacking) .+ (?:ended|interrupted)\b)", message, re.I)
                missing = re.search(r"\b(?:missing|unavailable|not found|absent|unsupported)\b", message, re.I)
                if meaningful or missing or self._failure_remaining:
                    self._append(message, "dim", accepted)
                    if self._failure_remaining:
                        self._failure_remaining -= 1
                else:
                    self._context.append(message[:480])
                continue
            self._append(message, tag, accepted)
        return tuple(accepted)

    def update_block(self, block_type, text):
        self.block_type = str(block_type)[:40]
        self._rows = parse_package_rows(block_type, text)
        return self._rows

    def commit_block(self):
        """Retain final rows as the stage's package result, ending live updates."""
        self.block_type = None
        return self._rows

    def clear_block(self):
        self.block_type = None
        self._rows = ()
        return self._rows

    def snapshot(self):
        return {"events": [asdict(event) for event in self._events],
                "rows": [asdict(row) for row in self._rows],
                "block_type": self.block_type,
                "successes": list(self._successes),
                "context": list(self._context),
                "failure_remaining": self._failure_remaining}

    def restore_snapshot(self, snapshot):
        """Restore only bounded presentation state; never replay raw events."""
        self.__init__()
        if not isinstance(snapshot, dict):
            return
        for record in snapshot.get("events", [])[-self.MAX_EVENTS:]:
            if isinstance(record, dict):
                event = SummaryEvent(str(record.get("text", ""))[:2000], record.get("tag", "normal"))
                self._events.append(event)
                self._chars += len(event.text)
        while self._events and self._chars > self.MAX_CHARS:
            self._chars -= len(self._events.popleft().text)
        rows = []
        for record in snapshot.get("rows", [])[:self.MAX_ROWS]:
            if isinstance(record, dict):
                pct = record.get("percent")
                rows.append(PackageRow(str(record.get("name", ""))[:240],
                                       str(record.get("status", ""))[:240],
                                       max(0, min(100, round(pct))) if isinstance(pct, (int, float)) else None,
                                       record.get("tone", "normal"), str(record.get("detail", ""))[:480]))
        self._rows = tuple(rows)
        self.block_type = str(snapshot["block_type"])[:40] if snapshot.get("block_type") else None
        self._successes = dict.fromkeys(str(value)[:2000] for value in snapshot.get("successes", [])[-self.MAX_SUCCESS_KEYS:])
        self._context.extend(str(value)[:480] for value in snapshot.get("context", [])[-4:])
        self._failure_remaining = max(0, min(6, int(snapshot.get("failure_remaining", 0))))


__all__ = ["PackageRow", "SummaryEvent", "BootstrapPresentation", "parse_package_rows",
           "compact_package_text", "concise_status"]
