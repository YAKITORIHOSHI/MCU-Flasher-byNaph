# Native Rust GUI and Backend Implementation Plan

> **For agentic workers:** Continue the existing migration task by task with independent review where available. Preserve the parity ledger and exact verification receipts; completed prototype work must not be repeated or called production ready.

**Goal:** Replace the Python GUI and application backend with a native Rust widget application, retaining offline Monaco and xterm as embedded components and private Python only for approved firmware tools.

**Architecture:** A native Rust widget shell owns windows, focus, layout and commands. Existing GUI-independent Rust core/host services remain the foundation. Embedded editor/terminal components use a bounded typed bridge to Rust; they cannot own filesystem, serial, cloud, credentials or process authority. The Tauri web shell remains a tested prototype while native widget/WebView integration is qualified.

**Tech Stack:** Rust core/host/contracts; GTK3 on Linux and Win32 on Windows for the current native-widget feasibility adapters, with an AppKit/WKWebView adapter required for macOS; existing Monaco/xterm assets embedded through the host browser engine; certified PlatformIO/private Python and native firmware utilities as subprocess tools. Full desktop and release qualification remains open.

**Spec:** User clarification, 20 October 2026 (Asia/Manila), the 21 October [platform expansion](PLATFORM_SUPPORT.md), and `docs/rust-tauri-transition-proposal.md` with these amendments taking precedence over its earlier TypeScript application-shell design and narrower host scope.

## Global constraints

- Native Rust GUI widgets; keep Monaco and xterm embedded.
- All application backend logic and authority move to Rust. No Python GUI, Python application server, or hidden Python service in the replacement release.
- Python may remain only in the private firmware tool runtime, including PlatformIO/builder compatibility code that belongs to those tools. Development reference tests may still use Python.
- Required replacement hosts now include Windows 10/11 AMD64/ARM64, Ubuntu 24.04/26.04 LTS AMD64/ARM64, macOS Intel/Apple Silicon, Fedora x86_64/aarch64 and Linux Mint AMD64. The user's 21 October expansion takes precedence over the earlier Windows AMD64 / Ubuntu-only scope. Use the [platform matrix and remaining gates](PLATFORM_SUPPORT.md); native desktops, emulation, hosted builds and device evidence remain separate.
- Preserve all 67 parity contracts, compact layout/workflow, source bytes, editor buffers, focus, independent projects, terminal/serial backpressure, fixed Firebase deployment and account compatibility.
- Theme preference approved for the continuation chat: retain glassmorphism if it works reliably with the native toolkit. If it is incompatible, unreliable or harms responsiveness, proceed with ordinary Light, Dark and `system_default` themes; `system_default` follows OS appearance. Preserve layout, controls, branding and editor state. Decorative effects must not block the functional migration. This preference takes precedence over earlier requirements to retain glass surfaces exactly.
- Preserve protected cache/journal boundaries and all user sketches/settings. Existing Python application remains production until replacement parity, native qualification and rollback gates pass.
- Current Python release qualification includes a real sample compile for every uniquely matched PlatformIO board from all configured indexes. No Arduino CLI fallback or generic-family substitution in this campaign.
- Use all 12 logical cores within a controlled compile budget; retain at least 5 GiB free disk and cap campaign-owned growth at 6 GiB on the current host. Only owned scratch outputs may be cleaned.

## Review focus

- Native widget/WebView ownership during detach, focus/IME/clipboard and mixed-DPI resize must preserve editor models and terminal sessions.
- A renderer-provided path, owner, revision or port must never grant native authority.
- Unexpected package/catalog identity or ambiguous board matching must produce an explicit unresolved/skip result, never a substitute board or a pass.
- Interruption/storage pressure must leave user state untouched and preserve resumable receipts; incomplete work must not appear as a successful compile.
- Release packaging must reject an application that starts Python to provide GUI/backend behavior, while allowing explicit firmware tool subprocesses.

## Accepted delivery order

The user approved organizing the remaining work around a usable Rust beta:

