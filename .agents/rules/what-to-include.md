---
trigger: always_on
---

## Platform Scope (Windows 10/11)

This project is built and optimized specifically for Windows 10 and 11 environments. Core files include `mcu_flash_gui.py`, `launcher.py`, `dedicated_AI.py`, `runThisOnWindows.vbs`, `src/modules/bootstrap.py`, `src/libs/win_subprocess_hide.py`, and Windows-bundled installers in `installers/` (`CP210x/`, `arduino-cli.msi`, `msys2-*.exe`, `MicrosoftEdgeWebview2Setup.exe`).
- All toolchain setup, subprocess execution, file hiding attributes (`attrib +h`), win32 APIs, and path handling are tailored for Windows.
- Always use Windows-compatible paths, subprocess flags (e.g. `CREATE_NO_WINDOW`), and ctypes Win32 calls.
- Linux/Ubuntu support has been fully removed. Do not add cross-platform abstractions or mention Linux support in any documentation. Present the app naturally as a Windows desktop application.

## Environment Scope (Experimental vs Actual)

This project has two environments:
- **Experimental** — sandbox for testing changes before they're proven.
- **Actual (Production)** — the verified version, only updated once a change has succeeded in Experimental.

- Only edit files in the environment the user's request refers to. Treat phrases like "test," "try," "experimental" as pointing to Experimental, and "real," "production," "actual," "live" as pointing to Actual.
- Never port a change from Experimental to Actual (or vice versa) unless the user explicitly confirms it succeeded and asks you to apply/promote it.
- If the user doesn't say which environment they mean, ask — don't assume.
- Only edit both environments in one task if explicitly asked.

## Project Structure & File Organization

The project follows a specific directory layout. Respect these conventions when moving files or adding new ones:

- **`src/modules/`** — Core bootstrap and environment management modules (`bootstrap.py`, `launcher.py`, `dedicated_AI.py`).
- **`src/libs/`** — Utility libraries (`arduino_lib_req.py`, `detector.py`, `win_subprocess_hide.py`, `reset_editor.py`).
- **`src/editor/`** — Monaco Editor offline Web UI (`index.html`, `bundle.js`, fonts).
- **`src/dbs/`** — Persistent JSON notification & event database store.
- **`installers/`** — Bundled silent installers, drivers, and offline runtimes. Tracked via Git LFS (see `.gitattributes`).
- **`direct/`** — Direct-launch entry points (`runThisOnWindows.vbs`).
- **`index_json/`** — Arduino board/library index caches.
- **`.mcu_flasher_build_cache/`** — Generated build cache (gitignored, hidden via Windows attributes).
- When moving files, always update all dependent imports, references, VBS paths, and documentation in the same operation.

## Git & Repository Hygiene

- **`.gitignore` is authoritative.** Generated caches, Python bytecode, build artifacts, local configs, and development-only files are gitignored. Respect these patterns.
- **Git LFS** is used for large binaries in `installers/`. Don't try to commit large binaries directly.
- **Commit messages:** When the user asks you to "decide the description," write clear, descriptive commit messages summarizing all changes made. Prefer conventional-commit style when appropriate.
- **Force push awareness:** The user sometimes requests `git push --force`. Only do so when explicitly asked. Always confirm the branch and remote before force pushing.
- **Clean up stale files:** When the user asks to remove files (`.opencode/`, `.claude/`, old reports with `old-` prefix), execute the deletion and update `.gitignore` if needed.

## Agent Behavior & Communication

- **Don't assume — always confirm.** Before taking destructive or irreversible actions (deleting files, force pushing, modifying production), confirm the user's intent. The user has explicitly asked: "Don't assumpt please always confirm everything before taking actions."
- **Don't overthink simple requests.** If the user asks for a small change (e.g., "add a notice to the README"), don't create a full implementation plan or modify unrelated files. Just make the targeted edit.
- **Follow the user's pointed references.** When the user @-mentions specific files, focus edits on those files. Don't expand scope unless asked.
- **Continue interrupted work.** If the user says "Continue" after a crash or interruption, check the conversation transcript and task list from the previous session and resume where you left off.
- **QA Engineer mode.** When asked to "act like a QA Engineer," produce structured defect reports with: reproduction steps, root cause analysis, severity/priority, affected components, and a remediation plan — before writing any code.

## Test & Temp Directory Isolation (`test/`, `tests/`, `temp/`)

- **Strict zero-touch default:** Never read, scan, view, search, edit, modify, or delete any files inside `test/`, `tests/`, or `temp/` during standard development, refactoring, or maintenance tasks.
- **Explicit user trigger only:** Only access, view, read, update, or touch files in `temp/`, `test/`, or `tests/` if the user explicitly asks for it in their prompt (e.g., `@temp/...`, `@test/...`, or specifically asking to inspect/use temporary files or run tests).
- **Excluded from Git:** The `temp/`, `test/`, and `tests/` directories are strictly gitignored and must never be tracked or committed to the repository for clean releases and fresh installations.
- **Search exclusion:** When performing repository searches, refactorings, or audits, exclude `temp/`, `test/`, and `tests/` unless explicitly prompted by the user.

## Documentation Updates

- When making feature changes that affect user-facing behavior, update these documents in the same operation:
  - `README.md` — project description, features list, storage requirements notice (~5GB starting).
  - Skill files in `.github/skills/` — if the change alters architecture, workflows, or file maps.
  - Agent rules in `.agents/rules/` — if new patterns or conventions emerge.
- **Never mention removed features.** Don't say "Linux support was removed" or "deprecated." Instead, write documentation as if the current state has always been the case.
- **README must stay current** with features like: remote/UNC path support, dual toolchain (Arduino CLI + PlatformIO), Monaco editor, AI assistant integration, etc.