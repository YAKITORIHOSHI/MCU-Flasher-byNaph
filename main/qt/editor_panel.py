#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.qt.editor_panel — Monaco Code Editor panel for MCU Flasher by Naph.

Embeds the offline Monaco editor (src/editor/index.html) inside a
QWebEngineView widget.  Communication between Python and the Monaco JS
layer uses QWebChannel — the same Python-callable API surface as before,
but without any pywebview dependency.

Architecture
─────────────
  Python side:  EditorBridgeAPI  (QObject with @Slot methods)
  JS side:      qtWebChannel object injected via qwebchannel.js
  Transport:    Qt WebEngine's built-in WebChannel transport

The Monaco editor HTML already lives at ``src/editor/index.html`` and
loads ``bundle.js`` offline.  A small patch is injected at load time
(via ``runJavaScript``) to initialise the QWebChannel bridge on the
JS side so editor → Python calls work.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from main.web_bridge import MCUWebBackendAPI

# pyrefly: ignore [missing-import]
from PySide6.QtCore import QObject, QUrl, Slot, Signal, QTimer, QEvent, Qt
# pyrefly: ignore [missing-import]
from PySide6.QtWebEngineCore import QWebEngineSettings, QWebEngineProfile
# pyrefly: ignore [missing-import]
from PySide6.QtWebEngineWidgets import QWebEngineView
# pyrefly: ignore [missing-import]
from PySide6.QtWebChannel import QWebChannel
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QApplication

# ─────────────────────────────────────────────────────────────────────────────
# QWebChannel JS bootstrap
# Injected after the page loads as fallback/sync to ensure QWebChannel connects
# and triggers safeLoadProject.
# ─────────────────────────────────────────────────────────────────────────────
_WEBCHANNEL_INIT_JS = """
(function() {
    if (typeof window.__initQtWebChannel === 'function') {
        window.__initQtWebChannel();
    }
    if (typeof window.safeLoadProject === 'function') {
        window.safeLoadProject();
    } else if (typeof window.loadProject === 'function') {
        window.loadProject();
    }
})();
"""