1. Integrate the everyday native workspace: persistent project ownership, Open/Edit/Save, exact board selection, settings and retained editor state.
2. Complete a real Rust-controlled Compile workflow, starting with verified target/source/config/tool staging and a tool-only Python boundary. Prove one prepared sample on Windows/Ubuntu, then expand through the configured board inventory.
3. Complete device/serial/terminal services, libraries, downloads, setup/repair and Firebase/account services in Rust; retire each corresponding Python application service only after end-to-end qualification.
4. Package beta artifacts for the [required host matrix](PLATFORM_SUPPORT.md) with correct icons; qualify installation, native input/layout, recovery and hardware workflows per host before switching the production launcher. Windows/Ubuntu remain the initial vertical slice, while macOS, Windows ARM64, Fedora and Mint porting must start before final packaging and cannot be omitted from replacement acceptance.

Run independent GUI/backend/Windows work in parallel where it helps. Measure progress by working end-to-end features and retired Python services, with tests as supporting evidence. Retain completed campaign/CI receipts and storage budgets; do not restart passed work. The detailed tasks below remain the dependency and qualification ledger.

## Task 1: Complete current Python release compile qualification

**Files:** `direct/board_compile_indexes.py`, `direct/board_compile_campaign.py`, `direct/board_compile_manifest.py`, target/index validators and isolated `direct/verify_*` checks; reports in `audit/migration/`.

**Interfaces:** Index inventory produces source URL/version/checksum and every visible `boards.txt` declaration. Manifest resolution produces one exact `(platform specification, board ID, Arduino)` per unique unambiguous target plus explicit skipped declarations. Campaign consumes that manifest and emits per-target result/command/package/manifest/artifact hashes and resource evidence.

- [x] Confirm current Python app and all configured indexes as the campaign scope.
- [x] Reproduce current registry rejection of legitimate plus-sign board IDs.
- [x] Verify the narrow board-ID fix and unsafe input refusals, including a fresh complete registry response.
- [x] Checksum index archives, retain exact declarations, resolve unique compatible PlatformIO targets and report every skipped/failed source.
- [x] Run real builds with `--jobs 12`, retain logs/artifact hashes, remove only completed owned build outputs, and report every eligible target's outcome.
- [x] Verify shared production INI/compatibility selection and close UNO Q's path-with-spaces defect with a real original-path tool build and pinned firmware-tool post hook.
- [ ] Complete current-checkpoint CI review and actual user desktop/device/installation release qualification. Shared generation fixtures and the compile campaign do not qualify every target's GUI Compile workflow or upload/reset/USB.

## Task 2: Prove the native Rust widget shell and embedded components

**Files:** Create a separately gated `desktop/native-ui/` feasibility crate and verifier after toolkit selection; reuse `desktop/crates/{core,host,contracts}` and existing staged assets.

**Interfaces:** Native window supplies trusted workspace/window identity; editor/terminal supply only typed requests and consumed-byte acknowledgements. No fixture may activate live workspaces or devices.

- [x] Compare official toolkit/control/WebView support on required hosts and record the provisional GTK3/Win32 decision, alternatives and dependency classes in the [toolkit decision](../../docs/native-rust-widget-decision.md).
- [ ] Measure clean-host prerequisite downloads, installed dependency closure, artifact/assets and process-tree RAM on each architecture before production toolkit/distribution qualification.
- [x] Prove real native Controls and split panes around separate offline Monaco/xterm fixture views on GTK Linux and Win32 Windows, including editor-only detach/reattach through native ownership.
- [x] Implement the shared original three-theme palette (39 tokens / 117 exact color values) and Montserrat in native GTK/Win32 toolbar, Controls, source-tab and tool-tab chrome. GTK reuses original action SVGs; Win32 reproduces native brand paths, while its action/tool-tab icons remain unfinished. Hosted CI passes at `e334d7b`; actual Windows 10/11 input and responsive desktop qualification remain open.
- [x] Add the GTK read-only Rust project-service adapter for an explicit `--project` folder and the native Project picker, opening each chosen folder in an independent process. Verify retained models/dirty state/selection/themes/undo and unchanged owned source bytes with 18 workspace smoke checks; no save or ownership activation is enabled.
- [x] Share the read-only project adapter with Win32 at `297cae7`, with `--project`, `--theme`, `--workspace-smoke`, exact ordered acknowledgements and known-ID monotonic dirty updates. Its new workspace initially fails before assertions at trusted-root path spelling; corrected `0e0db8a` passes Windows 35 GUI. Actual chooser/input/icon/child-survival qualification remains open.
- [x] Implement Windows native Project selection to an independent current-Rust-executable process with literal OS path/theme arguments, cancel/no-spawn and a 16-child cap; add original-ICO owned small/large HICONs through a bounded shared parser and aligned native-decoder storage. Source/re-review and Windows native units/35 GUI pass at `0e0db8a` in `39426407678`.
- [ ] Complete the actual workspace, source tabs, saved fonts/static glass/assets, auxiliary screens and live service integration beyond the feasibility controls.
- [ ] Verify resize/focus/tab order/IME/clipboard at native 100–200%, editor-only detach/reattach, stale response refusal and bounded stream backpressure on each host.
- [ ] Preserve the Tauri prototype's reusable Rust service/contract proofs; retire only the superseded application web shell after equivalent native evidence exists.
- [ ] Add the native AppKit/WKWebView adapter for macOS Intel/Apple Silicon; qualify the shared Win32 adapter on native Windows ARM64 and the GTK adapter on Ubuntu 26.04, Fedora and Mint. Follow the expanded matrix; no existing hosted result closes these host-specific gates.

