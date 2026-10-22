# Current migration handoff

Updated: 23 October 2026, Asia/Manila. Read this first, then only the reports
needed for the next slice. Preserve historical failures and exact tested sources.

## State and evidence

- Latest launcher requirement: `LAUNCHER/` contains native `WINDOWS.exe`,
  `UBUNTU` / `UBUNTU_ARM64`, `FEDORA` / `FEDORA_ARM64`, `LINUX_MINT`, executable
  `MacOS.command` and README, with no `.desktop` shortcuts. Ubuntu and Windows
  retain existing startup guards; Fedora/Mint/macOS remain availability notices.
  Native builders and moved-folder/literal-argument fixtures pass on Windows
  Server AMD64 and Ubuntu AMD64/ARM64 in `39824882032`. Final supplied-binary
  bundle/header/mode/archive qualification follows in the next CI run. See
  `../LAUNCHER_EXECUTABLES.json` for exact shipped bytes and scope.
- The isolated tool-only Compile entry now replaces only the in-process
  PlatformIO 6.2.0 SCons handoff with a fixed sibling entry, preserving `-I -S -B`
  and reinstalling package/import/network/subprocess guards before engine import.
  All 68 fake-tool checks pass, including actual nested fake SCons and modeled
  Windows vector serialization. Independent scoped review is clean. Complete
  runtime/loader admission, immutable inputs, finite external process ownership
  and real native-controlled compilation remain gates; no compiler is run by
  this slice. See `rust-scons-entry.json` and `desktop/firmware-tools/README.md`.
- Active native Save continuation: GTK/Win32 Save and Save All now dispatch
  bounded known-document snapshots to a Rust worker and acknowledge exact saved
  baselines without replacing editor models or undo state. Newer edits remain
  dirty; external changes refuse. Save All is sequential per-file replacement,
  with successful acknowledgements retained before a later failure. Linux
  permission/ACL/xattr/ownership refusals and observed recovery retention are
  explicit. Native save leases do not qualify legacy Python coexistence or
  hostile same-user final-use races. Local evidence: 255 library tests
  (1 contracts, 78 core, 148 host, 28 native UI), 32 new Save GUI checks,
  19 unchanged-source workspace checks and 17 detach checks. Strict Linux lint
  passes. Exact-source [CI 39823016524](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39823016524)
  at `bc41c1b` passes all three native GUI jobs: Ubuntu 24.04 AMD64/ARM64 and
  Windows Server 2022 AMD64 each pass 32 Save, 19 workspace and 17 detach
  checks. The Ubuntu 26.04 AMD64/ARM64 backend jobs and Windows 11 ARM64
  contracts/core job also pass. The fixture-only macOS correction at `4a99205`
  passes both Intel/ARM native core jobs in [CI 39824882032](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39824882032),
  alongside repeat Ubuntu 26 backend and Windows 11 ARM core passes. Historical
  macOS failures remain retained. A later worker-loss correction releases the
  close guard after confirmed worker termination, keeps saving disabled without
  native authority and warns of possible partial disk changes. That working-tree
  source passes 29 native UI library checks, strict Linux/MSVC lint and local
  GTK 32 Save / 19 workspace / 17 detach checks; its hosted GUI rerun is pending.
  See `native-source-save.json` for separate source hashes, receipts and scope.
  The 67-capability ledger remains unchanged; production launcher packaging is
  updated separately above.
- Current user priority is shipping GUI responsiveness, especially Board/Library
  opening/search/scroll/resize/selection, with permission to rework its backend.
  Close must sleep with loaded state in RAM; no visible Quit or repeated list
  reloads. The current changes address renderer work, unchanged data/search
  publication, background sleep IPC and constrained-host marquee policy.
  See [GUI performance evidence](../GUI_PERFORMANCE.md) for exact scope/checks
  and remaining host qualification. This slice changes the current Python
  application; it does not complete the Rust GUI or retire a Python service.
