#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.owner_ticket_dialog — Private Owner Issue Ticket Dialog.

Ultra-clean, modern glassmorphic developer portal for defect tracking, field issues,
and feature bugs. Backed by OwnerTicketService (local cache + Firebase REST).
Features authentic translucent glass cards, atmospheric ambient light mesh orbs,
official MCU Flasher chip emblem, unified vector visibility icons, and rephrased labels.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, List, Dict, Any

from PySide6.QtCore import Qt, QSize, QTimer, Signal, QPointF
from PySide6.QtGui import (
    QColor, QFont, QCursor, QGuiApplication, QIcon, QPixmap,
    QPainter, QRadialGradient, QLinearGradient, QBrush, QPen
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QStackedWidget,
    QLabel,
    QLineEdit,
    QTextEdit,
    QPushButton,
    QComboBox,
    QScrollArea,
    QFrame,
    QMessageBox,
    QSizePolicy,
    QGraphicsDropShadowEffect,
    QMenu,
)

from main.core.owner_tickets import OwnerTicketService, is_internet_available

# Dynamic project assets resolution
_this_file = Path(__file__).resolve()
_project_root = _this_file.parent.parent.parent
_assets_dir = _project_root / "src" / "assets"
_mcu_icon_path = _assets_dir / "mcu_icon.ico"
_eye_icon_path = _assets_dir / "icons" / "eye.svg"
_eye_slash_icon_path = _assets_dir / "icons" / "eye_slash.svg"


