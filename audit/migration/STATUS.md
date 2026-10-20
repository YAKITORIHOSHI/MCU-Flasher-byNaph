# Migration status

Read [CURRENT.md](CURRENT.md) first for continuation; consult targeted historical
reports below only as needed.

Updated: 20 October 2026, Asia/Manila. Latest recorded committed checkpoint:
`6ef749a1f55841359fc380a572265c8323408a15` on `codex/rust-tauri-migration`.
Only the “Select serial port” caption differs from CI-tested `0e0db8a`, with
strict all-target MSVC Clippy/fmt checks and no popup Escape change.
Its one-job [Windows run 39426407678](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39426407678)
passes at exact `0e0db8a` for the combined prefix/picker/icon correction. The four
unchanged successful jobs retain their prior `297cae7` results.
That checkpoint's [migration run 39421097096](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39421097096)
finished with four of five jobs passed. The new Windows workspace smoke failed
before assertions at trusted-root canonical/plain path spelling; its fix passes
three local reader checks and GTK 35 GUI, then passes the focused Windows rerun.
The earlier focused
[migration run 39392866712](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39392866712)
passes all five jobs at `e334d7b`; the later shared reader and color changes
are outside that completed receipt.
[Prior Rust/Tauri checkpoint CI](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39253280765).

**The full native Rust GUI/backend replacement remains unfinished. The Python application
remains the production release and behavioral reference.** No Python application
service has been retired; live prototype project/settings/cloud/hardware writes
remain gated. The 67-capability ledger now contains **14 partial fixture subsets
and 53 pending capabilities**, with no retirement approval.

## Production Ubuntu icon follow-up

`0cc3ec5` registers the existing app artwork in the desktop account's XDG icon
theme when shortcuts are explicitly generated, uses that icon for both desktop
entries, and gives Qt the matching desktop identity and measured X11 class.
All three shipped executable launchers and native C source remain byte-identical.
No runtime reset or native rebuild is needed for this icon/Python update.

Local Ubuntu 26.04 AMD64 verification passes 46 launcher checks, 46 runtime checks
and 21 platform checks (two Windows-only checks skipped). Two desktop entries
validate; an isolated native Xvfb/xcb window resolves the installed icon and
exposes the expected WM_CLASS and application ID. These fixtures never install
into the live account or qualify actual GNOME shortcut trust/rendering. Historical
CI below retains its exact prior revisions. See [icon receipt](ubuntu-launcher-icons.json).
All 25 jobs for that icon checkpoint subsequently passed: compatibility
`39261359647`, native ARM64 `39261359788`, migration `39261359735` and layout
`39261359712`. These are exact-source hosted fixtures, not physical GNOME/USB proof.

## Verified current Python candidate

The [candidate receipt](python-release-candidate.json) records the single ZIP from
source `fa02376`: 18,142 files verified, including 17,496 private Windows runtime
files and seven hydrated installer receipts. Archive SHA256 is
`abe8e2ca894a4ff7e70a46deca18705c2a319d7fc61e7778a6d5d2f7ad3c2652`.
The Python package intentionally omits developer `desktop/` and `audit/` trees;
these remain in the repository. About 10.35 GiB remains free after packaging.
The candidate has not been installed or published; desktop/device/installation
qualification and the full Rust replacement remain open.

## Current native Rust and compile continuation

The final architecture is **native Rust widgets and Rust application backend**,
with offline Monaco/xterm embedded and Python only for approved firmware tools.
The older Tauri web shell remains a reference fixture. See the
[native Rust plan](NATIVE_RUST_PLAN.md), [native widget receipt](native-rust-ui.json)
and [compile report](board-compile-campaign.json).
The [toolkit decision](../../docs/native-rust-widget-decision.md) records the
provisional GTK3/Win32 direction, official alternatives and dependency gates.

