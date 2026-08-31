---
trigger: always_on
---

## Multi-User Deployment Awareness

This project (MCU Flasher by Naph) will be deployed across multiple Windows 10/11 machines, not just one or two dev PCs. Always consider:

- **No hardcoded paths, usernames, or drive letters.** Never assume `C:\Users\napht\...` or similar — resolve paths dynamically (e.g. `os.path.expanduser("~")`, `os.environ["USERPROFILE"]`, `sys.executable`, relative-to-script paths).
- **No hardcoded system state assumptions.** Don't assume a specific Python version, install location, PATH configuration, admin privileges, or pre-installed tools (arduino-cli, npm, winget, etc.) exist on the target machine — always detect/verify first, and handle the case where they don't.
- **Environment differences across machines.** Account for different Windows builds, differing user permission levels, differing locale/username character sets, and antivirus/PATH refresh quirks that may vary machine to machine.
- **Dynamic over static.** When implementing detection, install, config, or file-resolution logic, prefer dynamic discovery (registry lookups, `where`/`shutil.which`, querying installed versions, reading config at runtime) over fixed/static values baked into code.
- **Test the "first run on a fresh machine" scenario mentally** for any new feature — would this work on a clean Windows 10/11 install with a different username than `napht`?

**Agent enforcement:** Before finalizing any generated code or edit, scan for hardcoded user-specific paths (e.g. containing `napht`), fixed drive letters, or assumptions of a single-machine environment. If found, flag them explicitly and propose a dynamic alternative instead of silently leaving them in.

---

## Threading & UI Safety (Tkinter)

The GUI is a monolithic Tkinter application (`mcu_flash_gui.py`, ~31k lines). Threading violations are a primary source of freeze/crash bugs.

- **All GUI updates from worker threads MUST go through `self.root.after(0, callback)`.** Never call `.config()`, `.insert()`, `.delete()`, `.set()`, or any Tkinter widget method directly from a non-main thread.
- **Detached windows (editor, panels) must handle `WM_DELETE_WINDOW` gracefully.** Closing a detached window via the native `[X]` button must not trigger thread deadlocks or freeze the main application. Always use `protocol("WM_DELETE_WINDOW", handler)` and ensure the handler runs reattach/cleanup on the main thread.
- **Non-blocking operations:** Compile, upload, serial read, and AI assistant tasks MUST run on background threads using `threading.Thread`. Never block the Tkinter main loop.
- **`_active_operation` phase scoping:** Respect the phase system — `"compile"` leaves serial monitor active; `"upload"` / `"flash"` / `"reset"` locks the COM port and pauses the monitor.

---

## Monaco Editor & pywebview (Windows)

The Monaco editor runs inside a pywebview WebView2 window that is Win32-reparented into the Tkinter frame. Key patterns:

- **WebView2 runtime is required.** The bootstrap pipeline installs `installers/MicrosoftEdgeWebview2Setup.exe` if WebView2 is not detected. Never assume it's pre-installed.
- **Win32 reparenting:** The pywebview OS-level window HWND is reparented into a Tkinter frame using `SetParent` and `SetWindowPos` Win32 calls. This applies to both the Monaco editor and the AI assistant panel.
- **Tk/WebView2 event loop conflicts:** The Project Terminal and AI Assistant run their pywebview instances as subprocesses to avoid competing with the Tkinter main loop.
- **Offline-only Monaco:** The Monaco editor uses `bundle.js` locally — never reference CDN URLs. All assets (fonts, JS) must be shipped offline in `src/editor/`.
- **JS ↔ Python bridge:** Python calls JS via `editor_window.evaluate_js("functionName()")`. JS calls Python via `window.pywebview.api.methodName()`. When adding new callable functions, expose them on the `window` object in JS and as methods on `EditorApi` in Python.

---

## Project Terminal & Shell Integration

The integrated terminal (`src/modules/project_terminal.py`) uses the same pywebview + xterm.js + pywinpty architecture as the AI assistant.

- **Shell switching (pwsh ↔ cmd):** Each shell type gets its own xterm instance to preserve scrollback. Switching shells must reinitialize environment variables correctly — failing to do so corrupts PATH and prompt rendering.
- **Subprocess isolation:** Terminal runs as a child process to prevent Tk event loop conflicts.
- **Port allocation:** HTTP and WebSocket servers use dynamically allocated free ports (never hardcode port numbers).

---

## Network & UNC Path Support

The app supports compiling/uploading sketches from remote network shares (SMB/UNC paths like `\\server\share`).

- **Always use `is_unc_or_network_path()` and `_unc_share_root()`** to detect network locations before build operations.
- **Drive mapping:** `_map_unc_for_build()` / `_unmap_unc_after_build()` map UNC shares to a free drive letter for subprocess CWD compatibility. Always clean up in `finally:` blocks.
- **Remote workspace isolation:** PlatformIO build workspaces for remote projects go to local fast storage (`remote_workspaces/<project>_<hash>/`) to avoid SMB `.sconsign*.dblite` locking errors.
- **Pre-create build directories on network paths.** SCons/PlatformIO's `dbm`/`dblite` module throws `FileNotFoundError` on SMB shares if the parent directory doesn't exist. Always `mkdir(parents=True, exist_ok=True)` the exact target environment directories before launching the build.
- **Never assume all paths are local.** Any code that touches the sketch path, build CWD, or subprocess environment must handle both local (`C:\...`) and UNC (`\\server\share\...`) paths.

