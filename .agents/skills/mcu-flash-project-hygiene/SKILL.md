---
name: mcu-flash-project-hygiene
description: Audit MCU Flasher project hygiene on Windows and Ubuntu, including Windows hidden attributes, safe writable metadata, exact-board caches, generated agent instructions, Clean targets and protection of user sketches. Use for metadata generation, file utilities or cache cleanup changes.
---

# MCU Flasher Project Hygiene

Work on the main app (`main/mcu_flash_gui.py` and `main/core/file_utils.py`), native launchers and supporting utilities. Windows attribute helpers are best-effort; Linux uses dot-prefixed containers and must not invoke `attrib.exe` or Windows junctions.

Generated AGENTS instructions keep root-sketch firmware boundaries, use
project-relative paths, and direct agents to the current GUI or a read-only
project-state snapshot instead of embedding machine-specific hardware values.
OpenCode discovers project instructions from the project-root `AGENTS.md`;
`.opencode/` is reserved for project skills and agents. `application_agent_guidance()`
adds application development rules only to application checkouts. Verify the
distinction in isolated fixtures without running metadata generation against
live caches or modifying AI backup journals.

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
- Use `write_generated_text(path, content)` for generated text/JSON metadata.
  It prepares a hidden sibling and atomically replaces the target, avoiding
  Windows hidden-file truncation errors and keeping repeated updates hidden.
- Notification storage also prepares a hidden sibling before atomic replacement
  and preserves existing history when replacement fails. Never fall back to
  copying over the old database. Creation, updates and deletion must return the
  actual persistence outcome; use the isolated notification write/delete verifiers.
- Use `unhide_hidden_attribute(path)` to repair any real user file hidden by an
  older app version.
- Keep `hide_internal_project_metadata(project)` idempotent and shallow. It
  should reconcile known root metadata without scanning build trees.
- Hide generic root instructions and IDE configurations only with an MCU Flasher
  signature/path. Preserve user-authored AGENTS and ignore rules when generating
  instructions. First-use sentinels have the explicit `.pio_bootstrap_first_use_*`
  prefix. Use `direct/verify_hygiene.py` for isolated native attribute checks.
- Schedule startup reconciliation after the workspace is shown and execute it
  in a background worker. Do not repeat the same reconciliation synchronously
  while constructing the project selector. Mock it in all Qt startup verifiers.

## Keep cache and Clean behavior safe

- Keep each exact board under its canonical
  `.mcu_flasher_build_cache/boards/<host-architecture>/<board-key>/` workspace.
  Keep generated INI, staged sources, `.pio/build/mcu_env` objects/output and
  the successful build receipt together. Arduino CLI uses a certified CLI
  subfolder in that same native board workspace. Do not restore shared or
  family-bucketed binaries into an exact-board build; legacy caches stay untouched.
- Board switching and framework changes never delete another target's build.
  Check root source bytes and the selected receipt before enabling Skip Compile;
  bind receipts to host, exact identity, build configuration, configured library
  bytes (including custom recipes/assets) and firmware bytes. Snapshot inputs
  before compiling and reject a changed post-build signature.
  Preserve incremental objects while invalidating the selected receipt before a
  rebuild. Verify A → B → A, restart, tampering and failure cases with disposable
  `direct/verify_board_build_cache.py` and Arduino backend fixtures under temp/.
- For remote/UNC network projects, workspaces are isolated on local fast storage
  (under `remote_workspaces/`) to prevent SMB signature file errors and network latency,
  and are cleanly registered for manual Clean operations.
- Preserve incremental objects after ordinary source or linker failures. Repair
  only the selected board workspace after explicit cache-corruption evidence.
- Manual Clean may remove generated configuration, metadata, compiled caches,
  and reset caches only after the user confirms the rebuild cost.
- Patch `SCRIPT_DIR`, temp-path helpers, and deletion helpers in tests so a test
  cannot remove live caches.

