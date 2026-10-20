# Native Rust GUI and Backend Implementation Plan

> **For agentic workers:** Continue the existing migration task by task with independent review where available. Preserve the parity ledger and exact verification receipts; completed prototype work must not be repeated or called production ready.

**Goal:** Replace the Python GUI and application backend with a native Rust widget application, retaining offline Monaco and xterm as embedded components and private Python only for approved firmware tools.

**Architecture:** A native Rust widget shell owns windows, focus, layout and commands. Existing GUI-independent Rust core/host services remain the foundation. Embedded editor/terminal components use a bounded typed bridge to Rust; they cannot own filesystem, serial, cloud, credentials or process authority. The Tauri web shell remains a tested prototype while native widget/WebView integration is qualified.

**Tech Stack:** Rust core/host/contracts; native widget toolkit selected after cross-platform embedding proof; existing Monaco/xterm assets; certified PlatformIO/private Python and native firmware utilities as subprocess tools.

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
- [ ] Run real builds with `--jobs 12`, retain logs/artifact hashes, remove only completed owned build outputs, and report every eligible target's outcome.
- [ ] Review actual application Compile integration and all relevant CI results; qualify current Python release independently of the incomplete Rust replacement.

## Task 2: Prove the native Rust widget shell and embedded components

**Files:** Create a separately gated `desktop/native-ui/` feasibility crate and verifier after toolkit selection; reuse `desktop/crates/{core,host,contracts}` and existing staged assets.

**Interfaces:** Native window supplies trusted workspace/window identity; editor/terminal supply only typed requests and consumed-byte acknowledgements. No fixture may activate live workspaces or devices.

- [ ] Compare official toolkit support for actual native controls and child WebViews on all required hosts. Record dependency/bundle costs and Windows/Ubuntu/ARM64 constraints before selecting.
- [ ] Implement native Controls, source tabs and split panes around offline Monaco/xterm fixture views, preserving the established visual assets.
- [ ] Verify resize/focus/tab order/IME/clipboard at native 100–200%, editor-only detach/reattach, stale response refusal and bounded stream backpressure on each host.
- [ ] Preserve the Tauri prototype's reusable Rust service/contract proofs; retire only the superseded application web shell after equivalent native evidence exists.

## Task 3: Finish Rust application service ownership

**Files:** Existing `desktop/crates/core`, `desktop/crates/host`, `desktop/crates/contracts`; service-specific native integration and differential verifiers.

**Interfaces:** Trusted native window/service registry owns workspace revisions, leases and process/device observations. Existing pure ports and metadata-only identity APIs keep their current scope until explicit transactional write/recovery tests pass.

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

20 October 2026: both configured index sources produced 37 inventoried package
entries and 949 visible declarations. The refreshed registry has 1,654 boards.
The narrow Python board-ID fix passes 34 catalog and 16 target-resolution checks;
Rust syntax parity covers 13,462 comparisons while strict non-board fields stay
unchanged. Real board compilation remains in progress in storage-bounded MCU
batches. Unresolved declarations remain explicit, rather than accepted aliases.

The Linux native-widget fixture passes six Rust protocol/path checks, five
controller lifecycle checks, three output/path checks and 17 native X11 smoke
checks. It retains the editor model and terminal through three editor-only
detach/reattach cycles. Those results qualify only the isolated Linux fixture,
not the remaining native UI/input/backend work above. CI wiring now requests
Linux AMD64/ARM64 execution and portable Windows protocol checks; configured
jobs are not recorded as successful until their actual results arrive.

Campaign review reproduced and corrected shared-identity false matches,
surviving child-process cleanup and scratch-ancestor symlink acceptance. Thirteen
offline guard checks pass. The audit runner refuses non-Linux hosts pending a
separate qualified native process implementation; this does not change the
production application's Windows support.

The user explicitly authorized continuation. Continue the campaign and independent backend work while native GUI feasibility is evaluated; no new approval checkpoint is required for reversible implementation within these boundaries. Final deployment remains a separate concrete release decision.
