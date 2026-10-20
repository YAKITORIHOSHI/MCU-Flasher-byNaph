# Current migration handoff

Updated: 20 October 2026, Asia/Manila. Read this first, then only the reports
needed for the next slice. Preserve historical failures and exact tested sources.

## State and evidence

- Goal: native Rust GUI and application backend, embedded Monaco/xterm, private
  Python only for firmware tools. Shipping entry still uses Python.
- User approved a usable Rust beta milestone: Open/Edit/Save/Compile, then
  remaining device/services and packaging. Preserve the design/layout; retain
  glassmorphism if reliable. Otherwise use ordinary Light/Dark/system_default
  (follow OS appearance). This explicit fallback overrides older glass mandates.
- Current source: `002cc95da9aec9a39f2ba9743123cf9c1c5bb867` on
  `codex/rust-tauri-migration`. Runtime service checkpoint `30d99af` adds native
  recipe-generation observation/revalidation. Follow-up `002cc95` changes only
  the Python mutation controller; all Rust runtime source remains unchanged.
- [Backend CI 39447542929](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39447542929)
  at `30d99af`: both Ubuntu AMD64/ARM64 pass **148 tests/31 process checks**.
  Windows passes **144 Rust tests**, strict checks and 951/979 differentials;
  controller os.replace fails with WinError5 on an intentionally retained file.
- [Windows backend rerun 39448048718](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39448048718)
  at `002cc95` passes **144 tests/23 process checks**. Controller renames the
  original aside, installs a distinct same-byte file and checks native identity.
  Both sources/failure receipts stay retained; no combined full GUI run claimed.
- Original compatibility behavior stays **987 Linux/979 Windows decisions**,
  with Python exceptions and stricter parser/path refusals separately counted;
  prior pure recipe 951 decisions/11 protocol checks also pass on all hosts.
- Local service/protocol unit work: 14 module tests (four moved/ten new) and two
  new probe tests; all 148 default tests, strict Linux/MSVC Clippy and fmt pass.
  Independent signed source review and the controller-only rereview are clean.
- Native GUI `39426407678` at `0e0db8a` passes Windows 23 units and 35 GUI checks,
  original HICON/alignment and Unicode projects. `6ef749a` later changes only
  the serial-port caption with strict MSVC/fmt checks. Both Ubuntu GUI/Windows
  editor jobs pass at `297cae7`; its retained native Windows failure is fixed by
  `0e0db8a`. Prior full five-job `39392866712` passes at `e334d7b`.
- Python Windows `39392313449` at `60e4bc4` passes two jobs, real sample Uno/Nano/
  Mega Compile-button builds and native AVR GCC path fixtures. Preserve the
  completed 318-target Linux campaign; no hardware upload/reset was qualified.
- **67 capabilities = 14 partial/53 pending; zero Python services retired.**
  Full BLD-01, complete native UI/backend and deployment remain unfinished.

## Implemented scope and next slice

- GTK/Win32 ProjectService workers read explicit projects; ordered/native IDs
  govern source tabs. No live activation/save/metadata/real build dispatch yet.
  Original 117 palette values/Montserrat and native icon assets are reused.
  Native Project selection opens independent Rust processes retaining buffers.
  Keep actual desktop input/IME/chooser/child/mixed-DPI/Explorer packaging gates.
- New host `recipe_snapshot.rs` extracts the descriptor reader, pins native
  root/all-prefix/consulted-file identities and hashes raw input bytes. Opaque
  receipt binds authority instance, generation, exact native target/backend/
  manifest/host/rule/options. Revalidation recomputes policy/rereads all inputs.
  Failed/stale/foreign/changed/partial/empty observations refuse and invalidate.
- Per observation: <=48 directories/8 files/512KiB total, <=64KiB/file; authority
  separately retains <=48 prefixes. Probe owns one receipt, 8KiB frames,
  64 commands and 10-second idle. API callers retain handles until receipt Drop;
  no process-global caller-owned handle cap, immutability or execution grant.
  Active ABA, non-atomic reads and final check/use remain explicit gaps.
- Next: **owned native compile-input staging/preflight**, with exact prepared
  target evidence, complete multi-file source generation, deterministic config
  and admitted tool/runtime provenance. Keep spawning unavailable until tool
  lifetime/immutability and finite child cleanup are qualified. Current Python
  offline wrapper imports app settings; a tool-only boundary must accept Rust
  policy explicitly. A verified native recipe with no override is separate from
  this positive-only service's NoRecipe refusal. See Task 3 in [the plan](NATIVE_RUST_PLAN.md).
- Continue this work in the newly requested chat using the accepted delivery
  order in the plan; integrate usable features instead of stopping at probes.
- Source-save leases/recovery, catalog/build integration, serial/PTY, settings/
  packages/setup/cloud/vault, complete screens and release/device gates remain.

## Work boundaries and focused commands

- Reuse checkout/desktop target; no new worktree/dependency install. Source
  `desktop/dev-env.sh`, offline Cargo and CARGO_BUILD_JOBS=6; retain >=5GiB disk.
  Around 10GiB was free. Only owned scratch may be cleaned.
- Build mcu-host bins board_compatibility_probe/recipe_generation_probe; private
  Python -B -W error runs desktop/scripts/verify-{board-compatibility,recipe-generation}.py.
  Existing migration workflow backend_only=true selects native backend hosts;
  windows_only=true narrows to Windows. Default five GUI/editor jobs unchanged.
- Protected caches/journals read-only: .mcu_flasher_build_cache, .pio, .vscode,
  .clangd, .opencode, AI backups/.state. Preserve root sketches, settings,
  accounts/hardware and nine unrelated dirty index/config/installer inputs.
  Eleven continuation inputs/four launchers match; baseline never regenerated.
- Reports: rust-recipe-generation.json, rust-board-compatibility.json,
  native-project-open.json, ci-results.json, source-preservation.json. Ignored
  scratch contains native artifacts, signed review and next-rust-build-slice.md;
  don't repeat whole histories, passing campaign or unchanged GUI suites.
- GitHub, Context7, Rust LSP, Codex Security and Firebase tools are available.
  No security scan/cloud action launched; use focused requests/compact receipts.
