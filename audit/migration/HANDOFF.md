# Restart handoff

Read [CURRENT.md](CURRENT.md) first; open only targeted reports needed for the
current slice. This file preserves the longer historical handoff.

Updated: 20 October 2026, Asia/Manila.

## Objective and checkpoint

Continue the full, reuse-first native Rust GUI and Rust backend migration with actual code and bounded
subagent ownership. The user reiterated “Continue all and everything according to
plan.” Full current feature/board/framework/programmer parity is required before
Python is replaced; private Python ultimately remains only for approved firmware
tools. Support Ubuntu 24.04+ AMD64/ARM64 and Windows 10/11 AMD64. Preserve current
compact workflow/static glass, offline Monaco/xterm, fixed Firebase deployment,
prepared offline tools, independent projects and editor/terminal/AI state.

The earlier native-UI caption checkpoint is
`6ef749a1f55841359fc380a572265c8323408a15` on
`codex/rust-tauri-migration`. Only the “Select serial port” caption differs from
tested `0e0db8a`; strict all-target MSVC Clippy/fmt pass and no Escape behavior
change remains. One-job Windows CI `39426407678` passes at exact `0e0db8a`
for the combined prefix/picker/icon checkpoint. The four unchanged successful
jobs retain their prior source. Earlier `297cae7` CI `39421097096` finishes 4/5: both Ubuntu and both
Windows editor jobs pass; the Windows native job passes old 17/source-state/
recipe checks but fails the new workspace at trusted-root path spelling before
assertions. The prefix fix passes three local reader checks and GTK 35 GUI;
the corrected Windows rerun now passes. Prior `e334d7b` CI `39392866712` passes all five jobs.
Documentation receipts may
advance HEAD without changing tested source. Original reference is
`1d00ff0bfdac7d158dde3df84b07741c5b1df6d4`. Python remains production; no service
is retired. **This is still an incomplete fixture prototype, not a public release.**

After CURRENT.md, consult only the relevant status/parity/verification/reuse
report for the change being made; do not reload every historical JSON by default.
The 67-capability ledger now has 14 partial fixture subsets and 53 pending. There
is no retirement approval. Do not restart, reduce scope to implemented probes or
physical boards, regenerate baseline hashes, or publish an incomplete replacement.

## Current continuation

Latest source is `cc6a6d7f5c482def023976f32e3759dfba1a4415`; documentation commits may advance HEAD. Codex Security and Firebase tools are installed and available; no scan or cloud action was started.

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


Read NATIVE_RUST_PLAN.md, native-rust-ui.json and board-compile-campaign.json first.
Final GUI uses native Rust widgets; retain Monaco/xterm as embedded islands.
All application backend moves to Rust; private Python remains only firmware tools.
Do not reinterpret the Tauri web shell as fulfilling this requirement.
The [toolkit decision](../../docs/native-rust-widget-decision.md) provisionally
retains GTK3/Win32 with Wry. Official alternatives and dependency classes are
recorded; measured costs, native input and packaging remain qualification gates.

The `e334d7b` `desktop/native-ui/` checkpoint implements native GTK/Win32
toolbar, Controls, source tabs and all seven tool tabs around retained
Monaco/xterm islands. `workspace_design.rs` shares 39 original palette tokens
across three themes (117 exact color values). Both adapters reuse original
Montserrat; GTK loads original action SVGs, while Win32 reproduces native brand
paths and still uses text action buttons. Win32 action/tool-tab icons remain
unfinished. Retain these assets/design choices; auxiliary surfaces and saved
preferences still require implementation and qualification.

`297cae7` shares `workspace_project.rs` across GTK/Win32, reading explicit
root-source manifests through `mcu_core::ProjectService` on a worker. Both hosts
accept `--project FOLDER`, `--theme` and `--workspace-smoke`. Exact ordered
acknowledgements bind native controls; dirty updates require known IDs and newer
revisions. GTK's native Project picker opens an independent
process instead of replacing the current buffers. Native source tabs/dirty
markers and theme updates operate on retained Monaco models. This is read-only
filesystem integration: no save, metadata generation, live registration or
activation, settings/cloud/device write or compile/package service is enabled.
Unavailable manual backend/fixture actions stay disabled. Windows native
Project picker/HICON are implemented and pass native units at `0e0db8a`;
GTK main/detached windows use the original
`mcu_icon.png` and expose actual 256×256 `_NET_WM_ICON` in all five captures.

