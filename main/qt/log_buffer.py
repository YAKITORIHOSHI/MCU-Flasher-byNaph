"""Bounded display buffers and small render batches for streaming log widgets."""
from collections import OrderedDict, deque
import re

TRUNCATED = "\n[Display truncated to protect memory]\n"
DIAGNOSTIC_TAGS = frozenset(("warning", "error", "severe_alert"))


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

    def adjust_size(self, item, old_text_length):
        """Account for one retained item's changed text without scanning history.

        Call immediately after mutating an item that is still in this buffer.
        Ordinary overflow trimming can evict the changed item with old history.
        """
        self.chars += len(self.text_of(item)) - old_text_length
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


class DiagnosticLogBuffer(LogBuffer):
    """Bound queued build output while retaining diagnostics before chatter.

    Two ordered indexes allow removal of the oldest ordinary entry in O(1)
    without moving retained diagnostics or scanning the queue. If diagnostics
    alone exceed either bound, retain their newest entries and report omissions.
    This protects delivery queues; retained display history still expires FIFO.
    """

    def __init__(self, max_chars, max_items, text_of, *, tag_of=None):
        super().__init__(max_chars, max_items, text_of)
        self._items = OrderedDict()
        self._ordinary = OrderedDict()
        self._next_key = 0
        self.tag_of = tag_of or self._default_tag

    @staticmethod
    def _default_tag(item):
        return item.get("tag", "normal") if isinstance(item, dict) else item[1]

    def __iter__(self):
        return iter(self._items.values())

    def __reversed__(self):
        return reversed(self._items.values())

    def append(self, item):
        key = self._next_key
        self._next_key += 1
        self._items[key] = item
        if self.tag_of(item) not in DIAGNOSTIC_TAGS:
            self._ordinary[key] = None
        self.chars += len(self.text_of(item)) + 32
        self._trim()

    def _remove(self, key):
        item = self._items.pop(key)
        self._ordinary.pop(key, None)
        self.chars -= len(self.text_of(item)) + 32
        return item

    def _trim(self):
        while self._items and (self.chars > self.max_chars or len(self._items) > self.max_items):
            source = self._ordinary if self._ordinary else self._items
            self._remove(next(iter(source)))
            self.dropped += 1

    def popleft(self):
        if not self._items:
            raise IndexError("pop from an empty log buffer")
        return self._remove(next(iter(self._items)))

    def recount(self):
        self.chars = sum(len(self.text_of(item)) + 32 for item in self._items.values())
        self._trim()

    def clear(self):
        self._items.clear()
        self._ordinary.clear()
        self._next_key = 0
        self.chars = self.dropped = 0

    def drain(self, max_items=64, max_chars=16384):
        items = []
        chars = 0
        while self._items and len(items) < max_items:
            first = next(iter(self._items.values()))
            size = len(self.text_of(first))
            if items and chars + size > max_chars:
                break
            items.append(self.popleft())
            chars += size
        return items


def coalesce_progress(items):
    """Keep the latest adjacent equivalent progress update before rendering.

    Items are (text, tag, newline, replace_pattern, timestamp) tuples. Ordinary
    messages, changes in pattern/tag/newline and diagnostics remain barriers.
    """
    result = []
    patterns = {}
    for item in items:
        equivalent = (result and item[3] and item[1] not in DIAGNOSTIC_TAGS
                      and item[1:4] == result[-1][1:4])
        valid = False
        if equivalent and isinstance(item[3], str):
            pattern = item[3]
            if pattern not in patterns:
                try:
                    re.compile(pattern)
                    patterns[pattern] = True
                except (re.error, TypeError):
                    patterns[pattern] = False
                if len(patterns) > 128:
                    patterns.pop(next(iter(patterns)))
            valid = patterns[pattern]
        if valid:
            result[-1] = item
        else:
            result.append(item)
    return result


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