class EditorBridgeAPI(QObject):
    """
    Python-side of the Monaco ↔ Python bridge via QWebChannel.

    Exposes all RPC methods required by index.html and bundle.js with
    proper PySide6 @Slot return type annotations.
    """

    # Signals FROM Python → Monaco
    file_loaded    = Signal(str, str)          # (file_path, content)
    theme_changed  = Signal(str)               # (theme_name)
    request_save   = Signal()
    request_save_all = Signal()
    save_finished = Signal(str, bool)
    _syntax_finished = Signal(dict)

    def __init__(self, backend: "MCUWebBackendAPI", parent: QObject | None = None):
        super().__init__(parent)
        self._backend = backend
        self._buffer_snapshots = {}
        self._recovery_buffers = {}
        self._recovering_buffers = False
        self._syntax_generation = 0
        self._syntax_running = False
        self._syntax_pending = None
        self._save_failure_count = 0
        self._syntax_finished.connect(self._finish_syntax, Qt.ConnectionType.QueuedConnection)

    # ── Called FROM Monaco JS ─────────────────────────────────────────────

    @Slot(bool)
    def finish_reload_request(self, success: bool) -> None:
        if self._backend:
            self._backend.emit("console:progress", {"action": "Completed" if success else "Failed"})
            if not success:
                self._backend.emit("console:log", {"text": "Reload failed. The editor buffer was preserved.", "tag": "error", "newline": True})

    @Slot(str, bool)
    def finish_save_request(self, token: str, success: bool) -> None:
        self.save_finished.emit(token, success)

    @Slot(result="QVariant")
    def get_project_files(self) -> list:
        """Return all editable files in the project folder."""
        if self._backend:
            return self._backend.get_project_files()
        return []

    @Slot(str, result="QVariant")
    def read_file(self, file_path: str) -> dict:
        """Read content of a sketch file for Monaco."""
        if self._backend:
            return self._backend.read_file(file_path)
        return {"content": "", "success": False, "error": "No backend"}

    @Slot(str, str, result="QVariant")
    def save_file(self, file_path: str, content: str) -> dict:
        """Monaco reports file save (Ctrl+S)."""
        if self._backend:
            try:
                from main.qt.signals import signals as sig_bus
                sig_bus.console_progress.emit({"action": "Saving"})
                QTimer.singleShot(600, lambda: sig_bus.console_progress.emit({"action": "Completed"}))
            except Exception:
                pass
            res = self._backend.save_file(file_path, content)
            if res.get("success"):
                self._buffer_snapshots.pop(str(Path(file_path).resolve()), None)
            else:
                self._save_failure_count += 1
            if hasattr(self._backend, "ai_watcher") and self._backend.ai_watcher:
                self._backend.ai_watcher.note_user_save(file_path, content)
            return res
        return {"success": False, "error": "No backend"}

    @Slot(result="QVariant")
    def save_all_files(self) -> dict:
        """Save all modified open files."""
        if self._backend:
            try:
                from main.qt.signals import signals as sig_bus
                sig_bus.console_progress.emit({"action": "Saving All"})
                QTimer.singleShot(700, lambda: sig_bus.console_progress.emit({"action": "Completed"}))
            except Exception:
                pass
            res = self._backend.save_all_files()
            if not res.get("success"):
                self._save_failure_count += 1
            if hasattr(self._backend, "ai_watcher") and self._backend.ai_watcher:
                for fp in getattr(self._backend, "modified_files", {}):
                    self._backend.ai_watcher.note_user_save(fp)
            return res
        return {"success": True}

    @Slot("QVariant", result="QVariant")
    def save_tab_order(self, paths: list) -> dict:
        """Persist tab order in cache."""
        if self._backend and isinstance(paths, list):
            self._backend.save_tab_order(paths)
        return {"success": True}

    @Slot(result=str)
    def get_theme_mode(self) -> str:
        """Return active theme mode."""
        try:
            from main.core.theme import get_theme_mode
            return get_theme_mode()
        except Exception:
            return "default"

    @Slot(result=int)
    def get_font_size(self) -> int:
        """Return configured font size for Monaco editor."""
        try:
            from main.core.config import get_editor_font_size
            return int(get_editor_font_size())
        except Exception:
            return 13

    @Slot(int, result="QVariant")
    def save_font_size(self, size: int) -> dict:
        """Monaco reports font size change from Ctrl + +/- shortcuts or mouse wheel zoom."""
        try:
            sz = max(6, min(48, int(size)))
            from main.core.config import set_editor_font_size
            if set_editor_font_size(sz) is False:
                from main.qt.preferences import report_preference_failure
                report_preference_failure("Editor font size")
                return {"success": False, "error": "Editor font size could not be saved"}
            self._current_font_size = sz
            from main.qt.signals import signals as sig_bus
            if hasattr(sig_bus, "editor_font_size_changed"):
                sig_bus.editor_font_size_changed.emit(sz)
            return {"success": True, "size": sz}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @Slot(result=str)
    def get_project_dir(self) -> str:
        """Return root project folder path."""
        if self._backend:
            return self._backend.get_project_dir()
        return ""

    @Slot()
    def open_project_search(self) -> None:
        """Route the editor shortcut to its window-owned native Find All."""
        panel = self.parent()
        if panel and hasattr(panel, "show_project_search"):
            panel.show_project_search()

    @Slot(str, str, result=str)
    def realtime_check_syntax(self, file_path: str, content: str) -> str:
        """Coalesce edits; parsing never blocks the WebChannel/UI thread."""
        self._syntax_generation += 1
        self._syntax_pending = (self._syntax_generation, file_path, content)
        self._start_syntax()
        return "null"  # Results arrive through the queued marker signal.

    @Slot(str, result=bool)
    def realtime_check_buffer(self, file_path: str) -> bool:
        """Reuse the recovery snapshot instead of transferring the file twice."""
        content = self._buffer_snapshots.get(str(Path(file_path).resolve()))
        if content is None:
            return False  # A clean/new model still needs its initial source text.
        self.realtime_check_syntax(file_path, content)
        return True

    def _start_syntax(self):
        if self._syntax_running or self._syntax_pending is None:
            return
        generation, path, content = self._syntax_pending
        self._syntax_pending = None
        self._syntax_running = True
        from src.syntax_checker import analyze_cpp_syntax, get_syntax_executor
        def analyze():
            result = {"generation": generation, "path": path}
            try:
                result["diagnostics"] = analyze_cpp_syntax(content, Path(path))
            except Exception as exc:
                result["error"] = str(exc)
            try:
                self._syntax_finished.emit(result)
            except RuntimeError:
                pass
        try:
            get_syntax_executor().submit(analyze)
        except Exception as exc:
            self._finish_syntax({"generation": generation, "path": path, "error": str(exc)})

    @Slot(dict)
    def _finish_syntax(self, result):
        self._syntax_running = False
        if result["generation"] == self._syntax_generation and self._backend and str(self._backend.active_file_path) == result["path"]:
            from main.qt.signals import signals
            if "error" in result:
                from uuid import uuid4
                signals.notification.emit({"id": "notif_" + uuid4().hex, "title": "Syntax check failed", "message": result["error"], "type": "warning"})
            else:
                signals.syntax_errors.emit(result["diagnostics"])
        self._start_syntax()

    @Slot(str)
    def set_active_file(self, file_path: str) -> None:
        """Monaco reports the active tab changed."""
        if self._backend:
            self._backend.set_active_file(file_path)

    @Slot(str)
    @Slot(str, bool)
    def mark_modified(self, file_path: str, is_modified: bool = True) -> None:
        """Monaco reports a file became dirty or clean."""
        if self._backend:
            self._backend.mark_modified(file_path, is_modified)
            if not is_modified:
                self._buffer_snapshots.pop(str(Path(file_path).resolve()), None)
            if is_modified:
                try:
                    from main.qt.signals import signals as sig_bus
                    if hasattr(sig_bus, "skip_compile_availability_changed"):
                        sig_bus.skip_compile_availability_changed.emit(False)
                except Exception:
                    pass
                p = self.parent()
                if p and hasattr(p, "_schedule_autosave"):
                    p._schedule_autosave()

    @Slot(str, str)
    def snapshot_buffer(self, file_path: str, content: str):
        """Keep dirty editor text in Python memory across renderer recovery."""
        if not self._backend or not self._backend.sketch_dir_path:
            return
        path = Path(file_path).resolve()
        if path.parent != Path(self._backend.sketch_dir_path).resolve():
            return
        if path.suffix.lower() in {".ino", ".cpp", ".c", ".h", ".hpp", ".txt"}:
            self._buffer_snapshots[str(path)] = content
            # Invalidate old diagnostics immediately, before the next debounce
            # requests a parse. Otherwise stale markers can appear while typing.
            self._syntax_generation += 1
            self._syntax_pending = None

    @Slot(str, str)
    def update_editor_buffer(self, file_path: str, content: str):
        """One WebChannel message per edit keeps dirty state and recovery current."""
        self.mark_modified(file_path, True)
        self.snapshot_buffer(file_path, content)

    @Slot(result="QVariant")
    def get_recovery_buffers(self):
        if not self._recovering_buffers or not self._backend:
            return {}
        project = Path(self._backend.sketch_dir_path).resolve()
        return {path: content for path, content in self._recovery_buffers.items()
                if Path(path).parent == project}

    def begin_buffer_recovery(self):
        # Freeze before loadProject marks disk models clean or emits content
        # events; those events must not erase the pre-crash dirty snapshots.
        if not self._recovering_buffers:
            self._recovery_buffers = {
                path: content for path, content in self._buffer_snapshots.items()
                if self._backend and self._backend.modified_files.get(path)
            }
        self._recovering_buffers = True

    @Slot()
    def recovery_complete(self):
        self._recovering_buffers = False
        self._recovery_buffers.clear()

    def retain_current_project_buffers(self) -> None:
        """Release discarded project snapshots after a confirmed project switch."""
        if not self._backend or not self._backend.sketch_dir_path:
            return
        project = Path(self._backend.sketch_dir_path).resolve()
        self._buffer_snapshots = {
            path: content for path, content in self._buffer_snapshots.items()
            if Path(path).parent == project
        }
        self._recovery_buffers = {
            path: content for path, content in self._recovery_buffers.items()
            if Path(path).parent == project
        }
        if not self._recovery_buffers:
            self._recovering_buffers = False

    @Slot(str, result="QVariant")
    def run_action(self, action_name: str) -> dict:
        """Monaco toolbar button dispatches an action."""
        if self._backend:
            return self._backend.run_action(action_name)
        return {"success": False}

    @Slot()
    def on_editor_content_change(self) -> None:
        """Fires on keystroke in Monaco."""
        if self._backend:
            self._backend.on_editor_content_change()
        p = self.parent()
        if p and hasattr(p, "_schedule_autosave"):
            p._schedule_autosave()

    # ── AI Review & Diff Bridge Methods ───────────────────────────────────

    @Slot("QVariant", result="QVariant")
    def consume_ai_edit_snapshot(self, path: Any) -> dict:
        """Fetch active AI review snapshot for Monaco diff decorations and review bar."""
        if self._backend and hasattr(self._backend, "ai_review_manager") and self._backend.ai_review_manager:
            return self._backend.ai_review_manager.consume_ai_edit_snapshot(str(path or ""))
        return {}

    @Slot("QVariant", result=bool)
    def has_pending_ai_edit(self, path: Any) -> bool:
        if self._backend and hasattr(self._backend, "ai_review_manager") and self._backend.ai_review_manager:
            return self._backend.ai_review_manager.has_pending_ai_edit(str(path or ""))
        return False

    @Slot("QVariant", result="QVariant")
    def get_pending_ai_review(self, path: Any) -> dict:
        return self.consume_ai_edit_snapshot(path)

    @Slot(result="QVariant")
    def get_ai_edit_reviews(self) -> list:
        if self._backend and hasattr(self._backend, "ai_review_manager") and self._backend.ai_review_manager:
            return self._backend.ai_review_manager.get_ai_edit_reviews()
        return []

    @Slot("QVariant", result="QVariant")
    @Slot("QVariant", "QVariant", result="QVariant")
    def accept_ai_edit(self, path: Any, revision: Any = "") -> dict:
        """User clicked Accept on the AI review bar."""
        p_str = str(path or "")
        rev_str = str(revision or "") if revision is not None else ""
        if self._backend and hasattr(self._backend, "ai_review_manager") and self._backend.ai_review_manager:
            res = self._backend.ai_review_manager.accept_ai_edit(p_str, rev_str)
            if res.get("success"):
                if hasattr(self._backend, "ai_watcher") and self._backend.ai_watcher:
                    self._backend.ai_watcher.note_user_save(p_str, res.get("appliedContent"))
                try:
                    from main.qt.signals import signals as sig_bus
                    sig_bus.ai_review_resolved.emit(p_str)
                except Exception:
                    pass
                file_name = Path(p_str).name
                self._backend.emit("notification", {
                    "title": "AI Edit Accepted",
                    "message": f"Applied changes to {file_name}",
                    "type": "success",
                })
            return res
        return {"success": False, "error": "No backend"}

    @Slot("QVariant", result="QVariant")
    @Slot("QVariant", "QVariant", result="QVariant")
    def reject_ai_edit(self, path: Any, revision: Any = "") -> dict:
        """User clicked Reject / Decline on the AI review bar."""
        p_str = str(path or "")
        rev_str = str(revision or "") if revision is not None else ""
        if self._backend and hasattr(self._backend, "ai_review_manager") and self._backend.ai_review_manager:
            res = self._backend.ai_review_manager.reject_ai_edit(p_str, rev_str)
            if res.get("success"):
                if hasattr(self._backend, "ai_watcher") and self._backend.ai_watcher:
                    self._backend.ai_watcher.note_user_save(p_str, res.get("appliedContent"))
                try:
                    from main.qt.signals import signals as sig_bus
                    sig_bus.ai_review_resolved.emit(p_str)
                except Exception:
                    pass
                file_name = Path(p_str).name
                self._backend.emit("notification", {
                    "title": "AI Edit Rejected",
                    "message": f"Restored original {file_name}",
                    "type": "warning",
                })
            return res
        return {"success": False, "error": "No backend"}

    @Slot("QVariant", result="QVariant")
    def accept_pending_ai_edit(self, path: Any) -> dict:
        return self.accept_ai_edit(path, "")

    @Slot("QVariant", result="QVariant")
    def reject_pending_ai_edit(self, path: Any) -> dict:
        return self.reject_ai_edit(path, "")

    @Slot(result="QVariant")
    def get_ai_history_state(self) -> dict:
        if self._backend and hasattr(self._backend, "ai_review_manager") and self._backend.ai_review_manager:
            return self._backend.ai_review_manager.get_ai_history_state()
        return {"canUndo": False, "canRedo": False}

    @Slot(result="QVariant")
    @Slot("QVariant", result="QVariant")
    def undo_ai_edit_decision(self, force: Any = False) -> dict:
        if self._backend and hasattr(self._backend, "ai_review_manager") and self._backend.ai_review_manager:
            res = self._backend.ai_review_manager.undo_ai_edit_decision(bool(force))
            if res.get("success") and res.get("path") and hasattr(self._backend, "ai_watcher") and self._backend.ai_watcher:
                self._backend.ai_watcher.note_user_save(res["path"], res.get("appliedContent"))
            return res
        return {"success": False, "error": "No backend"}

    @Slot(result="QVariant")
    @Slot("QVariant", result="QVariant")
    def redo_ai_edit_decision(self, force: Any = False) -> dict:
        if self._backend and hasattr(self._backend, "ai_review_manager") and self._backend.ai_review_manager:
            res = self._backend.ai_review_manager.redo_ai_edit_decision(bool(force))
            if res.get("success") and res.get("path") and hasattr(self._backend, "ai_watcher") and self._backend.ai_watcher:
                self._backend.ai_watcher.note_user_save(res["path"], res.get("appliedContent"))
            return res
        return {"success": False, "error": "No backend"}

    @Slot(result="QVariant")
    def undo_ai_decision(self) -> dict:
        return self.undo_ai_edit_decision(False)

    @Slot(result="QVariant")
    def redo_ai_decision(self) -> dict:
        return self.redo_ai_edit_decision(False)

    @Slot(result=bool)
    def unlock_editor(self) -> bool:
        """Force-unlock the Monaco editor (readOnly=false) and clear all AI lock flags."""
        return True  # Handled client-side by window.unlockEditorNow()

    # ── Called FROM Python → Monaco (via signals) ─────────────────────────

    def load_file_in_editor(self, file_path: str, content: str) -> None:
        """Trigger Monaco to open a file buffer."""
        self.file_loaded.emit(file_path, content)

    def set_editor_theme(self, theme: str) -> None:
        self.theme_changed.emit(theme)


