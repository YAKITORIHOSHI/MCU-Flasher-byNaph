---
name: mcu-flash-project-hygiene
description: "Maintain MCU Flasher's generated metadata, protected caches, Fresh cleanup and public release integrity on Windows and Ubuntu. Use for file-utility, cleanup, packaging or publication-preparation changes; preserve user sketches, settings and recovery journals."
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
Modify file operations advance the project source revision and refresh current
AI guidance through the existing serialized metadata writer. They must not reset
terminal/assistant sessions, unrelated panels or hardware choices. Keep root
source enumeration current and all verification writes inside disposable fixtures.

Saved hardware `upload_speed` describes the current target through
`serial_upload_speed`, never the last ESP upload. Store null for unknown recipe
baud, Arduino CLI and nonserial programmers. Keep `baud_rate` as the independent
Serial Monitor setting. Verify payloads with a mocked metadata queue in
`direct/verify_upload_logging.py`; do not generate them against live projects.

## Preserve ownership boundaries

The configured source container (normally `Documents/_MCUFlasherByNaph_src`)
contains `Boards`, `Libs` and `Cloud`. Treat `Cloud/<sketch name>` sources as
user-owned working projects. The public Firebase provider is fixed in the app;
never generate editable provider settings or use legacy files/vault/environment
values to override it. Keep account credentials in the OS vault and private
account/recovery records outside this shared source folder. When adopting an
older cloud location, copy only validated root sources and the nonsecret cloud
link; preserve original assets, protected build caches and AI journals. Reject
an additional working-folder adoption while the verified old copy is open;
focus its existing window and adopt saved sources only after it closes. Reject
concurrent source changes and never replace a different account's working copy.
Local name collisions require Replace/Cancel, never an automatic numbered
download. Replacement requires the reviewed source/link/folder state and cloud
revision; recovery retains all affected root sources. Do not overwrite an open
target workspace. Preserve unknown assets and protected cache/journal trees.
Recheck remote revision and open-target ownership after staging, before the final
local byte/identity checks. Check each source again before mutation. Failed Pull
rollback must preserve external edits to published files and links; restore only
unchanged attempt-owned files or still-absent deleted paths and report retained
recovery copies when full restoration is unsafe. Track partial staging writes for
cleanup and never roll back or clean through a changed/redirected project root.
Verify native case-only source renames and rollback spellings on Ubuntu, without
changing the Windows case-insensitive behavior. Use
`direct/verify_ubuntu_cloud_workflow.py` for isolated byte/configuration/staging,
permission and host-GCC dependency-path checks; its ESP32 compiler is mocked.

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
  Windows compiler launches may use a create-only, verified short junction to
  this canonical workspace. Keep receipt validation and Clean on canonical
  paths; never traverse the compiler alias as a new cache owner. Verify with
  `direct/verify_windows_build_paths.py` using only disposable `temp/` aliases.
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

- Ubuntu maintenance and repair admission requires native Ubuntu 24.04 or newer.
  Reject older or unknown releases before planning/mutation; never clean or migrate
  projects, settings, credentials, installed resources or AI journals on rejection.
  Keep release fixtures under temp and use older versions for rejection only.

- Keep `cleaner/windows/maintenance.ps1` and `cleaner/ubuntu/maintenance.py`
  separate. Compatibility wrappers in `cleaner/` and `DANGER-ZONE/` resolve
  from their own location, preserve exit codes and default to a read-only plan.
  Mutation requires `-Apply` / `--apply`. Interactive use asks for the exact typed
  acknowledgement; intentional automation after an explicit Fresh request may use
  Ubuntu `--apply --yes` or Windows `-Apply -Force` without asking again. These
  flags retain ownership, changed-plan and running-process checks; they do not
  authorize broader Runtime, shared-store, Stop or system-reset operations.
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
  Native Arduino CLI/OpenCode receipts may identify linux-amd64 or linux-arm64;
  validate ELF64 CPU bytes against the recorded architecture before recognizing
  an owned tool for cleanup. Legacy OpenCode receipts without architecture are
  amd64-only. Runtime discovery separately requires the current host CPU, so a
  copied owned tool can be reset safely without ever executing it on the wrong CPU.
  Python 3.14's `bin/𝜋thon` link is recognized only when it resolves to that
  environment's own `bin/python`; a different destination remains a refusal.
  In a receipt-authenticated native store, admit packaged file/directory aliases
  only when their final existing destination stays inside that exact architecture
  tree. Unlink aliases through the descriptor boundary; never traverse them.
  Refuse broken, cyclic, external and cross-architecture links before mutation.
  Accept the venv's standard Python-generated `.gitignore` only by its exact
  generated bytes, retaining custom/linked content and revalidating after preview.
