# Current migration handoff

Updated: 20 October 2026, Asia/Manila. Read this first; open historical reports
only for the slice being changed. Refresh this file after confirmed code/CI work.

## State and evidence

- Goal: native Rust GUI and application backend; retain embedded Monaco/xterm.
  Python remains production and may ultimately remain only for firmware tools.
- Confirmed code: `297cae75c5d4202461b3ab6dc0ba2aafe76fab25` on
  `codex/rust-tauri-migration`; includes pure recipe policy `69d845a`.
- [CI 39421097096](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39421097096)
  finished **4/5 jobs passed**. Both Ubuntu AMD64/ARM64 jobs pass GTK 35 GUI,
  the wide 40-tab regression and recipe 951 decisions / 11 protocol checks.
  Both Windows original-editor jobs pass. The Windows native job passes its
  old 17-check smoke, three source-state units and recipe checks, then fails
  the new 18-check workspace before assertions: canonical `\\?\D:` fixture
  spelling differs from the plain trusted application-root prefix.
- The shared-reader fix accepts original/canonical spellings of the trusted
  app root while preserving the literal selected project path. Local three
  reader checks and GTK 35 GUI pass; **Windows rerun pending**. Do not claim
  Windows 35 GUI passed or transfer the old checkpoint's result to this fix.
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
  reports 256×256 in all five captures. Windows native Project picker/HICON
  work is in progress: do not claim integrated or qualified yet.
- GTK 40-tab regression: RED top-level 6412 px; GREEN window 960 / row 6412 /
  all 40 tabs retained. Separate 42-source capture remains 960×540.
- Pure exact Onehorse/MiniMain policy: 126 default Rust tests (119+7), 951
  differential decisions (16 positive / 935 empty) plus 11 protocol checks.
  No trusted installed-metadata reader, Arduino Q hook selection or real Rust
  compile integration is claimed. Scoped policy/source reviews found no new
  actionable issue after the overflow and manual Windows action fixes.
- Next: qualify the prefix fix on Windows, then review/qualify Project picker
  and HICON separately. Remaining backend work includes save leases/recovery,
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
  `scratch/native-ui/{recipe-policy-review,windows-reader-review}.md`,
  `captures/native-workspace/`, `captures/native-workspace-many-sources/`.
- GitHub, Context7 and Rust LSP are usable. Codex Security and Firebase plugins
  were offered but are not installed/connected; the user must finish installation
  in the app. Do not assume they are callable or promise token savings.
- This short entry reduces repeated context loading; preserve historical audit
  files and read only the targeted report needed for the current decision.