---

## Bootstrap & First-Run Pipeline

The bootstrap system (`src/modules/bootstrap.py`) must handle a completely fresh machine with no pre-installed tools.

- **Private Python runtime:** `_heal_private_python_runtime()` auto-detects and repairs the portable Python at `src/_python/`. Uses the bundled installer from `installers/.handsoff/python-*-amd64.exe`.
- **Pre-built PlatformIO seeding:** `_ensure_platformio_core_prebuilt()` downloads a ~1.7GB pre-built toolchain zip with resume, SHA256 verification, and progress. Falls back gracefully to slower pip-based installation on failure.
- **Multi-Attempt Toolchain Retries:** Transient network issues during platform installation or dummy compile prewarming are retried automatically (`_PLATFORMIO_SETUP_ATTEMPTS = 3`). Non-default platforms defer prewarming until first actual use to ensure instant application startup.
- **Google Drive downloads use urllib (not requests)** because bootstrap runs before pip dependencies are installed. The downloader handles Google's virus-scan confirmation HTML page by parsing the form and extracting hidden fields.
- **`[WinError 32]` file locks:** Antivirus scanners may lock `.zip.part` download files. Always handle with retry logic, exponential backoff, and clear error messages. Never crash on a lock error.
- **Progress feedback is mandatory:** Downloads, extractions, and toolchain installs must show progress (percent, speed, ETA) inline in the bootstrap console.

---

## Board Resolution Safety & Discovery

- **Safe Board Metadata Access:** In any GUI mixin or helper method inspecting board parameters (such as `platform`, `board`, `framework`, `arduino_board_id`), always call `board_info = self._resolve_board_info()`. Never assume `board_info` is already in local scope or rely on unverified global dictionaries.
- **Unified Toolchain Backend:** All boards (ESP32, ESP8266, AVR) compile and flash through the unified PlatformIO toolchain and environment pipeline.
- **Multi-Root Board Discovery:** Board definitions and USB VID/PID detection scan both app-local (`Boards/`) and standard Arduino packages (`%LOCALAPPDATA%/Arduino15/packages`). Duplicate board display names are disambiguated by appending `(arduino_board_id)`.

---

## File & Project Hygiene

- **User files are sacred.** Never delete, hide, or modify user sketch files (`.ino`, `.cpp`, `.c`, `.h`, `.hpp`, `.txt`, and unrecognized files). Only app-generated paths may be hidden or cleaned.
- **Allowlist-based cleanup only.** Manual Clean and cache deletion operations must use explicit generated-path allowlists. Never infer that an unknown file is app-generated.
- **Windows hidden attributes:** Use `hide_generated_directory()` for directories (shallow, no child attribute changes), `hide_hidden_attribute()` for files, and `ensure_file_writable()` before overwriting generated files.
- **Board workspaces are isolated:** Each exact board gets its own `.mcu_flasher_build_cache/boards/<board-key>/` workspace. Never cross-contaminate board caches.
- **File distribution:** Runtime configs go in `src/dbs/`, logs go in `logs/`, modules go in `src/modules/`. The `src/libs/` directory is legacy and empty — do not add new files there.

---

## Subprocess Handling (Windows)

- **Always suppress console windows** for background subprocesses using `CREATE_NO_WINDOW` flags via `src/modules/win_subprocess_hide.py`.
- **PlatformIO long path workaround:** Core dir is junctioned to `C:\.platformio-mcu-gui` to avoid Windows MAX_PATH (>260 char) issues.
- **Timeout handling:** All `subprocess.run()` calls in build/upload pipelines should have reasonable timeouts. Watch for hangs on UNC-mapped drive operations.
- **File locking awareness:** On Windows, file lock errors (`[WinError 32]`) are common — always handle them gracefully with retry logic or clear error messages, especially for downloaded `.zip` files and build caches.

---

## Error Handling & User Feedback

- **Console status messages must be accurate and informative.** Show what's actually happening (downloading, extracting, compiling) with relevant details (speed, percent, board name). Remove misleading or unnecessary statements.
- **Progress feedback for long operations.** Downloads, extractions, and first-time toolchain installs MUST show progress (percent, speed, ETA where feasible).
- **Errors should be actionable.** When something fails, tell the user *what* failed, *why* (if known), and *what they can do* about it. Don't just log a stack trace.
- **Graceful degradation:** Cosmetic failures (hiding files, progress bar glitches) must never block core operations (compile, upload, serial monitor).
- **False-positive detection:** Build exit codes must be interpreted carefully. `rc == 1` with no error keywords does NOT automatically mean "user cancelled" — it may be a PlatformIO `PackageException` or toolchain error. Only classify as user-stopped when `_stop_requested` is set or an OS kill signal (`rc < 0`) occurred.

---

## Test & Temp Directory Isolation (`temp/`, `test/`, `tests/`)

- **Strict zero-touch default:** Never read, scan, search, modify, edit, or update files inside `temp/`, `test/`, or `tests/` during standard maintenance, refactoring, or feature development.
- **Explicit user trigger only:** Only access, view, run, or update files in `temp/`, `test/`, or `tests/` if the user explicitly asks for them in their prompt (e.g. `@temp/...`, `@test/...`, or specifically asking to inspect temp files or run tests).
- **Git exclusion:** Temporary archives and test sandboxes (`temp/`, `test/`, `tests/`, `___*.py`, `old-*`) must remain strictly ignored by git and never be bundled into clean releases or fresh-install distributions.