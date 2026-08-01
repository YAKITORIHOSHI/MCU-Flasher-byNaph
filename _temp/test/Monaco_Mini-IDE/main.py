"""
Monaco Code Editor - pywebview backend
========================================

Architecture:
    - Python (this file) owns the native window and all filesystem access.
    - The UI (web/index.html) runs Monaco Editor inside pywebview's embedded browser.
    - JS talks to Python through the `Api` class below, exposed as `window.pywebview.api`.
    - Python talks to JS by calling `window.evaluate_js(...)`.

This split is exactly how a "real" desktop IDE like VS Code is structured
(Electron main process <-> renderer), just with Python instead of Node
and pywebview's native webview instead of Chromium-in-Electron.
"""

import os
# pyrefly: ignore [missing-import]
import webview

class Api:
    """
    Every public method here becomes callable from JavaScript as
    `pywebview.api.<method_name>(...)`. Methods must return JSON-serializable
    data (str, dict, list, bool, number, None).
    """

    def __init__(self):
        self.current_path = None  # tracks the file currently open, or None for "untitled"

    # ---- file dialogs -------------------------------------------------

    def open_file_dialog(self):
        """Show a native 'Open File' dialog. Returns the chosen path or None."""
        window = webview.windows[0]
        result = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False)
        if not result:
            return None
        path = result[0]
        return self._read_file(path)

    def save_file_dialog(self, content):
        """Show a native 'Save As' dialog, write content, remember the path."""
        window = webview.windows[0]
        result = window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=os.path.basename(self.current_path) if self.current_path else "untitled.txt",
        )
        if not result:
            return None
        path = result if isinstance(result, str) else result[0]
        self._write_file(path, content)
        return {"path": path, "name": os.path.basename(path)}

    # ---- direct file ops (used once a path is already known) ----------

    def save_file(self, content):
        """Save to the currently tracked path. Falls back to Save As if none."""
        if not self.current_path:
            return self.save_file_dialog(content)
        self._write_file(self.current_path, content)
        return {"path": self.current_path, "name": os.path.basename(self.current_path)}

    def new_file(self):
        """Reset tracked path so the next save prompts for a location."""
        self.current_path = None
        return True

    # ---- internal helpers ----------------------------------------------

    def _read_file(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            return {"error": "Cannot open binary or non-UTF-8 file."}
        except OSError as e:
            return {"error": str(e)}

        self.current_path = path
        return {
            "path": path,
            "name": os.path.basename(path),
            "content": content,
        }

    def _write_file(self, path, content):
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        self.current_path = path


def main():
    api = Api()
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

    webview.create_window(
        title="Monaco Mini-IDE",
        url=html_path,
        js_api=api,
        width=1200,
        height=800,
        min_size=(700, 450),
    )
    # debug=True gives you right-click "Inspect Element" in the webview,
    # invaluable while you're building this out.
    webview.start(debug=False)


if __name__ == "__main__":
    main()
