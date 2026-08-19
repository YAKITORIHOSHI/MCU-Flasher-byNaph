---
trigger: always_on
---

## Platform Scope (Windows 10/11)

This project is built and optimized specifically for Windows 10 and 11 environments. Core files include `mcu_flash_gui.py`, `launcher.py`, `dedicated_AI.py`, `runThisOnWindows.vbs`, `src/modules/bootstrap.py`, `src/modules/win_subprocess_hide.py`, and Windows-bundled installers in `installers/` (`CP210x/`, `arduino-cli.msi`, `msys2-*.exe`, `MicrosoftEdgeWebview2Setup.exe`).
- All toolchain setup, subprocess execution, file hiding attributes (`attrib +h`), win32 APIs, and path handling are tailored for Windows.
- Always use Windows-compatible paths, subprocess flags (e.g. `CREATE_NO_WINDOW`), and ctypes Win32 calls.

## Environment Scope (Experimental vs Actual)

This project has two environments:
- **Experimental** — sandbox for testing changes before they're proven.
- **Actual (Production)** — the verified version, only updated once a change has succeeded in Experimental.

- Only edit files in the environment the user's request refers to. Treat phrases like "test," "try," "experimental" as pointing to Experimental, and "real," "production," "actual," "live" as pointing to Actual.
- Never port a change from Experimental to Actual (or vice versa) unless the user explicitly confirms it succeeded and asks you to apply/promote it.
- If the user doesn't say which environment they mean, ask — don't assume.
- Only edit both environments in one task if explicitly asked.