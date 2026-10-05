"""Bounded cleanup of terminal controls in a semantic build event stream."""
import re

_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ConsoleTextCleaner:
    """Remove CSI/OSC controls, including sequences split across events.

    Build events already carry semantic colors; terminal cursor commands must
    not become visible text or escape bytes in copied diagnostics.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._state = "text"
        self._length = 0

    def clean(self, text):
        if self._state == "text" and "\x1b" not in text:
            return _CONTROLS.sub("", text)
        output = []
        for char in text:
            if self._state == "text":
                if char == "\x1b":
                    self._state = "escape"
                    self._length = 0
                elif char in "\n\t" or (ord(char) >= 32 and char != "\x7f"):
                    output.append(char)
                continue
            self._length = min(self._length + 1, 257)
            if self._length > 256 and self._state not in ("osc", "osc_end"):
                self.reset()
                if char in "\n\t" or (ord(char) >= 32 and char != "\x7f"):
                    output.append(char)
            elif self._state == "escape":
                self._state = "csi" if char == "[" else "osc" if char == "]" else "text"
            elif self._state == "csi":
                if "@" <= char <= "~":
                    self.reset()
            elif self._state == "osc":
                if char == "\x07":
                    self.reset()
                elif char == "\x1b":
                    self._state = "osc_end"
            elif self._state == "osc_end":
                if char == "\\":
                    self.reset()
                else:
                    self._state = "osc"
        return "".join(output)
