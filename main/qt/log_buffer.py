"""Bounded display buffers and small render batches for streaming log widgets."""
from collections import deque

TRUNCATED = "\n[Display truncated to protect memory]\n"


def display_text(text, limit=8192):
    text = str(text)
    if len(text) <= limit:
        return text
    half = (limit - len(TRUNCATED)) // 2
    return text[:half] + TRUNCATED + text[-half:]


class LogBuffer:
    """Deque capped by both text characters and entries; newest output wins."""
    def __init__(self, max_chars, max_items, text_of):
        self.max_chars = max_chars
        self.max_items = max_items
        self.text_of = text_of
        self._items = deque()
        self.chars = 0
        self.dropped = 0

    def __len__(self):
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def __reversed__(self):
        return reversed(self._items)

    def append(self, item):
        self._items.append(item)
        self.chars += len(self.text_of(item)) + 32
        self._trim()

    def _trim(self):
        while self._items and (self.chars > self.max_chars or len(self._items) > self.max_items):
            self.popleft()
            self.dropped += 1

    def recount(self):
        self.chars = sum(len(self.text_of(item)) + 32 for item in self._items)
        self._trim()

    def popleft(self):
        item = self._items.popleft()
        self.chars -= len(self.text_of(item)) + 32
        return item

    def clear(self):
        self._items.clear()
        self.chars = self.dropped = 0

    def drain(self, max_items=64, max_chars=16384):
        items = []
        chars = 0
        while self._items and len(items) < max_items:
            size = len(self.text_of(self._items[0]))
            if items and chars + size > max_chars:
                break
            items.append(self.popleft())
            chars += size
        return items

    def take_notice(self):
        dropped, self.dropped = self.dropped, 0
        return f"[Display backlog: {dropped} older entries omitted to protect memory]" if dropped else ""


def trim_document(widget, max_chars):
    """Block limits alone do not bound a stream that never sends a newline."""
    from PySide6.QtGui import QTextCursor
    block = widget.document().lastBlock()
    if block.isValid() and block.length() > 16384:
        # Re-shaping an ever-growing paragraph is quadratic even with wrapping
        # disabled. Keep its newest portion and make the display truncation clear.
        cursor = QTextCursor(block)
        cursor.setPosition(block.position())
        cursor.setPosition(block.position() + block.length() - 8192, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.insertText("[Long line display truncated] ")
    excess = widget.document().characterCount() - max_chars - 1
    if excess > 0:
        cursor = QTextCursor(widget.document())
        cursor.setPosition(0)
        cursor.setPosition(excess, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