class TicketCard(QFrame):
    """Interactive card representing one defect issue ticket with glass styling."""

    status_changed = Signal(str, str)  # (ticket_id, new_status)
    deleted = Signal(str)             # (ticket_id)
    edit_requested = Signal(dict)     # (ticket_data)

    SEVERITY_COLORS = {
        "Critical": ("#ff4d4f", "rgba(255, 77, 79, 0.16)", "#ff4d4f"),
        "High":     ("#fa8c16", "rgba(250, 140, 22, 0.16)", "#fa8c16"),
        "Medium":   ("#faad14", "rgba(250, 173, 20, 0.16)", "#faad14"),
        "Low":      ("#38bdf8", "rgba(56, 189, 248, 0.16)", "#38bdf8"),
    }

    STATUS_COLORS = {
        "Open":        ("#00e5ff", "rgba(0, 229, 255, 0.12)"),
        "In Progress": ("#faad14", "rgba(250, 173, 20, 0.12)"),
        "Resolved":    ("#10b981", "rgba(16, 185, 129, 0.16)"),
        "Closed":      ("#8c8c8c", "rgba(140, 140, 140, 0.12)"),
    }

    def __init__(self, ticket: Dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.ticket = ticket
        self._ticket_id = ticket.get("id", "")
        try:
            self._setup_ui()
        except Exception:
            pass

    def _setup_ui(self) -> None:
        self.setObjectName("ticket-card")
        self.setStyleSheet("""
            QFrame#ticket-card {
                background-color: rgba(16, 24, 40, 0.65);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-top: 1px solid rgba(255, 255, 255, 0.22);
                border-radius: 14px;
            }
            QFrame#ticket-card:hover {
                background-color: rgba(22, 34, 54, 0.82);
                border-color: rgba(0, 210, 255, 0.35);
                border-top: 1px solid rgba(0, 210, 255, 0.70);
            }
            QLabel {
                background: transparent;
                border: none;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)

        # ── Header row: Severity, Status, Category, Date, Delete ────────────
        header = QHBoxLayout()
        header.setSpacing(10)

        # Severity Pill
        sev = self.ticket.get("severity", "Medium")
        s_fg, s_bg, s_bd = self.SEVERITY_COLORS.get(sev, ("#faad14", "rgba(250, 173, 20, 0.16)", "#faad14"))
        sev_lbl = QLabel(f"● {sev}")
        sev_lbl.setStyleSheet(f"""
            color: {s_fg};
            background-color: {s_bg};
            border: 1px solid {s_bd};
            border-radius: 11px;
            padding: 3px 11px;
            font-size: 11px;
            font-weight: 700;
        """)
        header.addWidget(sev_lbl)

        # Status Combo Pill
        status = self.ticket.get("status", "Open")
        self.status_cb = QComboBox()
        self.status_cb.addItems(["Open", "In Progress", "Resolved", "Closed"])
        self.status_cb.setCurrentText(status)
        self.status_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_status_style(status)
        self.status_cb.currentTextChanged.connect(self._on_status_change)
        header.addWidget(self.status_cb)

        # Category Pill
        cat = self.ticket.get("category", "General")
        cat_lbl = QLabel(cat)
        cat_lbl.setStyleSheet("""
            color: #94a3b8;
            background-color: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.09);
            border-radius: 11px;
            padding: 3px 11px;
            font-size: 11px;
            font-family: 'Consolas', monospace;
        """)
        header.addWidget(cat_lbl)

        header.addStretch(1)

        # Date Label
        created = self.ticket.get("created_at", "")
        updated = self.ticket.get("updated_at", "")
        if updated and updated != created:
            date_lbl = QLabel(f"{created}  (edited)")
            date_lbl.setToolTip(f"Created: {created}\nLast modified: {updated}")
        else:
            date_lbl = QLabel(created)
            date_lbl.setToolTip(f"Created: {created}")
        date_lbl.setStyleSheet("color: #64748b; font-size: 11px; font-family: 'Consolas', monospace;")
        header.addWidget(date_lbl)

        # Edit Action
        btn_edit = QPushButton("✎")
        btn_edit.setToolTip("Edit defect ticket (Title, Category, Severity, Status, Notes)")
        btn_edit.setFixedSize(26, 26)
        btn_edit.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_edit.setStyleSheet("""
            QPushButton {
                color: #8fa1b3;
                background: transparent;
                border: none;
                border-radius: 13px;
                font-weight: bold;
                font-size: 13px;
            }
            QPushButton:hover {
                color: #00e5ff;
                background: rgba(0, 229, 255, 0.20);
            }
        """)
        btn_edit.clicked.connect(self._on_edit)
        header.addWidget(btn_edit)

        # Delete Action
        btn_del = QPushButton("✕")
        btn_del.setToolTip("Delete this defect ticket")
        btn_del.setFixedSize(26, 26)
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.setStyleSheet("""
            QPushButton {
                color: #64748b;
                background: transparent;
                border: none;
                border-radius: 13px;
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover {
                color: #ff4d4f;
                background: rgba(255, 77, 79, 0.20);
            }
        """)
        btn_del.clicked.connect(self._on_delete)
        header.addWidget(btn_del)

        layout.addLayout(header)

        # ── Title ────────────────────────────────────────────────────────────
        title_lbl = QLabel(self.ticket.get("title", "Untitled Defect"))
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet("""
            color: #ffffff;
            font-size: 14.5px;
            font-weight: 700;
            background: transparent;
            border: none;
        """)
        layout.addWidget(title_lbl)

        # ── Description ──────────────────────────────────────────────────────
        desc = (self.ticket.get("description") or "").strip()
        if desc:
            desc_frame = QFrame()
            desc_frame.setObjectName("desc-frame")
            desc_frame.setStyleSheet("""
                QFrame#desc-frame {
                    background-color: rgba(9, 13, 22, 0.65);
                    border: 1px solid rgba(255, 255, 255, 0.06);
                    border-radius: 8px;
                }
            """)
            d_lay = QVBoxLayout(desc_frame)
            d_lay.setContentsMargins(12, 10, 12, 10)
            desc_lbl = QLabel(desc)
            desc_lbl.setWordWrap(True)
            desc_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            desc_lbl.setStyleSheet("""
                color: #94a3b8;
                font-size: 12px;
                line-height: 1.5;
                background: transparent;
                border: none;
                font-family: 'Consolas', 'Segoe UI', monospace;
            """)
            d_lay.addWidget(desc_lbl)
            layout.addWidget(desc_frame)

    def _update_status_style(self, status: str) -> None:
        try:
            c_fg, c_bg = self.STATUS_COLORS.get(status, ("#00e5ff", "rgba(0, 229, 255, 0.12)"))
            self.status_cb.setStyleSheet(f"""
                QComboBox {{
                    color: {c_fg};
                    background-color: {c_bg};
                    border: 1px solid {c_fg};
                    border-radius: 11px;
                    padding: 3px 20px 3px 10px;
                    font-size: 11px;
                    font-weight: 700;
                }}
                QComboBox::drop-down {{
                    subcontrol-origin: padding;
                    subcontrol-position: top right;
                    width: 16px;
                    border: none;
                }}
                QComboBox QAbstractItemView {{
                    background-color: #0c121d;
                    color: #e2e8f0;
                    selection-background-color: #1a2538;
                    border: 1px solid rgba(0, 210, 255, 0.3);
                    border-radius: 6px;
                }}
            """)
        except Exception:
            pass

    def _on_status_change(self, new_status: str) -> None:
        try:
            self._update_status_style(new_status)
            self.status_changed.emit(self._ticket_id, new_status)
        except Exception:
            pass

    def _on_delete(self) -> None:
        try:
            res = QMessageBox.question(
                self,
                "Delete Ticket",
                f"Are you sure you want to delete ticket:\n\n\"{self.ticket.get('title')}\"?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if res == QMessageBox.StandardButton.Yes:
                self.deleted.emit(self._ticket_id)
        except Exception:
            pass

    def _on_edit(self) -> None:
        try:
            self.edit_requested.emit(self.ticket)
        except Exception:
            pass

    def mouseDoubleClickEvent(self, event) -> None:
        try:
            self._on_edit()
            event.accept()
        except Exception:
            super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event) -> None:
        try:
            menu = QMenu(self)
            menu.setStyleSheet("""
                QMenu {
                    background-color: #0c121d;
                    color: #e2e8f0;
                    border: 1px solid rgba(0, 210, 255, 0.35);
                    border-radius: 8px;
                    padding: 4px;
                }
                QMenu::item {
                    padding: 6px 18px;
                    border-radius: 4px;
                    font-size: 12px;
                }
                QMenu::item:selected {
                    background-color: rgba(0, 210, 255, 0.20);
                    color: #00e5ff;
                }
            """)
            act_edit = menu.addAction("✎  Edit Ticket...")
            act_del = menu.addAction("✕  Delete Ticket")
            chosen = menu.exec(event.globalPos())
            if chosen == act_edit:
                self._on_edit()
            elif chosen == act_del:
                self._on_delete()
        except Exception:
            pass


class EditTicketDialog(QDialog):
    """Frosted Glass Modal for Editing Defect Report Details."""

    def __init__(self, ticket: Dict[str, Any], service: OwnerTicketService, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.ticket = ticket
        self.service = service
        self._ticket_id = ticket.get("id", "")

        self.setObjectName("edit-ticket-dialog")
        self.setWindowTitle("⚡ MCU Flasher — Edit Defect Report")
        if _mcu_icon_path.exists():
            try:
                self.setWindowIcon(QIcon(str(_mcu_icon_path)))
            except Exception:
                pass

        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.resize(620, 520)
        self.setMinimumSize(560, 460)

        self._build_ui()

    def paintEvent(self, event) -> None:
        """Paint dynamic ambient glassmorphic light mesh behind translucent surfaces."""
        try:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)

            w, h = self.width(), self.height()

            # Base deep obsidian background
            painter.fillRect(0, 0, w, h, QColor("#06090f"))

            # Ambient Light Orb 1 (Top-Right Electric Cyan Glow)
            g1 = QRadialGradient(QPointF(w * 0.85, h * 0.12), w * 0.50)
            g1.setColorAt(0.0, QColor(0, 210, 255, 30))
            g1.setColorAt(0.50, QColor(0, 130, 210, 10))
            g1.setColorAt(1.0, QColor(6, 9, 15, 0))
            painter.fillRect(0, 0, w, h, QBrush(g1))

            # Ambient Light Orb 2 (Bottom-Left Deep Indigo Glow)
            g2 = QRadialGradient(QPointF(w * 0.15, h * 0.88), w * 0.50)
            g2.setColorAt(0.0, QColor(99, 102, 241, 24))
            g2.setColorAt(0.50, QColor(49, 46, 129, 8))
            g2.setColorAt(1.0, QColor(6, 9, 15, 0))
            painter.fillRect(0, 0, w, h, QBrush(g2))
        except Exception:
            super().paintEvent(event)

    def keyPressEvent(self, event) -> None:
        try:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    self._do_save()
                    event.accept()
                    return
            elif event.key() == Qt.Key.Key_Escape:
                self.reject()
                event.accept()
                return
        except Exception:
            pass
        super().keyPressEvent(event)

    def _build_ui(self) -> None:
        self.setStyleSheet("""
            QDialog#edit-ticket-dialog {
                background: #06090f;
                color: #e2e8f0;
                font-family: 'Segoe UI', sans-serif;
            }
            QLabel {
                color: #e2e8f0;
                background: transparent;
                border: none;
            }
            QLineEdit, QComboBox, QTextEdit {
                background-color: rgba(14, 21, 35, 0.70);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 8px;
                padding: 8px 12px;
                color: #ffffff;
                font-size: 12px;
            }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus {
                border-color: #00e5ff;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 20px;
                border: none;
            }
            QComboBox QAbstractItemView {
                background-color: #0c121d;
                color: #e2e8f0;
                selection-background-color: #1a2538;
                border: 1px solid rgba(0, 210, 255, 0.3);
                border-radius: 6px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        # ── Header ───────────────────────────────────────────────────────────
        header_lay = QHBoxLayout()
        header_lay.setSpacing(10)

        title_lbl = QLabel("✎  Edit Defect Report")
        title_lbl.setStyleSheet("color: #00e5ff; font-size: 16px; font-weight: 800; letter-spacing: -0.2px;")
        header_lay.addWidget(title_lbl)

        header_lay.addStretch(1)

        t_id = self._ticket_id or "tkt_unknown"
        id_badge = QLabel(f"ID: {t_id}")
        id_badge.setStyleSheet("""
            color: #64748b;
            font-size: 11px;
            font-family: 'Consolas', monospace;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 6px;
            padding: 3px 8px;
        """)
        header_lay.addWidget(id_badge)
        layout.addLayout(header_lay)

        # Meta info row: created & updated
        created = self.ticket.get("created_at", "")
        updated = self.ticket.get("updated_at", "")
        meta_str = f"Created: {created}" if created else ""
        if updated and updated != created:
            meta_str += f"  •  Last modified: {updated}"
        if meta_str:
            meta_lbl = QLabel(meta_str)
            meta_lbl.setStyleSheet("color: #64748b; font-size: 11px; font-family: 'Consolas', monospace;")
            layout.addWidget(meta_lbl)

        # ── Form Card ────────────────────────────────────────────────────────
        form_frame = QFrame()
        form_frame.setStyleSheet("""
            QFrame {
                background-color: rgba(14, 21, 35, 0.50);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-top: 1px solid rgba(255, 255, 255, 0.16);
                border-radius: 12px;
            }
        """)
        fl = QVBoxLayout(form_frame)
        fl.setContentsMargins(18, 16, 18, 16)
        fl.setSpacing(12)

        # Field: Title
        lbl_t = QLabel("DEFECT TITLE")
        lbl_t.setStyleSheet("color: #8fa1b3; font-size: 10px; font-weight: 700; letter-spacing: 0.6px;")
        fl.addWidget(lbl_t)

        self.txt_title = QLineEdit()
        self.txt_title.setText(self.ticket.get("title", ""))
        self.txt_title.setPlaceholderText("Brief concise summary of the issue...")
        self.txt_title.returnPressed.connect(lambda: self.txt_desc.setFocus())
        fl.addWidget(self.txt_title)

        # Field: Grid of Category, Severity, Status
        r_meta = QHBoxLayout()
        r_meta.setSpacing(12)

        # Category
        col_cat = QVBoxLayout()
        col_cat.setSpacing(4)
        lbl_c = QLabel("CATEGORY")
        lbl_c.setStyleSheet("color: #8fa1b3; font-size: 10px; font-weight: 700; letter-spacing: 0.6px;")
        col_cat.addWidget(lbl_c)

        self.cb_category = QComboBox()
        categories = [
            "GUI / Interface",
            "Monaco Code Editor",
            "Serial Monitor",
            "PlatformIO / Toolchain",
            "Hardware & COM Port",
            "Low-End & HDD",
            "General Defect",
        ]
        cur_cat = self.ticket.get("category", "General Defect")
        if cur_cat and cur_cat not in categories:
            categories.append(cur_cat)
        self.cb_category.addItems(categories)
        self.cb_category.setCurrentText(cur_cat)
        col_cat.addWidget(self.cb_category)
        r_meta.addLayout(col_cat, stretch=4)

        # Severity
        col_sev = QVBoxLayout()
        col_sev.setSpacing(4)
        lbl_s = QLabel("SEVERITY")
        lbl_s.setStyleSheet("color: #8fa1b3; font-size: 10px; font-weight: 700; letter-spacing: 0.6px;")
        col_sev.addWidget(lbl_s)

        self.cb_severity = QComboBox()
        self.cb_severity.addItems(["Critical", "High", "Medium", "Low"])
        cur_sev = self.ticket.get("severity", "Medium")
        self.cb_severity.setCurrentText(cur_sev)
        col_sev.addWidget(self.cb_severity)
        r_meta.addLayout(col_sev, stretch=3)

        # Status
        col_st = QVBoxLayout()
        col_st.setSpacing(4)
        lbl_st = QLabel("STATUS")
        lbl_st.setStyleSheet("color: #8fa1b3; font-size: 10px; font-weight: 700; letter-spacing: 0.6px;")
        col_st.addWidget(lbl_st)

        self.cb_status = QComboBox()
        self.cb_status.addItems(["Open", "In Progress", "Resolved", "Closed"])
        cur_st = self.ticket.get("status", "Open")
        self.cb_status.setCurrentText(cur_st)
        col_st.addWidget(self.cb_status)
        r_meta.addLayout(col_st, stretch=3)

        fl.addLayout(r_meta)

        # Field: Description
        lbl_d = QLabel("DESCRIPTION & NOTES")
        lbl_d.setStyleSheet("color: #8fa1b3; font-size: 10px; font-weight: 700; letter-spacing: 0.6px;")
        fl.addWidget(lbl_d)

        self.txt_desc = QTextEdit()
        self.txt_desc.setPlainText(self.ticket.get("description", ""))
        self.txt_desc.setPlaceholderText("Detailed notes, steps to reproduce, observations, or resolution details...")
        self.txt_desc.setStyleSheet("""
            QTextEdit {
                background-color: rgba(9, 14, 22, 0.75);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 8px;
                padding: 8px 12px;
                color: #ffffff;
                font-size: 12px;
                font-family: 'Consolas', 'Segoe UI', monospace;
            }
            QTextEdit:focus {
                border-color: #00e5ff;
            }
        """)
        fl.addWidget(self.txt_desc, stretch=1)

        layout.addWidget(form_frame, stretch=1)

        # ── Bottom Action Row ────────────────────────────────────────────────
        bot_lay = QHBoxLayout()
        bot_lay.setSpacing(10)

        hint_lbl = QLabel("Tip: Press Ctrl+Enter to save")
        hint_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        bot_lay.addWidget(hint_lbl)

        bot_lay.addStretch(1)

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setFixedHeight(34)
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.setStyleSheet("""
            QPushButton {
                background: rgba(255, 255, 255, 0.05);
                color: #94a3b8;
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 8px;
                padding: 0 16px;
                font-size: 12px;
            }
            QPushButton:hover {
                color: #ffffff;
                background: rgba(255, 255, 255, 0.10);
            }
        """)
        btn_cancel.clicked.connect(self.reject)
        bot_lay.addWidget(btn_cancel)

        btn_save = QPushButton("💾  Save Changes")
        btn_save.setFixedHeight(34)
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0088cc, stop:1 #00e5ff);
                color: #040810;
                font-weight: 800;
                font-size: 12.5px;
                border: none;
                border-radius: 8px;
                padding: 0 18px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #009ce8, stop:1 #4de9ff);
            }
        """)
        btn_save.clicked.connect(self._do_save)
        bot_lay.addWidget(btn_save)

        layout.addLayout(bot_lay)

    def _do_save(self) -> None:
        title = self.txt_title.text().strip()
        if not title:
            QMessageBox.warning(self, "Validation", "Ticket title cannot be blank.")
            self.txt_title.setFocus()
            return

        cat = self.cb_category.currentText()
        sev = self.cb_severity.currentText()
        st = self.cb_status.currentText()
        desc = self.txt_desc.toPlainText().strip()

        updates = {
            "title": title,
            "category": cat,
            "severity": sev,
            "status": st,
            "description": desc,
        }

        try:
            ok = self.service.update_ticket(self._ticket_id, updates)
            if ok:
                self.accept()
            else:
                QMessageBox.critical(self, "Save Error", "Failed to update defect report in database.")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"An error occurred while updating the ticket:\n{e}")


class OwnerTicketDialog(QDialog):
    """Private Owner Defect & Issue Tracker Portal with Authentic Glassmorphic Aesthetics."""

    def __init__(self, backend=None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._backend = backend
        self.service = OwnerTicketService()
        self._active_filter = "All Status"
        self._search_query = ""

        self.setObjectName("owner-portal-dialog")
        self.setWindowTitle("⚡ MCU Flasher — Defect Portal")
        if _mcu_icon_path.exists():
            try:
                self.setWindowIcon(QIcon(str(_mcu_icon_path)))
            except Exception:
                pass

        self._setup_window_geometry()
        self._build_ui()

    def _setup_window_geometry(self) -> None:
        try:
            screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            avail = screen.availableGeometry() if screen else None
            avail_w = avail.width() if avail else 1280
            avail_h = avail.height() if avail else 720

            target_w = min(880, int(avail_w * 0.90))
            target_h = min(740, int(avail_h * 0.88))
            self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMinMaxButtonsHint)
            self.resize(max(700, target_w), max(560, target_h))
            self.setMinimumSize(660, 520)

            if avail:
                self.move(
                    avail.x() + max(0, (avail_w - target_w) // 2),
                    avail.y() + max(0, (avail_h - target_h) // 2),
                )
        except Exception:
            pass

    def paintEvent(self, event) -> None:
        """Paint dynamic ambient glassmorphic light mesh behind translucent surfaces."""
        try:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)

            w, h = self.width(), self.height()

            # Base deep obsidian background
            painter.fillRect(0, 0, w, h, QColor("#06090f"))

            # Ambient Light Orb 1 (Top-Right Electric Cyan Glow)
            g1 = QRadialGradient(QPointF(w * 0.82, h * 0.12), w * 0.55)
            g1.setColorAt(0.0, QColor(0, 210, 255, 42))
            g1.setColorAt(0.45, QColor(0, 130, 210, 18))
            g1.setColorAt(0.80, QColor(0, 50, 120, 5))
            g1.setColorAt(1.0, QColor(6, 9, 15, 0))
            painter.fillRect(0, 0, w, h, QBrush(g1))

            # Ambient Light Orb 2 (Bottom-Left Deep Sapphire / Indigo Aura)
            g2 = QRadialGradient(QPointF(w * 0.12, h * 0.84), w * 0.60)
            g2.setColorAt(0.0, QColor(99, 102, 241, 32))
            g2.setColorAt(0.50, QColor(49, 46, 129, 14))
            g2.setColorAt(1.0, QColor(6, 9, 15, 0))
            painter.fillRect(0, 0, w, h, QBrush(g2))

            # Ambient Light Orb 3 (Center Cyan Sheen for Auth Card Focus)
            if self.stack.currentIndex() == 0:
                g3 = QRadialGradient(QPointF(w * 0.5, h * 0.44), w * 0.40)
                g3.setColorAt(0.0, QColor(0, 229, 255, 24))
                g3.setColorAt(0.60, QColor(0, 110, 190, 7))
                g3.setColorAt(1.0, QColor(6, 9, 15, 0))
                painter.fillRect(0, 0, w, h, QBrush(g3))
            else:
                # Dashboard subtle top glow
                g4 = QRadialGradient(QPointF(w * 0.5, h * 0.08), w * 0.45)
                g4.setColorAt(0.0, QColor(0, 210, 255, 16))
                g4.setColorAt(0.70, QColor(0, 80, 160, 4))
                g4.setColorAt(1.0, QColor(6, 9, 15, 0))
                painter.fillRect(0, 0, w, h, QBrush(g4))
        except Exception:
            super().paintEvent(event)

    def keyPressEvent(self, event) -> None:
        """Prevent QDialog from vanishing/closing when Enter is pressed."""
        try:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self.stack.currentIndex() == 0:
                    self._do_authenticate()
                    event.accept()
                    return
                # On dashboard, keep dialog open on Enter (e.g. search or editing)
                event.accept()
                return
            elif key == Qt.Key.Key_Escape:
                self.close()
                event.accept()
                return
        except Exception:
            pass
        super().keyPressEvent(event)

    def _build_ui(self) -> None:
        # ── Global Clean Glassmorphic Styling ────────────────────────────────
        self.setStyleSheet("""
            QDialog#owner-portal-dialog {
                background: #06090f;
                color: #e2e8f0;
                font-family: 'Segoe UI', sans-serif;
            }
            QLabel {
                color: #e2e8f0;
                background: transparent;
                border: none;
                padding: 0px;
            }
            QScrollBar:vertical {
                background: rgba(6, 9, 15, 0.40);
                width: 8px;
                border: none;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: rgba(0, 210, 255, 0.25);
                border-radius: 4px;
                min-height: 24px;
            }
            QScrollBar::handle:vertical:hover {
                background: #00d2ff;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Frosted Top Bar ──────────────────────────────────────────────────
        top_bar = QFrame()
        top_bar.setObjectName("top-bar")
        top_bar.setFixedHeight(52)
        top_bar.setStyleSheet("""
            QFrame#top-bar {
                background-color: rgba(10, 15, 26, 0.65);
                border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            }
        """)
        tb_layout = QHBoxLayout(top_bar)
        tb_layout.setContentsMargins(20, 0, 18, 0)
        tb_layout.setSpacing(12)

        # Glowing Title Badge
        title_badge = QLabel("⚡  DEFECT PORTAL")
        title_badge.setStyleSheet("""
            color: #00e5ff;
            font-size: 13.5px;
            font-weight: 800;
            letter-spacing: 0.8px;
            background: transparent;
        """)
        tb_layout.addWidget(title_badge, alignment=Qt.AlignmentFlag.AlignVCenter)

        # Live Status Capsule (Compact, centered pill)
        self.cloud_badge = QLabel("● Offline Cache")
        self.cloud_badge.setFixedHeight(24)
        self.cloud_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cloud_badge.setStyleSheet("""
            QLabel {
                color: #94a3b8;
                font-size: 11px;
                font-weight: 600;
                font-family: 'Consolas', monospace;
                background: rgba(255, 255, 255, 0.05);
                padding: 0 10px;
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.10);
            }
        """)
        tb_layout.addWidget(self.cloud_badge, alignment=Qt.AlignmentFlag.AlignVCenter)

        tb_layout.addStretch(1)

        # Minimalist Close Button
        btn_close = QPushButton("✕")
        btn_close.setAutoDefault(False)
        btn_close.setDefault(False)
        btn_close.setFixedSize(28, 28)
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setStyleSheet("""
            QPushButton {
                color: #94a3b8;
                background: transparent;
                border: none;
                border-radius: 14px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                color: #ff6b6b;
                background: rgba(255, 77, 79, 0.20);
            }
        """)
        btn_close.clicked.connect(self.close)
        tb_layout.addWidget(btn_close, alignment=Qt.AlignmentFlag.AlignVCenter)

        main_layout.addWidget(top_bar)

        # ── Stacked Central Views (0: Login, 1: Dashboard) ───────────────────
        self.stack = QStackedWidget()
        self._build_auth_screen()
        self._build_dashboard_screen()
        main_layout.addWidget(self.stack)

        self._update_cloud_badge()

    # ─────────────────────────────────────────────────────────────────────────
    # Screen 0: Glassmorphism Authentication Gate
    # ─────────────────────────────────────────────────────────────────────────

    def _build_auth_screen(self) -> None:
        auth_widget = QWidget()
        layout = QVBoxLayout(auth_widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Floating Frosted Glass Login Card
        card = QFrame()
        card.setObjectName("glass-auth-card")
        card.setFixedWidth(420)
        card.setStyleSheet("""
            QFrame#glass-auth-card {
                background-color: rgba(16, 24, 40, 0.68);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-top: 1px solid rgba(255, 255, 255, 0.28);
                border-left: 1px solid rgba(255, 255, 255, 0.16);
                border-radius: 20px;
            }
            QLabel {
                background: transparent;
                border: none;
                padding: 0px;
            }
        """)

        # Ambient Cyan Glow behind the glass card
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(50)
        shadow.setColor(QColor(0, 210, 255, 28))
        shadow.setOffset(0, 10)
        card.setGraphicsEffect(shadow)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(32, 34, 32, 34)
        card_layout.setSpacing(12)

        # Official MCU Emblem in Frosted Glass Aura
        emblem_wrap = QHBoxLayout()
        emblem_wrap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        emblem = QFrame()
        emblem.setObjectName("icon-badge")
        emblem.setFixedSize(70, 70)
        emblem.setStyleSheet("""
            QFrame#icon-badge {
                background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,
                    stop:0 rgba(0, 229, 255, 0.24),
                    stop:0.75 rgba(0, 150, 220, 0.08),
                    stop:1 transparent);
                border: 1px solid rgba(0, 229, 255, 0.35);
                border-top: 1px solid rgba(255, 255, 255, 0.45);
                border-radius: 35px;
            }
        """)
        e_layout = QVBoxLayout(emblem)
        e_layout.setContentsMargins(0, 0, 0, 0)
        e_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon_lbl = QLabel()
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if _mcu_icon_path.exists():
            try:
                pix = QIcon(str(_mcu_icon_path)).pixmap(36, 36)
                icon_lbl.setPixmap(pix)
            except Exception:
                icon_lbl.setText("⚡")
                icon_lbl.setStyleSheet("font-size: 26px; color: #00e5ff;")
        else:
            icon_lbl.setText("⚡")
            icon_lbl.setStyleSheet("font-size: 26px; color: #00e5ff;")
        e_layout.addWidget(icon_lbl)

        emblem_wrap.addWidget(emblem)
        card_layout.addLayout(emblem_wrap)

        # Title & Subtitle (Clean & Rephrased)
        auth_title = QLabel("Developer Access")
        auth_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        auth_title.setStyleSheet("font-size: 20px; font-weight: 800; color: #ffffff; letter-spacing: -0.3px;")
        card_layout.addWidget(auth_title)

        auth_desc = QLabel("Sign in to access private defect tracking and telemetry.")
        auth_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        auth_desc.setWordWrap(True)
        auth_desc.setStyleSheet("font-size: 12px; color: #94a3b8; line-height: 1.45;")
        card_layout.addWidget(auth_desc)

        card_layout.addSpacing(6)

        # Section Label 1: Rephrased
        lbl_em_tag = QLabel("DEVELOPER ID / EMAIL")
        lbl_em_tag.setStyleSheet("color: #8fa1b3; font-size: 10px; font-weight: 700; letter-spacing: 0.6px;")
        card_layout.addWidget(lbl_em_tag)

        # Glass Email Input: Practical developer ID / email placeholder
        self.txt_auth_email = QLineEdit()
        self.txt_auth_email.setObjectName("glass-input")
        self.txt_auth_email.setPlaceholderText("Enter developer ID or email")
        self.txt_auth_email.setText(self.service.get_config().get("owner_email", ""))
        self.txt_auth_email.returnPressed.connect(self._on_auth_email_return)
        self.txt_auth_email.setStyleSheet("""
            QLineEdit#glass-input {
                background-color: rgba(9, 14, 24, 0.65);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-top: 1px solid rgba(255, 255, 255, 0.18);
                border-radius: 10px;
                padding: 10px 14px;
                font-size: 13px;
                color: #f8fafc;
                selection-background-color: #00d2ff;
                selection-color: #05070a;
            }
            QLineEdit#glass-input:focus {
                border: 1px solid #00e5ff;
                background-color: rgba(12, 19, 32, 0.88);
            }
            QLineEdit#glass-input:hover {
                border-color: rgba(0, 210, 255, 0.40);
            }
        """)
        card_layout.addWidget(self.txt_auth_email)

        # Section Label 2: Rephrased
        lbl_pwd_tag = QLabel("ACCESS KEY")
        lbl_pwd_tag.setStyleSheet("color: #8fa1b3; font-size: 10px; font-weight: 700; letter-spacing: 0.6px;")
        card_layout.addWidget(lbl_pwd_tag)

        # Glass Password Input with Unified Vector Visibility Toggle
        pwd_box = QHBoxLayout()
        pwd_box.setSpacing(8)
        self.txt_auth_pwd = QLineEdit()
        self.txt_auth_pwd.setObjectName("glass-input")
        self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_auth_pwd.setPlaceholderText("Enter master key")
        self.txt_auth_pwd.setStyleSheet(self.txt_auth_email.styleSheet())
        self.txt_auth_pwd.returnPressed.connect(self._do_authenticate)
        pwd_box.addWidget(self.txt_auth_pwd)

        self.btn_toggle_eye = QPushButton()
        self.btn_toggle_eye.setAutoDefault(False)
        self.btn_toggle_eye.setDefault(False)
        self.btn_toggle_eye.setFixedSize(40, 40)
        self.btn_toggle_eye.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_eye.setToolTip("Toggle key visibility")
        self._update_eye_icon(is_masked=True)
        self.btn_toggle_eye.setStyleSheet("""
            QPushButton {
                background: rgba(14, 21, 35, 0.70);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 10px;
                color: #94a3b8;
            }
            QPushButton:hover {
                border-color: #00e5ff;
                background: rgba(18, 28, 46, 0.90);
            }
        """)
        self.btn_toggle_eye.clicked.connect(self._toggle_password_mask)
        pwd_box.addWidget(self.btn_toggle_eye)
        card_layout.addLayout(pwd_box)

        # Error / Feedback message
        self.lbl_auth_error = QLabel()
        self.lbl_auth_error.setWordWrap(True)
        self.lbl_auth_error.setVisible(False)
        self.lbl_auth_error.setStyleSheet("color: #ff4d4f; font-size: 11.5px; padding: 2px 0;")
        card_layout.addWidget(self.lbl_auth_error)

        card_layout.addSpacing(6)

        # Vibrant Glowing Cyber Button (Rephrased)
        self.btn_unlock = QPushButton("Unlock Console  ➔")
        self.btn_unlock.setObjectName("btn-unlock")
        self.btn_unlock.setAutoDefault(True)
        self.btn_unlock.setDefault(True)
        self.btn_unlock.setFixedHeight(42)
        self.btn_unlock.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_unlock.setStyleSheet("""
            QPushButton#btn-unlock {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0088cc, stop:0.5 #00b4d8, stop:1 #00e5ff);
                border: none;
                border-radius: 10px;
                color: #040810;
                font-size: 13.5px;
                font-weight: 800;
                letter-spacing: 0.5px;
            }
            QPushButton#btn-unlock:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #009ce8, stop:0.5 #1ed2f7, stop:1 #4de9ff);
            }
            QPushButton#btn-unlock:pressed {
                background: #0077aa;
            }
        """)
        self.btn_unlock.clicked.connect(self._do_authenticate)
        card_layout.addWidget(self.btn_unlock)

        # Discreet Monospace Hint (Rephrased)
        hint = QLabel("Default key: owner  •  Configurable inside")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("font-size: 11px; color: #64748b; font-family: 'Consolas', monospace;")
        card_layout.addWidget(hint)

        layout.addWidget(card)
        self.stack.addWidget(auth_widget)

    def _on_auth_email_return(self) -> None:
        """Handle Enter key in email field by advancing to password or authenticating."""
        try:
            if not self.txt_auth_pwd.text().strip():
                self.txt_auth_pwd.setFocus()
            else:
                self._do_authenticate()
        except Exception:
            pass

    def _update_eye_icon(self, is_masked: bool) -> None:
        """Update toggle button icon using unified minimalist SVGs."""
        try:
            target_path = _eye_icon_path if is_masked else _eye_slash_icon_path
            if target_path.exists():
                self.btn_toggle_eye.setIcon(QIcon(str(target_path)))
                self.btn_toggle_eye.setIconSize(QSize(18, 18))
                self.btn_toggle_eye.setText("")
            else:
                self.btn_toggle_eye.setText("Show" if is_masked else "Hide")
                self.btn_toggle_eye.setStyleSheet("font-size: 10px; font-weight: bold; color: #94a3b8;")
        except Exception:
            pass

    def _toggle_password_mask(self) -> None:
        try:
            if self.txt_auth_pwd.echoMode() == QLineEdit.EchoMode.Password:
                self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Normal)
                self._update_eye_icon(is_masked=False)
            else:
                self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Password)
                self._update_eye_icon(is_masked=True)
        except Exception:
            pass

    def _do_authenticate(self) -> None:
        try:
            email = self.txt_auth_email.text().strip()
            pwd = self.txt_auth_pwd.text().strip()
            self.lbl_auth_error.setVisible(False)

            if not pwd:
                self.lbl_auth_error.setText("✖ Access key / master password cannot be blank.")
                self.lbl_auth_error.setVisible(True)
                self.txt_auth_pwd.setFocus()
                return

            ok, msg = self.service.authenticate(email, pwd)
            if ok:
                self.txt_auth_pwd.clear()
                self._update_eye_icon(is_masked=True)
                self.txt_auth_pwd.setEchoMode(QLineEdit.EchoMode.Password)
                self._update_cloud_badge()
                self._refresh_tickets_list()
                self.stack.setCurrentIndex(1)
                self.update()
            else:
                self.lbl_auth_error.setText(f"✖ {msg}")
                self.lbl_auth_error.setVisible(True)
        except Exception as e:
            try:
                self.lbl_auth_error.setText(f"✖ Authentication error: {e}")
                self.lbl_auth_error.setVisible(True)
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # Screen 1: Glassmorphic Dashboard View
    # ─────────────────────────────────────────────────────────────────────────

    def _build_dashboard_screen(self) -> None:
        dash_widget = QWidget()
        layout = QVBoxLayout(dash_widget)
        layout.setContentsMargins(20, 16, 20, 20)
        layout.setSpacing(14)

        # ── Glass Stats Ribbon ──────────────────────────────────────────────
        stats_ribbon = QHBoxLayout()
        stats_ribbon.setSpacing(12)

        self.stat_total = self._make_stat_badge("TOTAL DEFECTS", "0", "#00e5ff")
        self.stat_critical = self._make_stat_badge("CRITICAL", "0", "#ff4d4f")
        self.stat_open = self._make_stat_badge("OPEN", "0", "#faad14")
        self.stat_resolved = self._make_stat_badge("RESOLVED", "0", "#10b981")

        stats_ribbon.addWidget(self.stat_total)
        stats_ribbon.addWidget(self.stat_critical)
        stats_ribbon.addWidget(self.stat_open)
        stats_ribbon.addWidget(self.stat_resolved)
        layout.addLayout(stats_ribbon)

        # ── Control Bar: Search, Filters, + Button, Settings, Lock ──────────
        ctrl_bar = QHBoxLayout()
        ctrl_bar.setSpacing(10)

        # Glass Search Input (Rephrased)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍  Search defects, categories, or logs...")
        self.search_input.setFixedHeight(36)
        self.search_input.setStyleSheet("""
            QLineEdit {
                background-color: rgba(12, 18, 30, 0.65);
                border: 1px solid rgba(255, 255, 255, 0.09);
                border-top: 1px solid rgba(255, 255, 255, 0.18);
                border-radius: 9px;
                padding: 4px 14px;
                color: #f8fafc;
                font-size: 12.5px;
            }
            QLineEdit:focus {
                border-color: #00e5ff;
                background-color: rgba(16, 25, 42, 0.88);
            }
        """)
        self.search_input.textChanged.connect(self._on_search_changed)
        ctrl_bar.addWidget(self.search_input, stretch=2)

        # Glass Filter Dropdown
        self.filter_cb = QComboBox()
        self.filter_cb.addItems(["All Status", "Open", "In Progress", "Resolved", "Closed"])
        self.filter_cb.setFixedHeight(36)
        self.filter_cb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.filter_cb.setStyleSheet("""
            QComboBox {
                background-color: rgba(14, 21, 35, 0.70);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-top: 1px solid rgba(255, 255, 255, 0.18);
                border-radius: 9px;
                padding: 4px 26px 4px 12px;
                color: #e2e8f0;
                font-size: 12px;
                font-weight: 600;
            }
            QComboBox:hover {
                border-color: rgba(0, 210, 255, 0.40);
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 22px;
                border: none;
            }
            QComboBox QAbstractItemView {
                background-color: #0c121d;
                color: #e2e8f0;
                selection-background-color: #1a2538;
                border: 1px solid rgba(0, 210, 255, 0.3);
                border-radius: 6px;
            }
        """)
        self.filter_cb.currentTextChanged.connect(self._on_filter_changed)
        ctrl_bar.addWidget(self.filter_cb)

        # Glowing Emerald + New Ticket Action
        self.btn_new = QPushButton("✚  New Ticket")
        self.btn_new.setFixedHeight(36)
        self.btn_new.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_new.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0f766e, stop:1 #10b981);
                color: #ffffff;
                border: 1px solid rgba(16, 185, 129, 0.40);
                border-top: 1px solid rgba(255, 255, 255, 0.35);
                border-radius: 9px;
                padding: 0 16px;
                font-weight: 700;
                font-size: 12.5px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #115e59, stop:1 #34d399);
                border-color: #10b981;
            }
        """)
        self.btn_new.clicked.connect(self._toggle_new_ticket_form)
        ctrl_bar.addWidget(self.btn_new)

        # Glass Cloud Sync (Rephrased)
        btn_fb = QPushButton("⚙  Cloud Sync")
        btn_fb.setFixedHeight(36)
        btn_fb.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_fb.setStyleSheet("""
            QPushButton {
                background: rgba(14, 21, 35, 0.65);
                color: #94a3b8;
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-top: 1px solid rgba(255, 255, 255, 0.18);
                border-radius: 9px;
                padding: 0 14px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                color: #00e5ff;
                border-color: #00e5ff;
                background: rgba(18, 28, 46, 0.85);
            }
        """)
        btn_fb.clicked.connect(self._open_firebase_settings_modal)
        ctrl_bar.addWidget(btn_fb)

        # Lock / Signout
        btn_lock = QPushButton("🔒  Lock")
        btn_lock.setFixedHeight(36)
        btn_lock.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_lock.setStyleSheet("""
            QPushButton {
                background: rgba(14, 21, 35, 0.65);
                color: #94a3b8;
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-top: 1px solid rgba(255, 255, 255, 0.18);
                border-radius: 9px;
                padding: 0 14px;
                font-size: 12px;
            }
            QPushButton:hover {
                color: #ff6b6b;
                border-color: #ff4d4f;
                background: rgba(255, 77, 79, 0.15);
            }
        """)
        btn_lock.clicked.connect(self._do_lock)
        ctrl_bar.addWidget(btn_lock)

        layout.addLayout(ctrl_bar)

        # ── Collapsible Glass New Ticket Form ────────────────────────────────
        self.form_card = QFrame()
        self.form_card.setObjectName("glass-form-card")
        self.form_card.setVisible(False)
        self.form_card.setStyleSheet("""
            QFrame#glass-form-card {
                background-color: rgba(16, 24, 40, 0.82);
                border: 1px solid rgba(0, 210, 255, 0.40);
                border-top: 1px solid rgba(0, 210, 255, 0.80);
                border-radius: 14px;
            }
            QLabel {
                background: transparent;
                border: none;
            }
        """)
        f_layout = QVBoxLayout(self.form_card)
        f_layout.setContentsMargins(18, 16, 18, 16)
        f_layout.setSpacing(12)

        f_title = QLabel("✚  Create Defect Report")
        f_title.setStyleSheet("color: #00e5ff; font-weight: 800; font-size: 13.5px;")
        f_layout.addWidget(f_title)

        # Row 1: Title & Category (Rephrased placeholder)
        r1 = QHBoxLayout()
        r1.setSpacing(10)
        self.new_title = QLineEdit()
        self.new_title.setStyleSheet("""
            QLineEdit {
                background-color: rgba(9, 14, 22, 0.75);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 8px;
                padding: 7px 12px;
                color: #ffffff;
                font-size: 12.5px;
            }
            QLineEdit:focus {
                border-color: #00e5ff;
            }
        """)
        r1.addWidget(self.new_title, stretch=3)

        self.new_category = QComboBox()
        self.new_category.addItems([
            "GUI / Interface",
            "Monaco Code Editor",
            "Serial Monitor",
            "PlatformIO / Toolchain",
            "Hardware & COM Port",
            "Low-End & HDD",
            "General Defect",
        ])
        self.new_category.setStyleSheet("""
            QComboBox {
                background-color: rgba(10, 16, 26, 0.75);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 8px;
                padding: 6px 20px 6px 10px;
                color: #e2e8f0;
                font-size: 12px;
            }
        """)
        r1.addWidget(self.new_category, stretch=1)
        f_layout.addLayout(r1)

        # Row 2: Severity & Status
        r2 = QHBoxLayout()
        r2.setSpacing(12)
        lbl_sev = QLabel("Severity:")
        lbl_sev.setStyleSheet("color: #94a3b8; font-size: 12px;")
        r2.addWidget(lbl_sev)

        self.new_sev = QComboBox()
        self.new_sev.addItems(["Critical", "High", "Medium", "Low"])
        self.new_sev.setCurrentText("Medium")
        self.new_sev.setStyleSheet(self.new_category.styleSheet())
        r2.addWidget(self.new_sev)

        lbl_st = QLabel("Status:")
        lbl_st.setStyleSheet("color: #94a3b8; font-size: 12px;")
        r2.addWidget(lbl_st)

        self.new_status = QComboBox()
        self.new_status.addItems(["Open", "In Progress", "Resolved"])
        self.new_status.setStyleSheet(self.new_category.styleSheet())
        r2.addWidget(self.new_status)
        r2.addStretch(1)

        # Buttons
        btn_cancel_add = QPushButton("Cancel")
        btn_cancel_add.setFixedHeight(32)
        btn_cancel_add.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #94a3b8;
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 7px;
                padding: 0 14px;
            }
            QPushButton:hover {
                color: #ffffff;
                background: rgba(255, 255, 255, 0.05);
            }
        """)
        btn_cancel_add.clicked.connect(lambda: self.form_card.setVisible(False))
        r2.addWidget(btn_cancel_add)

        btn_save_add = QPushButton("💾  Save Report")
        btn_save_add.setFixedHeight(32)
        btn_save_add.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save_add.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0088cc, stop:1 #00e5ff);
                color: #040810;
                font-weight: 800;
                border: none;
                border-radius: 7px;
                padding: 0 16px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #009ce8, stop:1 #4de9ff);
            }
        """)
        btn_save_add.clicked.connect(self._do_save_new_ticket)
        r2.addWidget(btn_save_add)
        f_layout.addLayout(r2)

        # Row 3: Description Text Area
        self.new_desc = QTextEdit()
        self.new_desc.setFixedHeight(85)
        self.new_desc.setStyleSheet("""
            QTextEdit {
                background-color: rgba(9, 14, 22, 0.75);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 8px;
                padding: 8px 12px;
                color: #ffffff;
                font-size: 12px;
                font-family: 'Consolas', monospace;
            }
            QTextEdit:focus {
                border-color: #00e5ff;
            }
        """)
        f_layout.addWidget(self.new_desc)

        layout.addWidget(self.form_card)

        # ── Scroll Area for Frosted Ticket Cards ─────────────────────────────
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self.cards_container = QWidget()
        self.cards_container.setStyleSheet("background: transparent;")
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 4, 0)
        self.cards_layout.setSpacing(12)
        self.cards_layout.addStretch(1)

        self.scroll.setWidget(self.cards_container)
        layout.addWidget(self.scroll, stretch=1)

        self.stack.addWidget(dash_widget)

    def _make_stat_badge(self, label: str, val: str, accent_color: str) -> QFrame:
        """Create a clean, frosted glass metric card without nested bordered boxes."""
        frame = QFrame()
        frame.setObjectName("stat-card")
        frame.setStyleSheet("""
            QFrame#stat-card {
                background-color: rgba(16, 24, 40, 0.60);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-top: 1px solid rgba(255, 255, 255, 0.20);
                border-radius: 12px;
            }
            QFrame#stat-card:hover {
                background-color: rgba(22, 34, 54, 0.78);
                border-color: rgba(0, 210, 255, 0.30);
            }
            QLabel {
                background: transparent;
                border: none;
                padding: 0px;
            }
        """)
        fl = QHBoxLayout(frame)
        fl.setContentsMargins(14, 10, 14, 10)
        fl.setSpacing(10)

        lbl = QLabel(label)
        lbl.setStyleSheet("color: #94a3b8; font-size: 10.5px; font-weight: 700; letter-spacing: 0.6px;")
        fl.addWidget(lbl)

        val_lbl = QLabel(val)
        val_lbl.setObjectName("val-label")
        val_lbl.setStyleSheet(f"color: {accent_color}; font-size: 16px; font-weight: 800; font-family: 'Consolas', monospace;")
        fl.addWidget(val_lbl)
        return frame

    def _toggle_new_ticket_form(self) -> None:
        try:
            is_vis = not self.form_card.isVisible()
            self.form_card.setVisible(is_vis)
            if is_vis:
                self.new_title.setFocus()
        except Exception:
            pass

    def _do_save_new_ticket(self) -> None:
        try:
            title = self.new_title.text().strip()
            if not title:
                QMessageBox.warning(self, "Validation", "Ticket title cannot be blank.")
                return

            cat = self.new_category.currentText()
            sev = self.new_sev.currentText()
            st = self.new_status.currentText()
            desc = self.new_desc.toPlainText().strip()

            self.service.create_ticket(
                title=title,
                category=cat,
                severity=sev,
                description=desc,
                status=st,
            )

            # Reset form
            self.new_title.clear()
            self.new_desc.clear()
            self.form_card.setVisible(False)
            self._refresh_tickets_list()
        except Exception:
            pass

    def _refresh_tickets_list(self) -> None:
        """Clear and rebuild the list of ticket cards matching search and filter."""
        try:
            # Remove old widgets from layout
            while self.cards_layout.count() > 1:
                child = self.cards_layout.takeAt(0)
                if child and child.widget():
                    child.widget().deleteLater()

            all_tickets = self.service.get_tickets()

            # Update stats
            total_cnt = len(all_tickets)
            crit_cnt = sum(1 for t in all_tickets if t.get("severity") == "Critical")
            open_cnt = sum(1 for t in all_tickets if t.get("status") in ("Open", "In Progress"))
            res_cnt = sum(1 for t in all_tickets if t.get("status") in ("Resolved", "Closed"))

            for f, val in [(self.stat_total, total_cnt), (self.stat_critical, crit_cnt),
                           (self.stat_open, open_cnt), (self.stat_resolved, res_cnt)]:
                lbl = f.findChild(QLabel, "val-label")
                if lbl:
                    lbl.setText(str(val))

            filtered = []
            q = self._search_query.lower()
            for t in all_tickets:
                if self._active_filter not in ("All", "All Status") and t.get("status") != self._active_filter:
                    continue
                if q:
                    combined = f"{t.get('title', '')} {t.get('description', '')} {t.get('category', '')}".lower()
                    if q not in combined:
                        continue
                filtered.append(t)

            if not filtered:
                empty_card = QFrame()
                empty_card.setObjectName("empty-card")
                empty_card.setStyleSheet("""
                    QFrame#empty-card {
                        background-color: rgba(16, 24, 40, 0.50);
                        border: 1px solid rgba(255, 255, 255, 0.06);
                        border-top: 1px solid rgba(255, 255, 255, 0.15);
                        border-radius: 14px;
                    }
                    QLabel {
                        background: transparent;
                        border: none;
                    }
                """)
                el = QVBoxLayout(empty_card)
                el.setContentsMargins(30, 45, 30, 45)
                el.setSpacing(10)
                el.setAlignment(Qt.AlignmentFlag.AlignCenter)

                e_icon = QLabel("✦")
                e_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
                e_icon.setStyleSheet("color: #00e5ff; font-size: 26px;")
                el.addWidget(e_icon)

                e_title = QLabel("No Defect Reports Found")
                e_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
                e_title.setStyleSheet("color: #ffffff; font-size: 15px; font-weight: 700;")
                el.addWidget(e_title)

                e_desc = QLabel("All systems are nominal or no tickets match the current filter.")
                e_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
                e_desc.setStyleSheet("color: #64748b; font-size: 12px;")
                el.addWidget(e_desc)

                self.cards_layout.insertWidget(0, empty_card)
                return

            for idx, t in enumerate(filtered):
                card = TicketCard(t, parent=self.cards_container)
                card.status_changed.connect(self._on_ticket_status_changed)
                card.deleted.connect(self._on_ticket_deleted)
                card.edit_requested.connect(self._open_edit_ticket_modal)
                self.cards_layout.insertWidget(idx, card)
        except Exception:
            pass

    def _open_edit_ticket_modal(self, ticket: Dict[str, Any]) -> None:
        try:
            dlg = EditTicketDialog(ticket=ticket, service=self.service, parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._refresh_tickets_list()
        except Exception:
            pass

    def _on_ticket_status_changed(self, ticket_id: str, new_status: str) -> None:
        try:
            self.service.update_ticket(ticket_id, {"status": new_status})
            # Re-update stats
            all_tickets = self.service.get_tickets()
            total_cnt = len(all_tickets)
            crit_cnt = sum(1 for t in all_tickets if t.get("severity") == "Critical")
            open_cnt = sum(1 for t in all_tickets if t.get("status") in ("Open", "In Progress"))
            res_cnt = sum(1 for t in all_tickets if t.get("status") in ("Resolved", "Closed"))
            for f, val in [(self.stat_total, total_cnt), (self.stat_critical, crit_cnt),
                           (self.stat_open, open_cnt), (self.stat_resolved, res_cnt)]:
                lbl = f.findChild(QLabel, "val-label")
                if lbl:
                    lbl.setText(str(val))
        except Exception:
            pass

    def _on_ticket_deleted(self, ticket_id: str) -> None:
        try:
            self.service.delete_ticket(ticket_id)
            self._refresh_tickets_list()
        except Exception:
            pass

    def _on_filter_changed(self, text: str) -> None:
        try:
            self._active_filter = text
            self._refresh_tickets_list()
        except Exception:
            pass

    def _on_search_changed(self, text: str) -> None:
        try:
            self._search_query = text.strip()
            self._refresh_tickets_list()
        except Exception:
            pass

    def _do_lock(self) -> None:
        try:
            self.service.logout()
            self._update_cloud_badge()
            self.stack.setCurrentIndex(0)
            self.update()
        except Exception:
            pass

    def _update_cloud_badge(self) -> None:
        try:
            self.cloud_badge.setFixedHeight(24)
            self.cloud_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cfg = self.service.get_config()
            if cfg.get("use_firebase") and cfg.get("firebase_database_url"):
                if is_internet_available(timeout=0.4):
                    self.cloud_badge.setText("● Cloud Active")
                    self.cloud_badge.setStyleSheet("""
                        QLabel {
                            color: #10b981;
                            font-size: 11px;
                            font-weight: 600;
                            font-family: 'Consolas', monospace;
                            background: rgba(16, 185, 129, 0.12);
                            padding: 0 10px;
                            border-radius: 12px;
                            border: 1px solid rgba(16, 185, 129, 0.35);
                        }
                    """)
                    return

            self.cloud_badge.setText("● Offline Cache")
            self.cloud_badge.setStyleSheet("""
                QLabel {
                    color: #94a3b8;
                    font-size: 11px;
                    font-weight: 600;
                    font-family: 'Consolas', monospace;
                    background: rgba(255, 255, 255, 0.05);
                    padding: 0 10px;
                    border-radius: 12px;
                    border: 1px solid rgba(255, 255, 255, 0.10);
                }
            """)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # Frosted Firebase Cloud Settings Modal
    # ─────────────────────────────────────────────────────────────────────────

    def _open_firebase_settings_modal(self) -> None:
        try:
            cfg = self.service.get_config()
            dlg = QDialog(self)
            dlg.setWindowTitle("Firebase Cloud Synchronization")
            dlg.setWindowFlags(dlg.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
            dlg.resize(600, 450)
            dlg.setMinimumSize(560, 420)
            dlg.setStyleSheet("""
                QDialog {
                    background: #090e18;
                    color: #e2e8f0;
                    font-family: 'Segoe UI', sans-serif;
                }
                QLabel {
                    color: #e2e8f0;
                    background: transparent;
                    border: none;
                }
            """)

            l = QVBoxLayout(dlg)
            l.setContentsMargins(28, 24, 28, 24)
            l.setSpacing(14)

            hdr = QLabel("🔥  Cloud Synchronization & Auth")
            hdr.setStyleSheet("color: #ff9f00; font-size: 16px; font-weight: 800; letter-spacing: -0.2px;")
            l.addWidget(hdr)

            info = QLabel(
                "Sync defect tickets across developer workstations. If unconfigured or offline, "
                "tickets are saved securely to local cache."
            )
            info.setWordWrap(True)
            info.setStyleSheet("color: #94a3b8; font-size: 11.5px; line-height: 1.45;")
            l.addWidget(info)

            lbl_db = QLabel("Realtime Database URL:")
            lbl_db.setStyleSheet("color: #ffffff; font-weight: 600; font-size: 12px;")
            l.addWidget(lbl_db)

            db_in = QLineEdit()
            db_in.setPlaceholderText("https://mcu-flasher-c46e3-default-rtdb.asia-southeast1.firebasedatabase.app/")
            db_in.setText(cfg.get("firebase_database_url", "https://mcu-flasher-c46e3-default-rtdb.asia-southeast1.firebasedatabase.app/"))
            db_in.setStyleSheet("""
                QLineEdit {
                    background-color: rgba(14, 21, 35, 0.70);
                    border: 1px solid rgba(255, 255, 255, 0.10);
                    border-radius: 8px;
                    padding: 9px 12px;
                    color: #ffffff;
                    font-size: 12px;
                }
                QLineEdit:focus { border-color: #00e5ff; }
            """)
            l.addWidget(db_in)

            lbl_key = QLabel("Web API Key:")
            lbl_key.setStyleSheet("color: #ffffff; font-weight: 600; font-size: 12px;")
            l.addWidget(lbl_key)

            key_in = QLineEdit()
            key_in.setPlaceholderText("AIzaSy...")
            key_in.setText(cfg.get("firebase_api_key", ""))
            key_in.setStyleSheet(db_in.styleSheet())
            l.addWidget(key_in)

            # Test Section: dedicated row for button + full-width word-wrapped status card
            test_section = QVBoxLayout()
            test_section.setSpacing(8)

            t_row = QHBoxLayout()
            test_btn = QPushButton("Test Connection")
            test_btn.setFixedHeight(34)
            test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            test_btn.setStyleSheet("""
                QPushButton {
                    background: rgba(0, 210, 255, 0.15);
                    color: #00e5ff;
                    border: 1px solid rgba(0, 210, 255, 0.35);
                    border-radius: 7px;
                    padding: 0 16px;
                    font-weight: 700;
                    font-size: 12px;
                }
                QPushButton:hover {
                    background: rgba(0, 210, 255, 0.28);
                }
                QPushButton:disabled {
                    background: rgba(255, 255, 255, 0.05);
                    color: #64748b;
                    border-color: rgba(255, 255, 255, 0.10);
                }
            """)
            t_row.addWidget(test_btn)
            t_row.addStretch(1)
            test_section.addLayout(t_row)

            # Full-width status card with word-wrapping
            test_lbl = QLabel()
            test_lbl.setWordWrap(True)
            test_lbl.setStyleSheet("""
                QLabel {
                    font-size: 11.5px;
                    line-height: 1.45;
                    border-radius: 8px;
                    padding: 8px 12px;
                    background: transparent;
                }
            """)
            test_lbl.setVisible(False)
            test_section.addWidget(test_lbl)

            l.addLayout(test_section)

            def _run_test():
                try:
                    url = db_in.text().strip()
                    key = key_in.text().strip()
                    if not url:
                        test_lbl.setText("⚠️ Enter a database URL first.")
                        test_lbl.setStyleSheet("""
                            QLabel {
                                color: #faad14;
                                font-size: 11.5px;
                                font-weight: 600;
                                background-color: rgba(250, 173, 20, 0.12);
                                border: 1px solid rgba(250, 173, 20, 0.35);
                                border-radius: 8px;
                                padding: 8px 12px;
                            }
                        """)
                        test_lbl.setVisible(True)
                        return

                    test_btn.setEnabled(False)
                    test_btn.setText("Testing connection...")
                    test_lbl.setVisible(False)
                    QApplication.processEvents()

                    ok, msg = self.service.test_firebase_connection(key, url)
                    test_btn.setEnabled(True)
                    test_btn.setText("Test Connection")

                    if ok:
                        test_lbl.setText(f"✓  {msg}")
                        test_lbl.setStyleSheet("""
                            QLabel {
                                color: #10b981;
                                font-size: 11.5px;
                                font-weight: 600;
                                background-color: rgba(16, 185, 129, 0.12);
                                border: 1px solid rgba(16, 185, 129, 0.35);
                                border-radius: 8px;
                                padding: 8px 12px;
                            }
                        """)
                    else:
                        test_lbl.setText(f"✕  {msg}")
                        test_lbl.setStyleSheet("""
                            QLabel {
                                color: #ff4d4f;
                                font-size: 11.5px;
                                font-weight: 600;
                                background-color: rgba(255, 77, 79, 0.12);
                                border: 1px solid rgba(255, 77, 79, 0.35);
                                border-radius: 8px;
                                padding: 8px 12px;
                            }
                        """)
                    test_lbl.setVisible(True)
                except Exception as ex:
                    test_btn.setEnabled(True)
                    test_btn.setText("Test Connection")
                    test_lbl.setText(f"✕  Error: {ex}")
                    test_lbl.setStyleSheet("""
                        QLabel {
                            color: #ff4d4f;
                            font-size: 11.5px;
                            font-weight: 600;
                            background-color: rgba(255, 77, 79, 0.12);
                            border: 1px solid rgba(255, 77, 79, 0.35);
                            border-radius: 8px;
                            padding: 8px 12px;
                        }
                    """)
                    test_lbl.setVisible(True)

            test_btn.clicked.connect(_run_test)

            l.addStretch(1)

            b_row = QHBoxLayout()
            b_row.addStretch(1)
            cancel_b = QPushButton("Cancel")
            cancel_b.setFixedHeight(34)
            cancel_b.setStyleSheet("""
                QPushButton {
                    background: transparent;
                    color: #94a3b8;
                    border: 1px solid rgba(255, 255, 255, 0.10);
                    border-radius: 7px;
                    padding: 0 18px;
                    font-size: 12px;
                }
                QPushButton:hover { color: #ffffff; }
            """)
            cancel_b.clicked.connect(dlg.reject)
            b_row.addWidget(cancel_b)

            save_b = QPushButton("Save Settings")
            save_b.setFixedHeight(34)
            save_b.setCursor(Qt.CursorShape.PointingHandCursor)
            save_b.setStyleSheet("""
                QPushButton {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0088cc, stop:1 #00e5ff);
                    color: #040810;
                    font-weight: 800;
                    font-size: 12px;
                    border: none;
                    border-radius: 7px;
                    padding: 0 20px;
                }
                QPushButton:hover {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #009ce8, stop:1 #4de9ff);
                }
            """)
            def _save():
                try:
                    new_url = db_in.text().strip()
                    new_key = key_in.text().strip()
                    self.service.save_config({
                        "firebase_database_url": new_url,
                        "firebase_api_key": new_key,
                        "use_firebase": bool(new_url),
                    })
                    self._update_cloud_badge()
                    dlg.accept()
                except Exception:
                    dlg.reject()
            save_b.clicked.connect(_save)
            b_row.addWidget(save_b)
            l.addLayout(b_row)

            dlg.exec()
        except Exception:
            pass