## Task 3: Finish Rust application service ownership

**Files:** Existing `desktop/crates/core`, `desktop/crates/host`, `desktop/crates/contracts`; service-specific native integration and differential verifiers.

**Interfaces:** Trusted native window/service registry owns workspace revisions, leases and process/device observations. Existing pure ports and metadata-only identity APIs keep their current scope until explicit transactional write/recovery tests pass.

- [x] Port the pure exact Onehorse/MiniMain recipe decisions at `69d845a`: seven new unit tests and 951 differential decisions (16 positive / 935 empty) plus 11 protocol checks. This subset performs no installed-metadata reading, Arduino Q hook selection or actual Rust compilation and does not complete BLD-01 or retire a service.
- [x] Add fixture-only bounded native descriptor snapshots and exact Linux Arduino Q policy at `26c7675`; preserve wider JSON/path refusals as explicit limits. Both Ubuntu hosts pass 136 backend tests/987 decisions, and Windows passes 132 tests/979 decisions after the literal-parent fixture-only correction (`39443653534`); parser/path differences remain separately counted. This is a dependency milestone, not production package/build authority.
- [x] Add fixture-only native recipe-generation observation/revalidation at `30d99af`: retained ancestor/file identities, raw byte digests, exact selection/host/options, bounded opaque observations and refusal invalidation. Ubuntu AMD64/ARM64 pass 148 backend tests/31 process checks; Windows passes 144/23 after controller-only `002cc95`. This does not freeze packages, close final-use races, enable build dispatch or retire a service.
- [ ] Implement exact prepared-target plus multi-file source/config/tool-provenance staging in a create-new owned native compile scope; keep subprocess dispatch unavailable until admitted tool inputs and lifetime are qualified.
  Fixture implementation now captures root C/C++ bytes, exact descriptor pairs,
  fixed INI and the complete declared prepared tree in new files; opaque native
  owner/selection receipts revalidate inventories, identities and digests.
  This does not complete the item: real prepared runtime/closure/lifetime,
  `.ino` conversion and native workspace integration remain unqualified.
  Separate development dependencies now include a settings-free fixed AVR
  tool entry with isolated fake-PlatformIO verification and a fixed-self native
  process containment fixture. Neither grants external compiler dispatch.
- [ ] Complete exact target/catalog and build-input policy, source-save transactions/identity races/recovery, coexistence leases and activation/release.
- [ ] Integrate Rust serial reader/Send worker and PTY/process ownership with finite deadlines and no replay/retry of hardware writes.
- [ ] Port settings, cloud/auth/vault, downloader/package/setup/repair and native maintenance orchestration under the 67-capability ledger.
- [ ] Implement Darwin identity/process/PTY/keychain adapters and per-distribution Linux release/dependency/setup policies; qualify native ARM64 GUI/backend and USB-driver closures. Record and separately qualify any emulated firmware-tool/runtime mode without bypassing existing host checks.
- [ ] For each service: compare original Python outputs/failures, run native fixtures, integrate native UI, then classify/remove its Python application adapter. Keep tool-integrated Python only with an explicit tool classification.

