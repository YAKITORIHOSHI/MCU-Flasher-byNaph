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
from typing import Callable, Optional, Sequence

# pyrefly: ignore [missing-import]
from PySide6.QtCore import Qt, QRect, QRectF, QSize, QModelIndex, QTimer
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
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QFrame,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QStyle,
    QButtonGroup,
)

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

    def __init__(self, boards_dict: dict, recent_boards: Optional[list[str]] = None):
        self.boards = boards_dict
        self.recent_boards = set(recent_boards or [])
        self.index: list[dict] = []
        self._build_index()

    def _build_index(self) -> None:
        flagships = {
            "Arduino UNO", "Arduino Nano", "Arduino Mega or Mega 2560",
            "ESP32 Dev Module", "ESP32S3 Dev Module", "ESP32C3 Dev Module",
            "NodeMCU 1.0 (ESP-12E Module)", "Raspberry Pi Pico",
            "AI Thinker ESP32-CAM",
        }

        for name in sorted(self.boards.keys()):
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
            })

    def search(self, query: str, category_filter: str = "ALL") -> list[str]:
        """Return board names ranked by relevance score, optionally filtered by category."""
        raw_q = (query or "").strip().lower()
        clean_q = _normalize_text(raw_q)
        q_tokens = _extract_tokens(raw_q)
        if not q_tokens and clean_q:
            q_tokens = [clean_q]

        category = (category_filter or "ALL").upper()

        results: list[tuple[str, int, int]] = []
        for item in self.index:
            # Apply category chip filter if set
            if category == "RECENT":
                if not item["is_recent"]:
                    continue
            elif category != "ALL":
                if item["family"] != category:
                    continue

            if not raw_q:
                # When query is empty, keep natural ordering (recent boards boosted)
                score = 1000 if item["is_recent"] else 1
            else:
                score = self._score_item(item, raw_q, clean_q, q_tokens)

            if score > 0:
                results.append((item["name"], score, len(item["name"])))

        # Sort descending by score, then shortest name, then alphabet
        results.sort(key=lambda x: (-x[1], x[2], x[0].lower()))
        return [r[0] for r in results]

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
            clean_tok = _normalize_text(token)
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
                # Single-token fallback: check fuzzy typo match
                if len(q_tokens) == 1 and len(clean_q) >= 4:
                    if clean_q in clean_name or clean_name.startswith(clean_q):
                        score += 2000
                    else:
                        best_sim = max([difflib.SequenceMatcher(None, clean_q, w).ratio() for w in item["name_tokens"]] or [0])
                        if best_sim >= 0.8:
                            score += int(3000 * best_sim)
                        else:
                            return 0
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


# ── Custom Rich Item Delegate ────────────────────────────────────────────────