class MonacoEditorPanel(QWidget):
    """
    Monaco editor embedded in a QWebEngineView.

    The editor HTML is loaded from ``src/editor/index.html`` using a
    ``file://`` URL (offline, no CDN). Communication uses QWebChannel.
    """

    def __init__(self, backend: "MCUWebBackendAPI", project_root: Path,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._backend = backend
        self._project_root = project_root
        self._editor_html = project_root / "src" / "editor" / "index.html"
        from src.modules.recovery import RecoveryBudget
        self._renderer_recovery = RecoveryBudget(delays=(0.5, 1.5, 4.0), window=120)
        self._recovery_pending = False

        from main.core.config import get_autosave_settings
        self._autosave_enabled, self._autosave_delay = get_autosave_settings()
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self._on_autosave_timeout)

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._recovery_notice = QWidget(self)
        notice_layout = QHBoxLayout(self._recovery_notice)
        self._recovery_text = QLabel()
        self._recovery_text.setWordWrap(True)
        notice_layout.addWidget(self._recovery_text, 1)
        retry = QPushButton("Reload editor")
        retry.clicked.connect(self._manual_editor_reload)
        notice_layout.addWidget(retry)
        self._recovery_notice.hide()
        layout.addWidget(self._recovery_notice)

        # ── QWebEngineView ──────────────────────────────────────────────────
        self._view = QWebEngineView(self)
        self._view.setProperty("mcuTextPointer", True)

        # Configure WebEngine settings for local file access + offline Monaco
        settings = self._view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)

        from src.modules.runtime_resources import performance_profile
        self._performance = performance_profile()

        settings.setAttribute(
            QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled,
            not self._performance.low_memory
        )
        settings.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, False)

        # Utilize lightweight in-memory cache for Monaco offline assets (16 MB saves ~112MB RAM)
        profile = self._view.page().profile()
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        profile.setHttpCacheMaximumSize(16 * 1024 * 1024)

        # Lock WebEngine page zoom factor to 1.0 (prevent Chromium zoom accelerator from corrupting flexbox layout)
        page = self._view.page()
        if hasattr(page, "zoomFactorChanged"):
            page.zoomFactorChanged.connect(self._on_zoom_factor_changed)

        if hasattr(self._view, "renderProcessTerminated"):
            self._view.renderProcessTerminated.connect(self._on_render_process_terminated)

        # Intercept Ctrl + / Ctrl - / Ctrl 0 / Ctrl Wheel before Chromium zoom accelerator
        self._view.installEventFilter(self)
        focus_proxy = self._view.focusProxy()
        if focus_proxy:
            focus_proxy.installEventFilter(self)

        # ── QWebChannel ─────────────────────────────────────────────────────
        self._channel = QWebChannel(self._view.page())
        self._bridge  = EditorBridgeAPI(self._backend, parent=self)
        self._channel.registerObject("editorBridge", self._bridge)
        self._view.page().setWebChannel(self._channel)

        # Hook page load to inject the webchannel init script
        self._view.loadFinished.connect(self._on_load_finished)

        # Ensure page background matches active theme to prevent white flash
        try:
            from main.core.config import get_theme_mode
            from main.qt.theme import get_palette
            # pyrefly: ignore [missing-import]
            from PySide6.QtGui import QColor
            pal = get_palette(get_theme_mode())
            bg_col = pal.get("BG_DARK", "#002b36")
            self._view.page().setBackgroundColor(QColor(bg_col))
            self._view.setStyleSheet(f"background-color: {bg_col};")
        except Exception:
            pass

        layout.addWidget(self._view)

        # Load the editor
        self._load_editor()

    def _load_editor(self) -> None:
        if self._editor_html.exists():
            url = QUrl.fromLocalFile(str(self._editor_html.resolve()))
            self._view.load(url)
        else:
            # Fallback: blank page with error message
            self._view.setHtml(
                "<html><body style='background:#0d1117;color:#e74c3c;font-family:Consolas;padding:20px;'>"
                f"<h2>Monaco editor not found</h2>"
                f"<p>Expected: <code>{self._editor_html}</code></p>"
                "</body></html>"
            )

    @Slot(bool)
    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            self._recover_editor("The offline editor could not load.")
            return
        self._recovery_notice.hide()
        focus_proxy = self._view.focusProxy()
        if focus_proxy:
            focus_proxy.installEventFilter(self)

        # Ensure QWebChannel and project loading runs in JS
        self._view.page().runJavaScript(_WEBCHANNEL_INIT_JS)
        self._view.page().runJavaScript(
            f"window.setPerformanceMode?.({json.dumps(self._performance.constrained)})"
        )

        # Apply configured editor font size
        try:
            from main.core.config import get_editor_font_size
            self.set_font_size(get_editor_font_size())
        except Exception:
            pass

        # Apply active theme immediately upon page load
        try:
            from main.core.config import get_theme_mode
            self.set_theme(get_theme_mode())
        except Exception:
            pass

        # If an active file is set in the backend, activate its tab
        if self._backend and self._backend.active_file_path:
            self.open_file(self._backend.active_file_path)

        # Check if there is an active pending review to present in editor
        if self._backend and hasattr(self._backend, "ai_review_manager") and self._backend.ai_review_manager:
            reviews = self._backend.ai_review_manager.get_ai_edit_reviews()
            if reviews:
                QTimer.singleShot(700, lambda: self.trigger_ai_review(reviews[0].get("path", "")))

    @Slot(str)
    def trigger_ai_review(self, file_path: str) -> None:
        """Trigger Monaco to reload the active file with diff highlights and show the review banner."""
        if not file_path:
            return
        path_json = json.dumps(str(file_path))
        js = (
            f"if (typeof window.reloadActiveFileWithDiff === 'function') {{"
            f"  window.reloadActiveFileWithDiff({path_json});"
            f"}}"
        )
        self._view.page().runJavaScript(js)

    def open_file(self, file_path: str) -> None:
        """Activate the file tab in Monaco, or reload project if not present."""
        if not file_path:
            return
        path_json = json.dumps(str(file_path))
        js = (
            f"if (typeof window.activateProjectFile === 'function') {{"
            f"  if (!window.activateProjectFile({path_json})) {{"
            f"    if (typeof window.safeLoadProject === 'function') {{"
            f"      window.safeLoadProject().then(() => {{"
            f"        if (typeof window.activateProjectFile === 'function') window.activateProjectFile({path_json});"
            f"      }});"
            f"    }}"
            f"  }}"
            f"}}"
        )
        self._view.page().runJavaScript(js)

    def reload_project(self) -> None:
        """Trigger Monaco to reload project files and tabs."""
        js = (
            "if (typeof window.safeLoadProject === 'function') { "
            "  window.safeLoadProject(); "
            "} else if (typeof window.loadProject === 'function') { "
            "  window.loadProject(); "
            "}"
        )
        self._view.page().runJavaScript(js)

    def set_theme(self, theme_name: str) -> None:
        """Change the Monaco editor colour theme."""
        try:
            from main.qt.theme import get_palette
            # pyrefly: ignore [missing-import]
            from PySide6.QtGui import QColor
            pal = get_palette(theme_name)
            bg_col = pal.get("BG_DARK", "#002b36")
            self._view.page().setBackgroundColor(QColor(bg_col))
            self._view.setStyleSheet(f"background-color: {bg_col};")
        except Exception:
            pass
        theme_json = json.dumps(str(theme_name))
        js = f"if (typeof window.setEditorTheme === 'function') {{ window.setEditorTheme({theme_json}); }}"
        self._view.page().runJavaScript(js)

    def _on_zoom_factor_changed(self, factor: float) -> None:
        """Lock web page zoom factor to 1.0 so Monaco font size changes don't zoom the outer page."""
        if abs(factor - 1.0) > 0.01:
            self._view.setZoomFactor(1.0)
            self.force_layout()

    def _on_render_process_terminated(self, termination_status: Any, exit_code: int) -> None:
        self._bridge.begin_buffer_recovery()
        self._recover_editor(f"Editor renderer stopped (exit {exit_code}).")

    def _recover_editor(self, reason):
        if self._recovery_pending:
            return
        delay = self._renderer_recovery.next_delay()
        if delay is None:
            message = reason + " Automatic recovery stopped after repeated failures. Reopen the project or restart the app."
        else:
            message = reason + f" Reloading in {delay:g}s; recent unsaved buffer snapshots will be restored."
            self._recovery_pending = True
            QTimer.singleShot(int(delay * 1000), self._recovery_reload)
        self._recovery_text.setText(message)
        self._recovery_notice.show()
        if self._backend:
            self._backend.emit("console:log", {"text": message, "tag": "warning", "newline": True})

    def _recovery_reload(self):
        self._recovery_pending = False
        self._load_editor()

    def _manual_editor_reload(self):
        if self._recovery_pending:
            return
        from src.modules.recovery import RecoveryBudget
        self._renderer_recovery = RecoveryBudget(window=120)
        self._bridge.begin_buffer_recovery()
        self._recovery_reload()

    def _step_font_size(self, delta: int) -> None:
        """Step editor font size independently from monitor font size."""
        current = getattr(self, "_current_font_size", None)
        if current is None:
            try:
                from main.core.config import get_editor_font_size
                current = int(get_editor_font_size())
            except Exception:
                current = 13
        if delta == 0:
            new_sz = 13
        else:
            new_sz = max(6, min(48, current + delta))
        try:
            from main.core.config import set_editor_font_size
            if set_editor_font_size(new_sz) is False:
                from main.qt.preferences import report_preference_failure
                report_preference_failure("Editor font size")
                return
            self._current_font_size = new_sz
            from main.qt.signals import signals as sig_bus
            if hasattr(sig_bus, "editor_font_size_changed"):
                sig_bus.editor_font_size_changed.emit(new_sz)
        except Exception:
            from main.qt.preferences import report_preference_failure
            report_preference_failure("Editor font size")
            return
        self.set_font_size(new_sz)

    def eventFilter(self, watched: QObject, event: Any) -> bool:
        if event.type() == QEvent.Type.KeyPress:
            modifiers = event.modifiers()
            if modifiers & Qt.KeyboardModifier.ControlModifier:
                key = event.key()
                text = event.text()
                if key == Qt.Key.Key_F and modifiers & Qt.KeyboardModifier.ShiftModifier:
                    self.show_project_search()
                    return True
                elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal) or text in ("+", "="):
                    self._step_font_size(1)
                    return True
                elif key in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore) or text in ("-", "_"):
                    self._step_font_size(-1)
                    return True
                elif key == Qt.Key.Key_0 or text == "0":
                    self._step_font_size(0)
                    return True
        elif event.type() == QEvent.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                y_delta = event.angleDelta().y()
                if y_delta != 0:
                    delta = 1 if y_delta > 0 else -1
                    self._step_font_size(delta)
                return True

        return super().eventFilter(watched, event)

    def set_font_size(self, size: int) -> None:
        """Update Monaco editor font size."""
        try:
            sz = max(6, min(48, int(size)))
        except (ValueError, TypeError):
            sz = 13
        self._current_font_size = sz
        js = (
            f"if (typeof window.setEditorFontSize === 'function') {{ "
            f"  window.setEditorFontSize({sz}); "
            f"}} else if (window.editorInstance) {{ "
            f"  window.editorInstance.updateOptions({{ fontSize: {sz} }}); "
            f"  if (typeof window.editorInstance.layout === 'function') window.editorInstance.layout(); "
            f"}}"
        )
        self._view.page().runJavaScript(js)
        QTimer.singleShot(40, self.force_layout)

    def trigger_save(self) -> None:
        self._view.page().runJavaScript(
            "if (typeof window.saveActiveFile === 'function') { window.saveActiveFile(); }"
        )

    def trigger_save_all(
        self,
        callback: Optional[Callable[[], None]] = None,
        failure_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        # runJavaScript returns before an async Promise resolves. A WebChannel
        # acknowledgement is required before Compile/Upload can consume disk files.
        if getattr(self, "_save_pending", None):
            if failure_callback:
                failure_callback()
            return
        import uuid
        token = uuid.uuid4().hex
        failures = self._bridge._save_failure_count
        self._save_pending = token

        def finished(received, success):
            if received != token or self._save_pending != token:
                return
            self._save_pending = None
            self._bridge.save_finished.disconnect(finished)
            success = success and self._bridge._save_failure_count == failures
            if success and callback:
                callback()
            elif not success and self._backend:
                self._backend.emit("console:log", {"text": "Save All failed or timed out. Check the editor before compiling or uploading.", "tag": "error", "newline": True})
            if not success and failure_callback:
                failure_callback()

        self._bridge.save_finished.connect(finished)
        QTimer.singleShot(30000, self, lambda: finished(token, False))
        js = (
            "(async () => {"
            " let ok = false; try {"
            "  if (typeof window.saveAllFilesSafe === 'function') { await window.saveAllFilesSafe(); }"
            "  else if (typeof window.saveAllFiles === 'function') { await window.saveAllFiles(); }"
            "  else { throw new Error('Editor is not ready'); }"
            "  ok = true; } catch (error) { console.error(error); }"
            f" if (window.editorBridge) window.editorBridge.finish_save_request({json.dumps(token)}, ok);"
            "})()"
        )
        self._view.page().runJavaScript(js)

    def trigger_reload(self) -> None:
        """Reload through Monaco's own model and dirty-tab bookkeeping."""
        self._view.page().runJavaScript(
            "(async () => { let ok = false; try {"
            " if (typeof window.reloadActiveFile === 'function') {"
            "   ok = (await window.reloadActiveFile()) === true; }"
            " } catch (error) { console.error(error); }"
            " if (window.editorBridge) window.editorBridge.finish_reload_request(ok);"
            "})()"
        )

    def _schedule_autosave(self) -> None:
        """Debounce auto-save when user edits text in Monaco."""
        if not getattr(self, "_autosave_enabled", False):
            return
        delay = getattr(self, "_autosave_delay", 1500)
        if hasattr(self, "_autosave_timer"):
            self._autosave_timer.start(delay)

    def _on_autosave_timeout(self) -> None:
        """Trigger background file save on debounce expiration."""
        if getattr(self, "_autosave_enabled", False):
            if self._backend and hasattr(self._backend, "ai_review_manager") and self._backend.ai_review_manager:
                if self._backend.ai_review_manager.has_any_pending_ai_edits():
                    return
            self.trigger_save_all()

    @Slot(bool, int)
    def _on_autosave_settings_changed(self, enabled: bool, delay_ms: int) -> None:
        """Update auto-save state dynamically without restart."""
        self._autosave_enabled = enabled
        self._autosave_delay = max(200, delay_ms)
        if not enabled and hasattr(self, "_autosave_timer"):
            self._autosave_timer.stop()

    def connect_signals(self, sig_bus, *, connect_theme: bool = True) -> None:
        """Connect to global Qt signal bus."""
        sig_bus.editor_load_file.connect(self.open_file)
        sig_bus.editor_goto_line.connect(self.goto_line)
        sig_bus.editor_goto_diagnostic.connect(self.goto_diagnostic)
        sig_bus.editor_set_theme.connect(self.set_theme)
        sig_bus.syntax_errors.connect(self.set_markers)
        sig_bus.project_updated.connect(self._on_project_updated)
        if hasattr(sig_bus, "editor_font_size_changed"):
            sig_bus.editor_font_size_changed.connect(self.set_font_size)
        if connect_theme and hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.set_theme)
        if hasattr(sig_bus, "autosave_settings_changed"):
            sig_bus.autosave_settings_changed.connect(self._on_autosave_settings_changed)
        if hasattr(sig_bus, "ai_review_requested"):
            sig_bus.ai_review_requested.connect(self.trigger_ai_review)
        if hasattr(sig_bus, "ai_review_resolved"):
            sig_bus.ai_review_resolved.connect(self._on_ai_review_resolved)

    @Slot(str)
    def _on_ai_review_resolved(self, _path: str) -> None:
        """After accept/reject, guarantee the editor is writable (safety flush)."""
        self._view.page().runJavaScript(
            "if (typeof window.unlockEditorNow === 'function') { window.unlockEditorNow(); }"
        )

    @Slot(list)
    def set_markers(self, diagnostics: list) -> None:
        """Forward diagnostic errors/warnings to Monaco inline squiggly markers."""
        try:
            diag_json = json.dumps(diagnostics)
            js = f"if (typeof window.setEditorMarkers === 'function') {{ window.setEditorMarkers({json.dumps(diag_json)}); }}"
            self._view.page().runJavaScript(js)
        except Exception:
            pass

    @Slot(dict)
    def _on_project_updated(self, payload: dict) -> None:
        """Handle project folder change or new sketch creation."""
        self._bridge.retain_current_project_buffers()
        search = getattr(self, "_search_dialog", None)
        if search and self._backend and search.project != Path(self._backend.sketch_dir_path):
            search.close()
            search.deleteLater()
            self._search_dialog = None
        self.reload_project()
        active = payload.get("active_file")
        if active:
            self.open_file(active)

    def goto_line(self, file_path: str, line_no: int) -> None:
        """Navigate to file and scroll to line in Monaco editor."""
        self.goto_location(file_path, line_no)

    @Slot(dict)
    def goto_diagnostic(self, diagnostic: dict) -> None:
        """Reveal the exact diagnostic range at the top without reloading buffers."""
        self._navigate_source({
            "path": str(diagnostic.get("file") or ""),
            "line": diagnostic.get("line", 1),
            "column": diagnostic.get("col", 1),
            "endLine": diagnostic.get("endLine"),
            "endColumn": diagnostic.get("endCol"),
            "columnEncoding": diagnostic.get("columnEncoding", "codepoint"),
            "reveal": "top",
        })

    def goto_location(self, file_path: str, line_no: int, column: int = 1, end_column: int = 1) -> None:
        """Wait for file activation before positioning a search/syntax result."""
        line_no, column = max(1, int(line_no)), max(1, int(column))
        end_column = max(column, int(end_column))
        self._navigate_source({"path": str(file_path or ""), "line": line_no,
                               "column": column, "endLine": line_no,
                               "endColumn": end_column, "reveal": "center"})

    def _navigate_source(self, location: dict) -> None:
        host = self.window()
        if host is not None:
            if host.isMinimized():
                host.showNormal()
            reveal_editor = getattr(host, "reveal_editor_for_navigation", None)
            if callable(reveal_editor):
                reveal_editor()
            host.raise_()
            host.activateWindow()
        self._view.setFocus(Qt.FocusReason.OtherFocusReason)
        self._view.page().runJavaScript(f"window.navigateToSource?.({json.dumps(location)})")

    def show_project_search(self) -> None:
        """Search current text without saving files or changing dirty tabs."""
        if not self._backend or not self._backend.sketch_dir_path:
            return
        requested_project = Path(self._backend.sketch_dir_path)
        requested_buffers = dict(self._bridge._buffer_snapshots)

        def show(snapshot):
            if isinstance(snapshot, str):
                try:
                    snapshot = json.loads(snapshot)
                except (ValueError, TypeError):
                    snapshot = {}
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            project = Path(self._backend.sketch_dir_path)
            if project != requested_project:
                return
            search = getattr(self, "_search_dialog", None)
            if search is None or search.project != project or search.parentWidget() is not self.window():
                if search:
                    search.close()
                    search.deleteLater()
                from main.qt.project_search import ProjectSearchDialog
                search = ProjectSearchDialog(project, self._search_buffers, self.goto_location, self.window())
                self._search_dialog = search
            snapshot["buffer_present"] = snapshot.get("path") in requested_buffers
            snapshot["buffer_baseline"] = requested_buffers.get(snapshot.get("path"))
            self._search_active_snapshot = snapshot
            search.open_search(str(snapshot.get("selection", "")))

        self._view.page().runJavaScript(
            "(() => { const editor = window.editorInstance, model = editor?.getModel();"
            " const tab = document.querySelector('#tab-bar .tab.active');"
            " if (!model || !tab?._filePath) return '{}';"
            " const selection = editor.getSelection();"
            " const snapshot = {path: tab._filePath, selection: selection &&"
            " model.getValueLengthInRange(selection) <= 512 ? model.getValueInRange(selection) : ''};"
            " if (model.getValueLength() <= 2 * 1024 * 1024) snapshot.content = model.getValue();"
            " return JSON.stringify(snapshot); })()", show
        )

    def _search_buffers(self) -> dict[str, str]:
        buffers = dict(self._bridge._buffer_snapshots)
        active = getattr(self, "_search_active_snapshot", {})
        # The last keystroke can still be crossing WebChannel at shortcut time.
        # Use the renderer snapshot only if no newer edit/save has arrived.
        path = active.get("path")
        unchanged = ((path in buffers) == active.get("buffer_present", False)
                     and buffers.get(path) == active.get("buffer_baseline"))
        if path and "content" in active and unchanged:
            buffers[active["path"]] = active["content"]
        elif not unchanged:
            self._search_active_snapshot = {}
        return buffers

    def force_layout(self) -> None:
        """Force Monaco to instantly recalculate geometry and repaint."""
        if not hasattr(self, "_view") or not self._view:
            return
        page = self._view.page()
        if not page:
            return
        js = (
            "if (typeof window.forceEditorLayout === 'function') {"
            "  window.forceEditorLayout();"
            "} else if (window.editorInstance && typeof window.editorInstance.layout === 'function') {"
            "  window.editorInstance.layout();"
            "}"
        )
        page.runJavaScript(js)
        if hasattr(self._view, "update"):
            self._view.update()

    def has_input_focus(self) -> bool:
        """Include Chromium's native focus proxy when preserving editor focus."""
        focused = QApplication.focusWidget()
        return bool(focused and (focused is self._view or self._view.isAncestorOf(focused)))

    def restore_input_focus(self) -> None:
        """Restore only an explicitly preserved editing focus, without reloading."""
        if not self.isVisible() or not self._view.isEnabled():
            return
        self._view.setFocus(Qt.FocusReason.OtherFocusReason)
        self._view.page().runJavaScript(
            "if (window.editorInstance?.getModel()) { window.editorInstance.focus(); }"
        )

    def showEvent(self, event) -> None:
        """Instantly wake up Monaco and recalculate layout upon unhide/show."""
        super().showEvent(event)
        if hasattr(self, "_view") and self._view:
            self._view.show()
        self.force_layout()
        QTimer.singleShot(16, self.force_layout)
        QTimer.singleShot(60, self.force_layout)
        if self._backend and self._backend.active_file_path:
            self.open_file(self._backend.active_file_path)

    @property
    def bridge(self) -> EditorBridgeAPI:
        return self._bridge
