"""Incremental, clipboard-safe decoding of serial device bytes."""
from __future__ import annotations

import codecs


class SerialTextDecoder:
    """Preserve UTF-8 across reads and expose undecodable bytes explicitly.

    ESC/BEL are left for the display's incremental terminal-control parser.
    CRLF is one line ending; a standalone CR is visible in an append-only log.
    Only the codec's incomplete character (at most three bytes) and one CR can
    be retained here, independently of how long a device runs without a newline.
    """

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
        self._pending_cr = False
        self.invalid_bytes = 0

    def feed(self, data: bytes, *, final: bool = False) -> str:
        decoded = self._decoder.decode(data, final=final)
        output = []
        for char in decoded:
            if self._pending_cr:
                self._pending_cr = False
                if char == "\n":
                    output.append("\n")
                    continue
                output.append("\\x0d")
            code = ord(char)
            if char == "\r":
                self._pending_cr = True
            elif 0xDC80 <= code <= 0xDCFF:
                self.invalid_bytes += 1
                output.append(f"\\x{code - 0xDC00:02x}")
            elif code < 32 and char not in "\n\t\x1b\x07":
                output.append(f"\\x{code:02x}")
            elif code == 127:
                output.append("\\x7f")
            elif 0x80 <= code <= 0x9F or code in (0x2028, 0x2029):
                # Valid Unicode controls differ from undecodable single bytes.
                output.append(f"\\u{code:04x}")
            else:
                output.append(char)
        if final and self._pending_cr:
            self._pending_cr = False
            output.append("\\x0d")
        return "".join(output)
