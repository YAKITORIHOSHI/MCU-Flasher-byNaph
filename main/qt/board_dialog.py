#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.board_dialog — PySide6 Search & Select MCU Board Dialog.

Optimized intelligent search engine with multi-token matching, architectural
aliasing, fuzzy ranking, category filters, and rich metadata presentation.
"""
from __future__ import annotations

import re
import difflib
import threading
from collections import OrderedDict
from typing import Callable, Optional, Sequence

# pyrefly: ignore [missing-import]
from PySide6.QtCore import (Qt, QRect, QRectF, QSize, QModelIndex, QTimer,
                           QAbstractListModel, QObject, Signal, Slot)
# pyrefly: ignore [missing-import]
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPen,
    QBrush,
    QShortcut,
)
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import (
    QDialog,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QFrame,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QStyle,
    QButtonGroup,
)
from main.qt.icons import ActionButton as QPushButton

from main.core.board_catalog import SUPPORTED_BOARDS
from main.core.config import load_recent_boards, add_recent_board, get_theme_mode
from main.qt.theme import get_palette

# ── String & Token Helpers ───────────────────────────────────────────────────

def _normalize_text(text: str) -> str:
    """Lowercase and remove non-alphanumeric chars."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _extract_tokens(text: str) -> list[str]:
    """Extract words and clean tokens, expanding common compound hardware acronyms."""
    if not text:
        return []
    cleaned = re.sub(r"[^a-zA-Z0-9]+", " ", text).lower()
    raw_tokens = [t for t in cleaned.split() if t]

    tokens = list(raw_tokens)
    for t in raw_tokens:
        # e.g. esp32s3 -> esp32 + s3
        m = re.match(r"^(esp32)([sc]\d+|cam)?$", t)
        if m and m.group(2):
            if m.group(1) not in tokens:
                tokens.append(m.group(1))
            if m.group(2) not in tokens:
                tokens.append(m.group(2))
        # e.g. atmega328p -> atmega + 328p
        m2 = re.match(r"^(atmega)(\d+[a-z]?)$", t)
        if m2:
            if m2.group(1) not in tokens:
                tokens.append(m2.group(1))
            if m2.group(2) not in tokens:
                tokens.append(m2.group(2))
        # e.g. nodemcuv2 -> nodemcu + v2
        m3 = re.match(r"^(nodemcu)(v\d+)?$", t)
        if m3 and m3.group(2):
            if m3.group(1) not in tokens:
                tokens.append(m3.group(1))
            if m3.group(2) not in tokens:
                tokens.append(m3.group(2))
    return list(dict.fromkeys(tokens))


# Canonical flagship reference boards boosted when exact query matches
_FLAGSHIP_MAPPINGS: dict[str, list[str]] = {
    "arduino uno": ["Arduino UNO"],
    "uno": ["Arduino UNO"],
    "arduino nano": ["Arduino Nano"],
    "nano": ["Arduino Nano"],
    "arduino mega": ["Arduino Mega or Mega 2560"],
    "mega": ["Arduino Mega or Mega 2560"],
    "esp32": ["ESP32 Dev Module"],
    "esp32 dev": ["ESP32 Dev Module"],
    "esp32 s3": ["ESP32S3 Dev Module"],
    "esp32s3": ["ESP32S3 Dev Module"],
    "s3": ["ESP32S3 Dev Module"],
    "esp32 c3": ["ESP32C3 Dev Module"],
    "esp32c3": ["ESP32C3 Dev Module"],
    "c3": ["ESP32C3 Dev Module"],
    "esp32 cam": ["AI Thinker ESP32-CAM"],
    "cam": ["AI Thinker ESP32-CAM"],
    "pico": ["Raspberry Pi Pico", "Raspberry Pi Pico W"],
    "nodemcu": ["NodeMCU 1.0 (ESP-12E Module)", "NodeMCU-32S"],
    "wemos d1": ["WEMOS D1 MINI ESP32", "LOLIN(WEMOS) D1 R2 & mini"],
    "d1 mini": ["WEMOS D1 MINI ESP32", "LOLIN(WEMOS) D1 R2 & mini"],
}


# ── Search Index & Intelligent Ranking ────────────────────────────────────────

