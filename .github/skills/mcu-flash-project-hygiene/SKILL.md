---
name: mcu-flash-project-hygiene
description: Audit and improve Windows MCU Flasher project hygiene, including generated-file visibility, Windows hidden attributes, safe writable metadata, PlatformIO board caches, Clean targets, and protection of user sketch files. Use for changes involving hide_internal_project_metadata, hide_hidden_attribute, hide_generated_directory, ensure_file_writable, generated project files, or cache cleanup in mcu_flash_gui.py.
---

# MCU Flasher Project Hygiene

Work on the Windows main app, `mcu_flash_gui.py`, Windows launchers, and supporting utilities.

## Preserve ownership boundaries

Classify paths before changing attributes or deleting anything:

- Treat root sketch sources and user material as user-owned. This includes
  `.ino`, `.cpp`, `.c`, `.h`, `.hpp`, `.txt`, documentation, assets, and
  unrecognized files or directories. Keep them visible and writable.
- Treat only known app-created paths as generated. The preferred project-level
  container is `.mcu_flasher_build_cache/`; it contains the staged `src/`,
  generated `platformio.ini`, `.pio/` board workspaces, metadata, and AI
  recovery state. Legacy examples include `.pio/`,
  `src/_python/` (private Python runtime), `src/env/` (virtual environment),
  `src/`, `platformio.ini`, `build_artifacts/`, `compiled_builds/`,
  `.mcu_ai_edits/`, app cache JSON files, `.opencodeignore`, `.pio_bootstrap_first_use_*`,
  and generated `AGENTS.md`.
- Never infer that an unknown extension is app-generated. Use an explicit
  generated-path allowlist.
- Treat generic legacy names such as `SKILL.md`, `READ-FIRST.md`, `temp.json`,
  `here.txt`, and `logs/` as user-owned unless content or another reliable
  signature proves the app created them. Never delete them by name alone.
- Never recursively apply hidden, system, or read-only attributes to children.
  Hiding a generated directory is enough and keeps its cache files writable.

## Use the Windows attribute helpers deliberately

- Use `hide_generated_directory(path)` for generated directories. It applies
  the directory hidden bit without changing child files and is suitable for
  NTFS, FAT32, and exFAT.
- Use `hide_hidden_attribute(path)` for known generated files. It clears the
  Windows read-only bit while setting the hidden bit on NTFS, FAT32, and exFAT.
- Call `ensure_file_writable(path)` before overwriting or atomically replacing
  generated files. It clears read-only without removing hidden/system bits, so
  files remain out of Explorer while the app updates them.
- Use `unhide_hidden_attribute(path)` to repair any real user file hidden by an
  older app version.
- Keep `hide_internal_project_metadata(project)` idempotent and shallow. It
  should reconcile known root metadata without scanning build trees.

## Keep cache and Clean behavior safe

- Keep each exact board under its canonical
  `.mcu_flasher_build_cache/boards/<board-key>/` workspace. Do not restore
  family-bucketed binaries into an exact-board build.
- For remote/UNC network projects, workspaces are isolated on local fast storage
  (under `remote_workspaces/`) to prevent SMB signature file errors and network latency,
  and are cleanly registered for manual Clean operations.
- Preserve incremental objects after ordinary source or linker failures. Repair
  only the selected board workspace after explicit cache-corruption evidence.
- Manual Clean may remove generated configuration, metadata, compiled caches,
  and reset caches only after the user confirms the rebuild cost.
- Patch `SCRIPT_DIR`, temp-path helpers, and deletion helpers in tests so a test
  cannot remove live caches.

## File distribution conventions

Files generated at runtime or holding user-specific state belong in dedicated
subdirectories, not the project root:

- **`src/dbs/`** — Settings and config files: `bootstrap_config.json`,
  `arduino_browser_settings.json`, `arduino_cli_path.txt`, `dbs_notif.json`.
- **`logs/`** — Runtime logs and diagnostics: `error_log.txt`, `session_backup.json`,
  `temp.json`.
- **Project root** — `compile_commands.json` stays at root for clangd/IntelliSense
  but is marked with `attrib +h` (Windows hidden) to keep Explorer clean.

When moving files into new subdirectories, always update all dependent imports,
path references, and fallback logic in the same operation.

## Git repository hygiene

- **`.gitignore` is authoritative.** Files committed before their ignore rule
  existed remain tracked until explicitly untracked with `git rm --cached`.
  Periodically audit with `git ls-files -ic --exclude-standard`.
- **Never track compiled artifacts** that can be regenerated from source:
  `*.res`, `*.o`, `*.d`, `*.bin`, `*.elf`, `*.pyc`, build caches.
- **Never track machine-specific files**: `compile_commands.json`, `.clangd`,
  `src/gui_config.json`, `arduino_cli_path.txt`, notification databases.
- **Dev scratch files** (`___*.py`, `old-*` reports) must be gitignored and
  untracked before pushing.
- **`.gitattributes`** — Only include LFS rules for paths that actually exist
  in the repo. Remove stale rules for deleted directories.

## Audit workflow

1. Search for every create, write, move, replace, and delete site for generated
   project paths.
2. Verify the parent directory is created and hidden using the directory helper.
3. Verify generated files are made writable before writes and hidden afterward
   where the filesystem safely supports it.
4. Verify unknown user files and all supported source extensions stay visible.
5. Verify cleanup is allowlist-based and cannot escape its project/cache root.
6. Add focused `unittest` coverage with temporary directories and mocked Win32
   attribute calls. Do not require a board, COM port, PlatformIO download, or GUI.
7. Run the focused tests, then `python -m unittest discover -s tests -v`.

## Low-end device constraints

- Prefer a single shallow project-root pass over recursive traversal.
- Avoid rewriting unchanged files or touching every object in a cache tree.
- Keep attribute calls best-effort; a cosmetic hiding failure must not block a
  compile, save, upload, reset, or project open.
- Preserve shared frameworks/toolchains and exact-board incremental state.