## Task 4: Package and qualify the replacement

**Files:** Native launchers/build manifests/release verifiers, OS-specific CI and parity/removal ledgers.

- [ ] Enforce an allowlist of Python firmware-tool subprocess entry points and reject Python GUI/backend imports, servers or callbacks in replacement artifacts.
- [ ] Package native Windows AMD64/ARM64, Ubuntu AMD64/ARM64, macOS Intel/Apple Silicon, Fedora x86_64/aarch64 and Mint AMD64 artifacts from their qualified baselines; preserve offline tools, signatures/notices, icon integration and standard-user execution. Include macOS app-bundle signing/notarization and Fedora/Mint-specific dependency/desktop integration.
- [ ] Expand native CI and actual desktop/device evidence across every required platform row. Current Ubuntu 24.04 and Windows Server AMD64 fixtures remain only their recorded evidence; do not label cross-builds, containers or emulation as unperformed native desktop qualification.
- [ ] Run native complete workflow/device/USB, interruption/recovery, performance and rollback qualification; compile success alone does not qualify upload/reset behavior.
- [ ] Replace the production entry point only after every required parity/release gate passes; otherwise retain the working Python release and report the remaining gates.

## Execution record

The `e334d7b` `desktop/native-ui/` workspace milestone extends the
GTK/Win32 fixture with native toolbar/Controls/source/tool chrome and the shared
original design tokens and font. GTK loads original action SVGs; Win32
reproduces native brand paths but still uses text action buttons and needs
action/tool-tab icon parity. GTK reads an explicitly selected project's
root-source manifest through `mcu_core::ProjectService` on a worker, then binds
the results to retained Monaco models. The native Project picker opens another
independent process instead of replacing the current editor's buffers. It does
not register live ownership, write source/settings/cache files, or enable
compile/upload/reset/cloud/package services. Unavailable actions stay disabled.

The local GTK workspace smoke passes 18 checks across all three themes, including
two retained source models, dirty content/selection, undo/redo and source bytes
left unchanged. Twelve protocol/design unit checks and two Linux reader unit
checks pass, alongside five controller lifecycle and three path checks. The
separate 17-check detach fixture also passes, for 35 local GTK GUI smoke checks.
Five visually reviewed captures are retained under ignored
`captures/native-workspace/`: three themes at 1100×740, compact 960×540 and
`GDK_SCALE=2` at measured 1792×1062 on a 1920×1080 display. Their native reading
spans/active controls remain visible; normal close and unchanged explicit-source
bytes pass. Visual review corrected GTK tool-tab contrast. The `e334d7b`
styled adapters and Win32 short-height/DPI corrections now pass hosted native
execution: 17 Windows Server checks and 35 GTK checks on each Ubuntu AMD64/ARM64
host. Exact design parity still needs Settings, menus, auxiliary screens,
tool-tab icons and live toolbar/service behavior. Actual Windows 10/11 desktop
input, fractional-DPI/mixed-monitor layout and full service integration remain
unqualified. Subsequent GTK/Win32 logo ink (`#56cfbf`) and final Win32 glass
`TEXT_BRIGHT` action-ink corrections pass local strict Linux/MSVC-target Clippy
only; those corrections are included in `297cae7` and are outside the older
tested checkpoint. `297cae7` shares `workspace_project.rs` between GTK/Win32 for
explicit read-only projects. Ordered acknowledgements and known-ID monotonic
dirty updates preserve native ownership of source controls. Manual backend
backend actions remain disabled. Windows Project picker/HICON source is now in
`0e0db8a`, with native unit/fixture results passing and actual desktop qualification open.