class BoardSearchIndex:
    """Fast, pre-indexed in-memory search index for MCU boards."""

    def __init__(self, boards_dict: dict, recent_boards: Optional[list[str]] = None,
                 *, on_progress=None):
        self.boards = boards_dict
        self.recent_boards = set(recent_boards or [])
        self._recent_order = tuple(recent_boards or ())
        self._on_progress = on_progress
        self.index: list[dict] = []
        self._query_cache = OrderedDict()
        self._query_lock = threading.Lock()
        self._build_index()
        self.by_name = {item["name"]: item for item in self.index}
        self._vocabulary = frozenset(token for item in self.index for token in item["name_tokens"])

    def _build_index(self) -> None:
        flagships = {
            "Arduino UNO", "Arduino Nano", "Arduino Mega or Mega 2560",
            "ESP32 Dev Module", "ESP32S3 Dev Module", "ESP32C3 Dev Module",
            "NodeMCU 1.0 (ESP-12E Module)", "Raspberry Pi Pico",
            "AI Thinker ESP32-CAM",
        }

        # Put the first visible page (including recents) through the same
        # metadata path first. Search ranking still sorts by its original score.
        recent_names = [name for name in self._recent_order if name in self.boards]
        recent_set = set(recent_names)
        ordered_names = recent_names + [name for name in sorted(self.boards) if name not in recent_set]
        delivered = 0
        for name in ordered_names:
            info = self.boards.get(name, {}) or {}
            name_lower = name.lower()
            clean_name = _normalize_text(name)
            name_tokens = _extract_tokens(name)
            name_token_set = set(name_tokens)

            board_id = str(info.get("board", "") or "").lower()
            clean_board_id = _normalize_text(board_id)
            board_id_tokens = _extract_tokens(board_id)

            arduino_id = str(info.get("arduino_board_id", "") or "").lower()
            clean_arduino_id = _normalize_text(arduino_id)
            arduino_id_tokens = _extract_tokens(arduino_id)

            mcu = str(info.get("mcu", "") or "").lower()
            clean_mcu = _normalize_text(mcu)
            mcu_tokens = _extract_tokens(mcu)

            platform = str(info.get("platform", "") or "").lower()
            clean_platform = _normalize_text(platform)
            platform_tokens = _extract_tokens(platform)

            pio_name = str(info.get("pio_name", "") or "").lower()
            clean_pio_name = _normalize_text(pio_name)
            pio_name_tokens = _extract_tokens(pio_name)

            pio_vendor = str(info.get("pio_vendor", "") or "").lower()
            vendor_tokens = _extract_tokens(pio_vendor)

            flash_mb = info.get("flash_mb")
            has_psram = bool(info.get("has_psram"))

            # Determine architecture family & sub-variant
            family = "OTHER"
            sub_family = ""
            if "esp32" in platform or "esp32" in mcu or "esp32" in clean_name:
                family = "ESP32"
                if "s3" in mcu or "s3" in clean_name or "s3" in clean_board_id:
                    sub_family = "S3"
                elif "c3" in mcu or "c3" in clean_name or "c3" in clean_board_id:
                    sub_family = "C3"
                elif "c6" in mcu or "c6" in clean_name or "c6" in clean_board_id:
                    sub_family = "C6"
                elif "s2" in mcu or "s2" in clean_name or "s2" in clean_board_id:
                    sub_family = "S2"
                elif "cam" in clean_name or "cam" in clean_board_id:
                    sub_family = "CAM"
                else:
                    sub_family = "GENERIC"
            elif "8266" in platform or "8266" in mcu or "8266" in clean_name:
                family = "ESP8266"
            elif "avr" in platform or "atmega" in mcu or "attiny" in mcu:
                family = "AVR"
                if "328" in mcu or "328" in clean_name:
                    sub_family = "328P"
                elif "2560" in mcu or "2560" in clean_name:
                    sub_family = "2560"
                elif "32u4" in mcu or "32u4" in clean_name:
                    sub_family = "32U4"
            elif "rp2040" in mcu or "raspberry" in clean_name or "pico" in clean_name:
                family = "RP2040"
            elif "stm32" in platform or "stm32" in mcu:
                family = "STM32"

            # Aliases & hardware shorthand tags
            aliases: set[str] = set()
            if family == "ESP32":
                aliases.add("esp32")
                if sub_family == "S3":
                    aliases.update(["s3", "esp32s3", "esp32-s3"])
                elif sub_family == "C3":
                    aliases.update(["c3", "esp32c3", "esp32-c3"])
                elif sub_family == "S2":
                    aliases.update(["s2", "esp32s2", "esp32-s2"])
                elif sub_family == "C6":
                    aliases.update(["c6", "esp32c6", "esp32-c6"])
                elif sub_family == "CAM":
                    aliases.update(["cam", "esp32cam", "esp32-cam"])
            elif family == "ESP8266":
                aliases.update(["8266", "esp8266"])
            elif family == "RP2040":
                aliases.update(["rp2040", "pico", "raspberrypi", "raspberry pi"])
            elif family == "AVR":
                aliases.update(["avr", "atmega"])
            elif family == "STM32":
                aliases.update(["stm32", "bluepill", "blackpill"])

            if "uno" in clean_name or "uno" in clean_board_id:
                aliases.add("uno")
                if family == "AVR":
                    aliases.update(["uno r3", "unor3", "r3", "328", "328p", "atmega328", "atmega328p"])
            if "nano" in clean_name or "nano" in clean_board_id:
                aliases.add("nano")
                if family == "AVR":
                    aliases.update(["nano 328", "nano328", "328", "328p", "atmega328", "atmega328p", "168", "atmega168"])
            if "mega" in clean_name or "mega" in clean_board_id:
                aliases.update(["mega", "mega2560", "mega 2560", "2560", "atmega2560"])
            if "nodemcu" in clean_name or "nodemcu" in clean_board_id:
                aliases.update(["nodemcu", "esp8266", "esp12", "esp12e", "esp-12e"])
            if "wemos" in clean_name or "d1" in clean_name or "lolin" in clean_name:
                aliases.update(["wemos", "d1", "d1 mini", "d1mini", "lolin"])

            is_flagship = name in flagships

            all_searchable_tokens = set(
                name_tokens + board_id_tokens + arduino_id_tokens +
                mcu_tokens + platform_tokens + pio_name_tokens +
                vendor_tokens + list(aliases)
            )

            # Build readable secondary metadata line
            details_parts: list[str] = []
            if mcu:
                details_parts.append(f"MCU: {mcu}")
            if flash_mb:
                details_parts.append(f"{flash_mb:g}MB Flash")
            if has_psram:
                details_parts.append("PSRAM")
            if board_id and board_id != clean_name:
                details_parts.append(f"ID: {board_id}")
            if platform and platform not in ("espressif32", "atmelavr"):
                details_parts.append(platform)

            sub_info = "  •  ".join(details_parts) if details_parts else (platform.upper() if platform else "MCU")

            # Formatted display family tag
            display_family = family
            if family == "ESP32" and sub_family in ("S3", "C3", "S2", "C6", "CAM"):
                display_family = f"ESP32-{sub_family}"

            from main.core.target_profile import target_problem
            problem = target_problem(info)
            if problem:
                sub_info = "Definition required • " + sub_info

            self.index.append({
                "name": name,
                "name_lower": name_lower,
                "clean_name": clean_name,
                "name_tokens": name_tokens,
                "name_token_set": name_token_set,
                "board_id": board_id,
                "clean_board_id": clean_board_id,
                "arduino_id": arduino_id,
                "mcu": mcu,
                "clean_mcu": clean_mcu,
                "platform": platform,
                "pio_name": pio_name,
                "pio_vendor": pio_vendor,
                "family": family,
                "sub_family": sub_family,
                "display_family": display_family,
                "sub_info": sub_info,
                "aliases": aliases,
                "all_tokens": all_searchable_tokens,
                "is_flagship": is_flagship,
                "is_recent": name in self.recent_boards,
                "problem": problem,
            })
            if self._on_progress and (len(self.index) == 16 or len(self.index) - delivered >= 128):
                if self._on_progress(self.index[delivered:]) is False:
                    raise _BoardSearchCancelled()
                delivered = len(self.index)
        if self._on_progress and delivered < len(self.index):
            if self._on_progress(self.index[delivered:]) is False:
                raise _BoardSearchCancelled()
        self._on_progress = None

    def search(self, query: str, category_filter: str = "ALL") -> list[str]:
        """Return board names ranked by relevance score, optionally filtered by category."""
        raw_q = (query or "").strip().lower()
        clean_q = _normalize_text(raw_q)
        q_tokens = _extract_tokens(raw_q)
        if not q_tokens and clean_q:
            q_tokens = [clean_q]

        category = (category_filter or "ALL").upper()
        cache_key = (raw_q, category)
        with self._query_lock:
            cached = self._query_cache.get(cache_key)
            if cached is not None:
                self._query_cache.move_to_end(cache_key)
                return list(cached)

        results: list[tuple[str, int, int]] = []
        candidates = []
        for item in self.index:
            # Apply category chip filter if set
            if category == "RECENT":
                if not item["is_recent"]:
                    continue

            elif category != "ALL":
                if item["family"] != category:
                    continue

            candidates.append(item)

            if not raw_q:
                # When query is empty, keep natural ordering (recent boards boosted)
                score = 1000 if item["is_recent"] else 1
            else:
                score = self._score_item(item, raw_q, clean_q, q_tokens)

            if score > 0:
                results.append((item["name"], score, len(item["name"])))

        # Typo matching is a fallback. Compare each distinct word once, with
        # cheap length/character bounds before computing edit similarity.
        if not results and len(q_tokens) == 1 and len(clean_q) >= 4:
            fuzzy_tokens = {}
            for word in self._vocabulary:
                if 2 * min(len(word), len(clean_q)) / (len(word) + len(clean_q)) < 0.8:
                    continue
                matcher = difflib.SequenceMatcher(None, clean_q, word)
                if matcher.quick_ratio() >= 0.8:
                    similarity = matcher.ratio()
                    if similarity >= 0.8:
                        fuzzy_tokens[word] = similarity
            for item in candidates:
                similarity = max((fuzzy_tokens.get(word, 0) for word in item["name_tokens"]), default=0)
                if similarity:
                    score = int(3000 * similarity) + (800 if item["is_recent"] else 0)
                    results.append((item["name"], score, len(item["name"])))

        # Sort descending by score, then shortest name, then alphabet
        results.sort(key=lambda x: (-x[1], x[2], x[0].lower()))
        names = tuple(r[0] for r in results)
        with self._query_lock:
            self._query_cache[cache_key] = names
            if len(self._query_cache) > 32:
                self._query_cache.popitem(last=False)
        return list(names)

    def _score_item(self, item: dict, raw_q: str, clean_q: str, q_tokens: list[str]) -> int:
        name_lower = item["name_lower"]
        clean_name = item["clean_name"]
        name = item["name"]

        # ── 1. EXACT MATCHES (Top Tier) ──
        if name_lower == raw_q or clean_name == clean_q:
            return 100000

        # Exact flagship mapping hit (e.g. query "uno" -> "Arduino UNO")
        if raw_q in _FLAGSHIP_MAPPINGS:
            favs = _FLAGSHIP_MAPPINGS[raw_q]
            if name in favs:
                return 90000 - favs.index(name) * 500

        board_id = item["board_id"]
        clean_board_id = item["clean_board_id"]
        arduino_id = item["arduino_id"]

        if board_id == raw_q or clean_board_id == clean_q or arduino_id == raw_q:
            return 80000

        score = 0

        # ── 2. PREFIX MATCHES ON NAME OR ID ──
        if name_lower.startswith(raw_q):
            score += 15000
        elif clean_name.startswith(clean_q):
            score += 12000
        elif any(w.startswith(raw_q) for w in item["name_tokens"]):
            score += 9000

        # ── 3. MULTI-TOKEN EVALUATION ──
        matched_tokens = 0
        name_matched_tokens = 0

        for token in q_tokens:
            clean_tok = token  # Query tokens are already lowercase alphanumeric.
            tok_score = 0

            # Display name match
            if token in item["name_token_set"]:
                tok_score += 2000
                name_matched_tokens += 1
            elif any(w.startswith(token) for w in item["name_token_set"]):
                tok_score += 1000
                name_matched_tokens += 1
            elif any(token in w for w in item["name_token_set"]):
                tok_score += 500
                name_matched_tokens += 1
            elif clean_tok and clean_tok in clean_name:
                tok_score += 400
                name_matched_tokens += 1

            # Aliases, MCU, board ID, vendor
            if token in item["aliases"] or (clean_tok and clean_tok in item["aliases"]):
                tok_score += 1500
            elif token == item["mcu"] or clean_tok == item["clean_mcu"]:
                tok_score += 1200
            elif token in item["mcu"] or (clean_tok and clean_tok in item["clean_mcu"]):
                tok_score += 600
            elif token in item["board_id"] or token in item["arduino_id"]:
                tok_score += 800
            elif token in item["pio_name"] or token in item["platform"]:
                tok_score += 400
            elif token in item["all_tokens"]:
                tok_score += 200

            if tok_score > 0:
                matched_tokens += 1
                score += tok_score

        # Coverage check: require all tokens (or high percentage for long queries)
        if q_tokens:
            if matched_tokens == len(q_tokens):
                score += 5000
            elif matched_tokens / len(q_tokens) >= 0.7 and len(q_tokens) >= 3:
                score += int(1000 * (matched_tokens / len(q_tokens)))
            else:
                return 0

        # Sub-variant alignment: prefer generic when not searching for sub-family
        has_sub_in_query = any(t in ("s3", "c3", "s2", "c6", "cam") for t in q_tokens)
        if "esp32" in q_tokens:
            if not has_sub_in_query:
                if item["sub_family"] == "GENERIC":
                    score += 3000
                elif item["sub_family"] in ("S3", "C3", "S2", "C6"):
                    score -= 1000
            else:
                for sub in ("s3", "c3", "s2", "c6", "cam"):
                    if sub in q_tokens and item["sub_family"] == sub.upper():
                        score += 4000

        # Architecture specific boosts
        if "uno" in q_tokens:
            if item["family"] == "AVR":
                score += 3000
            if "arduino" in item["clean_name"]:
                score += 2000

        if "nano" in q_tokens:
            if item["family"] == "AVR":
                score += 3000
            if "arduino" in item["clean_name"]:
                score += 2000

        # Boost flagships & recent boards
        if item["is_flagship"]:
            score += 1500
        if item["is_recent"]:
            score += 800

        # Small penalty for unnecessary length difference
        score -= min(abs(len(item["name"]) - len(raw_q)) * 5, 500)

        return max(score, 1)


