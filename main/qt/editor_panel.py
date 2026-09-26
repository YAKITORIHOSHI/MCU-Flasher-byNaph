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
  Transport:    QWebChannel over the internal WebSocket transport

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
from PySide6.QtCore import QObject, QUrl, Slot, Signal, QTimer
# pyrefly: ignore [missing-import]
from PySide6.QtWebEngineCore import QWebEngineSettings, QWebEngineProfile
# pyrefly: ignore [missing-import]
from PySide6.QtWebEngineWidgets import QWebEngineView
# pyrefly: ignore [missing-import]
from PySide6.QtWebChannel import QWebChannel
# pyrefly: ignore [missing-import]
from PySide6.QtWidgets import QWidget, QVBoxLayout

# ─────────────────────────────────────────────────────────────────────────────
# QWebChannel JS bootstrap
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

    def __init__(self, backend: "MCUWebBackendAPI", parent: QObject | None = None):
        super().__init__(parent)
        self._backend = backend

    # ── Called FROM Monaco JS ─────────────────────────────────────────────

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
            from main.core.config import get_monitor_font_size
            return int(get_monitor_font_size())
        except Exception:
            return 12

    @Slot(result=str)
    def get_project_dir(self) -> str:
        """Return root project folder path."""
        if self._backend:
            return self._backend.get_project_dir()
        return ""

    @Slot(str, str, result=str)
    def realtime_check_syntax(self, file_path: str, content: str) -> str:
        """Monaco requests real-time syntax diagnostics."""
        if self._backend:
            return self._backend.realtime_check_syntax(file_path, content)
        return "[]"

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

        # ── QWebEngineView ──────────────────────────────────────────────────
        self._view = QWebEngineView(self)

        # Configure WebEngine settings for local file access + offline Monaco
        settings = self._view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, False)

        # Utilize high-performance in-memory cache for Monaco bundle, workers, and web assets
        profile = self._view.page().profile()
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        profile.setHttpCacheMaximumSize(128 * 1024 * 1024)  # 128 MB RAM cache

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
            return
        # Ensure QWebChannel and project loading runs in JS
        self._view.page().runJavaScript(_WEBCHANNEL_INIT_JS)

        # Apply configured font size
        try:
            from main.core.config import get_monitor_font_size
            self.set_font_size(get_monitor_font_size())
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

    def set_font_size(self, size: int) -> None:
        """Update Monaco editor font size."""
        try:
            sz = int(size)
        except (ValueError, TypeError):
            sz = 12
        js = (
            f"if (typeof window.setEditorFontSize === 'function') {{ "
            f"  window.setEditorFontSize({sz}); "
            f"}} else if (window.editorInstance) {{ "
            f"  window.editorInstance.updateOptions({{ fontSize: {sz} }}); "
            f"}}"
        )
        self._view.page().runJavaScript(js)

    def trigger_save(self) -> None:
        self._view.page().runJavaScript(
            "if (typeof window.saveActiveFile === 'function') { window.saveActiveFile(); }"
        )

    def trigger_save_all(self, callback: Optional[Callable[[], None]] = None) -> None:
        js = (
            "(async () => {"
            "  if (typeof window.saveAllFilesSafe === 'function') { await window.saveAllFilesSafe(); }"
            "  else if (typeof window.saveAllFiles === 'function') { await window.saveAllFiles(); }"
            "  if (typeof window.saveActiveFile === 'function') { await window.saveActiveFile(); }"
            "  return true;"
            "})()"
        )
        if callback:
            self._view.page().runJavaScript(js, lambda _: callback())
        else:
            self._view.page().runJavaScript(js)

    def trigger_reload(self) -> None:
        """Reload the currently active file from disk into the editor."""
        try:
            from main.qt.signals import signals as sig_bus
            sig_bus.console_progress.emit({"action": "Reloading"})
            QTimer.singleShot(600, lambda: sig_bus.console_progress.emit({"action": "Completed"}))
        except Exception:
            pass
        if self._backend and self._backend.active_file_path:
            file_path = self._backend.active_file_path
            try:
                content = Path(file_path).read_text(encoding="utf-8", errors="replace")
                path_json = json.dumps(str(file_path))
                content_json = json.dumps(content)
                js = (
                    f"if (typeof window.setFileContent === 'function') {{"
                    f"  window.setFileContent({path_json}, {content_json});"
                    f"}} else if (typeof window.activateProjectFile === 'function') {{"
                    f"  window.activateProjectFile({path_json});"
                    f"}}"
                )
                self._view.page().runJavaScript(js)
            except Exception:
                pass

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

    def connect_signals(self, sig_bus) -> None:
        """Connect to global Qt signal bus."""
        sig_bus.editor_load_file.connect(self.open_file)
        sig_bus.editor_goto_line.connect(self.goto_line)
        sig_bus.editor_set_theme.connect(self.set_theme)
        sig_bus.syntax_errors.connect(self.set_markers)
        sig_bus.project_updated.connect(self._on_project_updated)
        if hasattr(sig_bus, "font_size_changed"):
            sig_bus.font_size_changed.connect(self.set_font_size)
        if hasattr(sig_bus, "theme_changed"):
            sig_bus.theme_changed.connect(self.set_theme)
        if hasattr(sig_bus, "autosave_settings_changed"):
            sig_bus.autosave_settings_changed.connect(self._on_autosave_settings_changed)
        if hasattr(sig_bus, "ai_review_requested"):
            sig_bus.ai_review_requested.connect(self.trigger_ai_review)

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
        self.reload_project()
        active = payload.get("active_file")
        if active:
            self.open_file(active)

    def goto_line(self, file_path: str, line_no: int) -> None:
        """Navigate to file and scroll to line in Monaco editor."""
        if file_path:
            self.open_file(file_path)
        js = (
            f"if (window.editorInstance) {{"
            f"  window.editorInstance.revealLineInCenter({line_no});"
            f"  window.editorInstance.setPosition({{lineNumber: {line_no}, column: 1}});"
            f"  window.editorInstance.focus();"
            f"}}"
        )
        self._view.page().runJavaScript(js)

    def force_layout(self) -> None:
        """Force Monaco to instantly recalculate geometry and repaint."""
        js = (
            "if (typeof window.forceEditorLayout === 'function') {"
            "  window.forceEditorLayout();"
            "} else if (window.editorInstance && typeof window.editorInstance.layout === 'function') {"
            "  window.editorInstance.layout();"
            "}"
        )
        self._view.page().runJavaScript(js)
        if hasattr(self._view, "update"):
            self._view.update()

    def showEvent(self, event) -> None:
        """Instantly wake up Monaco and recalculate layout upon unhide/show."""
        super().showEvent(event)
        self._view.show()
        self.force_layout()
        QTimer.singleShot(16, self.force_layout)
        QTimer.singleShot(60, self.force_layout)
        if self._backend and self._backend.active_file_path:
            self.open_file(self._backend.active_file_path)

    @property
    def bridge(self) -> EditorBridgeAPI:
        return self._bridge
