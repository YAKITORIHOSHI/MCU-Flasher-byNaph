# Migration status

Updated: 19 October 2026, Asia/Manila. Latest code checkpoint:
`f952325ca4acfdf044c17c7813bc36609821381c` on `codex/rust-tauri-migration`.
[Rust/Tauri checkpoint CI](https://github.com/YAKITORIHOSHI/MCU-Flasher-byNaph/actions/runs/39253280765).

**The full Rust/Tauri replacement remains unfinished. The Python application
remains the production release and behavioral reference.** No Python application
service has been retired; no live project/settings/cloud/hardware writes have
been enabled. The 67-capability ledger now contains **14 partial fixture subsets
and 53 pending capabilities**, with no retirement approval.

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
Baseline hashes were not regenerated. Reference amendments explain two
production sources, nine verifiers and two legacy CI files separately. All 22 reused
asset sources, restored root launcher and unrelated catalog/bootstrap/installer
inputs retain their continuation-start hashes. No live sketches, protected
caches/journals, settings, credentials, cloud or hardware were used by fixtures.
Read [HANDOFF.md](HANDOFF.md) for continuation commands and exact next gates.