Current Linux checks pass: 18 native units, three focused reader checks
after the prefix fix, 18 GTK workspace smoke and 17 editor-only detach smoke
checks (35 GUI checks total), five controller lifecycle and three path checks.
The workspace smoke owns its two-file source project and verifies retained model
identity, dirty content/selection, themes, undo/redo, native-parented source
controls and unchanged source bytes. It is not a save/ownership/device proof.
Five reviewed captures live under ignored
`audit/migration/captures/native-workspace/`: dark/light/Solarized at 1100×740,
compact 960×540 and `GDK_SCALE=2` measured at 1792×1062 on a 1920×1080 display.
The receipt records visible native reading spans/active controls, normal close
and unchanged explicit-source bytes. Visual review corrected GTK tool-tab
contrast. Hosted CI now verifies the `e334d7b` styled adapters and Win32
short-height/DPI corrections: 17 native Windows Server checks and 35 GTK checks
on each Ubuntu AMD64/ARM64 host. Later GTK/Win32 logo ink (`#56cfbf`) and final
Win32 glass `TEXT_BRIGHT` action-ink corrections pass local strict Linux and
MSVC-target Clippy; they are now included in `297cae7`, whose Ubuntu jobs pass.
The Windows new workspace fails at canonical/plain trusted-root spelling before
assertions; the shared prefix fix preserves the literal selected project path
and subsequently passes the Windows rerun. Three Windows source-state units, old 17 smoke and
951 recipe / 11 protocol checks pass. An extra real GTK 40-tab check went from
RED window 6412 px to GREEN window 960 / row 6412 / all 40 retained; the separate
42-source compact capture is 960×540. The scoped review in
`scratch/native-ui/windows-reader-review.md` is clean after the overflow and
manual-action fixes. Exact Settings, menus, auxiliary screens, tool-tab
icons and live toolbar/service design parity remain unfinished. Hosted Windows
Server results do not qualify actual Windows 10/11 desktops or physical USB.

The new Windows picker launches the current Rust executable off-thread with
literal native path/current-theme arguments, cancel/no-spawn, a 16-child cap and
retained parent buffers. Small/large owned HICONs use the original ICO through a
bounded shared parser (three pure tests) and DWORD-aligned `Vec<u32>` storage,
preserving exact payload bytes/length. Scoped re-review is clean after fixing
the alignment P2; see `scratch/native-ui/windows-picker-icon-review.md`.
Windows CI `39426407678` passes 16 library/seven Windows binary units, 17/17
detach, 18/18 workspace and recipe 951 + 11 protocol checks. Cancel/Unicode
UTF-16 args, real small/large owned HICON and deliberately unaligned-buffer units
pass, as does the two-source Unicode/space fixture. Receipts are under
`scratch/native-project-ci-0e0db8a/migration-prototype-windows-2022/audit/migration/logs/`.
Actual chooser/child survival, Windows 10/11 input/IME, PE Explorer
icon and packaging remain gates. No new Cargo dependency/lockfile or runtime/
installer entry changed; only minimal existing Windows COM/Shell features.

Preview on a real Ubuntu display:

```sh
source desktop/dev-env.sh
CARGO_BUILD_JOBS=2 cargo build --offline --manifest-path desktop/Cargo.toml -p mcu-native-ui-probe
desktop/target/debug/mcu-native-ui-probe --theme light --project "/explicit/source-folder"
```

Run owned verification separately:

```sh
.venv-linux/bin/python3 desktop/native-ui/verify-smoke.py --workspace
.venv-linux/bin/python3 desktop/native-ui/verify-smoke.py
```

The first command writes `scratch/native-workspace/latest-smoke.json`; the
second retains `scratch/native-ui/latest-smoke.json`. Both are developer
controllers, not Python application services. A manual preview may leave empty
owned profile directories recreated by WebKit helpers after Rust exits; do not
claim zero residuals. The controller bounds and stops captured descendants.
See `desktop/native-ui/README.md` for host-specific flags and remaining gates.

Code 4b42146: Linux GTK/native owner/editor-only reparent fixture, board-ID fix,
configured-index inventory/exact mapping and storage-bounded compile helpers.
Code 7da4be0: Win32 controls/WebView2 adapter plus exact Onehorse/MiniMain recipe
repairs shared by the current Python application and campaign. All 25 CI jobs
pass at `7da4be0`; SDK-specific correction `77e5ccb` passes all five migration
jobs in run 39357769519. Windows Win32 and Linux GTK AMD64/ARM64 each execute
17 native smoke checks. Hosted results do not qualify Windows 10/11 desktop
input or physical USB.