class BoardListItemDelegate(QStyledItemDelegate):
    """
    Renders each MCU board item with a bold title, gold star for recent boards,
    sleek pill badge for hardware architecture, and a clean subtitle with chip/ID.
    """

    def __init__(self, parent: Optional[QWidget] = None, theme_pal: Optional[dict] = None):
        super().__init__(parent)
        self.pal = theme_pal or {}

    def set_palette(self, pal: dict) -> None:
        self.pal = pal

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        is_header = index.data(Qt.ItemDataRole.UserRole + 1) == "header"
        if is_header:
            return QSize(option.rect.width(), 26)
        return QSize(option.rect.width(), 44)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect
        is_header = index.data(Qt.ItemDataRole.UserRole + 1) == "header"

        if is_header:
            # Section header
            text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
            dim = QColor(self.pal.get("TEXT_DIM", "#8fa1b3"))
            cyan = QColor(self.pal.get("CYAN", "#00d2ff"))
            painter.setPen(cyan if "RECENT" in text else dim)
            f = QFont("Segoe UI", 9, QFont.Weight.Bold)
            painter.setFont(f)
            painter.drawText(rect.adjusted(12, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
            painter.restore()
            return

        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        bg_darkest = QColor(self.pal.get("BG_DARKEST", "#0d1117"))
        bg_mid = QColor(self.pal.get("BG_MID", "#1c2333"))
        bg_hover = QColor(self.pal.get("BG_HOVER", "#2a3a55"))
        border_col = QColor(self.pal.get("BORDER", "#2d3748"))
        cyan_col = QColor(self.pal.get("CYAN", "#00d2ff"))
        text_bright = QColor(self.pal.get("TEXT_BRIGHT", "#ffffff"))
        text_dim = QColor(self.pal.get("TEXT_DIM", "#8fa1b3"))

        # Row Background
        if is_selected:
            painter.fillRect(rect, bg_hover)
            # Left accent stripe
            painter.fillRect(QRect(rect.left(), rect.top() + 2, 3, rect.height() - 4), cyan_col)
        elif is_hover:
            painter.fillRect(rect, bg_mid)
        else:
            painter.fillRect(rect, bg_darkest)

        # Subtle bottom separator line
        painter.setPen(QColor(border_col.red(), border_col.green(), border_col.blue(), 50))
        painter.drawLine(rect.left() + 8, rect.bottom(), rect.right() - 8, rect.bottom())

        # Extract item metadata
        name = str(index.data(Qt.ItemDataRole.UserRole) or index.data(Qt.ItemDataRole.DisplayRole) or "")
        family = str(index.data(Qt.ItemDataRole.UserRole + 2) or "MCU")
        sub_info = str(index.data(Qt.ItemDataRole.UserRole + 3) or "")
        is_recent = bool(index.data(Qt.ItemDataRole.UserRole + 4))

        # Title line
        left_margin = rect.left() + 12
        title_y = rect.top() + 18

        title_font = QFont("Segoe UI", 10, QFont.Weight.Bold if is_selected else QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(cyan_col if is_selected else text_bright)

        if is_recent:
            painter.setPen(QColor("#f1c40f"))  # gold star
            painter.drawText(left_margin, title_y, "★ ")
            star_width = QFontMetrics(title_font).horizontalAdvance("★ ")
            left_margin += star_width
            painter.setPen(cyan_col if is_selected else text_bright)

        # Space for badge on right
        badge_text = f" {family} "
        badge_font = QFont("Consolas", 8, QFont.Weight.Bold)
        fm_badge = QFontMetrics(badge_font)
        badge_w = fm_badge.horizontalAdvance(badge_text) + 12
        badge_h = 18

        max_title_w = rect.width() - (left_margin - rect.left()) - badge_w - 20
        elided_title = QFontMetrics(title_font).elidedText(name, Qt.TextElideMode.ElideRight, max_title_w)
        painter.drawText(left_margin, title_y, elided_title)

        # Subtitle line
        sub_font = QFont("Consolas", 8)
        painter.setFont(sub_font)
        painter.setPen(cyan_col if is_selected else text_dim)
        sub_y = rect.top() + 34
        max_sub_w = rect.width() - 24
        elided_sub = QFontMetrics(sub_font).elidedText(sub_info, Qt.TextElideMode.ElideRight, max_sub_w)
        painter.drawText(rect.left() + 12, sub_y, elided_sub)

        # Draw Pill Badge on top-right
        badge_rect = QRect(rect.right() - badge_w - 12, rect.top() + 6, badge_w, badge_h)

        fam_colors: dict[str, tuple[int, int, int]] = {
            "ESP32": (0, 210, 255),       # cyan
            "ESP32-S3": (0, 230, 180),    # teal
            "ESP32-C3": (52, 152, 219),   # blue
            "ESP32-S2": (155, 89, 182),   # purple
            "ESP32-CAM": (230, 126, 34),  # orange
            "ESP8266": (165, 105, 189),   # purple
            "AVR": (243, 156, 18),        # amber/orange
            "RP2040": (46, 204, 113),     # green
            "STM32": (41, 128, 185),      # dark blue
        }
        rgb = fam_colors.get(family, (120, 140, 160))
        badge_bg = QColor(rgb[0], rgb[1], rgb[2], 40 if not is_selected else 70)
        badge_border = QColor(rgb[0], rgb[1], rgb[2], 120 if not is_selected else 220)
        badge_fg = QColor(rgb[0], rgb[1], rgb[2])

        painter.setBrush(QBrush(badge_bg))
        painter.setPen(QPen(badge_border, 1))
        painter.drawRoundedRect(QRectF(badge_rect), 3.0, 3.0)

        painter.setFont(badge_font)
        painter.setPen(badge_fg)
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, family)

        painter.restore()


# ── Search Input Box with Arrow Key List Navigation ──────────────────────────

class _SearchLineEdit(QLineEdit):
    """QLineEdit with Up / Down Arrow intercept to seamlessly navigate the list widget."""

    def __init__(self, target_list: QListWidget, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._target_list = target_list

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            count = self._target_list.count()
            if count > 0:
                curr = self._target_list.currentRow()
                step = 1 if key == Qt.Key.Key_Down else -1
                next_row = curr + step if curr >= 0 else (0 if key == Qt.Key.Key_Down else count - 1)

                while 0 <= next_row < count:
                    item = self._target_list.item(next_row)
                    if item and (item.flags() & Qt.ItemFlag.ItemIsSelectable):
                        self._target_list.setCurrentRow(next_row)
                        self._target_list.scrollToItem(item)
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

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        current_board: str = "",
        board_list: Optional[Sequence[str]] = None,
        on_select_callback: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("🔍 Search & Select MCU Board")
        self.setModal(True)
        self.resize(620, 530)
        self.setMinimumSize(540, 460)

        self.on_select_callback = on_select_callback
        if board_list is not None:
            self.all_boards = list(board_list)
        else:
            self.all_boards = sorted(list(SUPPORTED_BOARDS.keys()))
        self.current_board = current_board or ""
        self.result_board: Optional[str] = None
        self._active_category = "ALL"

        # Load up to 5 valid recent boards with canonical name resolution
        board_canonical_map = {b.lower(): b for b in self.all_boards}
        resolved_recents: list[str] = []
        for b in load_recent_boards():
            canon = board_canonical_map.get(b.lower(), b)
            if canon and canon not in resolved_recents:
                resolved_recents.append(canon)
        self.recent_boards = resolved_recents[:5]

        # Build in-memory search index
        self._search_index = BoardSearchIndex(
            SUPPORTED_BOARDS,
            recent_boards=self.recent_boards,
        )

        self._build_ui()
        self._apply_dialog_theme()
        self._apply_filter("")

        # Connect live theme re-styling
        try:
            from main.qt.signals import signals
            signals.theme_changed.connect(self._apply_dialog_theme)
        except Exception:
            pass

        # Pre-select active board if present
        if self.current_board in self.all_boards:
            self._select_item_by_name(self.current_board)

        # Autofocus search entry immediately
        QTimer.singleShot(40, self.search_ent.setFocus)

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
            QListWidget {{
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

        self.lbl_hdr = QLabel("🔍 Search MCU Board")
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

        self.listbox = QListWidget()
        self._delegate = BoardListItemDelegate(self.listbox)
        self.listbox.setItemDelegate(self._delegate)

        self.search_ent = _SearchLineEdit(self.listbox)
        self.search_ent.setPlaceholderText("Search board, MCU, chip, or architecture (e.g. ESP32-S3, Uno R3, Nano 328, C3)...")
        self.search_ent.setClearButtonEnabled(True)
        self.search_ent.textChanged.connect(self._on_search_text_changed)
        self.search_ent.returnPressed.connect(self._confirm_selection)
        search_layout.addWidget(self.search_ent)
        root.addWidget(self.search_frame)

        # ── Category Quick Filter Chips ───────────────────────────────────────
        self.chips_frame = QFrame()
        chips_layout = QHBoxLayout(self.chips_frame)
        chips_layout.setContentsMargins(14, 2, 14, 6)
        chips_layout.setSpacing(6)

        self._chip_buttons: dict[str, QPushButton] = {}
        chip_specs = [
            ("ALL", "All"),
            ("ESP32", "ESP32"),
            ("AVR", "AVR"),
            ("ESP8266", "ESP8266"),
            ("RP2040", "RP2040"),
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
            chips_layout.addWidget(btn)

        chips_layout.addStretch()
        root.addWidget(self.chips_frame)

        # ── Listbox Container ─────────────────────────────────────────────────
        list_container = QWidget()
        list_v = QVBoxLayout(list_container)
        list_v.setContentsMargins(14, 4, 14, 8)
        list_v.addWidget(self.listbox)
        root.addWidget(list_container, stretch=1)

        self.listbox.itemDoubleClicked.connect(lambda item: self._confirm_selection())
        self.listbox.itemSelectionChanged.connect(self._update_select_button_state)

        # ── Action Buttons Footer ─────────────────────────────────────────────
        self.btn_frame = QFrame()
        btn_layout = QHBoxLayout(self.btn_frame)
        btn_layout.setContentsMargins(14, 10, 14, 12)
        btn_layout.setSpacing(8)

        self.lbl_count = QLabel(f"{len(self.all_boards)} boards available")
        btn_layout.addWidget(self.lbl_count)

        btn_layout.addStretch()

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setFixedSize(85, 30)
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_cancel)

        self.btn_select = QPushButton("Select Board")
        self.btn_select.setFixedSize(110, 30)
        self.btn_select.setEnabled(False)
        self.btn_select.setCursor(Qt.CursorShape.ArrowCursor)
        self.btn_select.clicked.connect(self._confirm_selection)
        btn_layout.addWidget(self.btn_select)

        root.addWidget(self.btn_frame)

        # ── Shortcuts ─────────────────────────────────────────────────────────
        QShortcut(QKeySequence("Escape"), self, activated=self._on_escape_pressed)
        QShortcut(QKeySequence("Return"), self, activated=self._confirm_selection)
        QShortcut(QKeySequence("Enter"), self, activated=self._confirm_selection)

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
        self._apply_filter(text)

    def _on_escape_pressed(self) -> None:
        if self.search_ent.text():
            self.search_ent.clear()
        else:
            self.reject()

    def _populate_list(self, items: list[str], separator_after: int = -1) -> None:
        self.listbox.clear()

        # Build fast metadata lookup from index
        index_by_name = {item["name"]: item for item in self._search_index.index}

        if separator_after > 0:
            rec_hdr = QListWidgetItem("RECENTLY USED BOARDS")
            rec_hdr.setFlags(Qt.ItemFlag.NoItemFlags)
            rec_hdr.setData(Qt.ItemDataRole.UserRole + 1, "header")
            self.listbox.addItem(rec_hdr)

        for idx, name in enumerate(items):
            if idx == separator_after:
                sep = QListWidgetItem("─────────────────────────────────────────────")
                sep.setFlags(Qt.ItemFlag.NoItemFlags)
                sep.setData(Qt.ItemDataRole.UserRole + 1, "header")
                self.listbox.addItem(sep)

                all_hdr = QListWidgetItem("ALL BOARDS")
                all_hdr.setFlags(Qt.ItemFlag.NoItemFlags)
                all_hdr.setData(Qt.ItemDataRole.UserRole + 1, "header")
                self.listbox.addItem(all_hdr)

            item = QListWidgetItem()
            item.setText(name)
            item.setData(Qt.ItemDataRole.UserRole, name)

            meta = index_by_name.get(name, {})
            family = meta.get("display_family", "MCU")
            sub_info = meta.get("sub_info", "")
            is_rec = name in self.recent_boards

            item.setData(Qt.ItemDataRole.UserRole + 2, family)
            item.setData(Qt.ItemDataRole.UserRole + 3, sub_info)
            item.setData(Qt.ItemDataRole.UserRole + 4, is_rec)

            self.listbox.addItem(item)

        # Update counter
        cat_suffix = f" [{self._active_category}]" if self._active_category != "ALL" else ""
        self.lbl_count.setText(f"{len(items)} of {len(self.all_boards)} boards{cat_suffix}")
        self._update_select_button_state()

    def _update_select_button_state(self) -> None:
        curr = self.listbox.currentItem()
        is_sel = bool(curr and (curr.flags() & Qt.ItemFlag.ItemIsSelectable))
        self.btn_select.setEnabled(is_sel)
        self.btn_select.setCursor(Qt.CursorShape.PointingHandCursor if is_sel else Qt.CursorShape.ArrowCursor)

    def _select_item_by_name(self, name: str) -> None:
        for idx in range(self.listbox.count()):
            item = self.listbox.item(idx)
            if item and (item.flags() & Qt.ItemFlag.ItemIsSelectable):
                val = item.data(Qt.ItemDataRole.UserRole) or item.text()
                if val == name or val.replace("★", "").strip() == name:
                    self.listbox.setCurrentRow(idx)
                    self.listbox.scrollToItem(item)
                    break

    def _apply_filter(self, query: str) -> None:
        q = (query or "").strip()
        separator_after = -1

        if not q and self._active_category == "ALL":
            # Show recently used pinned at top, then the rest
            recent_set = set(self.recent_boards)
            rest = [b for b in self.all_boards if b not in recent_set]
            if self.recent_boards:
                matches = list(self.recent_boards) + rest
                separator_after = len(self.recent_boards)
            else:
                matches = list(self.all_boards)
        else:
            matches = self._search_index.search(q, category_filter=self._active_category)

        self._populate_list(matches, separator_after=separator_after)

        # Highlight first selectable match automatically for instant Enter-key selection
        if matches:
            for i in range(self.listbox.count()):
                item = self.listbox.item(i)
                if item and (item.flags() & Qt.ItemFlag.ItemIsSelectable):
                    self.listbox.setCurrentRow(i)
                    break

    def _confirm_selection(self) -> None:
        curr = self.listbox.currentItem()
        if curr and (curr.flags() & Qt.ItemFlag.ItemIsSelectable):
            raw_name = curr.data(Qt.ItemDataRole.UserRole)
            if not raw_name:
                raw_name = curr.text().replace("★", "").replace("⚡", "").strip()
            self.result_board = raw_name
            add_recent_board(self.result_board)
            if self.on_select_callback:
                self.on_select_callback(self.result_board)
            self.accept()
        else:
            self.reject()

    def selected_board(self) -> Optional[str]:
        return self.result_board


__all__ = ["BoardSearchDialog"]