Current Linux evidence is 18 native-unit checks, 35 GUI smoke checks,
and an additional real 40-tab GTK regression: before the fix the top-level
window grew to 6412 pixels; after the fix it remains 960 pixels while retaining
the 6412-pixel scrollable row and all 40 tabs. A 42-source compact 960×540 capture
is retained under ignored `captures/native-workspace-many-sources/`. Main and
detached GTK windows reuse the existing `mcu_icon.png` native icon. The bounded
source review in `scratch/native-ui/windows-reader-review.md` found no new
actionable issue after the two prior overflow/manual-action findings were fixed.
No launcher, runtime entry or installer changed.
The ledger remains 14 partial/53 pending with zero Python application services
retired, and the shipping Python launcher is unchanged. Use the current
[preview commands](../../desktop/native-ui/README.md) and update receipts only
after the corresponding execution completes.
Focused migration CI `39392866712` passes all five jobs at `e334d7b`, including
the new Linux workspace smoke. The separate current Python Windows runtime run
`39392313449` also passes both jobs at `60e4bc4`, including actual Uno/Nano/Mega
AVR Compile and native GCC path fixtures. These results retain their tested
sources and do not close replacement desktop/device/release gates.
CI `39421097096` at `297cae75c5d4202461b3ab6dc0ba2aafe76fab25` finishes 4/5.
Both Ubuntu jobs pass GTK 35, the extra wide-tab regression and recipe checks;
both Windows editor jobs pass. Windows native old 17, three source-state units
and recipe checks pass, but the new workspace fails before assertions because
canonical `\\?\D:` fixture spelling differs from the plain trusted app root.
The shared fix recognizes original/canonical trusted-root spellings without
changing the selected literal path; three local reader checks and GTK 35 pass.
The full Windows 35-check path subsequently passes at `0e0db8a`.

The native-UI caption checkpoint is `6ef749a1f55841359fc380a572265c8323408a15`; its only change from
CI-tested `0e0db8a` is “Select serial port” (strict MSVC/fmt checks). Combined
Windows-only run `39426407678` passes at exact `0e0db8a` (one native job); four unchanged
successful jobs retain their `297cae7` results. Native Project selection uses
literal OS path/theme arguments to the current Rust executable on a worker,
with cancel/no-spawn, a 16-child cap and existing parent buffers retained.
Original ICO parsing has three pure tests; main/detached small/large HICONs own
their handles. The review's DWORD-alignment P2 is fixed with aligned `Vec<u32>`
storage and exact payload bytes/length; scoped re-review is clean in
`scratch/native-ui/windows-picker-icon-review.md`. Cancel/Unicode UTF-16 args,
HICON/alignment units pass: 16 library/seven Windows binary units, including
real owned small/large HICONs and deliberately unaligned input, 17/17 detach,
18/18 workspace and recipe 951 + 11 protocol. Both Unicode/space source files
pass. Receipts are under `scratch/native-project-ci-0e0db8a/migration-prototype-windows-2022/audit/migration/logs/`.
Actual chooser interaction,
child survival, Windows 10/11 input and PE Explorer icon/packaging remain open.
No new Cargo dependency/lockfile or installer/runtime entry changed.

`69d845a` adds pure exact Onehorse/MiniMain recipe policy and brings the default
Rust suite to 126 tests (119 existing + seven policy tests). The differential
probe matches 951 decisions, with 16 positive and 935 empty decisions, plus 11
separate protocol/boundary checks. Independent bounded review found no
actionable issue (`scratch/native-ui/recipe-policy-review.md`). Trusted installed
metadata acquisition, Arduino Q filesystem/hook selection and real Rust build
integration remain open. This is a pure-policy appendix to pending BLD-01.

The completed campaign records **318/318 successful exact target builds** across
ten pinned platforms from all configured indexes (official Arduino and the saved
Espressif index). Of 213 distinct skipped declarations, 180 are unresolved and
33 ambiguous. Onehorse and MiniMain pass the shared exact-recipe repairs.
Experimental Linux UNO Q's path-with-spaces blocker is closed at `f249c21`:
`scratch/board-campaign-20261019/unoq-app-hook-batch/results.jsonl` retains a fresh
original-path job's preparation failure and successful real compatibility retry.
Its earlier no-space alias pass remains historical evidence only.

The shared production selector verifies exact board/platform/builder hashes and
framework identity before selecting `src/modules/arduinoq_link_paths.py`. This
Linux-only SCons post hook represents only two known `-T` operands as File nodes;
its digest is part of generated INI inputs and firmware reuse invalidation.
It is approved firmware-tool Python, not an application backend service. Port
its application selection/ownership policy to Rust; tool-integrated Python may
remain inside the external builder. Installed packages are never patched.
The 5,376-byte UNO Q ELF/LLEXT/packed sketch requires resident firmware and does
not qualify a standalone full-device image. See `BOARD_COMPILE_REPORT.md` and
`board-compile-campaign.json`.

