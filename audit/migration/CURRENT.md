# Current migration handoff

Updated: 20 October 2026, Asia/Manila. Read this first; open historical reports
only for the slice being changed. Refresh this file after confirmed code/CI work.

## State and evidence

- Goal: native Rust GUI and application backend; retain embedded Monaco/xterm.
  Python remains production and may ultimately remain only for firmware tools.
- Current code: `6ef749a1f55841359fc380a572265c8323408a15` on
  `codex/rust-tauri-migration`; only “Select serial port” differs from tested
  `0e0db8a`, with strict MSVC/fmt checks. No popup Escape change remains.
  [Windows CI 39426407678](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39426407678)
  passes its native job at exact `0e0db8a`: 16 library + seven Windows units,
  17 detach + 18 workspace GUI, recipe 951 + 11 protocol.
- [CI 39421097096](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39421097096)
  finished **4/5 jobs passed**. Both Ubuntu AMD64/ARM64 jobs pass GTK 35 GUI,
  the wide 40-tab regression and recipe 951 decisions / 11 protocol checks.
  Both Windows original-editor jobs pass. The Windows native job passes its
  old 17-check smoke, three source-state units and recipe checks, then fails
  the new 18-check workspace before assertions: canonical `\\?\D:` fixture
  spelling differs from the plain trusted application-root prefix.
- The shared-reader fix accepts original/canonical trusted app-root spellings
  while preserving the literal selected project path. Local three reader checks,
  18 Linux units, GTK 35 GUI and strict Linux/MSVC-target Clippy pass. Windows
  cancel/UTF-16 args, owned small/large HICON and deliberately unaligned-buffer
  units now pass, as does the two-source Unicode/space fixture. Preserve the
  prior four passed jobs at `297cae7`; this was not a new full-matrix execution.
- [Prior CI 39392866712](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39392866712)
  passes all five jobs at `e334d7b`: Windows old 17 / Ubuntu 35 GUI per host.
- [Python Windows CI 39392313449](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39392313449)
  passes both jobs at `60e4bc4`, including real Uno/Nano/Mega AVR Compile and
  native GCC path fixtures. Shipping runtime/launcher/installer entry is unchanged.
- Ledger: **67 capabilities = 14 partial / 53 pending; zero Python services
  retired**. Full BLD-01 remains pending. Hosted CI is not Windows 10/11 desktop,
  native input, physical Pi/USB, installation or release qualification.

## Implemented scope and next slice

- Both adapters accept `--project FOLDER`, `--theme`, `--workspace-smoke`.
  Shared `workspace_project.rs` reads through Rust ProjectService on a worker;
  ordered acknowledgements and known-ID newer revisions govern native tabs.
  No source save, metadata generation, live registration/activation or backend
  compile/upload/reset/settings/cloud/package service is enabled.
- Original three-theme 117 color values and Montserrat are shared. GTK uses
  original action SVGs; Win32 uses native brand paths and text actions.
  GTK main/detached icon is embedded `mcu_icon.png`; actual `_NET_WM_ICON`
  reports 256×256 in all five captures. Windows source now has a native Project
  picker: cancel returns without spawning; selection launches current Rust EXE
  with literal OS path/theme arguments off-thread, up to 16 children, retaining
  parent buffers. Owned main/detached small/large HICONs use original ICO bytes.
  The bounded parser has three pure tests; DWORD-aligned `Vec<u32>` storage
  preserves exact payload bytes/length. Alignment P2 is fixed; scoped re-review
  is clean. No new dependency/lockfile; only existing WinSDK COM/Shell features.
- GTK 40-tab regression: RED top-level 6412 px; GREEN window 960 / row 6412 /
  all 40 tabs retained. Separate 42-source capture remains 960×540.
- Pure exact Onehorse/MiniMain policy: 126 default Rust tests (119+7), 951
  differential decisions (16 positive / 935 empty) plus 11 protocol checks.
  No trusted installed-metadata reader, Arduino Q hook selection or real Rust
  compile integration is claimed. Scoped policy/source reviews found no new
  actionable issue after the overflow and manual Windows action fixes.
- Next: qualify actual chooser/cancel,
  literal arguments/themes, child survival and main/detached icons on Windows
  10/11. PE Explorer icon/packaging/input remain gates. Backend work includes save leases/recovery,
  build/targets, serial/PTY, settings, packages/setup/repair, Firebase/vault,
  complete screens/input/DPI, notices, packaging, rollback and native devices.

## Work boundaries and focused commands

- Reuse this checkout and `desktop/target`; no new worktree or tool installation.
  `source desktop/dev-env.sh`; use offline Cargo and `CARGO_BUILD_JOBS=6`.
  Keep resource/storage budgets; run only checks relevant to changed code.
- Build: `cargo build --offline --manifest-path desktop/Cargo.toml -p mcu-native-ui-probe`.
  GTK: `.venv-linux/bin/python3 desktop/native-ui/verify-smoke.py --workspace`
  and without `--workspace` for detach. Windows runs separate `--smoke` and
  `--workspace-smoke` binaries. Prefer focused Windows-only CI for its fix;
  the default migration dispatch still retains all five jobs.
- Protected caches/journals are read-only: `.mcu_flasher_build_cache/`, `.pio/`,
  `.vscode/`, `.clangd/`, `.opencode/`, including AI edit backups and `.state`.
  Preserve root sketches, live settings/accounts/devices and unrelated dirty
  inputs. See [source preservation](source-preservation.json) for hashes/list;
  do not regenerate the baseline inventory or clean/reset unrelated work.
- Audit scratch/captures stay under ignored `audit/migration/{scratch,captures,logs}`.
  [Preview/host flags](../../desktop/native-ui/README.md),
  [plan](NATIVE_RUST_PLAN.md), [ledger](parity.md).
- Compact artifacts: `native-workspace.json`, `ci-results.json`,
  `scratch/native-ui/{recipe-policy-review,windows-reader-review,windows-picker-icon-review}.md`,
  `captures/native-workspace/`, `captures/native-workspace-many-sources/`.
  Windows receipts: `scratch/native-project-ci-0e0db8a/migration-prototype-windows-2022/audit/migration/logs/`.
- GitHub, Context7 and Rust LSP are usable. Codex Security native MCP tools and
  local plugin 0.1.32 are available; no scan launched. Firebase remains uninstalled at the last
  catalog check; installation in the app is required. Do not promise token savings.
- This short entry reduces repeated context loading; preserve historical audit
  files and read only the targeted report needed for the current decision.