_INDEX_CACHE = OrderedDict()
_INDEX_CACHE_LOCK = threading.Lock()


class _BoardSearchCancelled(Exception):
    """A closed picker or newer catalog cancelled incomplete indexing."""


class _BoardSearchWorker(QObject):
    """One active search and one latest pending request; completion is queued."""

    completed = Signal(object)
    progress = Signal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self._condition = threading.Condition()
        self._pending = None
        self._closed = False
        self._thread = None
        self._progress_token = 0
        self._awaiting_progress = None

    def submit(self, request):
        with self._condition:
            if self._closed:
                return
            self._pending = request
            if self._thread is None:
                try:
                    self._thread = threading.Thread(target=self._run, name="MCU_BoardSearch", daemon=True)
                    self._thread.start()
                except Exception as exc:
                    self._thread = None
                    self._pending = None
                    self.completed.emit((request[0], [], [], None, str(exc)))
            self._condition.notify()

    def stop(self):
        with self._condition:
            self._closed = True
            self._pending = None
            self._condition.notify()

    def acknowledge_progress(self, token):
        with self._condition:
            if self._awaiting_progress == token:
                self._awaiting_progress = None
            self._condition.notify()

    def _deliver_progress(self, generation, key, rows, reset):
        # At most one queued page can be outstanding. Closed/stale requests
        # wake the wait immediately; no timer polling or unbounded Qt signals.
        with self._condition:
            if self._closed or (self._pending is not None and self._pending[1] != key):
                return False
            if self._pending is not None:
                return True  # Finish and cache this index for the latest query.
            self._progress_token += 1
            token = self._progress_token
            self._awaiting_progress = token
            self.progress.emit((generation, rows, reset, token))
            self._condition.wait_for(lambda: self._closed or self._pending is not None or
                                     self._awaiting_progress != token)
            return not self._closed and (self._pending is None or self._pending[1] == key)

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                request, self._pending = self._pending, None
            generation, key, boards, recents, query, category = request
            try:
                with _INDEX_CACHE_LOCK:
                    index = _INDEX_CACHE.get(key)
                    if index is not None:
                        _INDEX_CACHE.move_to_end(key)
                if index is None:
                    progressive = not query.strip() and category == "ALL"
                    preview_started = False
                    delivered_count = 0
                    recent_count = len([name for name in recents if name in boards])

                    def indexed_page(page):
                        nonlocal preview_started, delivered_count
                        rows = []
                        if progressive:
                            if not preview_started and recent_count:
                                rows.append({"header": "RECENTLY USED BOARDS"})
                            for meta in page:
                                if recent_count and delivered_count == recent_count:
                                    rows.append({"header": "ALL BOARDS"})
                                rows.append(meta)
                                delivered_count += 1
                            reset = not preview_started
                            preview_started = True
                            return self._deliver_progress(generation, key, rows, reset)
                        with self._condition:
                            return not self._closed and (self._pending is None or self._pending[1] == key)

                    index = BoardSearchIndex(boards, recent_boards=recents, on_progress=indexed_page)
                    with _INDEX_CACHE_LOCK:
                        _INDEX_CACHE[key] = index
                        while len(_INDEX_CACHE) > 2:
                            _INDEX_CACHE.popitem(last=False)
                separator = -1
                if not query.strip() and category == "ALL":
                    recent_names = [name for name in recents if name in index.by_name]
                    recent_set = set(recent_names)
                    names = recent_names + [name for name in sorted(boards) if name not in recent_set]
                    separator = len(recent_names) if recent_names else -1
                else:
                    names = index.search(query, category)
                rows = []
                if separator > 0:
                    rows.append({"header": "RECENTLY USED BOARDS"})
                for position, name in enumerate(names):
                    if position == separator:
                        rows.append({"header": "ALL BOARDS"})
                    rows.append(index.by_name[name])
                result = (generation, rows, names, index, "")
            except _BoardSearchCancelled:
                continue
            except Exception as exc:
                result = (generation, [], [], None, str(exc))
            with self._condition:
                if self._closed:
                    return
                # Newer requests already waiting do not need an intermediate UI update.
                if self._pending is not None:
                    continue
                self.completed.emit(result)