- Cleanup refuses running app/package operations and never kills them to unlock
  files. The separate legacy Windows Stop entry point requires explicit
  acknowledgement, captured creation identity and exact owned entry/executable
  boundaries. A root appearing in another program's arguments proves nothing.
  A permission-denied Linux executable probe may skip only the recognized
  systemd user manager, its PAM helper or the SSH agent when command, process
  name and exact kernel service unit all agree. Unknown inaccessible processes
  still refuse cleanup; never bypass the process check wholesale.
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

For a public Fresh request, run the needed private-runtime integrity verifiers
before applying cleanup: Ubuntu Fresh removes `.venv-linux`. Review the native
cleanup plan, apply only its authorized owned targets and retain the process
guard. If active application/package work blocks cleanup, report the blocker;
do not kill it or bypass the guard. Cleanup and public packaging are separate:
Fresh preserves settings, accounts and recovery data, while archive selection
excludes them without deleting their working copies.

Packaged Windows releases require the complete `src/_python/` private Python
distribution and its installed packages. Preserve it during fresh release
preparation; do not classify it with disposable `env` / `.venv-linux` or omit it
just because Git ignores it. Windows must not depend on system/PATH Python.
Use `direct/build_release.py` after committing release sources. It combines
committed defaults, verified materialized LFS installers and the local private
Windows runtime, then verifies every archived file. Missing runtime files must
stop packaging. GitHub source archives alone are not a packaged Windows release.
Keep user state, package stores, protected caches and audit fixtures out of
release ZIPs without deleting them from the working checkout.

Before a requested Git push, inspect the intended source diff and tracked ignored
files, check repository integrity and verify installer bytes against committed
LFS SHA-256/size receipts. Keep materialized installers out of ordinary Git blobs;
an unavailable LFS filter can make unchanged hydrated files appear modified.
Check tracked state and credential findings without printing secret values; the
fixed public Firebase client configuration alone is not a credential. Update
release descriptions and agent guidance when behavior changes, preserving actual
platform and preview limits. Build the ZIP from the intended committed revision,
verify every archived file and retain its SHA-256 manifest before distribution.

The public `LAUNCHER/` folder contains `WINDOWS.exe`, `UBUNTU`, `UBUNTU_ARM64`,
`FEDORA`, `FEDORA_ARM64`, `LINUX_MINT`, `MacOS.command` and `README.md`, without
`.desktop` entries. Preserve Unix executable modes and matching ELF CPUs/Windows
GUI PE headers. Fedora/Mint/macOS entries currently show availability notices;
Windows ARM64 and those application ports remain unqualified. The shipping GUI
is Python/PySide6; native Rust preview receipts do not expand shipping support.
Use `direct/verify_launcher_folder.py` and `direct/verify_release_sources.py` for
isolated launcher and archive-selection checks before Fresh removes their runtime.

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

- Shared archive path helpers add the `\\?\` namespace only on Windows. On
  Linux they must retain native absolute paths; otherwise extraction creates a
  relative backslash directory outside its staging tree. Verify actual seed
  extraction containment with `direct/verify_bootstrap_seed.py` and native/Windows
  path spellings with `direct/verify_bootstrap_archives.py`. A copied
  `_/home/.../temp/audit/bootstrap-seed-fixtures` tree may be old fixture residue;
  prove its contents/ownership before relocating it, preserve it under `temp/`,
  and exclude audit artifacts from release archives.

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
