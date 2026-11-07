"""Bounded cleanup of terminal controls in a semantic build event stream."""
import re

from main.core.ansi_sgr import PLAIN, apply_sgr

_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ConsoleTextCleaner:
    """Remove CSI/OSC controls, including sequences split across events.

    Build events already carry semantic colors; terminal cursor commands must
    not become visible text or escape bytes in copied diagnostics. SGR color and
    attribute sequences are kept as styled runs beside the clean text, so the
    copied text is unchanged and the console can still paint compiler colors.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        """Forget any unfinished sequence and the current style (Clear)."""
        self._end_sequence()
        self.style = PLAIN

    def _end_sequence(self):
        self._state = "text"
        self._length = 0
        self._csi = ""

    def clean(self, text):
        return self.clean_styled(text)[0]

    def clean_styled(self, text):
        """(clean text, runs) where runs are (start, length, SgrStyle) for styled spans only."""
        runs = []
        if self._state == "text" and "\x1b" not in text:
            plain = _CONTROLS.sub("", text)
            if plain and not self.style.plain:
                runs.append((0, len(plain), self.style))
            return plain, tuple(runs)
        output = []
        run_start = 0

        def mark():
            nonlocal run_start
            if not self.style.plain and len(output) > run_start:
                runs.append((run_start, len(output) - run_start, self.style))
            run_start = len(output)

        for char in text:
            if self._state == "text":
                if char == "\x1b":
                    self._state = "escape"
                    self._length = 0
                    self._csi = ""
                elif char in "\n\t" or (ord(char) >= 32 and char != "\x7f"):
                    output.append(char)
                continue
            self._length = min(self._length + 1, 257)
            if self._length > 256 and self._state not in ("osc", "osc_end"):
                self._end_sequence()
                if char in "\n\t" or (ord(char) >= 32 and char != "\x7f"):
                    output.append(char)
            elif self._state == "escape":
                self._state = "csi" if char == "[" else "osc" if char == "]" else "text"
            elif self._state == "csi":
                if "@" <= char <= "~":
                    if char == "m":
                        mark()
                        self.style = apply_sgr(self.style, self._csi)
                    self._end_sequence()
                else:
                    self._csi += char
            elif self._state == "osc":
                if char == "\x07":
                    self._end_sequence()
                elif char == "\x1b":
                    self._state = "osc_end"
            elif self._state == "osc_end":
                if char == "\\":
                    self._end_sequence()
                else:
                    self._state = "osc"
        mark()
        return "".join(output), tuple(runs)