`e334d7b` adds the original three-theme design to the native
workspace: 39 shared palette tokens / 117 exact color values, Montserrat,
native toolbar/Controls, source tabs and all seven tool tabs. GTK reuses the
original action SVGs; Win32 reproduces native brand paths and still uses text
action buttons. Its action/tool-tab icon parity remains unfinished. Monaco and
xterm remain retained embedded components. These controls are
workspace implementation, while unavailable application actions remain disabled.

`297cae7` shares `workspace_project.rs` / `mcu_core::ProjectService` across GTK
and Win32 to read root sources from an explicit `--project` folder on a worker.
Windows now accepts `--project`, `--theme` and `--workspace-smoke`. Exact ordered
acknowledged IDs bind source controls, and only known IDs/newer revisions update
dirty labels. The GTK native Project picker starts an independent
workspace process and preserves the original window's buffers. Source selection,
dirty markers and three-theme updates use retained Monaco models. No source
save, live ownership activation/registration, settings/cloud/hardware write or
build/package service is enabled. Manual backend/fixture actions remain disabled.
`0e0db8a` implements the native Windows Project picker and owned small/large
HICONs in source. Cancel creates no child; accepted native paths/current themes
go as literal OS arguments to the current Rust executable on a worker, capped
at 16 independent children with parent buffers retained. Original ICO parsing
is bounded/shared with three pure tests; aligned `Vec<u32>` storage preserves
exact payload bytes/length for the native decoder. The alignment P2 is fixed
and scoped re-review is clean (`scratch/native-ui/windows-picker-icon-review.md`).
GTK main and detached
windows now reuse the existing `mcu_icon.png` native icon; no installer, launcher
or runtime entry changed.

Current local verification passes **35 GTK GUI smoke checks** (18 workspace and
17 editor-only detach), **18 local Linux native-unit checks**, **five controller
lifecycle checks** and **three path checks**. Three new Windows-only source/ack
unit checks now pass in Windows CI. Its old 17-check smoke and policy checks
also pass, but the new 18-check workspace fails before assertions at trusted-root
path spelling at `297cae7`. The corrected `0e0db8a` Windows run now passes all
35 GUI checks, 16 library/seven Windows binary units and recipe 951 + 11 protocol.
The workspace smoke owns its two-file project and checks model identity, dirty content,
selection, all three themes, undo/redo, native-parented source controls and
unchanged source bytes. Five reviewed GTK captures are retained under ignored
`captures/native-workspace/`: dark/light/Solarized at 1100×740, compact 960×540
and a `GDK_SCALE=2` window measured at 1792×1062 on a 1920×1080 display. Native
reading spans and active controls remain visible, normal close completes and
explicit project source bytes stay unchanged. Visual review also corrected GTK
tool-tab contrast. These are local geometry/appearance checks. Hosted CI at
`e334d7b` now passes 17 native Windows Server checks and 35 GTK checks on each
Ubuntu AMD64/ARM64 host, including the Win32 short-height/DPI corrections.
Later logo-ink (`#56cfbf`) and final Win32 glass `TEXT_BRIGHT` action-ink
corrections pass local strict Linux/MSVC-target Clippy only; no fresh CI receipt
covers the later prefix fix. `297cae7` CI passes both Ubuntu jobs, including GTK
35 GUI, the wide-tab regression and recipe checks. Its Windows source-state
units and old smoke pass; the workspace fails because canonical `\\?\D:` fixture
spelling differs from the plain trusted app root. The shared fix recognizes both
trusted-root spellings while preserving the literal selected path; three local
reader checks and GTK 35 GUI pass; the focused Windows rerun now passes at
`0e0db8a`, including both Unicode/space source files. An additional real GTK 40-tab regression went from a 6412-pixel
top-level window to `window=960 row=6412 retained_tabs=40`; a 42-source 960×540
capture is retained under ignored `captures/native-workspace-many-sources/`.
Scoped follow-up review found no new actionable issue after the source-overflow
and manual-Windows-fixture-action corrections; see
`scratch/native-ui/windows-reader-review.md`.
GTK `_NET_WM_ICON` is confirmed as 256×256 in all five captures. Windows native
Project picker/HICON, cancel/Unicode UTF-16 arguments and deliberately unaligned
payload units pass on native Windows, including real owned small/large HICONs.
Receipts: `scratch/native-project-ci-0e0db8a/migration-prototype-windows-2022/audit/migration/logs/`.
Actual picker interaction,
child survival, Windows 10/11 input and PE Explorer icon/packaging remain gates.
No new Cargo dependency/lockfile change or installer/runtime entry is involved;
only minimal existing Windows COM/Shell features were added. Exact
design parity still needs Settings, menus, auxiliary
screens, tool-tab icons and live toolbar/service behavior. See
[native preview and commands](../../desktop/native-ui/README.md).