- The following shipping-UI continuation adds researched Arduino-inspired editor
  syntax colors, terminal theme/fit corrections, event-driven Linux PTY readiness
  and the root `LAUNCHER/` folder. Existing font/model/undo state is preserved.
  macOS/Fedora/Mint launcher entries are availability notices, not completed ports;
  Windows ARM64 is refused. See [current shipping platform verification](../PLATFORM_VERIFICATION.md)
  and [palette provenance](../../src/editor/ARDUINO_PALETTE.md). Focused CI expands
  to Ubuntu 24/26 on both CPUs and separately labels macOS/Windows ARM component
  availability. ARM Qt WebEngine's official wheel currently lacks native modules;
  record this gap without counting skipped rendering as a pass.
  AI replacement highlights now use green current text and a red original-text
  marker/hover; additions/removals keep green/red and paints are static. Existing
  diff counts, revisions and Accept/Reject/history authority are unchanged.
  These editor changes apply to the shipping offline page; native preview assets
  and full design/service parity remain separate migration work.
- Shipping GUI verification now combines exact checkpoints: `c8a1377`
  [39648393272](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39648393272)
  passes all four Ubuntu 24/26 AMD64/ARM64 browser jobs and both macOS offline
  component jobs. `2b79f98`
  [39650299818](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39650299818)
  passes both full Windows native rendering steps; its overall run retains two
  fixture failures. Final fixture-only `91632a1`
  [39653405199](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39653405199)
  passes both focused Windows jobs, each with 15 launcher passes / 9 platform
  skips, 7 release checks and 32 Qt resource checks. Product hashes remain
  unchanged across those renderer checkpoints. Windows ARM still lacks native
  WebEngine; macOS/Fedora/Mint full application ports and physical desktop/USB
  qualification remain pending. Continue native Save/Compile and host adapters
  under the existing usable-beta plan; no Python service is retired by this slice.
- User expanded replacement platform requirements on 21 October: Windows 10/11
  AMD64/ARM64, Ubuntu 24.04/26.04 LTS AMD64/ARM64, macOS Intel/Apple Silicon,
  Fedora x86_64/aarch64 and Linux Mint AMD64. See [required platform coverage](PLATFORM_SUPPORT.md)
  for assumptions, concrete source gaps and per-host release gates. This overrides
  the earlier Windows AMD64 / Ubuntu-only migration scope without changing shipping
  support. macOS has no native adapter; Windows 11 ARM64 now passes native
  contracts/core execution only and is refused by the current AVR host policy;
  Fedora/Mint still need distribution admission/setup. Both native macOS core
  jobs now pass after the fixture-only case-collision correction at `4a99205`;
  no macOS host backend or GUI is enabled. Keep the accepted usable-beta order while beginning
  these host adapters before final packaging; retain all existing exact receipts.
- Current backend code checkpoint: `29ec2b0`.
  [Backend CI 39541599780](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39541599780)
  passes Ubuntu AMD64/ARM64 226 tests and Windows 218, with 49 fake-tool checks
  on all three. The owned package metadata slice has separate exact-source
  evidence in `rust-avr-package-metadata.json`; actual package payload preparation
  remains local Linux x86_64 only. Native UI sources retain the checkpoint below.
- Native UI code checkpoint: `49ab312` on `codex/rust-tauri-migration`.
  [Complete native CI 39539221917](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39539221917)
  passes all five jobs: native Ubuntu AMD64/ARM64 and Windows plus both Windows
  editor jobs. Each native host passes 19 workspace/17 detach checks, including
  the retained native project session; native units are 23 per Ubuntu host and
  28 on Windows. Runtime source is `eb9d13c`; `49ab312` changes only the Windows
  workflow's expected workspace contract from 18 to 19 checks.
- Goal: native Rust GUI and application backend, embedded Monaco/xterm, private
  Python only for firmware tools. Shipping entry still uses Python.
- User approved a usable Rust beta milestone: Open/Edit/Save/Compile, then
  remaining device/services and packaging. Preserve the design/layout; retain
  glassmorphism if reliable. Otherwise use ordinary Light/Dark/system_default
  (follow OS appearance). This explicit fallback overrides older glass mandates.
