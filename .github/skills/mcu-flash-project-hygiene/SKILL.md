---
name: mcu-flash-project-hygiene
description: Audit and improve Windows MCU Flasher project hygiene, including generated-file visibility, Windows hidden attributes, safe writable metadata, PlatformIO board caches, Clean targets, and protection of user sketch files. Use for changes involving hide_internal_project_metadata, hide_hidden_attribute, hide_generated_directory, ensure_file_writable, generated project files, or cache cleanup in mcu_flash_gui.py.
---

# MCU Flasher Project Hygiene

Work on the Windows main app, `mcu_flash_gui.py`. Do not edit
`mcu_flash_gui_linux.py` unless the user explicitly expands the scope to Linux.

## Preserve ownership boundaries

Classify paths before changing attributes or deleting anything:

- Treat root sketch sources and user material as user-owned. This includes
  `.ino`, `.cpp`, `.c`, `.h`, `.hpp`, `.txt`, documentation, assets, and
  unrecognized files or directories. Keep them visible and writable.
- Treat only known app-created paths as generated. Examples include `.pio/`,
  `src/`, `platformio.ini`, `build_artifacts/`, `compiled_builds/`,
  `.mcu_ai_edits/`, app cache JSON files, `.opencodeignore`, and generated
  `AGENTS.md`.
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

- Keep each exact board under its canonical `.pio/boards/<board-key>/`
  workspace. Do not restore family-bucketed binaries into an exact-board build.
- Preserve incremental objects after ordinary source or linker failures. Repair
  only the selected board workspace after explicit cache-corruption evidence.
- Manual Clean may remove generated configuration, metadata, compiled caches,
  and reset caches only after the user confirms the rebuild cost.
- Patch `SCRIPT_DIR`, temp-path helpers, and deletion helpers in tests so a test
  cannot remove live caches.

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