All owned campaign work/package stores are cleaned, with approximately 10.9 GiB
free at cleanup. Metadata pruning preserved every board/platform hash while
removing approximately 398 MiB of unnecessary owned content. No user/global
stores were cleaned. At `f249c21`, fifteen local recipe checks pass and Windows
skips three Linux-only cases. Current checkpoint `8dbdb9d` reproduces and fixes
a FIFO metadata-reader stall with bounded regular-file descriptor reads; sixteen
local recipe checks pass, with four Linux-only cases expected to skip on Windows.
Five release source-selection checks pass for excluding developer `desktop/`
and `audit/` roots from current Python packages while preserving the strict
shipped state guard. These are source-selection and reader regressions, not a
fresh 318-target compile rerun or release installation. Thirteen campaign guard
checks, 46 runtime checks and 20 responsive checks pass at their recorded source.
Current Python build qualification passes;
actual user desktop/device/upload/reset/USB and installation qualification still
prevent unrestricted deployment.

All 25 CI jobs pass at `7da4be0`. The SDK-specific `77e5ccb` correction passes
all five migration jobs in run 39357769519: Windows Win32/WebView2 and Linux GTK
AMD64/ARM64 each execute 17 native smoke checks. The follow-up protocol has seven
checks on Linux and six on Windows. The `f249c21` focused migration run
39359319887 also passes all five jobs. The subsequent `8dbdb9d` focused CI passes
all five jobs in run 39360040590, with 17 native widget checks per host and
16 recipe checks (four expected Windows skips). These are native fixture proofs; full application/backend,
Windows 10/11 input, Wayland and mixed-monitor qualification remain open.

20 October 2026: both configured index sources produced 37 inventoried package
entries and 949 visible declarations. The refreshed registry has 1,654 boards.
The narrow Python board-ID fix passes 34 catalog and 16 target-resolution checks;
Rust syntax parity covers 13,462 comparisons while strict non-board fields stay
unchanged. Real board compilation completed in storage-bounded MCU batches.
Unresolved declarations remain explicit, rather than accepted aliases.

The native-widget smoke retains the editor model/history and terminal through
three editor-only detach/reattach cycles. Five controller lifecycle checks and
three output/path checks cover the Linux controller's bounded cleanup and owned
scratch publication. The 67-capability ledger remains 14 partial fixture subsets
and 53 pending, with zero Python application services retired. Native mappings
and fixture callbacks do not qualify the remaining Rust application services.

Campaign review reproduced and corrected shared-identity false matches,
surviving child-process cleanup and scratch-ancestor symlink acceptance. Thirteen
offline guard checks pass. The audit runner refuses non-Linux hosts pending a
separate qualified native process implementation; this does not change the
production application's Windows support.

The user explicitly authorized continuation. Continue remaining native application
and Rust backend work without restarting the completed campaign. No new approval
checkpoint is required for reversible implementation within these boundaries.
Final deployment remains a separate concrete release decision.

At `26c7675`, Rust adds development-only descriptor snapshots and exact
Arduino Q compatibility selection, reusing the unchanged Onehorse/MiniMain
policy. Native probe reads use bounded regular-file handles and native identity;
fixed owned fixtures contain package metadata and a replica application hook.
All 136 local backend tests pass. The Linux differential covers 987 matching
decisions, with four Python exceptions, three stricter path refusals and 21
parser refusals counted separately; 870 no-read and 32 protocol checks also pass.
Both Ubuntu architectures pass backend CI `39443196583`: 136 backend tests,
987 compatibility decisions and 32 protocol checks per host. Windows initially
finds a test-construction issue: joining `..` onto a canonical verbatim prefix
normalizes away the intended alias. `cc6a6d7` preserves literal OS units in that
test only; runtime reader/core policy stay unchanged. Its Windows-only rerun
`39443653534` passes 132 backend tests, 979 compatibility decisions and 32 protocol
checks, with ten Linux-only cases skipped and 18 parser refusals recorded
separately. Preserve the Ubuntu evidence at `26c7675`; no new combined full
GUI/editor matrix is claimed.
The new `backend_only` dispatch avoids GUI dependencies and retains compact
receipts; the default five migration jobs remain intact. These snapshots grant
no future build authority. Live package/ancestor/content races, wider JSON and
non-UTF8 path parity, production reader/build integration and full BLD-01 remain
open. See [compatibility evidence](rust-board-compatibility.json).