class _BoardListModel(QAbstractListModel):
    """Metadata stays in Python; Qt only paints visible rows."""

    def __init__(self, parent):
        super().__init__(parent)
        self.rows = []

    def replace(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def append(self, rows):
        if not rows:
            return
        first = len(self.rows)
        self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
        self.rows.extend(rows)
        self.endInsertRows()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def flags(self, index):
        if not index.isValid() or "header" in self.rows[index.row()]:
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        if "header" in row:
            if role == Qt.ItemDataRole.DisplayRole:
                return row["header"]
            if role == Qt.ItemDataRole.UserRole + 1:
                return "header"
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.UserRole):
            return row["name"]
        if role == Qt.ItemDataRole.ToolTipRole:
            return row["problem"] or row["sub_info"]
        if role == Qt.ItemDataRole.UserRole + 2:
            return row["display_family"]
        if role == Qt.ItemDataRole.UserRole + 3:
            return row["sub_info"]
        if role == Qt.ItemDataRole.UserRole + 4:
            return row["is_recent"]
        return None


# ── Custom Rich Item Delegate ────────────────────────────────────────────────

_FAM_COLORS: dict[str, tuple[int, int, int]] = {
    "ESP32": (0, 210, 255),       # cyan
    "ESP32-S3": (0, 230, 180),    # teal
    "ESP32-C3": (52, 152, 219),   # blue
    "ESP32-S2": (155, 89, 182),   # purple
    "ESP32-C6": (46, 204, 113),   # emerald
    "ESP32-CAM": (230, 126, 34),  # orange
    "ESP8266": (165, 105, 189),   # purple
    "AVR": (243, 156, 18),        # amber/orange
    "RP2040": (46, 204, 113),     # green
    "STM32": (41, 128, 185),      # dark blue
    "SAMD": (231, 76, 60),        # coral/red
    "TEENSY": (26, 188, 156),     # turquoise
}
_DEFAULT_FAM_RGB: tuple[int, int, int] = (120, 140, 160)


