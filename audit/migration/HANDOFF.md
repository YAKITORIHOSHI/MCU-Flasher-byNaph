# Restart handoff

Updated: 19 October 2026, Asia/Manila.

## Objective and current checkpoint

Continue the authorized reuse-first Rust + Tauri 2 migration. Full current
feature, board, framework and programmer parity is required before replacing
Python. Python remains only for approved firmware tools at the endpoint.
Support Ubuntu 24.04+ AMD64/ARM64 and Windows 10/11 AMD64. Preserve the current
workflow/glass style, existing offline Monaco/xterm/assets, fixed Firebase
accounts/schema and prepared offline tools. Prioritize Windows installer and
Ubuntu DEB. Do not narrow support to the initial physical boards.

Current code is `4cf730baf4db3399e16017bbb17abbb2419b126b` on `codex/rust-tauri-migration`.
Original application reference is `1d00ff0bfdac7d158dde3df84b07741c5b1df6d4`.
Read STATUS.md, parity.json/parity.md, verification.json, ci-results.json and
reference-amendments.json. No Python service has been retired; this is an
incomplete fixture prototype, not the finished replacement. No live project or
settings writes are enabled.

The user requests actual code work first, subagents with separate ownership,
and durable evidence under audit/, outside temp/. Update Audit/Skills at the
end of each actual code/check stage unless needed earlier.

## Implemented code to preserve

- Owner-scoped contracts and disposable project manifests/read/save/scaffolds;
  SHA/revision checks, unchanged-write avoidance, root sources, original checked
  header includes and BOM/original unchanged or undone mixed-EOL bytes.
- Retained Monaco models and record-bound save baselines; stale saves/reads and
  failed preflights do not replace unrelated buffers/history. Editor-only detach,
  full project/Modify/search/AI/syntax/recovery services remain incomplete.
- Pure serial text, preference merge, resource budgets and four transport helper
  ports compared with the original Python functions, retaining dynamic targets.
- Fixed child PTY with ConPTY cursor replies, bounded drain/cleanup/lease,
  native owner/sequence/raw-byte consumption acknowledgements and bounded queues.
  These are not general interactive shell/device sessions.
- Whole-webview transfer feasibility, three cycles retaining model/view/history;
  unstable Tauri API, not finished editor-only detach or native input proof.
- Native work-area fitting uses logical sizing once. Bundled local resources,
  CSP and explicit narrow IPC keep prototype writes in disposable audit scratch.
- Pure legacy PID/create_time ownership policy plus explicit native process
  evidence on Linux/Windows; unknown/denied/zombie/raced identities reserve claims.
  Only confirmed Exited frees a peer. Legacy identity tolerance is strictly <2s.
- Fixed fixture settings transactions acquire interoperable OS locks, freshly
  read and gather evidence under lock, preserve peers, refuse collisions, skip
  equal writes, stage/flush/sync and byte-check before replacement. Only one
  independent workspace per process is represented by the legacy PID schema.
- Registry unknown values use private validated JSON fragments and composition;
  huge integers, timestamp decimals and nested private-looking marker objects
  remain intact. Known identity parsing uses float_roundtrip. The host adds no
  raw_value feature, but Tauri enables it transitively: do not pass unknown
  settings through generic serde_json::Value. No global arbitrary_precision.
- Real legacy fixes: DialogFit stops unchanged Configure geometry/minimum writes;
  hiding a focused AI bar tries Monaco, then visible Review toggle, then blur,
  before hidden state. Unrelated focus and buffers remain intact.
- Native editor verifier uses a fully contained child pane and actual Qt/CSS/zoom
  geometry with both breakpoint regimes and hit tests. Capture paths allow only
  checkout temp/ or audit/migration/captures/, with resolved escape rejection.

## Verification and toolchains

```sh
bash desktop/scripts/verify-local.sh > audit/migration/logs/verification-latest.log 2>&1
bash desktop/scripts/run-prototype.sh
```

On Windows use desktop/scripts/verify-local.ps1 and run-prototype.ps1.
Pinned Rust 1.99.1, Node 24, pnpm 11.25.0; Python 3.10+ and psutil for reference
probes. Source desktop/dev-env.sh for individual commands. It selects ignored
checkout-local tools and the owned desktop/target, preventing stale inherited
binaries. No system profile changes or root package installation occurred.

Local latest counts: 90 Rust tests, 40 frontend tests, 46 native webview checks,
23,909 pure reference inputs (serial 325 / preferences 1,335 / scaffold 16 / budget 8,960 /
transport 13,039 / ownership 234), 8 lock, 6 process and 9 registration checks. The
ownership inputs include seven deliberate refusals; transport separately counts
51,993 equivalent comparisons and 163 explicit structural refusals. Strict
Clippy and Python warnings-as-errors pass. The registry has 21 Rust regressions.