The original Windows layout follow-up passes both jobs at `7733ac9` in run
`39388295330`.
The original runtime run `39390146279` passes Python 3.11; Python 3.14 exposed
an AVR fixture environment-wrapper failure. `60e4bc4` preserves the inherited
environment; runtime rerun `39392313449` passes both Windows jobs at that source,
including actual AVR Uno/Nano/Mega Compile and native GCC path fixtures. At `cd2fdf5`, the
five valid EXE argument cases and final VBS-to-Python five valid cases pass on
both versions;
the separate embedded-quote filename diagnostic is not a valid filename case.
These reference checks do not qualify the native workspace, and no
shipped executable rebuild is needed for the fixture correction.

`4b42146` adds real GTK controls around separate Monaco/xterm views. All five
migration jobs pass; Linux AMD64/ARM64 each pass 17 native widget checks, six Rust
checks, five lifecycle and three path checks. Windows at that checkpoint runs
only five portable checks. The native editor alone moves through three detach/
reattach cycles while its dirty model, selection and undo/redo remain intact.

`7da4be0` adds an authentic Win32/WebView2 adapter and one additional fixed-origin
protocol check (seven Linux/six Windows). Cross-target strict Clippy passes.
All 25 jobs pass at `7da4be0` across migration, compatibility, ARM64 and layout.
SDK proof correction `77e5ccb` also passes all five migration jobs in
[run 39357769519](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39357769519):
Windows Win32 and both Linux GTK hosts each execute 17 native smoke checks.
The UNO Q hook checkpoint `f249c21` passes all five focused migration jobs in
[run 39359319887](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39359319887).
The subsequent metadata-reader/release-selection checkpoint `8dbdb9d` also passes
all five jobs in [run 39360040590](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39360040590). Exact source/job receipts remain in ci-results.json;
pending work is not a pass. Native input, IME, clipboard, accessibility,
mixed monitors and complete workspace layout
remain gates on every required desktop.

The current Python application accepts legitimate plus-sign board IDs without
relaxing platform/framework/protocol validation. A complete registry query has
1,654 boards; 34 catalog checks and 16 target-resolution checks pass. Default
Rust services now pass 126 tests (119 existing + seven recipe-policy tests).
At `69d845a`, the pure exact Onehorse/MiniMain decision port matches 951
differential decisions (16 positive / 935 empty), plus 11 separate protocol/
boundary checks. The bounded independent review is clean in
`scratch/native-ui/recipe-policy-review.md`. This policy does not read trusted
installed metadata, select the Arduino Q hook or compile firmware through Rust;
full BLD-01 stays pending. The narrow ID predicate has 13,462 differential
comparisons; transport has 53,685 comparisons and 163 structural refusals.
These do not qualify the full target validator or live build service.

All configured indexes yield 37 inventoried package entries (36 unique releases),
949 visible declaration rows and 543 distinct declarations. Exact ID/name and
single-candidate matching selects 318 PlatformIO/Arduino targets, including one
explicitly experimental community UNO Q release. There are 213 distinct
skipped declarations: 180 unresolved and 33 ambiguous. They remain skips, not substitute boards or
proof of target absence. Historical aliases and storage/interrupted attempts
remain separately retained.