class BoardListItemDelegate(QStyledItemDelegate):
    """
    Renders each MCU board item with a bold title, gold star for recent boards,
    sleek pill badge for hardware architecture, and a clean subtitle with chip/ID.
    """

    def __init__(self, parent: Optional[QWidget] = None, theme_pal: Optional[dict] = None):
        super().__init__(parent)
        self.pal = theme_pal or {}
        self._f_title = QFont("Segoe UI", 10, QFont.Weight.DemiBold)
        self._f_title_bold = QFont("Segoe UI", 10, QFont.Weight.Bold)
        self._f_badge = QFont("Consolas", 8, QFont.Weight.Bold)
        self._f_sub = QFont("Consolas", 8)
        self._f_header = QFont("Segoe UI", 9, QFont.Weight.Bold)

        self._fm_title = QFontMetrics(self._f_title)
        self._fm_title_bold = QFontMetrics(self._f_title_bold)
        self._fm_badge = QFontMetrics(self._f_badge)
        self._fm_sub = QFontMetrics(self._f_sub)
        self._star_width = self._fm_title.horizontalAdvance("★ ")
        self._star_color = QColor("#f1c40f")

        self._badge_cache: dict[tuple[str, bool], tuple[QBrush, QPen, QColor]] = {}
        self._init_cached_colors()

    def set_palette(self, pal: dict) -> None:
        self.pal = pal
        self._init_cached_colors()

    def _init_cached_colors(self) -> None:
        self._c_bg_darkest = QColor(self.pal.get("BG_DARKEST", "#0d1117"))
        self._c_bg_mid = QColor(self.pal.get("BG_MID", "#1c2333"))
        self._c_bg_hover = QColor(self.pal.get("BG_HOVER", "#2a3a55"))
        border_col = QColor(self.pal.get("BORDER", "#2d3748"))
        self._c_border_line = QColor(border_col.red(), border_col.green(), border_col.blue(), 50)
        self._c_cyan = QColor(self.pal.get("CYAN", "#00d2ff"))
        self._c_text_bright = QColor(self.pal.get("TEXT_BRIGHT", "#ffffff"))
        self._c_text_dim = QColor(self.pal.get("TEXT_DIM", "#8fa1b3"))
        self._badge_cache.clear()

    def _get_badge_styling(self, family: str, is_selected: bool) -> tuple[QBrush, QPen, QColor]:
        key = (family, is_selected)
        cached = self._badge_cache.get(key)
        if cached is None:
            rgb = _FAM_COLORS.get(family, _DEFAULT_FAM_RGB)
            badge_bg = QBrush(QColor(rgb[0], rgb[1], rgb[2], 70 if is_selected else 40))
            badge_border = QPen(QColor(rgb[0], rgb[1], rgb[2], 220 if is_selected else 120), 1)
            badge_fg = QColor(rgb[0], rgb[1], rgb[2])
            cached = (badge_bg, badge_border, badge_fg)
            self._badge_cache[key] = cached
        return cached

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        view = self.parent()
        width = max(1, view.viewport().width() - 4) if isinstance(view, QListView) else 300
        is_header = index.data(Qt.ItemDataRole.UserRole + 1) == "header"
        if is_header:
            return QSize(width, 26)
        return QSize(width, 48)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect
        is_header = index.data(Qt.ItemDataRole.UserRole + 1) == "header"

        if is_header:
            # Section header
            text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            painter.setPen(self._c_cyan if "RECENT" in text else self._c_text_dim)
            painter.setFont(self._f_header)
            painter.drawText(rect.adjusted(12, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
            painter.restore()
            return

        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # Row Background
        if is_selected:
            painter.fillRect(rect, self._c_bg_hover)
            # Left accent stripe
            painter.fillRect(QRect(rect.left(), rect.top() + 2, 3, rect.height() - 4), self._c_cyan)
        elif is_hover:
            painter.fillRect(rect, self._c_bg_mid)
        else:
            painter.fillRect(rect, self._c_bg_darkest)

        # Subtle bottom separator line
        painter.setPen(self._c_border_line)
        painter.drawLine(rect.left() + 8, rect.bottom(), rect.right() - 8, rect.bottom())

        # Extract item metadata
        name = str(index.data(Qt.ItemDataRole.UserRole) or index.data(Qt.ItemDataRole.DisplayRole) or "")
        family = str(index.data(Qt.ItemDataRole.UserRole + 2) or "MCU")
        sub_info = str(index.data(Qt.ItemDataRole.UserRole + 3) or "")
        is_recent = bool(index.data(Qt.ItemDataRole.UserRole + 4))

        # Title line
        left_margin = rect.left() + 14
        title_y = rect.top() + 19

        title_font = self._f_title_bold if is_selected else self._f_title
        fm_title = self._fm_title_bold if is_selected else self._fm_title
        painter.setFont(title_font)
        painter.setPen(self._c_cyan if is_selected else self._c_text_bright)

        if is_recent:
            painter.setPen(self._star_color)
            painter.drawText(left_margin, title_y, "★ ")
            left_margin += self._star_width
            painter.setPen(self._c_cyan if is_selected else self._c_text_bright)

        # Space for badge on right
        badge_text = f" {family} "
        badge_w = self._fm_badge.horizontalAdvance(badge_text) + 12
        badge_h = 18

        max_title_w = rect.width() - (left_margin - rect.left()) - badge_w - 20
        elided_title = fm_title.elidedText(name, Qt.TextElideMode.ElideRight, max(10, max_title_w))
        painter.drawText(left_margin, title_y, elided_title)

        # Subtitle line
        painter.setFont(self._f_sub)
        painter.setPen(self._c_cyan if is_selected else self._c_text_dim)
        sub_y = rect.top() + 37
        max_sub_w = rect.width() - 28
        elided_sub = self._fm_sub.elidedText(sub_info, Qt.TextElideMode.ElideRight, max(10, max_sub_w))
        painter.drawText(rect.left() + 14, sub_y, elided_sub)

        # Draw Pill Badge on top-right
        badge_rect = QRect(rect.right() - badge_w - 14, rect.top() + 7, badge_w, badge_h)
        badge_bg, badge_border, badge_fg = self._get_badge_styling(family, is_selected)

        painter.setBrush(badge_bg)
        painter.setPen(badge_border)
        painter.drawRoundedRect(QRectF(badge_rect), 3.0, 3.0)

        painter.setFont(self._f_badge)
        painter.setPen(badge_fg)
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, family)

        painter.restore()


# ── Search Input Box with Arrow Key List Navigation ──────────────────────────

class _SearchLineEdit(QLineEdit):
    """QLineEdit with Up / Down Arrow intercept to seamlessly navigate the list widget."""

    def __init__(self, target_list: QListView, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._target_list = target_list

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            model = self._target_list.model()
            count = model.rowCount()
            if count > 0:
                curr = self._target_list.currentIndex().row()
                step = 1 if key == Qt.Key.Key_Down else -1
                next_row = curr + step if curr >= 0 else (0 if key == Qt.Key.Key_Down else count - 1)

                while 0 <= next_row < count:
                    item = model.index(next_row)
                    if item.flags() & Qt.ItemFlag.ItemIsSelectable:
                        self._target_list.setCurrentIndex(item)
                        self._target_list.scrollTo(item)
                        break
                    next_row += step
            return
        elif key == Qt.Key.Key_PageDown:
            self._target_list.setFocus()
            return
        super().keyPressEvent(event)


# ── Main BoardSearchDialog ───────────────────────────────────────────────────

class BoardSearchDialog(QDialog):
    """
    Modal dialog to search & select from all supported MCU boards.
    Features instant intelligent multi-token ranking, architecture chips,
    and rich hardware metadata.
    """

    _local_catalog_ready = Signal(int, object)

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        current_board: str = "",
        board_list: Optional[Sequence[str]] = None,
        on_select_callback: Optional[Callable[[str], None]] = None,
        backend: Optional[Any] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Select MCU board")
        self.setModal(True)
        from main.qt.responsive import fit_dialog, ScreenWatcher
        fit_dialog(self, (800, 600), (360, 280))
        self.setSizeGripEnabled(True)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowMaximizeButtonHint)

        self.on_select_callback = on_select_callback
        self._backend = backend
        self._board_subset = tuple(board_list) if board_list else None
        self.current_board = current_board or ""
        self.result_board: Optional[str] = None
        self._active_category = "ALL"
        self._closed = False
        self._local_refresh_generation = 0
        self._local_refresh_running = False
        self._local_catalog_ready.connect(self._local_catalog_finished, Qt.ConnectionType.QueuedConnection)
        self._generation = 0
        self._search_pending = False
        self._catalog_loading = False
        self._catalog_refresh_id = None
        self._catalog_pending = {}
        self._catalog_before_preview = None
        self._catalog_error = ""
        self._preview_count = 0
        self._confirm_when_ready = False
        self._select_initial_board = True
        self._pending_selection: Optional[str] = None
        self._search_index = None
        self._saved_recents = load_recent_boards()
        self._set_catalog_snapshot()
        self._worker = _BoardSearchWorker(self)
        self.destroyed.connect(self._worker.stop)
        self._worker.completed.connect(self._search_completed, Qt.ConnectionType.QueuedConnection)
        self._worker.progress.connect(self._search_progress, Qt.ConnectionType.QueuedConnection)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(75)
        self._search_timer.timeout.connect(self._dispatch_search)
        self._catalog_timer = QTimer(self)
        self._catalog_timer.setSingleShot(True)
        self._catalog_timer.setInterval(50)
        self._catalog_timer.timeout.connect(self._apply_catalog_batch)

        self._build_ui()
        self._screen_watcher = ScreenWatcher(self, lambda _screen: self._adapt_layout())
        self._apply_dialog_theme()
        self._apply_filter("")

        # Connect live theme re-styling
        try:
            from main.qt.signals import signals
            signals.theme_changed.connect(self._apply_dialog_theme)
        except Exception:
            pass

        # Autofocus search entry immediately
        QTimer.singleShot(40, self.search_ent.setFocus)

        # Auto-refresh board catalog only if catalog is currently empty and not restricted to a subset
        if self._board_subset is None and not self.all_boards:
            self.lbl_hdr_sub.setText("Loading definitions…")
            self.lbl_count.setText("Auto-refreshing board catalog…")
            self.btn_refresh.setEnabled(False)
            self.btn_refresh.setText("Refreshing…")
            QTimer.singleShot(0, self._auto_refresh_boards)

    def _set_catalog_snapshot(self, boards=None):
        if boards is not None:
            snapshot = dict(boards)
        elif hasattr(SUPPORTED_BOARDS, "snapshot"):
            _revision, snapshot = SUPPORTED_BOARDS.snapshot()
        else:
            snapshot = dict(SUPPORTED_BOARDS.items())
        if self._board_subset is not None:
            snapshot = {name: snapshot.get(name, {}) for name in self._board_subset}
        self._boards_snapshot = snapshot
        self.all_boards = sorted(snapshot)
        canonical = {name.lower(): name for name in self.all_boards}
        self.recent_boards = list(dict.fromkeys(canonical[name.lower()] for name in self._saved_recents
                                               if name.lower() in canonical))[:5]
        revision = tuple((name, id(info)) for name, info in snapshot.items())
        self._index_key = (id(SUPPORTED_BOARDS), revision, self._board_subset, tuple(self.recent_boards))

    def _apply_dialog_theme(self, theme_mode: str | None = None) -> None:
        """Apply active theme palette across all BoardSearchDialog components."""
        if not theme_mode:
            theme_mode = get_theme_mode()
        pal = get_palette(theme_mode)
        self._pal = pal

        bg_darkest = pal.get("BG_DARKEST", "#0d1117")
        bg_dark = pal.get("BG_DARK", "#151922")
        bg_mid = pal.get("BG_MID", "#1c2333")
        bg_hover = pal.get("BG_HOVER", "#2a3a55")

        text = pal.get("TEXT", "#e0e6ed")
        text_bright = pal.get("TEXT_BRIGHT", "#ffffff")
        text_dim = pal.get("TEXT_DIM", "#8fa1b3")
        cyan = pal.get("CYAN", "#00d2ff")
        border = pal.get("BORDER", "#2d3748")

        btn_compile = pal.get("BTN_COMPILE", "#1a5c3a")
        btn_compile_h = pal.get("BTN_COMPILE_H", "#216e46")
        btn_stop = pal.get("BTN_STOP", "#6e2020")
        btn_stop_h = pal.get("BTN_STOP_H", "#882828")

        self.setStyleSheet(f"""
            QDialog {{ background-color: {bg_dark}; color: {text}; }}
            QLabel {{ color: {text}; font-family: 'Montserrat', 'Segoe UI', sans-serif; }}
        """)
        self.hdr_frame.setStyleSheet(f"background-color: {bg_darkest}; padding: 10px 14px; border-bottom: 1px solid {border};")
        self.lbl_hdr.setStyleSheet(f"color: {cyan}; font-size: 13px; font-weight: bold; background: transparent;")
        self.lbl_hdr_sub.setStyleSheet(f"color: {text_dim}; font-size: 11px; background: transparent;")

        self.search_frame.setStyleSheet(f"background-color: {bg_dark}; padding: 8px 14px 4px 14px;")
        self.lbl_search.setStyleSheet(f"color: {text_dim}; font-size: 11px; font-weight: bold;")
        self.search_ent.setStyleSheet(f"""
            QLineEdit {{
                background-color: {bg_darkest};
                color: {text_bright};
                border: 1px solid {border};
                border-radius: 5px;
                padding: 6px 10px;
                font-family: Consolas, monospace;
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border: 1px solid {cyan};
            }}
        """)

        # Update category filter chips
        self._update_chip_styles()

        self.listbox.setStyleSheet(f"""
            QListView {{
                background-color: {bg_darkest};
                border: 1px solid {border};
                border-radius: 5px;
                color: {text};
                outline: none;
                padding: 2px;
            }}
        """)
        self._delegate.set_palette(pal)
        self.listbox.viewport().update()

        self.btn_frame.setStyleSheet(f"background-color: {bg_dark}; border-top: 1px solid {border};")
        self.lbl_count.setStyleSheet(f"color: {text_dim}; font-size: 11px;")

        self.btn_cancel.setStyleSheet(f"""
            QPushButton:enabled {{
                background-color: {btn_stop};
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 4px;
                font-family: 'Montserrat', 'Segoe UI', sans-serif;
                font-size: 11px;
            }}
            QPushButton:enabled:hover {{ background-color: {btn_stop_h}; border-color: {pal.get('RED', '#e74c3c')}; }}
            QPushButton:enabled:pressed {{ background-color: {btn_stop}; border-color: {pal.get('RED', '#e74c3c')}; }}
            QPushButton:disabled {{ background-color: {bg_mid}; color: {text_dim}; border: 1px solid {border}; }}
        """)

        self.btn_select.setStyleSheet(f"""
            QPushButton:enabled {{
                background-color: {btn_compile};
                color: #ffffff;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 4px;
                font-family: 'Montserrat', 'Segoe UI', sans-serif;
                font-size: 11px;
                font-weight: bold;
            }}
            QPushButton:enabled:hover {{ background-color: {btn_compile_h}; border-color: {pal.get('GREEN', '#4ec994')}; }}
            QPushButton:enabled:pressed {{ background-color: {btn_compile}; border-color: {pal.get('GREEN', '#4ec994')}; }}
            QPushButton:disabled {{ background-color: {bg_mid}; color: {text_dim}; border: 1px solid {border}; }}
        """)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header Banner ─────────────────────────────────────────────────────
        self.hdr_frame = QFrame()
        hdr_layout = QHBoxLayout(self.hdr_frame)
        hdr_layout.setContentsMargins(14, 8, 14, 8)

        self.lbl_hdr = QLabel("Choose your board")
        hdr_layout.addWidget(self.lbl_hdr)

        hdr_layout.addStretch()

        self.lbl_hdr_sub = QLabel(f"{len(self.all_boards)} definitions")
        hdr_layout.addWidget(self.lbl_hdr_sub)

        root.addWidget(self.hdr_frame)

        # ── Search Input Row ──────────────────────────────────────────────────
        self.search_frame = QFrame()
        search_layout = QHBoxLayout(self.search_frame)
        search_layout.setContentsMargins(14, 10, 14, 4)
        search_layout.setSpacing(8)

        self.lbl_search = QLabel("Search:")
        search_layout.addWidget(self.lbl_search)

        self.listbox = QListView(self)
        self._list_model = _BoardListModel(self.listbox)
        self.listbox.setModel(self._list_model)
        self.listbox.setLayoutMode(QListView.LayoutMode.Batched)
        self.listbox.setResizeMode(QListView.ResizeMode.Adjust)
        self.listbox.setBatchSize(128)
        self.listbox.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.listbox.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.listbox.setHorizontalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self._delegate = BoardListItemDelegate(self.listbox)
        self.listbox.setItemDelegate(self._delegate)

        self.search_ent = _SearchLineEdit(self.listbox)
        self.search_ent.setPlaceholderText("Search board, MCU, chip, or architecture (e.g. ESP32-S3, Uno R3, Nano 328, C3)...")
        self.search_ent.setClearButtonEnabled(True)
        self.search_ent.textChanged.connect(self._on_search_text_changed)
        self.search_ent.returnPressed.connect(self._confirm_selection)
        search_layout.addWidget(self.search_ent)
        self.btn_refresh = QPushButton("Refresh boards")
        self.btn_refresh.setToolTip("Refresh locally prepared board definitions; add or update packs in Boards & Libraries Manager")
        self.btn_refresh.clicked.connect(self._request_catalog_refresh)
        search_layout.addWidget(self.btn_refresh)
        root.addWidget(self.search_frame)

        # ── Category Quick Filter Chips ───────────────────────────────────────
        self.chips_frame = QFrame()
        chips_layout = QGridLayout(self.chips_frame)
        self._chips_layout = chips_layout
        chips_layout.setContentsMargins(14, 2, 14, 6)
        chips_layout.setSpacing(6)

        self._chip_buttons: dict[str, QPushButton] = {}
        chip_specs = [
            ("ALL", "All"),
            ("ESP32", "ESP32"),
            ("AVR", "AVR"),
            ("ESP8266", "ESP8266"),
            ("RP2040", "RP2040"),
            ("STM32", "STM32"),
            ("RECENT", "★ Recent"),
        ]
        self._chip_group = QButtonGroup(self)
        self._chip_group.setExclusive(True)

        for cat_id, label in chip_specs:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(cat_id == "ALL")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(22)
            btn.clicked.connect(lambda checked, c=cat_id: self._on_chip_clicked(c))
            self._chip_group.addButton(btn)
            self._chip_buttons[cat_id] = btn
            chips_layout.addWidget(btn, 0, len(self._chip_buttons) - 1)

        root.addWidget(self.chips_frame)

        # ── Listbox Container ─────────────────────────────────────────────────
        list_container = QWidget()
        list_v = QVBoxLayout(list_container)
        list_v.setContentsMargins(14, 4, 14, 8)
        list_v.addWidget(self.listbox)
        root.addWidget(list_container, stretch=1)

        self.listbox.doubleClicked.connect(lambda item: self._confirm_selection())
        self.listbox.selectionModel().selectionChanged.connect(self._update_select_button_state)

        # ── Action Buttons Footer ─────────────────────────────────────────────
        self.btn_frame = QFrame()
        btn_layout = QGridLayout(self.btn_frame)
        self._footer_grid = btn_layout
        btn_layout.setContentsMargins(14, 10, 14, 12)
        btn_layout.setSpacing(8)

        self.lbl_count = QLabel(f"{len(self.all_boards)} boards available")

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setFixedSize(85, 30)
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.clicked.connect(self.reject)
        self._actions_row = QHBoxLayout()
        self._actions_row.addStretch()
        self._actions_row.addWidget(self.btn_cancel)

        self.btn_select = QPushButton("Select Board")
        self.btn_select.setFixedSize(110, 30)
        self.btn_select.setEnabled(False)
        self.btn_select.setCursor(Qt.CursorShape.ArrowCursor)
        self.btn_select.clicked.connect(self._confirm_selection)
        self._actions_row.addWidget(self.btn_select)
        self._layout_mode = None
        self._chip_columns = None
        self._adapt_layout()

        root.addWidget(self.btn_frame)

        # ── Shortcuts ─────────────────────────────────────────────────────────
        QShortcut(QKeySequence("Escape"), self, activated=self._on_escape_pressed)
        QShortcut(QKeySequence("Return"), self, activated=self._confirm_selection)
        QShortcut(QKeySequence("Enter"), self, activated=self._confirm_selection)
        from main.qt.signals import signals
        signals.board_catalog_updated.connect(self._catalog_updated)
        # Opening uses the published catalog. Refresh reads local definitions
        # only; runtime never queries a network registry.

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_footer_grid"):
            self._adapt_layout()

    def _adapt_layout(self) -> None:
        buttons = list(self._chip_buttons.values())
        button_width = max(btn.minimumSizeHint().width() for btn in buttons) + 6
        cols = max(1, min(len(buttons), (self.width() - 28) // button_width))
        if cols != self._chip_columns:
            for index, btn in enumerate(buttons):
                self._chips_layout.removeWidget(btn)
                self._chips_layout.addWidget(btn, index // cols, index % cols)
            self._chip_columns = cols
        narrow = self.width() < (self.lbl_count.minimumSizeHint().width() +
                                 self._actions_row.minimumSize().width() + 52)
        if narrow != self._layout_mode:
            grid = self._footer_grid
            grid.removeWidget(self.lbl_count)
            grid.removeItem(self._actions_row)
            grid.addWidget(self.lbl_count, 0, 0, 1, 2 if narrow else 1)
            grid.addLayout(self._actions_row, 1 if narrow else 0, 0 if narrow else 1,
                           1, 2 if narrow else 1)
            grid.setColumnStretch(0, 1)
            self._layout_mode = narrow
        self.btn_refresh.setText("Refresh" if self.width() < 550 else "Refresh boards")

    def _get_backend(self):
        if self._backend is not None:
            return self._backend
        parent = self.parent()
        if parent is not None:
            backend = getattr(parent, "_backend", None)
            if backend is not None:
                return backend
            window = getattr(parent, "window", lambda: None)()
            if window is not None:
                backend = getattr(window, "_backend", None)
                if backend is not None:
                    return backend
        return None

    def _auto_refresh_boards(self) -> None:
        if self._closed or self._board_subset is not None:
            return
        if not self.all_boards:
            self.lbl_hdr_sub.setText("Loading definitions…")
            self.lbl_count.setText("Auto-refreshing board catalog…")
            self.btn_refresh.setEnabled(False)
            self.btn_refresh.setText("Refreshing…")
        self._request_catalog_refresh(invalidate_parsed=False)

    def _on_auto_refresh_timer(self) -> None:
        if self._closed or self._board_subset is not None:
            return
        backend = self._get_backend()
        if backend and (getattr(backend, "is_busy", False) or getattr(backend, "_catalog_refresh_running", False)):
            return
        self._request_catalog_refresh(invalidate_parsed=False)

    def _request_catalog_refresh(self, _checked=False, *, invalidate_parsed=True):
        if self._closed:
            return
        backend = self._get_backend()
        if backend and hasattr(backend, "refresh_board_catalog"):
            self.btn_refresh.setEnabled(False)
            self.btn_refresh.setText("Refreshing…")
            backend.refresh_board_catalog(include_registry=True, invalidate_parsed=invalidate_parsed)
        else:
            self._async_local_refresh(invalidate_parsed=invalidate_parsed)

    def _async_local_refresh(self, *, invalidate_parsed=False):
        if self._closed or self._local_refresh_running:
            return
        self._local_refresh_running = True
        self._local_refresh_generation += 1
        generation = self._local_refresh_generation
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("Refreshing…")

        def _worker():
            try:
                from main.core.board_catalog import load_dynamic_boards, SUPPORTED_BOARDS
                def on_batch(batch):
                    if self._closed or generation != self._local_refresh_generation:
                        return False
                    self._local_catalog_ready.emit(generation, {"batch": batch, "partial": True})
                    return True
                boards = load_dynamic_boards(dict(SUPPORTED_BOARDS), on_batch=on_batch,
                                            invalidate_parsed=invalidate_parsed)
                data = {"boards": boards}
            except Exception as exc:
                data = {"error": str(exc)}
            try:
                self._local_catalog_ready.emit(generation, data)
            except RuntimeError:
                pass  # The dialog may have been deleted during discovery.

        try:
            threading.Thread(target=_worker, name="MCU_LocalBoardRefresh", daemon=True).start()
        except Exception as exc:
            self._local_refresh_running = False
            self._catalog_updated({"error": str(exc)})

    @Slot(int, object)
    def _local_catalog_finished(self, generation, data):
        if self._closed:
            return
        if data.get("partial"):
            if generation == self._local_refresh_generation:
                self._catalog_updated(data)
            return
        self._local_refresh_running = False
        if generation != self._local_refresh_generation:
            return
        if "boards" in data:
            from main.core.board_catalog import SUPPORTED_BOARDS
            SUPPORTED_BOARDS.replace(data["boards"])
        self._catalog_updated(data)

    def _catalog_updated(self, data):
        if self._closed:
            return
        refresh_id = data.get("refresh_id")
        if isinstance(refresh_id, int):
            if self._catalog_refresh_id is not None and refresh_id < self._catalog_refresh_id:
                return
            self._catalog_refresh_id = refresh_id
        if data.get("partial"):
            batch = data.get("batch")
            if not isinstance(batch, dict) or not batch:
                return
            if not self._catalog_loading:
                self._catalog_before_preview = dict(self._boards_snapshot)
            self._catalog_loading = True
            self._catalog_error = ""
            self._catalog_pending.update(batch)
            if not self._catalog_timer.isActive():
                self._catalog_timer.start()
            self._update_select_button_state()
            return
        # A newer catalog delivery invalidates a still-running local refresh.
        self._local_refresh_generation += 1
        self._catalog_timer.stop()
        self._catalog_pending.clear()
        self._catalog_loading = False
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText("Refresh boards")
        if "error" in data:
            self._catalog_error = str(data["error"])
            if self._catalog_before_preview is not None:
                self._set_catalog_snapshot(self._catalog_before_preview)
                self._catalog_before_preview = None
                self.lbl_hdr_sub.setText(f"{len(self.all_boards)} definitions")
                self._apply_filter(self.search_ent.text())
            self.lbl_count.setText(f"Refresh failed: {self._catalog_error}")
            self._update_select_button_state()
            return
        self._catalog_error = ""
        self._catalog_before_preview = None
        self._set_catalog_snapshot(data.get("boards"))
        self.lbl_hdr_sub.setText(f"{len(self.all_boards)} definitions")
        self._apply_filter(self.search_ent.text())
        if data.get("warning"):
            self.lbl_count.setToolTip(data["warning"])

    def _apply_catalog_batch(self):
        if self._closed or not self._catalog_pending:
            return
        snapshot = dict(self._boards_snapshot)
        snapshot.update(self._catalog_pending)
        self._catalog_pending.clear()
        self._set_catalog_snapshot(snapshot)
        self.lbl_hdr_sub.setText(f"{len(self.all_boards)} definitions · loading…")
        self._apply_filter(self.search_ent.text())

    def _framework_for_board(self, name):
        """Retain valid preferences and select only an unambiguous default."""
        info = self._search_index.boards.get(name, {}) if self._search_index else {}
        allowed = sorted(info.get("frameworks") or ([info["framework"]] if info.get("framework") else []))
        unavailable = info.get("unavailable_frameworks")
        unavailable = unavailable if isinstance(unavailable, dict) else {}
        allowed = [framework for framework in allowed if not unavailable.get(framework)]
        backend = self._get_backend()
        selected = backend._resolve_board_info(name).get("framework") if backend and hasattr(backend, "_resolve_board_info") and name else ""
        if selected in allowed:
            return selected
        if "arduino" in allowed:
            return "arduino"
        return allowed[0] if len(allowed) == 1 else ""

    def _update_chip_styles(self) -> None:
        pal = getattr(self, "_pal", {})
        bg_mid = pal.get("BG_MID", "#1c2333")
        bg_hover = pal.get("BG_HOVER", "#2a3a55")
        text_dim = pal.get("TEXT_DIM", "#8fa1b3")
        cyan = pal.get("CYAN", "#00d2ff")
        border = pal.get("BORDER", "#2d3748")

        for cat_id, btn in self._chip_buttons.items():
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {bg_mid};
                    color: {text_dim};
                    border: 1px solid {border};
                    border-radius: 11px;
                    padding: 0 10px;
                    font-size: 11px;
                    font-family: 'Segoe UI', sans-serif;
                }}
                QPushButton:hover {{
                    background-color: {bg_hover};
                    color: {cyan};
                    border-color: {cyan};
                }}
                QPushButton:checked {{
                    background-color: {bg_hover};
                    color: {cyan};
                    border: 1px solid {cyan};
                    font-weight: bold;
                }}
            """)

    def _on_chip_clicked(self, category_id: str) -> None:
        self._active_category = category_id
        self._apply_filter(self.search_ent.text())
        self.search_ent.setFocus()

    def _on_search_text_changed(self, text: str) -> None:
        self._mark_search_pending()
        self._search_timer.start()

    def _on_escape_pressed(self) -> None:
        if self.search_ent.text():
            self.search_ent.clear()
        else:
            self.reject()

    def _update_select_button_state(self) -> None:
        curr = self.listbox.currentIndex()
        is_sel = bool(not self._search_pending and not self._catalog_loading and curr.isValid() and
                      (curr.flags() & Qt.ItemFlag.ItemIsSelectable))
        self.btn_select.setEnabled(is_sel)
        self.btn_select.setCursor(Qt.CursorShape.PointingHandCursor if is_sel else Qt.CursorShape.ArrowCursor)

    def _select_item_by_name(self, name: str) -> None:
        for row, meta in enumerate(self._list_model.rows):
            if meta.get("name") == name:
                item = self._list_model.index(row)
                self.listbox.setCurrentIndex(item)
                self.listbox.scrollTo(item)
                break

    def _apply_filter(self, query: str) -> None:
        curr = self.listbox.currentIndex()
        if curr.isValid():
            name = curr.data(Qt.ItemDataRole.UserRole)
            if name:
                self._pending_selection = name
        self._mark_search_pending()
        self._dispatch_search(query)

    def _mark_search_pending(self):
        self._generation += 1
        self._search_pending = True
        self._confirm_when_ready = False
        self._preview_count = 0
        self.lbl_count.setText("Searching…")
        self._update_select_button_state()

    def _dispatch_search(self, query=None):
        if self._closed:
            return
        self._search_timer.stop()
        self._worker.submit((self._generation, self._index_key, self._boards_snapshot,
                             tuple(self.recent_boards), self.search_ent.text() if query is None else query,
                             self._active_category))

    @Slot(object)
    def _search_progress(self, result):
        generation, rows, reset, token = result
        try:
            if self._closed or generation != self._generation:
                return
            if reset:
                self._list_model.replace(list(rows))
                self._preview_count = 0
            else:
                self._list_model.append(rows)
            self._preview_count += sum("name" in row for row in rows)
            self.lbl_count.setText(f"Loading {self._preview_count} of {len(self.all_boards)} boards…")
            if not self.listbox.currentIndex().isValid():
                preferred = self._pending_selection or self.current_board
                names = [row.get("name") for row in self._list_model.rows if "name" in row]
                self._select_item_by_name(preferred if preferred in names else (names[0] if names else ""))
            self._update_select_button_state()
        finally:
            self._worker.acknowledge_progress(token)

    @Slot(object)
    def _search_completed(self, result):
        generation, rows, matches, index, error = result
        if self._closed or generation != self._generation:
            return
        self._search_pending = False
        self._search_index = index
        self._list_model.replace(rows)
        suffix = f" [{self._active_category}]" if self._active_category != "ALL" else ""
        loading = " · loading definitions…" if self._catalog_loading else ""
        self.lbl_count.setText(f"Search failed: {error}" if error else
                               f"Refresh failed: {self._catalog_error}" if self._catalog_error else
                               f"{len(matches)} of {len(self.all_boards)} boards{suffix}{loading}")
        # Initial selection and arrow navigation still work with pinned headers.
        prior = self._pending_selection
        self._pending_selection = None
        select_name = matches[0] if matches else ""
        if prior and prior in matches:
            select_name = prior
        elif self._select_initial_board and not self.search_ent.text() and self.current_board in matches:
            select_name = self.current_board
        self._select_initial_board = False
        if select_name:
            self._select_item_by_name(select_name)
        self._update_select_button_state()
        if self._confirm_when_ready:
            self._confirm_when_ready = False
            self._confirm_selection()

    def _confirm_selection(self) -> None:
        if self._closed:
            return
        if self._search_pending:
            # Enter while typing waits for this query, never selects a stale row.
            self._confirm_when_ready = True
            self._dispatch_search()
            return
        if self._catalog_loading:
            return  # Provisional display aliases wait for the coherent catalog.
        backend = self._get_backend()
        if backend and (getattr(backend, "is_busy", False) or getattr(backend, "active_operation", None) is not None):
            return
        curr = self.listbox.currentIndex()
        if curr.isValid() and (curr.flags() & Qt.ItemFlag.ItemIsSelectable):
            raw_name = curr.data(Qt.ItemDataRole.UserRole)
            self.result_board = raw_name
            framework = self._framework_for_board(raw_name)
            if backend and hasattr(backend, "set_board_framework") and framework:
                backend.set_board_framework(raw_name, framework)
            if self.on_select_callback:
                self.on_select_callback(self.result_board)
            else:
                add_recent_board(self.result_board)
            self.accept()

    def done(self, result):
        if not self._closed:
            self._closed = True
            self._local_refresh_generation += 1
            self._local_catalog_ready.disconnect(self._local_catalog_finished)
            self._search_timer.stop()
            self._catalog_timer.stop()
            self._catalog_pending.clear()
            self._worker.stop()
            if hasattr(self, "_auto_refresh_timer"):
                self._auto_refresh_timer.stop()
            from main.qt.signals import signals
            try:
                signals.board_catalog_updated.disconnect(self._catalog_updated)
            except Exception:
                pass
            try:
                signals.theme_changed.disconnect(self._apply_dialog_theme)
            except Exception:
                pass
        super().done(result)

    def selected_board(self) -> Optional[str]:
        return self.result_board


__all__ = ["BoardSearchDialog"]