The native recipe-generation checkpoint `30d99af` now pins observed root,
ancestor and file identities, binds exact native selection/host/rule/options,
and rereads every consulted raw input before accepting revalidation. Stale,
foreign, incomplete, empty or changed observations refuse. Both Ubuntu hosts
pass 148 backend tests and 31 process checks in CI `39447542929`. Its Windows
Rust suite passes all 144 tests; a controller overwrite of an open retained file
fails before revalidation. Controller-only `002cc95` mirrors the passing native
rename-aside fixture, asserts new leaf identity/same bytes, and passes all 23
Windows process checks plus 144 tests in `39448048718`. Prior 987/979 compatibility
and 951 recipe comparisons remain passing. No Rust runtime source changes in
that follow-up; preserve both exact sources and the initial controller failure.
Opaque fixture observations grant no execution permission or immutable package
identity. Active ABA, non-atomic reads and final check/use remain gates. See
[recipe-generation evidence](rust-recipe-generation.json).

Next implementation follows the existing Task 3 authority boundary: use
`compile_inputs.rs` for typed exact prepared targets/deterministic input plans,
then `compile_fixture.rs` for create-new owned byte staging and revalidation.
Bind the full root-source inventory, exact config and admitted runtime/tool-tree
provenance; reject aliases, partial capture and stale selection before any spawn.
The current Python offline PlatformIO wrapper reads application settings through
`offline_mode`; a replacement tool-only entry must receive native policy and
must not import Python application configuration. A later finite native process
owner may run one literal already-prepared C++/Arduino sample, with installed-only
dependencies and unchanged storage/CPU/process cleanup bounds. Valid native
recipes with no compatibility override need verified-target evidence separately
from the generation service's intentional `NoRecipe` refusal. Cooperative leases
alone do not prove immutable tool inputs or close final check/use races.

### 21 October continuation: native compile inputs and tool boundary

Added pure literal target/config/source planning, exact paired descriptor checks
and fixture-only owned native staging. Source, prepared tools and writable
outputs have separate roots. Opaque owner/selection receipts re-read complete
inventories, native identities and raw hashes; create-new copies never retain
source hardlinks. Protected components, ambiguous descriptors, changed bytes,
foreign/stale/replaced observations and undeclared staged entries refuse.

The separate development firmware-tool entry has no application imports or
settings lookup and receives explicit native roots/jobs/target. Its 47 fake
checks cover exact config/argv, installed-only package guards using the observed
6.2.0 mixin shape, output/temp isolation and UTF-8 refusals. No real tool runs.
Fixed-self native process evidence adds bounded independent pipes, cancellation,
deadlines and owned group/job cleanup without reader threads. Review caught and
closed Linux inherited SIGCHLD auto-reap/reservation loss before the checkpoint.
Windows pre-pin running-image equality remains explicitly unproven.

Local Ubuntu 26.04.1 AMD64 passes 197 default Rust tests, 15 process checks,
strict Linux/MSVC-target linting, formatting and the existing 46-test private
runtime/isolated Qt check. Source and review scope are recorded in
`rust-compile-inputs.json`. CI `39534638853` at exact source `dbf92f0` now passes
both Ubuntu architectures (197 tests/15 process checks) and Windows (190/12),
with 47 fake-tool checks on all three hosts. Prior GUI receipts stay unchanged.
Task 3 and BLD-01 stay open: complete runtime/tool admission and immutable
lifetime, external compiler ownership, nested Python startup, `.ino`/libraries
and native workspace integration are still required. No Python application
service is retired, no hardware/cloud action ran and production stays Python.