The real compile campaign is complete: **318/318 targets passed**, with firmware
artifact hashes retained in board-compile-campaign.json. The UNO Q path-with-spaces
blocker is closed at `f249c21`: a fresh isolated original-path campaign job
retains its preparation failure and successful build-only compatibility retry.
The actual linker, strip and pack steps produce the 5,376-byte ELF/LLEXT and
packed sketch; resident firmware is still required. Its earlier no-space alias
pass is historical evidence only. See [full compile report](BOARD_COMPILE_REPORT.md).

The shared application/campaign compatibility selector pins exact board,
platform/builder and framework identities. It selects the Onehorse DIO boot/SDK
and MiniMain S2 native USB repairs, plus the Linux-only UNO Q SCons post hook
`src/modules/arduinoq_link_paths.py`. The hook represents only the two known
`-T` linker-script operands as SCons File nodes; its digest enters generated
INI inputs so changed hook bytes invalidate reuse. Installed packages are never
patched. This Python hook belongs to the firmware tool, while its application
selection/ownership policy still needs a Rust port.

At `f249c21`, fifteen local recipe checks pass; Windows skips three Linux-only
UNO Q cases as expected. `8dbdb9d` then reproduces and closes a FIFO metadata
read stall with bounded reads from verified regular-file descriptors. Its sixteen
local recipe checks pass, with four Linux-only cases expected to skip on Windows.
Five release source-selection checks pass: Python release packages omit the
developer-only `desktop/` and `audit/` trees while retaining the strict shipped
state guard. These checks do not represent a new 318-target compile rerun or
an installed release.

Thirteen campaign guard checks and 46 runtime checks plus Qt/Monaco recovery/Pull
pass. The responsive suite passes 20 checks and 135 terminal
layout cases. Original failed compiles remain retained. Current Python build
qualification passes; unrestricted deployment remains false pending user desktop,
device/upload/reset/USB and installation qualification. The complete Rust
replacement remains unfinished.

The campaign uses twelve compiler jobs, preserves 5 GiB free space and removes
only owned scratch packages/build outputs. Every owned campaign work/package
tree is now cleaned, leaving approximately 10.9 GiB free at the recorded cleanup.
No serial port, upload/reset, live
settings, sketch or protected cache/journal was used. Compile-only coverage does
not qualify full release workflows or the unfinished Rust replacement.


## Implemented in this continuation

- Native root/source identity adapter retains metadata-only handles: Linux
  `O_PATH`, Windows `FILE_READ_ATTRIBUTES` plus full 128-bit `FILE_ID_INFO`.
  Original objects survive observed pathname replacement; in-place content edits
  deliberately retain identity. Portable Windows device-name validation is
  shared by core project paths and the adapter, including COM/LPT superscript
  digits and spaces before extensions. Symlinks/reparses, unsafe namespaces,
  wrong kinds and uncertain filesystem identity are refused.
- The cross-language controller exercises fixed commands, command/byte/deadline
  budgets, missing/changed roots, hard links, aliases and independent native
  identity comparisons. Linux passes 31 checks; the native Windows Server job at
  `1131c7e` passes 30 (Windows junctions replace Linux symlinks; FIFO is Linux-only).
  The Windows fixture moves the retained source out before renaming its directory:
  descendant handles can prevent the directory move. This is not a portable
  rename-permission claim. Deadline timing is independent of late controller
  scheduling; parent cleanup also bounds pipes and termination.
- Pure serial Send policy preserves 64 KiB/message, 256 KiB active-plus-pending,
  16 messages, captured generation/connection/port/baud, compile-time allowance,
  startup rollback, discard, permanent Stop and exact completion IDs. Partial or
  failed writes produce one captured-connection close effect and no retry.
  Stale failures cannot disconnect a replacement. Responses serialize event by
  event into a capped 16 MiB buffer; amplified snapshots cannot build a larger
  aggregate or publish a partial response. No serial driver or worker/UI
  integration is enabled.