## Native maintenance utilities

- Keep `cleaner/windows/maintenance.ps1` and `cleaner/ubuntu/maintenance.py`
  separate. Compatibility wrappers in `cleaner/` and `DANGER-ZONE/` resolve
  from their own location, preserve exit codes and default to a read-only plan.
  Mutation requires `-Apply` / `--apply` and exact typed acknowledgement.
- Maintenance preserves sketches, all settings, generic logs, protected project
  build/IDE caches and every AI recovery journal. Do not describe it as a
  complete user-state wipe. Source bytecode scans prune protected/runtime trees
  before recursion and preserve unknown cache content.
- Windows Fresh retains portable Python; Runtime additionally resets its
  recognized private runtime. Both use only local Windows env/store/extras
  targets. Normalize drive-root boundaries correctly; aliases are nonrecursive
  unlink targets only when their destination belongs to this checkout's exact
  Windows store or modules. Revalidate markers, receipts and reviewed targets
  after confirmation. Refuse unknown envs, linked roots and changed plans.
- Ubuntu uses system Python to remove its validated `.venv-linux`; Runtime also
  handles receipt-authenticated native tools/extras. Removing the shared XDG
  store needs `--include-native-store` and a current Linux/architecture receipt.
  Retain other architectures, all Windows resources and system packages.
  Native descriptor-relative deletion must not follow venv interpreter links.
- Cleanup refuses running app/package operations and never kills them to unlock
  files. The separate legacy Windows Stop entry point requires explicit
  acknowledgement, captured creation identity and exact owned entry/executable
  boundaries. A root appearing in another program's arguments proves nothing.
- Driver and Windows system reset/repair entry points remain native and explicit;
  preview does not elevate or run repair. Driver selection requires a supported
  original INF and exact OEM package identity. FullReset is a separate opt-in
  for shared dependencies, with closed-program and installer preflight.
  Diagnostic captures belong under `temp/audit/crash-diagnostic`.
- Verify with `direct/verify_windows_maintenance.py` and
  `direct/verify_ubuntu_maintenance.py` using disposable fixtures under `temp/`.
  Mock process/hardware calls and exercise link, ownership, interruption and
  read-only preview boundaries. Never run a live cleaner or system repair for
  verification. Record native cases skipped on the current host.

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
7. Run focused hardware-free verifiers with the app's private runtime; keep all
   persistence and deletion fixtures under `temp/`. Use the maintenance verifiers
   for standalone cleanup utilities and the app verifiers for GUI Clean changes.

## Low-end device constraints

- Prefer a single shallow project-root pass over recursive traversal.
- Avoid rewriting unchanged files or touching every object in a cache tree.
- `write_generated_text()` compares bounded current content before replacing equal metadata; keep hidden/writable attribute repair without needless chmod calls. Settings transactions retain atomic cross-process merging and authoritative save failures while skipping equal portable/per-user copies.
- Treat slow HDD/removable/network storage separately from CPU speed. Keep storage hint probes read-only, bounded and off the UI thread; preserve exact-board incremental objects and actual-byte source validation on FAT/exFAT. Verify I/O counts and equal-size/coarse-timestamp edits in isolated `direct/verify_storage_io.py` fixtures, never live caches/journals.
- Keep attribute calls best-effort; a cosmetic hiding failure must not block a
  compile, save, upload, reset, or project open.
- Preserve shared frameworks/toolchains and exact-board incremental state.
- Arduino CLI board choices are local user settings scoped to vendor index,
  package, architecture and exact board ID. Default to PlatformIO and no CLI
  choices; do not migrate old prepared targets into automatic opt-ins. Disabling
  a board retires its CLI routing and future preparation, preserving already
  installed cores, download archives and certificates until explicit cleanup.
  A chosen board can still require its vendor's shared compiler package. Keep
  Windows and Ubuntu native tools/stores separate during preparation and cleanup.