- Continuation base: `dd1a6ff448367af1627241d991d8dd314167ec21` on
  `codex/rust-tauri-migration`. The new compile-input/tool-entry source is tracked
  in [its receipt](rust-compile-inputs.json). Runtime checkpoint `30d99af` adds native
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
- New continuation local evidence on Ubuntu 26.04.1 AMD64: **197 default Rust
  tests, 19 staging checks within that suite, 15 fixed process checks and 47
  fake-tool checks**. Strict Linux/MSVC-target linting and formatting pass.
  Private-runtime verification passes 46 tests plus isolated Qt recovery.
  Independent review fixes and exact source digests are in the new receipt;
  [CI 39534638853](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39534638853)
  at `dbf92f0` passes Ubuntu AMD64/ARM64 **197 tests/15 process checks** and
  Windows **190 tests/12 process checks**, with **47 fake-tool checks** on all
  hosts. Existing recipe-generation 31/23 checks also pass. No GUI jobs rerun.

## Implemented scope and next slice

- GTK/Win32 native project workers read explicit projects; ordered/native IDs
  govern source tabs. Save/Save All now use retained native file owners.
  No live app activation/metadata/real build dispatch yet.
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
- New `compile_inputs`/`prepared_target` pure services and `compile_fixture`
  capture/stage complete root C/C++ inputs, paired exact descriptor bytes,
  deterministic INI and every declared prepared-tree file. Native opaque
  owner/selection receipts pin identities, hashes and complete inventories;
  refusals revoke. Project, prepared inputs and writable output roots are
  separate. Only explicit `temp/audit/native-compile-inputs` fixtures are admitted;
  original/staged aliases and protected components refuse. No live target/store,
  `.ino` conversion, library discovery or Compile callback is integrated.
- The separate development `desktop/firmware-tools` entry accepts native roots,
  pinned AVR policy and jobs with mandatory `-I -S -B`; it imports no app settings
  or backend adapters. Its 47 fake-PlatformIO checks pass locally, including
  actual 6.2.0 mixin-shape guard fixtures, output/temp isolation and UTF-8 errors.
  This is not real PlatformIO/runtime qualification. The shipping Python wrapper
  remains unchanged. Nested startup, complete third-party provenance, native
  compiler processes/Windows tool locks and immutable input lifetime stay gates.
- A separate `compile_process_probe` owns only fixed self-child roles. Linux
  uses a dedicated subreaper/session and retained leader reservation; Windows
  uses suspended creation, explicit pipe inheritance and job assignment before
  resume. Output is bounded independently per stream without reader threads;
  incomplete cleanup poisons the single-probe lease. This does not adopt external
  compiler images, prove Windows pre-pin running-image equality or qualify
  detached external tool descendants. Exact checks/source follow in the receipt.
- Next: qualify admitted prepared runtime/byte lifetime and finite process
  ownership, then integrate one real native-controlled already-prepared C++
  sample on Windows/Ubuntu. Keep compiler dispatch unavailable until those gates
  pass. A verified native recipe with no override remains separate from the
  positive-only recipe service's NoRecipe refusal. See Task 3 in [the plan](NATIVE_RUST_PLAN.md).
- Prepared-sample investigation found the campaign work/package roots were
  cleaned. The retained successful Uno receipt used exact **atmelavr 5.3.0**,
  while **5.1.0** in the uninvoked tool entry was only the earlier proposal.
  New work is assembling an explicitly owned 5.3.0 sample from exact official
  archives, without restarting the campaign or adopting a live store. Runtime
  closure remains absent; `.venv-linux` uses an external interpreter link and
  app/setuptools `.pth` startup files. Do not call it an admitted staged runtime.
