# Native Rust GUI and Backend Implementation Plan

> **For agentic workers:** Continue the existing migration task by task with independent review where available. Preserve the parity ledger and exact verification receipts; completed prototype work must not be repeated or called production ready.

**Goal:** Replace the Python GUI and application backend with a native Rust widget application, retaining offline Monaco and xterm as embedded components and private Python only for approved firmware tools.

**Architecture:** A native Rust widget shell owns windows, focus, layout and commands. Existing GUI-independent Rust core/host services remain the foundation. Embedded editor/terminal components use a bounded typed bridge to Rust; they cannot own filesystem, serial, cloud, credentials or process authority. The Tauri web shell remains a tested prototype while native widget/WebView integration is qualified.

**Tech Stack:** Rust core/host/contracts; GTK3 on Linux and Win32 on Windows for the current native-widget feasibility adapters; existing Monaco/xterm assets embedded through the host browser engine; certified PlatformIO/private Python and native firmware utilities as subprocess tools. Full desktop and release qualification remains open.

**Spec:** User clarification, 20 October 2026 (Asia/Manila), and `docs/rust-tauri-transition-proposal.md` with this amendment taking precedence over its earlier TypeScript application-shell design.

## Global constraints

- Native Rust GUI widgets; keep Monaco and xterm embedded.
- All application backend logic and authority move to Rust. No Python GUI, Python application server, or hidden Python service in the replacement release.
- Python may remain only in the private firmware tool runtime, including PlatformIO/builder compatibility code that belongs to those tools. Development reference tests may still use Python.
- Require Windows 10/11 AMD64 and native Ubuntu 24.04+ AMD64/ARM64; qualify actual native desktops separately from hosted builds.
- Preserve all 67 parity contracts, compact/static glass workflow, source bytes, editor buffers, focus, independent projects, terminal/serial backpressure, fixed Firebase deployment and account compatibility.
- Preserve protected cache/journal boundaries and all user sketches/settings. Existing Python application remains production until replacement parity, native qualification and rollback gates pass.
- Current Python release qualification includes a real sample compile for every uniquely matched PlatformIO board from all configured indexes. No Arduino CLI fallback or generic-family substitution in this campaign.
- Use all 12 logical cores within a controlled compile budget; retain at least 5 GiB free disk and cap campaign-owned growth at 6 GiB on the current host. Only owned scratch outputs may be cleaned.

## Review focus

- Native widget/WebView ownership during detach, focus/IME/clipboard and mixed-DPI resize must preserve editor models and terminal sessions.
- A renderer-provided path, owner, revision or port must never grant native authority.
- Unexpected package/catalog identity or ambiguous board matching must produce an explicit unresolved/skip result, never a substitute board or a pass.
- Interruption/storage pressure must leave user state untouched and preserve resumable receipts; incomplete work must not appear as a successful compile.
- Release packaging must reject an application that starts Python to provide GUI/backend behavior, while allowing explicit firmware tool subprocesses.

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

## Task 3: Finish Rust application service ownership

**Files:** Existing `desktop/crates/core`, `desktop/crates/host`, `desktop/crates/contracts`; service-specific native integration and differential verifiers.

**Interfaces:** Trusted native window/service registry owns workspace revisions, leases and process/device observations. Existing pure ports and metadata-only identity APIs keep their current scope until explicit transactional write/recovery tests pass.

- [x] Port the pure exact Onehorse/MiniMain recipe decisions at `69d845a`: seven new unit tests and 951 differential decisions (16 positive / 935 empty) plus 11 protocol checks. This subset performs no installed-metadata reading, Arduino Q hook selection or actual Rust compilation and does not complete BLD-01 or retire a service.
- [ ] Complete exact target/catalog and build-input policy, source-save transactions/identity races/recovery, coexistence leases and activation/release.
- [ ] Integrate Rust serial reader/Send worker and PTY/process ownership with finite deadlines and no replay/retry of hardware writes.
- [ ] Port settings, cloud/auth/vault, downloader/package/setup/repair and native maintenance orchestration under the 67-capability ledger.
- [ ] For each service: compare original Python outputs/failures, run native fixtures, integrate native UI, then classify/remove its Python application adapter. Keep tool-integrated Python only with an explicit tool classification.

## Task 4: Package and qualify the replacement

**Files:** Native launchers/build manifests/release verifiers, OS-specific CI and parity/removal ledgers.

- [ ] Enforce an allowlist of Python firmware-tool subprocess entry points and reject Python GUI/backend imports, servers or callbacks in replacement artifacts.
- [ ] Package native Windows and Ubuntu artifacts from their supported baselines; preserve offline tools, signatures/notices, icon integration and standard-user execution.
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

Current code is `6ef749a1f55841359fc380a572265c8323408a15`; its only change from
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