- Existing xterm JavaScript exactly matches integrity-checked npm `xterm@5.3.0`
  and `xterm-addon-fit@0.8.0`. Their matching licenses and original CSS copyright
  block are recovered and staged. Existing CSS differs only in its pre-existing
  jslinux description/URL sentence. The original 22 asset sources still produce
  25 identical copies; three notices bring the staged total to 28. No original
  resource was rewritten. Monaco/codicon/full dependency provenance remain gates.

## Verified local scope

At `1131c7e`, Ubuntu 26.04 AMD64 passes the complete hardware-free
`desktop/scripts/verify-local.sh` under an owned checkout-local Xvfb:

- **117 Rust tests, 40 frontend tests, 46 native webview checks**, strict Clippy,
  Python warnings-as-errors, typecheck/build and controlled native PTY.
- The existing **23,909 pure differential inputs** remain separately counted:
  serial text 325, preferences 1,335, scaffold 16, budgets 8,960, target transport 13,039,
  ownership 234. Transport has 51,993 equivalent comparisons and 163 typed refusals.
- New serial Send: **401 trace inputs / 7,411 action comparisons**, plus **27
  explicit typed/protocol refusals**. Twelve Rust policy tests cover completion
  identity, stale outcomes and exhausted generations. These trace/action counts
  are not added to the older independent input-case count.
- Native file identity 31, OS-lock interop 8, process identity 6, registration
  interop 9 and registry Rust 21. All filesystem mutations use disposable scratch.
- **7 offline asset-provenance checks** validate matching artifact/notice hashes
  and all 28 staged files. A missing staged license was separately refused before
  exact restoration. No verifier downloads assets at runtime.
- Windows MSVC-target strict Clippy/checks passed on Linux. This is compilation
  evidence only. The prior `2e09d07` native Windows Server job also passes native
  identity/Send/webview execution; final-checkpoint CI is recorded separately.

Strict code/Python warnings passed. Native virtual-display execution emitted
GNOME/GTK portal, denied gvfs mount and closed D-Bus/WebKit name-release diagnostics;
assertions and exit status passed without suppressing failures. This is native
fixture/layout/IPC evidence, not physical GPU, input, performance or USB proof.

Before the subsequent verifier correction, the unchanged legacy reference was freshly rerun: runtime 46 plus real Qt/Monaco
keyboard/tab, three detach/recovery cycles and full cloud-buffer replacement smoke
pass. Offscreen Qt emitted native-feature/Vulkan diagnostics. Earlier editor
12/12 at 100%/200%, downloader appearance 17/performance 7, layout and other results
remain historical evidence at their stated revisions; they were not all rerun.
Native NVIDIA 200% and full keyboard/IME/clipboard/monitor qualification remain open.

See [verification](verification.json), [identity](file-identity.json),
[Send comparison](serial-send-parity.json), [provenance](xterm-provenance.json),
[native webview](native-webview.json), [exact CI results](ci-results.json),
and [source preservation](source-preservation.json).

## Native CI boundary

All completed job results retain their actual tested source revision:

| Matrix | Source | Passed jobs |
| --- | --- | --- |
| Rust/Tauri native prototype and Windows editor binding ranges | `1131c7e` | 5/5 |
| Original application compatibility, including native Ubuntu ESP32 Compile | `f952325` | 14/14 |
| Native workspace/downloader/auxiliary layout | `ece6339` | 4/4 |
| Fresh Ubuntu 24.04/26.04 ARM64 setup, GUI, ESP32 Compile and runtime | `f952325` | 2/2 |

The later reference-verifier correction changes no Rust/Tauri or production UI
source. Checked Git path equivalence and exact job/artifact links are retained
in ci-results.json. The first `9b5d640` Windows attempt and the `ece6339`
compiler-layout failures remain in the history alongside their corrections.
Earlier `4cf730b` is also retained as historical evidence; its results are never
silently promoted to another revision.

