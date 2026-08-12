---
trigger: always_on
---

## Platform Scope (Windows vs Linux)

This project has separate Windows and Linux code paths. When a request concerns only one platform:

- **Windows-only concern** → Only read/edit Windows-specific files (e.g. `bootstrap.py`, `win_subprocess_hide.py`, `installers/CP210x`, `installers/arduino-cli.msi`, `installers/msys2-*.exe`, `installers/MicrosoftEdgeWebview2Setup.exe`). Do not open or modify Linux-specific files (e.g. `bootstrap_linux.py`, `installers/readme-ubuntu.txt`).
- **Linux-only concern** → Only read/edit Linux-specific files (e.g. `bootstrap_linux.py`, `installers/readme-ubuntu.txt`, and anything with a `_linux` / `-linux` / `ubuntu` naming pattern). Do not open or modify Windows-specific files.
- Treat a file as platform-specific based on its name/suffix first; if that's ambiguous, check its contents before deciding.
- Only touch both platforms' files if the user explicitly says to update both, or the file is clearly shared/OS-agnostic core logic.
- If it's unclear which platform a request applies to, ask before editing either.

## Environment Scope (Experimental vs Actual)

This project has two environments:
- **Experimental** — sandbox for testing changes before they're proven.
- **Actual (Production)** — the verified version, only updated once a change has succeeded in Experimental.

- Only edit files in the environment the user's request refers to. Treat phrases like "test," "try," "experimental" as pointing to Experimental, and "real," "production," "actual," "live" as pointing to Actual.
- Never port a change from Experimental to Actual (or vice versa) unless the user explicitly confirms it succeeded and asks you to apply/promote it.
- If the user doesn't say which environment they mean, ask — don't assume.
- Only edit both environments in one task if explicitly asked.