Code `f249c21` closes UNO Q's linker-path defect in the shared production
compatibility path. Fifteen local recipe checks pass; Windows deliberately skips
three Linux-only checks. All five focused migration jobs pass in 39359319887.
The subsequent `8dbdb9d` reproduces and fixes a FIFO metadata-reader stall using
bounded regular-file descriptor reads, then passes sixteen local recipe checks.
Its new FIFO case makes four expected Linux-only skips on Windows. Five release
source-selection checks pass: current Python release packaging omits the
developer `desktop/` and `audit/` roots while keeping its strict shipped state
guard. This does not install a release or rerun all 318 target compiles. Focused
CI for this newer checkpoint passes all five jobs in run 39360040590; actual
receipts confirm 16 recipe checks (four expected Windows skips) and 17 native
widget checks on each host. Thirteen campaign guard checks, 46 runtime checks and
20 responsive checks pass at their recorded source.

The campaign is complete: 318/318 exact targets from all configured indexes
produced real artifacts. Every final outcome and skip belongs to
board-compile-campaign.json and BOARD_COMPILE_REPORT.md. Original failures remain.
There are 213 distinct skipped declarations: 180 unresolved and 33 ambiguous.
Do not restart completed targets or silently accept old aliases. UNO Q's latest
receipt is `scratch/board-campaign-20261019/unoq-app-hook-batch/results.jsonl`:
a fresh owned job in the original checkout path containing spaces retains its
preparation failure and successful real compatibility retry. The former no-space
Bubblewrap alias pass is historical only. The Linux-only tool hook
`src/modules/arduinoq_link_paths.py` pins platform/board/builder hashes and
framework identity, then changes only two `-T` operands to SCons File nodes.
Its digest is a generated INI input; installed packages remain untouched. The
5,376-byte ELF/LLEXT/packed sketch requires resident firmware and is not a
standalone device image. All owned campaign work/package stores are cleaned;
approximately 10.9 GiB was free at cleanup.

The current Python release passes build qualification. Unrestricted deployment
remains false pending actual user desktop/device/upload/reset/USB and installation
qualification. No replacement entry point or shipped executable was installed.
Continue the native Rust/backend plan and release gates below.

All 11 continuation-start input hashes and all four launcher executable/source
hashes (three shipped executables and Ubuntu C source) remain unchanged. Original inventory hash remains
70072652...e8d52; it was not
regenerated. Current baseline preservation: sources 280/300, resources 129/133,
legacy verifiers 111/123. Twenty-three unique amended baseline paths are
documented: eight production sources, twelve legacy verifiers and three legacy
CI files.

The historical native fixture has Linux 7/Windows 6 protocol checks at its
recorded source. Current workspace counts are listed above. The default
Rust services pass 126 tests (119+7 pure recipe-policy tests), and the ledger stays 14 partial/53 pending with
no Python service retired. Actual CI and artifact receipts are in ci-results.json
and native-rust-ui.json; pending jobs are never promoted to successful evidence.

`69d845a` ports pure exact Onehorse/MiniMain recipe decisions: 951 differential
decisions (16 positive / 935 empty) plus 11 protocol checks. The bounded review
`scratch/native-ui/recipe-policy-review.md` is clean. This performs no installed-
metadata reading, Arduino Q hook selection or real Rust compile integration;
BLD-01 remains pending.

## Verified current Python candidate

The [candidate receipt](python-release-candidate.json) records the single ZIP from
source `fa02376`: 18,142 files verified, including 17,496 private Windows runtime
files and seven hydrated installer receipts. Archive SHA256 is
`abe8e2ca894a4ff7e70a46deca18705c2a319d7fc61e7778a6d5d2f7ad3c2652`.
The Python package intentionally omits developer `desktop/` and `audit/` trees;
these remain in the repository. About 10.35 GiB remains free after packaging.
The candidate has not been installed or published; desktop/device/installation
qualification and the full Rust replacement remain open.

## Code stages completed in this continuation

- `9b5d640`: metadata-only retained native root/source identity handles, full
  Windows FILE_ID_INFO, portable shared reserved-device stem policy, strict
  root/source kind/alias checks and bounded cross-language fixture protocol.