The private .venv-linux runtime is available (Python 3.14.4 / PySide6 6.12.0).
Fresh legacy runtime 46 plus real Monaco tab/detach/recovery/cloud-Pull smoke pass.
Offscreen Qt emits unsupported-platform/Vulkan diagnostics, not deprecation
failures. Downloader appearance 17 / performance 7 at 100% / 200%, custom/chooser 18 passes +
1 Windows-only skip per scale, board search 25, PTY lifecycle 19 and targeted geometry
checks also passed earlier; they are bounded-work fixtures, not FPS benchmarks.

Editor DOM tests 10/10 passed on the native display at 100%, and at 200% with
verified Mesa OpenGL llvmpipe. Qt still initialized NVIDIA Vulkan; do not claim
GPU-free execution. NVIDIA 200% attempts stalled/timed out and remain incomplete.
After the capture guard addition, all 12 tests pass at 100% / 200% on private Xvfb.
The 200% Xvfb run emitted Chromium shared-image diagnostics after successful
checks; no JS/Python warning or test failure was suppressed.

Xvfb and xserver-common were extracted from official Ubuntu packages under
.migration-tools/, not installed system-wide. The latest Tauri check uses its
private display and process-local Mesa environment. The owned server was tracked
in audit/migration/scratch/xvfb-state.json; inspect running status before reuse.
This is native layout/IPC fixture evidence, not physical GPU/performance proof.

## CI and exact source evidence

At `4cf730b`, all five migration jobs passed: Ubuntu 24.04 AMD64/ARM64 and
Windows Server 2022 native prototypes each passed 46 webview checks, and both
Windows editor binding ranges passed the 12-test suite at 100% and 200%.
The separate 14-job compatibility, four-job native-layout and native ARM64
workflows also passed at this exact revision. See the audit's `ci-results.json`
for job links and source-qualified results. Hosted runners do not qualify
Windows 10/11 desktops, physical Pi/USB, or the completed replacement.

```sh
gh run view 39179835907 --json status,conclusion,jobs
```

Historical failures and fixes remain in ci-results.json. d68 Windows native
passed, but its new editor jobs rejected the audit output path before tests.
4cf730b corrects that confined capture allowlist; all five current jobs pass.

## Next technical gates

1. Native directory/file handles and final replacement: root alias/final-rename
   races, Windows save semantics, ACL/xattr preservation and interrupted staging/
   durable crash recovery. Keep live writes disabled until qualified.
2. Agree shared package-event/store roots across copies. Integrate legacy/new
   registration/activation with one process per workspace initially; no shared-PID
   multiwindow schema is assumed solved by a separate lock. Port/job leases and
   application lifecycle still require coordinated ownership.
3. Complete behavior/failure/process-tree performance baselines across all 67
   capability entries (13 partial, 54 pending), then incremental services/screens.
   Preserve all dynamic boards/frameworks/programmers; exact toolchain/firmware
   logic and private Python hooks are behavioral references, not redesign targets.
4. Finish settings import/validation/rollback, host operation identities and
   cancellation, full interactive terminal input/paste/capability handling,
   editor-only detach/recovery and native input/monitor qualification.
5. Finish all project/Modify/AI/search/syntax, downloader/packages/setup/repair/
   maintenance, serial/compile/upload/reset and offline workflow parity.
6. Fixed-provider Firebase/cloud/vault/conflict/Pull rollback: no editable provider
   settings or plaintext credential fallback. Client config encryption does not
   authorize a database; qualify deployed rules/API restrictions separately.
7. Resolve exact original asset/package provenance and missing notices, produce
   installers, then complete required native desktop and hardware sessions.

Available initial boards: ESP32-WROOM-32 38-pin, Uno R3, Nano revision pending,
ESP8266 model pending; no external programmer confirmed. User has Pi 5 Ubuntu 24.04
ARM64 USB ports, but no replacement hardware session has run. Windows version/USB
and Ubuntu AMD64 test hardware require confirmation before physical sessions.
Never infer the currently selected/connected board from this inventory.

## Boundaries and Git

Never mutate live .mcu_flasher_build_cache/, .pio/, .vscode/, .clangd/, .opencode/,
AI edit journals, live settings/accounts/tool stores/devices from fixtures.
Audit logs/captures/scratch are ignored; compact reports and handoff are tracked.
Baseline source-inventory.json stays unchanged. Reference amendments explain2
production sources, 7 verifiers, 1 legacy CI and the added migration workflow; some
baseline checkout line endings differ from Git (including mixed/doubled CRLF).
All 22 staged resource sources / 25 copies remain unchanged.

Pre-existing installer LFS materializations, index_json/package_index.json and
src/dbs/bootstrap_config.json are unrelated; do not stage, restore or clean them.
The root launcher disappeared externally; user explicitly authorized restoration
of the committed file. It is restored byte-for-byte, with no pending deletion.

Remove .git/index.lock only if >60s old, empty, unchanged and fuser confirms no
owner. Hooks post-commit/pre-push are non-executable: temporarily add owner execute
around commit/push, restore modes in finally, use .migration-tools/bin/git-lfs.
Push this branch safely; do not force-update main or retire Python for fixtures.

Record exact source/check results and safe next steps before ending continuation.
