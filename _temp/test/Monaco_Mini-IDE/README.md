# Monaco Mini-IDE (pywebview starter)

A minimal desktop code editor: **Monaco Editor** (the same editor component
that powers VS Code) rendered inside a native window via **pywebview**,
with Python handling all file I/O.

## How it's structured

```
monaco_pywebview_editor/
├── main.py            # Python side: creates the window, exposes an Api
│                       # class (open/save/new) to JavaScript
├── web/
│   └── index.html      # UI side: Monaco editor + toolbar + JS that calls
│                       # back into Python through window.pywebview.api
└── requirements.txt
```

The key idea: **Python owns the OS** (windows, dialogs, files), **JS owns
the editor UI**. They talk to each other through pywebview's JS bridge:

- JS -> Python: `await pywebview.api.some_method(args)`
- Python -> JS: `window.evaluate_js("someJsFunction()")`

This is the same mental model Electron apps use (main process vs.
renderer process) — pywebview is just a much lighter-weight way to get
there in Python, using your OS's native webview (Edge WebView2 on
Windows, WebKitGTK on Linux) instead of shipping a whole Chromium.

## Setup

On Windows (since you're doing embedded dev there):

```powershell
pip install -r requirements.txt
python main.py
```

On Linux (Ubuntu side of your dual boot), pywebview needs a GTK backend:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-webkit2-4.1
pip install -r requirements.txt
python3 main.py
```

First launch downloads Monaco from a CDN (jsDelivr) at runtime, so you'll
need internet access the first time the window opens. See "going fully
offline" below if you want to remove that dependency.

## What it already does

- New / Open / Save / Save As, with native OS file dialogs
- Ctrl+S to save
- Auto-detects language from file extension (`.py` → Python, `.js` →
  JavaScript, etc.) for syntax highlighting
- Manual language switcher dropdown (every language Monaco ships with)
- Dark theme, minimap, line/column status bar

## Natural next steps, roughly in order of difficulty

1. **Tabs / multiple open files** — keep a list of `{path, model}` and swap
   `editor.setModel()` when you switch tabs. Monaco's model system is built
   exactly for this (each open file = one `monaco.editor.ITextModel`).
2. **A file tree sidebar** — add a Python method `list_dir(path)` returning
   folder contents as JSON, and a "Open Folder" dialog
   (`webview.FOLDER_DIALOG`); render a collapsible tree in JS.
3. **Unsaved-changes indicator** — listen to `editor.onDidChangeModelContent`
   and show a dot/asterisk in the tab or title bar; prompt before closing.
4. **Running code** — add a Python method that shells out
   (`subprocess.run(["python", path], capture_output=True)`) and pipe
   stdout/stderr back to a terminal-style panel in the UI.
5. **Settings persistence** — remember window size, last-opened folder,
   theme, and font size in a small JSON file in the user's config dir.
6. **Going fully offline** — `npm install monaco-editor`, copy the `vs/`
   folder from `node_modules/monaco-editor/min/` into `web/vs/`, and change
   the two CDN URLs in `index.html` to `vs/loader.js` and `vs`. Then the
   whole app runs with zero network access, which matters if you ever
   package this as a standalone `.exe`.
7. **Packaging** — `pyinstaller --onefile --add-data "web;web" main.py`
   (Windows) gets you a distributable `.exe`. You already have experience
   wiring up VBS launchers / bootstrap scripts for portable deployment on
   `mcu_flash_gui.py`, so this part should feel familiar.

## A note on debugging

`webview.start(debug=True)` in `main.py` enables right-click → "Inspect
Element" inside the window, which opens normal browser devtools. That's
where you'll debug the JS side (console.log, breakpoints, network tab for
the Monaco CDN load) — same workflow as debugging any web page.