- `2e09d07`: pure bounded serial Send policy and original-Python deterministic
  trace harness; corrected native Windows junction separators and open-descendant
  directory rename fixture. Windows Server native job passes at this exact source.
- `0374725`: deadline fixture timing no longer assumes prompt ready delivery;
  documents that input timeouts alone cannot interrupt blocked stdout.
- `ece6339`: exact npm xterm 5.3.0 / fit 0.8.0 JavaScript matches, matching archive
  licenses and upstream CSS copyright block recovered; stager includes 3 notices
  and offline provenance verification. Original resources remain unchanged.
- `1131c7e`: Send probe serializes one event at a time into a capped 16 MiB
  response; an amplified-state negative check is refused without partial output.
  This developer-only follow-up is covered by an explicitly dispatched native
  migration matrix; all five jobs pass.
- `f952325`: corrected native ESP32 fixture checks for Espressif32 8 unified
  toolchains. Two original verifier files and the ARM64 test loop are amended;
  9 synthetic and 16 target-resolution checks pass. Native setup/compile reruns
  and compatibility all pass at that exact source. Rust/Tauri code remains 1131c7e.
- `0cc3ec5`: production Ubuntu shortcut icon/identity follow-up. Exact existing
  256px ICO artwork is registered under the desktop account's XDG hicolor theme;
  both shortcuts use the stable icon name, Qt declares the matching desktop ID
  and the measured X11 class is recorded. All three native launchers and C source
  are unchanged. Local checks pass: launcher 46, runtime 46, platform 21 with two
  Windows-only skips, desktop validation and native Xvfb theme/identity fixture.
  Actual GNOME rendering/trust and native Windows/ARM64 reruns are not claimed.
  Refresh actual shortcuts with `bash direct/ubuntu/run.sh --install-shortcut`;
  verification installed only into disposable fixtures. See ubuntu-launcher-icons.json.

The adapter has no public content/native-handle/write capability. Identity
observations do not close ancestor/final-replacement races, preserve ACL/xattrs,
provide crash recovery or authorize source writes. Windows may refuse directory
rename with open descendants: fixtures move the retained source out first. Do
not add delete/content capability just to force a test scenario.

Send is an I/O-free policy: 64KiB/message, 256KiB active-plus-pending, 16 messages;
nonblocking admission, captured native generation/token/port/baud, compile
allowance, startup rollback, discard, Stop, exact completion and no retry after
partial/error. A future native worker must own observations and finite driver
timeouts. No driver, hardware, serial UI or actual worker is integrated. Explicit
integer/UTF8 domain refusals are separate from equivalent comparisons.

All previously implemented project/scaffold/editor/registry/PTY/transfer behavior
remains. Registry unknown JSON stays opaque; never pass private fragments through
Value. Known timestamps use float_roundtrip. Only confirmed Exited frees a claim;
Unknown, including zombie/dead uncertainty, reserves it. One workspace per PID
is still the initial coexistence strategy. The whole-webview transfer remains
feasibility only; the separate native fixture proves editor-only detach. Full
native input, integrated project ownership and recovery remain unfinished.

## Verification

Latest full local `1131c7e`: 117 Rust tests, 40 frontend, 46 native webview;
strict Clippy/Python warnings, typecheck/build, fixed native PTY; 23,909 existing
pure inputs; 8 lock, 6 process, 9 registry interop; 31 Linux file-identity checks;
401 Send traces/7,411 action comparisons plus 27 typed/protocol refusals; 7 asset
provenance checks over 28 staged files (22 original sources / 25 copies  + 3 notices).
A missing staged license was separately refused then restored byte-for-byte.
Windows MSVC-target strict Clippy/check compilation passed on Linux.

Fresh unchanged legacy runtime 46 and real Qt/Monaco tabs/navigation,3 detach/
recovery cycles and complete cloud-buffer Pull smoke pass. Other legacy suites
remain historical at their recorded source/scales, including editor 12 per scale,
downloader appearance 17/performance 7 and the original 4cf matrix. NVIDIA 200%
rendering is still incomplete. Native virtual-display runs emit portal/gvfs/
closed-bus diagnostics; assertions pass. Do not call this GPU/performance/input
qualification or imply every old legacy suite was freshly rerun.

