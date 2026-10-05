"""Bounded parsing of PlatformIO's newline-free package progress."""
import codecs
import re


def output_chunks(stream):
    """Read available pipe bytes without waiting for a line or a full buffer."""
    binary = getattr(stream, "buffer", None)
    if binary is not None and hasattr(binary, "read1"):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while chunk := binary.read1(4096):
            yield decoder.decode(chunk)
        yield decoder.decode(b"", final=True)
    elif hasattr(stream, "read"):
        while chunk := stream.read(4096):
            yield chunk
    else:
        for line in stream:  # Legacy line iterators and isolated fixtures.
            yield line if line.endswith(("\r", "\n")) else line + "\n"


class PackageOutput:
    """Emit log/progress events; installation confirmation completes unpacking."""
    def __init__(self, emit):
        self.emit = emit
        self.item = "PlatformIO package"
        self.phase = None
        self.percent = None
        self._line = []
        self._shortened = False
        self._escape = 0

    def feed(self, chunk):
        for char in chunk:
            if self._escape:
                if self._escape == 1:
                    self._escape = 2 if char == "[" else 0
                elif "@" <= char <= "~":
                    self._escape = 0
                continue
            if char == "\x1b":
                self._escape = 1
                continue
            if char in "\r\n":
                self._flush_line()
                continue
            if len(self._line) < 3000:
                self._line.append(char)
            else:
                self._shortened = True
            if char == "%" and not self._shortened:
                text = "".join(self._line).strip()
                if re.fullmatch(r"(?:Downloading|Unpacking)\s+\d+(?:\.\d+)?%", text, re.I) or (
                    self.phase and re.fullmatch(r"\d+(?:\.\d+)?%", text)
                ):
                    self._flush_line()

    def _flush_line(self):
        text = "".join(self._line).strip()
        self._line.clear()
        shortened = self._shortened
        self._shortened = False
        if text:
            # Leave room within the existing 3,000-character event budget.
            if shortened:
                suffix = " … [display shortened; full output in setup log]"
                text = text[:3000 - len(suffix)] + suffix
            self._record(text)

    def _finish_phase(self, *, completed=False, interrupted=False):
        if not self.phase:
            return
        self.emit("clear", "")
        if interrupted:
            detail = f" at {self.percent}%" if self.percent is not None else ""
            self.emit("warn", f"{self.phase} {self.item} interrupted{detail}")
            self.emit("status", f"{self.phase} interrupted{detail}")
        elif completed or self.percent == 100:
            past = "Downloaded" if self.phase == "Downloading" else "Unpacked"
            self.emit("ok", f"{past} {self.item} — 100%")
        else:
            detail = f" at {self.percent}%" if self.percent is not None else ""
            self.emit("dim", f"{self.phase} {self.item} ended{detail}")
        self.phase, self.percent = None, None

    def _progress(self, phase, percent):
        if self.phase and (phase != self.phase or (
            percent is not None and self.percent is not None and percent < self.percent
        )):
            self._finish_phase(interrupted=phase == self.phase)
        self.phase = phase
        if percent is not None:
            self.percent = max(0, min(100, percent))
        label = f"  {phase} — {self.item}"
        if self.percent is not None:
            width = 30
            filled = round(width * self.percent / 100)
            label += "\n  " + "▰" * filled + "▱" * (width - filled) + f"  {self.percent}%"
        self.emit("progress", label)

    def _record(self, text):
        manager = re.match(r"(?:Tool|Platform|Library) Manager:\s*(.*)", text, re.I)
        body = manager[1] if manager else text
        if re.match(r"Warning\b|Warning!", body, re.I):
            self._finish_phase(interrupted=True)
            self.emit("warn", text)
        elif re.match(r"(?:Error\b|Offline bootstrap failed:|Traceback\b)", body, re.I):
            self._finish_phase(interrupted=True)
            self.emit("fail", text)
            self.emit("status", "Package preparation error — see setup log")
        elif manager and re.fullmatch(r".+\s+has been installed!?", body, re.I):
            self._finish_phase(completed=True)
            self.emit("ok", text)
        elif re.match(r"Successfully (?:installed|built)\b", body, re.I):
            self._finish_phase(completed=True)
            self.emit("ok", text)
        elif body.startswith(("Preparing builder ", "Still preparing builder ", "Preparing Zephyr Git modules")):
            self.emit("dim", text)
            self.emit("status", text)
        elif body.startswith("Builder ready:"):
            self.emit("ok", text)
        elif manager and re.match(r"Installing\s+", body, re.I):
            self._finish_phase()
            self.item = re.sub(r"^Installing\s+", "", body, flags=re.I)[:2000]
            self.emit("dim", text)
        elif match := re.fullmatch(r"(Downloading|Unpacking)(?:\s+(.+)|\.{3})?", text, re.I):
            percents = re.findall(r"(\d+(?:\.\d+)?)%", match[2] or "")
            self._progress(match[1].capitalize(), int(float(percents[-1])) if percents else None)
        elif self.phase and re.fullmatch(r"\d+(?:\.\d+)?%", text):
            self._progress(self.phase, int(float(text[:-1])))
        else:
            self.emit("dim", text)

    def finish(self):
        self._flush_line()
        self._finish_phase(interrupted=self.percent != 100)