Each final native Tauri host passes 46 webview checks and 401 Send traces/7,411
comparisons plus 27 refusals; Linux identity 31 and Windows identity 30 are separate.
Hosted Windows Server is not Windows 10/11 desktop proof. No replacement session
has qualified physical Pi 5/USB, Windows 10/11 desktop USB or Ubuntu AMD64 hardware.

## Fresh native setup finding

At ece6339, both fresh Ubuntu ARM64 jobs passed cold/warm workspace and readiness
but stopped before firmware compilation: the verifier required the absent legacy
ESP32 compiler path. Logs and the upstream 8.0.0 manifest show the unified
`toolchain-xtensa-esp-elf` package. This was not proof of a wrong-CPU payload.

`f952325` adds a read-only verifier helper for declared installed legacy/unified
layouts, package identity, path containment and native ELF64 CPU. It retains the
real Compile-button, firmware receipt and private-runtime assertions. Nine
synthetic checks and the 16 existing target-resolution checks pass with warnings
as errors. All 14 compatibility jobs and both fresh LTS ARM64 workflows pass
at this exact correction. Each ARM64 first-run pipeline produced 291,424 firmware
bytes after native compiler checks, actual cold/warm GUI launch and Compile,
then passed private-runtime verification. This qualifies the reference fixture;
the Rust build service and physical USB remain unfinished. See
[native compiler evidence](native-compiler-layout.json),
[Ubuntu 24.04 ARM64 receipt](hosts/native-ubuntu-24.04-arm-first-run-f952325.json),
[Ubuntu 26.04 ARM64 receipt](hosts/native-ubuntu-26.04-arm-first-run-f952325.json)
and ci-results.json.

## Remaining release gates

1. Native handle-relative source/directory authority and final replacement races,
   external writers, ACL/xattr preservation and durable interrupted-save recovery.
   Metadata identities alone do not grant content/write capabilities or a lease.
2. Coordinated legacy/new activation/release/save leases, initially one independent
   workspace per PID; common package-event/lease roots across shared stores.
3. Complete settings import/rollback and service/event/cancellation contracts;
   project selector/Modify/recovery/AI/search/syntax and editor-only detach/input.
4. Dynamic boards/frameworks/programmers, installed-only offline tools, exact-byte
   build receipts, serial worker/UI, compile/upload/reset and real failure paths.
5. Full interactive PTY input/paste/capabilities/backpressure, downloader/package
   jobs, setup/repair/maintenance and auxiliary surfaces with preserved workflow.
6. Fixed-provider Firebase account/schema/vault/conflict/Pull rollback; deployed
   rules/API restrictions must be verified separately. Client config is not secret.
7. Complete asset/package notices, installers/signing/upgrade/rollback and native
   desktop/device sessions for every supported OS/architecture/workflow.

The initial boards never narrow final supported feature/target coverage. Consult
[the parity ledger](parity.md) for all 67 acceptance/failure contracts.

## Preserved boundaries

Inventory SHA-256 remains
`70072652a96ad7a51f7134d7f10ee3ff3314947a1816842d9ef2bbe60d6e8d52`.
Baseline hashes were not regenerated. The 23 unique reference amendments now
explain eight production sources, twelve verifiers and three legacy CI files
separately; 280/300 source rows, 129/133 resource rows and 111/123 verifier rows
retain original hashes. All 11 continuation inputs and the four launcher
executable/source hashes remain unchanged. The added
production PNG reuses exact existing ICO bytes. All 22 reused
asset sources, restored root launcher and unrelated catalog/bootstrap/installer
inputs retain their continuation-start hashes. No live sketches, protected
caches/journals, settings, credentials, cloud or hardware were used by fixtures.
Read [HANDOFF.md](HANDOFF.md) for continuation commands and exact next gates.