```sh
source desktop/dev-env.sh
bash desktop/scripts/verify-local.sh > audit/migration/logs/verification-latest.log 2>&1
```

Use an actual available display or an owned private Xvfb on Ubuntu. This
continuation's ignored helper is
`audit/migration/scratch/continuation-20261019/run-native-checks.py`; it allocates
-displayfd, uses process-local Mesa, bounds process lifetime and stops its own
server. Its `serial` mode runs the entire suite. Do not reuse stale PID JSON:
the old Xvfb PID 20458 was already an unrelated kernel worker and was not touched.
The helper itself is audit scratch, not a shipped runtime or global setup.

On Windows use verify-local.ps1. Reference Python 3.10+ requires psutil >=5.9,<8;
local .venv-linux is available. Pinned Rust 1.99.1, Node 24, pnpm 11.25.0 and owned
desktop/target remain selected by dev-env.sh. No system install/profile change.

## CI and native scope

Current continuation results retain their actual source:

- `7da4be0`: all 25 jobs pass across migration 39356653640, compatibility
  39356653665, ARM64 39356653643 and layout 39356653635.
- `77e5ccb`: all five migration jobs pass in 39357769519, including 17 native
  smoke checks on Windows and both Linux architectures.
- `f249c21`: all five focused migration jobs pass in 39359319887.
- `8dbdb9d`: bounded-reader/release-selection focused CI passes all five jobs in
  run 39360040590. Actual artifacts confirm 17 native widget checks per host and
  16 recipe checks (four expected Linux-only skips on Windows).
- `e334d7b`: focused migration run `39392866712` passes all five jobs for the
  styled GTK/Win32 workspace. Native Windows Server executes 17 checks and
  Ubuntu AMD64/ARM64 each execute 35 (18 workspace / 17 detach), including
  Windows short-height/DPI/readiness-status corrections. Later color and
  Windows project-reader work are outside this receipt.
- `297cae7`: migration `39421097096` finishes 4/5. Ubuntu AMD64/ARM64 pass 35
  GUI, the extra wide-tab regression and 951 recipe / 11 protocol checks. Both
  Windows original-editor jobs pass. Windows native old 17, three source-state
  units and recipe checks pass; new workspace fails before assertions at
  trusted-root canonical/plain spelling. The local reader-prefix fix and GTK
  35 pass; the corrected Windows rerun subsequently passes at `0e0db8a`.
- `0e0db8a`: one-job Windows run `39426407678` passes for the combined
  trusted-prefix repair, native Project picker and original-ICO/alignment source.
  Preserve the four prior successful jobs at their actual source; no new full
  five-job execution is claimed. Windows 16 library/seven binary units, 35 GUI
  and recipe 951 + 11 protocol pass at this exact source.
- Original Windows layout follow-up at `7733ac9`: both jobs pass in run
  `39388295330`.
  Original runtime `39390146279` passes Python 3.11; Python 3.14's AVR fixture
  failed because its environment wrapper lost inherited variables. `60e4bc4`
  corrects that fixture; rerun `39392313449` passes both Windows jobs at that
  source, including actual Uno/Nano/Mega AVR Compile and native GCC path
  fixtures. At `cd2fdf5`, five
  valid EXE cases and the final five valid VBS-to-Python argument cases pass on both Python
  versions. Keep the separate embedded-quote filename diagnostic separate from
  valid Windows paths. No executable rebuild is required for this verifier-only
  correction. These are original-application results, not new native UI proof.

Earlier successful matrices remain historical evidence at their actual source:

- Rust/Tauri `1131c7e`: migration/editor 39253280765, all 5 jobs.
- Reference-verifier correction `f952325`: compatibility 39254790860, all 14 jobs.
- Reference-verifier correction `f952325`: native ARM64 39254786572, both LTS jobs.
- Unchanged production UI at `ece6339`: native layouts 39251970888, all 4 jobs.

ci-results.json records checked source equivalence and preserves prior failures:
9b5d640 Windows fixture separator/open-descendant assumptions; ece6339 obsolete
compiler names. Source 1131c7e host artifacts are retained under hosts/ with 46
webview checks each, 31 Linux/30 Windows identity checks and 401 Send traces/7,411
comparisons plus 27 refusals. Both f952325 native ARM64 first-run receipts retain
cold/warm GUI, actual ESP32 Arduino Compile, 291424 firmware bytes and runtime.
This is native hosted fixture evidence; Windows Server is not Windows 10/11
physical desktop qualification, and hosted ARM64 is not a physical Pi/USB session.