The next prepared-sample slice uses the retained successful Uno tuple
`platformio/atmelavr@5.3.0`, `framework-arduino-avr@5.4.0`,
`tool-scons@4.41101.0`, `toolchain-atmelavr@1.70300.191015`. Campaign work trees
were deliberately cleaned; receipts are not installed payload proof. This
explicit owned sample supersedes the earlier uninvoked 5.1.0 proposal only when
its preparer/tool-entry changes are integrated. Reacquire just the exact missing
archives, verify official record hashes and bounded owned extraction, then admit
the complete private runtime before any tool dispatch. No campaign restart.

The exact owned Linux x86_64 four-archive preparer now produces 2,758 regular
files / 240,449,534 bytes, with a closed output inventory and independent probe
rereads. Eight same-directory library links from the one reviewed toolchain
digest become recorded independent regular copies; other links/special entries
refuse. All archives are hashed before output reservation and after decoding,
with bounded raw GNU/PAX handling, gzip CRC/EOF checks and create-new files.
Local evidence is 220 default Rust tests (23 archive checks), 49 fake-tool checks,
strict Linux/MSVC-target linting and formatting, and the unchanged 46-test private
runtime/isolated Qt recovery. See `rust-avr-preparation.json`. Independent full
archive review remains pending after helper usage limits interrupted review.
Subsequent cross-host results follow below. This does not close BLD-01 or retire
a service.

The development entry now requires the deliberately selected 5.3.0 tuple and
refuses old 5.1.0 target/config. Runtime profiling measured 22 declared Python
distributions; a smaller 33,071,077-byte candidate excludes startup hooks,
GUI/development categories and aliases. It is not a qualified import/native
loader closure or owned interpreter. Admit those inputs and nested startup
before extending staging bounds or enabling external compiler dispatch.

Backend CI `39537857175` at `9a27b78` now passes Ubuntu AMD64/ARM64 220 tests
(23 archive checks) and Windows 212 (22), with 49 fake-tool checks on all three.
These are synthetic archive cases; actual payload preparation remains local
Linux x86_64 evidence. Full independent archive review is still incomplete.

The native project-session continuation integrates one retained read service,
workspace ID and original document baselines per GTK/Win32 window. Exact ordered
editor acknowledgements commit pending state; native Project still opens an
independent window. Script dispatch moves its owned payload out before browser
calls, and no session borrow crosses reentrant IPC. Errors/timeouts release
pending state. Local evidence: 23 native library tests, 19 workspace/17 detach
checks and strict Linux/MSVC-target linting/formatting. This is a direct native
workspace foundation for Save, not a live/coexistence write lease or source-save
implementation. See `native-project-sessions.json`.

Complete native CI `39539221917` at `49ab312` now passes all five jobs. Runtime
source equals `eb9d13c`; the follow-up changes only the Windows workflow contract
to require 19 workspace checks and the retained-session assertion. Each Ubuntu
host passes 23 native units, Windows passes 28, and all three pass 19 workspace
plus 17 detach checks. Both Windows Python editor jobs pass. Intermediate runs
were canceled by subsequent branch dispatches and do not qualify native runtime.
Full independent session/archive reviews and actual desktop/device gates remain.

The separate native package-fixture entry now creates four deterministic
PlatformIO 6.2.0 `.piopm` owner/name/version/registry-ID records after a fixed
official role/host size/hash envelope and all actual archive bytes verify.
Incoming archive metadata remains refused; only the four fixed native paths
enter the closed output inventory, within existing limits and create-new rules.
Local exact Linux x86_64 preparation produces 2,762 files / 240,450,165 bytes,
including 631 metadata bytes; the original 2,758-file aggregate remains identical
to the prior archive-only checkpoint. Local checks: 226 backend tests (29 archive
and metadata cases), strict Linux/MSVC-target linting and formatting. Backend CI
`39541599780` at `29ec2b0` now passes all three hosts: 226 tests on both Ubuntu
architectures, 218 on Windows, with 49 fake-tool checks everywhere. Native UI
source remains unchanged at its passing checkpoint. Full private
runtime/loader/nested-startup admission, immutable tool
lifetime, actual installed lookup and real compiler dispatch remain gates. See
`rust-avr-package-metadata.json`; package records are not a ready marker.
