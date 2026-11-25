"""Incremental, clipboard-safe decoding of serial device bytes."""
from __future__ import annotations

import codecs
import re
import time

# Everything that is not shown exactly as received. ESC/BEL stay with the display.
_SPECIAL = re.compile("[\x00-\x06\x08\x0b-\x1a\x1c-\x1f\x7f-\x9f  \udc80-\udcff]")


class SerialTextDecoder:
    """Preserve UTF-8 across reads and expose undecodable bytes explicitly.

    ESC/BEL are left for the display's incremental terminal-control parser.
    CRLF is one line ending; a standalone CR is visible in an append-only log.
    Only the codec's incomplete character (at most three bytes) and one CR can
    be retained here, independently of how long a device runs without a newline.

    With ``collapse``, a run of unreadable bytes (a wrong baud rate, a resetting
    board, a floating line) shows its first few bytes as \\xNN and then one
    counted summary instead of screens of escapes. Readable text ends the run and
    is never dropped; isolated control bytes are always shown.
    """

    NOISE_SHOWN = 8       # Escapes shown before a run is summarized.
    READABLE_RUN = 12     # Consecutive readable characters that end a run.
    SUMMARY_DELAYS = (1.0, 5.0, 15.0, 60.0)  # Summaries while a run keeps going.
    QUIET = 0.4           # Silence after which text following a first run is shown.
    LONG_QUIET = 2.0      # Silence that always shows it, however noisy the line was.

    def __init__(self, *, collapse: bool = False, clock=time.monotonic):
        self._decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
        self._pending_cr = False
        self.invalid_bytes = 0
        self.collapse = bool(collapse)
        self._clock = clock
        self._noise = 0          # Unreadable units in the current run.
        self._readable = 0       # Readable characters since the last one.
        self._hidden = 0         # Bytes not shown yet in this run.
        self._held = ""          # Readable text waiting to prove the run ended.
        self._summaries = 0
        self._next_summary = 0.0
        self._last_noise = 0.0
        self._last_char = "\n"

    def feed(self, data: bytes, *, final: bool = False) -> str:
        decoded = self._decoder.decode(data, final=final)
        output: list[str] = []
        position = 0
        if self._pending_cr and decoded:
            self._pending_cr = False
            if decoded[0] == "\n":
                position = 1
                self._text("\n", output)
            else:
                self._unreadable("\\x0d", 1, output)
        for match in _SPECIAL.finditer(decoded, position):
            start = match.start()
            if start < position:
                continue  # The line feed of a CRLF was consumed with its CR.
            if start > position:
                self._text(decoded[position:start], output)
            position = match.end()
            char = match.group()
            code = ord(char)
            if char == "\r":
                if position >= len(decoded):
                    self._pending_cr = True
                elif decoded[position] == "\n":
                    position += 1
                    self._text("\n", output)
                else:
                    self._unreadable("\\x0d", 1, output)
            elif 0xDC80 <= code <= 0xDCFF:
                self.invalid_bytes += 1
                self._unreadable(f"\\x{code - 0xDC00:02x}", 1, output)
            elif code < 0x80:
                self._unreadable(f"\\x{code:02x}", 1, output)
            else:
                # Valid Unicode controls differ from undecodable single bytes.
                self._unreadable(f"\\u{code:04x}", len(char.encode("utf-8")), output)
        if position < len(decoded):
            self._text(decoded[position:], output)
        if final:
            if self._pending_cr:
                self._pending_cr = False
                self._unreadable("\\x0d", 1, output)
            self._end_run(output)
        return "".join(output)

    def idle(self) -> str:
        """Text to show when the device goes quiet, such as a prompt after noise.

        A noisy line pauses between bursts. Waiting for real quiet keeps one stray
        readable byte per burst from printing a summary every time.
        """
        if not self._hidden and not self._held:
            return ""
        now = self._clock()
        quiet = now - self._last_noise
        due = now >= self._next_summary
        output: list[str] = []
        if self._held and (quiet >= self.LONG_QUIET or (quiet >= self.QUIET and (due or not self._summaries))):
            self._summarize(output)
            held, self._held = self._held, ""
            self._emit(held, output)
        elif self._hidden and due:
            self._summarize(output)
        return "".join(output)

    def _emit(self, text: str, output: list[str]) -> None:
        if text:
            output.append(text)
            self._last_char = text[-1]

    def _text(self, text: str, output: list[str]) -> None:
        self._readable += len(text)
        if not self._hidden and not self._held:
            if self._readable >= self.READABLE_RUN:
                self._noise = 0
            self._emit(text, output)
            return
        self._held += text
        if self._readable >= self.READABLE_RUN:
            self._end_run(output)

    def _unreadable(self, shown: str, size: int, output: list[str]) -> None:
        self._readable = 0
        if not self.collapse or self._noise < self.NOISE_SHOWN:
            self._noise += 1
            self._emit(shown, output)
            return
        # Characters between unreadable bytes are part of the same noise.
        now = self._last_noise = self._clock()
        if not self._hidden and not self._summaries:
            self._next_summary = now + self.SUMMARY_DELAYS[0]
        self._hidden += size + len(self._held.encode("utf-8", "surrogateescape"))
        self._held = ""
        if now >= self._next_summary:
            self._summarize(output)

    def _summarize(self, output: list[str]) -> None:
        """One counted line for bytes this run has not shown."""
        if not self._hidden:
            return
        count, self._hidden = self._hidden, 0
        lead = "" if self._last_char == "\n" else "\n"
        hint = " (check the baud rate)" if not self._summaries else ""
        plural = "" if count == 1 else "s"
        self._emit(f"{lead}[{count:,} unreadable byte{plural}{hint}]\n", output)
        self._summaries += 1
        delay = self.SUMMARY_DELAYS[min(self._summaries, len(self.SUMMARY_DELAYS) - 1)]
        self._next_summary = self._clock() + delay

    def _end_run(self, output: list[str]) -> None:
        summarized = bool(self._hidden)
        self._summarize(output)
        held, self._held = self._held, ""
        # The summary already ended its line; the noise's own line end adds nothing.
        self._emit(held.removeprefix("\n") if summarized else held, output)
        self._noise = self._summaries = 0