## Current qualification follow-up

The broader ece6339 ARM64 workflow failed only at its obsolete compiler path
check after successful cold/warm GUI launch. Both native LTS jobs now pass the
f952325 fresh setup/real compile/runtime rerun; all 14 compatibility jobs pass.
`direct/native_esp32_fixture.py` checks declared installed known legacy/unified
packages without execution or installation, package name/type/path containment
and native ELF64 CPU. An old native package cannot mask a foreign new package.
Unknown layouts fail closed. Real compile/firmware/runtime checks remain mandatory.
Do not mistake the old “different CPU” diagnostic for actual foreign binary
evidence; the file was absent after Espressif32 8 renamed its package.

## Next required implementation gates

1. Native handle-relative directory/content authority and safe final replacement:
   Windows semantics, external writers, ACL/xattrs, durable staging/crash recovery.
   The completed metadata adapter is evidence only; keep live writes gated.
2. Coordinated legacy/new activation, source-save leases and rollback, initially
   one independent workspace per PID; shared package-store/event/lease namespaces.
3. Settings import/rollback, service/event/cancellation contracts; full selector/
   Modify/editor-only detach/recovery/AI/search/syntax and native input/monitor.
4. Interactive PTY input/paste/capabilities/backpressure; all dynamic exact targets,
   frameworks/programmers, installed-only tools, exact-byte build/cache receipts,
   serial worker/UI, compile/upload/reset and failure handling.
5. Downloader/package jobs, setup/repair/maintenance and auxiliary screens;
   fixed-provider Firebase/vault/account/schema/conflicts/Pull rollback. No
   editable provider settings, plaintext fallback or invented deployed-rule proof.
6. Monaco/transitive bundle and codicon artifact provenance/notices, dependency/
   tool/package notices, installers/signing/upgrade/rollback and native sessions.

Initial physical inventory remains ESP32-WROOM-32 38-pin, Uno R3, Nano revision
unconfirmed and ESP8266 model unconfirmed; no external programmer confirmed.
Pi 5 Ubuntu 24.04 ARM64 USB exists but replacement hardware testing is unverified.
Confirm exact Windows version/USB, Ubuntu AMD64 hardware and board identities when
those sessions become necessary; never infer current board/port from inventory.

## Work boundaries, tools and Git

No live .mcu_flasher_build_cache/.pio/.vscode/.clangd/.opencode or AI journal
mutations. No live settings/cloud/accounts/devices/tool stores from fixtures.
Use audit/migration/{scratch,logs,captures} for large ignored evidence. Compact
reports/skills are updated after actual code/check work. Keep user sketches and
restored root mcu_flash_gui.py unchanged. Unrelated dirty catalog/bootstrap JSON
and installer EXE/MSI materializations must remain unstaged and untouched.

Baseline source-inventory.json SHA256 is still
70072652a96ad7a51f7134d7f10ee3ff3314947a1816842d9ef2bbe60d6e8d52.
The preserved before/after hashes for launcher, inventory and unrelated dirty
inputs are in ignored continuation scratch. Existing reference amendments are
separate; no baseline hash was rewritten to conceal changes.

Rust tools are already installed: 18 selected Rust skills, Superpowers 6.4.2,
official Context7 plugin 1.0.2, RustAnalyzer/rust-src 1.99.1 and pinned experimental
LSP 21a1f9b. LSP tools are loaded and workspace-symbol queries succeeded against
trusted desktop code. Read-only queries can run trusted build scripts/proc macros;
no upstream path fence exists. Do not rerun unpinned installers. Context7 queried
Serde's official enum/container documentation during strict protocol review.
Subagents helped implement/review isolated slices; two later turns hit an account
usage limit, so the parent finished probe/integration/provenance work directly.
Do not treat a failed subagent turn as proof its remaining work was completed.

Safe feature-branch pushes remain authorized; never force-push main. Remove
index.lock only when empty, unchanged, ownerless via fuser and older than 60s.
The recurring empty stale lock was removed only under those conditions. Hooks
post-commit/pre-push normally lack owner execute: restore that bit temporarily
around commit/push and restore original modes in finally. Add .migration-tools/bin
to PATH for git-lfs. Preserve unrelated work; do not reset/clean the checkout.
