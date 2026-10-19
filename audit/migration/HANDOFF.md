# Restart handoff

Updated: 19 October 2026, Asia/Manila.

## Objective and checkpoint

Continue the full, reuse-first Rust/Tauri 2 migration with actual code and bounded
subagent ownership. The user reiterated “Continue all and everything according to
plan.” Full current feature/board/framework/programmer parity is required before
Python is replaced; private Python ultimately remains only for approved firmware
tools. Support Ubuntu 24.04+ AMD64/ARM64 and Windows 10/11 AMD64. Preserve current
compact workflow/static glass, offline Monaco/xterm, fixed Firebase deployment,
prepared offline tools, independent projects and editor/terminal/AI state.

Latest code checkpoint is `0cc3ec56d0fbacbda8182c87f045e01b4b511a12`, safely pushed on
`codex/rust-tauri-migration`. Documentation receipts may advance HEAD without
changing tested source. Original reference is
`1d00ff0bfdac7d158dde3df84b07741c5b1df6d4`. Python remains production; no service
is retired. **This is still an incomplete fixture prototype, not a public release.**

Read STATUS.md, parity.json/parity.md, verification.json, ci-results.json,
reference-amendments.json, reuse.md, xterm-provenance.json and desktop/README.md.
The 67-capability ledger now has 14 partial fixture subsets and 53 pending. There
is no retirement approval. Do not restart, reduce scope to implemented probes or
physical boards, regenerate baseline hashes, or publish an incomplete replacement.

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
feasibility only; editor-only detach and full native input remain unfinished.

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

All these matrices are complete and successful, with each actual source retained:

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
