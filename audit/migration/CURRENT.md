# Current migration handoff

Updated: 20 October 2026, Asia/Manila. Read this first; open only reports needed
for the current slice. Preserve historical failures and exact tested revisions.

## State and evidence

- Goal: native Rust GUI and application backend, embedded Monaco/xterm, private
  Python only for firmware tools. Shipping entry still uses Python.
- Current source: `cc6a6d7f5c482def023976f32e3759dfba1a4415` on
  `codex/rust-tauri-migration`. `26c7675` adds fixture-only native recipe
  snapshots/Arduino Q policy; `cc6a6d7` changes only the Windows test's literal
  parent-path construction. Runtime reader/core policy are unchanged.
- Local backend: **136 tests**, six new core/four native reader units; strict
  Linux/MSVC all-target Clippy and formatting pass. Ordinary differential:
  **987 decisions** (29 positive/958 empty), four original Python exceptions,
  three stricter path/21 parser refusals separately recorded; 870 no-read and
  32 protocol checks pass.
- [Backend CI 39443196583](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39443196583)
  at `26c7675`: both Ubuntu AMD64/ARM64 pass **136 tests, 987 compatibility
  decisions/32 protocol checks**. Windows fails one new test because joining
  `..` to a canonical verbatim prefix normalizes away its intended alias.
- [Windows backend rerun 39443653534](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39443653534)
  at `cc6a6d7` passes **132 tests, 979 compatibility decisions/32 protocol**;
  ten Linux-only cases skip. Separate four reference exceptions/18 parser
  refusals remain explicit. Both runs pass the earlier 951/11 recipe checks.
  Only fixture construction changed; retain Ubuntu results at `26c7675`.
- [Native Windows CI 39426407678](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39426407678)
  passes at `0e0db8a`: 16 library/seven Windows units, 17 detach/18 workspace
  GUI, original HICON/alignment, Unicode project and recipe 951/11 checks.
  `6ef749a` later changes only the serial-port caption; strict MSVC/fmt pass.
- Both Ubuntu GUI jobs/Windows editor jobs pass at `297cae7` in `39421097096`;
  its native Windows workspace failure is retained/fixed by `0e0db8a`.
  Prior full five-job `39392866712` passes at `e334d7b`.
- Python Windows `39392313449` passes both jobs at `60e4bc4`: real sample
  Uno/Nano/Mega Compile-button builds and native AVR GCC path fixtures.
- **67 capabilities = 14 partial/53 pending; zero Python services retired.**
  Full BLD-01, desktop/device/install/rollback and deployment remain open.

## Implemented scope and next slice

- GTK/Win32 read explicit `--project` through the shared ProjectService worker.
  Ordered acknowledgements and known-ID newer revisions govern native source
  controls; no live activation/save/metadata or real build dispatch is enabled.
- Original 117 palette values/Montserrat; GTK original action SVGs/native PNG;
  Win32 brand paths/text actions and original small/large owned HICONs. Native
  Project picker opens independent Rust processes with literal path/theme
  arguments, cancel/no-spawn, 16-child cap and parent buffers retained.
- GTK icon property is 256x256 in five captures. The wide-tab fix keeps 40 tabs
  in a 960-pixel window with a scrollable 6412-pixel row; 42-source capture stays
  960x540. Actual Windows chooser/child survival/input/IME/mixed-DPI and PE
  Explorer icon/packaging still require desktop qualification.
- Compatibility core fixes owned fixture package roots/hook replica. Native
  reader implementation exists only in the development probe: 64-KiB bounded
  regular files/native identities; Linux opens are no-follow/nonblocking.
  Exact raw hashes/versions/frameworks select Onehorse/MiniMain or Linux Q.
  Snapshots grant no immutable package identity or future execution authority.
- Next: complete live package identity, ancestor/content races and build-time
  revalidation before real build dispatch. Wider JSON/non-UTF8 path parity,
  source-save leases/recovery, serial/PTY, settings/packages/setup/cloud/vault,
  complete native screens and release gates remain in [the plan](NATIVE_RUST_PLAN.md)
  and [ledger](parity.md). Preserve completed campaign and native GUI receipts.

## Work boundaries and focused commands

- Reuse this checkout/`desktop/target`; no new worktree or dependency install.
  `source desktop/dev-env.sh`; offline Cargo, `CARGO_BUILD_JOBS=6`, retain at
  least 5 GiB disk reserve. Around 10 GiB was free at the last check.
- Build `mcu-host --bin board_compatibility_probe`; run private Python with
  `-B -W error desktop/scripts/verify-board-compatibility.py`. The existing
  migration workflow accepts `backend_only=true` (three native hosts) and
  optional `windows_only=true`; default full five-job matrix is preserved.
- GUI commands/flags: [preview README](../../desktop/native-ui/README.md).
  Do not repeat unchanged GUI/campaign checks for backend-only changes.
- Protected caches/journals are read-only: `.mcu_flasher_build_cache/`, `.pio/`,
  `.vscode/`, `.clangd/`, `.opencode/`, AI backups and `.state`. Preserve user
  sketches/settings/accounts/devices and unrelated dirty installers/index/config.
  Eleven continuation inputs/four launcher hashes match; baseline inventory
  was never regenerated. See [preservation](source-preservation.json).
- Evidence: `rust-board-compatibility.json`, `native-project-open.json`,
  `native-workspace.json`, `ci-results.json`; ignored scratch has compact native
  artifacts/reviews/captures. Independent review reported a clean final
  differential, then its final report turn hit a usage limit; owner recorded
  the boundary and verified final source. No completed final sign-off inferred.
- GitHub, Context7 and Rust LSP are usable. Codex Security/Firebase tools are
  installed and available. No scan/cloud action launched. Use focused requests
  and compact receipts; plugins do not guarantee token savings.