- Exact owned Linux x86_64 archive preparation now passes locally: four pinned
  archives (54,095,870 compressed bytes) produce 2,758 independent regular files
  / 240,449,534 bytes. Eight reviewed local toolchain library links become
  explicitly recorded regular copies only for that exact archive digest.
  All other links/specials refuse; complete output inventories and raw archive
  hashes are revalidated. See [separate preparation evidence](rust-avr-preparation.json).
  Local checks: 220 default Rust tests, including 23 archive checks; 49 fake-tool
  checks; strict Linux/MSVC-target linting and formatting; existing 46-test
  private runtime and isolated Qt recovery pass. Full independent archive
  review was interrupted by the helper account usage limit; author review is
  recorded. [Backend CI 39537857175](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39537857175)
  at `9a27b78` passes Ubuntu AMD64/ARM64 220 tests (23 archive checks) and
  Windows 212 (22 archive checks); all three pass 49 fake-tool checks.
  These are synthetic archive fixtures; actual payload preparation remains
  qualified locally on Linux x86_64 only.
- The uninvoked development tool entry now deliberately requires 5.3.0 and
  refuses the earlier 5.1.0 target/config. Historical `dbf92f0` receipts remain
  unchanged. A read-only runtime profile measured 22 declared distributions and
  an unqualified 33,071,077-byte runtime candidate. No interpreter was copied,
  startup hook followed, package metadata synthesized or compiler executed.
  Native loader closure, nested startup and tool lifetime remain gates. The
  complete candidate exceeds existing compile-fixture file/byte limits; those
  limits have not been relaxed before actual runtime admission.
- Native workspace continuation now retains one process-local project service,
  workspace ID and original document snapshots per window. Exact ordered editor
  IDs commit pending state; refusal/timeout/dispatch errors discard pending state
  without a source write. Scripts move out before browser calls to avoid
  retaining native state borrows across IPC. Source saves and live/coexistence
  ownership remain disabled. Local evidence: 23 native library tests, 19 workspace
  plus 17 detach checks, strict Linux/MSVC-target linting and formatting. See
  [session evidence](native-project-sessions.json); the complete matrix above
  now passes. Intermediate runs `39538717552` and `39539003082` were canceled
  by subsequent branch dispatches, not counted as runtime failures or passes.
- Static PlatformIO 6.2.0 inspection confirmed that archive-only fallback
  metadata has no registry owner and cannot satisfy explicit `platformio/...`
  installed lookup. Verified registry identities and the `.piopm` format are
  recorded in `temp/audit/native-compile-inputs/owned-package-metadata-next.md`.
  A new separate `prepare_avr_package_fixture` now creates the four deterministic
  native records in a fresh generation after fixed official-envelope and full
  byte verification. Incoming `.piopm` stays refused; generated records count
  toward existing limits and the complete final inventory. Local exact Linux
  payload: 2,762 files / 240,450,165 bytes, including 631 metadata bytes. The
  original 2,758-file aggregate matches the prior checkpoint exactly. Local
  checks: 226 backend tests (29 archive/metadata checks), strict Linux/MSVC
  linting and formatting. See [new metadata evidence](rust-avr-package-metadata.json);
  backend CI `39541599780` at `29ec2b0` now passes all three hosts, with 29
  archive/metadata checks per Ubuntu host and 28 on Windows. Native UI sources
  remain unchanged from their passing full matrix.
  The named OS interpreter's static loader/required-library records are in
  `runtime-loader-profile.json` in the same temp folder. Complete native loader
  and private runtime admission remains unqualified; no dependency was copied.
- Continue this work using the accepted delivery
  order in the plan; integrate usable features instead of stopping at probes.
- Legacy source-save coexistence, catalog/build integration, serial/PTY, settings/
  packages/setup/cloud/vault, complete screens and release/device gates remain.

## Work boundaries and focused commands

- Reuse checkout/desktop target; no new worktree/dependency install. Source
  `desktop/dev-env.sh`, offline Cargo and CARGO_BUILD_JOBS=6; retain >=5GiB disk.
  Free space fell below the reserve during this continuation. Agent-owned Cargo
  and analyzer build products, the unused older stable Rust toolchain and two
  superseded prepared fixture trees were removed; pinned Rust 1.99.1, offline
  registry/SDK, newest prepared AVR payload, reports and the last verified UI
  executable remain. Around 9.5GiB is free at this checkpoint. Rebuild Cargo
  outputs only with the reserve rechecked; prefer the pending hosted checks.
  Cleanup receipt: `temp/audit/gui-performance/rebuildable-storage-cleanup.json`.
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
